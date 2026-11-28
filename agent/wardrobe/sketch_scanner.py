"""Read-only in-game 设计图 (Sketch) scanner to sync unlocked and craftable blueprints."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from typing import Any

import cv2
import numpy as np

from account_state import AccountState
from daily.settings import ROOT
from data_paths import data_path
from task_control import TaskStopped

# ---------------------------------------------------------------------------
# 1280x720 UI layout constants (calibrated against 3.0 设计图 screenshots)
# ---------------------------------------------------------------------------
TAB_LIST_ROI = [30, 95, 165, 565]
MIRACLE_REGION_ROI = [210, 14, 900, 50]
MIRACLE_NAME_ROI = [330, 545, 290, 65]
MIRACLE_PROGRESS_ROI = [1135, 125, 130, 50]

SUIT_GRID_ROI = [225, 65, 1020, 625]
ITEM_GRID_ROI = [215, 60, 1030, 635]
SCROLL_POINT = [640, 360]
PARK_POINT = [180, 690]

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


def _load_crafting_index() -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Build normalized name lookup tables from crafting_catalog.json."""
    catalog = json.loads(data_path("resource/data/crafting_catalog.json").read_text(encoding="utf-8"))
    by_suit: dict[str, list[str]] = {}
    by_piece: dict[str, list[str]] = {}
    for sk in catalog.get("sketches") or []:
        sid = str(sk["id"])
        piece_key = _norm(str(sk.get("name") or ""))
        if piece_key:
            by_piece.setdefault(piece_key, []).append(sid)
        if sk.get("category") == "suit" and sk.get("suit_name"):
            suit_key = _norm(str(sk["suit_name"]))
            if suit_key:
                by_suit.setdefault(suit_key, []).append(sid)
    return by_suit, by_piece


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
        # Repeated-character OCR confusion (e.g. 茸语嘿嘿 -> 茸语喁喁): both differing
        # positions must be identical repeated characters in both source and target.
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


def _ocr_all(rt, roi: list[int], frame: np.ndarray | None = None) -> list[Any]:
    res = rt.recognize("MaaNikki_Daily_OCR", {"roi": roi, "expected": ".+"}, image=frame)
    if not res:
        return []
    rows = getattr(res, "all_results", None) or getattr(res, "filtered_results", None) or []
    return list(rows)


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


