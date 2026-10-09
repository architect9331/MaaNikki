"""Offline performance/safety regressions: python/python.exe -B scripts/test_performance.py."""
from pathlib import Path
from types import SimpleNamespace
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
os.environ.setdefault("MAAFW_BINARY_PATH", str(ROOT / "maafw"))

import cv2
import numpy as np
from daily.exploration import AssistSchedule, Exploration
from daily.runtime import Runtime
from performance import Performance, _ACTIVE, action_profile, checkpoint, count, measure
from vision import prepared_template, clear_template_cache


def runtime(cls=Runtime):
    controller = SimpleNamespace(post_screencap=Mock(), post_touch_move=Mock(),
                                 post_input_text=Mock())
    frames = [np.full((16, 24, 3), i, np.uint8) for i in range(1, 80)]
    controller.post_screencap.side_effect = [SimpleNamespace(wait=lambda image=image:
        SimpleNamespace(succeeded=True, get=lambda: image)) for image in frames]
    controller.post_touch_move.return_value.wait.return_value.succeeded = True
    controller.post_input_text.return_value.wait.return_value.succeeded = True
    context = SimpleNamespace(tasker=SimpleNamespace(stopping=False, controller=controller),
                              get_node_data=Mock(return_value={}),
                              run_action=Mock(return_value=SimpleNamespace(success=True)),
                              run_recognition=Mock(return_value=SimpleNamespace(hit=False)))
    return cls(context)


class ObservationTests(unittest.TestCase):
    def test_independent_checks_share_one_capture_only_in_scope(self):
        rt = runtime()
        with rt.observe():
            for name in ("main", "dungeon", "reward", "loading"):
                rt.hit(name)
        self.assertEqual(rt.controller.post_screencap.call_count, 1)
        images = [call.args[1] for call in rt.context.run_recognition.call_args_list]
        self.assertTrue(all(image is images[0] for image in images))
        rt.hit("next_decision")
        self.assertEqual(rt.controller.post_screencap.call_count, 2)

    def test_nested_page_predicate_shares_frame(self):
        rt = runtime()
        self.assertFalse(rt.ui.page_matches(lambda: rt.hit("a") or rt.hit("b") or rt.hit("c")))
        self.assertEqual(rt.controller.post_screencap.call_count, 1)

    def test_slow_decision_cannot_keep_reusing_an_aging_frame(self):
        rt = runtime()
        clock = SimpleNamespace(now=0.0)
        with patch("daily.runtime.time.monotonic", side_effect=lambda: clock.now), rt.observe():
            first = rt.frame()
            clock.now = .1
            self.assertIs(rt.frame(), first)
            clock.now = .3
            self.assertIsNot(rt.frame(), first)
        self.assertEqual(rt.controller.post_screencap.call_count, 2)

    def test_input_wait_and_cursor_move_invalidate_pixels_even_when_input_fails(self):
        for operation in (lambda rt: rt.action("ClickKey", key=70),
                          lambda rt: rt.pause(0), lambda rt: rt.ui.unhover(),
                          lambda rt: rt.input_text("test")):
            with self.subTest(operation=operation):
                rt = runtime()
                rt.context.run_action.return_value.success = False
                with rt.observe():
                    first = rt.frame()
                    operation(rt)
                    self.assertIsNot(rt.frame(), first)
                self.assertEqual(rt.controller.post_screencap.call_count, 2)

    def test_explicit_capture_is_always_fresh_and_invalidates_parent(self):
        rt = runtime()
        with rt.observe():
            first = rt.frame()
            second = rt.capture()
            self.assertIsNot(second, first)
            self.assertIsNot(rt.frame(), second)
        self.assertEqual(rt.controller.post_screencap.call_count, 3)

    def test_page_confirmation_uses_two_new_frames_inside_existing_observation(self):
        rt = runtime()
        rt.context.run_recognition.return_value.hit = True
        rt.pause = Mock(return_value=True)
        with rt.observe():
            old = rt.frame()
            self.assertTrue(rt.ui.wait_page("ready"))
        images = [call.args[1] for call in rt.context.run_recognition.call_args_list
                  if call.args[0] == "ready"]
        self.assertEqual(len(images), 2)
        self.assertIsNot(images[0], images[1])
        self.assertTrue(all(image is not old for image in images))
        self.assertEqual(rt.controller.post_screencap.call_count, 3)

    def test_failed_capture_does_not_reuse_last_success(self):
        rt = runtime()
        first = rt.capture()
        rt.controller.post_screencap.side_effect = None
        rt.controller.post_screencap.return_value.wait.return_value.succeeded = False
        with rt.observe():
            self.assertFalse(rt.hit("a"))
            self.assertFalse(rt.hit("b"))
        self.assertIsNotNone(first)
        rt.context.run_recognition.assert_not_called()
        self.assertEqual(rt.controller.post_screencap.call_count, 2)

    def test_stop_prevents_cached_reads_and_input(self):
        rt = runtime()
        with rt.observe():
            rt.frame()
            rt.context.tasker.stopping = True
            self.assertIsNone(rt.frame())
            self.assertFalse(rt.action("ClickKey", key=70))
            self.assertFalse(rt.input_text("test"))
        rt.context.run_action.assert_not_called()
        rt.controller.post_input_text.assert_not_called()

    def test_scrolled_frame_is_searched_without_recapture(self):
        rt = runtime()
        rt.ui.asset = Mock(side_effect=lambda name, image, **kwargs:
                           [1, 1, 4, 4] if int(image[0, 0, 0]) == 3 else None)
        rt.ui.scroll = Mock(return_value=True)
        rt.ui.similarity = Mock(side_effect=[1, 0])
        rt.ui.click_box = Mock(return_value=True)
        self.assertTrue(rt.ui.find(roi=[0, 0, 24, 16], asset="target"))
        self.assertEqual(rt.controller.post_screencap.call_count, 3)
        self.assertEqual([int(call.kwargs["image"][0, 0, 0]) for call in rt.ui.asset.call_args_list], [1, 2, 3])


