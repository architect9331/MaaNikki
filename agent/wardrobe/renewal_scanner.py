"""Full tall-window Clothing Glow-Up (服装焕新) scanner for clothing levels & renewal state."""
from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timezone
from functools import lru_cache
import json
from pathlib import Path
import re

import cv2
import numpy as np

from account_state import AccountState
from collection.backpack import wardrobe_snapshot, wardrobe_snapshot_key
from collection.material_pursuit import load_catalogs, record_material_history
from daily.settings import ROOT
from tall_window import clamp_capture_park_point, safe_capture_y_range, tall_window, window_hwnd
from .model import WardrobeCatalog
from .scanner import MAKEUP_PARTS, PEAR_PAL_NEXT_PAGE, PEAR_PAL_PREV_PAGE


HEADER_ROI = [65, 15, 200, 48]
FILTER_BTN = [824, 52]
FILTER_RESET_ROI = [450, 475, 185, 65]
FILTER_CONFIRM_ROI = [645, 475, 185, 65]
FILTER_RESET = [543, 512]
FILTER_CONFIRM = [734, 512]
SORT_BOX_ROI = [848, 34, 300, 36]
SORT_MENU_ROI = [820, 72, 410, 250]
SORT_DIR_ROI = [1138, 32, 68, 58]

RAIL_X0 = 62
RAIL_SIZE = 48
RAIL_CENTER_X = 86
RAIL_MINOR_SIZE = 39
RAIL_MINOR_X0 = RAIL_CENTER_X - RAIL_MINOR_SIZE // 2
RAIL_MINOR_BAND_TOP_OFFSET = 42
RAIL_MINOR_BAND_HEIGHT = 370
RAIL_GLOW_X0 = 54
RAIL_GLOW_X1 = 118
RAIL_TOP_Y = 130
RAIL_BOTTOM_MARGIN = 50
RAIL_MINOR_BOTTOM_MARGIN = 16
RAIL_MAJOR_MAX_Y = 920
RAIL_MAX_Y = 2200
RAIL_TOP_SLOT = [86, 174]

COLS = 6
FIRST_X = 172
COL_PITCH = 176
CARD_W = 150
CARD_H = 188
ROW_PITCH_EXACT = 245.33
GRID_TOP = 94
GRID_BOTTOM_MARGIN = 10
GRID_SCROLL_TARGET = [620, 360]

MAX_LEVEL = 10
RENEWED_LEVEL = 11
ACCESSORY_PARTS = {
    "270", "271", "272", "273", "274", "275", "276", "277", "278", "279",
    "287", "292", "293", "294", "295", "296",
}


def clean_clothing_name(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]", "", str(text or "")).replace("・", "·")


def parse_rejuv_level(text: str) -> int | None:
    cleaned = re.sub(r"\s+", "", str(text or ""))
    if not cleaned:
        return None
    upper = cleaned.upper()
    if "MAX" in upper or "МАХ" in upper:
        return MAX_LEVEL
    match = re.search(r"(?:等级|LV|级)\s*[.:：]?\s*(\d{1,2})", cleaned, re.IGNORECASE)
    if match:
        lv = int(match.group(1))
        return lv if 0 <= lv <= MAX_LEVEL else None
    full = re.fullmatch(r"(\d{1,2})", cleaned)
    if full:
        lv = int(full.group(1))
        return lv if 0 <= lv <= MAX_LEVEL else None
    return None


def _default_scoring() -> dict:
    return {
        "schema_version": 1,
        "heart": {"flat": [0, 0, 0, 0, 0], "stat": [0, 0, 0, 0, 0, 0], "part": [0, 0, 0, 0, 0, 0], "display": 0},
        "levels": {},
        "rank_thresholds": {},
    }


