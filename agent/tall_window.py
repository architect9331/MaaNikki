"""Generalized game window resolution adjustment (closest 16:9 & temporary 1:4 tall scan).

Adapted and generalized from the window adjustment mechanism in CountingMomo
(https://github.com/ChanIok/CountingMomo) by ChanIok.
"""
from __future__ import annotations

import atexit
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import json
import math
from pathlib import Path
from threading import Lock
import time

from client_config import mxu_config, node_action_params
from file_io import write_json
from game_view import ALLOWED_FRAME_SIZES, is_clipped_game_frame
from message_log import write_message
from start_game import game_window
from task_control import active_session


ROOT = Path(__file__).resolve().parents[1]
HANDOFF_FILE = ROOT / "logs" / "startup" / "window_restore.json"

GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_MAXIMIZE = 0x01000000
WS_POPUP = 0x80000000
WS_BORDERLESS_MASK = 0x00CF0000
WS_EX_BORDERLESS_MASK = 0x00020301
SW_SHOWNORMAL = 1
SW_SHOWMINIMIZED = 2
SW_SHOWMAXIMIZED = 3
SW_SHOWNOACTIVATE = 4
SW_RESTORE = 9
MONITOR_DEFAULTTONEAREST = 2
SWP_RESIZE_FLAGS = 0x4034  # SWP_NOACTIVATE | SWP_NOZORDER | SWP_FRAMECHANGED | SWP_ASYNCWINDOWPOS
SWP_STYLE_FLAGS = 0x0037   # SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED
SWP_RECT_FLAGS = 0x0034    # SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)

FAILED_RETRY_COOLDOWN_SECONDS = 8.0
READJUST_SETTLE_SECONDS = 1.5
OSCILLATION_WINDOW_SECONDS = 45.0
MAX_RECENT_ADJUSTMENTS = 3
MAX_FAILED_ATTEMPTS = 2
TALL_FAILED_COOLDOWN_SECONDS = 15.0

BASE_CAPTURE_WIDTH = 1280
BASE_CAPTURE_HEIGHT = 720
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


@dataclass
class WindowSnapshot:
    hwnd: int
    style: int
    ex_style: int
    placement: WINDOWPLACEMENT
    rect: RECT
    client_size: tuple[int, int] | None


def _user32():
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    u32.IsWindow.argtypes = [wintypes.HWND]
    u32.IsWindow.restype = wintypes.BOOL
    u32.IsIconic.argtypes = [wintypes.HWND]
    u32.IsIconic.restype = wintypes.BOOL
    u32.IsZoomed.argtypes = [wintypes.HWND]
    u32.IsZoomed.restype = wintypes.BOOL
    u32.GetForegroundWindow.argtypes = []
    u32.GetForegroundWindow.restype = wintypes.HWND
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
    u32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(POINT)]
    u32.ClientToScreen.restype = wintypes.BOOL
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
    try:
        u32.GetDpiForWindow.argtypes = [wintypes.HWND]
        u32.GetDpiForWindow.restype = wintypes.UINT
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


def is_auto_adjust_resolution_enabled(context=None, options=None) -> bool:
    """Return whether automatic window resolution adjustment is enabled in Settings."""
    if context is not None:
        try:
            params = node_action_params(context, "MaaNikki_AutoAdjustResolution")
            if "value" in params and params["value"] not in ("", None):
                return bool(params["value"])
        except Exception:
            pass
    if isinstance(options, dict) and "AutoAdjustResolution" in options:
        opt = options.get("AutoAdjustResolution")
        if isinstance(opt, bool):
            return opt
        if isinstance(opt, dict):
            if opt.get("type") == "switch" and opt.get("value") not in ("", None):
                return bool(opt.get("value", True))
            case_name = opt.get("caseName")
            if case_name in ("Yes", "No"):
                return case_name == "Yes"
    try:
        cfg = mxu_config(ROOT) or {}
        opt = (cfg.get("globalOptionValues") or {}).get("AutoAdjustResolution")
        if isinstance(opt, dict):
            if opt.get("type") == "switch" and opt.get("value") not in ("", None):
                return bool(opt.get("value", True))
            case_name = opt.get("caseName")
            if case_name in ("Yes", "No"):
                return case_name == "Yes"
    except Exception:
        pass
    return True


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


def window_hwnd(rt=None, controller=None) -> int | None:
    """Return the active game window handle associated with rt/controller if available."""
    return _resolve_hwnd(rt=rt, controller=controller)


def _client_size(u32, hwnd: int) -> tuple[int, int] | None:
    rc = RECT()
    if not u32.GetClientRect(hwnd, ctypes.byref(rc)):
        return None
    return int(rc.right - rc.left), int(rc.bottom - rc.top)


def _client_screen_rect(u32, hwnd: int) -> tuple[int, int, int, int] | None:
    """Return rendered client bounds in physical screen pixels (excluding DWM shadow)."""
    csize = _client_size(u32, hwnd)
    if csize is None or csize[0] <= 0 or csize[1] <= 0:
        return None
    pt = POINT(0, 0)
    if not u32.ClientToScreen(hwnd, ctypes.byref(pt)):
        return None
    left, top = int(pt.x), int(pt.y)
    return left, top, left + csize[0], top + csize[1]


def _monitor_info(u32, hwnd: int) -> MONITORINFO | None:
    hmon = u32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    if not hmon:
        return None
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if not u32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        return None
    return mi


def _safe_screen_bounds(
    u32,
    hwnd: int,
    *,
    margin_px: int = 6,
    require_work_area: bool | None = None,
) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]] | None:
    """Return ((c_left, c_top, c_right, c_bottom), (s_left, s_top, s_right, s_bottom)) in physical px.

    Edges where rcWork is inset from rcMonitor carry a taskbar/dock band. Unless the target window is
    an unclipped active foreground fullscreen window (or require_work_area is False), coordinates are
    constrained inside rcWork inset by margin_px on taskbar edges and always within rcMonitor.
    """
    with _per_monitor_dpi(u32):
        if not hwnd or not u32.IsWindow(hwnd):
            return None
        cbox = _client_screen_rect(u32, hwnd)
        mi = _monitor_info(u32, hwnd)
        if cbox is None or mi is None:
            return None
        c_left, c_top, c_right, c_bottom = cbox
        cw = c_right - c_left
        ch = c_bottom - c_top
        if cw <= 0 or ch <= 0:
            return None
        mon = mi.rcMonitor
        work = mi.rcWork
        mon_w = int(mon.right - mon.left)
        mon_h = int(mon.bottom - mon.top)
        is_oversized = ch > mon_h + 2 or cw > mon_w + 2
        is_fullscreen_fg = (
            not is_oversized
            and c_left <= int(mon.left)
            and c_top <= int(mon.top)
            and c_right >= int(mon.right)
            and c_bottom >= int(mon.bottom)
            and int(u32.GetForegroundWindow() or 0) == int(hwnd)
        )
        enforce_work = (not is_fullscreen_fg) if require_work_area is None else bool(require_work_area)
        pad = max(0, int(margin_px))
        s_left = max(int(mon.left), c_left)
        s_top = max(int(mon.top), c_top)
        s_right = min(int(mon.right), c_right)
        s_bottom = min(int(mon.bottom), c_bottom)
        if enforce_work:
            if work.left > mon.left:
                s_left = max(s_left, int(work.left) + pad)
            else:
                s_left = max(s_left, int(work.left))
            if work.top > mon.top:
                s_top = max(s_top, int(work.top) + pad)
            else:
                s_top = max(s_top, int(work.top))
            if work.right < mon.right:
                s_right = min(s_right, int(work.right) - pad)
            else:
                s_right = min(s_right, int(work.right))
            if work.bottom < mon.bottom:
                s_bottom = min(s_bottom, int(work.bottom) - pad)
            else:
                s_bottom = min(s_bottom, int(work.bottom))
        return cbox, (s_left, s_top, s_right, s_bottom)


