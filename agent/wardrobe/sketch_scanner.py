"""Read-only in-game 设计图 (Sketch) scanner with tall-window acceleration and Copy-2 support."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from typing import Any

import cv2
import numpy as np

from account_state import AccountState
from collection.backpack import wardrobe_snapshot
from daily.settings import ROOT
from data_paths import data_path
from tall_window import clamp_capture_park_point, tall_window
from task_control import TaskStopped
from .scanner import PEAR_PAL_NEXT_PAGE, PEAR_PAL_PREV_PAGE

# ---------------------------------------------------------------------------
# 1280x720 (and 1280x5120 tall-mode) UI layout constants
# ---------------------------------------------------------------------------
TAB_LIST_ROI = [30, 95, 165, 565]
MIRACLE_REGION_ROI = [210, 14, 980, 50]
MIRACLE_EMBLEM_ROI = [820, 48, 440, 65]
MIRACLE_NAME_ROI = [330, 545, 290, 65]
MIRACLE_PROGRESS_ROI = [1135, 125, 130, 50]

SUIT_GRID_X = 225
SUIT_GRID_W = 1020
SUIT_GRID_TOP = 65

ITEM_GRID_X = 215
ITEM_GRID_W = 1030
ITEM_GRID_TOP = 60

SCROLL_POINT = [640, 360]
STRIP_HEIGHT = 720
STRIP_OVERLAP = 120

# 5-col suit grid geometry (能力套装 / 风格套装 / 传说套装)
SUIT_COL_CX0 = 333.0
SUIT_COL_PITCH = 200.0
SUIT_BADGE_X0 = 244.0
SUIT_BADGE_DY = -256  # relative to suit name box y

# 6-col item grid geometry (珍奇单品 / 奇想香氛 / 大喵斗篷)
ITEM_COL_CX0 = 308.0
ITEM_COL_PITCH = 169.5
ITEM_BADGE_X0 = 229.0
ITEM_BADGE_DY = -211  # relative to item name box y
ITEM_CRAFTED_DX = 116
ITEM_CRAFTED_DY = -214

SUIT_BADGE_THRESHOLD = 0.65
ITEM_BADGE_THRESHOLD = 0.68
CRAFTED_CHECK_THRESHOLD = 0.72

GRID_TABS: tuple[tuple[str, str], ...] = (
    ("suit", "传说套装"),
    ("suit", "能力套装"),
    ("suit", "风格套装"),
    ("item", "珍奇单品"),
    ("item", "奇想香氛"),
    ("item", "大喵斗篷"),
)

_RE_PROGRESS = re.compile(r"^(\d+)\s*[/／]\s*(\d+)$")
_RE_PROGRESS_SLASH_CONFUSED = re.compile(r"^(\d+)[1lI|](\d+)$")
_RE_IGNORE = re.compile(
    r"^(?:典雅|清新|甜美|性感|帅气|新|品质|前往制作|设计图|奇迹套装|传说套装|能力套装|风格套装|珍奇单品|奇想香氛|大喵斗篷)$"
)
_RE_NORMALIZE = re.compile(r"[\s·・•.。:：\-_「」『』()（）]")


def _norm(text: str) -> str:
    return _RE_NORMALIZE.sub("", text or "")


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _parse_progress(raw: str, expected_total: int | None = None) -> tuple[int, int] | None:
    """Parse suit progress 'x/y', recovering '/' misread as '1/l/I/|' using catalog piece count."""
    cleaned = re.sub(r"\s+", "", raw or "")
    m = _RE_PROGRESS.match(cleaned)
    if m:
        ow, tot = int(m.group(1)), int(m.group(2))
        if tot > 0 and ow <= tot:
            return ow, tot
    if expected_total is not None and expected_total > 0:
        m2 = _RE_PROGRESS_SLASH_CONFUSED.match(cleaned)
        if m2:
            ow, tot = int(m2.group(1)), int(m2.group(2))
            if tot == expected_total and ow <= tot:
                return ow, tot
    return None


def _load_crafting_index() -> tuple[dict[str, list[str]], dict[str, list[str]], dict[str, str]]:
    """Build normalized name lookup tables and suit_key -> suit_id map from crafting_catalog.json."""
    catalog = json.loads(data_path("resource/data/crafting_catalog.json").read_text(encoding="utf-8"))
    by_suit: dict[str, list[str]] = {}
    by_piece: dict[str, list[str]] = {}
    suit_id_by_key: dict[str, str] = {}
    for sk in catalog.get("sketches") or []:
        sid = str(sk["id"])
        piece_key = _norm(str(sk.get("name") or ""))
        if piece_key:
            by_piece.setdefault(piece_key, []).append(sid)
        if sk.get("category") == "suit" and sk.get("suit_name"):
            suit_key = _norm(str(sk["suit_name"]))
            if suit_key:
                by_suit.setdefault(suit_key, []).append(sid)
                if sk.get("suit_id"):
                    suit_id_by_key[suit_key] = str(sk["suit_id"])
    return by_suit, by_piece, suit_id_by_key


def _load_suit_evo_ownership(
    role: dict,
    by_suit: dict[str, list[str]],
) -> tuple[set[str], set[str], set[str]]:
    """Return (evolvable_suit_keys, base_owned_suit_keys, final_evo_owned_suit_keys) from wardrobe."""
    try:
        scoring = json.loads(data_path("resource/data/wardrobe_scoring.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set(), set(), set()

    snapshot = wardrobe_snapshot(role)
    owned_gids: set[int] = set()
    for pstate in (snapshot.get("parts") or {}).values():
        if not isinstance(pstate, dict):
            continue
        for row in pstate.get("items") or []:
            if not isinstance(row, dict) or row.get("source") == "selected_card_unconfirmed":
                continue
            for gid in row.get("item_ids") or []:
                if isinstance(gid, int) or str(gid).isdigit():
                    owned_gids.add(int(gid))

    game_items = scoring.get("game_items") or {}
    owned_score_ids: set[str] = set()
    for gid in owned_gids:
        entry = game_items.get(str(gid))
        if entry and entry.get("score_id") is not None:
            owned_score_ids.add(str(entry["score_id"]))

    suit_by_norm: dict[str, dict] = {}
    for s in scoring.get("suits") or []:
        sname = _norm(str(s.get("name") or ""))
        if sname:
            suit_by_norm[sname] = s

    evolvable: set[str] = set()
    base_owned: set[str] = set()
    final_evo_owned: set[str] = set()
    for skey, sids in by_suit.items():
        if sids and all(sid in owned_score_ids for sid in sids):
            base_owned.add(skey)
        smeta = suit_by_norm.get(skey)
        if not smeta:
            continue
        evos = [v for v in (smeta.get("variants") or []) if v.get("kind") == "evo"]
        if not evos:
            continue
        evolvable.add(skey)
        last_evo = max(evos, key=lambda v: int(v.get("stage") or 0))
        evo_gids = [int(g) for g in (last_evo.get("game_items") or []) if isinstance(g, int) or str(g).isdigit()]
        if evo_gids and all(g in owned_gids for g in evo_gids):
            final_evo_owned.add(skey)
    return evolvable, base_owned, final_evo_owned


def _lookup_name(key: str, table: dict[str, list[str]]) -> tuple[str | None, list[str]]:
    """Exact match with guarded OCR typo fallback (unique match only)."""
    if not key:
        return None, []
    if key in table:
        return key, table[key]
    if len(key) >= 3 and _has_cjk(key):
        cands = [
            (k, ids)
            for k, ids in table.items()
            if len(k) == len(key) and sum(a != b for a, b in zip(k, key)) == 1
        ]
        if len(cands) == 1:
            return cands[0]
        # Repeated-character OCR confusion (e.g. 茸语嘿嘿 -> 茸语喁喁)
        if len(key) >= 4:
            rep_cands = []
            for k, ids in table.items():
                if len(k) != len(key):
                    continue
                diffs = [i for i, (a, b) in enumerate(zip(k, key)) if a != b]
                if len(diffs) == 2 and key[diffs[0]] == key[diffs[1]] and k[diffs[0]] == k[diffs[1]]:
                    rep_cands.append((k, ids))
            if len(rep_cands) == 1:
                return rep_cands[0]
    return None, []


def _load_badge_templates() -> dict[str, np.ndarray]:
    base = ROOT / "resource/image/sketch"
    loaded: dict[str, np.ndarray] = {}
    for key, filename in (
        ("suit_full", "badge_suit_full.png"),
        ("suit_part", "badge_suit_part.png"),
        ("piece_craft", "badge_piece_craftable.png"),
        ("crafted_check", "badge_crafted_check.png"),
    ):
        path = base / filename
        if path.is_file():
            img = cv2.imread(str(path))
            if img is not None and img.size > 0:
                loaded[key] = img
    return loaded


def _match_score(frame: np.ndarray, x0: int, y0: int, w: int, h: int, tmpl: np.ndarray | None) -> float:
    if tmpl is None or frame is None:
        return 0.0
    fh, fw = frame.shape[:2]
    ax0, ay0 = max(0, x0), max(0, y0)
    ax1, ay1 = min(fw, x0 + w), min(fh, y0 + h)
    if ax1 - ax0 < tmpl.shape[1] or ay1 - ay0 < tmpl.shape[0]:
        return 0.0
    patch = frame[ay0:ay1, ax0:ax1]
    res = cv2.matchTemplate(patch, tmpl, cv2.TM_CCOEFF_NORMED)
    return float(res.max())


def _is_crafted_suit_visual(frame: np.ndarray, col: int, name_y: int) -> bool:
    """Distinguish Copy-1-crafted suit cards (rich warm gold card background) from uncrafted cream-veiled cards.

    Calibrated on 20 real 720p cards: pure right-margin card background below the '新' tag
    has HSV saturation >= 77.8 & value <= 204.9 on lit Copy-1-crafted cards vs
    saturation <= 56.1 & value >= 222.0 on cream-veiled uncrafted cards.
    """
    bx0 = int(round(SUIT_BADGE_X0 + col * SUIT_COL_PITCH))
    by0 = int(name_y + SUIT_BADGE_DY)
    fh, fw = frame.shape[:2]
    y0, y1 = max(0, by0 + 55), min(fh, by0 + 110)
    x0, x1 = max(0, bx0 + 145), min(fw, bx0 + 164)
    if y1 - y0 < 16 or x1 - x0 < 8:
        return False
    hsv = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    mean_s = float(hsv[:, :, 1].mean())
    mean_v = float(hsv[:, :, 2].mean())
    return mean_s >= 68.0 and mean_v <= 214.0


def _detect_miracle_emblems(frame: np.ndarray) -> list[tuple[int, int]]:
    """Detect circular Miracle Suit switcher emblems in the top-right emblem bar."""
    if frame is None:
        return []
    rx, ry, rw, rh = MIRACLE_EMBLEM_ROI
    fh, fw = frame.shape[:2]
    ax0, ay0 = max(0, rx), max(0, ry)
    ax1, ay1 = min(fw, rx + rw), min(fh, ry + rh)
    if ax1 - ax0 < 40 or ay1 - ay0 < 30:
        return []
    bar = frame[ay0:ay1, ax0:ax1]
    gray = cv2.GaussianBlur(cv2.cvtColor(bar, cv2.COLOR_BGR2GRAY), (5, 5), 1.5)
    circles = cv2.HoughCircles(
        gray,
        cv2.HOUGH_GRADIENT,
        dp=1.2,
        minDist=45,
        param1=80,
        param2=24,
        minRadius=18,
        maxRadius=32,
    )
    if circles is None:
        return []
    centers: list[tuple[int, int]] = []
    for c in sorted(circles[0], key=lambda item: float(item[0])):
        cx, cy = int(round(ax0 + float(c[0]))), int(round(ay0 + float(c[1])))
        if not centers or abs(cx - centers[-1][0]) >= 42:
            centers.append((cx, cy))
    return centers


def _rect_xywh(box) -> tuple[int, int, int, int] | None:
    """Extract (x, y, w, h) from a MaaFramework Rect or 4-element sequence."""
    if box is None:
        return None
    if all(hasattr(box, attr) for attr in ("x", "y", "w", "h")):
        return int(box.x), int(box.y), int(box.w), int(box.h)
    try:
        vals = tuple(int(v) for v in box)
    except TypeError:
        return None
    return vals[:4] if len(vals) >= 4 else None


def _ocr_all(rt, roi: list[int], frame: np.ndarray | None = None) -> list[Any]:
    res = rt.recognize("MaaNikki_Daily_OCR", {"roi": roi, "expected": ".+"}, image=frame)
    if not res:
        return []
    rows = getattr(res, "all_results", None) or getattr(res, "filtered_results", None) or []
    return list(rows)


def _iter_strips(frame_height: int, top: int, bottom_margin: int = 30) -> list[tuple[int, int]]:
    """Split a tall frame into overlapping <=720px horizontal strips for sharp OCR."""
    bottom = max(top + 100, frame_height - bottom_margin)
    if bottom - top <= STRIP_HEIGHT:
        return [(top, bottom - top)]
    strips: list[tuple[int, int]] = []
    step = STRIP_HEIGHT - STRIP_OVERLAP
    y = top
    while y < bottom:
        h = min(STRIP_HEIGHT, bottom - y)
        strips.append((y, h))
        if y + h >= bottom:
            break
        y += step
    return strips


def _park_point(rt) -> list[int]:
    hwnd, cap_size = rt._pointer_guard_context()
    return clamp_capture_park_point(hwnd, [640, 36], capture_size=cap_size)


class SketchScanner:
    """Scan the in-game 设计图 page to record unlocked & craftable sketches (Copy 1 & Copy 2)."""

    def __init__(self, rt, account: AccountState | None = None):
        self.rt = rt
        self.account = account or AccountState.for_runtime(ROOT, rt)
        self.by_suit, self.by_piece, self.suit_id_by_key = _load_crafting_index()
        role = self.account.load()
        self.evolvable_suits, self.base_owned_suits, self.final_evo_owned_suits = _load_suit_evo_ownership(
            role, self.by_suit
        )
        self.templates = _load_badge_templates()
        self.unlocked_ids: set[str] = set()
        self.game_craftable_ids: set[str] = set()
        self.game_full_suit_ids: set[str] = set()
        self.game_partial_suit_ids: set[str] = set()
        self.game_evo_full_suit_ids: set[str] = set()
        self.game_evo_partial_suit_ids: set[str] = set()
        self.evo_crafted_suit_ids: set[str] = set()
        self.suit_progress: dict[str, list[Any]] = {}
        self.extra_craftable: dict[tuple[str, str], dict[str, str]] = {}
        self.scanned_labels: set[str] = set()
        self.unmatched_labels: set[str] = set()
        self.summary: dict[str, Any] = {
            "complete": False,
            "tall_mode": False,
            "unlocked_count": 0,
            "game_craftable_count": 0,
            "game_full_suit_count": 0,
            "game_partial_suit_count": 0,
            "game_evo_full_suit_count": 0,
            "game_evo_partial_suit_count": 0,
            "extra_craftable_count": 0,
            "scanned_count": 0,
            "unmatched_count": 0,
            "reason": None,
        }

    def _open_sketch_page(self) -> bool:
        frame = self.rt.capture()
        if self.rt.hit("MaaNikki_SketchReady", image=frame):
            return True
        if self.rt.ui.find_menu_entry(node="MaaNikki_Sketch_Entrance", scroll=True):
            return True
        if not self.rt.ui.menu():
            return False
        frame = self.rt.capture()
        for arrow in (PEAR_PAL_PREV_PAGE, PEAR_PAL_PREV_PAGE, PEAR_PAL_NEXT_PAGE, PEAR_PAL_NEXT_PAGE):
            if self.rt.hit("MaaNikki_Sketch_Entrance", image=frame):
                break
            if not self.rt.action("Click", target=arrow) or not self.rt.pause(0.45):
                return False
            frame = self.rt.capture()
            if not self.rt.hit("MaaNikki_MenuReady", image=frame):
                return self.rt.hit("MaaNikki_SketchReady", image=frame)
        if not self.rt.hit("MaaNikki_Sketch_Entrance", image=frame):
            return False
        return bool(
            self.rt.ui.enter_page(
                lambda: self.rt.click_template("MaaNikki_Sketch_Entrance", verify=False),
                "MaaNikki_SketchReady",
                source="MaaNikki_MenuReady",
            )
        )

    def _click_tab(self, label: str) -> bool:
        res = self.rt.recognize("MaaNikki_Daily_OCR", {"roi": TAB_LIST_ROI, "expected": re.escape(label)})
        if not res or not res.hit:
            return False
        xywh = _rect_xywh(getattr(res, "box", None))
        if xywh is None:
            return False
        bx, by, bw, bh = xywh
        self.rt.action("Click", target=[int(bx + bw / 2), int(by + bh / 2)])
        self.rt.pause(0.55)
        return True

    def _scroll_grid(self, dy: int, wait_s: float = 0.35) -> np.ndarray | None:
        if (
            not self.rt.action("TouchMove", target=SCROLL_POINT)
            or not self.rt.pause(0.08)
            or not self.rt.action("Scroll", target=SCROLL_POINT, dx=0, dy=dy)
            or not self.rt.pause(wait_s)
            or not self.rt.action("TouchMove", target=_park_point(self.rt))
            or not self.rt.pause(0.1)
        ):
            return None
        return self.rt.capture()

    def _grid_compare_slice(self, frame: np.ndarray, x: int, top: int, w: int) -> np.ndarray:
        bottom = min(frame.shape[0] - 30, top + 2400)
        return frame[top:bottom, x:x + w]

    def _scroll_to_top(self, x: int, top: int, w: int, is_tall: bool) -> np.ndarray | None:
        frame = self.rt.capture()
        if frame is None:
            return None
        up_dy = 24000 if is_tall else 14400
        for _ in range(6):
            if self.rt.stopped:
                return None
            prev = self._grid_compare_slice(frame, x, top, w).copy()
            nxt = self._scroll_grid(up_dy, wait_s=0.25)
            if nxt is None:
                return None
            curr = self._grid_compare_slice(nxt, x, top, w)
            frame = nxt
            if curr.shape == prev.shape and float(np.mean(np.abs(curr.astype(np.float32) - prev.astype(np.float32)))) < 1.5:
                break
        return frame

    def _record_miracle_view(self, frame: np.ndarray) -> None:
        name_rows = _ocr_all(self.rt, MIRACLE_NAME_ROI, frame)
        prog_rows = _ocr_all(self.rt, MIRACLE_PROGRESS_ROI, frame)
        prog_raw = " ".join(str(getattr(r, "text", "") or "").strip() for r in prog_rows).strip()
        for row in name_rows:
            raw = str(getattr(row, "text", "") or "").strip()
            key = _norm(raw)
            matched_key, ids = _lookup_name(key, self.by_suit)
            if matched_key and ids:
                self.scanned_labels.add(f"suit:{matched_key}")
                self.unlocked_ids.update(ids)
                parsed = _parse_progress(prog_raw, expected_total=len(ids))
                suit_id = self.suit_id_by_key.get(matched_key)
                if parsed is not None and suit_id:
                    stage = "evo" if matched_key in self.base_owned_suits else "base"
                    self.suit_progress[suit_id] = [parsed[0], parsed[1], stage]

    def _scan_miracle_tab(self) -> None:
        if not self._click_tab("奇迹套装"):
            return
        frame = self.rt.capture()
        if frame is None:
            return
        visited_regions: set[str] = set()
        for sweep in range(2):
            if self.rt.stopped:
                break
            region_rows = _ocr_all(self.rt, MIRACLE_REGION_ROI, frame)
            region_tabs: list[tuple[str, tuple[int, int, int, int]]] = []
            for r in region_rows:
                xywh = _rect_xywh(getattr(r, "box", None))
                text = str(getattr(r, "text", "") or "").strip()
                norm_t = _norm(text)
                if xywh is not None and _has_cjk(text) and norm_t not in visited_regions:
                    region_tabs.append((norm_t, xywh))
            if not region_tabs and sweep == 0:
                self._record_miracle_view(frame)
                break
            if not region_tabs:
                break
            for rkey, box in region_tabs:
                if self.rt.stopped:
                    break
                visited_regions.add(rkey)
                self.rt.action("Click", target=[int(box[0] + box[2] / 2), int(box[1] + box[3] / 2)])
                self.rt.pause(0.45)
                cur = self.rt.capture()
                if cur is None:
                    continue
                emblems = _detect_miracle_emblems(cur)
                if len(emblems) >= 2:
                    for ex, ey in emblems:
                        if self.rt.stopped:
                            break
                        self.rt.action("Click", target=[ex, ey])
                        self.rt.pause(0.4)
                        view = self.rt.capture()
                        if view is not None:
                            self._record_miracle_view(view)
                else:
                    self._record_miracle_view(cur)
            # Scroll the horizontal region bar slightly right on sweep 0 in case extra regions (e.g. 西西亚) sit off-edge.
            if sweep == 0 and not self.rt.stopped:
                self.rt.action("Swipe", begin=[720, 38], end=[340, 38], duration=300)
                self.rt.pause(0.35)
                nxt = self.rt.capture()
                if nxt is None:
                    break
                frame = nxt

    def _classify_suit_badge(self, frame: np.ndarray, col: int, name_y: int) -> str:
        bx0 = int(round(SUIT_BADGE_X0 + col * SUIT_COL_PITCH)) - 4
        by0 = int(name_y + SUIT_BADGE_DY) - 4
        sf = _match_score(frame, bx0, by0, 56, 56, self.templates.get("suit_full"))
        sp = _match_score(frame, bx0, by0, 56, 56, self.templates.get("suit_part"))
        if max(sf, sp) < SUIT_BADGE_THRESHOLD:
            return "none"
        return "full" if sf > sp else "partial"

    def _ocr_strips_merged(
        self, frame: np.ndarray, grid_x: int, grid_top: int, grid_w: int
    ) -> tuple[list[tuple[str, tuple[int, int, int, int]]], bool]:
        """OCR all horizontal strips of a 720p or tall frame, deduplicating in full-frame coords."""
        strips = _iter_strips(frame.shape[0], grid_top)
        merged: list[tuple[str, tuple[int, int, int, int]]] = []
        seen_cells: set[tuple[str, int, int]] = set()
        saw_any = False
        empty_after_hit = 0
        reached_end = False

        for sy, sh in strips:
            if self.rt.stopped:
                break
            rows = _ocr_all(self.rt, [grid_x, sy, grid_w, sh], frame)
            strip_useful = 0
            for row in rows:
                raw = str(getattr(row, "text", "") or "").strip()
                xywh = _rect_xywh(getattr(row, "box", None))
                if not raw or xywh is None:
                    continue
                bx, by, bw, bh = xywh
                cx = bx + bw * 0.5
                cy = by + bh * 0.5
                dedup_key = (_norm(raw), int(round(cx / 14.0)), int(round(cy / 14.0)))
                if dedup_key not in seen_cells:
                    seen_cells.add(dedup_key)
                    merged.append((raw, xywh))
                cleaned = re.sub(r"\s+", "", raw)
                if (
                    _RE_PROGRESS.match(cleaned)
                    or _RE_PROGRESS_SLASH_CONFUSED.match(cleaned)
                    or (len(raw) >= 2 and _has_cjk(raw) and not _RE_IGNORE.match(raw))
                ):
                    strip_useful += 1
            if strip_useful > 0:
                saw_any = True
                empty_after_hit = 0
            elif saw_any:
                empty_after_hit += 1
                if empty_after_hit >= 2:
                    reached_end = True
                    break
        if saw_any and len(strips) > 1 and empty_after_hit >= 1:
            reached_end = True
        return merged, reached_end

    def _scan_suit_frame(self, frame: np.ndarray) -> bool:
        """Scan all visible rows in a 720p or tall-mode suit frame; returns True if list ended mid-frame."""
        rows, reached_end = self._ocr_strips_merged(frame, SUIT_GRID_X, SUIT_GRID_TOP, SUIT_GRID_W)
        progress_boxes: list[tuple[float, float, str]] = []
        name_candidates: list[tuple[str, tuple[int, int, int, int]]] = []
        for raw, xywh in rows:
            bx, by, bw, bh = xywh
            cleaned = re.sub(r"\s+", "", raw)
            if _RE_PROGRESS.match(cleaned) or _RE_PROGRESS_SLASH_CONFUSED.match(cleaned):
                progress_boxes.append((bx + bw * 0.5, by + bh * 0.5, cleaned))
                continue
            if len(raw) < 2 or _RE_IGNORE.match(raw) or not _has_cjk(raw):
                continue
            name_candidates.append((raw, xywh))

        for raw, box in name_candidates:
            nx, ny, nw, nh = box
            # Card must have enough headroom above its title for the top-left badge to be visible.
            if ny < 250:
                continue
            cx = nx + nw * 0.5
            col = max(0, min(4, int(round((cx - SUIT_COL_CX0) / SUIT_COL_PITCH))))
            col_cx = SUIT_COL_CX0 + col * SUIT_COL_PITCH
            if abs(cx - col_cx) > 85:
                continue
            matched_key, ids = _lookup_name(_norm(raw), self.by_suit)
            if not matched_key or not ids:
                if len(raw) >= 2:
                    self.unmatched_labels.add(raw)
                continue
            self.scanned_labels.add(f"suit:{matched_key}")
            self.unlocked_ids.update(ids)

            owned_cnt = None
            total_cnt = None
            for pcx, pcy, praw in progress_boxes:
                if abs(pcx - col_cx) <= 75 and (ny - 55) <= pcy <= (ny - 6):
                    parsed = _parse_progress(praw, expected_total=len(ids))
                    if parsed is not None:
                        owned_cnt, total_cnt = parsed
                        break

            # Distinguish Copy 1 vs Copy 2 (evolution copy) using BOTH visual cues
            # (lit full-color portrait / N/N counter) and wardrobe ownership.
            visual_copy1_done = _is_crafted_suit_visual(frame, col, ny)
            counter_full = owned_cnt is not None and total_cnt is not None and owned_cnt >= total_cnt
            copy1_done = visual_copy1_done or counter_full or (matched_key in self.base_owned_suits)
            suit_id = self.suit_id_by_key.get(matched_key)
            if owned_cnt is not None and total_cnt is not None and suit_id:
                self.suit_progress[suit_id] = [owned_cnt, total_cnt, "evo" if copy1_done else "base"]

            badge = self._classify_suit_badge(frame, col, ny)
            if copy1_done:
                # Copy 1 is complete; only evolvable suits whose final evo isn't owned need Copy 2.
                if matched_key in self.evolvable_suits and matched_key not in self.final_evo_owned_suits:
                    if badge == "full":
                        self.game_evo_full_suit_ids.update(ids)
                    elif badge == "partial":
                        self.game_evo_partial_suit_ids.update(ids)
                continue

            if badge == "full":
                self.game_full_suit_ids.update(ids)
                self.game_craftable_ids.update(ids)
            elif badge == "partial":
                self.game_partial_suit_ids.update(ids)
        return reached_end

    def _scan_item_frame(self, frame: np.ndarray, tab_label: str) -> bool:
        """Scan all visible rows in a 720p or tall-mode item frame; returns True if list ended mid-frame."""
        rows, reached_end = self._ocr_strips_merged(frame, ITEM_GRID_X, ITEM_GRID_TOP, ITEM_GRID_W)
        for raw, xywh in rows:
            if len(raw) < 2 or _RE_IGNORE.match(raw) or not _has_cjk(raw):
                continue
            nx, ny, nw, nh = xywh
            if ny < 205:
                continue
            cx = nx + nw * 0.5
            col = max(0, min(5, int(round((cx - ITEM_COL_CX0) / ITEM_COL_PITCH))))
            col_cx = ITEM_COL_CX0 + col * ITEM_COL_PITCH
            if abs(cx - col_cx) > 75:
                continue

            bx0 = int(round(ITEM_BADGE_X0 + col * ITEM_COL_PITCH))
            by0 = int(ny + ITEM_BADGE_DY)
            craft_score = _match_score(frame, bx0 - 4, by0 - 4, 56, 56, self.templates.get("piece_craft"))
            crafted_score = _match_score(
                frame, bx0 + ITEM_CRAFTED_DX - 4, int(ny + ITEM_CRAFTED_DY) - 4, 56, 56, self.templates.get("crafted_check")
            )
            is_craftable = craft_score >= ITEM_BADGE_THRESHOLD
            is_crafted = crafted_score >= CRAFTED_CHECK_THRESHOLD

            key = _norm(raw)
            matched_key, ids = _lookup_name(key, self.by_piece)
            if matched_key and ids:
                self.scanned_labels.add(f"piece:{matched_key}")
                self.unlocked_ids.update(ids)
                if is_craftable and not is_crafted:
                    self.game_craftable_ids.update(ids)
            else:
                if is_craftable and not is_crafted:
                    self.scanned_labels.add(f"extra:{tab_label}:{key}")
                    self.extra_craftable[(tab_label, raw)] = {"tab": tab_label, "name": raw}
                elif tab_label == "珍奇单品":
                    self.unmatched_labels.add(raw)
        return reached_end

    def _scroll_and_scan_grid(self, tab_kind: str, label: str, is_tall: bool) -> None:
        if not self._click_tab(label):
            return
        gx = SUIT_GRID_X if tab_kind == "suit" else ITEM_GRID_X
        gw = SUIT_GRID_W if tab_kind == "suit" else ITEM_GRID_W
        gtop = SUIT_GRID_TOP if tab_kind == "suit" else ITEM_GRID_TOP
        # Keep generous vertical overlap (~1000-1600px in 5120px tall mode) so no row slips at page seams.
        scroll_dy = -6400 if is_tall else (-840 if tab_kind == "suit" else -720)
        max_pages = 16 if is_tall else (40 if tab_kind == "suit" else 75)

        frame = self._scroll_to_top(gx, gtop, gw, is_tall)
        if frame is None:
            return

        stagnant = 0
        for _ in range(max_pages):
            if self.rt.stopped:
                break
            reached_end = (
                self._scan_suit_frame(frame)
                if tab_kind == "suit"
                else self._scan_item_frame(frame, label)
            )
            if reached_end:
                break
            prev = self._grid_compare_slice(frame, gx, gtop, gw).copy()
            nxt = self._scroll_grid(scroll_dy, wait_s=0.35)
            if nxt is None:
                break
            curr = self._grid_compare_slice(nxt, gx, gtop, gw)
            frame = nxt
            if curr.shape == prev.shape:
                diff = float(np.mean(np.abs(curr.astype(np.float32) - prev.astype(np.float32))))
                if diff < 1.5:
                    stagnant += 1
                    if stagnant >= 2:
                        break
                else:
                    stagnant = 0

    def save(self, reason: str | None = None, *, complete: bool = False) -> None:
        now = datetime.now(timezone.utc).isoformat()
        extra_list = [self.extra_craftable[k] for k in sorted(self.extra_craftable)]
        payload = {
            "updated_at": now,
            "complete": bool(complete),
            "tall_mode": bool(self.summary.get("tall_mode")),
            "unlocked_ids": sorted(self.unlocked_ids),
            "game_craftable_ids": sorted(self.game_craftable_ids),
            "game_full_suit_ids": sorted(self.game_full_suit_ids),
            "game_partial_suit_ids": sorted(self.game_partial_suit_ids),
            "game_evo_full_suit_ids": sorted(self.game_evo_full_suit_ids),
            "game_evo_partial_suit_ids": sorted(self.game_evo_partial_suit_ids),
            "evo_crafted_suit_ids": sorted(self.evo_crafted_suit_ids),
            "suit_progress": dict(self.suit_progress),
            "extra_craftable": extra_list,
            "scanned_count": len(self.scanned_labels),
            "unmatched_count": len(self.unmatched_labels),
            "unmatched_samples": sorted(self.unmatched_labels)[:30],
            "reason": reason,
        }
        self.summary.update(
            complete=bool(complete),
            unlocked_count=len(self.unlocked_ids),
            game_craftable_count=len(self.game_craftable_ids),
            game_full_suit_count=len(self.game_full_suit_ids),
            game_partial_suit_count=len(self.game_partial_suit_ids),
            game_evo_full_suit_count=len(self.game_evo_full_suit_ids),
            game_evo_partial_suit_count=len(self.game_evo_partial_suit_ids),
            extra_craftable_count=len(extra_list),
            scanned_count=len(self.scanned_labels),
            unmatched_count=len(self.unmatched_labels),
            reason=reason,
        )

        def _write(role: dict) -> None:
            mats = role.setdefault("materials", {})
            mats["sketch_scan"] = payload

        self.account.update(_write)

    def run(self) -> bool:
        try:
            if not self._open_sketch_page():
                self.save("未能打开设计图页面", complete=False)
                return False
            self.rt.log("设计图扫描：正在读取奇迹套装…", live=True)
            self._scan_miracle_tab()
            with tall_window(self.rt) as is_tall:
                self.summary["tall_mode"] = bool(is_tall)
                if is_tall:
                    self.rt.log("设计图扫描：已切换超长竖屏模式，正在加速读取各分类设计图。")
                for tab_kind, label in GRID_TABS:
                    if self.rt.stopped:
                        break
                    self.rt.log(f"设计图扫描：正在读取「{label}」…", live=True)
                    self._scroll_and_scan_grid(tab_kind, label, bool(is_tall))
            complete = not self.rt.stopped
            self.save(None if complete else "用户停止，已保存已读取的设计图进度", complete=complete)
            return complete
        except TaskStopped:
            self.save("用户停止，已保存已读取的设计图进度", complete=False)
            raise
