"""Full tall-window backpack scanner for Tab 1 (材料), Tab 2 (养成材料), and Bling."""
from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
import json
from pathlib import Path
import re

import cv2
import numpy as np

from account_state import AccountState, game_day
from daily.settings import ROOT
from tall_window import tall_window


HEADER_ROI = [65, 15, 240, 48]
BLING_ROI = [1052, 16, 105, 34]
RIGHT_TITLE_ROI = [895, 80, 345, 52]
TAB_MATERIALS = [457, 36]
TAB_GROWTH = [539, 36]
GRID_SCROLL_TARGET = [420, 360]

FIRST_X = 42
COL_PITCH = 96
ICON_SIZE = 84
CARD_HEIGHT = 90
ROW_PITCH_EXACT = 170.0 * 2.0 / 3.0
GRID_TOP = 115
GRID_BOTTOM_MARGIN = 80

_SHIFTS = [(dy, dx) for dy in (-2, -1, 0, 1, 2) for dx in (-2, -1, 0, 1, 2)]
_VALID84 = np.ones((84, 84), dtype=np.float32)
_VALID84[63:, 30:] = 0.0
_VALID84[:2, :] = 0.0
_VALID84[-2:, :] = 0.0
_VALID84[:, :2] = 0.0
_VALID84[:, -2:] = 0.0
_VALID42 = _VALID84[::2, ::2]
_VALID28 = cv2.resize(_VALID84, (28, 28), interpolation=cv2.INTER_AREA) > 0.5


def current_game_day() -> str:
    return game_day().isoformat()


def wardrobe_snapshot(role: dict) -> dict:
    wardrobe = role.get("wardrobe") or {}
    snapshot = wardrobe.get("inventory") if (wardrobe.get("inventory") or {}).get("complete") else (
        wardrobe.get("last_scan") or wardrobe.get("inventory") or {})
    return snapshot if isinstance(snapshot, dict) else {}


def renewal_source(mats: dict) -> str:
    """Which data drives the 焕新 goal: the wardrobe plan, or fixed material targets."""
    plan = mats.get("renewal_plan") if isinstance(mats.get("renewal_plan"), dict) else None
    if plan and plan.get("source") in ("plan", "targets"):
        return plan["source"]
    if plan and plan.get("enabled"):
        return "plan"
    glow_plan = mats.get("glow_plan") if isinstance(mats.get("glow_plan"), dict) else None
    if (glow_plan and glow_plan.get("enabled", True) and not glow_plan.get("completed")
            and any(int(value or 0) > 0 for value in (glow_plan.get("targets") or {}).values())):
        return "targets"
    return "plan"


def wardrobe_snapshot_key(role: dict) -> str:
    snapshot = wardrobe_snapshot(role)
    if not snapshot:
        return ""
    return f"{snapshot.get('updated_at') or ''}:{snapshot.get('scanned_count') or 0}"


def normalize_name(text: str) -> str:
    return re.sub(r"[\s·・•.。:：\-_]", "", text or "")


def parse_badge_quantity(raw: str) -> int | None:
    cleaned = re.sub(r"\s+", "", raw or "").upper().replace("O", "0")
    match = re.search(r"(\d+(?:\.\d+)?)\s*([K万W]?)", cleaned)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2)
    if unit == "K":
        return int(round(value * 1000))
    if unit in ("万", "W"):
        return int(round(value * 10000))
    return int(round(value))


