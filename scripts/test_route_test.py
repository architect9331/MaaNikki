"""Offline route-test checks: python/python.exe -B scripts/test_route_test.py."""
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
from daily.route_test import RouteTest, inputs_allowed, test_document
from navigation.models import NavigationError

RESOURCE = ROOT / "resource"
DOCUMENT = json.loads((RESOURCE / "routes/daily/xinghai_meteor1.json").read_text(encoding="utf-8"))


class RouteTestChecks(unittest.TestCase):
    def test_current_submission_controller_takes_priority_over_saved_active_tab(self):
        with patch("daily.route_test.foreground_inputs", return_value=False) as saved:
            for mouse, keyboard, allowed in [("Seize", "Seize", True), ("Seize", "SendMessage", False)]:
                controller = {"type": "Win32", "win32": {"mouse": mouse, "keyboard": keyboard}}
                with patch.dict(os.environ, {"PI_CONTROLLER": json.dumps(controller)}):
                    self.assertEqual(inputs_allowed(), allowed)
            with patch.dict(os.environ, {"PI_CONTROLLER": "invalid"}):
                self.assertFalse(inputs_allowed())
            saved.assert_not_called()
    def test_all_dropdown_routes_are_valid_movement_routes(self):
        definition = json.loads((ROOT / "tasks/RouteTest.json").read_text(encoding="utf-8"))
        self.assertTrue(definition["task"][0]["developer_only"])
        for case in definition["option"]["RouteTestRoute"]["cases"]:
            if case["name"] != "file":
                model, spec = test_document(RESOURCE, {"route": case["name"]})
                self.assertEqual(model.map_id, spec.id)
                self.assertFalse(any(point.action for point in model.points))

    def test_invalid_files_and_task_actions_are_rejected_before_input(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "route.json"
            for change in [
                {"points": [{"x": 1, "y": 2, "action": "macro"}]},
                {"points": [{"x": float("nan"), "y": 2}]},
                {"points": [{"x": 1, "y": 2, "radius": -1}]},
                {"points": [{"x": 1, "y": 2, "jump": "false"}]},
                {"map": "missing"}, {"timeout": 10000}, {"points": []},
                {"teleport": {"name": "missing"}},
            ]:
                file.write_text(json.dumps({**DOCUMENT, **change}), encoding="utf-8")
                with self.subTest(change=change), self.assertRaises(NavigationError):
                    test_document(RESOURCE, {"file": str(file)})
            for settings in [{"route": "../other"}, {"file": "relative.json"}, {"file": str(file.with_suffix('.exe'))}]:
                with self.subTest(settings=settings), self.assertRaises(NavigationError):
                    test_document(RESOURCE, settings)

    def runner(self):
        context = SimpleNamespace(tasker=SimpleNamespace(controller=Mock(), stopping=False),
                                  get_node_data=lambda name: {"action": {"param": {"custom_action_param": {"value": "V"}}}}
                                  if name == "MaaNikki_Key_map" else {})
        rt = RouteTest(context)
        rt.log = Mock()
        return rt

    def test_disabled_mode_sends_no_input(self):
        rt = self.runner()
        with patch.dict(os.environ, {"PI_MAANIKKI_DEV_MODE": "0"}), patch("daily.route_test.TestInputs") as inputs:
            self.assertFalse(rt.run_test({"route": "xinghai_meteor1"}))
            inputs.assert_not_called()
        self.assertIsNone(rt.directory)

    def test_waypoint_ui_logging_does_not_block_held_movement(self):
        rt = self.runner()
        rt.navigation_inputs = SimpleNamespace(held={87}, buttons=set())
        rt.event({"type": "waypoint", "index": 0, "total": 2})
        rt.log.assert_not_called()
        rt.navigation_inputs.held.clear()
        rt.event({"type": "inputs_released"})
        rt.log.assert_called_once()
        self.assertIn("第 1 点", rt.log.call_args.args[0])

    def test_snapshot_transmission_and_release_on_success_failure_and_stop(self):
        for outcome in ("success", "failure", "stop"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                rt = self.runner()
                rt.last_capture = np.full((720, 1280, 3), 255, dtype=np.uint8)
                file = Path(directory) / "route.json"
                original = copy.deepcopy(DOCUMENT)
                file.write_text(json.dumps(original), encoding="utf-8")
                inputs = Mock()
                inputs.foreground.return_value = True
                inputs.focus_details.return_value = {"game_hwnd": 42, "foreground_hwnd": 42}
                navigator = Mock()
                navigator.follow.return_value = SimpleNamespace(dict=lambda: {"x": 1376.31, "y": 971.61})
                if outcome == "failure":
                    navigator.follow.side_effect = NavigationError("游戏已离开前台")
                if outcome == "stop":
                    def stop(model):
                        rt.context.tasker.stopping = True
                        raise NavigationError("已停止")
                    navigator.follow.side_effect = stop
                def changed_source(model):
                    altered = copy.deepcopy(original)
                    altered["points"][1]["x"] += 100
                    file.write_text(json.dumps(altered), encoding="utf-8")
                teleporter = Mock()
                teleporter.prepare.side_effect = changed_source
                ticks = [0]
                def sleep(seconds):
                    ticks[0] += seconds
                with patch.dict(os.environ, {"PI_MAANIKKI_DEV_MODE": "1"}), \
                        patch("daily.route_test.ROOT", Path(directory)), \
                        patch("daily.route_test.inputs_allowed", return_value=True), \
                        patch("daily.route_test.Locator"), \
                        patch("daily.route_test.TestInputs", return_value=inputs) as bind, \
                        patch("daily.route_test.TestTeleporter", return_value=teleporter), \
                        patch("daily.route_test.Navigator", return_value=navigator) as navigate, \
                        patch("daily.route_test.time.monotonic", side_effect=lambda: ticks[0]), \
                        patch("daily.route_test.time.sleep", side_effect=sleep):
                    self.assertEqual(rt.run_test({"file": str(file)}), outcome == "success")
                self.assertIs(bind.call_args.args[0], rt.controller)
                self.assertIs(bind.call_args.kwargs["bindings"], rt.game_keys)
                self.assertEqual(rt.game_keys.get("map").code, ord("V"))
                self.assertIsNone(rt.navigation_inputs)
                inputs.release.assert_called_once()
                self.assertNotIn("point_action", navigate.call_args.kwargs)
                self.assertEqual(navigator.follow.call_args.args[0].document, original)
                self.assertEqual(json.loads((rt.directory / "route.json").read_text(encoding="utf-8")), original)
                result = json.loads((rt.directory / "result.json").read_text(encoding="utf-8"))
                self.assertEqual(result["success"], outcome == "success")
                if outcome != "success":
                    self.assertTrue((rt.directory / "failure.png").is_file())
                    self.assertEqual(result["stopped"], outcome == "stop")


if __name__ == "__main__":
    unittest.main(verbosity=2)
