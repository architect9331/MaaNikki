"""Independent, click-through Win32 overlay following one controller HWND."""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import os
import threading
import time

from message_log import write_message
from .surface import LayeredSurface, bind, checked


PROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)


class WindowClass(ctypes.Structure):
    _fields_ = [("style", w.UINT), ("proc", PROC), ("extra", ctypes.c_int),
                ("window_extra", ctypes.c_int), ("instance", w.HINSTANCE), ("icon", w.HICON),
                ("cursor", w.HANDLE), ("background", w.HBRUSH), ("menu", w.LPCWSTR),
                ("name", w.LPCWSTR)]


class Paint(ctypes.Structure):
    _fields_ = [("dc", w.HDC), ("erase", w.BOOL), ("rect", w.RECT),
                ("restore", w.BOOL), ("update", w.BOOL), ("reserved", w.BYTE * 32)]


class OverlayWindow:
    def __init__(self, game, *, exclude_from_capture=True):
        self.game = game
        self.exclude_from_capture = exclude_from_capture
        self.lock = threading.Lock()
        self.snapshot = ((), "", None, 0.0)
        self.status = {"state": "starting"}
        self.phase = "starting"
        self.last_tick = time.monotonic()
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self._thread_main, name="map-mask-window", daemon=True)
        self.thread.start()
        if not self.ready.wait(3) or self.error:
            self.stop.set()
            raise RuntimeError(self.error or "地图遮罩窗口创建超时。")

    def publish(self, points=(), hint="", circle=None):
        with self.lock:
            self.snapshot = (points, hint, circle, time.monotonic())

    def hide(self):
        with self.lock:
            self.snapshot = ((), "", None, 0.0)

    def close(self):
        self.hide()
        self.stop.set()
        self.thread.join(timeout=1)

    def diagnostic(self):
        return {**self.status, "phase": self.phase, "alive": self.thread.is_alive(),
                "tick_age_ms": round((time.monotonic()-self.last_tick)*1000)}

    def _thread_main(self):
        try:
            self._run()
        except Exception as error:
            self.error = f"地图遮罩窗口不可用（{type(error).__name__}）：{error}"
            self.phase = "error"
            self.status = {"state": "error", "error": self.error}
            write_message(self.error, None)
        finally:
            self.ready.set()

    def _run(self):
        user = ctypes.WinDLL("user32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        dpi = bind(user, "SetThreadDpiAwarenessContext", w.HANDLE, w.HANDLE)
        previous_dpi = dpi(ctypes.c_void_p(-4))
        module = bind(kernel, "GetModuleHandleW", w.HMODULE, w.LPCWSTR)(None)
        default = bind(user, "DefWindowProcW", ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        begin = bind(user, "BeginPaint", w.HDC, w.HWND, ctypes.POINTER(Paint))
        end = bind(user, "EndPaint", w.BOOL, w.HWND, ctypes.POINTER(Paint))
        hwnd = surface = None
        class_name = f"MaaNikkiMapMask-{os.getpid()}-{id(self)}"
        registered = False

        @PROC
        def procedure(window, message, wp, lp):
            if message == 0x000F:
                # The compositor receives a complete BGRA surface explicitly;
                # WM_PAINT delivery is independent of presenting that surface.
                # BeginPaint also acknowledges internal paint requests, which
                # can have no update rectangle for ValidateRect to clear.
                ps = Paint()
                begin(window, ctypes.byref(ps))
                end(window, ctypes.byref(ps))
                return 0
            if message == 0x0014:
                return 1
            if message == 0x0084:
                return -1  # HTTRANSPARENT
            if message == 0x0021:
                return 3  # MA_NOACTIVATE
            return default(window, message, wp, lp)

        try:
            register = bind(user, "RegisterClassW", w.ATOM, ctypes.POINTER(WindowClass))
            checked(register(ctypes.byref(WindowClass(0, procedure, 0, 0, module, None, None,
                                                       None, None, class_name))), "注册地图遮罩窗口")
            registered = True
            create = bind(user, "CreateWindowExW", w.HWND, w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
                          *([ctypes.c_int] * 4), w.HWND, w.HMENU, w.HINSTANCE, ctypes.c_void_p)
            # Independent popup, topmost, click-through and non-activating. Do
            # not add it to the game's owned-window capture tree.
            hwnd = checked(create(0x080800A8, class_name, "MaaNikki 地图标记", 0x80000000,
                                   0, 0, 1, 1, None, None, module, None), "创建地图遮罩窗口")
            # UpdateLayeredWindow supplies per-pixel alpha; calling
            # SetLayeredWindowAttributes here would disable that presentation path.
            affinity = 0x11 if self.exclude_from_capture else 0
            affinity_set = bind(user, "SetWindowDisplayAffinity", w.BOOL, w.HWND, w.DWORD)(hwnd, affinity)
            excluded = bool(affinity_set and affinity)
            write_message(f"地图遮罩窗口：renderer=layered_bgra, independent=True, "
                          f"capture_excluded={excluded}, affinity_set={bool(affinity_set)}", None)
            get_client = bind(user, "GetClientRect", w.BOOL, w.HWND, ctypes.POINTER(w.RECT))
            to_screen = bind(user, "ClientToScreen", w.BOOL, w.HWND, ctypes.POINTER(w.POINT))
            foreground = bind(user, "GetForegroundWindow", w.HWND)
            valid = bind(user, "IsWindow", w.BOOL, w.HWND)
            iconic = bind(user, "IsIconic", w.BOOL, w.HWND)
            shown = bind(user, "IsWindowVisible", w.BOOL, w.HWND)
            show = bind(user, "ShowWindow", w.BOOL, w.HWND, ctypes.c_int)
            move = bind(user, "SetWindowPos", w.BOOL, w.HWND, w.HWND, *([ctypes.c_int] * 4), w.UINT)
            window_style = bind(user, "GetWindowLongPtrW", ctypes.c_ssize_t, w.HWND, ctypes.c_int)
            peek = bind(user, "PeekMessageW", w.BOOL, ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT)
            translate = bind(user, "TranslateMessage", w.BOOL, ctypes.POINTER(w.MSG))
            dispatch = bind(user, "DispatchMessageW", ctypes.c_ssize_t, ctypes.POINTER(w.MSG))
            self.ready.set()
            geometry = rendered = last_signature = None
            next_topmost = next_diagnostic = 0.0
            rendered_pixels = 0
            previous_messages = ()
            while not self.stop.is_set():
                self.last_tick = time.monotonic()
                self.phase = "snapshot"
                with self.lock:
                    markers, hint, circle, updated = self.snapshot
                now = time.monotonic()
                reason = ("game_closed" if not valid(self.game) else
                          "minimized" if iconic(self.game) else
                          "unfocused" if foreground() != self.game else
                          "empty" if not (markers or hint) else
                          "stale" if now-updated >= 1 else "visible")
                if reason == "visible":
                    rect, origin = w.RECT(), w.POINT()
                    checked(get_client(self.game, ctypes.byref(rect)), "读取游戏客户区")
                    checked(to_screen(self.game, ctypes.byref(origin)), "读取游戏窗口位置")
                    if rect.right <= 0 or rect.bottom <= 0:
                        reason = "empty_client"
                    else:
                        next_geometry = origin.x, origin.y, rect.right, rect.bottom
                        content = markers, hint, circle
                        if surface is None or (surface.width, surface.height) != (rect.right, rect.bottom):
                            if surface:
                                surface.close()
                            surface = LayeredSurface(rect.right, rect.bottom)
                            rendered = None
                        if content != rendered:
                            self.phase = "draw"
                            rendered_pixels = surface.draw(*content)
                        if content != rendered or next_geometry != geometry or not shown(hwnd):
                            self.phase = "present"
                            surface.present(hwnd, origin)
                            rendered, geometry = content, next_geometry
                        if not shown(hwnd):
                            show(hwnd, 4)  # SW_SHOWNOACTIVATE
                            checked(shown(hwnd), "显示地图遮罩窗口")
                        if now >= next_topmost:
                            checked(move(hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, 0x0013), "置顶地图遮罩")
                            next_topmost = now+.25
                if reason != "visible" and shown(hwnd):
                    show(hwnd, 0)
                    rendered = None
                self.status = {"state": reason, "shown": bool(shown(hwnd)),
                               "topmost": bool(window_style(hwnd, -20) & 8),
                               "geometry": geometry, "markers": len(markers),
                               "capture_excluded": excluded,
                               "pixels": rendered_pixels if reason == "visible" else 0,
                               "messages": previous_messages}
                signature = reason, bool(markers), bool(hint), geometry
                if signature != last_signature or now >= next_diagnostic:
                    write_message(f"地图遮罩显示：{self.status}", None)
                    last_signature, next_diagnostic = signature, now+10
                msg = w.MSG()
                messages = []
                pump_started = time.monotonic()
                # Some window messages regenerate as they are dispatched. Never
                # let a busy queue starve focus/snapshot updates or task Stop.
                for _ in range(32):
                    if self.stop.is_set() or time.monotonic()-pump_started >= .01:
                        break
                    self.phase = "peek"
                    if not peek(ctypes.byref(msg), hwnd, 0, 0, 1):
                        break
                    messages.append(hex(msg.message))
                    self.phase = f"dispatch:{hex(msg.message)}"
                    translate(ctypes.byref(msg))
                    dispatch(ctypes.byref(msg))
                previous_messages = tuple(messages[-8:])
                self.phase = "idle"
                self.stop.wait(1/60)
        finally:
            if hwnd:
                bind(user, "DestroyWindow", w.BOOL, w.HWND)(hwnd)
            if surface:
                surface.close()
            if registered:
                bind(user, "UnregisterClassW", w.BOOL, w.LPCWSTR, w.HINSTANCE)(class_name, module)
            if previous_dpi:
                dpi(previous_dpi)