class TemplateTests(unittest.TestCase):
    def test_registered_glyphs_keep_their_method_after_framework_normalization(self):
        code = """
import json, pathlib, sys
from maa.resource import Resource
from maa.tasker import Tasker
Tasker.set_log_dir(sys.argv[1])
root = pathlib.Path.cwd()
assets = json.loads((root / 'resource/image/game/catalog.json').read_text(encoding='utf-8'))['assets']
resource = Resource()
resource.use_cpu()
assert resource.post_bundle(root / 'resource').wait().succeeded
for name in assets:
    if (name.startswith(('IconFishing', 'ButtonMiraCrown')) or name in
            ('IconAbilityStarCollect', 'IconAbilityFish', 'IconBigMapHomeFeature',
             'IconPickupFeature', 'IconSkipDialog', 'IconTalkFeature')):
        data = resource.get_node_data('MaaNikki_Asset_' + name.lower())
        assert data['recognition']['param']['method'] == 3, (name, data)
"""
        result = subprocess.run([sys.executable, "-B", "-c", code, self.directory.name],
                                cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)

    def test_map_maximum_requires_gold_segment_at_right_end_not_gray_track(self):
        from recognition.ui_template import UITemplateRecognition
        param = json.loads((ROOT / "resource/pipeline/navigation.json").read_text(encoding="utf-8"))[
            "MaaNikki_Navigation_MapMaxScale"]["custom_recognition_param"]
        template = cv2.imread(str(ROOT / "resource/image" / param["template"]))
        height, width = template.shape[:2]
        for gold_x, matched in ((65, False), (120, False), (189, True)):
            with self.subTest(gold_x=gold_x):
                frame = np.zeros((720, 1280, 3), np.uint8)
                frame[688:688+height, 65:231] = 180  # Bright neutral track used to match after thresholding.
                frame[688:688+height, gold_x:gold_x+width] = template
                result = UITemplateRecognition().analyze(None, SimpleNamespace(
                    image=frame, custom_recognition_param=param))
                self.assertEqual(result.box is not None, matched, result.detail)

    def test_ui_template_brightness_matching_remains_available(self):
        from recognition.ui_template import UITemplateRecognition
        scene = np.zeros((30, 40, 3), np.uint8)
        scene[5:17, 8:22] = self.image[:, :, :3]
        param = {"template": "template.png", "gray_limit": [210, 255], "threshold": .99}
        with patch("recognition.ui_template.ROOT", Path(self.directory.name)), \
                patch("recognition.ui_template.prepared_template", side_effect=lambda *args, **kwargs:
                    prepared_template(self.path, **kwargs)):
            result = UITemplateRecognition().analyze(None, SimpleNamespace(
                image=scene, custom_recognition_param=param))
        self.assertIsNotNone(result.box, result.detail)
        self.assertEqual(result.detail["gray_limit"], [210, 255])

    def setUp(self):
        clear_template_cache()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "template.png"
        self.image = np.zeros((12, 14, 4), np.uint8)
        self.image[3:9, 4:11, :3] = 255
        self.image[4:8, 5:10, :3] = [0, 0, 255]
        self.image[3:9, 4:11, 3] = 255
        self.assertTrue(cv2.imwrite(str(self.path), self.image))

    def test_cached_pixels_are_immutable_and_decoded_once(self):
        with patch.object(cv2, "imread", wraps=cv2.imread) as reads:
            first = prepared_template(self.path, gray=[210, 255])
            second = prepared_template(self.path, gray=(210, 255))
            colored = prepared_template(self.path, color=([0, 200, 200], [10, 255, 255]))
        self.assertIs(first, second)
        self.assertEqual(reads.call_count, 1)
        self.assertEqual(first.image.shape, (6, 7))
        self.assertEqual(colored.image.shape, (4, 5))
        with self.assertRaises(ValueError):
            first.image[0, 0] = 0
        with self.assertRaises(ValueError):
            first.mask[0, 0] = 0

    def test_file_replacement_invalidates_cache(self):
        first = prepared_template(self.path)
        revision = self.path.stat().st_mtime_ns
        self.image[:, :, :3] = 12
        cv2.imwrite(str(self.path), self.image)
        os.utime(self.path, ns=(revision+1_000_000_000, revision+1_000_000_000))
        second = prepared_template(self.path)
        self.assertIsNot(first, second)
        self.assertTrue(np.all(second.image == 12))

    def test_changed_preprocessing_never_reuses_another_variant(self):
        scaled = prepared_template(self.path, scale=.5)
        normal = prepared_template(self.path)
        grayscale = prepared_template(self.path, gray=(210, 255), grayscale=True)
        self.assertEqual(scaled.image.shape[:2], (6, 7))
        self.assertEqual(normal.image.shape[:2], (12, 14))
        self.assertIsNone(grayscale.mask)
        self.assertFalse(np.shares_memory(scaled.image, normal.image))

    def test_deleted_file_cannot_use_cached_template(self):
        prepared_template(self.path)
        self.path.unlink()
        with self.assertRaises(OSError):
            prepared_template(self.path)

    def test_prepared_cache_has_a_fixed_size_for_long_running_tasks(self):
        from vision import _prepare, _decode
        for level in range(1, 200):
            prepared_template(self.path, gray=(level, 255))
        self.assertEqual(_prepare.cache_info().currsize, 128)
        self.assertEqual(_decode.cache_info().currsize, 1)

    def test_cached_asset_retains_masked_match_location_and_count(self):
        rt = runtime()
        rt.ui.resource = Path(self.directory.name)
        image_dir = rt.ui.resource / "image"
        image_dir.mkdir()
        self.path.replace(image_dir / "template.png")
        scene = np.zeros((45, 70, 3), np.uint8)
        scene[5:17, 8:22] = self.image[:, :, :3]
        scene[24:36, 43:57] = self.image[:, :, :3]
        data = {"assets": {"IconTest": {"template": "template.png", "roi": None}}}
        with patch("daily.ui.catalog", return_value=data):
            boxes = rt.ui.asset_boxes("IconTest", image=scene, gray=(210, 255), count=2, threshold=.99)
            repeated = rt.ui.asset_boxes("IconTest", image=scene, gray=(210, 255), count=2, threshold=.99)
        self.assertEqual({tuple(box) for box in boxes}, {(12, 8, 7, 6), (47, 27, 7, 6)})
        self.assertEqual(repeated, boxes)


