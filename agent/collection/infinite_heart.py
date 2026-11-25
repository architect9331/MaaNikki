"""Infinite Heart (无限之心) GLOBAL + zoomed multi-frame decoder and automated game scanner.

Two-stage recognition grounded in Infinity Nikki's UMG/Lua rendering rules:
1. Minimum zoom (`GLOBAL`, scale 1.0): camera translation is clamped to `(0, 0)`,
   providing a deterministic HUD currency capture and full-shard mural baseline.
2. Medium zoom (`MEDIUM`, scale 2.385, 1 click of `+`) + Detail zoom (`DETAIL`,
   scale 4.667, 2 clicks of `+`): renders crisp individual icons for all 383 nodes
   (eliminating `group_all`, `unmapped`, `shared_root`, and thin-stroke ambiguities)
   and prints exact `lv/10` progress labels beneath the 5 multi-level styling nodes.

Because node layout is rigid in UMG coordinates, any zoomed frame is auto-aligned
to `< 1 px` via Sobel edge-energy correlation over the exact node constellation,
and local ring-background subtraction removes screen-edge radial vignette fade.
"""
from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
import json
from pathlib import Path
import re

import cv2
import numpy as np

from account_state import AccountState, account_lock
from daily.settings import ROOT
from data_paths import data_path
from file_io import write_json
from task_control import TaskStopped

MODEL_W = 1280
MODEL_H = 720
SCALE_1080_TO_720 = MODEL_W / 1920.0

# Exact zoom scales from game Lua TalentGraphConfig.configZoomScale
ZOOM_SCALE_GLOBAL = 1.0
ZOOM_SCALE_MEDIUM = 2.385
ZOOM_SCALE_DETAIL = 4.667

# Top-left page header & top-right shard name + currency ROIs at 1280x720
HEADER_TITLE_ROI = [60, 14, 170, 44]
SHARD_NAME_ROI = [1040, 72, 230, 52]
HUD_CURRENCY_ROI = [995, 14, 275, 44]
HUD_HOME_MANAGE_ROI = [760, 0, 520, 90]

# Florawish checkpoint used when leaving Home (where the Infinite Heart hotkey opens 家园经营 instead).
WORLD_EXIT_TELEPORT = {
    "name": "心愿花街",
    "province": "心愿原野",
    "region": "花愿镇",
    "x": 10260.861,
    "y": 10085.844,
}

# Top-center shard tab click centers at 1280x720 (verified against live screenshots):
# Left: 星海碎片 (ver 3), Middle: 心愿原野碎片 (ver 1), Right: 伊赞之土碎片 (ver 2)
SHARD_TABS = (
    (3, "星海碎片", [521, 38]),
    (1, "心愿原野碎片", [640, 38]),
    (2, "伊赞之土碎片", [759, 38]),
)
# Bottom-left zoom controls at 1280x720 (verified against live screenshots)
ZOOM_OUT_BTN = [88, 661]
ZOOM_MIN_TRACK = [128, 661]
ZOOM_IN_BTN = [274, 661]
CANVAS_CENTER = [640, 360]

# Wishing 5 multi-level (Lv.0..10) styling nodes
STYLING_MULTI_LEVEL_IDS = (1030101, 1030102, 1030103, 1030104, 1030105)


@lru_cache(maxsize=1)
def load_heart_assets() -> tuple[dict, dict, dict[str, np.ndarray]]:
    catalog = json.loads(data_path("resource/data/infinite_heart_catalog.json", ROOT).read_text(encoding="utf-8"))
    rules = json.loads(data_path("resource/model/infinite_heart/rules.json", ROOT).read_text(encoding="utf-8"))
    raw_npz = np.load(data_path("resource/model/infinite_heart/masks.npz", ROOT))
    masks = {key: raw_npz[key] for key in raw_npz.files}
    return catalog, rules, masks


def _to_720_bgr(image_bgr: np.ndarray) -> np.ndarray:
    """Normalize a BGR screenshot (any 16:9 size) to 1280x720 BGR."""
    h, w = image_bgr.shape[:2]
    if (w, h) != (MODEL_W, MODEL_H):
        interp = cv2.INTER_AREA if w > MODEL_W else cv2.INTER_LINEAR
        return cv2.resize(image_bgr, (MODEL_W, MODEL_H), interpolation=interp)
    return image_bgr


