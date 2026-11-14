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
WS_POPUP = 0x80000000
WS_BORDERLESS_MASK = 0x00CF0000
WS_EX_BORDERLESS_MASK = 0x00020301
SW_SHOWNOACTIVATE = 4
SW_RESTORE = 9
MONITOR_DEFAULTTONEAREST = 2
SWP_RESIZE_FLAGS = 0x4034  # SWP_NOACTIVATE | SWP_NOZORDER | SWP_FRAMECHANGED | SWP_ASYNCWINDOWPOS
SWP_STYLE_FLAGS = 0x0037   # SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED
SWP_RECT_FLAGS = 0x0034    # SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)

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


def _client_size(u32, hwnd: int) -> tuple[int, int] | None:
    rc = RECT()
    if not u32.GetClientRect(hwnd, ctypes.byref(rc)):
        return None
    return int(rc.right - rc.left), int(rc.bottom - rc.top)


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
        u32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_STYLE_FLAGS)
        u32.SetWindowPlacement(hwnd, ctypes.byref(snap.placement))
        if not u32.IsZoomed(hwnd):
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
                        if abs(dw) <= 16 and abs(dh) <= 16 and w + dw > 0 and h + dh > 0:
                            u32.SetWindowPos(
                                hwnd, 0, int(snap.rect.left), int(snap.rect.top),
                                w + dw, h + dh, SWP_RECT_FLAGS,
                            )


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


