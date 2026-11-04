"""Temporary 1:4 ultra-tall borderless window resize for Compendium and Eureka scanning.

Adapted from the window adjustment mechanism in CountingMomo
(https://github.com/ChanIok/CountingMomo) by ChanIok.
"""
from __future__ import annotations

import atexit
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import sys
import time

from game_view import ALLOWED_FRAME_SIZES
from start_game import game_window
from task_control import active_session


GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_POPUP = 0x80000000
WS_BORDERLESS_MASK = 0x00CF0000
SW_RESTORE = 9
MONITOR_DEFAULTTONEAREST = 2
SWP_RESIZE_FLAGS = 0x4034  # SWP_NOACTIVATE | SWP_NOZORDER | SWP_FRAMECHANGED | SWP_ASYNCWINDOWPOS
SWP_STYLE_FLAGS = 0x0037   # SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED
SWP_RECT_FLAGS = 0x0034    # SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED

TALL_PHYSICAL_WIDTH = 1440
TALL_PHYSICAL_HEIGHT = 5760
TALL_CAPTURE_WIDTH = 1280
TALL_CAPTURE_HEIGHT = 5120


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.UINT),
        ("flags", wintypes.UINT),
        ("showCmd", wintypes.UINT),
        ("ptMinPosition", POINT),
        ("ptMaxPosition", POINT),
        ("rcNormalPosition", RECT),
    ]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wintypes.DWORD),
    ]


def _user32():
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    u32.IsWindow.argtypes = [wintypes.HWND]
    u32.IsWindow.restype = wintypes.BOOL
    u32.IsIconic.argtypes = [wintypes.HWND]
    u32.IsIconic.restype = wintypes.BOOL
    u32.IsZoomed.argtypes = [wintypes.HWND]
    u32.IsZoomed.restype = wintypes.BOOL
    u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u32.ShowWindow.restype = wintypes.BOOL
    u32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    u32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    u32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    u32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    u32.GetWindowPlacement.argtypes = [wintypes.HWND, ctypes.POINTER(WINDOWPLACEMENT)]
    u32.GetWindowPlacement.restype = wintypes.BOOL
    u32.SetWindowPlacement.argtypes = [wintypes.HWND, ctypes.POINTER(WINDOWPLACEMENT)]
    u32.SetWindowPlacement.restype = wintypes.BOOL
    u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    u32.GetWindowRect.restype = wintypes.BOOL
    u32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    u32.GetClientRect.restype = wintypes.BOOL
    u32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    u32.MonitorFromWindow.restype = wintypes.HMONITOR
    u32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
    u32.GetMonitorInfoW.restype = wintypes.BOOL
    u32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.UINT,
    ]
    u32.SetWindowPos.restype = wintypes.BOOL
    return u32


def _resolve_hwnd(rt) -> int | None:
    session = active_session()
    if session is not None and getattr(session, "adapter", None) is not None:
        hwnd = getattr(session.adapter, "hwnd", None)
        if hwnd:
            return int(hwnd)
    try:
        info = rt.controller.info
        handle = info.get("handle") or info.get("hwnd")
        if handle:
            return int(handle)
    except Exception:
        pass
    hwnd = game_window()
    return int(hwnd) if hwnd else None


def _client_size(u32, hwnd: int) -> tuple[int, int] | None:
    rc = RECT()
    if not u32.GetClientRect(hwnd, ctypes.byref(rc)):
        return None
    return int(rc.right - rc.left), int(rc.bottom - rc.top)


