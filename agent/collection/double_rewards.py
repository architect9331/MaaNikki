"""Priority scheduler for timed and welcome-back double-reward realm bonuses."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import math
import re

import cv2
import numpy as np

from account_state import AccountState, period_key
from action.blessing_pursuit import ORGANIZE_OPTIONS, blessing_focus, glow_shortfall, organization_issue
from action.weekly_challenge import TARGETS as WEEKLY_TARGETS, _record_weekly_state, read_count as read_weekly_count
from collection.backpack import ensure_backpack_scanned, renewal_source, wardrobe_snapshot
from collection.inventory_workflow import FiveStarInventory
from collection.material_pursuit import (
    ensure_wardrobe_synced_for_materials,
    execute_material_run,
    farm_common_mats_enabled,
    goal_has_jihua_potential,
    jihua_goal_deficits,
    load_catalogs,
    record_accrual,
    record_material_history,
)
from collection.organize import organize_inventory
from collection.pursuit import FiveStarPursuit
from collection.realm_goals import GOAL_LABELS, parse_realm_queue
from collection.scanner import RescanSlot
from collection.statistics import is_enabled as stats_enabled, upload
from daily import run_state
from daily.gameplay import Gameplay
from daily.runtime import Executors, parameters
from daily.settings import ROOT
from daily.ui import fields
from task_control import TaskStopped

# 720p crops for WBP_Challenge_Entrance_Item_V2.DoubleTag in RealmOverview.
OVERVIEW_BADGE_ROIS = {
    "jihua": [995, 472, 135, 55],
    "bless": [350, 472, 135, 55],
    "weekly": [1122, 218, 135, 55],
}

# 720p crops for WBP_Challenge_Stage_Main_V2_PC RightTop quota banner & button DoubleTag.
STAGE_QUOTA_ROI = [450, 12, 650, 56]
STAGE_ENERGY_ROI = [1100, 15, 150, 50]
STAGE_BUTTON_DOUBLE_ROI = [680, 580, 530, 75]

DOUBLE_EXEC_ORDER = ("weekly", "jihua", "bless")
JIHUA_LEVEL_IDS = {
    "闪亮泡泡": "50361309",
    "丝线": "3448636403",
    "纯真丝线": "3448636403",
    "噗灵": "992617511",
}

_PRIMARY_PATTERNS = {
    "jihua": (r"超级激化[^\d]{0,16}(\d+)\s*[/／]\s*(\d+)", "今日点数已用完"),
    "bless": (r"灵感迸发[^\d]{0,16}(\d+)\s*[/／]\s*(\d+)", "今日次数已用完"),
    "weekly": (r"深度突破[^\d]{0,16}(\d+)\s*[/／]\s*(\d+)", "本周次数已用完"),
}
_WB_PATTERNS = {
    "jihua": r"回流剩余双倍(?:活跃)?能量[^\d]{0,16}(\d+)\s*[/／]\s*(\d+)",
    "bless": r"回流剩余双倍(?:奖励)?次数[^\d]{0,16}(\d+)\s*[/／]\s*(\d+)",
    "weekly": r"回流剩余双倍(?:奖励)?次数[^\d]{0,16}(\d+)\s*[/／]\s*(\d+)",
}


@dataclass
class DoubleRunSummary:
    spent: bool = False
    last_kind: str | None = None
    collection_used: bool = False
    remaining_energy: int | None = None


def _load_double_config(rt) -> dict:
    context = rt.context
    opt = parameters(context, "MaaNikki_Double_Option")
    enabled = opt.get("enabled", True) is not False
    types = {
        kind
        for kind, suffix in (("jihua", "Jihua"), ("bless", "Bless"), ("weekly", "Weekly"))
        if (context.get_node_data(f"MaaNikki_Double_Type_{suffix}") or {}).get("enabled", False)
    }
    return {
        "enabled": enabled,
        "types": types,
        "jihua_mode": str(opt.get("jihua_mode", "follow_deficit")),
        "bless_mode": str(opt.get("bless_mode", "glow_pursuit")),
        "weekly_priority": str(opt.get("weekly_priority", "Qiggeda")),
    }

def _crop_upscaled(frame: np.ndarray, roi: list[int], scale: float = 3.0, pad: int = 12) -> np.ndarray | None:
    x, y, w, h = roi
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(frame.shape[1], x + w), min(frame.shape[0], y + h)
    if x1 <= x0 or y1 <= y0:
        return None
    patch = frame[y0:y1, x0:x1]
    up = cv2.resize(patch, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    return cv2.copyMakeBorder(up, pad, pad, pad, pad, cv2.BORDER_REPLICATE)


def _badge_present(rt, frame: np.ndarray | None, roi: list[int], *, ocr_only: bool = False) -> bool:
    if frame is None:
        return False
    padded = _crop_upscaled(frame, roi, scale=3.0, pad=12)
    if padded is None:
        return False
    res = rt.recognize(
        "MaaNikki_Daily_OCR",
        {"roi": [0, 0, padded.shape[1], padded.shape[0]], "expected": r"[234]\s*倍|双\s*倍"},
        image=padded,
    )
    if res and res.hit:
        return True
    if ocr_only:
        return False
    x, y, w, h = roi
    patch = frame[max(0, y):min(frame.shape[0], y + h), max(0, x):min(frame.shape[1], x + w)]
    if patch.size == 0:
        return False
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    # Match T_UI_physical_img_double salmon-orange pill with embedded bright white text.
    pill_bg = cv2.inRange(hsv, np.array([2, 75, 150], dtype=np.uint8), np.array([24, 255, 255], dtype=np.uint8))
    if cv2.countNonZero(pill_bg) / pill_bg.size < 0.16:
        return False
    white_text = cv2.inRange(hsv, np.array([0, 0, 210], dtype=np.uint8), np.array([180, 55, 255], dtype=np.uint8))
    return int(cv2.countNonZero(white_text)) >= 40


def _ocr_upscaled_line(rt, frame: np.ndarray | None, roi: list[int]) -> str:
    if frame is None:
        return ""
    padded = _crop_upscaled(frame, roi, scale=2.0, pad=10)
    if padded is not None:
        txt = rt.text([0, 0, padded.shape[1], padded.shape[0]], expected=".+", image=padded)
        if txt:
            return txt
    return rt.text(roi, expected=".+", image=frame) or ""


def _parse_stage_double_text(kind: str, text: str) -> tuple[int | None, bool]:
    """Return (remaining_quota_for_active_tier, has_secondary_welcomeback_after)."""
    p_regex, exhausted_kw = _PRIMARY_PATTERNS[kind]
    p_match = re.search(p_regex, text)
    primary_rem: int | None = None
    if p_match:
        cur, cap = map(int, p_match.groups())
        if 0 <= cur <= cap and cap > 0:
            primary_rem = cur
    elif exhausted_kw in text:
        primary_rem = 0

    wb_match = re.search(_WB_PATTERNS[kind], text)
    wb_rem: int | None = None
    if wb_match:
        cur, cap = map(int, wb_match.groups())
        if 0 <= cur <= cap and cap > 0:
            wb_rem = cur

    if primary_rem is not None and primary_rem > 0:
        return primary_rem, bool(wb_rem and wb_rem > 0)
    if wb_rem is not None:
        return wb_rem, False
    if primary_rem == 0:
        return 0, False
    return None, False


def _read_stage_energy(rt, frame: np.ndarray | None) -> int | None:
    if frame is None:
        return None
    raw = rt.text(STAGE_ENERGY_ROI, image=frame) or ""
    m = re.search(r"(\d+)\s*[/／]\s*(\d+)", raw)
    if not m:
        return None
    cur, total = map(int, m.groups())
    return cur if 0 <= cur <= 9999 and 0 < total <= 9999 else None


def _read_stage_double_quota(rt, kind: str) -> tuple[int | None, bool, int | None]:
    """Read (remaining_double_quota, has_more_wb, current_energy) on an active Stage page."""
    frame = None
    energy = None
    for attempt in range(2):
        if attempt > 0 and not rt.pause(0.35):
            break
        frame = rt.capture()
        if frame is None:
            break
        if energy is None:
            energy = _read_stage_energy(rt, frame)
        top_text = _ocr_upscaled_line(rt, frame, STAGE_QUOTA_ROI)
        quota, has_wb = _parse_stage_double_text(kind, top_text)
        if quota is not None:
            return quota, has_wb, energy
    if frame is not None and _badge_present(rt, frame, STAGE_BUTTON_DOUBLE_ROI, ocr_only=True):
        return (10 if kind == "jihua" else 1), False, energy
    return None, False, energy

def _run_double_weekly(
    rt,
    config: dict,
    energy: int | None,
    weekly_cal_count: tuple[int, int] | None,
) -> tuple[bool, int | None]:
    priority = config["weekly_priority"]
    targets = [
        (node, label)
        for node, label in WEEKLY_TARGETS
        if (rt.context.get_node_data(node) or {}).get("enabled", False)
    ]
    if not targets:
        rt.log("深度突破双倍：未勾选每周幻境目标，跳过。")
        return False, energy
    targets.sort(key=lambda item: 0 if item[0].rsplit("_", 1)[-1] == priority else 1)

    if not rt.ui.realm("weekly"):
        rt.log("深度突破双倍：未能进入心之突破幻境页面，跳过。")
        return False, energy
    quota, _, stage_energy = _read_stage_double_quota(rt, "weekly")
    if stage_energy is not None:
        energy = stage_energy
    if not quota or quota <= 0:
        rt.log("深度突破双倍：本周双倍次数已用完或未确认双倍标识，跳过。")
        return False, energy
    if energy is not None and energy < Gameplay.MIN_ENERGY["weekly"]:
        rt.log(f"深度突破双倍：当前活跃能量 {energy} 点，不足 {Gameplay.MIN_ENERGY['weekly']} 点，跳过。")
        return False, energy

    game = Gameplay(rt)
    done_runs = 0
    completed_targets: list[str] = []
    finished_now, total_now = weekly_cal_count if weekly_cal_count is not None else (0, 2)
    for node, label in targets:
        if rt.stopped or done_runs >= quota:
            break
        if energy is not None and energy < game.MIN_ENERGY["weekly"]:
            break
        rt.log(f"限时双倍插队 · 深度突破：按优先设置尝试挑战「{label}」。")
        ok = game.realm("weekly", maximize=False, entered=True, weekly_node=node, allow_exhausted=True)
        if not ok and game.level_missing:
            rt.log(f"深度突破双倍：未找到「{label}」，尝试下一个已勾选目标。")
            continue
        if game.energy_exhausted:
            break
        if ok:
            if game.weekly_quota_used:
                rt.log(f"深度突破双倍：「{label}」本周普通奖励次数已用完，尝试下一个已勾选目标。")
                continue
            completed_targets.append(node.rsplit("_", 1)[-1])
            done_runs += 1
            if weekly_cal_count is not None:
                finished_now = min(total_now, finished_now + 1)
            if energy is not None:
                energy = max(0, energy - game.MIN_ENERGY["weekly"])
            continue
        raise RuntimeError(f"深度突破双倍挑战「{label}」未确认完成")

    if weekly_cal_count is not None and completed_targets:
        _record_weekly_state(rt, finished_now, total_now, completed_targets)
    if done_runs == 0:
        rt.log("深度突破双倍：所选每周幻境目标本周已无剩余挑战次数或尚未解锁，跳过。")
    if not rt.stopped and not rt.ui.calendar():
        raise RuntimeError("深度突破双倍挑战后未能返回奇想日历")
    refreshed = Gameplay(rt).read_energy()
    return done_runs > 0, (refreshed if refreshed is not None else energy)

def _run_double_jihua(
    rt,
    account: AccountState,
    crafting: dict,
    scoring: dict,
    config: dict,
    queue_goals: list[str],
    energy: int | None,
) -> tuple[bool, int | None]:
    jihua_mode = config["jihua_mode"]
    jihua_goals = [goal for goal in queue_goals if goal in ("renewal", "crafting")]
    if jihua_mode == "follow_deficit" and not jihua_goals:
        rt.log("超级激化双倍（跟随缺口）：刷取顺序中未开启「焕新材料」或「制衣材料」，跳过本次双倍。")
        return False, energy

    any_spent = False
    goal_satisfied = False
    inventory: dict[str, int] | None = None
    wardrobe_synced = False
    include_common = farm_common_mats_enabled(rt)
    mat_defs = crafting.get("materials") or {}

    for pass_idx in range(2):
        if rt.stopped:
            break
        if not rt.ui.realm("jihua"):
            raise RuntimeError("未能进入素材激化幻境页面")
        rem_double_energy, has_wb, page_energy = _read_stage_double_quota(rt, "jihua")
        if page_energy is not None:
            energy = page_energy
        if not rem_double_energy or rem_double_energy < Gameplay.MIN_ENERGY["jihua"]:
            if pass_idx == 0:
                rt.log("超级激化双倍：今日双倍点数已用完或未确认双倍标识，跳过。")
            break
        if energy is None:
            if not rt.ui.calendar():
                raise RuntimeError("未能进入奇想日历读取活跃能量")
            energy = Gameplay(rt).read_energy()
            if energy is None:
                raise RuntimeError("无法读取当前活跃能量")
        if energy < Gameplay.MIN_ENERGY["jihua"]:
            rt.log(f"超级激化双倍：当前活跃能量 {energy} 点，不足 10 点，跳过。")
            break

        if jihua_mode == "inherit_fallback":
            target_label = str((fields(rt.context, "MaaNikki_Jihua_Target") or {}).get("expected") or "闪亮泡泡").strip()
            mid = JIHUA_LEVEL_IDS.get(target_label)
            meta = mat_defs.get(mid or "") or {}
            base_drop = int(meta.get("drop") or 0)
            max_afford = energy // 10
            max_double = rem_double_energy // 10
            runs = min(max_afford, max_double)
            if runs <= 0:
                break
            use_max = runs >= max_afford
            rt.log(f"限时双倍插队 · 超级激化（继承兜底）：目标「{target_label}」，挑战 {runs} 次（消耗 {runs * 10} 体力）。")
            _, energy_after = execute_material_run(rt, "jihua", target_label, runs, use_max)
            spent = max(0, energy - energy_after)
            if spent < 10:
                raise RuntimeError("超级激化双倍挑战未完成或未确认活跃能量消耗")
            any_spent = True
            if mid and base_drop > 0:
                double_part = min(spent, rem_double_energy)
                normal_part = max(0, spent - double_part)
                gained = (double_part // 10) * (base_drop * 2) + (normal_part // 10) * base_drop
                if gained > 0:
                    record_accrual(account, mid, gained)
                    record_material_history(account, crafting, scoring, include_common=include_common)
                    rt.log(f"限时双倍插队 · 超级激化：获得 {meta.get('name', target_label)} +{gained}。")
            energy = energy_after
        else:
            if not wardrobe_synced:
                wardrobe_synced = True
                role_now = account.load()
                if any(
                    g == "crafting" or (g == "renewal" and renewal_source(role_now.get("materials") or {}) == "plan")
                    for g in jihua_goals
                ):
                    ensure_wardrobe_synced_for_materials(rt, account)
            role_check = account.load()
            has_any_potential = any(
                goal_has_jihua_potential(goal, role_check, crafting, scoring, include_common=include_common)
                for goal in jihua_goals
            )
            if inventory is None and has_any_potential:
                inventory = ensure_backpack_scanned(rt, account, crafting)
                record_material_history(account, crafting, scoring, include_common=include_common)
            finished: set[str] = set()
            while rem_double_energy >= 10 and energy >= 10 and not rt.stopped:
                role = account.load()
                picked = None
                for goal in jihua_goals:
                    deficits = (
                        jihua_goal_deficits(
                            goal, role, inventory or {}, crafting, scoring, multiplier=2, include_common=include_common
                        )
                        if has_any_potential
                        else []
                    )
                    if deficits:
                        picked = (goal, deficits[0])
                        break
                    if goal not in finished:
                        finished.add(goal)
                        if goal == "renewal" and renewal_source(role.get("materials") or {}) == "targets":
                            record_accrual(account, mark_glow_completed=True)
                if picked is None:
                    goal_satisfied = True
                    mat_phrase = "泡泡、丝线与噗灵" if include_common else "闪亮泡泡"
                    rt.log(
                        f"超级激化双倍（跟随缺口）：所需{mat_phrase}已备齐，停止继续消耗双倍点数。"
                        if any_spent
                        else f"超级激化双倍（跟随缺口）：所选焕新／制衣目标的{mat_phrase}已备齐，跳过本次双倍。"
                    )
                    break
                goal, (_, _, mid, meta, deficit) = picked
                base_drop = int(meta["drop"])
                runs_needed = math.ceil(deficit / (base_drop * 2))
                max_afford = energy // 10
                max_double = rem_double_energy // 10
                runs = min(runs_needed, max_afford, max_double)
                use_max = runs >= max_afford
                rt.log(
                    f"限时双倍插队 · 超级激化（{GOAL_LABELS[goal]}）：{meta['name']} 还差 {deficit}，"
                    f"挑战「{meta['level']}」{runs} 次（消耗 {runs * 10} 体力）。"
                )
                _, energy_after = execute_material_run(rt, "jihua", meta["level"], runs, use_max)
                spent = max(0, energy - energy_after)
                if spent < 10:
                    raise RuntimeError("超级激化双倍挑战未完成或未确认活跃能量消耗")
                double_part = min(spent, rem_double_energy)
                normal_part = max(0, spent - double_part)
                gained = (double_part // 10) * (base_drop * 2) + (normal_part // 10) * base_drop
                if gained > 0:
                    inventory[mid] = int(inventory.get(mid, 0)) + gained
                    record_accrual(account, mid, gained)
                    record_material_history(account, crafting, scoring, include_common=include_common)
                    rt.log(f"限时双倍插队 · 超级激化：获得 {meta['name']} +{gained}（当前库存 {inventory[mid]}）。")
                rem_double_energy = max(0, rem_double_energy - spent)
                energy = energy_after
                any_spent = True

        if goal_satisfied or not has_wb or (energy is not None and energy < 10):
            break

    if not rt.stopped and not rt.ui.calendar():
        raise RuntimeError("超级激化双倍处理后未能返回奇想日历")
    return any_spent, energy

def _run_double_bless(
    rt,
    account: AccountState,
    config: dict,
    energy: int | None,
) -> tuple[bool, int | None, bool]:
    bless_mode = config["bless_mode"]
    any_spent = False
    collection_used = False
    organized_once = False
    goal_satisfied = False

    for pass_idx in range(2):
        if rt.stopped:
            break
        if not rt.ui.realm("bless"):
            raise RuntimeError("未能进入祝福闪光幻境页面")
        rem_runs, has_wb, page_energy = _read_stage_double_quota(rt, "bless")
        if page_energy is not None:
            energy = page_energy
        if not rem_runs or rem_runs <= 0:
            if pass_idx == 0:
                rt.log("灵感迸发双倍：今日双倍次数已用完或未确认双倍标识，跳过。")
            break
        if energy is None:
            if not rt.ui.calendar():
                raise RuntimeError("未能进入奇想日历读取活跃能量")
            energy = Gameplay(rt).read_energy()
            if energy is None:
                raise RuntimeError("无法读取当前活跃能量")
        if energy < Gameplay.MIN_ENERGY["bless"]:
            rt.log(f"灵感迸发双倍：当前活跃能量 {energy} 点，不足 40 点，跳过。")
            break

        if bless_mode == "inherit_fallback":
            target_label = str((fields(rt.context, "MaaNikki_Bless_Level") or {}).get("expected") or "巨蛇遗迹")
            cap = energy // 40
            runs = min(rem_runs, cap)
            if runs <= 0:
                break
            capped = None if runs >= cap else runs
            rt.log(f"限时双倍插队 · 灵感迸发（继承兜底）：目标「{target_label}」，挑战 {runs} 次（消耗 {runs * 40} 体力）。")
            clone = rt.context.clone()
            child = Executors(clone)
            try:
                ok = Gameplay(child).realm(
                    "bless",
                    maximize=(capped is None),
                    fixed_count=capped,
                    allow_exhausted=True,
                    return_page="calendar",
                )
            finally:
                rt.navigation_halted |= child.navigation_halted
            if not rt.stopped and not rt.ui.calendar():
                raise RuntimeError("灵感迸发双倍挑战后未能返回奇想日历")
            energy_after = Gameplay(rt).read_energy()
            if energy_after is not None and energy_after < energy:
                any_spent = True
                energy = energy_after
            elif not ok:
                raise RuntimeError("灵感迸发双倍挑战未确认完成")
        else:
            catalog = json.loads((ROOT / "resource/data/blessing_catalog.json").read_text(encoding="utf-8"))
            workflow = parameters(rt.context, "MaaNikki_Pursuit_WorkflowOption").get("mode", "managed")
            farm_only = workflow == "farm_only"
            raw_reserve = parameters(rt.context, "MaaNikki_Pursuit_ReserveBlingOption").get("amount", 0)
            try:
                reserve_bling = max(0, int(str(raw_reserve or 0).strip().replace(",", "")))
            except (ValueError, TypeError):
                reserve_bling = 0
            organize = parameters(rt.context, "MaaNikki_Pursuit_OrganizeOption").get("mode", "auto")
            if organize not in ORGANIZE_OPTIONS:
                raise ValueError("未知祝福闪光整理选项")
            issue = organization_issue(rt, catalog)
            if (not organized_once and organize != "auto") or issue is not None:
                rt.log("限时双倍插队 · 灵感迸发：正在核对并整理全部祝福闪光。", ui_text="双倍插队：整理祝福闪光仓库。")
                org_result = organize_inventory(
                    rt,
                    catalog,
                    allow_unlock=False if farm_only else bool(ORGANIZE_OPTIONS[organize]),
                    read_only=farm_only,
                )
                organized_once = True
                if not org_result["published"] and organization_issue(rt, catalog) is not None:
                    rt.log("灵感迸发双倍（五星追全）：仓库整理未完成且无可用记录，跳过本次双倍。")
                    break

            folder = ROOT / "logs/blessing-pursuit" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            folder.mkdir(parents=True, exist_ok=True)
            pursuit = FiveStarInventory(rt, catalog, folder)
            try:
                decompose = parameters(rt.context, "MaaNikki_Pursuit_DecomposeOption").get("enabled") is True
                share = stats_enabled(ROOT, parameters(rt.context, "MaaNikki_Pursuit_StatsOption").get("enabled") is not False)
                flow = FiveStarPursuit(
                    pursuit,
                    decompose_duplicates=decompose,
                    auto_bling=False,
                    chance_priority=True,
                    share_statistics=share,
                    farm_only=farm_only,
                    reserve_bling=reserve_bling,
                    focus_sets=blessing_focus(account.load()),
                    max_farm_runs=rem_runs,
                )
                if flow.target_complete():
                    goal_satisfied = True
                    rt.log("灵感迸发双倍（五星追全）：所选范围五星颜色已收齐，跳过本次双倍。")
                    break
                rt.log(f"限时双倍插队 · 灵感迸发（五星追全）：按当前缺色顺位挑战（本次双倍上限 {rem_runs} 次）。")
                flow.run()
                if any(op.get("kind") == "realm" and op.get("input_attempted") is not False
                       for op in pursuit.report["operations"]):
                    any_spent = True
                    collection_used = True
                if flow.target_complete():
                    goal_satisfied = True
            except RescanSlot as error:
                pursuit.record_interruption(error, reason="焦点变化使列表状态失效")
                raise RuntimeError("焦点变化使当前祝福闪光升级或列表状态失效") from error
            except Exception as error:
                pursuit.record_interruption(error, reason=str(error))
                raise
            finally:
                try:
                    pursuit.log_progress("双倍插队结束" if pursuit.report.get("complete") else "双倍插队阶段完成")
                    pursuit.record_history()
                    if pursuit.share_statistics:
                        upload(pursuit.store.account, ROOT, catalog_version=pursuit.catalog["version"])
                    if pursuit.prepared and "inventory" not in pursuit.report:
                        pursuit.checkpoint()
                    else:
                        pursuit.save()
                except (OSError, ValueError, KeyError, TypeError):
                    pass

            if not rt.stopped and not rt.ui.calendar():
                raise RuntimeError("灵感迸发双倍挑战后未能返回奇想日历")
            refreshed = Gameplay(rt).read_energy()
            if refreshed is not None:
                energy = refreshed

        if goal_satisfied or not has_wb or (energy is not None and energy < 40):
            break

    if not rt.stopped and not rt.ui.calendar():
        raise RuntimeError("灵感迸发双倍处理后未能返回奇想日历")
    return any_spent, energy, collection_used

def run_double_priority(rt) -> DoubleRunSummary:
    """Detect active 2x realm events and consume eligible bonuses before the normal queue."""
    summary = DoubleRunSummary()
    config = _load_double_config(rt)
    enabled_types = config["types"]
    if not config["enabled"] or not enabled_types:
        return summary

    queue_goals = parse_realm_queue(parameters(rt.context, "MaaNikki_RealmQueue").get("order"))
    organize = parameters(rt.context, "MaaNikki_Pursuit_OrganizeOption").get("mode", "auto")
    account = AccountState.for_runtime(ROOT, rt)
    role = account.load()
    crafting, scoring = load_catalogs()

    include_common = farm_common_mats_enabled(rt)
    candidates: set[str] = set()
    if "weekly" in enabled_types and any(
        (rt.context.get_node_data(node) or {}).get("enabled", False) for node, _ in WEEKLY_TARGETS
    ):
        candidates.add("weekly")
    if "jihua" in enabled_types:
        if config["jihua_mode"] == "inherit_fallback" or any(
            (not wardrobe_snapshot(role) and (goal == "crafting" or renewal_source(role.get("materials") or {}) == "plan"))
            or goal_has_jihua_potential(goal, role, crafting, scoring, include_common=include_common)
            for goal in queue_goals
            if goal in ("renewal", "crafting")
        ):
            candidates.add("jihua")
    if "bless" in enabled_types:
        if config["bless_mode"] == "inherit_fallback" or glow_shortfall(rt, role, organize):
            candidates.add("bless")

    if not candidates:
        rt.log("限时双倍插队：所勾选双倍活动的目标已备齐或尚未具备计算数据，跳过双倍检查。")
        return summary

    if not rt.ui.calendar():
        raise RuntimeError("未能进入奇想日历检查限时双倍活动")
    energy = Gameplay(rt).read_energy()
    if energy is None:
        raise RuntimeError("无法读取当前活跃能量")

    weekly_cal_count = None
    if "weekly" in candidates:
        weekly_cal_count = read_weekly_count(rt)
        if weekly_cal_count is None:
            image = rt.frame()
            if rt.hit("MaaNikki_CalendarReady", image=image) and rt.text(
                [860, 175, 290, 60], "尚未解锁|未解锁", image=image
            ):
                candidates.discard("weekly")
        elif weekly_cal_count[0] >= weekly_cal_count[1]:
            _record_weekly_state(rt, weekly_cal_count[0], weekly_cal_count[1])
            candidates.discard("weekly")

    candidates = {kind for kind in candidates if energy >= Gameplay.MIN_ENERGY[kind]}
    if not candidates:
        return summary

    if not rt.ui.realm_overview():
        raise RuntimeError("未能打开幻境挑战总览检查双倍标识")
    overview = rt.capture()
    active_kinds = [
        kind for kind in DOUBLE_EXEC_ORDER
        if kind in candidates and _badge_present(rt, overview, OVERVIEW_BADGE_ROIS[kind])
    ]
    if not active_kinds:
        rt.log("限时双倍插队：当前没有待刷的可参与双倍幻境，按刷取顺序继续。", detail=True)
        rt.ui.calendar()
        return summary

    labels = {"weekly": "深度突破", "jihua": "超级激化", "bless": "灵感迸发"}
    rt.log("限时双倍插队：检测到 " + "、".join(labels[k] for k in active_kinds) + " 双倍标识，优先核对并处理。")

    for kind in active_kinds:
        if rt.stopped:
            break
        if energy is not None and energy < Gameplay.MIN_ENERGY[kind]:
            continue
        spent_now = False
        if kind == "weekly":
            spent_now, energy = _run_double_weekly(rt, config, energy, weekly_cal_count)
        elif kind == "jihua":
            spent_now, energy = _run_double_jihua(rt, account, crafting, scoring, config, queue_goals, energy)
        elif kind == "bless":
            spent_now, energy, col_used = _run_double_bless(rt, account, config, energy)
            summary.collection_used |= col_used
        if spent_now:
            summary.spent = True
            summary.last_kind = kind
            run_state.update_realm(rt, kind=kind, collection=summary.collection_used)

    if not rt.stopped:
        rt.ui.calendar()
        refreshed = Gameplay(rt).read_energy()
        if refreshed is not None:
            energy = refreshed
    summary.remaining_energy = energy
    return summary
