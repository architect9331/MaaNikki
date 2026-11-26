"""Live material shortfalls of the realm queue goals (焕新 / 制衣) and their farming."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import re

from account_state import AccountState, game_day
from adventure_report import try_record_adventure_entry
from daily.gameplay import Gameplay
from daily.runtime import Executors, parameters
from daily.settings import ROOT
from task_control import TaskStopped
from .realm_goals import GOAL_LABELS
from .backpack import (
    ensure_backpack_scanned,
    has_backpack_baseline,
    renewal_source,
    required_tabs_for_mids,
    wardrobe_snapshot,
    wardrobe_snapshot_key,
)
from .dominance import strictly_dominated_standalone_ids


def load_catalogs():
    from data_paths import data_path
    crafting = json.loads(data_path("resource/data/crafting_catalog.json").read_text(encoding="utf-8"))
    scoring = json.loads(data_path("resource/data/wardrobe_scoring.json").read_text(encoding="utf-8"))
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
    """Normalize plan mode: ``all``, ``power`` or ``selected``."""
    if isinstance(plan, dict) and plan.get("mode") in ("power", "selected"):
        return plan["mode"]
    return "all"


def normalize_renewal_scope(scope) -> str:
    if scope in ("five_star", "five_plus_four_suits", "four_plus", "all"):
        return str(scope)
    return "all"


def matches_renewal_scope(quality: int, in_suit: bool, scope: str) -> bool:
    if scope == "five_star":
        return quality >= 5
    if scope == "five_plus_four_suits":
        return quality >= 5 or (quality == 4 and in_suit)
    if scope == "four_plus":
        return quality >= 4
    return True


def compute_crafting_requirements(role: dict, crafting: dict, scoring: dict) -> dict[str, int]:
    owned = owned_score_ids(role, scoring)
    materials_state = role.get("materials") or {}
    plan = materials_state.get("crafting_plan")
    mode = plan_mode(plan)
    selected = mode == "selected"
    picked = {str(sid) for sid in (plan.get("sketch_ids") or [])} if selected else set()
    scope = materials_state.get("crafting_scope", "all")
    sketches = crafting.get("sketches") or []
    dominated: set[str] = set()
    if mode == "power" and scope != "suits":
        available = owned | {str(sk["id"]) for sk in sketches}
        uncrafted = [str(sk["id"]) for sk in sketches if str(sk["id"]) not in owned]
        dominated = strictly_dominated_standalone_ids(scoring, uncrafted, available)
    totals: dict[str, int] = {}
    for sketch in sketches:
        sid = str(sketch["id"])
        if sid in owned:
            continue
        if selected:
            if sid not in picked:
                continue
        else:
            if scope == "suits" and sketch.get("category") != "suit":
                continue
            if mode == "power" and sid in dominated:
                continue
        for mid, cnt in (sketch.get("costs") or {}).items():
            totals[str(mid)] = totals.get(str(mid), 0) + int(cnt)
    # Crafting between two wardrobe reads is only known as an account-wide total,
    # so it can only be deducted from non-selected plans.
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
RENEWAL_TRACKED_KINDS = {"monster_glow"}
RENEWAL_TRACKED_IDS = {"50361309"}
SHINY_BUBBLE_ID = "50361309"
THREAD_ID = "3448636403"
BLING_ID = "992617511"


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
    suit_item_ids = {str(iid) for suit in (scoring.get("suits") or []) for iid in (suit.get("items") or [])}
    result = []
    for group in groups.values():
        item = items.get(group["score_id"] or "")
        if not item or int(item.get("type", 0)) >= 200 or int(item.get("curve", -1)) >= len(curves):
            continue
        curve = curves[int(item["curve"])]
        manual = levels.get(group["id"])
        level = RENEWED_LEVEL if group["reward"] else manual if isinstance(manual, int) and 0 <= manual <= RENEWED_LEVEL else 0
        renewable = bool(level_cost(curve, MAX_LEVEL, RENEWED_LEVEL))
        result.append({
            "id": group["id"],
            "score_id": group["score_id"],
            "quality": int(item.get("quality", 0)),
            "in_suit": group["score_id"] in suit_item_ids,
            "curve": curve,
            "level": level,
            "renewable": renewable,
        })
    return result


def compute_renewal_requirements_tiered(role: dict, scoring: dict) -> tuple[dict[str, int], dict[str, int]]:
    """Return (all_planned_totals, priority_targets_totals) after subtracting consumption."""
    mats = role.get("materials") or {}
    plan = mats.get("renewal_plan") or {}
    mode = plan_mode(plan)
    scope = normalize_renewal_scope(mats.get("renewal_scope"))
    selected = mode == "selected"
    targets = plan.get("targets") or {}
    priority_targets = plan.get("priority_targets") or {}
    groups = renewal_groups(role, scoring)
    dominated: set[str] = set()
    if mode == "power":
        owned_score = {g["score_id"] for g in groups}
        dominated = strictly_dominated_standalone_ids(scoring, owned_score, owned_score)
    totals: dict[str, int] = {}
    priority: dict[str, int] = {}
    for group in groups:
        top = RENEWED_LEVEL if group["renewable"] else MAX_LEVEL
        base_target: int | None = None
        if selected:
            wanted = targets.get(group["id"])
            if wanted in (MAX_LEVEL, RENEWED_LEVEL):
                base_target = min(int(wanted), top)
        else:
            if not (mode == "power" and group["score_id"] in dominated) and matches_renewal_scope(
                group["quality"], group["in_suit"], scope
            ):
                base_target = top
        prio_wanted = priority_targets.get(group["id"])
        p_target: int | None = None
        if isinstance(prio_wanted, int) and not isinstance(prio_wanted, bool) and prio_wanted > 0:
            p_target = min(int(prio_wanted), top)
        effective_target = max(base_target or 0, p_target or 0)
        if effective_target <= 0:
            continue
        for mid, cnt in level_cost(group["curve"], group["level"], effective_target).items():
            totals[mid] = totals.get(mid, 0) + cnt
        if p_target is not None and p_target > 0:
            for mid, cnt in level_cost(group["curve"], group["level"], p_target).items():
                priority[mid] = priority.get(mid, 0) + cnt
    key = renewal_baseline_key(role)
    if key and mats.get("renewal_baseline_key") == key:
        for mid, used in (mats.get("renewal_consumed") or {}).items():
            if str(mid) in totals and int(used) > 0:
                totals[str(mid)] = max(0, totals[str(mid)] - int(used))
    for mid in list(priority):
        capped = min(priority[mid], totals.get(mid, 0))
        if capped > 0:
            priority[mid] = capped
        else:
            del priority[mid]
    return totals, priority


def compute_renewal_requirements(role: dict, scoring: dict) -> dict[str, int]:
    """Materials still needed for the renewal plan, from the current wardrobe and saved levels."""
    return compute_renewal_requirements_tiered(role, scoring)[0]


def effective_realm_runs(spent: int, per_run: int, ok: bool, *, planned_runs: int | None = None) -> int:
    """Resolve completed realm runs from net energy delta with tolerance for <=3 natural regen ticks."""
    if per_run <= 0:
        return 0
    runs = max(0, int(spent)) // per_run
    rem = max(0, int(spent)) % per_run
    if ok and rem >= max(1, per_run - 3) and (planned_runs is None or runs < int(planned_runs)):
        runs += 1
    return runs


def execute_material_run(
    rt,
    kind: str,
    level_name: str,
    runs_to_do: int,
    use_max: bool,
    *,
    record_report: bool = False,
) -> tuple[bool, int, dict[str, bool]]:
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
            record_report=record_report,
        )
    finally:
        rt.navigation_halted |= child.navigation_halted
    if rt.stopped or not rt.ui.calendar():
        raise RuntimeError("幻境挑战后未能返回奇想日历")
    energy_after = Gameplay(rt).read_energy()
    if energy_after is None:
        raise RuntimeError("幻境挑战后无法核对剩余活跃能量")
    flags = {
        "energy_exhausted": bool(game.energy_exhausted),
        "jihua_exhausted": bool(game.jihua_exhausted),
        "jihua_material_capped": bool(getattr(game, "jihua_material_capped", False)),
    }
    return ok, energy_after, flags


def _goal_energy_summary(
    goal: str,
    role: dict,
    inventory: dict,
    crafting: dict,
    scoring: dict,
) -> tuple[int, int]:
    mat_defs = crafting.get("materials") or {}
    energy_total = 0
    missing_kinds = 0
    for mid, need in goal_requirements(goal, role, crafting, scoring).items():
        meta = mat_defs.get(str(mid)) or {}
        drop = int(meta.get("drop") or 0)
        energy = int(meta.get("energy") or 0)
        deficit = max(0, int(need) - int(inventory.get(str(mid), 0)))
        if deficit > 0 and drop > 0 and energy > 0:
            energy_total += math.ceil(deficit / drop) * energy
            missing_kinds += 1
    return energy_total, missing_kinds


def record_material_history(
    account: AccountState,
    crafting: dict,
    scoring: dict,
):
    """Record one daily point of remaining crafting/renewal energy for trend charts."""
    try:
        day = str(game_day())

        def update(data):
            inv = (data.get("materials") or {}).get("inventory") or {}
            mats = data.get("materials") or {}
            has_wb = bool(wardrobe_snapshot(data))
            has_renewal = renewal_source(mats) == "targets" or has_wb
            if not has_wb and not has_renewal:
                return
            point = {}
            if has_renewal:
                r_energy, r_missing = _goal_energy_summary(
                    "renewal", data, inv, crafting, scoring,
                )
                point["renewal_energy"] = r_energy
                point["renewal_missing"] = r_missing
            if has_wb:
                c_energy, c_missing = _goal_energy_summary(
                    "crafting", data, inv, crafting, scoring,
                )
                point["crafting_energy"] = c_energy
                point["crafting_missing"] = c_missing
            history = data.setdefault("observations", {}).setdefault("material_history", {})
            history[day] = point
            for old_day in sorted(history)[:-1000]:
                del history[old_day]

        account.update(update)
    except Exception:
        pass


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



def goal_requirements(
    goal: str,
    role: dict,
    crafting: dict,
    scoring: dict,
) -> dict[str, int]:
    """Materials a goal still needs, computed live from the wardrobe and plan data."""
    mats = role.get("materials") or {}
    if goal == "crafting":
        if not wardrobe_snapshot(role):
            return {}
        mat_defs = crafting.get("materials") or {}
        # Regular crafting farms the trial crystals; double jihua covers thread & bling when needed.
        return {mid: count for mid, count in compute_crafting_requirements(role, crafting, scoring).items()
                if (mat_defs.get(mid) or {}).get("kind") == "monster_craft"}
    if goal == "renewal":
        if renewal_source(mats) == "targets":
            glow_plan = mats.get("glow_plan") or {}
            if glow_plan.get("completed") or glow_plan.get("enabled") is False:
                return {}
            return {str(mid): int(value) for mid, value in (glow_plan.get("targets") or {}).items() if int(value) > 0}
        if not wardrobe_snapshot(role):
            return {}
        return compute_renewal_requirements(role, scoring)
    return {}


def goal_priority_requirements(
    goal: str,
    role: dict,
    crafting: dict,
    scoring: dict,
) -> dict[str, int]:
    """Priority sub-requirements (e.g. from 搭配计算) that should be stocked before general goals."""
    if goal != "renewal":
        return {}
    mats = role.get("materials") or {}
    if renewal_source(mats) != "plan" or not wardrobe_snapshot(role):
        return {}
    _, prio = compute_renewal_requirements_tiered(role, scoring)
    return prio


_JIHUA_TIEBREAK = {
    SHINY_BUBBLE_ID: 0,
    THREAD_ID: 1,
    BLING_ID: 2,
}


def _jihua_requirements(
    goal: str,
    role: dict,
    crafting: dict,
    scoring: dict,
) -> tuple[dict[str, int], dict[str, int]]:
    """Return (total_jihua_req, priority_jihua_req) for one goal."""
    mat_defs = crafting.get("materials") or {}
    if goal == "crafting":
        if not wardrobe_snapshot(role):
            return {}, {}
        raw = compute_crafting_requirements(role, crafting, scoring)
        prio_raw: dict[str, int] = {}
    elif goal == "renewal":
        raw = goal_requirements("renewal", role, crafting, scoring)
        prio_raw = goal_priority_requirements("renewal", role, crafting, scoring)
    else:
        return {}, {}
    total = {
        str(mid): int(count)
        for mid, count in raw.items()
        if int(count) > 0
        and (mat_defs.get(str(mid)) or {}).get("realm") == "jihua"
        and (mat_defs.get(str(mid)) or {}).get("level")
    }
    prio = {
        str(mid): min(int(count), total[str(mid)])
        for mid, count in prio_raw.items()
        if int(count) > 0 and str(mid) in total
    }
    return total, prio


def has_realm_material_potential(
    goal: str,
    role: dict,
    crafting: dict,
    scoring: dict,
    *,
    realm: str,
) -> bool:
    """Whether an enabled goal could possibly need any material from ``realm`` before backpack subtraction."""
    if realm == "jihua":
        return bool(_jihua_requirements(goal, role, crafting, scoring)[0])
    mat_defs = crafting.get("materials") or {}
    return any(
        int(count) > 0
        and (mat_defs.get(str(mid)) or {}).get("realm") == realm
        and (mat_defs.get(str(mid)) or {}).get("level")
        for mid, count in goal_requirements(goal, role, crafting, scoring).items()
    )


def goal_has_jihua_potential(
    goal: str,
    role: dict,
    crafting: dict,
    scoring: dict,
) -> bool:
    """Whether an enabled goal could possibly need any Jihua material before backpack subtraction."""
    return has_realm_material_potential(goal, role, crafting, scoring, realm="jihua")


def goal_has_monster_potential(
    goal: str,
    role: dict,
    crafting: dict,
    scoring: dict,
) -> bool:
    """Whether an enabled goal could possibly need any Monster material before backpack subtraction."""
    return has_realm_material_potential(goal, role, crafting, scoring, realm="monster")


def jihua_goal_deficits(
    goal: str,
    role: dict,
    inventory: dict,
    crafting: dict,
    scoring: dict,
    *,
    multiplier: int = 2,
) -> list[tuple]:
    """Jihua shortfalls: priority tier first, bubbles & thread by larger deficit before bling."""
    mat_defs = crafting.get("materials") or {}
    scale = max(1, int(multiplier))
    total_reqs, prio_reqs = _jihua_requirements(goal, role, crafting, scoring)
    items = []
    for mid, need in total_reqs.items():
        meta = mat_defs.get(mid) or {}
        base_drop = int(meta.get("drop") or 0)
        energy = int(meta.get("energy") or 0)
        if base_drop <= 0 or energy <= 0:
            continue
        owned = int(inventory.get(mid, 0))
        deficit = int(need) - owned
        if deficit > 0:
            prio_deficit = max(0, int(prio_reqs.get(mid, 0)) - owned)
            eff_deficit = prio_deficit if prio_deficit > 0 else deficit
            tier = 0 if prio_deficit > 0 else 1
            is_bling = 1 if mid == BLING_ID else 0
            runs_needed = math.ceil(eff_deficit / (base_drop * scale))
            tiebreak = _JIHUA_TIEBREAK.get(mid, 9)
            items.append((tier * 2 + is_bling, (-runs_needed, tiebreak, -eff_deficit), mid, meta, eff_deficit))
    items.sort()
    return items


def goal_deficits(
    goal: str,
    role: dict,
    inventory: dict,
    crafting: dict,
    scoring: dict,
) -> list[tuple]:
    """Farmable shortfalls: priority tier first, bubbles/crystals/thread by energy gap before bling."""
    mat_defs = crafting.get("materials") or {}
    prio_reqs = goal_priority_requirements(goal, role, crafting, scoring)
    items = []
    for mid, need in goal_requirements(goal, role, crafting, scoring).items():
        meta = mat_defs.get(str(mid))
        if not meta or not meta.get("realm") or not meta.get("level"):
            continue
        owned = int(inventory.get(str(mid), 0))
        deficit = int(need) - owned
        if deficit > 0:
            prio_deficit = max(0, int(prio_reqs.get(str(mid), 0)) - owned)
            eff_deficit = prio_deficit if prio_deficit > 0 else deficit
            tier = 0 if prio_deficit > 0 else 1
            is_bling = 1 if str(mid) == BLING_ID else 0
            base_drop = max(1, int(meta.get("drop") or 1))
            per_energy = max(1, int(meta.get("energy") or 40))
            energy_need = math.ceil(eff_deficit / base_drop) * per_energy
            tiebreak = _JIHUA_TIEBREAK.get(str(mid), 9)
            items.append((tier * 2 + is_bling, (-energy_need, tiebreak, -eff_deficit), str(mid), meta, eff_deficit))
    items.sort()
    return items


def realm_goal_deficits(
    goal: str,
    role: dict,
    inventory: dict,
    crafting: dict,
    scoring: dict,
    *,
    realm: str,
    multiplier: int = 1,
) -> list[tuple]:
    """Return unmet shortfalls for one goal restricted to ``realm`` (``jihua`` / ``monster``)."""
    if realm == "jihua":
        return jihua_goal_deficits(goal, role, inventory, crafting, scoring, multiplier=multiplier)
    return [
        item
        for item in goal_deficits(goal, role, inventory, crafting, scoring)
        if (item[3] or {}).get("realm") == realm
    ]


def needed_material_map(
    goals: list[str],
    role: dict,
    crafting: dict,
    scoring: dict,
    *,
    realm: str | None = None,
) -> dict[str, int]:
    """Sum positive material requirements across ``goals``, optionally scoped to one realm."""
    mat_defs = crafting.get("materials") or {}
    needed: dict[str, int] = {}
    for goal in goals:
        if goal not in ("renewal", "crafting"):
            continue
        for raw_mid, raw_cnt in goal_requirements(goal, role, crafting, scoring).items():
            cnt = int(raw_cnt or 0)
            if cnt <= 0:
                continue
            mid = str(raw_mid)
            if realm is not None and (mat_defs.get(mid) or {}).get("realm") != realm:
                continue
            needed[mid] = needed.get(mid, 0) + cnt
    return needed


def has_wardrobe_renewal_baseline(role: dict) -> bool:
    snap = wardrobe_snapshot(role)
    wb = role.get("wardrobe") or {}
    return bool(snap and snap.get("parts") and (wb.get("renewal_scan") or {}).get("complete"))


def goal_missing_baseline(
    goal: str,
    role: dict,
    crafting: dict,
    scoring: dict,
    *,
    realm: str | None = None,
    allow_tab2_init: bool = False,
) -> str | None:
    """Return 'wardrobe' or 'backpack' if ``goal`` lacks local baseline data."""
    if goal not in ("renewal", "crafting"):
        return None
    needs_wb = goal == "crafting" or renewal_source(role.get("materials") or {}) == "plan"
    if needs_wb and not has_wardrobe_renewal_baseline(role):
        return "wardrobe"
    req_map = needed_material_map([goal], role, crafting, scoring, realm=realm)
    if not req_map:
        return None
    tabs = required_tabs_for_mids(req_map)
    if 1 in tabs and not has_backpack_baseline(role, {1}):
        return "backpack"
    if 2 in tabs and not allow_tab2_init and not has_backpack_baseline(role, {2}):
        return "backpack"
    return None


def pick_realm_material_deficit(
    rt,
    account: AccountState,
    realm: str,
    goals: list[str],
    *,
    inventory: dict[str, int] | None = None,
    multiplier: int = 1,
    wardrobe_synced: bool = False,
    crafting: dict | None = None,
    scoring: dict | None = None,
    allow_scan: bool = True,
    allow_wardrobe_sync: bool = True,
    allow_full_wardrobe: bool = False,
    allow_init_backpack: bool = False,
) -> tuple[tuple[str, str, dict, int] | None, dict[str, int] | None, bool, bool]:
    """Pick the top unmet material shortfall for ``realm`` along ``goals``.

    When ``allow_scan`` is False, relies strictly on existing local wardrobe and
    backpack records without triggering any in-game scans.
    """
    mat_goals = [g for g in goals if g in ("renewal", "crafting")]
    if not mat_goals:
        return None, inventory, wardrobe_synced, False
    if crafting is None or scoring is None:
        crafting, scoring = load_catalogs()

    if allow_scan and allow_wardrobe_sync and not wardrobe_synced:
        role_now = account.load()
        if any(
            g == "crafting" or (g == "renewal" and renewal_source(role_now.get("materials") or {}) == "plan")
            for g in mat_goals
        ):
            wardrobe_synced = True
            ensure_wardrobe_synced_for_materials(rt, account, allow_full=allow_full_wardrobe)

    role = account.load()
    ready_goals = [
        g for g in mat_goals
        if goal_missing_baseline(
            g,
            role,
            crafting,
            scoring,
            realm=realm,
            allow_tab2_init=(allow_scan and allow_init_backpack),
        ) is None
    ]
    if not ready_goals:
        return None, inventory, wardrobe_synced, False

    has_potential = any(
        has_realm_material_potential(goal, role, crafting, scoring, realm=realm)
        for goal in ready_goals
    )
    if not has_potential:
        return None, inventory, wardrobe_synced, False

    if inventory is None:
        if allow_scan:
            needed_mids = needed_material_map(ready_goals, role, crafting, scoring, realm=realm)
            inventory = ensure_backpack_scanned(
                rt,
                account,
                crafting,
                needed_mids=needed_mids,
                allow_init_scan=allow_init_backpack,
            )
            record_material_history(account, crafting, scoring)
            role = account.load()
        else:
            inventory = dict((role.get("materials") or {}).get("inventory") or {})

    for goal in ready_goals:
        deficits = realm_goal_deficits(
            goal, role, inventory, crafting, scoring,
            realm=realm, multiplier=multiplier,
        )
        if deficits:
            _, _, mid, meta, deficit = deficits[0]
            return (goal, str(mid), meta, int(deficit)), inventory, wardrobe_synced, True
    return None, inventory, wardrobe_synced, True


def ensure_wardrobe_synced_for_materials(
    rt,
    account: AccountState,
    *,
    allow_full: bool = False,
    allow_incremental: bool = True,
) -> bool:
    """Lazily sync wardrobe & renewal levels before checking renewal/crafting shortfalls.

    - Only runs full 服装焕新 scan when ``allow_full=True`` and baseline is missing.
    - Runs fast incremental 服装焕新 scan (获得时间 descending) when PearPal cloth_num changed.
    - Returns immediately with zero UI cost when baseline & cloth_num already match.
    """
    role = account.load()
    snap = wardrobe_snapshot(role)
    wb = role.get("wardrobe") or {}
    has_wb = bool(snap and snap.get("parts"))
    has_renewal = bool((wb.get("renewal_scan") or {}).get("complete"))
    cn = (((role.get("observations") or {}).get("pearpal_notebook") or {}).get("summary") or {}).get("cloth_num")
    recorded_cn = wb.get("pearpal_cloth_num")

    need_full = not has_wb or not has_renewal
    if need_full and not allow_full:
        return False
    need_inc = (
        not need_full
        and allow_incremental
        and isinstance(cn, int)
        and not isinstance(cn, bool)
        and cn > 0
        and recorded_cn != cn
    )
    if not need_full and not need_inc:
        return True

    try:
        from wardrobe.renewal_scanner import RenewalScanner

        if need_full:
            reason = "尚无衣柜记录" if not has_wb else "尚未建立服装焕新等级记录"
            rt.log(f"{reason}，正在通过服装焕新页面进行一次完整读取（同步服装与养成等级）。")
            scanner = RenewalScanner(rt, account, mode="full")
            ok = scanner.run()
            rt.log(
                f"服装焕新读取{'完成' if ok else '未完全结束'}："
                f"同步 {scanner.summary.get('matched_groups', 0)} 组等级，"
                f"录入 {scanner.summary.get('added_items', 0)} 件服装。"
            )
            return bool(ok)

        if recorded_cn is None:
            rt.log(f"正在按获得时间快速核对服装焕新与美鸭梨记录（{cn} 件）。", live=True)
        else:
            rt.log(f"检测到美鸭梨服装数变化（{recorded_cn} → {cn}），正按获得时间进行服装焕新快速增量同步。", live=True)
        scanner = RenewalScanner(rt, account, mode="incremental")
        ok = scanner.run()
        added = scanner.summary.get("added_items", 0)
        upd = scanner.summary.get("updated_levels", 0)
        rt.log(
            f"服装焕新快速增量同步{'完成' if ok else '结束'}：新增 {added} 件服装，更新 {upd} 组等级。",
            detail=bool(ok and added == 0 and upd == 0),
        )
        return bool(ok)
    except TaskStopped:
        raise
    except Exception as error:
        if not rt.stopped:
            rt.diagnostic("wardrobe_auto_sync", str(error))
        rt.log(f"自动核对服装焕新未完成（沿用现有衣柜数据继续）：{error}。", detail=True)
        return False


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
    wardrobe_synced = False
    jihua_blocked = False
    jihua_blocked_goals: set[str] = set()
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
            needs_wb = goal == "crafting" or (goal == "renewal" and renewal_source(role.get("materials") or {}) == "plan")
            if needs_wb and not wardrobe_synced:
                wardrobe_synced = True
                ensure_wardrobe_synced_for_materials(rt, account, allow_full=False)
                role = account.load()
            missing = goal_missing_baseline(goal, role, crafting, scoring, allow_tab2_init=True)
            if missing is not None:
                if goal not in finished:
                    finished.add(goal)
                    source_hint = "衣柜与服装焕新" if missing == "wardrobe" else "背包材料库存"
                    rt.log(
                        f"幻境挑战顺序：{GOAL_LABELS[goal]}尚未在「收集进度」建立{source_hint}档案，"
                        "本次不自动全量读取，跳过该项并交给下一目标或默认副本。"
                    )
                continue
            reqs = goal_requirements(goal, role, crafting, scoring)
            all_deficits = []
            if reqs:
                if inventory is None:
                    ready_mat_goals = [
                        g for g in goals
                        if g in ("renewal", "crafting")
                        and g not in finished
                        and goal_missing_baseline(g, role, crafting, scoring, allow_tab2_init=True) is None
                    ]
                    needed_mids = needed_material_map(ready_mat_goals or [goal], role, crafting, scoring)
                    inventory = ensure_backpack_scanned(
                        rt,
                        account,
                        crafting,
                        needed_mids=needed_mids,
                        allow_init_scan=True,
                    )
                    record_material_history(account, crafting, scoring)
                    role = account.load()
                all_deficits = goal_deficits(goal, role, inventory, crafting, scoring)
                deficits = (
                    [d for d in all_deficits if (d[3] or {}).get("realm") != "jihua"]
                    if jihua_blocked
                    else all_deficits
                )
            else:
                deficits = []
            if deficits:
                picked = (goal, deficits[0])
                break
            if all_deficits and jihua_blocked:
                if goal not in jihua_blocked_goals:
                    jihua_blocked_goals.add(goal)
                    rt.log(f"幻境挑战顺序：{GOAL_LABELS[goal]}剩余缺口均为素材激化项，因激化材料不足或达今日上限，先交给下一项。")
                continue
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
        ok, energy_after, run_flags = execute_material_run(rt, kind, meta["level"], runs, use_max)
        spent = max(0, energy - energy_after)
        actual_runs = effective_realm_runs(spent, per_run, ok, planned_runs=runs)
        gained = actual_runs * per_drop
        if actual_runs > 0 or spent > 0:
            realm_label = "素材激化" if kind == "jihua" else "魔物试炼"
            try_record_adventure_entry(
                ROOT,
                rt,
                category="realm",
                task="realm_material",
                title=f"{realm_label} · {meta['level']}",
                detail=f"{GOAL_LABELS[goal]}筹备：消耗 {spent} 活跃能量 · {actual_runs} 次",
                energy_spent=spent,
                runs=actual_runs,
                items={meta["name"]: gained} if gained > 0 else {},
            )
        if gained > 0:
            inventory[mid] = int(inventory.get(mid, 0)) + gained
            record_accrual(account, mid, gained)
            record_material_history(account, crafting, scoring)
            rt.log(f"幻境挑战顺序：已获得 {meta['name']} +{gained}（当前库存 {inventory[mid]}）。")
        energy = energy_after
        if actual_runs <= 0:
            if ok and run_flags["energy_exhausted"]:
                return "done", last_kind
            if kind == "jihua" and ok and run_flags["jihua_exhausted"]:
                jihua_blocked = True
                rt.log("幻境挑战顺序：当前无可继续激化的材料或已达今日上限，跳过剩余素材激化并继续后续目标。")
                continue
            raise RuntimeError(f"{GOAL_LABELS[goal]}挑战未完成或未确认活跃能量消耗")
        last_kind = kind
        if kind == "jihua" and (run_flags["jihua_material_capped"] or run_flags["jihua_exhausted"]):
            jihua_blocked = True
        if not ok:
            rt.log(
                f"幻境挑战顺序：结算提示确认超时，但已返回奇想日历并核实消耗 {spent} 点活跃能量。",
                detail=True,
            )
    return "done", last_kind
