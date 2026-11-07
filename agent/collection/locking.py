"""Lock one model-identified maximum-level Eureka per set, slot and colour."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import re

import cv2
import numpy as np

from task_control import TaskStopped
from daily.runtime import parameters
from daily.settings import ROOT
from .card_models import CardModels, badge_roi as classifier_roi
from .blessing import SLOTS
from .details import HEADER
from file_io import write_json
from .policy import capabilities
from .scanner import IncompleteObservation, InventoryScanner, clean_text


READY = "MaaNikki_Blessing_InventoryReady"


class BlessingLocker(InventoryScanner):
    """Shared normal-inventory details, selection and confirmed lock toggles.

    Acquisition-order traversal lives in ordered.py; organization and upgrades
    use the same verified lock button without re-finding a split selected item.
    """

    def __init__(self, runtime, catalog, folder, *, allow_unlock=False, read_only=False):
        super().__init__(runtime, catalog, folder)
        if type(allow_unlock) is not bool:
            raise ValueError("解锁不符合要求的闪光选项必须为布尔值")
        self.allow_unlock = allow_unlock
        self.read_only = bool(read_only)
        self.layout = parameters(runtime.context, "MaaNikki_BlessingLock_Layout")
        self._base_layout = deepcopy(self.layout)
        self._tall_mode = False
        self._sort_verified = False
        self.grid = self.layout["grid"]
        self._ordered_scroll_step = self.layout["scroll_step"]
        self.lock_templates = {}
        references = {}
        for state in ("locked", "unlocked"):
            reference = ROOT / f"resource/image/blessing_lock/{state}.png"
            image = cv2.imdecode(np.fromfile(str(reference), np.uint8), cv2.IMREAD_COLOR)
            if image is None or image.shape[:2] != tuple(reversed(self.layout["lock_icon"][2:])):
                raise ValueError(f"锁按钮图像资源缺失或尺寸不符：{state}")
            self.lock_templates[state] = self.white_mask(image) > 0
            references[state] = image
        # Extract the connected upper-left stroke that exists only when closed.
        # The shared body/keyhole and the right-hand chain never decide state.
        closed, opened = self.lock_templates["locked"], self.lock_templates["unlocked"]
        height, width = closed.shape
        gap = (closed & ~opened).astype(np.uint8)
        gap[height // 2:] = 0
        gap[:, width // 2:] = 0
        count, labels, areas, _ = cv2.connectedComponentsWithStats(gap)
        if count <= 1:
            raise ValueError("锁扣素材缺少已锁与未锁的左上缺口差异")
        component = 1 + int(np.argmax(areas[1:, cv2.CC_STAT_AREA]))
        self.lock_gap_mask = labels == component
        gx, gy, gw, gh = [int(value) for value in areas[component, :4]]
        self.lock_gap_roi = [gx, gy, gw, gh]
        # Shared upper/right strokes align the gap within the existing +/-2 px
        # capture tolerance. The gap itself is excluded from alignment scoring.
        self.lock_shackle_anchor = closed & opened
        self.lock_shackle_anchor[gy + gh:] = False
        if not self.lock_shackle_anchor.any():
            raise ValueError("锁扣素材缺少可对齐的共同锁梁")
        # Learn white brightness from the labelled white/non-white gap patches,
        # independently of whichever clothing appears behind the live button.
        white_floor = float(references["locked"][self.lock_gap_mask].min(axis=1).min())
        open_ceiling = float(references["unlocked"][self.lock_gap_mask].min(axis=1).max())
        if white_floor <= open_ceiling:
            raise ValueError("锁扣缺口素材无法区分白色与非白色")
        self.lock_white_cutoff = (white_floor + open_ceiling) / 2
        reference = ROOT / "resource/image/blessing_lock/locked_background.png"
        background = cv2.imdecode(np.fromfile(str(reference), np.uint8), cv2.IMREAD_UNCHANGED)
        if (background is None or background.ndim != 3 or background.shape[2] != 4
                or background.shape[:2] != tuple(reversed(self.layout["lock_background"][2:]))):
            raise ValueError("锁按钮背景资源缺失或尺寸不符")
        self.lock_background_mask = background[:, :, 3] > 0
        self.lock_background_lab = cv2.cvtColor(background[:, :, :3], cv2.COLOR_BGR2LAB)
        self.counter = 0
        self.report = {"created": datetime.now().isoformat(timespec="seconds"),
                       "unlock_nonconforming": allow_unlock,
                       "configured_scroll_step": self._ordered_scroll_step,
                       "complete": False, "slots": {}, "operations": [], "reason": "尚未完成"}
        self.pending = None
        self.inventory_snapshot = None
        self.lock_groups = {}
        self._hover_box = None
        self._hover_panel = None
        self.stack_count_cache = {}
        self.card_models = CardModels(ROOT / "resource/model", catalog)
        self._model_slot = None
        self.snapshot["scan"].update(verification_policy="page_card_models", model_cards=0, model_seconds=0.0)

    def save(self):
        write_json(self.folder / "locking.json", self.report)

    def record_card(self, frame, box, row, evidence, generation, index, *, error=None):
        """Keep the original card, quantity corner and lock corner together."""
        self.check_generation(generation)
        x, y, w, h = box
        badge_roi = [x + w - 51, y + h - 28, 48, 21]
        lock_roi = [x + w - 30, y + h - 35, 30, 32]
        prefix = f"card_{index:05d}"
        lock_recognition = dict(self.report.get("last_card_lock", {}))
        if not row["mutable"] and not row.get("catalog_unknown"):
            lock_recognition = {"source": "catalogue_immutable", "state": "locked"}
        elif lock_recognition.get("box") != list(box):
            lock_recognition = {"source": "unavailable"}
        diagnostic = {
            "index": index, "box": list(box), "badge_roi": badge_roi, "lock_roi": lock_roi,
            "reading_window": dict(self.report.get("reading_window", {})),
            "item": {key: row.get(key) for key in
                     ("name", "slot", "rarity", "level", "color", "ordinal", "locked", "count",
                      "catalog_unknown", "inventory_evidence", "count_evidence")},
            "lock_recognition": lock_recognition,
            "selected_for_observation": evidence.get("selected_for_observation", False),
            "selected_lock_button": dict(self.report.get("lock_recognition", {}))
                                    if evidence.get("selected_for_observation") else None,
            "detail": evidence.get("image"),
            "recognition": {key: evidence[key] for key in ("source", "model_versions", "level_source",
                                                           "identity_logit_gap", "colour_logit_gap") if key in evidence},
            "card": self.image(prefix + ".png", self.region(frame, box)),
            "badge": self.image(prefix + "_badge.png", self.region(frame, badge_roi)),
            "lock": self.image(prefix + "_lock.png", self.region(frame, lock_roi)),
            "classifier_patch": self.image(prefix + "_classifier.png", self.region(frame, classifier_roi(box))),
        }
        if error is not None:
            diagnostic.update(error=error, page=self.image(prefix + "_page.png", frame),
                              quantity_failure=self.report.get("quantity_failure"))
            self.rt.diagnostic("blessing_quantity", error, image=frame, details=diagnostic)
        if evidence.get("selected_for_observation", False):
            # The card badge may be ambiguous while selected. Retain the
            # actual button frame used for its gap/background classification,
            # rather than only the earlier hover screenshot.
            diagnostic["selected_page"] = self.image(prefix + "_selected.png", self._lock_frame)
        write_json(self.folder / (prefix + ".json"), diagnostic)

    def set_tall_layout(self, enabled: bool):
        self._tall_mode = bool(enabled)
        self.layout = deepcopy(self._base_layout)
        if self._tall_mode:
            dy = 5120 - 720
            self.layout["grid"][3] += dy
            self.layout["capacity"][1] += dy
            self.layout["lock_icon"][1] += dy
            self.layout["lock_background"][1] += dy
            self.layout["lock_button"][1] += dy
            self.layout["sort_label"][1] += dy
            self.layout["scroll_step"] = 14400
            self.layout["adaptive_scroll_delta"] = True
        self.grid = self.layout["grid"]
        self._ordered_scroll_step = self.layout["scroll_step"]

    def frame(self, generation):
        for attempt in range(6):
            frame = self.rt.capture()
            self.check_generation(generation)
            if (frame is not None
                    and frame.shape[:2] in ((720, 1280), (5120, 1280))
                    and self.ready(frame)):
                return frame
            if attempt < 5 and frame is not None and float(frame.mean()) < 8.0:
                if not self.rt.pause(.15):
                    break
                continue
            break
        raise IncompleteObservation("未确认祝福闪光普通库存页（需要 16:9 窗口）")

    def ready(self, frame):
        generation = self.rt._focus_generation
        self.check_generation(generation)
        if (generation == self._ready_generation
                and self.same_structure(frame, self._ready_frame, [930, 15, 340, 52], threshold=.997)):
            return True
        result = self.rt.recognize(READY, image=frame)
        self.check_generation(generation)
        matched = bool(result and result.hit)
        if matched:
            self._ready_frame, self._ready_generation = frame, generation
        return matched

    def check_generation(self, generation):
        if self.rt.stopped:
            raise TaskStopped("用户停止祝福闪光仓库读取")
        super().check_generation(generation)

    def open(self):
        return self.rt.ui.find_menu_entry(node="MaaNikki_Blessing_Entrance", scroll=True)

    def detail_frame(self, generation):
        return self.frame(generation)

    def clear_hover(self, generation, *, settle_card=False):
        old_box = self._hover_box
        panel = self._hover_panel
        if panel is not None and (panel[0] != old_box or panel[1] != generation):
            panel = None
        self._hover_box = None
        if not self.rt.action("TouchMove", target=[900, 32]) or not self.rt.pause(.06):
            self.check_generation(generation)
            raise IncompleteObservation("未能关闭悬停信息")
        previous = None
        for _ in range(8 if settle_card else 6):
            frame = self.frame(generation)
            visible = False
            if old_box is not None:
                roi = self.detail_roi(panel[2]) if panel is not None else self.header_region(frame, old_box)
                if roi is not None:
                    if panel is not None and self.same_fields(panel[3], frame, panel[2]):
                        visible = True
                    elif not (self.same_structure(frame, self._page_frame, roi)
                              or previous is not None and self.same_structure(previous, frame, roi)):
                        # Bright clothing can look like the tooltip's paper.
                        # The clear-page reference handles unchanged scenery;
                        # after selection/camera changes, verify local header
                        # text using the scanner's existing stale-panel check.
                        visible = bool(self.ocr(roi, frame, HEADER))
                        self.check_generation(generation)
            if not visible:
                if (not settle_card or old_box is None or previous is not None
                        and self.same_structure(previous, frame, old_box, threshold=.997)):
                    self._hover_panel = None
                    return frame
                # The tooltip can disappear before the hovered card's glow
                # fades. That glow defeated the third-column scroll matches.
                previous = frame
            else:
                previous = None
            if not self.rt.pause(.04):
                self.check_generation(generation)
        reason = "悬停提示尚未关闭" if visible else "格子高亮尚未稳定"
        raise IncompleteObservation(reason + "，未继续读取或翻页")

    def capacity_values(self, frame):
        """Used and total copies; the game can show more used than total, e.g. 1006/1000."""
        counts = []
        for item in self.ocr(self.layout["capacity"], frame):
            match = re.search(r"(\d+)[/／](\d+)", clean_text(item.text))
            if match and float(getattr(item, "score", 0)) >= .90:
                used, limit = int(match.group(1)), int(match.group(2))
                if 0 <= used <= 10000 and 0 < limit <= 10000:
                    counts.append((used, limit))
        return counts

    def scroll_list(self, amount):
        # Generic ui.scroll waits .2 s with the pointer still over the grid;
        # the scanner then moves it away and waits another .25 s. Issue both
        # inputs together here and retain the full post-move settling window.
        # advance() still verifies translation before reading the next page.
        x, y, w, h = self.grid
        target_y = min(y + h - 8, 520)
        return (self.rt.action("Scroll", target=[x + w - 8, target_y], dx=0, dy=amount)
                and self.rt.action("TouchMove", target=[420, 32]) and self.rt.pause(.25))

    def scroll_amounts(self):
        # Observed 1200 wheel units move 213 px; 1800 targets about 320 px.
        # In the 560 px grid this leaves roughly two rows of overlap, within
        # scroll_position's 343 px matching range (grid - card - row pitch).
        # Smaller amounts are retries of this move only. Repeated artwork or
        # missing contours in a local overlap do not imply a smaller wheel
        # budget is necessary for the rest of the inventory.
        step = self._ordered_scroll_step
        smaller = max(120, step // 240 * 120)
        return tuple(dict.fromkeys((step, smaller, 120)))

    def record_scroll(self, before, after, amount, delta, phase):
        super().record_scroll(before, after, amount, delta, phase)
        if delta is None and phase in ("down", "ordered_up") and abs(amount) > 120:
            self.report.setdefault("local_scroll_fallbacks", []).append({
                "slot": self._model_slot, "wheel_amount": amount,
                "scroll_index": len(self.snapshot["scan"]["scrolls"]) - 1})

    def header_region(self, frame, box):
        """Locate the light tooltip panel left of a card without OCR."""
        left = max(0, box[0] - 334)
        band = self.region(frame, [left, 70, 328, 550])
        high, low = band.max(axis=2), band.min(axis=2)
        paper = (low > 170) & (high.astype(np.int16) - low < 90)
        rows = np.mean(paper[:, 12:-12], axis=1) > .65
        # A thin border or a bright inventory card is not a tooltip panel.
        starts = np.flatnonzero(np.convolve(rows.astype(np.uint8), np.ones(8, dtype=np.uint8), "valid") == 8)
        if not len(starts):
            return None
        top = max(70, 70 + int(starts[0]) - 6)
        return [left, top, 328, min(126, 720 - top)]

    def recognize_cards(self, frame, boxes, slot, *, purpose="page"):
        generation = self.rt._focus_generation
        self.check_generation(generation)
        matches = self.card_models.match_batch(frame, boxes, slot)
        self.check_generation(generation)
        timing = {**self.card_models.last_timing, "slot": slot, "purpose": purpose}
        if boxes:
            self.report.setdefault("model_batches", []).append(timing)
            scan = self.snapshot["scan"]
            scan["model_cards"] += len(boxes)
            scan["model_seconds"] = round(scan["model_seconds"] + timing["seconds"], 6)
            if purpose == "page":
                self.rt.log(f"祝福闪光{slot}：批量识别 {len(boxes)} 个新格子，模型耗时 {timing['seconds']:.3f} 秒。",
                            detail=True)
        return matches

    def confirm_copy(self, frame, box, match, level, generation, index):
        self.check_generation(generation)
        facts = match.get("model_facts")
        if facts is None:
            raise IncompleteObservation("当前格子缺少批量模型识别结果，保留已有进度")
        return dict(facts), dict(match["model_evidence"])

    def read_details(self, box, generation, index):
        """Recheck a selected card directly; no pointer move or tooltip wait."""
        frame = self.frame(generation)
        result, = self.recognize_cards(frame, [box], self._model_slot, purpose="selected_recheck")
        return self.confirm_copy(frame, box, result, result["model_facts"]["level"], generation, index)

    def same_card_facts(self, actual, expected):
        # Upgrades always continue to the maximum indicator. Intermediate
        # numeral estimates do not decide whether the selected target matches.
        return (self.key(actual) == self.key(expected) and actual["rarity"] == expected["rarity"]
                and self.is_maximum(actual) == self.is_maximum(expected))

    @staticmethod
    def key(facts):
        return facts["name"], facts["slot"], facts["color"]

    def is_maximum(self, facts):
        return facts["level"] == self.catalog["upgrade_costs"][str(facts["rarity"])]["max_level"]

    @staticmethod
    def white_mask(image):
        high, low = image.max(axis=2), image.min(axis=2)
        # The translucent button inherits the scene brightness. A fixed white
        # cutoff erased the bottom of the glyph in the 2026-10-15 run. Preserve
        # local contrast while keeping coloured backgrounds out of the mask.
        cutoff = max(140, float(np.median(low)) + 35)
        return ((low >= cutoff) & (high.astype(np.int16) - low <= 40)).astype(np.uint8) * 255

    def button_background(self, frame):
        """Observe the gold fill independently of the translucent lock glyph."""
        crop = self.region(frame, self.layout["lock_background"])
        if crop.shape != self.lock_background_lab.shape:
            return {"state": None}
        lab = cv2.cvtColor(crop, cv2.COLOR_BGR2LAB).astype(np.int16)
        reference = self.lock_background_lab.astype(np.int16)
        # The yellow button's brightness and saturation vary with the scene.
        # Saved closed-lock captures retain the yellow fill but are darker
        # than the reference, especially along the upper rim. Verify colour
        # rather than reference luminance: preserve the existing red/green
        # tolerance and minimum yellow chroma; stronger yellow is still yellow.
        # The four separated sectors continue to require a filled background.
        similar = ((np.abs(lab[:, :, 1] - reference[:, :, 1]) <= 8)
                   & (lab[:, :, 2] >= reference[:, :, 2] - 8))
        mask = self.lock_background_mask
        height, width = mask.shape
        sectors = []
        for ys in (slice(0, height // 2), slice(height // 2, height)):
            for xs in (slice(0, width // 2), slice(width // 2, width)):
                sector = mask[ys, xs]
                if not sector.any():
                    return {"state": None}
                sectors.append(float(similar[ys, xs][sector].mean()))
        fraction = float(similar[mask].mean())
        state = "locked" if fraction >= .88 and min(sectors) >= .75 else "unlocked"
        return {"state": state, "gold_fraction": fraction, "sector_fractions": sectors,
                "lab_chroma_tolerance": 8, "luminance_checked": False,
                "stronger_yellow_allowed": True, "gold_minimum": .88,
                "gold_sector_minimum": .75}

    def icon_state(self, frame):
        """Non-white gap is open; a white gap requires the gold-fill check."""
        x, y, w, h = self.layout["lock_icon"]
        search = self.region(frame, [x - 2, y - 2, w + 4, h + 4])
        if search.shape[:2] != (h + 4, w + 4):
            self.report["lock_recognition"] = {"state": None, "reason": "incomplete_lock_crop"}
            return None
        low, high = search.min(axis=2), search.max(axis=2)
        white = ((low >= self.lock_white_cutoff)
                 & (high.astype(np.int16) - low <= 40))
        # Only the invariant upper/right shackle locates the sampling mask.
        # Prefer the calibrated position for equal support; never select a
        # translation because it makes the gap look open or closed.
        candidates = []
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                patch = white[2 + dy:2 + dy + h, 2 + dx:2 + dx + w]
                support = int(np.count_nonzero(patch[self.lock_shackle_anchor]))
                candidates.append((support, -(dx * dx + dy * dy), dx, dy))
        support, _, dx, dy = max(candidates, key=lambda item: item[:2])
        if support == 0:
            self.report["lock_recognition"] = {"state": None, "reason": "shackle_not_visible"}
            return None
        patch = white[2 + dy:2 + dy + h, 2 + dx:2 + dx + w]
        white_pixels = int(np.count_nonzero(patch[self.lock_gap_mask]))
        gap_pixels = int(np.count_nonzero(self.lock_gap_mask))
        gap_white = white_pixels == gap_pixels
        # Follow the requested asymmetric rule exactly: any non-white gap
        # establishes unlocked state. White alone may come from clothing, so
        # only that branch checks the independent opaque yellow background.
        background = self.button_background(frame) if gap_white else {"checked": False}
        state = background.get("state") if gap_white else "unlocked"
        gx, gy, gw, gh = self.lock_gap_roi
        self.report["lock_recognition"] = {
            "strategy": "shackle_gap_then_gold_background",
            "gap_roi": [x + dx + gx, y + dy + gy, gw, gh],
            "gap_pixels": gap_pixels, "white_pixels": white_pixels,
            "gap_white": gap_white, "white_cutoff": self.lock_white_cutoff,
            "white_channel_spread": 40,
            "alignment_shift": [dx, dy], "alignment_white_pixels": support,
            "alignment_pixels": int(np.count_nonzero(self.lock_shackle_anchor)),
            "background": background, "state": state}
        return state

    def card_lock_state(self, frame, box, *, classification=None):
        """Reuse the page batch; only ambiguous badges need button selection."""
        result = classification if classification is not None else self.card_models.badges(frame, [box])[0]
        kind = result["badge_state"]
        state = "locked" if kind == "locked" else "unlocked" if kind in ("stack", "single") else None
        source = "classifier_" + kind if kind is not None else "ambiguous"
        stats = self.report.setdefault("card_state_sources", {})
        stats[source] = stats.get(source, 0) + 1
        self.report["last_card_lock"] = {**result, "box": list(box), "state": state, "source": source}
        return state

    def lock_state(self, generation, *, frame=None):
        if frame is None:
            frame = self.clear_hover(generation)
        submitted = (self.pending is not None and self.pending.get("kind") in ("lock", "unlock")
                     and self.pending.get("status") == "submitted_unconfirmed")
        previous = self.icon_state(frame)
        observations = [{"attempt": "initial", **self.report.get("lock_recognition", {})}]
        for attempt in range(12):
            if attempt == 2 and previous is None and submitted:
                # Reset hover only after a submitted toggle. Before a toggle,
                # preserve the pointer position prepared for its single click.
                frame = self.clear_hover(generation)
                previous = self.icon_state(frame)
                observations.append({"attempt": "hover_cleared", **self.report.get("lock_recognition", {})})
            if not self.rt.pause(.06):
                if not submitted:
                    raise IncompleteObservation("锁定状态核对被中断")
                raise RuntimeError("锁定状态核对被中断")
            frame = self.frame(generation)
            current = self.icon_state(frame)
            observations.append({"attempt": attempt + 1, **self.report.get("lock_recognition", {})})
            if current is not None and current == previous:
                self._lock_frame = frame
                return current
            previous = current
        self.counter += 1
        self.image(f"lock_{self.counter:05d}_unknown.png", frame)
        reason = ("锁按钮已点击，但未能确认结果，已停止且不会再次点击" if submitted
                  else "操作前无法区分锁定与解锁，未点击锁按钮")
        self.rt.diagnostic("blessing_lock_state", reason, image=frame,
                           details={**self.report.get("lock_recognition", {}), "frames": observations})
        if not submitted:
            raise IncompleteObservation(reason)
        raise RuntimeError(reason)

    def selected(self, frame, box):
        x, y, w, h = box
        # Selected cards expand their opaque gold border 3-4 px outside the
        # nominal unselected cell box, while the translucent interior blends
        # with the 3D scene behind the panel. Measure the strongest 4 px
        # border strip across each edge's inner/outer boundary.
        x0, x1 = max(0, x - 4), min(frame.shape[1], x + w + 3)
        crop = frame[y + 5:y + h - 8, x0:x1]
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        gold = ((hsv[:, :, 0] >= 12) & (hsv[:, :, 0] <= 42)
                & (hsv[:, :, 1] >= 25) & (hsv[:, :, 2] >= 120))
        width = gold.shape[1]
        if width < 8:
            return False
        left = max(float(gold[:, i:i + 4].mean()) for i in range(min(5, width - 3)))
        # Nominal cell width w=95 extends 2 px past the unhovered right edge;
        # after clear_hover() shrinks the selected card back from its hovered
        # outer frame, the 4 px inner gold strip sits at [x+w-6 : x+w-2] (i=5).
        right = max(float(gold[:, width - 4 - i:width - i].mean()) for i in range(min(6, width - 3)))
        return bool(left > .45 and right > .45)

    def select(self, box, facts, generation, *, verify_details=True, already_selected=False):
        self.check_generation(generation)
        # One direct click selects the card; no preparatory tooltip hover.
        x, y, w, h = box
        point = [round(x + w / 2), round(y + h * .45)]
        self._hover_box = None
        if not already_selected and self._page_frame is not None and self.selected(self._page_frame, box):
            already_selected = True
        if not already_selected and (
                not self.rt.action("Click", target=point, ui_click=False) or not self.rt.pause(.15)):
            raise RuntimeError("未能选中目标闪光")
        if not already_selected:
            # Track the pointer only so scrolling can wait for its highlight
            # to fade after moving away. No tooltip metadata are read.
            self._hover_box = tuple(box)
        frame = self.frame(generation)
        if not self.selected(frame, box):
            # Click delivery and the selection animation are asynchronous.
            # Keep the pointer in place and observe the same target until its
            # existing highlight predicate passes; never submit another click.
            with self.rt.ui.preserve_cursor():
                if not self.rt.ui.wait_page(lambda: self.selected(self.frame(generation), box)):
                    raise IncompleteObservation("目标格子的选中高亮尚未确认，保留进度")
            frame = self.frame(generation)
        if verify_details:
            selected, = self.recognize_cards(frame, [box], self._model_slot, purpose="selected_recheck")
            if not self.same_card_facts(selected["model_facts"], facts):
                raise RuntimeError("选中后闪光信息发生变化，未操作锁按钮")
        # The lock button is outside the card tooltip. Keep the pointer here;
        # both its state and the selection are read from fresh captures.
        return self.lock_state(generation, frame=frame)

    def sort_direction(self, frame, template):
        # Desktop screenshots and runtime captures can differ by a few pixels
        # after normalization. Search a small neighbourhood, not a fixed crop.
        dy = frame.shape[0] - 720
        crop = cv2.cvtColor(self.region(frame, [1220, 665 + dy, 34, 38]), cv2.COLOR_BGR2GRAY)
        scores = {state: float(cv2.matchTemplate(crop, candidate, cv2.TM_CCOEFF_NORMED).max())
                  for state, candidate in (("desc", template), ("asc", cv2.rotate(template, cv2.ROTATE_180)))}
        best = max(scores, key=scores.get)
        other = "asc" if best == "desc" else "desc"
        self.report["sort_recognition"] = {"scores": scores, "best": best,
                                            "minimum_score": .85, "minimum_margin": .10}
        return best if scores[best] >= .85 and scores[best] - scores[other] >= .10 else None

    def set_slot(self, slot):
        node = "MaaNikki_Blessing_" + slot
        index = SLOTS.index(slot)
        ready = self.rt.ui.enter_page(
            lambda: self.rt.action("Click", target=self.layout["slot_buttons"][index]),
            node, source=READY, attempts=2)
        if ready:
            self._model_slot = slot
            self._ordered_scroll_step = self.layout["scroll_step"]
            if self._tall_mode:
                generation = self.rt._focus_generation
                prev_count = -1
                for _ in range(6):
                    frame = self.frame(generation)
                    cur_count = len(self.cards(frame, []))
                    if cur_count > 0 and cur_count == prev_count:
                        break
                    prev_count = cur_count
                    if not self.rt.pause(.08):
                        break
        return ready

    def acquisition_sort(self):
        """Select acquisition order and confirm the user-supplied down arrow."""
        if self._tall_mode and self._sort_verified:
            return
        generation = self.rt._focus_generation
        frame = self.clear_hover(generation)
        if not self.rt.text(self.layout["sort_label"], "获得时间", image=frame):
            # Capacity text sits beside the final option and can merge into its OCR line.
            if (not self.rt.action("Click", target=self.layout["sort_label"])
                    or not self.rt.pause(.2)
                    or not self.rt.click_text("^获得时间$", [1055, 550, 84, 110])
                    or not self.rt.pause(.3)):
                raise RuntimeError("未能切换获得时间排序")
        reference = ROOT / "resource/image/blessing_lock/acquired_desc.png"
        template = cv2.imdecode(np.fromfile(str(reference), np.uint8), cv2.IMREAD_GRAYSCALE)
        if template is None:
            raise RuntimeError("获得时间排序方向资源缺失")
        self._acquired_desc_template = template
        for attempt in range(2):
            frame = self.clear_hover(generation)
            if not self.rt.text(self.layout["sort_label"], "获得时间", image=frame):
                raise RuntimeError("未确认获得时间排序")
            direction = self.sort_direction(frame, template)
            if not self.rt.pause(.1):
                raise RuntimeError("排序方向核对被中断")
            frame = self.frame(generation)
            if direction != self.sort_direction(frame, template):
                direction = None
            if direction == "desc":
                self._sort_verified = True
                return
            if attempt == 0 and direction == "asc":
                if self.rt.action("Click", target=[1237, 684]) and self.rt.pause(.3):
                    continue
            self.image("sort_direction_unknown.png", frame)
            raise RuntimeError("未确认获得时间从新到旧的排序箭头，请设为默认降序后重试")

    def unchanged(self, before, after):
        if not self._tall_mode:
            return self.same_structure(before, after, self.grid, threshold=.997)
        if self.same_structure(before, after, self._base_layout["grid"], threshold=.997):
            return True
        boxes = self.cards(before, [])
        if not boxes:
            return False
        # Cards themselves are opaque; exclude transparent gaps, empty space
        # below the list, the selected first card's border, and scroll target.
        sample = [
            b for b in (boxes[1:16] if len(boxes) > 1 else boxes[:1])
            if not (b[0] <= 1246 <= b[0] + b[2] and b[1] <= 520 <= b[1] + b[3])
        ] or boxes[:1]
        return all(
            self.same_structure(before, after, [x + 4, y + 4, w - 8, h - 8], threshold=.99)
            for x, y, w, h in sample
        )

    def top(self):
        generation = self.rt._focus_generation
        self.clear_hover(generation)
        return super().top()

    def check_coverage(self, seen):
        """Do not declare an audit complete if cell discovery left a gap."""
        rows = []
        for x, y in sorted(seen, key=lambda point: point[1]):
            row = next((row for row in rows if abs(row["y"] - y) < 12), None)
            if row is None:
                row = {"y": y, "columns": set()}
                rows.append(row)
            row["columns"].add(round((x - self.layout["first_x"]) / self.layout["column_pitch"]))
        pitch = self.layout["row_pitch"]
        if not rows or abs(rows[0]["y"] - self.grid[1]) > 10:
            raise RuntimeError("未完整识别列表首行")
        for index, row in enumerate(rows):
            columns = row["columns"]
            expected = set(range(self.layout["columns"] if index < len(rows) - 1 else len(columns)))
            if columns != expected or index and abs(row["y"] - rows[index - 1]["y"] - pitch) > 12:
                raise RuntimeError("库存格子存在识别空缺，未执行后续补锁")

    def change_lock(self, box, facts, evidence, generation, *, target="locked", reason="missing_maximum"):
        if not capabilities(self.catalog, facts["name"], observed=facts)["lock_mutable"]:
            raise RuntimeError("特殊闪光不可修改锁定状态")
        if target not in ("locked", "unlocked"):
            raise ValueError("目标锁定状态无效")
        if target == "unlocked" and not self.allow_unlock:
            raise RuntimeError("解锁选项关闭，禁止提交解锁操作")
        verb = "锁定" if target == "locked" else "解锁"
        source = "unlocked" if target == "locked" else "locked"
        # No stale coordinate is reused for the toggle. Stage the mouse first,
        # then recapture the page, selection and source state before one input.
        if not self.rt.action("TouchMove", target=self.layout["lock_button"]) or not self.rt.pause(.3):
            raise RuntimeError(f"未能准备{verb}操作")
        current = self.frame(generation)
        if not self.selected(current, box):
            raise RuntimeError(f"{verb}前目标选中状态丢失")
        if self.lock_state(generation, frame=current) != source:
            raise RuntimeError(f"{verb}前状态改变，未再次点击")
        self._hover_box = None
        self.check_generation(generation)
        operation = {"key": list(self.key(facts)), "level": facts["level"],
                     "kind": "lock" if target == "locked" else "unlock", "reason": reason,
                     "expected_state": target,
                     "status": "submitted_unconfirmed", "evidence": evidence,
                     "input_status": "prepared",
                     "before": self.image(f"before_{len(self.report['operations']):03d}.png", current)}
        self.pending = operation
        self.report["operations"].append(operation)
        self.save()
        # The lock toggle is submitted once even if delivery/verification fails.
        if not self.rt.action("Click", target=self.layout["lock_button"], ui_click=False,
                              focus_generation=generation):
            operation["input_status"] = "uncertain"
            raise RuntimeError(f"{verb}输入结果不确定，停止且不重试，请检查当前物品")
        operation["input_status"] = "succeeded"
        if not self.rt.pause(.4):
            raise RuntimeError(f"{verb}后核对被中断")
        self.check_generation(generation)

    def confirm_selected_lock(self, generation):
        """Observe the verified selection's button without another grid read.

        Identity was checked before the toggle. No selection or scroll input
        intervenes, so splitting the stack does not require finding it again.
        """
        target = self.pending["expected_state"]
        if self.lock_state(generation, frame=self.frame(generation)) != target:
            raise RuntimeError("未确认当前选中项的锁定状态已改变，已停止且不重试")
        evidence = {"source": "selected_lock_button",
                    "image": self.image(f"after_{len(self.report['operations']) - 1:03d}.png",
                                        self._lock_frame),
                    "lock_recognition": dict(self.report["lock_recognition"])}
        self.pending.update(status="confirmed", confirmation=evidence)
        self.save()
        self.pending = None
