from __future__ import annotations

from contextlib import contextmanager, nullcontext
import ctypes
from ctypes import wintypes
import os
import re
import time as wall_time
from task_control import task_time as time

from .models import GameWindowClosed, NavigationError
from game_keys import Binding, GameKeys, MACRO_KEYS


MAX_CAMERA_PIXELS = 8192


class MouseEvent(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("extra", ctypes.c_size_t)]


class InputPayload(ctypes.Union):
    # Windows x64 INPUT's largest member is MOUSEINPUT (32 bytes).
    _fields_ = [("mouse", MouseEvent), ("alignment", ctypes.c_byte*32)]


class MouseInput(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("payload", InputPayload)]


def send_mouse(user32, flags, *, dx=0, dy=0, data=0):
    event = MouseInput(type=0, payload=InputPayload(mouse=MouseEvent(
        dx=dx, dy=dy, mouseData=data, dwFlags=flags)))
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(MouseInput), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    return user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(event)) == 1


def _window_key_spec(user32, key: int):
    scan = user32.MapVirtualKeyW(key, 4)  # MAPVK_VK_TO_VSC_EX
    extended = (scan >> 8) in (0xE0, 0xE1)
    flags = 1 | ((scan & 0xFF) << 16) | (int(extended) << 24)
    # Alt uses WM_SYSKEY*, other configured keys use WM_KEY*.
    down, up = (0x0104, 0x0105) if key in (18, 164, 165) else (0x0100, 0x0101)
    return down, up, flags


