"""Full tall-window backpack scanner for Tab 1 (材料), Tab 2 (养成材料), and Bling."""
from __future__ import annotations

from contextlib import nullcontext
from datetime import date, datetime, timezone
from functools import lru_cache
import json
from pathlib import Path
import re

import cv2
import numpy as np

from account_state import AccountState, game_day
from daily.settings import ROOT
from tall_window import clamp_capture_park_point, safe_capture_y_range, tall_window, window_hwnd


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
_NEIGHBOR_SHIFTS = ((0, 0), (-1, 0), (1, 0), (0, -1), (0, 1))
_VALID84 = np.ones((84, 84), dtype=np.float32)
_VALID84[63:, 20:] = 0.0
_VALID84[:2, :] = 0.0
_VALID84[-2:, :] = 0.0
_VALID84[:, :2] = 0.0
_VALID84[:, -2:] = 0.0
_MASK28 = (_VALID84[1::3, 1::3] > 0).reshape(-1)
_MASK42_BOOL = (_VALID84[::2, ::2] > 0)
_MASK84_FLAT = (_VALID84 > 0).reshape(-1)


TAB1_LEDGER_TTL_DAYS = 7
TAB2_LEDGER_TTL_DAYS = 3


def current_game_day() -> str:
    return game_day().isoformat()


@lru_cache(maxsize=1)
def catalog_tab_map() -> dict[str, int]:
    """Return {catalog_id_or_bling_id: tab (0 for top Bling, 1 for 材料, 2 for 养成材料)}."""
    catalog = json.loads((ROOT / "resource/model/backpack/catalog.json").read_text(encoding="utf-8"))
    mapping: dict[str, int] = {str(catalog.get("bling_id") or "992617511"): 0}
    for meta in (catalog.get("items") or {}).values():
        cid = meta.get("catalog_id")
        if cid is not None:
            mapping[str(cid)] = int(meta.get("tab") or 1)
    return mapping


