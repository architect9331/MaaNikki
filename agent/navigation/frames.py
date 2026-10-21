"""Shared coordinates for the game's 720p capture and 1080p minimap reference."""
from __future__ import annotations

import cv2

from .models import NavigationError


def reference_frame(frame):
    if frame is None or frame.shape[:2] != (720, 1280):
        raise NavigationError("导航需要 16:9 游戏画面，截图基准为 1280×720。")
    return cv2.resize(frame, (1920, 1080), interpolation=cv2.INTER_LINEAR)


def minimap_crop(reference, *, padding=0):
    return reference[22-padding:222+padding, 81-padding:281+padding]
