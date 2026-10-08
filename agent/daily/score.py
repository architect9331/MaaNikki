"""Prepare the small daily score line without inventing or correcting digits."""
from __future__ import annotations

import cv2
import numpy as np


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
