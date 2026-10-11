"""Resident exploration helpers with window-targeted keyboard interaction."""
from __future__ import annotations

from contextlib import nullcontext
from task_control import active_session, task_time as time

from navigation.controller import ForegroundInput
from navigation.models import NavigationError
from performance import checkpoint, count, measure
from .runtime import Runtime, parameters


class AssistSchedule:
    """Wall-clock checks; fishing exclusively owns input during its session."""
    INTERVALS = {"Fishing": .3, "Clear": .3, "Dialogue": .5, "Pickup": .1}
    COOLDOWNS = {"Fishing": 5, "Clear": .3, "Dialogue": .25, "Pickup": .02}

    def __init__(self, enabled):
        self.next_check = {name: 0.0 for name in self.INTERVALS if enabled.get(name, False)}

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
        session = active_session()
        polling = session.coalesce_stop_queries() if session else nullcontext()
        observation = session.background_observation() if session else nullcontext()
        with self.ui.cache_assets(), polling, observation:
            return self._run_assist()

    def _run_assist(self):
        enabled = {key: parameters(self.context, "MaaNikki_Assist_" + key).get("value", True)
                   for key in ("Pickup", "Clear", "Dialogue")}
        enabled["Fishing"] = parameters(self.context, "MaaNikki_Assist_Fishing").get("value", False)
        if not any(enabled.values()):
            self.log("请至少开启一项开荒辅助功能。")
            return False
        guard = ForegroundInput(self.controller, lambda: self.stopped, bindings=self.game_keys)
        binding = self.game_keys.get("interact")
        self.log("开荒辅助已启动：键盘交互支持切出游戏后继续拾取、对白和跳过；鼠标交互及钓鱼仅在游戏前台执行。最小化后暂停。手动停止可结束辅助。")
        schedule, waiting_clear = AssistSchedule(enabled), False
        previous_mode = None

        def available():
            return (not self.stopped and guard.user32.IsWindow(guard.hwnd)
                    and not guard.user32.IsIconic(guard.hwnd)
                    and (binding.kind == "key" or guard.foreground()))

        @measure("input.assist")
        def press():
            # Scene interaction must not reposition even to a sampled cursor:
            # capture scaling/rounding and user movement can change that point.
            session = active_session()
            if session and session.stopped(fresh=True):
                return False
            if not available():
                return False
            if binding.kind == "key":
                if self.check_focus():
                    return False
                self.invalidate_frame()
                result = guard.click_window_key(binding.code, self.pause)
                if result:
                    count("assist.window_key")
                    if not guard.foreground():
                        count("assist.background_key")
                return result
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
                foreground = guard.foreground()
                mode = ("minimized" if guard.user32.IsIconic(guard.hwnd) else
                        "foreground" if foreground else "background" if binding.kind == "key" else "paused")
                if mode != previous_mode:
                    self.log({"foreground": "开荒辅助在游戏前台运行，键盘交互使用 PostMessage。",
                              "background": "开荒辅助继续后台识别，交互键通过 PostMessage 发往游戏窗口。",
                              "minimized": "游戏已最小化，开荒辅助暂停识别和输入。",
                              "paused": "交互键设置为鼠标按键，切出游戏后暂停；切回后继续。"}[mode])
                    previous_mode = mode
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
                    if helper == "Fishing":
                        from .fishing import fishing_opportunity, run_fishing
                        schedule.checked(helper, time.monotonic())
                        if not guard.foreground():
                            continue
                        try:
                            opportunity = fishing_opportunity(self, guard, frame)
                        except NavigationError:
                            if guard.foreground():
                                raise
                            break
                        if not opportunity:
                            continue
                        # A fishing session owns the inputs until it ends; the
                        # pickup/dialogue helpers must not act on its prompts.
                        if guard.foreground():
                            try:
                                run_fishing(self, guard, passive=True)
                                count("assist.Fishing")
                            except NavigationError:
                                if guard.foreground():
                                    raise
                            finally:
                                guard.release()
                                schedule.checked(helper, time.monotonic(), acted=True)
                        break
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
