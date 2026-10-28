"""Lock one text-verified maximum-level Eureka per set, slot and colour."""
from __future__ import annotations

from datetime import datetime
import re

import cv2
import numpy as np

from task_control import TaskStopped
from daily.runtime import parameters
from daily.settings import ROOT
from .blessing import SLOTS
from .details import COLOR_OCR_THRESHOLD, tooltip_facts
from file_io import write_json
from .policy import capabilities
from .quantity import stack_badge
from .scanner import InventoryScanner


READY = "MaaNikki_Blessing_InventoryReady"


class BlessingLocker(InventoryScanner):
    """Shared normal-inventory details, selection and confirmed lock toggles.

    Acquisition-order traversal lives in ordered.py; organization and upgrades
    use the same verified lock button without re-finding a split selected item.
    """

    def __init__(self, runtime, catalog, folder, *, allow_unlock=False):
        super().__init__(runtime, catalog, folder)
        if type(allow_unlock) is not bool:
            raise ValueError("解锁不符合要求的闪光选项必须为布尔值")
        self.allow_unlock = allow_unlock
        self.layout = parameters(runtime.context, "MaaNikki_BlessingLock_Layout")
        self.grid = self.layout["grid"]
        self._ordered_scroll_step = self.layout["scroll_step"]
        self.lock_templates = {}
        self.lock_gray_templates = {}
        for state in ("locked", "unlocked"):
            reference = ROOT / f"resource/image/blessing_lock/{state}.png"
            image = cv2.imdecode(np.fromfile(str(reference), np.uint8), cv2.IMREAD_COLOR)
            if image is None or image.shape[:2] != tuple(reversed(self.layout["lock_icon"][2:])):
                raise ValueError(f"锁按钮图像资源缺失或尺寸不符：{state}")
            self.lock_templates[state] = self.white_mask(image) > 0
            self.lock_gray_templates[state] = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
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
                       "complete": False, "slots": {}, "operations": [], "reason": "尚未完成"}
        self.pending = None
        self.inventory_read = False
        self.inventory_snapshot = None
        self.lock_groups = {}
        self._hover_read = None
        self._hover_box = None
        self.stack_count_cache = {}
        badge_path = ROOT / "resource/image/blessing_lock/card_locked.png"
        badge = cv2.imdecode(np.fromfile(str(badge_path), np.uint8), cv2.IMREAD_COLOR)
        if badge is None:
            raise ValueError("列表锁标资源缺失")
        self.card_lock_template = self.white_mask(badge) > 0
        self.card_lock_gray = cv2.cvtColor(badge, cv2.COLOR_BGR2GRAY)
        stars_path = ROOT / "resource/image/blessing_lock/three_stars.png"
        stars = cv2.imdecode(np.fromfile(str(stars_path), np.uint8), cv2.IMREAD_GRAYSCALE)
        if stars is None:
            raise ValueError("三星提示框图像资源缺失")
        self.three_stars = stars
        self.rarity_templates = {3: stars}
        for rarity, filename in ((4, "four_stars.png"), (5, "five_stars.png")):
            path = ROOT / "resource/image/blessing_lock" / filename
            template = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_GRAYSCALE)
            if template is None:
                raise ValueError(f"{rarity}星提示框图像资源缺失")
            self.rarity_templates[rarity] = template

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
            "card": self.image(prefix + ".png", self.region(frame, box)),
            "badge": self.image(prefix + "_badge.png", self.region(frame, badge_roi)),
            "lock": self.image(prefix + "_lock.png", self.region(frame, lock_roi)),
        }
        if error is not None:
            diagnostic.update(error=error, page=self.image(prefix + "_page.png", frame),
                              quantity_failure=self.report.get("quantity_failure"))
            self.rt.diagnostic("blessing_quantity", error, image=frame, details=diagnostic)
        write_json(self.folder / (prefix + ".json"), diagnostic)

    def frame(self, generation):
        frame = self.rt.capture()
        self.check_generation(generation)
        if frame is None or frame.shape[:2] != (720, 1280) or not self.ready(frame):
            raise RuntimeError("未确认祝福闪光普通库存页（需要 16:9 窗口）")
        return frame

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
        return self.rt.ui.find_menu_entry(node="MaaNikki_Blessing_Entrance")

    def detail_frame(self, generation):
        return self.frame(generation)

    def clear_hover(self, generation, *, settle_card=False):
        old_box = self._hover_box
        self._hover_read = None
        self._hover_box = None
        if not self.rt.action("TouchMove", target=[900, 32]) or not self.rt.pause(.06):
            self.check_generation(generation)
            raise RuntimeError("未能关闭悬停信息")
        previous = None
        for _ in range(8 if settle_card else 6):
            frame = self.frame(generation)
            if old_box is None or self.header_region(frame, old_box) is None:
                if (not settle_card or old_box is None or previous is not None
                        and self.same_structure(previous, frame, old_box, threshold=.997)):
                    return frame
                # The tooltip can disappear before the hovered card's glow
                # fades. That glow defeated the third-column scroll matches.
                previous = frame
            else:
                previous = None
            if not self.rt.pause(.04):
                self.check_generation(generation)
        raise RuntimeError("悬停提示或格子高亮尚未稳定，未继续读取或翻页")

    def scroll_amounts(self):
        # Keep roughly two rows of overlap. Once a large move is ambiguous,
        # retain the smaller setting instead of repeating the same rollback.
        step = self._ordered_scroll_step
        smaller = max(120, step // 240 * 120)
        return tuple(dict.fromkeys((step, smaller, 120)))

    def record_scroll(self, before, after, amount, delta, phase):
        super().record_scroll(before, after, amount, delta, phase)
        if delta is None and phase in ("down", "ordered_up") and abs(amount) > 120:
            self._ordered_scroll_step = min(self._ordered_scroll_step, max(120, abs(amount) // 240 * 120))
            self.report["scroll_step"] = self._ordered_scroll_step

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

    def read_details(self, box, generation, index, *, retry_hover=True):
        self.check_generation(generation)
        old = self._hover_read
        # The next card to the right cannot be covered by the current tooltip.
        # Keep the existing clear-page reference and switch directly. Row wraps,
        # cached/unknown hovers and mutation rereads retain the clear-hover path.
        direct = (old is not None and self._hover_box == old[0] and old[1] == generation
                  and abs(box[1] - old[0][1]) < 12
                  and abs(box[0] - old[0][0] - self.layout["column_pitch"]) < 8
                  and self._page_frame is not None)
        baseline = self._page_frame if direct else self.clear_hover(generation)
        stats = self.report.setdefault("hover_navigation", {"direct": 0, "cleared": 0})
        stats["direct" if direct else "cleared"] += 1
        self._hover_read = None
        x, y, w, h = box
        if not self.rt.action("TouchMove", target=[round(x + w / 2), round(y + h * .45)]):
            raise RuntimeError("未能悬停祝福闪光")
        self._hover_box = tuple(box)
        self._page_frame = baseline
        previous_frame, previous_roi = None, None
        previous_facts, previous_lines, previous_strict = None, [], False
        facts_frame, facts_roi = None, None
        reads, captures, last_lines = 0, 0, []
        frame, roi = baseline, None

        def usable(facts):
            return facts is not None and (facts.get("color") is not None
                or not capabilities(self.catalog, facts["name"], observed=facts)["lock_mutable"]
                or not self.is_maximum(facts) and (self.inventory_read or self.allow_unlock))

        def read_facts(results, threshold, *, color_threshold=None):
            facts, lines = tooltip_facts(results, self.catalog, threshold=threshold,
                                         color_threshold=color_threshold)
            if facts is not None:
                return facts, lines
            # Catalogue updates can lag game drops. Recognize the complete
            # rarity pill before accepting a new title; never guess three stars.
            facts, lines = tooltip_facts(results, self.catalog, threshold=threshold,
                                         color_threshold=color_threshold,
                                         allow_unknown=True)
            if facts is None or not facts.get("catalog_unknown"):
                return None, lines
            left, top = facts["header_box"]
            area = [max(0, left - 12), top + 30, 130, 38]
            gray = cv2.cvtColor(self.region(frame, area), cv2.COLOR_BGR2GRAY)
            scores = {rarity: float(cv2.matchTemplate(gray, template, cv2.TM_CCOEFF_NORMED).max())
                      for rarity, template in self.rarity_templates.items()}
            if not all(np.isfinite(score) for score in scores.values()):
                return None, lines
            rarity, other = sorted(scores, key=scores.get, reverse=True)[:2]
            if scores[rarity] < .94 or scores[rarity] - scores[other] < .04:
                return None, lines
            facts, lines = tooltip_facts(results, self.catalog, threshold=threshold,
                                         color_threshold=color_threshold,
                                         allow_unknown=True, unknown_rarity=rarity)
            if facts is None:
                return None, lines
            facts["field_boxes"].append(area)
            facts["inventory_evidence"] = {"basis": "tooltip_rarity", "rarity": rarity,
                                            "rarity_score": scores[rarity], "scores": scores}
            return facts, lines

        def finish(facts, lines, confirmation):
            self._hover_read = (tuple(box), generation, facts, frame)
            evidence = {"ocr": lines, "reads": reads, "captures": captures,
                        "roi": roi, "confirmation": confirmation,
                        "image": self.image(f"detail_{index:05d}.png",
                                            self.region(frame, self.detail_roi(facts)))}
            if facts.get("protected_inventory") or facts.get("catalog_unknown"):
                evidence["inventory_evidence"] = facts["inventory_evidence"]
            self.report["last_detail"] = {"name": facts["name"], "level": facts["level"],
                                          "reads": reads, "captures": captures, "roi": roi}
            return facts, evidence

        # Wait for pixels, not eight repeated OCR calls over half the screen.
        for attempt in range(18):
            if not self.rt.pause(.12 if attempt == 0 and not direct else .04):
                raise RuntimeError("悬停核对被中断")
            frame = self.frame(generation)
            captures += 1
            # Never accept the previous card's still-visible tooltip as the new
            # item. Pixel stability is considered only after its fields change.
            if direct and self.same_fields(old[3], frame, old[2]):
                previous_frame, previous_roi = None, None
                previous_facts, previous_strict = None, False
                facts_frame, facts_roi = None, None
                continue
            roi = self.header_region(frame, box)
            if roi is None or not self.tooltip_visible(frame, roi):
                previous_frame, previous_roi = None, None
                previous_facts, previous_strict = None, False
                facts_frame, facts_roi = None, None
                continue
            if (usable(previous_facts) and not previous_facts.get("catalog_unknown")
                    and previous_strict and facts_roi == roi
                    and self.same_fields(facts_frame, frame, previous_facts)):
                return finish(previous_facts, previous_lines, "field_pixels")
            settled = (previous_frame is not None and previous_roi == roi
                       and self.tooltip_stable(previous_frame, frame, roi))
            if not settled and attempt < 8:
                previous_frame, previous_roi = frame, roi
                continue
            results = self.ocr(roi, frame)
            reads += 1
            strict, lines = read_facts(results, .90)
            facts = strict
            if not usable(facts):
                facts, lines = read_facts(results, .85, color_threshold=COLOR_OCR_THRESHOLD)
            last_lines = lines
            self.check_generation(generation)
            if usable(facts):
                if (usable(strict) and not facts.get("catalog_unknown") and settled
                        and self.same_fields(previous_frame, frame, facts)):
                    return finish(facts, lines, "stable_before_ocr")
                # Marginal confidence requires two local OCR observations with
                # identical identity/level/colour and stable field pixels.
                if (usable(previous_facts) and self.key(facts) == self.key(previous_facts)
                        and facts["level"] == previous_facts["level"]
                        and facts["rarity"] == previous_facts["rarity"]
                        and facts_roi == roi
                        and self.same_fields(facts_frame, frame, previous_facts)
                        and self.same_fields(facts_frame, frame, facts)):
                    return finish(facts, lines, "two_local_reads")
            previous_facts, previous_lines = facts, lines
            previous_strict = usable(strict)
            facts_frame, facts_roi = frame, roi
            previous_frame, previous_roi = frame, roi
            if reads >= 3:
                break
        diagnostic = {"box": list(box), "roi": roi, "reads": reads,
                      "captures": captures, "ocr": last_lines, "direct": direct,
                      "recognized_fields": {key: previous_facts.get(key) for key in
                                            ("name", "slot", "level", "color")} if previous_facts else {}}
        if reads == 0 and retry_hover:
            # Selection can dismiss a tooltip or delay the next hover. Re-enter
            # once from a confirmed clear page; never click or relax identity.
            self.report.setdefault("hover_recoveries", []).append(diagnostic)
            self.image(f"detail_{index:05d}_hover_retry.png", frame)
            self._hover_read = None
            self.rt.log("祝福闪光详情未显示，正在重新悬停核对。", detail=True)
            return self.read_details(box, generation, index, retry_hover=False)
        self.report["detail_failure"] = diagnostic
        write_json(self.folder / f"detail_{index:05d}_unknown.json", diagnostic)
        self.image(f"detail_{index:05d}_unknown.png", frame)
        raise RuntimeError("重新悬停后详情仍未显示或更新，已保存截图" if reads == 0
                           else "提示框局部名称、等级或颜色未能确认，已保存识别内容")

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
        difference = np.abs(lab - self.lock_background_lab.astype(np.int16))
        # The locked fill is opaque. Compare its colour in four separated
        # sectors so a gold garment behind an open lock is not enough.
        similar = np.all(difference <= np.array([25, 8, 8]), axis=2)
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
        state = None
        if fraction >= .88 and min(sectors) >= .75:
            state = "locked"
        elif fraction <= .20 and max(sectors) <= .35:
            state = "unlocked"
        return {"state": state, "gold_fraction": fraction, "sector_fractions": sectors,
                "lab_tolerance": [25, 8, 8], "gold_minimum": .88,
                "gold_sector_minimum": .75, "transparent_maximum": .20,
                "transparent_sector_maximum": .35}

    def icon_state(self, frame):
        """Compare both user-labelled states; never infer state from geometry."""
        crop = self.region(frame, self.layout["lock_icon"])
        raw = self.white_mask(crop)
        if any(raw.shape != template.shape for template in self.lock_templates.values()):
            return None
        # Allow capture scaling to shift the entire glyph by up to two pixels.
        # Both states compete over the COMPLETE glyph, including the chain in
        # the confirmed locked screenshot. The common lock body is insufficient.
        scores = {state: 0.0 for state in self.lock_templates}
        shackle_scores = {state: 0.0 for state in self.lock_templates}
        height, width = raw.shape
        shackle_height = height // 2
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                aligned = cv2.warpAffine(raw, np.float32([[1, 0, dx], [0, 1, dy]]),
                                         (width, height), flags=cv2.INTER_NEAREST) > 0
                for state, template in self.lock_templates.items():
                    union = np.count_nonzero(aligned | template)
                    score = np.count_nonzero(aligned & template) / union if union else 0
                    scores[state] = max(scores[state], float(score))
                    shackle = aligned[:shackle_height]
                    reference = template[:shackle_height]
                    union = np.count_nonzero(shackle | reference)
                    score = np.count_nonzero(shackle & reference) / union if union else 0
                    shackle_scores[state] = max(shackle_scores[state], float(score))
        ranked = sorted(scores, key=scores.get, reverse=True)
        best, other = ranked
        # Preserve anti-aliased shackle/chain pixels that a binary white mask
        # loses against the brighter locked-button background. Both complete
        # state templates still compete; the common lock body is insufficient.
        x, y, w, h = self.layout["lock_icon"]
        gray = cv2.cvtColor(self.region(frame, [x - 2, y - 2, w + 4, h + 4]), cv2.COLOR_BGR2GRAY)
        gray_scores = {state: float(cv2.matchTemplate(gray, template, cv2.TM_CCOEFF_NORMED).max())
                       for state, template in self.lock_gray_templates.items()}
        gray_best, gray_other = sorted(gray_scores, key=gray_scores.get, reverse=True)
        shackle_best, shackle_other = sorted(shackle_scores, key=shackle_scores.get, reverse=True)
        background = self.button_background(frame)
        self.report["lock_recognition"] = {"scores": scores, "best": best,
                                           "minimum_score": .90, "minimum_margin": .06,
                                           "gray_scores": gray_scores, "gray_best": gray_best,
                                           "gray_minimum_score": .94, "gray_minimum_margin": .06,
                                           "shackle_scores": shackle_scores,
                                           "shackle_best": shackle_best,
                                           "shackle_height": shackle_height,
                                           "background": background,
                                           "shackle_minimum_score": .90,
                                           "shackle_minimum_margin": .06}
        decisions = set()
        if scores[best] >= .90 and scores[best] - scores[other] >= .06:
            decisions.add(best)
        if gray_scores[gray_best] >= .94 and gray_scores[gray_best] - gray_scores[gray_other] >= .06:
            decisions.add(gray_best)
        # Both states share most of the lock body. A strong full-glyph match
        # may therefore miss the margin even when the open shackle is clear.
        # Require both the full glyph and a separate discriminating match;
        # conflicting evidence still leaves the state unknown.
        full_match = ((shackle_best == best and scores[best] >= .90)
                      or (shackle_best == gray_best and gray_scores[gray_best] >= .94))
        if (full_match and shackle_scores[shackle_best] >= .90
                and shackle_scores[shackle_best] - shackle_scores[shackle_other] >= .06):
            decisions.add(shackle_best)
        # A transparent button can show the player's outfit through the lower
        # lock body. Keep the shackle threshold and replace only that polluted
        # full-glyph requirement with independent button-fill evidence.
        background_match = (background["state"] == shackle_best
                            and shackle_scores[shackle_best] >= .90
                            and shackle_scores[shackle_best] - shackle_scores[shackle_other] >= .06)
        if background_match:
            decisions.add(shackle_best)
        state = next(iter(decisions)) if len(decisions) == 1 else None
        if background["state"] is not None and state != background["state"]:
            state = None
        self.report["lock_recognition"]["shackle_background_match"] = background_match
        self.report["lock_recognition"]["state"] = state
        return state

    def card_lock_state(self, frame, box, labels):
        """Positive evidence only: lock glyph, or a stack's white count tab."""
        x, y, w, h = box
        crop = self.region(frame, [x + w - 30, y + h - 35, 30, 32])
        mask = self.white_mask(crop) > 0
        template = self.card_lock_template
        th, tw = template.shape
        best = 0.0
        for dy in range(mask.shape[0] - th + 1):
            for dx in range(mask.shape[1] - tw + 1):
                candidate = mask[dy:dy + th, dx:dx + tw]
                union = np.count_nonzero(candidate | template)
                best = max(best, np.count_nonzero(candidate & template) / union if union else 0)
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        gray_score = float(cv2.matchTemplate(gray, self.card_lock_gray, cv2.TM_CCOEFF_NORMED).max())
        # Tiny count digits are often absent from page OCR (22:23 run).
        # Their broad white backing and dark strokes are still visible. A
        # locked card has only a small lock glyph, never this count tab.
        count_tab = stack_badge(frame, box)["state"] == "stack"
        counts = []
        for item in labels:
            bx, by, bw, bh = list(item.box)
            text = getattr(item, "text", "").strip()
            if (float(getattr(item, "score", 0)) >= .90 and re.fullmatch(r"[0-9]{1,4}", text)
                    and x + w - 32 <= bx and bx + bw <= x + w + 2
                    and y + h - 30 <= by and by + bh <= y + h + 2 and int(text) >= 2):
                counts.append(int(text))
        state, source = None, "ambiguous"
        if (best >= .90 or gray_score >= .94) and not counts and not count_tab:
            state, source = "locked", "lock_glyph"
        elif (len(counts) == 1 or count_tab) and best < .70 and gray_score < .70:
            state, source = "unlocked", "stack_label" if counts else "stack_tab"
        stats = self.report.setdefault("card_state_sources", {})
        stats[source] = stats.get(source, 0) + 1
        self.report["last_card_lock"] = {"box": list(box), "glyph_score": best,
                                          "gray_score": gray_score, "count_tab": count_tab,
                                          "counts": counts, "state": state, "source": source}
        return state

    def lock_state(self, generation, *, frame=None):
        if frame is None:
            frame = self.clear_hover(generation)
        submitted = (self.pending is not None and self.pending.get("kind") in ("lock", "unlock")
                     and self.pending.get("status") == "submitted_unconfirmed")
        previous = self.icon_state(frame)
        for attempt in range(12):
            if attempt == 2 and previous is None and submitted:
                # Reset hover only after a submitted toggle. Before a toggle,
                # preserve the pointer position prepared for its single click.
                frame = self.clear_hover(generation)
                previous = self.icon_state(frame)
            if not self.rt.pause(.06):
                raise RuntimeError("锁定状态核对被中断")
            frame = self.frame(generation)
            current = self.icon_state(frame)
            if current is not None and current == previous:
                self._lock_frame = frame
                return current
            previous = current
        self.counter += 1
        self.image(f"lock_{self.counter:05d}_unknown.png", frame)
        reason = ("锁按钮已点击，但未能确认结果，已停止且不会再次点击" if submitted
                  else "操作前无法区分锁定与解锁，未点击锁按钮")
        self.rt.diagnostic("blessing_lock_state", reason, image=frame,
                           details=self.report.get("lock_recognition", {}))
        raise RuntimeError(reason)

    def selected(self, frame, box):
        x, y, w, h = box
        crop = frame[y + 5:y + h - 8, x:x + w]
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        gold = ((hsv[:, :, 0] >= 12) & (hsv[:, :, 0] <= 42)
                & (hsv[:, :, 1] >= 25) & (hsv[:, :, 2] >= 120))
        return bool(gold[:, :4].mean() > .45 and gold[:, -4:].mean() > .45)

    def select(self, box, facts, generation, *, verify_details=True, already_selected=False):
        self.check_generation(generation)
        # read_details leaves the pointer on this card. Use that exact point:
        # Runtime's generic click would move, wait and unhover it again.
        x, y, w, h = box
        point = [round(x + w / 2), round(y + h * .45)]
        if self._hover_box != tuple(box):
            self._hover_read = None
            if not self.rt.action("TouchMove", target=point) or not self.rt.pause(.15):
                raise RuntimeError("未能定位目标闪光")
            self._hover_box = tuple(box)
        if not already_selected and (
                not self.rt.action("Click", target=point, ui_click=False) or not self.rt.pause(.15)):
            raise RuntimeError("未能选中目标闪光")
        frame = self.frame(generation)
        proof = self._hover_read
        same_tooltip = (proof is not None and proof[0] == tuple(box) and proof[1] == generation
                        and self.key(proof[2]) == self.key(facts) and proof[2]["level"] == facts["level"]
                        and self.same_fields(proof[3], frame, facts))
        if not same_tooltip:
            # Clicking can close the panel even when the pointer stays put.
            # A later adjacent read must not reuse that dismissed tooltip.
            self._hover_read = None
        if not self.selected(frame, box):
            raise RuntimeError("未确认目标格子的选中高亮，未操作锁按钮")
        if verify_details:
            if not same_tooltip:
                self.counter += 1
                selected, _ = self.read_details(box, generation, self.counter)
                if self.key(selected) != self.key(facts) or selected["level"] != facts["level"]:
                    raise RuntimeError("选中后闪光信息发生变化，未操作锁按钮")
                frame = self.frame(generation)
        # The lock button is outside the card tooltip. Keep the pointer here;
        # both its state and the selection are read from fresh captures.
        return self.lock_state(generation, frame=frame)

    def sort_direction(self, frame, template):
        # Desktop screenshots and runtime captures can differ by a few pixels
        # after normalization. Search a small neighbourhood, not a fixed crop.
        crop = cv2.cvtColor(self.region(frame, [1220, 665, 34, 38]), cv2.COLOR_BGR2GRAY)
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
        return self.rt.ui.enter_page(
            lambda: self.rt.action("Click", target=self.layout["slot_buttons"][index]),
            node, source=READY, attempts=2)

    def acquisition_sort(self):
        """Select acquisition order and confirm the user-supplied down arrow."""
        generation = self.rt._focus_generation
        frame = self.clear_hover(generation)
        if not self.rt.text(self.layout["sort_label"], "获得时间", image=frame):
            if (not self.rt.action("Click", target=self.layout["sort_label"])
                    or not self.rt.pause(.2)
                    or not self.rt.click_text("^获得时间$", [1055, 550, 205, 110])
                    or not self.rt.pause(.3)):
                raise RuntimeError("未能切换获得时间排序")
        reference = ROOT / "resource/image/blessing_lock/acquired_desc.png"
        template = cv2.imdecode(np.fromfile(str(reference), np.uint8), cv2.IMREAD_GRAYSCALE)
        if template is None:
            raise RuntimeError("获得时间排序方向资源缺失")
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
                return
            if attempt == 0 and direction == "asc":
                if self.rt.action("Click", target=[1237, 684]) and self.rt.pause(.3):
                    continue
            self.image("sort_direction_unknown.png", frame)
            raise RuntimeError("未确认获得时间从新到旧的排序箭头，请设为默认降序后重试")

    def unchanged(self, before, after):
        return self.same_structure(before, after, self.grid, threshold=.997)

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
        self._hover_read = None
        self._hover_box = None
        self.check_generation(generation)
        operation = {"key": list(self.key(facts)), "level": facts["level"],
                     "kind": "lock" if target == "locked" else "unlock", "reason": reason,
                     "expected_state": target,
                     "status": "submitted_unconfirmed", "evidence": evidence,
                     "before": self.image(f"before_{len(self.report['operations']):03d}.png", current)}
        self.pending = operation
        self.report["operations"].append(operation)
        self.save()
        # The lock toggle is submitted once even if delivery/verification fails.
        if not self.rt.action("Click", target=self.layout["lock_button"], ui_click=False):
            raise RuntimeError(f"{verb}输入结果不确定，停止且不重试，请检查当前物品")
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
