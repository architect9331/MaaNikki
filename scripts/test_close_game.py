"""Offline close-game regressions: python/python.exe -B scripts/test_close_game.py."""
import ast
import json
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

import game_close
from game_close import (PROCESS_TERMINATE, CloseResult, bound_game_hwnd,
                        close_connected_game, close_launcher, launcher_eligible,
                        select_launcher_for_game)


class WinError(OSError):
    def __init__(self, code):
        super().__init__(code, f"win32 error {code}")
        self.winerror = code


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class FakeApi:
    """Scripted Win32 surface; time only moves inside wait_process."""

    def __init__(self, clock):
        self.clock = clock
        self.windows = {}
        self.processes = {}
        self.handles = {}
        self.closed_handles = []
        self.calls = []
        self.errors = {}
        self.hung_windows = set()
        self.post_close_fail = set()
        self.denied_access = 0
        self.after_open = None
        self.before_wait = None
        self._next_handle = 100

    def add_process(self, pid, image, *, on_close=None, on_terminate=None, created=0):
        self.processes[pid] = {"image": image, "exit_at": None, "on_close": on_close,
                               "on_terminate": on_terminate, "created": created}
        return pid

    def add_window(self, hwnd, pid, *, title="无限暖暖", cls="UnrealWindow", visible=True,
                   visible_at=None):
        self.windows[hwnd] = {"pid": pid, "title": title, "cls": cls, "visible": visible,
                              "gone_at": None, "gone_after_close": None, "visible_at": visible_at}
        return hwnd

    def add_game(self, hwnd=1, pid=10, image=r"C:\Game\X6Game-Win64-Shipping.exe", *,
                 on_close=1.0, on_terminate=0.5, title="无限暖暖", cls="UnrealWindow",
                 visible=True, window_gone_after_close=None):
        self.add_process(pid, image, on_close=on_close, on_terminate=on_terminate, created=1234)
        self.add_window(hwnd, pid, title=title, cls=cls, visible=visible)
        self.windows[hwnd]["gone_after_close"] = window_gone_after_close
        return hwnd

    def _raise(self, name):
        error = self.errors.get(name)
        if error is not None:
            raise error

    def _alive(self, pid):
        proc = self.processes.get(pid)
        return bool(proc) and (proc["exit_at"] is None or self.clock.now < proc["exit_at"])

    def _window(self, hwnd):
        window = self.windows.get(hwnd)
        if not window or not self._alive(window["pid"]):
            return None
        if window["gone_at"] is not None and self.clock.now >= window["gone_at"]:
            return None
        return window

    def is_window(self, hwnd):
        self._raise("is_window")
        return self._window(hwnd) is not None

    def is_visible(self, hwnd):
        window = self._window(hwnd)
        if not window or not window["visible"]:
            return False
        visible_at = window.get("visible_at")
        return visible_at is None or self.clock.now >= visible_at

    def is_hung(self, hwnd):
        self._raise("is_hung")
        return hwnd in self.hung_windows

    def window_pid(self, hwnd):
        window = self._window(hwnd)
        return window["pid"] if window else 0

    def window_class(self, hwnd):
        window = self._window(hwnd)
        return window["cls"] if window else ""

    def window_title(self, hwnd):
        window = self._window(hwnd)
        return window["title"] if window else ""

    def enum_windows(self):
        self._raise("enum_windows")
        return [hwnd for hwnd in self.windows if self._window(hwnd)]

    def list_processes(self):
        self._raise("list_processes")
        return [(pid, proc["image"].split("\\")[-1])
                for pid, proc in self.processes.items() if self._alive(pid)]

    def open_process(self, pid, access):
        self.calls.append(("open_process", pid, access))
        if access & self.denied_access:
            raise WinError(5)
        self._raise("open_process")
        handle = self._next_handle
        self._next_handle += 1
        self.handles[handle] = pid
        if self.after_open:
            self.after_open()
        return handle

    def process_image(self, handle):
        return self.processes[self.handles[handle]]["image"]

    def process_creation_time(self, handle):
        return self.processes[self.handles[handle]]["created"]

    def wait_process(self, handle, seconds):
        self._raise("wait_process")
        if self.before_wait:
            self.before_wait()
        proc = self.processes[self.handles[handle]]
        end = self.clock.now + seconds
        if proc["exit_at"] is not None and proc["exit_at"] <= end:
            self.clock.now = max(self.clock.now, proc["exit_at"])
            return True
        self.clock.now = end
        return False

    def post_close(self, hwnd):
        self.calls.append(("post_close", hwnd))
        if hwnd in self.post_close_fail:
            raise WinError(5)
        self._raise("post_close")
        window = self.windows[hwnd]
        proc = self.processes[window["pid"]]
        if window["gone_after_close"] is not None:
            window["gone_at"] = self.clock.now + window["gone_after_close"]
        if proc["on_close"] is not None:
            proc["exit_at"] = self.clock.now + proc["on_close"]

    def terminate(self, handle):
        self.calls.append(("terminate", handle))
        self._raise("terminate")
        proc = self.processes[self.handles[handle]]
        if proc["on_terminate"] is not None:
            proc["exit_at"] = self.clock.now + proc["on_terminate"]

    def close_handle(self, handle):
        self.closed_handles.append(handle)

    def called(self, name):
        return [call for call in self.calls if call[0] == name]


class CloseFlowTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.api = FakeApi(self.clock)

    def run_close(self, hwnd, **kwargs):
        kwargs.setdefault("countdown_seconds", 0.0)
        kwargs.setdefault("clock", self.clock)
        result = close_connected_game(self.api, hwnd, **kwargs)
        self.assertIsInstance(result, CloseResult)
        # Every opened handle is released exactly once on every branch.
        self.assertEqual(sorted(self.api.handles), sorted(self.api.closed_handles))
        return result

    def test_not_running_is_idempotent_success(self):
        result = self.run_close(None)
        self.assertEqual(result.status, "already_stopped")
        self.assertTrue(result.ok)
        self.assertFalse(launcher_eligible(result))
        self.assertEqual(self.api.calls, [])

    def test_bound_window_closes_normally_without_terminate(self):
        hwnd = self.api.add_game(on_close=2.0)
        result = self.run_close(hwnd)
        self.assertEqual(result.status, "closed")
        self.assertTrue(launcher_eligible(result))
        self.assertEqual(len(self.api.called("post_close")), 1)
        self.assertEqual(self.api.called("terminate"), [])
        self.assertFalse(self.api.called("open_process")[0][2] & PROCESS_TERMINATE)

    def test_window_gone_before_process_keeps_waiting(self):
        hwnd = self.api.add_game(on_close=6.0, window_gone_after_close=0.5)
        result = self.run_close(hwnd)
        self.assertEqual(result.status, "closed")
        self.assertGreaterEqual(self.clock.now, 6.0)

    def test_window_already_gone_but_pinned_process_alive(self):
        hwnd = self.api.add_game(on_close=None)

        def vanish():
            # The window disappears right after pinning; the process exits later.
            self.api.windows[hwnd]["gone_at"] = self.clock.now
            self.api.processes[10]["exit_at"] = 3.0

        self.api.before_wait = vanish
        result = self.run_close(hwnd)
        self.assertEqual(result.status, "closed")
        self.assertEqual(self.api.called("post_close"), [])
        self.assertFalse(result.details["close_requested"])
        self.assertFalse(launcher_eligible(result))

    def test_timeout_without_force_never_terminates(self):
        hwnd = self.api.add_game(on_close=None)
        result = self.run_close(hwnd)
        self.assertEqual(result.status, "timeout")
        self.assertFalse(result.ok)
        self.assertEqual(self.api.called("terminate"), [])
        self.assertAlmostEqual(self.clock.now, game_close.GRACE_SECONDS, places=3)

    def test_timeout_with_force_terminates_pinned_handle_once(self):
        hwnd = self.api.add_game(on_close=None, on_terminate=0.5)
        result = self.run_close(hwnd, force_on_timeout=True)
        self.assertEqual(result.status, "forced_exit")
        self.assertTrue(result.ok)
        terminate = self.api.called("terminate")
        self.assertEqual(len(terminate), 1)
        self.assertEqual(self.api.handles[terminate[0][1]], 10)
        # No PID re-open after waiting.
        self.assertEqual(len(self.api.called("open_process")), 1)

    def test_force_falls_back_to_graceful_when_terminate_right_denied(self):
        hwnd = self.api.add_game(on_close=1.0)
        self.api.denied_access = PROCESS_TERMINATE
        self.assertEqual(self.run_close(hwnd, force_on_timeout=True).status, "closed")

    def test_force_without_terminate_right_reports_access_denied(self):
        hwnd = self.api.add_game(on_close=None)
        self.api.denied_access = PROCESS_TERMINATE
        result = self.run_close(hwnd, force_on_timeout=True)
        self.assertEqual(result.status, "access_denied")
        self.assertEqual(self.api.called("terminate"), [])

    def test_open_process_denied(self):
        hwnd = self.api.add_game()
        self.api.errors["open_process"] = WinError(5)
        result = self.run_close(hwnd)
        self.assertEqual(result.status, "access_denied")
        self.assertEqual(self.api.called("post_close"), [])

    def test_post_close_denied(self):
        hwnd = self.api.add_game()
        self.api.errors["post_close"] = WinError(5)
        self.assertEqual(self.run_close(hwnd).status, "access_denied")

    def test_post_close_generic_failure_never_forces(self):
        hwnd = self.api.add_game()
        self.api.errors["post_close"] = WinError(1400)
        result = self.run_close(hwnd, force_on_timeout=True)
        self.assertEqual(result.status, "error")
        self.assertEqual(self.api.called("terminate"), [])

    def test_terminate_denied(self):
        hwnd = self.api.add_game(on_close=None)
        self.api.errors["terminate"] = WinError(5)
        self.assertEqual(self.run_close(hwnd, force_on_timeout=True).status, "access_denied")

    def test_terminate_then_wait_timeout(self):
        hwnd = self.api.add_game(on_close=None, on_terminate=None)
        result = self.run_close(hwnd, force_on_timeout=True)
        self.assertEqual(result.status, "force_timeout")
        self.assertFalse(result.ok)
        self.assertIn("已请求强制终止", result.message)
        self.assertIn("仍未确认退出", result.message)

    def test_target_already_exited_after_pinning(self):
        hwnd = self.api.add_game()
        self.api.before_wait = lambda: self.api.processes[10].update(exit_at=self.clock.now)
        result = self.run_close(hwnd)
        self.assertEqual(result.status, "already_stopped")
        self.assertEqual(self.api.called("post_close"), [])

    def test_cancel_during_wait_never_escalates(self):
        hwnd = self.api.add_game(on_close=None)
        result = self.run_close(hwnd, force_on_timeout=True, stopped=lambda: self.clock.now >= 3)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(self.api.called("terminate"), [])
        self.assertLess(self.clock.now, game_close.GRACE_SECONDS)

    def test_cancel_before_request(self):
        hwnd = self.api.add_game()
        result = self.run_close(hwnd, stopped=lambda: True)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.message, "任务已停止，未发送关闭请求。")
        self.assertEqual(self.api.called("post_close"), [])

    def test_window_owner_changes_after_open(self):
        hwnd = self.api.add_game()
        self.api.processes[11] = dict(self.api.processes[10])
        self.api.after_open = lambda: self.api.windows[hwnd].update(pid=11)
        result = self.run_close(hwnd)
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(self.api.called("post_close"), [])

    def test_bound_window_must_match_game_rule(self):
        hwnd = self.api.add_game(title="记事本", cls="Notepad")
        result = self.run_close(hwnd)
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(self.api.called("open_process"), [])

    def test_bound_window_gone_with_other_instance_is_not_touched(self):
        self.api.add_game(hwnd=2, pid=20)
        result = self.run_close(1)
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(self.api.called("open_process"), [])

    def test_bound_window_gone_and_nothing_left(self):
        self.assertEqual(self.run_close(1).status, "already_stopped")

    def test_fallback_requires_single_window(self):
        self.api.add_game(hwnd=1, pid=10)
        self.api.add_game(hwnd=2, pid=20)
        self.assertEqual(self.run_close(None).status, "ambiguous")
        self.assertEqual(self.api.called("open_process"), [])

    def test_fallback_single_window_closes(self):
        self.api.add_game(hwnd=1, pid=10)
        self.assertEqual(self.run_close(None).status, "closed")

    def test_fallback_rejects_unknown_executable_but_binding_does_not(self):
        self.api.add_game(hwnd=1, pid=10, image=r"C:\Other\Unknown.exe")
        self.assertEqual(self.run_close(None).status, "ambiguous")
        self.assertEqual(self.api.called("post_close"), [])
        self.assertEqual(self.run_close(1).status, "closed")

    def test_background_process_without_window_is_not_killed(self):
        self.api.add_game(hwnd=1, pid=10)
        self.api.windows[1]["visible"] = False
        result = self.run_close(None, force_on_timeout=True)
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(self.api.called("open_process"), [])

    def test_enumeration_failure_is_not_treated_as_stopped(self):
        self.api.errors["enum_windows"] = WinError(8)
        result = self.run_close(None)
        self.assertEqual(result.status, "error")
        self.assertFalse(result.ok)

    def test_before_close_runs_once_and_errors_do_not_block(self):
        hwnd = self.api.add_game()
        seen = []

        def hook(target):
            seen.append(target)
            raise RuntimeError("restore failed")

        result = self.run_close(hwnd, before_close=hook)
        self.assertEqual(result.status, "closed")
        self.assertEqual(seen, [hwnd])
        self.assertIn("before_close_error", result.details)

    def test_repeat_run_after_close_is_idempotent(self):
        hwnd = self.api.add_game()
        self.assertEqual(self.run_close(hwnd).status, "closed")
        self.assertEqual(self.run_close(hwnd).status, "already_stopped")


class CountdownTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.api = FakeApi(self.clock)

    def run_close(self, hwnd, **kwargs):
        kwargs.setdefault("clock", self.clock)
        result = close_connected_game(self.api, hwnd, **kwargs)
        self.assertEqual(sorted(self.api.handles), sorted(self.api.closed_handles))
        return result

    def test_countdown_logs_fifteen_lines_then_closes(self):
        hwnd = self.api.add_game(on_close=2.0)
        ticks = []
        send_clock = []

        def hook(target):
            send_clock.append(self.clock.now)

        result = self.run_close(hwnd, countdown_seconds=15.0,
                                on_countdown=ticks.append, before_close=hook)
        self.assertEqual(result.status, "closed")
        self.assertEqual(ticks, list(range(15, 0, -1)))
        # The countdown phase alone consumed 15 s; closing happened afterwards.
        self.assertAlmostEqual(send_clock[0], 15.0, places=6)
        self.assertAlmostEqual(self.clock.now, 17.0, places=6)

    def test_countdown_exit_returns_distinct_success(self):
        hwnd = self.api.add_game(on_close=None)
        fired = []

        def once():
            if not fired:
                fired.append(True)
                self.api.processes[10]["exit_at"] = self.clock.now + 0.5

        self.api.before_wait = once
        ticks = []
        result = self.run_close(hwnd, countdown_seconds=15.0, on_countdown=ticks.append)
        self.assertEqual(result.status, "exited_during_countdown")
        self.assertTrue(result.ok)
        self.assertNotEqual(result.details.get("close_requested"), True)
        self.assertFalse(launcher_eligible(result))
        self.assertEqual(len(ticks), 1)
        self.assertEqual(self.api.called("post_close"), [])

    def test_countdown_cancelled_midway(self):
        hwnd = self.api.add_game(on_close=None)
        ticks = []
        result = self.run_close(hwnd, countdown_seconds=15.0, on_countdown=ticks.append,
                                stopped=lambda: self.clock.now >= 3)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.message, "任务已停止，未发送关闭请求。")
        self.assertEqual(ticks, [15, 14, 13])
        self.assertEqual(self.api.called("post_close"), [])

    def test_countdown_final_second_cancel(self):
        hwnd = self.api.add_game(on_close=None)
        ticks = []
        result = self.run_close(hwnd, countdown_seconds=15.0, on_countdown=ticks.append,
                                stopped=lambda: self.clock.now >= 14)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(len(ticks), 14)
        self.assertEqual(ticks[-1], 2)

    def test_exit_precedes_stop_when_simultaneous_in_countdown(self):
        hwnd = self.api.add_game(on_close=None)
        fired = []

        def once():
            if not fired:
                fired.append(True)
                self.api.processes[10]["exit_at"] = self.clock.now

        self.api.before_wait = once
        result = self.run_close(hwnd, countdown_seconds=15.0,
                                on_countdown=lambda n: None, stopped=lambda: True)
        self.assertEqual(result.status, "exited_during_countdown")

    def test_countdown_window_gone_notifies_once(self):
        hwnd = self.api.add_game(on_close=None)
        wait_count = []

        def hook():
            wait_count.append(True)
            if len(wait_count) == 2:
                # The window closes mid-countdown; the process keeps cleaning up.
                self.api.windows[hwnd]["gone_at"] = self.clock.now

        self.api.before_wait = hook
        ticks = []
        gone_notes = []
        result = self.run_close(hwnd, countdown_seconds=15.0, on_countdown=ticks.append,
                                on_window_gone=lambda: gone_notes.append(True))
        self.assertEqual(result.status, "timeout")
        self.assertEqual(ticks, list(range(15, 0, -1)))
        self.assertEqual(len(gone_notes), 1)
        self.assertFalse(launcher_eligible(result))

    def test_exit_precedes_stop_when_simultaneous_before_send(self):
        hwnd = self.api.add_game(on_close=None)
        fired = []

        def once():
            if not fired:
                fired.append(True)
                self.api.processes[10]["exit_at"] = self.clock.now

        self.api.before_wait = once
        result = self.run_close(hwnd, countdown_seconds=0.0, stopped=lambda: True)
        self.assertEqual(result.status, "already_stopped")
        self.assertEqual(self.api.called("post_close"), [])

    def test_bring_forward_errors_do_not_block(self):
        hwnd = self.api.add_game()
        calls = []

        def broken():
            calls.append(True)
            raise RuntimeError("focus failed")

        result = self.run_close(hwnd, countdown_seconds=0.0, bring_forward=broken)
        self.assertEqual(result.status, "closed")
        self.assertEqual(len(calls), 1)
        self.assertIn("bring_forward_error", result.details)

    def test_presend_recheck_stop_after_restore(self):
        hwnd = self.api.add_game(on_close=2.0)
        holder = {"stopped": False}

        def hook(target):
            holder["stopped"] = True

        result = self.run_close(hwnd, stopped=lambda: holder["stopped"], before_close=hook)
        self.assertEqual(result.status, "cancelled")
        self.assertTrue(result.details.get("restore_executed"))
        self.assertEqual(self.api.called("post_close"), [])

    def test_presend_recheck_exit_after_restore(self):
        hwnd = self.api.add_game(on_close=None)

        def hook(target):
            self.api.processes[10]["exit_at"] = self.clock.now

        result = self.run_close(hwnd, before_close=hook)
        self.assertEqual(result.status, "already_stopped")
        self.assertEqual(self.api.called("post_close"), [])

    def test_presend_recheck_ownership_changed(self):
        hwnd = self.api.add_game()
        self.api.processes[11] = dict(self.api.processes[10])

        def hook(target):
            self.api.windows[hwnd]["pid"] = 11

        result = self.run_close(hwnd, before_close=hook)
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(self.api.called("post_close"), [])

    def test_presend_window_gone_keeps_wait_path_and_skips_launcher(self):
        hwnd = self.api.add_game(on_close=3.0)

        def hook(target):
            self.api.windows[hwnd]["gone_at"] = self.clock.now
            self.api.processes[10]["exit_at"] = self.clock.now + 3.0

        result = self.run_close(hwnd, before_close=hook)
        self.assertEqual(result.status, "closed")
        self.assertFalse(result.details["close_requested"])
        self.assertFalse(launcher_eligible(result))
        self.assertEqual(self.api.called("post_close"), [])

    def test_post_close_error_but_exited_skips_launcher(self):
        hwnd = self.api.add_game(on_close=None)
        self.api.errors["post_close"] = WinError(1400)
        wait_calls = []

        def exit_on_third_wait():
            wait_calls.append(True)
            if len(wait_calls) == 3:
                self.api.processes[10]["exit_at"] = self.clock.now

        self.api.before_wait = exit_on_third_wait
        result = self.run_close(hwnd, force_on_timeout=True)
        self.assertEqual(result.status, "closed")
        self.assertFalse(result.details["close_requested"])
        self.assertFalse(launcher_eligible(result))
        self.assertEqual(self.api.called("terminate"), [])


class TimeoutMessageTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.api = FakeApi(self.clock)

    def run_close(self, hwnd, **kwargs):
        kwargs.setdefault("clock", self.clock)
        return close_connected_game(self.api, hwnd, **kwargs)

    def test_timeout_message_not_hung(self):
        hwnd = self.api.add_game(on_close=None)
        result = self.run_close(hwnd, countdown_seconds=0.0)
        self.assertEqual(result.status, "timeout")
        self.assertIn("已发送关闭请求", result.message)
        self.assertIn("未检测到无响应", result.message)
        self.assertNotIn("确认框", result.message)
        self.assertNotIn("响应正常", result.message)
        self.assertEqual(result.details["window_state_at_timeout"], "not_hung")
        self.assertTrue(result.details["close_requested"])

    def test_timeout_message_hung(self):
        hwnd = self.api.add_game(on_close=None)
        self.api.hung_windows.add(hwnd)
        result = self.run_close(hwnd, countdown_seconds=0.0)
        self.assertEqual(result.status, "timeout")
        self.assertIn("游戏窗口无响应", result.message)
        self.assertEqual(result.details["window_state_at_timeout"], "hung")

    def test_timeout_message_observation_unknown(self):
        hwnd = self.api.add_game(on_close=None)
        self.api.errors["is_hung"] = WinError(8)
        result = self.run_close(hwnd, countdown_seconds=0.0)
        self.assertEqual(result.status, "timeout")
        self.assertIn("窗口状态观测失败", result.message)
        self.assertEqual(result.details["window_state_at_timeout"], "unknown")

    def test_timeout_recycled_hwnd_reports_unknown(self):
        hwnd = self.api.add_game(on_close=None)
        self.api.processes[11] = dict(self.api.processes[10])
        calls = []

        def hook():
            calls.append(True)
            if len(calls) == 4:
                # The original window died and the handle was recycled.
                self.api.windows[hwnd]["pid"] = 11

        self.api.before_wait = hook
        result = self.run_close(hwnd, countdown_seconds=0.0)
        self.assertEqual(result.status, "timeout")
        self.assertEqual(result.details["window_state_at_timeout"], "unknown")
        self.assertIn("窗口状态观测失败", result.message)

    def test_timeout_false_gone_reports_no_request(self):
        hwnd = self.api.add_game(on_close=None)

        def vanish_on_second_wait():
            # The window disappears during the pre-send recheck; the process lingers.
            if len(self.api.called("wait")) == 0:  # placeholder, replaced below
                pass

        calls = []

        def hook():
            calls.append(True)
            if len(calls) == 2:
                self.api.windows[hwnd]["gone_at"] = self.clock.now

        self.api.before_wait = hook
        result = self.run_close(hwnd, countdown_seconds=0.0, force_on_timeout=False)
        self.assertEqual(result.status, "timeout")
        self.assertIn("未发送关闭请求", result.message)
        self.assertIn("游戏窗口已关闭", result.message)
        self.assertEqual(result.details["close_requested"], False)
        self.assertEqual(result.details["window_state_at_timeout"], "gone")
        self.assertEqual(self.api.called("terminate"), [])

    def test_timeout_false_gone_force_allowed_when_enabled(self):
        hwnd = self.api.add_game(on_close=None, on_terminate=0.5)
        calls = []

        def hook():
            calls.append(True)
            if len(calls) == 2:
                self.api.windows[hwnd]["gone_at"] = self.clock.now

        self.api.before_wait = hook
        result = self.run_close(hwnd, countdown_seconds=0.0, force_on_timeout=True)
        self.assertEqual(result.status, "forced_exit")
        self.assertTrue(result.ok)
        self.assertEqual(result.details["close_requested"], False)
        self.assertTrue(result.details["forced"])
        terminate = self.api.called("terminate")
        self.assertEqual(len(terminate), 1)
        self.assertEqual(self.api.handles[terminate[0][1]], 10)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.api = FakeApi(self.clock)

    def run_launcher(self, **kwargs):
        kwargs.setdefault("clock", self.clock)
        # The injected sleep advances the same simulated clock, keeping one source of time.
        kwargs.setdefault("sleep", lambda seconds: setattr(self.clock, "now", self.clock.now + seconds))
        result = close_launcher(self.api, **kwargs)
        # Every opened handle is released exactly once.
        self.assertEqual(sorted(self.api.handles), sorted(self.api.closed_handles))
        return result

    def test_closes_only_windows_matching_resolved_path(self):
        self.api.add_process(20, r"C:\Install\launcher.exe", on_close=1.0)
        self.api.add_window(5, 20, title="启动器")
        self.api.add_process(30, r"C:\Other\launcher.exe", on_close=1.0)
        self.api.add_window(6, 30, title="别的程序")
        self.api.add_process(40, r"C:\Install\1.3.1\XStarter.exe", on_close=0.5)
        self.api.add_window(7, 40, title="助手")
        result = self.run_launcher(launcher_path=Path(r"C:\Install\launcher.exe"))
        self.assertEqual(result.status, "closed")
        self.assertEqual(self.api.called("post_close"),
                         [("post_close", 5), ("post_close", 7)])
        self.assertEqual(self.api.called("terminate"), [])

    def test_versioned_subfolder_helper_matches_install_root(self):
        # Observed B服 layout: the visible UI is <install>\1.3.1\xstarter.exe.
        self.api.add_process(20, r"C:\Install\1.3.1\xstarter.exe", on_close=1.0)
        self.api.add_window(5, 20, title="启动器")
        result = self.run_launcher(launcher_path=Path(r"C:\Install\launcher.exe"))
        self.assertEqual(result.status, "closed")
        self.assertEqual(self.api.called("post_close"), [("post_close", 5)])

    def test_path_matching_is_case_insensitive_and_rejects_aliases(self):
        self.api.add_process(20, r"C:\Install\LAUNCHER.EXE", on_close=1.0)
        self.api.add_window(5, 20, title="启动器")
        self.api.add_process(30, r"C:\InstallX\launcher.exe", on_close=1.0)
        self.api.add_window(6, 30, title="同名程序")
        self.api.add_process(40, r"C:\Temp\xstarter.exe", on_close=1.0)
        self.api.add_window(7, 40, title="别处助手")
        result = self.run_launcher(launcher_path=Path(r"C:\Install\launcher.exe"))
        self.assertEqual(result.status, "closed")
        self.assertEqual(self.api.called("post_close"), [("post_close", 5)])

    def test_no_matching_window_is_idempotent_after_bounded_wait(self):
        result = self.run_launcher(launcher_path=Path(r"C:\Install\launcher.exe"))
        self.assertEqual(result.status, "already_stopped")
        self.assertIn("未找到匹配官方启动器路径的窗口", result.message)
        self.assertIn("已等待 10 秒", result.message)
        self.assertEqual(result.details["waited_seconds"], 10.0)
        # The wait is bounded by the injected sleep on the same clock.
        self.assertAlmostEqual(self.clock.now, 10.0, places=6)

    def test_waits_for_slow_launcher_window(self):
        self.api.add_process(20, r"C:\Install\launcher.exe", on_close=1.0)
        self.api.add_window(5, 20, title="启动器", visible_at=2.0)
        result = self.run_launcher(launcher_path=Path(r"C:\Install\launcher.exe"))
        self.assertEqual(result.status, "closed")
        self.assertEqual(self.api.called("post_close"), [("post_close", 5)])
        self.assertGreaterEqual(self.clock.now, 2.0)

    def test_appear_wait_stops_on_stop_signal(self):
        self.api.add_process(20, r"C:\Install\launcher.exe", on_close=1.0)
        self.api.add_window(5, 20, title="启动器", visible_at=2.0)
        result = self.run_launcher(launcher_path=Path(r"C:\Install\launcher.exe"),
                                   stopped=lambda: self.clock.now >= 1.0)
        self.assertEqual(result.status, "cancelled")
        self.assertIn("跳过关闭启动器", result.message)
        self.assertEqual(self.api.called("post_close"), [])
        self.assertLess(self.clock.now, 2.0)

    def test_send_phase_stops_before_sending(self):
        self.api.add_process(20, r"C:\Install\launcher.exe", on_close=1.0)
        self.api.add_window(5, 20, title="启动器")
        holder = {"stopped": False}
        self.api.after_open = lambda: holder.update(stopped=True)
        result = self.run_launcher(launcher_path=Path(r"C:\Install\launcher.exe"),
                                   stopped=lambda: holder["stopped"])
        # after_open fires while matching; the stop is honoured before any request.
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(self.api.called("post_close"), [])

    def test_wait_phase_stops_on_stop_signal(self):
        self.api.add_process(20, r"C:\Install\launcher.exe", on_close=None)
        self.api.add_window(5, 20, title="启动器")
        result = self.run_launcher(launcher_path=Path(r"C:\Install\launcher.exe"),
                                   stopped=lambda: self.clock.now >= 2.0)
        self.assertEqual(result.status, "cancelled")
        self.assertIn("不再等待官方启动器退出", result.message)
        self.assertEqual(result.details.get("unconfirmed"), 1)

    def test_wait_error_returns_error_and_releases_handles(self):
        self.api.add_process(20, r"C:\Install\launcher.exe", on_close=None)
        self.api.add_window(5, 20, title="启动器")
        self.api.errors["wait_process"] = WinError(8)
        result = self.run_launcher(launcher_path=Path(r"C:\Install\launcher.exe"))
        self.assertEqual(result.status, "error")
        self.assertIn("系统调用失败", result.message)

    def test_partial_send_failure_is_reported(self):
        self.api.add_process(20, r"C:\Install\launcher.exe", on_close=1.0)
        self.api.add_window(5, 20, title="启动器")
        self.api.add_process(40, r"C:\Install\1.3.1\xstarter.exe", on_close=1.0)
        self.api.add_window(7, 40, title="助手")
        self.api.post_close_fail.add(7)
        result = self.run_launcher(launcher_path=Path(r"C:\Install\launcher.exe"))
        self.assertEqual(result.status, "closed")
        self.assertIn("发送失败 1", result.message)
        self.assertEqual(result.details["send_failed"], 1)
        self.assertEqual(result.details["requested"], 1)

    def test_unconfirmed_launcher_is_reported(self):
        self.api.add_process(20, r"C:\Install\launcher.exe", on_close=None)
        self.api.add_window(5, 20, title="启动器")
        result = self.run_launcher(launcher_path=Path(r"C:\Install\launcher.exe"))
        self.assertEqual(result.status, "timeout")
        self.assertIn("未确认 1", result.message)
        self.assertFalse(launcher_eligible(CloseResult("timeout", "m", result.details)))

    def test_recheck_failure_skips_instead_of_sending(self):
        self.api.add_process(20, r"C:\Install\launcher.exe", on_close=1.0)
        self.api.add_window(5, 20, title="启动器")
        self.api.errors["is_window"] = WinError(8)
        result = self.run_launcher(launcher_path=Path(r"C:\Install\launcher.exe"))
        self.assertEqual(result.status, "error")
        self.assertEqual(self.api.called("post_close"), [])

    def test_empty_launcher_path_reports_unknown_identity(self):
        result = self.run_launcher(launcher_path=None)
        self.assertEqual(result.status, "already_stopped")
        self.assertIn("未能确认官方启动器身份", result.message)


