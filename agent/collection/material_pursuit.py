"""Live material shortfalls of the realm queue goals (换新 / 制衣) and their farming."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import re

from account_state import AccountState
from daily.gameplay import Gameplay
from daily.runtime import Executors
from daily.settings import ROOT
from .realm_goals import GOAL_LABELS
from .backpack import ensure_backpack_scanned, renewal_source, wardrobe_snapshot, wardrobe_snapshot_key


def load_catalogs():
    crafting = json.loads((ROOT / "resource/data/crafting_catalog.json").read_text(encoding="utf-8"))
    scoring = json.loads((ROOT / "resource/data/wardrobe_scoring.json").read_text(encoding="utf-8"))
    return crafting, scoring


def owned_score_ids(role: dict, scoring: dict) -> set[str]:
    snapshot = wardrobe_snapshot(role)
    parts = snapshot.get("parts") or {}
    game_items = scoring.get("game_items") or {}
    owned = set()
    for state in parts.values():
        if not isinstance(state, dict):
            continue
        for row in state.get("items") or []:
            if not isinstance(row, dict) or row.get("source") == "selected_card_unconfirmed":
                continue
            item_ids = row.get("item_ids") or []
            entries = [game_items.get(str(gid)) for gid in item_ids]
            if not entries or any(not e for e in entries):
                continue
            meanings = {f"{e['score_id']}:{e['group']}:{e['glow_reward']}" for e in entries}
            if len(meanings) == 1:
                owned.add(str(entries[0]["score_id"]))
    return owned


def plan_mode(plan) -> str:
    """Plans saved before 自选 existed have no mode and mean 全收集."""
    return "selected" if isinstance(plan, dict) and plan.get("mode") == "selected" else "all"


def compute_crafting_requirements(role: dict, crafting: dict, scoring: dict) -> dict[str, int]:
    owned = owned_score_ids(role, scoring)
    materials_state = role.get("materials") or {}
    plan = materials_state.get("crafting_plan")
    selected = plan_mode(plan) == "selected"
    picked = {str(sid) for sid in (plan.get("sketch_ids") or [])} if selected else set()
    scope = materials_state.get("crafting_scope", "all")
    totals: dict[str, int] = {}
    for sketch in crafting.get("sketches") or []:
        if str(sketch["id"]) in owned:
            continue
        if selected:
            if str(sketch["id"]) not in picked:
                continue
        elif scope == "suits" and sketch.get("category") != "suit":
            continue
        for mid, cnt in (sketch.get("costs") or {}).items():
            totals[str(mid)] = totals.get(str(mid), 0) + int(cnt)
    # Crafting between two wardrobe reads is only known as an account-wide total,
    # so it can only be deducted from the 全收集 plan.
    wb_key = wardrobe_snapshot_key(role)
    if not selected and wb_key and materials_state.get("wardrobe_baseline_key") == wb_key:
        consumed = materials_state.get("crafted_consumed") or {}
        for mid, used in consumed.items():
            if str(mid) in totals and int(used) > 0:
                totals[str(mid)] = max(0, totals[str(mid)] - int(used))
    return totals


MAX_LEVEL = 10
RENEWED_LEVEL = 11
COSMETIC_PARTS = {"281", "282", "283", "284", "285", "286"}
# Bubbles are spent only on clothing growth; bling and thread have other consumers.
RENEWAL_TRACKED_KINDS = {"monster_glow"}
RENEWAL_TRACKED_IDS = {"50361309"}


def renewal_baseline_key(role: dict) -> str:
    """Saved levels or a new wardrobe read restart the renewal consumption ledger."""
    wb_key = wardrobe_snapshot_key(role)
    scoring = (role.get("wardrobe") or {}).get("scoring") or {}
    return f"{wb_key}|{scoring.get('updated_at') or ''}" if wb_key else ""


def level_cost(curve: dict, from_level: int, to_level: int) -> dict[str, int]:
    costs = curve.get("costs") or []
    if from_level >= to_level or to_level >= len(costs):
        return {}
    base = costs[from_level] if 0 <= from_level < len(costs) else {}
    return {str(mid): int(value) - int(base.get(mid, 0))
            for mid, value in (costs[to_level] or {}).items() if int(value) - int(base.get(mid, 0)) > 0}


def renewal_groups(role: dict, scoring: dict) -> list[dict]:
    """Owned clothing groups with their shared level, mirroring the client profile."""
    snapshot = wardrobe_snapshot(role)
    game_items = scoring.get("game_items") or {}
    items = scoring.get("items") or {}
    curves = scoring.get("curves") or []
    levels = ((role.get("wardrobe") or {}).get("scoring") or {}).get("levels") or {}
    groups: dict[str, dict] = {}
    for part, state in (snapshot.get("parts") or {}).items():
        if str(part) in COSMETIC_PARTS or not isinstance(state, dict):
            continue
        for row in state.get("items") or []:
            if not isinstance(row, dict) or row.get("source") == "selected_card_unconfirmed":
                continue
            entries = [game_items.get(str(gid)) for gid in row.get("item_ids") or []]
            if not entries or any(not e for e in entries):
                continue
            if len({f"{e['score_id']}:{e['group']}:{e['glow_reward']}" for e in entries}) != 1:
                continue
            entry = entries[0]
            group = groups.setdefault(str(entry["group"]), {"id": str(entry["group"]), "score_id": None, "reward": False})
            group["score_id"] = group["score_id"] or str(entry["score_id"])
            group["reward"] = group["reward"] or bool(entry.get("glow_reward"))
    result = []
    for group in groups.values():
        item = items.get(group["score_id"] or "")
        if not item or int(item.get("type", 0)) >= 200 or int(item.get("curve", -1)) >= len(curves):
            continue
        curve = curves[int(item["curve"])]
        manual = levels.get(group["id"])
        level = RENEWED_LEVEL if group["reward"] else manual if isinstance(manual, int) and 0 <= manual <= RENEWED_LEVEL else 0
        renewable = bool(level_cost(curve, MAX_LEVEL, RENEWED_LEVEL))
        result.append({"id": group["id"], "curve": curve, "level": level, "renewable": renewable})
    return result


def compute_renewal_requirements(role: dict, scoring: dict) -> dict[str, int]:
    """Materials still needed for the renewal plan, from the current wardrobe and saved levels."""
    mats = role.get("materials") or {}
    plan = mats.get("renewal_plan") or {}
    selected = plan_mode(plan) == "selected"
    targets = plan.get("targets") or {}
    totals: dict[str, int] = {}
    for group in renewal_groups(role, scoring):
        top = RENEWED_LEVEL if group["renewable"] else MAX_LEVEL
        if selected:
            wanted = targets.get(group["id"])
            if wanted not in (MAX_LEVEL, RENEWED_LEVEL):
                continue
            target = min(int(wanted), top)
        else:
            target = top
        for mid, cnt in level_cost(group["curve"], group["level"], target).items():
            totals[mid] = totals.get(mid, 0) + cnt
    key = renewal_baseline_key(role)
    if key and mats.get("renewal_baseline_key") == key:
        for mid, used in (mats.get("renewal_consumed") or {}).items():
            if str(mid) in totals and int(used) > 0:
                totals[str(mid)] = max(0, totals[str(mid)] - int(used))
    return totals


def execute_material_run(rt, kind: str, level_name: str, runs_to_do: int, use_max: bool) -> tuple[bool, int]:
    clone = rt.context.clone()
    child = Executors(clone)
    game = Gameplay(child)
    try:
        if kind == "monster":
            if not clone.override_pipeline({"MaaNikki_Monster_Level": {"expected": re.escape(level_name)}}):
                raise RuntimeError("魔物试炼关卡设置未能应用")
        elif kind == "jihua":
            if not clone.override_pipeline({"MaaNikki_Jihua_Target": {"expected": level_name},
                                            "MaaNikki_Jihua_TargetRetry": {"expected": level_name}}):
                raise RuntimeError("素材激化目标设置未能应用")
        ok = game.realm(
            kind,
            maximize=use_max,
            fixed_count=None if use_max else runs_to_do,
            allow_exhausted=True,
            return_page="calendar",
        )
    finally:
        rt.navigation_halted |= child.navigation_halted
    if rt.stopped or not rt.ui.calendar():
        raise RuntimeError("幻境挑战后未能返回奇想日历")
    energy_after = Gameplay(rt).read_energy()
    if energy_after is None:
        raise RuntimeError("幻境挑战后无法核对剩余活跃能量")
    return ok, energy_after


def record_accrual(account: AccountState, mid: str | None = None, gained: int = 0, *, mark_glow_completed: bool = False):
    now = datetime.now(timezone.utc).isoformat()

    def update(data):
        mats = data.setdefault("materials", {})
        if mid is not None:
            inv = mats.setdefault("inventory", {})
            inv[mid] = max(0, int(inv.get(mid, 0)) + int(gained))
            mats["inventory_updated_at"] = now
        if mark_glow_completed and isinstance(mats.get("glow_plan"), dict):
            mats["glow_plan"]["completed"] = True
            mats["glow_plan"]["completed_at"] = now

    account.update(update)



def goal_requirements(goal: str, role: dict, crafting: dict, scoring: dict) -> dict[str, int]:
    """Materials a goal still needs, computed live from the wardrobe and plan data."""
    mats = role.get("materials") or {}
    if goal == "crafting":
        if not wardrobe_snapshot(role):
            return {}
        mat_defs = crafting.get("materials") or {}
        # Automatic farming covers the trial crystals; bling, thread and world
        # materials are listed in the crafting page for the player to prepare.
        return {mid: count for mid, count in compute_crafting_requirements(role, crafting, scoring).items()
                if (mat_defs.get(mid) or {}).get("kind") == "monster_craft"}
    if goal == "renewal":
        if renewal_source(mats) == "targets":
            glow_plan = mats.get("glow_plan") or {}
            if glow_plan.get("completed") or glow_plan.get("enabled") is False:
                return {}
            return {str(mid): int(value) for mid, value in (glow_plan.get("targets") or {}).items() if int(value) > 0}
        return compute_renewal_requirements(role, scoring) if wardrobe_snapshot(role) else {}
    return {}


def goal_deficits(goal: str, role: dict, inventory: dict, crafting: dict, scoring: dict) -> list[tuple]:
    """Farmable shortfalls of a goal, largest first; trial materials before bling and thread."""
    mat_defs = crafting.get("materials") or {}
    items = []
    for mid, need in goal_requirements(goal, role, crafting, scoring).items():
        meta = mat_defs.get(str(mid))
        if not meta or not meta.get("realm") or not meta.get("level"):
            continue
        deficit = int(need) - int(inventory.get(str(mid), 0))
        if deficit > 0:
            priority = 0 if meta.get("kind") in ("monster_glow", "monster_craft") else 1
            items.append((priority, -deficit, str(mid), meta, deficit))
    items.sort()
    return items


def run_material_goals(rt, goals: list[str], glow_shortfall) -> tuple[str, str | None]:
    """Farm the enabled goals in order, re-choosing before every challenge batch.

    Returns the hand-off ("glow", "fallback" or "done") and the realm kind last
    farmed. ``glow_shortfall(role)`` reports whether the blessing goal still has
    colours to chase; a pending glow goal hands off to the blessing workflow.
    """
    account = AccountState.for_runtime(ROOT, rt)
    crafting, scoring = load_catalogs()
    inventory = None
    energy = None
    game = None
    last_kind = None
    finished: set[str] = set()
    while not rt.stopped:
        role = account.load()
        picked = None
        for goal in goals:
            if goal == "glow":
                if glow_shortfall(role):
                    return "glow", last_kind
                if goal not in finished:
                    finished.add(goal)
                    rt.log("幻境挑战顺序：五星闪光已收齐所选范围，跳过。")
                continue
            if inventory is None:
                inventory = ensure_backpack_scanned(rt, account, crafting)
                role = account.load()
            deficits = goal_deficits(goal, role, inventory, crafting, scoring)
            if deficits:
                picked = (goal, deficits[0])
                break
            if goal not in finished:
                finished.add(goal)
                rt.log(f"幻境挑战顺序：{GOAL_LABELS[goal]}已备齐，交给下一项。")
                if goal == "renewal" and renewal_source(role.get("materials") or {}) == "targets":
                    record_accrual(account, mark_glow_completed=True)
        if picked is None:
            return "fallback", last_kind
        if energy is None:
            if not rt.ui.calendar():
                raise RuntimeError("未能进入奇想日历读取活跃能量")
            game = Gameplay(rt)
            energy = game.read_energy()
            if energy is None:
                raise RuntimeError("无法读取当前活跃能量")
        goal, (_, _, mid, meta, deficit) = picked
        kind = meta["realm"]
        per_run = int(meta.get("energy", 40))
        per_drop = int(meta.get("drop", 5))
        if energy < game.MIN_ENERGY[kind]:
            rt.log(f"幻境挑战顺序：剩余活跃能量 {energy} 点，不足继续挑战。")
            return "done", last_kind
        runs_needed = math.ceil(deficit / per_drop)
        max_runs = energy // per_run
        runs = min(runs_needed, max_runs)
        use_max = runs_needed >= max_runs
        rt.log(f"幻境挑战顺序：{GOAL_LABELS[goal]} · {meta['name']} 还差 {deficit}，挑战「{meta['level']}」{runs} 次。")
        ok, energy_after = execute_material_run(rt, kind, meta["level"], runs, use_max)
        spent = max(0, energy - energy_after)
        gained = (spent // per_run) * per_drop
        if gained > 0:
            inventory[mid] = int(inventory.get(mid, 0)) + gained
            record_accrual(account, mid, gained)
            rt.log(f"幻境挑战顺序：已获得 {meta['name']} +{gained}（当前库存 {inventory[mid]}）。")
        energy = energy_after
        last_kind = kind
        if not ok or spent == 0:
            raise RuntimeError(f"{GOAL_LABELS[goal]}挑战未完成或未确认活跃能量消耗")
    return "done", last_kind