class BackpackModel:
    """Packed game icon templates & catalog metadata for Backpack Tab 1 + Tab 2."""

    def __init__(self, directory: Path):
        catalog = json.loads((directory / "catalog.json").read_text(encoding="utf-8"))
        self.bling_id = str(catalog.get("bling_id") or "992617511")
        self.items: dict[str, dict] = catalog["items"]
        self.templates: list[dict] = catalog["templates"]
        bgra = np.load(directory / "icons.npz")["bgra"]
        self.a_all = bgra[:, :, :, 3].astype(np.float32) / 255.0
        self.fg_all = bgra[:, :, :, :3].astype(np.float32)
        self.w_all = np.clip(self.a_all, 0.0, 1.0) * _VALID84[None, :, :]
        self.tab_indices: dict[int, np.ndarray] = {}
        self.coarse_mats: dict[int, np.ndarray] = {}
        self.by_norm_tab: dict[int, dict[str, list[str]]] = {1: {}, 2: {}}
        self.by_norm_all: dict[str, list[str]] = {}

        for iid, meta in self.items.items():
            norm = normalize_name(meta.get("name", ""))
            if not norm:
                continue
            tab = int(meta.get("tab") or 1)
            self.by_norm_tab.setdefault(tab, {}).setdefault(norm, []).append(iid)
            self.by_norm_all.setdefault(norm, []).append(iid)

        for tab in (1, 2):
            idxs = np.array([i for i, t in enumerate(self.templates) if t["tab"] == tab], dtype=np.int32)
            self.tab_indices[tab] = idxs
            vecs = []
            for i in idxs:
                # Alpha-weighted Gaussian blur keeps thin leaf/twig templates invariant to +/-2px shifts.
                num = cv2.GaussianBlur(self.fg_all[i] * self.w_all[i, :, :, None], (5, 5), 1.4)
                den = np.maximum(cv2.GaussianBlur(self.w_all[i], (5, 5), 1.4)[:, :, None], 1e-4)
                sm = np.where(self.w_all[i, :, :, None] > 0.02, num / den, 0.0)
                sm28 = cv2.resize(sm, (28, 28), interpolation=cv2.INTER_AREA)
                w28 = (cv2.resize(self.w_all[i], (28, 28), interpolation=cv2.INTER_AREA) * _VALID28).astype(np.float32)
                ws = float(w28.sum()) + 1e-6
                m28 = (sm28 * w28[:, :, None]).sum(axis=(0, 1)) / ws
                v = ((sm28 - m28) * w28[:, :, None]).reshape(-1)
                v /= (np.linalg.norm(v) + 1e-6)
                vecs.append(v)
            self.coarse_mats[tab] = np.stack(vecs) if vecs else np.zeros((0, 28 * 28 * 3), dtype=np.float32)

    def match_cell(self, frame: np.ndarray, cy: int, col: int, tab: int) -> tuple[dict | None, float, float, float, float]:
        """Return (template_meta, score, ncc, l1_fg, margin) for the occupied cell."""
        idxs = self.tab_indices.get(tab)
        cmat = self.coarse_mats.get(tab)
        if idxs is None or len(idxs) == 0:
            return None, -1.0, -1.0, 255.0, 0.0
        cx = FIRST_X + col * COL_PITCH
        h, w_img = frame.shape[:2]
        if cy - 2 < 0 or cy + ICON_SIZE + 2 > h or cx - 2 < 0 or cx + ICON_SIZE + 2 > w_img:
            return None, -1.0, -1.0, 255.0, 0.0

        crop = frame[cy:cy + ICON_SIZE, cx:cx + ICON_SIZE]
        sm = cv2.GaussianBlur(crop.astype(np.float32), (5, 5), 1.4)
        sm28 = cv2.resize(sm, (28, 28), interpolation=cv2.INTER_AREA)
        m28 = sm28[_VALID28].mean(axis=0)
        v = ((sm28 - m28) * _VALID28[:, :, None]).reshape(-1)
        v /= (np.linalg.norm(v) + 1e-6)
        coarse = cmat @ v
        k_coarse = min(36, len(idxs))
        top_idx = idxs[np.argsort(coarse)[::-1][:k_coarse]]

        patches84 = np.stack([
            frame[cy + dy:cy + dy + ICON_SIZE, cx + dx:cx + dx + ICON_SIZE].astype(np.float32)
            for dy, dx in _SHIFTS
        ])
        p0 = patches84[12]
        a = self.a_all[top_idx]
        fg = self.fg_all[top_idx]
        bg_mask = (a < 0.05) & (_VALID84[None] > 0)
        bg_cnt = bg_mask.sum(axis=(1, 2))
        bg_mean = np.where(
            (bg_cnt >= 40)[:, None],
            (p0[None] * bg_mask[:, :, :, None]).sum(axis=(1, 2)) / np.maximum(bg_cnt, 1)[:, None],
            p0[4:12, 4:80].mean(axis=(0, 1))[None, :],
        )

        p42 = patches84[:, ::2, ::2, :]
        comp42 = a[:, ::2, ::2, None] * fg[:, ::2, ::2, :] + (1.0 - a[:, ::2, ::2, None]) * bg_mean[:, None, None, :]
        diff42 = np.abs(p42[None] - comp42[:, None]).mean(axis=4)
        l1_42 = (diff42 * _VALID42[None, None]).sum(axis=(2, 3)) / (_VALID42.sum() + 1e-6)
        bs42 = np.argmin(l1_42, axis=1)
        k_fine = min(10, k_coarse)
        top_fine = np.argsort(l1_42[np.arange(k_coarse), bs42])[:k_fine]

        final = []
        for ki in top_fine:
            ti = int(top_idx[ki])
            b_dy, b_dx = _SHIFTS[int(bs42[ki])]
            ak, fgk, wk, bgk = self.a_all[ti], self.fg_all[ti], self.w_all[ti], bg_mean[ki]
            ws = float(wk.sum()) + 1e-6
            fg_m = (fgk * wk[:, :, None]).sum(axis=(0, 1)) / ws
            ta = fgk - fg_m
            ta_ss = float(((ta ** 2) * wk[:, :, None]).sum())
            compk = ak[:, :, None] * fgk + (1.0 - ak[:, :, None]) * bgk[None, None, :]
            best_k = (-999.0, 0.0, 999.0)
            for ndy, ndx in ((0, 0), (-1, 0), (1, 0), (0, -1), (0, 1)):
                s_y = max(-2, min(2, b_dy + ndy)) + 2
                s_x = max(-2, min(2, b_dx + ndx)) + 2
                ps = patches84[s_y * 5 + s_x]
                diff = np.abs(ps - compk).mean(axis=2)
                l1_fg = float((diff * ak * _VALID84).sum() / ((ak * _VALID84).sum() + 1e-6))
                l1_all = float((diff * _VALID84).sum() / (_VALID84.sum() + 1e-6))
                pw_mean = (ps * wk[:, :, None]).sum(axis=(0, 1)) / ws
                pa = ps - pw_mean
                p_dot_t = float((pa * ta * wk[:, :, None]).sum())
                p_ss = float(((pa ** 2) * wk[:, :, None]).sum())
                ncc = p_dot_t / (np.sqrt(p_ss * ta_ss) + 1e-6)
                sc = ncc - 0.015 * l1_all - 0.010 * l1_fg
                if sc > best_k[0]:
                    best_k = (sc, ncc, l1_fg)
            final.append((*best_k, self.templates[ti]))
        final.sort(key=lambda x: -x[0])
        top1 = final[0]
        margin = top1[0] - (final[1][0] if len(final) > 1 else -1.0)
        return top1[3], top1[0], top1[1], top1[2], margin

    def lookup_by_name(self, tab: int, raw_title: str) -> list[str]:
        norm = normalize_name(raw_title)
        if not norm:
            return []
        for table in (self.by_norm_tab.get(tab) or {}, self.by_norm_all):
            if norm in table:
                return table[norm]
        if len(norm) >= 3:
            for table in (self.by_norm_tab.get(tab) or {}, self.by_norm_all):
                hits = [ids for k, ids in table.items() if k and (norm in k or k in norm)]
                if len(hits) == 1:
                    return hits[0]
        return []


