"""Shared capture geometry for game recognition and input coordinates."""
from __future__ import annotations

from navigation.models import NavigationError


class GameFrameSizeError(NavigationError):
    pass


def require_game_frame(frame):
    if frame is None or frame.size == 0:
        raise NavigationError("未获取到游戏画面。")
    height, width = frame.shape[:2]
    if (width, height) != (1280, 720):
        raise GameFrameSizeError(
            f"当前游戏截图为 {width}×{height}，识别与点击需要 1280×720 基准画面。"
            "请在游戏设置中选择 16:9 分辨率（推荐 1920×1080），"
            "并确保游戏窗口实际保持 16:9 后重新运行。")
