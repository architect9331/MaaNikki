"""Cooperative foreground pause and active-time deadlines for game tasks."""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from threading import RLock
import time as wall_time
import traceback
from weakref import WeakSet

from client_config import node_action_params
from navigation.models import GameWindowClosed, NavigationError
from game_view import GameFrameSizeError, require_game_frame
from message_log import write_message
from diagnostics import FailureEvidence
from performance import count


class TaskStopped(NavigationError):
    pass


class ForegroundChanged(RuntimeError):
    """A guarded input was cancelled before submission after a focus pause."""


_ACTIVE = ContextVar("foreground_task", default=None)
REFOCUS_INTERVAL = 3
# Allow up to 90s for genuine cross-map UE5 loading/shader compilation stalls
# while still aborting true deadlocks confirmed by active WM_NULL timeouts.
HUNG_WINDOW_TIMEOUT = 90.0
HUNG_NOTICE_DELAY = 5.0
_SESSIONS_LOCK = RLock()
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
        self.pause_generation = 0
        self.waiting = False
        self._stop_poll_interval = 0.03
        self._stop_checked_at = None
        self._cached_stopping = False
        self._background_observation = False
        self._observed_foreground = None
        self._foreground_required_depth = 0
        self._foreground_pause_message = None
        self._tasker_key = None
        self._hud = None
        self._task_label = ""
        self._stage_label = ""
        self._step_text = ""
        self._observation_hud_mode = "observation"
        self._manage_observation_hud = True
        self._focus_lost_cache = None
        self._exclude_cache = None
        self._hud_mode_cache = None
        self.frame_validated = False
        self.account_uid = None
        self.account_generation = None
        self.account_mismatch = False
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

    def _cached_context_setting(self, attr, ttl, loader):
        """Cache per-context node parameters briefly to avoid tight-loop IPC queries."""
        now = wall_time.monotonic()
        cached = getattr(self, attr)
        if cached is not None and cached[0] is self.context and 0.0 <= now - cached[1] < ttl:
            return cached[2]
        resolved = loader()
        setattr(self, attr, (self.context, now, resolved))
        return resolved

    def _focus_lost_mode(self):
        def load():
            try:
                mode = node_action_params(self.context, "MaaNikki_FocusLost").get("mode")
                return mode if mode in {"pause", "return", "background"} else "pause"
            except Exception:
                traceback.print_exc()
                return "pause"
        return self._cached_context_setting("_focus_lost_cache", 0.25, load)

    def _hud_mode_setting(self):
        from game_hud import _read_hud_mode
        return self._cached_context_setting("_hud_mode_cache", 0.5, lambda: _read_hud_mode(self.context))

    def _exclude_from_capture_setting(self, controller):
        if controller is None:
            return None
        cached = self._exclude_cache
        if cached is not None and cached[0] is controller:
            return cached[1]
        from overlay_style import should_exclude_from_capture
        resolved = should_exclude_from_capture(controller)
        self._exclude_cache = (controller, resolved)
        return resolved

    def _background_mode(self):
        return self._focus_lost_mode() == "background"

    def _background_active(self):
        return self._background_observation or self._background_mode()

    def _returns_to_game(self):
        return self._focus_lost_mode() == "return"

    def _control_mode(self):
        if self._background_observation:
            return self._observation_hud_mode or "observation"
        return self._focus_lost_mode()

    def _resolve_hwnd(self, hwnd=None):
        if hwnd:
            return int(hwnd)
        adapter_hwnd = getattr(self.adapter, "hwnd", 0)
        if adapter_hwnd:
            return int(adapter_hwnd)
        try:
            ctrl = self.controller
            info = getattr(ctrl, "info", None)
            val = info.get("hwnd", 0) if isinstance(info, dict) else 0
            return int(val or 0)
        except Exception:
            return 0

    @property
    def hud(self):
        return self._hud

    def ensure_hud(self, hwnd=None, *, refresh=False):
        if self._tasker_key is None or self.context is None:
            return None
        previous = self._hud
        if (not refresh and previous is not None and previous.alive()
                and (not hwnd or int(hwnd) == previous.game)):
            return previous
        game_hwnd = self._resolve_hwnd(hwnd)
        if game_hwnd <= 0:
            return previous
        from game_hud import acquire_game_hud
        ctrl = self._controller
        hud = acquire_game_hud(
            self._tasker_key,
            game_hwnd,
            controller=ctrl,
            context=self.context,
            exclude_from_capture=self._exclude_from_capture_setting(ctrl),
            mode_setting=self._hud_mode_setting(),
        )
        self._hud = hud
        if hud is not None:
            if hud is not previous:
                hud.set_paused(False)
                hud.clear_forced()
                hud.clear_route()
                hud.clear_exploration_hint()
            manage_hud = not self._background_observation or self._manage_observation_hud
            hud.set_context(
                control_mode=self._control_mode() if manage_hud else None,
                task_label=self._task_label or None,
                stage_label=self._stage_label,
                step_text=self._step_text if manage_hud else None,
            )
        return hud

    def set_task_label(self, label, *, reset_stage=False):
        clean = str(label or "").strip()
        if not clean:
            return
        if clean != self._task_label:
            self._task_label = clean
            if reset_stage:
                self._stage_label = ""
        elif reset_stage:
            self._stage_label = ""
        if self._hud is not None:
            self._hud.set_task(clean, stage="" if reset_stage else None)

    def set_stage_label(self, label):
        self._stage_label = str(label or "").strip()
        if self._hud is not None:
            self._hud.set_stage(self._stage_label)

    def set_step_text(self, text):
        self._step_text = str(text or "").strip()
        if self._hud is not None:
            self._hud.set_step(self._step_text)

    def ensure_adapter(self):
        if self.adapter is None:
            if self._background_mode():
                from navigation.controller import BackgroundInput
                self.adapter = BackgroundInput(self.controller, self.stopped)
            else:
                from navigation.controller import ForegroundInput
                self.adapter = ForegroundInput(self.controller, self.stopped)
        return self.adapter

    def window_closed(self):
        """Stop the queue: every task after a closed game could only fail too.

        The resident helper handles this itself and waits for the game to return.
        """
        for inputs in list(self.inputs):
            inputs.release()
        if not self._background_observation:
            try:
                self.notify("游戏窗口已关闭，后续任务已停止。重新打开游戏后再运行。")
            except Exception:
                traceback.print_exc()
            finally:
                try:
                    self.context.tasker.post_stop()
                except Exception:
                    traceback.print_exc()
        raise GameWindowClosed("连接的游戏窗口已经关闭，请重新连接。")

    def _stop_queue_with_failure(self, stage, message, *, details=None):
        """Save evidence, release inputs, halt the task queue, and abort execution."""
        self.evidence.save(stage, message, details=details, generation=self.generation)
        self._frame_failure = message
        write_message(message, message)
        for inputs in list(self.inputs):
            try:
                inputs.release()
            except Exception:
                traceback.print_exc()
        if not self._background_observation:
            try:
                self.notify(message)
            except Exception:
                traceback.print_exc()
            finally:
                try:
                    self.context.tasker.post_stop()
                except Exception:
                    traceback.print_exc()
        raise TaskStopped(message)

    def window_hung(self, *, details=None):
        """Stop the queue when the connected game window stays unresponsive past recovery."""
        self._stop_queue_with_failure(
            "window_hung",
            "游戏窗口长时间未响应，后续任务已停止。请恢复或重启游戏后再运行。",
            details=details,
        )

    def game_disconnected(self, *, message=None):
        """Stop the queue when the game shows a disconnect, relogin, or hotfix-restart modal."""
        self._stop_queue_with_failure(
            "game_disconnected",
            message or "检测到游戏断线或需要重新登录提示，后续任务已停止。请重新进入游戏后再运行。",
        )

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

    def bump_generation(self, *, paused=True):
        self.generation += 1
        if paused:
            self.pause_generation += 1

    @contextmanager
    def background_observation(self, *, hud_mode="observation", manage_hud=True):
        """Allow resident-helper captures; physical input still requires focus."""
        previous = self._background_observation
        previous_mode = self._observation_hud_mode
        previous_manage = self._manage_observation_hud
        self._background_observation = True
        self._observation_hud_mode = hud_mode or "observation"
        self._manage_observation_hud = bool(manage_hud)
        self._observed_foreground = None
        self.bump_generation(paused=True)
        if self._hud is not None and self._manage_observation_hud:
            self._hud.set_context(control_mode=self._control_mode())
        try:
            yield
        finally:
            self._background_observation = previous
            self._observation_hud_mode = previous_mode
            self._manage_observation_hud = previous_manage
            self._observed_foreground = None
            self.bump_generation(paused=True)
            if self._hud is not None and (not self._background_observation or self._manage_observation_hud):
                self._hud.set_context(control_mode=self._control_mode())

    @contextmanager
    def suspend_background_observation(self):
        """Temporarily exit background_observation when a UI modal requires foreground clicks."""
        if not self._background_observation:
            yield
            return
        previous = self._background_observation
        self._background_observation = False
        self._observed_foreground = None
        self.bump_generation(paused=True)
        if self._hud is not None:
            self._hud.set_context(control_mode=self._control_mode())
        try:
            yield
        finally:
            self._background_observation = previous
            self._observed_foreground = None
            self.bump_generation(paused=True)
            if self._hud is not None and (not self._background_observation or self._manage_observation_hud):
                self._hud.set_context(control_mode=self._control_mode())

    @contextmanager
    def foreground_required(self, *, message=None):
        """Seize and auto-pull foreground while a physical-mouse/camera phase runs."""
        was_depth = self._foreground_required_depth
        self._foreground_required_depth += 1
        previous_message = self._foreground_pause_message
        if message is not None and (was_depth == 0 or not self._foreground_pause_message):
            self._foreground_pause_message = message
        bg_mode = self._background_mode()
        if self._hud is not None and bg_mode and self._foreground_required_depth == 1:
            self._hud.set_forced(
                self._foreground_pause_message or "当前阶段需要使用物理鼠标或视角控制",
                active=True,
            )
        try:
            yield
        finally:
            self._foreground_pause_message = previous_message
            self._foreground_required_depth = max(0, self._foreground_required_depth - 1)
            if self._foreground_required_depth == 0 and self._hud is not None:
                if bg_mode and not self.stopped():
                    self._hud.clear_forced(
                        recovered_msg="物理鼠标/视角阶段已完成，可随时切回其他窗口",
                        recovered_seconds=3.5,
                    )
                else:
                    self._hud.clear_forced()

    @staticmethod
    def _bring_to_foreground(adapter):
        if adapter.foreground():
            return True
        user32 = adapter.user32
        hwnd = adapter.hwnd
        try:
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        except Exception:
            pass
        try:
            adapter.controller.post_key_up(18).wait()
        except Exception:
            pass
        if adapter.foreground():
            return True
        # Fall back to thread-queue attach + Win32 activation if focus lock blocks Seize key-up.
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=False)
            kernel32.GetCurrentThreadId.argtypes = []
            kernel32.GetCurrentThreadId.restype = wintypes.DWORD
            user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            user32.GetWindowThreadProcessId.restype = wintypes.DWORD
            user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
            user32.AttachThreadInput.restype = wintypes.BOOL
            user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.ShowWindow.restype = wintypes.BOOL
            user32.BringWindowToTop.argtypes = [wintypes.HWND]
            user32.BringWindowToTop.restype = wintypes.BOOL
            user32.SetForegroundWindow.argtypes = [wintypes.HWND]
            user32.SetForegroundWindow.restype = wintypes.BOOL
            current_thread = kernel32.GetCurrentThreadId()
            fg_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
            target_thread = user32.GetWindowThreadProcessId(hwnd, None)
            attached = []
            try:
                for thread in dict.fromkeys((fg_thread, target_thread)):
                    if thread and thread != current_thread and user32.AttachThreadInput(current_thread, thread, True):
                        attached.append(thread)
                if user32.IsIconic(hwnd):
                    user32.ShowWindow(hwnd, 9)
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
            finally:
                for thread in reversed(attached):
                    user32.AttachThreadInput(current_thread, thread, False)
        except Exception:
            pass
        return adapter.foreground()

    def _hung_evidence_details(self, adapter, *, waited_s: float, bring_back: bool) -> dict:
        details = {}
        hung_details = getattr(adapter, "hung_details", None)
        if callable(hung_details):
            try:
                details.update(hung_details())
            except Exception:
                pass
        details["waited_seconds"] = round(waited_s, 2)
        details["timeout_seconds"] = HUNG_WINDOW_TIMEOUT
        details["control_mode"] = self._control_mode()
        details["bring_back"] = bool(bring_back)
        return details

    def _wait_for_hung_recovery(
        self,
        adapter,
        hud,
        *,
        manage_hud: bool,
        pause_if_unfocused: bool,
        pause_if_uncapturable: bool,
        bring_back: bool,
    ) -> str:
        """Wait for a genuinely unresponsive active window to recover, or exit to pause."""
        hung_begin = wall_time.monotonic()
        self.waiting = True
        notified_stall = False
        try:
            for inputs in list(self.inputs):
                inputs.release()
            while True:
                if self.stopped() or adapter.stopped():
                    raise TaskStopped("任务已停止。")
                if not adapter.user32.IsWindow(adapter.hwnd):
                    self.window_closed()
                if adapter.user32.IsIconic(adapter.hwnd):
                    return "paused"
                if pause_if_uncapturable and not adapter.capturable():
                    return "paused"
                if pause_if_unfocused and not adapter.foreground():
                    return "paused"
                if not adapter.hung():
                    break
                elapsed = wall_time.monotonic() - hung_begin
                if not notified_stall and elapsed >= HUNG_NOTICE_DELAY:
                    notified_stall = True
                    write_message("游戏窗口暂时未响应，正在等待恢复。", None)
                    if hud is not None and manage_hud:
                        hud.set_paused(True, reason="游戏窗口暂时未响应，正在等待恢复…")
                if elapsed >= HUNG_WINDOW_TIMEOUT:
                    self.window_hung(
                        details=self._hung_evidence_details(
                            adapter,
                            waited_s=elapsed,
                            bring_back=bring_back,
                        )
                    )
                wall_time.sleep(.2)
            self.bump_generation(paused=True)
            return "recovered"
        finally:
            self.paused_seconds += wall_time.monotonic() - hung_begin
            self.waiting = False
            if notified_stall and hud is not None and manage_hud:
                hud.set_paused(False)
                if self.stopped():
                    hud.clear_forced()

    def _wait_until(self, adapter, ready, pause_message, resume_message, *, check_hung, bring_back):
        if self.stopped():
            raise TaskStopped("任务已停止。")
        begin = wall_time.monotonic()
        self.waiting = True
        try:
            for inputs in list(self.inputs):
                inputs.release()
            self.notify(pause_message)
            stable_since = None
            refocused_at = None
            hung_since = None
            notified_hung = False
            while True:
                if self.stopped() or adapter.stopped():
                    raise TaskStopped("任务已停止。")
                if not adapter.user32.IsWindow(adapter.hwnd):
                    self.window_closed()
                now = wall_time.monotonic()
                is_iconic = bool(adapter.user32.IsIconic(adapter.hwnd))
                if check_hung and not is_iconic and adapter.hung():
                    hung_since = now if hung_since is None else hung_since
                    elapsed_hung = now - hung_since
                    if not notified_hung and elapsed_hung >= HUNG_NOTICE_DELAY:
                        notified_hung = True
                        write_message("游戏窗口暂时未响应，正在等待恢复。", None)
                        if self._hud is not None:
                            self._hud.set_paused(True, reason="游戏窗口暂时未响应，正在等待恢复…")
                    if elapsed_hung >= HUNG_WINDOW_TIMEOUT:
                        self.window_hung(
                            details=self._hung_evidence_details(
                                adapter,
                                waited_s=elapsed_hung,
                                bring_back=bring_back,
                            )
                        )
                else:
                    hung_since = None
                    notified_hung = False
                if ready() and (not check_hung or hung_since is None):
                    stable_since = now if stable_since is None else stable_since
                    if now-stable_since >= .15:
                        break
                else:
                    stable_since = None
                    # Spaced out so a user alt-tabbing away can still reach the stop button.
                    if (bring_back and hung_since is None
                            and (refocused_at is None or now-refocused_at >= REFOCUS_INTERVAL)):
                        refocused_at = now
                        self._bring_to_foreground(adapter)
                wall_time.sleep(.05)
            self.bump_generation(paused=True)
            if self._background_active():
                self._observed_foreground = adapter.foreground()
            self.notify(resume_message)
        finally:
            self.paused_seconds += wall_time.monotonic()-begin
            self.waiting = False
            if self._hud is not None:
                self._hud.set_paused(False)
                if self.stopped():
                    self._hud.clear_forced()

    def wait(self, adapter=None, *, require_foreground=False, require_capturable=False):
        if self.waiting:
            return
        bg_mode = self._background_mode()
        bg_active = self._background_observation or bg_mode
        effective_foreground = require_foreground or self._foreground_required_depth > 0
        manage_hud = not self._background_observation or self._manage_observation_hud
        if adapter is None:
            adapter = self.ensure_adapter()
        hud = self.ensure_hud(getattr(adapter, "hwnd", 0))
        if hud is not None and manage_hud:
            hud.set_context(control_mode=self._control_mode())
        if not adapter.user32.IsWindow(adapter.hwnd):
            self.window_closed()
        if not self.started:
            if self.stopped():
                raise TaskStopped("任务已停止。")
            self.started = True
            # Keep automatic startup focus only for foreground runs.
            if not bg_active and not adapter.foreground():
                if not adapter.controller.post_key_up(18).wait().succeeded:
                    raise NavigationError("未能自动聚焦游戏，请检查窗口连接。")
        while True:
            if self.stopped():
                raise TaskStopped("任务已停止。")
            if not adapter.user32.IsWindow(adapter.hwnd):
                self.window_closed()
            if bg_active and not effective_foreground:
                window_only = bool(getattr(adapter, "window_only", False))
                foreground = adapter.foreground()
                if self._observed_foreground is not None and foreground != self._observed_foreground:
                    for inputs in list(self.inputs):
                        # Keep window-targeted movement keys held across focus switches
                        # when the window remains capturable; release physical inputs.
                        if not (getattr(inputs, "window_only", False) and inputs.capturable()):
                            inputs.release()
                    self.bump_generation(paused=False)
                self._observed_foreground = foreground
                need_capturable = require_capturable or window_only
                if need_capturable and not adapter.capturable():
                    pause_message = (
                        "游戏窗口已最小化，任务暂停；恢复窗口后自动继续。"
                        if adapter.user32.IsIconic(adapter.hwnd)
                        else "游戏已离开前台且当前截图方式需要前台画面，任务暂停；切回无限暖暖后自动继续。"
                    )
                    if hud is not None:
                        hud.set_paused(
                            True,
                            reason=("游戏窗口已最小化，请恢复窗口后继续"
                                    if adapter.user32.IsIconic(adapter.hwnd)
                                    else "当前截图方式需要前台画面，切回无限暖暖后继续"),
                        )
                    self._wait_until(
                        adapter,
                        adapter.capturable,
                        pause_message,
                        "游戏窗口已恢复，任务继续。",
                        check_hung=False,
                        bring_back=False,
                    )
                    continue
                if (
                    not self._background_observation
                    and not adapter.user32.IsIconic(adapter.hwnd)
                    and adapter.hung()
                ):
                    state = self._wait_for_hung_recovery(
                        adapter,
                        hud,
                        manage_hud=manage_hud,
                        pause_if_unfocused=False,
                        pause_if_uncapturable=need_capturable,
                        bring_back=False,
                    )
                    if state == "paused":
                        continue
                if hud is not None and (manage_hud or self._foreground_required_depth > 0):
                    hud.set_paused(False)
                    if self._foreground_required_depth == 0:
                        hud.clear_forced()
                return
            allow_auto_focus = not self._background_observation
            bg_temp_fg = allow_auto_focus and bg_mode and effective_foreground
            return_fg = allow_auto_focus and (not bg_temp_fg) and self._returns_to_game()
            bring_back = bg_temp_fg or return_fg
            if adapter.foreground():
                if not self._background_observation and adapter.hung():
                    state = self._wait_for_hung_recovery(
                        adapter,
                        hud,
                        manage_hud=manage_hud,
                        pause_if_unfocused=not bring_back,
                        pause_if_uncapturable=False,
                        bring_back=bring_back,
                    )
                    if state == "paused":
                        continue
                if not adapter.foreground():
                    continue
                if hud is not None:
                    hud.set_paused(False)
                    if bg_mode and self._foreground_required_depth > 0:
                        hud.set_forced(
                            self._foreground_pause_message or "当前阶段需要使用物理鼠标或视角控制",
                            active=True,
                        )
                    elif self._foreground_required_depth == 0:
                        hud.clear_forced()
                return
            pause_message = (
                (self._foreground_pause_message
                 or "当前操作需要使用前台鼠标或视角控制，正在自动将游戏切回前台并保持至本阶段结束。")
                if bg_temp_fg else
                ("游戏当前动作需要在前台执行，任务暂停；切回无限暖暖后自动继续。"
                 if bg_active else
                 ("游戏已离开前台，正在切回游戏继续运行。"
                  if return_fg else "游戏已离开前台，任务暂停；切回无限暖暖后自动继续。"))
            )
            if hud is not None:
                if bg_temp_fg:
                    hud.set_forced(
                        self._foreground_pause_message or "当前操作需要使用物理鼠标或视角控制",
                        active=True,
                    )
                elif return_fg:
                    hud.set_forced("已开启「回到游戏」模式，正在自动拉回游戏窗口", active=True)
                else:
                    hud.set_paused(
                        True,
                        reason=("当前动作需要在前台执行，切回游戏后继续"
                                if bg_active else "游戏已离开前台自动暂停，切回窗口后继续"),
                    )
            self._wait_until(
                adapter,
                (lambda: adapter.foreground() and not adapter.hung()) if bring_back else adapter.foreground,
                pause_message,
                "游戏已回到前台，任务继续。",
                check_hung=bool(bring_back and not self._background_observation),
                bring_back=bring_back,
            )
            if hud is not None and return_fg and not self.stopped():
                hud.clear_forced(
                    flash_reason="已开启「回到游戏」模式，游戏离开前台时会自动切回（可在设置中改为「自动暂停」或「后台运行」）",
                    flash_seconds=4.5,
                )
            if bring_back:
                return


