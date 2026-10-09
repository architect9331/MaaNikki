"""Opt-in, foreground-only helpers while the player explores manually."""
from __future__ import annotations

import time

from navigation.controller import ForegroundInput
from navigation.models import NavigationError
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
        self.log("开荒辅助已启动：请切回游戏自由探索；切出游戏会暂停输入，回来后继续。手动停止可结束辅助。")
        schedule, waiting_clear = AssistSchedule(enabled), False

        def available():
            return not self.stopped and guard.foreground()

        @measure("input.assist")
        def press():
            # Scene interaction must not reposition even to a sampled cursor:
            # capture scaling/rounding and user movement can change that point.
            if not available():
                return False
            binding = self.game_keys.get("interact")
            if binding.kind == "key":
                if not available():
                    return False
                self.invalidate_frame()
                return self.controller.post_click_key(binding.code).wait().succeeded
            if not available():
                return False
            self.invalidate_frame()
            try:
                return guard.click_mouse(binding.code)
            except NavigationError:
                # The resident helper pauses on focus loss, including loss
                # during the short pulse; click_mouse has already released it.
                if not available():
                    return False
                raise

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
                    if available() and press():
                        count("assist."+helper)
                        schedule.checked(helper, time.monotonic(), acted=True)
                        waiting_clear = helper == "Clear"
                    # Submit at most one input for this frame, even on failure.
                    # Skips/dialogue precede pickup when multiple features match.
                    break
            return True
        finally:
            guard.release()
