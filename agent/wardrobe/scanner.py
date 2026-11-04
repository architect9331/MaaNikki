"""Read acquisition-ordered wardrobe cards without selecting or changing outfits."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import math
import re
from types import SimpleNamespace

import cv2
import numpy as np

from account_state import AccountState
from collection.scanner import InventoryScanner, RescanSlot
from daily.settings import ROOT
from file_io import write_json
from task_control import TaskStopped, task_time as time
from .model import WardrobeModel
from .progress import (MODEL_ONLY_PARTS, card_body, decode_head, encode_head, find_head,
                       find_visual_head, head_anchor, head_capacity, legacy_heads, ordered_prefix)


HEADER = [930, 69, 325, 30]
SORT = [1055, 679, 159, 40]


def clean(text):
    return re.sub(r"[^\w\u4e00-\u9fff]", "", text)


class WardrobeScanner(InventoryScanner):
    def __init__(self, runtime, folder):
        self.rt, self.folder = runtime, folder
        self.model = WardrobeModel(ROOT / "resource/model/wardrobe")
        self.account = AccountState.for_runtime(ROOT, runtime)
        # Geometry is the supplied 1920x1080 UI scaled to Maa's 1280x720 frame.
        # Border contours are 98x104 inside the 103x107 training-card canvas.
        self.layout = {"grid": [929, 112, 325, 568], "card_size": [103, 107],
                       "first_x": 933, "column_pitch": 108, "row_pitch": 113,
                       "first_y": 118, "row_pitch_exact": 170 * 2 / 3,
                       "columns": 3, "scroll_step": 840, "scroll_anchor_ocr": False}
        self.grid = self.layout["grid"]
        # Reuse the existing shared-column shape/colour scroll verification.
        self.matcher = SimpleNamespace(color_threshold=.80, color_margin=.025)
        self._scroll_diagnostics = {}
        self._partial_rows = []
        self._page_frame, self.current_part = None, None
        self.ui_parts = set(self.model.parts) - MODEL_ONLY_PARTS
        self.snapshot = {"schema_version": 2, "model_version": self.model.version,
                         "uid": self.account.uid,
                         "label_fingerprint": self.model.fingerprint, "sort": "acquired_desc",
                         "identity_policy": "model_candidates", "identity_verified": False,
                         "started_at": datetime.now(timezone.utc).isoformat(),
                         "updated_at": None, "complete": False, "parts": {},
                         "total_parts": len(self.ui_parts), "verified_parts": [],
                         "skipped_categories": [], "scan": {"pages": [], "scrolls": []}}
        wardrobe = self.account.load().get("wardrobe", {})
        previous = wardrobe.get("last_scan") or wardrobe.get("inventory")
        self._previous = {}
        self._legacy_visuals = None
        if (isinstance(previous, dict) and previous.get("sort") == "acquired_desc"
                and previous.get("model_version") == self.model.version
                and previous.get("label_fingerprint") == self.model.fingerprint
                and isinstance(previous.get("parts"), dict)):
            self._previous = deepcopy(previous["parts"])
            self.snapshot["parts"] = deepcopy(self._previous)
            self.snapshot["resumed_from"] = previous.get("updated_at")

    def category(self, frame):
        header = self.rt.text(HEADER, image=frame)
        value = clean(header)
        matches = []
        for part, info in self.model.parts.items():
            names = (info["name"], info["name"].split("·")[-1])
            if any(value.startswith(clean(name)) for name in names):
                matches.append(part)
        return (matches[0] if len(matches) == 1 else None), header

    def ready(self, frame):
        if frame is None:
            return False
        if self._page_frame is not None and all(self.same_structure(frame, self._page_frame, roi)
                for roi in ([387, 18, 193, 45], [25, 615, 270, 85], HEADER)):
            return True
        if not self.rt.hit("MaaNikki_WardrobeReady", image=frame):
            return False
        part, _ = self.category(frame)
        return self.current_part is None or part == self.current_part

    def open(self):
        if self.rt.ui.page_matches("MaaNikki_WardrobeReady"):
            return True
        if not self.rt.main():
            return False
        return self.rt.ui.enter_page(lambda: self.rt.key("dress"), "MaaNikki_WardrobeReady",
                                     source="MaaNikki_MainDetected", attempts=2)

    def acquisition_sort(self):
        # Acquisition time defaults to newest first in this wardrobe UI. Its
        # direction icon differs from the Eureka page; checking that unrelated
        # template blocks an otherwise valid list. Keep the game's default
        # direction and only select/confirm the sort criterion.
        if self.rt.text(SORT, "获得时间"):
            return
        if (not self.rt.action("Click", target=SORT) or not self.rt.pause(.2)
                or not self.rt.click_text("^获得时间$", [1050, 340, 190, 339])):
            raise RuntimeError("未能切换衣柜获得时间排序")
        if not self.rt.text(SORT, "获得时间"):
            raise RuntimeError("未确认衣柜获得时间排序")

    def zoom_out(self):
        # Category changes zoom in on the equipped body part. Wheel input in
        # the character panel resets that zoom without touching the list.
        if (not self.rt.action("Scroll", target=[500, 350], dx=0, dy=-12000)
                or not self.rt.action("TouchMove", target=[420, 32]) or not self.rt.pause(.25)):
            raise TaskStopped()

    @staticmethod
    def scroll_thumb(frame):
        # The fixed scrollbar track is outside the translucent card panel.
        # A filled narrow capsule is its thumb; the track outline has little
        # enclosed area. Keep the existing card-contour area criterion.
        if frame is None:
            return None
        strip = frame[116:663, 1260:1272]
        edges = cv2.Canny(cv2.cvtColor(strip, cv2.COLOR_BGR2GRAY), 35, 90)
        contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        thumbs = set()
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if (abs(x - 3) <= 2 and abs(w - 6) <= 2 and h > w
                    and cv2.contourArea(contour) / ((w - 1) * (h - 1)) > .8):
                thumbs.add((y + 116, y + h + 116))
        return next(iter(thumbs)) if len(thumbs) == 1 else None

    def at_top(self, frame):
        thumb = self.scroll_thumb(frame)
        boxes = self.cards(frame)
        return bool(thumb and thumb[0] == 120 and boxes
                    and abs(min(box[1] for box in boxes) - self.layout["first_y"]) < 4)

    def cards(self, frame, levels=None):
        gray = cv2.cvtColor(self.crop(frame), cv2.COLOR_BGR2GRAY)
        edges = cv2.morphologyEx(cv2.Canny(gray, 35, 90), cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        gx, gy, _, gh = self.grid
        rows = []
        self._partial_rows = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if abs(w - 98) > 2:
                continue
            column = round((x + gx - 4 - self.layout["first_x"]) / 108)
            if not 0 <= column < 3 or abs(x + gx - (937 + column*108)) > 2:
                continue
            top = y + gy - 2
            # The bottom gradient can hide the bottom border. Retain evidence
            # of that row, so a stationary wheel cannot silently omit it.
            if 40 <= h < 102 and top > gy + gh - 107:
                self._partial_rows.append(top)
            if not (abs(h - 104) <= 2 and cv2.contourArea(contour) / (w*h) > .8):
                continue
            row = next((row for row in rows if abs(row["top"] - top) < 4), None)
            if row is None:
                row = {"top": top, "columns": set()}
                rows.append(row)
            row["columns"].add(column)
        # Artwork can join the frame's Canny edges, so a complete card need
        # not have a closed rectangular contour. Recover it from its bottom
        # border independently of contour area. The fixed three-column layout
        # and row pitch constrain the search; empty trailing slots have no
        # border and must never become model inputs. The 90px line leaves 4px
        # at either end of the 98px border for its corner decoration.
        pitch = self.layout["row_pitch"]
        expected_tops = [row["top"] + step * pitch for row in rows
                         for step in range(-math.ceil(gh / pitch), math.ceil(gh / pitch) + 1)]
        horizontal = cv2.morphologyEx(edges, cv2.MORPH_OPEN, np.ones((1, 90), np.uint8))
        lines, _ = cv2.findContours(horizontal, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        recovered = []
        for line in lines:
            x, y, w, h = cv2.boundingRect(line)
            column = round((x + gx - 937) / self.layout["column_pitch"])
            border_left = 937 + column * self.layout["column_pitch"]
            if (not 0 <= column < self.layout["columns"] or h > 4
                    or abs(x + gx - border_left) > 6
                    or abs(x + gx + w - (border_left + 98)) > 6):
                continue
            # The outer lower edge is 105px below the training-canvas top;
            # the inner edge lies 6px above it. Retain the existing 4px
            # geometry tolerance, so those two edges cannot seed two rows.
            candidate_top = y + gy - 105
            expected_top = min(expected_tops, key=lambda expected: abs(candidate_top - expected),
                               default=None)
            if (expected_top is None or abs(candidate_top - expected_top) >= 4
                    or expected_top < gy or expected_top + 107 > gy + gh):
                continue
            # Several edge fragments can describe the same border a few
            # pixels apart. Snap them to the contour-derived row lattice so
            # they cannot create duplicate rows or accumulate scroll drift.
            top = round(expected_top)
            row = next((row for row in rows if abs(row["top"] - top) < 4), None)
            if row is None:
                row = {"top": top, "columns": set()}
                rows.append(row)
            if column not in row["columns"]:
                recovered.append([column, row["top"]])
            row["columns"].add(column)
        self._border_recovered = recovered
        boxes = []
        valid_rows = [row for row in sorted(rows, key=lambda row: row["top"])
                      if gy <= row["top"] and row["top"] + 107 <= gy + gh]
        for index, row in enumerate(valid_rows):
            top = row["top"]
            # A detected right-hand cell or any lower row in the row-major grid
            # proves every preceding cell in this row is occupied.
            has_lower_row = (index + 1 < len(valid_rows)
                             or any(partial > top + 4 for partial in self._partial_rows))
            last_column = self.layout["columns"] - 1 if has_lower_row else max(row["columns"])
            for column in range(last_column + 1):
                boxes.append([933 + 108*column, top, 103, 107])
        return boxes

    def scroll_list(self, amount, frame):
        # The final row may leave its middle/right cells empty. Wheel input
        # must land on an occupied card, not merely inside the grid rectangle.
        # Reuse the caller's frame and pick the first complete left-column cell.
        boxes = self.cards(frame)
        left = [box for box in boxes if box[0] == self.layout["first_x"]]
        x, y, w, h = min(left, key=lambda box: box[1]) if left else [
            self.layout["first_x"], self.grid[1], *self.layout["card_size"]]
        target = [round(x + w / 2), round(y + h / 2)]
        return (self.rt.action("Scroll", target=target, dx=0, dy=amount)
                and self.rt.action("TouchMove", target=[420, 32]) and self.rt.pause(.25))

    def unchanged(self, before, after):
        if self.same_structure(before, after, self.grid, threshold=.997):
            return True
        # Unoccupied slots are transparent: on a short list, the character
        # animation behind them changes even when scrolling has stopped.
        # Keep the same image threshold, but compare actual cards at the same
        # positions rather than letting empty background prevent termination.
        boxes = self.cards(before)
        before_partial = list(self._partial_rows)
        other = self.cards(after)
        if not boxes or len(boxes) != len(other):
            return False
        if any(abs(a[0] - b[0]) >= 4 or abs(a[1] - b[1]) >= 4
               for a, b in zip(boxes, other)):
            return False
        # A partially visible new row is not an empty slot. Leave it to the
        # full-grid / translation checks rather than masking its motion out.
        if any(not any(abs(top - box[1]) < 4 for box in detected)
               for partial, detected in ((before_partial, boxes), (self._partial_rows, other))
               for top in partial):
            return False
        thumb = self.scroll_thumb(before)
        if thumb and thumb[1] == 659 and thumb == self.scroll_thumb(after):
            # At the lower track end, the same thumb and card coordinates
            # prove the wheel could not move the list, even if a translucent
            # card's background continues animating.
            return True
        return all(self.same_structure(before, after, box, threshold=.997) for box in boxes)

    def top(self):
        before = self.rt.capture()
        generation = self.rt._focus_generation
        if self.at_top(before):
            return before
        # Each category can contain its catalogued cards plus missing assets.
        # A large upward scroll avoids walking back one page at a time.
        limit = math.ceil(self.model.parts[self.current_part]["item_ids"] / 3) + 2
        for attempt in range(limit):
            if not self.scroll_list(12000, before):
                raise TaskStopped()
            after = self.rt.capture()
            self.check_generation(generation)
            if not self.ready(after):
                raise RuntimeError("衣柜页面发生变化")
            if self.at_top(after) or self.unchanged(before, after):
                return after
            if attempt == limit - 1:
                self.snapshot["scan"].setdefault("bounds", []).append({
                    "part": self.current_part, "phase": "top", "attempts": limit,
                    "before": self.image(f"{self.current_part}_top_before.jpg", before),
                    "after": self.image(f"{self.current_part}_top_after.jpg", after)})
            before = after
        raise RuntimeError("未能确认衣柜列表顶部")

    def advance(self, before, generation):
        # Keep shared cards within the 348px translation search. Seven wheel
        # notches moved this wardrobe by 299px in the recorded runs; the old
        # fifteen-notch attempt exhausted the overlap and forced a rollback
        # before every successful seven-notch page.
        for amount in (self.layout["scroll_step"], 120):
            if not self.scroll_list(-amount, before):
                raise TaskStopped()
            after = self.rt.capture()
            self.check_generation(generation)
            if not self.ready(after):
                raise RuntimeError("衣柜翻页后页面发生变化")
            self._scroll_diagnostics = {}
            if self.unchanged(before, after):
                delta = 0
                self._scroll_diagnostics = {"anchor_source": "stationary", "delta": 0}
            else:
                delta = self.scroll_position(before, after)
            self.record_scroll(before, after, -amount, delta, "down")
            if delta is not None:
                return after, delta
            if amount == 120:
                break
            if not self.scroll_list(amount, after):
                raise TaskStopped()
            restored = self.rt.capture()
            self.check_generation(generation)
            if not (self.unchanged(before, restored)
                    or (self.ready(restored) and self.scroll_position(before, restored) == 0)):
                raise RuntimeError("衣柜翻页位置未确认，已保存本次记录")
        raise RuntimeError("衣柜翻页位移未确认")

    def save(self, reason=None):
        self.snapshot["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.snapshot["reason"] = reason
        self.snapshot["scanned_count"] = sum(p["scanned_count"] for p in self.snapshot["parts"].values())
        self.snapshot["ambiguous_count"] = sum(p["ambiguous_count"] for p in self.snapshot["parts"].values())
        self.snapshot["unconfirmed_count"] = sum(p["unconfirmed_count"] for p in self.snapshot["parts"].values())
        self.snapshot["completed_parts"] = sum(p["complete"] for p in self.snapshot["parts"].values())
        # UI polling uses the same UID JSON. Keep scroll diagnostics in the
        # run report rather than rewriting screenshots/events into the journal.
        record = {k: v for k, v in self.snapshot.items() if k != "scan"}
        def change(data):
            wardrobe = data.setdefault("wardrobe", {})
            wardrobe["last_scan"] = record
            if record["complete"]:
                wardrobe["inventory"] = record
        write_json(self.folder / "scan.json", self.snapshot)
        self.account.update(change)

    def read_part(self, part):
        self.current_part = part
        self.zoom_out()
        self.acquisition_sort()
        generation = self.rt._focus_generation
        frame = self.top()
        self._page_frame = frame
        old_state = self._previous.get(part, {})
        old_items = ordered_prefix(old_state, self.layout)
        anchor = head_anchor(old_items, self.layout)
        reference = decode_head(old_state.get("head_images"), self.layout)
        if old_items and not reference:
            if self._legacy_visuals is None:
                self._legacy_visuals = legacy_heads(ROOT / "logs/wardrobe", self._previous,
                    self.layout, self.model.fingerprint, self.account.uid)
            reference = decode_head(self._legacy_visuals.get(part), self.layout)
        state = {"name": self.model.parts[part]["name"], "items": [], "scanned_count": 0,
                 "ambiguous_count": 0, "unconfirmed_count": 0, "complete": False}
        # Keep the previous checkpoint until its head has been found. Stopping
        # while checking newly acquired clothes must not replace it with only
        # the few new head cards observed so far.
        if anchor is None and not reference:
            self.snapshot["parts"][part] = state
        known, observed, position, reused, boundary = {}, {}, 0, 0, None

        def reuse(at):
            nonlocal known, reused, boundary
            boundary = at
            # New acquisitions prepend to a permanent ordered list. Preserve
            # column shifts too when their count is not divisible by three.
            known = {**{boundary + index: item for index, item in enumerate(old_items)}, **known}
            reused = len(old_items)
            self.snapshot["parts"][part] = state
            state["resume"] = {"head_offset": boundary, "reused_count": reused}

        def materialize():
            items = []
            for slot, value in sorted(known.items()):
                row, column = divmod(slot, self.layout["columns"])
                point = [self.layout["first_x"] + column * self.layout["column_pitch"],
                         round(self.layout["first_y"] + row * self.layout["row_pitch_exact"], 3)]
                items.append({**value, "slot": slot, "position": point, "order": slot + 1})
            state.update(items=items, scanned_count=len(items), reused_count=reused,
                         ambiguous_count=sum(len(item["item_ids"]) > 1 for item in items),
                         unconfirmed_count=sum(item["source"] == "selected_card_unconfirmed" for item in items))

        for page in range(4096):
            self.check_generation(generation)
            boxes = self.cards(frame)
            if not boxes:
                if not known:
                    state["reason"] = "未识别到完整衣服卡片"
                    if anchor is None and not reference:
                        self.snapshot["parts"][part] = state
                    self.save()
                    self.rt.log(f"衣柜读取：{state['name']}待核对。")
                    return
                raise RuntimeError(f"{state['name']}未发现完整卡片，请确认衣柜筛选已清空")
            slots = [round((box[1] + position - self.layout["first_y"]) / self.layout["row_pitch_exact"])
                     * self.layout["columns"]
                     + round((box[0] - self.layout["first_x"]) / self.layout["column_pitch"])
                     for box in boxes]
            if any(slot < 0 for slot in slots) or len(set(slots)) != len(slots):
                raise RuntimeError(f"{state['name']}卡片行列位置未确认，已保存读取进度")
            prior_boundary = boundary
            for slot, box in zip(slots, boxes):
                if boundary is None or slot < head_capacity(self.layout):
                    observed[slot] = card_body(frame, box)
            if reference and boundary is None:
                matched, evidence = find_visual_head(observed, reference, self.layout)
                self.snapshot["scan"].setdefault("resume_checks", []).append({
                    "part": part, "page": page, "matched_offset": matched, **evidence})
                if matched is not None:
                    reuse(matched)
            fresh = [(slot, box) for slot, box in zip(slots, boxes) if slot not in known]
            new = [box for _, box in fresh]
            predictions = self.model.predict(frame, new, part)
            # Selection persists when the pointer is moved away. Its visible
            # title supplies independent identity evidence for the gold card.
            header = self.rt.text(HEADER, image=frame) if page == 0 and new else None
            if page == 0:
                self._selected_header = header
            self.model.correct_selection(frame, new, predictions, part, self._selected_header)
            self.check_generation(generation)
            for (slot, _), prediction in zip(fresh, predictions):
                known[slot] = prediction
            if anchor is not None and boundary is None:
                matched = find_head(known, anchor)
                if matched is not None:
                    reuse(matched)
                    self.snapshot["scan"].setdefault("resume_checks", []).append({
                        "part": part, "page": page, "basis": "item_ids", "matched_offset": matched})
            materialize()
            state["head_images"] = encode_head(observed, self.layout)
            self.snapshot["scan"]["pages"].append({
                "part": part, "page": page, "new": len(new), "boxes": new, "offset": position,
                "border_recovered": self._border_recovered,
                "image": self.image(f"{part}_{page:04d}.jpg", self.crop(frame)) if new else None})
            if new or page == 0 or (prior_boundary is None and boundary is not None):
                self.save()
            if (boundary is not None and old_state.get("complete")
                    and len(old_items) == len(old_state.get("items", []))
                    and max(known) < boundary + len(old_items)):
                break
            after, delta = self.advance(frame, generation)
            if delta == 0:
                after, delta = self.advance(after, generation)
                if delta == 0:
                    self.cards(after)
                    if any(round((top + position - self.layout["first_y"])
                                 / self.layout["row_pitch_exact"]) * self.layout["columns"] not in known
                           for top in self._partial_rows):
                        raise RuntimeError(f"{state['name']}底部卡片未完整显示，已保存读取进度")
                    break
            position += delta
            frame = after
        else:
            raise RuntimeError("衣柜读取达到页数上限")
        # Acquisition slots encode physical rows AND columns, so a missing
        # interior cell/row still prevents a complete checkpoint.
        if sorted(known) != list(range(len(known))):
            raise RuntimeError(f"{state['name']}列表存在未读取的格子，已保留部分记录")
        self.snapshot["parts"][part] = state
        state["complete"] = True
        if part not in self.snapshot["verified_parts"]:
            self.snapshot["verified_parts"].append(part)
        self.save()
        detail = f"，沿用 {reused} 件，新读 {len(known) - reused} 件" if boundary is not None else ""
        self.rt.log(f"衣柜读取：{state['name']} {state['scanned_count']} 件{detail}。")

    def change_category(self, key, previous_header):
        if not self.rt.key(key, delay=.25):
            raise TaskStopped()
        deadline = time.monotonic() + 5
        while not self.rt.stopped and time.monotonic() < deadline:
            frame = self.rt.capture()
            if (self.rt.hit("MaaNikki_WardrobeReady", image=frame)
                    and self.category(frame)[1] != previous_header):
                return frame
            self.rt.pause(.1)
        if self.rt.stopped:
            raise TaskStopped()
        return None

    def first_category(self):
        # W/S traverse a bounded list, not a ring. Resume may start at the
        # previous failure's final tab, so navigate back to hair before S.
        frame = self.rt.capture()
        direction = 87
        for _ in range(len(self.model.parts) * 2 + 2):
            if not self.rt.hit("MaaNikki_WardrobeReady", image=frame):
                raise RuntimeError("未确认换衣间页面")
            part, header = self.category(frame)
            if part == "210":
                return
            after = self.change_category(direction, header)
            if after is None:
                if direction == 83:
                    break
                # A non-item tab can precede hair. At the upper endpoint,
                # search forward once rather than assuming W wraps around.
                direction = 83
                frame = self.rt.capture()
            else:
                frame = after
        raise RuntimeError("未能切换到衣柜发型部位")

    def run(self):
        if not self.open():
            raise RuntimeError("未能打开换衣间")
        self.rt.action("TouchMove", target=[420, 32])
        self.rt.pause(.2)
        self.first_category()
        self.acquisition_sort()
        first, visited = None, set()
        previous_header = None
        # Includes non-item categories (such as outfit sets) between model parts.
        for _ in range(len(self.model.parts) * 2 + 2):
            frame = self.rt.capture()
            if not self.rt.hit("MaaNikki_WardrobeReady", image=frame):
                raise RuntimeError("未确认换衣间页面")
            part, header = self.category(frame)
            if part is not None:
                if first is None:
                    first = part
                elif part == first:
                    break
                if part in visited:
                    raise RuntimeError("衣柜部位顺序发生变化，已保存本次记录")
                self.rt.log(f"衣柜读取：开始读取{self.model.parts[part]['name']}。", detail=True)
                while True:
                    if self.rt.stopped:
                        raise TaskStopped()
                    try:
                        self.read_part(part)
                        break
                    except RescanSlot:
                        self._page_frame = None
                        resumed = self.rt.capture()
                        if self.category(resumed)[0] != part:
                            raise RuntimeError("切回游戏后衣柜部位已变化，请重新读取")
                        if part in self.snapshot["parts"]:
                            self._previous[part] = deepcopy(self.snapshot["parts"][part])
                        self.rt.log(f"衣柜读取：重新核对{self.model.parts[part]['name']}。")
                visited.add(part)
            else:
                self.snapshot["skipped_categories"].append(header)
            # S advances to ability/Eureka tabs after clothing and does not
            # wrap at the final tab. Completion is established by the actual
            # required parts read in this run, not by returning to the first.
            if self.ui_parts.issubset(self.snapshot["verified_parts"]):
                break
            previous_header = header
            self.current_part, self._page_frame = None, None
            if self.change_category(83, previous_header) is None:
                raise RuntimeError("按 S 后未确认衣柜部位切换")
        else:
            raise RuntimeError("未确认衣柜部位循环终点")
        missing = [info["name"] for p, info in self.model.parts.items() if p in self.ui_parts
                   and (p not in visited or not self.snapshot["parts"].get(p, {}).get("complete"))]
        self.snapshot["complete"] = not missing
        self.save("待核对部位：" + "、".join(missing) if missing else None)
        return self.snapshot["complete"]
