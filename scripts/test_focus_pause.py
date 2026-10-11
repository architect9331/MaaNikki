"""Foreground pause regressions: python/python.exe -B scripts/test_focus_pause.py."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import sys
import os
import unittest
from unittest.mock import Mock, patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
os.environ.setdefault("MAAFW_BINARY_PATH", str(ROOT / "maafw"))

from task_control import TaskStopped, task_session, task_time, _SESSIONS, _ACTIVE
from daily.runtime import Runtime
from navigation.controller import ForegroundInput
from navigation.engine import Motion, Navigator
from navigation.vision import Pose
from game_keys import GameKeys


class PauseTests(unittest.TestCase):
    @contextmanager
    def simulation(self, *, lost_at=None, resume_at=2, stop_at=None):
        state = SimpleNamespace(now=0.0, focused=True, pauses=0)
        controller = Mock()
        controller.post_key_down.return_value.wait.return_value.succeeded = True
        controller.post_key_up.return_value.wait.return_value.succeeded = True
        controller.post_touch_up.return_value.wait.return_value.succeeded = True
        frames = [np.full((16, 24, 3), n, np.uint8) for n in range(1, 20)]
        controller.post_screencap.side_effect = [SimpleNamespace(wait=lambda frame=frame:
            SimpleNamespace(succeeded=True, get=lambda: frame)) for frame in frames]
        context = SimpleNamespace(tasker=SimpleNamespace(controller=controller, stopping=False),
            run_task=Mock(), run_recognition=Mock(return_value=SimpleNamespace(hit=True)),
            get_node_data=Mock(return_value={}))
        inputs = ForegroundInput.__new__(ForegroundInput)
        inputs.controller, inputs.hwnd, inputs.user32 = controller, 1, Mock()
        inputs.user32.IsWindow.return_value = True
        inputs.stopped = lambda: context.tasker.stopping
        inputs.foreground = lambda: state.focused
        inputs.held, inputs.buttons = set(), set()
        def sleep(seconds):
            state.now += max(seconds, 1e-9)
            if lost_at is not None and lost_at <= state.now < resume_at:
                state.focused = False
            if state.now >= resume_at:
                state.focused = True
            if stop_at is not None and state.now >= stop_at:
                context.tasker.stopping = True
        _SESSIONS.clear()
        with task_session(context, 1) as session, \
                patch("task_control.wall_time.monotonic", side_effect=lambda: state.now), \
                patch("task_control.wall_time.sleep", side_effect=sleep):
            session.started, session.adapter = True, inputs
            session.register(inputs)
            yield context, inputs, session, state
        _SESSIONS.clear()

    def test_focus_loss_releases_inputs_and_resumes_without_refocusing(self):
        with self.simulation() as (context, inputs, session, state):
            inputs.held, inputs.buttons = {87}, {1}
            begin = task_time.monotonic()
            state.focused = False
            inputs.check()
            self.assertAlmostEqual(task_time.monotonic(), begin)
            self.assertGreaterEqual(state.now, 2)
            self.assertEqual(session.generation, 1)
            self.assertEqual((inputs.held, inputs.buttons), (set(), set()))
            inputs.user32.keybd_event.assert_called_once()
            inputs.user32.mouse_event.assert_called_once()
            context.tasker.controller.post_key_up.assert_not_called()
            context.tasker.controller.post_key_down.assert_not_called()
            self.assertEqual(context.run_task.call_count, 2)

    def test_stop_during_pause_exits_without_resuming_or_sending_input(self):
        with self.simulation(stop_at=.3) as (context, inputs, session, state):
            state.focused = False
            with self.assertRaises(TaskStopped):
                inputs.check()
            self.assertEqual(session.generation, 0)
            context.tasker.controller.post_key_down.assert_not_called()
            context.tasker.controller.post_key_up.assert_not_called()
            self.assertEqual(context.run_task.call_count, 1)

    def test_pause_time_does_not_consume_page_wait_duration(self):
        with self.simulation(lost_at=.1) as (context, inputs, session, state):
            rt = Runtime(context)
            self.assertTrue(rt.pause(.4))
            self.assertGreater(state.now, 2)
            self.assertAlmostEqual(task_time.monotonic(), .4)
            self.assertFalse(rt.stopped)

    def test_cache_and_recognition_use_fresh_frames_after_resume(self):
        with self.simulation() as (context, inputs, session, state):
            rt = Runtime(context)
            with rt.observe():
                old = rt.frame()
                state.focused = False
                new = rt.frame()
                self.assertIsNot(old, new)
                self.assertEqual(int(new[0, 0, 0]), 2)
            self.assertEqual(context.tasker.controller.post_screencap.call_count, 2)
            state.focused = False
            # A cropped/processed screenshot supplied before pause is also stale.
            self.assertIsNone(rt.recognize("test", image=new))
            self.assertIsNone(rt.recognize("test", image=new))
            context.run_recognition.assert_not_called()
            self.assertTrue(rt.recognize("test").hit)

    def test_long_hold_restores_only_after_focus_returns(self):
        with self.simulation(lost_at=.1) as (context, inputs, session, state):
            inputs.held = {82}
            submissions = []
            def down(key):
                submissions.append((key, state.focused, state.now))
                return SimpleNamespace(wait=lambda: SimpleNamespace(succeeded=True))
            context.tasker.controller.post_key_down.side_effect = down
            inputs.wait(.4)
            self.assertEqual(len(submissions), 1)
            self.assertEqual(submissions[0][:2], (82, True))
            self.assertGreaterEqual(submissions[0][2], 2)
            self.assertAlmostEqual(task_time.monotonic(), .4)

    def test_navigation_discards_old_movement_and_jump_schedule(self):
        with self.simulation() as (context, inputs, session, state):
            motion = Motion(inputs)
            motion.until, motion.jump_state, motion.jump_at = 10, "first", 0
            inputs.held = {87}
            state.focused = False
            motion.tick()
            self.assertEqual((motion.until, motion.jump_state), (0, "idle"))
            context.tasker.controller.post_key_down.assert_not_called()

    def test_route_refreshes_pose_before_waypoint_decision_after_resume(self):
        with self.simulation() as (context, inputs, session, state):
            inputs.bindings = GameKeys()
            inputs.ready = lambda: True
            locator = SimpleNamespace(spec=SimpleNamespace(id="map"), previous=object())
            old = Pose(5, 0, 1, 1, 0, 1)
            fresh = Pose(.5, 0, 1, 1, 0, 1)
            navigator = Navigator(locator, inputs, Mock(), lambda: False, Mock(),
                                  waypoint=lambda point: "done")
            navigator.observe = Mock(side_effect=[old, fresh])
            navigator.turn = Mock()
            context.tasker.controller.post_click_key.return_value.wait.return_value.succeeded = True
            def startup(key):
                state.focused = False
                return SimpleNamespace(wait=lambda: SimpleNamespace(succeeded=True))
            context.tasker.controller.post_click_key.side_effect = startup
            point = SimpleNamespace(x=0, y=0, radius=1, jump=False, action=None)
            route = SimpleNamespace(map_id="map", points=[point], timeout=30, start_radius=10)
            self.assertEqual(navigator.follow(route), fresh)
            self.assertIsNone(locator.previous)
            self.assertEqual(navigator.observe.call_count, 2)
            navigator.turn.assert_not_called()

    def test_physical_hold_resumes_and_stop_cleanup_does_not_refocus(self):
        with self.simulation(lost_at=.1) as (context, inputs, session, state):
            rt = Runtime(context)
            @contextmanager
            def guard():
                yield inputs
            with patch.object(rt, "input_guard", guard):
                self.assertTrue(rt.hold(82, .4))
            self.assertEqual(context.tasker.controller.post_key_down.call_count, 2)
            self.assertEqual(inputs.held, set())
            self.assertAlmostEqual(task_time.monotonic(), .4)

    def test_nested_callbacks_and_pipeline_nodes_share_pause_session(self):
        with self.simulation() as (context, inputs, session, state):
            with task_session(context, 2) as nested:
                self.assertIs(nested, session)
                state.focused = False
                nested.wait()
            self.assertEqual(session.generation, 1)
            self.assertGreater(session.paused_seconds, 0)

    def test_native_nested_callback_without_contextvar_inherits_parent(self):
        with self.simulation() as (context, inputs, session, state):
            token = _ACTIVE.set(None)
            try:
                with task_session(context, 2) as child:
                    self.assertIs(child, session)
                    state.focused = False
                    child.wait()
                self.assertEqual(session.generation, 1)
            finally:
                _ACTIVE.reset(token)

    def test_ui_recognition_refreshes_pipeline_screenshot_after_resume(self):
        from registration import agent_recognition
        with patch("registration.AgentServer.custom_recognition", return_value=lambda cls: cls):
            @agent_recognition("test")
            class Recognition:
                def analyze(self, context, argv):
                    return int(argv.image[0, 0, 0])
        with self.simulation() as (context, inputs, session, state):
            state.focused = False
            argv = SimpleNamespace(image=np.full((16, 24, 3), 99, np.uint8))
            self.assertEqual(Recognition().analyze(context, argv), 1)
            self.assertEqual(int(argv.image[0, 0, 0]), 99)

    def test_physical_shortcut_keys_are_released_on_focus_loss(self):
        with self.simulation() as (context, inputs, session, state):
            rt = Runtime(context)
            self.assertTrue(rt.action("KeyDown", key=17))
            self.assertEqual(inputs.held, {17})
            state.focused = False
            rt.check_focus()
            self.assertEqual(inputs.held, set())
            inputs.user32.keybd_event.assert_called_once()

    def test_closed_game_window_is_distinct_from_switching_focus(self):
        with self.simulation() as (context, inputs, session, state):
            inputs.user32.IsWindow.return_value = False
            with self.assertRaisesRegex(ValueError, "窗口已经关闭"):
                inputs.check()
            self.assertEqual(state.now, 0)


if __name__ == "__main__":
    unittest.main()
