from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import re
import time

from .models import NavigationError
from game_keys import GameKeys, MACRO_KEYS, MOVEMENT


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
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.GetForegroundWindow.argtypes = []
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.IsWindow.argtypes = [wintypes.HWND]
        self.user32.IsWindow.restype = wintypes.BOOL
        self.user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        self.user32.GetWindowTextLengthW.restype = ctypes.c_int
        self.user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user32.GetWindowTextW.restype = ctypes.c_int
        if not self.user32.IsWindow(self.hwnd):
            raise NavigationError("连接的游戏窗口已经关闭，请重新连接。")
        length = self.user32.GetWindowTextLengthW(self.hwnd)
        if not 0 < length <= 256:
            raise NavigationError("无法确认连接窗口的标题，导航已停止。")
        title = ctypes.create_unicode_buffer(length+1)
        if (not self.user32.GetWindowTextW(self.hwnd, title, len(title))
                or not re.fullmatch(r"\s*(Infinity Nikki|无限暖暖)\s*", title.value)):
            raise NavigationError("当前连接的窗口不是无限暖暖，导航已停止，请检查连接对象。")

    def foreground(self):
        return bool(self.user32.IsWindow(self.hwnd) and self.user32.GetForegroundWindow() == self.hwnd)

    def check(self):
        if self.stopped():
            raise NavigationError("已停止导航。")
        if not self.foreground():
            raise NavigationError("游戏已离开前台，导航已停止，请恢复游戏窗口后重试。")

    def pulse(self, keys, seconds, jump=False):
        if not keys or any(k not in MOVEMENT for k in keys) or not 0.04 <= seconds <= 0.35:
            raise NavigationError("移动参数超出安全范围。")
        try:
            self.bindings.movement()
            resolved = [self.bindings.get(k) for k in keys]
            jumping = self.bindings.get("jump") if jump else None
            if jumping and jumping.kind == "key" and jumping.code in self.bindings.movement():
                raise ValueError("跳跃按键不能与移动方向相同。")
        except ValueError as error:
            raise NavigationError(str(error)) from error
        self.check()
        if not self.ready():
            raise NavigationError("当前不是可移动的主界面，导航已停止。")
        try:
            for binding in resolved:
                self.check()
                # Track before submission so a failed/interrupted down still gets an up.
                self._down(binding)
            if jump:
                self._down(jumping)
            deadline = time.monotonic()+seconds
            while time.monotonic() < deadline:
                self.check()
                time.sleep(min(0.02, max(0, deadline-time.monotonic())))
        finally:
            self.release()

    def release(self):
        # Maa Seize's key_up can refocus the game. Direct ups on focus loss avoid
        # stealing focus while still preventing globally stuck movement keys.
        for key in list(self.held):
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
        for button in list(self.buttons):
            self.release_mouse(button)

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
            self.wait(seconds)
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

    def key_down(self, key):
        if key not in MACRO_KEYS:
            raise NavigationError("宏按键未登记。")
        self.check()
        self._down(self.bindings.get(key))

    def _down(self, binding):
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

    def wait(self, seconds):
        if not 0 <= seconds <= 8:
            raise NavigationError("动作等待时间超出限制。")
        deadline = time.monotonic()+seconds
        while time.monotonic() < deadline:
            self.check()
            time.sleep(min(.05, max(0, deadline-time.monotonic())))


def relative_camera(controller, dx: int, dy: int) -> bool:
    """Prefer native Maa relative motion; bounded fallback for older Seize builds.

    This is part of the application, not a computer-use testing workaround.
    No background or arbitrary-window injection is supported.
    """
    if (type(dx) is not int or type(dy) is not int
            or abs(dx) > MAX_CAMERA_PIXELS or abs(dy) > 300):
        return False
    adapter = ForegroundInput(controller, lambda: False)
    if not adapter.foreground():
        return False
    if controller.post_relative_move(dx, dy).wait().succeeded:
        return True
    if not adapter.foreground():
        return False
    return send_mouse(adapter.user32, 0x0001, dx=dx, dy=dy)

