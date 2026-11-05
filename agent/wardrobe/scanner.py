"""Read Compendium Search clothes by game-displayed names without selecting or changing outfits."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import re
from types import SimpleNamespace

import cv2
import numpy as np

from account_state import AccountState
from collection.scanner import InventoryScanner, RescanSlot
from daily.settings import ROOT
from file_io import write_json
from tall_window import tall_window
from task_control import TaskStopped, task_time as time
from .model import WardrobeCatalog
from .progress import MODEL_ONLY_PARTS, ordered_prefix


HEADER = [255, 78, 360, 42]
SEARCH_BOX = [850, 20, 380, 48]
SEARCH_CLEAR = [1202, 44]
FILTER_BUTTON = [940, 679]
SORT_BOX = [966, 661, 288, 36]
FILTER_RESET_ROI = [450, 475, 185, 65]
FILTER_CONFIRM_ROI = [645, 475, 185, 65]
FILTER_RESET = [544, 511]
FILTER_CONFIRM = [734, 511]
FILTER_ACQUIRE_ROI = [300, 200, 680, 280]
CLOTHES_TAB = [80, 148]
TAB_X = 203

MAJOR_SEQUENCE = [
    ("210", 1),
    ("290", 2),
    ("220", 3),
    ("230", 4),
    ("242", 5),
    ("250", 6),
    ("260", 7),
]

ACCESSORY_PARTS = [
    "271", "272", "273", "274", "275", "276", "277",
    "292", "293", "294", "295", "296", "270", "278", "279",
]

MAKEUP_PARTS = ["281", "282", "283", "284", "285", "286"]
MAKEUP_ALL = ["280", *MAKEUP_PARTS]


def clean(text):
    return re.sub(r"[^\w\u4e00-\u9fff]", "", str(text or "")).replace("・", "·")


class WardrobeScanner(InventoryScanner):
    def __init__(self, runtime, folder):
        self.rt, self.folder = runtime, folder
        self.model = WardrobeCatalog(ROOT / "resource/model/wardrobe")
        self.account = AccountState.for_runtime(ROOT, runtime)
        self.layout = {
            "grid": [265, 130, 950, 505],
            "card_size": [101, 100],
            "first_x": 270,
            "column_pitch": 324,
            "row_pitch": 117,
            "first_y": 139,
            "row_pitch_exact": 176 * 2 / 3,
            "columns": 3,
            "scroll_step": 1920,
            "adaptive_scroll_delta": True,
            "scroll_anchor_ocr": False,
        }
        self.grid = self.layout["grid"]
        self.matcher = SimpleNamespace(color_threshold=.80, color_margin=.025)
        self._scroll_diagnostics = {}
        self._partial_rows = []
        self._border_recovered = []
        self._cards_cache = (None, (), (), ())
        self._tall_mode = False
        self._page_frame, self.current_part = None, None
        self.ui_parts = set(self.model.parts) - MODEL_ONLY_PARTS
        self.snapshot = {
            "schema_version": 2,
            "model_version": self.model.version,
            "uid": self.account.uid,
            "label_fingerprint": self.model.fingerprint,
            "sort": "compendium_default",
            "identity_policy": "compendium_ocr",
            "identity_verified": True,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": None,
            "complete": False,
            "parts": {},
            "total_parts": len(self.ui_parts),
            "verified_parts": [],
            "skipped_categories": [],
            "scan": {"pages": [], "scrolls": []},
        }
        wardrobe = self.account.load().get("wardrobe", {})
        previous = wardrobe.get("last_scan") or wardrobe.get("inventory")
        self._previous = {}
        if (isinstance(previous, dict)
                and previous.get("model_version") == self.model.version
                and previous.get("label_fingerprint") == self.model.fingerprint
                and previous.get("identity_policy") == "compendium_ocr"
                and isinstance(previous.get("parts"), dict)):
            self._previous = deepcopy(previous["parts"])
            self.snapshot["parts"] = deepcopy(self._previous)
            self.snapshot["resumed_from"] = previous.get("updated_at")

    def category_info(self, frame):
        header = self.rt.text(HEADER, image=frame)
        match = re.search(r"(\d+)\s*[/／]\s*(\d+)", header)
        owned = int(match.group(1)) if match else None
        total = int(match.group(2)) if match else None
        prefix = re.split(r"[:：]|\d", header, maxsplit=1)[0]
        value = clean(prefix)
        if not value:
            return None, header, owned, total
        extra = {"套装": "299", "妆容·全妆": "280", "全妆": "280"}
        if prefix.strip() in extra:
            return extra[prefix.strip()], header, owned, total
        for key, part in extra.items():
            if value == clean(key):
                return part, header, owned, total
        exact, partial = [], []
        for part, info in self.model.parts.items():
            full_c = clean(info["name"])
            short_c = clean(info["name"].split("·")[-1])
            if value in (full_c, short_c):
                exact.append(part)
            elif value.startswith(full_c) or value.endswith(short_c):
                partial.append(part)
        if len(exact) == 1:
            return exact[0], header, owned, total
        if len(partial) == 1:
            return partial[0], header, owned, total
        return None, header, owned, total

    def category(self, frame):
        part, header, _, _ = self.category_info(frame)
        return part, header

    def ready(self, frame):
        if frame is None:
            return False
        if self._page_frame is not None and all(
            self.same_structure(frame, self._page_frame, roi)
            for roi in ([66, 15, 110, 38], [25, 115, 110, 65], HEADER)
        ):
            return True
        if not self.rt.hit("MaaNikki_CompendiumSearchReady", image=frame):
            return False
        part, _ = self.category(frame)
        return self.current_part is None or part == self.current_part

    def wait_settled(self, roi=None, seconds=2.5):
        target_roi = roi or [150, 75, 1065, 560]
        self.rt.pause(.35)
        self.rt.ui.stable(.985, seconds=seconds, roi=target_roi)
        return self.rt.capture()

    def open(self):
        frame = self.rt.capture()
        if self.rt.hit("MaaNikki_CompendiumSearchReady", image=frame):
            return True
        if self.rt.hit("MaaNikki_Compendium_ClothesReady", image=frame):
            if self.rt.ui.enter_page(
                lambda: self.rt.action("Click", target=[1176, 37]),
                "MaaNikki_CompendiumSearchReady",
                source="MaaNikki_Compendium_ClothesReady",
                attempts=2,
            ):
                return True
        if self.rt.hit("MaaNikki_CompendiumReady", image=frame):
            if self.rt.click_template("MaaNikki_Compendium_SearchEntrance"):
                return True
        if not self.rt.ui.recover_page("MaaNikki_CompendiumReady"):
            return False
        return self.rt.click_template("MaaNikki_Compendium_SearchEntrance")

    def clear_search_box(self, frame=None):
        frame = frame if frame is not None else self.rt.capture()
        text = clean(self.rt.text(SEARCH_BOX, image=frame))
        if not text or "输入名称" in text or "搜索" in text:
            return frame
        self.rt.action("Click", target=SEARCH_CLEAR)
        return self.wait_settled(self.grid)

    def ensure_default_sort(self, frame=None):
        frame = frame if frame is not None else self.rt.capture()
        if self.rt.text(SORT_BOX, "默认", image=frame):
            return frame
        if (not self.rt.action("Click", target=[1110, 679])
                or not self.rt.pause(.25)
                or not self.rt.click_text("默认", [960, 480, 295, 215])):
            raise RuntimeError("未能切换博物图鉴默认排序")
        frame = self.wait_settled(self.grid)
        if not self.rt.text(SORT_BOX, "默认", image=frame):
            raise RuntimeError("未确认博物图鉴默认排序")
        return frame

    def wait_filter_closed(self, seconds=4.0):
        deadline = time.monotonic() + seconds
        while not self.rt.stopped and time.monotonic() < deadline:
            frame = self.rt.capture()
            if (not self.rt.hit("MaaNikki_CompendiumFilterReady", image=frame)
                    and self.rt.hit("MaaNikki_CompendiumSearchReady", image=frame)):
                return frame
            self.rt.pause(.15)
        return None

    def apply_owned_filter(self):
        if not self.rt.ui.enter_page(
            lambda: self.rt.action("Click", target=FILTER_BUTTON),
            "MaaNikki_CompendiumFilterReady",
            source="MaaNikki_CompendiumSearchReady",
            attempts=2,
        ):
            raise RuntimeError("未能打开博物图鉴筛选面板")
        self.rt.pause(.25)
        if not self.rt.click_text("^重置$", FILTER_RESET_ROI):
            self.rt.action("Click", target=FILTER_RESET)
        self.rt.pause(.35)
        frame = self.rt.capture()
        if not self.rt.hit("MaaNikki_CompendiumFilterReady", image=frame):
            if not self.rt.ui.enter_page(
                lambda: self.rt.action("Click", target=FILTER_BUTTON),
                "MaaNikki_CompendiumFilterReady",
                source="MaaNikki_CompendiumSearchReady",
                attempts=2,
            ):
                raise RuntimeError("重置筛选后未能重新打开筛选面板")
            self.rt.pause(.25)
        if not self.rt.click_text("^已获得$", FILTER_ACQUIRE_ROI):
            raise RuntimeError("筛选面板未找到“已获得”选项")
        self.rt.pause(.2)
        if not self.rt.click_text("^筛选$", FILTER_CONFIRM_ROI):
            self.rt.action("Click", target=FILTER_CONFIRM)
        if self.wait_filter_closed() is None:
            self.rt.action("Click", target=FILTER_CONFIRM)
            if self.wait_filter_closed() is None:
                raise RuntimeError("确认筛选后未关闭博物图鉴筛选面板")
        return self.wait_settled(self.grid)

    def prepare_search_view(self):
        frame = self.wait_settled()
        part, _, _, _ = self.category_info(frame)
        if part is None:
            self.rt.action("Click", target=CLOTHES_TAB)
            frame = self.wait_settled()
        frame = self.clear_search_box(frame)
        frame = self.ensure_default_sort(frame)
        frame = self.apply_owned_filter()
        self.rt.action("TouchMove", target=[640, 36])
        self.rt.pause(.15)
        return frame

    @staticmethod
    def scroll_thumb(frame):
        if frame is None:
            return None
        strip = frame[130:635, 1241:1249]
        gray = cv2.cvtColor(strip, cv2.COLOR_BGR2GRAY)
        profile = gray.mean(axis=1)
        bright = np.where(profile > 205)[0]
        if len(bright) < 6:
            return None
        return int(bright[0] + 130), int(bright[-1] + 130)

    def at_top(self, frame):
        boxes = self.cards(frame)
        if not boxes:
            return True
        if abs(min(box[1] for box in boxes) - self.layout["first_y"]) >= 4:
            return False
        thumb = self.scroll_thumb(frame)
        return thumb is None or thumb[0] <= 144

    def cards(self, frame, levels=None):
        if frame is not None and self._cards_cache[0] == id(frame):
            self._partial_rows = list(self._cards_cache[2])
            self._border_recovered = list(self._cards_cache[3])
            return [list(b) for b in self._cards_cache[1]]
        gray = cv2.cvtColor(self.crop(frame), cv2.COLOR_BGR2GRAY)
        edges = cv2.morphologyEx(cv2.Canny(gray, 30, 90), cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        gx, gy, _, gh = self.grid
        rows = []
        self._partial_rows = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            ax, ay = x + gx, y + gy
            column = None
            top = None
            if 285 <= w <= 298:
                col = round((ax - 269) / 324)
                if 0 <= col < 3 and abs(ax - (269 + col * 324)) <= 6:
                    column = col
                    if 16 <= h < 92 and ay > gy + gh - 115:
                        self._partial_rows.append(ay - 1)
                    elif 95 <= h <= 106:
                        top = (ay + h - 101) if h > 101 else (ay - 1)
            elif 96 <= w <= 107:
                col = round((ax - 270) / 324)
                if 0 <= col < 3 and abs(ax - (270 + col * 324)) <= 6:
                    column = col
                    if 16 <= h < 92 and ay > gy + gh - 115:
                        self._partial_rows.append(ay - 1)
                    elif 95 <= h <= 105:
                        top = (ay + h - 101) if h < 99 else (ay - 1)
            elif 80 <= w <= 86 and 78 <= h <= 86:
                col = round((ax - 278) / 324)
                if 0 <= col < 3 and abs(ax - (278 + col * 324)) <= 5:
                    column = col
                    top = ay - 9
            if column is None or top is None:
                continue
            if not (gy <= top and top + 100 <= gy + gh):
                continue
            row = next((row for row in rows if abs(row["top"] - top) < 10), None)
            if row is None:
                row = {"top": top, "tops": [top], "columns": set()}
                rows.append(row)
            else:
                row["tops"].append(top)
                row["top"] = int(round(np.median(row["tops"])))
            row["columns"].add(column)

        pitch = self.layout["row_pitch_exact"]
        max_steps = int(gh // pitch) + 3
        expected_tops = sorted({
            round(row["top"] + step * pitch)
            for row in rows
            for step in range(-max_steps, max_steps + 1)
            if gy <= round(row["top"] + step * pitch) <= gy + gh - 100
        })
        horizontal = cv2.morphologyEx(edges, cv2.MORPH_OPEN, np.ones((1, 140), np.uint8))
        lines, _ = cv2.findContours(horizontal, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        recovered = []
        for line in lines:
            x, y, w, h = cv2.boundingRect(line)
            if h > 5:
                continue
            ax, ay = x + gx, y + gy
            column = round((ax - 269) / 324)
            if not 0 <= column < 3:
                continue
            card_left = 269 + column * 324
            if ax < card_left - 8 or ax + w > card_left + 300:
                continue
            for candidate_top in (ay - 1, ay - 100):
                expected_top = min(
                    expected_tops,
                    key=lambda expected: abs(candidate_top - expected),
                    default=None,
                )
                if (expected_top is None or abs(candidate_top - expected_top) >= 5
                        or expected_top < gy or expected_top + 100 > gy + gh):
                    continue
                top = int(expected_top)
                row = next((row for row in rows if abs(row["top"] - top) < 10), None)
                if row is None:
                    row = {"top": top, "tops": [top], "columns": set()}
                    rows.append(row)
                if column not in row["columns"]:
                    recovered.append([column, row["top"]])
                row["columns"].add(column)

        if rows:
            first_y = self.layout["first_y"]
            base_offsets = [
                row["top"] - round((row["top"] - first_y) / pitch) * pitch
                for row in rows
            ]
            anchor_y = float(np.median(base_offsets))
            for step in range(-2, max_steps + 2):
                cand_top = int(round(anchor_y + step * pitch))
                if not (gy <= cand_top and cand_top + 100 <= gy + gh):
                    continue
                existing = next((r for r in rows if abs(r["top"] - cand_top) < 10), None)
                row_top = existing["top"] if existing is not None else cand_top
                for col in range(3):
                    if existing is not None and col in existing["columns"]:
                        continue
                    cx = 270 + col * 324
                    port = gray[row_top - gy + 9:row_top - gy + 91, cx - gx + 9:cx - gx + 91]
                    body = gray[row_top - gy + 55:row_top - gy + 92, cx - gx + 120:cx - gx + 260]
                    if (port.size > 0 and body.size > 0
                            and float(np.percentile(port, 15)) < 115
                            and float(body.mean() - port.mean()) > 25):
                        if existing is None:
                            existing = {"top": row_top, "tops": [row_top], "columns": set()}
                            rows.append(existing)
                        recovered.append([col, existing["top"]])
                        existing["columns"].add(col)

        self._border_recovered = recovered
        boxes = []
        valid_rows = [
            row for row in sorted(rows, key=lambda row: row["top"])
            if gy <= row["top"] and row["top"] + 100 <= gy + gh
        ]
        for index, row in enumerate(valid_rows):
            top = row["top"]
            has_lower_row = (
                index + 1 < len(valid_rows)
                or any(partial > top + 8 for partial in self._partial_rows)
            )
            last_column = self.layout["columns"] - 1 if has_lower_row else max(row["columns"])
            for column in range(last_column + 1):
                boxes.append([270 + 324 * column, top, 101, 100])
        if frame is not None:
            self._cards_cache = (
                id(frame),
                tuple(tuple(b) for b in boxes),
                tuple(self._partial_rows),
                tuple(self._border_recovered),
            )
        return boxes

    def scroll_list(self, amount, frame):
        boxes = self.cards(frame)
        left = [box for box in boxes if box[0] == self.layout["first_x"]]
        x, y, w, h = min(left, key=lambda box: box[1]) if left else [
            self.layout["first_x"], self.grid[1] + 20, *self.layout["card_size"]
        ]
        target = [round(x + w / 2), round(y + h / 2)]
        return (
            self.rt.action("TouchMove", target=target)
            and self.rt.pause(.15)
            and self.rt.action("Scroll", target=target, dx=0, dy=amount)
            and self.rt.pause(.12)
            and self.rt.action("TouchMove", target=[640, 36])
            and self.rt.pause(.22)
        )

    def unchanged(self, before, after):
        if self.same_structure(before, after, self.grid, threshold=.997):
            return True
        boxes = self.cards(before)
        before_partial = list(self._partial_rows)
        other = self.cards(after)
        if not boxes or len(boxes) != len(other):
            return False
        if any(abs(a[0] - b[0]) >= 4 or abs(a[1] - b[1]) >= 4 for a, b in zip(boxes, other)):
            return False
        if any(
            not any(abs(top - box[1]) < 4 for box in detected)
            for partial, detected in ((before_partial, boxes), (self._partial_rows, other))
            for top in partial
        ):
            return False
        return all(
            self.same_structure(before, after, [bx, by, 290, 100], threshold=.997)
            for bx, by, _, _ in boxes
        )

    def text_scroll_position(self, before, after, boxes):
        gx, gy, _, gh = self.grid
        _, height = self.layout["card_size"]
        maximum = gh - height - self.layout["row_pitch"]
        if boxes:
            maximum = max(maximum, int(max(b[1] for b in boxes) + 22 - gy))
        first, second = self.features(before), self.features(after)
        anchors = [b for b in boxes if gy + 60 <= b[1] <= gy + gh - height - 8]
        votes = []
        for bx, by, _, _ in anchors:
            col = round((bx - self.layout["first_x"]) / self.layout["column_pitch"])
            left, right, top, bottom = bx + 110, bx + 270, by + 4, by + 30
            template = first[top:bottom, left:right]
            if float(np.std(template)) < 2:
                continue
            low = max(gy, top - maximum)
            high = min(top + 3, gy + gh - template.shape[0])
            if high < low:
                continue
            search = second[low:high + template.shape[0], left:right]
            scores = np.nan_to_num(
                cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED).ravel(),
                nan=-1, posinf=-1, neginf=-1,
            )
            order = np.argsort(scores)[::-1]
            best = int(order[0])
            competing = [int(i) for i in order if abs(int(i) - best) > 8]
            gap = float(scores[best] - scores[competing[0]]) if competing else 1.0
            if scores[best] >= .80 and gap >= .08:
                votes.append({
                    "column": col,
                    "delta": top - (low + best),
                    "score": round(float(scores[best]), 4),
                    "margin": round(gap, 4),
                })
        clusters = [[v for v in votes if abs(v["delta"] - c["delta"]) <= 2] for c in votes]
        cluster = max(clusters, key=lambda vs: (len({v["column"] for v in vs}), len(vs)), default=[])
        if len(cluster) >= min(2, len(anchors)) and len(cluster) > len(votes) / 2:
            value = round(float(np.median([v["delta"] for v in cluster])))
            if -2 <= value <= maximum:
                self._scroll_diagnostics = {
                    "anchor_source": "card_title_band",
                    "delta": max(0, value),
                    "votes": votes,
                }
                return max(0, value)
        return None

    def scroll_position(self, before, after):
        delta = super().scroll_position(before, after)
        if delta is not None:
            return delta
        boxes = self.cards(before)
        if not boxes:
            return None
        return self.text_scroll_position(before, after, boxes)

    def top(self):
        before = self.rt.capture()
        generation = self.rt._focus_generation
        if self.at_top(before):
            return before
        for _ in range(25):
            if not self.scroll_list(12000, before):
                raise TaskStopped()
            after = self.rt.capture()
            self.check_generation(generation)
            if not self.ready(after):
                raise RuntimeError("博物图鉴检索页面发生变化")
            if self.at_top(after) or self.unchanged(before, after):
                return after
            before = after
        raise RuntimeError("未能确认博物图鉴列表顶部")

    def advance(self, before, generation):
        if self._tall_mode:
            steps = (14400, 9600, 4800, 1920)
            min_step = 1920
        else:
            last_top = max((b[1] for b in self.cards(before)), default=0)
            steps = (1920, 1440, 960, 480) if last_top >= 472 else (1440, 960, 480)
            min_step = 480
        for amount in steps:
            if not self.scroll_list(-amount, before):
                raise TaskStopped()
            after = self.rt.capture()
            self.check_generation(generation)
            if not self.ready(after):
                raise RuntimeError("博物图鉴翻页后页面发生变化")
            self._scroll_diagnostics = {}
            if self.unchanged(before, after):
                delta = 0
                self._scroll_diagnostics = {"anchor_source": "stationary", "delta": 0}
            else:
                delta = self.scroll_position(before, after)
            self.record_scroll(before, after, -amount, delta, "down")
            if delta is not None:
                return after, delta
            if amount == min_step:
                break
            if not self.scroll_list(amount, after):
                raise TaskStopped()
            restored = self.rt.capture()
            self.check_generation(generation)
            if not (self.unchanged(before, restored)
                    or (self.ready(restored) and self.scroll_position(before, restored) == 0)):
                raise RuntimeError("博物图鉴翻页位置未确认，已保存本次记录")
        raise RuntimeError("博物图鉴翻页位移未确认")

    def save(self, reason=None):
        self.snapshot["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.snapshot["reason"] = reason
        self.snapshot["scanned_count"] = sum(
            p["scanned_count"] for p in self.snapshot["parts"].values()
        )
        self.snapshot["ambiguous_count"] = sum(
            p["ambiguous_count"] for p in self.snapshot["parts"].values()
        )
        self.snapshot["unconfirmed_count"] = sum(
            p["unconfirmed_count"] for p in self.snapshot["parts"].values()
        )
        self.snapshot["completed_parts"] = sum(
            bool(p.get("complete")) for p in self.snapshot["parts"].values()
        )
        record = {k: v for k, v in self.snapshot.items() if k != "scan"}

        def change(data):
            wardrobe = data.setdefault("wardrobe", {})
            wardrobe["last_scan"] = record
            if record["complete"]:
                wardrobe["inventory"] = record

        write_json(self.folder / "scan.json", self.snapshot)
        self.account.update(change)

    def extract_card_titles(self, frame, boxes, check_unowned=False):
        if not boxes:
            return []
        gx, gy, gw, gh = self.grid
        ocr_items = []
        sorted_boxes = sorted(boxes, key=lambda b: (b[1], b[0]))
        all_min_y = max(gy, min(b[1] for b in sorted_boxes) - 6)
        all_max_y = min(gy + gh, max(b[1] for b in sorted_boxes) + 104)
        band_start = all_min_y
        while band_start < all_max_y:
            band_boxes = [b for b in sorted_boxes if band_start - 10 <= b[1] <= band_start + 500]
            if band_boxes:
                b_min = max(gy, min(b[1] for b in band_boxes) - 6)
                b_max = min(gy + gh, max(b[1] for b in band_boxes) + 104)
                items = self.ocr([gx, b_min, gw, max(108, b_max - b_min)], frame=frame)
                for item in items:
                    ibx, iby, ibw, ibh = list(item.box)
                    icx, icy = ibx + ibw / 2, iby + ibh / 2
                    if not any(
                        abs(icx - (obx + obw / 2)) < 12 and abs(icy - (oby + obh / 2)) < 10
                        for obx, oby, obw, obh in (list(old.box) for old in ocr_items)
                    ):
                        ocr_items.append(item)
                band_start = max(b[1] for b in band_boxes) + 80
            else:
                band_start += 500
        results = []
        for bx, by, _, _ in boxes:
            unowned = check_unowned and any(
                "未获得" in r.text
                and bx - 6 <= r.box[0] + r.box[2] / 2 <= bx + 106
                and by - 4 <= r.box[1] + r.box[3] / 2 <= by + 104
                for r in ocr_items
            )
            title_lines = [
                r for r in ocr_items
                if bx + 100 <= r.box[0] + r.box[2] / 2 <= bx + 248
                and by - 4 <= r.box[1] + r.box[3] / 2 <= by + 38
            ]
            title_lines.sort(key=lambda r: r.box[0])
            title = "".join(r.text.strip() for r in title_lines)
            if not clean(title):
                title = self.rt.text([bx + 104, by + 2, 144, 34], image=frame).strip()
            results.append((title, unowned))
        return results

    def apply_inferred_handhelds(self):
        part_state = self.snapshot["parts"].get("278")
        if not isinstance(part_state, dict) or not part_state.get("complete"):
            return
        inferred = self.model.infer_suit_handhelds(self.snapshot["parts"])
        if not inferred:
            return
        items = list(part_state.get("items") or [])
        base_slot = len(items)
        for offset, entry in enumerate(inferred):
            slot = base_slot + offset
            row, column = divmod(slot, self.layout["columns"])
            point = [
                self.layout["first_x"] + column * self.layout["column_pitch"],
                round(self.layout["first_y"] + row * self.layout["row_pitch_exact"], 3),
            ]
            items.append({**entry, "slot": slot, "position": point, "order": slot + 1})
        part_state["items"] = items
        part_state["scanned_count"] = len(items)
        part_state["ambiguous_count"] = sum(len(item["item_ids"]) > 1 for item in items)
        part_state["unconfirmed_count"] = sum(
            item["source"] == "compendium_ocr_unconfirmed" for item in items
        )

    def wait_page_cards(self, frame, owned_count, generation):
        max_rows = int((self.grid[3] - 100) // self.layout["row_pitch_exact"]) + 1
        expected_min = min(owned_count, (max_rows - 1) * 3) if owned_count is not None else 0
        prev_boxes = self.cards(frame)
        for _ in range(12):
            if not self.rt.pause(.14):
                raise TaskStopped()
            nxt = self.rt.capture()
            self.check_generation(generation)
            nxt_boxes = self.cards(nxt)
            if (len(nxt_boxes) >= expected_min
                    and len(nxt_boxes) == len(prev_boxes)
                    and (not nxt_boxes or abs(nxt_boxes[-1][1] - prev_boxes[-1][1]) <= 1)):
                return nxt
            frame, prev_boxes = nxt, nxt_boxes
        return frame

    def read_part(self, part):
        self.current_part = part
        generation = self.rt._focus_generation
        frame = self.rt.capture()
        actual_part, header, owned_count, total_count = self.category_info(frame)
        if actual_part != part:
            raise RuntimeError(f"当前图鉴部位未对准（期望 {self.model.parts[part]['name']}，实际 {header}）")

        if not self._tall_mode and owned_count is not None and total_count is not None and owned_count < total_count:
            probe_boxes = self.cards(frame)[:3]
            if probe_boxes:
                _, any_unowned = zip(*self.extract_card_titles(frame, probe_boxes, check_unowned=True))
                if any(any_unowned):
                    frame = self.apply_owned_filter()
                    actual_part, header, owned_count, total_count = self.category_info(frame)

        old_state = self._previous.get(part, {})
        old_items = ordered_prefix(old_state, self.layout)
        old_scanned = [
            item for item in old_items if item.get("source") != "compendium_suit_inferred"
        ]
        if (owned_count is not None
                and old_state.get("complete")
                and old_state.get("unconfirmed_count", 0) == 0
                and len(old_scanned) == owned_count):
            reused_state = deepcopy(old_state)
            reused_state["items"] = deepcopy(old_scanned)
            reused_state["scanned_count"] = len(old_scanned)
            reused_state["reused_count"] = len(old_scanned)
            self.snapshot["parts"][part] = reused_state
            if part == "278":
                self.apply_inferred_handhelds()
            if part not in self.snapshot["verified_parts"]:
                self.snapshot["verified_parts"].append(part)
            self.save()
            self.rt.log(
                f"衣柜读取：{self.model.parts[part]['name']} {self.snapshot['parts'][part]['scanned_count']} 件（数量未变，直接沿用）。"
            )
            return

        if owned_count == 0:
            state = {
                "name": self.model.parts[part]["name"],
                "items": [],
                "scanned_count": 0,
                "reused_count": 0,
                "ambiguous_count": 0,
                "unconfirmed_count": 0,
                "complete": True,
            }
            self.snapshot["parts"][part] = state
            if part == "278":
                self.apply_inferred_handhelds()
            if part not in self.snapshot["verified_parts"]:
                self.snapshot["verified_parts"].append(part)
            self.save()
            self.rt.log(f"衣柜读取：{state['name']} 0 件。")
            return

        frame = self.top()
        frame = self.wait_page_cards(frame, owned_count, generation)
        self._page_frame = frame
        state = {
            "name": self.model.parts[part]["name"],
            "items": [],
            "scanned_count": 0,
            "reused_count": 0,
            "ambiguous_count": 0,
            "unconfirmed_count": 0,
            "complete": False,
        }
        self.snapshot["parts"][part] = state
        known, position = {}, 0

        def materialize():
            items = []
            for slot, value in sorted(known.items()):
                row, column = divmod(slot, self.layout["columns"])
                point = [
                    self.layout["first_x"] + column * self.layout["column_pitch"],
                    round(self.layout["first_y"] + row * self.layout["row_pitch_exact"], 3),
                ]
                items.append({**value, "slot": slot, "position": point, "order": slot + 1})
            state.update(
                items=items,
                scanned_count=len(items),
                ambiguous_count=sum(len(item["item_ids"]) > 1 for item in items),
                unconfirmed_count=sum(
                    item["source"] == "compendium_ocr_unconfirmed" for item in items
                ),
            )

        try:
            for page in range(4096):
                self.check_generation(generation)
                boxes = self.cards(frame)
                if not boxes:
                    if not known and owned_count in (0, None):
                        state["complete"] = True
                        if part not in self.snapshot["verified_parts"]:
                            self.snapshot["verified_parts"].append(part)
                        self.save()
                        self.rt.log(f"衣柜读取：{state['name']} 0 件。")
                        return
                    raise RuntimeError(f"{state['name']}未发现完整卡片，请确认图鉴筛选正常")
                slots = [
                    round((box[1] + position - self.layout["first_y"]) / self.layout["row_pitch_exact"])
                    * self.layout["columns"]
                    + round((box[0] - self.layout["first_x"]) / self.layout["column_pitch"])
                    for box in boxes
                ]
                if any(slot < 0 for slot in slots) or len(set(slots)) != len(slots):
                    raise RuntimeError(f"{state['name']}卡片行列位置未确认，已保存读取进度")
                pairs = [
                    (slot, box)
                    for slot, box in zip(slots, boxes)
                    if owned_count is None or slot < owned_count
                ]
                fresh = [(slot, box) for slot, box in pairs if slot not in known]
                new_boxes = [box for _, box in fresh]
                if new_boxes:
                    extracted = self.extract_card_titles(
                        frame, new_boxes, check_unowned=(page == 0 and owned_count is not None and total_count is not None and owned_count < total_count),
                    )
                    if any(unowned for _, unowned in extracted):
                        frame = self.apply_owned_filter()
                        raise RescanSlot()
                    self.check_generation(generation)
                    for (slot, box), (raw_name, _) in zip(fresh, extracted):
                        matched = self.model.lookup(part, raw_name)
                        if matched is None:
                            bx, by, _, _ = box
                            retry_name = self.rt.text([bx + 104, by + 2, 144, 34], image=frame).strip()
                            if retry_name and retry_name != raw_name:
                                matched = self.model.lookup(part, retry_name)
                                if matched is not None:
                                    raw_name = retry_name
                        if matched is None:
                            matched = {
                                "class_id": -1,
                                "item_ids": [],
                                "names": [raw_name or f"未识别_{slot + 1}"],
                                "qualities": [],
                                "source": "compendium_ocr_unconfirmed",
                                "ocr_text": raw_name,
                            }
                        known[slot] = matched
                materialize()
                self.snapshot["scan"]["pages"].append({
                    "part": part,
                    "page": page,
                    "new": len(new_boxes),
                    "boxes": new_boxes,
                    "offset": position,
                    "border_recovered": self._border_recovered,
                    "image": None,
                })
                if owned_count is not None and len(known) >= owned_count:
                    break
                after, delta = self.advance(frame, generation)
                if delta == 0:
                    if len(self.cards(after)) > len(boxes):
                        frame = after
                        continue
                    after, delta = self.advance(after, generation)
                    if delta == 0:
                        if len(self.cards(after)) > len(boxes):
                            frame = after
                            continue
                        self.cards(after)
                        if any(
                            round((top + position - self.layout["first_y"]) / self.layout["row_pitch_exact"])
                            * self.layout["columns"] not in known
                            and (
                                owned_count is None
                                or round((top + position - self.layout["first_y"]) / self.layout["row_pitch_exact"])
                                * self.layout["columns"] < owned_count
                            )
                            for top in self._partial_rows
                        ):
                            raise RuntimeError(f"{state['name']}底部卡片未完整显示，已保存读取进度")
                        break
                position += delta
                frame = after
            else:
                raise RuntimeError("衣柜读取达到页数上限")
        except BaseException:
            if known:
                materialize()
                self.save()
            raise

        if sorted(known) != list(range(len(known))):
            raise RuntimeError(f"{state['name']}列表存在未读取的格子，已保留部分记录")
        if owned_count is not None and len(known) != owned_count:
            raise RuntimeError(
                f"{state['name']}已读取数量（{len(known)}）与图鉴显示数量（{owned_count}）不一致"
            )
        state["complete"] = True
        if part == "278":
            self.apply_inferred_handhelds()
        if part not in self.snapshot["verified_parts"]:
            self.snapshot["verified_parts"].append(part)
        self.save()
        self.rt.log(f"衣柜读取：{state['name']} {state['scanned_count']} 件。")

    def scroll_tabs(self, dy, target=None):
        point = target or [TAB_X, 340]
        if (not self.rt.action("TouchMove", target=point)
                or not self.rt.pause(.15)
                or not self.rt.action("Scroll", target=point, dx=0, dy=dy)
                or not self.rt.pause(.12)
                or not self.rt.action("TouchMove", target=[640, 36])
                or not self.rt.pause(.22)):
            raise TaskStopped()
        return self.rt.capture()

    def reset_subtab_scroll(self, y_min, y_max):
        mid_y = (y_min + y_max) // 2
        self.scroll_tabs(-12000, target=[TAB_X, 110])
        self.scroll_tabs(12000, target=[TAB_X, mid_y])
        return self.scroll_tabs(-12000, target=[TAB_X, 110])

    def click_tab_y(self, y):
        target_y = max(92, min(628, round(y)))
        if (not self.rt.action("Click", target=[TAB_X, target_y])
                or not self.rt.action("TouchMove", target=[640, 36])
                or not self.rt.pause(.28)):
            raise TaskStopped()
        return self.rt.capture(), target_y

    def read_current_part_with_retry(self, part):
        self.rt.log(f"衣柜读取：开始读取{self.model.parts[part]['name']}。", detail=True)
        while True:
            if self.rt.stopped:
                raise TaskStopped()
            try:
                self.read_part(part)
                return
            except RescanSlot:
                self._page_frame = None
                resumed = self.rt.capture()
                if self.category(resumed)[0] != part:
                    raise RuntimeError("切回游戏后图鉴部位已变化，请重新读取")
                if part in self.snapshot["parts"]:
                    self._previous[part] = deepcopy(self.snapshot["parts"][part])
                self.rt.log(f"衣柜读取：重新核对{self.model.parts[part]['name']}。")

    def select_major_tab(self, expected_part, major_index):
        frame = self.rt.capture()
        actual, _ = self.category(frame)
        if actual == expected_part:
            return frame
        order_map = {
            "299": 0, "210": 1, "290": 2, "220": 3,
            "230": 4, "242": 5, "250": 6, "260": 7,
        }
        if actual in ACCESSORY_PARTS or actual in MAKEUP_ALL:
            self.scroll_tabs(12000, target=[TAB_X, 110])
            frame, _ = self.click_tab_y(118)
            actual, _ = self.category(frame)
            if actual == expected_part:
                return frame
        if major_index <= 6:
            self.scroll_tabs(12000, target=[TAB_X, 110])
            y = 118 + 74 * major_index
        else:
            self.scroll_tabs(-12000, target=[TAB_X, 110])
            y = 610 - 74 * (9 - major_index)
        for _ in range(6):
            frame, clicked_y = self.click_tab_y(y)
            actual, _ = self.category(frame)
            if actual == expected_part:
                return frame
            if actual in order_map:
                next_y = clicked_y + (major_index - order_map[actual]) * 74
                if next_y < 92:
                    self.scroll_tabs(12000, target=[TAB_X, 110])
                    y = 118 + 74 * major_index
                elif next_y > 628:
                    self.scroll_tabs(-12000, target=[TAB_X, 110])
                    y = 610 - 74 * (9 - major_index)
                else:
                    y = next_y
            elif actual in ACCESSORY_PARTS or actual in MAKEUP_ALL:
                self.scroll_tabs(12000, target=[TAB_X, 110])
                self.click_tab_y(118)
                if major_index <= 6:
                    self.scroll_tabs(12000, target=[TAB_X, 110])
                    y = 118 + 74 * major_index
                else:
                    self.scroll_tabs(-12000, target=[TAB_X, 110])
                    y = 610 - 74 * (9 - major_index)
            else:
                break
        raise RuntimeError(f"未能切换到图鉴分类：{self.model.parts[expected_part]['name']}")

    def expand_subtab_group(self, group_Order_index, valid_subparts):
        frame = self.rt.capture()
        actual, _ = self.category(frame)
        if actual in valid_subparts:
            return frame, actual, 260
        if (actual in ACCESSORY_PARTS or actual in MAKEUP_ALL) and group_Order_index == 8:
            self.scroll_tabs(12000, target=[TAB_X, 110])
            self.click_tab_y(118)
        self.scroll_tabs(-12000, target=[TAB_X, 110])
        y = 610 - 74 * (9 - group_Order_index)
        for offset in (0, -24, 24, -48):
            frame, clicked_y = self.click_tab_y(y + offset)
            actual, _ = self.category(frame)
            if actual in valid_subparts:
                return frame, actual, clicked_y
        raise RuntimeError("未能展开图鉴二级分类列表")

    def traverse_subtabs(self, sub_order, target_parts, scroll_box_y_range, group_index=8):
        y_min, y_max = scroll_box_y_range
        pitch = 60.7
        first_slot_y = y_min + 16
        frame = self.reset_subtab_scroll(y_min, y_max)
        actual, _ = self.category(frame)
        if actual not in sub_order:
            raise RuntimeError("当前未处于预期二级分类列表")
        anchor_idx = sub_order.index(actual)
        anchor_y = first_slot_y + anchor_idx * pitch

        for target_part in target_parts:
            target_idx = sub_order.index(target_part)
            stalled = 0
            for _ in range(12):
                frame = self.rt.capture()
                actual, _ = self.category(frame)
                if actual == target_part:
                    break
                if actual not in sub_order:
                    self.expand_subtab_group(group_index, sub_order)
                    frame = self.reset_subtab_scroll(y_min, y_max)
                    actual, _ = self.category(frame)
                    if actual in sub_order:
                        anchor_idx = sub_order.index(actual)
                        anchor_y = first_slot_y + anchor_idx * pitch
                        stalled = 0
                        if actual == target_part:
                            break
                prev_idx = anchor_idx
                pred_y = anchor_y + (target_idx - anchor_idx) * pitch
                if y_min <= pred_y <= y_max:
                    nudge = (1 if target_idx >= anchor_idx else -1) * 18 * stalled
                    target_y = min(y_max - 6, max(first_slot_y, round(pred_y + nudge)))
                    frame, clicked_y = self.click_tab_y(target_y)
                    actual, _ = self.category(frame)
                    if actual in sub_order:
                        anchor_idx = sub_order.index(actual)
                        anchor_y = clicked_y
                        stalled = stalled + 1 if anchor_idx == prev_idx else 0
                        if actual == target_part:
                            break
                elif pred_y > y_max:
                    if stalled >= 1:
                        self.scroll_tabs(-12000, target=[TAB_X, 110])
                    self.scroll_tabs(-360, target=[TAB_X, (y_min + y_max) // 2])
                    if stalled >= 1:
                        probe_y = min(y_max - 6, max(first_slot_y, round(anchor_y + stalled * 42)))
                    else:
                        probe_y = min(y_max - 12, max(first_slot_y, round(pred_y - pitch)))
                    frame, clicked_y = self.click_tab_y(probe_y)
                    actual, _ = self.category(frame)
                    if actual in sub_order:
                        anchor_idx = sub_order.index(actual)
                        anchor_y = clicked_y
                        stalled = stalled + 1 if anchor_idx <= prev_idx else 0
                        if actual == target_part:
                            break
                else:
                    if target_idx <= 4:
                        self.reset_subtab_scroll(y_min, y_max)
                        probe_y = min(y_max - 12, max(first_slot_y, round(first_slot_y + target_idx * pitch)))
                    else:
                        self.scroll_tabs(360, target=[TAB_X, (y_min + y_max) // 2])
                        probe_y = max(first_slot_y, min(y_max - 12, round(pred_y + pitch)))
                    frame, clicked_y = self.click_tab_y(probe_y)
                    actual, _ = self.category(frame)
                    if actual in sub_order:
                        anchor_idx = sub_order.index(actual)
                        anchor_y = clicked_y
                        stalled = stalled + 1 if anchor_idx >= prev_idx else 0
                        if actual == target_part:
                            break
            else:
                raise RuntimeError(f"未能切换到图鉴子分类：{self.model.parts[target_part]['name']}")
            self.read_current_part_with_retry(target_part)

    def run(self):
        if not self.open():
            raise RuntimeError("未能打开博物图鉴检索页面")
        self.prepare_search_view()
        visited = set()

        with tall_window(self.rt) as is_tall:
            if is_tall:
                self._tall_mode = True
                self.layout["grid"] = [265, 130, 950, 4915]
                self.grid = self.layout["grid"]
                self.rt.log("衣柜读取：已切换超长竖屏模式（单屏 42 行），读取结束后将自动恢复游戏窗口。")
            try:
                for part, major_idx in MAJOR_SEQUENCE:
                    self.select_major_tab(part, major_idx)
                    self.read_current_part_with_retry(part)
                    visited.add(part)

                self.expand_subtab_group(8, ACCESSORY_PARTS)
                self.traverse_subtabs(ACCESSORY_PARTS, ACCESSORY_PARTS, (248, 535), group_index=8)
                visited.update(ACCESSORY_PARTS)

                self.expand_subtab_group(9, MAKEUP_ALL)
                self.traverse_subtabs(MAKEUP_ALL, MAKEUP_PARTS, (322, 625), group_index=9)
                visited.update(MAKEUP_PARTS)

                self.apply_inferred_handhelds()
                missing = [
                    info["name"]
                    for p, info in self.model.parts.items()
                    if p in self.ui_parts
                    and (p not in visited or not self.snapshot["parts"].get(p, {}).get("complete"))
                ]
                self.snapshot["complete"] = not missing
                self.save("待核对部位：" + "、".join(missing) if missing else None)
            finally:
                self._tall_mode = False
                self.layout["grid"] = [265, 130, 950, 505]
                self.grid = self.layout["grid"]

        return self.snapshot["complete"]
