"""Mini-map global matching regressions: python/python.exe -B scripts/test_navigation.py."""
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from navigation.models import NavigationError, load_route
from navigation.vision import Locator, Pose
from navigation.controller import ForegroundInput, relative_camera
from navigation.daily_route import RouteActions
from navigation.engine import Navigator
from navigation.insect import Insect


class GlobalMatchTests(unittest.TestCase):
    def setUp(self):
        self.image = np.random.default_rng(9).integers(0, 255, (80, 100), dtype=np.uint8)
        self.patch = self.image[30:36, 40:46].copy()

    def test_smaller_bigmap_response_mask_keeps_minimap_center_coordinates(self):
        # 75x95 correlation response, with a legacy 70x90 center mask.
        mask = np.zeros((70, 90), dtype=np.uint8)
        mask[28, 38] = 255
        x, y, score, detail = Locator.match(self.image, self.patch, peak_mask=mask)
        expected = Locator.match(self.image, self.patch)
        self.assertAlmostEqual(x, expected[0])
        self.assertAlmostEqual(y, expected[1])
        self.assertGreater(score, .99)

    def test_existing_response_mask_behavior_is_unchanged(self):
        unmasked = Locator.match(self.image, self.patch)
        masked = Locator.match(self.image, self.patch, peak_mask=np.full((80, 100), 255, dtype=np.uint8))
        self.assertEqual(masked, unmasked)

    def test_forbidden_centers_stay_forbidden(self):
        mask = np.zeros((70, 90), dtype=np.uint8)
        with self.assertRaises(NavigationError):
            Locator.match(self.image, self.patch, peak_mask=mask)


class SteeringTests(unittest.TestCase):
    def navigator(self, heading, effects=None):
        inputs = Mock()
        navigator = Navigator(Mock(), inputs, Mock(), lambda: False, Mock(return_value=True))
        navigator.turn_ratio = 12
        navigator.motion = Mock()
        state = SimpleNamespace(heading=heading)
        pose = lambda: Pose(0, 0, 1, 1, state.heading, 1)
        navigator.observe = Mock(side_effect=lambda hint: pose())
        navigator.stable_heading = Mock(side_effect=lambda previous: pose())
        effects = iter(effects or [])
        def move(controller, dx, dy):
            self.assertEqual(dy, 0)
            state.heading = (state.heading+dx/12*next(effects, 1)) % 360
            return True
        return navigator, pose, move

    def test_large_turn_uses_complete_calibrated_displacement_across_north(self):
        navigator, pose, move = self.navigator(340)
        with patch("navigation.controller.relative_camera", side_effect=move) as camera:
            result = navigator.turn(100, pose())
        camera.assert_called_once_with(navigator.inputs.controller, 1440, 0)
        self.assertAlmostEqual(result.heading, 100)
        navigator.motion.stop.assert_called_once()

    def test_small_turn_applies_full_correction_and_verifies_heading(self):
        navigator, pose, move = self.navigator(0)
        with patch("navigation.controller.relative_camera", side_effect=move) as camera:
            result = navigator.turn(35, pose())
        camera.assert_called_once_with(navigator.inputs.controller, 420, 0)
        self.assertAlmostEqual(result.heading, 35)
        navigator.motion.stop.assert_not_called()
        navigator.observe.assert_called_once()

    def test_incomplete_small_turn_stops_and_corrects_before_returning(self):
        navigator, pose, move = self.navigator(0, [.5, 1])
        with patch("navigation.controller.relative_camera", side_effect=move) as camera:
            result = navigator.turn(20, pose())
        self.assertEqual([call.args[1] for call in camera.call_args_list], [240, 120])
        self.assertAlmostEqual(result.heading, 20)
        navigator.motion.stop.assert_called_once()

    def test_stationary_catch_uses_three_degree_tolerance_and_stops_on_no_response(self):
        navigator, pose, move = self.navigator(0, [0, 0, 0])
        with patch("navigation.controller.relative_camera", side_effect=move):
            with self.assertRaisesRegex(NavigationError, "没有响应"):
                navigator.turn(4, pose(), stationary=True, tolerance=3)
        navigator.inputs.release.assert_called_once()

    def test_stop_prevents_camera_submission(self):
        navigator, pose, move = self.navigator(0)
        navigator.inputs.check.side_effect = NavigationError("已停止")
        with patch("navigation.controller.relative_camera") as camera:
            with self.assertRaises(NavigationError):
                navigator.turn(90, pose())
        camera.assert_not_called()


