"""Offline developer-subtask tests: python/python.exe -B scripts/test_subtask_test.py."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
os.environ.setdefault("MAAFW_BINARY_PATH", str(ROOT / "maafw"))
from daily.planner import Card, classify
from daily.subtask_test import SubtaskTest, RULES, test_rule, unsupported_reason
from daily.startup import focus_game
from navigation.models import NavigationError


class SubtaskTests(unittest.TestCase):
    def runner(self, mode="xinghai"):
        context = SimpleNamespace(tasker=SimpleNamespace(controller=Mock(), stopping=False),
                                  get_node_data=lambda name: {})
        rt = SubtaskTest(context, mode)
        rt.log = Mock()
        rt.main = Mock(return_value=True)
        rt.execute = Mock(return_value=True)
        rt.available = Mock(return_value=True)
        return rt

    def run_with(self, rt, key, directory, inputs=None):
        inputs = inputs or Mock()
        inputs.foreground.return_value = True
        inputs.focus_details.return_value = {"game_hwnd": 42, "foreground_hwnd": 42}
        with patch.dict(os.environ, {"PI_MAANIKKI_DEV_MODE": "1"}), \
                patch("daily.subtask_test.ROOT", Path(directory)), \
                patch("daily.subtask_test.TestInputs", return_value=inputs), \
                patch("daily.subtask_test.inputs_allowed", return_value=True), \
                patch("daily.subtask_test.focus_game"):
            result = rt.run_test(key)
        report = json.loads((rt.test_directory / "result.json").read_text(encoding="utf-8"))
        return result, report, inputs

    def test_catalog_covers_every_rule_with_unique_entries_and_developer_flag(self):
        definition = json.loads((ROOT / "tasks/SubtaskTest.json").read_text(encoding="utf-8"))
        nodes = json.loads((ROOT / "resource/pipeline/subtask_test.json").read_text(encoding="utf-8"))
        entries = definition["task"]
        self.assertEqual(len(entries), 32)
        self.assertEqual(len({task["name"] for task in entries}), 32)
        self.assertEqual(len({task["entry"] for task in entries}), 32)
        expected = {(mode, rule.key) for mode, rules in RULES.items() for rule in rules}
        actual = set()
        imported = json.loads((ROOT / "interface.json").read_text(encoding="utf-8"))["import"]
        self.assertIn("tasks/SubtaskTest.json", imported)
        available_options = set()
        for filename in imported:
            available_options.update(json.loads((ROOT / filename).read_text(encoding="utf-8")).get("option", {}))
        for task in entries:
            self.assertTrue(task["developer_only"])
            self.assertFalse(task["default_check"])
            self.assertTrue(set(task["option"]) <= available_options)
            node = nodes[task["entry"]]
            self.assertEqual(node["custom_action"], "nikki.subtask_test")
            self.assertEqual(node["next"], [])
            mode, key = node["custom_action_param"].values()
            actual.add((mode, key))
            if unsupported_reason(test_rule(mode, key)):
                self.assertTrue("未适配" in task["label"] or "进度条件" in task["label"])
        self.assertEqual(actual, expected)

    def test_disabled_mode_or_invalid_key_never_sends_input(self):
        for mode, key, enabled in (("xinghai", "bell", "0"), ("bad", "bell", "1"),
                                   ("xinghai", "bad", "1")):
            rt = self.runner(mode)
            with patch.dict(os.environ, {"PI_MAANIKKI_DEV_MODE": enabled}), \
                    patch("daily.subtask_test.TestInputs") as inputs:
                self.assertFalse(rt.run_test(key))
                inputs.assert_not_called()
                rt.execute.assert_not_called()
                self.assertIsNone(rt.test_directory)

    def test_unsupported_and_cumulative_energy_only_report_no_game_input(self):
        for mode, key in (("xinghai", "coco"), ("zhaoxi", "energy")):
            rt = self.runner(mode)
            with tempfile.TemporaryDirectory() as directory, \
                    patch.dict(os.environ, {"PI_MAANIKKI_DEV_MODE": "1"}), \
                    patch("daily.subtask_test.ROOT", Path(directory)), \
                    patch("daily.subtask_test.TestInputs") as inputs:
                self.assertFalse(rt.run_test(key))
                inputs.assert_not_called()
                rt.execute.assert_not_called()
                report = json.loads((rt.test_directory / "result.json").read_text(encoding="utf-8"))
                self.assertEqual(report["status"], "unsupported")

    def test_plain_executor_runs_without_score_scan_claim_or_daily_schedule(self):
        rt = self.runner()
        rt.scan, rt.score, rt.claim, rt.run_daily = Mock(), Mock(), Mock(), Mock()
        with tempfile.TemporaryDirectory() as directory:
            ok, report, inputs = self.run_with(rt, "bell", directory)
            self.assertTrue(ok)
            self.assertEqual(rt.execute.call_args.args[1].rule.key, "bell")
            self.assertEqual(report["card_source"], "developer_test")
            for method in (rt.scan, rt.score, rt.claim, rt.run_daily):
                method.assert_not_called()
            inputs.release.assert_called_once()
            self.assertIsNone(rt.navigation_inputs)
            self.assertFalse((Path(directory) / "logs/daily/xinghai-latest.json").exists())

    def test_auto_disabled_setting_does_not_disable_explicit_test_but_other_flags_stay(self):
        rt = self.runner()
        with patch("action.daily_tasks.Executors.setting", return_value={
                "auto_complete": False, "allow_chat": False, "bubble_equipped": False}):
            settings = rt.setting("xinghai")
        self.assertTrue(settings["auto_complete"])
        self.assertFalse(settings["allow_chat"])
        self.assertFalse(settings["bubble_equipped"])

    def test_unavailable_option_does_not_execute(self):
        rt = self.runner()
        rt.available.return_value = False
        with tempfile.TemporaryDirectory() as directory:
            ok, report, inputs = self.run_with(rt, "bubble", directory)
            self.assertFalse(ok)
            self.assertEqual(report["status"], "unavailable")
            rt.execute.assert_not_called()
            inputs.release.assert_called_once()

    def test_focus_startup_failure_does_not_attempt_main_recovery(self):
        rt = self.runner()
        inputs = Mock()
        inputs.foreground.return_value = True
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"PI_MAANIKKI_DEV_MODE": "1"}), \
                patch("daily.subtask_test.ROOT", Path(directory)), \
                patch("daily.subtask_test.TestInputs", return_value=inputs), \
                patch("daily.subtask_test.inputs_allowed", return_value=True), \
                patch("daily.subtask_test.focus_game", side_effect=NavigationError("前台未稳定")):
            self.assertFalse(rt.run_test("bell"))
            rt.main.assert_not_called()
            rt.execute.assert_not_called()
            inputs.release.assert_called_once()

    def test_shared_startup_automatically_focuses_without_moving_or_pressing(self):
        for already_focused in (False, True):
            runtime, inputs = Mock(), Mock()
            runtime.stopped = False
            inputs.foreground.return_value = already_focused
            def activate(*args, **kwargs):
                inputs.foreground.return_value = True
                return SimpleNamespace(succeeded=True)
            runtime.controller.post_key_up.return_value.wait.side_effect = activate
            ticks = [0.0]
            def sleep(seconds):
                ticks[0] += seconds
            with patch("daily.startup.time.monotonic", side_effect=lambda: ticks[0]), \
                    patch("daily.startup.time.sleep", side_effect=sleep):
                focus_game(runtime, inputs)
            if already_focused:
                runtime.controller.post_key_up.assert_not_called()
            else:
                runtime.controller.post_key_up.assert_called_once_with(18)
            runtime.action.assert_not_called()
            self.assertLess(ticks[0], .5)
            inputs.check.assert_called_once()

    def test_stopped_startup_does_not_refocus_and_failed_activation_is_rejected(self):
        runtime, inputs = Mock(), Mock()
        runtime.stopped = True
        with self.assertRaises(NavigationError):
            focus_game(runtime, inputs)
        runtime.action.assert_not_called()
        runtime.stopped = False
        inputs.foreground.return_value = False
        runtime.controller.post_key_up.return_value.wait.return_value.succeeded = False
        with self.assertRaises(NavigationError):
            focus_game(runtime, inputs)
        inputs.check.assert_not_called()

    def test_meteor_uses_real_unfinished_card_and_preserves_slot(self):
        rt = self.runner()
        card = classify("xinghai", "召唤1次流星(0/1)", 4)
        rt.open_page = Mock(return_value=True)
        rt.scan = Mock(return_value=[classify("xinghai", "召唤摇铃", 0), card])
        with tempfile.TemporaryDirectory() as directory:
            ok, report, inputs = self.run_with(rt, "meteor", directory)
            self.assertTrue(ok)
            rt.execute.assert_called_once_with("xinghai", card)
            self.assertEqual(report["card"]["slot"], 4)
            self.assertEqual(report["card_source"], "daily_scan")

    def test_absent_meteor_does_not_fake_a_go_now_card(self):
        rt = self.runner()
        rt.open_page, rt.scan = Mock(return_value=True), Mock(return_value=[])
        with tempfile.TemporaryDirectory() as directory:
            ok, report, inputs = self.run_with(rt, "meteor", directory)
            self.assertFalse(ok)
            self.assertIn("未完成", report["reason"])
            rt.execute.assert_not_called()
            inputs.release.assert_called_once()

    def test_energy_is_one_specific_realm_with_test_context_and_minimum_count(self):
        for key in ("jihua", "bless", "monster"):
            rt = self.runner("zhaoxi")
            with tempfile.TemporaryDirectory() as directory, \
                    patch("daily.subtask_test.Gameplay") as gameplay:
                gameplay.return_value.realm.return_value = True
                ok, report, inputs = self.run_with(rt, key, directory)
                self.assertTrue(ok)
                gameplay.assert_called_once_with(rt)
                gameplay.return_value.realm.assert_called_once_with(key, maximize=False, return_page="calendar")
                rt.execute.assert_not_called()

    def test_dig_reuses_existing_executor(self):
        rt = self.runner("zhaoxi")
        with tempfile.TemporaryDirectory() as directory:
            ok, report, inputs = self.run_with(rt, "dig", directory)
            self.assertTrue(ok)
            self.assertEqual(rt.execute.call_args.args[1].rule.executor, "dig")

    def test_failure_frame_is_kept_before_return_to_main(self):
        rt = self.runner()
        rt.last_frame = np.full((720, 1280, 3), 123, dtype=np.uint8)
        rt.execute.return_value = False
        def recover():
            rt.last_frame = np.zeros((720, 1280, 3), dtype=np.uint8)
            return True
        rt.main.side_effect = recover
        with tempfile.TemporaryDirectory() as directory:
            ok, report, inputs = self.run_with(rt, "photo", directory)
            self.assertFalse(ok)
            frame = cv2.imread(str(rt.test_directory / "failure.png"))
            self.assertEqual(frame.shape[:2], (680, 1280))
            self.assertEqual(int(frame.mean()), 123)
            inputs.release.assert_called_once()

    def test_stop_and_return_failure_are_not_reported_as_success(self):
        for outcome in ("stop", "return_failure", "focus_loss", "exception"):
            rt = self.runner()
            inputs = Mock()
            if outcome == "stop":
                def stop(*args):
                    rt.context.tasker.stopping = True
                    return True
                rt.execute.side_effect = stop
            elif outcome == "return_failure":
                rt.main.return_value = False
            elif outcome == "focus_loss":
                rt.execute.side_effect = lambda *args: setattr(inputs.foreground, "return_value", False) or False
            else:
                rt.execute.side_effect = NavigationError("test failure")
            with tempfile.TemporaryDirectory() as directory:
                ok, report, inputs = self.run_with(rt, "bell", directory, inputs)
                self.assertFalse(ok)
                inputs.release.assert_called_once()
                self.assertIsNone(rt.navigation_inputs)
                if outcome in ("stop", "focus_loss"):
                    rt.main.assert_not_called()
                    self.assertEqual(report["status"], "stopped")


if __name__ == "__main__":
    unittest.main()