def required_tabs_for_mids(needed_mids: dict[str, int] | None) -> set[int]:
    if not needed_mids:
        return set()
    tab_map = catalog_tab_map()
    tabs: set[int] = set()
    for mid, need in needed_mids.items():
        if int(need or 0) <= 0:
            continue
        tab = tab_map.get(str(mid), 1)
        tabs.add(2 if tab == 0 else tab)
    return tabs


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
        self.tab_indices: dict[int, np.ndarray] = {}
        self.by_norm_tab: dict[int, dict[str, list[str]]] = {1: {}, 2: {}}
        self.by_norm_all: dict[str, list[str]] = {}

        for iid, meta in self.items.items():
            norm = normalize_name(meta.get("name", ""))
            if not norm:
                continue
            tab = int(meta.get("tab") or 1)
            self.by_norm_tab.setdefault(tab, {}).setdefault(norm, []).append(iid)
            self.by_norm_all.setdefault(norm, []).append(iid)

        n_tmpl = len(self.templates)
        n_m28 = int(_MASK28.sum())
        w28_all = np.zeros((n_tmpl, n_m28), dtype=np.float32)
        ta28_all = np.zeros((n_tmpl, n_m28 * 3), dtype=np.float32)
        ws28_all = np.zeros(n_tmpl, dtype=np.float32)
        for i in range(n_tmpl):
            w84 = np.clip(self.a_all[i], 0.0, 1.0) * _VALID84
            # Alpha-weighted slight blur keeps thin icon strokes resilient across subpixel shifts.
            num = cv2.GaussianBlur(self.fg_all[i] * w84[:, :, None], (3, 3), 0.9)
            den = np.maximum(cv2.GaussianBlur(w84, (3, 3), 0.9)[:, :, None], 1e-4)
            sm = np.where(w84[:, :, None] > 0.02, num / den, 0.0)
            sm28 = sm[1::3, 1::3].reshape(-1, 3)[_MASK28]
            w28 = np.clip(w84[1::3, 1::3].reshape(-1)[_MASK28], 0.0, 1.0)
            ws = float(w28.sum()) + 1e-6
            m28 = (sm28 * w28[:, None]).sum(axis=0) / ws
            ta = (sm28 - m28) * w28[:, None]
            ss = float(((sm28 - m28) ** 2 * w28[:, None]).sum()) + 1e-6
            w28_all[i] = w28
            ws28_all[i] = ws
            ta28_all[i] = (ta / np.sqrt(ss)).reshape(-1)

        self._w28_by_tab: dict[int, np.ndarray] = {}
        self._ws28_by_tab: dict[int, np.ndarray] = {}
        self._ta28_by_tab: dict[int, np.ndarray] = {}
        for tab in (1, 2):
            idxs = np.array([i for i, t in enumerate(self.templates) if t["tab"] == tab], dtype=np.int32)
            self.tab_indices[tab] = idxs
            self._w28_by_tab[tab] = np.ascontiguousarray(w28_all[idxs])
            self._ws28_by_tab[tab] = np.ascontiguousarray(ws28_all[idxs][:, None])
            self._ta28_by_tab[tab] = np.ascontiguousarray(ta28_all[idxs])

        # Stage 2: alpha-weighted 42x42 zero-mean NCC across all 25 +/-2px shift candidates.
        a42 = self.a_all[:, ::2, ::2][:, _MASK42_BOOL]
        self._w42_all = np.ascontiguousarray(np.clip(a42, 0.0, 1.0) ** 1.5)
        fg42 = self.fg_all[:, ::2, ::2, :][:, _MASK42_BOOL, :]
        self._ws42_all = np.ascontiguousarray(self._w42_all.sum(axis=1) + 1e-6)
        fg42_m = (fg42 * self._w42_all[:, :, None]).sum(axis=1) / self._ws42_all[:, None]
        ta42 = (fg42 - fg42_m[:, None, :]) * self._w42_all[:, :, None]
        ta42_ss = ((fg42 - fg42_m[:, None, :]) ** 2 * self._w42_all[:, :, None]).sum(axis=(1, 2)) + 1e-6
        self._ta42_flat = np.ascontiguousarray((ta42 / np.sqrt(ta42_ss)[:, None, None]).reshape(n_tmpl, -1))

        # Stage 3: square alpha on 84x84 to suppress semi-transparent backdrop shadow bleed on tiny icons,
        # paired with opponent-chroma NCC & color-residual L1 to separate same-shape color twins.
        self._w84_flat = np.ascontiguousarray(
            ((np.clip(self.a_all, 0.0, 1.0) ** 2).reshape(n_tmpl, -1) * _MASK84_FLAT[None, :])[:, _MASK84_FLAT]
        )
        fg84_flat = np.ascontiguousarray(self.fg_all.reshape(n_tmpl, -1, 3)[:, _MASK84_FLAT, :])
        self._ws84 = np.ascontiguousarray(self._w84_flat.sum(axis=1) + 1e-6)
        fg84_m = (fg84_flat * self._w84_flat[:, :, None]).sum(axis=1) / self._ws84[:, None]
        ta84_diff = np.ascontiguousarray(fg84_flat - fg84_m[:, None, :])
        self._ta84_flat = np.ascontiguousarray(ta84_diff * self._w84_flat[:, :, None])
        self._ta84_ss = np.ascontiguousarray(((ta84_diff ** 2) * self._w84_flat[:, :, None]).sum(axis=(1, 2)) + 1e-6)
        ta84_std = np.sqrt(self._ta84_ss / (3.0 * self._ws84)) + 1e-6
        self._ta84_normed = np.ascontiguousarray(ta84_diff / ta84_std[:, None, None])
        self._tc84_normed = np.ascontiguousarray(
            (ta84_diff - ta84_diff.mean(axis=2, keepdims=True)) / ta84_std[:, None, None]
        )

        ch84_flat = np.stack([
            fg84_flat[:, :, 2] - fg84_flat[:, :, 1],
            fg84_flat[:, :, 0] - 0.5 * (fg84_flat[:, :, 2] + fg84_flat[:, :, 1]),
        ], axis=2)
        ch84_m = (ch84_flat * self._w84_flat[:, :, None]).sum(axis=1) / self._ws84[:, None]
        ca84_diff = ch84_flat - ch84_m[:, None, :]
        self._ca84_flat = np.ascontiguousarray(ca84_diff * self._w84_flat[:, :, None])
        self._ca84_ss = np.ascontiguousarray(((ca84_diff ** 2) * self._w84_flat[:, :, None]).sum(axis=(1, 2)) + 1e-6)

    def match_cell(self, frame: np.ndarray, cy: int, col: int, tab: int) -> tuple[dict | None, float, float, float, float]:
        """Return (template_meta, score, ncc, col_l1_scaled, margin) for the occupied cell."""
        idxs = self.tab_indices.get(tab)
        if idxs is None or len(idxs) == 0:
            return None, -1.0, -1.0, 255.0, 0.0
        cx = FIRST_X + col * COL_PITCH
        h, w_img = frame.shape[:2]
        if cy - 2 < 0 or cy + ICON_SIZE + 2 > h or cx - 2 < 0 or cx + ICON_SIZE + 2 > w_img:
            return None, -1.0, -1.0, 255.0, 0.0

        win88 = frame[cy - 2:cy + ICON_SIZE + 2, cx - 2:cx + ICON_SIZE + 2].astype(np.float32)

        # Stage 1: multi-shift alpha-weighted 28x28 NCC without estimating scalar cell background.
        sm88 = cv2.GaussianBlur(win88, (3, 3), 0.9)
        p28 = np.stack([
            sm88[3 + dy:3 + dy + 84:3, 3 + dx:3 + dx + 84:3].reshape(-1, 3)[_MASK28]
            for dy, dx in _SHIFTS
        ])
        p28_flat = p28.reshape(len(_SHIFTS), -1).T
        p28_sq_sum = (p28 ** 2).sum(axis=2).T
        w28_sub = self._w28_by_tab[tab]
        ws28_sub = self._ws28_by_tab[tab]
        num28 = self._ta28_by_tab[tab] @ p28_flat
        pw28 = np.stack([
            w28_sub @ p28[:, :, 0].T,
            w28_sub @ p28[:, :, 1].T,
            w28_sub @ p28[:, :, 2].T,
        ], axis=0) / ws28_sub[None]
        p28_wsq = (w28_sub @ p28_sq_sum) - ws28_sub * (pw28 ** 2).sum(axis=0)
        coarse = (num28 / (np.sqrt(np.maximum(0.0, p28_wsq)) + 1e-6)).max(axis=1)
        k_coarse = min(56, len(idxs))
        top_idx = idxs[np.argpartition(coarse, -k_coarse)[-k_coarse:]]

        # Stage 2: vectorized 42x42 foreground-weighted NCC across all 25 +/-2px shift candidates.
        p42 = np.stack([
            win88[2 + dy:2 + dy + ICON_SIZE:2, 2 + dx:2 + dx + ICON_SIZE:2][_MASK42_BOOL]
            for dy, dx in _SHIFTS
        ])
        p42_flat = p42.reshape(25, -1).T
        p42_sq_sum = (p42 ** 2).sum(axis=2).T
        w42_sub = self._w42_all[top_idx]
        ws42_sub = self._ws42_all[top_idx][:, None]
        num42 = self._ta42_flat[top_idx] @ p42_flat
        pw42_b = (w42_sub @ p42[:, :, 0].T) / ws42_sub
        pw42_g = (w42_sub @ p42[:, :, 1].T) / ws42_sub
        pw42_r = (w42_sub @ p42[:, :, 2].T) / ws42_sub
        p42_wsq = (w42_sub @ p42_sq_sum) - ws42_sub * (pw42_b ** 2 + pw42_g ** 2 + pw42_r ** 2)
        ncc42 = num42 / (np.sqrt(np.maximum(0.0, p42_wsq)) + 1e-6)
        bs42 = np.argmax(ncc42, axis=1)
        best42_sc = ncc42[np.arange(k_coarse), bs42]
        k_fine = min(8, k_coarse)
        top_fine = np.argpartition(best42_sc, -k_fine)[-k_fine:]

        shift_cache: dict[int, tuple[np.ndarray, np.ndarray]] = {}

        def _get_shift(s_idx: int) -> tuple[np.ndarray, np.ndarray]:
            cached = shift_cache.get(s_idx)
            if cached is None:
                dy, dx = _SHIFTS[s_idx]
                patch = win88[2 + dy:2 + dy + ICON_SIZE, 2 + dx:2 + dx + ICON_SIZE].reshape(-1, 3)[_MASK84_FLAT]
                cached = (patch, (patch ** 2).sum(axis=1))
                shift_cache[s_idx] = cached
            return cached

        final = []
        for ki in top_fine:
            ti = int(top_idx[ki])
            b_dy, b_dx = _SHIFTS[int(bs42[ki])]
            wk = self._w84_flat[ti]
            ws = float(self._ws84[ti])
            tak = self._ta84_flat[ti]
            ta_ss = float(self._ta84_ss[ti])
            s_indices = [
                (max(-2, min(2, b_dy + ndy)) + 2) * 5 + (max(-2, min(2, b_dx + ndx)) + 2)
                for ndy, ndx in _NEIGHBOR_SHIFTS
            ]
            pairs = [_get_shift(si) for si in s_indices]
            ps5 = np.stack([x[0] for x in pairs])
            psq5 = np.stack([x[1] for x in pairs])
            pw_mean = np.einsum("snc,n->sc", ps5, wk) / ws
            p_dot_t = np.einsum("snc,nc->s", ps5, tak)
            p_wsq_tot = (psq5 @ wk) - ws * (pw_mean ** 2).sum(axis=1)
            ncc_5 = p_dot_t / (np.sqrt(np.maximum(0.0, p_wsq_tot) * ta_ss) + 1e-6)
            bi = int(np.argmax(ncc_5))
            ncc_b = float(ncc_5[bi])
            p_b = ps5[bi]
            pa_b = p_b - pw_mean[bi][None, :]

            pc_b = np.stack([p_b[:, 2] - p_b[:, 1], p_b[:, 0] - 0.5 * (p_b[:, 2] + p_b[:, 1])], axis=1)
            pca_b = pc_b - ((pc_b * wk[:, None]).sum(axis=0) / ws)[None, :]
            c_dot = float((pca_b * self._ca84_flat[ti]).sum())
            c_ss = float(((pca_b ** 2) * wk[:, None]).sum())
            cncc_b = c_dot / float(np.sqrt(c_ss * float(self._ca84_ss[ti]) + 9.0 * ws * ws))

            p_std = float(np.sqrt(max(0.0, float(p_wsq_tot[bi])) / (3.0 * ws))) + 1e-6
            l1_norm = float((np.abs(pa_b / p_std - self._ta84_normed[ti]).mean(axis=1) * wk).sum()) / ws
            pc_diff = (pa_b - pa_b.mean(axis=1, keepdims=True)) / p_std
            col_l1 = float((np.abs(pc_diff - self._tc84_normed[ti]).mean(axis=1) * wk).sum()) / ws
            sc = 0.72 * ncc_b + 0.28 * cncc_b - 0.16 * col_l1 - 0.10 * l1_norm - 0.12 * float(
                np.sqrt(max(0.0, 1.0 - min(1.0, ncc_b)))
            )
            final.append((sc, ncc_b, col_l1 * 30.0, self.templates[ti]))
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

    def _eval_lattice(phase: float, pitch: float) -> tuple[float, list[int]]:
        ys: list[int] = []
        k = 0
        while True:
            cy = int(round(grid_top - 30 + phase + k * pitch))
            k += 1
            if cy > grid_bottom - 88:
                break
            if cy >= grid_top - 4:
                ys.append(cy)
        if not ys:
            return -1e9, []
        sc = 0.0
        for cy in ys:
            te = float(np.max(top_edge[max(0, cy - 3):cy + 2]))
            be = float(np.max(top_edge[cy + 84:min(len(top_edge), cy + 90)]))
            mid = float(np.mean(interior_tex[cy + 10:cy + 62]))
            gap_end = min(len(interior_tex), cy + 107)
            gap = float(np.mean(interior_tex[cy + 92:gap_end])) if gap_end > cy + 94 else 0.0
            sc += (te + be) * 4.0 + (mid - gap) * 8.0
        return sc / len(ys), ys

    best_score, best_ys, best_phase = -1e9, [], 0.0
    for phase in np.linspace(0, ROW_PITCH_EXACT, 114, endpoint=False):
        sc, ys = _eval_lattice(float(phase), ROW_PITCH_EXACT)
        if sc > best_score:
            best_score, best_ys, best_phase = sc, ys, float(phase)
    for pitch in np.linspace(ROW_PITCH_EXACT - 0.15, ROW_PITCH_EXACT + 0.15, 11):
        for phase in best_phase + np.linspace(-2.0, 2.0, 9):
            sc, ys = _eval_lattice(float(phase), float(pitch))
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
    # Reject translucent sky/gradient slots that have smooth background std without sharp icon strokes.
    inner = gray[8:64, 8:76].astype(np.int16)
    dx = np.abs(np.diff(inner, axis=1))[:-1, :]
    dy = np.abs(np.diff(inner, axis=0))[:, :-1]
    if std >= 4.5 and float(((dx + dy) > 18).mean()) >= 0.004:
        return True
    badge = gray[67:81, 34:78]
    badge_bright = int(np.sum(badge > 215))
    badge_sharp = int(np.sum(np.abs(np.diff(badge.astype(np.int16), axis=1)) > 30))
    return std >= 3.5 and badge_bright >= 6 and badge_sharp >= 6