class CollectionTests(unittest.TestCase):
    def plant_actions(self, remaining=5):
        resource = Path(__file__).resolve().parents[1] / "resource"
        card = SimpleNamespace(rule=SimpleNamespace(executor="plant"), remaining=remaining)
        return RouteActions(Mock(stopped=False), resource, load_route(resource, "zhaoxi_plant"),
                            "zhaoxi", card, Mock(), Mock())

    def test_lamp_flower_completes_quota_after_four_star_grasses(self):
        actions = self.plant_actions()
        actions.count = 4
        actions.notification = Mock(return_value="路灯花×1\n路灯花精粹×1\n采集心得×10")
        self.assertTrue(actions.obtained("", interaction=True))
        self.assertEqual(actions.count, 5)
        self.assertTrue(actions.complete())
        self.assertEqual(actions.event.call_args.args[0]["material"], "路灯花")

    def test_both_plants_count_but_byproducts_and_other_items_do_not(self):
        actions = self.plant_actions()
        actions.notification = Mock(return_value="星荧草×1\n路灯花×1\n路灯花种子×1\n星荧草精粹×1\n噗灵×10")
        self.assertTrue(actions.obtained("", interaction=True))
        self.assertEqual(actions.count, 2)
        self.assertFalse(actions.complete())

    def test_repeated_passive_notice_does_not_count_twice(self):
        actions = self.plant_actions()
        actions.notification = Mock(return_value="路灯花×1")
        self.assertTrue(actions.obtained("", single=True))
        self.assertFalse(actions.obtained("", single=True))
        self.assertEqual(actions.count, 1)

    def test_each_plant_point_waits_two_seconds_then_one(self):
        actions = RouteActions.__new__(RouteActions)
        actions.count, actions.quota = 0, None
        actions.rt = Mock(stopped=False)
        actions.rt.ui.wait_pickup.side_effect = [True, False]
        actions.notification, actions.obtained = Mock(), Mock()
        actions.collect()
        self.assertEqual([call.args[0] for call in actions.rt.ui.wait_pickup.call_args_list], [2, 1])

    def test_insect_reuses_route_calibration_and_never_walks_after_failed_turn(self):
        for fail in (False, True):
            actions = Mock(count=0, navigator=SimpleNamespace(turn_ratio=12))
            actions.rt.stopped = False
            actions.rt.game_keys.get.return_value = SimpleNamespace(kind="mouse", code=1)
            actions.rt.game_keys.movement.return_value = {}
            insect = Insect(actions)
            insect.track = Mock(return_value=True)
            insect.active = Mock(side_effect=[False, True, True])
            insect.degree = Mock(return_value=90)
            def obtained(*args, **kwargs):
                actions.count += 1
            actions.obtained.side_effect = obtained
            navigator, motion = Mock(), Mock()
            def turn(*args, **kwargs):
                if fail:
                    raise NavigationError("没有对准")
                navigator.turn_ratio = 12.5
            navigator.turn.side_effect = turn
            with patch("navigation.insect.Navigator", return_value=navigator), \
                    patch("navigation.insect.Motion", return_value=motion):
                if fail:
                    with self.assertRaises(NavigationError):
                        insect.catch(1)
                    motion.forward.assert_not_called()
                else:
                    self.assertTrue(insect.catch(1))
                    motion.forward.assert_called_once_with(.35)
            navigator.turn.assert_called_once_with(90, actions.teleporter.locator.locate.return_value,
                                                   stationary=True, tolerance=3)
            self.assertEqual(actions.navigator.turn_ratio, 12 if fail else 12.5)
            motion.close.assert_called_once()


