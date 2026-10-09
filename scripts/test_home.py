"""Home daily regressions: python/python.exe -B scripts/test_home.py."""
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import json
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from daily.home import Home
from navigation.daily_route import RouteActions, preflight
from navigation.engine import Navigator
from navigation.fishing import StarFishing, FishingState, FishingResult
from navigation.models import load_route, Point, Route, NavigationError
from navigation.vision import Pose
from game_keys import GameKeys
from daily.ui import GameUI


class HomeTests(unittest.TestCase):
    def fishing(self):
        actions = Mock()
        actions.rt.stopped = False
        actions.rt.observe.side_effect = lambda **kwargs: nullcontext()
        actions.rt.capture.return_value = np.zeros((1, 1, 3), dtype=np.uint8)
        fish = StarFishing(actions)
        fish.ability = Mock()
        fish.press = Mock()
        self.now = 0
        def wait(seconds):
            self.now += seconds
        fish.wait = Mock(side_effect=wait)
        return fish

    def test_route_preserves_upstream_coordinates_order_and_departure_modes(self):
        route = load_route(ROOT / "resource", "home_daily")
        native = [(4707, 8847), (5148, 8536.5), (5067.58, 8876.87),
                  (5395.5, 8536.5), (5683.5, 8311.5), (6377.94, 10226.58),
                  (5688, 11344.5), (4801.5, 11088), (6003, 11124),
                  (6835.5, 8896.5), (7015.5, 8064), (1255.5, 4063.5),
                  (1255.5, 4063.5), (1255.5, 4063.5)]
        self.assertEqual([(p.x, p.y) for p in route.points],
                         [(round(x*2/90+1989, 1)/2, round(y*2/90+1243, 1)/2) for x,y in native])
        self.assertEqual([p.action for p in route.points],
                         ["teleport", "", "fishing_star", "", "", "", "", "fishing_star",
                          "", "", "macro", "teleport", "macro", "wait"])
        self.assertEqual([i for i,p in enumerate(route.points) if p.jump], [4, 10])
        self.assertEqual(route.points[-1].params["seconds"], 2)
        preflight(ROOT / "resource", route, "home")

    def test_cast_keeps_state_order_and_waits_before_no_fish_check(self):
        fish = self.fishing()
        fish.ui.asset.return_value = None
        fish.state = Mock(side_effect=[FishingState.UNKNOWN, FishingState.FINISH,
                                      FishingState.STRIKE, FishingState.PULL_LINE,
                                      FishingState.REEL_IN, FishingState.SKIP])
        fish.pull_line, fish.reel_in, fish.skip = Mock(), Mock(), Mock()
        with patch("navigation.fishing.time.monotonic", side_effect=lambda: self.now):
            self.assertEqual(fish.cast(), FishingResult.SUCCESS)
        self.assertEqual([c.args[0] for c in fish.press.call_args_list], ["sub_ability", 83])
        fish.wait.assert_called_once_with(2)
        fish.pull_line.assert_called_once()
        fish.reel_in.assert_called_once()
        fish.skip.assert_called_once()

    def test_fifth_strike_ends_fishing_without_another_strike_input(self):
        fish = self.fishing()
        fish.ui.asset.return_value = None
        fish.state = Mock(side_effect=[FishingState.FINISH]+[FishingState.STRIKE]*5)
        with patch("navigation.fishing.time.monotonic", side_effect=lambda: self.now):
            self.assertEqual(fish.cast(), FishingResult.NO_FISH)
        self.assertEqual([c.args[0] for c in fish.press.call_args_list], [83]*4)

    def test_brief_unrecognized_transition_does_not_enter_reward_handler(self):
        fish = self.fishing()
        fish.ui.asset.return_value = None
        fish.state = Mock(side_effect=[FishingState.FINISH]+[FishingState.UNKNOWN]*5+[FishingState.SKIP])
        fish.skip = Mock()
        with patch("navigation.fishing.time.monotonic", side_effect=lambda: self.now):
            self.assertEqual(fish.cast(), FishingResult.SUCCESS)
        fish.skip.assert_called_once()
        self.assertEqual(fish.wait.call_count, 6)

    def test_entrance_accepts_biting_prompt_without_waiting_or_cancelling(self):
        fish = self.fishing()
        fish.ui.asset.return_value = None
        fish.state = Mock(side_effect=[FishingState.UNKNOWN, FishingState.STRIKE, FishingState.SKIP])
        fish.skip = Mock()
        with patch("navigation.fishing.time.monotonic", side_effect=lambda: self.now):
            self.assertEqual(fish.cast(), FishingResult.SUCCESS)
        self.assertEqual([c.args[0] for c in fish.press.call_args_list], ["sub_ability", 83])
        fish.wait.assert_not_called()
        fish.skip.assert_called_once()

    def test_fishing_state_uses_text_when_image_templates_miss(self):
        for text, expected in (("R 收竿", FishingState.FINISH), ("S 提竿", FishingState.STRIKE),
                               ("A / D 调整发力", FishingState.PULL_LINE),
                               ("收线", FishingState.REEL_IN), ("", FishingState.UNKNOWN)):
            with self.subTest(text=text):
                fish = self.fishing()
                fish.ui.asset.return_value = None
                fish.ui.text.return_value = text
                self.assertEqual(fish.state(), expected)
                fish.ui.text.assert_called_once()
                self.assertIs(fish.ui.text.call_args.kwargs["image"], fish.last_state_frame)

    def test_unrecognized_start_saves_footer_and_halts_without_exit_input(self):
        fish = self.fishing()
        fish.ui.asset.return_value = None
        fish.ui.text.return_value = ""
        fish.ui.roi.return_value = [962, 675, 267, 27]
        fish.rt.capture.return_value = np.full((720, 1280, 3), 100, dtype=np.uint8)
        with tempfile.TemporaryDirectory() as directory, patch("navigation.fishing.ROOT", Path(directory)), \
                patch("navigation.fishing.time.monotonic", side_effect=lambda: self.now):
            with self.assertRaisesRegex(NavigationError, "未识别到钓星操作提示"):
                fish.cast()
            import cv2
            saved = list((Path(directory) / "logs/navigation").glob("*-fishing-state.png"))
            self.assertEqual(len(saved), 1)
            image = cv2.imread(str(saved[0]))
            self.assertEqual(image.shape[:2], (720, 1280))
            self.assertTrue((image[675:702, 962:1229] == 100).all())
            self.assertFalse(image[700:, :240].any())
        self.assertTrue(fish.rt.navigation_halted)
        self.assertEqual([c.args[0] for c in fish.press.call_args_list], ["sub_ability"])

    def test_at_most_five_casts_and_second_candidate_skips_even_zero_catches(self):
        fish = self.fishing()
        fish.ui.asset.return_value = [1, 2, 3, 4]
        fish.cast = Mock(return_value=FishingResult.SUCCESS)
        self.assertTrue(fish.run())
        self.assertEqual(fish.cast.call_count, 5)
        self.assertEqual(fish.materials, {"陨星": 0})
        self.assertTrue(fish.run())
        self.assertEqual(fish.cast.call_count, 5)
        self.assertEqual([c.args[0] for c in fish.press.call_args_list], ["ability_use", "ability_use"])

    def test_unavailable_subability_keeps_second_candidate_available(self):
        fish = self.fishing()
        fish.ui.asset.return_value = None
        fish.ui.roi.return_value = [1083, 583, 40, 40]
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        fish.rt.capture.return_value = frame
        fish.cast = Mock()
        with tempfile.TemporaryDirectory() as directory, patch("navigation.fishing.ROOT", Path(directory)):
            self.assertTrue(fish.run())
            saved = list((Path(directory) / "logs/navigation").glob("*-fishing-availability.png"))
            self.assertEqual(len(saved), 1)
            import cv2
            self.assertEqual(cv2.imread(str(saved[0])).shape[:2], (680, 1280))
        self.assertNotIn("陨星", fish.materials)
        fish.cast.assert_not_called()
        self.assertEqual(fish.ui.asset.call_count, 3)
        self.assertTrue(all(c.kwargs["image"] is frame for c in fish.ui.asset.call_args_list))
        events = [c.args[0] for c in fish.actions.event.call_args_list]
        self.assertEqual([e["attempt"] for e in events[:-1]], [1, 2, 3])
        self.assertTrue(all(not e["available"] for e in events[:-1]))
        self.assertEqual(events[-1]["reason"], "subability_not_detected")
        self.assertEqual([c.args[0] for c in fish.press.call_args_list], ["ability_use", "ability_use"])

    def test_fishing_icon_matches_recorded_hud_and_rejects_missing_icon(self):
        import cv2
        from navigation.ability import WHITE
        nodes = json.loads((ROOT / "resource/pipeline/alignment_assets.json").read_text(encoding="utf-8"))
        rt = SimpleNamespace(context=SimpleNamespace(get_node_data=lambda node: nodes.get(node, {})))
        ui = GameUI(rt, ROOT / "resource")
        for name, available in (("available", True), ("unavailable", False)):
            with self.subTest(name=name):
                hud = cv2.imread(str(ROOT / "scripts/fixtures" / f"fishing_{name}_hud.png"))
                self.assertIsNotNone(hud)
                frame = np.zeros((720, 1280, 3), dtype=np.uint8)
                frame[550:675, 1040:1190] = hud
                box = ui.asset("IconAbilityFish", image=frame, roi=ui.roi("AreaSubAbilityButton"),
                               color=WHITE, threshold=.8)
                self.assertEqual(bool(box), available)
                if box:
                    self.assertGreaterEqual(box[0], 1094)
                    self.assertGreaterEqual(box[1], 593)
                    self.assertLessEqual(box[0]+box[2], 1115)
                    self.assertLessEqual(box[1]+box[3], 616)

    def test_pull_direction_releases_when_progress_stops_improving(self):
        fish = self.fishing()
        fish.inputs.foreground.return_value = True
        fish.tension = Mock(side_effect=[90, 85])
        self.assertEqual(fish.pull_direction("A", 100), 85)
        down = fish.inputs._down.call_args.args[0]
        self.assertEqual(down.code, 65)
        fish.inputs._up.assert_called_once_with(down)
        self.assertEqual(fish.wait.call_count, 2)

    def test_route_teleport_happens_before_walking_and_recalibrates_nearby_too(self):
        point = load_route(ROOT / "resource", "home_daily").points[11]
        actions = RouteActions.__new__(RouteActions)
        actions.motion, actions.rt, actions.teleporter, actions.navigator, actions.event = Mock(), Mock(), Mock(), Mock(), Mock()
        for distance, moved in ((40, True), (10, False)):
            actions.teleporter.reset_mock()
            actions.navigator.turn_ratio = 12
            self.assertTrue(actions.prepare_point(point, Pose(point.x+distance, point.y, 1, 1, 0, 1)))
            self.assertEqual(actions.teleporter.transport.called, moved)
            self.assertIsNone(actions.navigator.turn_ratio)

    def test_navigator_prepares_distant_teleport_before_turning_or_walking(self):
        origin, target = Point(0, 0), Point(100, 100, action="teleport")
        route = Route("test", "home", (origin, target), 15, 60, None, {})
        inputs, locator, action = Mock(), Mock(), Mock(return_value="continue")
        inputs.bindings = GameKeys()
        locator.spec.id = "home"
        current = Pose(0, 0, 1, 1, 0, 1)
        def prepare(point, pose):
            nonlocal current
            if point.action != "teleport":
                return False
            self.assertEqual((pose.x, pose.y), (0, 0))
            current = Pose(100, 100, 1, 1, 0, 1)
            return True
        navigator = Navigator(locator, inputs, Mock(), lambda: False, Mock(),
                              point_action=action, point_prepare=prepare)
        navigator.observe = Mock(side_effect=lambda hint: current)
        navigator.turn = Mock(side_effect=AssertionError("Must teleport before walking"))
        result = navigator.follow(route)
        self.assertEqual((result.x, result.y), (100, 100))
        navigator.turn.assert_not_called()
        inputs.key_down.assert_not_called()
        action.assert_called_once_with(target)

    def test_fishing_failure_records_failure_and_continues_route(self):
        route = load_route(ROOT / "resource", "home_daily")
        card = SimpleNamespace(rule=SimpleNamespace(executor="home"), remaining=None)
        actions = RouteActions(Mock(stopped=False), ROOT / "resource", route, "home", card, Mock(), Mock())
        actions.fishing.run = Mock(side_effect=NavigationError("切换采星能力失败"))
        self.assertEqual(actions(route.points[2]), "continue")
        self.assertFalse(actions.complete())

    def test_giant_meteor_notice_keeps_route_result_and_skips_after_stop(self):
        rt = Mock(stopped=False)
        rt.navigate.return_value = False
        rt.ui.text.return_value = "发现巨陨星"
        self.assertFalse(Home(rt).run())
        rt.ui.open_map.assert_called_once_with()
        self.assertIn("今日有巨陨星！", [c.args[0] for c in rt.log.call_args_list])
        rt.stopped = True
        rt.ui.open_map.reset_mock()
        self.assertFalse(Home(rt).run())
        rt.ui.open_map.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