def _pick_best_badge_qty(ocr_res) -> int | None:
    if not ocr_res or not ocr_res.hit:
        return None
    items = getattr(ocr_res, "filtered_results", None) or (
        [ocr_res.best_result] if ocr_res.best_result is not None else []
    )
    ranked: list[tuple[float, int]] = []
    for item in items:
        qty = parse_badge_quantity(getattr(item, "text", "") or "")
        if qty is not None:
            ranked.append((float(getattr(item, "score", 0.0) or 0.0), qty))
    if not ranked:
        return None
    return max(ranked, key=lambda pair: pair[0])[1]


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
        "threshold": 0.35,
    }, image=padded)
    qty = _pick_best_badge_qty(res)
    if qty is None:
        res2 = rt.recognize("MaaNikki_Daily_OCR", {
            "roi": [x0, y0, x1 - x0, y1 - y0],
            "expected": r"[0-9][0-9.,kK万wW]*",
            "threshold": 0.35,
        }, image=frame)
        qty = _pick_best_badge_qty(res2)
    return qty


def batch_read_badge_qtys(rt, frame: np.ndarray, cells: list[tuple[int, int]]) -> dict[tuple[int, int], int | None]:
    """Read quantities for [(cy, col), ...] in batched montage sheets with single-cell retry fallback."""
    results: dict[tuple[int, int], int | None] = {}
    if not cells:
        return results
    # Pack 18 cells per dark neutral montage sheet (3 cols x 6 rows, 300x120px per tile).
    tile_w, tile_h = 300, 120
    chunk_size = 18
    for start in range(0, len(cells), chunk_size):
        batch = cells[start:start + chunk_size]
        rows_cnt = (len(batch) + 2) // 3
        sheet = np.full((rows_cnt * tile_h, 3 * tile_w, 3), 32, dtype=np.uint8)
        for idx, (cy, col) in enumerate(batch):
            cx = FIRST_X + col * COL_PITCH
            y0, y1 = max(0, cy + 62), min(frame.shape[0], cy + 90)
            x0, x1 = max(0, cx + 10), min(frame.shape[1], cx + 88)
            crop = frame[y0:y1, x0:x1]
            if crop.size == 0:
                continue
            up = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
            tile = cv2.copyMakeBorder(up, 8, 8, 8, 8, cv2.BORDER_REPLICATE)
            th, tw = min(tile.shape[0], tile_h - 8), min(tile.shape[1], tile_w - 16)
            r, c_i = divmod(idx, 3)
            py = r * tile_h + (tile_h - th) // 2
            px = c_i * tile_w + (tile_w - tw) // 2
            sheet[py:py + th, px:px + tw] = tile[:th, :tw]
        sh, sw = sheet.shape[:2]
        ocr_res = rt.recognize("MaaNikki_Daily_OCR", {
            "roi": [0, 0, sw, sh],
            "expected": r"[0-9][0-9.,kK万wW]*",
            "threshold": 0.55,
        }, image=sheet)
        by_tile: dict[int, list[tuple[float, float, str]]] = {}
        if ocr_res and ocr_res.hit:
            items = getattr(ocr_res, "filtered_results", []) or [ocr_res.best_result]
            for item in items:
                txt = (getattr(item, "text", "") or "").strip()
                if not txt or not re.search(r"\d", txt):
                    continue
                bx, by, bw, bh = list(item.box)
                tcx, tcy = bx + bw / 2.0, by + bh / 2.0
                r_idx, c_idx = int(tcy // tile_h), int(tcx // tile_w)
                t_idx = r_idx * 3 + c_idx
                if 0 <= t_idx < len(batch):
                    sc = float(getattr(item, "score", 0.0) or 0.0)
                    by_tile.setdefault(t_idx, []).append((bx, sc, txt))
        for idx, cell_key in enumerate(batch):
            raw_parts = by_tile.get(idx, [])
            if any(sc >= 0.80 for _, sc, _ in raw_parts):
                raw_parts = [p for p in raw_parts if p[1] >= 0.70]
            parts = [txt for _, _, txt in sorted(raw_parts, key=lambda p: p[0])]
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
        "expected": r"^\s*[0-9][0-9,\s|/]*$",
    }, image=padded)
    if not res or not res.hit:
        return None
    raw = getattr(res.best_result, "text", "") or ""
    match = re.match(r"^\s*([0-9][0-9,\s]*)", raw)
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(1))
    return int(digits) if digits else None


