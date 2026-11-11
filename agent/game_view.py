"""Shared capture geometry for game recognition and input coordinates."""
from __future__ import annotations

from navigation.models import NavigationError


class GameFrameSizeError(NavigationError):
    pass


ALLOWED_FRAME_SIZES: set[tuple[int, int]] = {(1280, 720)}


def is_clipped_game_frame(frame) -> bool:
    """Detect a 1280x720 capture whose right and bottom edges are clipped off-screen."""
    if frame is None or getattr(frame, "ndim", 0) < 2 or frame.shape[:2] != (720, 1280):
        return False
    return bool(
        frame[:, 1276:].max() == 0
        and frame[708:, :].max() == 0
        and frame[:12, :1270].max() > 15
        and frame[:700, :12].max() > 15
    )


def require_game_frame(frame):
    if frame is None or frame.size == 0:
        raise NavigationError("未获取到游戏画面。")
    height, width = frame.shape[:2]
    if (width, height) not in ALLOWED_FRAME_SIZES:
        raise GameFrameSizeError(
            f"当前游戏截图为 {width}×{height}，识别与点击需要 1280×720（16:9）基准画面。"
            "请在游戏设置中选择 16:9 分辨率（若窗口高度被标题栏或任务栏挤占，"
            "请在 16:9 屏幕上改用全屏/无边框或换用 1600×900、1280×720 等更小的 16:9 窗口；"
            "若开启超分辨率 TSR 后分辨率异常，请切换为 DLSS/XeSS/TAA）后重新运行。")
    if is_clipped_game_frame(frame):
        raise GameFrameSizeError(
            "当前游戏窗口超出屏幕右/下边界，截图右侧与底部出现黑边且画面整体偏移"
            "（常见于在 1920×1080 屏幕上使用 1920×1080 带边框窗口模式）。"
            "请在游戏设置中切换为「全屏」或「无边框窗口」，或改用 1600×900、1280×720 等更小的 16:9 窗口并移回屏幕内后重新运行。")
