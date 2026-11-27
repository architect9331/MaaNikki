"""Prepare and locate the v3.0 sliding daily score line without inventing digits."""
from __future__ import annotations

import cv2
import numpy as np

# In v3.0 每日灵感 (both 朝夕心愿 and 星海拾光), the right-side vertical reward track
# hosts 5 milestone circles at x~1196, y~160..465. When score is 100..500, the game
# draws `<score> ★` directly to the left of the active milestone circle (digits in
# x~1092..1142, 4-point golden star pointer in x~1140..1172). When score is 0, no
# pointer or digit label is drawn and all 5 reward circles remain dark/unlit.
SCORE_COLUMN_ROI = (1085, 140, 62, 350)
SCORE_POINTER_ROI = (1138, 140, 36, 350)
REWARD_TRACK_ROI = (1174, 140, 48, 350)


def locate_score_pointer(frame: np.ndarray) -> tuple[int, int] | None:
    """Locate the golden-white 4-point star beside the active daily score (100..500)."""
    if frame is None or frame.size == 0:
        return None
    h, w = frame.shape[:2]
    rx, ry, rw, rh = SCORE_POINTER_ROI
    if ry + rh > h or rx + rw > w:
        return None
    sub = frame[ry:ry + rh, rx:rx + rw]
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    gold_mask = cv2.inRange(hsv, (8, 35, 150), (38, 255, 255))
    core_mask = cv2.inRange(hsv, (0, 0, 220), (180, 60, 255))
    combined = cv2.bitwise_or(gold_mask, core_mask)
    count, _labels, stats, centroids = cv2.connectedComponentsWithStats(combined)
    candidates = []
    for index in range(1, count):
        bx, by, bw, bh, area = stats[index]
        if area < 60 or not (12 <= bw <= 38 and 12 <= bh <= 42):
            continue
        gold_px = int((gold_mask[by:by + bh, bx:bx + bw] > 0).sum())
        core_px = int((core_mask[by:by + bh, bx:bx + bw] > 0).sum())
        if gold_px >= 30 and core_px >= 30:
            cx = rx + round(float(centroids[index][0]))
            cy = ry + round(float(centroids[index][1]))
            candidates.append((gold_px + core_px, cx, cy))
    if len(candidates) != 1:
        return None
    _, cx, cy = candidates[0]
    return cx, cy


def score_digit_roi(pointer_y: int, frame_shape: tuple[int, int]) -> list[int]:
    """Return the tight [x, y, w, h] digit box immediately left of the star pointer."""
    h, w = frame_shape[:2]
    y0 = max(0, pointer_y - 14)
    y1 = min(h, pointer_y + 14)
    x0 = 1090
    x1 = min(w, 1142)
    return [x0, y0, max(1, x1 - x0), max(1, y1 - y0)]


def reward_track_unlit(frame: np.ndarray) -> bool:
    """Return True when none of the 5 right-side reward circles show golden activation."""
    if frame is None or frame.size == 0:
        return False
    h, w = frame.shape[:2]
    rx, ry, rw, rh = REWARD_TRACK_ROI
    if ry + rh > h or rx + rw > w:
        return False
    hsv = cv2.cvtColor(frame[ry:ry + rh, rx:rx + rw], cv2.COLOR_BGR2HSV)
    gold_px = int((cv2.inRange(hsv, (10, 40, 130), (35, 255, 255)) > 0).sum())
    return gold_px < 120


def score_image(crop):
    # The framework has already downscaled to 720p. Pure-white (V >= 250,
    # S == 0) masking breaks the anti-aliased top and bottom of a lone zero.
    mask = cv2.inRange(cv2.cvtColor(crop, cv2.COLOR_BGR2HSV), (0, 0, 210), (180, 60, 255))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    cleaned = np.zeros_like(mask)
    for index in range(1, count):
        x, y, w, h, area = stats[index]
        if h >= max(5, crop.shape[0]*.4) and area >= 4:
            cleaned[labels == index] = 255
    x, y, w, h = cv2.boundingRect(cleaned)
    if not w or not h:
        return None
    # Tight glyph bounds remove side decorations; modest padding preserves
    # the natural single-character aspect ratio for recognition-only OCR.
    line = cv2.resize(cleaned[y:y+h, x:x+w], None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    line = cv2.copyMakeBorder(line, 6, 6, 6, 6, cv2.BORDER_CONSTANT, value=0)
    return cv2.cvtColor(line, cv2.COLOR_GRAY2BGR)
