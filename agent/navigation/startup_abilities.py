"""Automatic wheel setup and idempotent activation of session-persistent abilities.

Supported persistent overworld abilities (effects survive ability switches and map
teleports until the game closes):
- 幸福雪未融 (绽若颂愿心): G summons Gingerbread companion; active when left+right
  secondary key badges replace the single centre badge.
- 化万相 (万相渡浮生): G toggles Bling/Starlight auto-absorb; active when the single
  centre secondary button shows the warm-gold SetSlotHighlight border ring.
- 栖林沐光 (花影心期): MouseRight activates Forest Light & insect attraction; active
  when the centre G secondary button is present (absent before activation). When the
  performance animation setting is on, the first press enters a looping pose and a
  second MouseRight press exits the loop and completes activation.
- 金鹰之眼 (蒙尘之罪): G toggles persistent invisibility (secondary button highlight
  ring) and MouseRight toggles Eagle Vision chest/collectable highlights (main button
  highlight ring). Missing abilities are assigned to slots 7..1 before slot 8 so they
  do not collide with transient daily tasks.

Highlight-ring recognition rationale and limits:
SetSlotHighlight(true) renders an additive warm-gold circular border stroke at fixed
720p radii (27.9 px on 60x60 AreaAbilityButton, 19.2 px on 40x40 AreaSubAbilityButton).
World surfaces behind translucent HUD buttons vary smoothly across the 4 px step across
the border or cover only part of the circle, whereas the UI highlight forms a 4-quadrant
concentric brightness ridge above both the button's inner dark disc and the surrounding
ring, with warm gold chroma (10 <= H <= 42, S >= 30, R > B + 15). Decisions require
CONFIRM_FRAMES consecutive agreeing frames on the settled main HUD; non-default HUD
scales fail closed rather than toggling an already-active ability off.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import cv2
import numpy as np
from task_control import task_time as time
from vision import read_image

from .ability import (
    Ability,
    HUD_WHITE,
    SLOTS,
    WHITE,
    find_ability_in_list,
    open_slot_ability_list,
)
from .gingerbread_companion import (
    BADGE_DY,
    CONFIRM_FRAMES,
    POSITION_TOLERANCE,
    badge_centres,
    companion_state,
    main_centre,
    observe as observe_gingerbread,
)
from .models import NavigationError

EAGLE_WHITE = ([0, 0, 220], [180, 72, 255])
EAGLE_HUD_WHITE = ([0, 0, 195], [180, 72, 255])
MAIN_RING_RADIUS = 27.9
SUB_RING_RADIUS = 19.2
PREFERRED_SLOTS = (7, 6, 5, 4, 3, 2, 1, 8)


@dataclass(frozen=True)
class WheelGroupSpec:
    key: str
    label: str
    suit: str
    icon: str
    wheel_color: tuple[list[int], list[int]] = WHITE
    hud_color: tuple[list[int], list[int]] = HUD_WHITE


@dataclass(frozen=True)
class PersistentToggleSpec:
    key: str
    label: str
    group_key: str
    trigger_key: str


GROUPS: dict[str, WheelGroupSpec] = {
    "gingerbread": WheelGroupSpec("gingerbread", "幸福雪未融", "绽若颂愿心", "IconAbilityGingerbread"),
    "mask": WheelGroupSpec("mask", "化万相", "万相渡浮生", "IconAbilityMask"),
    "forest_light": WheelGroupSpec("forest_light", "栖林沐光", "花影心期", "IconAbilityForestLight"),
    "eagle_eye": WheelGroupSpec(
        "eagle_eye", "金鹰之眼", "蒙尘之罪", "IconAbilityEagleEye", EAGLE_WHITE, EAGLE_HUD_WHITE
    ),
}

TOGGLE_ORDER: tuple[PersistentToggleSpec, ...] = (
    PersistentToggleSpec("gingerbread", "幸福雪未融·小姜饼人随行", "gingerbread", "ability_derive_1"),
    PersistentToggleSpec("mask_absorb", "化万相·自动拾取噗灵与星光", "mask", "ability_derive_1"),
    PersistentToggleSpec("forest_light", "栖林沐光·生息流光与聚虫", "forest_light", "ability_use"),
    PersistentToggleSpec("eagle_invis", "金鹰之眼·常驻隐匿", "eagle_eye", "ability_derive_1"),
    PersistentToggleSpec("eagle_vision", "金鹰之眼·金鹰视野", "eagle_eye", "ability_use"),
)


def _color_mask(bgr: np.ndarray, bounds: tuple[list[int], list[int]]) -> np.ndarray:
    return cv2.inRange(
        cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV),
        np.array(bounds[0], dtype=np.uint8),
        np.array(bounds[1], dtype=np.uint8),
    )


def hud_ability_matches(runtime, group: WheelGroupSpec, frame=None) -> bool:
    """Recognize whether ``group`` is currently equipped on the main HUD button."""
    data = runtime.ui.asset_definition(group.icon)
    from daily.ui import catalog
    template_rel = data.get("template") or catalog(runtime.ui.resource)["assets"][group.icon]["template"]
    if isinstance(template_rel, list):
        template_rel = template_rel[0] if template_rel else ""
    if not template_rel:
        return False
    path = runtime.ui.asset_path(template_rel)
    image = read_image(path)
    target_frame = runtime.capture() if frame is None else frame
    if image is None or target_frame is None:
        return False
    x, y, w, h = runtime.ui.roi("AreaAbilityButton")
    scale = 0.73 * 2 / 3
    tmpl = cv2.resize(_color_mask(image, group.wheel_color), None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)
    patch = _color_mask(target_frame[y:y + h, x:x + w], group.hud_color)
    if patch.shape[0] < tmpl.shape[0] or patch.shape[1] < tmpl.shape[1]:
        return False
    bx, by, bw, bh = cv2.boundingRect(tmpl)
    if not bw or not bh:
        return False
    score = float(cv2.matchTemplate(patch, tmpl[by:by + bh, bx:bx + bw], cv2.TM_CCORR_NORMED).max())
    return score > 0.78


def slot_highlighted(frame, roi: list[int] | tuple[int, int, int, int], radius: float) -> tuple[bool, dict]:
    """Detect the 4-quadrant warm-gold SetSlotHighlight ring around a HUD ability button."""
    if frame is None:
        return False, {}
    x0, y0, w, h = roi
    margin = 6
    fx0, fy0 = max(0, x0 - margin), max(0, y0 - margin)
    fx1, fy1 = min(frame.shape[1], x0 + w + margin), min(frame.shape[0], y0 + h + margin)
    patch = frame[fy0:fy1, fx0:fx1]
    if patch.size == 0:
        return False, {}
    cx = (x0 - fx0) + (w - 1) / 2.0
    cy = (y0 - fy0) + (h - 1) / 2.0
    ph, pw = patch.shape[:2]
    yy, xx = np.mgrid[0:ph, 0:pw]
    dx, dy = xx - cx, yy - cy
    rr = np.hypot(dx, dy)
    valid = ~((dy > radius * 0.5) & (np.abs(dx) <= 11))
    ring = (np.abs(rr - radius) <= 1.4) & valid
    inner = (np.abs(rr - (radius - 4.2)) <= 1.4) & valid
    outer = (np.abs(rr - (radius + 4.2)) <= 1.4) & valid
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV).astype(np.float32)
    bgr = patch.astype(np.float32)
    v, s, h_ch = hsv[:, :, 2], hsv[:, :, 1], hsv[:, :, 0]
    quads = ((dx < 0) & (dy <= 0), (dx >= 0) & (dy <= 0), (dx < 0) & (dy > 0), (dx >= 0) & (dy > 0))
    dv_in, ridges, golds = [], [], []
    for q in quads:
        rm, im, om = ring & q, inner & q, outer & q
        if int(rm.sum()) < 4 or int(im.sum()) < 4:
            return False, {}
        dv_in.append(float(v[rm].mean() - v[im].mean()))
        ref = 0.5 * (v[im].mean() + v[om].mean()) if int(om.sum()) >= 4 else float(v[im].mean())
        ridges.append(float(v[rm].mean() - ref))
        warm = (
            (h_ch[rm] >= 10)
            & (h_ch[rm] <= 42)
            & (s[rm] >= 30)
            & (v[rm] >= 145)
            & (bgr[:, :, 2][rm] > bgr[:, :, 0][rm] + 15)
        )
        golds.append(float(warm.mean()))
    mean_in, min_in = float(np.mean(dv_in)), float(np.min(dv_in))
    mean_ridge, min_ridge = float(np.mean(ridges)), float(np.min(ridges))
    mean_gold = float(np.mean(golds))
    active = (
        mean_in >= 35.0
        and min_in >= 22.0
        and mean_ridge >= 20.0
        and min_ridge >= 10.0
        and mean_gold >= 0.45
    )
    return active, {
        "dv_in": round(mean_in, 1),
        "min_in": round(min_in, 1),
        "ridge": round(mean_ridge, 1),
        "min_ridge": round(min_ridge, 1),
        "gold": round(mean_gold, 2),
    }


def _slot_area(slot: int) -> list[int]:
    x, y = SLOTS[slot - 1]
    return [round((x - 50) * 2 / 3), round((y - 50) * 2 / 3), 67, 67]


def _slot_matches(runtime, group: WheelGroupSpec, slot: int, *, frame) -> bool:
    if frame is None:
        return False
    return bool(
        runtime.ui.asset(
            group.icon,
            image=frame,
            roi=_slot_area(slot),
            scale=2 / 3,
            color=group.wheel_color,
            threshold=0.78,
            method=cv2.TM_CCORR_NORMED,
        )
    )


def _find_group_slot(runtime, group: WheelGroupSpec, frame) -> int | None:
    if frame is None:
        return None
    for index in range(1, len(SLOTS) + 1):
        if _slot_matches(runtime, group, index, frame=frame):
            return index
    return None


def _configure_wheel_groups(runtime, inputs, event, groups: list[WheelGroupSpec], plan: int) -> dict[str, int]:
    """Ensure all ``groups`` are present on the active ability wheel without slot conflicts.

    Returns ``{group.key: slot}`` (slot ``0`` means single group already equipped on HUD).
    Missing abilities that are not unlocked on the current account are logged and omitted.
    """
    if not groups:
        return {}
    if type(plan) is not int or plan not in (1, 2, 3):
        raise NavigationError("常驻能力方案设置无效，请选择方案一、二或三。")
    if not runtime.main():
        raise NavigationError("未能回到游戏主界面，尚未开启常驻能力。")
    if len(groups) == 1 and hud_ability_matches(runtime, groups[0], runtime.capture()):
        return {groups[0].key: 0}

    ui = runtime.ui
    if not ui.ability_page():
        raise NavigationError("未能进入能力配置页面，尚未开启常驻能力。")
    ability_feature = ui.feature(asset="IconAbilityFeature", threshold=0.99, method=cv2.TM_CCORR_NORMED)
    if not ui.wait_page(ability_feature):
        raise NavigationError("未能确认能力配置页面，未更改能力轮盘。")

    frame = runtime.capture()
    if frame is None:
        raise NavigationError("无法读取能力轮盘，未更改能力配置。")
    slot_by_group: dict[str, int] = {}
    for group in groups:
        found = _find_group_slot(runtime, group, frame)
        if found is not None:
            slot_by_group[group.key] = found

    if len(slot_by_group) < len(groups):
        actions_shim = SimpleNamespace(rt=runtime, inputs=inputs, event=event, ability=lambda _: False)
        plan_helper = Ability(actions_shim, "常驻能力", groups[0].icon, "", "persistent", "persistent")
        if not plan_helper.change_plan(plan):
            raise NavigationError("未能切换到允许修改的能力方案，未更改能力轮盘。")
        if not plan_helper.plan_selected():
            raise NavigationError("未能确认允许修改的能力方案，未更改能力轮盘。")

        def permitted_plan() -> bool:
            return bool(ability_feature() and plan_helper.plan_selected())

        frame = runtime.capture()
        if frame is None:
            raise NavigationError("无法读取所选方案的能力轮盘，未更改能力配置。")
        slot_by_group = {}
        for group in groups:
            found = _find_group_slot(runtime, group, frame)
            if found is not None:
                slot_by_group[group.key] = found

        reserved = set(slot_by_group.values())
        candidates = [s for s in PREFERRED_SLOTS if s not in reserved]
        list_roi = ui.roi("AreaAbilityChange")
        saved_on_plan = False

        for group in groups:
            if group.key in slot_by_group:
                continue
            if not candidates:
                raise NavigationError("能力轮盘已无可用槽位，无法继续装入常驻能力。")
            target_slot = candidates[0]
            if not open_slot_ability_list(runtime, target_slot, permitted_plan, list_roi):
                raise NavigationError(f"未能打开能力槽位 {target_slot} 的更换列表。")
            event({"type": "ability_slot_requested", "ability": group.key, "plan": plan, "slot": target_slot})
            if not find_ability_in_list(
                runtime,
                group.icon,
                list_roi,
                primary_color=group.wheel_color,
                fallback_color=group.hud_color,
            ):
                runtime.log(f"能力列表中未识别到「{group.label}」（套装：{group.suit}），当前账号可能尚未解锁，已跳过。")
                event({"type": "ability_missing_skipped", "ability": group.key, "suit": group.suit})
                continue

            if not ui.wait_page(lambda g=group, s=target_slot: permitted_plan() and _slot_matches(runtime, g, s, frame=runtime.frame())):
                raise NavigationError(f"未能确认「{group.label}」已装入槽位 {target_slot}，尚未保存搭配。")
            if not ui.click_asset("ButtonAbilitySave"):
                raise NavigationError(f"未能点击保存按钮保存「{group.label}」能力配置。")
            if not ui.wait_page(lambda g=group, s=target_slot: permitted_plan() and _slot_matches(runtime, g, s, frame=runtime.frame())):
                raise NavigationError(f"保存后未能确认所选方案已装备「{group.label}」。")
            saved_on_plan = True
            slot_by_group[group.key] = target_slot
            candidates.pop(0)
            event({"type": "ability_configured", "ability": group.key, "plan": plan, "slot": target_slot})

        # Switching the dropdown in the ability editor only previews that plan;
        # click Save even when all requested abilities were already equipped on
        # that plan tab so the game commits it to the overworld wheel.
        if slot_by_group and not saved_on_plan:
            sample_key, sample_slot = next(iter(slot_by_group.items()))
            sample_group = GROUPS[sample_key]
            if not ui.click_asset("ButtonAbilitySave"):
                raise NavigationError(f"未能点击保存按钮启用能力方案 {plan}。")
            if not ui.wait_page(lambda: permitted_plan() and _slot_matches(runtime, sample_group, sample_slot, frame=runtime.frame())):
                raise NavigationError(f"保存后未能确认已启用能力方案 {plan}。")
            event({"type": "ability_plan_applied", "plan": plan})

    if not runtime.main():
        raise NavigationError("完成能力轮盘检查后未能返回游戏主界面。")
    return slot_by_group


def _select_equipped_group(runtime, group: WheelGroupSpec, slot: int, event) -> None:
    if not runtime.main():
        raise NavigationError(f"切换「{group.label}」前未能回到游戏主界面。")
    if hud_ability_matches(runtime, group, runtime.capture()):
        return
    if slot <= 0:
        raise NavigationError(f"未能确认当前已装备「{group.label}」能力图标。")
    if not runtime.key(f"ability_{slot}", 0.2):
        raise NavigationError(f"未能按下切换能力快捷键 {slot}。")
    for _ in range(6):
        if not runtime.pause(0.45):
            break
        if hud_ability_matches(runtime, group, runtime.capture()):
            event({"type": "ability_selected", "ability": group.key, "slot": slot})
            return
    raise NavigationError(f"已按下槽位 {slot} 快捷键，但未能确认切换出「{group.label}」图标。")


def _observe_toggle(runtime, classify, seconds: float, *, want: str | None = None) -> tuple[str, dict]:
    deadline = time.monotonic() + seconds
    last, streak, meta = None, 0, {}
    while not runtime.stopped:
        frame = runtime.capture()
        state, meta = classify(frame)
        streak = streak + 1 if state == last else 1
        last = state
        if state != "unknown" and streak >= CONFIRM_FRAMES and (want is None or state == want):
            return state, meta
        if time.monotonic() >= deadline or not runtime.pause(0.25):
            break
    confirmed = last if last and last != "unknown" and streak >= CONFIRM_FRAMES else "unknown"
    return confirmed, meta


def _activate_gingerbread(runtime, event) -> None:
    centre = main_centre(runtime)
    state, badges = observe_gingerbread(runtime, centre, 3.0)
    event({"type": "persistent_ability", "ability": "gingerbread", "phase": "check", "state": state})
    if state == "out":
        runtime.log("「幸福雪未融」小姜饼人已在身边，无需重复召唤。")
        return
    if state != "in":
        raise NavigationError("右下角未能确认「幸福雪未融」衍生召唤按钮，请检查画面是否被遮挡。")
    for attempt in (1, 2, 3):
        if not runtime.key("ability_derive_1", 0.3):
            raise NavigationError("未能按下「能力衍生功能1」，请检查按键设置。")
        # Wait past the ~3.8-4.5s bell summon animation before treating "in" as swallowed.
        state, badges = observe_gingerbread(runtime, centre, 8.0, want="out", abort_on_steady="in")
        event({"type": "persistent_ability", "ability": "gingerbread", "phase": "summon", "attempt": attempt, "state": state})
        if state == "out":
            runtime.log("已开启「幸福雪未融」小姜饼人随行。")
            return
        if state != "in":
            break
    raise NavigationError("已按下召唤键，但未能确认小姜饼人出现。")


def _activate_highlighted_slot(
    runtime,
    group: WheelGroupSpec,
    toggle: PersistentToggleSpec,
    *,
    sub_slot: bool,
    wait_seconds: float,
    event,
) -> None:
    centre = main_centre(runtime)
    roi = runtime.ui.roi("AreaSubAbilityButton" if sub_slot else "AreaAbilityButton")
    radius = SUB_RING_RADIUS if sub_slot else MAIN_RING_RADIUS

    def classify(frame):
        if frame is None or not runtime.hit("MaaNikki_MainDetected", image=frame):
            return "unknown", {"main": False}
        if not hud_ability_matches(runtime, group, frame):
            return "unknown", {"icon": False}
        if sub_slot:
            b_state, _ = companion_state(frame, centre)
            if b_state != "in":
                return "unknown", {"badge_state": b_state}
        active, metrics = slot_highlighted(frame, roi, radius)
        return ("active" if active else "inactive"), metrics

    state, metrics = _observe_toggle(runtime, classify, 3.0)
    event({"type": "persistent_ability", "ability": toggle.key, "phase": "check", "state": state, **metrics})
    if state == "active":
        runtime.log(f"「{toggle.label}」已处于开启状态，跳过按键。")
        return
    if state != "inactive":
        raise NavigationError(f"未能确认「{toggle.label}」按钮状态，为防误关闭未执行按键。")

    for attempt in (1, 2):
        if not runtime.key(toggle.trigger_key, 0.3):
            raise NavigationError(f"未能按下「{toggle.label}」触发按键。")
        state, metrics = _observe_toggle(runtime, classify, wait_seconds, want="active")
        event({"type": "persistent_ability", "ability": toggle.key, "phase": "activate", "attempt": attempt, "state": state, **metrics})
        if state == "active":
            runtime.log(f"已开启「{toggle.label}」。")
            return
        if not runtime.main():
            raise NavigationError(f"开启「{toggle.label}」后未能回到游戏主界面。")
        recheck, _ = _observe_toggle(runtime, classify, 2.0)
        if recheck == "active":
            runtime.log(f"已开启「{toggle.label}」。")
            return
        if recheck != "inactive":
            break
    raise NavigationError(f"已按下触发键，但未能确认「{toggle.label}」进入开启状态。")


def _activate_forest_light(runtime, group: WheelGroupSpec, event) -> None:
    centre = main_centre(runtime)
    cx, cy = centre
    row = cy + BADGE_DY
    tx, ty = POSITION_TOLERANCE

    def classify(frame):
        if frame is None or not runtime.hit("MaaNikki_MainDetected", image=frame):
            return "unknown", {"main": False}
        if not hud_ability_matches(runtime, group, frame):
            return "unknown", {"icon": False}
        badges = badge_centres(frame, centre)
        near_row = [(round(x, 1), round(y, 1)) for x, y in badges if abs(y - row) <= ty + 2]
        if any(abs(x - cx) <= tx and abs(y - row) <= ty for x, y in badges) and len(near_row) == 1:
            return "active", {"badges": near_row}
        if not near_row:
            return "inactive", {"badges": []}
        return "unknown", {"badges": near_row}

    state, meta = _observe_toggle(runtime, classify, 3.0)
    event({"type": "persistent_ability", "ability": "forest_light", "phase": "check", "state": state, **meta})
    if state == "active":
        runtime.log("「栖林沐光·生息流光与聚虫」已处于开启状态，跳过按键。")
        return
    if state != "inactive":
        raise NavigationError("未能确认「栖林沐光」当前开关状态，为防误触未执行按键。")

    if not runtime.key("ability_use", 0.3):
        raise NavigationError("未能按下「栖林沐光」主能力按键。")
    pressed_at = time.monotonic()
    last_exit_press = 0.0
    exit_presses = 0
    deadline = pressed_at + 15.0
    streak = 0
    while not runtime.stopped and time.monotonic() < deadline:
        frame = runtime.capture()
        if frame is None:
            break
        if runtime.hit("MaaNikki_MainDetected", image=frame):
            st, meta = classify(frame)
            if st == "active":
                streak += 1
                if streak >= CONFIRM_FRAMES:
                    event({"type": "persistent_ability", "ability": "forest_light", "phase": "activate", "state": "active", "exit_presses": exit_presses})
                    runtime.log("已开启「栖林沐光·生息流光与聚虫」。")
                    return
            else:
                streak = 0
        else:
            streak = 0
            now = time.monotonic()
            # When the performance animation setting is enabled, the first MouseRight
            # enters a looping pose; once LoopStart enables slot input (~2.2s), a
            # second MouseRight exits the loop and completes activation.
            if exit_presses < 3 and now - pressed_at >= 2.3 and now - last_exit_press >= 2.3:
                runtime.key("ability_use", 0.25)
                last_exit_press = time.monotonic()
                exit_presses += 1
        if not runtime.pause(0.25):
            break

    if not runtime.main():
        raise NavigationError("开启「栖林沐光」后未能返回游戏主界面。")
    state, meta = _observe_toggle(runtime, classify, 2.5, want="active")
    if state == "active":
        runtime.log("已开启「栖林沐光·生息流光与聚虫」。")
        return
    raise NavigationError("已触发「栖林沐光」，但未能确认衍生聚虫按钮出现。")


def _parse_plan(value, fallback: int = 3) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed in (1, 2, 3) else 0


def ensure_persistent_abilities(runtime, options: dict) -> bool:
    """Configure missing wheel slots and activate all selected persistent abilities."""
    raw_abilities = options.get("abilities")
    enabled_map = raw_abilities if isinstance(raw_abilities, dict) else {}
    selected = [spec for spec in TOGGLE_ORDER if enabled_map.get(spec.key) is True]
    if not selected:
        runtime.log("未勾选需要开启的常驻能力，请先在任务选项中勾选至少一项。")
        return False

    default_plan = _parse_plan(options.get("ability_plan", 3), fallback=0)
    per_ability_plan = options.get("per_ability_plan") is True
    raw_group_plans = options.get("group_plans")
    group_plan_map = raw_group_plans if isinstance(raw_group_plans, dict) else {}

    def plan_for_group(group_key: str) -> int:
        if per_ability_plan and group_key in group_plan_map:
            return _parse_plan(group_plan_map.get(group_key), fallback=0)
        return default_plan if default_plan in (1, 2, 3) else 3

    batches_by_plan: dict[int, list[PersistentToggleSpec]] = {}
    plan_order: list[int] = []
    for spec in selected:
        target_plan = plan_for_group(spec.group_key)
        if target_plan not in (1, 2, 3):
            raise NavigationError(f"「{GROUPS[spec.group_key].label}」的能力方案设置无效，请选择方案一、二或三。")
        if target_plan not in batches_by_plan:
            batches_by_plan[target_plan] = []
            plan_order.append(target_plan)
        batches_by_plan[target_plan].append(spec)

    events: list[dict] = []
    activated_count = 0
    any_slot_found = False
    with runtime.input_guard() as inputs:
        for batch_plan in plan_order:
            batch_specs = batches_by_plan[batch_plan]
            batch_groups: list[WheelGroupSpec] = []
            seen_groups: set[str] = set()
            for spec in batch_specs:
                if spec.group_key not in seen_groups:
                    seen_groups.add(spec.group_key)
                    batch_groups.append(GROUPS[spec.group_key])
            slot_by_group = _configure_wheel_groups(runtime, inputs, events.append, batch_groups, batch_plan)
            if slot_by_group:
                any_slot_found = True
            for spec in batch_specs:
                if spec.group_key not in slot_by_group:
                    continue
                group = GROUPS[spec.group_key]
                slot = slot_by_group[spec.group_key]
                _select_equipped_group(runtime, group, slot, events.append)
                if spec.key == "gingerbread":
                    _activate_gingerbread(runtime, events.append)
                elif spec.key == "forest_light":
                    _activate_forest_light(runtime, group, events.append)
                elif spec.key in ("mask_absorb", "eagle_invis"):
                    _activate_highlighted_slot(
                        runtime, group, spec, sub_slot=True, wait_seconds=10.0, event=events.append
                    )
                elif spec.key == "eagle_vision":
                    _activate_highlighted_slot(
                        runtime, group, spec, sub_slot=False, wait_seconds=12.0, event=events.append
                    )
                activated_count += 1

    if activated_count == 0 and not any_slot_found:
        runtime.log("所选常驻能力在当前账号均未识别到已解锁图标，本项结束。")
    return True