class LauncherEligibleTests(unittest.TestCase):
    def test_truth_table(self):
        cases = [
            ("closed", True, True),
            ("closed", False, False),
            ("forced_exit", True, True),
            ("already_stopped", False, False),
            ("exited_during_countdown", False, False),
            ("timeout", True, False),
            ("cancelled", True, False),
            ("ambiguous", True, False),
        ]
        for status, requested, expected in cases:
            details = {"close_requested": requested} if requested is not None else {}
            result = CloseResult(status, "m", details)
            self.assertEqual(launcher_eligible(result), expected,
                             f"{status}/{requested}")


class LauncherSelectionTests(unittest.TestCase):
    def test_single_root_containing_game_is_selected(self):
        result = select_launcher_for_game([Path(r"D:\A\launcher.exe")],
                                          r"D:\A\Games\X6Game\game.exe")
        self.assertEqual(result, Path(r"D:\A\launcher.exe"))

    def test_unrelated_roots_return_none(self):
        self.assertIsNone(select_launcher_for_game([Path(r"D:\A\launcher.exe")],
                                                   r"D:\B\Games\game.exe"))

    def test_no_candidates_or_no_game_return_none(self):
        game = r"D:\A\Games\game.exe"
        self.assertIsNone(select_launcher_for_game([], game))
        self.assertIsNone(select_launcher_for_game(None, game))
        self.assertIsNone(select_launcher_for_game([Path(r"D:\A\launcher.exe")], ""))

    def test_ambiguous_nested_channels_return_none(self):
        # Two channel roots both contain the game image: refuse to guess.
        candidates = [Path(r"D:\A\launcher.exe"), Path(r"D:\A\Channel\launcher.exe")]
        self.assertIsNone(select_launcher_for_game(candidates,
                                                   r"D:\A\Channel\Games\game.exe"))

    def test_multiple_channels_pick_the_matching_one(self):
        candidates = [Path(r"D:\A\launcher.exe"), Path(r"D:\B\launcher.exe")]
        result = select_launcher_for_game(candidates, r"D:\B\Games\game.exe")
        self.assertEqual(result, Path(r"D:\B\launcher.exe"))


