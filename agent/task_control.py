"""Cooperative foreground pause and active-time deadlines for game tasks."""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
import time as wall_time
import traceback
from weakref import WeakSet

from navigation.models import NavigationError
from game_view import GameFrameSizeError, require_game_frame
from message_log import write_message
from diagnostics import FailureEvidence
from performance import count


class TaskStopped(NavigationError):
    pass


_ACTIVE = ContextVar("foreground_task", default=None)
_SESSIONS = OrderedDict()
_RUNNING = {}


def active_session():
    return _ACTIVE.get()


class ForegroundSession:
    def __init__(self, context):
        self.context = context
        self._controller = None
        self.adapter = None
        self.inputs = WeakSet()
        self.started = False
        self.paused_seconds = 0.0
        self.generation = 0
        self.waiting = False
        self._stop_poll_interval = 0.0
        self._stop_checked_at = None
        self._cached_stopping = False
        self._background_observation = False
        self._observed_foreground = None
        self.frame_validated = False
        self._frame_failure = None
        self.evidence = FailureEvidence()

    def register(self, inputs):
        self.inputs.add(inputs)

    @property
    def controller(self):
        # Repeated Tasker.controller queries replace the Agent's native proxy,
        # invalidating controllers already held by Runtime/input adapters.
        # All nested callbacks must use one proxy for the active root scope.
        if self._controller is None:
            self._controller = self.context.tasker.controller
        return self._controller

    def notify(self, message):
        self.context.run_task("MaaNikki_Daily_Message", {
            "MaaNikki_Daily_Message": {"focus": {"Node.Action.Starting": message}}})

    def validate_frame(self, frame):
        """Reject incompatible coordinates once, before any recovery can click."""
        if self._frame_failure:
            raise TaskStopped(self._frame_failure)
        try:
            require_game_frame(frame)
        except GameFrameSizeError as error:
            self.evidence.save("frame_size", str(error), image=frame, generation=self.generation)
            message = f"任务已停止：{error}"
            self._frame_failure = message
            write_message(message, message)
            for inputs in list(self.inputs):
                try:
                    inputs.release()
                except Exception:
                    traceback.print_exc()
            try:
                self.notify(message)
            except Exception:
                traceback.print_exc()
            finally:
                try:
                    self.context.tasker.post_stop()
                except Exception:
                    traceback.print_exc()
            raise TaskStopped(message) from error
        self.frame_validated = True

    @contextmanager
    def coalesce_stop_queries(self, interval=.05):
        """Bound resident-helper IPC polling without caching window focus."""
        previous = self._stop_poll_interval
        self._stop_poll_interval = interval
        self._stop_checked_at = None
        self._cached_stopping = False
        try:
            yield
        finally:
            self._stop_poll_interval = previous
            self._stop_checked_at = None
            self._cached_stopping = False

    def stopped(self, *, fresh=False):
        if self._frame_failure:
            return True
        now = wall_time.monotonic()
        if (not fresh and self._stop_poll_interval > 0 and self._stop_checked_at is not None
                and (self._cached_stopping or now-self._stop_checked_at < self._stop_poll_interval)):
            count("control.stop_reused")
            return self._cached_stopping
        stopping = bool(self.context.tasker.stopping)
        # Timestamp after the IPC response so slow queries do not immediately repeat.
        self._stop_checked_at = wall_time.monotonic()
        self._cached_stopping = stopping
        count("control.stop_queries")
        return stopping

    @contextmanager
    def background_observation(self):
        """Allow resident-helper captures; physical input still requires focus."""
        previous = self._background_observation
        self._background_observation = True
        self._observed_foreground = None
        self.generation += 1
        try:
            yield
        finally:
            self._background_observation = previous
            self._observed_foreground = None
            self.generation += 1

    def wait(self, adapter=None, *, require_foreground=False):
        if self.waiting:
            return
        if adapter is None:
            if self.adapter is None:
                from navigation.controller import ForegroundInput
                self.adapter = ForegroundInput(self.controller, self.stopped)
            adapter = self.adapter
        if not adapter.user32.IsWindow(adapter.hwnd):
            for inputs in list(self.inputs):
                inputs.release()
            raise NavigationError("连接的游戏窗口已经关闭，请重新连接。")
        if not self.started:
            if self.stopped():
                raise TaskStopped("任务已停止。")
            self.started = True
            # Keep automatic startup focus, but never refocus a paused task.
            if not self._background_observation and not adapter.foreground():
                if not adapter.controller.post_key_up(18).wait().succeeded:
                    raise NavigationError("未能自动聚焦游戏，请检查窗口连接。")
        if self._background_observation and not require_foreground:
            if self.stopped():
                raise TaskStopped("任务已停止。")
            foreground = adapter.foreground()
            if self._observed_foreground is not None and foreground != self._observed_foreground:
                for inputs in list(self.inputs):
                    inputs.release()
                self.generation += 1
            self._observed_foreground = foreground
            return
        if adapter.foreground():
            return
        if self.stopped():
            raise TaskStopped("任务已停止。")
        begin = wall_time.monotonic()
        self.waiting = True
        try:
            for inputs in list(self.inputs):
                inputs.release()
            self.notify("游戏已离开前台，任务暂停；切回无限暖暖后自动继续。")
            stable_since = None
            while True:
                if self.stopped() or adapter.stopped():
                    raise TaskStopped("任务已停止。")
                if not adapter.user32.IsWindow(adapter.hwnd):
                    raise NavigationError("连接的游戏窗口已经关闭，请重新连接。")
                now = wall_time.monotonic()
                if adapter.foreground():
                    stable_since = now if stable_since is None else stable_since
                    if now-stable_since >= .15:
                        break
                else:
                    stable_since = None
                wall_time.sleep(.05)
            self.generation += 1
            self.notify("游戏已回到前台，任务继续。")
        finally:
            self.paused_seconds += wall_time.monotonic()-begin
            self.waiting = False


