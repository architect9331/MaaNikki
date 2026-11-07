"""Overlay-only map candidates, confirmed against separate terrain patches."""
from __future__ import annotations

import math
import time
from itertools import combinations

import cv2
import numpy as np

from daily.settings import ROOT
from navigation.frames import minimap_sample
from navigation.matching import luma, match_map
from navigation.models import NavigationError


SIFT_CACHE_DIR = ROOT / "cache/map-mask"


def _fit_scale_residual(tiles, scale):
    if len(tiles) < 3:
        return None
    pivots = np.float64([(t["roi"][0] + t["roi"][2] / 2,
                          t["roi"][1] + t["roi"][3] / 2) for t in tiles])
    centers = np.float64([(t["x"], t["y"]) for t in tiles])
    best_group = ()
    best_correction = 0.0
    best_proposal = None
    tol = 2.5 * max(1.0, scale)
    for indices in combinations(range(len(tiles)), min(len(tiles), 3)):
        sub_p = pivots[list(indices)]
        sub_c = centers[list(indices)]
        pos = sub_p - sub_p.mean(axis=0)
        denom = float(np.sum(pos ** 2))
        if denom < 1.0:
            continue
        corr = float(np.sum(pos * (sub_c - sub_c.mean(axis=0))) / denom)
        if not (0.0002 < abs(corr) <= scale * 0.14):
            continue
        adjusted = centers - (pivots - (640.0, 360.0)) * corr
        proposal = np.median(adjusted[list(indices)], axis=0)
        inliers = [i for i in range(len(tiles))
                   if math.hypot(adjusted[i, 0] - proposal[0], adjusted[i, 1] - proposal[1]) <= tol]
        if len(inliers) > len(best_group):
            in_p = pivots[inliers]
            in_c = centers[inliers]
            in_pos = in_p - in_p.mean(axis=0)
            in_corr = float(np.sum(in_pos * (in_c - in_c.mean(axis=0))) / max(1.0, np.sum(in_pos ** 2)))
            if 0.0002 < abs(in_corr) <= scale * 0.14:
                best_group = tuple(inliers)
                best_correction = in_corr
                best_proposal = tuple(in_c.mean(axis=0) - (in_p.mean(axis=0) - (640.0, 360.0)) * in_corr)
    if len(best_group) >= 3 and best_proposal is not None:
        return scale + best_correction, best_proposal
    return None


