"""Build a complete acquisition-order baseline without upgrading embryos."""
from collections import Counter
from datetime import datetime
import re

from task_control import ForegroundChanged
from .blessing import SLOTS, summarize_scan
from .locking import BlessingLocker
from .ordered import OrderedView, baseline, signature
from .policy import normalize_scope, observation_fields, reconcile_exclusions
from .scanner import RescanSlot, clean_text


class BlessingOrganizer(BlessingLocker):
    def same_ordered_cards(self, checkpoint, frame, *, shift=0, ignore_box=None):
        before = checkpoint["frame"]
        checked = []
        for box in checkpoint["boxes"]:
            if ignore_box is not None and list(box) == list(ignore_box):
                continue
            x, y, w, h = box
            target_y = y - shift
            if not self.grid[1] <= target_y <= self.grid[1] + self.grid[3] - h - 32:
                continue
            # Compare translated patches with the existing edge matcher. The
            # level/count/lock strip must agree as well as the central artwork.
            selection_changed = self.selected(before, box) != self.selected(frame, [x, target_y, w, h])
            for dx, dy, width, height in ((12, 22, w - 24, h - 52), (5, h - 29, w - 10, 23)):
                old = self.region(before, [x + dx, y + dy, width, height])
                new = self.region(frame, [x + dx, target_y + dy, width, height])
                threshold = .90 if dy == 22 and selection_changed else .97
                if not self.same_structure(old, new, [0, 0, width, height], threshold=threshold):
                    return False
            checked.append(box)
        if shift:
            return len({box[0] for box in checked}) == self.layout["columns"]
        return bool(checked) or ignore_box is not None and len(checkpoint["boxes"]) == 1

    def same_ordered_view(self, checkpoint, frame, *, ignore_box=None):
        """Check every clear card plus the scroll thumb, ignoring hover lighting."""
        # Equal artwork alone cannot distinguish identical physical copies.
        return (self.same_ordered_cards(checkpoint, frame, ignore_box=ignore_box)
                and self.same_pixels(checkpoint["frame"], frame, [1256, 72, 14, 550]))

    def resumed_scene(self, view):
        checkpoint = view._focus_checkpoint
        if checkpoint is None or checkpoint["slot"] != view.slot:
            raise RuntimeError("切回后缺少当前页参照，请重新读取仓库")
        generation = self.rt._focus_generation
        if not self.rt.action("TouchMove", target=[900, 32]) or not self.rt.pause(.2):
            raise RuntimeError("前台恢复被中断")
        first = self.rt.capture()
        if not self.rt.pause(.12):
            raise RuntimeError("前台恢复被中断")
        frame = self.rt.capture()
        if (frame is None or frame.shape[:2] != (720, 1280)
                or not self.same_structure(first, frame, self.grid, threshold=.997)
                or not self.rt.text([930, 15, 330, 52], view.slot, image=frame)
                or not self.rt.text(self.layout["sort_label"], "获得时间", image=frame)
                or self.sort_direction(frame, self._acquired_desc_template) != "desc"):
            raise RuntimeError("切回后列表页面、部位或排序未能确认，请重新读取仓库")
        labels = self.ocr(self.layout["capacity"], frame)
        capacities = [re.search(r"(\d+)[/／](\d+)", clean_text(item.text)) for item in labels
                      if float(getattr(item, "score", 0)) >= .90]
        capacities = [(int(match[1]), int(match[2])) for match in capacities if match]
        if capacities != [self.initial_capacity] or self.rt._focus_generation != generation:
            raise RuntimeError("切回后仓库容量或前台状态发生变化，请重新读取仓库")
        return checkpoint, frame, generation

    def accept_ordered_focus(self, view, frame, generation, phase, *, delta=0):
        self.check_generation(generation)
        view.generation = self.organization_generation = generation
        self._hover_read = self._hover_box = None
        self._page_frame = self._ready_frame = frame
        self._ready_generation = generation
        self.verified.clear()
        view._page_frame, view._page_cells = None, []
        self.report.setdefault("focus_recoveries", []).append({
            "time": datetime.now().isoformat(timespec="seconds"), "slot": view.slot,
            "phase": phase, "position": view.position, "delta": delta, "generation": generation})
        self.rt.log("已核对当前列表，继续读取祝福闪光仓库。")

    def recover_ordered_focus(self, view, phase, args):
        if view._recovering_focus:
            raise RuntimeError("局部核对期间再次发生前台切换，请重新读取仓库")
        view._recovering_focus = True
        try:
            return self._recover_ordered_focus(view, phase, args)
        finally:
            view._recovering_focus = False

    def _recover_ordered_focus(self, view, phase, args):
        checkpoint, frame, generation = self.resumed_scene(view)
        delta = 0
        if not self.same_ordered_view(checkpoint, frame):
            if phase != "step":
                raise RuntimeError("切回后当前页条目或滚动位置改变，请重新读取仓库")
            direction = args[0]
            delta = (self.scroll_position(checkpoint["frame"], frame) if direction > 0
                     else self.scroll_position(frame, checkpoint["frame"]))
            if (delta is None or delta <= 2
                    or not self.same_ordered_cards(checkpoint, frame, shift=direction * delta)):
                raise RuntimeError("切回后翻页位移未能确认，请重新读取仓库")
        if phase == "step":
            direction = args[0]
            view.position = max(0, checkpoint["position"] + direction * delta)
            boxes = [[x, y - direction * delta, w, h] for x, y, w, h in checkpoint["boxes"]
                     if self.grid[1] <= y - direction * delta
                     and y - direction * delta + h <= self.grid[1] + self.grid[3]]
        else:
            boxes = checkpoint["boxes"]
        self.accept_ordered_focus(view, frame, generation, phase, delta=delta)
        view.remember_view(frame, boxes)
        if phase != "step":
            view.cells()
            if phase == "select_target":
                args[3]["selected_for_observation"] = False
        return delta

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
                    checkpoint, frame, resumed = self.resumed_scene(view)
                    if (not self.selected(frame, box)
                            or not self.same_ordered_view(checkpoint, frame, ignore_box=box)):
                        raise RuntimeError("切回后锁定操作影响了列表位置，已保留操作记录，请重新读取仓库") from error
                    self.accept_ordered_focus(view, frame, resumed, "confirm_lock")
                    self.counter += 1
                    actual, _ = self.read_details(box, resumed, self.counter)
                    if self.key(actual) != self.key(facts) or actual["level"] != facts["level"]:
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
                    raise RuntimeError("切回后整理对象未能重新确认") from error
                box, facts, evidence, generation = found

    def run(self):
        if not self.open():
            raise RuntimeError("未能打开祝福闪光库存")
        self.organization_generation = self.rt._focus_generation
        self.initial_capacity = self.read_capacity()
        self.snapshot["scan"].update(view="normal_inventory", workflow="blessing_lock",
                                     reported_capacity=self.initial_capacity[0])
        self.inventory_read = True
        ordered = {}
        view = OrderedView(self)
        try:
            for slot in SLOTS:
                self.check_generation(self.organization_generation)
                rows, _ = view.scan(slot, quantities=True)
                locked = Counter(signature(row) for row in rows if row["locked"])
                maximum = {signature(row) for row in rows if row["maximum"] and row["mutable"]}
                keep = set()
                operations = []
                # Plan from verified existing locks before adding any lock.
                # Acquisition order does not place locked copies before stacks.
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
                    # Multiple identical locked copies are interchangeable for
                    # toggling, but their inferred remaining position is not.
                    # Only unchanged unique locks can delimit these lookups.
                    row["anchor_eligible"] = signature(row) not in changed_keys
                for row, target in operations:
                    found = view.locate(row, rows, exact_lock=row["locked"])
                    if found is None:
                        raise RuntimeError("整理对象未在完整定位范围内找到，未修改锁定状态")
                    box, facts, evidence, generation = found
                    self.apply_organized_lock(view, row, rows, box, facts, evidence, generation, target)
                    row["locked"] = target
                    self.rt.log(f"已{'锁定' if target else '解锁'}：{row['name']}·{slot}·{row['level']}级。", detail=True)
                # Unlocks may merge backwards. One final ordered audit is
                # necessary only for slots changed during organization.
                if operations:
                    self.verified.clear()
                    rows, _ = view.scan(slot, quantities=True)
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
        finally:
            self.inventory_read = False
        self.organization = baseline(ordered)
        self.report.update(complete=False, locking_complete=True, phase="organized",
                           reason="三个部位已整理，等待普通仓库数量与容量核对")
        self.save()

    def read_capacity(self):
        frame = self.clear_hover(self.organization_generation)
        labels = self.ocr(self.layout["capacity"], frame)
        self.check_generation(self.organization_generation)
        counts = []
        for item in labels:
            match = re.search(r"(\d+)[/／](\d+)", clean_text(item.text))
            if match and float(getattr(item, "score", 0)) >= .90:
                used, limit = int(match.group(1)), int(match.group(2))
                if 0 <= used <= limit <= 10000:
                    counts.append((used, limit))
        if len(counts) != 1:
            self.image("normal_capacity_unknown.png", frame)
            raise RuntimeError("普通仓库容量未能确认，未发布完整库存")
        return counts[0]

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
            raise RuntimeError("整理前后容量发生变化，未发布完整库存，请重新整理")
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