class ForegroundInput:
    """Bounded Maa key pulses. Stop/focus loss always releases held keys."""
    def __init__(self, controller, stopped, ready=lambda: True, bindings=None):
        self.controller = controller
        self.stopped = stopped
        self.ready = ready
        self.held = set()
        self.buttons = set()
        self.bindings = bindings or GameKeys()
        if os.name != "nt":
            raise NavigationError("前台导航仅支持 Windows 游戏窗口。")
        # Controller.info is forwarded by MaaAgentServer; Toolkit enumeration
        # is not. Bind to the client's actual HWND, never an independently found
        # window or the current foreground window.
        try:
            info = controller.info
        except (OSError, RuntimeError, ValueError, TypeError, AttributeError) as error:
            raise NavigationError("无法读取当前连接的游戏窗口，请重新连接后再试。") from error
        hwnd = info.get("hwnd") if isinstance(info, dict) else None
        if (not isinstance(info, dict) or info.get("type") != "win32" or type(hwnd) is not int
                or not 0 < hwnd < 2**(ctypes.sizeof(ctypes.c_void_p)*8)):
            raise NavigationError("当前连接没有有效的 Windows 游戏窗口，导航已停止。")
        self.hwnd = hwnd
        from maa.define import MaaWin32ScreencapMethodEnum
        capture_method = info.get("screencap_method", 0)
        screen_methods = int(MaaWin32ScreencapMethodEnum.Foreground
                             | MaaWin32ScreencapMethodEnum.DXGI_DesktopDup
                             | MaaWin32ScreencapMethodEnum.GDI)
        self.capture_requires_foreground = bool(type(capture_method) is int
                                                and capture_method & screen_methods)
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.GetForegroundWindow.argtypes = []
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.IsWindow.argtypes = [wintypes.HWND]
        self.user32.IsWindow.restype = wintypes.BOOL
        self.user32.IsIconic.argtypes = [wintypes.HWND]
        self.user32.IsIconic.restype = wintypes.BOOL
        self.user32.IsHungAppWindow.argtypes = [wintypes.HWND]
        self.user32.IsHungAppWindow.restype = wintypes.BOOL
        self.user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
        self.user32.MapVirtualKeyW.restype = wintypes.UINT
        self.user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user32.PostMessageW.restype = wintypes.BOOL
        self.user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        self.user32.GetWindowTextLengthW.restype = ctypes.c_int
        self.user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user32.GetWindowTextW.restype = ctypes.c_int
        self.user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user32.GetClassNameW.restype = ctypes.c_int
        if not self.user32.IsWindow(self.hwnd):
            raise GameWindowClosed("连接的游戏窗口已经关闭，请重新连接。")
        class_name = ctypes.create_unicode_buffer(257)
        if (not self.user32.GetClassNameW(self.hwnd, class_name, len(class_name))
                or class_name.value != "UnrealWindow"):
            raise NavigationError("当前连接的窗口不是游戏客户端，请重新连接无限暖暖。")
        length = self.user32.GetWindowTextLengthW(self.hwnd)
        if not 0 < length <= 256:
            raise NavigationError("无法确认连接窗口的标题，导航已停止。")
        title = ctypes.create_unicode_buffer(length+1)
        if (not self.user32.GetWindowTextW(self.hwnd, title, len(title))
                or not re.fullmatch(r"\s*(Infinity\s*Nikki|无限暖暖|無限暖暖)\s*", title.value)):
            raise NavigationError("当前连接的窗口不是无限暖暖，导航已停止，请检查连接对象。")
        from task_control import active_session
        session = active_session()
        if session is not None:
            session.register(self)

    def foreground(self):
        return bool(
            self.user32.IsWindow(self.hwnd)
            and not self.user32.IsIconic(self.hwnd)
            and self.user32.GetForegroundWindow() == self.hwnd
        )

    def focus_details(self):
        hwnd = self.user32.GetForegroundWindow()
        text = ctypes.create_unicode_buffer(1024)
        if hwnd:
            self.user32.GetWindowTextW(hwnd, text, len(text))
        return {"game_hwnd": self.hwnd, "foreground_hwnd": int(hwnd or 0), "foreground_title": text.value}

    def hung(self):
        return bool(self.user32.IsWindow(self.hwnd) and self.user32.IsHungAppWindow(self.hwnd))

    def check(self, *, require_foreground=True):
        if self.stopped():
            raise NavigationError("已停止导航。")
        from task_control import active_session
        session = active_session()
        if session is not None:
            session.wait(self, require_foreground=True)
        elif not self.foreground():
            self.release()
            while not self.foreground():
                if self.stopped():
                    raise NavigationError("任务已停止。")
                if not self.user32.IsWindow(self.hwnd):
                    raise GameWindowClosed("连接的游戏窗口已经关闭，请重新连接。")
                time.sleep(.05)

    def _post_window_key(self, key: int, *, down: bool) -> bool:
        down_msg, up_msg, flags = _window_key_spec(self.user32, key)
        msg = down_msg if down else up_msg
        lparam = flags if down else (flags | 0xC0000000)
        return bool(self.user32.PostMessageW(self.hwnd, msg, key, lparam))

    def click_window_key(self, key, pause, seconds=.05):
        """Post a key pulse to the connected window without global input/focus."""
        if type(key) is not int or not 0 < key < 256 or not .01 <= seconds <= 1:
            raise NavigationError("后台按键参数无效。")
        if self.stopped() or not self.user32.IsWindow(self.hwnd) or self.user32.IsIconic(self.hwnd):
            return False
        if not self._post_window_key(key, down=True):
            raise NavigationError(f"无法向游戏窗口发送后台按键（错误 {ctypes.get_last_error()}）。")
        try:
            return pause(seconds)
        finally:
            # Even Stop during the pulse must enqueue the matching window-only up.
            # Never use global key_up here: it could affect the user's active app.
            if self.user32.IsWindow(self.hwnd):
                if not self._post_window_key(key, down=False):
                    raise NavigationError(f"无法释放游戏窗口的后台按键（错误 {ctypes.get_last_error()}）。")

    @property
    def generation(self):
        from task_control import active_session
        session = active_session()
        return session.generation if session else 0

    def release(self):
        # Maa Seize's key_up can refocus the game. Direct ups on focus loss avoid
        # stealing focus while still preventing globally stuck movement keys.
        for key in list(self.held):
            self.release_key(key)
        for button in list(self.buttons):
            self.release_mouse(button)

    def face_camera(self):
        """Align the character to the camera with a brief Ctrl+forward pulse."""
        forward = self.bindings.get("forward")
        control = Binding("key", 17)
        if forward.kind != "key" or forward.code in {17, 162, 163}:
            raise NavigationError("朝向对齐需要前进键为 Ctrl 以外的键盘按键。")
        self.check()
        if not self.ready():
            raise NavigationError("当前不是可移动的主界面，未调整角色朝向。")
        self.release()
        generation = self.generation
        hold, settle = .08, .4
        try:
            self._down(control)
            if self.generation != generation:
                raise NavigationError("调整朝向时游戏前台发生变化，请重新确认朝向。")
            started = time.monotonic()
            self._down(forward)
            # Count key-down submission time within the short pulse. Never
            # restore a held W after focus loss or perform OCR while holding it.
            self.wait(max(0, hold-(time.monotonic()-started)), restore=False)
        finally:
            # Release W before Ctrl so cleanup cannot leave ordinary forward
            # movement held. These releases also work after Stop/focus loss.
            try:
                self.release_key(forward.code)
                released_at = time.monotonic()
            finally:
                self.release_key(control.code)
        self.wait(settle, restore=False)
        if self.generation != generation:
            raise NavigationError("调整朝向时游戏前台发生变化，请重新确认朝向。")
        return {"hold_seconds": hold, "settle_seconds": settle,
                "forward_input_seconds": released_at-started}

    def release_key(self, key):
        if key not in self.held:
            return
        try:
            if self.foreground():
                try:
                    released = self.controller.post_key_up(key).wait().succeeded
                except (OSError, RuntimeError, ValueError):
                    released = False
                if not released:
                    self.force_key_up(key)
            else:
                self.force_key_up(key)
        finally:
            self.held.discard(key)

    def release_mouse(self, button):
        if button not in self.buttons:
            return
        try:
            if self.foreground():
                try:
                    released = self.controller.post_touch_up(button).wait().succeeded
                except (OSError, RuntimeError, ValueError):
                    released = False
                if not released:
                    self.force_mouse_up(button)
            else:
                self.force_mouse_up(button)
        finally:
            self.buttons.discard(button)

    def click_mouse(self, button, seconds=.05):
        """Pulse a scene button without moving the cursor or releasing movement."""
        if button not in (0, 1, 2, 3, 4) or not .01 <= seconds <= 1:
            raise NavigationError("鼠标点击参数无效。")
        self.check()
        if button in self.buttons:
            raise NavigationError("该鼠标按键正在按住，不能重复点击。")
        try:
            self.mouse_down(button)
            self.wait(seconds, restore=False)
            self.check()
            return True
        finally:
            self.release_mouse(button)

    def force_key_up(self, key):
        # Cleanup only: do not require a working Agent channel or refocus.
        extended = key in {33, 34, 35, 36, 37, 38, 39, 40, 45, 46, 163, 165}
        self.user32.keybd_event(key, 0, 0x0002 | (0x0001 if extended else 0), 0)

    def force_mouse_up(self, button):
        self.user32.mouse_event({0: 0x0004, 1: 0x0010, 2: 0x0040, 3: 0x0100, 4: 0x0100}[button],
                                0, 0, button-2 if button >= 3 else 0, 0)

    def hold(self, key, seconds, *, restore=True):
        """Press any registered game action, for route-authored key steps."""
        binding = self.bindings.get(key)
        self._down(binding, restore=restore)
        try:
            self.wait(seconds, restore=restore)
        finally:
            self._up(binding)

    def key_down(self, key):
        if key not in MACRO_KEYS:
            raise NavigationError("宏按键未登记。")
        self.check()
        self._down(self.bindings.get(key))

    def _down(self, binding, *, restore=True):
        self.check()
        if binding.kind == "key" and binding.code in self.held:
            return
        if binding.kind == "mouse":
            if binding.code in self.buttons:
                return
            self.mouse_down(binding.code)
            return
        self.held.add(binding.code)
        if not self.controller.post_key_down(binding.code).wait().succeeded:
            raise NavigationError("宏按键输入失败。")

    def key_up(self, key):
        if key not in MACRO_KEYS:
            raise NavigationError("宏按键未登记。")
        self.check()
        binding = self.bindings.get(key)
        if binding.kind == "mouse":
            if binding.code in self.buttons:
                self.mouse_up(binding.code)
            return
        if binding.code not in self.held:
            return
        if not self.controller.post_key_up(binding.code).wait().succeeded:
            raise NavigationError("宏按键释放失败。")
        self.held.discard(binding.code)

    def _up(self, binding):
        self.check()
        if binding.kind == "mouse":
            if binding.code in self.buttons:
                self.mouse_up(binding.code)
        elif binding.code in self.held:
            if not self.controller.post_key_up(binding.code).wait().succeeded:
                raise NavigationError("按键释放失败。")
            self.held.discard(binding.code)

    def mouse_down(self, button=0):
        if button not in (0, 1, 2, 3, 4):
            raise NavigationError("鼠标按键未登记。")
        self.check()
        self.buttons.add(button)
        # Maa Seize touch_down relocates the cursor before pressing. Scene
        # buttons must only press, otherwise that warp can turn the camera.
        flags = {0: 0x0002, 1: 0x0008, 2: 0x0020, 3: 0x0080, 4: 0x0080}[button]
        if not send_mouse(self.user32, flags, data=button-2 if button >= 3 else 0):
            raise NavigationError("鼠标动作输入失败。")

    def mouse_up(self, button=0):
        if button not in self.buttons:
            raise NavigationError("鼠标按键尚未按下。")
        self.check()
        if not self.controller.post_touch_up(button).wait().succeeded:
            raise NavigationError("鼠标动作释放失败。")
        self.buttons.discard(button)

    def wait(self, seconds, *, restore=True):
        if not 0 <= seconds <= 8:
            raise NavigationError("动作等待时间超出限制。")
        keys, buttons = set(self.held), set(self.buttons)
        generation = self.generation
        deadline = time.monotonic()+seconds
        while time.monotonic() < deadline:
            self.check()
            if restore and self.generation != generation:
                generation = self.generation
                # A bounded macro/ability hold resumes only its remaining
                # active duration. Navigation movement replans separately.
                from game_keys import Binding
                for key in keys:
                    self._down(Binding("key", key))
                for button in buttons:
                    self.mouse_down(button)
            time.sleep(min(.05, max(0, deadline-time.monotonic())))