class BigMapMatcher:
    """Track calibrated scale across the full zoom range and reacquire on zoom jumps."""

    def __init__(self, locator, initial_scale=None):
        self.locator = locator
        self.scale = initial_scale
        self.next_acquire = 0.0
        self.detector = cv2.SIFT_create(nfeatures=28000, contrastThreshold=0.014, edgeThreshold=15)
        self.ref_step = 2 if max(locator.spec.width, locator.spec.height) <= 4096 else 4
        self.layers = None
        self._index_sec = 0.0

    def consume_index_time(self):
        sec, self._index_sec = self._index_sec, 0.0
        return sec

    def locate(self, frame, hint=None, *, maximum=False):
        diagnostic = {}
        if self.scale is not None and hint is not None:
            gray = luma(frame)
            center, diagnostic = _terrain_pose(self.locator, gray, hint, scale=self.scale)
            diagnostic["search"] = "terrain_local"
            if center is not None:
                self.scale = diagnostic.get("scale", self.scale)
                return center, diagnostic
            center, diagnostic = _terrain_pose(self.locator, gray, hint, scale=self.scale, radius=160)
            diagnostic["search"] = "terrain_recovery"
            if center is not None:
                self.scale = diagnostic.get("scale", self.scale)
                return center, diagnostic
        now = time.monotonic()
        if self.scale is None and now < self.next_acquire:
            diagnostic.update(search="zoom_wait", accepted=False)
            return None, diagnostic
        center, candidate = self._acquire(frame, hint=hint)
        if center is not None:
            self.scale = candidate["scale"]
            self.next_acquire = 0.0
        else:
            self.next_acquire = now + 0.25
        return center, {**candidate, "previous_scale": self.scale}

    def _ensure_index(self):
        if self.layers is not None:
            return bool(self.layers)
        t0 = time.monotonic()
        step = self.ref_step
        cache_path = SIFT_CACHE_DIR / f"sift_{self.locator.spec.id}_s{step}_v2.npz"
        raw_layers = []
        try:
            if cache_path.is_file() and cache_path.stat().st_mtime >= self.locator.spec.image.stat().st_mtime:
                data = np.load(cache_path)
                for lname in ("detail", "coarse"):
                    if f"{lname}_pts" in data and f"{lname}_desc" in data:
                        raw_layers.append((lname, data[f"{lname}_pts"], data[f"{lname}_desc"]))
        except Exception:
            raw_layers = []
        if not raw_layers:
            ys, xs = np.nonzero(self.locator.map)
            x0, y0 = int(xs.min() // step), int(ys.min() // step)
            x1 = min(self.locator.map.shape[1] // step, int(xs.max() // step) + 2)
            y1 = min(self.locator.map.shape[0] // step, int(ys.max() // step) + 2)
            detail_crop = self.locator.map[y0 * step:y1 * step, x0 * step:x1 * step]
            detail_ref = cv2.resize(detail_crop, (x1 - x0, y1 - y0), interpolation=cv2.INTER_AREA)
            sources = [("detail", detail_ref, x0, y0)]
            if step == 4 and self.locator.coarse is not None:
                sources.append(("coarse", self.locator.coarse[y0:y1, x0:x1], x0, y0))
            save_dict = {}
            for lname, ref, ox, oy in sources:
                known = cv2.erode((ref > 0).astype(np.uint8) * 255, np.ones((9, 9), np.uint8))
                kp, desc = self.detector.detectAndCompute(ref, known)
                if desc is not None and len(kp) >= 12:
                    pts = np.float32([(k.pt[0] + ox, k.pt[1] + oy) for k in kp])
                    raw_layers.append((lname, pts, desc))
                    save_dict[f"{lname}_pts"] = pts
                    save_dict[f"{lname}_desc"] = desc
            if save_dict:
                try:
                    SIFT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    np.savez(cache_path, **save_dict)
                except Exception:
                    pass
        built = []
        for lname, pts, desc in raw_layers:
            idx = cv2.FlannBasedMatcher(dict(algorithm=1, trees=4), dict(checks=64))
            idx.add([desc])
            idx.train()
            built.append((lname, pts, idx))
        self.layers = built
        self._index_sec = max(0.0, time.monotonic() - t0)
        return bool(self.layers)

    def _acquire(self, frame, hint=None):
        if not self._ensure_index():
            return None, {"search": "zoom_features", "accepted": False, "reason": "reference_features"}
        gray = luma(frame)
        baseline = self.locator.spec.bigmap_scale
        best_diag = {"search": "zoom_features", "accepted": False}
        ordered_layers = (list(reversed(self.layers))
                          if (self.scale is not None and self.scale >= 1.25) else self.layers)
        for fx in (0.25, 0.5):
            sample = cv2.resize(gray[80:660, 160:1120], None, fx=fx, fy=fx,
                                interpolation=cv2.INTER_AREA)
            keypoints, descriptors = self.detector.detectAndCompute(sample, None)
            if descriptors is None or len(keypoints) < 8:
                continue
            q_pts = np.float32([(k.pt[0] / fx + 160, k.pt[1] / fx + 80) for k in keypoints])
            for lname, r_pts, idx in ordered_layers:
                neighbors = idx.knnMatch(descriptors, k=2)
                pairs = [pair[0] for pair in neighbors if len(pair) == 2
                         and pair[0].distance < 0.75 * pair[1].distance]
                if len(pairs) > best_diag.get("feature_matches", 0):
                    best_diag["feature_matches"] = len(pairs)
                if len(pairs) < 8:
                    continue
                source = q_pts[[p.queryIdx for p in pairs]]
                target = r_pts[[p.trainIdx for p in pairs]]
                matrix, inliers = cv2.estimateAffinePartial2D(
                    source, target, method=cv2.RANSAC,
                    ransacReprojThreshold=3.2, maxIters=2000, confidence=0.995)
                if matrix is None or inliers is None:
                    continue
                selected = inliers.ravel().astype(bool)
                count = int(selected.sum())
                scale = float(math.hypot(matrix[0, 0], matrix[1, 0]) * self.ref_step)
                angle = math.degrees(math.atan2(matrix[1, 0], matrix[0, 0]))
                diagnostic = {"search": f"zoom_{lname}", "accepted": False,
                              "feature_matches": len(pairs), "feature_inliers": count,
                              "scale": scale, "rotation": round(angle, 3)}
                best_diag = diagnostic
                if (count < 8 or count < len(pairs) * 0.32 or abs(angle) > 1.5
                        or not baseline * 0.80 <= scale <= baseline * 12.0):
                    continue
                span = np.ptp(source[selected], axis=0)
                if span[0] < 140 or span[1] < 100:
                    continue
                center = tuple((matrix @ np.array([640, 360, 1])) * self.ref_step)
                for mult in (1.0, 0.97, 1.03):
                    verified, terrain = _terrain_pose(self.locator, gray, center,
                                                      scale=scale * mult, radius=40)
                    if verified is not None:
                        final_scale = terrain.get("scale", scale * mult)
                        diagnostic.update(terrain)
                        diagnostic["scale"] = final_scale
                        return verified, diagnostic
        if hint is not None:
            center, hint_diag = self._acquire_around_hint(gray, hint)
            if center is not None:
                return center, hint_diag
        return None, best_diag

    def _acquire_around_hint(self, gray, hint):
        baseline = self.locator.spec.bigmap_scale
        crop = gray[140:580, 260:1020]
        hx, hy = hint
        r_map = 700
        mx0, my0 = max(0, int(hx - r_map)), max(0, int(hy - r_map))
        mx1 = min(self.locator.map.shape[1], int(hx + r_map))
        my1 = min(self.locator.map.shape[0], int(hy + r_map))
        local_detail = cv2.resize(self.locator.map[my0:my1, mx0:mx1], None,
                                  fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA)
        bx, by = hx * 0.25, hy * 0.25
        source = self.locator.coarse
        best = None
        for i in range(24):
            scale = baseline * (1.095 ** i)
            patch = cv2.resize(crop, None, fx=scale * 0.25, fy=scale * 0.25,
                               interpolation=cv2.INTER_AREA)
            if patch.shape[0] < 24 or patch.shape[1] < 32:
                continue
            if (scale <= 1.5 and patch.shape[0] + 10 < local_detail.shape[0]
                    and patch.shape[1] + 10 < local_detail.shape[1]):
                try:
                    mx, my, score, peak = match_map(local_detail, patch,
                                                    kernel=7, minimum_score=0.42)
                    if all(math.isfinite(v) for v in (mx, my, score, peak)) and score >= 0.45:
                        if best is None or score > best[0]:
                            best = (score, scale, (mx0 + mx * 4.0, my0 + my * 4.0))
                except (NavigationError, cv2.error):
                    pass
            if scale >= 1.1:
                pad_x = max(120, patch.shape[1] // 2 + 64)
                pad_y = max(100, patch.shape[0] // 2 + 64)
                x0, y0 = max(0, round(bx - pad_x)), max(0, round(by - pad_y))
                x1 = min(source.shape[1], round(bx + pad_x))
                y1 = min(source.shape[0], round(by + pad_y))
                if y1 - y0 > patch.shape[0] + 8 and x1 - x0 > patch.shape[1] + 8:
                    try:
                        mx, my, score, peak = match_map(source[y0:y1, x0:x1], patch,
                                                        kernel=7, minimum_score=0.45)
                        if all(math.isfinite(v) for v in (mx, my, score, peak)) and score >= 0.48:
                            if best is None or score > best[0]:
                                best = (score, scale, ((mx + x0) * 4.0, (my + y0) * 4.0))
                    except (NavigationError, cv2.error):
                        pass
        if best is not None:
            _, scale, candidate = best
            for mult in (1.0, 0.96, 1.04):
                center, terrain = _terrain_pose(self.locator, gray, candidate,
                                                scale=scale * mult, radius=48)
                if center is not None:
                    terrain["search"] = "zoom_hint_pyramid"
                    return center, terrain
        return None, {"search": "zoom_hint_pyramid", "accepted": False}



def bigmap_pose(locator, frame, hint=None, *, scale=None):
    scale = locator.spec.bigmap_scale if scale is None else scale
    gray = luma(frame)
    tracking = None
    if hint is not None:
        center, diagnostic = _terrain_pose(locator, gray, hint, scale=scale)
        diagnostic["search"] = "terrain_local"
        if center is not None:
            return center, diagnostic
        center, diagnostic = _terrain_pose(locator, gray, hint, scale=scale, radius=160)
        diagnostic["search"] = "terrain_recovery"
        if center is not None:
            return center, diagnostic
        tracking = diagnostic
    patch = cv2.resize(gray, None, fx=scale * 0.25, fy=scale * 0.25,
                       interpolation=cv2.INTER_AREA)
    left = top = 0
    source, mask = locator.coarse, locator.bigmap_mask
    if hint is not None:
        bx, by = (value * 0.25 for value in hint)
        pad_x, pad_y = max(180, patch.shape[1] / 2 + 80), max(140, patch.shape[0] / 2 + 80)
        left, top = max(0, round(bx - pad_x)), max(0, round(by - pad_y))
        source = source[top:round(by + pad_y), left:round(bx + pad_x)]
        mask = (mask[top:round(by + pad_y), left:round(bx + pad_x)]
                if mask is not None and mask.shape == locator.coarse.shape else None)
    search = "local" if hint else "global"
    try:
        x, y, score, peak = match_map(source, patch, peak_mask=mask, kernel=9,
                                      minimum_score=0.42)
    except (NavigationError, cv2.error) as error:
        return None, {"search": search, "scale": scale, "accepted": False, "error": str(error)}
    x, y = (x + left) * 4, (y + top) * 4
    diagnostic = {"x": round(x, 2), "y": round(y, 2), "score": round(score, 3),
                  "peak": round(peak, 3), "search": search, "scale": scale}
    if tracking is not None:
        diagnostic["tracking"] = tracking
    if not all(math.isfinite(v) for v in (x, y, score, peak)) or score < 0.35 or peak < 0.01:
        diagnostic["accepted"] = False
        return None, diagnostic

    center, terrain = _terrain_pose(locator, gray, (x, y), scale=scale)
    if center is not None:
        diagnostic.update(x=round(center[0], 2), y=round(center[1], 2))
    diagnostic.update(terrain)
    return center, diagnostic


def _terrain_pose(locator, gray, hint, *, scale=None, radius=16, allow_scale_fit=True):
    scale = locator.spec.bigmap_scale if scale is None else scale
    x, y = hint
    ref_step = 4.0 if scale >= 2.0 else 1.0
    ref_image = locator.coarse if ref_step == 4.0 else locator.map
    ref_h, ref_w = ref_image.shape[:2]
    eff_scale = scale / ref_step
    eff_radius = max(radius, round(16 * max(1.0, scale)))
    r_ref = max(12, round(eff_radius / ref_step))
    min_score = 0.55 if scale >= 1.4 else 0.60

    candidates = []
    tiles = []
    sigma = 0.5 * math.sqrt(max(0.0, 1.0 / eff_scale ** 2 - 1.0))
    filtered = cv2.GaussianBlur(gray, (0, 0), sigma) if sigma else gray
    for top in (160, 400):
        for left in (260, 500, 740):
            width, height = 240, 200
            sh, sw = max(16, round(height * eff_scale)), max(16, round(width * eff_scale))
            yy, xx = np.indices((sh, sw), dtype=np.float32)
            sample = cv2.remap(filtered, left + (xx + 0.5) / eff_scale - 0.5,
                               top + (yy + 0.5) / eff_scale - 0.5, cv2.INTER_LINEAR)
            anchor = ((640 - left) * eff_scale - 0.5, (360 - top) * eff_scale - 0.5)
            cx, cy = x / ref_step - anchor[0], y / ref_step - anchor[1]
            x0, y0 = max(0, math.floor(cx - r_ref)), max(0, math.floor(cy - r_ref))
            x1 = min(ref_w, math.ceil(cx + sample.shape[1] + r_ref))
            y1 = min(ref_h, math.ceil(cy + sample.shape[0] + r_ref))
            try:
                mx, my, rate, detail = match_map(ref_image[y0:y1, x0:x1], sample,
                                                 minimum_score=min_score, anchor=anchor)
            except (NavigationError, cv2.error):
                continue
            center = (mx + x0) * ref_step, (my + y0) * ref_step
            accepted = (all(math.isfinite(v) for v in (*center, rate, detail))
                        and rate >= min_score and detail >= 0.01
                        and math.hypot(center[0] - x, center[1] - y) <= eff_radius - 3 * ref_step)
            tiles.append({"roi": [left, top, width, height],
                          "x": round(center[0], 2), "y": round(center[1], 2),
                          "score": round(rate, 3),
                          "peak": round(detail, 3), "accepted": accepted})
            if accepted:
                candidates.append(center)
    tol = 2.2 * max(1.0, scale)
    group = next((group for size in range(len(candidates), 1, -1)
                  for group in combinations(candidates, size)
                  if all(math.dist(a, b) <= tol for a, b in combinations(group, 2))), ())
    if allow_scale_fit and len(candidates) >= 3 and len(group) < max(3, len(candidates)):
        fitted = _fit_scale_residual([t for t in tiles if t["accepted"]], scale)
        if fitted is not None:
            refined_scale, proposal = fitted
            fixed, details = _terrain_pose(locator, gray, proposal, scale=refined_scale,
                                           radius=max(16, round(18 * max(1.0, refined_scale))),
                                           allow_scale_fit=False)
            if fixed is not None and details.get("agreed", 0) >= max(len(group), 3):
                return fixed, details
    agreed = bool(group)
    diagnostic = {"tiles": tiles, "agreed": len(group), "accepted": agreed,
                  "radius": eff_radius, "scale": scale}
    if not agreed:
        return None, diagnostic
    center = tuple(float(value) for value in np.median(group, axis=0))
    diagnostic.update(x=round(center[0], 2), y=round(center[1], 2))
    return center, diagnostic



class MinimapMatcher:
    """Fast local and global minimap localization directly against the minimap reference."""

    def __init__(self, locator):
        self.locator = locator
        self.spec = locator.spec
        half = round(self.spec.radius * self.spec.scale)
        self.patch_size = half * 2 + 1
        yy, xx = np.indices((self.patch_size, self.patch_size), dtype=np.float32)
        center = (self.patch_size - 1) / 2.0
        dist = np.hypot(xx - center, yy - center)
        self.mask = ((dist <= self.patch_size / 2.0 - 2.0) & (dist >= 14.0)).astype(np.uint8)
        self.edge_mask = ((dist <= self.patch_size / 2.0 - 3.0) & (dist >= 14.0)).astype(np.uint8)
        self._ready = False
        self._x0 = self._y0 = 0
        self._crop_size = self._crop_offset = 0
        self._roi_centered = self._small_mask = self._s_std = self._usable = None
        self._ring_size = 0.0

    def sample(self, frame):
        return minimap_sample(frame, self.spec)

    def _ensure_coarse(self):
        if self._ready:
            return
        ys, xs = np.nonzero(self.locator.map)
        self._x0 = int(xs.min() // 4) * 4
        self._y0 = int(ys.min() // 4) * 4
        x1 = min(self.locator.map.shape[1], int(xs.max() // 4 + 2) * 4)
        y1 = min(self.locator.map.shape[0], int(ys.max() // 4 + 2) * 4)
        crop = self.locator.map[self._y0:y1, self._x0:x1]
        roi = cv2.resize(crop, (crop.shape[1] // 4, crop.shape[0] // 4),
                         interpolation=cv2.INTER_AREA).astype(np.float32)
        known = (roi > 0).astype(np.float32)
        smooth_num = cv2.boxFilter(roi * known, cv2.CV_32F, (15, 15),
                                   normalize=False, borderType=cv2.BORDER_REPLICATE)
        smooth_den = np.maximum(cv2.boxFilter(known, cv2.CV_32F, (15, 15),
                                              normalize=False, borderType=cv2.BORDER_REPLICATE), 1e-5)
        filled = np.where(known > 0, roi, smooth_num / smooth_den)
        self._roi_centered = filled - 128.0

        self._crop_size = (self.patch_size // 4) * 4
        self._crop_offset = (self.patch_size - self._crop_size) // 2
        tsize = self._crop_size // 4
        yy, xx = np.indices((tsize, tsize), dtype=np.float32)
        tc = (tsize - 1) / 2.0
        tr = np.hypot(xx - tc, yy - tc)
        self._small_mask = ((tr <= tsize / 2.0 - 1.0) & (tr >= 3.2)).astype(np.float32)
        self._ring_size = float(self._small_mask.sum())
        cov = cv2.matchTemplate(known, self._small_mask, cv2.TM_CCORR)
        s_sum = cv2.matchTemplate(self._roi_centered, self._small_mask, cv2.TM_CCORR)
        s_sq = cv2.matchTemplate(self._roi_centered * self._roi_centered,
                                 self._small_mask, cv2.TM_CCORR)
        s_var = np.maximum(s_sq - (s_sum * s_sum) / self._ring_size, 0.0)
        self._s_std = np.sqrt(s_var)
        self._usable = (cov >= self._ring_size * 0.65) & (self._s_std > 4.0 * math.sqrt(self._ring_size))
        self._ready = True

    def locate_local(self, patch, hint, *, radius=80, floor=0.52):
        hx, hy = hint
        half = self.patch_size / 2.0
        x0 = max(0, int(hx - half - radius))
        y0 = max(0, int(hy - half - radius))
        x1 = min(self.locator.map.shape[1], int(hx + half + radius))
        y1 = min(self.locator.map.shape[0], int(hy + half + radius))
        roi = self.locator.map[y0:y1, x0:x1]
        if roi.shape[0] <= patch.shape[0] + 2 or roi.shape[1] <= patch.shape[1] + 2:
            return None
        best = None
        try:
            mx, my, score, peak = match_map(roi, patch, self.mask, minimum_score=0.40)
            if all(math.isfinite(v) for v in (mx, my, score, peak)):
                best = (x0 + mx, y0 + my, float(score), float(peak), "luma")
        except (NavigationError, cv2.error):
            pass
        if best is None or best[2] < max(floor, 0.58) or best[3] < 0.015:
            try:
                ex, ey, escore, epeak = match_map(roi, patch, self.edge_mask,
                                                  minimum_score=0.25, edges=True)
                if all(math.isfinite(v) for v in (ex, ey, escore, epeak)):
                    edge_cand = (x0 + ex, y0 + ey, float(escore), float(epeak), "edge")
                    if best is None or (escore >= 0.32 and epeak >= 0.015 and best[2] < floor):
                        best = edge_cand
            except (NavigationError, cv2.error):
                pass
        if best is None:
            return None
        x, y, score, peak, mode = best
        if mode == "luma" and (score >= floor and peak >= 0.01):
            return x, y, score, peak, mode
        if mode == "edge" and (score >= 0.32 and peak >= 0.015):
            return x, y, score, peak, mode
        return None

    def locate_global(self, patch, *, floor=0.56, min_peak=0.018):
        self._ensure_coarse()
        off = self._crop_offset
        end = off + self._crop_size
        small = cv2.resize(patch[off:end, off:end],
                           (self._crop_size // 4, self._crop_size // 4),
                           interpolation=cv2.INTER_AREA).astype(np.float32)
        t_vals = small[self._small_mask > 0]
        t_std = float(np.std(t_vals))
        if t_std < 3.5:
            return None
        t_centered = (small - float(np.mean(t_vals))) * self._small_mask
        t_norm = float(np.sqrt(np.sum(t_centered * t_centered)))
        if t_norm < 1e-4:
            return None
        ccorr = cv2.matchTemplate(self._roi_centered, t_centered, cv2.TM_CCORR)
        denom = np.maximum(self._s_std * t_norm, 1e-5)
        rates = np.where(self._usable, np.clip(ccorr / denom, -1.0, 1.0), -1.0)
        dilated = cv2.dilate(rates, np.ones((9, 9), np.uint8))
        peaks = (rates == dilated) & (rates >= 0.42)
        py, px = np.nonzero(peaks)
        if len(px) == 0:
            return None
        scores = rates[py, px]
        order = np.argsort(scores)[::-1][:16]
        best = None
        half = self.patch_size / 2.0
        for idx in order:
            gx = self._x0 + int(px[idx]) * 4 + half
            gy = self._y0 + int(py[idx]) * 4 + half
            local = self.locate_local(patch, (gx, gy), radius=16, floor=floor)
            if local is not None and local[4] == "luma" and local[3] >= min_peak:
                if best is None or local[2] > best[2]:
                    best = (*local[:4], "global_luma")
                    if best[2] >= 0.72 and best[3] >= 0.04:
                        break
        return best


def minimap_hint(locator, frame):
    """Find a full-resolution verified global minimap candidate."""
    matcher = getattr(locator, "_minimap_matcher", None)
    if matcher is None:
        matcher = MinimapMatcher(locator)
        locator._minimap_matcher = matcher
    found = matcher.locate_global(matcher.sample(frame))
    return (found[0], found[1]) if found is not None else None
