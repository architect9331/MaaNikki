"""Fishing regressions: python/python.exe -B scripts/test_fishing.py."""
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from copy import deepcopy
import json
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from daily.fishing import current_fishing, fishing_opportunity, route_fishing, selected_fishing_route
from navigation.fishing import Fishing, FishingResult, FishingState, cast_count, fish_name
from navigation.daily_route import RouteActions, preflight
from navigation.models import NavigationError, Point


class FishingTests(unittest.TestCase):
    def fishing(self, kind="fish"):
        actions = Mock()
        actions.rt.stopped = False
        actions.rt.observe.side_effect = lambda **kwargs: nullcontext()
        fish = Fishing(actions, kind)
        fish.ability = Mock()
        fish.press, fish.wait = Mock(), Mock()
        fish.cast = Mock(return_value=FishingResult.SUCCESS)
        return fish

    def test_default_cast_counts_and_home_cleanup(self):
        fish = self.fishing()
        self.assertTrue(fish.run())
        self.assertEqual(fish.cast.call_count, 3)
        fish.press.assert_not_called()
        home = self.fishing("home")
        home.prepare_home = Mock(return_value=True)
        self.assertTrue(home.run())
        self.assertEqual(home.cast.call_count, 5)
        home.press.assert_called_once_with("ability_use")

    def test_count_validation_happens_before_ability_or_input(self):
        self.assertEqual(cast_count(" 7 ", 3), 7)
        self.assertEqual(cast_count(None, 3), 3)
        for count in (True, False, 0, -1, 1.5, "abc", "2.0"):
            with self.subTest(count=count):
                fish = self.fishing()
                with self.assertRaises(NavigationError):
                    fish.run(max_cast_count=count)
                fish.ability.select.assert_not_called()
                fish.press.assert_not_called()

    def test_explicit_count_and_early_no_fish(self):
        fish = self.fishing()
        fish.run(max_cast_count=2)
        self.assertEqual(fish.cast.call_count, 2)
        fish.cast.reset_mock()
        fish.cast.side_effect = [FishingResult.SUCCESS, FishingResult.NO_FISH]
        fish.run(max_cast_count=9)
        self.assertEqual(fish.cast.call_count, 2)

    def test_ordinary_and_home_cast_keys(self):
        for kind, key in (("fish", "ability_use"), ("home", "sub_ability")):
            fish = self.fishing(kind)
            fish.switch_fishing()
            fish.press.assert_called_once_with(key)

    def test_passive_flow_preserves_current_ability(self):
        for home in (False, True):
            fish = self.fishing(None)
            fish.state = Mock(return_value=FishingState.STRIKE)
            fish.last_state_frame = np.zeros((720, 1280, 3), np.uint8)
            fish.ui.asset.return_value = [1, 2, 3, 4] if home else None
            fish.run(passive=True)
            self.assertEqual(fish.kind, "home" if home else "fish")
            fish.ability.select.assert_not_called()
            self.assertEqual(fish.cast.call_count, 5 if home else 3)

    def test_passive_unknown_state_never_casts(self):
        fish = self.fishing(None)
        fish.state = Mock(return_value=FishingState.UNKNOWN)
        self.assertFalse(fish.run(passive=True))
        fish.cast.assert_not_called()
        fish.press.assert_not_called()

    def test_bait_precedes_fishing_selection(self):
        fish = self.fishing()
        events = []
        bait = Mock()
        bait.select.side_effect = lambda: events.append("bait") or True
        fish.ability.select.side_effect = lambda: events.append("fishing") or True
        fish.press.side_effect = lambda key: events.append(key)
        with patch("navigation.fishing.Ability", return_value=bait):
            fish.run(use_rhythms=True)
        self.assertEqual(events, ["bait", "ability_use", "fishing"])
        fish.ui.wait_page.assert_called_once_with("MaaNikki_MainDetected", seconds=20)

    def test_notifications_count_fish_instead_of_weight(self):
        self.assertEqual(fish_name("获得道具\n笔刷鱼 × 1.6 kg"), "笔刷鱼")
        self.assertIsNone(fish_name("笔刷鱼×2\n心得×1"))
        fish = self.fishing()
        result = SimpleNamespace(hit=True, filtered_results=[
            SimpleNamespace(text="其他道具×1"), SimpleNamespace(text="笔刷鱼×1.6kg")])
        fish.rt.recognize.return_value = result
        fish.record_material()
        fish.record_material()
        self.assertEqual(fish.materials, {"笔刷鱼": 2})
        result.filtered_results = [SimpleNamespace(text="心得×1")]
        fish.record_material()
        self.assertEqual(fish.materials, {"笔刷鱼": 2})

    def test_reward_records_once_only_after_main_confirmation(self):
        fish = self.fishing()
        fish.ui.asset.return_value = True
        fish.rt.hit.side_effect = [False, True]
        fish.record_material = Mock()
        fish.skip()
        fish.record_material.assert_called_once()

    def test_opportunity_uses_given_frame_and_ignores_cutscene(self):
        fish = self.fishing(None)
        for state, expected in ((FishingState.FINISH, True), (FishingState.SKIP, False),
                                (FishingState.UNKNOWN, False)):
            fish.state = Mock(return_value=state)
            frame = object()
            with patch("daily.fishing.Fishing", return_value=fish):
                self.assertEqual(fishing_opportunity(fish.rt, fish.inputs, frame), expected)
            fish.state.assert_called_once_with(image=frame)

    def test_current_task_releases_after_error_without_treating_focus_as_halt(self):
        rt = Mock(navigation_inputs=None, navigation_halted=False, stopped=False)
        rt.setting.return_value = {"use_rhythms": False}
        inputs = Mock()
        inputs.foreground.return_value = False
        events = []
        rt.main.side_effect = lambda: events.append("main") or True
        with patch("daily.fishing.parameters", return_value={"value": 3}), \
                patch("daily.fishing.foreground_inputs", return_value=True), \
                patch("daily.fishing.ForegroundInput", return_value=inputs), \
                patch("daily.fishing.focus_game", side_effect=lambda *args: events.append("focus")), \
                patch("daily.fishing.run_fishing", side_effect=NavigationError("focus lost")):
            with self.assertRaises(NavigationError):
                current_fishing(rt)
        self.assertEqual(events, ["focus", "main"])
        self.assertFalse(rt.navigation_halted)
        self.assertIsNone(rt.navigation_inputs)
        inputs.release.assert_called_once()

    def test_route_validates_cast_count(self):
        route = SimpleNamespace(document={}, points=[Point(0, 0, action="fishing", params={"cast_count": 0})])
        with self.assertRaises(NavigationError):
            preflight(ROOT / "resource", route, "fishing")
        route.points[0].params["cast_count"] = 2
        preflight(ROOT / "resource", route, "fishing")

    def test_route_counts_only_requested_fish(self):
        rt = Mock()
        route = SimpleNamespace(document={"material": "笔刷鱼"}, points=[Point(0, 0, action="fishing")])
        card = SimpleNamespace(rule=SimpleNamespace(executor="fishing"), remaining=3)
        actions = RouteActions(rt, ROOT / "resource", route, "zhaoxi", card, Mock(), Mock())
        actions.fishing = Mock(materials={"笔刷鱼": 2, "胡子鱼": 1})
        actions(route.points[0])
        self.assertEqual(actions.count, 2)
        self.assertFalse(actions.complete())

    def route_actions(self):
        rt = Mock()
        rt.setting.return_value = {"route_cast_count": "", "use_rhythms": False}
        route = SimpleNamespace(document={"material": "厨师帽鱼", "capacity": 1},
                                points=[Point(0, 0, action="fishing")])
        card = SimpleNamespace(rule=SimpleNamespace(executor="fishing"), remaining=None)
        actions = RouteActions(rt, ROOT / "resource", route, "fishing", card, Mock(), Mock())
        actions.fishing = Mock(materials={})
        return actions

    def test_route_cast_override_and_empty_default(self):
        actions = self.route_actions()
        point = Point(0, 0, action="fishing", params={"cast_count": 2})
        actions(point)
        actions.fishing.run.assert_called_with(max_cast_count=2, use_rhythms=False)
        actions.rt.setting.return_value["route_cast_count"] = "4"
        actions(point)
        actions.fishing.run.assert_called_with(max_cast_count="4", use_rhythms=False)

    def test_route_exhaustion_does_not_require_catches_or_stop_at_capacity(self):
        actions = self.route_actions()
        actions.fishing.materials = {"厨师帽鱼": 3}
        self.assertEqual(actions(Point(0, 0, action="fishing")), "continue")
        self.assertIsNone(actions.quota)
        self.assertTrue(actions.complete())
        actions = self.route_actions()
        actions(Point(0, 0, action="fishing"))
        self.assertEqual(actions.count, 0)
        self.assertTrue(actions.complete())
        actions.fishing.run.return_value = False
        actions(Point(0, 0, action="fishing"))
        self.assertFalse(actions.complete())

    def test_registered_route_preserves_coordinates_and_fishing_point(self):
        route = selected_fishing_route(ROOT / "resource", "fishing_chef_hat_florawish")
        positions = [(28750.74, -44928.26), (28777.74, -44347.76), (28426.74, -43015.76),
                     (28129.74, -42381.26), (27207.24, -40945.76), (27576.24, -40621.76),
                     (28147.74, -40284.26), (27670.74, -39627.26), (28089.24, -40212.26)]
        self.assertEqual([(p.x, p.y) for p in route.points],
            [(round(x*2/90+20819, 1)/2, round(y*2/90+21099, 1)/2) for x, y in positions])
        self.assertEqual([i for i, p in enumerate(route.points) if p.action], [7])
        self.assertEqual(route.points[7].action, "fishing")
        self.assertEqual(route.document["teleport"]["name"], "织裙街")
        self.assertTrue(all(not p.jump and p.radius == 1 for p in route.points))

    def test_route_task_delegates_focus_to_navigation_before_main_inputs(self):
        rt = Mock(stopped=False)
        rt.setting.return_value = {"route_cast_count": ""}
        with patch("daily.fishing.parameters", return_value={"value": "fishing_chef_hat_florawish"}):
            self.assertTrue(route_fishing(rt))
        rt.navigate.assert_called_once()
        name, mode, card = rt.navigate.call_args.args
        self.assertEqual((name, mode), ("fishing_chef_hat_florawish", "fishing"))
        self.assertIsNone(card.remaining)
        self.assertEqual(card.rule.executor, "fishing")
        calls = [c[0] for c in rt.mock_calls]
        self.assertLess(calls.index("navigate"), calls.index("main"))
        rt.reset_mock()
        rt.navigate.return_value = False
        with patch("daily.fishing.parameters", return_value={"value": name}):
            self.assertFalse(route_fishing(rt))
        rt.main.assert_not_called()

    def test_invalid_route_or_count_never_starts_navigation(self):
        rt = Mock()
        for name, count in (("../home_daily", ""), ("home_daily", ""),
                            ("fishing_missing", ""), ("fishing_chef_hat_florawish", "0")):
            with self.subTest(name=name, count=count):
                rt.setting.return_value = {"route_cast_count": count}
                with patch("daily.fishing.parameters", return_value={"value": name}), \
                        self.assertRaises(NavigationError):
                    route_fishing(rt)
                rt.navigate.assert_not_called()
                rt.main.assert_not_called()

    def test_invalid_coordinates_are_rejected_before_input(self):
        original = selected_fishing_route(ROOT / "resource", "fishing_chef_hat_florawish").document
        for value in (float("nan"), -1, 15000):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                (root / "routes/daily").mkdir(parents=True)
                (root / "navigation").mkdir()
                (root / "navigation/maps.json").write_bytes((ROOT / "resource/navigation/maps.json").read_bytes())
                document = deepcopy(original)
                document["points"][3]["x"] = value
                (root / "routes/daily/fishing_chef_hat_florawish.json").write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaises(NavigationError):
                    selected_fishing_route(root, document["id"])


if __name__ == "__main__":
    unittest.main()