class SceneInputTests(unittest.TestCase):
    def test_scene_click_preserves_cursor_and_other_held_inputs(self):
        for button in range(5):
            inputs = ForegroundInput.__new__(ForegroundInput)
            inputs.controller, inputs.user32 = Mock(), Mock()
            inputs.check, inputs.wait = Mock(), Mock()
            inputs.foreground = Mock(return_value=True)
            other = 4 if button != 4 else 2
            inputs.buttons, inputs.held = {other}, {87}
            inputs.controller.post_touch_up.return_value.wait.return_value.succeeded = True
            with patch("navigation.controller.send_mouse", return_value=True) as send:
                self.assertTrue(inputs.click_mouse(button))
            flags = {0: 0x0002, 1: 0x0008, 2: 0x0020, 3: 0x0080, 4: 0x0080}
            send.assert_called_once_with(inputs.user32, flags[button], data=button-2 if button >= 3 else 0)
            inputs.controller.post_touch_up.assert_called_once_with(button)
            inputs.controller.post_touch_down.assert_not_called()
            inputs.controller.post_touch_move.assert_not_called()
            inputs.controller.post_click.assert_not_called()
            inputs.controller.post_key_up.assert_not_called()
            self.assertEqual(inputs.buttons, {other})
            self.assertEqual(inputs.held, {87})

    def test_scene_click_releases_after_interruption_without_stealing_focus(self):
        inputs = ForegroundInput.__new__(ForegroundInput)
        inputs.controller, inputs.user32 = Mock(), Mock()
        inputs.check, inputs.wait = Mock(), Mock(side_effect=NavigationError("已停止"))
        inputs.foreground = Mock(return_value=False)
        inputs.buttons, inputs.held = set(), {87}
        with patch("navigation.controller.send_mouse", return_value=True):
            with self.assertRaises(NavigationError):
                inputs.click_mouse(0)
        inputs.user32.mouse_event.assert_called_once_with(0x0004, 0, 0, 0, 0)
        inputs.controller.post_touch_up.assert_not_called()
        self.assertEqual(inputs.buttons, set())
        self.assertEqual(inputs.held, {87})

    def test_failed_scene_click_down_is_released_with_fallback(self):
        inputs = ForegroundInput.__new__(ForegroundInput)
        inputs.controller, inputs.user32, inputs.check = Mock(), Mock(), Mock()
        inputs.foreground = Mock(return_value=True)
        inputs.buttons = set()
        inputs.controller.post_touch_up.return_value.wait.return_value.succeeded = False
        with patch("navigation.controller.send_mouse", return_value=False):
            with self.assertRaises(NavigationError):
                inputs.click_mouse(1)
        inputs.controller.post_touch_up.assert_called_once_with(1)
        inputs.user32.mouse_event.assert_called_once_with(0x0010, 0, 0, 0, 0)
        self.assertEqual(inputs.buttons, set())

    def test_scene_button_does_not_relocate_cursor_and_failed_down_is_tracked(self):
        for succeeded in (True, False):
            inputs = ForegroundInput.__new__(ForegroundInput)
            inputs.controller, inputs.user32, inputs.check = Mock(), Mock(), Mock()
            inputs.buttons = set()
            with patch("navigation.controller.send_mouse", return_value=succeeded) as send:
                if succeeded:
                    inputs.mouse_down(1)
                else:
                    with self.assertRaises(NavigationError):
                        inputs.mouse_down(1)
            send.assert_called_once_with(inputs.user32, 0x0008, data=0)
            inputs.controller.post_touch_down.assert_not_called()
            self.assertEqual(inputs.buttons, {1})

    def test_relative_motion_accepts_full_turn_but_keeps_vertical_bound_and_focus_guard(self):
        controller = Mock()
        controller.post_relative_move.return_value.wait.return_value.succeeded = True
        with patch("navigation.controller.ForegroundInput") as adapter:
            adapter.return_value.foreground.return_value = True
            self.assertTrue(relative_camera(controller, 1440, 0))
            self.assertFalse(relative_camera(controller, 1440, 301))
            adapter.return_value.foreground.return_value = False
            self.assertFalse(relative_camera(controller, 1440, 0))
        controller.post_relative_move.assert_called_once_with(1440, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