@lru_cache(maxsize=1)
def load_backpack_model() -> BackpackModel:
    return BackpackModel(ROOT / "resource/model/backpack")


def detect_grid_rows(frame: np.ndarray, grid_top: int = GRID_TOP, grid_bottom: int | None = None) -> list[int]:
    """Detect all complete visible card row top Y coordinates aligned to the periodic row lattice."""
    if frame is None:
        return []
    if grid_bottom is None:
        grid_bottom = frame.shape[0] - GRID_BOTTOM_MARGIN
    if grid_bottom - grid_top < ICON_SIZE:
        return []
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)
    gy_abs = np.abs(np.diff(gray, axis=0, prepend=gray[:1]))
    gx_abs = np.abs(np.diff(gray, axis=1, prepend=gray[:, :1]))
    cols = [slice(FIRST_X + c * COL_PITCH + 6, FIRST_X + c * COL_PITCH + 84) for c in range(8)]
    top_edge = np.mean([gy_abs[:, sl].mean(axis=1) for sl in cols], axis=0)
    interior_tex = np.mean([gx_abs[:, sl].mean(axis=1) for sl in cols], axis=0)
    best_score, best_ys = -1e9, []
    for phase in np.linspace(0, ROW_PITCH_EXACT, 114, endpoint=False):
        ys = []
        k = 0
        while True:
            cy = int(round(grid_top - 30 + phase + k * ROW_PITCH_EXACT))
            k += 1
            if cy > grid_bottom - 88:
                break
            if cy >= grid_top - 4:
                ys.append(cy)
        if not ys:
            continue
        sc = 0.0
        for cy in ys:
            te = float(np.max(top_edge[max(0, cy - 3):cy + 2]))
            be = float(np.max(top_edge[cy + 84:min(len(top_edge), cy + 90)]))
            mid = float(np.mean(interior_tex[cy + 10:cy + 62]))
            gap_end = min(len(interior_tex), cy + 107)
            gap = float(np.mean(interior_tex[cy + 92:gap_end])) if gap_end > cy + 94 else 0.0
            sc += (te + be) * 4.0 + (mid - gap) * 8.0
        sc /= len(ys)
        if sc > best_score:
            best_score, best_ys = sc, ys
    return [cy for cy in best_ys if cy >= grid_top and cy + 88 <= grid_bottom]