def safe_capture_bounds(
    hwnd: int | None,
    capture_size: tuple[int, int] = (BASE_CAPTURE_WIDTH, BASE_CAPTURE_HEIGHT),
    *,
    margin_px: int = 6,
    require_work_area: bool | None = None,
) -> tuple[int, int, int, int]:
    """Return inclusive (min_x, min_y, max_x, max_y) in capture coordinates avoiding taskbar/offscreen."""
    cap_w = max(1, int(capture_size[0]))
    cap_h = max(1, int(capture_size[1]))
    fallback_max_y = min(cap_h, BASE_CAPTURE_HEIGHT) - 1 if cap_h > BASE_CAPTURE_HEIGHT else cap_h - 1
    if not hwnd:
        return 0, 0, cap_w - 1, max(0, fallback_max_y)
    u32 = _user32()
    resolved = _safe_screen_bounds(
        u32,
        int(hwnd),
        margin_px=margin_px,
        require_work_area=require_work_area,
    )
    if resolved is None:
        return 0, 0, cap_w - 1, max(0, fallback_max_y)
    (c_left, c_top, c_right, c_bottom), (s_left, s_top, s_right, s_bottom) = resolved
    if s_right <= s_left or s_bottom <= s_top:
        return 0, 0, -1, -1
    scale_x = (c_right - c_left) / cap_w
    scale_y = (c_bottom - c_top) / cap_h
    min_x = max(0, min(cap_w - 1, math.ceil((s_left - c_left) / scale_x)))
    max_x = max(-1, min(cap_w - 1, math.floor((s_right - 1 - c_left) / scale_x)))
    min_y = max(0, min(cap_h - 1, math.ceil((s_top - c_top) / scale_y)))
    max_y = max(-1, min(cap_h - 1, math.floor((s_bottom - 1 - c_top) / scale_y)))
    return min_x, min_y, max_x, max_y


def safe_capture_y_range(
    hwnd: int | None,
    capture_height: int,
    *,
    capture_width: int = BASE_CAPTURE_WIDTH,
    margin_px: int = 6,
    require_work_area: bool | None = None,
) -> tuple[int, int]:
    """Return inclusive (min_y, max_y) capture Y bounds strictly inside the clickable work area."""
    _, min_y, _, max_y = safe_capture_bounds(
        hwnd,
        (capture_width, capture_height),
        margin_px=margin_px,
        require_work_area=require_work_area,
    )
    return min_y, max_y


def is_capture_point_safe(
    hwnd: int | None,
    target,
    *,
    capture_size: tuple[int, int] = (BASE_CAPTURE_WIDTH, BASE_CAPTURE_HEIGHT),
    margin_px: int = 4,
    require_work_area: bool | None = None,
) -> bool:
    """Return True when capture-space point or rect center stays inside monitor & taskbar-safe bounds."""
    if not isinstance(target, (list, tuple)) or len(target) not in (2, 4):
        return False
    px = float(target[0]) + (float(target[2]) / 2.0 if len(target) == 4 else 0.0)
    py = float(target[1]) + (float(target[3]) / 2.0 if len(target) == 4 else 0.0)
    if not (math.isfinite(px) and math.isfinite(py)):
        return False
    min_x, min_y, max_x, max_y = safe_capture_bounds(
        hwnd,
        capture_size,
        margin_px=margin_px,
        require_work_area=require_work_area,
    )
    if max_x < min_x or max_y < min_y:
        return False
    rx, ry = round(px), round(py)
    return min_x <= rx <= max_x and min_y <= ry <= max_y


def clamp_capture_park_point(
    hwnd: int | None,
    point,
    *,
    capture_size: tuple[int, int] = (BASE_CAPTURE_WIDTH, BASE_CAPTURE_HEIGHT),
    margin_px: int = 8,
) -> list[int]:
    """Clamp an idle hover/park capture point into the clickable work area."""
    px = round(float(point[0]))
    py = round(float(point[1]))
    min_x, min_y, max_x, max_y = safe_capture_bounds(
        hwnd,
        capture_size,
        margin_px=margin_px,
        require_work_area=True,
    )
    if max_x >= min_x:
        px = max(min_x, min(max_x, px))
    if max_y >= min_y:
        py = max(min_y, min(max_y, py))
    return [px, py]


def _poll_capture(controller, stopped_fn=None):
    """Capture a transition frame directly without triggering fatal frame_size stops."""
    if controller is None or (stopped_fn is not None and stopped_fn()):
        return None
    try:
        job = controller.post_screencap().wait()
        if (stopped_fn is not None and stopped_fn()) or not job.succeeded:
            return None
        return job.get()
    except Exception:
        return None


def _capture_snapshot(u32, hwnd: int) -> WindowSnapshot | None:
    with _per_monitor_dpi(u32):
        if not u32.IsWindow(hwnd):
            return None
        style = int(u32.GetWindowLongPtrW(hwnd, GWL_STYLE))
        ex_style = int(u32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE))
        wp = WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(WINDOWPLACEMENT)
        if not u32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
            return None
        saved_rect = RECT()
        if not u32.GetWindowRect(hwnd, ctypes.byref(saved_rect)):
            return None
        saved_client = _client_size(u32, hwnd)
        return WindowSnapshot(
            hwnd=int(hwnd),
            style=style,
            ex_style=ex_style,
            placement=wp,
            rect=saved_rect,
            client_size=saved_client,
        )


def _restore_snapshot(
    u32,
    snap: WindowSnapshot,
    *,
    controller=None,
    discard_capture_size: tuple[int, int] | None = None,
):
    if discard_capture_size is not None:
        ALLOWED_FRAME_SIZES.discard(discard_capture_size)
    if controller is not None:
        try:
            controller.set_screenshot_target_short_side(BASE_CAPTURE_HEIGHT)
        except Exception:
            pass
    with _per_monitor_dpi(u32):
        hwnd = snap.hwnd
        if not u32.IsWindow(hwnd):
            return
        u32.SetWindowLongPtrW(hwnd, GWL_STYLE, snap.style)
        u32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, snap.ex_style)
        show_cmd = int(snap.placement.showCmd)
        if show_cmd in (SW_SHOWMINIMIZED, SW_SHOWMAXIMIZED):
            u32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_STYLE_FLAGS)
            u32.SetWindowPlacement(hwnd, ctypes.byref(snap.placement))
            if not u32.IsZoomed(hwnd) and not u32.IsIconic(hwnd):
                w = int(snap.rect.right - snap.rect.left)
                h = int(snap.rect.bottom - snap.rect.top)
                if w > 0 and h > 0:
                    u32.SetWindowPos(
                        hwnd, 0, int(snap.rect.left), int(snap.rect.top), w, h, SWP_RECT_FLAGS
                    )
        else:
            w = int(snap.rect.right - snap.rect.left)
            h = int(snap.rect.bottom - snap.rect.top)
            if w > 0 and h > 0:
                u32.SetWindowPos(
                    hwnd, 0, int(snap.rect.left), int(snap.rect.top), w, h, SWP_RECT_FLAGS
                )
                if snap.client_size is not None:
                    actual = _client_size(u32, hwnd)
                    if actual is not None and actual != snap.client_size:
                        dw = snap.client_size[0] - actual[0]
                        dh = snap.client_size[1] - actual[1]
                        if abs(dw) <= 32 and abs(dh) <= 64 and w + dw > 0 and h + dh > 0:
                            u32.SetWindowPos(
                                hwnd, 0, int(snap.rect.left), int(snap.rect.top),
                                w + dw, h + dh, SWP_RECT_FLAGS,
                            )
            else:
                u32.SetWindowPlacement(hwnd, ctypes.byref(snap.placement))