class RejuvenateModel:
    """Packed left-rail category icons + scoring catalog name-to-group lookup."""

    def __init__(self, directory: Path, scoring: dict):
        cat = json.loads((directory / "categories.json").read_text(encoding="utf-8"))
        self.majors: list[dict] = list(cat.get("major_categories") or [])
        self.minors: list[dict] = list(cat.get("accessory_minors") or [])
        self.meta_by_id: dict[str, dict] = {m["id"]: m for m in [*self.majors, *self.minors]}
        self.major_ids: list[str] = [m["id"] for m in self.majors]
        self.minor_ids: list[str] = [m["id"] for m in self.minors]
        self.non_skip_majors: list[str] = [m["id"] for m in self.majors if not m.get("skip") and m["id"] != "acc"]
        self.expected_categories: list[str] = [*self.non_skip_majors, *self.minor_ids]

        npz = np.load(directory / "icons.npz")
        major_id_set = set(self.major_ids)
        minor_id_set = set(self.minor_ids)
        self.major_templates: list[tuple[str, np.ndarray]] = []
        self.minor_templates: list[tuple[str, np.ndarray]] = []
        self.templates: list[tuple[str, np.ndarray]] = []
        for key in npz.files:
            cid = key[4:].removesuffix("_s") if key.startswith("cat_") else key
            alpha = npz[key][:, :, 3].astype(np.float32) / 255.0
            norm_t = alpha - float(alpha.mean())
            denom = float(np.sqrt((norm_t ** 2).sum()))
            if denom > 1e-6:
                tmpl = norm_t / denom
                self.templates.append((cid, tmpl))
                if cid in major_id_set:
                    self.major_templates.append((cid, tmpl))
            if cid in minor_id_set:
                # Accessory minor sub-slots live inside ScaleBox_1 (UserSpecifiedScale=0.8),
                # rendering at ~39x39px at 720p rather than the 48x48px major icon size.
                alpha_minor = cv2.resize(alpha, (RAIL_MINOR_SIZE, RAIL_MINOR_SIZE), interpolation=cv2.INTER_AREA)
                norm_m = alpha_minor - float(alpha_minor.mean())
                denom_m = float(np.sqrt((norm_m ** 2).sum()))
                if denom_m > 1e-6:
                    self.minor_templates.append((cid, norm_m / denom_m))

        # Build clean_name -> ordered unique group IDs per part and combined for accessories,
        # plus canonical metadata per group for merging newly seen pieces into wardrobe.inventory.
        self.part_groups: dict[str, dict[str, list[str]]] = {}
        self.group_meta: dict[str, dict] = {}
        game_items = scoring.get("game_items") or {}
        entries = sorted(
            game_items.items(),
            key=lambda kv: (kv[0] != str(kv[1].get("group")), int(kv[0])),
        )
        for gid, g in entries:
            part = str(g.get("part") or "")
            if not part or part == "413" or part in MAKEUP_PARTS:
                continue
            cname = clean_clothing_name(g.get("name", ""))
            grp = str(g.get("group") or "")
            if not cname or not grp:
                continue
            meta = self.group_meta.setdefault(grp, {
                "group": grp,
                "part": part,
                "base_gid": int(gid),
                "name": str(g.get("name") or ""),
                "quality": int(g.get("quality") or 3),
                "reward_gid": None,
                "reward_name": None,
                "reward_quality": None,
            })
            if g.get("glow_reward") and meta["reward_gid"] is None:
                meta["reward_gid"] = int(gid)
                meta["reward_name"] = str(g.get("name") or "")
                meta["reward_quality"] = int(g.get("quality") or meta["quality"])
            for bucket in ([part, "acc"] if part in ACCESSORY_PARTS else [part]):
                table = self.part_groups.setdefault(bucket, {})
                groups = table.setdefault(cname, [])
                if grp not in groups:
                    groups.append(grp)

    def lookup_groups(self, part: str, raw_name: str) -> list[str]:
        cname = clean_clothing_name(raw_name)
        if not cname:
            return []
        table = self.part_groups.get(part) or {}
        if cname in table:
            return list(table[cname])

        # Build sanitized candidates only after exact verbatim match misses
        # (preserves real names ending in digits like 全黑信念01 or starting with style words).
        candidates: list[str] = []
        no_alnum = re.sub(r"[0-9A-Za-z_]", "", cname)
        if no_alnum and no_alnum != cname:
            candidates.append(no_alnum)
        for base in (cname, no_alnum):
            if not base:
                continue
            for pref in ("典雅", "清新", "甜美", "性感", "帅气"):
                if base.startswith(pref) and len(base) >= len(pref) + 2:
                    stripped = base[len(pref):]
                    if stripped not in candidates:
                        candidates.append(stripped)

        for cand in candidates:
            if cand in table:
                return list(table[cand])

        for cand in (cname, *candidates):
            cjk_count = sum("\u4e00" <= ch <= "\u9fff" for ch in cand)
            if len(cand) < 3 or cjk_count < 2:
                continue
            dist1 = [
                groups for key, groups in table.items()
                if len(key) == len(cand) and sum(a != b for a, b in zip(cand, key)) == 1
            ]
            if len(dist1) == 1:
                return list(dist1[0])
            # Single omitted char at any position (prefix, suffix, or interior such as 晨跑第天 -> 晨跑第一天).
            del1 = [
                groups for key, groups in table.items()
                if len(key) - len(cand) == 1
                and any(key[:i] + key[i + 1:] == cand for i in range(len(key)))
            ]
            if len(del1) == 1:
                return list(del1[0])
        return []

    def category_name(self, cid: str) -> str:
        meta = self.meta_by_id.get(cid)
        return str(meta.get("name") or cid) if meta else cid

    def _rail_glow_candidates(self, frame: np.ndarray) -> tuple[int, list[tuple[np.ndarray, int, float, float]]]:
        """Collect sorted left-rail major-tab golden highlight block candidates."""
        if frame is None:
            return 0, []
        h, _w = frame.shape[:2]
        y_end = min(max(RAIL_TOP_Y + 40, h - 24), RAIL_MAJOR_MAX_Y)
        strip = frame[RAIL_TOP_Y:y_end, RAIL_GLOW_X0:RAIL_GLOW_X1]
        if strip.size == 0:
            return 0, []
        r = strip[:, :, 2].astype(np.int32)
        b = strip[:, :, 0].astype(np.int32)
        glow_rows = np.where(((r > 145) & ((r - b) > 22)).sum(axis=1) >= 20)[0] + RAIL_TOP_Y
        if len(glow_rows) < 16:
            return int(len(glow_rows)), []

        breaks = np.where(np.diff(glow_rows) > 8)[0]
        candidates: list[tuple[np.ndarray, int, float, float]] = []
        for seg in np.split(glow_rows, breaks + 1):
            if len(seg) < 16:
                continue
            yc = int(round(float(np.median(seg))))
            box = frame[max(0, yc - 34):min(h, yc + 34), RAIL_GLOW_X0:RAIL_GLOW_X1]
            if box.size == 0:
                continue
            box_r = box[:, :, 2].astype(np.int32)
            box_b = box[:, :, 0].astype(np.int32)
            warm_frac = float(((box_r > 145) & ((box_r - box_b) > 22)).mean())
            if warm_frac < 0.25:
                continue
            bright_warm = float(((box_r > 180) & ((box_r - box_b) > 30)).mean())
            candidates.append((seg, yc, warm_frac, bright_warm))
        candidates.sort(
            key=lambda item: (
                len(item[0]) > 120,
                -item[2],
                -item[3],
                -len(item[0]),
            )
        )
        return int(len(glow_rows)), candidates

    def _detect_active_major(self, frame: np.ndarray) -> tuple[str | None, int | None, float]:
        """Locate the glowing selected major-tab slot (T_UI_common_btn_tab_04_s, 48x48 icon)."""
        if frame is None:
            return None, None, -1.0
        h, w = frame.shape[:2]
        _glow_count, candidates = self._rail_glow_candidates(frame)
        if not candidates:
            return None, None, -1.0
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)

        fallback_yc = candidates[0][1]
        fallback_sc = -1.0
        best_hit: tuple[tuple[int, float, float, float], str, int, float] | None = None
        for seg, yc, wf, bw in candidates[:6]:
            best_by_cid: dict[str, float] = {}
            for y0 in range(max(0, yc - 38), min(h - RAIL_SIZE, yc - 20), 2):
                for x0 in range(max(0, RAIL_X0 - 2), min(w - RAIL_SIZE, RAIL_X0 + 3)):
                    patch = gray[y0:y0 + RAIL_SIZE, x0:x0 + RAIL_SIZE]
                    p = patch - float(patch.mean())
                    denom = float(np.sqrt((p ** 2).sum()))
                    if denom < 1e-6:
                        continue
                    pn = p / denom
                    for cid, tmpl in self.major_templates:
                        sc = float((pn * tmpl).sum())
                        if sc > best_by_cid.get(cid, -1.0):
                            best_by_cid[cid] = sc
            if not best_by_cid:
                continue
            ranked = sorted(best_by_cid.items(), key=lambda kv: -kv[1])
            top1_id, top1_sc = ranked[0]
            top2_sc = ranked[1][1] if len(ranked) > 1 else -1.0
            if top1_sc > fallback_sc:
                fallback_sc = top1_sc
                fallback_yc = yc
            if top1_sc >= 0.62 and (top1_sc - top2_sc) >= 0.06:
                rank_key = (
                    1 if len(seg) <= 120 else 0,
                    round(float(wf), 3),
                    round(float(bw), 3),
                    top1_sc,
                )
                if best_hit is None or rank_key > best_hit[0]:
                    best_hit = (rank_key, top1_id, yc, top1_sc)
        if best_hit is not None:
            return best_hit[1], best_hit[2], best_hit[3]
        return None, fallback_yc, fallback_sc

    def _minor_glow_candidates(
        self,
        frame: np.ndarray,
        y_start: int,
        y_end: int,
    ) -> list[tuple[int, float, float]]:
        """Locate accessory minor highlight bands (T_UI_common_btn_tab_05_s bottom warm crescent).

        Unlike major tabs (full golden box), accessory sub-slots use a half-height warm gradient
        whose opaque band sits in the lower ~20-26px of the 0.8x-scaled slot (~18px below icon center).
        """
        h, _w = frame.shape[:2]
        y0 = max(RAIL_TOP_Y - 10, int(y_start))
        y1 = min(h - RAIL_MINOR_BOTTOM_MARGIN, int(y_end))
        if y1 - y0 < 32:
            return []
        strip = frame[y0:y1, RAIL_GLOW_X0:RAIL_GLOW_X1]
        if strip.size == 0:
            return []
        r = strip[:, :, 2].astype(np.int32)
        b = strip[:, :, 0].astype(np.int32)
        glow_rows = np.where(((r > 145) & ((r - b) > 22)).sum(axis=1) >= 16)[0] + y0
        if len(glow_rows) < 8:
            return []

        breaks = np.where(np.diff(glow_rows) > 8)[0]
        candidates: list[tuple[int, float, float]] = []
        for seg in np.split(glow_rows, breaks + 1):
            if len(seg) < 8:
                continue
            if len(seg) <= 80:
                glow_yc = int(round(float(np.median(seg))))
                box = frame[max(0, glow_yc - 14):min(h, glow_yc + 14), RAIL_GLOW_X0 + 4:RAIL_GLOW_X1 - 4]
                if box.size == 0:
                    continue
                br = box[:, :, 2].astype(np.int32)
                bb = box[:, :, 0].astype(np.int32)
                wf = float(((br > 145) & ((br - bb) > 22)).mean())
                if wf < 0.30:
                    continue
                bw = float(((br > 180) & ((br - bb) > 30)).mean())
                candidates.append((glow_yc, wf, bw))
            else:
                # If a warm 3D background floods the rail strip, locate the brighter tab_05_s
                # golden bottom frame using outer border columns (clear of the central icon)
                # relative to the segment median.
                s0, s1 = int(seg[0]), int(seg[-1]) + 1
                sub = frame[s0:s1, RAIL_GLOW_X0 + 4:RAIL_GLOW_X1 - 4].astype(np.float32)
                if sub.shape[0] < 22 or sub.shape[1] < 24:
                    continue
                side = np.concatenate([sub[:, :10, :], sub[:, -10:, :]], axis=1)
                row_luma = side[:, :, 2] * 0.5 + side[:, :, 1] * 0.35 + side[:, :, 0] * 0.15
                row_sig = np.median(row_luma, axis=1)
                excess = np.maximum(0.0, row_sig - float(np.median(row_sig)))
                kernel = np.ones(14, dtype=np.float32) / 14.0
                conv = np.convolve(excess, kernel, mode="same").copy()
                # Extract up to 3 separated local peaks (>= 48px apart) so a bright parent tab_04_s
                # or localized 3D sunspot cannot mask the active tab_05_s sub-slot.
                for _ in range(3):
                    peak_idx = int(np.argmax(conv))
                    peak_val = float(conv[peak_idx])
                    if peak_val < 3.5:
                        break
                    candidates.append((s0 + peak_idx, 0.45, peak_val / 50.0))
                    lo = max(0, peak_idx - 48)
                    hi = min(len(conv), peak_idx + 49)
                    conv[lo:hi] = 0.0
        candidates.sort(key=lambda item: (-item[1], -item[2]))
        return candidates

    def _minor_frame_candidates(
        self,
        frame: np.ndarray,
        y_start: int,
        y_end: int,
    ) -> list[tuple[int, int, float]]:
        """Fallback locator for T_UI_common_btn_tab_05_s using its symmetric left/right box borders.

        When the active accessory minor sits at the bottom slot of X6ScrollBox_0, RetainerBox_365's
        MI_ListViewMask_TopBottom bottom fade attenuates the lower warm crescent enough to drop
        R-B below threshold on cool backgrounds, while the upper ~35-40px of the 58x58 bright
        square border (x≈58 and x≈114, clear of 39x39 icons at x=67..105) remains unmasked.
        """
        h, w = frame.shape[:2]
        if w < 128:
            return []
        y0 = max(RAIL_TOP_Y - 10, int(y_start))
        y1 = min(h - RAIL_MINOR_BOTTOM_MARGIN, int(y_end))
        if y1 - y0 < 36:
            return []

        def _wluma(patch: np.ndarray) -> np.ndarray:
            p = patch.astype(np.float32)
            return p[:, :, 2] * 0.55 + p[:, :, 1] * 0.30 + p[:, :, 0] * 0.15

        left_edge = _wluma(frame[y0:y1, 55:62]).max(axis=1)
        left_bg = np.median(_wluma(frame[y0:y1, 44:50]), axis=1)
        right_edge = _wluma(frame[y0:y1, 110:117]).max(axis=1)
        right_bg = np.median(_wluma(frame[y0:y1, 122:128]), axis=1)
        pair_excess = np.minimum(left_edge - left_bg, right_edge - right_bg)

        rows = np.where(pair_excess >= 16.0)[0] + y0
        if len(rows) < 20:
            return []
        breaks = np.where(np.diff(rows) > 6)[0]
        candidates: list[tuple[int, int, float]] = []
        for seg in np.split(rows, breaks + 1):
            if len(seg) < 20 or len(seg) > 76:
                continue
            seg_top = int(seg[0])
            seg_bot = int(seg[-1])
            strength = float(np.median(pair_excess[seg - y0]))
            if strength < 20.0:
                continue
            candidates.append((seg_top, seg_bot, strength))
        candidates.sort(key=lambda item: -item[2])
        return candidates

    def _classify_minor_window(
        self,
        gray: np.ndarray,
        y_lo: int,
        y_hi: int,
    ) -> tuple[str | None, int | None, float, float]:
        """Run detrended 39x39 NCC across [y_lo, y_hi] and return (top1_id, icon_yc, top1_sc, top2_sc)."""
        h, w = gray.shape[:2]
        y_lo = max(0, int(y_lo))
        y_hi = min(h - RAIL_MINOR_SIZE, int(y_hi))
        if y_hi < y_lo:
            return None, None, -1.0, -1.0
        x_lo = max(0, RAIL_MINOR_X0 - 3)
        x_hi = min(w - RAIL_MINOR_SIZE, RAIL_MINOR_X0 + 4)
        best_by_cid: dict[str, float] = {}
        best_y0_by_cid: dict[str, int] = {}
        for y0 in range(y_lo, y_hi + 1, 2):
            for x0 in range(x_lo, x_hi):
                patch = gray[y0:y0 + RAIL_MINOR_SIZE, x0:x0 + RAIL_MINOR_SIZE]
                # Subtract per-row lower quartile to cancel tab_05_s's vertical gradient
                # without attenuating the white foreground icon silhouette.
                detrend = patch - np.percentile(patch, 25, axis=1, keepdims=True)
                p = detrend - float(detrend.mean())
                denom = float(np.sqrt((p ** 2).sum()))
                if denom < 1e-6:
                    continue
                pn = p / denom
                for cid, tmpl in self.minor_templates:
                    sc = float((pn * tmpl).sum())
                    if sc > best_by_cid.get(cid, -1.0):
                        best_by_cid[cid] = sc
                        best_y0_by_cid[cid] = y0
        if not best_by_cid:
            return None, None, -1.0, -1.0
        ranked = sorted(best_by_cid.items(), key=lambda kv: -kv[1])
        top1_id, top1_sc = ranked[0]
        top2_sc = ranked[1][1] if len(ranked) > 1 else -1.0
        icon_yc = best_y0_by_cid[top1_id] + RAIL_MINOR_SIZE // 2
        return top1_id, icon_yc, top1_sc, top2_sc

    def _detect_active_minor(
        self,
        frame: np.ndarray,
        y_start: int,
        y_end: int,
    ) -> tuple[str | None, int | None, float]:
        """Classify the active 0.8x accessory minor sub-slot inside [y_start, y_end]."""
        if frame is None or not self.minor_templates:
            return None, None, -1.0
        h, _w = frame.shape[:2]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)

        candidates = self._minor_glow_candidates(frame, y_start, y_end)
        best_hit: tuple[tuple[float, float, float], str, int, float] | None = None
        fallback_yc: int | None = None
        fallback_sc = -1.0
        for glow_yc, wf, bw in candidates[:4]:
            # Icon center lies ~14-22px above tab_05_s's lower warm crescent median,
            # so icon top y0 = icon_cy - RAIL_MINOR_SIZE // 2 lies in [glow_yc - 44, glow_yc - 22].
            top1_id, icon_yc, top1_sc, top2_sc = self._classify_minor_window(
                gray,
                glow_yc - 44,
                glow_yc - 22,
            )
            if top1_id is None or icon_yc is None:
                continue
            if top1_sc > fallback_sc:
                fallback_sc = top1_sc
                fallback_yc = icon_yc
            if top1_sc >= 0.56 and (top1_sc - top2_sc) >= 0.08:
                rank_key = (round(float(wf), 3), round(float(bw), 3), top1_sc)
                if best_hit is None or rank_key > best_hit[0]:
                    best_hit = (rank_key, top1_id, icon_yc, top1_sc)
        if best_hit is not None:
            return best_hit[1], best_hit[2], best_hit[3]

        # Fallback: detect bottom-masked tab_05_s via its unmasked upper left/right border frame.
        frame_cands = self._minor_frame_candidates(frame, y_start, y_end)
        best_frame_hit: tuple[tuple[float, float], str, int, float] | None = None
        for seg_top, seg_bot, strength in frame_cands[:3]:
            # tab_05_s top border lies ~29px above icon center -> icon top y0 lies ~10px below seg_top
            # (or slightly higher when the very top corner of the border is soft on bright backgrounds).
            y_lo = max(0, seg_top - 4)
            y_hi = min(h - RAIL_MINOR_SIZE, max(seg_top + 20, seg_bot - 26))
            top1_id, icon_yc, top1_sc, top2_sc = self._classify_minor_window(gray, y_lo, y_hi)
            if top1_id is None or icon_yc is None:
                continue
            if top1_sc > fallback_sc:
                fallback_sc = top1_sc
                fallback_yc = icon_yc
            if top1_sc >= 0.56 and (top1_sc - top2_sc) >= 0.08:
                rank_key = (round(float(strength), 2), top1_sc)
                if best_frame_hit is None or rank_key > best_frame_hit[0]:
                    best_frame_hit = (rank_key, top1_id, icon_yc, top1_sc)
        if best_frame_hit is not None:
            return best_frame_hit[1], best_frame_hit[2], best_frame_hit[3]
        return None, fallback_yc, fallback_sc

    def detect_active_category(self, frame: np.ndarray) -> tuple[str | None, int | None, float]:
        """Locate the active left-rail category, resolving '饰品' to its selected minor subcategory.

        In 服装焕新, '饰品' has no unified overview view: whenever any of its 15 accessory
        subcategories is active, both the parent '饰品' major tab and the active minor sub-slot
        below it are highlighted simultaneously.
        """
        if frame is None:
            return None, None, -1.0
        h, _w = frame.shape[:2]
        major_cid, major_yc, major_sc = self._detect_active_major(frame)
        if major_cid is not None and major_cid != "acc":
            return major_cid, major_yc, major_sc
        if major_cid == "acc" and major_yc is not None:
            minor_cid, minor_yc, minor_sc = self._detect_active_minor(
                frame,
                major_yc + RAIL_MINOR_BAND_TOP_OFFSET,
                min(h - RAIL_MINOR_BOTTOM_MARGIN, major_yc + RAIL_MINOR_BAND_TOP_OFFSET + RAIL_MINOR_BAND_HEIGHT),
            )
            if minor_cid is not None:
                return minor_cid, minor_yc, minor_sc
            # Return 'acc' only when the accessory sub-list is collapsed or mid-animation so
            # the caller can wait/expand it rather than misreading it as an overview page.
            return "acc", major_yc, major_sc
        # Fallback if '饰品' itself was scrolled off the top edge of the rail.
        minor_cid, minor_yc, minor_sc = self._detect_active_minor(
            frame,
            RAIL_TOP_Y,
            min(h - RAIL_MINOR_BOTTOM_MARGIN, RAIL_MAX_Y),
        )
        if minor_cid is not None:
            return minor_cid, minor_yc, minor_sc
        return None, major_yc, major_sc

    def debug_active_category(self, frame: np.ndarray | None) -> dict:
        """Return JSON-safe rail detection metrics for incomplete-scan diagnostics."""
        if frame is None:
            return {
                "frame_shape": None,
                "glow_rows_count": 0,
                "candidates": [],
                "minor_candidates": [],
                "detected_cid": None,
                "detected_yc": None,
                "detected_score": -1.0,
            }
        h = int(frame.shape[0])
        glow_count, candidates = self._rail_glow_candidates(frame)
        major_cid, major_yc, _major_sc = self._detect_active_major(frame)
        if major_cid == "acc" and major_yc is not None:
            minor_y0 = major_yc + RAIL_MINOR_BAND_TOP_OFFSET
            minor_y1 = min(h - RAIL_MINOR_BOTTOM_MARGIN, minor_y0 + RAIL_MINOR_BAND_HEIGHT)
            minor_cands = self._minor_glow_candidates(frame, minor_y0, minor_y1)
            minor_frame_cands = self._minor_frame_candidates(frame, minor_y0, minor_y1)
        else:
            minor_cands = []
            minor_frame_cands = []
        cid, yc, sc = self.detect_active_category(frame)
        return {
            "frame_shape": [h, int(frame.shape[1])],
            "glow_rows_count": glow_count,
            "candidates": [
                {
                    "yc": int(cand_yc),
                    "len": int(len(seg)),
                    "warm_frac": round(float(wf), 3),
                    "bright_warm": round(float(bw), 3),
                }
                for seg, cand_yc, wf, bw in candidates[:6]
            ],
            "minor_candidates": [
                {
                    "glow_yc": int(gyc),
                    "warm_frac": round(float(wf), 3),
                    "bright_warm": round(float(bw), 3),
                }
                for gyc, wf, bw in minor_cands[:4]
            ],
            "minor_frame_candidates": [
                {
                    "top": int(seg_top),
                    "bot": int(seg_bot),
                    "strength": round(float(strength), 2),
                }
                for seg_top, seg_bot, strength in minor_frame_cands[:3]
            ],
            "detected_cid": cid,
            "detected_yc": int(yc) if yc is not None else None,
            "detected_score": round(float(sc), 4),
        }