class SketchScanner:
    """Scan the in-game 设计图 page to record unlocked & craftable sketches."""

    def __init__(self, rt, account: AccountState | None = None):
        self.rt = rt
        self.account = account or AccountState.for_runtime(ROOT, rt)
        self.by_suit, self.by_piece = _load_crafting_index()
        self.templates = _load_badge_templates()
        self.unlocked_ids: set[str] = set()
        self.game_craftable_ids: set[str] = set()
        self.game_full_suit_ids: set[str] = set()
        self.game_partial_suit_ids: set[str] = set()
        self.extra_craftable: dict[tuple[str, str], dict[str, str]] = {}
        self.scanned_labels: set[str] = set()
        self.unmatched_labels: set[str] = set()
        self.summary: dict[str, Any] = {
            "complete": False,
            "unlocked_count": 0,
            "game_craftable_count": 0,
            "game_full_suit_count": 0,
            "game_partial_suit_count": 0,
            "extra_craftable_count": 0,
            "scanned_count": 0,
            "unmatched_count": 0,
            "reason": None,
        }

    def _open_sketch_page(self) -> bool:
        if self.rt.hit("MaaNikki_SketchReady"):
            return True
        if not self.rt.ui.menu():
            return False
        return bool(self.rt.ui.enter_page("MaaNikki_Sketch_Entrance", "MaaNikki_SketchReady", attempts=2))

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

    def _scroll_grid(self, dy: int, wait_s: float = 0.4) -> np.ndarray | None:
        if (
            not self.rt.action("TouchMove", target=SCROLL_POINT)
            or not self.rt.pause(0.08)
            or not self.rt.action("Scroll", target=SCROLL_POINT, dx=0, dy=dy)
            or not self.rt.pause(wait_s)
            or not self.rt.action("TouchMove", target=PARK_POINT)
            or not self.rt.pause(0.1)
        ):
            return None
        return self.rt.capture()

    def _scroll_to_top(self, grid_roi: list[int]) -> np.ndarray | None:
        frame = self.rt.capture()
        if frame is None:
            return None
        gx, gy, gw, gh = grid_roi
        for _ in range(6):
            if self.rt.stopped:
                return None
            prev = frame[gy:gy + gh, gx:gx + gw].copy()
            nxt = self._scroll_grid(4800, wait_s=0.25)
            if nxt is None:
                return None
            curr = nxt[gy:gy + gh, gx:gx + gw]
            frame = nxt
            if float(np.mean(np.abs(curr.astype(np.float32) - prev.astype(np.float32)))) < 1.5:
                break
        return frame

    def _scan_miracle_tab(self) -> None:
        if not self._click_tab("奇迹套装"):
            return
        frame = self.rt.capture()
        if frame is None:
            return
        region_rows = _ocr_all(self.rt, MIRACLE_REGION_ROI, frame)
        region_tabs = []
        for r in region_rows:
            xywh = _rect_xywh(getattr(r, "box", None))
            text = str(getattr(r, "text", "") or "").strip()
            if xywh is not None and _has_cjk(text):
                region_tabs.append((text, xywh))
        if not region_tabs:
            region_tabs = [("当前", None)]
        for _rname, box in region_tabs:
            if self.rt.stopped:
                break
            if box is not None:
                self.rt.action("Click", target=[int(box[0] + box[2] / 2), int(box[1] + box[3] / 2)])
                self.rt.pause(0.45)
            cur = self.rt.capture()
            if cur is None:
                continue
            name_rows = _ocr_all(self.rt, MIRACLE_NAME_ROI, cur)
            for row in name_rows:
                raw = str(getattr(row, "text", "") or "").strip()
                key = _norm(raw)
                matched_key, ids = _lookup_name(key, self.by_suit)
                if matched_key and ids:
                    self.scanned_labels.add(f"suit:{matched_key}")
                    self.unlocked_ids.update(ids)

    def _classify_suit_badge(self, frame: np.ndarray, col: int, name_y: int) -> str:
        bx0 = int(round(SUIT_BADGE_X0 + col * SUIT_COL_PITCH))
        by0 = int(name_y + SUIT_BADGE_DY)
        sf = _match_score(frame, bx0, by0, 48, 48, self.templates.get("suit_full"))
        sp = _match_score(frame, bx0, by0, 48, 48, self.templates.get("suit_part"))
        if max(sf, sp) < SUIT_BADGE_THRESHOLD:
            return "none"
        return "full" if sf > sp else "partial"

    def _scan_suit_frame(self, frame: np.ndarray) -> None:
        rows = _ocr_all(self.rt, SUIT_GRID_ROI, frame)
        progress_boxes: list[tuple[float, float, str]] = []
        name_candidates: list[tuple[str, tuple[int, int, int, int]]] = []
        for row in rows:
            raw = str(getattr(row, "text", "") or "").strip()
            xywh = _rect_xywh(getattr(row, "box", None))
            if not raw or xywh is None:
                continue
            bx, by, bw, bh = xywh
            cleaned = re.sub(r"\s+", "", raw)
            if _RE_PROGRESS.match(cleaned) or _RE_PROGRESS_SLASH_CONFUSED.match(cleaned):
                cx = bx + bw * 0.5
                cy = by + bh * 0.5
                progress_boxes.append((cx, cy, cleaned))
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
                continue
            tag = f"suit:{matched_key}"
            self.scanned_labels.add(tag)
            self.unlocked_ids.update(ids)

            owned_cnt = None
            total_cnt = None
            for pcx, pcy, praw in progress_boxes:
                if abs(pcx - col_cx) <= 75 and (ny - 55) <= pcy <= (ny - 6):
                    parsed = _parse_progress(praw, expected_total=len(ids))
                    if parsed is not None:
                        owned_cnt, total_cnt = parsed
                        break

            # Already fully crafted suits (e.g. 8/8) show badges for 2nd evolution copies; skip as uncrafted.
            if owned_cnt is not None and total_cnt is not None and owned_cnt >= total_cnt:
                continue

            badge = self._classify_suit_badge(frame, col, ny)
            if badge == "full":
                self.game_full_suit_ids.update(ids)
                self.game_craftable_ids.update(ids)
            elif badge == "partial":
                self.game_partial_suit_ids.update(ids)

    def _scan_item_frame(self, frame: np.ndarray, tab_label: str) -> None:
        rows = _ocr_all(self.rt, ITEM_GRID_ROI, frame)
        for row in rows:
            raw = str(getattr(row, "text", "") or "").strip()
            xywh = _rect_xywh(getattr(row, "box", None))
            if not raw or xywh is None:
                continue
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
            craft_score = _match_score(frame, bx0, by0, 48, 48, self.templates.get("piece_craft"))
            crafted_score = _match_score(
                frame, bx0 + ITEM_CRAFTED_DX, int(ny + ITEM_CRAFTED_DY), 48, 48, self.templates.get("crafted_check")
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
            elif is_craftable and not is_crafted:
                # Non-wardrobe craftable blueprints (makeup, 奇想香氛, 大喵斗篷)
                self.scanned_labels.add(f"extra:{tab_label}:{key}")
                self.extra_craftable[(tab_label, raw)] = {"tab": tab_label, "name": raw}

    def _scroll_and_scan_grid(self, tab_kind: str, label: str) -> None:
        if not self._click_tab(label):
            return
        grid_roi = SUIT_GRID_ROI if tab_kind == "suit" else ITEM_GRID_ROI
        scroll_dy = -960 if tab_kind == "suit" else -840
        max_pages = 35 if tab_kind == "suit" else 65
        frame = self._scroll_to_top(grid_roi)
        if frame is None:
            return
        gx, gy, gw, gh = grid_roi
        stagnant = 0
        for _ in range(max_pages):
            if self.rt.stopped:
                break
            if tab_kind == "suit":
                self._scan_suit_frame(frame)
            else:
                self._scan_item_frame(frame, label)
            prev = frame[gy:gy + gh, gx:gx + gw].copy()
            nxt = self._scroll_grid(scroll_dy, wait_s=0.38)
            if nxt is None:
                break
            curr = nxt[gy:gy + gh, gx:gx + gw]
            diff = float(np.mean(np.abs(curr.astype(np.float32) - prev.astype(np.float32))))
            frame = nxt
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
            "unlocked_ids": sorted(self.unlocked_ids),
            "game_craftable_ids": sorted(self.game_craftable_ids),
            "game_full_suit_ids": sorted(self.game_full_suit_ids),
            "game_partial_suit_ids": sorted(self.game_partial_suit_ids),
            "extra_craftable": extra_list,
            "scanned_count": len(self.scanned_labels),
            "unmatched_count": len(self.unmatched_labels),
            "reason": reason,
        }
        self.summary.update(
            complete=bool(complete),
            unlocked_count=len(self.unlocked_ids),
            game_craftable_count=len(self.game_craftable_ids),
            game_full_suit_count=len(self.game_full_suit_ids),
            game_partial_suit_count=len(self.game_partial_suit_ids),
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
            self.rt.log("设计图扫描：正在遍历奇迹套装、能力套装、风格套装、珍奇单品、奇想香氛与大喵斗篷。", live=True)
            self._scan_miracle_tab()
            for tab_kind, label in GRID_TABS:
                if self.rt.stopped:
                    break
                self.rt.log(f"设计图扫描：正在读取「{label}」…", live=True)
                self._scroll_and_scan_grid(tab_kind, label)
            complete = not self.rt.stopped
            self.save(None if complete else "用户停止，已保存已读取的设计图进度", complete=complete)
            return complete
        except TaskStopped:
            self.save("用户停止，已保存已读取的设计图进度", complete=False)
            raise