def _snapshot_to_dict(snap: WindowSnapshot) -> dict:
    wp = snap.placement
    return {
        "hwnd": int(snap.hwnd),
        "style": int(snap.style),
        "ex_style": int(snap.ex_style),
        "placement": {
            "flags": int(wp.flags),
            "showCmd": int(wp.showCmd),
            "ptMinPosition": [int(wp.ptMinPosition.x), int(wp.ptMinPosition.y)],
            "ptMaxPosition": [int(wp.ptMaxPosition.x), int(wp.ptMaxPosition.y)],
            "rcNormalPosition": [
                int(wp.rcNormalPosition.left), int(wp.rcNormalPosition.top),
                int(wp.rcNormalPosition.right), int(wp.rcNormalPosition.bottom),
            ],
        },
        "rect": [int(snap.rect.left), int(snap.rect.top), int(snap.rect.right), int(snap.rect.bottom)],
        "client_size": list(snap.client_size) if snap.client_size is not None else None,
    }


def _snapshot_from_dict(data: dict) -> WindowSnapshot | None:
    try:
        wp_data = data["placement"]
        wp = WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(WINDOWPLACEMENT)
        wp.flags = int(wp_data["flags"])
        wp.showCmd = int(wp_data["showCmd"])
        wp.ptMinPosition = POINT(int(wp_data["ptMinPosition"][0]), int(wp_data["ptMinPosition"][1]))
        wp.ptMaxPosition = POINT(int(wp_data["ptMaxPosition"][0]), int(wp_data["ptMaxPosition"][1]))
        rc_norm = wp_data["rcNormalPosition"]
        wp.rcNormalPosition = RECT(int(rc_norm[0]), int(rc_norm[1]), int(rc_norm[2]), int(rc_norm[3]))
        r = data["rect"]
        rect = RECT(int(r[0]), int(r[1]), int(r[2]), int(r[3]))
        cs = data.get("client_size")
        client_size = (int(cs[0]), int(cs[1])) if isinstance(cs, (list, tuple)) and len(cs) == 2 else None
        return WindowSnapshot(
            hwnd=int(data["hwnd"]),
            style=int(data["style"]),
            ex_style=int(data["ex_style"]),
            placement=wp,
            rect=rect,
            client_size=client_size,
        )
    except Exception:
        return None


def _work_area_safe_rect(mi: MONITORINFO, *, pad: int = 4) -> tuple[int, int, int, int]:
    """Return rcWork inset by pad on edges carrying a taskbar/dock band."""
    pad = max(0, int(pad))
    mon = mi.rcMonitor
    work = mi.rcWork
    left = int(work.left) + pad if work.left > mon.left else int(work.left)
    top = int(work.top) + pad if work.top > mon.top else int(work.top)
    right = int(work.right) - pad if work.right < mon.right else int(work.right)
    bottom = int(work.bottom) - pad if work.bottom < mon.bottom else int(work.bottom)
    return left, top, right, bottom


