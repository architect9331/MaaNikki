"""Inventory-targeted realm farming for glow-up preparation and crafting material collection."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import re

from account_state import AccountState
from daily.gameplay import Gameplay
from daily.runtime import Executors
from daily.settings import ROOT
from .backpack import ensure_backpack_scanned, wardrobe_snapshot, wardrobe_snapshot_key


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


def compute_crafting_requirements(role: dict, crafting: dict, scoring: dict) -> dict[str, int]:
    owned = owned_score_ids(role, scoring)
    materials_state = role.get("materials") or {}
    scope = materials_state.get("crafting_scope", "all")
    totals: dict[str, int] = {}
    for sketch in crafting.get("sketches") or []:
        if str(sketch["id"]) in owned:
            continue
        if scope == "suits" and sketch.get("category") != "suit":
            continue
        for mid, cnt in (sketch.get("costs") or {}).items():
            totals[str(mid)] = totals.get(str(mid), 0) + int(cnt)
    wb_key = wardrobe_snapshot_key(role)
    if wb_key and materials_state.get("wardrobe_baseline_key") == wb_key:
        consumed = materials_state.get("crafted_consumed") or {}
        for mid, used in consumed.items():
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


def run_glow_prep(rt) -> bool:
    account = AccountState.for_runtime(ROOT, rt)
    role = account.load()
    mats_state = role.get("materials") or {}
    glow_plan = mats_state.get("glow_plan") or {}
    targets = glow_plan.get("targets") or {}
    if not glow_plan.get("enabled", True) or glow_plan.get("completed") or not targets:
        rt.log("焕新材料筹备：当前无待刷取的焕新材料目标，跳过。")
        return True

    crafting, _ = load_catalogs()
    mat_defs = crafting.get("materials") or {}
    inventory = ensure_backpack_scanned(rt, account, crafting)
    role = account.load()
    targets = ((role.get("materials") or {}).get("glow_plan") or {}).get("targets") or {}

    def pending_deficits():
        items = []
        for mid, target_val in targets.items():
            meta = mat_defs.get(str(mid))
            if not meta or "realm" not in meta:
                continue
            current = int(inventory.get(str(mid), 0))
            deficit = int(target_val) - current
            if deficit > 0:
                prio = 0 if meta["kind"] == "monster_glow" else 1
                items.append((prio, -deficit, str(mid), meta, deficit))
        items.sort()
        return items

    deficits = pending_deficits()
    if not deficits:
        record_accrual(account, mark_glow_completed=True)
        rt.log("焕新材料筹备：目标材料库存均已达标，跳过。")
        return True

    if not rt.ui.calendar():
        return False
    game = Gameplay(rt)
    energy = game.read_energy()
    if energy is None:
        rt.log("焕新材料筹备：无法读取当前活跃能量，本次停止。")
        return False

    while not rt.stopped and deficits:
        _, _, mid, meta, deficit = deficits[0]
        kind = meta["realm"]
        e_per_run = int(meta["energy"])
        d_per_run = int(meta["drop"])
        if energy < game.MIN_ENERGY[kind]:
            rt.log(f"焕新材料筹备：剩余活跃能量 {energy} 点，不足继续挑战，今日筹备结束。")
            return True
        runs_needed = math.ceil(deficit / d_per_run)
        max_runs = energy // e_per_run
        runs_to_do = min(runs_needed, max_runs)
        use_max = runs_needed >= max_runs
        rt.log(f"焕新材料筹备：{meta['name']} 还差 {deficit}，计划挑战「{meta['level']}」{runs_to_do} 次。")
        ok, energy_after = execute_material_run(rt, kind, meta["level"], runs_to_do, use_max)
        spent = max(0, energy - energy_after)
        actual_runs = spent // e_per_run
        gained = actual_runs * d_per_run
        if gained > 0:
            inventory[mid] = int(inventory.get(mid, 0)) + gained
            remaining = pending_deficits()
            record_accrual(account, mid, gained, mark_glow_completed=not remaining)
            rt.log(f"焕新材料筹备：已获得 {meta['name']} +{gained}（当前库存 {inventory[mid]}）。")
        energy = energy_after
        if not ok or spent == 0:
            break
        deficits = pending_deficits()

    if not pending_deficits():
        rt.log("焕新材料筹备：全部焕新目标材料已刷齐！剩余活跃能量交由后续任务继续使用。")
    return True


def run_crafting_pursuit(rt) -> bool:
    account = AccountState.for_runtime(ROOT, rt)
    role = account.load()
    if not wardrobe_snapshot(role):
        rt.log("制衣材料全收集：尚未读取过衣柜，本次跳过；请先在「衣柜与搭配」中读取衣柜以建立设计图缺口清单。")
        return True

    crafting, scoring = load_catalogs()
    mat_defs = crafting.get("materials") or {}
    inventory = ensure_backpack_scanned(rt, account, crafting)
    role = account.load()
    required = compute_crafting_requirements(role, crafting, scoring)

    def crystal_deficits():
        items = []
        for mid, meta in mat_defs.items():
            if meta.get("kind") != "monster_craft":
                continue
            req = int(required.get(mid, 0))
            cur = int(inventory.get(mid, 0))
            deficit = req - cur
            if deficit > 0:
                items.append((-deficit, mid, meta, deficit, req, cur))
        items.sort()
        return items

    deficits = crystal_deficits()
    if not deficits:
        rt.log("制衣材料全收集：所有未制作设计图的魔物试炼晶石已全部集齐！本次不消耗活跃能量。")
        return True

    if not rt.ui.calendar():
        return False
    game = Gameplay(rt)
    energy = game.read_energy()
    if energy is None:
        rt.log("制衣材料全收集：无法读取当前活跃能量，本次停止。")
        return False

    while not rt.stopped and deficits:
        _, mid, meta, deficit, req, cur = deficits[0]
        kind = meta.get("realm", "monster")
        e_per_run = int(meta.get("energy", 40))
        d_per_run = int(meta.get("drop", 5))
        if energy < game.MIN_ENERGY[kind]:
            rt.log(f"制衣材料全收集：剩余活跃能量 {energy} 点，不足一次试炼挑战，今日结束。")
            return True
        runs_needed = math.ceil(deficit / d_per_run)
        max_runs = energy // e_per_run
        runs_to_do = min(runs_needed, max_runs)
        use_max = runs_needed >= max_runs
        rt.log(f"制衣材料全收集：{meta['name']}（已有 {cur}/{req}，差 {deficit}），挑战「{meta['level']}」{runs_to_do} 次。")
        ok, energy_after = execute_material_run(rt, kind, meta["level"], runs_to_do, use_max)
        spent = max(0, energy - energy_after)
        gained = (spent // e_per_run) * d_per_run
        if gained > 0:
            inventory[mid] = int(inventory.get(mid, 0)) + gained
            record_accrual(account, mid, gained)
            rt.log(f"制衣材料全收集：{meta['name']} +{gained}（进度 {inventory[mid]}/{req}）。")
        energy = energy_after
        if not ok or spent == 0:
            break
        deficits = crystal_deficits()

    if not crystal_deficits():
        rt.log("制衣材料全收集：恭喜！所有未制作设计图的魔物试炼晶石已全部刷齐！")
    return True
