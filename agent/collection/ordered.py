"""Acquisition-order observations and bounded, anchor-delimited lookups.

Coordinates are hints, never item identities. Only a complete interval between
immutable locked anchors (or physical list ends) can prove an item absent.
"""
from collections import Counter
from copy import deepcopy
from functools import wraps
import re
from uuid import uuid4
from task_control import task_time as time

from .blessing import SLOTS
from .policy import capabilities, observation_fields
from .quantity import read_count
from .scanner import LEVEL, RescanSlot, clean_text


def resumable_observation(method):
    """Only organization opts into a bounded retry of a read/selection."""
    @wraps(method)
    def run(self, *args, **kwargs):
        for attempt in range(2):
            try:
                return method(self, *args, **kwargs)
            except (RescanSlot, RuntimeError):
                recover = getattr(self.o, "recover_ordered_focus", None)
                if (attempt or self._recovering_focus or self.o.rt.stopped or recover is None
                        or self.generation == self.o.rt._focus_generation):
                    raise
                result = recover(self, method.__name__, args)
                if method.__name__ == "step":
                    return result
        raise RuntimeError("前台恢复次数超过上限")
    return run


class IncompleteLookup(RuntimeError):
    """A read-only interval observation failed; saved identities are intact."""


def signature(row):
    return row["name"], row["slot"], row["level"], row.get("color") if row["maximum"] else None


def anchor(row):
    return signature(row) if (row["locked"] and row["maximum"] and row.get("mutable", True)
                              and row.get("anchor_eligible", True)) else None


def anchors(rows):
    counts = Counter(anchor(row) for row in rows if anchor(row))
    return {anchor(row): index for index, row in enumerate(rows)
            if anchor(row) and counts[anchor(row)] == 1}


def pending(row):
    return row["rarity"] == 5 and not row["maximum"] and not row["locked"] and row["collectible"]


