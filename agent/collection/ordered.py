"""Acquisition-order observations and bounded, anchor-delimited lookups.

Coordinates are hints, never item identities. Only a complete interval between
immutable locked anchors (or physical list ends) can prove an item absent.
"""
from collections import Counter
from copy import deepcopy
import re
from uuid import uuid4
from task_control import task_time as time

from .blessing import SLOTS
from .policy import capabilities
from .quantity import read_count
from .scanner import LEVEL, clean_text


def signature(row):
    return row["name"], row["slot"], row["level"], row.get("color") if row["maximum"] else None


def anchor(row):
    return signature(row) if row["locked"] and row["maximum"] and row.get("anchor_eligible", True) else None


def anchors(rows):
    counts = Counter(anchor(row) for row in rows if anchor(row))
    return {anchor(row): index for index, row in enumerate(rows)
            if anchor(row) and counts[anchor(row)] == 1}


def pending(row):
    return row["rarity"] == 5 and not row["maximum"] and not row["locked"] and row["collectible"]


def make_row(owner, facts, locked, ordinal):
    flags = capabilities(owner.catalog, facts["name"])
    return {**{key: facts[key] for key in ("name", "slot", "rarity", "level", "color")},
            "id": uuid4().hex, "locked": locked, "maximum": owner.is_maximum(facts),
            "collectible": flags["collection_target"], "mutable": flags["lock_mutable"],
            "ordinal": ordinal, "priority": 0}


