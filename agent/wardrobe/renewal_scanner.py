"""Full tall-window Clothing Glow-Up (服装焕新) scanner for clothing levels & renewal state."""
from __future__ import annotations

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
from tall_window import tall_window
from .scanner import PEAR_PAL_NEXT_PAGE, PEAR_PAL_PREV_PAGE


HEADER_ROI = [65, 15, 200, 48]
FILTER_BTN = [822, 71]
FILTER_PANEL_ROI = [200, 120, 980, 560]

RAIL_X0 = 62
RAIL_SIZE = 48
RAIL_GLOW_X0 = 54
RAIL_GLOW_X1 = 118
RAIL_TOP_Y = 130
RAIL_BOTTOM_MARGIN = 50
RAIL_TOP_SLOT = [86, 174]

COLS = 6
FIRST_X = 172
COL_PITCH = 176
CARD_W = 150
CARD_H = 188
ROW_PITCH_EXACT = 245.0
GRID_TOP = 94
GRID_BOTTOM_MARGIN = 10
GRID_SCROLL_TARGET = [620, 360]

MAX_LEVEL = 10
RENEWED_LEVEL = 11
ACCESSORY_PARTS = {
    "270", "271", "272", "273", "274", "275", "276", "277", "278", "279",
    "292", "293", "294", "295", "296",
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
        self.non_skip_majors: list[str] = [m["id"] for m in self.majors if not m.get("skip")]

        npz = np.load(directory / "icons.npz")
        self.templates: list[tuple[str, np.ndarray]] = []
        for key in npz.files:
            cid = key[4:].removesuffix("_s") if key.startswith("cat_") else key
            alpha = npz[key][:, :, 3].astype(np.float32) / 255.0
            norm_t = alpha - float(alpha.mean())
            denom = float(np.sqrt((norm_t ** 2).sum()))
            if denom > 1e-6:
                self.templates.append((cid, norm_t / denom))

        # Build clean_name -> ordered unique group IDs per part and combined for accessories.
        self.part_groups: dict[str, dict[str, list[str]]] = {}
        game_items = scoring.get("game_items") or {}
        entries = sorted(
            game_items.items(),
            key=lambda kv: (kv[0] != str(kv[1].get("group")), int(kv[0])),
        )
        for _gid, g in entries:
            part = str(g.get("part") or "")
            if not part or part == "413" or part.startswith("28"):
                continue
            cname = clean_clothing_name(g.get("name", ""))
            grp = str(g.get("group") or "")
            if not cname or not grp:
                continue
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
        cname_no_ascii = re.sub(r"[A-Za-z]", "", cname)
        if cname_no_ascii and cname_no_ascii != cname and cname_no_ascii in table:
            return list(table[cname_no_ascii])
        if len(cname) >= 3:
            same_len = [
                (groups, sum(a != b for a, b in zip(cname, key)))
                for key, groups in table.items() if len(key) == len(cname)
            ]
            dist1 = [groups for groups, d in same_len if d == 1]
            dist2 = [groups for groups, d in same_len if d == 2]
            if len(dist1) == 1 and (len(cname) >= 4 or not dist2):
                return list(dist1[0])
            trunc1 = [groups for key, groups in table.items() if cname in key and len(key) - len(cname) == 1]
            if len(trunc1) == 1:
                return list(trunc1[0])
        return []

    def category_name(self, cid: str) -> str:
        meta = self.meta_by_id.get(cid)
        return str(meta.get("name") or cid) if meta else cid

    def detect_active_category(self, frame: np.ndarray) -> tuple[str | None, int | None, float]:
        """Locate the glowing selected left-rail slot and classify its category icon."""
        if frame is None:
            return None, None, -1.0
        h, w = frame.shape[:2]
        y_end = max(RAIL_TOP_Y + 40, h - RAIL_BOTTOM_MARGIN)
        strip = frame[RAIL_TOP_Y:y_end, RAIL_GLOW_X0:RAIL_GLOW_X1]
        if strip.size == 0:
            return None, None, -1.0
        r = strip[:, :, 2].astype(np.int32)
        b = strip[:, :, 0].astype(np.int32)
        glow_rows = np.where(((r > 145) & ((r - b) > 22)).sum(axis=1) >= 8)[0] + RAIL_TOP_Y
        if len(glow_rows) < 6:
            return None, None, -1.0

        # Pick the strongest contiguous warm highlight block.
        breaks = np.where(np.diff(glow_rows) > 8)[0]
        segments = np.split(glow_rows, breaks + 1)
        best_seg = max(segments, key=len)
        yc = int(round(float(np.median(best_seg))))
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)

        best_by_cid: dict[str, float] = {}
        for y0 in range(max(0, yc - 38), min(h - RAIL_SIZE, yc - 20), 2):
            for x0 in range(max(0, RAIL_X0 - 2), min(w - RAIL_SIZE, RAIL_X0 + 3)):
                patch = gray[y0:y0 + RAIL_SIZE, x0:x0 + RAIL_SIZE]
                p = patch - float(patch.mean())
                denom = float(np.sqrt((p ** 2).sum()))
                if denom < 1e-6:
                    continue
                pn = p / denom
                for cid, tmpl in self.templates:
                    sc = float((pn * tmpl).sum())
                    if sc > best_by_cid.get(cid, -1.0):
                        best_by_cid[cid] = sc
        if not best_by_cid:
            return None, yc, -1.0
        ranked = sorted(best_by_cid.items(), key=lambda kv: -kv[1])
        top1_id, top1_sc = ranked[0]
        top2_sc = ranked[1][1] if len(ranked) > 1 else -1.0
        if top1_sc < 0.62 or (top1_sc - top2_sc) < 0.06:
            return None, yc, top1_sc
        return top1_id, yc, top1_sc