@contextmanager
def tall_window(rt):
    """Temporarily resize the game window to 1440x5760 (1280x5120 capture) and restore on exit."""
    u32 = _user32()
    hwnd = _resolve_hwnd(rt)
    if not hwnd or not u32.IsWindow(hwnd):
        yield False
        return

    style = u32.GetWindowLongPtrW(hwnd, GWL_STYLE)
    ex_style = u32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    wp = WINDOWPLACEMENT()
    wp.length = ctypes.sizeof(WINDOWPLACEMENT)
    if not u32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
        yield False
        return
    saved_rect = RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(saved_rect))

    restored = False

    def _restore_win32():
        nonlocal restored
        if restored:
            return
        restored = True
        ALLOWED_FRAME_SIZES.discard((TALL_CAPTURE_WIDTH, TALL_CAPTURE_HEIGHT))
        try:
            rt.controller.set_screenshot_target_short_side(720)
        except Exception:
            pass
        if u32.IsWindow(hwnd):
            u32.SetWindowLongPtrW(hwnd, GWL_STYLE, style)
            u32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex_style)
            u32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_STYLE_FLAGS)
            u32.SetWindowPlacement(hwnd, ctypes.byref(wp))
            if not u32.IsZoomed(hwnd):
                w = int(saved_rect.right - saved_rect.left)
                h = int(saved_rect.bottom - saved_rect.top)
                if w > 0 and h > 0:
                    u32.SetWindowPos(hwnd, 0, int(saved_rect.left), int(saved_rect.top), w, h, SWP_RECT_FLAGS)

    atexit.register(_restore_win32)
    try:
        if u32.IsIconic(hwnd) or u32.IsZoomed(hwnd):
            u32.ShowWindow(hwnd, SW_RESTORE)
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline and (u32.IsIconic(hwnd) or u32.IsZoomed(hwnd)):
                time.sleep(0.03)

        hmon = u32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not hmon or not u32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            yield False
            return

        mon_w = int(mi.rcMonitor.right - mi.rcMonitor.left)
        win_x = int(mi.rcMonitor.left) + (mon_w - TALL_PHYSICAL_WIDTH) // 2
        win_y = int(mi.rcMonitor.top)

        tall_style = ctypes.c_ssize_t((style & ~WS_BORDERLESS_MASK) | WS_POPUP).value
        u32.SetWindowLongPtrW(hwnd, GWL_STYLE, tall_style)
        if not u32.SetWindowPos(
            hwnd, 0, win_x, win_y, TALL_PHYSICAL_WIDTH, TALL_PHYSICAL_HEIGHT, SWP_RESIZE_FLAGS
        ):
            _restore_win32()
            yield False
            return

        deadline = time.monotonic() + 4.0
        stable_since = None
        resized_ok = False
        while time.monotonic() < deadline:
            if rt.stopped or not u32.IsWindow(hwnd):
                break
            csize = _client_size(u32, hwnd)
            now = time.monotonic()
            if csize == (TALL_PHYSICAL_WIDTH, TALL_PHYSICAL_HEIGHT):
                if stable_since is None:
                    stable_since = now
                elif now - stable_since >= 0.25:
                    resized_ok = True
                    break
            else:
                stable_since = None
            time.sleep(0.04)

        if not resized_ok:
            _restore_win32()
            rt.log("超长竖屏切换未生效（请确认游戏为窗口模式），继续使用常规窗口读取。", detail=True)
            yield False
            return

        ALLOWED_FRAME_SIZES.add((TALL_CAPTURE_WIDTH, TALL_CAPTURE_HEIGHT))
        rt.controller.set_screenshot_target_short_side(TALL_CAPTURE_WIDTH)
        rt.invalidate_frame()

        capture_ok = False
        for _ in range(16):
            if rt.stopped:
                break
            time.sleep(0.12)
            frame = rt.capture()
            if (
                frame is not None
                and frame.shape[:2] == (TALL_CAPTURE_HEIGHT, TALL_CAPTURE_WIDTH)
                and float(frame.mean()) > 8.0
                and float(frame.std()) > 5.0
            ):
                capture_ok = True
                break

        if not capture_ok:
            _restore_win32()
            yield False
            return

        rt.action("TouchMove", target=[640, 36])
        rt.pause(0.25)
        yield True
    finally:
        exc_active = sys.exc_info()[0] is not None
        _restore_win32()
        try:
            atexit.unregister(_restore_win32)
        except Exception:
            pass
        if not exc_active:
            rt.invalidate_frame()
            time.sleep(0.25)
            rendered_streak = 0
            for _ in range(20):
                time.sleep(0.12)
                try:
                    frame = rt.capture()
                    if (
                        frame is not None
                        and frame.shape[:2] == (720, 1280)
                        and float(frame.mean()) > 8.0
                        and float(frame.std()) > 5.0
                    ):
                        rendered_streak += 1
                        if rendered_streak >= 2:
                            break
                    else:
                        rendered_streak = 0
                except Exception:
                    break