class OrderedView:
    LOCAL_PAGES = 6
    PREFIX_PAGES = 24

    def __init__(self, owner):
        self.o = owner
        self.slot = None
        self.position = 0
        self.generation = None

    def enter(self, slot):
        if self.slot == slot:
            self.o.check_generation(self.generation)
            return
        if not self.o.set_slot(slot):
            raise RuntimeError("未确认" + slot + "列表")
        self.o.acquisition_sort()
        self.generation = self.o.rt._focus_generation
        if self.o.top() is None:
            raise RuntimeError("未确认获得时间列表顶部")
        self.slot, self.position = slot, 0

    def step(self, direction):
        before = self.o.clear_hover(self.generation, settle_card=True)
        if direction > 0:
            after, delta = self.o.advance(before, self.generation)
        else:
            after, delta = None, None
            # Reverse the two frames to reuse the verified downward matcher.
            amounts = self.o.scroll_amounts()
            for attempt, amount in enumerate(amounts):
                self.o._scroll_diagnostics = {}
                if not self.o.scroll_list(amount):
                    break
                after = self.o.frame(self.generation)
                delta = 0 if self.o.unchanged(before, after) else self.o.scroll_position(after, before)
                self.o.record_scroll(before, after, amount, delta, "ordered_up")
                if delta is not None:
                    break
                if attempt == len(amounts) - 1 or not self.o.scroll_list(-amount):
                    break
                restored = self.o.frame(self.generation)
                if not self.o.unchanged(before, restored):
                    break
        if after is None or delta is None:
            raise RuntimeError("获得时间列表位移无法确认，请重新整理")
        self.position = max(0, self.position + direction * delta)
        return delta

    def seek(self, ordinal):
        target = max(0, (ordinal // self.o.layout["columns"] - 1) * self.o.layout["row_pitch"])
        # Navigation only: no tooltip audit while moving to a saved area.
        rows = int(abs(target - self.position) / self.o.layout["row_pitch"]) + 2
        limit = max(160, min(2000, rows * self.o.layout["columns"] * 2 + 12))
        for _ in range(limit):
            distance = target - self.position
            if target != 0 and abs(distance) < self.o.layout["row_pitch"] * 2:
                return
            direction = 1 if distance > 0 else -1
            if self.step(direction) == 0:
                if direction < 0:
                    self.position = 0
                return
        raise RuntimeError("定位滚动超过上限，请重新整理")

    def cells(self):
        frame = self.o.clear_hover(self.generation)
        if not self.o.rt.text([930, 15, 330, 52], self.slot, image=frame):
            raise RuntimeError("获得时间列表部位变化")
        self.labels = self.o.ocr(self.o.grid, frame)
        # Stack counts share the page OCR, but are not level baselines and
        # must never create extra card rows or shift their coordinates.
        levels = [item for item in self.labels
                  if re.fullmatch(LEVEL, clean_text(item.text), re.IGNORECASE)]
        boxes = self.o.cards(frame, levels)
        if not boxes and not self.o.rt.text(self.o.grid, "暂无|没有|未获得|空空如也", image=frame):
            raise RuntimeError("获得时间列表格子无法读取")
        values = [self.o.level(box, levels) for box in boxes]
        matches = self.o.matcher.match_batch(frame, boxes, levels=values)
        return frame, list(zip(boxes, values, matches))

    def observe(self, frame, cell, *, verify_selection=False, quantities=False):
        box, level, match = cell
        self.o.counter += 1
        facts, evidence = self.o.confirm_copy(frame, box, match, level, self.generation, self.o.counter)
        if facts is None or facts["slot"] != self.slot:
            raise RuntimeError("顺序记录身份或部位无法确认")
        flags = capabilities(self.o.catalog, facts["name"])
        state = "locked" if not flags["lock_mutable"] else self.o.card_lock_state(frame, box, self.labels)
        stats = self.o.report.setdefault("selection_stats", {"badge_reads": 0, "clicked_reads": 0})
        selected = flags["lock_mutable"] and (verify_selection or state is None)
        if selected:
            state = self.o.select(box, facts, self.generation, verify_details=verify_selection)
            stats["clicked_reads"] += 1
        else:
            stats["badge_reads"] += 1
        locked = state == "locked"
        ordinal = self.ordinal(box)
        row = make_row(self.o, facts, locked, ordinal)
        if quantities:
            row["count"], row["count_evidence"] = read_count(
                self.o, frame, box, locked, self.labels, self.generation, self.o.counter)
        if not flags["collection_target"]:
            self.o.special_items[(facts["name"], facts["slot"])] = {
                **{key: facts[key] for key in ("name", "slot", "rarity", "color", "level")},
                **flags, "count": 1, "xp": 0, "xp_known": False,
                "locked_count": int(locked), "lock_state_known": True, "observed": True}
        return row, facts, {**evidence, "selected_for_observation": selected}

    def ordinal(self, box):
        return (round((box[1] + self.position - self.o.grid[1]) / self.o.layout["row_pitch"])
                * self.o.layout["columns"]
                + round((box[0] - self.o.layout["first_x"]) / self.o.layout["column_pitch"]))

    def scan(self, slot, *, old=None, quantities=False):
        """One full organization audit, or only the newly acquired prefix."""
        self.slot = None
        self.enter(slot)
        self.o.rt.log(f"{'整理' if quantities else '检查新增闪光'}：{slot}。")
        last_notice = time.monotonic()
        old_anchors = anchors(old or [])
        expected = list(old_anchors)[:3]
        matched = []
        rows, seen = [], []
        # Full audits must still reach the end after reducing the wheel step.
        # Prefix lookups retain their separate, small bound.
        copies = self.o.snapshot["scan"].get("reported_capacity") or len(old or [])
        limit = self.PREFIX_PAGES if expected else max(160, min(2000, copies * 2 + 12))
        if old is not None and not expected:
            self.o.rt.log(f"{slot}尚无唯一满级锁定参照，本轮仅在开始时完整读取该部位一次。",
                          ui_text=f"正在读取{slot}完整列表，建立定位参照。")
        for page in range(limit):
            frame, cells = self.cells()
            for cell in cells:
                box = cell[0]
                point = (box[0], box[1] + self.position)
                if any(abs(point[0] - x) < 12 and abs(point[1] - y) < 12 for x, y in seen):
                    continue
                seen.append(point)
                row, _, _ = self.observe(frame, cell, quantities=quantities)
                rows.append(row)
                if len(rows) % 6 == 0:
                    self.o.report["traversal"] = {"slot": slot, "visited_cells": len(rows)}
                    self.o.save()
                    self.o.rt.log(f"祝福闪光{slot}：已核对 {len(rows)} 格。", detail=True)
                key = anchor(row)
                if old is not None and key in old_anchors:
                    if len(matched) >= len(expected) or key != expected[len(matched)]:
                        raise RuntimeError("已有锁定参照顺序改变，请重新运行祝福闪光整理")
                    matched.append(key)
                    if matched == expected:
                        self.o.check_coverage(seen)
                        return rows, old_anchors[key] + 1
            self.o.rt.log(f"祝福闪光{slot}：{'读取新增前缀' if old is not None else '记录获得顺序'}，已读取 {len(rows)} 格。", detail=True)
            if time.monotonic() - last_notice >= 30:
                self.o.rt.log(f"正在处理{slot}，已查看 {len(rows)} 格。")
                last_notice = time.monotonic()
            if self.step(1) == 0 and self.step(1) == 0:
                if expected and matched != expected:
                    raise RuntimeError("未找到完整的旧锁定参照，请重新整理")
                if seen:
                    self.o.check_coverage(seen)
                return rows, len(old or [])
        raise RuntimeError("新增区域超过定位上限，请重新运行祝福闪光整理建立基准")

    def locate(self, target, rows, *, exact_lock=None):
        """Check a bounded neighbourhood, with anchor proof for absence.

        Fast icon filtering applies only to exclusion; mutations always verify
        tooltip identity, selection highlight and the actual lock button.
        """
        self.enter(target["slot"])
        stats = self.o.report.setdefault("lookup_stats", {
            "current_view_hits": 0, "local_lookups": 0, "local_pages": 0, "confirmed_absent": 0})
        duplicates = sum(signature(row) == signature(target) and row["locked"] == target["locked"]
                         for row in rows) > 1
        # Esc retains this viewport. Most consecutive embryos are found here;
        # do not scroll back to an interval boundary after every upgrade.
        frame, cells = self.cells()
        for cell in ([] if duplicates else cells):
            _, level, match = cell
            candidate = match.get("candidate") or {}
            if match.get("identity_accepted") and candidate.get("name") != target["name"]:
                continue
            if level is not None and level != 1 and level != target["level"]:
                continue
            row, facts, evidence = self.observe(frame, cell)
            expected_lock = False if exact_lock is None else exact_lock
            if signature(row) == signature(target) and row["locked"] == expected_lock:
                self.select_target(cell[0], facts, expected_lock, evidence)
                target["ordinal"] = row["ordinal"]
                stats["current_view_hits"] += 1
                return cell[0], facts, evidence, self.generation
        index = next(i for i, row in enumerate(rows) if row["id"] == target["id"])
        stable = anchors(rows)
        before = [(key, i) for key, i in stable.items() if i < index]
        after = [(key, i) for key, i in stable.items() if i > index]
        left = before[-1] if before else None
        right = after[0] if after else None
        # Leave two screenfuls for shifts from consumed stacks. Never turn a
        # stale coordinate into a click or an unbounded search from the top.
        hint = target["ordinal"]
        if left and hint - rows[left[1]]["ordinal"] <= 18:
            hint = rows[left[1]]["ordinal"]
        self.seek(max(0, hint - 18))
        stats["local_lookups"] += 1
        self.o.rt.log(f"祝福闪光{target['slot']}：在记录位置附近查找 {target['name']}·{target['level']}级，最多 {self.LOCAL_PAGES} 页。", detail=True)
        left_seen = left is None and self.position == 0
        right_seen = False
        left_ordinal = -1 if left_seen else None
        right_ordinal = None
        observed_ordinals = set()
        seen = []

        def complete_interval(end):
            return (left_ordinal is not None and end > left_ordinal
                    and all(value in observed_ordinals for value in range(left_ordinal + 1, end)))

        for _ in range(self.LOCAL_PAGES):
            stats["local_pages"] += 1
            frame, cells = self.cells()
            for cell in cells:
                box, level, match = cell
                point = (box[0], box[1] + self.position)
                if any(abs(point[0] - x) < 12 and abs(point[1] - y) < 12 for x, y in seen):
                    continue
                seen.append(point)
                observed_ordinals.add(self.ordinal(box))
                candidate = match.get("candidate") or {}
                possible_target = not match.get("identity_accepted") or candidate.get("name") == target["name"]
                possible_anchor = any(bound and (not match.get("identity_accepted") or candidate.get("name") == bound[0][0])
                                      for bound in (left, right))
                if not possible_target and not possible_anchor:
                    continue
                row, facts, evidence = self.observe(frame, cell)
                key = anchor(row)
                if left and key == left[0]:
                    left_seen = True
                    left_ordinal = row["ordinal"]
                    rows[left[1]]["ordinal"] = row["ordinal"]
                if right and key == right[0]:
                    right_seen = True
                    right_ordinal = row["ordinal"]
                    rows[right[1]]["ordinal"] = row["ordinal"]
                if (signature(row) == signature(target)
                        and (not duplicates or left_seen and not right_seen)
                        and (exact_lock is None and not row["locked"] or row["locked"] == exact_lock)):
                    self.select_target(box, facts, row["locked"], evidence)
                    target["ordinal"] = row["ordinal"]
                    return box, facts, evidence, self.generation
            if left_seen and right_seen:
                if complete_interval(right_ordinal):
                    stats["confirmed_absent"] += 1
                    return None
                raise RuntimeError("定位区间存在格子识别空缺，未将目标记为已消耗")
            if self.step(1) == 0 and self.step(1) == 0:
                end = max(observed_ordinals, default=-1) + 1
                if left_seen and right is None and complete_interval(end):
                    stats["confirmed_absent"] += 1
                    return None
                break
        raise RuntimeError("目标附近未能确认对象或完整边界，已停止局部查找；请重新整理，未反复扫描全仓")

    def select_target(self, box, facts, locked, evidence):
        """Only matching mutation targets require selected-detail verification."""
        selected = evidence.get("selected_for_observation", False)
        state = self.o.select(box, facts, self.generation, verify_details=True, already_selected=selected)
        stats = self.o.report.setdefault("selection_stats", {"badge_reads": 0, "clicked_reads": 0})
        stats["clicked_reads"] += int(not selected)
        if state != ("locked" if locked else "unlocked"):
            raise RuntimeError("选中目标后锁定状态发生变化，未操作锁按钮")


def merge_prefix(old, prefix, cut, next_priority):
    """Moved/merged old stacks retain their oldest queue priority."""
    available = list(old)
    for row in prefix:
        same = [item for item in available
                if signature(item) == signature(row) and item["locked"] == row["locked"]]
        if same:
            prior = min(same, key=lambda item: item["priority"])
            row.update(id=prior["id"], priority=prior["priority"])
            available.remove(prior)
    for row in reversed(prefix):
        if row["priority"] == 0:
            row["priority"] = next_priority
            next_priority += 1
    used = {row["id"] for row in prefix}
    suffix = [deepcopy(row) for row in old[cut:] if row["id"] not in used]
    shift = (prefix[-1]["ordinal"] - old[cut - 1]["ordinal"]
             if prefix and cut else len(prefix))
    for row in suffix:
        row["ordinal"] = max(0, row["ordinal"] + shift)
    return prefix + suffix, next_priority


def baseline(ordered):
    priority = 1
    for slot in SLOTS:
        for row in reversed(ordered[slot]):
            row["priority"] = priority
            priority += 1
    return {"version": 1, "complete": True, "sort": "acquired_desc",
            "slots": ordered, "next_priority": priority, "unfinished": None,
            "pending_operation": None, "reason": None}
