"""Build a complete acquisition-order baseline without upgrading embryos."""
from collections import Counter
from datetime import datetime
import re

from tall_window import tall_window
from task_control import ForegroundChanged
from .blessing import SLOTS, summarize_scan
from .locking import BlessingLocker
from .ordered import OrderedView, TallSelectUnsafe, baseline, signature
from .policy import normalize_scope, observation_fields, reconcile_exclusions
from .scanner import RescanSlot, clean_text


class _RestartOrganization(Exception):
    """Raised when warehouse capacity changes across focus switches before locking."""


class BlessingOrganizer(BlessingLocker):
    def save(self):
        super().save()
        # Persist a dirty slot before submitting any toggle. A stopped read
        # alone leaves an earlier complete baseline intact.
        count = getattr(self, "_baseline_operations", 0)
        store = getattr(self, "baseline_store", None)
        changed = getattr(self, "_baseline_slots", set())
        if store is not None:
            for operation in self.report["operations"][count:]:
                slot = operation["key"][1]
                if slot not in changed:
                    store.mark_organization_change(slot)
                    changed.add(slot)
            self._baseline_slots = changed
            self._baseline_operations = len(self.report["operations"])

    def apply_organized_lock(self, view, row, rows, box, facts, evidence, generation, target):
        for attempt in range(2):
            try:
                self.change_lock(box, facts, evidence, generation,
                                 target="locked" if target else "unlocked",
                                 reason="missing_maximum" if target else "nonconforming")
                self.confirm_selected_lock(generation)
                return
            except (RescanSlot, RuntimeError) as error:
                if attempt or self.rt.stopped or self.rt._focus_generation == generation:
                    raise
                operation = self.pending
                if operation is not None and operation.get("input_status") == "succeeded":
                    try:
                        checkpoint, frame, resumed = self.resumed_scene(view, check_initial_capacity=True)
                    except RescanSlot as rescan_err:
                        raise RuntimeError("切回后锁定操作页面发生变化，已保留操作记录，下次运行会重新整理") from rescan_err
                    if (not self.selected(frame, box)
                            or not self.same_ordered_view(checkpoint, frame, ignore_box=box)):
                        raise RuntimeError("切回后锁定操作影响了列表位置，已保留操作记录，下次运行会重新整理") from error
                    self.accept_ordered_focus(view, frame, resumed, "confirm_lock")
                    self.counter += 1
                    actual, _ = self.read_details(box, resumed, self.counter)
                    if not self.same_card_facts(actual, facts):
                        raise RuntimeError("切回后选中闪光与操作记录不一致") from error
                    self.confirm_selected_lock(resumed)
                    view.remember_view(self.clear_hover(resumed), checkpoint["boxes"])
                    return
                if operation is not None:
                    if not isinstance(error, ForegroundChanged) or operation.get("input_status") != "prepared":
                        raise
                    operation["status"] = "cancelled_before_input"
                    self.pending = None
                self.recover_ordered_focus(view, "locate", ())
                found = view.locate(row, rows, exact_lock=row["locked"])
                if found is None:
                    self._abort_or_rescan_focus("切回后整理对象未能重新确认")
                box, facts, evidence, generation = found

    def _finalize_slot(self, slot, rows, maximum, ordered):
        if not self.read_only:
            final = Counter(signature(row) for row in rows if row["locked"])
            if any(final[key] < 1 for key in maximum):
                raise RuntimeError("整理复核发现满级款色未保护，未发布基准")
            if self.allow_unlock and any(row["mutable"] and row["locked"] and
                                         (not row["maximum"] or final[signature(row)] != 1) for row in rows):
                raise RuntimeError("整理复核仍有未满级或多余锁定，未发布基准")
        for row in rows:
            if row["locked"] and row["mutable"]:
                key = (row["name"], slot, row["color"], row["level"])
                self.lock_groups[key] = self.lock_groups.get(key, 0) + 1
        ordered[slot] = rows
        self.report["slots"][slot] = {"complete": True, "audited": True,
                                        "ordering": "acquired_desc", "cells": len(rows),
                                        "copies": sum(row["count"] for row in rows)}
        self.save()

    def _plan_slot_operations(self, rows):
        locked = Counter(signature(row) for row in rows if row["locked"])
        maximum = {signature(row) for row in rows if row["maximum"] and row["mutable"]}
        keep = set()
        operations = []
        # Plan from verified existing locks before adding any lock.
        # Acquisition order does not place locked copies before stacks.
        if not self.read_only:
            for row in reversed(rows):
                key = signature(row)
                target = None
                if row["mutable"]:
                    if row["maximum"]:
                        if row["locked"]:
                            if key in keep and self.allow_unlock:
                                target = False
                            keep.add(key)
                        elif locked[key] == 0 and key not in keep:
                            target = True
                            keep.add(key)
                    elif row["locked"] and self.allow_unlock:
                        target = False
                if target is not None:
                    operations.append((row, target))
        changed_keys = {signature(row) for row, _ in operations}
        for row in rows:
            row["anchor_eligible"] = signature(row) not in changed_keys
        return maximum, operations

    def _run_slot_operations(self, view, slot, rows, maximum, operations, ordered):
        for row, target in operations:
            found = view.locate(row, rows, exact_lock=row["locked"])
            if found is None:
                already = view.locate(row, rows, exact_lock=target)
                if already is not None:
                    row["locked"] = target
                    continue
                raise RuntimeError("整理对象未在完整定位范围内找到，未修改锁定状态")
            box, facts, evidence, generation = found
            self.apply_organized_lock(view, row, rows, box, facts, evidence, generation, target)
            row["locked"] = target
            self.rt.log(f"已{'锁定' if target else '解锁'}：{row['name']}·{slot}·{row['level']}级。", detail=True)
        if operations:
            self.verified.clear()
            rows, _ = view.scan(slot, quantities=True)
        self._finalize_slot(slot, rows, maximum, ordered)

    def _ensure_inventory_ready(self):
        self.rt.check_focus()
        frame = self.rt.capture()
        if frame is not None and frame.shape[:2] == (720, 1280) and self.ready(frame):
            self.organization_generation = self.rt._focus_generation
            return
        if not self.open():
            raise RuntimeError("未能打开祝福闪光库存")
        self.rt.check_focus()
        self.organization_generation = self.rt._focus_generation

    def _run_normal_slot(self, view, slot, ordered, *, operations_only=None):
        rows_cache, maximum_cache, ops_cache = (operations_only if operations_only is not None else (None, None, None))
        for attempt in range(4):
            try:
                self._ensure_inventory_ready()
                if self.read_capacity() != self.initial_capacity:
                    raise _RestartOrganization("切回后仓库容量发生变化，重新开始整理")
                view.slot = None
                view.enter(slot)
                if ops_cache is not None and attempt == 0:
                    rows, maximum, operations = rows_cache, maximum_cache, ops_cache
                else:
                    rows, _ = view.scan(slot, quantities=True)
                    maximum, operations = self._plan_slot_operations(rows)
                self._run_slot_operations(view, slot, rows, maximum, operations, ordered)
                return
            except RescanSlot:
                if self.pending is not None or self.rt.stopped or attempt >= 3:
                    raise
                self.rt.log(f"祝福闪光整理：窗口焦点恢复后重新核对{slot}。", detail=True)

    def _run_once(self):
        self.lock_groups = {}
        if not self.open():
            raise RuntimeError("未能打开祝福闪光库存")
        self.rt.check_focus()
        self.organization_generation = self.rt._focus_generation
        self.initial_capacity = self.read_capacity()
        self.snapshot["scan"].update(view="normal_inventory", workflow="blessing_lock",
                                     reported_capacity=self.initial_capacity[0])
        ordered = {}
        view = OrderedView(self)
        for attempt in range(3):
            try:
                self.rt.check_focus()
                self.organization_generation = self.rt._focus_generation
                if not self.set_slot(SLOTS[0]):
                    raise RuntimeError("未确认" + SLOTS[0] + "列表")
                self.acquisition_sort()
                break
            except RescanSlot:
                if self.pending is not None or self.rt.stopped or attempt >= 2:
                    raise
                self._ensure_inventory_ready()
                if self.read_capacity() != self.initial_capacity:
                    raise _RestartOrganization("切回后仓库容量发生变化，重新开始整理")

        pending_lock_slots = []
        deferred_normal_slots = []
        with tall_window(self.rt) as is_tall:
            if is_tall:
                self.set_tall_layout(True)
                self.rt.log("祝福闪光整理：已切换超长竖屏模式（单屏 42 行），扫描结束后将自动恢复游戏窗口。")
            try:
                for slot_idx, slot in enumerate(SLOTS):
                    slot_done = False
                    for attempt in range(3):
                        self.rt.check_focus()
                        self.organization_generation = self.rt._focus_generation
                        try:
                            rows, _ = view.scan(slot, quantities=True)
                            maximum, operations = self._plan_slot_operations(rows)
                            if operations and is_tall:
                                pending_lock_slots.append((slot, rows, maximum, operations))
                            else:
                                self._run_slot_operations(view, slot, rows, maximum, operations, ordered)
                            slot_done = True
                            break
                        except TallSelectUnsafe:
                            if slot not in deferred_normal_slots:
                                deferred_normal_slots.append(slot)
                            self.rt.log(f"祝福闪光整理：{slot}含任务栏安全区外待核对条目，将在常规窗口下继续读取。", detail=True)
                            slot_done = True
                            break
                        except RescanSlot:
                            if self.pending is not None or self.rt.stopped:
                                raise
                            if is_tall:
                                frame = self.rt.capture()
                                if frame is None or frame.shape[:2] != (5120, 1280) or not self.ready(frame):
                                    for rem_slot in SLOTS[slot_idx:]:
                                        if rem_slot not in deferred_normal_slots:
                                            deferred_normal_slots.append(rem_slot)
                                    self.rt.log("祝福闪光整理：超长竖屏期间窗口切换，恢复常规窗口后继续剩余部位。", detail=True)
                                    slot_done = False
                                    break
                            view.slot = None
                            if attempt < 2:
                                self.rt.log(f"祝福闪光整理：窗口焦点恢复后重新核对{slot}。", detail=True)
                            else:
                                raise
                    if not slot_done:
                        break
            finally:
                self.set_tall_layout(False)

        for slot in deferred_normal_slots:
            self._run_normal_slot(view, slot, ordered)

        for slot, rows, maximum, operations in pending_lock_slots:
            if slot in ordered:
                continue
            self._run_normal_slot(view, slot, ordered, operations_only=(rows, maximum, operations))
        self.organization = baseline(ordered)
        self.report.update(complete=False, locking_complete=True, phase="organized",
                           reason="三个部位已整理，等待普通仓库数量与容量核对")
        self.save()

    def run(self):
        for restart in range(3):
            try:
                self._run_once()
                return
            except _RestartOrganization:
                if self.pending is not None or self.rt.stopped or restart >= 2:
                    raise RuntimeError("切回后仓库容量多次变化，请稍后重新整理")
                self.rt.log("祝福闪光整理：切回后检测到仓库数量变化，重新核对整份仓库。")

    def read_capacity(self):
        for attempt in range(4):
            try:
                self.rt.check_focus()
                gen = self.rt._focus_generation
                frame = self.clear_hover(gen)
                counts = self.capacity_values(frame)
                self.check_generation(gen)
                self.organization_generation = gen
                if len(counts) != 1:
                    self.image("normal_capacity_unknown.jpg", frame)
                    raise RuntimeError("普通仓库容量未能确认，未发布完整库存")
                return counts[0]
            except RescanSlot:
                if self.pending is not None or self.rt.stopped or attempt >= 3:
                    raise
                if not self.open():
                    raise RuntimeError("切回后未能恢复祝福闪光库存页面")

    def capture_inventory(self):
        """Publish the final normal-page audit; no second decomposition scan."""
        self.rt.log("三个部位已整理，正在汇总堆叠数量并与普通仓库容量核对。",
                    ui_text="正在核对仓库数量。")
        self.report["phase"] = "inventory"
        snapshot = self.snapshot
        self.inventory_snapshot = snapshot
        snapshot["items"] = []
        snapshot["excluded_items"] = []
        for rows in self.organization["slots"].values():
            for row in rows:
                snapshot["items"].append({
                    **{key: row[key] for key in ("name", "slot", "rarity", "level", "color", "count")},
                    "xp": 0, "xp_known": row["maximum"],
                    **observation_fields(row),
                    "locked_count": row["count"] if row["locked"] else 0, "lock_state_known": True,
                    "identity_known": True, "color_known": row["color"] is not None,
                    "observed": True, "evidence": [{"source": "normal_inventory",
                        "ordinal": row["ordinal"], "quantity": row["count_evidence"]}]})
        normalize_scope(snapshot, self.catalog)
        snapshot["scan"].update(excluded_observation_complete=True, traversed=False)
        # Even a failed capacity read retains the observed inventory, without
        # asserting complete quantities or a reusable organization baseline.
        self.organization["complete"] = False
        snapshot["organization"] = self.organization
        if self.read_capacity() != self.initial_capacity:
            raise RuntimeError("整理前后容量发生变化，未发布完整库存，下次运行会重新整理")
        traversed = all(self.report["slots"].get(slot, {}).get("complete") for slot in SLOTS)
        reconcile_exclusions(snapshot, self.catalog, traversed=traversed)
        summarize_scan(snapshot, traversed, "普通仓库数量核对未完成")
        complete = snapshot["scan"]["identity_complete"]
        snapshot["locks"] = {
            "complete": complete, "source": "normal_inventory", "traversal_mode": "acquired_desc_audit",
            "observed_at": datetime.now().isoformat(timespec="seconds"), "groups": [
                {"name": key[0], "slot": key[1], "color": key[2], "level": key[3], "locked_count": count}
                for key, count in sorted(self.lock_groups.items(), key=lambda pair: (
                    pair[0][0], pair[0][1], pair[0][2] or "", pair[0][3]))]}
        self.organization["complete"] = complete
        self.report["quantity_check"] = {
            "reported": self.initial_capacity[0],
            "regular": snapshot["scan"]["recorded_copies"],
            "special": snapshot["scan"]["excluded_copies"], "complete": complete}
        if not complete:
            snapshot["scan"]["warnings"].append("普通仓库堆叠数量与容量不一致，未发布全收集基准。")
        snapshot["scan"]["warnings"].append("未读取等级内已积累经验，中间等级的升级预算按等级起点估算。")
        unknown = sorted({row["name"] for rows in self.organization["slots"].values()
                          for row in rows if row.get("catalog_unknown")})
        if unknown:
            snapshot["scan"]["warnings"].append(
                "图鉴外闪光仅记录实测库存，不自动整理或追全：" + "、".join(unknown) + "。请更新图鉴。")
        self.report.update(complete=complete, phase="complete" if complete else "inventory_incomplete",
                           reason="三个部位整理及普通仓库数量核对完成" if complete else snapshot["scan"]["reason"])
        return snapshot