@lru_cache(maxsize=2)
def _load_rejuvenate_model(scoring_version: str = "") -> RejuvenateModel:
    _crafting, scoring = load_catalogs()
    return RejuvenateModel(ROOT / "resource/model/rejuvenate", scoring)


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
    best_sc, best_ys = -1e9, []
    for phase in np.linspace(0, ROW_PITCH_EXACT, 245, endpoint=False):
        ys = []
        k = 0
        while True:
            by = int(round(-180 + phase + k * ROW_PITCH_EXACT))
            k += 1
            if by >= h_img - 40:
                break
            if by + CARD_H > grid_top:
                ys.append(by)
        if not ys:
            continue
        tot = 0.0
        for by in ys:
            c0, c1 = max(grid_top + 2, by + 6), min(h_img, by + 184)
            in_card = float(np.mean(bv[c0:c1])) if c1 > c0 + 10 else 0.0
            g1_0, g1_1 = max(grid_top + 2, by - 36), max(grid_top + 2, by - 2)
            g1 = float(np.mean(bv[g1_0:g1_1])) if g1_1 > g1_0 + 4 else 0.0
            g2_0, g2_1 = min(h_img, by + 190), min(h_img, by + 238)
            g2 = float(np.mean(bv[g2_0:g2_1])) if g2_1 > g2_0 + 8 else 0.0
            tot += in_card - 1.5 * g1 - 1.5 * g2
        sc = tot / len(ys)
        if sc > best_sc:
            best_sc, best_ys = sc, ys
    return [by for by in best_ys if by >= grid_top and by + 222 <= grid_bottom]


def is_card_present(frame: np.ndarray, bx: int, by: int) -> bool:
    if by < 0 or by + CARD_H > frame.shape[0] or bx < 0 or bx + CARD_W > frame.shape[1]:
        return False
    body = frame[by + 16:by + 166, bx + 12:bx + 138]
    gray = cv2.cvtColor(body, cv2.COLOR_BGR2GRAY)
    return float(np.std(gray)) >= 9.0


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


