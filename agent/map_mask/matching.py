"""Overlay-only map candidates, confirmed against separate terrain patches."""
from __future__ import annotations

import math
import time
from itertools import combinations

import cv2
import numpy as np

from navigation.frames import minimap_sample
from navigation.matching import luma, match_map
from navigation.models import NavigationError


class BigMapMatcher:
    """Track one calibrated scale; reacquire scale only after terrain is lost."""

    def __init__(self, locator):
        self.locator = locator
        self.scale = None
        self.next_acquire = 0.0
        self.detector = cv2.SIFT_create(nfeatures=24000)
        self.keypoints = self.index = None

    def locate(self, frame, hint=None, *, maximum=False):
        scale = self.scale or (self.locator.spec.bigmap_scale if maximum else None)
        diagnostic = {}
        if scale is not None:
            center, diagnostic = bigmap_pose(self.locator, frame, hint, scale=scale)
            if center is not None:
                self.scale = scale
                return center, diagnostic
        now = time.monotonic()
        if now < self.next_acquire:
            diagnostic.update(search="zoom_wait", accepted=False)
            return None, diagnostic
        self.next_acquire = now+.5
        center, candidate = self._acquire(frame)
        if center is not None:
            self.scale = candidate["scale"]
        return center, {**candidate, "previous_scale": scale}

    def _acquire(self, frame):
        # Coarse map descriptors and their search index are built once for this
        # map, then reused across zoom changes and reopening the map. Feature
        # matching proposes scale/position; independent terrain still decides.
        if self.index is None:
            coarse = self.locator.coarse
            known = cv2.erode((coarse > 0).astype(np.uint8)*255, np.ones((9, 9), np.uint8))
            keypoints, descriptors = self.detector.detectAndCompute(coarse, known)
            if descriptors is None or len(keypoints) < 12:
                return None, {"search": "zoom_features", "accepted": False,
                              "reason": "reference_features"}
            self.keypoints = keypoints
            self.index = cv2.FlannBasedMatcher(dict(algorithm=1, trees=4), dict(checks=64))
            self.index.add([descriptors])
            self.index.train()
        gray = luma(frame)
        sample = cv2.resize(gray[140:620, 240:1000], None, fx=.5, fy=.5,
                            interpolation=cv2.INTER_AREA)
        keypoints, descriptors = self.detector.detectAndCompute(sample, None)
        diagnostic = {"search": "zoom_features", "accepted": False}
        if descriptors is None or len(keypoints) < 12:
            return None, diagnostic
        neighbors = self.index.knnMatch(descriptors, k=2)
        pairs = [pair[0] for pair in neighbors if len(pair) == 2
                 and pair[0].distance < .7*pair[1].distance]
        diagnostic["feature_matches"] = len(pairs)
        if len(pairs) < 12:
            return None, diagnostic
        source = np.float32([(keypoints[p.queryIdx].pt[0]*2+240,
                              keypoints[p.queryIdx].pt[1]*2+140) for p in pairs])
        target = np.float32([self.keypoints[p.trainIdx].pt for p in pairs])
        matrix, inliers = cv2.estimateAffinePartial2D(source, target, method=cv2.RANSAC,
            ransacReprojThreshold=2, maxIters=2000, confidence=.995)
        if matrix is None or inliers is None:
            return None, diagnostic
        selected = inliers.ravel().astype(bool)
        count = int(selected.sum())
        scale = float(math.hypot(matrix[0, 0], matrix[1, 0])*4)
        angle = math.degrees(math.atan2(matrix[1, 0], matrix[0, 0]))
        diagnostic.update(feature_inliers=count, scale=scale, rotation=round(angle, 3))
        baseline = self.locator.spec.bigmap_scale
        if (count < 10 or count < len(pairs)*.4 or abs(angle) > 1
                or not baseline*.85 <= scale <= baseline*5):
            return None, diagnostic
        span = np.ptp(source[selected], axis=0)
        if span[0] < 200 or span[1] < 140:
            return None, diagnostic
        center = tuple((matrix @ np.array([640, 360, 1]))*4)
        center, terrain = _terrain_pose(self.locator, gray, center, scale=scale, radius=32)
        # A slightly biased feature scale makes widely separated terrain tiles
        # report systematically different centers. Fit that residual against
        # their screen positions, then verify all tiles again at the new scale.
        tiles = [t for t in terrain["tiles"] if t["accepted"]]
        if len(tiles) >= 3:
            pivots = np.float64([(t["roi"][0]+t["roi"][2]/2,
                                  t["roi"][1]+t["roi"][3]/2) for t in tiles])
            centers = np.float64([(t["x"], t["y"]) for t in tiles])
            positions = pivots-pivots.mean(axis=0)
            correction = float(np.sum(positions*(centers-centers.mean(axis=0))) /
                               max(1, np.sum(positions**2)))
            if 0.0002 < abs(correction) <= scale*.03:
                refined = scale+correction
                proposal = tuple(centers.mean(axis=0)-(pivots.mean(axis=0)-(640, 360))*correction)
                fixed, details = _terrain_pose(self.locator, gray, proposal,
                                              scale=refined, radius=16)
                if fixed is not None:
                    center, terrain, scale = fixed, details, refined
        diagnostic.update(terrain)
        diagnostic["scale"] = scale
        return center, diagnostic


