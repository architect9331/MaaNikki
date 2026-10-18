"""Build a complete acquisition-order baseline without upgrading embryos."""
from collections import Counter
from datetime import datetime
import re

from .blessing import SLOTS, summarize_scan
from .locking import BlessingLocker
from .ordered import OrderedView, baseline, signature
from .policy import normalize_scope, reconcile_exclusions
from .scanner import clean_text


class BlessingOrganizer(BlessingLocker):
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
                    self.change_lock(box, facts, evidence, generation,
                                     target="locked" if target else "unlocked",
                                     reason="missing_maximum" if target else "nonconforming")
                    self.confirm_selected_lock(generation)
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
                    "locked_count": int(row["locked"]), "lock_state_known": True,
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
        self.report.update(complete=complete, phase="complete" if complete else "inventory_incomplete",
                           reason="三个部位整理及普通仓库数量核对完成" if complete else snapshot["scan"]["reason"])
        return snapshot
