"""Offline regressions: python/python.exe -B scripts/test_meteor.py."""
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
os.environ.setdefault("MAAFW_BINARY_PATH", str(ROOT / "maafw"))
from daily.meteor import Meteor, meteor_routes, select_meteor_route, MAP, CLOSE, READY
from daily.planner import classify, select_free
from daily.runtime import Executors, Runtime
from navigation.models import NavigationError
from recognition.ui_template import UITemplateRecognition


class Clock:
    now = 0.0

    def pause(self, seconds):
        self.now += seconds
        return True


class MeteorTests(unittest.TestCase):
    def setUp(self):
        self.card = classify("xinghai", "召唤1次流星(0/1)", 3)
        self.routes = meteor_routes(ROOT / "resource")
        self.clock = Clock()
        self.rt = Mock()
        self.rt.stopped = False
        self.rt.pause.side_effect = self.clock.pause
        self.rt.input_guard.return_value = nullcontext()
        self.service = Meteor(self.rt, ROOT / "resource")
        self.service.report = Mock()

    def test_scan_card_is_executable_and_bell_stays_first(self):
        bell = classify("xinghai", "召唤摇铃(0/1)", 0)
        self.assertEqual(self.card.rule.executor, "meteor")
        self.assertEqual(select_free([self.card, bell], 0)[0], bell)
        self.assertEqual(select_free([self.card], 500), [])

    def test_four_centers_select_exact_routes(self):
        for name, route in self.routes.items():
            center = route.document["editor_target"]
            selected, _ = select_meteor_route((center["x"], center["y"]), self.routes)
            self.assertEqual(selected["route"], name)
            self.assertFalse(any(p.action for p in route.points))

    def test_unknown_and_ambiguous_locations_rejected(self):
        self.assertIsNone(select_meteor_route((0, 0), self.routes)[0])
        a = SimpleNamespace(document={"editor_target": {"x": 10, "y": 10}})
        b = SimpleNamespace(document={"editor_target": {"x": 12, "y": 10}})
        self.assertIsNone(select_meteor_route((10, 10), {"a": a, "b": b})[0])

    def test_incomplete_resource_rejected(self):
        with patch("daily.meteor.load_route", return_value=None):
            with self.assertRaises(NavigationError):
                meteor_routes(ROOT / "resource")

    def test_available_respects_auto_option_and_mode(self):
        self.rt.setting.return_value = {"auto_complete": True}
        self.assertTrue(Executors.available(self.rt, "xinghai", self.card))
        self.assertFalse(Executors.available(self.rt, "zhaoxi", self.card))
        self.rt.setting.return_value = {"auto_complete": False}
        self.assertFalse(Executors.available(self.rt, "xinghai", self.card))

    def test_remembered_slot_is_reselected(self):
        layout = {"card_centers": [[100, 200], [200, 200], [300, 200], [400, 200]],
                  "detail_roi": [395, 588, 735, 65]}
        self.rt.context.get_node_data.return_value = {"action": "Custom", "custom_action_param": layout}
        self.assertTrue(self.service.select_card(self.card))
        self.rt.action.assert_called_once_with("Click", target=[400, 200])

    def test_wrong_or_finished_card_cannot_use_go_now(self):
        self.rt.hit.side_effect = lambda node: node == READY
        for text in ("召唤摇铃(0/1)", "召唤1次流星(1/1)", "试试吧，摇动星之铃"):
            self.rt.text.return_value = text
            self.assertFalse(self.service.selected_card(self.card, [0, 0, 100, 100]))
        self.rt.text.return_value = "召唤1次流星(0/1)"
        self.assertTrue(self.service.selected_card(self.card, [0, 0, 100, 100]))
        self.rt.hit.side_effect = lambda node: True
        self.assertFalse(self.service.selected_card(self.card, [0, 0, 100, 100]))

    def test_popup_must_disappear_before_map_is_ready(self):
        self.rt.hit.side_effect = lambda node: node in (MAP, CLOSE)
        self.rt.text.return_value = ""
        self.assertFalse(self.service.clean_map())
        self.rt.hit.side_effect = lambda node: node == MAP
        self.rt.text.return_value = "呼唤流星"
        self.assertFalse(self.service.clean_map())
        self.rt.text.return_value = ""
        self.assertTrue(self.service.clean_map())

    def locate(self, poses, *, popup=False):
        teleporter = Mock()
        sequence = iter(poses)
        last = poses[-1]
        teleporter.map_pose.side_effect = lambda frame: (next(sequence, last), 1)
        self.rt.capture.return_value = np.zeros((720, 1280, 3), dtype=np.uint8)
        self.rt.recognize.side_effect = lambda node, **kw: SimpleNamespace(hit=(popup if node == CLOSE else True))
        with patch("daily.meteor.time.monotonic", side_effect=lambda: self.clock.now):
            return self.service.locate_target(teleporter, self.routes)

    def test_center_animation_must_settle_without_five_second_delay(self):
        self.assertEqual(self.locate([(1200, 900, .8, .05), (1350, 950, .8, .05),
            (1366, 954, .8, .05), (1367, 954, .8, .05)]), "xinghai_meteor1")
        self.assertGreaterEqual(self.clock.now, 1.0)
        self.assertLess(self.clock.now, 2.0)

    def test_unreliable_pose_or_popup_cannot_classify(self):
        for pose, popup in (((1366, 954, .4, .05), False),
                            ((1366, 954, .8, .001), False),
                            ((1366, 954, .8, .05), True)):
            self.clock.now = 0
            with self.assertRaises(NavigationError):
                self.locate([pose], popup=popup)

    def setup_chain(self):
        for name in ("open_target", "close_dialog"):
            setattr(self.service, name, Mock(return_value=True))
        self.service.locate_target = Mock(return_value="xinghai_meteor2")
        self.service.teleporter = Mock()
        self.service.ring = Mock(return_value=True)

    def test_full_chain_only_rings_at_destination(self):
        self.setup_chain()
        order = []
        self.service.open_target.side_effect = lambda card: order.append("open") or True
        self.service.close_dialog.side_effect = lambda: order.append("close") or True
        self.service.locate_target.side_effect = lambda *args: order.append("locate") or "xinghai_meteor2"
        self.rt.navigate.side_effect = lambda *args, **kw: order.append("travel") or True
        self.service.ring.side_effect = lambda **kw: order.append("bell") or True
        self.assertTrue(self.service.run("xinghai", self.card))
        self.assertEqual(order, ["open", "close", "locate", "travel", "bell"])
        self.rt.navigate.assert_called_once_with("xinghai_meteor2", "xinghai", self.card, meteor_travel=True)
        self.service.ring.assert_called_once_with(at_hub=True)
        self.service.report.assert_called_once_with("meteor")

    def test_failed_travel_or_stop_never_rings(self):
        self.setup_chain()
        self.rt.navigate.return_value = False
        self.assertFalse(self.service.run("xinghai", self.card))
        self.service.ring.assert_not_called()
        self.rt.navigate.return_value = True
        self.rt.stopped = True
        self.assertFalse(self.service.run("xinghai", self.card))
        self.service.ring.assert_not_called()

    def test_failed_close_never_locates_or_travels(self):
        self.setup_chain()
        self.service.close_dialog.return_value = False
        self.assertFalse(self.service.run("xinghai", self.card))
        self.service.locate_target.assert_not_called()
        self.rt.navigate.assert_not_called()
        self.service.ring.assert_not_called()

    def test_meteor_navigation_transports_from_current_map_without_task_actions(self):
        rt = self.rt
        rt.routes.return_value = self.routes["xinghai_meteor2"].document
        rt.game_keys, rt.navigation_inputs = Mock(), None
        with tempfile.TemporaryDirectory(dir=ROOT / ".build") as folder, \
                patch("daily.runtime.ROOT", Path(folder)), \
                patch("daily.runtime.foreground_inputs", return_value=True), \
                patch("navigation.controller.ForegroundInput") as inputs, \
                patch("navigation.vision.Locator"), \
                patch("navigation.engine.Navigator") as navigator, \
                patch("navigation.teleport.Teleporter") as teleporter, \
                patch("navigation.daily_route.RouteActions") as actions:
            inputs.return_value.hwnd = 123
            inputs.return_value.foreground.return_value = True
            self.assertTrue(Runtime.navigate(rt, "xinghai_meteor2", "xinghai", self.card, meteor_travel=True))
            teleporter.return_value.transport.assert_called_once_with(rt.routes.return_value["teleport"])
            teleporter.return_value.prepare.assert_not_called()
            actions.assert_not_called()
            navigator.return_value.follow.assert_called_once()
            inputs.return_value.release.assert_called_once()
            self.assertIsNone(rt.navigation_inputs)

    def test_binary_close_template_positive_and_negative(self):
        node = json.loads((ROOT / "resource/pipeline/meteor.json").read_text())[CLOSE]
        param = node["custom_recognition_param"]
        template = cv2.imread(str(ROOT / "resource/image" / param["template"]))
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        frame[516:516+template.shape[0], 1109:1109+template.shape[1]] = template
        argv = SimpleNamespace(image=frame, custom_recognition_param=param)
        result = UITemplateRecognition().analyze(None, argv)
        self.assertIsNotNone(result.box)
        self.assertGreater(result.detail["score"], .98)
        # Clicking the cropped bright-feature center still lands inside the X.
        x, y, w, h = result.box
        self.assertTrue(1120 <= x+w/2 <= 1145 and 525 <= y+h/2 <= 555)
        frame[:] = 0
        self.assertIsNone(UITemplateRecognition().analyze(None, argv).box)


if __name__ == "__main__":
    unittest.main()