def bigmap_pose(locator, frame, hint=None, *, scale=None):
    scale = locator.spec.bigmap_scale if scale is None else scale
    gray = luma(frame)
    tracking = None
    if hint is not None:
        # Once found, track terrain directly. Fixed UI can spoil the coarse
        # full-screen score even while all terrain tiles remain unchanged.
        center, diagnostic = _terrain_pose(locator, gray, hint, scale=scale)
        diagnostic["search"] = "terrain_local"
        if center is not None:
            return center, diagnostic
        # A mouse drag can move the map much farther than the narrow refinement
        # window in one frame. Search terrain before falling back to UI-heavy
        # whole-screen correlation, with the same independent-tile requirements.
        center, diagnostic = _terrain_pose(locator, gray, hint, scale=scale, radius=160)
        diagnostic["search"] = "terrain_recovery"
        if center is not None:
            return center, diagnostic
        tracking = diagnostic
    patch = cv2.resize(gray, None, fx=scale*.25, fy=scale*.25,
                       interpolation=cv2.INTER_AREA)
    left = top = 0
    source, mask = locator.coarse, locator.bigmap_mask
    if hint is not None:
        bx, by = (value*.25 for value in hint)
        pad_x, pad_y = max(180, patch.shape[1]/2+80), max(140, patch.shape[0]/2+80)
        left, top = max(0, round(bx-pad_x)), max(0, round(by-pad_y))
        source = source[top:round(by+pad_y), left:round(bx+pad_x)]
        mask = (mask[top:round(by+pad_y), left:round(bx+pad_x)]
                if mask is not None and mask.shape == locator.coarse.shape else None)
    search = "local" if hint else "global"
    try:
        x, y, score, peak = match_map(source, patch, peak_mask=mask, kernel=9,
                                     minimum_score=.45)
    except (NavigationError, cv2.error) as error:
        return None, {"search": search, "scale": scale, "accepted": False, "error": str(error)}
    x, y = (x+left)*4, (y+top)*4
    diagnostic = {"x": round(x, 2), "y": round(y, 2), "score": round(score, 3),
                  "peak": round(peak, 3), "search": search, "scale": scale}
    if tracking is not None:
        diagnostic["tracking"] = tracking
    if not all(math.isfinite(v) for v in (x, y, score, peak)) or score < .35 or peak < .01:
        diagnostic["accepted"] = False
        return None, diagnostic

    center, terrain = _terrain_pose(locator, gray, (x, y), scale=scale)
    if center is not None:
        diagnostic.update(x=round(center[0], 2), y=round(center[1], 2))
    diagnostic.update(terrain)
    return center, diagnostic


def _terrain_pose(locator, gray, hint, *, scale=None, radius=16):
    scale = locator.spec.bigmap_scale if scale is None else scale
    x, y = hint

    # Full-screen scores include fixed controls, text and player/quest icons.
    # Refine at native map density and require independent terrain agreement.
    candidates = []
    tiles = []
    sigma = .5*math.sqrt(max(0, 1/scale**2-1))
    filtered = cv2.GaussianBlur(gray, (0, 0), sigma) if sigma else gray
    for top in (160, 400):
        for left in (260, 500, 740):
            width, height = 240, 200
            # Exact scale and pixel-center mapping keep different tiles on the
            # same screen pivot despite rounded resized image dimensions.
            yy, xx = np.indices((round(height*scale), round(width*scale)), dtype=np.float32)
            sample = cv2.remap(filtered, left+(xx+.5)/scale-.5, top+(yy+.5)/scale-.5,
                               cv2.INTER_LINEAR)
            anchor = ((640-left)*scale-.5, (360-top)*scale-.5)
            cx, cy = x-anchor[0], y-anchor[1]
            x0, y0 = max(0, math.floor(cx-radius)), max(0, math.floor(cy-radius))
            x1 = min(locator.spec.width, math.ceil(cx+sample.shape[1]+radius))
            y1 = min(locator.spec.height, math.ceil(cy+sample.shape[0]+radius))
            try:
                mx, my, rate, detail = match_map(locator.map[y0:y1, x0:x1], sample,
                                                 minimum_score=.65, anchor=anchor)
            except (NavigationError, cv2.error):
                continue
            center = mx+x0, my+y0
            accepted = (all(math.isfinite(v) for v in (*center, rate, detail))
                        and rate >= .65 and detail >= .01
                        and math.hypot(center[0]-x, center[1]-y) <= radius-4)
            tiles.append({"roi": [left, top, width, height],
                          "x": round(center[0], 2), "y": round(center[1], 2),
                          "score": round(rate, 3),
                          "peak": round(detail, 3), "accepted": accepted})
            if accepted:
                candidates.append(center)
    # Select the largest mutually consistent group; one obscured tile has no vote.
    group = next((group for size in range(len(candidates), 1, -1)
                  for group in combinations(candidates, size)
                  if all(math.dist(a, b) <= 2 for a, b in combinations(group, 2))), ())
    agreed = bool(group)
    diagnostic = {"tiles": tiles, "agreed": len(group), "accepted": agreed,
                  "radius": radius, "scale": scale}
    if not agreed:
        return None, diagnostic
    center = tuple(float(value) for value in np.median(group, axis=0))
    diagnostic.update(x=round(center[0], 2), y=round(center[1], 2))
    return center, diagnostic


def minimap_hint(locator, frame):
    """A global coarse candidate is only a hint for full-resolution confirmation."""
    sample = minimap_sample(frame, locator.spec)
    small = cv2.resize(sample, None, fx=.25, fy=.25, interpolation=cv2.INTER_AREA)
    yy, xx = np.indices(small.shape, dtype=np.float32)
    radius = np.hypot(xx-(small.shape[1]-1)/2, yy-(small.shape[0]-1)/2)
    mask = ((radius <= min(small.shape)/2-1) & (radius >= 3)).astype(np.uint8)
    try:
        x, y, score, peak = match_map(locator.coarse, small, mask,
                                     peak_mask=locator.bigmap_mask, minimum_score=.55)
    except (NavigationError, cv2.error):
        return None
    if all(math.isfinite(v) for v in (x, y, score, peak)) and score >= .55 and peak >= .01:
        return x*4, y*4
    return None
