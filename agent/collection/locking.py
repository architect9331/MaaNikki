"""Lock one model-identified maximum-level Eureka per set, slot and colour."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import re

import cv2
import numpy as np

from task_control import ForegroundChanged, TaskStopped
from daily.runtime import parameters
from daily.settings import ROOT
from tall_window import clamp_capture_park_point, is_capture_point_safe, safe_capture_y_range
from .card_models import CardModels, badge_roi as classifier_roi
from .blessing import SLOTS
from .details import COLOR_OCR_THRESHOLD, HEADER, tooltip_facts
from file_io import write_json
from .policy import capabilities
from .scanner import FocusRetryBudget, IncompleteObservation, InventoryScanner, RescanSlot, clean_text


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
        from data_paths import data_path
        self.stack_count_cache = {}
        self.card_models = CardModels(data_path("resource/model"), catalog)
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
        }
        if error is not None or row.get("catalog_unknown") or lock_recognition.get("source") == "unavailable":
            diagnostic.update(
                badge=self.image(prefix + "_badge.png", self.region(frame, badge_roi)),
                lock=self.image(prefix + "_lock.png", self.region(frame, lock_roi)),
                classifier_patch=self.image(prefix + "_classifier.png", self.region(frame, classifier_roi(box))),
            )
        if error is not None:
            diagnostic.update(error=error, page=self.image(prefix + "_page.jpg", frame),
                              quantity_failure=self.report.get("quantity_failure"))
            self.rt.diagnostic("blessing_quantity", error, image=frame, details=diagnostic)
        if evidence.get("selected_for_observation", False):
            # The card badge may be ambiguous while selected. Retain the
            # actual button frame used for its gap/background classification,
            # rather than only the earlier hover screenshot.
            diagnostic["selected_page"] = self.image(prefix + "_selected.jpg", self._lock_frame)
        write_json(self.folder / (prefix + ".json"), diagnostic)

    def set_tall_layout(self, enabled: bool):
        self._tall_mode = bool(enabled)
        self.layout = deepcopy(self._base_layout)
        if self._tall_mode:
            dy = 5120 - 720
            base_grid_h = self._base_layout["grid"][3]
            self.layout["grid"][3] += dy
            self.layout["bottom_fade_margin"] = round(
                self._base_layout.get("bottom_fade_margin", 32)
                * self.layout["grid"][3]
                / base_grid_h
            )
            self.layout["capacity"][1] += dy
            self.layout["lock_icon"][1] += dy
            self.layout["lock_background"][1] += dy
            self.layout["lock_button"][1] += dy
            self.layout["sort_label"][1] += dy
            self.layout["scroll_step"] = 14400
            self.layout["adaptive_scroll_delta"] = True
        self.grid = self.layout["grid"]
        self._ordered_scroll_step = self.layout["scroll_step"]

    def frame(self, generation, *, capture_fn=None):
        grab = self.rt.capture if capture_fn is None else capture_fn
        for attempt in range(6):
            frame = grab()
            self.check_generation(generation)
            if (frame is not None
                    and frame.shape[:2] in ((720, 1280), (5120, 1280))
                    and self.ready(frame)):
                return frame
            if attempt < 5:
                if not self.rt.pause(.15):
                    break
                continue
            break
        raise IncompleteObservation("未确认祝福闪光普通库存页（需要 16:9 窗口）")

    def ready(self, frame):
        generation = self.current_generation()
        self.check_generation(generation)
        focus_generation = self.rt._focus_generation
        if (focus_generation == self._ready_generation
                and self.same_structure(frame, self._ready_frame, [930, 15, 340, 52], threshold=.997)):
            return True
        result = self.rt.recognize(READY, image=frame)
        self.check_generation(generation)
        matched = bool(result and result.hit)
        if matched:
            self._ready_frame, self._ready_generation = frame, focus_generation
        return matched

    def check_generation(self, generation, *, focus_sensitive=False):
        if self.rt.stopped:
            raise TaskStopped("用户停止祝福闪光仓库读取")
        super().check_generation(generation, focus_sensitive=focus_sensitive)

    def open(self):
        return self.rt.ui.find_menu_entry(node="MaaNikki_Blessing_Entrance", scroll=True)

    def detail_frame(self, generation):
        return self.frame(generation)

    def _park_point(self):
        hwnd, cap_size = self.rt._pointer_guard_context()
        return clamp_capture_park_point(hwnd, [900, 32], capture_size=cap_size)

    def clear_hover(self, generation, *, settle_card=False):
        old_box = self._hover_box
        panel = self._hover_panel
        if panel is not None and (panel[0] != old_box or panel[1] != generation):
            panel = None
        self._hover_box = None
        if not self.rt.action("TouchMove", target=self._park_point()) or not self.rt.pause(.06):
            self.check_generation(generation)
            raise IncompleteObservation("未能关闭悬停信息")
        previous = None
        hold_park = False
        for _ in range(8 if settle_card else 6):
            capture_fn = (
                (lambda: self.rt.capture_parked(self._park_point()))
                if hold_park and hasattr(self.rt, "capture_parked")
                else None
            )
            frame = self.frame(generation, capture_fn=capture_fn)
            visible = False
            candidates = ([self.detail_roi(panel[2]) if panel is not None else self.header_region(frame, old_box)]
                          if old_box is not None else
                          [self.header_region(frame, [self.layout["first_x"] + col * self.layout["column_pitch"], 72, 95, 101])
                           for col in range(self.layout["columns"])])
            for roi in candidates:
                if roi is None:
                    continue
                if old_box is not None and panel is not None and self.same_fields(panel[3], frame, panel[2]):
                    visible = True
                    break
                if not (self.same_structure(frame, self._page_frame, roi)
                        or previous is not None and self.same_structure(previous, frame, roi)):
                    # Bright clothing can look like the tooltip's paper.
                    # The clear-page reference handles unchanged scenery;
                    # after selection/camera changes, verify local header
                    # text using the scanner's existing stale-panel check.
                    if self.ocr(roi, frame, HEADER):
                        self.check_generation(generation)
                        visible = True
                        break
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
                hold_park = True
                self.rt.action("TouchMove", target=self._park_point())
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
        # inputs together in the inter-column gap so wheel events never hover a
        # card, and retain the full post-move settling window.
        x, y, w, h = self.grid
        gap_x = (self.layout["first_x"] + self.layout["card_size"][0]
                 + max(2, (self.layout["column_pitch"] - self.layout["card_size"][0]) // 2))
        hwnd, cap_size = self.rt._pointer_guard_context()
        safe_min_y, safe_max_y = safe_capture_y_range(hwnd, cap_size[1], capture_width=cap_size[0], margin_px=8)
        target_x = min(x + w - 8, max(x + 8, gap_x))
        target_y = max(safe_min_y + 8, min(y + h - 8, safe_max_y - 8, 520))
        return (self.rt.action("Scroll", target=[target_x, target_y], dx=0, dy=amount)
                and self.rt.action("TouchMove", target=self._park_point()) and self.rt.pause(.25))

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
        generation = self.current_generation()
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
        """Recheck a selected card directly after its selection animation settles."""
        frame = self._settle_selected_card(self.frame(generation), box, generation)
        result, = self.recognize_cards(frame, [box], self._model_slot, purpose="selected_recheck")
        return self.confirm_copy(frame, box, result, result["model_facts"]["level"], generation, index)

    def _settle_selected_card(self, frame, box, generation):
        """Wait for the post-click flash/scale transition on a selected card to settle."""
        previous = frame
        check_selected = (
            self.unhovered_selected if self._hover_box is None else self.selected
        )
        with self.rt.ui.preserve_cursor():
            for _ in range(6):
                if not self.rt.pause(.05):
                    self.check_generation(generation)
                    break
                current = self.frame(generation)
                if not check_selected(current, box):
                    previous = None
                    continue
                if previous is not None and self.same_structure(previous, current, box, threshold=.997):
                    return current
                previous = current
        if previous is None:
            raise IncompleteObservation("目标格子的选中高亮尚未稳定，保留进度")
        return previous

    def _read_selected_tooltip(self, box, generation, *, retry_hover=True):
        """Re-hover a selected card in 720p mode to read its authoritative tooltip text."""
        if self._tall_mode:
            return None, None
        baseline = self.clear_hover(generation)
        if not retry_hover and not self.rt.pause(.08):
            self.check_generation(generation)
            return None, baseline
        x, y, w, h = box
        point = [round(x + w / 2), round(y + h * .45)]
        if not self.rt.action("TouchMove", target=point):
            self.check_generation(generation)
            return None, baseline
        self._hover_box = tuple(box)
        self._page_frame = baseline

        def usable(parsed):
            return parsed is not None and (
                parsed.get("color") is not None
                or not capabilities(self.catalog, parsed["name"], observed=parsed)["lock_mutable"]
                or not self.is_maximum(parsed)
            )

        previous_frame, previous_roi = None, None
        previous_facts, previous_strict = None, False
        facts_frame, facts_roi = None, None
        reads = 0
        hold_point = False
        roi = None
        frame = baseline
        for attempt in range(16):
            if not self.rt.pause(.12 if attempt == 0 else .04):
                self.check_generation(generation)
                break
            capture_fn = (
                (lambda: self.rt.capture_parked(point, settle_s=.12 if roi is None else .06))
                if hold_point and hasattr(self.rt, "capture_parked")
                else None
            )
            frame = self.frame(generation, capture_fn=capture_fn)
            if not (self.selected(frame, box) or self._selection_border(frame, box)):
                return None, frame
            roi = self.header_region(frame, box)
            if roi is None or not self.tooltip_visible(frame, roi):
                if attempt >= 3 and self.rt._seamless_background():
                    hold_point = True
                previous_frame, previous_roi = None, None
                previous_facts, previous_strict = None, False
                facts_frame, facts_roi = None, None
                continue
            if (usable(previous_facts) and not previous_facts.get("catalog_unknown")
                    and previous_strict and facts_roi == roi
                    and self.same_fields(facts_frame, frame, previous_facts)):
                self._hover_panel = (tuple(box), generation, previous_facts, frame)
                return previous_facts, frame
            settled = (previous_frame is not None and previous_roi == roi
                       and self.tooltip_stable(previous_frame, frame, roi))
            if not settled and attempt < 8:
                previous_frame, previous_roi = frame, roi
                continue
            results = self.ocr(roi, frame)
            reads += 1
            strict, _ = tooltip_facts(results, self.catalog, threshold=.90)
            parsed = strict
            if not usable(parsed):
                parsed, _ = tooltip_facts(
                    results, self.catalog, threshold=.85, color_threshold=COLOR_OCR_THRESHOLD
                )
            self.check_generation(generation)
            if usable(parsed) and not parsed.get("catalog_unknown"):
                if usable(strict) and settled and self.same_fields(previous_frame, frame, parsed):
                    self._hover_panel = (tuple(box), generation, parsed, frame)
                    return parsed, frame
                if (usable(previous_facts) and self.key(parsed) == self.key(previous_facts)
                        and parsed["level"] == previous_facts["level"]
                        and parsed["rarity"] == previous_facts["rarity"]
                        and facts_roi == roi
                        and self.same_fields(facts_frame, frame, previous_facts)
                        and self.same_fields(facts_frame, frame, parsed)):
                    self._hover_panel = (tuple(box), generation, parsed, frame)
                    return parsed, frame
            previous_facts = parsed
            previous_strict = usable(strict)
            facts_frame, facts_roi = frame, roi
            previous_frame, previous_roi = frame, roi
            if reads >= 3:
                break
        if reads == 0 and retry_hover:
            return self._read_selected_tooltip(box, generation, retry_hover=False)
        return None, frame

    def verify_selected_card(self, frame, box, facts, generation, *, slot=None, settle=True):
        """Verify a selected card via settled icon models, falling back to its hover tooltip."""
        target_slot = self._model_slot if slot is None else slot
        if settle:
            frame = self._settle_selected_card(frame, box, generation)
        selected, = self.recognize_cards(frame, [box], target_slot, purpose="selected_recheck")
        if self.same_card_facts(selected["model_facts"], facts):
            return True, frame
        tooltip, latest = self._read_selected_tooltip(box, generation)
        if latest is not None:
            frame = latest
        if tooltip is not None:
            self.report.setdefault("selected_tooltip_fallbacks", []).append({
                "slot": target_slot,
                "box": list(box),
                "expected": [facts["name"], facts["slot"], facts.get("color"), facts["level"]],
                "model": [selected["model_facts"]["name"], selected["model_facts"]["slot"],
                          selected["model_facts"].get("color"), selected["model_facts"]["level"]],
                "tooltip": [tooltip["name"], tooltip["slot"], tooltip.get("color"), tooltip["level"]],
            })
            return self.same_card_facts(tooltip, facts), frame
        return False, frame

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
        self.image(f"lock_{self.counter:05d}_unknown.jpg", frame)
        reason = ("锁按钮已点击，但未能确认结果，已停止且不会再次点击" if submitted
                  else "操作前无法区分锁定与解锁，未点击锁按钮")
        self.rt.diagnostic("blessing_lock_state", reason, image=frame,
                           details={**self.report.get("lock_recognition", {}), "frames": observations})
        if not submitted:
            raise IncompleteObservation(reason)
        raise RuntimeError(reason)

    def _selection_border(self, frame, box):
        x, y, w, h = box
        # Selected cards expand their opaque gold border 3-4 px outside the
        # nominal unselected cell box. Over cool lavender/purple backgrounds,
        # the translucent outer rim can shift slightly toward rose-gold (H 8-11)
        # while unselected unhovered cards stay near zero (< 0.01).
        x0, x1 = max(0, x - 4), min(frame.shape[1], x + w + 3)
        y0, y1 = max(0, y + 5), min(frame.shape[0], y + h - 8)
        crop = frame[y0:y1, x0:x1]
        if crop.shape[0] < 8 or crop.shape[1] < 8:
            return False
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        gold = ((hsv[:, :, 0] >= 8) & (hsv[:, :, 0] <= 42)
                & (hsv[:, :, 1] >= 25) & (hsv[:, :, 2] >= 120))
        width = gold.shape[1]
        left = max(float(gold[:, i:i + 4].mean()) for i in range(min(5, width - 3)))
        # Nominal cell width w=95 extends 2 px past the unhovered right edge;
        # after clear_hover() shrinks the selected card back from its hovered
        # outer frame, the 4 px inner gold strip sits at [x+w-6 : x+w-2] (i=5).
        right = max(float(gold[:, width - 4 - i:width - i].mean()) for i in range(min(6, width - 3)))
        return bool(left > .45 and right > .45)

    def selected(self, frame, box):
        x, y, w, h = box
        # Hovered unselected cards draw a similar outer gold border around a
        # dark translucent body. While the pointer may still hover this card,
        # require both side borders and the opaque warm-gold card background
        # inside the side margins (excluding central artwork and bottom strip).
        if not self._selection_border(frame, box):
            return False
        inner = cv2.cvtColor(frame[y + 10:y + h - 28, x:x + w], cv2.COLOR_BGR2HSV)
        if inner.shape[0] < 8 or inner.shape[1] < 24:
            return False
        fill = ((inner[:, :, 0] >= 8) & (inner[:, :, 0] <= 38)
                & (inner[:, :, 1] >= 40) & (inner[:, :, 2] >= 175))
        margins = np.concatenate([fill[:, 5:13], fill[:, w - 15:w - 7]], axis=1)
        return bool(float(margins.mean()) > .55)

    def unhovered_selected(self, frame, box):
        """Confirm selection after the pointer has been moved off the card grid.

        When the pointer is parked away from the grid, hovered-only unselected
        cards lose their gold border completely, whereas a selected card keeps
        its gold frame (and may either reveal its opaque cream-gold fill or
        retain a warm translucent glow with full top+side gold borders while no
        sibling card holds opaque selection).
        """
        if self.selected(frame, box):
            return True
        if not self._selection_border(frame, box):
            return False
        x, y, w, h = box
        ty0, ty1 = max(0, y - 4), min(frame.shape[0], y + 4)
        tx0, tx1 = max(0, x + 5), min(frame.shape[1], x + w - 5)
        top_crop = frame[ty0:ty1, tx0:tx1]
        if top_crop.shape[0] < 4 or top_crop.shape[1] < 16:
            return False
        top_hsv = cv2.cvtColor(top_crop, cv2.COLOR_BGR2HSV)
        top_gold = ((top_hsv[:, :, 0] >= 8) & (top_hsv[:, :, 0] <= 42)
                    & (top_hsv[:, :, 1] >= 25) & (top_hsv[:, :, 2] >= 120))
        top_score = max(float(top_gold[i:i + 4, :].mean()) for i in range(top_gold.shape[0] - 3))
        if top_score <= .45:
            return False
        inner_bgr = frame[y + 10:y + h - 28, x:x + w]
        if inner_bgr.shape[0] < 8 or inner_bgr.shape[1] < 24:
            return False
        margins_bgr = np.concatenate([inner_bgr[:, 5:13], inner_bgr[:, w - 15:w - 7]], axis=1).astype(np.float32)
        margins_hsv = cv2.cvtColor(
            np.concatenate([inner_bgr[:, 5:13], inner_bgr[:, w - 15:w - 7]], axis=1),
            cv2.COLOR_BGR2HSV,
        )
        if float((margins_bgr[:, :, 2] - margins_bgr[:, :, 0]).mean()) < 2.0 or float(margins_hsv[:, :, 2].mean()) < 90.0:
            return False
        # Reject if any sibling card in the visible grid holds opaque selection.
        first_x = self.layout["first_x"]
        col_pitch = self.layout["column_pitch"]
        row_pitch = self.layout["row_pitch"]
        grid_top = self.grid[1]
        grid_bottom = self.grid[1] + self.grid[3]
        row_y = y
        while row_y - row_pitch >= grid_top - 6:
            row_y -= row_pitch
        while row_y + h <= grid_bottom + 6:
            if row_y >= 5 and row_y + h <= frame.shape[0] - 5:
                for col in range(self.layout["columns"]):
                    sib = [round(first_x + col * col_pitch), row_y, w, h]
                    if abs(sib[0] - x) < 10 and abs(sib[1] - y) < 10:
                        continue
                    if self.selected(frame, sib):
                        return False
            row_y += row_pitch
        return True

    def select(self, box, facts, generation, *, verify_details=True, already_selected=False):
        self.check_generation(generation)
        # Stage the pointer first so Seize/Slate hit-testing settles on the
        # card before clicking; otherwise LEFTDOWN can land on the previous
        # clear_hover() position and leave the card merely hovered.
        x, y, w, h = box
        point = [round(x + w / 2), round(y + h * .45)]
        if not already_selected:
            if self._tall_mode:
                hwnd, cap_size = self.rt._pointer_guard_context()
                if not is_capture_point_safe(
                    hwnd,
                    point,
                    capture_size=cap_size,
                    margin_px=6,
                    require_work_area=True,
                ):
                    raise IncompleteObservation("超长竖屏下目标闪光位于安全可点击区外")
            if self._hover_box != tuple(box):
                self._hover_box = None
                if not self.rt.action("TouchMove", target=point) or not self.rt.pause(.15):
                    raise RuntimeError("未能定位目标闪光")
                self._hover_box = tuple(box)
            if not self.rt.action("Click", target=point, ui_click=False) or not self.rt.pause(.15):
                raise RuntimeError("未能选中目标闪光")
        frame = self.frame(generation)
        if not self.selected(frame, box):
            # Click delivery and the selection animation are asynchronous, and
            # keeping the pointer on the clicked card can occasionally mask the
            # opaque selected fill under the translucent hover style. Wait
            # briefly with the cursor in place, then clear hover to distinguish
            # an active selection from a swallowed click before retrying once.
            confirmed = False
            with self.rt.ui.preserve_cursor():
                wait_seconds = 5 if already_selected else 1.2
                confirmed = self.rt.ui.wait_page(
                    lambda: self.selected(self.frame(generation), box),
                    seconds=wait_seconds,
                )
            if not confirmed:
                frame = self.clear_hover(generation, settle_card=True)
                confirmed = self.unhovered_selected(frame, box)
            if not confirmed and not already_selected:
                self.check_generation(generation)
                self._hover_box = None
                if (not self.rt.action("TouchMove", target=point)
                        or not self.rt.pause(.3)
                        or not self.rt.action("Click", target=point, ui_click=False)
                        or not self.rt.pause(.2)):
                    raise RuntimeError("未能选中目标闪光")
                self._hover_box = tuple(box)
                with self.rt.ui.preserve_cursor():
                    confirmed = self.rt.ui.wait_page(
                        lambda: self.selected(self.frame(generation), box),
                        seconds=2.5,
                    )
                if not confirmed:
                    frame = self.clear_hover(generation, settle_card=True)
                    confirmed = self.unhovered_selected(frame, box)
            if not confirmed:
                raise IncompleteObservation("目标格子的选中高亮尚未确认，保留进度")
            if self._hover_box is not None:
                frame = self.frame(generation)
        if verify_details:
            matched, frame = self.verify_selected_card(frame, box, facts, generation)
            if not matched:
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
                generation = self.current_generation()
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

    def _get_acquired_desc_template(self):
        template = getattr(self, "_acquired_desc_template", None)
        if template is not None:
            return template
        reference = ROOT / "resource/image/blessing_lock/acquired_desc.png"
        template = cv2.imdecode(np.fromfile(str(reference), np.uint8), cv2.IMREAD_GRAYSCALE)
        if template is None:
            raise RuntimeError("获得时间排序方向资源缺失")
        self._acquired_desc_template = template
        return template

    def acquisition_sort(self):
        """Select acquisition order and confirm the user-supplied down arrow."""
        if self._tall_mode:
            if self._sort_verified:
                return
            raise IncompleteObservation("超长竖屏模式下排序选项位于安全可点击区外")
        dy = self.layout["sort_label"][1] - self._base_layout["sort_label"][1]
        generation = self.current_generation()
        frame = self.clear_hover(generation)
        if not self.rt.text(self.layout["sort_label"], "获得时间", image=frame):
            # Capacity text sits beside the final option and can merge into its OCR line.
            if (not self.rt.action("Click", target=self.layout["sort_label"])
                    or not self.rt.pause(.2)
                    or not self.rt.click_text("^获得时间$", [1055, 550 + dy, 84, 110])
                    or not self.rt.pause(.3)):
                raise RuntimeError("未能切换获得时间排序")
        template = self._get_acquired_desc_template()
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
                if self.rt.action("Click", target=[1237, 684 + dy]) and self.rt.pause(.3):
                    continue
            self.image("sort_direction_unknown.jpg", frame)
            raise RuntimeError("未确认获得时间从新到旧的排序箭头，请设为默认降序后重试")

    def unchanged(self, before, after):
        grid = self._base_layout["grid"] if self._tall_mode else self.grid
        if self.same_structure(before, after, grid, threshold=.997):
            return True
        boxes = self.cards(before, [])
        if not boxes:
            return False
        # Cards themselves are opaque; exclude transparent gaps, empty space
        # below the list, the selected card's border, and tall-mode scroll target.
        if self._tall_mode:
            sample = [
                b for b in (boxes[1:16] if len(boxes) > 1 else boxes[:1])
                if not (b[0] <= 1246 <= b[0] + b[2] and b[1] <= 520 <= b[1] + b[3])
            ] or boxes[:1]
        else:
            sample = [b for b in boxes if not self.selected(before, b)] or boxes
        return all(
            self.same_structure(before, after, [x + 4, y + 4, w - 8, h - 8], threshold=.99)
            for x, y, w, h in sample
        )

    def top(self):
        generation = self.current_generation()
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
        if self._tall_mode:
            hwnd, cap_size = self.rt._pointer_guard_context()
            if not is_capture_point_safe(
                hwnd,
                self.layout["lock_button"],
                capture_size=cap_size,
                margin_px=6,
                require_work_area=True,
            ):
                raise IncompleteObservation("超长竖屏下锁按钮位于安全可点击区外")
        # No stale coordinate is reused for the toggle. Stage the mouse first,
        # then recapture the page, selection and source state before one input.
        if not self.rt.action("TouchMove", target=self.layout["lock_button"]) or not self.rt.pause(.3):
            raise RuntimeError(f"未能准备{verb}操作")
        self.rt.check_focus()
        click_generation = self.rt._focus_generation
        current = self.frame(generation)
        if not self.unhovered_selected(current, box):
            raise RuntimeError(f"{verb}前目标选中状态丢失")
        if self.lock_state(generation, frame=current) != source:
            raise RuntimeError(f"{verb}前状态改变，未再次点击")
        self._hover_box = None
        self._hover_panel = None
        self.check_generation(generation)
        if self.rt._focus_generation != click_generation:
            raise ForegroundChanged("切窗后已重新核对目标，取消本次锁定点击")
        operation = {"key": list(self.key(facts)), "level": facts["level"],
                     "kind": "lock" if target == "locked" else "unlock", "reason": reason,
                     "expected_state": target,
                     "status": "submitted_unconfirmed", "evidence": evidence,
                     "input_status": "prepared",
                     "before": self.image(f"before_{len(self.report['operations']):03d}.jpg", current)}
        self.pending = operation
        self.report["operations"].append(operation)
        self.save()
        # The lock toggle is submitted once even if delivery/verification fails.
        if not self.rt.action("Click", target=self.layout["lock_button"], ui_click=False,
                              focus_generation=click_generation):
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
                    "image": self.image(f"after_{len(self.report['operations']) - 1:03d}.jpg",
                                        self._lock_frame),
                    "lock_recognition": dict(self.report["lock_recognition"])}
        self.pending.update(status="confirmed", confirmation=evidence)
        self.save()
        self.pending = None

    def same_ordered_cards(self, checkpoint, frame, *, shift=0, ignore_box=None):
        before = checkpoint["frame"]
        fade = self.layout.get("bottom_fade_margin", 32)
        checked = []
        for box in checkpoint["boxes"]:
            if ignore_box is not None and list(box) == list(ignore_box):
                continue
            x, y, w, h = box
            target_y = y - shift
            if not self.grid[1] <= target_y <= self.grid[1] + self.grid[3] - h - fade:
                continue
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

    def _scrollbar_roi(self):
        return [1256, self.grid[1], 14, self.grid[3] - 10]

    def _same_scrollbar_thumb(self, first, second):
        if first is None or second is None or first.shape != second.shape:
            return False
        roi = self._scrollbar_roi()
        c1, c2 = self.region(first, roi), self.region(second, roi)
        if not c1.size or c1.shape != c2.shape:
            return False
        # Opaque white thumb has luma ~248-254 while translucent track over 3D scene stays ~40-65.
        b1 = np.flatnonzero(cv2.cvtColor(c1, cv2.COLOR_BGR2GRAY).max(axis=1) >= 180)
        b2 = np.flatnonzero(cv2.cvtColor(c2, cv2.COLOR_BGR2GRAY).max(axis=1) >= 180)
        if len(b1) == 0 and len(b2) == 0:
            return True
        if (len(b1) == 0) != (len(b2) == 0):
            return False
        return abs(int(b1[0]) - int(b2[0])) <= 3 and abs(int(b1[-1]) - int(b2[-1])) <= 3

    def same_ordered_view(self, checkpoint, frame, *, ignore_box=None):
        """Check every clear card plus the scroll thumb, ignoring translucent track animation."""
        return (self.same_ordered_cards(checkpoint, frame, ignore_box=ignore_box)
                and self._same_scrollbar_thumb(checkpoint["frame"], frame))

    def remember_ordered_selection(self, view):
        checkpoint = view._focus_checkpoint
        if checkpoint is not None:
            frame = self.clear_hover(view.generation, settle_card=True)
            view.remember_view(frame, checkpoint["boxes"])

    def _has_live_pending(self):
        return self.pending is not None and getattr(self, "_live_pending", True)

    def _abort_or_rescan_focus(self, message):
        if self._has_live_pending():
            raise RuntimeError(message)
        raise RescanSlot(message)

    def resumed_scene(self, view, *, slot=None, require_checkpoint=True, check_initial_capacity=False):
        checkpoint = view._focus_checkpoint
        target_slot = view.slot if slot is None else slot
        if require_checkpoint and (checkpoint is None or checkpoint["slot"] != target_slot):
            self._abort_or_rescan_focus("切回后缺少当前页参照")
        self.rt.check_focus()
        generation = self.current_generation()
        focus_generation = self.rt._focus_generation
        if not self.rt.action("TouchMove", target=self._park_point()) or not self.rt.pause(.2):
            if self.rt.stopped:
                raise TaskStopped("用户停止祝福闪光读取")
            if self.current_generation() != generation:
                raise RescanSlot("恢复期间焦点再次变化")
            raise RuntimeError("前台恢复被中断")
        if self.current_generation() != generation:
            raise RescanSlot("恢复期间焦点再次变化")
        first = (
            self.rt.capture_parked(self._park_point())
            if self.rt._seamless_background() and hasattr(self.rt, "capture_parked")
            else self.rt.capture()
        )
        frame = first
        if not (require_checkpoint and first is not None
                and first.shape[:2] in ((720, 1280), (5120, 1280))
                and self.same_ordered_view(checkpoint, first)):
            for _ in range(3):
                if not self.rt.pause(.12):
                    if self.rt.stopped:
                        raise TaskStopped("用户停止祝福闪光读取")
                    if self.current_generation() != generation:
                        raise RescanSlot("恢复期间焦点再次变化")
                    raise RuntimeError("前台恢复被中断")
                frame = self.rt.capture()
                if frame is not None and first is not None and frame.shape == first.shape:
                    grid_roi = self._base_layout["grid"] if self._tall_mode else self.grid
                    if self.same_structure(first, frame, grid_roi, threshold=.985):
                        break
                first = frame
        if self.current_generation() != generation:
            raise RescanSlot("恢复核对期间焦点再次变化")
        if (frame is None or frame.shape[:2] not in ((720, 1280), (5120, 1280))
                or not (self.rt.text([930, 15, 330, 52], target_slot, image=frame)
                        if require_checkpoint else self.ready(frame))
                or (require_checkpoint and (
                    not self.rt.text(self.layout["sort_label"], "获得时间", image=frame)
                    or self.sort_direction(frame, self._get_acquired_desc_template()) != "desc"))):
            if self.current_generation() != generation or self.rt._focus_generation != focus_generation:
                raise RescanSlot("恢复核对期间焦点再次变化")
            self._abort_or_rescan_focus("切回后列表页面、部位或排序变化")
        if check_initial_capacity and getattr(self, "initial_capacity", None) is not None:
            if self.capacity_values(frame) != [self.initial_capacity]:
                if self.current_generation() != generation or self.rt._focus_generation != focus_generation:
                    raise RescanSlot("恢复核对期间焦点再次变化")
                self._abort_or_rescan_focus("切回后仓库容量发生变化")
        if self.current_generation() != generation:
            raise RescanSlot("恢复核对期间焦点再次变化")
        return checkpoint, frame, generation

    def accept_ordered_focus(self, view, frame, generation, phase, *, delta=0):
        self.check_generation(generation)
        view.generation = generation
        if hasattr(self, "organization_generation"):
            self.organization_generation = generation
        self._hover_box = None
        self._hover_panel = None
        self._page_frame = self._ready_frame = frame
        self._ready_generation = self.rt._focus_generation
        self.verified.clear()
        view._page_frame, view._page_cells = None, []
        self.report.setdefault("focus_recoveries", []).append({
            "time": datetime.now().isoformat(timespec="seconds"), "slot": view.slot,
            "phase": phase, "position": view.position, "delta": delta, "generation": generation})
        self.rt.log("窗口焦点恢复后已核对当前列表，继续处理。", detail=True)

    def recover_ordered_focus(self, view, phase, args):
        if view._recovering_focus:
            raise RescanSlot("局部核对期间再次发生焦点切换")
        view._recovering_focus = True
        try:
            return self._recover_ordered_focus(view, phase, args)
        finally:
            view._recovering_focus = False

    def _recover_ordered_focus(self, view, phase, args):
        check_cap = bool(
            getattr(self, "baseline_store", None) is not None
            and getattr(self, "initial_capacity", None) is not None
        )
        if phase == "enter":
            target_slot = args[0] if args else view.slot
            _, frame, generation = self.resumed_scene(
                view, slot=target_slot, require_checkpoint=False, check_initial_capacity=check_cap)
            view.slot, view.position = None, 0
            view._focus_checkpoint = None
            view._slot_header = None
            self.accept_ordered_focus(view, frame, generation, phase, delta=0)
            return 0
        checkpoint, frame, generation = self.resumed_scene(view, check_initial_capacity=check_cap)
        delta = 0
        if not self.same_ordered_view(checkpoint, frame):
            if phase != "step":
                self.report["focus_mismatch"] = {
                    "phase": phase, "slot": view.slot, "position": view.position,
                    "before": self.image(f"focus_{generation}_before.jpg", checkpoint["frame"]),
                    "after": self.image(f"focus_{generation}_after.jpg", frame),
                    "cards_match": self.same_ordered_cards(checkpoint, frame),
                    "scrollbar_match": self._same_scrollbar_thumb(checkpoint["frame"], frame)}
                self._abort_or_rescan_focus("切回后当前页条目或滚动位置改变，重新核对该部位")
            direction = args[0]
            delta = (self.scroll_position(checkpoint["frame"], frame) if direction > 0
                     else self.scroll_position(frame, checkpoint["frame"]))
            if (delta is None or delta <= 2
                    or not self.same_ordered_cards(checkpoint, frame, shift=direction * delta)):
                self._abort_or_rescan_focus("切回后翻页位移未能确认，重新核对该部位")
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
        if phase not in ("step", "cells"):
            view.cells()
            if phase == "select_target":
                args[3]["selected_for_observation"] = False
        return delta