@contextmanager
def task_session(context, task_id, *, node_name=""):
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
    with _SESSIONS_LOCK:
        running = _RUNNING.get(tasker_key)
    if running is not None:
        # Native nested callbacks can arrive on another Python thread.
        # They must share their waiting parent's input gate and active clock.
        session = running
        token = _ACTIVE.set(session)
        try:
            yield session
        finally:
            _ACTIVE.reset(token)
        return
    key = (tasker_key, task_id)
    with _SESSIONS_LOCK:
        session = _SESSIONS.get(key)
        if session is None:
            session = ForegroundSession(context)
            _SESSIONS[key] = session
            while len(_SESSIONS) > 16:
                _SESSIONS.popitem(last=False)
        else:
            _SESSIONS.move_to_end(key)
            session.context = context
        session._tasker_key = tasker_key
        _RUNNING[tasker_key] = session
    if node_name:
        from game_hud import resolve_task_label
        resolved = resolve_task_label(node_name, session._task_label)
        if resolved:
            session.set_task_label(resolved)
    token = _ACTIVE.set(session)
    stopping = False
    try:
        session.ensure_hud(refresh=True)
        yield session
    finally:
        try:
            for inputs in list(session.inputs):
                inputs.release()
            stopping = bool(session._frame_failure or context.tasker.stopping)
            if stopping:
                with _SESSIONS_LOCK:
                    _SESSIONS.pop(key, None)
        finally:
            from game_hud import release_game_hud
            release_game_hud(tasker_key, immediate=stopping)
            if stopping:
                session._hud = None
            # Native proxy/context lifetimes end with the callback. Persist
            # only pause accounting across successive pipeline nodes.
            session.inputs.clear()
            session.evidence.release_frame()
            session.frame_validated = False
            session._foreground_required_depth = 0
            session._foreground_pause_message = None
            session._manage_observation_hud = True
            session._focus_lost_cache = None
            session._exclude_cache = None
            session._hud_mode_cache = None
            session._stop_checked_at = None
            session._cached_stopping = False
            session.adapter = session._controller = session.context = None
            with _SESSIONS_LOCK:
                _RUNNING.pop(tasker_key, None)
            _ACTIVE.reset(token)


class TaskTime:
    def monotonic(self):
        session = active_session()
        if session is not None:
            session.wait()
        return wall_time.monotonic()-(session.paused_seconds if session else 0)

    def sleep_for(self, seconds, *, check=None, aborted=None):
        """Sleep for `seconds` of active time, running `check` once per wakeup tick."""
        session = active_session()
        if session is None:
            if seconds > 0 and not (aborted and aborted()):
                wall_time.sleep(seconds)
            return
        gate = check or session.wait
        gate()
        deadline = wall_time.monotonic()-session.paused_seconds+seconds
        while True:
            if aborted and aborted():
                return
            if session.stopped():
                raise TaskStopped("任务已停止。")
            remaining = deadline-(wall_time.monotonic()-session.paused_seconds)
            if remaining <= 0:
                return
            wall_time.sleep(min(.05, remaining))
            gate()

    def sleep(self, seconds):
        self.sleep_for(seconds)

    def __getattr__(self, name):
        return getattr(wall_time, name)


task_time = TaskTime()