def _grid_box(frame: np.ndarray) -> tuple[int, int]:
    return GRID_TOP + 6, max(GRID_TOP + 120, frame.shape[0] - GRID_BOTTOM_MARGIN - 6)


def _detect_occupied_grid_rows(frame: np.ndarray, row_ys: list[int]) -> tuple[list[tuple[int, list[int]]], bool]:
    """Return ([(cy, occupied_cols)], reached_list_end) without probing empty space past list tail."""
    rows: list[tuple[int, list[int]]] = []
    reached_end = False
    for cy in row_ys:
        occ_cols = [col for col in range(8) if is_cell_occupied(frame, cy, col)]
        if not occ_cols:
            if rows:
                reached_end = True
                break
            continue
        rows.append((cy, occ_cols))
        if occ_cols[-1] < 7:
            reached_end = True
            break
    return rows, reached_end


def _same_grid(
    first: np.ndarray | None,
    second: np.ndarray | None,
    occupied_rows: list[tuple[int, int]] | None = None,
) -> bool:
    if first is None or second is None or first.shape != second.shape:
        return False
    if occupied_rows:
        scores: list[float] = []
        f_gray = cv2.cvtColor(first, cv2.COLOR_BGR2GRAY)
        s_gray = cv2.cvtColor(second, cv2.COLOR_BGR2GRAY)
        picks = occupied_rows if len(occupied_rows) <= 3 else [
            occupied_rows[0], occupied_rows[len(occupied_rows) // 2], occupied_rows[-1]
        ]
        for cy, n_cols in picks:
            y0, y1 = max(GRID_TOP, cy + 4), min(first.shape[0] - GRID_BOTTOM_MARGIN, cy + ICON_SIZE - 4)
            x0, x1 = FIRST_X + 4, min(first.shape[1] - 20, FIRST_X + max(1, n_cols) * COL_PITCH - 14)
            if y1 - y0 < 40 or x1 - x0 < 60:
                continue
            p1 = f_gray[y0:y1, x0:x1]
            s_y0, s_y1 = max(0, y0 - 2), min(second.shape[0], y1 + 2)
            p2 = s_gray[s_y0:s_y1, x0:x1]
            if float(np.std(p1)) < 4.0 or p2.shape[0] < p1.shape[0]:
                continue
            scores.append(float(np.max(cv2.matchTemplate(p2, p1, cv2.TM_CCOEFF_NORMED))))
        if scores:
            return float(np.median(scores)) >= 0.94
    y0, y1 = _grid_box(first)
    diff = cv2.absdiff(first[y0:y1, 42:806], second[y0:y1, 42:806])
    return float(np.mean(diff)) < 1.5 or float(np.mean(diff > 24)) < 0.015


def _scroll_grid_and_park(rt, dy: int, wait_seconds: float) -> np.ndarray | None:
    park_pt = clamp_capture_park_point(
        window_hwnd(rt),
        [640, 36],
        capture_size=rt._pointer_guard_context()[1],
    )
    if (not rt.action("TouchMove", target=GRID_SCROLL_TARGET)
            or not rt.pause(0.1)
            or not rt.action("Scroll", target=GRID_SCROLL_TARGET, dx=0, dy=dy)
            or not rt.pause(wait_seconds)
            or not rt.action("TouchMove", target=park_pt)
            or not rt.pause(0.12)):
        return None
    return rt.capture()


def _scroll_to_top(rt) -> np.ndarray | None:
    frame = rt.capture()
    if frame is None:
        return None
    scroll_dy = 24000 if frame.shape[0] > 1200 else 14400
    for _ in range(12):
        if rt.stopped or frame is None:
            return None
        grid_bottom = frame.shape[0] - GRID_BOTTOM_MARGIN
        row_ys = detect_grid_rows(frame, grid_top=GRID_TOP, grid_bottom=grid_bottom)
        occ_rows, _ = _detect_occupied_grid_rows(frame, row_ys)
        occ_anchors = [(cy, len(cols)) for cy, cols in occ_rows]
        after = _scroll_grid_and_park(rt, scroll_dy, 0.28)
        if after is None:
            return None
        if _same_grid(frame, after, occ_anchors):
            return after
        frame = after
    return frame


def _measure_scroll_delta(
    before: np.ndarray,
    after: np.ndarray,
    anchors: list[tuple[int, int]],
) -> int | None:
    if before is None or after is None or before.shape != after.shape or not anchors:
        return None
    grid_bottom = before.shape[0] - GRID_BOTTOM_MARGIN
    # Anchor against the lowest occupied rows with sufficient width & vertical travel room.
    picks = [(cy, n_cols) for cy, n_cols in reversed(anchors) if cy >= GRID_TOP + 4][:2]
    b_gray = cv2.cvtColor(before, cv2.COLOR_BGR2GRAY)
    a_gray = cv2.cvtColor(after, cv2.COLOR_BGR2GRAY)
    votes: list[int] = []
    for cy, n_cols in picks:
        x0 = FIRST_X + 4
        x1 = min(before.shape[1] - 10, FIRST_X + max(1, n_cols) * COL_PITCH - 14)
        tmpl = b_gray[cy:cy + 80, x0:x1]
        if tmpl.shape[0] < 80 or tmpl.shape[1] < 60 or float(np.std(tmpl)) < 4.0:
            continue
        low = max(GRID_TOP - 8, 0)
        high = min(cy + 4, grid_bottom - 80)
        if high < low + 10:
            continue
        search = a_gray[low:high + 80, x0:x1]
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
    *,
    prev_panel: np.ndarray | None = None,
    slot_key: tuple[int, int] | None = None,
) -> tuple[str | None, str | None, np.ndarray | None]:
    cx = FIRST_X + col * COL_PITCH
    if not rt.action("Click", target=[cx + 42, cy + 42]) or not rt.pause(0.2):
        return None, None, prev_panel
    clicked = rt.capture()
    if clicked is None:
        return None, None, prev_panel
    cur_panel = clicked[80:280, 885:1245]
    if slot_key != (0, 0) and prev_panel is not None and prev_panel.shape == cur_panel.shape:
        if float(np.mean(cv2.absdiff(prev_panel, cur_panel))) < 2.0:
            return "__EMPTY__", None, cur_panel
    title = rt.text(RIGHT_TITLE_ROI, image=clicked)
    hits = model.lookup_by_name(tab, title)
    if hits:
        return hits[0], None, cur_panel
    cat_fallback = cat_by_norm.get(normalize_name(title))
    return None, cat_fallback, cur_panel


