"""Independent Win32 overlay following one controller HWND with big-map tutorials."""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import math
import os
import threading
import time

from message_log import write_message
from .surface import LayeredSurface, bind, checked


PROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)


class WindowClass(ctypes.Structure):
    _fields_ = [("style", w.UINT), ("proc", PROC), ("extra", ctypes.c_int),
                ("window_extra", w.INT), ("instance", w.HINSTANCE), ("icon", w.HICON),
                ("cursor", w.HANDLE), ("background", w.HBRUSH), ("menu", w.LPCWSTR),
                ("name", w.LPCWSTR)]


class Paint(ctypes.Structure):
    _fields_ = [("dc", w.HDC), ("erase", w.BOOL), ("rect", w.RECT),
                ("restore", w.BOOL), ("update", w.BOOL), ("reserved", w.BYTE * 32)]


def _in_rect(x, y, rect):
    return rect is not None and rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]


class OverlayWindow:
    def __init__(self, game, *, exclude_from_capture=True, tutorial_provider=None):
        self.game = game
        self.exclude_from_capture = exclude_from_capture
        self.tutorial_provider = tutorial_provider
        self.lock = threading.Lock()
        self.snapshot = ((), "", None, 0.0)
        self.status = {"state": "starting"}
        self.phase = "starting"
        self.last_tick = time.monotonic()
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.error = None
        self.interactive = False
        self.cursor_hand = False
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
                "tick_age_ms": round((time.monotonic() - self.last_tick) * 1000)}

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
        load_cursor = bind(user, "LoadCursorW", w.HANDLE, w.HINSTANCE, w.LPCWSTR)
        set_cursor = bind(user, "SetCursor", w.HANDLE, w.HANDLE)
        post_msg = bind(user, "PostMessageW", w.BOOL, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        arrow_cursor = load_cursor(None, ctypes.cast(32512, w.LPCWSTR))
        hand_cursor = load_cursor(None, ctypes.cast(32649, w.LPCWSTR))
        hwnd = surface = None
        class_name = f"MaaNikkiMapMask-{os.getpid()}-{id(self)}"
        registered = False

        @PROC
        def procedure(window, message, wp, lp):
            if message == 0x000F:
                ps = Paint()
                begin(window, ctypes.byref(ps))
                end(window, ctypes.byref(ps))
                return 0
            if message == 0x0014:
                return 1
            if message == 0x0084:
                return 1 if self.interactive else -1
            if message == 0x0021:
                return 3  # MA_NOACTIVATE
            if message == 0x0020 and self.interactive:
                set_cursor(hand_cursor if self.cursor_hand else arrow_cursor)
                return 1
            if message == 0x020A and valid(self.game):
                post_msg(self.game, message, wp, lp)
                return 0
            return default(window, message, wp, lp)

        try:
            register = bind(user, "RegisterClassW", w.ATOM, ctypes.POINTER(WindowClass))
            checked(register(ctypes.byref(WindowClass(0, procedure, 0, 0, module, None, None,
                                                      None, None, class_name))), "注册地图遮罩窗口")
            registered = True
            create = bind(user, "CreateWindowExW", w.HWND, w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
                          *([ctypes.c_int] * 4), w.HWND, w.HMENU, w.HINSTANCE, ctypes.c_void_p)
            base_exstyle = 0x08080088  # LAYERED | TOPMOST | TOOLWINDOW | NOACTIVATE
            passthrough_exstyle = base_exstyle | 0x00000020  # + WS_EX_TRANSPARENT
            hwnd = checked(create(passthrough_exstyle, class_name, "MaaNikki 地图标记", 0x80000000,
                                  0, 0, 1, 1, None, None, module, None), "创建地图遮罩窗口")
            affinity = 0x11 if self.exclude_from_capture else 0
            affinity_set = bind(user, "SetWindowDisplayAffinity", w.BOOL, w.HWND, w.DWORD)(hwnd, affinity)
            excluded = bool(affinity_set and affinity)
            write_message(f"地图遮罩窗口：renderer=layered_bgra, independent=True, "
                          f"capture_excluded={excluded}, affinity_set={bool(affinity_set)}", None)
            get_client = bind(user, "GetClientRect", w.BOOL, w.HWND, ctypes.POINTER(w.RECT))
            to_screen = bind(user, "ClientToScreen", w.BOOL, w.HWND, ctypes.POINTER(w.POINT))
            get_cursor = bind(user, "GetCursorPos", w.BOOL, ctypes.POINTER(w.POINT))
            key_state = bind(user, "GetAsyncKeyState", ctypes.c_short, ctypes.c_int)
            foreground = bind(user, "GetForegroundWindow", w.HWND)
            valid = bind(user, "IsWindow", w.BOOL, w.HWND)
            iconic = bind(user, "IsIconic", w.BOOL, w.HWND)
            shown = bind(user, "IsWindowVisible", w.BOOL, w.HWND)
            show = bind(user, "ShowWindow", w.BOOL, w.HWND, ctypes.c_int)
            move = bind(user, "SetWindowPos", w.BOOL, w.HWND, w.HWND, *([ctypes.c_int] * 4), w.UINT)
            window_style = bind(user, "GetWindowLongPtrW", ctypes.c_ssize_t, w.HWND, ctypes.c_int)
            set_style = bind(user, "SetWindowLongPtrW", ctypes.c_ssize_t, w.HWND, ctypes.c_int, ctypes.c_ssize_t)
            peek = bind(user, "PeekMessageW", w.BOOL, ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT)
            translate = bind(user, "TranslateMessage", w.BOOL, ctypes.POINTER(w.MSG))
            dispatch = bind(user, "DispatchMessageW", ctypes.c_ssize_t, ctypes.POINTER(w.MSG))
            self.ready.set()
            geometry = rendered = last_signature = None
            next_topmost = next_diagnostic = 0.0
            rendered_pixels = 0
            previous_messages = ()
            selected_id = None
            selected_key = None
            selected_items = ()
            selected_index = 0
            was_down = False
            press_pos = (0, 0)
            press_time = 0.0
            dragging_outside = False
            applied_interactive = False

            while not self.stop.is_set():
                self.last_tick = time.monotonic()
                self.phase = "snapshot"
                with self.lock:
                    markers, hint, circle, updated = self.snapshot
                now = time.monotonic()
                fg = foreground()
                reason = ("game_closed" if not valid(self.game) else
                          "minimized" if iconic(self.game) else
                          "unfocused" if fg not in (self.game, hwnd) else
                          "empty" if not (markers or hint) else
                          "stale" if now - updated >= 1 else "visible")
                if reason != "visible" or circle is not None:
                    selected_id = selected_key = None
                    selected_items = ()
                    was_down = dragging_outside = False
                if reason == "visible":
                    rect, origin = w.RECT(), w.POINT()
                    checked(get_client(self.game, ctypes.byref(rect)), "读取游戏客户区")
                    checked(to_screen(self.game, ctypes.byref(origin)), "读取游戏窗口位置")
                    if rect.right <= 0 or rect.bottom <= 0:
                        reason = "empty_client"
                    else:
                        next_geometry = origin.x, origin.y, rect.right, rect.bottom
                        if surface is None or (surface.width, surface.height) != (rect.right, rect.bottom):
                            if surface:
                                surface.close()
                            surface = LayeredSurface(rect.right, rect.bottom)
                            rendered = None

                        hovered_key = None
                        hovered_entry = None
                        hover_part = None
                        in_card = False
                        if not circle and self.tutorial_provider is not None:
                            if key_state(0x1B) & 0x8000:
                                selected_id = selected_key = None
                                selected_items = ()
                            cur = w.POINT()
                            get_cursor(ctypes.byref(cur))
                            cx, cy = cur.x - origin.x, cur.y - origin.y
                            sx, sy = rect.right / 1280.0, rect.bottom / 720.0
                            s = min(sx, sy)
                            layout = surface.card_layout if selected_id else None
                            if layout and _in_rect(cx, cy, layout.get("card")):
                                in_card = True
                                for part in ("close", "prev", "next"):
                                    if _in_rect(cx, cy, layout.get(part)):
                                        hover_part = part
                                        break
                            else:
                                hit_r = max(14.0, 20.0 * s)
                                best_d = hit_r
                                for entry in markers:
                                    mx, my, kind = entry[0], entry[1], entry[2]
                                    d = math.hypot(cx - mx * sx, cy - my * sy)
                                    if d <= best_d:
                                        best_d = d
                                        hovered_entry = entry
                                        hovered_key = (round(mx), round(my), kind)
                            down = bool(key_state(0x01) & 0x8000)
                            if down and not was_down:
                                press_pos = (cx, cy)
                                press_time = now
                                dragging_outside = (hovered_entry is None and not in_card)
                            elif down and was_down:
                                if math.hypot(cx - press_pos[0], cy - press_pos[1]) > 8.0 * s and not in_card:
                                    dragging_outside = True
                            elif not down and was_down:
                                is_click = (now - press_time <= 0.42
                                            and math.hypot(cx - press_pos[0], cy - press_pos[1]) <= 8.0 * s)
                                dragging_outside = False
                                if is_click:
                                    if hover_part == "close":
                                        selected_id = selected_key = None
                                        selected_items = ()
                                    elif hover_part == "prev" and selected_items:
                                        selected_index = (selected_index - 1) % len(selected_items)
                                        selected_id = selected_items[selected_index].id
                                    elif hover_part == "next" and selected_items:
                                        selected_index = (selected_index + 1) % len(selected_items)
                                        selected_id = selected_items[selected_index].id
                                    elif in_card:
                                        pass
                                    elif hovered_entry is not None and len(hovered_entry) >= 5 and hovered_entry[4]:
                                        items = hovered_entry[4]
                                        if selected_key == hovered_key and len(items) == 1:
                                            selected_id = selected_key = None
                                            selected_items = ()
                                        else:
                                            selected_items = items
                                            selected_index = 0
                                            selected_id = items[0].id
                                            selected_key = hovered_key
                                    else:
                                        selected_id = selected_key = None
                                        selected_items = ()
                            was_down = down

                        card_data = None
                        if selected_id and not circle and self.tutorial_provider is not None:
                            matched_entry = None
                            matched_idx = 0
                            for entry in markers:
                                items = entry[4] if len(entry) >= 5 else ()
                                for i, pt in enumerate(items):
                                    if pt.id == selected_id:
                                        matched_entry = entry
                                        matched_idx = i
                                        break
                                if matched_entry is not None:
                                    break
                            if matched_entry is not None:
                                selected_items = matched_entry[4]
                                selected_index = matched_idx
                                selected_key = (round(matched_entry[0]), round(matched_entry[1]), matched_entry[2])
                                active_pt = selected_items[selected_index]
                                tut = self.tutorial_provider(active_pt)
                                card_data = {
                                    "tutorial": tut,
                                    "anchor": (matched_entry[0], matched_entry[1]),
                                    "index": selected_index,
                                    "total": len(selected_items),
                                    "hover": hover_part,
                                }
                            else:
                                selected_id = selected_key = None
                                selected_items = ()

                        want_interactive = bool(
                            not circle and not dragging_outside and (hovered_key is not None or in_card))
                        self.interactive = want_interactive
                        self.cursor_hand = bool(hovered_key is not None or hover_part is not None)
                        if want_interactive != applied_interactive:
                            set_style(hwnd, -20, base_exstyle if want_interactive else passthrough_exstyle)
                            applied_interactive = want_interactive

                        active_key = selected_key or hovered_key
                        card_sig = None
                        if card_data is not None:
                            t = card_data["tutorial"]
                            card_sig = (t.point_id, t.loading, t.error, t.image is not None,
                                        card_data["anchor"], card_data["index"], card_data["total"],
                                        card_data["hover"])
                        draw_coords = tuple(entry[:4] for entry in markers)
                        content = draw_coords, hint, circle, active_key, card_sig
                        if content != rendered:
                            self.phase = "draw"
                            rendered_pixels = surface.draw(
                                markers, hint, circle, active_key=active_key, card=card_data)
                        if content != rendered or next_geometry != geometry or not shown(hwnd):
                            self.phase = "present"
                            surface.present(hwnd, origin)
                            rendered, geometry = content, next_geometry
                        if not shown(hwnd):
                            show(hwnd, 4)  # SW_SHOWNOACTIVATE
                            checked(shown(hwnd), "显示地图遮罩窗口")
                        if now >= next_topmost:
                            checked(move(hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, 0x0013), "置顶地图遮罩")
                            next_topmost = now + .25
                if reason != "visible":
                    if applied_interactive:
                        set_style(hwnd, -20, passthrough_exstyle)
                        applied_interactive = self.interactive = False
                    if shown(hwnd):
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
                    last_signature, next_diagnostic = signature, now + 10
                msg = w.MSG()
                messages = []
                pump_started = time.monotonic()
                for _ in range(32):
                    if self.stop.is_set() or time.monotonic() - pump_started >= .01:
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
                self.stop.wait(1 / 60)
        finally:
            if hwnd:
                bind(user, "DestroyWindow", w.BOOL, w.HWND)(hwnd)
            if surface:
                surface.close()
            if registered:
                bind(user, "UnregisterClassW", w.BOOL, w.LPCWSTR, w.HINSTANCE)(class_name, module)
            if previous_dpi:
                dpi(previous_dpi)