@lru_cache(maxsize=2)
def _load_rejuvenate_model(scoring_version: str = "") -> RejuvenateModel:
    from data_paths import data_path
    _crafting, scoring = load_catalogs()
    return RejuvenateModel(data_path("resource/model/rejuvenate"), scoring)


def detect_rejuv_rows(frame: np.ndarray, grid_top: int = GRID_TOP, grid_bottom: int | None = None) -> list[int]:
    """Detect all complete visible card row top Y coordinates in 服装焕新."""
    if frame is None:
        return []
    if grid_bottom is None:
        grid_bottom = frame.shape[0] - GRID_BOTTOM_MARGIN
    if grid_bottom - grid_top < CARD_H + 34:
        return []
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)
    gx_abs = np.abs(np.diff(gray, axis=1, prepend=gray[:, :1]))
    # Left/right vertical card side borders vanish cleanly in the sky/title gap between rows.
    # Taking the max across columns keeps detection strong even on 1-card bottom rows.
    col_bv = np.stack([
        gx_abs[:, FIRST_X + c * COL_PITCH - 2:FIRST_X + c * COL_PITCH + 4].mean(axis=1)
        + gx_abs[:, FIRST_X + c * COL_PITCH + CARD_W - 3:FIRST_X + c * COL_PITCH + CARD_W + 3].mean(axis=1)
        for c in range(COLS)
    ])
    bv = np.max(col_bv, axis=0)
    h_img = len(bv)

    def _eval_lattice(phase: float, pitch: float) -> tuple[float, list[int]]:
        ys: list[int] = []
        k = 0
        while True:
            by = int(round(-180 + phase + k * pitch))
            k += 1
            if by >= h_img - 40:
                break
            if by + CARD_H > grid_top:
                ys.append(by)
        if not ys:
            return -1e9, []
        tot = 0.0
        for by in ys:
            c0, c1 = max(grid_top + 2, by + 6), min(h_img, by + 184)
            in_card = float(np.mean(bv[c0:c1])) if c1 > c0 + 10 else 0.0
            g1_0, g1_1 = max(grid_top + 2, by - 36), max(grid_top + 2, by - 2)
            g1 = float(np.mean(bv[g1_0:g1_1])) if g1_1 > g1_0 + 4 else 0.0
            g2_0, g2_1 = min(h_img, by + 190), min(h_img, by + 238)
            g2 = float(np.mean(bv[g2_0:g2_1])) if g2_1 > g2_0 + 8 else 0.0
            tot += in_card - 1.5 * g1 - 1.5 * g2
        return tot / len(ys), ys

    best_sc, best_ys, best_phase = -1e9, [], 0.0
    for phase in np.linspace(0, ROW_PITCH_EXACT, 245, endpoint=False):
        sc, ys = _eval_lattice(float(phase), ROW_PITCH_EXACT)
        if sc > best_sc:
            best_sc, best_ys, best_phase = sc, ys, float(phase)
    # Refine fractional pitch slightly so 20-row tall grids do not accumulate multi-pixel vertical drift.
    for pitch in np.linspace(ROW_PITCH_EXACT - 0.6, ROW_PITCH_EXACT + 0.6, 13):
        for phase in best_phase + np.linspace(-3.0, 3.0, 13):
            sc, ys = _eval_lattice(float(phase), float(pitch))
            if sc > best_sc:
                best_sc, best_ys = sc, ys
    return [by for by in best_ys if by >= grid_top and by + 222 <= grid_bottom]


def is_card_present(frame: np.ndarray, bx: int, by: int) -> bool:
    if by < 0 or by + CARD_H > frame.shape[0] or bx < 0 or bx + CARD_W > frame.shape[1]:
        return False
    body = frame[by + 16:by + 166, bx + 12:bx + 138]
    gray = cv2.cvtColor(body, cv2.COLOR_BGR2GRAY)
    if float(np.std(gray)) < 9.0:
        return False
    # Reject smooth translucent 3D background patches (sky/clouds) lacking sharp card UI/text strokes.
    ig = gray.astype(np.int16)
    dx = np.abs(np.diff(ig, axis=1))[:-1, :]
    dy = np.abs(np.diff(ig, axis=0))[:, :-1]
    return float(np.mean((dx + dy) > 14)) >= 0.005


def has_renewed_star(frame: np.ndarray, bx: int, by: int) -> bool:
    """Detect the glowing 4-pointed compass badge at the top-right corner of renewed cards."""
    y0 = max(0, by - 4)
    y1 = min(frame.shape[0], by + 32)
    x0 = max(0, bx + 112)
    x1 = min(frame.shape[1], bx + CARD_W)
    roi = frame[y0:y1, x0:x1]
    if roi.size == 0:
        return False
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    return int(np.sum(gray > 235)) >= 25


def _read_single_ocr(rt, frame: np.ndarray, roi: list[int], expected: str, *, threshold: float = 0.50) -> str:
    rx, ry, rw, rh = roi
    crop = frame[max(0, ry):min(frame.shape[0], ry + rh), max(0, rx):min(frame.shape[1], rx + rw)]
    if crop.size == 0:
        return ""
    up = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    padded = cv2.copyMakeBorder(up, 8, 8, 8, 8, cv2.BORDER_REPLICATE)
    res = rt.recognize("MaaNikki_Daily_OCR", {
        "roi": [0, 0, padded.shape[1], padded.shape[0]],
        "expected": expected,
        "threshold": threshold,
    }, image=padded)
    if res and res.hit:
        items = getattr(res, "filtered_results", []) or [res.best_result]
        return "".join(getattr(item, "text", "") or "" for item in items).strip()
    return rt.text(roi, image=frame).strip()


def _batch_read_rows_ocr(
    rt,
    frame: np.ndarray,
    rows: list[tuple[int, list[int]]],
) -> dict[tuple[int, int], tuple[str, int | None, bool]]:
    """Read (name_raw, level, renewed) for active columns in each row using <=480px OCR bands."""
    if not rows:
        return {}
    level_by_cell: dict[tuple[int, int], int | None] = {}
    name_by_cell: dict[tuple[int, int], str] = {}

    # Chunk 2 rows per band (<=480px height) so det.onnx never downscales tall 5120p crops.
    for start in range(0, len(rows), 2):
        chunk = rows[start:start + 2]
        top_y = max(GRID_TOP, min(by for by, _ in chunk) + 114)
        bot_y = min(frame.shape[0], max(by for by, _ in chunk) + 234)
        band_roi = [FIRST_X - 10, top_y, 1080, bot_y - top_y]
        res = rt.recognize("MaaNikki_Daily_OCR", {
            "roi": band_roi,
            "expected": ".+",
            "threshold": 0.55,
        }, image=frame)
        items = (getattr(res, "filtered_results", []) or [res.best_result]) if (res and res.hit) else []

        # Collect fragments per (row_by, col, kind)
        lv_frags: dict[tuple[int, int], list[tuple[float, str]]] = {}
        name_frags: dict[tuple[int, int], list[tuple[float, str]]] = {}
        for item in items:
            ix, iy, iw, ih = list(item.box)
            cx = ix + iw / 2.0
            cy = iy + ih / 2.0
            col = round((cx - (FIRST_X + CARD_W / 2.0)) / COL_PITCH)
            if not (0 <= col < COLS):
                continue
            bx = FIRST_X + col * COL_PITCH
            if cx < bx - 12 or cx > bx + CARD_W + 12:
                continue
            matched_by = min((by for by, _ in chunk), key=lambda r_by: abs(cy - (r_by + 168.0)))
            txt = (getattr(item, "text", "") or "").strip()
            if not txt:
                continue
            if matched_by + 112 <= cy <= matched_by + 158:
                lv_frags.setdefault((matched_by, col), []).append((ix, txt))
            elif matched_by + 192 <= cy <= matched_by + 232:
                # Style tag & attribute score glyphs sit at by+178..181 (bottom ~by+189); clothing name at by+194..217.
                name_frags.setdefault((matched_by, col), []).append((ix, txt))

        for by, cols in chunk:
            for col in cols:
                lv_txt = "".join(t for _, t in sorted(lv_frags.get((by, col), []), key=lambda p: p[0]))
                level_by_cell[(by, col)] = parse_rejuv_level(lv_txt)
                nm_txt = "".join(t for _, t in sorted(name_frags.get((by, col), []), key=lambda p: p[0]))
                name_by_cell[(by, col)] = nm_txt

    out: dict[tuple[int, int], tuple[str, int | None, bool]] = {}
    for by, cols in rows:
        for col in cols:
            bx = FIRST_X + col * COL_PITCH
            renewed = has_renewed_star(frame, bx, by)
            lv = RENEWED_LEVEL if renewed else level_by_cell.get((by, col))
            if lv is None:
                lv_raw = _read_single_ocr(rt, frame, [bx + 18, by + 118, 114, 36], r"(?:MAX|等级\d+|\d+)", threshold=0.55)
                lv = parse_rejuv_level(lv_raw)
            nm = name_by_cell.get((by, col), "")
            if not clean_clothing_name(nm):
                nm = _read_single_ocr(rt, frame, [bx - 4, by + 193, 158, 34], ".+", threshold=0.45)
            out[(by, col)] = (nm, lv, renewed)
    return out


