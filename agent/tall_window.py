"""Temporary 1:4 ultra-tall borderless window resize for Compendium and Eureka scanning.

Adapted from the window adjustment mechanism in CountingMomo
(https://github.com/ChanIok/CountingMomo) by ChanIok.
"""
from __future__ import annotations

import atexit
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import time

from game_view import ALLOWED_FRAME_SIZES
from start_game import game_window
from task_control import active_session


GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_POPUP = 0x80000000
WS_BORDERLESS_MASK = 0x00CF0000
WS_EX_BORDERLESS_MASK = 0x00020301
SW_RESTORE = 9
MONITOR_DEFAULTTONEAREST = 2
SWP_RESIZE_FLAGS = 0x4034  # SWP_NOACTIVATE | SWP_NOZORDER | SWP_FRAMECHANGED | SWP_ASYNCWINDOWPOS
SWP_STYLE_FLAGS = 0x0037   # SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED
SWP_RECT_FLAGS = 0x0034    # SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)

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
    try:
        u32.SetThreadDpiAwarenessContext.argtypes = [wintypes.HANDLE]
        u32.SetThreadDpiAwarenessContext.restype = wintypes.HANDLE
    except Exception:
        pass
    return u32


@contextmanager
def _per_monitor_dpi(u32):
    """Run Win32 window geometry queries and updates in physical monitor pixels."""
    prev = None
    try:
        prev = u32.SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
    except Exception:
        prev = None
    try:
        yield
    finally:
        if prev:
            try:
                u32.SetThreadDpiAwarenessContext(prev)
            except Exception:
                pass


def _resolve_hwnd(rt=None, controller=None) -> int | None:
    session = active_session()
    if session is not None and getattr(session, "adapter", None) is not None:
        hwnd = getattr(session.adapter, "hwnd", None)
        if hwnd:
            return int(hwnd)
    ctrl = controller or getattr(rt, "controller", None)
    if ctrl is not None:
        try:
            info = ctrl.info
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


def _poll_capture(rt):
    """Capture a transition frame directly without triggering fatal frame_size stops."""
    if rt.stopped:
        return None
    try:
        job = rt.controller.post_screencap().wait()
        if rt.stopped or not job.succeeded:
            return None
        return job.get()
    except Exception:
        return None


def heal_16_9_window(controller=None, *, hwnd: int | None = None):
    """Repair a windowed 16:9 game window whose physical client size is off by 1-4 px."""
    u32 = _user32()
    target_hwnd = int(hwnd) if hwnd else _resolve_hwnd(controller=controller)
    if not target_hwnd:
        return None
    resized = False
    with _per_monitor_dpi(u32):
        if not u32.IsWindow(target_hwnd) or u32.IsIconic(target_hwnd) or u32.IsZoomed(target_hwnd):
            return None
        csize = _client_size(u32, target_hwnd)
        if csize is None:
            return None
        cw, ch = csize
        if cw <= 0 or ch <= 0:
            return None
        target_cw = round(ch * 16 / 9)
        target_ch = round(cw * 9 / 16)
        dw = dh = 0
        if (cw, ch) != (target_cw, ch) and 1 <= abs(cw - target_cw) <= 4:
            dw = target_cw - cw
        elif (cw, ch) != (cw, target_ch) and 1 <= abs(ch - target_ch) <= 4:
            dh = target_ch - ch
        if dw or dh:
            wrect = RECT()
            if u32.GetWindowRect(target_hwnd, ctypes.byref(wrect)):
                w = int(wrect.right - wrect.left) + dw
                h = int(wrect.bottom - wrect.top) + dh
                if w > 0 and h > 0:
                    resized = bool(
                        u32.SetWindowPos(
                            target_hwnd, 0, int(wrect.left), int(wrect.top), w, h, SWP_RECT_FLAGS
                        )
                    )
    if not resized or controller is None:
        return None
    for _ in range(8):
        time.sleep(0.1)
        try:
            job = controller.post_screencap().wait()
            frame = job.get() if job.succeeded else None
            if frame is not None and frame.shape[:2] == (720, 1280):
                return frame
        except Exception:
            break
    return None


