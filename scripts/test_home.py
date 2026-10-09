"""Home daily regressions: python/python.exe -B scripts/test_home.py."""
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from daily.home import Home
from navigation.daily_route import RouteActions, preflight
from navigation.engine import Navigator
from navigation.fishing import StarFishing, FishingState, FishingResult
from navigation.models import load_route, Point, Route, NavigationError
from navigation.vision import Pose
from game_keys import GameKeys


class HomeTests(unittest.TestCase):
    def fishing(self):
        actions = Mock()
        actions.rt.stopped = False
        actions.rt.observe.side_effect = lambda **kwargs: nullcontext()
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

    def test_five_unknown_frames_use_upstream_finish_handling(self):
        fish = self.fishing()
        fish.ui.asset.return_value = None
        fish.state = Mock(side_effect=[FishingState.FINISH]+[FishingState.UNKNOWN]*5)
        fish.skip = Mock()
        with patch("navigation.fishing.time.monotonic", side_effect=lambda: self.now):
            self.assertEqual(fish.cast(), FishingResult.SUCCESS)
        fish.skip.assert_called_once()
        self.assertEqual(fish.wait.call_count, 5)

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
        fish.cast = Mock()
        self.assertTrue(fish.run())
        self.assertNotIn("陨星", fish.materials)
        fish.cast.assert_not_called()
        self.assertEqual(fish.ui.asset.call_count, 3)
        self.assertEqual([c.args[0] for c in fish.press.call_args_list], ["ability_use", "ability_use"])

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
