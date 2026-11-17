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
_WM_ACTIVATE = 0x0006
_WA_ACTIVE = 1
_WM_CHAR = 0x0102
_WM_MOUSEMOVE = 0x0200
_WM_LBUTTONDOWN = 0x0201
_WM_LBUTTONUP = 0x0202
_WM_MBUTTONDOWN = 0x0207
_WM_MBUTTONUP = 0x0208
_WM_MOUSEWHEEL = 0x020A
_WM_MOUSEHWHEEL = 0x020E
_MK_LBUTTON = 0x0001
_MK_MBUTTON = 0x0010
_GA_ROOTOWNER = 3
_SMTO_ABORTIFHUNG = 0x0002
_ERROR_TIMEOUT = 1460
_WINDOW_SEND_TIMEOUT_MS = 2000
_MONITOR_DEFAULTTONEAREST = 2


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


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
    window_only = False
    capture_requires_foreground = False

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

    def capturable(self):
        """Return whether the bound window exists, is unminimized, and satisfies capture requirements."""
        return bool(
            self.user32.IsWindow(self.hwnd)
            and not self.user32.IsIconic(self.hwnd)
            and (not self.capture_requires_foreground or self.foreground())
        )

    def focus_details(self):
        hwnd = self.user32.GetForegroundWindow()
        text = ctypes.create_unicode_buffer(1024)
        if hwnd:
            self.user32.GetWindowTextW(hwnd, text, len(text))
        return {"game_hwnd": self.hwnd, "foreground_hwnd": int(hwnd or 0), "foreground_title": text.value}

    def hung(self):
        return bool(self.user32.IsWindow(self.hwnd) and self.user32.IsHungAppWindow(self.hwnd))

    def _wait_without_session(self, ready):
        if not ready():
            self.release()
            while not ready():
                if self.stopped():
                    raise NavigationError("任务已停止。")
                if not self.user32.IsWindow(self.hwnd):
                    raise GameWindowClosed("连接的游戏窗口已经关闭，请重新连接。")
                time.sleep(.05)

    def check(self, *, require_foreground=True):
        if self.stopped():
            raise NavigationError("已停止导航。")
        from task_control import active_session
        session = active_session()
        if session is not None:
            session.wait(self, require_foreground=require_foreground)
            return
        ready = self.foreground if require_foreground else self.capturable
        self._wait_without_session(ready)

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
    """Window-targeted input with activation notice; pauses on minimize or uncapturable windows."""

    window_only = True

    def __init__(self, controller, stopped, ready=lambda: True, bindings=None):
        super().__init__(controller, stopped, ready=ready, bindings=bindings)
        self._restore_keys = set()
        self._synced_generation = self.generation
        self.user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.user32.GetClientRect.restype = wintypes.BOOL
        self.user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        self.user32.ClientToScreen.restype = wintypes.BOOL
        self.user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        self.user32.ScreenToClient.restype = wintypes.BOOL
        self.user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        self.user32.GetCursorPos.restype = wintypes.BOOL
        self.user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        self.user32.SetCursorPos.restype = wintypes.BOOL
        self.user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user32.GetAncestor.restype = wintypes.HWND
        self.user32.GetLastActivePopup.argtypes = [wintypes.HWND]
        self.user32.GetLastActivePopup.restype = wintypes.HWND
        self.user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user32.IsWindowVisible.restype = wintypes.BOOL
        self.user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        self.user32.MonitorFromWindow.restype = wintypes.HMONITOR
        self.user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MONITORINFO)]
        self.user32.GetMonitorInfoW.restype = wintypes.BOOL
        self.user32.SendMessageTimeoutW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self.user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
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

    def check(self, *, require_foreground=False):
        if self.stopped():
            raise NavigationError("已停止导航。")
        from task_control import active_session
        session = active_session()
        if session is not None:
            session.wait(self, require_foreground=require_foreground, require_capturable=True)
        else:
            ready = self.foreground if require_foreground else self.capturable
            self._wait_without_session(ready)
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
        with (session.foreground_required(message="需要通过物理鼠标点击完成交互") if session is not None else nullcontext()):
            self.check(require_foreground=True)
            return super().click_mouse(button, seconds)

    def hold(self, key, seconds, *, restore=True):
        from task_control import active_session
        binding = self.bindings.get(key)
        session = active_session()
        need_fg = session is not None and binding.kind == "mouse"
        with (session.foreground_required(message="需要持续按住物理鼠标按键") if need_fg else nullcontext()):
            return super().hold(key, seconds, restore=restore)

    def mouse_down(self, button=0):
        if button not in (0, 1, 2, 3, 4):
            raise NavigationError("鼠标按键未登记。")
        self.check(require_foreground=True)
        self.buttons.add(button)
        flags = {0: 0x0002, 1: 0x0008, 2: 0x0020, 3: 0x0080, 4: 0x0080}[button]
        if not send_mouse(self.user32, flags, data=button-2 if button >= 3 else 0):
            raise NavigationError("鼠标动作输入失败。")

    @staticmethod
    def _effective_capture_size(
        width: int,
        height: int,
        capture_size: tuple[int, int] | None = None,
    ) -> tuple[int, int]:
        if (
            capture_size is not None
            and len(capture_size) == 2
            and capture_size[0] > 0
            and capture_size[1] > 0
            and abs((capture_size[0] / capture_size[1]) - (width / height)) <= 0.05
        ):
            return int(capture_size[0]), int(capture_size[1])
        # Tall windows preserve client aspect while scaling the short side (width) to 1280.
        if height > width * 1.5:
            return 1280, max(720, round(1280 * height / width))
        return 1280, 720

    def _clickable_screen_bounds(
        self,
        *,
        margin_px: int = 2,
        check_work_area: bool = True,
    ) -> tuple[int, int, int, int] | None:
        """Compute physical screen bounds inside client area and away from taskbar/offscreen edges."""
        with self._per_monitor_dpi():
            if not self.user32.IsWindow(self.hwnd):
                return None
            rc = wintypes.RECT()
            if not self.user32.GetClientRect(self.hwnd, ctypes.byref(rc)):
                return None
            cw, ch = int(rc.right - rc.left), int(rc.bottom - rc.top)
            if cw <= 0 or ch <= 0:
                return None
            origin = wintypes.POINT(0, 0)
            if not self.user32.ClientToScreen(self.hwnd, ctypes.byref(origin)):
                return None
            c_left, c_top = int(origin.x), int(origin.y)
            c_right, c_bottom = c_left + cw, c_top + ch
            hmon = self.user32.MonitorFromWindow(self.hwnd, _MONITOR_DEFAULTTONEAREST)
            if not hmon:
                return None
            mi = _MONITORINFO()
            mi.cbSize = ctypes.sizeof(_MONITORINFO)
            if not self.user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                return None
            mon, work = mi.rcMonitor, mi.rcWork
            left = max(c_left, int(mon.left))
            top = max(c_top, int(mon.top))
            right = min(c_right, int(mon.right))
            bottom = min(c_bottom, int(mon.bottom))
            if left >= right or top >= bottom:
                return None
            if not check_work_area:
                return left, top, right, bottom
            mon_w = int(mon.right - mon.left)
            mon_h = int(mon.bottom - mon.top)
            is_oversized = ch > mon_h + 2 or cw > mon_w + 2
            is_fullscreen_fg = (
                not is_oversized
                and c_left <= int(mon.left)
                and c_top <= int(mon.top)
                and c_right >= int(mon.right)
                and c_bottom >= int(mon.bottom)
                and int(self.user32.GetForegroundWindow() or 0) == int(self.hwnd)
            )
            if is_fullscreen_fg:
                return left, top, right, bottom
            pad = max(0, int(margin_px))
            s_left = int(work.left) + pad if work.left > mon.left else int(work.left)
            s_top = int(work.top) + pad if work.top > mon.top else int(work.top)
            s_right = int(work.right) - pad if work.right < mon.right else int(work.right)
            s_bottom = int(work.bottom) - pad if work.bottom < mon.bottom else int(work.bottom)
            left = max(left, s_left)
            top = max(top, s_top)
            right = min(right, s_right)
            bottom = min(bottom, s_bottom)
            if left >= right or top >= bottom:
                return None
            return left, top, right, bottom

    def _screen_point_clickable(
        self,
        screen_x: int,
        screen_y: int,
        *,
        margin_px: int = 2,
        check_work_area: bool = True,
    ) -> bool:
        """Verify physical screen coordinates lie within client bounds and never land on taskbar/offscreen."""
        bounds = self._clickable_screen_bounds(
            margin_px=margin_px,
            check_work_area=check_work_area,
        )
        if bounds is None:
            return False
        left, top, right, bottom = bounds
        return left <= screen_x < right and top <= screen_y < bottom

    @contextmanager
    def _borrowed_cursor(
        self,
        screen_x: int,
        screen_y: int,
        *,
        enabled: bool = True,
        lease_ms: int = 450,
    ):
        from mouse_guard import guarded_borrowed_cursor

        clip_rect = (
            self._clickable_screen_bounds(margin_px=2, check_work_area=True)
            if enabled
            else None
        )
        with guarded_borrowed_cursor(
            screen_x,
            screen_y,
            enabled=enabled,
            clickable_check=lambda sx, sy: self._screen_point_clickable(
                sx, sy, margin_px=2, check_work_area=True
            ),
            per_monitor_dpi=self._per_monitor_dpi,
            stopped=self.stopped,
            ready=self.capturable,
            lease_ms=lease_ms,
            clip_rect=clip_rect,
        ) as lease:
            yield lease

    def _active_window_target(self) -> int:
        """Resolve an active visible same-process popup owned by the bound game window."""
        if not self.user32.IsWindow(self.hwnd):
            return 0
        root = self.user32.GetAncestor(self.hwnd, _GA_ROOTOWNER)
        if not root:
            return self.hwnd
        popup = self.user32.GetLastActivePopup(root)
        if not popup or popup == self.hwnd:
            return self.hwnd
        if not self.user32.IsWindow(popup) or not self.user32.IsWindowVisible(popup):
            return self.hwnd
        base_pid = wintypes.DWORD(0)
        popup_pid = wintypes.DWORD(0)
        self.user32.GetWindowThreadProcessId(self.hwnd, ctypes.byref(base_pid))
        self.user32.GetWindowThreadProcessId(popup, ctypes.byref(popup_pid))
        if base_pid.value and popup_pid.value != base_pid.value:
            return self.hwnd
        return int(popup)

    def _send_window_message(
        self,
        target: int,
        msg: int,
        wparam: int,
        lparam: int,
        error_label: str,
        *,
        allow_post_fallback: bool = False,
        ignore_stop: bool = False,
    ) -> bool:
        """Synchronously send a Win32 message with hang protection."""
        if not target or not self.user32.IsWindow(target):
            return False
        if not ignore_stop and self.stopped():
            return False
        result = ctypes.c_size_t(0)
        ctypes.set_last_error(0)
        ok = self.user32.SendMessageTimeoutW(
            target,
            msg,
            wparam,
            lparam,
            _SMTO_ABORTIFHUNG,
            _WINDOW_SEND_TIMEOUT_MS,
            ctypes.byref(result),
        )
        if ok:
            return True
        err = ctypes.get_last_error()
        # Ensure button-up release still reaches a transiently busy window so mouse state never sticks.
        if allow_post_fallback and err in (0, _ERROR_TIMEOUT) and self.user32.IsWindow(target):
            ctypes.set_last_error(0)
            if self.user32.PostMessageW(target, msg, wparam, lparam):
                return True
            err = ctypes.get_last_error() or err
        if self.stopped() or not self.user32.IsWindow(self.hwnd) or not self.user32.IsWindow(target):
            return False
        if err in (0, _ERROR_TIMEOUT):
            return False
        raise NavigationError(f"无法向游戏窗口发送{error_label}（错误 {err}）。")

    def _activate_window(self, target: int) -> bool:
        """Notify an inactive target window of WA_ACTIVE synchronously without stealing foreground."""
        if not target or not self.user32.IsWindow(target):
            return False
        if self.user32.GetForegroundWindow() != target:
            if not self._send_window_message(target, _WM_ACTIVATE, _WA_ACTIVE, 0, "后台激活通知"):
                return False
            wall_time.sleep(0.01)
        return not self.stopped()

    def _window_point_coords(
        self,
        x,
        y,
        *,
        target=None,
        capture_size: tuple[int, int] | None = None,
        check_work_area: bool = True,
    ):
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
            cap_w, cap_h = self._effective_capture_size(width, height, capture_size)
            cx = max(0, min(width - 1, round(x * width / cap_w)))
            cy = max(0, min(height - 1, round(y * height / cap_h)))
            pt = wintypes.POINT(cx, cy)
            if not self.user32.ClientToScreen(self.hwnd, ctypes.byref(pt)):
                return None
            sx, sy = int(pt.x), int(pt.y)
            if not self._screen_point_clickable(sx, sy, margin_px=2, check_work_area=check_work_area):
                return None
            tx, ty = cx, cy
            if target and target != self.hwnd:
                target_pt = wintypes.POINT(sx, sy)
                if self.user32.ScreenToClient(target, ctypes.byref(target_pt)):
                    tx, ty = int(target_pt.x), int(target_pt.y)
            lparam = ((ty & 0xFFFF) << 16) | (tx & 0xFFFF)
            return lparam, sx, sy, tx, ty

    def _prepare_window_points(self, *points, capture_size=None, check_work_area: bool = True):
        """Validate window readiness, resolve target HWND, map capture points, and send activation."""
        if not self.capturable() or self.stopped():
            return None
        target = self._active_window_target()
        coords_list = []
        for pt in points:
            coords = self._window_point_coords(
                pt[0],
                pt[1],
                target=target,
                capture_size=capture_size,
                check_work_area=check_work_area,
            )
            if coords is None:
                return None
            coords_list.append(coords)
        if not self._activate_window(target):
            return None
        return target, coords_list

    def _release_window_button(
        self,
        target: int,
        lparam: int,
        error_label: str,
        *,
        msg: int = _WM_LBUTTONUP,
    ) -> None:
        if self.user32.IsWindow(target):
            self._send_window_message(
                target,
                msg,
                0,
                lparam,
                error_label,
                allow_post_fallback=True,
                ignore_stop=True,
            )

    def move_window_point(self, x, y, *, capture_size=None):
        """Send a synchronous WM_MOUSEMOVE (with activation notice) without moving the cursor."""
        prepared = self._prepare_window_points((x, y), capture_size=capture_size)
        if prepared is None:
            return False
        target, (coords,) = prepared
        return self._send_window_message(target, _WM_MOUSEMOVE, 0, coords[0], "后台鼠标移动")

    def _click_window_button(
        self,
        x,
        y,
        pause,
        *,
        down_msg: int,
        down_wparam: int,
        up_msg: int,
        label: str,
        with_cursor_pos: bool = True,
        capture_size: tuple[int, int] | None = None,
    ) -> bool:
        """Send a synchronous window-targeted mouse button click with optional cursor borrowing."""
        prepared = self._prepare_window_points(
            (x, y),
            capture_size=capture_size,
            check_work_area=bool(with_cursor_pos),
        )
        if prepared is None:
            return False
        target, ((lparam, screen_x, screen_y, _, _),) = prepared
        with self._borrowed_cursor(screen_x, screen_y, enabled=with_cursor_pos) as cursor_ok:
            if not cursor_ok:
                return False
            if with_cursor_pos:
                wall_time.sleep(0.002)
            if not self._send_window_message(target, _WM_MOUSEMOVE, 0, lparam, "后台鼠标移动"):
                return False
            wall_time.sleep(0.02 if with_cursor_pos else 0.01)
            if not self._send_window_message(target, down_msg, down_wparam, lparam, label):
                return False
            try:
                if with_cursor_pos:
                    wall_time.sleep(0.05)
                    pressed = not self.stopped()
                else:
                    pressed = (
                        bool(pause(0.05))
                        if pause is not None
                        else (wall_time.sleep(0.05), not self.stopped())[1]
                    )
            finally:
                self._release_window_button(target, lparam, f"{label}释放", msg=up_msg)
            if with_cursor_pos:
                wall_time.sleep(0.03)
                return pressed and not self.stopped()
            return pressed

    def click_window_point(self, x, y, pause, *, with_cursor_pos=True, capture_size=None):
        """Send a synchronous left click to the target window after activation; briefly borrow cursor if requested."""
        return self._click_window_button(
            x,
            y,
            pause,
            down_msg=_WM_LBUTTONDOWN,
            down_wparam=_MK_LBUTTON,
            up_msg=_WM_LBUTTONUP,
            label="后台鼠标点击",
            with_cursor_pos=with_cursor_pos,
            capture_size=capture_size,
        )

    def click_window_middle(self, pause=None, *, with_cursor_pos=True, capture_size=None):
        """Send a middle-button click to center the game camera on character facing without stealing focus."""
        if not self.capturable() or self.stopped():
            return False
        if self.foreground():
            # Never warp the cursor while the 3D viewport owns foreground mouse look.
            if 2 in self.buttons:
                return False
            self.buttons.add(2)
            if not send_mouse(self.user32, 0x0020):
                self.buttons.discard(2)
                raise NavigationError("鼠标中键输入失败。")
            try:
                if pause is not None:
                    pressed = bool(pause(0.05))
                else:
                    wall_time.sleep(0.05)
                    pressed = not self.stopped()
                return pressed and not self.stopped()
            finally:
                self.release_mouse(2)
        with self._per_monitor_dpi():
            rc = wintypes.RECT()
            if not self.user32.GetClientRect(self.hwnd, ctypes.byref(rc)):
                return False
            width, height = int(rc.right - rc.left), int(rc.bottom - rc.top)
        if width <= 0 or height <= 0:
            return False
        cap_w, cap_h = self._effective_capture_size(width, height, capture_size)
        return self._click_window_button(
            cap_w * 0.5,
            cap_h * 0.5,
            pause,
            down_msg=_WM_MBUTTONDOWN,
            down_wparam=_MK_MBUTTON,
            up_msg=_WM_MBUTTONUP,
            label="后台中键点击",
            with_cursor_pos=with_cursor_pos,
            capture_size=(cap_w, cap_h),
        )

    def scroll_window_point(self, x, y, dx, dy, *, capture_size=None):
        """Send synchronous WM_MOUSEWHEEL to the target coordinate after activation and cursor hover."""
        prepared = self._prepare_window_points((x, y), capture_size=capture_size)
        if prepared is None:
            return False
        target, ((lparam, screen_x, screen_y, _, _),) = prepared
        # Win32 WM_MOUSEWHEEL lParam carries screen coordinates, not client coordinates.
        screen_lparam = ((screen_y & 0xFFFF) << 16) | (screen_x & 0xFFFF)
        with self._borrowed_cursor(screen_x, screen_y, enabled=True) as cursor_ok:
            if not cursor_ok:
                return False
            wall_time.sleep(0.002)
            if not self._send_window_message(target, _WM_MOUSEMOVE, 0, lparam, "后台鼠标移动"):
                return False
            wall_time.sleep(0.04)
            if dy:
                delta_y = max(-32768, min(32767, round(dy)))
                if not self._send_window_message(
                    target, _WM_MOUSEWHEEL, (delta_y & 0xFFFF) << 16, screen_lparam, "后台滚轮消息"
                ):
                    return False
            if dx:
                delta_x = max(-32768, min(32767, round(dx)))
                if not self._send_window_message(
                    target, _WM_MOUSEHWHEEL, (delta_x & 0xFFFF) << 16, screen_lparam, "后台横向滚轮消息"
                ):
                    return False
            wall_time.sleep(0.03)
        return not self.stopped()

    def swipe_window_points(self, begin, end, duration_ms=300, *, capture_size=None):
        """Send a synchronous bounded drag between two capture points after activation."""
        if (not isinstance(begin, (list, tuple)) or len(begin) < 2
                or not isinstance(end, (list, tuple)) or len(end) < 2):
            return False
        prepared = self._prepare_window_points(begin, end, capture_size=capture_size)
        if prepared is None:
            return False
        target, ((lp0, sx0, sy0, cx0, cy0), (_, sx1, sy1, cx1, cy1)) = prepared
        duration_s = max(0.12, min(1.5, float(duration_ms or 300) / 1000.0))
        steps = max(8, min(30, round(duration_s / 0.02)))
        last_lp = lp0
        pressed = False
        with self._borrowed_cursor(sx0, sy0, enabled=True, lease_ms=450) as cursor_lease:
            if not cursor_lease:
                return False
            wall_time.sleep(0.002)
            if not self._send_window_message(target, _WM_MOUSEMOVE, 0, lp0, "后台拖动起点"):
                return False
            wall_time.sleep(0.02)
            if not self._send_window_message(target, _WM_LBUTTONDOWN, _MK_LBUTTON, lp0, "后台拖动按下"):
                return False
            pressed = True
            try:
                wall_time.sleep(0.02)
                for step in range(1, steps + 1):
                    if self.stopped() or not self.capturable():
                        return False
                    ratio = step / steps
                    cx = round(cx0 + (cx1 - cx0) * ratio)
                    cy = round(cy0 + (cy1 - cy0) * ratio)
                    sx = round(sx0 + (sx1 - sx0) * ratio)
                    sy = round(sy0 + (sy1 - sy0) * ratio)
                    if not self._screen_point_clickable(sx, sy, margin_px=2, check_work_area=True):
                        return False
                    last_lp = ((cy & 0xFFFF) << 16) | (cx & 0xFFFF)
                    cursor_lease.renew(350)
                    with self._per_monitor_dpi():
                        self.user32.SetCursorPos(sx, sy)
                    if not self._send_window_message(
                        target, _WM_MOUSEMOVE, _MK_LBUTTON, last_lp, "后台拖动轨迹"
                    ):
                        return False
                    wall_time.sleep(duration_s / steps)
                wall_time.sleep(0.03)
                return not self.stopped()
            finally:
                if pressed:
                    self._release_window_button(target, last_lp, "后台拖动释放")
                    wall_time.sleep(0.02)

    def input_window_text(self, content):
        """Send UTF-16 WM_CHAR code units synchronously after activation."""
        if not isinstance(content, str) or not self.capturable() or self.stopped():
            return False
        target = self._active_window_target()
        if not self._activate_window(target):
            return False
        enter_down, enter_up, enter_flags = _window_key_spec(self.user32, 13)
        for ch in content:
            if self.stopped() or not self.user32.IsWindow(target):
                return False
            if ch == "\n":
                if (not self._send_window_message(target, enter_down, 13, enter_flags, "后台回车按下")
                        or not self._send_window_message(target, enter_up, 13, enter_flags | 0xC0000000, "后台回车释放")):
                    return False
                wall_time.sleep(0.02)
                continue
            raw = ch.encode("utf-16-le")
            for idx in range(0, len(raw), 2):
                code = int.from_bytes(raw[idx:idx + 2], "little")
                if not self._send_window_message(target, _WM_CHAR, code, 1, "后台文本"):
                    return False
                wall_time.sleep(0.02)
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