@contextmanager
def tall_window(rt):
    """Temporarily resize the game window to 1440x5760 (1280x5120 capture) and restore on exit."""
    u32 = _user32()
    hwnd = _resolve_hwnd(rt)
    if not hwnd or not u32.IsWindow(hwnd):
        yield False
        return

    heal_16_9_window(hwnd=hwnd)
    with _per_monitor_dpi(u32):
        style = u32.GetWindowLongPtrW(hwnd, GWL_STYLE)
        ex_style = u32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        wp = WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(WINDOWPLACEMENT)
        if not u32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
            yield False
            return
        saved_rect = RECT()
        u32.GetWindowRect(hwnd, ctypes.byref(saved_rect))
        saved_client = _client_size(u32, hwnd)

    restored = False
    window_modified = False
    restored_frames_waited = False

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
        with _per_monitor_dpi(u32):
            if u32.IsWindow(hwnd):
                u32.SetWindowLongPtrW(hwnd, GWL_STYLE, style)
                u32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex_style)
                u32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_STYLE_FLAGS)
                u32.SetWindowPlacement(hwnd, ctypes.byref(wp))
                if not u32.IsZoomed(hwnd):
                    w = int(saved_rect.right - saved_rect.left)
                    h = int(saved_rect.bottom - saved_rect.top)
                    if w > 0 and h > 0:
                        u32.SetWindowPos(
                            hwnd, 0, int(saved_rect.left), int(saved_rect.top), w, h, SWP_RECT_FLAGS
                        )
                        if saved_client is not None:
                            actual = _client_size(u32, hwnd)
                            if actual is not None and actual != saved_client:
                                dw = saved_client[0] - actual[0]
                                dh = saved_client[1] - actual[1]
                                if abs(dw) <= 16 and abs(dh) <= 16 and w + dw > 0 and h + dh > 0:
                                    u32.SetWindowPos(
                                        hwnd, 0, int(saved_rect.left), int(saved_rect.top),
                                        w + dw, h + dh, SWP_RECT_FLAGS,
                                    )

    def _restore_and_wait():
        nonlocal restored_frames_waited
        _restore_win32()
        rt.invalidate_frame()
        if window_modified and not restored_frames_waited and not rt.stopped:
            restored_frames_waited = True
            time.sleep(0.25)
            rendered_streak = 0
            for _ in range(20):
                if rt.stopped:
                    break
                time.sleep(0.12)
                frame = _poll_capture(rt)
                if (
                    frame is not None
                    and frame.shape[:2] == (720, 1280)
                    and float(frame.mean()) > 8.0
                    and float(frame.std()) > 5.0
                    and float(frame[:, 960:].mean()) > 5.0
                    and float(frame[:, 960:].std()) > 3.0
                ):
                    rendered_streak += 1
                    if rendered_streak >= 2:
                        break
                else:
                    rendered_streak = 0
            rt.invalidate_frame()

    atexit.register(_restore_win32)
    try:
        with _per_monitor_dpi(u32):
            if u32.IsIconic(hwnd) or u32.IsZoomed(hwnd):
                window_modified = True
                u32.ShowWindow(hwnd, SW_RESTORE)
                deadline = time.monotonic() + 1.5
                while time.monotonic() < deadline and (u32.IsIconic(hwnd) or u32.IsZoomed(hwnd)):
                    time.sleep(0.03)

            hmon = u32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            if not hmon or not u32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                _restore_and_wait()
                yield False
                return

            mon_w = int(mi.rcMonitor.right - mi.rcMonitor.left)
            win_x = int(mi.rcMonitor.left) + (mon_w - TALL_PHYSICAL_WIDTH) // 2
            win_y = int(mi.rcMonitor.top)

            tall_style = ctypes.c_ssize_t((style & ~WS_BORDERLESS_MASK) | WS_POPUP).value
            tall_ex_style = ctypes.c_ssize_t(ex_style & ~WS_EX_BORDERLESS_MASK).value
            window_modified = True
            u32.SetWindowLongPtrW(hwnd, GWL_STYLE, tall_style)
            u32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, tall_ex_style)
            req_w, req_h = TALL_PHYSICAL_WIDTH, TALL_PHYSICAL_HEIGHT
            if not u32.SetWindowPos(hwnd, 0, win_x, win_y, req_w, req_h, SWP_RESIZE_FLAGS):
                _restore_and_wait()
                yield False
                return

            deadline = time.monotonic() + 4.0
            stable_since = None
            mismatch_since = None
            last_csize = None
            adjusted_delta = False
            resized_ok = False
            while time.monotonic() < deadline:
                if rt.stopped or not u32.IsWindow(hwnd):
                    break
                csize = _client_size(u32, hwnd)
                now = time.monotonic()
                if csize == (TALL_PHYSICAL_WIDTH, TALL_PHYSICAL_HEIGHT):
                    mismatch_since = None
                    if stable_since is None:
                        stable_since = now
                    elif now - stable_since >= 0.25:
                        resized_ok = True
                        break
                else:
                    stable_since = None
                    if csize is not None and csize == last_csize:
                        if mismatch_since is None:
                            mismatch_since = now
                        elif not adjusted_delta and now - mismatch_since >= 0.16:
                            cw, ch = csize
                            dw = TALL_PHYSICAL_WIDTH - cw
                            dh = TALL_PHYSICAL_HEIGHT - ch
                            if abs(dw) <= 32 and abs(dh) <= 64:
                                adjusted_delta = True
                                req_w += dw
                                req_h += dh
                                u32.SetWindowPos(
                                    hwnd, 0, win_x, win_y, req_w, req_h, SWP_RESIZE_FLAGS
                                )
                                mismatch_since = None
                    else:
                        last_csize = csize
                        mismatch_since = now
                time.sleep(0.04)

        if not resized_ok:
            _restore_and_wait()
            rt.log("超长竖屏切换未生效（请确认游戏为窗口模式），继续使用常规窗口读取。", detail=True)
            yield False
            return

        ALLOWED_FRAME_SIZES.add((TALL_CAPTURE_WIDTH, TALL_CAPTURE_HEIGHT))
        rt.controller.set_screenshot_target_short_side(TALL_CAPTURE_WIDTH)
        rt.invalidate_frame()

        capture_ok = False
        nudged_capture_height = False
        for _ in range(16):
            if rt.stopped:
                break
            time.sleep(0.12)
            frame = _poll_capture(rt)
            if frame is None:
                continue
            if (
                frame.shape[:2] == (TALL_CAPTURE_HEIGHT, TALL_CAPTURE_WIDTH)
                and float(frame.mean()) > 8.0
                and float(frame.std()) > 5.0
            ):
                capture_ok = True
                break
            if (
                not nudged_capture_height
                and frame.shape[1] == TALL_CAPTURE_WIDTH
                and 1 <= abs(frame.shape[0] - TALL_CAPTURE_HEIGHT) <= 2
            ):
                nudged_capture_height = True
                dh = round((TALL_CAPTURE_HEIGHT - frame.shape[0]) * req_w / TALL_CAPTURE_WIDTH)
                if dh:
                    req_h += dh
                    with _per_monitor_dpi(u32):
                        u32.SetWindowPos(hwnd, 0, win_x, win_y, req_w, req_h, SWP_RESIZE_FLAGS)

        if not capture_ok:
            _restore_and_wait()
            rt.log("超长竖屏截图尺寸未就绪，继续使用常规窗口读取。", detail=True)
            yield False
            return

        rt.action("TouchMove", target=[640, 36])
        rt.pause(0.25)
        yield True
    finally:
        _restore_and_wait()
        try:
            atexit.unregister(_restore_win32)
        except Exception:
            pass