def _geometry_candidates(
    u32, hwnd: int, snap: WindowSnapshot, *, fit_work_area: bool = False
) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """Return prioritized borderless 16:9 (size, pos) candidates for hwnd."""
    if snap.client_size is None:
        return []
    cw, ch = snap.client_size
    if cw <= 0 or ch <= 0:
        return []
    with _per_monitor_dpi(u32):
        mi = _monitor_info(u32, hwnd)
        if mi is None:
            return []
    mon_left, mon_top = int(mi.rcMonitor.left), int(mi.rcMonitor.top)
    mon_right, mon_bottom = int(mi.rcMonitor.right), int(mi.rcMonitor.bottom)
    mon_w, mon_h = mon_right - mon_left, mon_bottom - mon_top
    work_left, work_top = int(mi.rcWork.left), int(mi.rcWork.top)
    work_right, work_bottom = int(mi.rcWork.right), int(mi.rcWork.bottom)
    work_w, work_h = work_right - work_left, work_bottom - work_top
    safe_left, safe_top, safe_right, safe_bottom = _work_area_safe_rect(mi, pad=4)
    safe_w, safe_h = safe_right - safe_left, safe_bottom - safe_top

    max_k = min(mon_w // 16, mon_h // 9)
    if fit_work_area and safe_w > 0 and safe_h > 0:
        safe_max_k = min(safe_w // 16, safe_h // 9)
        if safe_max_k >= 40:
            max_k = min(max_k, safe_max_k)
    if max_k < 40:
        return []
    min_k = min(80, max_k)
    win_w = int(snap.rect.right - snap.rect.left)
    win_h = int(snap.rect.bottom - snap.rect.top)

    if cw * 9 <= ch * 16:
        k = max(min_k, min(max_k, round(cw / 16)))
    elif work_h > 0 and win_h >= work_h - 24 and round(cw / 16) <= max_k:
        k = max(min_k, min(max_k, round(cw / 16)))
    else:
        k = max(min_k, min(max_k, round(ch / 9)))

    cx = (int(snap.rect.left) + int(snap.rect.right)) // 2 if win_w > 0 else (mon_left + mon_right) // 2
    cy = (int(snap.rect.top) + int(snap.rect.bottom)) // 2 if win_h > 0 else (mon_top + mon_bottom) // 2

    def _place(k_val: int) -> tuple[tuple[int, int], tuple[int, int]]:
        tw, th = 16 * k_val, 9 * k_val
        wx = cx - tw // 2
        wy = cy - th // 2
        if fit_work_area and tw <= safe_w and th <= safe_h:
            wx = max(safe_left, min(wx, safe_right - tw))
            wy = max(safe_top, min(wy, safe_bottom - th))
        elif tw <= work_w and th <= work_h:
            wx = max(work_left, min(wx, work_right - tw))
            wy = max(work_top, min(wy, work_bottom - th))
        else:
            wx = max(mon_left, min(wx, mon_right - tw))
            wy = max(mon_top, min(wy, mon_bottom - th))
        return (tw, th), (wx, wy)

    candidates = [_place(k)]
    work_k = min(work_w // 16, work_h // 9)
    if work_k >= 40 and work_k < k and (16 * k > work_w or 9 * k > work_h):
        candidates.append(_place(work_k))
    return candidates


def closest_16_9_geometry(
    u32, hwnd: int, snap: WindowSnapshot
) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """Compute the closest physical 16:9 size and unclipped monitor position for hwnd."""
    candidates = _geometry_candidates(u32, hwnd, snap)
    return candidates[0] if candidates else None


def heal_16_9_window(controller=None, *, hwnd: int | None = None, context=None):
    """Repair a windowed 16:9 game window whose physical client size is off by border/DPI rounding."""
    if (TALL_CAPTURE_WIDTH, TALL_CAPTURE_HEIGHT) in ALLOWED_FRAME_SIZES:
        return None
    u32 = _user32()
    target_hwnd = int(hwnd) if hwnd else _resolve_hwnd(controller=controller)
    if not target_hwnd:
        return None
    if is_resolution_adjustment_blocked(target_hwnd):
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
        dpi = 96
        try:
            queried_dpi = int(u32.GetDpiForWindow(target_hwnd))
            if queried_dpi > 0:
                dpi = queried_dpi
        except Exception:
            pass
        # Allow up to 4 logical pixels of Win32 border / DPI / swapchain rounding,
        # never attempting to expand past the monitor's usable work area.
        max_px = max(4, round(4 * dpi / 96))
        target_cw = round(ch * 16 / 9)
        target_ch = round(cw * 9 / 16)
        wrect = RECT()
        if not u32.GetWindowRect(target_hwnd, ctypes.byref(wrect)):
            return None
        win_w = int(wrect.right - wrect.left)
        win_h = int(wrect.bottom - wrect.top)
        work_w = work_h = 0
        mi = _monitor_info(u32, target_hwnd)
        if mi is not None:
            work_w = int(mi.rcWork.right - mi.rcWork.left)
            work_h = int(mi.rcWork.bottom - mi.rcWork.top)
        dw = dh = 0
        cand_dw = target_cw - cw
        cand_dh = target_ch - ch
        can_dw = (
            1 <= abs(cand_dw) <= max_px
            and (work_w <= 0 or win_w + cand_dw <= work_w)
            and (cw < BASE_CAPTURE_WIDTH or cw + cand_dw >= BASE_CAPTURE_WIDTH)
        )
        can_dh = (
            1 <= abs(cand_dh) <= max_px
            and (work_h <= 0 or win_h + cand_dh <= work_h)
            and (ch < BASE_CAPTURE_HEIGHT or ch + cand_dh >= BASE_CAPTURE_HEIGHT)
        )
        if can_dw and (not can_dh or abs(cand_dw) <= abs(cand_dh)):
            dw = cand_dw
        elif can_dh:
            dh = cand_dh
        w = win_w + dw
        h = win_h + dh
        new_left = int(wrect.left)
        new_top = int(wrect.top)
        client_box = _client_screen_rect(u32, target_hwnd)
        if mi is not None and client_box is not None and 0 < w <= work_w and 0 < h <= work_h:
            c_left, c_top, c_right, c_bottom = client_box
            mon_w = int(mi.rcMonitor.right - mi.rcMonitor.left)
            mon_h = int(mi.rcMonitor.bottom - mi.rcMonitor.top)
            is_fullscreen_fg = (
                (c_right - c_left) <= mon_w + 2
                and (c_bottom - c_top) <= mon_h + 2
                and c_left <= int(mi.rcMonitor.left)
                and c_top <= int(mi.rcMonitor.top)
                and c_right >= int(mi.rcMonitor.right)
                and c_bottom >= int(mi.rcMonitor.bottom)
                and int(u32.GetForegroundWindow() or 0) == int(target_hwnd)
            )
            past_monitor = (
                c_right + dw > int(mi.rcMonitor.right)
                or c_bottom + dh > int(mi.rcMonitor.bottom)
                or c_left < int(mi.rcMonitor.left)
                or c_top < int(mi.rcMonitor.top)
            )
            past_work = (
                not is_fullscreen_fg
                and (dw or dh or is_auto_adjust_resolution_enabled(context))
                and (
                    c_right + dw > int(mi.rcWork.right)
                    or c_bottom + dh > int(mi.rcWork.bottom)
                    or c_left < int(mi.rcWork.left)
                    or c_top < int(mi.rcWork.top)
                )
            )
            if past_monitor or past_work:
                shifted_x = max(int(mi.rcWork.left), min(new_left, int(mi.rcWork.right) - w))
                shifted_y = max(int(mi.rcWork.top), min(new_top, int(mi.rcWork.bottom) - h))
                if shifted_x != new_left or shifted_y != new_top:
                    new_left, new_top = shifted_x, shifted_y
        if (dw or dh or new_left != int(wrect.left) or new_top != int(wrect.top)) and w > 0 and h > 0:
            resized = bool(
                u32.SetWindowPos(
                    target_hwnd, 0, new_left, new_top, w, h, SWP_RECT_FLAGS
                )
            )
    if not resized or controller is None:
        return None
    for _ in range(8):
        time.sleep(0.1)
        frame = _poll_capture(controller)
        if frame is not None and frame.shape[:2] == (720, 1280) and not is_clipped_game_frame(frame):
            return frame
    return None


def is_vertically_stretched_tall_frame(frame, reference_16_9=None) -> bool:
    """Detect a 16:9 swapchain frame stretched vertically into a 1:4 tall capture.

    Some display/driver configurations resize the Win32 window to 1440x5760
    without recreating the Unreal swapchain at 1:4, causing Windows/DXGI to
    stretch the existing 16:9 frame ~7.11x vertically. When that happens:
    1. Squeezing the tall frame back to 1280x720 closely matches the pre-resize
       16:9 reference while the unstretched top 80 px header no longer matches.
    2. Even without a reference, 7.11x vertical smearing suppresses vertical
       gradient energy so mean(|dx|) / mean(|dy|) in the top 720 px jumps to
       ~3.8-4.3 (vs ~0.89-1.32 on genuine 1:1 tall captures).
    """
    if frame is None or getattr(frame, "ndim", 0) < 2:
        return False
    h, w = int(frame.shape[0]), int(frame.shape[1])
    if h <= w * 2 or w < 320:
        return False
    import cv2
    import numpy as np

    if (
        reference_16_9 is not None
        and getattr(reference_16_9, "ndim", 0) >= 2
        and reference_16_9.shape[:2] == (BASE_CAPTURE_HEIGHT, BASE_CAPTURE_WIDTH)
    ):
        ref_gray = cv2.cvtColor(reference_16_9, cv2.COLOR_BGR2GRAY)
        ref_small = cv2.resize(ref_gray, (320, 180), interpolation=cv2.INTER_AREA)
        sq_small = cv2.resize(
            cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (320, 180), interpolation=cv2.INTER_AREA
        )
        if float(ref_small.std()) > 5.0 and float(sq_small.std()) > 5.0:
            sq_corr = float(cv2.matchTemplate(ref_small, sq_small, cv2.TM_CCOEFF_NORMED)[0, 0])
            ref_top = cv2.resize(ref_gray[:80, :], (320, 20), interpolation=cv2.INTER_AREA)
            frm_top = cv2.resize(
                cv2.cvtColor(frame[:80, :], cv2.COLOR_BGR2GRAY), (320, 20), interpolation=cv2.INTER_AREA
            )
            top_corr = (
                float(cv2.matchTemplate(ref_top, frm_top, cv2.TM_CCOEFF_NORMED)[0, 0])
                if float(ref_top.std()) > 3.0 and float(frm_top.std()) > 3.0
                else 0.0
            )
            if sq_corr >= 0.72 and top_corr < 0.45:
                return True

    full_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)
    top_gray = full_gray[: min(h, BASE_CAPTURE_HEIGHT), :]
    dx_top = float(np.abs(np.diff(top_gray, axis=1)).mean())
    dy_top = float(np.abs(np.diff(top_gray, axis=0)).mean())
    dx_full = float(np.abs(np.diff(full_gray, axis=1)).mean())
    dy_full = float(np.abs(np.diff(full_gray, axis=0)).mean())
    return (
        dx_full >= 1.5
        and (dx_top / max(dy_top, 1e-6)) >= 2.4
        and (dx_full / max(dy_full, 1e-6)) >= 2.4
    )


def _apply_borderless_resolution(
    u32,
    hwnd: int,
    *,
    snap: WindowSnapshot,
    target_physical_size: tuple[int, int],
    target_capture_size: tuple[int, int],
    target_short_side: int,
    win_pos: tuple[int, int],
    controller=None,
    stopped_fn=None,
    require_unclipped: bool = True,
    allow_solid_frame: bool = False,
    allow_unminimize: bool = True,
    reference_16_9=None,
):
    """Switch hwnd to a borderless target physical size and verify MaaFramework capture size."""
    target_w, target_h = int(target_physical_size[0]), int(target_physical_size[1])
    cap_w, cap_h = int(target_capture_size[0]), int(target_capture_size[1])
    win_x, win_y = int(win_pos[0]), int(win_pos[1])
    window_modified = False

    with _per_monitor_dpi(u32):
        if not u32.IsWindow(hwnd):
            return False, None, False
        if u32.IsIconic(hwnd):
            if not allow_unminimize:
                return False, None, False
            window_modified = True
            u32.ShowWindow(hwnd, SW_RESTORE)
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline and u32.IsIconic(hwnd):
                time.sleep(0.03)
        borderless_style = ctypes.c_ssize_t(
            (snap.style & ~(WS_BORDERLESS_MASK | WS_MAXIMIZE)) | WS_POPUP
        ).value
        borderless_ex_style = ctypes.c_ssize_t(snap.ex_style & ~WS_EX_BORDERLESS_MASK).value
        req_w, req_h = target_w, target_h
        if u32.IsZoomed(hwnd):
            window_modified = True
            fg = int(u32.GetForegroundWindow() or 0)
            u32.SetWindowLongPtrW(hwnd, GWL_STYLE, borderless_style)
            u32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, borderless_ex_style)
            wp = WINDOWPLACEMENT()
            wp.length = ctypes.sizeof(WINDOWPLACEMENT)
            if u32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
                wp.showCmd = SW_SHOWNORMAL if fg == hwnd else SW_SHOWNOACTIVATE
                wp.rcNormalPosition = RECT(win_x, win_y, win_x + req_w, win_y + req_h)
                u32.SetWindowPlacement(hwnd, ctypes.byref(wp))
            if u32.IsZoomed(hwnd):
                u32.ShowWindow(hwnd, SW_RESTORE if fg == hwnd else SW_SHOWNOACTIVATE)
                deadline = time.monotonic() + 1.5
                while time.monotonic() < deadline and u32.IsZoomed(hwnd):
                    time.sleep(0.03)
        else:
            window_modified = True
            u32.SetWindowLongPtrW(hwnd, GWL_STYLE, borderless_style)
            u32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, borderless_ex_style)
        if not u32.SetWindowPos(hwnd, 0, win_x, win_y, req_w, req_h, SWP_RESIZE_FLAGS):
            return False, None, window_modified

        deadline = time.monotonic() + 4.0
        stable_since = None
        mismatch_since = None
        last_csize = None
        last_wsize = None
        inset_adjustments = 0
        resized_ok = False
        while time.monotonic() < deadline:
            if (stopped_fn is not None and stopped_fn()) or not u32.IsWindow(hwnd):
                break
            csize = _client_size(u32, hwnd)
            wrect = RECT()
            wsize = (
                (int(wrect.right - wrect.left), int(wrect.bottom - wrect.top))
                if u32.GetWindowRect(hwnd, ctypes.byref(wrect))
                else None
            )
            now = time.monotonic()
            if csize == (target_w, target_h):
                mismatch_since = None
                if stable_since is None:
                    stable_since = now
                elif now - stable_since >= 0.25:
                    resized_ok = True
                    break
            else:
                stable_since = None
                if csize is not None and csize == last_csize and wsize == last_wsize:
                    if mismatch_since is None:
                        mismatch_since = now
                    elif (
                        inset_adjustments < 2
                        and now - mismatch_since >= 0.22
                        and wsize is not None
                        and (wsize == (req_w, req_h) or now - mismatch_since >= 0.45)
                    ):
                        cw, ch = csize
                        nc_w = wsize[0] - cw
                        nc_h = wsize[1] - ch
                        if 0 <= nc_w <= 64 and 0 <= nc_h <= 64:
                            desired_w = target_w + nc_w
                            desired_h = target_h + nc_h
                            if (desired_w, desired_h) != (req_w, req_h):
                                inset_adjustments += 1
                                req_w, req_h = desired_w, desired_h
                                u32.SetWindowPos(hwnd, 0, win_x, win_y, req_w, req_h, SWP_RESIZE_FLAGS)
                                mismatch_since = None
                                last_csize = last_wsize = None
                else:
                    last_csize = csize
                    last_wsize = wsize
                    mismatch_since = now
            time.sleep(0.04)

    if not resized_ok:
        return False, None, window_modified

    if (cap_w, cap_h) != (BASE_CAPTURE_WIDTH, BASE_CAPTURE_HEIGHT):
        ALLOWED_FRAME_SIZES.add((cap_w, cap_h))
    if controller is None:
        return True, None, window_modified

    try:
        controller.set_screenshot_target_short_side(int(target_short_side))
    except Exception:
        pass

    expected_w, expected_h = target_w, target_h
    nudged_capture = False
    settled_streak = 0
    solid_streak = 0
    clamped_streak = 0
    stretched_streak = 0
    csize_mismatch_streak = 0
    off_shape = None
    off_streak = 0
    for _ in range(16):
        if stopped_fn is not None and stopped_fn():
            break
        time.sleep(0.12)
        frame = _poll_capture(controller, stopped_fn)
        if frame is None:
            settled_streak = 0
            solid_streak = 0
            continue
        with _per_monitor_dpi(u32):
            csize = _client_size(u32, hwnd) if u32.IsWindow(hwnd) else None
        tol = 2 if nudged_capture else 0
        csize_ok = (
            csize is not None
            and abs(csize[0] - expected_w) <= tol
            and abs(csize[1] - expected_h) <= tol
        )
        if not csize_ok:
            settled_streak = 0
            solid_streak = 0
            clamped_streak = 0
            stretched_streak = 0
            off_shape = None
            off_streak = 0
            csize_mismatch_streak += 1
            if csize_mismatch_streak >= 4:
                break
            continue
        csize_mismatch_streak = 0
        if frame.shape[:2] == (cap_h, cap_w) and (not require_unclipped or not is_clipped_game_frame(frame)):
            off_shape = None
            off_streak = 0
            if cap_h > cap_w:
                top_band = frame[: max(1, cap_w * 9 // 16), :]
                bottom_band = frame[cap_h * 3 // 4 :, :]
                if (
                    float(frame.mean()) > 8.0
                    and float(frame.std()) > 5.0
                    and float(bottom_band.mean()) > 5.0
                    and float(bottom_band.std()) > 3.0
                ):
                    clamped_streak = 0
                    if is_vertically_stretched_tall_frame(frame, reference_16_9):
                        settled_streak = 0
                        stretched_streak += 1
                        if stretched_streak >= 6:
                            break
                    else:
                        stretched_streak = 0
                        settled_streak += 1
                        if settled_streak >= 2:
                            return True, frame, window_modified
                else:
                    settled_streak = 0
                    stretched_streak = 0
                    if (
                        float(top_band.mean()) > 8.0
                        and float(bottom_band.mean()) < 1.0
                        and float(bottom_band.std()) < 1.0
                    ):
                        clamped_streak += 1
                        if clamped_streak >= 4:
                            break
                    else:
                        clamped_streak = 0
            elif float(frame.mean()) > 8.0 and float(frame.std()) > 5.0:
                solid_streak = 0
                settled_streak += 1
                if settled_streak >= 2:
                    return True, frame, window_modified
            elif allow_solid_frame and cap_h <= cap_w:
                settled_streak = 0
                solid_streak += 1
                if solid_streak >= 4:
                    return True, frame, window_modified
            else:
                settled_streak = 0
                solid_streak = 0
        else:
            settled_streak = 0
            solid_streak = 0
            clamped_streak = 0
            stretched_streak = 0
            shape_now = frame.shape[:2]
            is_small_delta = (
                (shape_now[1] == cap_w and 1 <= abs(shape_now[0] - cap_h) <= 2)
                or (shape_now[0] == cap_h and 1 <= abs(shape_now[1] - cap_w) <= 2)
            )
            if is_small_delta and shape_now == off_shape:
                off_streak += 1
            elif is_small_delta:
                off_shape = shape_now
                off_streak = 1
            else:
                off_shape = None
                off_streak = 0
            if not nudged_capture and off_streak >= 3:
                if shape_now[1] == cap_w and 1 <= abs(shape_now[0] - cap_h) <= 2:
                    nudged_capture = True
                    dh = round((cap_h - shape_now[0]) * req_w / cap_w)
                    if dh:
                        req_h += dh
                        expected_h += dh
                        with _per_monitor_dpi(u32):
                            u32.SetWindowPos(hwnd, 0, win_x, win_y, req_w, req_h, SWP_RESIZE_FLAGS)
                        time.sleep(0.20)
                elif shape_now[0] == cap_h and 1 <= abs(shape_now[1] - cap_w) <= 2:
                    nudged_capture = True
                    dw = round((cap_w - shape_now[1]) * req_h / cap_h)
                    if dw:
                        req_w += dw
                        expected_w += dw
                        with _per_monitor_dpi(u32):
                            u32.SetWindowPos(hwnd, 0, win_x, win_y, req_w, req_h, SWP_RESIZE_FLAGS)
                        time.sleep(0.20)
                off_shape = None
                off_streak = 0

    return False, None, window_modified


_SESSION_LOCK = Lock()
_SESSION_16_9_SNAPSHOT: dict[int, WindowSnapshot] = {}
_SESSION_NOTIFIED: set[int] = set()
_HWND_STATE: dict[int, dict] = {}
_TALL_FAILED: dict[int, list[float]] = {}
_ATEXIT_REGISTERED = False


def _window_state(hwnd: int) -> dict:
    return _HWND_STATE.setdefault(
        int(hwnd),
        {
            "adj_times": [],
            "fail_times": [],
            "failed_sizes": set(),
            "blocked": False,
            "blocked_notified": False,
        },
    )


def _emit_resolution_notice(notify, msg: str, *, write_log: bool = True) -> None:
    if notify is None or write_log:
        write_message(msg, msg)
    if notify is not None:
        try:
            notify(msg)
        except Exception:
            pass


def is_resolution_adjustment_blocked(hwnd: int | None = None) -> bool:
    """Return True when repeated failures or engine resets tripped the anti-oscillation guard."""
    target = int(hwnd) if hwnd else _resolve_hwnd()
    if not target:
        return False
    with _SESSION_LOCK:
        state = _HWND_STATE.get(target)
        return bool(state and state.get("blocked"))


def _ensure_atexit_registered():
    global _ATEXIT_REGISTERED
    if not _ATEXIT_REGISTERED:
        _ATEXIT_REGISTERED = True
        atexit.register(restore_session_16_9_windows)


def adopt_session_16_9_handoff():
    """Adopt a pre-StartGame window snapshot handed off by the startup pretask."""
    with _SESSION_LOCK:
        if not HANDOFF_FILE.is_file():
            return
        try:
            payload = json.loads(HANDOFF_FILE.read_text(encoding="utf-8-sig"))
        except Exception:
            payload = None
        try:
            HANDOFF_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        if not isinstance(payload, dict):
            return
        try:
            if abs(time.time() - float(payload.get("saved_at", 0))) > 300.0:
                return
        except Exception:
            return
        u32 = _user32()
        for item in payload.get("windows", []):
            if not isinstance(item, dict):
                continue
            snap = _snapshot_from_dict(item)
            if snap is not None and u32.IsWindow(snap.hwnd):
                _SESSION_16_9_SNAPSHOT.setdefault(snap.hwnd, snap)
                _SESSION_NOTIFIED.add(snap.hwnd)
        if _SESSION_16_9_SNAPSHOT:
            _ensure_atexit_registered()


def persist_session_16_9_handoff():
    """Persist adjusted window snapshots for the main Agent batch and skip local restore."""
    with _SESSION_LOCK:
        if not _SESSION_16_9_SNAPSHOT:
            return
        u32 = _user32()
        live = [
            _snapshot_to_dict(snap)
            for hwnd, snap in _SESSION_16_9_SNAPSHOT.items()
            if u32.IsWindow(hwnd)
        ]
        _SESSION_16_9_SNAPSHOT.clear()
        _SESSION_NOTIFIED.clear()
        _HWND_STATE.clear()
        _TALL_FAILED.clear()
    if live:
        try:
            HANDOFF_FILE.parent.mkdir(parents=True, exist_ok=True)
            write_json(HANDOFF_FILE, {"saved_at": time.time(), "windows": live})
        except Exception:
            pass


def restore_session_16_9_windows():
    """Restore all game windows auto-adjusted to 16:9 during the current task session."""
    with _SESSION_LOCK:
        snapshots = list(_SESSION_16_9_SNAPSHOT.values())
        _SESSION_16_9_SNAPSHOT.clear()
        _SESSION_NOTIFIED.clear()
        _HWND_STATE.clear()
        _TALL_FAILED.clear()
    if not snapshots:
        return
    u32 = _user32()
    for snap in snapshots:
        try:
            _restore_snapshot(u32, snap)
        except Exception:
            pass


def _is_already_borderless_in_monitor(
    u32,
    hwnd: int,
    snap: WindowSnapshot,
    target_size: tuple[int, int],
    *,
    require_work_area: bool = False,
) -> bool:
    if (snap.style & WS_BORDERLESS_MASK) != 0 or (snap.ex_style & WS_EX_BORDERLESS_MASK) != 0:
        return False
    if snap.client_size != target_size:
        return False
    with _per_monitor_dpi(u32):
        if u32.IsIconic(hwnd) or u32.IsZoomed(hwnd):
            return False
        mi = _monitor_info(u32, hwnd)
        cbox = _client_screen_rect(u32, hwnd)
        if mi is None or cbox is None:
            return False
        if require_work_area:
            safe_left, safe_top, safe_right, safe_bottom = _work_area_safe_rect(mi, pad=4)
            if (
                cbox[0] < safe_left
                or cbox[1] < safe_top
                or cbox[2] > safe_right
                or cbox[3] > safe_bottom
            ):
                return False
        extends_past_work = (
            cbox[0] < int(mi.rcWork.left)
            or cbox[1] < int(mi.rcWork.top)
            or cbox[2] > int(mi.rcWork.right)
            or cbox[3] > int(mi.rcWork.bottom)
        )
        if extends_past_work and (snap.style & WS_POPUP) == 0:
            return False
        return (
            cbox[0] >= int(mi.rcMonitor.left)
            and cbox[1] >= int(mi.rcMonitor.top)
            and cbox[2] <= int(mi.rcMonitor.right)
            and cbox[3] <= int(mi.rcMonitor.bottom)
        )


def _poll_valid_16_9_frame(controller, *, stopped_fn=None, timeout: float = 0.9):
    """Poll captures briefly for a settled unclipped 1280x720 frame."""
    if controller is None:
        return None
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        if stopped_fn is not None and stopped_fn():
            return None
        frame = _poll_capture(controller, stopped_fn=stopped_fn)
        if (
            frame is not None
            and frame.shape[:2] == (BASE_CAPTURE_HEIGHT, BASE_CAPTURE_WIDTH)
            and not is_clipped_game_frame(frame)
        ):
            return frame
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(0.08, remaining))


def ensure_16_9_window(
    controller=None,
    *,
    context=None,
    hwnd: int | None = None,
    notify=None,
    write_log: bool = True,
    fit_work_area: bool = False,
):
    """Ensure the game window renders an unclipped 16:9 frame, adjusting resolution when enabled."""
    if (TALL_CAPTURE_WIDTH, TALL_CAPTURE_HEIGHT) in ALLOWED_FRAME_SIZES:
        return None
    target_hwnd = int(hwnd) if hwnd else _resolve_hwnd(controller=controller)
    if not target_hwnd:
        return None
    if is_resolution_adjustment_blocked(target_hwnd):
        return None
    healed = heal_16_9_window(controller, hwnd=target_hwnd, context=context)
    if healed is not None:
        return healed
    if not is_auto_adjust_resolution_enabled(context):
        return None

    u32 = _user32()
    session = active_session()
    stopped_fn = session.stopped if session else None
    in_bg = bool(session and (
        session._background_active() if hasattr(session, "_background_active")
        else getattr(session, "_background_observation", False)
    ))
    with _per_monitor_dpi(u32):
        if not u32.IsWindow(target_hwnd) or (in_bg and u32.IsIconic(target_hwnd)):
            return None

    adopt_session_16_9_handoff()
    waited_settle = False
    while True:
        current_snap = _capture_snapshot(u32, target_hwnd)
        if current_snap is None or current_snap.client_size is None:
            return None

        candidates = _geometry_candidates(
            u32, target_hwnd, current_snap, fit_work_area=fit_work_area
        )
        if not candidates:
            return None

        now = time.monotonic()
        settle_wait = 0.0
        oscillation_warn = False
        with _SESSION_LOCK:
            for dead in [h for h in _HWND_STATE if h != target_hwnd and not u32.IsWindow(h)]:
                _HWND_STATE.pop(dead, None)
            state = _window_state(target_hwnd)
            if state["blocked"]:
                return None
            state["fail_times"] = [t for t in state["fail_times"] if now - t < OSCILLATION_WINDOW_SECONDS]
            state["adj_times"] = [t for t in state["adj_times"] if now - t < OSCILLATION_WINDOW_SECONDS]
            if state["fail_times"] and now - state["fail_times"][-1] < FAILED_RETRY_COOLDOWN_SECONDS:
                return None
            already_adjusted = target_hwnd in _SESSION_16_9_SNAPSHOT
            if already_adjusted and state["adj_times"]:
                since_adj = now - state["adj_times"][-1]
                if since_adj < READJUST_SETTLE_SECONDS and not waited_settle:
                    if controller is None:
                        return None
                    settle_wait = READJUST_SETTLE_SECONDS - since_adj
                elif len(state["adj_times"]) >= MAX_RECENT_ADJUSTMENTS:
                    state["blocked"] = True
                    if not state["blocked_notified"]:
                        state["blocked_notified"] = True
                        oscillation_warn = True
            failed_sizes = set(state["failed_sizes"])
        if settle_wait > 0.0:
            settled = _poll_valid_16_9_frame(
                controller,
                stopped_fn=stopped_fn,
                timeout=settle_wait,
            )
            if settled is not None:
                return settled
            waited_settle = True
            continue
        if oscillation_warn:
            _emit_resolution_notice(
                notify,
                "检测到游戏窗口反复恢复原分辨率（可能处于独占全屏或受系统缩放限制），"
                "已暂停自动调整以避免窗口跳动；请在游戏设置中使用无边框或 16:9 窗口模式。",
                write_log=write_log,
            )
            return None
        break

    usable = [c for c in candidates if c[0] not in failed_sizes] or [candidates[-1]]
    if _is_already_borderless_in_monitor(
        u32, target_hwnd, current_snap, usable[0][0], require_work_area=fit_work_area
    ):
        return _poll_valid_16_9_frame(controller, stopped_fn=stopped_fn, timeout=0.9)

    with _SESSION_LOCK:
        newly_recorded = target_hwnd not in _SESSION_16_9_SNAPSHOT
        if newly_recorded:
            _SESSION_16_9_SNAPSHOT[target_hwnd] = current_snap
            _ensure_atexit_registered()
        baseline_snap = _SESSION_16_9_SNAPSHOT[target_hwnd]

    ok = False
    frame = None
    modified = False
    target_w, target_h = usable[0][0]
    for (cand_w, cand_h), (cand_x, cand_y) in usable:
        ok, frame, modified = _apply_borderless_resolution(
            u32,
            target_hwnd,
            snap=current_snap,
            target_physical_size=(cand_w, cand_h),
            target_capture_size=(BASE_CAPTURE_WIDTH, BASE_CAPTURE_HEIGHT),
            target_short_side=BASE_CAPTURE_HEIGHT,
            win_pos=(cand_x, cand_y),
            controller=controller,
            stopped_fn=stopped_fn,
            require_unclipped=True,
            allow_solid_frame=True,
            allow_unminimize=not in_bg,
        )
        if ok and (controller is None or frame is not None):
            target_w, target_h = cand_w, cand_h
            break
        with _SESSION_LOCK:
            _window_state(target_hwnd)["failed_sizes"].add((cand_w, cand_h))

    if not ok or (controller is not None and frame is None):
        warn_blocked = False
        with _SESSION_LOCK:
            state = _window_state(target_hwnd)
            state["fail_times"].append(time.monotonic())
            if len(state["fail_times"]) >= MAX_FAILED_ATTEMPTS:
                state["blocked"] = True
                if not state["blocked_notified"]:
                    state["blocked_notified"] = True
                    warn_blocked = True
            if newly_recorded:
                _SESSION_16_9_SNAPSHOT.pop(target_hwnd, None)
        if modified:
            _restore_snapshot(u32, current_snap, controller=controller)
        if warn_blocked:
            _emit_resolution_notice(
                notify,
                "自动调整游戏分辨率多次未生效（可能处于独占全屏或受系统缩放限制），"
                "已暂停自动调整以避免窗口反复跳动。",
                write_log=write_log,
            )
        return None

    should_notify = False
    with _SESSION_LOCK:
        state = _window_state(target_hwnd)
        state["fail_times"].clear()
        state["adj_times"].append(time.monotonic())
        if target_hwnd not in _SESSION_NOTIFIED:
            _SESSION_NOTIFIED.add(target_hwnd)
            should_notify = True

    if should_notify:
        orig_cs = baseline_snap.client_size or current_snap.client_size
        if orig_cs == (target_w, target_h):
            msg = (
                f"已自动将游戏窗口切换为 {target_w}×{target_h} 无边框 16:9 显示"
                "以避免画面超出屏幕，任务结束后将自动恢复。"
            )
        else:
            msg = (
                f"已自动将游戏窗口从 {orig_cs[0]}×{orig_cs[1]} 调整为最相近的 16:9 分辨率"
                f"（{target_w}×{target_h}），任务结束后将自动恢复。"
            )
        _emit_resolution_notice(notify, msg, write_log=write_log)
    return frame



@contextmanager
def temporary_window_resize(
    rt,
    *,
    target_physical_size: tuple[int, int],
    target_capture_size: tuple[int, int],
    target_short_side: int,
    win_pos: tuple[int, int],
    require_unclipped: bool = False,
    allow_solid_frame: bool = False,
    fail_message: str | None = None,
):
    """Temporarily switch the game window to a target borderless resolution and restore on exit."""
    u32 = _user32()
    hwnd = _resolve_hwnd(rt)
    if not hwnd:
        yield False
        return
    snap = _capture_snapshot(u32, hwnd)
    if snap is None:
        yield False
        return

    restored = False
    window_modified = False
    restored_frames_waited = False

    def _restore_win32():
        nonlocal restored
        if restored:
            return
        restored = True
        _restore_snapshot(
            u32,
            snap,
            controller=rt.controller,
            discard_capture_size=target_capture_size,
        )

    def _restore_and_wait():
        nonlocal restored_frames_waited
        _restore_win32()
        rt.invalidate_frame()
        if hasattr(rt, "set_capture_size"):
            rt.set_capture_size(None)
        if window_modified and not restored_frames_waited and not rt.stopped:
            restored_frames_waited = True
            time.sleep(0.25)
            rendered_streak = 0
            stopped_fn = lambda: bool(rt.stopped)
            for _ in range(20):
                if rt.stopped:
                    break
                time.sleep(0.12)
                frame = _poll_capture(rt.controller, stopped_fn)
                if (
                    frame is not None
                    and frame.shape[:2] == (BASE_CAPTURE_HEIGHT, BASE_CAPTURE_WIDTH)
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
        reference_16_9 = None
        if target_capture_size[1] > target_capture_size[0] * 2 and rt.controller is not None:
            cand_ref = _poll_capture(rt.controller, lambda: bool(rt.stopped))
            if cand_ref is not None and cand_ref.shape[:2] == (BASE_CAPTURE_HEIGHT, BASE_CAPTURE_WIDTH):
                reference_16_9 = cand_ref
        ok, _, window_modified = _apply_borderless_resolution(
            u32,
            hwnd,
            snap=snap,
            target_physical_size=target_physical_size,
            target_capture_size=target_capture_size,
            target_short_side=target_short_side,
            win_pos=win_pos,
            controller=rt.controller,
            stopped_fn=lambda: bool(rt.stopped),
            require_unclipped=require_unclipped,
            allow_solid_frame=allow_solid_frame,
            reference_16_9=reference_16_9,
        )
        rt.invalidate_frame()
        if hasattr(rt, "set_capture_size"):
            rt.set_capture_size(None)
        if not ok:
            _restore_and_wait()
            if fail_message:
                rt.log(fail_message, detail=True)
            yield False
            return
        if hasattr(rt, "set_capture_size"):
            rt.set_capture_size(target_capture_size)
        yield True
    finally:
        _restore_and_wait()
        try:
            atexit.unregister(_restore_win32)
        except Exception:
            pass



@contextmanager
def tall_window(rt):
    """Temporarily resize the game window to 1440x5760 (1280x5120 capture) and restore on exit."""
    if not is_auto_adjust_resolution_enabled(getattr(rt, "context", None)):
        yield False
        return
    u32 = _user32()
    hwnd = _resolve_hwnd(rt)
    if not hwnd or not u32.IsWindow(hwnd):
        yield False
        return
    now = time.monotonic()
    with _SESSION_LOCK:
        fails = [t for t in _TALL_FAILED.get(hwnd, ()) if now - t < 120.0]
        _TALL_FAILED[hwnd] = fails
        if len(fails) >= 2 or (fails and now - fails[-1] < TALL_FAILED_COOLDOWN_SECONDS):
            yield False
            return
    heal_16_9_window(hwnd=hwnd)
    with _per_monitor_dpi(u32):
        hmon = u32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not hmon or not u32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            yield False
            return
        mon_w = int(mi.rcMonitor.right - mi.rcMonitor.left)
        win_x = int(mi.rcMonitor.left) + (mon_w - TALL_PHYSICAL_WIDTH) // 2
        win_y = int(mi.rcMonitor.top)

    with temporary_window_resize(
        rt,
        target_physical_size=(TALL_PHYSICAL_WIDTH, TALL_PHYSICAL_HEIGHT),
        target_capture_size=(TALL_CAPTURE_WIDTH, TALL_CAPTURE_HEIGHT),
        target_short_side=TALL_CAPTURE_WIDTH,
        win_pos=(win_x, win_y),
        require_unclipped=False,
        allow_solid_frame=False,
        fail_message="超长竖屏切换未生效（请确认游戏非独占全屏模式），继续使用常规窗口读取。",
    ) as is_tall:
        if is_tall:
            with _SESSION_LOCK:
                _TALL_FAILED.pop(hwnd, None)
            park_pt = clamp_capture_park_point(
                hwnd,
                [640, 36],
                capture_size=(TALL_CAPTURE_WIDTH, TALL_CAPTURE_HEIGHT),
            )
            rt.action("TouchMove", target=park_pt)
            rt.pause(0.25)
        elif not rt.stopped:
            with _SESSION_LOCK:
                _TALL_FAILED.setdefault(hwnd, []).append(time.monotonic())
        yield is_tall