def _same_rejuv_grid(
    first: np.ndarray | None,
    second: np.ndarray | None,
    occupied_rows: list[tuple[int, int]] | None = None,
) -> bool:
    if first is None or second is None or first.shape != second.shape:
        return False
    # Compare actual occupied card artwork bands using zero-shift normalized correlation
    # so live translucent 3D background motion never breaks top/bottom stillness checks.
    if occupied_rows:
        scores: list[float] = []
        f_gray = cv2.cvtColor(first, cv2.COLOR_BGR2GRAY)
        s_gray = cv2.cvtColor(second, cv2.COLOR_BGR2GRAY)
        picks = occupied_rows if len(occupied_rows) <= 3 else [
            occupied_rows[0], occupied_rows[len(occupied_rows) // 2], occupied_rows[-1]
        ]
        for by, n_cols in picks:
            y0, y1 = max(GRID_TOP + 6, by + 14), min(first.shape[0] - 20, by + 150)
            x0, x1 = FIRST_X + 8, min(first.shape[1] - 20, FIRST_X + max(1, n_cols) * COL_PITCH - 24)
            if y1 - y0 < 60 or x1 - x0 < 80:
                continue
            p1 = f_gray[y0:y1, x0:x1]
            s_y0, s_y1 = max(0, y0 - 2), min(second.shape[0], y1 + 2)
            p2 = s_gray[s_y0:s_y1, x0:x1]
            if float(np.std(p1)) < 5.0 or p2.shape[0] < p1.shape[0]:
                continue
            sc = float(np.max(cv2.matchTemplate(p2, p1, cv2.TM_CCOEFF_NORMED)))
            scores.append(sc)
        if scores:
            return float(np.median(scores)) >= 0.94

    y1 = max(GRID_TOP + 20, first.shape[0] - 24)
    diff = cv2.absdiff(first[GRID_TOP + 6:y1, 168:1224], second[GRID_TOP + 6:y1, 168:1224])
    return float(np.mean(diff)) < 1.5 or float(np.mean(diff > 24)) < 0.015


def _park_point(rt):
    hwnd, cap_size = rt._pointer_guard_context()
    return clamp_capture_park_point(hwnd, [640, 36], capture_size=cap_size)


def _scroll_rejuv_and_park(rt, dy: int, wait_seconds: float) -> np.ndarray | None:
    if (not rt.action("TouchMove", target=GRID_SCROLL_TARGET)
            or not rt.pause(0.1)
            or not rt.action("Scroll", target=GRID_SCROLL_TARGET, dx=0, dy=dy)
            or not rt.pause(wait_seconds)
            or not rt.action("TouchMove", target=_park_point(rt))
            or not rt.pause(0.12)):
        return None
    return rt.capture()


def _detect_occupied_rejuv_rows(frame: np.ndarray, row_ys: list[int]) -> list[tuple[int, list[int]]]:
    """Return [(by, present_cols)] for rows with visible cards before empty space.

    Check every column in a row rather than stopping at the first dim slot so a single
    mid-FadeIn card in col 0..4 cannot truncate the rest of the row or lower rows,
    while still stopping after a clean left-aligned partial last row so empty shimmering
    3D background rows below the list end are never mistaken for cards.
    """
    rows: list[tuple[int, list[int]]] = []
    for by in row_ys:
        present = [
            col for col in range(COLS)
            if is_card_present(frame, FIRST_X + col * COL_PITCH, by)
        ]
        if not present:
            if rows:
                break
            continue
        rows.append((by, present))
        if len(present) < COLS and present == list(range(len(present))):
            break
    return rows


def _scroll_rejuv_to_top(rt) -> np.ndarray | None:
    frame = rt.capture()
    if frame is None:
        return None
    scroll_dy = 24000 if frame.shape[0] > 1200 else 14400
    for _ in range(12):
        if rt.stopped or frame is None:
            return None
        grid_bottom = frame.shape[0] - GRID_BOTTOM_MARGIN
        row_ys = detect_rejuv_rows(frame, grid_top=GRID_TOP, grid_bottom=grid_bottom)
        occ = [(by, len(cols)) for by, cols in _detect_occupied_rejuv_rows(frame, row_ys)]
        after = _scroll_rejuv_and_park(rt, scroll_dy, 0.28)
        if after is None:
            return None
        if _same_rejuv_grid(frame, after, occ):
            return after
        frame = after
    return frame


def _is_card_artwork_present(frame: np.ndarray, bx: int, by: int, grid_bottom: int) -> bool:
    y1 = min(frame.shape[0], grid_bottom, by + 146)
    if (
        by < GRID_TOP
        or y1 - (by + 16) < 72
        or bx < 0
        or bx + CARD_W > frame.shape[1]
    ):
        return False
    body = frame[by + 16:y1, bx + 12:bx + 138]
    gray = cv2.cvtColor(body, cv2.COLOR_BGR2GRAY)
    if float(np.std(gray)) < 9.0:
        return False
    ig = gray.astype(np.int16)
    dx = np.abs(np.diff(ig, axis=1))[:-1, :]
    dy = np.abs(np.diff(ig, axis=0))[:, :-1]
    return float(np.mean((dx + dy) > 14)) >= 0.005


def _build_scroll_anchors(
    frame: np.ndarray,
    occ_rows: list[tuple[int, list[int]]],
    grid_bottom: int,
) -> list[tuple[int, int]]:
    anchors = [(by, len(cols)) for by, cols in occ_rows]
    if occ_rows and len(occ_rows[-1][1]) == COLS:
        next_by = int(round(occ_rows[-1][0] + ROW_PITCH_EXACT))
        if next_by + 96 <= min(frame.shape[0], grid_bottom):
            extra_cols = 0
            for col in range(COLS):
                bx = FIRST_X + col * COL_PITCH
                if not _is_card_artwork_present(frame, bx, next_by, grid_bottom):
                    break
                extra_cols += 1
            if extra_cols > 0:
                anchors.append((next_by, extra_cols))
    return anchors


def _measure_rejuv_scroll_delta(
    before: np.ndarray,
    after: np.ndarray,
    occupied_rows: list[tuple[int, int]],
) -> int | None:
    if before is None or after is None or before.shape != after.shape or not occupied_rows:
        return None
    grid_bottom = before.shape[0] - GRID_BOTTOM_MARGIN
    anchors = [(cy, n_cols) for cy, n_cols in reversed(occupied_rows) if cy >= GRID_TOP][:4]
    b_gray = cv2.cvtColor(before, cv2.COLOR_BGR2GRAY)
    a_gray = cv2.cvtColor(after, cv2.COLOR_BGR2GRAY)
    votes: list[int] = []
    for cy, n_cols in anchors:
        x0 = FIRST_X + 10
        x1 = min(before.shape[1] - 10, FIRST_X + max(1, n_cols) * COL_PITCH - 24)
        bands = [(cy + 16, min(grid_bottom, cy + 146))]
        if cy + 186 <= grid_bottom:
            bands.append((cy + 86, cy + 186))
        for t0, t1 in bands:
            if t1 - t0 < 72:
                continue
            tmpl = b_gray[t0:t1, x0:x1]
            if tmpl.shape[1] < 80 or float(np.std(tmpl)) < 4.0:
                continue
            low = GRID_TOP
            high = min(t0 + 12, grid_bottom - tmpl.shape[0])
            if high < low + 8:
                continue
            search = a_gray[low:high + tmpl.shape[0], x0:x1]
            res = cv2.matchTemplate(search, tmpl, cv2.TM_CCOEFF_NORMED).ravel()
            best_idx = int(np.argmax(res))
            if float(res[best_idx]) >= 0.86:
                votes.append(t0 - (low + best_idx))
                break
    if not votes:
        return None
    delta = int(round(float(np.median(votes))))
    return delta if delta >= 0 else None


class RenewalScanner:
    """Scanner for 美鸭梨 -> 服装焕新, syncing both wardrobe ownership and levels (0..11)."""

    INCREMENTAL_STOP_STREAK = 12

    def __init__(self, rt, account: AccountState | None = None, *, mode: str = "full"):
        self.rt = rt
        self.account = account or AccountState.for_runtime(ROOT, rt)
        self.mode = "incremental" if mode == "incremental" else "full"
        from data_paths import data_path
        self.crafting, self.scoring = load_catalogs()
        self.model = _load_rejuvenate_model(str(self.scoring.get("version") or ""))
        self.wb_catalog = WardrobeCatalog(data_path("resource/model/wardrobe"), data_path("resource/data/wardrobe_scoring.json"))
        self._started_at = datetime.now(timezone.utc).isoformat()
        self.summary: dict = {
            "complete": False,
            "mode": self.mode,
            "tall_mode": False,
            "scanned_cards": 0,
            "matched_groups": 0,
            "added_items": 0,
            "updated_levels": 0,
            "unmatched_count": 0,
            "unmatched_samples": [],
            "parts": {},
        }
        self.scanned_levels: dict[str, int] = {}
        self.scanned_renewed: dict[str, bool] = {}
        self._last_scan_frame: np.ndarray | None = None

    def _owned_groups(self, role: dict) -> set[str]:
        snapshot = wardrobe_snapshot(role)
        game_items = self.scoring.get("game_items") or {}
        owned: set[str] = set()
        for state in (snapshot.get("parts") or {}).values():
            if not isinstance(state, dict):
                continue
            for row in state.get("items") or []:
                for iid in row.get("item_ids") or []:
                    meta = game_items.get(str(iid))
                    if meta and meta.get("group"):
                        owned.add(str(meta["group"]))
        return owned

    def open(self) -> bool:
        frame = self.rt.capture()
        if self.rt.hit("MaaNikki_RejuvenateReady", image=frame):
            return True
        if self.rt.ui.find_menu_entry(node="MaaNikki_Rejuvenate_Entrance", scroll=True):
            return True
        if not self.rt.ui.menu():
            return False
        frame = self.rt.capture()
        for arrow in (PEAR_PAL_PREV_PAGE, PEAR_PAL_PREV_PAGE, PEAR_PAL_NEXT_PAGE, PEAR_PAL_NEXT_PAGE):
            if self.rt.hit("MaaNikki_Rejuvenate_Entrance", image=frame):
                break
            if not self.rt.action("Click", target=arrow) or not self.rt.pause(0.45):
                return False
            frame = self.rt.capture()
            if not self.rt.hit("MaaNikki_MenuReady", image=frame):
                return self.rt.hit("MaaNikki_RejuvenateReady", image=frame)
        if not self.rt.hit("MaaNikki_Rejuvenate_Entrance", image=frame):
            return False
        return self.rt.ui.enter_page(
            lambda: self.rt.click_template("MaaNikki_Rejuvenate_Entrance", verify=False),
            "MaaNikki_RejuvenateReady",
            source="MaaNikki_MenuReady",
        )

    def _is_list_view(self, frame: np.ndarray | None) -> bool:
        """True when frame shows the 服装焕新 grid list rather than a suit/piece detail subpage."""
        if frame is None:
            return False
        cid, _yc, _sc = self.model.detect_active_category(frame)
        if cid is not None:
            return True
        sort_text = clean_clothing_name(self.rt.text(SORT_BOX_ROI, image=frame))
        return bool(re.search(r"获得时间|品质|分数|等级|推荐|默认", sort_text))

    def ensure_list_view(self) -> bool:
        """Step back via Esc when 服装焕新 opened or drifted onto a suit/piece detail subpage."""
        frame = self.rt.capture()
        if self._is_list_view(frame):
            return True
        if not self.rt.pause(0.35):
            return False
        frame = self.rt.capture()
        if self._is_list_view(frame):
            return True
        for _ in range(2):
            if self.rt.stopped:
                return False
            if frame is not None and self.rt.hit("MaaNikki_RejuvenateReady", image=frame):
                self.rt.key(27, delay=0.5)  # VK_ESCAPE
                frame = self.rt.capture()
                if self._is_list_view(frame):
                    self.rt.log("焕新读取：已从详情子页返回服装焕新列表。", detail=True)
                    return True
                continue
            if not self.open():
                return False
            frame = self.rt.capture()
            if self._is_list_view(frame):
                return True
        return self._is_list_view(self.rt.capture())

    def _filter_modal_open(self, frame: np.ndarray | None) -> bool:
        if frame is None:
            return False
        return bool(self.rt.text(FILTER_RESET_ROI, r"^重置$", image=frame))

    def _wait_filter_modal_closed(self, seconds: float = 1.5) -> bool:
        from task_control import task_time
        deadline = task_time.monotonic() + seconds
        while not self.rt.stopped:
            frame = self.rt.capture()
            if not self._filter_modal_open(frame):
                return True
            if task_time.monotonic() >= deadline:
                return False
            if not self.rt.pause(0.25):
                return False
        return False

    def clear_filter(self) -> None:
        if not self.rt.action("Click", target=FILTER_BTN) or not self.rt.pause(0.35):
            return
        frame = self.rt.capture()
        if not self._filter_modal_open(frame):
            if not self.rt.pause(0.25):
                return
            frame = self.rt.capture()
        if not self._filter_modal_open(frame):
            return

        if not self.rt.click_text(r"^重置$", FILTER_RESET_ROI):
            frame = self.rt.capture()
            if self._filter_modal_open(frame):
                self.rt.action("Click", target=FILTER_RESET)
                self.rt.pause(0.25)

        frame = self.rt.capture()
        if self._filter_modal_open(frame):
            # Target bottom button ROI so the top modal header ("筛选") is never matched first.
            # Never issue a second blind click at FILTER_CONFIRM ([734, 512]): once the modal
            # closes, that coordinate lands directly on a Row-2 suit card and enters suit detail.
            if not self.rt.click_text(r"^(筛选|确认|确定|应用)$", FILTER_CONFIRM_ROI):
                frame = self.rt.capture()
                if self._filter_modal_open(frame):
                    self.rt.action("Click", target=FILTER_CONFIRM)
                    self.rt.pause(0.35)
            if not self._wait_filter_modal_closed(1.5):
                self.rt.key(27, delay=0.35)  # VK_ESCAPE
        self.rt.action("TouchMove", target=_park_point(self.rt))
        self.rt.pause(0.25)

    @staticmethod
    def _sort_arrow_descending(frame: np.ndarray | None) -> tuple[bool | None, list[int]]:
        """Return (is_descending, click_center) for the sort direction arrows on 服装焕新."""
        default_pt = [1172, 56]
        if frame is None:
            return None, default_pt
        x0, y0, w, h = SORT_DIR_ROI
        patch = frame[y0:y0 + h, x0:x0 + w]
        if patch.size == 0:
            return None, default_pt
        gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
        ys, xs = np.where(gray > 195)
        if len(xs) < 8:
            return None, default_pt
        ax0, ax1 = int(xs.min()), int(xs.max())
        ay0, ay1 = int(ys.min()), int(ys.max())
        # When spread across both icons or single arrow inside SORT_DIR_ROI:
        # split the icon box [ax0-22 .. ax1+22] around center [w//2]: right (↓) bright = descending.
        mid_x = w // 2
        left_bright = int(np.sum(gray[:, :mid_x] > 190))
        right_bright = int(np.sum(gray[:, mid_x:] > 190))
        click_pt = [x0 + mid_x, y0 + max(10, min(h - 10, (ay0 + ay1) // 2))]
        if right_bright > left_bright + 8:
            return True, click_pt
        if left_bright > right_bright + 8:
            return False, click_pt
        return None, click_pt

    def ensure_sort_acquired_desc(self) -> None:
        """Switch 服装焕新 sort order to 获得时间 descending (newest clothes first)."""
        frame = self.rt.capture()
        current_sort = clean_clothing_name(self.rt.text(SORT_BOX_ROI, image=frame)) if frame is not None else ""
        if "获得时间" not in current_sort:
            res = self.rt.recognize("MaaNikki_Daily_OCR", {"roi": SORT_BOX_ROI, "expected": ".+"}, image=frame)
            if res and res.hit and res.best_result is not None:
                bx, by, bw, bh = list(res.best_result.box)
                dropdown_pt = [bx + bw // 2, by + bh // 2]
            else:
                dropdown_pt = [990, 52]
            if not self.rt.action("Click", target=dropdown_pt) or not self.rt.pause(0.35):
                raise RuntimeError("未能展开服装焕新排序菜单")
            if not self.rt.click_text(r"^获得时间$", SORT_MENU_ROI):
                self.rt.action("Click", target=[950, 96])
            self.rt.pause(0.35)
            self.rt.action("TouchMove", target=_park_point(self.rt))
            self.rt.pause(0.3)
            frame = self.rt.capture()
            current_sort = clean_clothing_name(self.rt.text(SORT_BOX_ROI, image=frame)) if frame is not None else ""
            if "获得时间" not in current_sort:
                raise RuntimeError("未能将服装焕新切换为按“获得时间”排序")

        is_desc, dir_pt = self._sort_arrow_descending(frame)
        if is_desc is False:
            if not self.rt.action("Click", target=dir_pt) or not self.rt.pause(0.35):
                raise RuntimeError("未能切换服装焕新排序方向")
            self.rt.action("TouchMove", target=_park_point(self.rt))
            self.rt.pause(0.25)
            after = self.rt.capture()
            is_desc_after, _ = self._sort_arrow_descending(after)
            if is_desc_after is False:
                raise RuntimeError("未能确认服装焕新为“获得时间降序”")

    def _reset_rail_top(self) -> tuple[np.ndarray | None, str | None, int | None]:
        frame = self.rt.capture()
        if frame is None:
            # Allow a brief settle window when falling back to 16:9 after a failed tall-mode resize.
            for _ in range(5):
                if self.rt.stopped or not self.rt.pause(0.25):
                    return None, None, None
                frame = self.rt.capture()
                if frame is not None:
                    break
        # 8 major tabs + 15 expanded accessory subcategories = 23 slots total.
        for _ in range(26):
            if self.rt.stopped or frame is None:
                return None, None, None
            cid, yc, _sc = self.model.detect_active_category(frame)
            if cid == "299":
                return frame, cid, yc
            self.rt.key(87, delay=0.25)  # VK_W
            after = self.rt.capture()
            if after is not None:
                diff_y1 = min(frame.shape[0] - RAIL_MINOR_BOTTOM_MARGIN, after.shape[0] - RAIL_MINOR_BOTTOM_MARGIN, 1150)
                if diff_y1 > RAIL_TOP_Y + 40:
                    rail_diff = float(np.mean(cv2.absdiff(
                        frame[RAIL_TOP_Y:diff_y1, RAIL_GLOW_X0:RAIL_GLOW_X1],
                        after[RAIL_TOP_Y:diff_y1, RAIL_GLOW_X0:RAIL_GLOW_X1],
                    )))
                    if rail_diff < 1.0 and cid is not None and cid != "acc":
                        break
            frame = after
        self.rt.action("Click", target=RAIL_TOP_SLOT)
        self.rt.pause(0.3)
        frame = self.rt.capture()
        cid, yc, _sc = self.model.detect_active_category(frame)
        return frame, cid, yc

    def _step_rail_down(self, prev_frame: np.ndarray, prev_cid: str | None, prev_yc: int | None) -> tuple[np.ndarray | None, str | None, int | None, bool]:
        """Step one slot down the left category rail using [S] hotkey with click fallback."""
        if prev_frame is None or self.rt.stopped:
            return None, None, None, False
        self.rt.key(83, delay=0.22)  # VK_S
        after = self.rt.capture()
        if after is not None:
            cid, yc, _sc = self.model.detect_active_category(after)
            # Treat unresolved 'acc' as mid-expansion/animation and wait briefly for the active minor slot.
            if cid is not None and cid != "acc" and cid != prev_cid:
                return after, cid, yc, True
            if cid is not None and cid != "acc" and yc is not None and prev_yc is not None and abs(yc - prev_yc) > 12:
                return after, cid, yc, True
            for wait_s in (0.16, 0.18):
                if not self.rt.pause(wait_s):
                    break
                after_retry = self.rt.capture()
                if after_retry is None:
                    break
                after = after_retry
                cid, yc, _sc = self.model.detect_active_category(after_retry)
                if cid is not None and cid != "acc" and cid != prev_cid:
                    return after_retry, cid, yc, True
                if cid is not None and cid != "acc" and yc is not None and prev_yc is not None and abs(yc - prev_yc) > 12:
                    return after_retry, cid, yc, True
            if cid == "acc" and prev_cid != "acc":
                return after, cid, yc, True

        # Fallback: click the next visible slot directly below the active highlight inside clickable work area.
        # Never click below 'acc' here: when already inside an expanded accessory sub-list, clicking
        # acc_yc + 70 jumps back to Slot #1 and loops over already-visited subcategories.
        if prev_yc is not None and prev_cid != "acc":
            try:
                safe_min_y, safe_max_y = safe_capture_y_range(
                    window_hwnd(self.rt),
                    prev_frame.shape[0],
                    capture_width=prev_frame.shape[1],
                    margin_px=8,
                )
            except OSError:
                safe_min_y, safe_max_y = 0, prev_frame.shape[0]
            pitch = 60 if prev_cid in ACCESSORY_PARTS else 70
            cand_y = min(prev_frame.shape[0] - 32, safe_max_y - 12, prev_yc + pitch)
            if cand_y >= safe_min_y + 12 and cand_y > prev_yc + 18:
                self.rt.action("Click", target=[RAIL_CENTER_X, cand_y])
                self.rt.pause(0.28)
                after2 = self.rt.capture()
                if after2 is not None:
                    cid2, yc2, _sc2 = self.model.detect_active_category(after2)
                    if (cid2 is not None and cid2 != prev_cid) or not _same_rejuv_grid(prev_frame, after2):
                        return after2, cid2, yc2, True
        return (after if after is not None else prev_frame), prev_cid, prev_yc, False

    def _category_expects_items(self, cid: str, owned_groups: set[str]) -> bool:
        """True when the account already owns at least one clothing group in this category."""
        if not owned_groups:
            return False
        for grp in owned_groups:
            meta = self.model.group_meta.get(grp)
            if not meta:
                continue
            part = meta.get("part")
            if cid == "acc":
                if part in ACCESSORY_PARTS:
                    return True
            elif part == cid:
                return True
        return False

    def _detect_grid_cards_with_retry(
        self,
        frame: np.ndarray,
        *,
        prev_grid_frame: np.ndarray | None = None,
        max_wait_seconds: float = 1.5,
        poll_interval: float = 0.15,
    ) -> tuple[np.ndarray, list[int], list[tuple[int, list[int]]], bool]:
        """Detect visible card rows, retrying briefly only when the grid is empty, stale, or still populating.

        Returns (frame, row_ys, occ_rows, stale_unchanged).
        Adds zero extra wait when cards are already loaded and occupy the full viewport.
        """
        from task_control import task_time

        def _eval(cur: np.ndarray) -> tuple[list[int], list[tuple[int, list[int]]], bool]:
            cur_bottom = cur.shape[0] - GRID_BOTTOM_MARGIN
            r_ys = detect_rejuv_rows(cur, grid_top=GRID_TOP, grid_bottom=cur_bottom)
            occ = _detect_occupied_rejuv_rows(cur, r_ys) if r_ys else []
            stale = bool(
                prev_grid_frame is not None
                and occ
                and _same_rejuv_grid(cur, prev_grid_frame, [(by, len(cols)) for by, cols in occ])
            )
            return r_ys, occ, stale

        def _card_count(occ: list[tuple[int, list[int]]]) -> int:
            return sum(len(cols) for _, cols in occ)

        row_ys, occ_rows, stale = _eval(frame)
        waited_from_unready = False
        deadline = task_time.monotonic() + max_wait_seconds

        # Only poll when 0 occupied cards are visible or the grid still shows the previous category.
        while (not occ_rows or stale) and not self.rt.stopped:
            if task_time.monotonic() >= deadline:
                break
            if not self.rt.pause(poll_interval):
                break
            fresh = self.rt.capture()
            if fresh is None:
                break
            waited_from_unready = True
            frame = fresh
            row_ys, occ_rows, stale = _eval(frame)

        # If cards just appeared from an empty/stale grid or look partial/gapped, confirm after short
        # settle ticks so staggered FadeIn animations and async streaming rows finish populating.
        def _has_row_gap(occ: list[tuple[int, list[int]]]) -> bool:
            for idx_r, (_by, cols) in enumerate(occ):
                if not cols:
                    continue
                if len(cols) < max(cols) + 1:
                    return True
                if idx_r < len(occ) - 1 and len(cols) < COLS:
                    return True
            return False

        if occ_rows and not stale and not self.rt.stopped:
            has_gap = _has_row_gap(occ_rows)
            is_partial = has_gap or len(occ_rows[-1][1]) < COLS or len(occ_rows) < len(row_ys)
            if waited_from_unready or is_partial:
                prev_count = _card_count(occ_rows)
                for settle_step in range(4):
                    if self.rt.stopped or (settle_step > 0 and task_time.monotonic() >= deadline):
                        break
                    if not self.rt.pause(poll_interval):
                        break
                    fresh = self.rt.capture()
                    if fresh is None:
                        break
                    r2, o2, s2 = _eval(fresh)
                    if s2 or not o2:
                        break
                    new_count = _card_count(o2)
                    if new_count >= prev_count:
                        frame, row_ys, occ_rows = fresh, r2, o2
                    if new_count <= prev_count and not (_has_row_gap(occ_rows) and settle_step < 2):
                        break
                    prev_count = new_count

        return frame, row_ys, occ_rows, stale

    def _scan_category(
        self,
        cid: str,
        is_tall: bool,
        owned_groups: set[str],
        prev_levels: dict[str, int] | None = None,
        *,
        incremental: bool = False,
        initial_frame: np.ndarray | None = None,
        prev_grid_frame: np.ndarray | None = None,
        assume_at_top: bool = False,
        verify_sort: bool = False,
    ) -> bool:
        expects_items = self._category_expects_items(cid, owned_groups)
        page0_wait = (
            2.4
            if (expects_items or (not incremental and cid in self.model.non_skip_majors))
            else 1.2
        )
        frame = initial_frame if (assume_at_top and initial_frame is not None) else None
        prefetched_rows: list[int] = []
        prefetched_occ: list[tuple[int, list[int]]] = []
        page0_stale = False

        if frame is not None:
            if verify_sort:
                cur_sort = clean_clothing_name(self.rt.text(SORT_BOX_ROI, image=frame))
                if "获得时间" not in cur_sort:
                    self.ensure_sort_acquired_desc()
                    frame = None
                    verify_sort = False
            if frame is not None:
                frame, prefetched_rows, prefetched_occ, page0_stale = self._detect_grid_cards_with_retry(
                    frame,
                    prev_grid_frame=prev_grid_frame,
                    max_wait_seconds=page0_wait,
                )
                # Only fall back to scroll-to-top when loaded cards are actually scrolled past the top margin.
                if prefetched_occ and not page0_stale and prefetched_occ[0][0] > GRID_TOP + 55:
                    frame = None
                    prefetched_rows = []
                    prefetched_occ = []
                    page0_stale = False

        if frame is None:
            frame = _scroll_rejuv_to_top(self.rt)
            if frame is None:
                return False
            if verify_sort:
                cur_sort = clean_clothing_name(self.rt.text(SORT_BOX_ROI, image=frame))
                if "获得时间" not in cur_sort:
                    self.ensure_sort_acquired_desc()
                    frame = _scroll_rejuv_to_top(self.rt)
                    if frame is None:
                        return False
            frame, prefetched_rows, prefetched_occ, page0_stale = self._detect_grid_cards_with_retry(
                frame,
                prev_grid_frame=prev_grid_frame,
                max_wait_seconds=page0_wait,
            )

        self._last_scan_frame = frame
        grid_bottom = frame.shape[0] - GRID_BOTTOM_MARGIN
        scroll_y = 0
        first_row_y: int | None = None
        matched_slots: set[tuple[int, int]] = set()
        # (row_idx, col) -> (last_by, raw_name, lv) so slots faded by RetainerBox_0's BottomMask
        # or mid-FadeIn can be re-read cleanly once scrolled away from the viewport edge.
        unmatched_slots: dict[tuple[int, int], tuple[int, str, int | None]] = {}
        matched_groups_in_cat: set[str] = set()
        renewed_slots: set[tuple[int, int]] = set()
        known_tail_streak = 0
        complete = False
        prev_map = prev_levels or {}
        last_occ_rows: list[tuple[int, list[int]]] = []

        def _row_cols_to_scan(occ_rows_list: list[tuple[int, list[int]]]) -> list[tuple[int, int, list[int]]]:
            """Expand any interior mid-FadeIn gaps (cols < max_col or rows above non-final rows)."""
            out_rows: list[tuple[int, int, list[int]]] = []
            for idx_r, (by, present_cols) in enumerate(occ_rows_list):
                if not present_cols:
                    continue
                row_idx = int(round((by + scroll_y - first_row_y) / ROW_PITCH_EXACT))
                if row_idx < 0:
                    continue
                max_col = COLS - 1 if idx_r < len(occ_rows_list) - 1 else max(present_cols)
                out_rows.append((by, row_idx, list(range(max_col + 1))))
            return out_rows

        def _process_ocr_rows(
            src_frame: np.ndarray,
            read_spec: list[tuple[int, int, list[int]]],
            *,
            allow_incremental_stop: bool,
        ) -> bool:
            nonlocal known_tail_streak, complete
            if not read_spec:
                return False
            batch_step = 2 if (incremental and allow_incremental_stop) else len(read_spec)
            for start in range(0, len(read_spec), batch_step):
                if self.rt.stopped:
                    return False
                chunk = read_spec[start:start + batch_step]
                parsed = _batch_read_rows_ocr(self.rt, src_frame, [(by, cols) for by, _ri, cols in chunk])
                for by, row_idx, cols in chunk:
                    for col in cols:
                        item = parsed.get((by, col))
                        if item is None:
                            continue
                        raw_name, lv, renewed = item
                        slot_key = (row_idx, col)
                        if lv is None and not clean_clothing_name(raw_name):
                            continue
                        if lv is None:
                            if slot_key not in matched_slots:
                                unmatched_slots[slot_key] = (by, raw_name, None)
                            known_tail_streak = 0
                            continue
                        groups = self.model.lookup_groups(cid, raw_name)
                        if not groups:
                            if slot_key not in matched_slots:
                                unmatched_slots[slot_key] = (by, raw_name, lv)
                            known_tail_streak = 0
                            continue
                        unmatched_slots.pop(slot_key, None)
                        matched_slots.add(slot_key)
                        owned_hits = [g for g in groups if g in owned_groups]
                        targets = owned_hits if owned_hits else [groups[0]]
                        is_renewed = lv == RENEWED_LEVEL or renewed
                        was_known = all(g in owned_groups and prev_map.get(g) == lv for g in targets)
                        for grp in targets:
                            self.scanned_levels[grp] = lv
                            self.scanned_renewed[grp] = is_renewed
                            matched_groups_in_cat.add(grp)
                        if is_renewed:
                            renewed_slots.add(slot_key)
                        if incremental and allow_incremental_stop and was_known:
                            known_tail_streak += 1
                            if known_tail_streak >= self.INCREMENTAL_STOP_STREAK:
                                complete = True
                                return True
                        else:
                            known_tail_streak = 0
            return False

        max_pages = 64 if is_tall else 400
        page = 0
        page0_ocr_retries = 0
        while page < max_pages:
            if self.rt.stopped:
                break
            if page == 0 and page0_ocr_retries == 0:
                row_ys = prefetched_rows
                occ_rows = [] if page0_stale else prefetched_occ
            else:
                frame, row_ys, occ_rows, page0_stale = self._detect_grid_cards_with_retry(
                    frame,
                    prev_grid_frame=prev_grid_frame if page == 0 else None,
                    max_wait_seconds=1.1,
                )
                self._last_scan_frame = frame
                grid_bottom = frame.shape[0] - GRID_BOTTOM_MARGIN
                if page == 0 and page0_stale:
                    occ_rows = []
            if not row_ys or not occ_rows:
                complete = page == 0 and not expects_items and not page0_stale
                break
            last_occ_rows = occ_rows
            if first_row_y is None:
                first_row_y = row_ys[0]
            occ_anchors = _build_scroll_anchors(frame, occ_rows, grid_bottom)
            # Short-circuit list end on page 0 when empty rows exist below the list, or on page >= 1
            # when the last row has <= 4 clean left-aligned cards (at least 2 trailing columns empty,
            # which vertical RetainerBox_0 BottomMask cannot cause while leaving earlier cols clear).
            # Keep scroll confirmation when 5 or 6 columns are filled so a single masked/fading
            # rightmost card (col 5) never truncates remaining pages.
            last_cols = occ_rows[-1][1]
            clean_short_tail = (
                len(last_cols) <= COLS - 2
                and last_cols == list(range(len(last_cols)))
            )
            reached_list_end = (
                (page == 0 and len(occ_rows) < len(row_ys))
                or (page >= 1 and clean_short_tail)
            )

            # On full tall pages that will scroll down, defer the bottom-most row (which sits inside
            # RetainerBox_0's BottomMask) so it is OCR'd once cleanly after scrolling up instead of
            # paying slow single-card fallback OCR twice. If scrolling reveals we are already at the
            # bottom, OCR that deferred row immediately before exiting.
            defer_bottom = (
                is_tall
                and not incremental
                and not reached_list_end
                and len(occ_rows) == len(row_ys)
                and len(occ_rows) >= 4
            )
            scan_occ_rows = occ_rows[:-1] if defer_bottom else occ_rows
            deferred_occ_rows = occ_rows[-1:] if defer_bottom else []

            def _build_read_spec(target_occ: list[tuple[int, list[int]]]) -> list[tuple[int, int, list[int]]]:
                spec: list[tuple[int, int, list[int]]] = []
                for by, row_idx, scan_cols in _row_cols_to_scan(target_occ):
                    cols_needed: list[int] = []
                    for col in scan_cols:
                        slot_key = (row_idx, col)
                        if slot_key in matched_slots:
                            continue
                        prev_unmatched = unmatched_slots.get(slot_key)
                        if prev_unmatched is not None and abs(by - prev_unmatched[0]) < 40:
                            continue
                        cols_needed.append(col)
                    if cols_needed:
                        spec.append((by, row_idx, cols_needed))
                return spec

            rows_to_read = _build_read_spec(scan_occ_rows)
            hit_tail = _process_ocr_rows(frame, rows_to_read, allow_incremental_stop=True)
            readable_count = len(matched_slots) + len(unmatched_slots)
            if page == 0 and rows_to_read and readable_count == 0:
                if page0_ocr_retries < 3 and not self.rt.stopped:
                    page0_ocr_retries += 1
                    if not self.rt.pause(0.2):
                        break
                    fresh = self.rt.capture()
                    if fresh is None:
                        break
                    frame = fresh
                    self._last_scan_frame = frame
                    grid_bottom = frame.shape[0] - GRID_BOTTOM_MARGIN
                    first_row_y = None
                    matched_slots.clear()
                    unmatched_slots.clear()
                    matched_groups_in_cat.clear()
                    renewed_slots.clear()
                    continue
                complete = not expects_items
                break
            if hit_tail:
                break
            if reached_list_end:
                complete = True
                break

            step_dy = -9600 if is_tall else -480
            partial_bottom = len(last_cols) < COLS
            moved = False
            for attempt_dy in (step_dy, step_dy // 2, step_dy // 4):
                after = _scroll_rejuv_and_park(self.rt, attempt_dy, 0.28)
                if after is None:
                    break
                same = _same_rejuv_grid(frame, after, occ_anchors)
                delta = 0 if same else _measure_rejuv_scroll_delta(frame, after, occ_anchors)
                if same or delta == 0:
                    if partial_bottom:
                        complete = True
                        moved = True
                        break
                    after = _scroll_rejuv_and_park(self.rt, attempt_dy, 0.28)
                    if after is None:
                        break
                    same = _same_rejuv_grid(frame, after, occ_anchors)
                    delta = 0 if same else _measure_rejuv_scroll_delta(frame, after, occ_anchors)
                    if same or delta == 0:
                        complete = True
                        moved = True
                        break
                if delta is not None and delta > 0:
                    scroll_y += delta
                    frame = after
                    self._last_scan_frame = after
                    moved = True
                    break
                undo = _scroll_rejuv_and_park(self.rt, -attempt_dy, 0.28)
                if undo is None:
                    break
                frame = undo
                self._last_scan_frame = undo
                undo_rows = detect_rejuv_rows(frame, grid_top=GRID_TOP, grid_bottom=grid_bottom)
                if undo_rows:
                    undo_occ = _detect_occupied_rejuv_rows(frame, undo_rows)
                    if undo_occ:
                        occ_anchors = _build_scroll_anchors(frame, undo_occ, grid_bottom)
            if complete or not moved:
                if deferred_occ_rows and not self.rt.stopped:
                    _process_ocr_rows(frame, _build_read_spec(deferred_occ_rows), allow_incremental_stop=False)
                break
            page += 1

        # If any cells on the final visible viewport remain unmatched (e.g. staggered FadeIn on the
        # last page), give them one brief settle capture and re-OCR only those cells.
        if unmatched_slots and last_occ_rows and first_row_y is not None and not self.rt.stopped:
            retry_spec: list[tuple[int, int, list[int]]] = []
            for by, row_idx, scan_cols in _row_cols_to_scan(last_occ_rows):
                cols_retry = [col for col in scan_cols if (row_idx, col) in unmatched_slots]
                if cols_retry:
                    retry_spec.append((by, row_idx, cols_retry))
            if retry_spec and self.rt.pause(0.35):
                settled = self.rt.capture()
                if settled is not None:
                    frame = settled
                    self._last_scan_frame = settled
                    _process_ocr_rows(settled, retry_spec, allow_incremental_stop=False)

        cat_cards = len(matched_slots) + len(unmatched_slots)
        cat_matched = len(matched_groups_in_cat)
        cat_unmatched = len(unmatched_slots)
        cat_renewed = len(renewed_slots)
        self.summary["scanned_cards"] += cat_cards
        self.summary["unmatched_count"] += cat_unmatched
        for _slot_key, (_u_by, raw_name, lv) in sorted(unmatched_slots.items()):
            if lv is not None and len(self.summary["unmatched_samples"]) < 20:
                self.summary["unmatched_samples"].append({
                    "part": cid, "name": raw_name, "level": lv,
                })

        self.summary["parts"][cid] = {
            "name": self.model.category_name(cid),
            "cards": cat_cards,
            "matched": cat_matched,
            "renewed": cat_renewed,
            "unmatched": cat_unmatched,
            "complete": complete,
        }
        self.summary["matched_groups"] = len(self.scanned_levels)
        return complete

    def _merge_wardrobe_inventory(self, data: dict, now: str) -> tuple[int, bool]:
        """Merge scanned clothes & renewed rewards into wardrobe.inventory / last_scan.

        Returns (added_row_count, snapshot_complete).
        """
        wardrobe = data.setdefault("wardrobe", {})
        prev_inv = wardrobe.get("inventory")
        prev_last = wardrobe.get("last_scan")
        game_items = self.scoring.get("game_items") or {}

        # Short-circuit before deepcopying multi-MB inventory JSON when incremental scan sees zero additions.
        if (
            self.mode == "incremental"
            and isinstance(prev_inv, dict)
            and prev_inv.get("complete")
            and isinstance(prev_inv.get("parts"), dict)
        ):
            inv_parts = prev_inv["parts"]
            inv_groups: dict[str, set[str]] = {}
            inv_ids: dict[str, set[int]] = {}
            for pcode_k, pstate in inv_parts.items():
                if not isinstance(pstate, dict):
                    continue
                gset: set[str] = set()
                iset: set[int] = set()
                for row in pstate.get("items") or []:
                    if not isinstance(row, dict):
                        continue
                    for iid in row.get("item_ids") or []:
                        iset.add(int(iid))
                        meta = game_items.get(str(iid))
                        if meta and meta.get("group"):
                            gset.add(str(meta["group"]))
                inv_groups[str(pcode_k)] = gset
                inv_ids[str(pcode_k)] = iset
            needs_merge = False
            for grp, is_renewed in self.scanned_renewed.items():
                meta = self.model.group_meta.get(grp)
                if not meta:
                    continue
                pcode = meta["part"]
                if grp not in inv_groups.get(pcode, ()) and meta["base_gid"] not in inv_ids.get(pcode, ()):
                    needs_merge = True
                    break
                reward_gid = meta.get("reward_gid")
                if is_renewed and reward_gid and reward_gid not in inv_ids.get(pcode, ()):
                    needs_merge = True
                    break
            if not needs_merge:
                p278_raw = inv_parts.get("278")
                if isinstance(p278_raw, dict) and p278_raw.get("complete"):
                    ids278 = inv_ids.get("278", set())
                    for entry in self.wb_catalog.infer_suit_handhelds(inv_parts):
                        iids = [int(x) for x in entry.get("item_ids") or []]
                        if iids and not any(i in ids278 for i in iids):
                            needs_merge = True
                            break
            if not needs_merge:
                return 0, True

        base = deepcopy(
            prev_inv
            if isinstance(prev_inv, dict) and isinstance(prev_inv.get("parts"), dict)
            else prev_last
            if isinstance(prev_last, dict) and isinstance(prev_last.get("parts"), dict)
            else None
        )
        if not isinstance(base, dict):
            base = {
                "schema_version": 2,
                "model_version": self.wb_catalog.version,
                "uid": self.account.uid,
                "label_fingerprint": self.wb_catalog.fingerprint,
                "sort": "rejuvenate_acquired_desc" if self.mode == "incremental" else "rejuvenate_full",
                "identity_policy": "compendium_ocr",
                "identity_verified": True,
                "scan_source": "rejuvenate",
                "started_at": self._started_at,
                "updated_at": now,
                "complete": False,
                "parts": {},
                "verified_parts": [],
                "skipped_categories": [],
            }
        parts: dict = base.setdefault("parts", {})

        # Index existing groups and item_ids per part to avoid duplicates.
        existing_groups_by_part: dict[str, set[str]] = {}
        existing_ids_by_part: dict[str, set[int]] = {}
        for part_code, pstate in parts.items():
            if not isinstance(pstate, dict):
                continue
            gset: set[str] = set()
            iset: set[int] = set()
            for row in pstate.get("items") or []:
                if not isinstance(row, dict):
                    continue
                for iid in row.get("item_ids") or []:
                    iset.add(int(iid))
                    meta = game_items.get(str(iid))
                    if meta and meta.get("group"):
                        gset.add(str(meta["group"]))
            existing_groups_by_part[str(part_code)] = gset
            existing_ids_by_part[str(part_code)] = iset

        def ensure_part(pcode: str) -> dict:
            pstate = parts.get(pcode)
            if not isinstance(pstate, dict):
                pname = (self.wb_catalog.parts.get(pcode) or {}).get("name") or self.model.category_name(pcode)
                pstate = {
                    "name": pname,
                    "items": [],
                    "scanned_count": 0,
                    "reused_count": 0,
                    "ambiguous_count": 0,
                    "unconfirmed_count": 0,
                    "complete": False,
                }
                parts[pcode] = pstate
            existing_groups_by_part.setdefault(pcode, set())
            existing_ids_by_part.setdefault(pcode, set())
            return pstate

        added = 0
        for grp, is_renewed in self.scanned_renewed.items():
            meta = self.model.group_meta.get(grp)
            if not meta:
                continue
            pcode = meta["part"]
            pstate = ensure_part(pcode)
            items = list(pstate.get("items") or [])
            if grp not in existing_groups_by_part[pcode] and meta["base_gid"] not in existing_ids_by_part[pcode]:
                items.append({
                    "class_id": -1,
                    "item_ids": [meta["base_gid"]],
                    "names": [meta["name"]],
                    "qualities": [meta["quality"]],
                    "source": "rejuvenate_ocr",
                })
                existing_groups_by_part[pcode].add(grp)
                existing_ids_by_part[pcode].add(meta["base_gid"])
                added += 1
            reward_gid = meta.get("reward_gid")
            if is_renewed and reward_gid and reward_gid not in existing_ids_by_part[pcode]:
                items.append({
                    "class_id": -1,
                    "item_ids": [reward_gid],
                    "names": [meta.get("reward_name") or meta["name"]],
                    "qualities": [int(meta.get("reward_quality") or meta["quality"])],
                    "source": "rejuvenate_renewed",
                })
                existing_ids_by_part[pcode].add(reward_gid)
                added += 1
            pstate["items"] = items

        # Mark each actually scanned category complete in full mode.
        # '饰品' has no overview view, so never bulk-mark all accessory parts from 'acc'.
        if self.mode == "full":
            for cid, pinfo in self.summary.get("parts", {}).items():
                if not isinstance(pinfo, dict) or not pinfo.get("complete") or cid == "acc":
                    continue
                ensure_part(cid)["complete"] = True

        # Infer missing ability-suit handhelds whenever 278 is complete.
        p278 = parts.get("278")
        if isinstance(p278, dict) and p278.get("complete"):
            for entry in self.wb_catalog.infer_suit_handhelds(parts):
                iids = [int(x) for x in entry.get("item_ids") or []]
                if iids and not any(i in existing_ids_by_part["278"] for i in iids):
                    p278.setdefault("items", []).append(entry)
                    existing_ids_by_part["278"].update(iids)
                    added += 1

        # Recompute counters cleanly.
        verified_parts = list(base.get("verified_parts") or [])
        for pcode, pstate in parts.items():
            if not isinstance(pstate, dict):
                continue
            items = list(pstate.get("items") or [])
            pstate["scanned_count"] = len(items)
            pstate["ambiguous_count"] = sum(len(item.get("item_ids") or []) > 1 for item in items)
            pstate["unconfirmed_count"] = sum(
                item.get("source") == "compendium_ocr_unconfirmed" for item in items
            )
            if pstate.get("complete") and pcode not in verified_parts:
                verified_parts.append(pcode)

        non_makeup_parts = [m["id"] for m in self.model.majors if not m.get("skip") and m["id"] != "acc"] + sorted(ACCESSORY_PARTS)
        all_non_makeup_complete = all(bool((parts.get(p) or {}).get("complete")) for p in non_makeup_parts)
        has_makeup = all(bool((parts.get(p) or {}).get("complete")) for p in MAKEUP_PARTS)
        base["updated_at"] = now
        base["scanned_count"] = sum(int((p or {}).get("scanned_count") or 0) for p in parts.values() if isinstance(p, dict))
        base["ambiguous_count"] = sum(int((p or {}).get("ambiguous_count") or 0) for p in parts.values() if isinstance(p, dict))
        base["unconfirmed_count"] = sum(int((p or {}).get("unconfirmed_count") or 0) for p in parts.values() if isinstance(p, dict))
        base["completed_parts"] = sum(bool((p or {}).get("complete")) for p in parts.values() if isinstance(p, dict))
        base["total_parts"] = len(non_makeup_parts) + (len(MAKEUP_PARTS) if has_makeup else 0)
        base["verified_parts"] = verified_parts
        base["complete"] = bool(base.get("complete") or (self.mode == "full" and all_non_makeup_complete and self.summary.get("complete")))

        wardrobe["last_scan"] = base
        if base["complete"]:
            wardrobe["inventory"] = base
        return added, bool(base["complete"])

    def save(self, reason: str | None = None) -> None:
        if reason is not None:
            self.summary["reason"] = reason
        if not self.scanned_levels and not self.summary["parts"] and not self.summary.get("complete"):
            return
        now = datetime.now(timezone.utc).isoformat()
        self.summary["updated_at"] = now

        added_total = [0]

        def update(data):
            wardrobe = data.setdefault("wardrobe", {})
            scoring = wardrobe.get("scoring")
            if not isinstance(scoring, dict) or scoring.get("schema_version") != 1:
                scoring = _default_scoring()
                levels = scoring.setdefault("levels", {})
                changed_levels = 0
                for grp, lv in self.scanned_levels.items():
                    key = str(grp)
                    val = int(lv)
                    if levels.get(key) != val:
                        levels[key] = val
                        changed_levels += 1
                scoring["updated_at"] = now
                wardrobe["scoring"] = scoring
            else:
                orig_levels = scoring.get("levels") or {}
                changed_levels = sum(
                    orig_levels.get(str(grp)) != int(lv)
                    for grp, lv in self.scanned_levels.items()
                )
                if changed_levels > 0 or not scoring.get("updated_at"):
                    scoring = deepcopy(scoring)
                    levels = scoring.setdefault("levels", {})
                    for grp, lv in self.scanned_levels.items():
                        levels[str(grp)] = int(lv)
                    scoring["updated_at"] = now
                    wardrobe["scoring"] = scoring

            added, _inv_complete = self._merge_wardrobe_inventory(data, now)
            added_total[0] = added
            self.summary["added_items"] = added
            self.summary["updated_levels"] = changed_levels
            wardrobe["renewal_scan"] = dict(self.summary)

            if self.summary.get("complete"):
                cn = (((data.get("observations") or {}).get("pearpal_notebook") or {}).get("summary") or {}).get("cloth_num")
                if cn is not None:
                    wardrobe["pearpal_cloth_num"] = cn

            mats = data.setdefault("materials", {})
            wb_key = wardrobe_snapshot_key(data)
            if wb_key and (changed_levels > 0 or added > 0 or not mats.get("renewal_baseline_key")):
                mats["renewal_baseline_key"] = f"{wb_key}|{scoring.get('updated_at') or now}"
                mats["renewal_consumed"] = {}
            if wb_key and (added > 0 or not mats.get("wardrobe_baseline_key")):
                mats["wardrobe_baseline_key"] = wb_key
                mats["crafted_consumed"] = {}

        self.account.update(update)

    def run(self) -> bool:
        if not self.open():
            raise RuntimeError("未能打开服装焕新页面")
        if not self.ensure_list_view():
            raise RuntimeError("未能确认服装焕新列表页面")
        role = self.account.load()
        owned_groups = self._owned_groups(role)
        prev_levels = {
            str(k): int(v)
            for k, v in (((role.get("wardrobe") or {}).get("scoring") or {}).get("levels") or {}).items()
            if isinstance(v, int)
        }
        prev_renewal = (role.get("wardrobe") or {}).get("renewal_scan") or {}
        prev_renewal_parts = prev_renewal.get("parts") if isinstance(prev_renewal.get("parts"), dict) else {}
        missing_accessory_baseline = not all(p in prev_renewal_parts for p in self.model.minor_ids)
        if self.mode == "incremental" and (
            not owned_groups
            or not wardrobe_snapshot(role).get("complete")
            or missing_accessory_baseline
        ):
            self.mode = "full"
            self.summary["mode"] = "full"
            if missing_accessory_baseline and owned_groups and wardrobe_snapshot(role).get("complete"):
                self.rt.log("焕新读取：检测到此前饰品子分类尚未完整读取，自动切换为完整读取以补全全部饰品。")
            else:
                self.rt.log("焕新读取：当前尚无完整衣柜基准，自动切换为首次完整读取。")

        self.clear_filter()
        if not self.ensure_list_view():
            raise RuntimeError("清空筛选后未能保持在服装焕新列表页面")
        if self.mode == "incremental":
            self.ensure_sort_acquired_desc()

        visited_cids: set[str] = set()
        expected_categories = list(self.model.expected_categories)

        def _missing_majors() -> list[str]:
            return [
                self.model.category_name(cid)
                for cid in expected_categories
                if cid not in visited_cids
            ]

        def _incomplete_parts() -> list[str]:
            return [
                str(p.get("name") or cid)
                for cid, p in self.summary["parts"].items()
                if isinstance(p, dict) and not p.get("complete")
            ]

        def _build_reasons(
            missing_majors: list[str],
            incomplete_parts: list[str],
            *,
            zero_cards: bool = False,
        ) -> list[str]:
            reasons = []
            if missing_majors:
                reasons.append("未读完部位：" + "、".join(missing_majors))
            if incomplete_parts:
                reasons.append("未到底部部位：" + "、".join(incomplete_parts))
            if zero_cards:
                reasons.append("未读取到任何服装卡片")
            return reasons

        def _record_incomplete_diagnostic(
            stage: str,
            is_tall: bool,
            last_frame: np.ndarray | None,
            last_cid: str | None,
            last_yc: int | None,
            step_trace: list[dict],
        ) -> None:
            missing_majors = _missing_majors()
            incomplete_parts = _incomplete_parts()
            zero_cards = self.summary.get("scanned_cards", 0) == 0
            reasons = _build_reasons(missing_majors, incomplete_parts, zero_cards=zero_cards)
            diag_frame = self.rt.capture()
            if diag_frame is None:
                diag_frame = last_frame
            self.rt.diagnostic(
                stage,
                "；".join(reasons) or "焕新读取未完成",
                image=diag_frame,
                details={
                    "mode": self.mode,
                    "tall_mode": bool(is_tall),
                    "scanned_cards": self.summary.get("scanned_cards", 0),
                    "frame_shape": list(diag_frame.shape[:2]) if diag_frame is not None else None,
                    "visited_cids": sorted(visited_cids),
                    "missing_majors": missing_majors,
                    "incomplete_parts": incomplete_parts,
                    "last_cid": last_cid,
                    "last_yc": last_yc,
                    "rail_debug": self.model.debug_active_category(diag_frame) if diag_frame is not None else None,
                    "step_trace": step_trace[-16:],
                },
            )

        def _scroll_accessory_sublist_down(acc_yc: int, frame: np.ndarray) -> np.ndarray | None:
            """Scroll X6ScrollBox_0 down slightly to bring a bottom-clipped active minor into view."""
            try:
                safe_min_y, safe_max_y = safe_capture_y_range(
                    window_hwnd(self.rt),
                    frame.shape[0],
                    capture_width=frame.shape[1],
                    margin_px=8,
                )
            except OSError:
                safe_min_y, safe_max_y = 0, frame.shape[0]
            target_y = min(frame.shape[0] - 60, safe_max_y - 24, acc_yc + 180)
            if target_y < max(safe_min_y + 12, acc_yc + 50):
                return None
            if (
                not self.rt.action("TouchMove", target=[RAIL_CENTER_X, target_y])
                or not self.rt.pause(0.08)
                or not self.rt.action("Scroll", target=[RAIL_CENTER_X, target_y], dx=0, dy=-240)
                or not self.rt.pause(0.25)
                or not self.rt.action("TouchMove", target=_park_point(self.rt))
                or not self.rt.pause(0.1)
            ):
                return None
            return self.rt.capture()

        def _scan_rail(is_tall: bool, step_trace: list[dict]) -> tuple[np.ndarray | None, str | None, int | None]:
            frame, cid, yc = self._reset_rail_top()
            prev_cid: str | None = None
            prev_grid_frame: np.ndarray | None = None
            sort_verified = False
            acc_expand_retries = 0
            in_accessory_sublist = False
            for step in range(56):
                if self.rt.stopped or frame is None:
                    break
                if cid == "299":
                    visited_cids.add("299")
                    prev_cid = "299"
                    in_accessory_sublist = False
                    prev_grid_frame = frame
                    frame, cid, yc, moved = self._step_rail_down(frame, cid, yc)
                    step_trace.append({
                        "step": step,
                        "cid": cid,
                        "yc": yc,
                        "moved": moved,
                        "parts_count": len(self.summary["parts"]),
                    })
                    if not moved:
                        break
                    continue
                if cid == "acc":
                    # '饰品' has no overview view. Distinguish entering a collapsed drawer from
                    # stepping inside an already-expanded sub-list whose active slot is bottom-clipped.
                    if acc_expand_retries == 0:
                        acc_expand_retries += 1
                        if self.rt.pause(0.3):
                            frame = self.rt.capture()
                            cid, yc, _sc = self.model.detect_active_category(frame)
                            step_trace.append({
                                "step": step,
                                "cid": cid,
                                "yc": yc,
                                "moved": cid not in (None, "acc"),
                                "parts_count": len(self.summary["parts"]),
                            })
                            continue
                    if in_accessory_sublist:
                        # Drawer is already open: never click the '饰品' header (which collapses it).
                        # Gently scroll X6ScrollBox_0 down to pull any bottom-clipped active minor up.
                        if yc is not None and acc_expand_retries < 3:
                            acc_expand_retries += 1
                            scrolled = _scroll_accessory_sublist_down(yc, frame)
                            if scrolled is not None:
                                frame = scrolled
                            cid, yc, _sc = self.model.detect_active_category(frame)
                            step_trace.append({
                                "step": step,
                                "cid": cid,
                                "yc": yc,
                                "moved": cid not in (None, "acc"),
                                "parts_count": len(self.summary["parts"]),
                            })
                            continue
                    else:
                        if yc is not None and acc_expand_retries == 1:
                            acc_expand_retries += 1
                            self.rt.action("Click", target=[RAIL_CENTER_X, yc])
                            self.rt.pause(0.35)
                            frame = self.rt.capture()
                            cid, yc, _sc = self.model.detect_active_category(frame)
                            step_trace.append({
                                "step": step,
                                "cid": cid,
                                "yc": yc,
                                "moved": cid not in (None, "acc"),
                                "parts_count": len(self.summary["parts"]),
                            })
                            continue
                        if yc is not None and acc_expand_retries == 2:
                            # If clicking once didn't select Slot #1, click Slot #1 directly (~95px below acc)
                            # rather than clicking 'acc' a second time (which would re-collapse the drawer).
                            acc_expand_retries += 1
                            slot1_y = min(frame.shape[0] - 32, yc + 95)
                            self.rt.action("Click", target=[RAIL_CENTER_X, slot1_y])
                            self.rt.pause(0.35)
                            frame = self.rt.capture()
                            cid, yc, _sc = self.model.detect_active_category(frame)
                            step_trace.append({
                                "step": step,
                                "cid": cid,
                                "yc": yc,
                                "moved": cid not in (None, "acc"),
                                "parts_count": len(self.summary["parts"]),
                            })
                            continue
                    prev_grid_frame = frame
                    frame, cid, yc, moved = self._step_rail_down(frame, cid, yc)
                    step_trace.append({
                        "step": step,
                        "cid": cid,
                        "yc": yc,
                        "moved": moved,
                        "parts_count": len(self.summary["parts"]),
                    })
                    if not moved:
                        break
                    continue
                if cid is None or cid in visited_cids:
                    if cid in ACCESSORY_PARTS:
                        in_accessory_sublist = True
                        acc_expand_retries = 0
                    if cid in visited_cids and (not _missing_majors() or cid == "299"):
                        break
                    prev_grid_frame = frame
                    frame, cid, yc, moved = self._step_rail_down(frame, cid, yc)
                    step_trace.append({
                        "step": step,
                        "cid": cid,
                        "yc": yc,
                        "moved": moved,
                        "parts_count": len(self.summary["parts"]),
                    })
                    if not moved:
                        break
                    continue

                visited_cids.add(cid)
                if cid in ACCESSORY_PARTS:
                    in_accessory_sublist = True
                    acc_expand_retries = 0
                else:
                    in_accessory_sublist = False
                fresh_switch = prev_cid is not None and cid != prev_cid
                self._last_scan_frame = frame
                self.rt.log(f"焕新读取：正在读取「{self.model.category_name(cid)}」。", detail=True)
                self._scan_category(
                    cid,
                    is_tall,
                    owned_groups,
                    prev_levels,
                    incremental=(self.mode == "incremental"),
                    initial_frame=frame,
                    prev_grid_frame=prev_grid_frame if fresh_switch else None,
                    assume_at_top=(self.mode == "incremental" and fresh_switch),
                    verify_sort=(self.mode == "incremental" and not sort_verified),
                )
                sort_verified = True
                prev_cid = cid
                if self.mode != "incremental":
                    self.save()
                if not _missing_majors():
                    step_trace.append({
                        "step": step,
                        "cid": cid,
                        "yc": yc,
                        "moved": False,
                        "parts_count": len(self.summary["parts"]),
                    })
                    break

                if self.mode == "incremental":
                    nav_frame = self._last_scan_frame if self._last_scan_frame is not None else frame
                    prev_grid_frame = nav_frame
                    frame, cid, yc, moved = self._step_rail_down(nav_frame, cid, yc)
                else:
                    frame = self.rt.capture()
                    prev_grid_frame = frame
                    cid_now, yc_now, _sc = self.model.detect_active_category(frame)
                    cid_use = cid_now if (cid_now is not None and cid_now != "acc") else cid
                    yc_use = yc_now if (cid_now is not None and cid_now != "acc" and yc_now is not None) else yc
                    frame, cid, yc, moved = self._step_rail_down(frame, cid_use, yc_use)
                step_trace.append({
                    "step": step,
                    "cid": cid,
                    "yc": yc,
                    "moved": moved,
                    "parts_count": len(self.summary["parts"]),
                })
                if not moved or (cid in visited_cids and cid == "299"):
                    break
            return frame, cid, yc

        saved_final = False
        need_regular_fallback = False
        try:
            tall_ctx = tall_window(self.rt) if self.mode == "full" else nullcontext(False)
            with tall_ctx as is_tall:
                self.summary["tall_mode"] = bool(is_tall)
                if is_tall:
                    self.rt.log("焕新读取：已切换超长竖屏模式，正在读取各部位服装等级与焕新状态。")
                elif self.mode == "incremental":
                    self.rt.log("焕新读取：正在常规窗口按获得时间快速增量同步。", detail=True)

                tall_trace: list[dict] = []
                last_frame, last_cid, last_yc = _scan_rail(bool(is_tall), tall_trace)
                need_regular_fallback = bool(is_tall and not self.summary["parts"] and not self.rt.stopped)
                if not self.rt.stopped and (_missing_majors() or _incomplete_parts() or self.summary.get("scanned_cards", 0) == 0):
                    _record_incomplete_diagnostic(
                        "renewal_tall_fallback" if need_regular_fallback else "renewal_incomplete",
                        bool(is_tall),
                        last_frame,
                        last_cid,
                        last_yc,
                        tall_trace,
                    )

            if need_regular_fallback:
                self.rt.log("焕新读取：超长竖屏模式未能识别左侧部位栏，自动切回常规窗口继续读取。")
                visited_cids.clear()
                self.summary["tall_mode"] = False
                fallback_trace: list[dict] = []
                last_frame, last_cid, last_yc = _scan_rail(False, fallback_trace)
                if not self.rt.stopped and (_missing_majors() or _incomplete_parts() or self.summary.get("scanned_cards", 0) == 0):
                    _record_incomplete_diagnostic(
                        "renewal_incomplete",
                        False,
                        last_frame,
                        last_cid,
                        last_yc,
                        fallback_trace,
                    )

            missing_majors = _missing_majors()
            incomplete_parts = _incomplete_parts()
            zero_cards = self.summary.get("scanned_cards", 0) == 0
            parts_complete = bool(self.summary["parts"]) and not incomplete_parts
            self.summary["complete"] = bool(not missing_majors and parts_complete and not zero_cards)
            reasons = _build_reasons(missing_majors, incomplete_parts, zero_cards=zero_cards)
            reason = "；".join(reasons) if reasons else None
            self.save(reason)
            saved_final = True
            record_material_history(self.account, self.crafting, self.scoring)
            return self.summary["complete"]
        finally:
            if not saved_final and self.mode == "incremental" and (self.scanned_levels or self.summary["parts"]):
                try:
                    self.save("未读完部位：" + "、".join(_missing_majors()) if _missing_majors() else "读取中断")
                except Exception:
                    pass
            if not self.rt.stopped:
                self.rt.main()