class AssistTests(unittest.TestCase):
    def simulate(self, features, enabled=None, duration=.8, foreground=None, mouse=False, input_ok=True,
                 window=True):
        rt = runtime(Exploration)
        enabled = enabled or {name: True for name in ("Pickup", "Clear", "Dialogue")}
        rt.context.get_node_data.side_effect = lambda name: {
            "action": {"param": {"custom_action_param": {
                "value": enabled[name.removeprefix("MaaNikki_Assist_")]}}}
        } if name.startswith("MaaNikki_Assist_") else {}
        clock = SimpleNamespace(now=0.0)
        captures, checks, inputs = [], [], []
        guard = SimpleNamespace(hwnd=1, user32=SimpleNamespace(IsWindow=Mock(return_value=window),
            GetCursorPos=Mock(return_value=True), ScreenToClient=Mock(return_value=True),
            GetClientRect=Mock(return_value=True)), release=Mock())
        guard.foreground = lambda: True if foreground is None else foreground(clock.now)
        rt.log = Mock()
        rt.ui.roi = Mock(return_value=[0, 0, 24, 16])

        def pause(seconds):
            rt.invalidate_frame()
            clock.now += seconds
            if clock.now >= duration:
                rt.context.tasker.stopping = True
            return not rt.stopped

        def capture():
            clock.now += .01
            if clock.now >= duration:
                rt.context.tasker.stopping = True
                return None
            captures.append(len(captures)+1)
            return np.full((16, 24, 3), captures[-1], np.uint8)

        def asset(name, image=None, **kwargs):
            self.assertIsNotNone(image)
            checks.append((name, clock.now, int(image[0, 0, 0])))
            return features(name, int(image[0, 0, 0]), clock.now)

        def submit(*args, **kwargs):
            inputs.append((clock.now, captures[-1], args, kwargs))
            return SimpleNamespace(wait=lambda: SimpleNamespace(succeeded=input_ok))

        rt.pause, rt.capture, rt.ui.asset = pause, Mock(side_effect=capture), Mock(side_effect=asset)
        rt.controller.post_click_key, rt.controller.post_click = Mock(side_effect=submit), Mock(side_effect=submit)
        guard.click_mouse = Mock(side_effect=lambda button: submit(button).wait().succeeded)
        if mouse:
            rt.game_keys.get = Mock(return_value=SimpleNamespace(kind="mouse", code=1))
            def position(point):
                point._obj.x, point._obj.y = 500, 250
                return True
            def rectangle(hwnd, rect):
                rect._obj.right, rect._obj.bottom = 1000, 500
                return True
            guard.user32.GetCursorPos.side_effect = position
            guard.user32.GetClientRect.side_effect = rectangle
        with patch("daily.exploration.ForegroundInput", return_value=guard), \
                patch("daily.exploration.time.monotonic", side_effect=lambda: clock.now):
            self.assertTrue(rt.run_assist())
        guard.release.assert_called_once()
        return rt, captures, checks, inputs

    def test_no_match_shares_each_due_decision_frame(self):
        rt, captures, checks, inputs = self.simulate(lambda *args: False)
        self.assertEqual([check[2] for check in checks[:3]], [1, 1, 1])
        self.assertEqual([check[0] for check in checks[:3]], ["IconSkip", "IconSkipDialog", "IconPickupFeature"])
        self.assertEqual(rt.capture.call_count, len(captures))
        self.assertFalse(inputs)

    def test_continuous_pickup_cannot_starve_clear_or_dialogue_checks(self):
        _, _, checks, inputs = self.simulate(lambda name, *args: name == "IconPickupFeature", duration=1.2)
        for icon, maximum_gap in (("IconSkip", .35), ("IconSkipDialog", .55)):
            times = [time for name, time, _ in checks if name == icon]
            self.assertGreaterEqual(len(times), 3)
            self.assertLessEqual(max(b-a for a, b in zip(times, times[1:])), maximum_gap)
        self.assertGreater(len(inputs), 15)
        self.assertEqual(len({item[1] for item in inputs}), len(inputs))

    def test_clear_is_submitted_once_until_its_feature_disappears(self):
        _, _, checks, inputs = self.simulate(lambda *args: True)
        self.assertEqual(len(inputs), 1)
        self.assertTrue(all(name == "IconSkip" for name, _, _ in checks))

    def test_dialogue_keeps_cooldown_and_new_frame_after_every_input(self):
        _, _, checks, inputs = self.simulate(lambda name, *args: name == "IconSkipDialog", duration=1.1)
        self.assertGreaterEqual(len(inputs), 3)
        self.assertTrue(all(b[0]-a[0] >= .25 for a, b in zip(inputs, inputs[1:])))
        self.assertEqual(len({item[1] for item in inputs}), len(inputs))

    def test_failed_input_does_not_submit_another_action_on_same_frame(self):
        _, _, _, inputs = self.simulate(lambda *args: True, input_ok=False)
        self.assertEqual(len({item[1] for item in inputs}), len(inputs))

    def test_mouse_binding_clicks_in_place_without_extra_capture(self):
        rt, _, checks, inputs = self.simulate(lambda name, *args: name == "IconPickupFeature", mouse=True)
        self.assertTrue(inputs)
        self.assertEqual(inputs[0][2], (1,))
        self.assertEqual(inputs[0][3], {})
        self.assertEqual(inputs[0][1], 1)
        rt.controller.post_click_key.assert_not_called()
        rt.controller.post_click.assert_not_called()

    def test_background_never_captures_or_sends_input(self):
        rt, _, _, inputs = self.simulate(lambda *args: True, foreground=lambda now: False)
        rt.capture.assert_not_called()
        self.assertFalse(inputs)

    def test_losing_focus_during_recognition_prevents_input(self):
        focus = {"active": True}
        def features(*args):
            focus["active"] = False
            return True
        _, _, _, inputs = self.simulate(features, foreground=lambda now: focus["active"])
        self.assertFalse(inputs)

    def test_closed_window_never_captures_or_sends_input(self):
        rt, _, _, inputs = self.simulate(lambda *args: True, window=False)
        rt.capture.assert_not_called()
        self.assertFalse(inputs)

    def test_clear_disappearance_releases_other_helpers_on_a_new_frame(self):
        _, _, _, inputs = self.simulate(lambda name, frame, now:
            frame < 4 if name == "IconSkip" else True, duration=1.3)
        self.assertGreaterEqual(len(inputs), 2)
        self.assertEqual(inputs[0][1], 1)
        self.assertEqual(inputs[1][1], 5)

    def test_all_disabled_helpers_do_not_start_input_guard(self):
        rt = runtime(Exploration)
        rt.log = Mock()
        rt.context.get_node_data.return_value = {"action": {"param": {"custom_action_param": {"value": False}}}}
        with patch("daily.exploration.ForegroundInput") as guard:
            self.assertFalse(rt.run_assist())
        guard.assert_not_called()

    def test_disabled_helpers_are_never_recognized(self):
        _, _, checks, _ = self.simulate(lambda *args: False,
            enabled={"Pickup": True, "Clear": False, "Dialogue": False})
        self.assertTrue(all(name == "IconPickupFeature" for name, _, _ in checks))

    def test_schedule_uses_elapsed_time_even_after_slow_capture(self):
        schedule = AssistSchedule({name: True for name in ("Pickup", "Clear", "Dialogue")})
        for name in schedule.due(10):
            schedule.checked(name, 10)
        self.assertEqual(schedule.due(10.15), ["Pickup"])
        self.assertEqual(schedule.due(11), ["Clear", "Dialogue", "Pickup"])