@contextmanager
def task_session(context, task_id):
    parent = active_session()
    if parent is not None:
        yield parent
        return
    tasker = getattr(context, "tasker", None)
    if tasker is None:
        yield None
        return
    handle = getattr(tasker, "_handle", None)
    handle = getattr(handle, "value", handle)
    tasker_key = handle if type(handle) is int else id(tasker)
    if tasker_key in _RUNNING:
        # Native nested callbacks can arrive on another Python thread.
        # They must share their waiting parent's input gate and active clock.
        session = _RUNNING[tasker_key]
        token = _ACTIVE.set(session)
        try:
            yield session
        finally:
            _ACTIVE.reset(token)
        return
    key = (tasker_key, task_id)
    session = _SESSIONS.get(key)
    if session is None:
        session = ForegroundSession(context)
        _SESSIONS[key] = session
        while len(_SESSIONS) > 16:
            _SESSIONS.popitem(last=False)
    else:
        _SESSIONS.move_to_end(key)
        session.context = context
    token = _ACTIVE.set(session)
    _RUNNING[tasker_key] = session
    try:
        yield session
    finally:
        try:
            for inputs in list(session.inputs):
                inputs.release()
            if session._frame_failure or context.tasker.stopping:
                _SESSIONS.pop(key, None)
        finally:
            # Native proxy/context lifetimes end with the callback. Persist
            # only pause accounting across successive pipeline nodes.
            session.inputs.clear()
            session.evidence.release_frame()
            session.frame_validated = False
            session.adapter = session._controller = session.context = None
            _RUNNING.pop(tasker_key, None)
            _ACTIVE.reset(token)


class TaskTime:
    def monotonic(self):
        session = active_session()
        if session is not None:
            session.wait()
        return wall_time.monotonic()-(session.paused_seconds if session else 0)

    def sleep(self, seconds):
        if active_session() is None:
            return wall_time.sleep(seconds)
        deadline = self.monotonic()+seconds
        while True:
            if active_session().stopped():
                raise TaskStopped("任务已停止。")
            remaining = deadline-self.monotonic()
            if remaining <= 0:
                return
            wall_time.sleep(min(.05, remaining))

    def __getattr__(self, name):
        return getattr(wall_time, name)


task_time = TaskTime()
