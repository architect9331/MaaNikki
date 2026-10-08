"""Offline gameplay regressions: python/python.exe -B scripts/test_gameplay.py."""
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
os.environ.setdefault("MAAFW_BINARY_PATH", str(ROOT / "maafw"))
from action.daily_tasks import DailyRun
from daily.starsea import Starsea, CRYSTAL_CENTERS, marker_location, select_crystal_route, crystal_pan
from daily.crown import Crown, parse_progress
from daily.runtime import Executors
from action.claim_daily_rewards import ClaimDailyRewardsAction


class Clock:
    def __init__(self):
        self.now = 0.0

    def pause(self, seconds):
        self.now += seconds
        return True


class GameplayTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=ROOT / ".build")
        self.addCleanup(directory.cleanup)
        root = patch("action.daily_tasks.ROOT", Path(directory.name))
        root.start()
        self.addCleanup(root.stop)

    def runtime(self, clock):
        rt = Mock()
        rt.stopped = False
        rt.pause.side_effect = clock.pause
        rt.capture.return_value = np.zeros((720, 1280, 3), dtype=np.uint8)
        rt.recognize.return_value = SimpleNamespace(hit=True)
        rt.context.get_node_data.return_value = {"roi": [1178, 537, 40, 20], "recognition": "OCR"}
        return rt

    def score_runtime(self, clock, values):
        rt = self.runtime(clock)
        rt.prefix, rt.mode, rt.score_reads = "Xinghai", "xinghai", []
        rt.recognize.side_effect = [SimpleNamespace(hit=value is not None,
            best_result=SimpleNamespace(text="" if value is None else str(value))) for value in values]
        return rt

    def test_score_accepts_two_equal_readings_with_only_point_four_pause(self):
        clock = Clock()
        rt = self.score_runtime(clock, [500, 500])
        with patch("daily.score.score_image", return_value=None):
            self.assertEqual(DailyRun.score(rt), 500)
        self.assertEqual(rt.capture.call_count, 2)
        rt.pause.assert_called_once_with(.4)
        self.assertAlmostEqual(clock.now, .4)

    def test_score_change_requires_another_confirming_read(self):
        clock = Clock()
        rt = self.score_runtime(clock, [100, 300, 300])
        with patch("daily.score.score_image", return_value=None):
            self.assertEqual(DailyRun.score(rt), 300)
        self.assertEqual(rt.capture.call_count, 3)
        self.assertAlmostEqual(clock.now, .8)

    def test_score_does_not_extend_wait_when_three_reads_disagree(self):
        clock = Clock()
        rt = self.score_runtime(clock, [100, 200, 300])
        with patch("daily.score.score_image", return_value=None):
            self.assertIsNone(DailyRun.score(rt))
        self.assertEqual(rt.capture.call_count, 3)
        self.assertAlmostEqual(clock.now, .8)
        self.assertIn("failure_image", rt.score_reads[-1])

    def test_unreadable_score_is_not_zero(self):
        clock = Clock()
        rt = self.score_runtime(clock, [None, None, None])
        with patch("daily.score.score_image", return_value=None):
            self.assertIsNone(DailyRun.score(rt))
        self.assertEqual(rt.capture.call_count, 3)

    def test_score_stops_when_pause_is_cancelled(self):
        clock = Clock()
        rt = self.score_runtime(clock, [500])
        rt.pause.return_value = False
        rt.pause.side_effect = None
        with patch("daily.score.score_image", return_value=None):
            self.assertIsNone(DailyRun.score(rt))
        self.assertEqual(rt.capture.call_count, 1)

    def test_standalone_claim_returns_without_animation_wait_when_no_button(self):
        rt = Mock(stopped=False)
        rt.capture.return_value = np.zeros((720, 1280, 3), dtype=np.uint8)
        with patch("action.claim_daily_rewards.Executors", return_value=rt):
            self.assertTrue(ClaimDailyRewardsAction().run(None,
                SimpleNamespace(node_name="MaaNikki_ClaimXinghaiRewards", custom_action_param={})))
        rt.capture.assert_called_once()
        rt.action.assert_not_called()
        rt.pause.assert_not_called()

    def test_all_crystal_centers_select_their_own_routes(self):
        for n, (x, y) in enumerate(CRYSTAL_CENTERS, 1):
            self.assertEqual(select_crystal_route((x/2, y/2))[0]["route"], f"xinghai_crystal_{n}")

    def test_ambiguous_neighboring_islands_are_rejected(self):
        a, b = CRYSTAL_CENTERS[6:]
        location = ((a[0]+b[0])/4, (a[1]+b[1])/4)
        self.assertIsNone(select_crystal_route(location)[0])
        self.assertIsNone(select_crystal_route((1223, 793))[0])

    def test_marker_projection_preserves_pixel_offsets(self):
        boxes = [[810, 346, 40, 40], [877, 319, 40, 40], [700, 360, 40, 40]]
        location = marker_location((1120.4, 796.2), boxes, .465)
        self.assertAlmostEqual(location[0], 1120.4+((830+897+720)/3-640)*.465)
        self.assertAlmostEqual(location[1], 796.2+((366+339+380)/3-360)*.465)

    def test_crystal_snapshot_is_retained_without_recentering_markers(self):
        clock = Clock()
        rt = self.runtime(clock)
        star = Starsea(rt, ROOT / "resource")
        star.crystal_view = (1164.5, 767)
        frame = rt.capture.return_value
        teleporter = Mock()
        teleporter.map_pose.return_value = ((1164.5, 767, .8, .05), 1)
        teleporter.locator.spec.bigmap_scale = .465
        star.boxes = Mock(return_value=[[630, 350, 20, 20]]*3)
        with patch("daily.starsea.time.monotonic", side_effect=lambda: clock.now):
            self.assertEqual(star.locate_crystals(teleporter), (1164.5, 767))
        self.assertIs(teleporter.last_frame, frame)
        teleporter.map_pose.assert_called_once_with(frame)
        teleporter.zoom.assert_not_called()
        teleporter.locate_map.assert_not_called()
        rt.action.assert_not_called()
        rt.ui.stable.assert_not_called()

    def test_map_recenters_on_player_then_uses_one_bounded_large_pan(self):
        clock = Clock()
        rt = self.runtime(clock)
        star = Starsea(rt, ROOT / "resource")
        star.crystal_view = (1164.5, 767)
        teleporter = Mock()
        teleporter.locator.spec.bigmap_scale = .465
        teleporter.map_pose.side_effect = [((832, 1015, .8, .05), 1), ((1164.5, 767, .8, .05), 1)]
        star.boxes = Mock(side_effect=[[], [[630, 350, 20, 20]]*3])
        with patch("daily.starsea.time.monotonic", side_effect=lambda: clock.now):
            self.assertEqual(star.locate_crystals(teleporter), (1164.5, 767))
        rt.action.assert_called_once()
        self.assertEqual(rt.action.call_args.kwargs["duration"], 250)
        rt.ui.stable.assert_not_called()

    def test_partial_markers_do_not_use_map_center_as_location(self):
        clock = Clock()
        rt = self.runtime(clock)
        star = Starsea(rt, ROOT / "resource")
        star.crystal_view = (1164.5, 767)
        teleporter = Mock()
        teleporter.map_pose.return_value = ((1164.5, 767, .8, .05), 1)
        teleporter.locator.spec.bigmap_scale = .465
        star.boxes = Mock(return_value=[[810, 346, 40, 40], [877, 319, 40, 40]])
        with patch("daily.starsea.time.monotonic", side_effect=lambda: clock.now):
            self.assertIsNone(star.locate_crystals(teleporter))

    def test_crystal_matching_excludes_right_panel_and_uses_snapshot(self):
        rt = Mock()
        star = Starsea(rt, ROOT / "resource")
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        star.boxes(frame)
        self.assertIs(rt.ui.asset_boxes.call_args.kwargs["image"], frame)
        roi = rt.ui.asset_boxes.call_args.kwargs["roi"]
        self.assertLessEqual(roi[0]+roi[2], 1000)

    def test_pan_stays_in_terrain_for_distant_target(self):
        for dx, dy in ((1500, 400), (-1500, -900), (0, 2000), (10, 0)):
            begin, end = crystal_pan(dx, dy)
            for x, y in (begin, end):
                self.assertTrue(90 <= x <= 970)
                self.assertTrue(100 <= y <= 640)
            self.assertLessEqual((end[0]-begin[0])*dx, 0)
            self.assertLessEqual((end[1]-begin[1])*dy, 0)

    def placement_runtime(self, clock):
        rt = self.runtime(clock)
        rt.select_item.return_value = True
        rt.ui.wait_page.return_value = True
        rt.observe.side_effect = lambda **kwargs: nullcontext()
        rt.placement_ready.return_value = True
        rt.text.return_value = ""
        rt.hit.return_value = False
        return rt

    def test_persistent_category_icon_does_not_abort_placement(self):
        clock = Clock()
        rt = self.placement_runtime(clock)
        rt.hit.side_effect = lambda node: node in ("MaaNikki_Daily_ItemCategory", "MaaNikki_MainDetected")
        rt.placement_ready.return_value = False
        with patch("daily.runtime.foreground_inputs", return_value=True), \
                patch("navigation.controller.relative_camera", return_value=True), \
                patch("daily.runtime.time.monotonic", side_effect=lambda: clock.now):
            self.assertTrue(Executors.place(rt))
        rt.select_item.assert_called_once()
        self.assertEqual(rt.key.call_count, 1)

    def test_absent_cannot_place_message_does_not_mean_success(self):
        clock = Clock()
        rt = self.placement_runtime(clock)
        with patch("daily.runtime.foreground_inputs", return_value=True), \
                patch("navigation.controller.relative_camera", return_value=True), \
                patch("daily.runtime.time.monotonic", side_effect=lambda: clock.now):
            self.assertFalse(Executors.place(rt))
        rt.key.assert_called_once_with("place", .3)
        rt.save_placement_failure.assert_called_once_with("placement_unconfirmed")

    def test_no_placement_control_never_submits_world_click(self):
        clock = Clock()
        rt = self.placement_runtime(clock)
        rt.ui.wait_page.return_value = False
        with patch("daily.runtime.foreground_inputs", return_value=True):
            self.assertFalse(Executors.place(rt))
        rt.key.assert_not_called()

    def test_new_recover_control_confirms_object_placement(self):
        clock = Clock()
        rt = self.placement_runtime(clock)
        rt.text.side_effect = ["", "", "回收", "", "回收"]
        with patch("daily.runtime.foreground_inputs", return_value=True), \
                patch("navigation.controller.relative_camera", return_value=True), \
                patch("daily.runtime.time.monotonic", side_effect=lambda: clock.now):
            self.assertTrue(Executors.place(rt))
        rt.key.assert_called_once_with("place", .3)

    def test_persistent_cannot_place_toast_does_not_trigger_repeated_clicks(self):
        clock = Clock()
        rt = self.placement_runtime(clock)
        rt.hit.side_effect = lambda node: node == "MaaNikki_Daily_CantPlace"
        with patch("daily.runtime.foreground_inputs", return_value=True), \
                patch("navigation.controller.relative_camera", return_value=True), \
                patch("daily.runtime.time.monotonic", side_effect=lambda: clock.now):
            self.assertFalse(Executors.place(rt))
        rt.key.assert_called_once_with("place", .3)

    def test_crown_decorative_star_is_ignored(self):
        self.assertEqual(parse_progress("*21/24"), (21, 24))
        self.assertEqual(parse_progress("★ 21 ／ 24"), (21, 24))
        for raw in ("", "25/24", "21/0", "81/90 21/24"):
            self.assertIsNone(parse_progress(raw))

    def test_crown_progress_retries_and_requires_repeatable_value(self):
        rt = Mock(stopped=False)
        rt.text.side_effect = ["", "*21/24", "*21/24"]
        rt.require.side_effect = lambda result, message: result
        self.assertTrue(Crown.run_crown(rt))
        self.assertEqual(rt.capture.call_count, 3)
        rt.challenge.assert_not_called()


if __name__ == "__main__":
    unittest.main()
