"""Map-image correlation. Unrecorded terrain is always excluded.

This module is for navigation reference maps, where zero means missing data.
UI templates have different pixel semantics and use agent/vision.py instead.
"""
from __future__ import annotations

import cv2
import numpy as np

from .models import NavigationError


MIN_KNOWN_COVERAGE = .6


def luma(frame):
    return cv2.cvtColor(frame, cv2.COLOR_BGR2YUV)[:, :, 0] if frame.ndim == 3 else frame


def known_pixels(image):
    """Exclude missing tiles and their interpolated/blurred boundary."""
    return cv2.erode((image > 0).astype(np.uint8), np.ones((7, 7), np.uint8)).astype(np.float32)


def coverage(image, shape, mask, center):
    valid = known_pixels(image)
    patch = cv2.getRectSubPix(valid, (shape[1], shape[0]), (center[0]-.5, center[1]-.5))
    selected = np.ones(shape, dtype=bool) if mask is None else mask > 0
    return float(np.mean(patch[selected] >= .99)) if selected.any() else 0.0


def correlation_rates(image, patch, mask=None, *, edges=False):
    """All callers share coverage and invalid-pixel rules, including alignment."""
    if image.shape[0] < patch.shape[0] or image.shape[1] < patch.shape[1]:
        raise NavigationError("地图匹配区域不足。")
    if not edges and not np.any(image == 0):
        rates = cv2.matchTemplate(image, patch, cv2.TM_CCOEFF_NORMED, mask=mask)
        return np.nan_to_num(rates, nan=-1, posinf=-1, neginf=-1)
    valid = known_pixels(image)
    source, target = image.astype(np.float32), patch.astype(np.float32)
    if edges:
        source = cv2.GaussianBlur(source, (0, 0), 1)-cv2.GaussianBlur(source, (0, 0), 2)
        target = cv2.GaussianBlur(target, (0, 0), 1)-cv2.GaussianBlur(target, (0, 0), 2)
    else:
        # Center values before subtracting squared means on pale terrain.
        source, target = source-128, target-128
    source *= valid
    weights = np.ones(patch.shape, np.float32) if mask is None else (mask > 0).astype(np.float32)

    def correlate(a, b):
        return cv2.matchTemplate(a, b, cv2.TM_CCORR)

    count = correlate(valid, weights)
    size = np.maximum(count, 1)
    source_sum, target_sum = correlate(source, weights), correlate(valid, target*weights)
    source_var = np.maximum(correlate(source*source, weights)-source_sum**2/size, 0)
    target_var = np.maximum(correlate(valid, target*target*weights)-target_sum**2/size, 0)
    covariance = correlate(source, target*weights)-source_sum*target_sum/size
    denominator = np.sqrt(source_var*target_var)
    rates = covariance/np.maximum(denominator, 1)
    usable = (count >= weights.sum()*MIN_KNOWN_COVERAGE) & (denominator > 1)
    return np.nan_to_num(np.where(usable, np.clip(rates, -1, 1), -1),
                         nan=-1, posinf=-1, neginf=-1).astype(np.float32)


def match_map(image, patch, mask=None, peak_mask=None, kernel=5, minimum_score=None, *, edges=False,
              anchor=None):
    rates = correlation_rates(image, patch, mask, edges=edges)
    # Invalid candidates must not manufacture sharp peaks next to missing tiles.
    valid = (rates > -1).astype(np.float32)
    weights = cv2.GaussianBlur(valid, (kernel, kernel), 0)
    baseline = cv2.GaussianBlur(rates*valid, (kernel, kernel), 0)/np.maximum(weights, 1e-6)
    local = np.where(valid > 0, rates-baseline, -np.inf)
    if peak_mask is not None:
        h, w = local.shape
        mh, mw = peak_mask.shape
        dh, dw = max(0, h-mh), max(0, w-mw)
        if dh or dw:
            peak_mask = cv2.copyMakeBorder(peak_mask, dh//2, dh-dh//2, dw//2, dw-dw//2,
                                          cv2.BORDER_CONSTANT, value=0)
        oy, ox = (peak_mask.shape[0]-h)//2, (peak_mask.shape[1]-w)//2
        local = np.where(peak_mask[oy:oy+h, ox:ox+w] > 0, local, -np.inf)
    if minimum_score is not None:
        eligible = np.isfinite(local)
        strongest = float(np.max(rates[eligible])) if np.any(eligible) else -1
        credible = eligible & (rates >= max(minimum_score, strongest-.12))
        if np.any(credible):
            local = np.where(credible, local, -np.inf)
    _, detail, _, (px, py) = cv2.minMaxLoc(local)
    left, top = max(0, px-4), max(0, py-4)
    fine = rates[top:min(rates.shape[0], py+4), left:min(rates.shape[1], px+4)]
    if not fine.size or not np.isfinite(detail):
        raise NavigationError("没有可识别的地图区域。")
    _, score, _, (fx, fy) = cv2.minMaxLoc(
        cv2.resize(fine, None, fx=20, fy=20, interpolation=cv2.INTER_CUBIC))
    if anchor is not None:
        # OpenCV resize maps destination pixel centers back to source centers.
        # Report the caller's actual sample anchor, not a rounded patch center.
        return (left+(fx+.5)/20-.5+anchor[0], top+(fy+.5)/20-.5+anchor[1],
                float(score), float(detail))
    # Preserve existing big-map/tool coordinate conventions until those callers
    # explicitly supply their own sample anchors.
    return (left+fx/20-1+patch.shape[1]/2, top+fy/20-1+patch.shape[0]/2,
            float(score), float(detail))


def align_reference(background, observed):
    """Align camera-background pixels using the same missing-terrain rules."""
    rates = correlation_rates(background, observed)
    if not np.any(rates > -1):
        raise NavigationError("视角识别缺少已采集的地图背景。")
    _, _, _, (bx, by) = cv2.minMaxLoc(
        cv2.resize(rates, None, fx=20, fy=20, interpolation=cv2.INTER_CUBIC))
    yy, xx = np.indices(observed.shape, dtype=np.float32)
    aligned = cv2.remap(background, xx+bx/20, yy+by/20, cv2.INTER_LINEAR)
    valid = cv2.remap(known_pixels(background), xx+bx/20, yy+by/20, cv2.INTER_LINEAR) >= .99
    return aligned, valid