class BackgroundInput(ForegroundInput):
    """Window-targeted PostMessage input; pauses on minimize or uncapturable windows."""

    window_only = True

    def __init__(self, controller, stopped, ready=lambda: True, bindings=None):
        super().__init__(controller, stopped, ready=ready, bindings=bindings)
        self._restore_keys = set()
        self._synced_generation = self.generation
        self.user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.user32.GetClientRect.restype = wintypes.BOOL
        self.user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        self.user32.ClientToScreen.restype = wintypes.BOOL
        self.user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        self.user32.GetCursorPos.restype = wintypes.BOOL
        self.user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        self.user32.SetCursorPos.restype = wintypes.BOOL
        try:
            self.user32.SetThreadDpiAwarenessContext.argtypes = [wintypes.HANDLE]
            self.user32.SetThreadDpiAwarenessContext.restype = wintypes.HANDLE
        except Exception:
            pass

    @contextmanager
    def _per_monitor_dpi(self):
        prev = None
        try:
            prev = self.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            prev = None
        try:
            yield
        finally:
            if prev:
                try:
                    self.user32.SetThreadDpiAwarenessContext(prev)
                except Exception:
                    pass

    def capturable(self):
        return bool(
            self.user32.IsWindow(self.hwnd)
            and not self.user32.IsIconic(self.hwnd)
            and (not self.capture_requires_foreground or self.foreground())
        )

    def check(self, *, require_foreground=False):
        if self.stopped():
            raise NavigationError("已停止导航。")
        from task_control import active_session
        session = active_session()
        if session is not None:
            session.wait(self, require_foreground=require_foreground, require_capturable=True)
        else:
            ready = self.foreground if require_foreground else self.capturable
            if not ready():
                self.release()
                while not ready():
                    if self.stopped():
                        raise NavigationError("任务已停止。")
                    if not self.user32.IsWindow(self.hwnd):
                        raise GameWindowClosed("连接的游戏窗口已经关闭，请重新连接。")
                    time.sleep(.05)
        if not require_foreground:
            self._restore_active_keys()

    def _restore_active_keys(self):
        if not self._restore_keys or self.generation == self._synced_generation:
            return
        self._synced_generation = self.generation
        # Give UnrealWindow a moment to drain focus-transition state resets
        # before re-posting sustained movement keys.
        wall_time.sleep(.05)
        if self.stopped() or not self.capturable():
            return
        for key in list(self._restore_keys):
            if not self._post_window_key(key, down=True):
                raise NavigationError(f"恢复游戏窗口后台按键失败（错误 {ctypes.get_last_error()}）。")

    def release_key(self, key):
        self._restore_keys.discard(key)
        if key not in self.held:
            return
        try:
            if self.user32.IsWindow(self.hwnd):
                if not self._post_window_key(key, down=False) and self.user32.IsWindow(self.hwnd):
                    raise NavigationError(f"无法释放游戏窗口的后台按键（错误 {ctypes.get_last_error()}）。")
        finally:
            self.held.discard(key)

    def _down(self, binding, *, restore=True):
        if binding.kind == "mouse":
            self.check(require_foreground=True)
            if binding.code not in self.buttons:
                self.mouse_down(binding.code)
            return
        self.check()
        if binding.code in self.held:
            if restore:
                self._restore_keys.add(binding.code)
            return
        self._synced_generation = self.generation
        self.held.add(binding.code)
        if restore:
            self._restore_keys.add(binding.code)
        if not self._post_window_key(binding.code, down=True):
            raise NavigationError(f"无法向游戏窗口发送后台按键（错误 {ctypes.get_last_error()}）。")

    def key_up(self, key):
        if key not in MACRO_KEYS:
            raise NavigationError("宏按键未登记。")
        self._up(self.bindings.get(key))

    def _up(self, binding):
        if binding.kind == "mouse":
            if binding.code in self.buttons:
                self.release_mouse(binding.code)
            return
        self.release_key(binding.code)

    def click_mouse(self, button, seconds=.05):
        from task_control import active_session
        session = active_session()
        with (session.foreground_required() if session is not None else nullcontext()):
            self.check(require_foreground=True)
            return super().click_mouse(button, seconds)

    def hold(self, key, seconds, *, restore=True):
        from task_control import active_session
        binding = self.bindings.get(key)
        session = active_session()
        need_fg = session is not None and binding.kind == "mouse"
        with (session.foreground_required() if need_fg else nullcontext()):
            return super().hold(key, seconds, restore=restore)

    def mouse_down(self, button=0):
        if button not in (0, 1, 2, 3, 4):
            raise NavigationError("鼠标按键未登记。")
        self.check(require_foreground=True)
        self.buttons.add(button)
        flags = {0: 0x0002, 1: 0x0008, 2: 0x0020, 3: 0x0080, 4: 0x0080}[button]
        if not send_mouse(self.user32, flags, data=button-2 if button >= 3 else 0):
            raise NavigationError("鼠标动作输入失败。")

    @contextmanager
    def _borrowed_cursor(self, screen_x: int, screen_y: int, *, enabled: bool = True):
        if not enabled:
            yield
            return
        saved = wintypes.POINT()
        restore_cursor = False
        with self._per_monitor_dpi():
            restore_cursor = bool(self.user32.GetCursorPos(ctypes.byref(saved)))
            self.user32.SetCursorPos(screen_x, screen_y)
        try:
            yield
        finally:
            if restore_cursor:
                with self._per_monitor_dpi():
                    self.user32.SetCursorPos(saved.x, saved.y)

    def _window_point_coords(self, x, y):
        if (type(x) not in (int, float) or type(y) not in (int, float)
                or not (x == x and y == y) or not self.user32.IsWindow(self.hwnd)):
            return None
        with self._per_monitor_dpi():
            rc = wintypes.RECT()
            if not self.user32.GetClientRect(self.hwnd, ctypes.byref(rc)):
                return None
            width, height = rc.right - rc.left, rc.bottom - rc.top
            if width <= 0 or height <= 0:
                return None
            cx = max(0, min(width - 1, round(x * width / 1280)))
            cy = max(0, min(height - 1, round(y * height / 720)))
            pt = wintypes.POINT(cx, cy)
            if not self.user32.ClientToScreen(self.hwnd, ctypes.byref(pt)):
                return None
            lparam = ((cy & 0xFFFF) << 16) | (cx & 0xFFFF)
            return lparam, int(pt.x), int(pt.y), cx, cy

    def move_window_point(self, x, y):
        """Post a client-relative WM_MOUSEMOVE without moving the physical cursor."""
        if not self.capturable() or self.stopped():
            return False
        coords = self._window_point_coords(x, y)
        if coords is None:
            return False
        lparam = coords[0]
        if not self.user32.PostMessageW(self.hwnd, 0x0200, 0, lparam):
            raise NavigationError(f"无法向游戏窗口发送后台鼠标移动（错误 {ctypes.get_last_error()}）。")
        return True

    def click_window_point(self, x, y, pause, *, with_cursor_pos=True):
        """Post a client-relative left click to the bound window; briefly borrow cursor if requested."""
        if not self.capturable() or self.stopped():
            return False
        coords = self._window_point_coords(x, y)
        if coords is None:
            return False
        lparam, screen_x, screen_y, _, _ = coords
        with self._borrowed_cursor(screen_x, screen_y, enabled=with_cursor_pos):
            if not self.user32.PostMessageW(self.hwnd, 0x0200, 0, lparam):
                raise NavigationError(f"无法向游戏窗口发送后台鼠标移动（错误 {ctypes.get_last_error()}）。")
            if with_cursor_pos:
                wall_time.sleep(.02)
            if not self.user32.PostMessageW(self.hwnd, 0x0201, 0x0001, lparam):
                raise NavigationError(f"无法向游戏窗口发送后台鼠标点击（错误 {ctypes.get_last_error()}）。")
            try:
                if with_cursor_pos:
                    wall_time.sleep(.05)
                    pressed = not self.stopped()
                else:
                    pressed = pause(.05)
            finally:
                if self.user32.IsWindow(self.hwnd):
                    if not self.user32.PostMessageW(self.hwnd, 0x0202, 0, lparam):
                        raise NavigationError(f"无法释放游戏窗口的后台鼠标点击（错误 {ctypes.get_last_error()}）。")
            if with_cursor_pos:
                wall_time.sleep(.03)
                return pressed and not self.stopped()
            return pressed

    def scroll_window_point(self, x, y, dx, dy):
        """Post WM_MOUSEWHEEL to the target coordinate while briefly borrowing cursor hover."""
        if not self.capturable() or self.stopped():
            return False
        coords = self._window_point_coords(x, y)
        if coords is None:
            return False
        lparam, screen_x, screen_y, _, _ = coords
        # Win32 WM_MOUSEWHEEL lParam carries screen coordinates, not client coordinates.
        screen_lparam = ((screen_y & 0xFFFF) << 16) | (screen_x & 0xFFFF)
        with self._borrowed_cursor(screen_x, screen_y, enabled=True):
            if not self.user32.PostMessageW(self.hwnd, 0x0200, 0, lparam):
                raise NavigationError(f"无法向游戏窗口发送后台鼠标移动（错误 {ctypes.get_last_error()}）。")
            wall_time.sleep(.04)
            if dy:
                delta_y = max(-32768, min(32767, round(dy)))
                if not self.user32.PostMessageW(self.hwnd, 0x020A, (delta_y & 0xFFFF) << 16, screen_lparam):
                    raise NavigationError(f"无法向游戏窗口发送后台滚轮消息（错误 {ctypes.get_last_error()}）。")
            if dx:
                delta_x = max(-32768, min(32767, round(dx)))
                if not self.user32.PostMessageW(self.hwnd, 0x020E, (delta_x & 0xFFFF) << 16, screen_lparam):
                    raise NavigationError(f"无法向游戏窗口发送后台横向滚轮消息（错误 {ctypes.get_last_error()}）。")
            wall_time.sleep(.03)
        return not self.stopped()

    def swipe_window_points(self, begin, end, duration_ms=300):
        """Post a bounded drag between two 1280x720 points while borrowing cursor only during the drag."""
        if (not self.capturable() or self.stopped()
                or not isinstance(begin, (list, tuple)) or len(begin) < 2
                or not isinstance(end, (list, tuple)) or len(end) < 2):
            return False
        start_coords = self._window_point_coords(begin[0], begin[1])
        end_coords = self._window_point_coords(end[0], end[1])
        if start_coords is None or end_coords is None:
            return False
        lp0, sx0, sy0, cx0, cy0 = start_coords
        _, sx1, sy1, cx1, cy1 = end_coords
        duration_s = max(.12, min(1.5, float(duration_ms or 300) / 1000.0))
        steps = max(8, min(30, round(duration_s / .02)))
        last_lp = lp0
        pressed = False
        with self._borrowed_cursor(sx0, sy0, enabled=True):
            if not self.user32.PostMessageW(self.hwnd, 0x0200, 0, lp0):
                raise NavigationError(f"无法向游戏窗口发送后台拖动起点（错误 {ctypes.get_last_error()}）。")
            wall_time.sleep(.02)
            if not self.user32.PostMessageW(self.hwnd, 0x0201, 0x0001, lp0):
                raise NavigationError(f"无法向游戏窗口发送后台拖动按下（错误 {ctypes.get_last_error()}）。")
            pressed = True
            try:
                wall_time.sleep(.02)
                for step in range(1, steps + 1):
                    if self.stopped() or not self.capturable():
                        return False
                    ratio = step / steps
                    cx = round(cx0 + (cx1 - cx0) * ratio)
                    cy = round(cy0 + (cy1 - cy0) * ratio)
                    sx = round(sx0 + (sx1 - sx0) * ratio)
                    sy = round(sy0 + (sy1 - sy0) * ratio)
                    last_lp = ((cy & 0xFFFF) << 16) | (cx & 0xFFFF)
                    with self._per_monitor_dpi():
                        self.user32.SetCursorPos(sx, sy)
                    if not self.user32.PostMessageW(self.hwnd, 0x0200, 0x0001, last_lp):
                        raise NavigationError(f"无法向游戏窗口发送后台拖动轨迹（错误 {ctypes.get_last_error()}）。")
                    wall_time.sleep(duration_s / steps)
                wall_time.sleep(.03)
                return not self.stopped()
            finally:
                if pressed and self.user32.IsWindow(self.hwnd):
                    self.user32.PostMessageW(self.hwnd, 0x0202, 0, last_lp)
                    wall_time.sleep(.02)

    def input_window_text(self, content):
        """Post UTF-16 WM_CHAR code units directly to the bound window."""
        if not isinstance(content, str) or not self.capturable() or self.stopped():
            return False
        for ch in content:
            if self.stopped() or not self.user32.IsWindow(self.hwnd):
                return False
            if ch == "\n":
                if not self._post_window_key(13, down=True) or not self._post_window_key(13, down=False):
                    return False
                continue
            raw = ch.encode("utf-16-le")
            for idx in range(0, len(raw), 2):
                code = int.from_bytes(raw[idx:idx+2], "little")
                if not self.user32.PostMessageW(self.hwnd, 0x0102, code, 1):
                    raise NavigationError(f"无法向游戏窗口发送后台文本（错误 {ctypes.get_last_error()}）。")
        return True


def relative_camera(controller, dx: int, dy: int) -> bool:
    """Prefer native Maa relative motion; bounded fallback for older Seize builds.

    This is part of the application, not a computer-use testing workaround.
    No background or arbitrary-window injection is supported.
    """
    if (type(dx) is not int or type(dy) is not int
            or abs(dx) > MAX_CAMERA_PIXELS or abs(dy) > 300):
        return False
    adapter = ForegroundInput(controller, lambda: False)
    from task_control import active_session
    if active_session() is not None:
        adapter.check()
    if not adapter.foreground():
        return False
    if controller.post_relative_move(dx, dy).wait().succeeded:
        return True
    if active_session() is not None:
        adapter.check()
    if not adapter.foreground():
        return False
    return send_mouse(adapter.user32, 0x0001, dx=dx, dy=dy)