def closest_16_9_geometry(
    u32, hwnd: int, snap: WindowSnapshot
) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """Compute the closest physical 16:9 size and unclipped monitor position for hwnd."""
    if snap.client_size is None:
        return None
    cw, ch = snap.client_size
    if cw <= 0 or ch <= 0:
        return None
    with _per_monitor_dpi(u32):
        hmon = u32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not hmon or not u32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return None
    mon_left, mon_top = int(mi.rcMonitor.left), int(mi.rcMonitor.top)
    mon_right, mon_bottom = int(mi.rcMonitor.right), int(mi.rcMonitor.bottom)
    mon_w, mon_h = mon_right - mon_left, mon_bottom - mon_top
    work_left, work_top = int(mi.rcWork.left), int(mi.rcWork.top)
    work_right, work_bottom = int(mi.rcWork.right), int(mi.rcWork.bottom)
    work_w, work_h = work_right - work_left, work_bottom - work_top

    max_k = min(mon_w // 16, mon_h // 9)
    if max_k < 40:
        return None
    min_k = min(80, max_k)
    win_w = int(snap.rect.right - snap.rect.left)
    win_h = int(snap.rect.bottom - snap.rect.top)

    if cw * 9 <= ch * 16:
        k = max(min_k, min(max_k, round(cw / 16)))
    elif work_h > 0 and win_h >= work_h - 24 and round(cw / 16) <= max_k:
        k = max(min_k, min(max_k, round(cw / 16)))
    else:
        k = max(min_k, min(max_k, round(ch / 9)))

    target_w, target_h = 16 * k, 9 * k
    cx = (int(snap.rect.left) + int(snap.rect.right)) // 2 if win_w > 0 else (mon_left + mon_right) // 2
    cy = (int(snap.rect.top) + int(snap.rect.bottom)) // 2 if win_h > 0 else (mon_top + mon_bottom) // 2
    win_x = cx - target_w // 2
    win_y = cy - target_h // 2
    if target_w <= work_w and target_h <= work_h:
        win_x = max(work_left, min(win_x, work_right - target_w))
        win_y = max(work_top, min(win_y, work_bottom - target_h))
    else:
        win_x = max(mon_left, min(win_x, mon_right - target_w))
        win_y = max(mon_top, min(win_y, mon_bottom - target_h))
    return (target_w, target_h), (win_x, win_y)


def heal_16_9_window(controller=None, *, hwnd: int | None = None):
    """Repair a windowed 16:9 game window whose physical client size is off by border/DPI rounding."""
    if (TALL_CAPTURE_WIDTH, TALL_CAPTURE_HEIGHT) in ALLOWED_FRAME_SIZES:
        return None
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
        mi = None
        hmon = u32.MonitorFromWindow(target_hwnd, MONITOR_DEFAULTTONEAREST)
        if hmon:
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(MONITORINFO)
            if u32.GetMonitorInfoW(hmon, ctypes.byref(info)):
                mi = info
                work_w = int(mi.rcWork.right - mi.rcWork.left)
                work_h = int(mi.rcWork.bottom - mi.rcWork.top)
        dw = dh = 0
        cand_dw = target_cw - cw
        cand_dh = target_ch - ch
        if 1 <= abs(cand_dw) <= max_px and (work_w <= 0 or win_w + cand_dw <= work_w):
            dw = cand_dw
        elif 1 <= abs(cand_dh) <= max_px and (work_h <= 0 or win_h + cand_dh <= work_h):
            dh = cand_dh
        w = win_w + dw
        h = win_h + dh
        new_left = int(wrect.left)
        new_top = int(wrect.top)
        if mi is not None and 0 < w <= work_w and 0 < h <= work_h:
            if (
                int(wrect.right) > int(mi.rcMonitor.right)
                or int(wrect.bottom) > int(mi.rcMonitor.bottom)
                or new_left < int(mi.rcMonitor.left)
                or new_top < int(mi.rcMonitor.top)
            ):
                new_left = max(int(mi.rcWork.left), min(new_left, int(mi.rcWork.right) - w))
                new_top = max(int(mi.rcWork.top), min(new_top, int(mi.rcWork.bottom) - h))
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
        if u32.IsZoomed(hwnd):
            window_modified = True
            fg = int(u32.GetForegroundWindow() or 0)
            u32.ShowWindow(hwnd, SW_RESTORE if fg == hwnd else SW_SHOWNOACTIVATE)
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline and u32.IsZoomed(hwnd):
                time.sleep(0.03)

        borderless_style = ctypes.c_ssize_t((snap.style & ~WS_BORDERLESS_MASK) | WS_POPUP).value
        borderless_ex_style = ctypes.c_ssize_t(snap.ex_style & ~WS_EX_BORDERLESS_MASK).value
        window_modified = True
        u32.SetWindowLongPtrW(hwnd, GWL_STYLE, borderless_style)
        u32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, borderless_ex_style)
        req_w, req_h = target_w, target_h
        if not u32.SetWindowPos(hwnd, 0, win_x, win_y, req_w, req_h, SWP_RESIZE_FLAGS):
            return False, None, window_modified

        deadline = time.monotonic() + 4.0
        stable_since = None
        mismatch_since = None
        last_csize = None
        adjusted_delta = False
        resized_ok = False
        while time.monotonic() < deadline:
            if (stopped_fn is not None and stopped_fn()) or not u32.IsWindow(hwnd):
                break
            csize = _client_size(u32, hwnd)
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
                if csize is not None and csize == last_csize:
                    if mismatch_since is None:
                        mismatch_since = now
                    elif not adjusted_delta and now - mismatch_since >= 0.16:
                        cw, ch = csize
                        dw = target_w - cw
                        dh = target_h - ch
                        if abs(dw) <= 32 and abs(dh) <= 64:
                            adjusted_delta = True
                            req_w += dw
                            req_h += dh
                            u32.SetWindowPos(hwnd, 0, win_x, win_y, req_w, req_h, SWP_RESIZE_FLAGS)
                            mismatch_since = None
                else:
                    last_csize = csize
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

    nudged_capture = False
    solid_streak = 0
    for _ in range(16):
        if stopped_fn is not None and stopped_fn():
            break
        time.sleep(0.12)
        frame = _poll_capture(controller, stopped_fn)
        if frame is None:
            continue
        if frame.shape[:2] == (cap_h, cap_w) and (not require_unclipped or not is_clipped_game_frame(frame)):
            if float(frame.mean()) > 8.0 and float(frame.std()) > 5.0:
                return True, frame, window_modified
            if allow_solid_frame:
                solid_streak += 1
                if solid_streak >= 4:
                    return True, frame, window_modified
        else:
            solid_streak = 0
        if not nudged_capture:
            if frame.shape[1] == cap_w and 1 <= abs(frame.shape[0] - cap_h) <= 2:
                nudged_capture = True
                dh = round((cap_h - frame.shape[0]) * req_w / cap_w)
                if dh:
                    req_h += dh
                    with _per_monitor_dpi(u32):
                        u32.SetWindowPos(hwnd, 0, win_x, win_y, req_w, req_h, SWP_RESIZE_FLAGS)
            elif frame.shape[0] == cap_h and 1 <= abs(frame.shape[1] - cap_w) <= 2:
                nudged_capture = True
                dw = round((cap_w - frame.shape[1]) * req_h / cap_h)
                if dw:
                    req_w += dw
                    with _per_monitor_dpi(u32):
                        u32.SetWindowPos(hwnd, 0, win_x, win_y, req_w, req_h, SWP_RESIZE_FLAGS)

    return False, None, window_modified


_SESSION_LOCK = Lock()
_SESSION_16_9_SNAPSHOT: dict[int, WindowSnapshot] = {}
_SESSION_NOTIFIED: set[int] = set()
_LAST_FAILED_ATTEMPT: dict[tuple[int, tuple[int, int]], float] = {}
_ATEXIT_REGISTERED = False
_HANDOFF_ADOPTED = False