def is_cell_occupied(frame: np.ndarray, cy: int, col: int) -> bool:
    cx = FIRST_X + col * COL_PITCH
    if cy < 0 or cy + ICON_SIZE > frame.shape[0] or cx < 0 or cx + ICON_SIZE > frame.shape[1]:
        return False
    crop = frame[cy:cy + ICON_SIZE, cx:cx + ICON_SIZE]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    std = float(np.std(gray[:64, :]))
    white_cnt = int(np.sum(gray[66:84, 34:84] > 238))
    return std >= 5.0 or white_cnt > 0


def read_badge_qty_single(rt, frame: np.ndarray, col: int, cy: int) -> int | None:
    cx = FIRST_X + col * COL_PITCH
    y0, y1 = max(0, cy + 62), min(frame.shape[0], cy + 90)
    x0, x1 = max(0, cx + 10), min(frame.shape[1], cx + 88)
    crop = frame[y0:y1, x0:x1]
    if crop.size == 0:
        return None
    upscaled = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    padded = cv2.copyMakeBorder(upscaled, 12, 12, 12, 12, cv2.BORDER_REPLICATE)
    h, w = padded.shape[:2]
    res = rt.recognize("MaaNikki_Daily_OCR", {
        "roi": [0, 0, w, h],
        "expected": r"[0-9][0-9.,kK万wW]*",
    }, image=padded)
    raw = getattr(res.best_result, "text", "") if (res and res.hit) else ""
    qty = parse_badge_quantity(raw)
    if qty is None:
        raw2 = rt.text([x0, y0, x1 - x0, y1 - y0], image=frame)
        qty = parse_badge_quantity(raw2)
    return qty


