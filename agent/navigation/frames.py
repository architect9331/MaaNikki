"""Shared coordinates for the game's 720p capture and 1080p minimap reference."""
from __future__ import annotations

import math

import cv2
import numpy as np

from game_view import require_game_frame
from .matching import luma
from .models import MINIMAP_CENTER, MINIMAP_RADIUS


def reference_frame(frame):
    require_game_frame(frame)
    return cv2.resize(frame, (1920, 1080), interpolation=cv2.INTER_LINEAR)


def minimap_crop(reference, *, padding=0, spec=None):
    center = spec.center if spec else MINIMAP_CENTER
    radius = round((spec.radius if spec else MINIMAP_RADIUS)*1.5)+padding
    x, y = (round(v*1.5) for v in center)
    return reference[y-radius:y+radius, x-radius:x+radius]


def minimap_sample(frame, spec):
    """Sample at map density with the exact player pivot on the middle pixel.

    Avoid the 720p -> 1080p -> nearest-neighbor reduction. An odd patch keeps
    its anchor independent of dimension rounding, including other map scales.
    """
    require_game_frame(frame)
    half = round(spec.radius*spec.scale)
    cx, cy = (v-.5 for v in spec.center)
    sigma = .5*math.sqrt(max(0, 1/spec.scale**2-1))
    padding = max(2, math.ceil(3*sigma)+1)
    extent = half/spec.scale
    height, width = frame.shape[:2]
    left, top = max(0, math.floor(cx-extent)-padding), max(0, math.floor(cy-extent)-padding)
    right, bottom = min(width, math.ceil(cx+extent)+padding+1), min(height, math.ceil(cy+extent)+padding+1)
    gray = luma(frame[top:bottom, left:right])
    if sigma > 0:
        gray = cv2.GaussianBlur(gray, (0, 0), sigma)
    yy, xx = np.indices((half*2+1, half*2+1), dtype=np.float32)
    return cv2.remap(gray, (xx-half)/spec.scale+cx-left,
                     (yy-half)/spec.scale+cy-top, cv2.INTER_LINEAR)