def _finalize_tab_stats(stats: dict, deferred_slots: set) -> dict:
    traversed = bool(stats.get("traversed")) and not deferred_slots
    items = int(stats.get("items", 0))
    unknown = int(stats.get("unknown", 0))
    stats["traversed"] = traversed
    stats["complete"] = traversed and unknown == 0
    stats["usable"] = traversed and (unknown == 0 or (items > 0 and unknown <= max(5, int(items * 0.05))))
    return stats


def _scan_tab(
    rt,
    model: BackpackModel,
    tab: int,
    is_tall: bool,
    crafting_catalog: dict,
    *,
    target_slots: set[tuple[int, int]] | None = None,
) -> tuple[dict[str, int], dict, set[tuple[int, int]]]:
    counts: dict[str, int] = {}
    stats = {"items": 0, "clicked": 0, "unknown": 0, "traversed": False, "complete": False, "usable": False}
    deferred_slots: set[tuple[int, int]] = set()
    frame = _scroll_to_top(rt)
    if frame is None:
        return counts, stats, deferred_slots

    hwnd = window_hwnd(rt)
    grid_bottom = frame.shape[0] - GRID_BOTTOM_MARGIN
    scroll_y = 0
    first_row_y: int | None = None
    seen_slots: set[tuple[int, int]] = set()
    cat_by_norm = {
        normalize_name(meta.get("name", "")): str(mid)
        for mid, meta in (crafting_catalog.get("materials") or {}).items()
        if str(mid) != model.bling_id
    }
    px_per_unit: float | None = None
    at_bottom_stuck = False

    for _page in range(48):
        if rt.stopped:
            return counts, stats, deferred_slots
        row_ys = detect_grid_rows(frame, grid_top=GRID_TOP, grid_bottom=grid_bottom)
        if not row_ys:
            # No rows visible on initial top view -> genuine empty tab.
            if _page == 0 and target_slots is None:
                stats["traversed"] = True
                _finalize_tab_stats(stats, deferred_slots)
            break
        if first_row_y is None:
            first_row_y = row_ys[0]

        occ_rows, reached_list_end = _detect_occupied_grid_rows(frame, row_ys)
        if not occ_rows:
            if _page == 0 and target_slots is None:
                stats["traversed"] = True
                _finalize_tab_stats(stats, deferred_slots)
            break
        occ_anchors = [(cy, max(cols) + 1) for cy, cols in occ_rows]

        safe_min_y, safe_max_y = safe_capture_y_range(
            hwnd,
            frame.shape[0],
            capture_width=frame.shape[1],
            margin_px=8,
        )
        pending_qty: list[tuple[int, int, str]] = []  # (cy, col, storage_key)
        blocked_cy: int | None = None
        cur_panel = frame[80:280, 885:1245]
        max_visible_row = -1

        for cy, present_cols in occ_rows:
            row_idx = int(round((cy + scroll_y - first_row_y) / ROW_PITCH_EXACT))
            if row_idx < 0:
                continue
            max_visible_row = max(max_visible_row, row_idx)
            row_candidates = []
            for col in present_cols:
                slot_key = (row_idx, col)
                if slot_key in seen_slots or slot_key in deferred_slots:
                    continue
                if target_slots is not None and slot_key not in target_slots:
                    continue
                tmpl, sc, ncc, _l1, margin = model.match_cell(frame, cy, col, tab)
                ambiguous = tmpl is not None and len(set(tmpl.get("names") or [])) > 1
                confident = (
                    tmpl is not None
                    and not ambiguous
                    and (
                        (ncc >= 0.60 and sc >= 0.25 and margin >= 0.05)
                        or (ncc >= 0.50 and sc >= 0.22 and margin >= 0.14)
                        or (ncc >= 0.90 and sc >= 0.80 and margin >= 0.03)
                    )
                )
                row_candidates.append((col, slot_key, tmpl, confident))

            if not row_candidates:
                continue

            click_y = cy + ICON_SIZE // 2
            row_outside_safe = click_y < safe_min_y or click_y > safe_max_y
            needs_click = any(not confident for _, _, _, confident in row_candidates)
            if needs_click and row_outside_safe and not at_bottom_stuck:
                # Scroll unconfident lower rows into the safe top band before clicking so nothing touches the taskbar.
                blocked_cy = cy
                break

            for col, slot_key, tmpl, confident in row_candidates:
                seen_slots.add(slot_key)
                tb_id: str | None = None
                if confident:
                    tb_id = tmpl["item_ids"][0]
                elif row_outside_safe:
                    deferred_slots.add(slot_key)
                    continue
                else:
                    stats["clicked"] += 1
                    tb_id, cat_fallback, cur_panel = _resolve_clicked_item(
                        rt,
                        model,
                        tab,
                        cy,
                        col,
                        cat_by_norm,
                        prev_panel=cur_panel,
                        slot_key=slot_key,
                    )
                    if tb_id == "__EMPTY__":
                        stats["clicked"] = max(0, stats["clicked"] - 1)
                        continue
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

        remaining_targets = (target_slots - seen_slots - deferred_slots) if target_slots is not None else set()
        if (
            at_bottom_stuck
            or (reached_list_end and blocked_cy is None and not remaining_targets)
            or (target_slots is not None and not remaining_targets)
        ):
            if target_slots is None:
                stats["traversed"] = (at_bottom_stuck or reached_list_end) and blocked_cy is None
            else:
                stats["traversed"] = not remaining_targets
            _finalize_tab_stats(stats, deferred_slots)
            return counts, stats, deferred_slots

        if blocked_cy is not None and is_tall:
            desired_y = max(GRID_TOP + 80, min(safe_max_y - ICON_SIZE - 48, safe_max_y // 2))
            want_px = max(240, blocked_cy - desired_y)
            rate = px_per_unit if px_per_unit is not None else 0.45
            step_dy = -max(360, min(4200, round(want_px / rate / 120.0) * 120))
        elif target_slots is not None and not is_tall and remaining_targets and max_visible_row >= 0:
            min_target_row = min(r for r, _ in remaining_targets)
            if min_target_row > max_visible_row + 1:
                rate = px_per_unit if px_per_unit is not None else 0.21
                want_px = min(390.0, max(180.0, (min_target_row - max_visible_row - 1) * ROW_PITCH_EXACT))
                step_dy = -max(960, min(2400, round(want_px / rate / 120.0) * 120))
            else:
                step_dy = -960
        else:
            step_dy = -4200 if is_tall else -960

        moved = False
        for attempt_dy in (step_dy, step_dy // 2, step_dy // 4):
            after = _scroll_grid_and_park(rt, attempt_dy, 0.32)
            if after is None:
                return counts, stats, deferred_slots
            same = _same_grid(frame, after, occ_anchors)
            delta = 0 if same else _measure_scroll_delta(frame, after, occ_anchors)
            if same or delta == 0:
                # Retry once to rule out a dropped Slate scroll event before concluding end of list.
                after = _scroll_grid_and_park(rt, attempt_dy, 0.32)
                if after is None:
                    return counts, stats, deferred_slots
                same = _same_grid(frame, after, occ_anchors)
                delta = 0 if same else _measure_scroll_delta(frame, after, occ_anchors)
                if same or delta == 0:
                    if blocked_cy is not None:
                        # Tail rows are stuck below safe work area: count confident cells and defer clicks to normal view.
                        at_bottom_stuck = True
                        moved = True
                        break
                    stats["traversed"] = True
                    _finalize_tab_stats(stats, deferred_slots)
                    return counts, stats, deferred_slots
            if delta is not None and delta > 0:
                if attempt_dy != 0:
                    px_per_unit = max(0.08, min(2.0, delta / abs(attempt_dy)))
                scroll_y += delta
                frame = after
                moved = True
                break
            # Overshot overlap window -> restore cursor on grid, rewind upward, and retry smaller step.
            if _scroll_grid_and_park(rt, -attempt_dy, 0.25) is None:
                return counts, stats, deferred_slots
        if not moved:
            break
    return counts, stats, deferred_slots


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


def scan_backpack_inventory(
    rt,
    crafting_catalog: dict,
    *,
    tabs: tuple[int, ...] = (1, 2),
) -> tuple[dict[str, int], dict] | None:
    """Open backpack and scan requested tabs (1=材料, 2=养成材料) plus top Bling."""
    scan_tabs = tuple(t for t in (1, 2) if t in tabs)
    if not scan_tabs:
        return None
    if not rt.hit("MaaNikki_BackpackReady"):
        if not rt.main():
            return None
        if not rt.ui.enter_page(
            lambda: rt.key("backpack"),
            "MaaNikki_BackpackReady",
            source="MaaNikki_MainDetected",
            recover=rt.ui.main,
            attempts=2,
        ):
            rt.log("背包扫描：未能打开背包界面，沿用已有材料库存记录。")
            return None

    model = load_backpack_model()
    found: dict[str, int] = {}
    summary: dict = {"complete": False, "scanned_tabs": list(scan_tabs), "tab1": {}, "tab2": {}, "tall_mode": False}
    try:
        frame = rt.capture()
        if frame is None:
            return None
        bling = read_top_bling(rt, frame)
        if bling is not None:
            found[model.bling_id] = bling
            summary["bling"] = bling

        deferred_by_tab: dict[int, set[tuple[int, int]]] = {}
        entered_tabs: set[int] = set()
        use_tall = 1 in scan_tabs
        tall_ctx = tall_window(rt) if use_tall else nullcontext(False)
        with tall_ctx as is_tall:
            summary["tall_mode"] = bool(is_tall)
            if is_tall:
                rt.log("背包扫描：已切换超长竖屏模式，正在读取材料库存。")

            for tab in scan_tabs:
                want_growth = tab == 2
                if not _switch_tab(rt, want_growth=want_growth):
                    if tab == scan_tabs[0]:
                        return None
                    continue
                entered_tabs.add(tab)
                c_tab, s_tab, deferred_tab = _scan_tab(rt, model, tab, is_tall, crafting_catalog)
                for k, v in c_tab.items():
                    found[k] = found.get(k, 0) + v
                summary[f"tab{tab}"] = s_tab
                deferred_by_tab[tab] = deferred_tab

        for tab in scan_tabs:
            deferred_set = deferred_by_tab.get(tab)
            if rt.stopped or not deferred_set or tab not in entered_tabs:
                continue
            want_growth = tab == 2
            tab_key = f"tab{tab}"
            rt.log("背包扫描：已恢复常规窗口，正在补充识别列表末尾待定材料。", detail=True)
            if _switch_tab(rt, want_growth=want_growth):
                dc, ds, rem = _scan_tab(
                    rt,
                    model,
                    tab,
                    False,
                    crafting_catalog,
                    target_slots=deferred_set,
                )
                for k, v in dc.items():
                    found[k] = found.get(k, 0) + v
                tab_stats = summary[tab_key]
                tab_stats["items"] = int(tab_stats.get("items", 0)) + int(ds.get("items", 0))
                tab_stats["clicked"] = int(tab_stats.get("clicked", 0)) + int(ds.get("clicked", 0))
                tab_stats["unknown"] = int(tab_stats.get("unknown", 0)) + int(ds.get("unknown", 0))
                tab_stats["traversed"] = bool(ds.get("traversed")) and not rem
                _finalize_tab_stats(tab_stats, rem)

        summary["scanned_tabs"] = sorted(entered_tabs)
        summary["complete"] = bool(entered_tabs) and all(
            bool((summary.get(f"tab{t}") or {}).get("complete")) for t in entered_tabs
        )
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
    scanned_tabs = {int(t) for t in (summary.get("scanned_tabs") or (1, 2)) if int(t) in (1, 2)}
    completed_tabs = {tab for tab in scanned_tabs if (summary.get(f"tab{tab}") or {}).get("complete")}
    zeroable_tabs = {
        tab for tab in scanned_tabs
        if (summary.get(f"tab{tab}") or {}).get("complete")
        or (
            (summary.get(f"tab{tab}") or {}).get("usable")
            and int((summary.get(f"tab{tab}") or {}).get("unknown", 0)) == 0
        )
    }
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
            if tab in zeroable_tabs:
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

        tab_scans = dict(m.get("backpack_tabs") or {})
        for tab in scanned_tabs:
            ts = dict(summary.get(f"tab{tab}") or {})
            tab_scans[str(tab)] = {
                "scanned_day": today,
                "updated_at": now,
                "traversed": bool(ts.get("traversed")),
                "complete": bool(ts.get("complete")),
                "usable": bool(ts.get("usable")),
                "items": int(ts.get("items", 0)),
                "unknown": int(ts.get("unknown", 0)),
            }
        m["backpack_tabs"] = tab_scans

        prev_summary = m.get("backpack_summary") if isinstance(m.get("backpack_summary"), dict) else {}
        merged_t1 = summary.get("tab1") if 1 in scanned_tabs else (prev_summary.get("tab1") or {})
        merged_t2 = summary.get("tab2") if 2 in scanned_tabs else (prev_summary.get("tab2") or {})
        merged_bling = summary.get("bling") if "bling" in summary else prev_summary.get("bling")

        m["backpack_scanned_day"] = today
        m["inventory_updated_at"] = now
        m["backpack_summary"] = {
            "updated_at": now,
            "complete": bool(merged_t1.get("complete")) and bool(merged_t2.get("complete")),
            "tall_mode": bool(summary.get("tall_mode")),
            "tab1": merged_t1,
            "tab2": merged_t2,
            "bling": merged_bling,
            "total_tracked": len(cur),
        }

    account.update(update)
    for line in logs:
        rt.log(line)
    role_after = account.load()
    return dict((role_after.get("materials") or {}).get("inventory") or {})


def _tab_record(mats: dict, tab: int) -> dict | None:
    tabs = mats.get("backpack_tabs")
    if isinstance(tabs, dict) and isinstance(tabs.get(str(tab)), dict):
        rec = dict(tabs[str(tab)])
        if "usable" not in rec:
            items = int(rec.get("items", 0))
            unknown = int(rec.get("unknown", 0))
            rec["usable"] = bool(rec.get("complete")) or (items > 0 and unknown <= max(5, int(items * 0.05)))
        return rec
    day = mats.get("backpack_scanned_day")
    summary = mats.get("backpack_summary") if isinstance(mats.get("backpack_summary"), dict) else {}
    tinfo = summary.get(f"tab{tab}") if isinstance(summary.get(f"tab{tab}"), dict) else None
    if not day or not isinstance(tinfo, dict) or not tinfo:
        return None
    items = int(tinfo.get("items", 0))
    unknown = int(tinfo.get("unknown", 0))
    complete = bool(tinfo.get("complete"))
    usable = complete or (items > 0 and unknown <= max(5, int(items * 0.05)))
    return {
        "scanned_day": str(day),
        "updated_at": str(summary.get("updated_at") or mats.get("inventory_updated_at") or ""),
        "traversed": complete or items > 0,
        "complete": complete,
        "usable": usable,
        "items": items,
        "unknown": unknown,
    }


def _tab_age_days(rec: dict | None, today_date: date) -> int:
    if not rec:
        return 9999
    day_str = str(rec.get("scanned_day") or "")
    try:
        parsed = date.fromisoformat(day_str)
    except ValueError:
        return 9999
    return max(0, (today_date - parsed).days)


def has_backpack_baseline(role: dict, needed_tabs: set[int] | None = None) -> bool:
    """Whether the role has usable baseline records for every tab in ``needed_tabs``."""
    mats = role.get("materials") or {}
    if not mats.get("inventory") and not mats.get("backpack_summary") and not mats.get("backpack_tabs"):
        return False
    check_tabs = needed_tabs if needed_tabs else {1, 2}
    for tab in check_tabs:
        rec = _tab_record(mats, tab)
        if not rec or not rec.get("usable"):
            return False
    return True


def tabs_needing_refresh(
    role: dict,
    crafting_catalog: dict,
    needed_mids: dict[str, int] | None = None,
) -> tuple[int, ...]:
    """Choose only the backpack tabs needing refresh based on age TTL and completion proximity."""
    mats = role.get("materials") or {}
    inv = mats.get("inventory") or {}
    today_date = game_day()

    if needed_mids is None:
        tabs: list[int] = []
        for tab, ttl in ((1, TAB1_LEDGER_TTL_DAYS), (2, TAB2_LEDGER_TTL_DAYS)):
            rec = _tab_record(mats, tab)
            if not rec or not rec.get("usable") or _tab_age_days(rec, today_date) >= ttl:
                tabs.append(tab)
        return tuple(tabs)

    tab_map = catalog_tab_map()
    mat_defs = crafting_catalog.get("materials") or {}
    refresh: set[int] = set()
    checked_tabs: dict[int, tuple[int, bool]] = {}

    for raw_mid, raw_need in needed_mids.items():
        need = int(raw_need or 0)
        if need <= 0:
            continue
        mid = str(raw_mid)
        raw_tab = tab_map.get(mid, 1)
        tab = 2 if raw_tab == 0 else raw_tab
        if tab not in checked_tabs:
            rec = _tab_record(mats, tab)
            age = _tab_age_days(rec, today_date)
            usable = bool(rec and rec.get("usable"))
            checked_tabs[tab] = (age, usable)
            ttl = TAB1_LEDGER_TTL_DAYS if tab == 1 else TAB2_LEDGER_TTL_DAYS
            if not usable or age >= ttl:
                refresh.add(tab)
                continue
        age, usable = checked_tabs[tab]
        if tab in refresh or age <= 0:
            continue
        meta = mat_defs.get(mid) or {}
        drop = max(1, int(meta.get("drop") or 1))
        realm = meta.get("realm")
        owned = int(inv.get(mid, 0))
        deficit = need - owned
        near_runs = 12 if realm == "jihua" else 4
        surplus_runs = 6 if realm == "jihua" else 2
        if (0 < deficit <= drop * near_runs) or (0 <= -deficit <= drop * surplus_runs):
            refresh.add(tab)

    return tuple(sorted(refresh))


def ensure_backpack_scanned(
    rt,
    account: AccountState,
    crafting_catalog: dict,
    *,
    needed_mids: dict[str, int] | None = None,
    allow_init_scan: bool = True,
) -> dict[str, int]:
    role = account.load()
    mats = role.get("materials") or {}
    inv = dict(mats.get("inventory") or {})
    refresh_tabs = tabs_needing_refresh(role, crafting_catalog, needed_mids)
    if not refresh_tabs:
        return inv
    if not allow_init_scan:
        refresh_tabs = tuple(t for t in refresh_tabs if _tab_record(mats, t) is not None)
        if not refresh_tabs:
            return inv
    if refresh_tabs == (2,):
        rt.log("正在快速核对养成材料与噗灵库存（仅养成材料页）。")
    else:
        rt.log("正在打开背包核对所需材料库存。")
    result = scan_backpack_inventory(rt, crafting_catalog, tabs=refresh_tabs)
    if not result:
        return inv
    scanned, summary = result
    updated_inv = _apply_backpack_results(rt, account, crafting_catalog, scanned, summary)
    rt.log(f"背包材料库存核对完成：本次读取 {len(scanned)} 项有库存物品（累计追踪 {len(updated_inv)} 项）。")
    return updated_inv


def rescan_backpack(rt, account: AccountState, crafting_catalog: dict) -> dict[str, int] | None:
    """Manual full backpack read returning updated inventory or None on failure."""
    result = scan_backpack_inventory(rt, crafting_catalog, tabs=(1, 2))
    if not result:
        return None
    scanned, summary = result
    updated_inv = _apply_backpack_results(rt, account, crafting_catalog, scanned, summary)
    t1 = (summary.get("tab1") or {}).get("items", 0)
    t2 = (summary.get("tab2") or {}).get("items", 0)
    rt.log(f"背包读取完成：材料页 {t1} 项、养成材料页 {t2} 项（累计追踪 {len(updated_inv)} 项）。")
    return updated_inv