def _read_single_ocr(rt, frame: np.ndarray, roi: list[int], expected: str) -> str:
    rx, ry, rw, rh = roi
    crop = frame[max(0, ry):min(frame.shape[0], ry + rh), max(0, rx):min(frame.shape[1], rx + rw)]
    if crop.size == 0:
        return ""
    up = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    padded = cv2.copyMakeBorder(up, 8, 8, 8, 8, cv2.BORDER_REPLICATE)
    res = rt.recognize("MaaNikki_Daily_OCR", {
        "roi": [0, 0, padded.shape[1], padded.shape[0]],
        "expected": expected,
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

    # Chunk 2 rows per band (<=470px height) so det.onnx never downscales tall 5120p crops.
    for start in range(0, len(rows), 2):
        chunk = rows[start:start + 2]
        top_y = max(GRID_TOP, min(by for by, _ in chunk) + 114)
        bot_y = min(frame.shape[0], max(by for by, _ in chunk) + 224)
        band_roi = [FIRST_X - 10, top_y, 1080, bot_y - top_y]
        res = rt.recognize("MaaNikki_Daily_OCR", {
            "roi": band_roi,
            "expected": ".+",
            "threshold": 0.60,
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
            elif matched_by + 184 <= cy <= matched_by + 226:
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
                lv_raw = _read_single_ocr(rt, frame, [bx + 18, by + 118, 114, 36], r"(?:MAX|等级\d+|\d+)")
                lv = parse_rejuv_level(lv_raw)
            nm = name_by_cell.get((by, col), "")
            if not clean_clothing_name(nm):
                nm = _read_single_ocr(rt, frame, [bx - 4, by + 186, 158, 36], ".+")
            out[(by, col)] = (nm, lv, renewed)
    return out


def _same_rejuv_grid(first: np.ndarray | None, second: np.ndarray | None) -> bool:
    if first is None or second is None or first.shape != second.shape:
        return False
    y1 = max(GRID_TOP + 20, first.shape[0] - 24)
    diff = cv2.absdiff(first[GRID_TOP + 6:y1, 168:1224], second[GRID_TOP + 6:y1, 168:1224])
    return float(np.mean(diff)) < 1.5


def _scroll_rejuv_and_park(rt, dy: int, wait_seconds: float) -> np.ndarray | None:
    if (not rt.action("TouchMove", target=GRID_SCROLL_TARGET)
            or not rt.pause(0.1)
            or not rt.action("Scroll", target=GRID_SCROLL_TARGET, dx=0, dy=dy)
            or not rt.pause(wait_seconds)
            or not rt.action("TouchMove", target=[640, 36])
            or not rt.pause(0.12)):
        return None
    return rt.capture()


def _scroll_rejuv_to_top(rt) -> np.ndarray | None:
    frame = rt.capture()
    for _ in range(12):
        if rt.stopped or frame is None:
            return None
        after = _scroll_rejuv_and_park(rt, 14400, 0.28)
        if after is None:
            return None
        if _same_rejuv_grid(frame, after):
            return after
        frame = after
    return frame


def _measure_rejuv_scroll_delta(before: np.ndarray, after: np.ndarray, row_ys: list[int]) -> int | None:
    if before is None or after is None or before.shape != after.shape or not row_ys:
        return None
    grid_bottom = before.shape[0] - GRID_BOTTOM_MARGIN
    anchors = [cy for cy in reversed(row_ys) if cy >= GRID_TOP][:3]
    b_gray = cv2.cvtColor(before, cv2.COLOR_BGR2GRAY)
    a_gray = cv2.cvtColor(after, cv2.COLOR_BGR2GRAY)
    votes: list[int] = []
    for cy in anchors:
        tmpl = b_gray[cy + 16:cy + 146, FIRST_X + 10:FIRST_X + COLS * COL_PITCH - 30]
        if tmpl.shape[0] < 100 or float(np.std(tmpl)) < 4.0:
            continue
        low = max(12, GRID_TOP - 40)
        high = min(cy + 20, grid_bottom - tmpl.shape[0])
        if high < low + 8:
            continue
        search = a_gray[low:high + tmpl.shape[0], FIRST_X + 10:FIRST_X + COLS * COL_PITCH - 30]
        res = cv2.matchTemplate(search, tmpl, cv2.TM_CCOEFF_NORMED).ravel()
        best_idx = int(np.argmax(res))
        if float(res[best_idx]) >= 0.86:
            votes.append((cy + 16) - (low + best_idx))
    if not votes:
        return None
    delta = int(round(float(np.median(votes))))
    return delta if delta >= 0 else None


class RenewalScanner:
    """Scanner for 美鸭梨 -> 服装焕新, syncing card levels (0..11) to wardrobe.scoring.levels."""

    def __init__(self, rt, account: AccountState | None = None):
        self.rt = rt
        self.account = account or AccountState.for_runtime(ROOT, rt)
        self.crafting, self.scoring = load_catalogs()
        self.model = _load_rejuvenate_model(str(self.scoring.get("version") or ""))
        self.summary: dict = {
            "complete": False,
            "tall_mode": False,
            "scanned_cards": 0,
            "matched_groups": 0,
            "unmatched_count": 0,
            "unmatched_samples": [],
            "parts": {},
        }
        self.scanned_levels: dict[str, int] = {}

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

    def clear_filter(self) -> None:
        if not self.rt.action("Click", target=FILTER_BTN) or not self.rt.pause(0.35):
            return
        frame = self.rt.capture()
        if frame is None:
            return
        if self.rt.click_text("^重置$", FILTER_PANEL_ROI):
            self.rt.pause(0.25)
        for pattern in ("^(筛选|确认|确定)$", "^重置$"):
            if self.rt.click_text(pattern, FILTER_PANEL_ROI):
                self.rt.pause(0.3)
                frame = self.rt.capture()
                if frame is not None and not self.rt.hit(self.rt.ui.feature(text="^重置$", roi=FILTER_PANEL_ROI), image=frame):
                    return
        frame = self.rt.capture()
        if frame is not None and "重置" in self.rt.text(FILTER_PANEL_ROI, image=frame):
            self.rt.key(27, delay=0.35)  # VK_ESCAPE
        self.rt.action("TouchMove", target=[640, 36])
        self.rt.pause(0.2)

    def _reset_rail_top(self) -> tuple[np.ndarray | None, str | None, int | None]:
        frame = self.rt.capture()
        for _ in range(16):
            if self.rt.stopped or frame is None:
                return None, None, None
            cid, yc, _sc = self.model.detect_active_category(frame)
            if cid == "299":
                return frame, cid, yc
            self.rt.key(87, delay=0.25)  # VK_W
            after = self.rt.capture()
            if after is not None and np.mean(cv2.absdiff(frame[130:660, 54:118], after[130:660, 54:118])) < 1.0:
                if cid is not None:
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
        self.rt.key(83, delay=0.32)  # VK_S
        after = self.rt.capture()
        if after is not None:
            cid, yc, _sc = self.model.detect_active_category(after)
            if (cid is not None and cid != prev_cid) or (yc is not None and prev_yc is not None and abs(yc - prev_yc) > 12):
                return after, cid, yc, True

        # Fallback: click the next visible slot directly below the active highlight, or click [S] badge.
        if prev_yc is not None:
            cand_y = min(prev_frame.shape[0] - 55, prev_yc + 75)
            self.rt.action("Click", target=[86, cand_y])
            self.rt.pause(0.32)
            after2 = self.rt.capture()
            if after2 is not None:
                cid2, yc2, _sc2 = self.model.detect_active_category(after2)
                if (cid2 is not None and cid2 != prev_cid) or not _same_rejuv_grid(prev_frame, after2):
                    return after2, cid2, yc2, True
        return after or prev_frame, prev_cid, prev_yc, False

    def _scan_category(self, cid: str, is_tall: bool, owned_groups: set[str]) -> bool:
        frame = _scroll_rejuv_to_top(self.rt)
        if frame is None:
            return False
        grid_bottom = frame.shape[0] - GRID_BOTTOM_MARGIN
        scroll_y = 0
        first_row_y: int | None = None
        seen_slots: set[tuple[int, int]] = set()
        cat_cards = 0
        cat_matched = 0
        cat_unmatched = 0
        cat_renewed = 0
        complete = False

        for _page in range(64):
            if self.rt.stopped:
                break
            row_ys = detect_rejuv_rows(frame, grid_top=GRID_TOP, grid_bottom=grid_bottom)
            if not row_ys:
                complete = _page == 0
                break
            if first_row_y is None:
                first_row_y = row_ys[0]

            rows_to_read: list[tuple[int, list[int]]] = []
            for by in row_ys:
                row_idx = int(round((by + scroll_y - first_row_y) / ROW_PITCH_EXACT))
                if row_idx < 0:
                    continue
                cols_needed: list[int] = []
                for col in range(COLS):
                    if (row_idx, col) in seen_slots:
                        continue
                    bx = FIRST_X + col * COL_PITCH
                    if not is_card_present(frame, bx, by):
                        continue
                    seen_slots.add((row_idx, col))
                    cols_needed.append(col)
                if cols_needed:
                    rows_to_read.append((by, cols_needed))

            if rows_to_read:
                parsed = _batch_read_rows_ocr(self.rt, frame, rows_to_read)
                for (by, col), (raw_name, lv, renewed) in parsed.items():
                    cat_cards += 1
                    self.summary["scanned_cards"] += 1
                    if lv is None:
                        cat_unmatched += 1
                        self.summary["unmatched_count"] += 1
                        continue
                    groups = self.model.lookup_groups(cid, raw_name)
                    if not groups:
                        cat_unmatched += 1
                        self.summary["unmatched_count"] += 1
                        if len(self.summary["unmatched_samples"]) < 20:
                            self.summary["unmatched_samples"].append({
                                "part": cid, "name": raw_name, "level": lv,
                            })
                        continue
                    owned_hits = [g for g in groups if g in owned_groups]
                    targets = owned_hits if owned_hits else [groups[0]]
                    for grp in targets:
                        self.scanned_levels[grp] = lv
                        cat_matched += 1
                    if lv == RENEWED_LEVEL or renewed:
                        cat_renewed += 1

            step_dy = -4200 if is_tall else -480
            moved = False
            for attempt_dy in (step_dy, step_dy // 2, step_dy // 4):
                after = _scroll_rejuv_and_park(self.rt, attempt_dy, 0.32)
                if after is None:
                    break
                if _same_rejuv_grid(frame, after):
                    after = _scroll_rejuv_and_park(self.rt, attempt_dy, 0.32)
                    if after is None:
                        break
                    if _same_rejuv_grid(frame, after):
                        complete = True
                        moved = True
                        break
                delta = _measure_rejuv_scroll_delta(frame, after, row_ys)
                if delta is not None:
                    scroll_y += delta
                    frame = after
                    moved = True
                    break
                if _scroll_rejuv_and_park(self.rt, -attempt_dy, 0.25) is None:
                    break
            if complete or not moved:
                break

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

    def save(self, reason: str | None = None) -> None:
        if not self.scanned_levels and not self.summary["parts"]:
            return
        now = datetime.now(timezone.utc).isoformat()
        self.summary["updated_at"] = now
        if reason is not None:
            self.summary["reason"] = reason

        def update(data):
            wardrobe = data.setdefault("wardrobe", {})
            scoring = wardrobe.get("scoring")
            if not isinstance(scoring, dict) or scoring.get("schema_version") != 1:
                scoring = _default_scoring()
            else:
                scoring = deepcopy(scoring)
            levels = scoring.setdefault("levels", {})
            for grp, lv in self.scanned_levels.items():
                levels[str(grp)] = int(lv)
            scoring["updated_at"] = now
            wardrobe["scoring"] = scoring
            wardrobe["renewal_scan"] = dict(self.summary)

            mats = data.setdefault("materials", {})
            wb_key = wardrobe_snapshot_key(data)
            if wb_key:
                mats["renewal_baseline_key"] = f"{wb_key}|{now}"
                mats["renewal_consumed"] = {}

        self.account.update(update)

    def run(self) -> bool:
        if not self.open():
            raise RuntimeError("未能打开服装焕新页面")
        role = self.account.load()
        owned_groups = self._owned_groups(role)
        self.clear_filter()

        visited_cids: set[str] = set()
        expected_majors = [m["id"] for m in self.model.majors if not m.get("skip")]
        try:
            with tall_window(self.rt) as is_tall:
                self.summary["tall_mode"] = bool(is_tall)
                if is_tall:
                    self.rt.log("焕新读取：已切换超长竖屏模式，正在读取各部位服装等级与焕新状态。")

                frame, cid, yc = self._reset_rail_top()
                for _step in range(32):
                    if self.rt.stopped or frame is None:
                        break
                    if cid == "299":
                        visited_cids.add("299")
                        frame, cid, yc, moved = self._step_rail_down(frame, cid, yc)
                        if not moved:
                            break
                        continue
                    if cid is None or cid in visited_cids:
                        if cid in visited_cids and (
                            all(m in visited_cids for m in expected_majors[:-1])
                            or cid == "299"
                        ):
                            break
                        frame, cid, yc, moved = self._step_rail_down(frame, cid, yc)
                        if not moved:
                            break
                        continue

                    visited_cids.add(cid)
                    self.rt.log(f"焕新读取：正在读取「{self.model.category_name(cid)}」。", detail=True)
                    self._scan_category(cid, is_tall, owned_groups)
                    self.save()

                    frame = self.rt.capture()
                    cid_now, yc_now, _sc = self.model.detect_active_category(frame)
                    frame, cid, yc, moved = self._step_rail_down(frame, cid_now or cid, yc_now if yc_now is not None else yc)
                    if not moved or (cid in visited_cids and cid == "299"):
                        break

            missing_majors = [
                self.model.category_name(mid)
                for mid in expected_majors
                if (mid != "acc" and mid not in visited_cids)
                or (mid == "acc" and "acc" not in visited_cids and not any(p in visited_cids for p in ACCESSORY_PARTS))
            ]
            parts_complete = bool(self.summary["parts"]) and all(
                p.get("complete") for p in self.summary["parts"].values()
            )
            self.summary["complete"] = bool(not missing_majors and parts_complete)
            reason = ("未读完部位：" + "、".join(missing_majors)) if missing_majors else None
            self.save(reason)
            record_material_history(self.account, self.crafting, self.scoring)
            return self.summary["complete"]
        finally:
            if not self.rt.stopped:
                self.rt.main()
