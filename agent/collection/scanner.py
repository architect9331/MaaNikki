"""Read-only, page-batched Eureka scanning on the decomposition inventory."""
from __future__ import annotations

from datetime import datetime
import math
import re
import time

import cv2
import numpy as np

from daily.runtime import parameters
from daily.settings import ROOT
from .blessing import summarize_scan
from .icons import IconMatcher
from .details import COLOR_OCR_THRESHOLD, HEADER, VerifiedVariants, tooltip_facts
from .policy import reconcile_exclusions


LEVEL = r"(?:等级|[Ll][Vv]|级)[\s.．·:：]*(10|[0-9])(?=\D|$)"


def clean_text(value):
    return re.sub(r"\s+", "", value).replace("：", ":")


class RescanSlot(Exception):
    """Restart traversal when a foreground pause invalidates list positions."""


class InventoryScanner:
    def __init__(self, runtime, catalog, folder):
        self.rt, self.catalog, self.folder = runtime, catalog, folder
        self.layout = parameters(runtime.context, "MaaNikki_Blessing_Layout")
        self.tooltip_initial_wait = float(self.layout.get("tooltip_initial_wait", .14))
        if not math.isfinite(self.tooltip_initial_wait) or not 0 <= self.tooltip_initial_wait <= 1:
            raise ValueError("悬停初始等待必须在 0～1 秒内")
        self.grid = self.layout["grid"]
        self._scroll_diagnostics = {}
        self._ready_frame, self._ready_generation = None, None
        self._page_frame, self._tooltip_roi = None, None
        self.matcher = IconMatcher(ROOT / "resource/image/blessing", catalog,
                                   threshold=self.layout.get("icon_threshold", .72),
                                   margin=self.layout.get("icon_margin", .035),
                                   color_threshold=self.layout.get("color_threshold", .80),
                                   color_margin=self.layout.get("color_margin", .025))
        self.verified = VerifiedVariants(self.matcher)
        self.snapshot = {"schema_version": 1, "created": datetime.now().isoformat(timespec="seconds"),
                         "items": [], "excluded_items": [], "unresolved": [], "scan": {
                             "complete": False, "slots": {}, "reported_capacity": None,
                             "excluded_owned": [], "excluded_observation_complete": False,
                             "view": "decomposition", "pages": [], "scrolls": [], "warnings": [],
                             "verification_policy": "tooltip_first_variant",
                             "tooltip_verified": 0, "cached_verified": 0,
                             "tooltip_seconds": 0.0, "tooltip_ocr_reads": 0,
                             "reason": "扫描尚未完成"}}

    def image(self, filename, frame):
        if frame is not None:
            png = filename.lower().endswith(".png")
            success, encoded = cv2.imencode(".png" if png else ".jpg", frame,
                [cv2.IMWRITE_PNG_COMPRESSION, 1] if png else [cv2.IMWRITE_JPEG_QUALITY, 85])
            if not success:
                raise OSError("扫描截图保存失败")
            encoded.tofile(str(self.folder / filename))
        return filename

    def ocr(self, roi, frame, expected=".+"):
        result = self.rt.recognize("MaaNikki_Daily_OCR", {
            "roi": roi, "expected": expected, "threshold": .65}, image=frame)
        if not result or not result.hit:
            return []
        return getattr(result, "filtered_results", []) or [result.best_result]

    def text(self, roi, frame):
        return " ".join(getattr(item, "text", "") for item in self.ocr(roi, frame))

    def ready(self, frame):
        generation = self.rt._focus_generation
        self.check_generation(generation)
        if frame is self._ready_frame and generation == self._ready_generation:
            return True
        result = self.rt.recognize("MaaNikki_Blessing_DecomposeReady", image=frame)
        self.check_generation(generation)
        matched = bool(result and result.hit)
        if matched:
            self._ready_frame, self._ready_generation = frame, generation
        return matched

    def open(self):
        if self.rt.ui.page_matches("MaaNikki_Blessing_DecomposeReady"):
            return self.rt.ui.wait_page("MaaNikki_Blessing_DecomposeReady")
        if not self.rt.ui.find_menu_entry(node="MaaNikki_Blessing_Entrance"):
            return False
        return self.rt.click_template("MaaNikki_Blessing_DecomposeEntrance")

    def crop(self, frame):
        x, y, w, h = self.grid
        return frame[y:y + h, x:x + w]

    def cards(self, frame, levels):
        x, y, width, height = self.grid
        gray = cv2.cvtColor(self.crop(frame), cv2.COLOR_BGR2GRAY)
        edges = cv2.morphologyEx(cv2.Canny(gray, 35, 90), cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        contours_found = []
        card_width, card_height = self.layout["card_size"]
        first_x, pitch = self.layout["first_x"], self.layout["column_pitch"]
        for contour in contours:
            cx, cy, w, h = cv2.boundingRect(contour)
            if (abs(w - card_width) <= 7 and abs(h - card_height) <= 7
                    and cv2.contourArea(contour) / (w * h) > .8):
                box = [cx + x, cy + y, w, h]
                column = round((box[0] - first_x) / pitch)
                if (0 <= column < self.layout["columns"]
                        and abs(box[0] - (first_x + column * pitch)) < 8
                        and not any(abs(box[0] - old[0]) < 10 and abs(box[1] - old[1]) < 10
                                    for old in contours_found)):
                    contours_found.append(box)
        # Inner and outer borders yield different sizes. Use the shared level
        # baseline for each row and fixed cell dimensions for icon crops.
        rows = []
        for item in levels:
            bx, by, bw, bh = list(item.box)
            column = round((bx - first_x) / pitch)
            if not 0 <= column < self.layout["columns"]:
                continue
            bottom = by + bh
            row = next((row for row in rows if abs(float(np.median(row["bottoms"])) - bottom) < 12), None)
            if row is None:
                row = {"bottoms": [], "columns": set()}
                rows.append(row)
            row["bottoms"].append(bottom)
            row["columns"].add(column)
        boxes = []
        for row in rows:
            top = round(float(np.median(row["bottoms"]))) + self.layout["level_bottom_padding"] - card_height
            columns = set(row["columns"])
            columns.update(round((box[0] - first_x) / pitch) for box in contours_found if abs(box[1] - top) < 10)
            boxes.extend([round(first_x + column * pitch), top, card_width, card_height] for column in columns)
        # Preserve a cell with unreadable level as unresolved rather than
        # dropping it, while still normalising the source artwork geometry.
        for box in contours_found:
            if not any(abs(box[0] - old[0]) < 12 and abs(box[1] - old[1]) < 12 for old in boxes):
                column = round((box[0] - first_x) / pitch)
                boxes.append([round(first_x + column * pitch), box[1], card_width, card_height])
        boxes = [box for box in boxes if x <= box[0] and box[0] + box[2] <= x + width
                 and y <= box[1] and box[1] + box[3] <= y + height]
        return sorted(boxes, key=lambda box: (round(box[1] / 12), box[0]))

    @staticmethod
    def level(box, levels):
        x, y, w, h = box
        found = []
        for item in levels:
            if float(getattr(item, "score", 0)) < .90:
                continue
            bx, by, bw, bh = list(item.box)
            if x - 3 <= bx + bw / 2 <= x + w and y + h - 32 <= by + bh / 2 <= y + h:
                match = re.fullmatch(LEVEL, clean_text(item.text), re.IGNORECASE)
                if match:
                    found.append(int(match.group(1)))
        return found[0] if len(found) == 1 else None

    def scroll_position(self, before, after):
        """Confirm translation from independent columns of local card artwork."""
        gx, gy, gw, gh = self.grid
        width, height = self.layout["card_size"]
        boxes = self.cards(before, [])
        boxes = [box for box in boxes if gy + gh / 2 <= box[1] <= gy + gh - height - 8]
        anchor_source = "lower_contours"
        if (len(boxes) < 6 or len({round((box[0] - self.layout["first_x"]) / self.layout["column_pitch"])
                                  for box in boxes}) < 3):
            # Selected/glowing cards can break a contour, leaving fewer than
            # three columns in the lower half (2026-10-15 normal inventory).
            # One row with three columns is also too fragile: a selected card
            # can fail to match, leaving only two votes (20:58 locking run).
            # Recover row geometry from level text in the same BEFORE frame
            # and use all complete visible cards. Artwork must still vote for
            # the same translation across three independent columns below.
            levels = self.ocr(self.grid, before, LEVEL)
            boxes = self.cards(before, levels)
            boxes = [box for box in boxes if gy <= box[1] <= gy + gh - height - 8]
            anchor_source = "levels_and_contours"

        def features(frame):
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)
            return gray - cv2.GaussianBlur(gray, (0, 0), 3)

        first, second = features(before), features(after)
        votes = []
        maximum = gh - height - self.layout.get("row_pitch", 123)
        for bx, by, _, _ in boxes:
            left, right, top, bottom = bx + 12, bx + width - 12, by + 22, by + height - 30
            template = first[top:bottom, left:right]
            if float(np.std(template)) < 2:
                continue
            low, high = max(gy, top - maximum), min(top + 3, gy + gh - template.shape[0])
            search = second[low:high + template.shape[0], left:right]
            scores = cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED).ravel()
            scores = np.nan_to_num(scores, nan=-1, posinf=-1, neginf=-1)
            order = np.argsort(scores)[::-1]
            best = int(order[0])
            competing = [int(i) for i in order if abs(int(i) - best) > 8]
            gap = float(scores[best] - scores[competing[0]]) if competing else 1.0
            if scores[best] < .80 or gap < .04:
                continue
            delta = top - (low + best)
            column = round((bx - self.layout["first_x"]) / self.layout["column_pitch"])
            votes.append({"column": column, "delta": delta, "score": round(float(scores[best]), 4)})
        clusters = [[vote for vote in votes if abs(vote["delta"] - candidate["delta"]) <= 2]
                    for candidate in votes]
        cluster = max(clusters, key=lambda values: (len({v["column"] for v in values}), len(values)), default=[])
        columns = len({vote["column"] for vote in cluster})
        self._scroll_diagnostics = {"anchors": len(boxes), "anchor_source": anchor_source,
                                    "votes": votes, "consistent_columns": columns}
        # Repeated artwork in one column cannot establish a physical-copy index.
        if columns < 3 or len(cluster) <= len(votes) / 2:
            return None
        delta = round(float(np.median([vote["delta"] for vote in cluster])))
        self._scroll_diagnostics["delta"] = delta
        return max(0, delta) if -2 <= delta <= maximum else None

    def unchanged(self, before, after):
        return self.rt.ui.similarity(self.crop(before), self.crop(after)) > .998

    def scroll_list(self, amount):
        # A hover tooltip can change both frames and cover neighbouring cards.
        # Move to the title without clicking before waiting for the new frame.
        return (self.rt.ui.scroll(self.grid, amount)
                and self.rt.action("TouchMove", target=[420, 32]) and self.rt.pause(.25))

    def scroll_amounts(self):
        return tuple(dict.fromkeys((self.layout["scroll_step"], 120)))

    def record_scroll(self, before, after, amount, delta, phase):
        events = self.snapshot["scan"]["scrolls"]
        event = {**self._scroll_diagnostics, "phase": phase,
                 "wheel_amount": amount, "delta": delta}
        if delta is None:
            prefix = f"scroll_{len(events):03d}_{phase}"
            event["before"] = self.image(prefix + "_before.jpg", before)
            event["after"] = self.image(prefix + "_after.jpg", after)
        events.append(event)

    def check_generation(self, generation):
        self.rt.check_focus()
        if self.rt._focus_generation != generation:
            raise RescanSlot()

    @staticmethod
    def region(frame, roi):
        x, y, w, h = roi
        return frame[y:y + h, x:x + w]

    @classmethod
    def same_pixels(cls, first, second, roi):
        if first is None or second is None or first.shape != second.shape:
            return False
        a, b = cls.region(first, roi), cls.region(second, roi)
        if not a.size or a.shape != b.shape:
            return False
        delta = cv2.absdiff(a, b)
        return float(delta.mean()) <= .6 and float(np.mean(delta > 6)) <= .002

    @classmethod
    def same_structure(cls, first, second, roi, threshold=.985):
        if first is None or second is None or first.shape != second.shape:
            return False
        a, b = cls.region(first, roi), cls.region(second, roi)
        if not a.size or a.shape != b.shape:
            return False

        def edges(image):
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float32)
            return gray - cv2.GaussianBlur(gray, (0, 0), 3)

        a, b = edges(a), edges(b)
        # Background lighting drifts; card/text edges establish that the
        # underlying inventory is visible again, without accepting a panel.
        if min(float(a.std()), float(b.std())) < 1:
            return cls.same_pixels(first, second, roi)
        score = float(cv2.matchTemplate(a, b, cv2.TM_CCOEFF_NORMED)[0, 0])
        return bool(np.isfinite(score) and score >= threshold)

    def detail_frame(self, generation):
        frame = self.rt.capture()
        self.check_generation(generation)
        # The page title is fixed while hovering. OCR is needed only when
        # its pixels differ from the already-confirmed page observation.
        if frame is None or not (self.same_structure(frame, self._page_frame, [75, 15, 300, 40])
                                 or self.ready(frame)):
            raise RuntimeError("悬停核对时分解页面特征丢失")
        return frame

    @staticmethod
    def detail_roi(facts):
        left, top = facts["header_box"]
        x, y = max(0, left - 6), max(0, top - 5)
        return [x, y, min(332, 1280 - x), min(126, 720 - y)]

    def same_fields(self, first, second, facts):
        # Compare text alone: decorative stars and panel effects may animate
        # even when all inventory facts are already fully displayed.
        return all(self.same_pixels(first, second, box) for box in facts["field_boxes"])

    def tooltip_visible(self, frame, roi):
        # Inventory/background animation alone must not start OCR. A tooltip
        # changes the structure and a substantial part of its expected band.
        if self.same_structure(frame, self._page_frame, roi, threshold=.97):
            return False
        delta = cv2.absdiff(self.region(frame, roi), self.region(self._page_frame, roi))
        return float(np.mean(np.max(delta, axis=2) > 20)) >= .12

    def tooltip_stable(self, first, second, roi):
        a, b = self.region(first, roi), self.region(second, roi)
        baseline = self.region(self._page_frame, roi)
        changed = np.max(cv2.absdiff(b, baseline), axis=2) > 20
        if not np.any(changed):
            return False
        gray = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)
        background = cv2.GaussianBlur(gray, (11, 11), 0)
        # Tooltip text is dark on a light panel. Decorative stars and the
        # underlying card art are not a reason to delay its text recognition.
        # This is only the pre-read gate: the exact name/colour/level boxes
        # must still agree across independent captures after OCR locates them.
        text = changed & (gray < 170) & (background > 185)
        if np.count_nonzero(text) >= 80:
            delta = cv2.absdiff(a, b)[text]
            return float(delta.mean()) <= 1 and float(np.mean(delta > 6)) <= .01
        # Preserve the conservative recovery for unusual panel contrast.
        if not self.same_structure(first, second, roi, threshold=.997):
            return False
        delta = cv2.absdiff(a, b)[changed]
        return float(delta.mean()) <= 1 and float(np.mean(delta > 6)) <= .01

    def wait_tooltip(self, generation, roi):
        """Observe appearance and settling without invoking text recognition."""
        previous, frame = None, None
        for capture in range(10):
            # Avoid repeatedly capturing the grid during the tooltip's
            # initial display delay; start close to its observed appearance.
            if not self.rt.pause(self.tooltip_initial_wait if capture == 0 else .02):
                return None, frame, capture, False
            frame = self.detail_frame(generation)
            if not self.tooltip_visible(frame, roi):
                previous = None
                continue
            if previous is not None and self.tooltip_stable(previous, frame, roi):
                return previous, frame, capture + 1, True
            previous = frame
        # Unusual panel placement or persistent decoration may defeat the
        # image gate. Preserve the bounded OCR recovery rather than discarding
        # this copy or waiting forever.
        return None, frame, 10, False

    def save_details(self, index, facts, frame, evidence, ocr_reads, captures):
        self._tooltip_roi = self.detail_roi(facts)
        evidence.update(reads=ocr_reads, captures=captures,
                        stability="unchanged_field_pixels", image_roi=self._tooltip_roi)
        suffix = "" if facts["color"] is not None else "_pending"
        evidence["image"] = self.image(f"tooltip_{index:04d}{suffix}.png",
                                       self.region(frame, self._tooltip_roi))
        return facts, evidence

    def read_details(self, box, generation, index):
        """Hover only: never select inventory items or decomposition materials."""
        roi = self.layout.get("tooltip_view", [0, 70, 1280, 570])
        self.check_generation(generation)
        if not self.rt.action("TouchMove", target=[420, 32]):
            return None, None
        if self._tooltip_roi is not None:
            # Compare the old tooltip fields to the unhovered page, avoiding
            # another full OCR pass. Never accept an uncleared previous card.
            for _ in range(6):
                if not self.rt.pause(.02):
                    return None, None
                frame = self.detail_frame(generation)
                if self.same_structure(frame, self._page_frame, self._tooltip_roi):
                    break
            else:
                # Animation can defeat a baseline comparison. One OCR pass
                # resolves that ambiguity rather than rejecting every later
                # card against the same stale baseline.
                headers = self.ocr(roi, frame, HEADER)
                self.check_generation(generation)
                if headers:
                    self.image(f"tooltip_{index:04d}_stale.png", frame)
                    raise RuntimeError("旧悬停提示未关闭，已保存截图及已确认库存")
            self._tooltip_roi = None
        x, y, w, h = box
        if not self.rt.action("TouchMove", target=[round(x + w / 2), round(y + h * .45)]):
            return None, None
        previous, previous_frame, evidence = None, None, None
        x_start = min(1279, max(0, x + w + 8))
        # Lower-row panels move upward to fit their description. Include
        # the observed header band rather than OCR-ing the description too.
        y_start = min(max(roi[1], y - 24), 270)
        local_roi = [x_start, y_start, min(330, 1280 - x_start), min(170, 640 - y_start)]
        column_roi = [x_start, roi[1], min(330, 1280 - x_start), roi[3]]
        read_roi = local_roi
        settled_before, first_frame, pre_captures, settled = self.wait_tooltip(generation, local_roi)
        if self.rt.stopped or first_frame is None:
            return None, None
        ocr_reads, fields_retried = 0, False
        for attempt in range(6):
            if attempt == 0:
                frame = first_frame
            else:
                if not self.rt.pause(.02):
                    return None, None
                frame = self.detail_frame(generation)
            if previous is not None and self.same_fields(frame, previous_frame, previous):
                # An independent fresh capture has identical field pixels:
                # repeating deterministic OCR on them adds no evidence.
                if previous["color"] is not None or fields_retried:
                    return self.save_details(index, previous, frame, evidence, ocr_reads,
                                             pre_captures + attempt)
                read_roi = self.detail_roi(previous)
            results = self.ocr(read_roi, frame)
            ocr_reads += 1
            fields_retried = fields_retried or (previous is not None and read_roi == self.detail_roi(previous))
            self.check_generation(generation)
            facts, lines = tooltip_facts(results, self.catalog,
                threshold=self.layout.get("tooltip_ocr_threshold", .85),
                color_threshold=COLOR_OCR_THRESHOLD)
            if facts is None:
                # Retry the same local band first: absent text often means
                # a delayed tooltip, not an incorrect layout. Only unusual
                # placements need column discovery, then a full-view read.
                read_roi = local_roi if attempt < 2 else column_roi if attempt < 4 else roi
            else:
                read_roi = self.detail_roi(facts)
            evidence = {"source": "tooltip", "reads": ocr_reads,
                        "captures": pre_captures + attempt, "pre_read_captures": pre_captures,
                        "pre_read_stable": settled, "ocr": lines}
            if (attempt == 0 and settled and facts is not None and facts["color"] is not None
                    and self.same_fields(settled_before, frame, facts)):
                # OCR runs on the second of two stable captures. Its exact
                # field boxes also agree in the first capture, so another
                # screenshot or OCR pass would add no independent evidence.
                return self.save_details(index, facts, frame, evidence, ocr_reads, pre_captures)
            previous, previous_frame = facts, frame
        evidence = evidence or {"source": "tooltip", "reads": 0, "ocr": []}
        # Keep a conservative clear region even when the tooltip OCR fails.
        self._tooltip_roi = self.detail_roi(previous) if previous is not None else roi
        evidence["image"] = self.image(f"tooltip_{index:04d}_pending.png", frame)
        evidence["reason"] = "未取得稳定的部件及等级文字"
        return None, evidence

    def confirm_copy(self, frame, box, match, level, generation, index):
        cached = self.verified.find(frame, box, match, level)
        if cached is not None:
            facts, reference = cached
            self.snapshot["scan"]["cached_verified"] += 1
            return facts, {"source": "verified_duplicate", "reference": reference.get("image")}
        started = time.perf_counter()
        try:
            facts, evidence = self.read_details(box, generation, index)
        finally:
            scan = self.snapshot["scan"]
            scan["tooltip_seconds"] = round(scan["tooltip_seconds"] + time.perf_counter() - started, 3)
        if evidence is not None:
            evidence["seconds"] = round(time.perf_counter() - started, 3)
            scan["tooltip_ocr_reads"] += evidence.get("reads", 0)
        if facts is not None:
            self.snapshot["scan"]["tooltip_verified"] += 1
            self.verified.remember(frame, box, facts, evidence)
        return facts, evidence

    def top(self):
        previous = self.rt.capture()
        if previous is None:
            return None
        generation = self.rt._focus_generation
        for _ in range(60):
            if not self.scroll_list(1800):
                return None
            current = self.rt.capture()
            self.check_generation(generation)
            if current is None:
                return None
            if self.unchanged(previous, current):
                if not self.scroll_list(1800):
                    return None
                last = self.rt.capture()
                self.check_generation(generation)
                if last is not None and self.unchanged(current, last):
                    return last
            previous = current
        return None

    def advance(self, before, generation):
        amounts = self.scroll_amounts()
        for attempt, amount in enumerate(amounts):
            self._scroll_diagnostics = {}
            if not self.scroll_list(-amount):
                return None, None
            after = self.rt.capture()
            self.check_generation(generation)
            if after is None or not self.ready(after):
                return None, None
            if self.unchanged(before, after):
                self.record_scroll(before, after, -amount, 0, "down")
                return after, 0
            delta = self.scroll_position(before, after)
            self.record_scroll(before, after, -amount, delta, "down")
            if delta is not None:
                return after, delta
            # Undo an ambiguous large scroll before retrying a smaller step.
            # Never advance across an unseen gap or silently deduplicate copies.
            if attempt == len(amounts) - 1:
                return None, None
            self.rt.log("祝福闪光：未确认翻页位移，已保存前后截图，回滚后缩小滚动幅度重试。")
            if not self.scroll_list(amount):
                return None, None
            restored = self.rt.capture()
            self.check_generation(generation)
            if restored is None or not self.ready(restored):
                return None, None
            self._scroll_diagnostics = {}
            restored_delta = 0 if self.unchanged(before, restored) else self.scroll_position(before, restored)
            self.record_scroll(before, restored, amount, restored_delta, "restore")
            if restored_delta != 0:
                return None, None
        return None, None

    def traverse(self):
        self.snapshot["items"].clear()
        self.snapshot["unresolved"].clear()
        self.snapshot["scan"]["pages"].clear()
        self.snapshot["scan"]["scrolls"].clear()
        self.snapshot["scan"]["slots"].clear()
        self.snapshot["scan"].update(tooltip_verified=0, cached_verified=0,
                                     tooltip_seconds=0.0, tooltip_ocr_reads=0)
        self.verified.clear()
        self._page_frame, self._tooltip_roi = None, None
        frame = self.top()
        if frame is None:
            return False, "未确认列表顶部"
        generation = self.rt._focus_generation
        position, seen = 0, []
        for page in range(150):
            self.check_generation(generation)
            if self.rt.stopped:
                return False, "用户停止"
            started = time.perf_counter()
            if not self.ready(frame):
                return False, "分解页特征丢失"
            self._page_frame, self._tooltip_roi = frame, None
            # One level OCR and all icon candidates share the same capture.
            # New variants then receive read-only tooltip verification.
            levels = self.ocr(self.grid, frame, LEVEL)
            boxes = self.cards(frame, levels)
            capacity = self.snapshot["scan"]["reported_capacity"]
            if not boxes:
                if not seen and self.rt.text(self.grid, "暂无|没有|未获得|空空如也", image=frame):
                    return True, "已确认分解列表为空"
                return (capacity == 0, "库存为空" if capacity == 0 else "未识别到完整库存格子")
            new_boxes = [box for box in boxes if not any(
                abs(box[0] - px) < 12 and abs(box[1] + position - py) < 12 for px, py in seen)]
            card_levels = [self.level(box, levels) for box in new_boxes]
            matches = self.matcher.match_batch(frame, new_boxes, levels=card_levels)
            self.check_generation(generation)
            page_file = self.image(f"batch_{page:03d}.png", frame)
            added, accepted, color_accepted = 0, 0, 0
            for box, match, level in zip(new_boxes, matches, card_levels):
                absolute = (box[0], box[1] + position)
                seen.append(absolute)
                added += 1
                facts, verification = self.confirm_copy(frame, box, match, level, generation, len(seen))
                self.check_generation(generation)
                valid = facts is not None
                color_known = bool(valid and facts["color"] is not None)
                if valid:
                    verification["image_disagrees"] = any(
                        facts[key] != match["candidate"][key] for key in ("name", "slot", "color"))
                    verification["grid_level_disagrees"] = level is not None and level != facts["level"]
                evidence = {"box": box, "position": list(absolute), "page": page_file,
                            "level": facts["level"] if valid else level, "grid_level": level,
                            "count": 1, "icon_match": match, "verification": verification}
                if valid:
                    item = {**evidence, **{key: facts[key] for key in ("name", "slot", "rarity", "color")},
                            "identity_known": True, "color_known": color_known, "xp": 0, "xp_known": False}
                    if not color_known:
                        filename = f"color_unknown_{len(seen):04d}.png"
                        x, y, w, h = box
                        self.image(filename, frame[y:y + h, x:x + w])
                        item.update(detail=filename, reason="部件及等级已确认，颜色待确认")
                    self.snapshot["items"].append(item)
                    accepted += 1
                    color_accepted += int(color_known)
                else:
                    filename = f"unknown_{len(seen):04d}.png"
                    x, y, w, h = box
                    self.image(filename, frame[y:y + h, x:x + w])
                    self.snapshot["unresolved"].append({**evidence, "detail": filename,
                                                        "reason": "悬停信息未能稳定核对，候选未计入确认库存"})
            elapsed = time.perf_counter() - started
            self.snapshot["scan"]["pages"].append({"file": page_file, "visible": len(boxes),
                                                   "new": added, "accepted": accepted,
                                                   "color_accepted": color_accepted,
                                                   "seconds": round(elapsed, 3)})
            self.check_generation(generation)
            after, delta = self.advance(frame, generation)
            if after is None or delta is None:
                return False, "滚动位置未确认，保留部分扫描，避免重复或漏计"
            if delta == 0:
                after, delta = self.advance(after, generation)
                if after is None or delta is None:
                    return False, "列表底部未能确认"
                if delta == 0:
                    return True, "已确认列表底部"
            position += delta
            frame = after
        return False, "达到扫描页数上限"

    def run(self):
        if not self.open():
            self.snapshot["scan"]["reason"] = "未能打开祝福闪光分解库存"
            self.image("entrance.jpg", self.rt.capture())
            return self.snapshot
        frame = self.rt.capture()
        if frame is None or frame.shape[:2] != (720, 1280):
            self.snapshot["scan"]["reason"] = "库存扫描需要 16:9 窗口（截图 1280×720）"
            return self.snapshot
        traversed, reason = False, "未开始扫描"
        while not self.rt.stopped:
            try:
                generation = self.rt._focus_generation
                frame = self.rt.capture()
                if frame is None or not self.ready(frame):
                    reason = "未确认分解库存页面"
                    break
                capacity = clean_text(self.text(self.layout["capacity"], frame))
                self.check_generation(generation)
                count = re.search(r"(\d+)[/／](\d+)", capacity)
                self.snapshot["scan"]["reported_capacity"] = int(count.group(1)) if count else None
                traversed, reason = self.traverse()
                break
            except RescanSlot:
                self.rt.log("已恢复前台，从顶部重新核对整份祝福闪光列表。")
                if not self.open():
                    reason = "恢复后未确认分解库存入口"
                    break
        reconcile_exclusions(self.snapshot, self.catalog, traversed=traversed)
        summarize_scan(self.snapshot, traversed, reason)
        scan = self.snapshot["scan"]
        if not scan["coverage_complete"]:
            scan["warnings"].append(f"库存容量 {scan['reported_capacity']}，实际遍历 {scan['visited_copies']} 个；"
                                    "请确认列表无筛选，锁定项是否可见，数量未一致时按部分库存处理。")
        scan["warnings"].append("批量图标扫描不读取等级内已积累经验，升级预算按该等级的起点估算。")
        return self.snapshot