def _to_720_rgb(image_bgr: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(_to_720_bgr(image_bgr), cv2.COLOR_BGR2RGB)


def _unpack_art_mask(packed: np.ndarray) -> np.ndarray:
    bits = np.unpackbits(packed)[: MODEL_W * MODEL_H]
    return bits.reshape((MODEL_H, MODEL_W)).astype(bool)


def _local_delta_lum(rgb: np.ndarray, art_mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return (luminance, luminance - smooth_local_non_artwork_background) at 1280x720."""
    arr = rgb.astype(np.float32)
    lum = 0.2126 * arr[:, :, 0] + 0.7152 * arr[:, :, 1] + 0.0722 * arr[:, :, 2]
    dilated = cv2.dilate(art_mask.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)
    bg_valid = (~dilated).astype(np.float32)
    num = cv2.GaussianBlur(lum * bg_valid, (0, 0), sigmaX=30.0, sigmaY=30.0)
    den = cv2.GaussianBlur(bg_valid, (0, 0), sigmaX=30.0, sigmaY=30.0)
    bg_lum = num / np.maximum(den, 1e-4)
    return lum, lum - bg_lum


def _slice_score(pts: np.ndarray, delta_lum: np.ndarray, version_id: int) -> tuple[float, float, int]:
    """Compute signed lit-vs-dark score in [-1, +1] and median delta-luminance for a sparse mask."""
    cnt = int(len(pts))
    if cnt == 0:
        return 0.0, 0.0, 0
    dvs = delta_lum[pts[:, 0], pts[:, 1]]
    med = float(np.median(dvs))
    if version_id == 3:
        per_px = np.clip((dvs - 28.0) / 30.0, -1.0, 1.0)
    else:
        per_px = np.clip(dvs / 25.0, -1.0, 1.0)
    return float(np.mean(per_px)), med, cnt


def _apply_dependency_closure(decoded: dict[int, dict], ver_nodes: dict[int, dict]) -> None:
    """Propagate unlock consistency without letting unverified GLOBAL nodes override zoom-verified locked nodes."""
    # First, if a predecessor is directly zoom-verified as locked (level 0), any unverified GLOBAL
    # descendant cannot be unlocked either.
    zoom_sources = ("zoom", "zoom_ocr", "zoom_inferred")
    changed = True
    while changed:
        changed = False
        for nid, entry in decoded.items():
            if entry.get("source") in ("zoom", "zoom_ocr"):
                continue
            if entry["state"] == "locked" and entry.get("source") == "zoom_inferred":
                continue
            preds = ver_nodes.get(nid, {}).get("predecessor_ids") or []
            if preds and any(
                decoded.get(pid, {}).get("source") in zoom_sources
                and decoded.get(pid, {}).get("state") == "locked"
                and int(decoded.get(pid, {}).get("level", 0)) == 0
                for pid in preds
            ):
                entry["state"] = "locked"
                entry["level"] = 0
                entry["source"] = "zoom_inferred"
                changed = True

    changed = True
    while changed:
        changed = False
        for nid, entry in decoded.items():
            if entry["state"] != "unlocked":
                continue
            src_is_zoom = entry.get("source") in ("zoom", "zoom_ocr")
            for pid in ver_nodes.get(nid, {}).get("predecessor_ids") or []:
                pred_entry = decoded.get(pid)
                if not pred_entry or pred_entry["state"] == "unlocked":
                    continue
                if pred_entry.get("source") in zoom_sources and not src_is_zoom:
                    continue
                max_lv = int(ver_nodes.get(pid, {}).get("max_level", 1))
                if max_lv == 1:
                    pred_entry["state"] = "unlocked"
                    pred_entry["level"] = 1
                    changed = True


def _shard_stats(decoded: dict[int, dict]) -> dict[str, int]:
    return {
        "unlocked": sum(1 for v in decoded.values() if v["state"] == "unlocked"),
        "locked": sum(1 for v in decoded.values() if v["state"] == "locked"),
        "ambiguous": sum(1 for v in decoded.values() if v["state"] == "ambiguous"),
        "total": len(decoded),
    }


def decode_shard_image(image_bgr: np.ndarray, version_id: int, debug_out: Path | None = None) -> dict:
    """Decode one shard's GLOBAL-view screenshot into per-node states & confidence scores."""
    catalog, rules, masks = load_heart_assets()
    vid_key = str(version_id)
    shard_rules = rules["shards"][vid_key]
    art_key = {1: "wish_art", 2: "loe_art", 3: "ss_art"}[version_id]
    art_mask = _unpack_art_mask(masks[art_key])

    rgb = _to_720_rgb(image_bgr)
    _, delta_lum = _local_delta_lum(rgb, art_mask)

    ver_nodes = {int(n["id"]): n for n in catalog["nodes"] if int(n["version_id"]) == version_id}
    decoded: dict[int, dict] = {}
    debug_overlay = np.zeros((MODEL_H, MODEL_W, 4), dtype=np.uint8) if debug_out is not None else None

    def paint(pts: np.ndarray, color_rgba: tuple[int, int, int, int]) -> None:
        if debug_overlay is not None and len(pts) > 0:
            debug_overlay[pts[:, 0], pts[:, 1]] = color_rgba

    # 1. Prefix-constrained chains
    for chain in shard_rules.get("prefix_chains") or []:
        items = chain["nodes"]
        n = len(items)
        scores: list[tuple[float, float, int, np.ndarray]] = []
        for item in items:
            pts = masks[item["mask"]]
            sc, med, cnt = _slice_score(pts, delta_lum, version_id)
            scores.append((sc, med, cnt, pts))

        best_k = 0
        best_obj = -1e18
        for k in range(n + 1):
            obj = 0.0
            for i in range(n):
                sc, _, cnt, _ = scores[i]
                w = float(min(cnt, 400)) ** 0.5
                obj += w * (sc if i < k else -sc)
            if obj > best_obj:
                best_obj = obj
                best_k = k

        for i, item in enumerate(items):
            nid = int(item["node_id"])
            sc, _, cnt, pts = scores[i]
            unlocked = i < best_k
            max_lv = int(ver_nodes.get(nid, {}).get("max_level", 1))
            decoded[nid] = {
                "state": "unlocked" if unlocked else "locked",
                "level": max_lv if unlocked else 0,
                "score": round(sc, 3),
                "px": cnt,
            }
            paint(pts, (46, 213, 115, 190) if unlocked else (235, 77, 75, 115))

    # 2. Independent single masks
    for item in shard_rules.get("singles") or []:
        nid = int(item["node_id"])
        pts = masks[item["mask"]]
        sc, _, cnt = _slice_score(pts, delta_lum, version_id)
        max_lv = int(ver_nodes.get(nid, {}).get("max_level", 1))
        if sc > 0.12:
            state = "unlocked"
            level = max_lv
            paint(pts, (46, 213, 115, 190))
        elif max_lv > 1:
            state = "ambiguous"
            level = 0
            paint(pts, (255, 195, 18, 175))
        elif sc < -0.10:
            state = "locked"
            level = 0
            paint(pts, (235, 77, 75, 115))
        else:
            state = "ambiguous"
            level = 0
            paint(pts, (255, 195, 18, 175))
        decoded[nid] = {"state": state, "level": level, "score": round(sc, 3), "px": cnt}

    # 3. Group-all indicators
    for grp in shard_rules.get("group_all") or []:
        pts = masks[grp["mask"]]
        sc, _, cnt = _slice_score(pts, delta_lum, version_id)
        all_unlocked = sc > 0.14
        paint(pts, (46, 213, 115, 190) if all_unlocked else (255, 195, 18, 150))
        for raw_id in grp["node_ids"]:
            nid = int(raw_id)
            if nid in decoded and decoded[nid]["state"] == "unlocked":
                continue
            max_lv = int(ver_nodes.get(nid, {}).get("max_level", 1))
            if all_unlocked:
                decoded[nid] = {"state": "unlocked", "level": max_lv, "score": round(sc, 3), "px": cnt}
            else:
                decoded[nid] = {"state": "ambiguous", "level": 0, "score": round(sc, 3), "px": cnt}

    # 4. Shared-any root polygons
    for grp in shard_rules.get("shared_any") or []:
        pts = masks[grp["mask"]]
        sc, _, cnt = _slice_score(pts, delta_lum, version_id)
        any_unlocked = sc > 0.12
        paint(pts, (46, 213, 115, 170) if any_unlocked else (235, 77, 75, 115))
        nids = [int(x) for x in grp["node_ids"]]
        if not any_unlocked:
            for nid in nids:
                decoded[nid] = {"state": "locked", "level": 0, "score": round(sc, 3), "px": cnt}
        else:
            for nid in nids:
                succs = ver_nodes.get(nid, {}).get("successor_ids") or []
                if any(decoded.get(sid, {}).get("state") == "unlocked" for sid in succs):
                    decoded[nid] = {"state": "unlocked", "level": 1, "score": round(sc, 3), "px": cnt}
                else:
                    decoded[nid] = {"state": "ambiguous", "level": 0, "score": round(sc, 3), "px": cnt}

    # 5. Unmapped / duplicate-polygon nodes
    for nid, meta in ver_nodes.items():
        if nid in decoded:
            continue
        preds = meta.get("predecessor_ids") or []
        if preds and all(decoded.get(pid, {}).get("state") == "locked" for pid in preds):
            decoded[nid] = {"state": "locked", "level": 0, "score": 0.0, "px": 0}
        else:
            decoded[nid] = {"state": "ambiguous", "level": 0, "score": 0.0, "px": 0}

    # 6. Dependency closure
    _apply_dependency_closure(decoded, ver_nodes)

    if debug_out is not None and debug_overlay is not None:
        debug_out.parent.mkdir(parents=True, exist_ok=True)
        alpha = debug_overlay[:, :, 3:4].astype(np.float32) / 255.0
        blended = rgb.astype(np.float32) * (1.0 - alpha) + debug_overlay[:, :, :3].astype(np.float32) * alpha
        cv2.imwrite(str(debug_out), cv2.cvtColor(np.clip(blended, 0, 255).astype(np.uint8), cv2.COLOR_RGB2BGR))

    return {
        "version_id": version_id,
        "nodes": {str(k): v for k, v in sorted(decoded.items())},
        "stats": _shard_stats(decoded),
    }


def _node_scaled_coords(version_id: int, scale: float) -> tuple[list[dict], np.ndarray, np.ndarray]:
    """Return (nodes_meta, raw_x_720, raw_y_720) at `scale` before camera translation (tx, ty)."""
    catalog, _, _ = load_heart_assets()
    nodes = [n for n in catalog["nodes"] if int(n["version_id"]) == version_id and n.get("screen_pos")]
    xs = np.array([640.0 + scale * (float(n["screen_pos"][0]) * SCALE_1080_TO_720 - 640.0) for n in nodes], dtype=np.float32)
    ys = np.array([360.0 + scale * (float(n["screen_pos"][1]) * SCALE_1080_TO_720 - 360.0) for n in nodes], dtype=np.float32)
    return nodes, xs, ys


def _is_safe_canvas_xy(x: float, y: float) -> bool:
    """True when (x, y) at 1280x720 lies clear of top header, bottom zoom bar, and key legend."""
    if not (24.0 <= x < 1256.0 and 68.0 <= y < 652.0):
        return False
    if x < 296.0 and y > 610.0:
        return False
    if x > 1050.0 and y > 630.0:
        return False
    return True


def _edge_energy_map(img_bgr: np.ndarray, scale: float) -> np.ndarray:
    """Compute blurred Sobel gradient map masked to non-HUD canvas for fast node-constellation alignment."""
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    mag = np.sqrt(gx * gx + gy * gy)
    # Use a radius wide enough (~3.2*scale) so large bright StarSea diamonds collapse into a single central peak.
    energy = cv2.GaussianBlur(mag, (0, 0), max(4.0, 3.2 * scale))
    energy[:66, :] = 0.0
    energy[612:, :296] = 0.0
    energy[632:, 1050:] = 0.0
    energy[:, :20] = 0.0
    energy[:, 1260:] = 0.0
    energy[696:, :] = 0.0
    return energy


def align_zoomed_frame(
    image_bgr: np.ndarray,
    version_id: int,
    scale: float = ZOOM_SCALE_MEDIUM,
    tx_range: tuple[int, int] | None = None,
    ty_range: tuple[int, int] | None = None,
) -> tuple[int, int, float, int]:
    """Find exact integer camera translation (tx, ty) at 1280x720 for a zoomed frame.

    Uses vectorized Sobel edge-energy correlation against the rigid UMG node constellation.
    Returns `(tx, ty, score, visible_node_count)`.
    """
    img = _to_720_bgr(image_bgr)
    energy = _edge_energy_map(img, scale)
    _, raw_x, raw_y = _node_scaled_coords(version_id, scale)
    if len(raw_x) == 0:
        return 0, 0, 0.0, 0

    # Clamp bounds from WBP_HeartNodeMainBase_C (`clampXPercent=0.26, clampYPercent=1.0`),
    # widened conservatively so even non-default clamp margins are always covered.
    max_hw = int((scale - 1.0) * 640.0 * 0.55) + 40
    max_hh = int((scale - 1.0) * 360.0 * 1.12) + 40
    tx0, tx1 = tx_range if tx_range is not None else (-max_hw, max_hw)
    ty0, ty1 = ty_range if ty_range is not None else (-max_hh, max_hh)

    def eval_grid(tx_vals: np.ndarray, ty_vals: np.ndarray) -> tuple[int, int, float, int]:
        best_score = -1.0
        best_tx, best_ty, best_cnt = 0, 0, 0
        # Evaluate rows of ty vectorized over (tx, node) to stay fast and memory-light (~15 ms total).
        for ty in ty_vals:
            py = np.rint(raw_y + float(ty)).astype(np.int32)
            valid_y = (py >= 68) & (py < 652)
            if int(valid_y.sum()) < 4:
                continue
            rx = raw_x[valid_y]
            ry = py[valid_y]
            px = np.rint(tx_vals[:, None] + rx[None, :]).astype(np.int32)
            in_x = (px >= 24) & (px < 1256)
            # Exclude bottom-left zoom bar & bottom-right legend
            in_hud = ((px < 296) & (ry[None, :] > 610)) | ((px > 1050) & (ry[None, :] > 630))
            valid = in_x & (~in_hud)
            cnts = valid.sum(axis=1)
            ok = cnts >= 4
            if not np.any(ok):
                continue
            px_c = np.clip(px, 0, MODEL_W - 1)
            vals = energy[ry[None, :], px_c] * valid
            scores = vals.sum(axis=1) / np.sqrt(np.maximum(cnts, 1))
            scores = np.where(ok, scores, -1.0)
            idx = int(np.argmax(scores))
            if float(scores[idx]) > best_score:
                best_score = float(scores[idx])
                best_tx = int(tx_vals[idx])
                best_ty = int(ty)
                best_cnt = int(cnts[idx])
        return best_tx, best_ty, best_score, best_cnt

    step = max(8, int(round(scale * 4.0)))
    coarse_tx = np.arange(tx0, tx1 + 1, step, dtype=np.float32)
    coarse_ty = np.arange(ty0, ty1 + 1, step, dtype=np.int32)
    ctx, cty, _, _ = eval_grid(coarse_tx, coarse_ty)

    # Fine 1 px refinement around coarse peak
    fine_tx = np.arange(ctx - step, ctx + step + 1, 1, dtype=np.float32)
    fine_ty = np.arange(cty - step, cty + step + 1, 1, dtype=np.int32)
    return eval_grid(fine_tx, fine_ty)


def sample_zoomed_nodes(
    image_bgr: np.ndarray,
    version_id: int,
    scale: float = ZOOM_SCALE_MEDIUM,
    tx: int = 0,
    ty: int = 0,
) -> dict[int, dict]:
    """Classify every visible node in one aligned zoomed frame using local-contrast optics.

    Rationale & limits:
    - Subtracting local surrounding annulus luminance (`15 < r <= 25`) cancels the game's
      screen-edge radial vignette fade and parchment brightness variation.
    - Checking inner core (`r <= 3.5` on LOE; `r <= 5.5` delta-p85 + warm-rim guard on
      Wishing/StarSea) separates filled unlocked icons from dark-center locked and
      hollow-rim unlockable (`bCurrent`) icons with >60-point luminance margins.
    - Multi-level nodes (`max_level > 1`) only render cool-bright when maxed (Lv.10);
      lower levels render a warm golden rim + `lv/10` caption read at `DETAIL` zoom.
    """
    img = _to_720_bgr(image_bgr)
    nodes, raw_x, raw_y = _node_scaled_coords(version_id, scale)
    obs: dict[int, dict] = {}
    r_outer = int(round(26 * (scale / ZOOM_SCALE_MEDIUM)))
    r_c3 = 3.5 * (scale / ZOOM_SCALE_MEDIUM)
    r_c5 = 5.5 * (scale / ZOOM_SCALE_MEDIUM)
    r_bg0 = 15.0 * (scale / ZOOM_SCALE_MEDIUM)
    r_bg1 = 25.0 * (scale / ZOOM_SCALE_MEDIUM)
    r_rim0 = 4.0 * (scale / ZOOM_SCALE_MEDIUM)
    r_rim1 = 11.0 * (scale / ZOOM_SCALE_MEDIUM)

    for idx, meta in enumerate(nodes):
        x = int(round(float(raw_x[idx]) + tx))
        y = int(round(float(raw_y[idx]) + ty))
        if not _is_safe_canvas_xy(x, y):
            continue
        x0, x1 = max(0, x - r_outer), min(MODEL_W, x + r_outer + 1)
        y0, y1 = max(0, y - r_outer), min(MODEL_H, y + r_outer + 1)
        patch = img[y0:y1, x0:x1].astype(np.float32)
        yy, xx = np.mgrid[y0:y1, x0:x1]
        dist = np.sqrt((xx - x) ** 2 + (yy - y) ** 2)
        lum = 0.114 * patch[:, :, 0] + 0.587 * patch[:, :, 1] + 0.299 * patch[:, :, 2]

        c3 = lum[dist <= r_c3]
        c5 = lum[dist <= r_c5]
        bg = lum[(dist > r_bg0) & (dist <= r_bg1)]
        rim_px = patch[(dist > r_rim0) & (dist <= r_rim1)]
        if len(c3) == 0 or len(c5) == 0 or len(bg) == 0:
            continue

        c3_mean = float(c3.mean())
        c3_med = float(np.median(c3))
        c5_mean = float(c5.mean())
        c5_p85 = float(np.percentile(c5, 85))
        bg_med = float(np.median(bg))
        d_c5 = c5_mean - bg_med
        d_p85 = c5_p85 - bg_med
        ring_rb = float((rim_px[:, 2] - rim_px[:, 0]).mean()) if len(rim_px) > 0 else 0.0
        r_ell = float(np.sqrt(((x - 640.0) / 520.0) ** 2 + ((y - 360.0) / 275.0) ** 2))

        nid = int(meta["id"])
        max_lv = int(meta.get("max_level", 1))
        state: str | None = None
        level: int | None = None
        score = 0.0

        if max_lv > 1:
            # Wishing 5 multi-level styling nodes: maxed (Lv.10) is cool blue-white filled;
            # 0..9 has warm golden rim (r_minus_b > -5) or dark locked center -> defer to Detail OCR.
            if ring_rb < -12.0 and d_p85 >= 55.0 and c3_med >= 100.0:
                state = "unlocked"
                level = max_lv
                score = min(1.0, d_p85 / 120.0)
            else:
                state = "needs_level_ocr"
                level = None
                score = round(ring_rb, 2)
        elif version_id == 2:
            # LOE: filled golden-white core (>=182) vs dark locked/unlockable core (<=105)
            if c3_mean >= 150.0 and c3_med >= 130.0:
                state = "unlocked"
                level = 1
                score = min(1.0, (c3_mean - 135.0) / 70.0)
            elif c3_mean <= 130.0 or c3_med <= 115.0:
                state = "locked"
                level = 0
                score = -min(1.0, (140.0 - min(c3_mean, c3_med)) / 70.0)
        else:
            # Wishing & StarSea: bright cool-glow core above local bg vs dark core below local bg
            golden_rim_not_filled = (version_id == 1) and (ring_rb > -2.0) and (c3_med < 95.0)
            if not golden_rim_not_filled and d_p85 >= 42.0 and d_c5 >= 2.0:
                state = "unlocked"
                level = 1
                score = min(1.0, d_p85 / 110.0)
            elif golden_rim_not_filled or (d_p85 <= 20.0 and d_c5 <= -5.0):
                state = "locked"
                level = 0
                score = -min(1.0, max(0.0, 25.0 - d_p85) / 45.0)

        obs[nid] = {
            "state": state,
            "level": level,
            "score": round(float(score), 3),
            "r_ell": round(r_ell, 3),
            "x": x,
            "y": y,
        }
    return obs


def fuse_shard_observations(
    version_id: int,
    global_decoded: dict | None,
    zoomed_frames: list[dict[int, dict]],
    level_overrides: dict[int, int] | None = None,
) -> dict:
    """Fuse GLOBAL baseline, zoomed multi-frame observations, and Detail-zoom multi-level OCR."""
    catalog, _, _ = load_heart_assets()
    ver_nodes = {int(n["id"]): n for n in catalog["nodes"] if int(n["version_id"]) == version_id}

    # Start from GLOBAL baseline if present
    decoded: dict[int, dict] = {}
    if global_decoded and global_decoded.get("nodes"):
        for k, v in global_decoded["nodes"].items():
            decoded[int(k)] = dict(v)
    for nid in ver_nodes:
        decoded.setdefault(nid, {"state": "ambiguous", "level": 0, "score": 0.0, "px": 0})

    # Pick the most-centered (smallest r_ell) observation for each node across zoomed frames
    best_obs: dict[int, dict] = {}
    for frame_obs in zoomed_frames:
        for nid, entry in frame_obs.items():
            if entry["state"] is None:
                continue
            prev = best_obs.get(nid)
            if prev is None or entry["r_ell"] < prev["r_ell"]:
                best_obs[nid] = entry

    for nid, entry in best_obs.items():
        if nid not in ver_nodes:
            continue
        max_lv = int(ver_nodes[nid].get("max_level", 1))
        st = entry["state"]
        if st in ("unlocked", "locked"):
            decoded[nid] = {
                "state": st,
                "level": max_lv if st == "unlocked" else 0,
                "score": entry["score"],
                "px": int(decoded[nid].get("px", 0)),
                "source": "zoom",
            }
        elif st == "needs_level_ocr" and max_lv > 1:
            # Confirmed not Lv.max by zoom view; will be filled by level_overrides if OCR succeeded
            decoded[nid] = {
                "state": "ambiguous",
                "level": 0,
                "score": entry["score"],
                "px": int(decoded[nid].get("px", 0)),
                "source": "zoom",
            }

    # Apply exact Detail-zoom OCR levels (e.g. 0/10 .. 10/10 for Wishing styling nodes)
    for nid, lv in (level_overrides or {}).items():
        if nid not in ver_nodes:
            continue
        max_lv = int(ver_nodes[nid].get("max_level", 1))
        clamped = max(0, min(max_lv, int(lv)))
        decoded[nid] = {
            "state": "unlocked" if clamped >= max_lv else "locked",
            "level": clamped,
            "score": 1.0,
            "px": int(decoded[nid].get("px", 0)),
            "source": "zoom_ocr",
        }

    _apply_dependency_closure(decoded, ver_nodes)
    return {
        "version_id": version_id,
        "nodes": {str(k): v for k, v in sorted(decoded.items())},
        "stats": _shard_stats(decoded),
    }


def _parse_hud_currencies(ocr_items: list[dict]) -> tuple[int | None, int | None]:
    """Parse top-right HUD numbers: [token_count (奇想星/星之魄), bling_count (噗灵)]."""
    nums: list[tuple[int, int]] = []
    for item in ocr_items:
        text = re.sub(r"[,，\s]", "", str(item.get("text") or ""))
        box = item.get("box") or [0, 0, 0, 0]
        for m in re.finditer(r"\d+", text):
            nums.append((int(box[0]), int(m.group(0))))
    nums.sort(key=lambda pair: pair[0])
    if len(nums) >= 2:
        return nums[0][1], nums[-1][1]
    if len(nums) == 1:
        return (nums[0][1], None) if nums[0][0] < 1140 else (None, nums[0][1])
    return None, None


def parse_level_fraction(texts: list[str], max_level: int = 10) -> int | None:
    """Parse a multi-level caption such as '3/10', '0/10', or '10/10'."""
    joined = " ".join(str(t or "") for t in texts).strip()
    if not joined:
        return None
    # Primary: explicit slash/pipe separator
    m = re.search(rf"(\d{{1,2}})\s*[/／|\\lI]\s*{max_level}\b", joined)
    if m:
        val = int(m.group(1))
        if 0 <= val <= max_level:
            return val
    # Fallback: OCR dropped slash ('310' -> '3' + '10', '010' -> '0' + '10')
    compact = re.sub(r"\s+", "", joined)
    m2 = re.fullmatch(rf"(\d{{1,2}}){max_level}", compact)
    if m2:
        val = int(m2.group(1))
        if 0 <= val <= max_level:
            return val
    return None


class InfiniteHeartScanner:
    """Navigates to 无限之心, captures GLOBAL + zoomed multi-frame views on all 3 shards, and persists unlocks."""

    def __init__(self, runtime, log_dir: Path | None = None):
        self.rt = runtime
        self.log_dir = log_dir
        self.summary: dict = {}

    def _ocr_box(self, image, roi: list[int]) -> list[dict]:
        res = self.rt.context.run_recognition(
            "MaaNikki_InfiniteHeart_Ocr",
            image,
            pipeline_override={
                "MaaNikki_InfiniteHeart_Ocr": {
                    "recognition": "OCR",
                    "roi": roi,
                    "only_rec": False,
                    "action": "DoNothing",
                }
            },
        )
        if not res or not res.hit:
            return []
        raw = res.all_results or ([res.best_result] if res.best_result else [])
        out = []
        for entry in raw:
            text = getattr(entry, "text", None)
            box = getattr(entry, "box", None)
            if text:
                out.append({"text": str(text), "box": list(box) if box else [0, 0, 0, 0]})
        return out

    def _detect_shard_id(self, image) -> int | None:
        texts = self.rt.text(SHARD_NAME_ROI, image=image)
        if "心愿" in texts or "原野" in texts:
            return 1
        if "伊赞" in texts:
            return 2
        if "星海" in texts:
            return 3
        return None

    def _is_on_heart_page(self, image) -> bool:
        if self.rt.hit("MaaNikki_InfiniteHeartReady", image=image):
            return True
        texts = self.rt.text(HEADER_TITLE_ROI, image=image)
        return "无限之心" in texts

    def _ensure_min_zoom(self) -> bool:
        """Zoom all the way out to GLOBAL (minimum zoom) where camera translation is locked to (0, 0)."""
        if self.rt.stopped:
            raise TaskStopped()
        for _ in range(5):
            if self.rt.stopped:
                raise TaskStopped()
            if not self.rt.action("Click", target=ZOOM_OUT_BTN) or not self.rt.pause(0.16):
                return False
        if not self.rt.action("Click", target=ZOOM_MIN_TRACK):
            return False
        return bool(self.rt.pause(0.75))

    def _zoom_in_once(self) -> bool:
        """Step up one zoom level (`+` button) and wait for the 0.6s scale animation to settle."""
        if self.rt.stopped:
            raise TaskStopped()
        if not self.rt.action("Click", target=ZOOM_IN_BTN):
            return False
        return bool(self.rt.pause(0.75))

    def _drag_canvas(self, dx: int, dy: int, duration: int = 420) -> bool:
        """Drag the zoomed map canvas by (dx, dy) pixels inside the safe central area and wait for inertia."""
        if self.rt.stopped:
            raise TaskStopped()
        # Choose start/end symmetrically around center (640, 360) within safe non-HUD bounds [240..1040, 140..580]
        dx = max(-760, min(760, int(dx)))
        dy = max(-420, min(420, int(dy)))
        x0 = 640 - dx // 2
        y0 = 360 - dy // 2
        x1 = x0 + dx
        y1 = y0 + dy
        if not self.rt.action("Swipe", begin=[x0, y0], end=[x1, y1], duration=duration):
            return False
        return bool(self.rt.pause(0.45))

    def _capture_zoomed_stop(
        self,
        vid: int,
        scale: float,
        zoomed_obs: list[dict[int, dict]],
        tag: str,
    ) -> tuple[np.ndarray | None, int, int]:
        frame = self.rt.capture()
        if frame is None:
            return None, 0, 0
        tx, ty, score, cnt = align_zoomed_frame(frame, vid, scale=scale)
        if cnt >= 4 and score >= 80.0:
            obs = sample_zoomed_nodes(frame, vid, scale=scale, tx=tx, ty=ty)
            zoomed_obs.append(obs)
            if self.log_dir:
                self._save_zoom_debug(frame, vid, scale, tx, ty, obs, tag)
        return frame, tx, ty

    def _save_zoom_debug(
        self,
        frame: np.ndarray,
        vid: int,
        scale: float,
        tx: int,
        ty: int,
        obs: dict[int, dict],
        tag: str,
    ) -> None:
        if not self.log_dir:
            return
        self.log_dir.mkdir(parents=True, exist_ok=True)
        vis = _to_720_bgr(frame).copy()
        for entry in obs.values():
            st = entry["state"]
            color = (80, 220, 80) if st == "unlocked" else (80, 80, 235) if st == "locked" else (40, 210, 255)
            cv2.circle(vis, (entry["x"], entry["y"]), max(8, int(round(5 * scale))), color, 1)
        cv2.imwrite(str(self.log_dir / f"shard_{vid}_{tag}.png"), vis)

    def _scan_wishing_styling_levels(self, zoomed_obs: list[dict[int, dict]]) -> dict[int, int]:
        """Zoom from MEDIUM (2.385) to DETAIL (4.667) near top-left and OCR exact `lv/10` for the 5 styling nodes."""
        if not self._zoom_in_once():
            return {}
        _, raw_x, raw_y = _node_scaled_coords(1, ZOOM_SCALE_DETAIL)
        catalog, _, _ = load_heart_assets()
        w_nodes = [n for n in catalog["nodes"] if int(n["version_id"]) == 1 and n.get("screen_pos")]
        idx_by_id = {int(n["id"]): i for i, n in enumerate(w_nodes)}
        style_raw = {nid: (float(raw_x[idx_by_id[nid]]), float(raw_y[idx_by_id[nid]])) for nid in STYLING_MULTI_LEVEL_IDS if nid in idx_by_id}

        frame = self.rt.capture()
        if frame is None:
            return {}
        tx, ty, score, cnt = align_zoomed_frame(frame, 1, scale=ZOOM_SCALE_DETAIL)

        # Pan up to 2 times if the 5 styling nodes are not all cleanly inside the OCR viewport
        for _ in range(2):
            pts = [(rx + tx, ry + ty) for rx, ry in style_raw.values()]
            if all(75 <= px <= 1205 and 90 <= py <= 585 for px, py in pts):
                break
            cx = sum(p[0] for p in pts) / len(pts)
            cy = sum(p[1] for p in pts) / len(pts)
            move_x = max(-420, min(420, int(round(420.0 - cx))))
            move_y = max(-360, min(360, int(round(310.0 - cy))))
            if abs(move_x) < 25 and abs(move_y) < 25:
                break
            if not self._drag_canvas(move_x, move_y):
                return {}
            frame = self.rt.capture()
            if frame is None:
                return {}
            tx, ty, score, cnt = align_zoomed_frame(frame, 1, scale=ZOOM_SCALE_DETAIL)

        if cnt < 3 or score < 60.0:
            return {}

        levels: dict[int, int] = {}
        img720 = _to_720_bgr(frame)
        for nid, (rx, ry) in style_raw.items():
            x = int(round(rx + tx))
            y = int(round(ry + ty))
            if not (55 <= x <= 1225 and 75 <= y <= 620):
                continue
            roi = [max(0, x - 42), max(0, y + 12), 84, 44]
            items = self._ocr_box(img720, roi)
            lv = parse_level_fraction([it["text"] for it in items], max_level=10)
            if lv is not None:
                levels[nid] = lv
        if self.log_dir:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            vis = img720.copy()
            for nid, (rx, ry) in style_raw.items():
                x, y = int(round(rx + tx)), int(round(ry + ty))
                cv2.rectangle(vis, (x - 42, y + 12), (x + 42, y + 56), (40, 210, 255), 1)
                if nid in levels:
                    cv2.putText(vis, f"{levels[nid]}/10", (x - 24, y - 16), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (80, 240, 80), 2)
            cv2.imwrite(str(self.log_dir / "shard_1_styling_ocr.png"), vis)
        return levels

    def _scan_shard_zoomed(self, vid: int, global_decoded: dict) -> dict:
        """Sweep zoomed stops at MEDIUM (2.385x) + optional DETAIL (4.667x) and fuse with GLOBAL baseline."""
        if not self._zoom_in_once():
            return global_decoded

        zoomed_obs: list[dict[int, dict]] = []
        styling_levels: dict[int, int] = {}

        if vid == 1:
            # Wishing: Center (0 drags, covers all 24 group_all nodes) -> Bottom clamp -> Top clamp -> optional Styling OCR
            self._capture_zoomed_stop(vid, ZOOM_SCALE_MEDIUM, zoomed_obs, "z1_center")
            for _ in range(2):
                self._drag_canvas(0, -400)
            self._capture_zoomed_stop(vid, ZOOM_SCALE_MEDIUM, zoomed_obs, "z1_bottom")
            for _ in range(3):
                self._drag_canvas(0, 400)
            self._capture_zoomed_stop(vid, ZOOM_SCALE_MEDIUM, zoomed_obs, "z1_top")

            # Check if any of the 5 multi-level styling nodes is not maxed (Lv.10)
            need_ocr = any(
                not any(
                    obs.get(nid, {}).get("state") == "unlocked" and obs.get(nid, {}).get("level") == 10
                    for obs in zoomed_obs
                )
                for nid in STYLING_MULTI_LEVEL_IDS
            )
            if need_ocr:
                styling_levels = self._scan_wishing_styling_levels(zoomed_obs)

        elif vid == 2:
            # LOE: Center -> Top clamp -> Bottom clamp -> Left-Center -> Right-Center (100% of 222 nodes in central zone)
            self._capture_zoomed_stop(vid, ZOOM_SCALE_MEDIUM, zoomed_obs, "z1_center")
            for _ in range(2):
                self._drag_canvas(0, 400)
            self._capture_zoomed_stop(vid, ZOOM_SCALE_MEDIUM, zoomed_obs, "z1_top")
            for _ in range(3):
                self._drag_canvas(0, -400)
            self._capture_zoomed_stop(vid, ZOOM_SCALE_MEDIUM, zoomed_obs, "z1_bottom")
            self._drag_canvas(0, 400)
            self._drag_canvas(360, 0)
            self._capture_zoomed_stop(vid, ZOOM_SCALE_MEDIUM, zoomed_obs, "z1_left")
            for _ in range(2):
                self._drag_canvas(-360, 0)
            self._capture_zoomed_stop(vid, ZOOM_SCALE_MEDIUM, zoomed_obs, "z1_right")

        elif vid == 3:
            # StarSea: Center -> Top-Right -> Bottom-Right -> Bottom-Left -> Top-Left (100% of 42 nodes)
            self._capture_zoomed_stop(vid, ZOOM_SCALE_MEDIUM, zoomed_obs, "z1_center")
            self._drag_canvas(-360, 360)
            self._capture_zoomed_stop(vid, ZOOM_SCALE_MEDIUM, zoomed_obs, "z1_tr")
            for _ in range(2):
                self._drag_canvas(0, -400)
            self._capture_zoomed_stop(vid, ZOOM_SCALE_MEDIUM, zoomed_obs, "z1_br")
            for _ in range(2):
                self._drag_canvas(360, 0)
            self._capture_zoomed_stop(vid, ZOOM_SCALE_MEDIUM, zoomed_obs, "z1_bl")
            for _ in range(2):
                self._drag_canvas(0, 400)
            self._capture_zoomed_stop(vid, ZOOM_SCALE_MEDIUM, zoomed_obs, "z1_tl")

        return fuse_shard_observations(vid, global_decoded, zoomed_obs, styling_levels)

    def _is_in_home_hud(self, image) -> bool:
        if image is None:
            return False
        return bool(self.rt.text(HUD_HOME_MANAGE_ROI, r"家园经营", image=image))

    def _is_on_home_manage_page(self, image) -> bool:
        if image is None:
            return False
        if self.rt.hit("MaaNikki_HomeShop_ManageReady", image=image):
            return True
        return "家园经营" in (self.rt.text(HEADER_TITLE_ROI, image=image) or "")

    def _leave_home_to_miraland(self, *, check_map_first: bool = False) -> bool:
        from navigation.models import NavigationError, maps
        from navigation.teleport import Teleporter
        from navigation.vision import Locator

        try:
            with self.rt.input_guard() as inputs:
                teleporter = Teleporter(
                    self.rt,
                    Locator(maps(self.rt.ui.resource)["miraland"]),
                    inputs=inputs,
                )
                reuse_map = False
                if check_map_first:
                    if not teleporter.player_in_home():
                        self.rt.ui.main()
                        return False
                    reuse_map = True
                self.rt.log("无限之心读取：检测到当前位于家园，正在先传送到花愿镇再打开无限之心。")
                teleporter.transport(WORLD_EXIT_TELEPORT, reuse_map=reuse_map)
        except (NavigationError, ValueError, KeyError, OSError, RuntimeError, TypeError, cv2.error) as error:
            self.rt.log(f"无限之心读取：从家园传送到花愿镇失败（{error}）。")
            return False
        return bool(self.rt.ui.main())

    def _enter_infinite_heart(self) -> bool:
        frame = self.rt.capture()
        if frame is not None and self._is_on_heart_page(frame):
            return True
        if not self.rt.ui.main():
            return False

        left_home = False
        frame = self.rt.capture()
        if self._is_in_home_hud(frame):
            if not self._leave_home_to_miraland():
                return False
            left_home = True

        def heart_or_home_manage():
            with self.rt.observe():
                return bool(
                    self.rt.hit("MaaNikki_InfiniteHeartReady")
                    or self.rt.hit("MaaNikki_HomeShop_ManageReady")
                )

        for _ in range(2):
            if self.rt.stopped:
                return False
            if self.rt.ui.enter_page(
                lambda: self.rt.key("infinite_heart", 0),
                heart_or_home_manage,
                source="MaaNikki_MainDetected",
                recover=self.rt.ui.main,
            ):
                self.rt.pause(0.4)
                frame = self.rt.capture()
                if frame is not None and self._is_on_home_manage_page(frame):
                    if left_home:
                        return False
                    if not self.rt.ui.main() or not self._leave_home_to_miraland():
                        return False
                    left_home = True
                    continue
                # Let the opener zoom/HUD fade animation settle before reading tabs and currencies.
                self.rt.pause(0.6)
                frame = self.rt.capture()
                if frame is not None and self._is_on_heart_page(frame):
                    return True
                if frame is not None and self._is_on_home_manage_page(frame) and not left_home:
                    if not self.rt.ui.main() or not self._leave_home_to_miraland():
                        return False
                    left_home = True
                    continue
                return False
            if not left_home and not self.rt.stopped:
                frame = self.rt.capture()
                if self._is_on_home_manage_page(frame) or self._is_in_home_hud(frame):
                    if not self.rt.ui.main() or not self._leave_home_to_miraland():
                        return False
                    left_home = True
                    continue
                # Fallback when HUD was hidden or hotkey opened another Home-only UI: verify via map.
                if self.rt.ui.main() and self._leave_home_to_miraland(check_map_first=True):
                    left_home = True
                    continue
            break

        key_label = self.rt.game_keys.label("infinite_heart")
        self.rt.log(
            f"未能通过快捷键「{key_label}」进入无限之心，请确认游戏停留在可移动的主界面，且「游戏键位 → 系统功能」中的无限之心按键与游戏内一致。"
        )
        return False

    def run(self) -> bool:
        if self.rt.stopped:
            raise TaskStopped()
        account = AccountState.for_runtime(ROOT, self.rt)
        if not self._enter_infinite_heart():
            raise RuntimeError("未能进入「无限之心」界面，请确认游戏已停留在主界面或无限之心页面。")

        shards_result: dict[str, dict] = {}
        currencies: dict[str, int] = {}
        latest_bling: int | None = None

        for vid, shard_name, tab_target in SHARD_TABS:
            if self.rt.stopped:
                raise TaskStopped()
            frame = self.rt.capture()
            cur = self._detect_shard_id(frame) if frame is not None else None
            if cur != vid:
                if not self.rt.action("Click", target=tab_target) or not self.rt.pause(0.6):
                    raise TaskStopped()
                for _ in range(6):
                    if self.rt.stopped:
                        raise TaskStopped()
                    frame = self.rt.capture()
                    cur = self._detect_shard_id(frame) if frame is not None else None
                    if cur == vid:
                        break
                    self.rt.pause(0.3)
                if cur != vid:
                    self.rt.log(f"未检测到「{shard_name}」（可能尚未解锁该区域碎片），已跳过。")
                    continue

            self.rt.log(f"正在读取「{shard_name}」解锁进度（全景 + 放大复核）...")
            if not self._ensure_min_zoom():
                raise TaskStopped()
            frame = self.rt.capture()
            if frame is None:
                raise RuntimeError(f"未能截取「{shard_name}」画面。")
            tok_cnt, bling_cnt = _parse_hud_currencies(self._ocr_box(frame, HUD_CURRENCY_ROI))
            if tok_cnt is not None:
                currencies[str(vid)] = tok_cnt
            if bling_cnt is not None:
                latest_bling = bling_cnt

            dbg = (self.log_dir / f"shard_{vid}_global.png") if self.log_dir else None
            global_decoded = decode_shard_image(frame, vid, debug_out=dbg)
            decoded = self._scan_shard_zoomed(vid, global_decoded)
            shards_result[str(vid)] = decoded
            st = decoded["stats"]
            self.rt.log(
                f"「{shard_name}」识别完成：已解锁 {st['unlocked']} / {st['total']}"
                + (f"，待确认 {st['ambiguous']}" if st["ambiguous"] else "")
                + (f"，当前持有 {tok_cnt}" if tok_cnt is not None else "")
                + "。"
            )

        if not shards_result:
            raise RuntimeError("未能读取任何无限之心碎片。")

        now = datetime.now(timezone.utc).isoformat()
        merged_nodes: dict[str, dict] = {}
        for sdata in shards_result.values():
            merged_nodes.update(sdata["nodes"])

        scan_payload = {
            "schema_version": 1,
            "updated_at": now,
            "complete": len(shards_result) == len(SHARD_TABS),
            "scanned_shards": sorted(int(k) for k in shards_result),
            "currencies": currencies,
            "bling": latest_bling,
            "nodes": merged_nodes,
        }

        with account_lock(ROOT):
            data = account.load()
            heart_section = data.setdefault("infinite_heart", {})
            heart_section["last_scan"] = scan_payload
            heart_section.setdefault("overrides", {})
            data["updated_at"] = now
            write_json(account.path, data)

        self.summary = {
            "shards": len(shards_result),
            "unlocked": sum(s["stats"]["unlocked"] for s in shards_result.values()),
            "total": sum(s["stats"]["total"] for s in shards_result.values()),
            "ambiguous": sum(s["stats"]["ambiguous"] for s in shards_result.values()),
        }
        return True