class BindingTests(unittest.TestCase):
    def test_bound_hwnd_reads_win32_controller_info_only(self):
        controller = type("C", (), {})()
        controller.info = {"type": "win32", "hwnd": 4242}
        self.assertEqual(bound_game_hwnd(controller), 4242)
        controller.info = {"type": "adb", "hwnd": 4242}
        self.assertIsNone(bound_game_hwnd(controller))
        controller.info = {"type": "win32", "hwnd": "4242"}
        self.assertIsNone(bound_game_hwnd(controller))

        class Broken:
            @property
            def info(self):
                raise RuntimeError("disconnected")

        self.assertIsNone(bound_game_hwnd(Broken()))


def read_json(relative):
    return json.loads((ROOT / relative).read_text(encoding="utf-8-sig"))


class ConfigTests(unittest.TestCase):
    def test_task_is_opt_in_and_after_regular_tasks(self):
        imports = read_json("interface.json")["import"]
        self.assertIn("tasks/CloseGame.json", imports)
        index = imports.index("tasks/CloseGame.json")
        for name in imports[:index]:
            groups = {group for task in read_json(name).get("task", []) for group in task.get("group", [])}
            self.assertFalse(any(group.startswith("developer") for group in groups), name)
        task = read_json("tasks/CloseGame.json")["task"][0]
        self.assertEqual(task["name"], "close_game")
        self.assertFalse(task["default_check"])
        self.assertEqual(task["group"], ["posttask"])
        labels = {group["name"]: group["label"] for group in read_json("interface.json")["group"]}
        self.assertEqual(labels.get("posttask"), "后置任务")
        self.assertIn("倒计时 15 秒", task["description"])
        presets = read_json("tasks/Presets.json")["preset"]
        self.assertFalse(any(item["name"] == "close_game" for preset in presets for item in preset["task"]))

    def test_force_option_defaults_off_everywhere(self):
        option = read_json("tasks/CloseGame.json")["option"]["ForceCloseOnTimeout"]
        self.assertEqual(option["type"], "switch")
        self.assertEqual(option["default_case"], "No")
        self.assertIn("窗口已消失但进程未退出", option["description"])
        cases = {case["name"]: case["pipeline_override"]["MaaNikki_CloseGame"]["custom_action_param"]
                 for case in option["cases"]}
        self.assertEqual(cases, {"Yes": {"force_on_timeout": True}, "No": {"force_on_timeout": False}})
        launcher = read_json("tasks/CloseGame.json")["option"]["CloseLauncher"]
        self.assertEqual(launcher["type"], "switch")
        self.assertEqual(launcher["default_case"], "No")
        launcher_cases = {case["name"]: case["pipeline_override"]["MaaNikki_CloseGame"]["custom_action_param"]
                          for case in launcher["cases"]}
        self.assertEqual(launcher_cases, {"Yes": {"close_launcher": True}, "No": {"close_launcher": False}})
        node = read_json("resource/pipeline/close_game.json")["MaaNikki_CloseGame"]
        self.assertEqual(node["custom_action_param"], {"force_on_timeout": False, "close_launcher": False})

    def test_entry_needs_no_screenshot_before_or_after_action(self):
        node = read_json("resource/pipeline/close_game.json")["MaaNikki_CloseGame"]
        self.assertEqual(node["recognition"], "DirectHit")
        self.assertEqual(node["next"], [])
        self.assertEqual((node["pre_delay"], node["post_delay"]), (0, 0))
        for key in ("pre_wait_freezes", "post_wait_freezes", "on_error"):
            self.assertNotIn(key, node)

    def test_action_name_and_registration_match(self):
        node = read_json("resource/pipeline/close_game.json")["MaaNikki_CloseGame"]
        source = (ROOT / "agent/action/close_game.py").read_text(encoding="utf-8")
        self.assertIn(f'@agent_action("{node["custom_action"]}")', source)
        self.assertNotIn("stop_on_failure", source)
        main = (ROOT / "agent/main.py").read_text(encoding="utf-8")
        self.assertIn("from action import close_game as _close_game", main)

    def test_action_wiring_is_present(self):
        source = (ROOT / "agent/action/close_game.py").read_text(encoding="utf-8")
        self.assertIn("on_countdown=", source)
        self.assertIn("on_window_gone=", source)
        self.assertIn("launcher_eligible(", source)
        self.assertIn('resolve_official_launcher(result.details.get("image"))', source)
        self.assertIn("stopped=lambda: runtime.stopped", source)

    def test_window_rule_matches_controller_and_launcher(self):
        controller = read_json("interface.json")["controller"][0]["win32"]
        self.assertEqual(controller["window_regex"], game_close.GAME_TITLE_PATTERN)
        self.assertEqual(controller["class_regex"], f"^{game_close.GAME_CLASS}$")
        tree = ast.parse((ROOT / "agent/start_game.py").read_text(encoding="utf-8"))
        programs = next(ast.literal_eval(node.value) for node in tree.body
                        if isinstance(node, ast.Assign)
                        and any(getattr(target, "id", None) == "PROGRAMS" for target in node.targets))
        self.assertEqual(set(programs), set(game_close.KNOWN_IMAGES))
        self.assertEqual(set(programs), set(game_close.KNOWN_IMAGES))
        for title in ("无限暖暖", " Infinity Nikki ", "無限暖暖"):
            self.assertTrue(re.fullmatch(game_close.GAME_TITLE_PATTERN, title))
        self.assertIsNone(re.fullmatch(game_close.GAME_TITLE_PATTERN, "无限暖暖 启动器"))

    def test_success_statuses_cover_countdown_exit(self):
        self.assertIn("exited_during_countdown", game_close.SUCCESS_STATUSES)
        self.assertTrue(CloseResult("exited_during_countdown", "m").ok)


if __name__ == "__main__":
    unittest.main()
