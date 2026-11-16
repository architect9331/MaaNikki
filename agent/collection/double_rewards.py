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
from action.blessing_pursuit import ORGANIZE_OPTIONS, blessing_focus, blessing_mode, glow_shortfall, organization_issue
from action.weekly_challenge import TARGETS as WEEKLY_TARGETS, _record_weekly_state, read_count as read_weekly_count
from collection.backpack import ensure_backpack_scanned, renewal_source, wardrobe_snapshot
from collection.inventory_workflow import FiveStarInventory
from collection.material_pursuit import (
    effective_realm_runs,
    execute_material_run,
    goal_has_jihua_potential,
    goal_has_monster_potential,
    goal_missing_baseline,
    jihua_goal_deficits,
    load_catalogs,
    needed_material_map,
    realm_goal_deficits,
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
from daily.ui import expected_text, fields
from task_control import TaskStopped

# 720p crops for WBP_Challenge_Entrance_Item_V2.DoubleTag in RealmOverview.
OVERVIEW_BADGE_ROIS = {
    "jihua": [995, 472, 135, 55],
    "bless": [350, 472, 135, 55],
    "monster": [220, 212, 145, 60],
    "weekly": [1122, 218, 135, 55],
}

# 720p crops for WBP_Challenge_Stage_Main_V2_PC RightTop quota banner & button DoubleTag.
STAGE_QUOTA_ROI = [400, 12, 708, 56]
STAGE_ENERGY_ROI = [1100, 15, 150, 50]
STAGE_BUTTON_DOUBLE_ROI = [680, 580, 530, 75]

DOUBLE_EXEC_ORDER = ("weekly", "jihua", "bless", "monster")
JIHUA_LEVEL_IDS = {
    "闪亮泡泡": "50361309",
    "丝线": "3448636403",
    "纯真丝线": "3448636403",
    "噗灵": "992617511",
}

_PRIMARY_PATTERNS = {
    "jihua": (
        r"(?:今日剩[余餘]?高倍活跃能量|今日剩[余餘]?超级激化(?:活跃能量)?点数?|超级激化)[^\d]{0,16}(\d+)\s*[/／]\s*(\d+)",
        r"今日点数已用完|今日高倍活跃能量已用完|高倍活跃能量已用完",
    ),
    "bless": (
        r"(?:今日剩[余餘]?高倍(?:奖励)?次数|今日剩[余餘]?双倍(?:奖励)?次数|今日剩[余餘]?灵感迸发(?:奖励)?次数|今日剩[余餘]?次数|灵感迸发)[^\d]{0,16}(\d+)\s*[/／]\s*(\d+)",
        r"今日(?:高倍|双倍)?次数已用完",
    ),
    "monster": (
        r"(?:今日剩[余餘]?高倍(?:奖励)?次数|今日剩[余餘]?双倍(?:奖励)?次数|今日剩[余餘]?次数|魔物试炼|双倍|每日|限时)[^\d]{0,16}(\d+)\s*[/／]\s*(\d+)",
        r"今日(?:高倍|双倍)?次数已用完",
    ),
    "weekly": (
        r"(?:本周剩[余餘]?高倍(?:奖励)?次数|本周剩[余餘]?双倍(?:奖励)?次数|本周剩[余餘]?深度突破(?:奖励)?次数|本周剩[余餘]?次数|深度突破)[^\d]{0,16}(\d+)\s*[/／]\s*(\d+)",
        r"本周(?:高倍|双倍)?次数已用完",
    ),
}
_WB_PATTERNS = {
    "jihua": r"回流剩[余餘]?双倍(?:活跃)?能量[^\d]{0,16}(\d+)\s*[/／]\s*(\d+)",
    "bless": r"回流剩[余餘]?双倍(?:奖励)?次数[^\d]{0,16}(\d+)\s*[/／]\s*(\d+)",
    "monster": r"回流剩[余餘]?双倍(?:奖励)?次数[^\d]{0,16}(\d+)\s*[/／]\s*(\d+)",
    "weekly": r"回流剩[余餘]?双倍(?:奖励)?次数[^\d]{0,16}(\d+)\s*[/／]\s*(\d+)",
}


@dataclass
class DoubleRunSummary:
    spent: bool = False
    last_kind: str | None = None
    collection_used: bool = False
    remaining_energy: int | None = None


def load_realm_policy(rt) -> dict:
    """Shared realm target policy used by double-reward priority and Zhaoxi daily energy fill."""
    context = rt.context
    opt = parameters(context, "MaaNikki_Double_Option")
    enabled = opt.get("enabled", True) is not False
    types = {
        kind
        for kind, suffix in (
            ("jihua", "Jihua"),
            ("bless", "Bless"),
            ("monster", "Monster"),
            ("weekly", "Weekly"),
        )
        if (context.get_node_data(f"MaaNikki_Double_Type_{suffix}") or {}).get("enabled", False)
    }
    raw_reserve = parameters(context, "MaaNikki_Pursuit_ReserveBlingOption").get("amount", 0)
    try:
        reserve_bling = max(0, int(str(raw_reserve or 0).strip().replace(",", "")))
    except (ValueError, TypeError):
        reserve_bling = 0
    return {
        "enabled": enabled,
        "types": types,
        "jihua_mode": str(opt.get("jihua_mode", "follow_deficit")),
        "bless_mode": str(opt.get("bless_mode", "glow_pursuit")),
        "monster_mode": str(opt.get("monster_mode", "follow_deficit")),
        "weekly_priority": str(opt.get("weekly_priority", "Qiggeda")),
        "queue_goals": parse_realm_queue(parameters(context, "MaaNikki_RealmQueue").get("order")),
        "organize": parameters(context, "MaaNikki_Pursuit_OrganizeOption").get("mode", "auto"),
        "workflow": parameters(context, "MaaNikki_Pursuit_WorkflowOption").get("mode", "managed"),
        "reserve_bling": reserve_bling,
        "decompose": parameters(context, "MaaNikki_Pursuit_DecomposeOption").get("enabled") is True,
        "share_stats": stats_enabled(
            ROOT,
            parameters(context, "MaaNikki_Pursuit_StatsOption").get("enabled") is not False,
        ),
    }


def _load_double_config(rt) -> dict:
    return load_realm_policy(rt)


def material_mode_uses_default(config: dict, kind: str) -> tuple[bool, bool]:
    """Return ``(use_default, auto_fallback)`` for Jihua / Monster strategy.

    Falls back to the default realm target automatically when the strategy is
    ``follow_deficit`` but neither ``renewal`` nor ``crafting`` is enabled in
    the queue, keeping explicit ``inherit_fallback`` distinct only for logging.
    """
    mode_key = "jihua_mode" if kind == "jihua" else "monster_mode"
    if config.get(mode_key) == "inherit_fallback":
        return True, False
    if not any(g in ("renewal", "crafting") for g in (config.get("queue_goals") or ())):
        return True, True
    return False, False


def _has_material_candidate(kind: str, config: dict, role: dict, crafting: dict, scoring: dict) -> bool:
    use_default, _ = material_mode_uses_default(config, kind)
    if use_default:
        return True
    checker = goal_has_jihua_potential if kind == "jihua" else goal_has_monster_potential
    return any(
        (not wardrobe_snapshot(role) and (goal == "crafting" or renewal_source(role.get("materials") or {}) == "plan"))
        or checker(goal, role, crafting, scoring)
        for goal in (config.get("queue_goals") or ())
        if goal in ("renewal", "crafting")
    )


def overview_double_badges(rt, frame: np.ndarray | None, kinds=DOUBLE_EXEC_ORDER) -> set[str]:
    if frame is None:
        return set()
    return {
        kind
        for kind in kinds
        if kind in OVERVIEW_BADGE_ROIS and _badge_present(rt, frame, OVERVIEW_BADGE_ROIS[kind])
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
    p_regex, exhausted_re = _PRIMARY_PATTERNS[kind]
    p_match = re.search(p_regex, text)
    primary_rem: int | None = None
    if p_match:
        cur, cap = map(int, p_match.groups())
        if 0 <= cur <= cap and cap > 0:
            primary_rem = cur
    elif re.search(exhausted_re, text):
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


read_stage_double_quota = _read_stage_double_quota


def run_shared_bless_collection(
    rt,
    account: AccountState,
    policy: dict,
    *,
    max_runs: int | None,
    full_workflow: bool,
    organize_if_missing: bool,
    organized_once: bool = False,
    farm_only_override: bool | None = None,
    skip_prefix_scan: bool = False,
    allow_always_organize: bool = True,
    run_mode: str | None = None,
) -> dict:
    """Execute 5-star missing-color blessing realm runs cleanly for double or Zhaoxi supplement."""
    from data_paths import data_path
    catalog = json.loads(data_path("resource/data/blessing_catalog.json").read_text(encoding="utf-8"))
    workflow = policy.get("workflow", "managed")
    farm_only = workflow == "farm_only" if farm_only_override is None else bool(farm_only_override)
    organize = policy.get("organize", "auto")
    if organize not in ORGANIZE_OPTIONS:
        raise ValueError("未知祝福闪光整理选项")

    issue = organization_issue(rt, catalog)
    need_organize = (
        ((not organized_once and allow_always_organize and organize != "auto") or issue is not None)
        if full_workflow
        else (issue is not None)
    )
    if need_organize:
        if not organize_if_missing:
            return {"status": "fallback", "reason": "no_organization", "organized_once": organized_once}
        rt.log(
            "限时双倍插队 · 灵感迸发：正在核对并整理全部祝福闪光。"
            if full_workflow
            else "朝夕心愿补分 · 祝福闪光：正在建立祝福闪光仓库整理记录。",
            ui_text="双倍插队：整理祝福闪光仓库。" if full_workflow else "朝夕补分：整理祝福闪光仓库。",
        )
        org_result = organize_inventory(
            rt,
            catalog,
            allow_unlock=False if farm_only else bool(ORGANIZE_OPTIONS[organize]),
            read_only=farm_only,
        )
        organized_once = True
        if not org_result["published"] and organization_issue(rt, catalog) is not None:
            return {
                "status": "organize_failed" if full_workflow else "fallback",
                "reason": "organize_incomplete",
                "organized_once": organized_once,
            }

    effective_mode = run_mode or ("double_bless" if farm_only else "zhaoxi_bless")
    folder_root = "logs/blessing-pursuit" if (full_workflow or effective_mode == "double_bless") else "logs/daily-bless"
    folder = ROOT / folder_root / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    folder.mkdir(parents=True, exist_ok=True)
    pursuit = FiveStarInventory(rt, catalog, folder)
    pursuit.reserve_bling = int(policy.get("reserve_bling", 0) or 0)
    pursuit.share_statistics = bool(policy.get("share_stats", False))
    role_state = account.load()
    try:
        flow = FiveStarPursuit(
            pursuit,
            decompose_duplicates=bool(policy.get("decompose", False)),
            auto_bling=False,
            chance_priority=True,
            share_statistics=pursuit.share_statistics,
            farm_only=farm_only,
            skip_prefix_scan=skip_prefix_scan,
            reserve_bling=pursuit.reserve_bling,
            focus_sets=blessing_focus(role_state),
            power_mode=(blessing_mode(role_state) == "power"),
            max_farm_runs=max_runs,
        )
        if full_workflow:
            if flow.target_complete():
                return {"status": "complete", "organized_once": organized_once}
            rt.log(f"限时双倍插队 · 灵感迸发（智能选择）：按当前缺色顺位挑战（本次双倍上限 {max_runs} 次）。")
            flow.run()
            spent = any(
                op.get("kind") == "realm" and op.get("input_attempted") is not False
                for op in pursuit.report["operations"]
            )
            return {
                "status": "done",
                "spent": spent,
                "complete": flow.target_complete(),
                "organized_once": organized_once,
            }

        if farm_only:
            if effective_mode == "double_bless":
                rt.log(f"限时双倍插队 · 灵感迸发（仅刷胚子）：按已有记录的缺色顺位挑战（上限 {max_runs} 次，升级与锁定留待全收集统一处理）。")
            flow._run_farm_only(mode=effective_mode)
            if pursuit.report.get("phase") == "complete":
                return {"status": "complete", "organized_once": organized_once}
            if pursuit.report.get("phase") == "recovery_pending":
                return {"status": "fallback", "reason": "recovery_pending", "organized_once": organized_once}
            if pursuit.report.get("warehouse_full"):
                return {"status": "warehouse_full", "organized_once": organized_once}
            supply = pursuit.report.get("supply_result") or {}
            status = supply.get("status")
            if status == "reward_confirmed":
                return {
                    "status": "spent",
                    "spent": True,
                    "complete": flow.target_complete(),
                    "source": supply.get("source"),
                    "organized_once": organized_once,
                }
            if status == "energy_exhausted":
                return {"status": "energy_exhausted", "spent": False, "organized_once": organized_once}
            return {
                "status": "fallback",
                "spent": False,
                "reason": str(status or "unavailable"),
                "organized_once": organized_once,
            }

        pursuit.prepare(effective_mode)
        if flow.target_complete():
            pursuit.finish("当前收集范围的五星颜色已收齐，改用默认关卡")
            return {"status": "complete", "organized_once": organized_once}
        if not flow.make_room():
            pursuit.finish("祝福闪光仓库已满，本次未刷取")
            return {"status": "warehouse_full", "organized_once": organized_once}

        acquired = bool(flow.spend_energy())
        supply = pursuit.report.get("supply_result") or {}
        status = supply.get("status")
        if status == "reward_confirmed" and acquired:
            pursuit.finish("朝夕补分已完成 1 次缺色祝福闪光挑战，新增胚子待后续整理升级")
            return {"status": "spent", "source": supply.get("source"), "organized_once": organized_once}
        if status == "energy_exhausted":
            pursuit.finish("活跃能量不足，本次未提交祝福闪光挑战")
            return {"status": "energy_exhausted", "organized_once": organized_once}
        pursuit.finish("当前缺色祝福闪光副本暂不可用")
        return {"status": "fallback", "reason": str(status or "unavailable"), "organized_once": organized_once}
    except RescanSlot as error:
        pursuit.record_interruption(error, reason="焦点变化使列表状态失效")
        raise RuntimeError("焦点变化使当前祝福闪光升级或列表状态失效") from error
    except Exception as error:
        pursuit.record_interruption(error, reason=str(error))
        raise
    finally:
        try:
            if full_workflow:
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
    jihua_goals = [goal for goal in queue_goals if goal in ("renewal", "crafting")]
    use_default, auto_fallback = material_mode_uses_default(config, "jihua")

    any_spent = False
    goal_satisfied = False
    inventory: dict[str, int] | None = None
    wardrobe_synced = False
    mat_defs = crafting.get("materials") or {}

    def _run_default_jihua(rem_double: int, cur_energy: int, mode_note: str) -> tuple[bool, int]:
        target_label = expected_text((fields(rt.context, "MaaNikki_Jihua_Target") or {}).get("expected"), "闪亮泡泡")
        mid = JIHUA_LEVEL_IDS.get(target_label)
        meta = mat_defs.get(mid or "") or {}
        base_drop = int(meta.get("drop") or 0)
        max_afford = cur_energy // 10
        max_double = rem_double // 10
        runs = min(max_afford, max_double)
        if runs <= 0:
            return False, cur_energy
        use_max = runs >= max_afford
        rt.log(f"限时双倍插队 · 超级激化（{mode_note}）：目标「{target_label}」，挑战 {runs} 次（消耗 {runs * 10} 体力）。")
        ok, energy_after = execute_material_run(rt, "jihua", target_label, runs, use_max)
        spent = max(0, cur_energy - energy_after)
        actual_runs = effective_realm_runs(spent, 10, ok, planned_runs=runs)
        if actual_runs <= 0:
            raise RuntimeError("超级激化双倍挑战未完成或未确认活跃能量消耗")
        if mid and base_drop > 0:
            double_runs = min(actual_runs, rem_double // 10)
            normal_runs = max(0, actual_runs - double_runs)
            gained = double_runs * (base_drop * 2) + normal_runs * base_drop
            if gained > 0:
                record_accrual(account, mid, gained)
                record_material_history(account, crafting, scoring)
                rt.log(f"限时双倍插队 · 超级激化：获得 {meta.get('name', target_label)} +{gained}。")
        return True, energy_after

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

        ready_goals: list[str] = []
        effective_default = use_default
        mode_note = "未开启材料计划，自动回退默认设置" if auto_fallback else "默认设置"
        if not effective_default:
            role_check = account.load()
            ready_goals = [
                g for g in jihua_goals
                if goal_missing_baseline(g, role_check, crafting, scoring, realm="jihua", allow_tab2_init=False) is None
            ]
            if not ready_goals:
                effective_default = True
                mode_note = "尚未建立衣柜或背包记录，改用默认设置"

        if effective_default:
            spent_now, energy = _run_default_jihua(rem_double_energy, energy, mode_note)
            if not spent_now:
                break
            any_spent = True
        else:
            has_any_potential = any(
                goal_has_jihua_potential(goal, role_check, crafting, scoring)
                for goal in ready_goals
            )
            if inventory is None and has_any_potential:
                needed_mids = needed_material_map(ready_goals, role_check, crafting, scoring, realm="jihua")
                inventory = ensure_backpack_scanned(
                    rt,
                    account,
                    crafting,
                    needed_mids=needed_mids,
                    allow_init_scan=False,
                )
                record_material_history(account, crafting, scoring)
            finished: set[str] = set()
            while rem_double_energy >= 10 and energy >= 10 and not rt.stopped:
                role = account.load()
                picked = None
                for goal in ready_goals:
                    deficits = (
                        jihua_goal_deficits(goal, role, inventory or {}, crafting, scoring, multiplier=2)
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
                    rt.log(
                        "超级激化双倍（智能选择）：所需泡泡、丝线与噗灵已备齐，停止继续消耗双倍点数。"
                        if any_spent
                        else "超级激化双倍（智能选择）：所选焕新／制衣目标的泡泡、丝线与噗灵已备齐，跳过本次双倍。"
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
                ok, energy_after = execute_material_run(rt, "jihua", meta["level"], runs, use_max)
                spent = max(0, energy - energy_after)
                actual_runs = effective_realm_runs(spent, 10, ok, planned_runs=runs)
                if actual_runs <= 0:
                    raise RuntimeError("超级激化双倍挑战未完成或未确认活跃能量消耗")
                double_runs = min(actual_runs, rem_double_energy // 10)
                normal_runs = max(0, actual_runs - double_runs)
                gained = double_runs * (base_drop * 2) + normal_runs * base_drop
                if gained > 0:
                    inventory[mid] = int(inventory.get(mid, 0)) + gained
                    record_accrual(account, mid, gained)
                    record_material_history(account, crafting, scoring)
                    rt.log(f"限时双倍插队 · 超级激化：获得 {meta['name']} +{gained}（当前库存 {inventory[mid]}）。")
                rem_double_energy = max(0, rem_double_energy - double_runs * 10)
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
    organized_once = False
    goal_satisfied = False

    def _run_default_bless(rem_runs: int, cur_energy: int, *, mode_note: str) -> tuple[bool, int]:
        target_label = expected_text((fields(rt.context, "MaaNikki_Bless_Level") or {}).get("expected"), "巨蛇遗迹")
        cap = cur_energy // 40
        runs = min(rem_runs, cap)
        if runs <= 0:
            return False, cur_energy
        capped = None if runs >= cap else runs
        rt.log(f"限时双倍插队 · 灵感迸发（{mode_note}）：目标「{target_label}」，挑战 {runs} 次（消耗 {runs * 40} 体力）。")
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
        if energy_after is not None and energy_after < cur_energy:
            return True, energy_after
        if not ok:
            raise RuntimeError("灵感迸发双倍挑战未确认完成")
        return False, (energy_after if energy_after is not None else cur_energy)

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
            spent_now, energy = _run_default_bless(rem_runs, energy, mode_note="默认设置")
            any_spent |= spent_now
        else:
            # Double priority only burns the bonus quota on embryos without scanning prefixes or upgrading.
            res = run_shared_bless_collection(
                rt,
                account,
                config,
                max_runs=rem_runs,
                full_workflow=False,
                organize_if_missing=False,
                organized_once=organized_once,
                farm_only_override=True,
                skip_prefix_scan=True,
                allow_always_organize=False,
                run_mode="double_bless",
            )
            organized_once = bool(res.get("organized_once"))
            status = res.get("status")
            if status == "complete":
                goal_satisfied = True
                rt.log("灵感迸发双倍（智能选择）：所选范围五星颜色已收齐，跳过本次双倍。")
                break
            if status == "warehouse_full":
                rt.log("灵感迸发双倍：祝福闪光仓库已满，本次不刷取，待后续全收集清理后再处理。")
                break
            if status == "fallback":
                reason = res.get("reason")
                if reason == "no_organization":
                    note = "尚未建立祝福闪光仓库记录，不自动全仓整理"
                elif reason == "recovery_pending":
                    note = "存在待核对仓库恢复状态，不中断扫描，改用默认关卡"
                else:
                    note = "无可用缺色关卡，改用默认关卡"
                if not rt.stopped and not rt.ui.calendar():
                    raise RuntimeError("灵感迸发双倍回退默认前未能返回奇想日历")
                spent_now, energy = _run_default_bless(rem_runs, energy, mode_note=note)
                any_spent |= spent_now
            else:
                if res.get("spent"):
                    any_spent = True
                if res.get("complete"):
                    goal_satisfied = True

                if not rt.stopped and not rt.ui.calendar():
                    raise RuntimeError("灵感迸发双倍挑战后未能返回奇想日历")
                refreshed = Gameplay(rt).read_energy()
                if refreshed is not None:
                    energy = refreshed

        if goal_satisfied or not has_wb or (energy is not None and energy < 40):
            break

    if not rt.stopped and not rt.ui.calendar():
        raise RuntimeError("灵感迸发双倍处理后未能返回奇想日历")
    return any_spent, energy, False


def _run_double_monster(
    rt,
    account: AccountState,
    crafting: dict,
    scoring: dict,
    config: dict,
    queue_goals: list[str],
    energy: int | None,
) -> tuple[bool, int | None]:
    monster_goals = [goal for goal in queue_goals if goal in ("renewal", "crafting")]
    use_default, auto_fallback = material_mode_uses_default(config, "monster")

    any_spent = False
    goal_satisfied = False
    inventory: dict[str, int] | None = None
    wardrobe_synced = False
    mat_defs = crafting.get("materials") or {}
    per_run = Gameplay.MIN_ENERGY["monster"]

    def _run_default_monster(rem: int, cur_energy: int, mode_note: str) -> tuple[bool, int]:
        target_label = expected_text((fields(rt.context, "MaaNikki_Monster_Level") or {}).get("expected"), "攀越")
        cap = cur_energy // per_run
        runs = min(rem, cap)
        if runs <= 0:
            return False, cur_energy
        use_max = runs >= cap
        rt.log(f"限时双倍插队 · 魔物试炼（{mode_note}）：目标「{target_label}」，挑战 {runs} 次（消耗 {runs * per_run} 体力）。")
        ok, energy_after = execute_material_run(rt, "monster", target_label, runs, use_max)
        spent = max(0, cur_energy - energy_after)
        total_runs = effective_realm_runs(spent, per_run, ok, planned_runs=runs)
        if total_runs <= 0:
            raise RuntimeError("魔物试炼双倍挑战未完成或未确认活跃能量消耗")
        matches = [
            (k, m) for k, m in mat_defs.items()
            if m.get("realm") == "monster" and m.get("level") == target_label
        ]
        if len(matches) == 1:
            mid, meta = matches[0]
            base_drop = int(meta.get("drop") or 0)
            if base_drop > 0:
                double_runs = min(total_runs, rem)
                normal_runs = max(0, total_runs - double_runs)
                gained = double_runs * (base_drop * 2) + normal_runs * base_drop
                if gained > 0:
                    record_accrual(account, mid, gained)
                    record_material_history(account, crafting, scoring)
                    rt.log(f"限时双倍插队 · 魔物试炼：获得 {meta.get('name', target_label)} +{gained}。")
        return True, energy_after

    for pass_idx in range(2):
        if rt.stopped:
            break
        if not rt.ui.realm("monster"):
            raise RuntimeError("未能进入魔物试炼幻境页面")
        rem_runs, has_wb, page_energy = _read_stage_double_quota(rt, "monster")
        if page_energy is not None:
            energy = page_energy
        if not rem_runs or rem_runs <= 0:
            if pass_idx == 0:
                rt.log("魔物试炼双倍：今日双倍次数已用完或未确认双倍标识，跳过。")
            break
        if energy is None:
            if not rt.ui.calendar():
                raise RuntimeError("未能进入奇想日历读取活跃能量")
            energy = Gameplay(rt).read_energy()
            if energy is None:
                raise RuntimeError("无法读取当前活跃能量")
        if energy < per_run:
            rt.log(f"魔物试炼双倍：当前活跃能量 {energy} 点，不足 {per_run} 点，跳过。")
            break

        ready_goals: list[str] = []
        effective_default = use_default
        mode_note = "未开启材料计划，自动回退默认设置" if auto_fallback else "默认设置"
        if not effective_default:
            role_check = account.load()
            ready_goals = [
                g for g in monster_goals
                if goal_missing_baseline(g, role_check, crafting, scoring, realm="monster", allow_tab2_init=False) is None
            ]
            if not ready_goals:
                effective_default = True
                mode_note = "尚未建立衣柜或背包记录，改用默认设置"

        if effective_default:
            spent_now, energy = _run_default_monster(rem_runs, energy, mode_note)
            if not spent_now:
                break
            any_spent = True
        else:
            has_any_potential = any(
                goal_has_monster_potential(goal, role_check, crafting, scoring)
                for goal in ready_goals
            )
            if inventory is None and has_any_potential:
                needed_mids = needed_material_map(ready_goals, role_check, crafting, scoring, realm="monster")
                inventory = ensure_backpack_scanned(
                    rt,
                    account,
                    crafting,
                    needed_mids=needed_mids,
                    allow_init_scan=False,
                )
                record_material_history(account, crafting, scoring)
            while rem_runs > 0 and energy >= per_run and not rt.stopped:
                role = account.load()
                picked = None
                for goal in ready_goals:
                    deficits = (
                        realm_goal_deficits(goal, role, inventory or {}, crafting, scoring, realm="monster", multiplier=2)
                        if has_any_potential
                        else []
                    )
                    if deficits:
                        picked = (goal, deficits[0])
                        break
                if picked is None:
                    goal_satisfied = True
                    rt.log(
                        "魔物试炼双倍（智能选择）：所需试炼晶石与魔物泡泡已备齐，停止继续消耗双倍次数。"
                        if any_spent
                        else "魔物试炼双倍（智能选择）：所选焕新／制衣目标的试炼素材已备齐，跳过本次双倍。"
                    )
                    break
                goal, (_, _, mid, meta, deficit) = picked
                base_drop = int(meta["drop"])
                runs_needed = math.ceil(deficit / (base_drop * 2))
                max_afford = energy // per_run
                runs = min(runs_needed, max_afford, rem_runs)
                use_max = runs >= max_afford
                rt.log(
                    f"限时双倍插队 · 魔物试炼（{GOAL_LABELS[goal]}）：{meta['name']} 还差 {deficit}，"
                    f"挑战「{meta['level']}」{runs} 次（消耗 {runs * per_run} 体力）。"
                )
                ok, energy_after = execute_material_run(rt, "monster", meta["level"], runs, use_max)
                spent = max(0, energy - energy_after)
                total_runs = effective_realm_runs(spent, per_run, ok, planned_runs=runs)
                if total_runs <= 0:
                    raise RuntimeError("魔物试炼双倍挑战未完成或未确认活跃能量消耗")
                double_runs = min(total_runs, rem_runs)
                normal_runs = max(0, total_runs - double_runs)
                gained = double_runs * (base_drop * 2) + normal_runs * base_drop
                if gained > 0:
                    inventory[mid] = int(inventory.get(mid, 0)) + gained
                    record_accrual(account, mid, gained)
                    record_material_history(account, crafting, scoring)
                    rt.log(f"限时双倍插队 · 魔物试炼：获得 {meta['name']} +{gained}（当前库存 {inventory[mid]}）。")
                rem_runs = max(0, rem_runs - double_runs)
                energy = energy_after
                any_spent = True

        if goal_satisfied or not has_wb or (energy is not None and energy < per_run):
            break

    if not rt.stopped and not rt.ui.calendar():
        raise RuntimeError("魔物试炼双倍处理后未能返回奇想日历")
    return any_spent, energy


def run_double_priority(rt) -> DoubleRunSummary:
    """Detect active 2x realm events and consume eligible bonuses before the normal queue."""
    summary = DoubleRunSummary()
    config = load_realm_policy(rt)
    enabled_types = config["types"]
    if not config["enabled"] or not enabled_types:
        return summary

    queue_goals = config["queue_goals"]
    organize = config["organize"]
    account = AccountState.for_runtime(ROOT, rt)
    role = account.load()
    crafting, scoring = load_catalogs()

    candidates: set[str] = set()
    if "weekly" in enabled_types and any(
        (rt.context.get_node_data(node) or {}).get("enabled", False) for node, _ in WEEKLY_TARGETS
    ):
        candidates.add("weekly")
    for kind in ("jihua", "monster"):
        if kind in enabled_types and _has_material_candidate(kind, config, role, crafting, scoring):
            candidates.add(kind)
    if "bless" in enabled_types:
        if config["bless_mode"] == "inherit_fallback" or glow_shortfall(rt, role, "auto"):
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
    badged = overview_double_badges(rt, overview, candidates)
    active_kinds = [kind for kind in DOUBLE_EXEC_ORDER if kind in badged]
    if not active_kinds:
        rt.log("限时双倍插队：当前没有待刷的可参与双倍幻境，按刷取顺序继续。", detail=True)
        rt.ui.calendar()
        return summary

    labels = {"weekly": "深度突破", "jihua": "超级激化", "bless": "灵感迸发", "monster": "魔物试炼"}
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
        elif kind == "monster":
            spent_now, energy = _run_double_monster(rt, account, crafting, scoring, config, queue_goals, energy)
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