def _ensure_atexit_registered():
    global _ATEXIT_REGISTERED
    if not _ATEXIT_REGISTERED:
        _ATEXIT_REGISTERED = True
        atexit.register(restore_session_16_9_windows)


def adopt_session_16_9_handoff():
    """Adopt a pre-StartGame window snapshot handed off by the startup pretask."""
    global _HANDOFF_ADOPTED
    with _SESSION_LOCK:
        if _HANDOFF_ADOPTED:
            return
        _HANDOFF_ADOPTED = True
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
    if live:
        try:
            HANDOFF_FILE.parent.mkdir(parents=True, exist_ok=True)
            write_json(HANDOFF_FILE, {"saved_at": time.time(), "windows": live})
        except Exception:
            pass


def restore_session_16_9_windows():
    """Restore all game windows auto-adjusted to 16:9 during the current task session."""
    with _SESSION_LOCK:
        if not _SESSION_16_9_SNAPSHOT:
            return
        snapshots = list(_SESSION_16_9_SNAPSHOT.values())
        _SESSION_16_9_SNAPSHOT.clear()
        _SESSION_NOTIFIED.clear()
        _LAST_FAILED_ATTEMPT.clear()
    u32 = _user32()
    for snap in snapshots:
        try:
            _restore_snapshot(u32, snap)
        except Exception:
            pass


def ensure_16_9_window(controller=None, *, context=None, hwnd: int | None = None, notify=None):
    """Ensure the game window renders an unclipped 16:9 frame, adjusting resolution when enabled."""
    if (TALL_CAPTURE_WIDTH, TALL_CAPTURE_HEIGHT) in ALLOWED_FRAME_SIZES:
        return None
    healed = heal_16_9_window(controller, hwnd=hwnd)
    if healed is not None:
        return healed
    if not is_auto_adjust_resolution_enabled(context):
        return None

    target_hwnd = int(hwnd) if hwnd else _resolve_hwnd(controller=controller)
    if not target_hwnd:
        return None
    u32 = _user32()
    session = active_session()
    in_bg = bool(session and getattr(session, "_background_observation", False))
    with _per_monitor_dpi(u32):
        if not u32.IsWindow(target_hwnd) or (in_bg and u32.IsIconic(target_hwnd)):
            return None

    current_snap = _capture_snapshot(u32, target_hwnd)
    if current_snap is None or current_snap.client_size is None:
        return None
    fail_key = (target_hwnd, current_snap.client_size)
    with _SESSION_LOCK:
        last_fail = _LAST_FAILED_ATTEMPT.get(fail_key)
        if last_fail is not None and time.monotonic() - last_fail < 1.0:
            return None

    adopt_session_16_9_handoff()
    geom = closest_16_9_geometry(u32, target_hwnd, current_snap)
    if geom is None:
        return None
    (target_w, target_h), (win_x, win_y) = geom

    with _SESSION_LOCK:
        newly_recorded = target_hwnd not in _SESSION_16_9_SNAPSHOT
        if newly_recorded:
            _SESSION_16_9_SNAPSHOT[target_hwnd] = current_snap
            _ensure_atexit_registered()
        baseline_snap = _SESSION_16_9_SNAPSHOT[target_hwnd]

    ok, frame, modified = _apply_borderless_resolution(
        u32,
        target_hwnd,
        snap=current_snap,
        target_physical_size=(target_w, target_h),
        target_capture_size=(BASE_CAPTURE_WIDTH, BASE_CAPTURE_HEIGHT),
        target_short_side=BASE_CAPTURE_HEIGHT,
        win_pos=(win_x, win_y),
        controller=controller,
        stopped_fn=(session.stopped if session else None),
        require_unclipped=True,
        allow_solid_frame=True,
        allow_unminimize=not in_bg,
    )
    if not ok or (controller is not None and frame is None):
        with _SESSION_LOCK:
            _LAST_FAILED_ATTEMPT[fail_key] = time.monotonic()
            if newly_recorded:
                _SESSION_16_9_SNAPSHOT.pop(target_hwnd, None)
        if newly_recorded and modified:
            _restore_snapshot(u32, current_snap, controller=controller)
        return None

    should_notify = False
    with _SESSION_LOCK:
        _LAST_FAILED_ATTEMPT.pop(fail_key, None)
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
        write_message(msg, msg)
        if notify is not None:
            try:
                notify(msg)
            except Exception:
                pass
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
        )
        rt.invalidate_frame()
        if not ok:
            _restore_and_wait()
            if fail_message:
                rt.log(fail_message, detail=True)
            yield False
            return
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
            rt.action("TouchMove", target=[640, 36])
            rt.pause(0.25)
        yield is_tall