class PerformanceTests(unittest.TestCase):
    def test_nested_actions_share_one_profile_and_restore_context(self):
        with patch.object(Performance, "save") as save:
            with action_profile("outer") as outer:
                with action_profile("inner") as inner:
                    self.assertIsNone(inner)
                    count("nested")
                self.assertEqual(outer.counters["nested"], 1)
            self.assertIsNone(_ACTIVE.get())
        save.assert_called_once()

    def test_histogram_storage_is_bounded_and_serializable(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Performance("offline", directory)
            token = _ACTIVE.set(profile)
            try:
                for _ in range(10_000):
                    profile.record("capture", .012)
                count("capture.reused", 3)
                with measure("recognition"):
                    pass
                profile.succeeded = True
                profile.save()
            finally:
                _ACTIVE.reset(token)
            saved = json.loads((Path(directory) / profile.filename).read_text(encoding="utf-8"))
            self.assertEqual(saved["stages"]["capture"]["count"], 10_000)
            self.assertEqual(saved["stages"]["capture"]["mean_ms"], 12)
            self.assertEqual(saved["stages"]["capture"]["p95_upper_ms"], 20)
            self.assertEqual(saved["counters"]["capture.reused"], 3)
            self.assertEqual(len(profile.stages["capture"]["buckets"]), 15)

    def test_logging_failure_does_not_interrupt_action_or_retry_every_frame(self):
        profile = Performance("offline", ROOT / ".build/unused-performance")
        token = _ACTIVE.set(profile)
        try:
            with patch.object(Path, "mkdir", side_effect=OSError("unwritable")), \
                    patch("performance.time.perf_counter", return_value=1000):
                profile.last_saved = 0
                checkpoint()
                checkpoint()
                self.assertEqual(profile.last_saved, 1000)
        finally:
            _ACTIVE.reset(token)


if __name__ == "__main__":
    unittest.main()