def make_row(owner, facts, locked, ordinal):
    flags = capabilities(owner.catalog, facts["name"], observed=facts)
    return {**{key: facts[key] for key in ("name", "slot", "rarity", "level", "color")},
            **observation_fields(facts),
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
        self._focus_checkpoint = None
        self._page_frame = None
        self._page_cells = []
        self._recovering_focus = False

    def remember_view(self, frame, boxes):
        self._focus_checkpoint = {"frame": frame, "boxes": list(boxes),
                                  "slot": self.slot, "position": self.position}

    @resumable_observation
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
        self._focus_checkpoint = None

    @resumable_observation
    def step(self, direction):
        before = self.o.clear_hover(self.generation, settle_card=True)
        boxes = self._focus_checkpoint["boxes"] if self._focus_checkpoint else []
        self.remember_view(before, boxes)
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
        self.remember_view(after, [[x, y - direction * delta, w, h] for x, y, w, h in boxes
                                  if self.o.grid[1] <= y - direction * delta
                                  and y - direction * delta + h <= self.o.grid[1] + self.o.grid[3]])
        self._page_frame, self._page_cells = None, []
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

    @resumable_observation
    def cells(self, *, at_bottom=False):
        frame = self.o.clear_hover(self.generation)
        if not self.o.rt.text([930, 15, 330, 52], self.slot, image=frame):
            raise RuntimeError("获得时间列表部位变化")
        roi = list(self.o.grid)
        if not at_bottom:
            roi[3] -= self.o.layout.get("bottom_fade_margin", 32)
        self.labels = self.o.ocr(roi, frame)
        # Stack counts share the page OCR, but are not level baselines and
        # must never create extra card rows or shift their coordinates.
        levels = [item for item in self.labels
                  if re.fullmatch(LEVEL, clean_text(item.text), re.IGNORECASE)]
        boxes = self.o.cards(frame, levels)
        if not boxes and not self.o.rt.text(self.o.grid, "暂无|没有|未获得|空空如也", image=frame):
            raise RuntimeError("获得时间列表格子无法读取")
        # Contours can still discover cards in the faded strip. Defer the
        # entire card, including its count and lock glyph, until it is clear.
        visible = len(boxes)
        boxes = [box for box in boxes if box[1] + box[3] <= roi[1] + roi[3]]
        self.o.report["reading_window"] = {"slot": self.slot, "position": self.position,
                                            "roi": roi, "at_bottom": at_bottom,
                                            "deferred_cards": visible - len(boxes)}
        values = [self.o.level(box, levels) for box in boxes]
        matches = self.o.matcher.match_batch(frame, boxes, levels=values)
        self._page_frame, self._page_cells = frame, list(zip(boxes, values, matches))
        self.remember_view(frame, boxes)
        return frame, self._page_cells

    def pages(self, limit):
        """Read clear cards first, then include the tail only at a proven end."""
        for _ in range(limit):
            frame, cells = self.cells()
            yield frame, cells, False
            if self.step(1) == 0 and self.step(1) == 0:
                # The bottom fade disappears at the physical list end. Read
                # this viewport again before coverage or absence is decided.
                frame, cells = self.cells(at_bottom=True)
                self.o.image(f"bottom_{self.slot}_{self.o.counter:05d}.png", frame)
                yield frame, cells, True
                return

    @resumable_observation
    def observe(self, frame, cell, *, verify_selection=False, quantities=False):
        if self._page_frame is not None and frame is not self._page_frame:
            # A resumed page has fresh OCR, icon and quantity observations.
            original_box = cell[0]
            frame = self._page_frame
            cell = next((value for value in self._page_cells
                         if all(abs(a - b) <= 2 for a, b in zip(value[0], original_box))), None)
            if cell is None:
                raise RuntimeError("恢复后目标格子位置未能确认")
        box, level, match = cell
        self.o.counter += 1
        facts, evidence = self.o.confirm_copy(frame, box, match, level, self.generation, self.o.counter)
        if facts is None or facts["slot"] != self.slot:
            raise RuntimeError("顺序记录身份或部位无法确认")
        flags = capabilities(self.o.catalog, facts["name"], observed=facts)
        inspect_lock = flags["lock_mutable"] or facts.get("catalog_unknown")
        state = self.o.card_lock_state(frame, box, self.labels) if inspect_lock else "locked"
        stats = self.o.report.setdefault("selection_stats", {"badge_reads": 0, "clicked_reads": 0})
        selected = inspect_lock and (verify_selection or state is None)
        if selected:
            state = self.o.select(box, facts, self.generation, verify_details=verify_selection)
            stats["clicked_reads"] += 1
        else:
            stats["badge_reads"] += 1
        locked = state == "locked"
        ordinal = self.ordinal(box)
        row = make_row(self.o, facts, locked, ordinal)
        observation = {**evidence, "selected_for_observation": selected}
        if quantities:
            try:
                row["count"], row["count_evidence"] = read_count(
                    self.o, frame, box, locked, self.labels, self.generation, self.o.counter)
            except RuntimeError as error:
                self.o.record_card(frame, box, row, observation, self.generation, self.o.counter, error=str(error))
                raise
        self.o.record_card(frame, box, row, observation, self.generation, self.o.counter)
        return row, facts, observation

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
        for frame, cells, at_bottom in self.pages(limit):
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
                        raise RuntimeError("已有锁定参照顺序改变，请重新运行祝福闪光仓库读取")
                    matched.append(key)
                    if matched == expected:
                        self.o.check_coverage(seen)
                        return rows, old_anchors[key] + 1
            self.o.rt.log(f"祝福闪光{slot}：{'读取新增前缀' if old is not None else '记录获得顺序'}，已读取 {len(rows)} 格。", detail=True)
            if time.monotonic() - last_notice >= 30:
                self.o.rt.log(f"正在处理{slot}，已查看 {len(rows)} 格。")
                last_notice = time.monotonic()
            if at_bottom:
                if expected and matched != expected:
                    raise RuntimeError("未找到完整的旧锁定参照，请重新整理")
                if seen:
                    self.o.check_coverage(seen)
                return rows, len(old or [])
        raise RuntimeError("新增区域超过定位上限，请重新运行祝福闪光仓库读取建立基准")

    def locate(self, target, rows, *, exact_lock=None, quantities=False):
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
                if quantities:
                    self.target_quantity(frame, cell[0], row, evidence)
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

        for frame, cells, at_bottom in self.pages(self.LOCAL_PAGES):
            stats["local_pages"] += 1
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
                    if quantities:
                        self.target_quantity(frame, box, row, evidence)
                    self.select_target(box, facts, row["locked"], evidence)
                    target["ordinal"] = row["ordinal"]
                    return box, facts, evidence, self.generation
            if left_seen and right_seen:
                self.o.report["lookup_interval"] = {
                    "target": {key: target[key] for key in ("name", "slot", "level", "ordinal")},
                    "left": list(left[0]) if left else None, "left_ordinal": left_ordinal,
                    "right": list(right[0]), "right_ordinal": right_ordinal,
                    "observed_ordinals": sorted(observed_ordinals)}
                if right_ordinal <= left_ordinal:
                    raise RuntimeError("定位参照的实际顺序与记录不一致，已保存参照位置")
                if complete_interval(right_ordinal):
                    stats["confirmed_absent"] += 1
                    return None
                raise IncompleteLookup("定位区间读取不完整，已保留待处理项，可重新运行")
            if at_bottom:
                end = max(observed_ordinals, default=-1) + 1
                if left_seen and right is None and complete_interval(end):
                    stats["confirmed_absent"] += 1
                    return None
                break
        raise RuntimeError("目标附近未能确认对象或完整边界，已停止局部查找；请重新整理，未反复扫描全仓")

    def target_quantity(self, frame, box, row, evidence):
        # Only the selected upgrade candidate needs a quantity. Singles use
        # the badge shape directly; stacks reuse the page's existing OCR.
        try:
            count, proof = read_count(self.o, frame, box, row["locked"], self.labels,
                                      self.generation, self.o.counter)
        except RuntimeError as error:
            self.o.record_card(frame, box, row, evidence, self.generation, self.o.counter, error=str(error))
            raise
        evidence.update(copies_before=count, quantity=proof)
        row.update(count=count, count_evidence=proof)
        self.o.record_card(frame, box, row, evidence, self.generation, self.o.counter)

    @resumable_observation
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
    return {"version": 1, "anchor_policy_version": 2, "complete": True, "sort": "acquired_desc",
            "slots": ordered, "next_priority": priority, "unfinished": None,
            "pending_operation": None, "reason": None}
