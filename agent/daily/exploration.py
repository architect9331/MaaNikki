"""Opt-in, foreground-only helpers while the player explores manually."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import time

from navigation.controller import ForegroundInput
from performance import checkpoint, count, measure
from .runtime import Runtime, parameters


class AssistSchedule:
    """Wall-clock deadlines: an active helper never owns an inner input loop."""
    INTERVALS = {"Clear": .3, "Dialogue": .5, "Pickup": .1}
    COOLDOWNS = {"Clear": .3, "Dialogue": .25, "Pickup": .02}

    def __init__(self, enabled):
        self.next_check = {name: 0.0 for name in self.INTERVALS if enabled[name]}

    def due(self, now):
        return [name for name, deadline in self.next_check.items() if now >= deadline]

    def checked(self, name, now, *, acted=False):
        interval = self.COOLDOWNS[name] if acted else self.INTERVALS[name]
        self.next_check[name] = now+interval

    def delay(self, now, *, clearing=False):
        deadline = self.next_check["Clear"] if clearing else min(self.next_check.values())
        return min(.1, max(0, deadline-now))


class Exploration(Runtime):
    def run_assist(self):
        enabled = {key: parameters(self.context, "MaaNikki_Assist_" + key).get("value", True)
                   for key in ("Pickup", "Clear", "Dialogue")}
        if not any(enabled.values()):
            self.log("请至少开启一项开荒辅助功能。")
            return False
        guard = ForegroundInput(self.controller, lambda: self.stopped, bindings=self.game_keys)
        for name, signature in (("GetCursorPos", [ctypes.POINTER(wintypes.POINT)]),
                                ("ScreenToClient", [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]),
                                ("GetClientRect", [wintypes.HWND, ctypes.POINTER(wintypes.RECT)])):
            function = getattr(guard.user32, name)
            function.argtypes = signature
            function.restype = wintypes.BOOL
        self.log("开荒辅助已启动：请切回游戏自由探索；切出游戏会暂停输入，回来后继续。手动停止可结束辅助。")
        schedule, waiting_clear = AssistSchedule(enabled), False

        def available():
            return not self.stopped and guard.foreground()

        @measure("input.assist")
        def press(frame):
            # Do not use the center of the screen for mouse-bound interaction.
            if not available():
                return False
            binding = self.game_keys.get("interact")
            if binding.kind == "key":
                if not available():
                    return False
                self.invalidate_frame()
                return self.controller.post_click_key(binding.code).wait().succeeded
            point, rect = wintypes.POINT(), wintypes.RECT()
            if not (guard.user32.GetCursorPos(ctypes.byref(point))
                    and guard.user32.ScreenToClient(guard.hwnd, ctypes.byref(point))
                    and guard.user32.GetClientRect(guard.hwnd, ctypes.byref(rect))):
                return False
            if not (0 <= point.x < rect.right and 0 <= point.y < rect.bottom):
                return False
            if frame is None or not available():
                return False
            height, width = frame.shape[:2]
            self.invalidate_frame()
            return self.controller.post_click(round(point.x * width / rect.right),
                round(point.y * height / rect.bottom), contact=binding.code).wait().succeeded

        def found(name, frame=None):
            roi = self.ui.roi("AreaPickup") if name == "IconPickupFeature" else None
            return bool(self.ui.asset(name, image=frame, roi=roi, gray=(210, 255),
                                      scale=2/3 if name == "IconPickupFeature" else 1,
                                      threshold=.75 if name == "IconPickupFeature" else .73))

        try:
            while not self.stopped:
                checkpoint()
                if not guard.user32.IsWindow(guard.hwnd):
                    # Stay resident until manually stopped, but never send to a stale HWND.
                    if not self.pause(.5):
                        break
                    continue
                if not available():
                    self.pause(.1)
                    continue
                delay = schedule.delay(time.monotonic(), clearing=waiting_clear)
                if delay:
                    self.pause(delay)
                    continue
                # One capture per decision; after any input the next iteration
                # captures again. Do not carry detections across scene changes.
                frame = self.capture()
                if frame is None:
                    self.pause(.1)
                    continue
                if waiting_clear:
                    if not found("IconSkip", frame):
                        waiting_clear = False
                    schedule.checked("Clear", time.monotonic())
                    continue
                icons = {"Clear": "IconSkip", "Dialogue": "IconSkipDialog", "Pickup": "IconPickupFeature"}
                for helper in schedule.due(time.monotonic()):
                    matched = found(icons[helper], frame)
                    schedule.checked(helper, time.monotonic())
                    if not matched:
                        continue
                    if available() and press(frame):
                        count("assist."+helper)
                        schedule.checked(helper, time.monotonic(), acted=True)
                        waiting_clear = helper == "Clear"
                    # Submit at most one input for this frame, even on failure.
                    # Skips/dialogue precede pickup when multiple features match.
                    break
            return True
        finally:
            guard.release()