def batch_read_badge_qtys(rt, frame: np.ndarray, cells: list[tuple[int, int]]) -> dict[tuple[int, int], int | None]:
    """Read quantities for [(cy, col), ...] in batched montage sheets with single-cell retry fallback."""
    results: dict[tuple[int, int], int | None] = {}
    if not cells:
        return results
    # Pack 18 cells per montage sheet (3 cols x 6 rows, 280x110px per tile -> <=680px tall).
    tile_w, tile_h = 280, 110
    chunk_size = 18
    for start in range(0, len(cells), chunk_size):
        batch = cells[start:start + chunk_size]
        rows_cnt = (len(batch) + 2) // 3
        sheet = np.full((rows_cnt * tile_h, 3 * tile_w, 3), 245, dtype=np.uint8)
        for idx, (cy, col) in enumerate(batch):
            cx = FIRST_X + col * COL_PITCH
            y0, y1 = max(0, cy + 62), min(frame.shape[0], cy + 90)
            x0, x1 = max(0, cx + 10), min(frame.shape[1], cx + 88)
            crop = frame[y0:y1, x0:x1]
            if crop.size == 0:
                continue
            up = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
            r, c_i = divmod(idx, 3)
            py = r * tile_h + 13
            px = c_i * tile_w + 20
            uh, uw = min(up.shape[0], tile_h - 20), min(up.shape[1], tile_w - 40)
            sheet[py:py + uh, px:px + uw] = up[:uh, :uw]
        sh, sw = sheet.shape[:2]
        ocr_res = rt.recognize("MaaNikki_Daily_OCR", {
            "roi": [0, 0, sw, sh],
            "expected": r"[0-9][0-9.,kK万wW]*",
        }, image=sheet)
        by_tile: dict[int, list[tuple[float, str]]] = {}
        if ocr_res and ocr_res.hit:
            items = getattr(ocr_res, "filtered_results", []) or [ocr_res.best_result]
            for item in items:
                bx, by, bw, bh = list(item.box)
                tcx, tcy = bx + bw / 2.0, by + bh / 2.0
                r_idx, c_idx = int(tcy // tile_h), int(tcx // tile_w)
                t_idx = r_idx * 3 + c_idx
                if 0 <= t_idx < len(batch):
                    by_tile.setdefault(t_idx, []).append((bx, getattr(item, "text", "") or ""))
        for idx, cell_key in enumerate(batch):
            parts = [txt for _, txt in sorted(by_tile.get(idx, []), key=lambda p: p[0])]
            qty = parse_badge_quantity("".join(parts)) if parts else None
            if qty is None:
                qty = read_badge_qty_single(rt, frame, cell_key[1], cell_key[0])
            results[cell_key] = qty
    return results


def read_top_bling(rt, frame: np.ndarray) -> int | None:
    x, y, w, h = BLING_ROI
    crop = frame[y:y + h, x:x + w]
    if crop.size == 0:
        return None
    upscaled = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    padded = cv2.copyMakeBorder(upscaled, 12, 12, 12, 12, cv2.BORDER_REPLICATE)
    res = rt.recognize("MaaNikki_Daily_OCR", {
        "roi": [0, 0, padded.shape[1], padded.shape[0]],
        "only_rec": True,
        "expected": r"^[0-9\s]+$",
    }, image=padded)
    if not res or not res.hit:
        return None
    digits = re.sub(r"\D", "", getattr(res.best_result, "text", "") or "")
    return int(digits) if digits else None


def _grid_box(frame: np.ndarray) -> tuple[int, int]:
    return GRID_TOP + 6, max(GRID_TOP + 120, frame.shape[0] - GRID_BOTTOM_MARGIN - 6)


def _same_grid(first: np.ndarray | None, second: np.ndarray | None) -> bool:
    if first is None or second is None or first.shape != second.shape:
        return False
    y0, y1 = _grid_box(first)
    diff = cv2.absdiff(first[y0:y1, 42:806], second[y0:y1, 42:806])
    return float(np.mean(diff)) < 1.5


def _scroll_grid_and_park(rt, dy: int, wait_seconds: float) -> np.ndarray | None:
    if (not rt.action("TouchMove", target=GRID_SCROLL_TARGET)
            or not rt.pause(0.1)
            or not rt.action("Scroll", target=GRID_SCROLL_TARGET, dx=0, dy=dy)
            or not rt.pause(wait_seconds)
            or not rt.action("TouchMove", target=[640, 36])
            or not rt.pause(0.12)):
        return None
    return rt.capture()


def _scroll_to_top(rt) -> np.ndarray | None:
    frame = rt.capture()
    for _ in range(12):
        if rt.stopped or frame is None:
            return None
        after = _scroll_grid_and_park(rt, 14400, 0.28)
        if after is None:
            return None
        if _same_grid(frame, after):
            return after
        frame = after
    return frame


def _measure_scroll_delta(before: np.ndarray, after: np.ndarray, row_ys: list[int]) -> int | None:
    if before is None or after is None or before.shape != after.shape or not row_ys:
        return None
    grid_bottom = before.shape[0] - GRID_BOTTOM_MARGIN
    # Anchor against the lowest complete row(s) that still have sufficient vertical room to move up.
    anchors = [cy for cy in reversed(row_ys) if cy >= GRID_TOP + 4][:2]
    b_gray = cv2.cvtColor(before, cv2.COLOR_BGR2GRAY)
    a_gray = cv2.cvtColor(after, cv2.COLOR_BGR2GRAY)
    votes: list[int] = []
    for cy in anchors:
        tmpl = b_gray[cy:cy + 80, 42:800]
        if tmpl.shape[0] < 80 or float(np.std(tmpl)) < 4.0:
            continue
        low = max(GRID_TOP - 8, 0)
        high = min(cy + 4, grid_bottom - 80)
        if high < low + 10:
            continue
        search = a_gray[low:high + 80, 42:800]
        res = cv2.matchTemplate(search, tmpl, cv2.TM_CCOEFF_NORMED).ravel()
        best_idx = int(np.argmax(res))
        if float(res[best_idx]) >= 0.88:
            votes.append(cy - (low + best_idx))
    if not votes:
        return None
    delta = int(round(float(np.median(votes))))
    return delta if delta >= 0 else None


def _resolve_clicked_item(
    rt,
    model: BackpackModel,
    tab: int,
    cy: int,
    col: int,
    cat_by_norm: dict[str, str],
) -> tuple[str | None, str | None]:
    cx = FIRST_X + col * COL_PITCH
    if not rt.action("Click", target=[cx + 42, cy + 42]) or not rt.pause(0.2):
        return None, None
    clicked = rt.capture()
    if clicked is None:
        return None, None
    title = rt.text(RIGHT_TITLE_ROI, image=clicked)
    hits = model.lookup_by_name(tab, title)
    if hits:
        return hits[0], None
    cat_fallback = cat_by_norm.get(normalize_name(title))
    return None, cat_fallback


def _scan_tab(
    rt,
    model: BackpackModel,
    tab: int,
    is_tall: bool,
    crafting_catalog: dict,
) -> tuple[dict[str, int], dict]:
    counts: dict[str, int] = {}
    stats = {"items": 0, "clicked": 0, "unknown": 0, "complete": False}
    frame = _scroll_to_top(rt)
    if frame is None:
        return counts, stats

    grid_bottom = frame.shape[0] - GRID_BOTTOM_MARGIN
    scroll_y = 0
    first_row_y: int | None = None
    seen_slots: set[tuple[int, int]] = set()
    cat_by_norm = {
        normalize_name(meta.get("name", "")): str(mid)
        for mid, meta in (crafting_catalog.get("materials") or {}).items()
        if str(mid) != model.bling_id
    }

    for _page in range(48):
        if rt.stopped:
            return counts, stats
        row_ys = detect_grid_rows(frame, grid_top=GRID_TOP, grid_bottom=grid_bottom)
        if not row_ys:
            # No rows visible on initial top view -> genuine empty tab.
            stats["complete"] = _page == 0
            break
        if first_row_y is None:
            first_row_y = row_ys[0]

        pending_qty: list[tuple[int, int, str]] = []  # (cy, col, storage_key)
        for cy in row_ys:
            row_idx = int(round((cy + scroll_y - first_row_y) / ROW_PITCH_EXACT))
            if row_idx < 0:
                continue
            for col in range(8):
                slot_key = (row_idx, col)
                if slot_key in seen_slots or not is_cell_occupied(frame, cy, col):
                    continue
                seen_slots.add(slot_key)
                tmpl, sc, ncc, _l1, margin = model.match_cell(frame, cy, col, tab)
                tb_id: str | None = None
                ambiguous = tmpl is not None and len(set(tmpl.get("names") or [])) > 1
                confident = (
                    tmpl is not None
                    and not ambiguous
                    and ncc >= 0.62
                    and sc >= 0.25
                    and margin >= 0.06
                )
                if confident:
                    tb_id = tmpl["item_ids"][0]
                else:
                    stats["clicked"] += 1
                    tb_id, cat_fallback = _resolve_clicked_item(rt, model, tab, cy, col, cat_by_norm)
                    if tb_id is None and cat_fallback is not None:
                        pending_qty.append((cy, col, cat_fallback))
                        continue
                if tb_id is None:
                    stats["unknown"] += 1
                    continue
                meta = model.items.get(tb_id) or {}
                storage_key = meta.get("catalog_id") or tb_id
                pending_qty.append((cy, col, str(storage_key)))

        if pending_qty:
            qty_map = batch_read_badge_qtys(rt, frame, [(cy, col) for cy, col, _ in pending_qty])
            for cy, col, storage_key in pending_qty:
                qty = qty_map.get((cy, col))
                if qty is not None:
                    counts[storage_key] = counts.get(storage_key, 0) + int(qty)
                    stats["items"] += 1
                else:
                    stats["unknown"] += 1

        step_dy = -4200 if is_tall else -960
        moved = False
        for attempt_dy in (step_dy, step_dy // 2, step_dy // 4):
            after = _scroll_grid_and_park(rt, attempt_dy, 0.32)
            if after is None:
                return counts, stats
            if _same_grid(frame, after):
                # Retry once to rule out a dropped Slate scroll event before concluding end of list.
                after = _scroll_grid_and_park(rt, attempt_dy, 0.32)
                if after is None:
                    return counts, stats
                if _same_grid(frame, after):
                    stats["complete"] = stats["unknown"] == 0
                    return counts, stats
            delta = _measure_scroll_delta(frame, after, row_ys)
            if delta is not None:
                scroll_y += delta
                frame = after
                moved = True
                break
            # Overshot overlap window -> restore cursor on grid, rewind upward, and retry smaller step.
            if _scroll_grid_and_park(rt, -attempt_dy, 0.25) is None:
                return counts, stats
        if not moved:
            break
    return counts, stats


def _switch_tab(rt, *, want_growth: bool) -> bool:
    target = TAB_GROWTH if want_growth else TAB_MATERIALS
    for _ in range(3):
        if rt.stopped:
            return False
        frame = rt.capture()
        header = rt.text(HEADER_ROI, image=frame) if frame is not None else ""
        if want_growth and "养成" in header:
            return True
        if not want_growth and "材料" in header and "养成" not in header:
            return True
        if not rt.action("Click", target=target) or not rt.pause(0.45):
            return False
    frame = rt.capture()
    header = rt.text(HEADER_ROI, image=frame) if frame is not None else ""
    return ("养成" in header) if want_growth else ("材料" in header and "养成" not in header)


def scan_backpack_inventory(rt, crafting_catalog: dict) -> tuple[dict[str, int], dict] | None:
    """Open backpack, scan Bling + all Tab 1 (材料) & Tab 2 (养成材料) items using tall_window."""
    if not rt.hit(rt.ui.feature(text="背包", roi=HEADER_ROI)):
        if not rt.main():
            return None
        if not rt.ui.enter_page(
            lambda: rt.key("backpack"),
            rt.ui.feature(text="背包", roi=HEADER_ROI),
            source="MaaNikki_MainDetected",
            attempts=2,
        ):
            rt.log("背包扫描：未能打开背包界面，沿用已有材料库存记录。")
            return None

    model = load_backpack_model()
    found: dict[str, int] = {}
    summary: dict = {"complete": False, "tab1": {}, "tab2": {}, "tall_mode": False}
    try:
        frame = rt.capture()
        if frame is None:
            return None
        bling = read_top_bling(rt, frame)
        if bling is not None:
            found[model.bling_id] = bling
            summary["bling"] = bling

        with tall_window(rt) as is_tall:
            summary["tall_mode"] = bool(is_tall)
            if is_tall:
                rt.log("背包扫描：已切换超长竖屏模式，正在读取材料与养成材料库存。")

            if not _switch_tab(rt, want_growth=False):
                return None

            c1, s1 = _scan_tab(rt, model, 1, is_tall, crafting_catalog)
            found.update(c1)
            summary["tab1"] = s1

            if _switch_tab(rt, want_growth=True):
                c2, s2 = _scan_tab(rt, model, 2, is_tall, crafting_catalog)
                for k, v in c2.items():
                    found[k] = found.get(k, 0) + v
                summary["tab2"] = s2

        summary["complete"] = bool(summary["tab1"].get("complete")) and bool(summary["tab2"].get("complete"))
        return found, summary
    finally:
        if not rt.stopped:
            rt.main()


def _apply_backpack_results(
    rt,
    account: AccountState,
    crafting_catalog: dict,
    scanned: dict[str, int],
    summary: dict,
) -> dict[str, int]:
    now = datetime.now(timezone.utc).isoformat()
    today = current_game_day()
    model = load_backpack_model()
    mat_defs = crafting_catalog.get("materials") or {}
    # Catalog IDs whose tabs were completely scanned (excluding quest-tab-only items like 弹力星胶).
    cat_tabs: dict[str, int] = {}
    for meta in model.items.values():
        if meta.get("catalog_id"):
            cat_tabs[str(meta["catalog_id"])] = int(meta["tab"])
    completed_tabs = {tab for tab, key in ((1, "tab1"), (2, "tab2")) if (summary.get(key) or {}).get("complete")}
    extra_keys_by_tab = {
        tab: {iid for iid, meta in model.items.items() if int(meta["tab"]) == tab and not meta.get("catalog_id")}
        for tab in (1, 2)
    }
    logs: list[str] = []

    def update(data):
        m = data.setdefault("materials", {})
        cur = m.setdefault("inventory", {})
        wb_key = wardrobe_snapshot_key(data)
        same_wardrobe = bool(wb_key) and m.get("wardrobe_baseline_key") == wb_key
        if wb_key and m.get("wardrobe_baseline_key") != wb_key:
            m["wardrobe_baseline_key"] = wb_key
            m["crafted_consumed"] = {}
        consumed_map = m.setdefault("crafted_consumed", {})

        glow_plan = m.get("glow_plan") if isinstance(m.get("glow_plan"), dict) else None
        glow_targets = glow_plan.get("targets") if (glow_plan and isinstance(glow_plan.get("targets"), dict)) else None
        renewal_dynamic = renewal_source(m) == "plan"
        scoring_state = (data.get("wardrobe") or {}).get("scoring") or {}
        renewal_key = f"{wb_key}|{scoring_state.get('updated_at') or ''}" if wb_key else ""
        if renewal_key and m.get("renewal_baseline_key") != renewal_key:
            m["renewal_baseline_key"] = renewal_key
            m["renewal_consumed"] = {}
        renewal_consumed = m.setdefault("renewal_consumed", {})

        final_scanned = dict(scanned)
        for cid, tab in cat_tabs.items():
            if tab in completed_tabs:
                final_scanned.setdefault(cid, 0)

        for mid, v in final_scanned.items():
            new_qty = int(v)
            prev_qty = int(cur[mid]) if mid in cur else None
            meta = mat_defs.get(mid) or {}
            kind = meta.get("kind")

            if prev_qty is not None and new_qty < prev_qty:
                used = prev_qty - new_qty
                if kind == "monster_craft" and same_wardrobe:
                    consumed_map[mid] = int(consumed_map.get(mid, 0)) + used
                    logs.append(f"检测到 {meta.get('name', mid)} 库存减少 {used}（{prev_qty} -> {new_qty}），已自动核销对应制衣材料需求。")
                if (kind == "monster_glow" or mid == "50361309") and renewal_dynamic and renewal_key:
                    renewal_consumed[mid] = int(renewal_consumed.get(mid, 0)) + used
                    logs.append(f"检测到 {meta.get('name', mid)} 库存减少 {used}（已用于服装焕新），焕新计划所需已同步扣减。")
                elif (kind == "monster_glow" or mid == "50361309") and glow_targets and int(glow_targets.get(mid, 0)) > 0:
                    old_target = int(glow_targets[mid])
                    new_target = max(0, old_target - used)
                    if new_target > 0:
                        glow_targets[mid] = new_target
                    else:
                        glow_targets.pop(mid, None)
                    logs.append(f"检测到 {meta.get('name', mid)} 库存减少 {used}（已用于服装焕新），筹备目标已同步扣减为 {new_target}。")

            cur[mid] = new_qty

        for tab in completed_tabs:
            for extra_id in extra_keys_by_tab[tab]:
                if extra_id not in final_scanned:
                    cur.pop(extra_id, None)

        if glow_plan and glow_targets is not None:
            has_remaining = any(int(t) > int(cur.get(str(gid), 0)) for gid, t in glow_targets.items() if int(t) > 0)
            glow_plan["completed"] = not has_remaining
            if not has_remaining:
                glow_plan["completed_at"] = now

        m["backpack_scanned_day"] = today
        m["inventory_updated_at"] = now
        m["backpack_summary"] = {
            "updated_at": now,
            "complete": bool(summary.get("complete")),
            "tall_mode": bool(summary.get("tall_mode")),
            "tab1": summary.get("tab1"),
            "tab2": summary.get("tab2"),
            "bling": summary.get("bling"),
            "total_tracked": len(cur),
        }

    account.update(update)
    for line in logs:
        rt.log(line)
    role_after = account.load()
    return dict((role_after.get("materials") or {}).get("inventory") or {})


def ensure_backpack_scanned(rt, account: AccountState, crafting_catalog: dict) -> dict[str, int]:
    role = account.load()
    mats = role.get("materials") or {}
    inv = dict(mats.get("inventory") or {})
    today = current_game_day()
    if mats.get("backpack_scanned_day") == today and inv:
        return inv
    rt.log("正在打开背包核对今日材料与养成材料库存。")
    result = scan_backpack_inventory(rt, crafting_catalog)
    if not result:
        return inv
    scanned, summary = result
    updated_inv = _apply_backpack_results(rt, account, crafting_catalog, scanned, summary)
    rt.log(f"背包材料库存核对完成：已记录 {len(scanned)} 项有库存物品（累计追踪 {len(updated_inv)} 项）。")
    return updated_inv


def rescan_backpack(rt, account: AccountState, crafting_catalog: dict) -> dict[str, int] | None:
    """Manual full backpack read returning updated inventory or None on failure."""
    result = scan_backpack_inventory(rt, crafting_catalog)
    if not result:
        return None
    scanned, summary = result
    updated_inv = _apply_backpack_results(rt, account, crafting_catalog, scanned, summary)
    t1 = (summary.get("tab1") or {}).get("items", 0)
    t2 = (summary.get("tab2") or {}).get("items", 0)
    rt.log(f"背包读取完成：材料页 {t1} 项、养成材料页 {t2} 项（累计追踪 {len(updated_inv)} 项）。")
    return updated_inv
