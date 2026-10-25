"""Shared coordinates for the game's 720p capture and 1080p minimap reference."""
from __future__ import annotations

import cv2

from game_view import require_game_frame


def reference_frame(frame):
    require_game_frame(frame)
    return cv2.resize(frame, (1920, 1080), interpolation=cv2.INTER_LINEAR)


def minimap_crop(reference, *, padding=0):
    return reference[22-padding:222+padding, 81-padding:281+padding]
