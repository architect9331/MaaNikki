"""Offline input regressions: python/python.exe -B scripts/test_clicks.py."""
from pathlib import Path
from types import SimpleNamespace
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
os.environ.setdefault("MAAFW_BINARY_PATH", str(ROOT / "maafw"))
from daily.gameplay import Gameplay
from daily.runtime import Executors, Runtime, parameters
from daily import run_state
from daily.ui import GameUI
from daily.startup import focus_game
from navigation.models import NavigationError


class ClickTests(unittest.TestCase):
    def test_home_and_other_routes_focus_before_guarded_operations(self):
        for executor in ("home", "plant"):
            rt = self.runtime()
            state = {"focused": False}
            rt.routes = Mock(return_value={"schema_version": 3, "id": "test", "map": "home",
                "points": [{"x": 1, "y": 2}], "teleport": {"name": "test"}})
            inputs = Mock(hwnd=1)
            inputs.foreground.side_effect = lambda: state["focused"]
            def checked(*args):
                self.assertTrue(state["focused"])
                return True
            def focused(*args):
                state["focused"] = True
                inputs.check()
            actions = Mock(count=0, quota=None)
            actions.complete.return_value = True
            actions.fishing.configure.side_effect = checked
            card = SimpleNamespace(rule=SimpleNamespace(executor=executor))
            with tempfile.TemporaryDirectory() as directory, \
                    patch("daily.runtime.ROOT", Path(directory)), \
                    patch("daily.runtime.foreground_inputs", return_value=True), \
                    patch("navigation.models.maps", return_value={"home": Mock()}), \
                    patch("navigation.vision.Locator"), \
                    patch("navigation.controller.ForegroundInput", return_value=inputs), \
                    patch("daily.startup.focus_game", side_effect=focused) as focus, \
                    patch("navigation.daily_route.RouteActions", return_value=actions), \
                    patch("navigation.daily_route.preflight"), \
                    patch("navigation.teleport.Teleporter") as teleport, \
                    patch("navigation.engine.Navigator") as navigator:
                inputs.check.side_effect = checked
                teleport.return_value.prepare.side_effect = checked
                navigator.return_value.follow.side_effect = checked
                self.assertTrue(rt.navigate("test", "home", card))
                focus.assert_called_once_with(rt, inputs)
                inputs.release.assert_called_once()
                self.assertIsNone(rt.navigation_inputs)

    def test_nested_route_does_not_refocus_after_parent_loses_foreground(self):
        rt = self.runtime()
        parent, inputs = Mock(), Mock(hwnd=1)
        rt.navigation_inputs = parent
        parent.check.side_effect = NavigationError("游戏已离开前台")
        inputs.foreground.return_value = False
        rt.routes = Mock(return_value={"schema_version": 2, "id": "test", "map": "home",
                                      "points": [{"x": 1, "y": 2}]})
        with tempfile.TemporaryDirectory() as directory, \
                patch("daily.runtime.ROOT", Path(directory)), \
                patch("daily.runtime.foreground_inputs", return_value=True), \
                patch("navigation.models.maps", return_value={"home": Mock()}), \
                patch("navigation.vision.Locator"), \
                patch("navigation.controller.ForegroundInput", return_value=inputs), \
                patch("daily.startup.focus_game") as focus:
            self.assertFalse(rt.navigate("test"))
            focus.assert_not_called()
            self.assertIs(rt.navigation_inputs, parent)
            self.assertTrue(rt.navigation_halted)

    def test_item_selection_finishes_settings_before_using_or_recalling_item(self):
        for outcome, expected_clicks in (("preview", 1), ("recall", 2), ("lantern", 1)):
            with self.subTest(outcome=outcome):
                rt = self.runtime()
                state = {"page": "world", "item_clicks": 0}
                rt.main = Mock(return_value=True)
                rt.hit = Mock(side_effect=lambda node: {
                    "MaaNikki_MainDetected": state["page"] in {"world", "preview"},
                    "MaaNikki_Daily_ItemSetting": state["page"] == "wheel",
                    "MaaNikki_Daily_ItemFinish": state["page"] == "settings",
                    "MaaNikki_Daily_ItemCategory": state["page"] in {
                        "settings", "browser", "preview", "lantern"},
                    "MaaNikki_Daily_LanternConfirm": state["page"] == "lantern",
                }.get(node, False))
                rt.ui.wait_page = Mock(side_effect=lambda page, **kwargs: rt.ui.page_matches(page))
                rt.ui.roi = Mock(return_value=[1052, 134, 60, 60])
                rt.placement_ready = Mock(side_effect=lambda: state["page"] == "preview")

                def hold(*args):
                    state["page"] = "wheel"
                    return True

                def click_template(node, **kwargs):
                    if node == "MaaNikki_Daily_ItemSetting":
                        state["page"] = "settings"
                    elif node == "MaaNikki_Daily_ItemFinish":
                        state["page"] = "browser"
                    return True

                def choose(*args, **kwargs):
                    self.assertEqual(state["page"], "browser")
                    state["item_clicks"] += 1
                    state["page"] = ("browser" if outcome == "recall" and state["item_clicks"] == 1
                                     else "lantern" if outcome == "lantern" else "preview")
                    return True

                rt.hold = Mock(side_effect=hold)
                rt.click_template = Mock(side_effect=click_template)
                rt.action = Mock(side_effect=choose)
                self.assertTrue(Executors.select_item(rt))
                self.assertEqual(state["item_clicks"], expected_clicks)
                finish_calls = [call for call in rt.click_template.call_args_list
                                if call.args[0] == "MaaNikki_Daily_ItemFinish"]
                self.assertEqual(len(finish_calls), 1)

    def test_main_confirmation_never_moves_cursor(self):
        rt = self.runtime()
        rt.hit = Mock(return_value=True)
        self.assertTrue(rt.ui.wait_page("MaaNikki_MainDetected"))
        self.assertEqual(rt.hit.call_count, 2)
        rt.ui.unhover.assert_not_called()

    def test_scene_transition_suppresses_unhover_and_restores_ui_behavior(self):
        rt = self.runtime()
        rt.ui.unhover = lambda: GameUI.unhover(rt.ui)
        rt.hit = Mock(return_value=False)
        rt.controller.post_touch_move = Mock()
        rt.controller.post_touch_move.return_value.wait.return_value.succeeded = True
        with rt.ui.preserve_cursor():
            self.assertTrue(rt.action("Click", target=[1082, 164]))
            rt.ui.wait_page(lambda: True)
        rt.controller.post_touch_move.assert_not_called()
        self.assertTrue(rt.ui.unhover())
        rt.controller.post_touch_move.assert_called_once_with(0, 0)

    def test_unhover_moves_cursor_only_in_ui_and_never_after_stop(self):
        rt = self.runtime()
        rt.controller.post_touch_move = Mock()
        rt.controller.post_touch_move.return_value.wait.return_value.succeeded = True
        rt.hit = Mock(return_value=True)
        self.assertTrue(GameUI.unhover(rt.ui))
        rt.controller.post_touch_move.assert_not_called()
        rt.hit.return_value = False
        self.assertTrue(GameUI.unhover(rt.ui))
        rt.controller.post_touch_move.assert_called_once_with(0, 0)
        rt.context.tasker.stopping = True
        self.assertFalse(GameUI.unhover(rt.ui))
        self.assertEqual(rt.controller.post_touch_move.call_count, 1)

    def runtime(self):
        context = SimpleNamespace(
            tasker=SimpleNamespace(stopping=False, controller=SimpleNamespace()),
            get_node_data=Mock(return_value={"recognition": {"param": {"roi": [820, 420, 120, 90]}}}),
            run_action=Mock(return_value=SimpleNamespace(success=True)),
        )
        rt = Runtime(context)
        rt.pause = Mock(return_value=True)
        rt.log = Mock()
        rt.ui.unhover = Mock(return_value=True)
        rt.ui.report_entry = Mock()
        rt.ui.entry_failure_frame = Mock(return_value={})
        return rt

    def actions(self, rt):
        return [call.kwargs["pipeline_override"]["MaaNikki_Daily_Action"]
                for call in rt.context.run_action.call_args_list]

    def test_rectangle_uses_center_after_hover(self):
        rt = self.runtime()
        self.assertTrue(rt.action("Click", target=[165, 307, 53, 53]))
        self.assertEqual(self.actions(rt), [
            {"action": "TouchMove", "target": [192, 334]},
            {"action": "Click", "target": [192, 334]},
        ])
        self.assertEqual([call.args[0] for call in rt.pause.call_args_list], [.3, .1])

    def test_point_click_and_asset_use_same_path(self):
        rt = self.runtime()
        rt.ui.asset = Mock(return_value=[10, 20, 40, 60])
        self.assertTrue(rt.ui.click_asset("button"))
        self.assertEqual(self.actions(rt)[1]["target"], [30, 50])

    def test_no_click_when_hover_fails(self):
        rt = self.runtime()
        rt.context.run_action.return_value.success = False
        self.assertFalse(rt.action("Click", target=[10, 20]))
        self.assertEqual(len(self.actions(rt)), 1)

    def test_stop_during_hover_prevents_click(self):
        rt = self.runtime()
        rt.pause.return_value = False
        self.assertFalse(rt.action("Click", target=[10, 20]))
        self.assertEqual(len(self.actions(rt)), 1)

    def test_gameplay_mouse_binding_keeps_original_timing(self):
        for name, button in (("place", 0), ("recover", 1), ("attack", 0), ("capture", 1)):
            with self.subTest(name=name):
                rt = self.runtime()
                inputs = Mock()
                rt.navigation_inputs = inputs
                with patch("navigation.controller.ForegroundInput") as create:
                    self.assertTrue(rt.key(name))
                create.assert_not_called()
                inputs.click_mouse.assert_called_once_with(button)
                inputs.release.assert_not_called()
                self.assertEqual(self.actions(rt), [])
                rt.pause.assert_called_once_with(.35)
                rt.ui.unhover.assert_not_called()

    def test_mouse_bound_interaction_never_uses_ui_clicks(self):
        rt = self.runtime()
        inputs = Mock()
        rt.game_keys.values = {"interact": "MouseX1"}
        rt.controller.post_touch_move = Mock()
        with patch("navigation.controller.ForegroundInput", return_value=inputs):
            self.assertTrue(rt.key("interact", .5))
        inputs.click_mouse.assert_called_once_with(3)
        inputs.release.assert_called_once()
        self.assertEqual(self.actions(rt), [])
        rt.controller.post_touch_move.assert_not_called()

    def test_stopped_mouse_action_never_creates_input_guard(self):
        rt = self.runtime()
        rt.context.tasker.stopping = True
        with patch("navigation.controller.ForegroundInput") as create:
            self.assertFalse(rt.key("place"))
        create.assert_not_called()
        self.assertEqual(self.actions(rt), [])

    def test_developer_focus_never_repositions_the_cursor(self):
        rt = self.runtime()
        inputs, state = Mock(), {"foreground": False, "time": 0.0}
        inputs.foreground.side_effect = lambda: state["foreground"]
        def activate(key):
            self.assertEqual(key, 18)
            state["foreground"] = True
            return SimpleNamespace(wait=lambda: SimpleNamespace(succeeded=True))
        def tick():
            state["time"] += .1
            return state["time"]
        rt.controller.post_key_up = Mock(side_effect=activate)
        with patch("daily.startup.time.monotonic", side_effect=tick), \
                patch("daily.startup.time.sleep"):
            focus_game(rt, inputs)
        rt.controller.post_key_up.assert_called_once_with(18)
        self.assertEqual(self.actions(rt), [])
        inputs.check.assert_called_once()

    def test_enter_page_retries_unhandled_click(self):
        rt = self.runtime()
        rt.hit = Mock(side_effect=lambda node: node == "source")
        rt.ui.wait_page = Mock(side_effect=[False, True])
        click = Mock(return_value=True)
        self.assertTrue(rt.ui.enter_page(click, "destination", source="source"))
        self.assertEqual(click.call_count, 2)

    def test_existing_destination_is_not_clicked_again(self):
        rt = self.runtime()
        rt.hit = Mock(return_value=True)
        rt.ui.wait_page = Mock(return_value=True)
        click = Mock(return_value=True)
        self.assertTrue(rt.ui.enter_page(click, "destination"))
        click.assert_not_called()

    def test_unknown_page_is_not_blindly_clicked(self):
        rt = self.runtime()
        rt.hit = Mock(return_value=False)
        rt.ui.wait_page = Mock(return_value=False)
        click = Mock(return_value=True)
        self.assertFalse(rt.ui.enter_page(click, "destination", source="source"))
        click.assert_not_called()

    def test_page_retry_is_bounded(self):
        rt = self.runtime()
        rt.hit = Mock(side_effect=lambda node: node == "source")
        rt.ui.wait_page = Mock(return_value=False)
        click = Mock(return_value=True)
        self.assertFalse(rt.ui.enter_page(click, "destination", source="source"))
        self.assertEqual(click.call_count, 3)

    def test_page_wait_needs_consecutive_features_not_static_animation(self):
        rt = self.runtime()
        rt.hit = Mock(side_effect=[True, False, True, True])
        rt.ui.stable = Mock(return_value=False)
        self.assertTrue(rt.ui.wait_page("destination"))
        self.assertEqual(rt.hit.call_count, 4)
        rt.ui.stable.assert_not_called()

    def test_optional_stability_only_checks_feature_roi(self):
        rt = self.runtime()
        rt.hit = Mock(return_value=True)
        rt.ui.stable = Mock(return_value=True)
        self.assertTrue(rt.ui.wait_page("destination", stable=True))
        self.assertEqual(rt.ui.stable.call_args.kwargs["roi"], [820, 420, 120, 90])

    def test_entry_recovers_unknown_page_before_retrying(self):
        rt = self.runtime()
        state = {"page": "source"}
        rt.hit = Mock(side_effect=lambda node: node == state["page"])
        rt.ui.wait_page = Mock(side_effect=[False, False, True])
        def submit():
            state["page"] = "unknown"
            return True
        def restore():
            state["page"] = "source"
            return True
        click, recover = Mock(side_effect=submit), Mock(side_effect=restore)
        self.assertTrue(rt.ui.enter_page(click, "destination", source="source", recover=recover))
        self.assertEqual(click.call_count, 2)
        recover.assert_called_once()
        events = rt.ui.report_entry.call_args.args[2]
        self.assertTrue(any(e["type"] == "recovery" and e["success"] for e in events))
        self.assertEqual(events[-1]["type"], "arrived")

    def test_late_arrival_does_not_trigger_escape_or_second_click(self):
        rt = self.runtime()
        state = {"page": "source"}
        rt.hit = Mock(side_effect=lambda node: node == state["page"])
        rt.ui.wait_page = Mock(side_effect=[False, True])
        def submit():
            state["page"] = "loading"
            return True
        click, recover = Mock(side_effect=submit), Mock(return_value=True)
        self.assertTrue(rt.ui.enter_page(click, "destination", source="source", recover=recover))
        click.assert_called_once()
        recover.assert_not_called()

    def test_failed_recovery_is_bounded_without_blind_inputs(self):
        rt = self.runtime()
        rt.hit = Mock(return_value=False)
        rt.ui.wait_page = Mock(return_value=False)
        click, recover = Mock(return_value=True), Mock(return_value=False)
        self.assertFalse(rt.ui.enter_page(click, "destination", source="source", recover=recover))
        self.assertEqual(recover.call_count, 3)
        click.assert_not_called()

    def test_unclassified_input_is_not_repeated(self):
        rt = self.runtime()
        rt.hit = Mock(return_value=False)
        rt.ui.wait_page = Mock(return_value=False)
        click = Mock(return_value=True)
        self.assertFalse(rt.ui.enter_page(click, "destination"))
        click.assert_called_once()

    def test_stopping_prevents_recovery_and_more_input(self):
        rt = self.runtime()
        rt.hit = Mock(side_effect=lambda node: node == "source")
        def wait(*args, **kwargs):
            rt.context.tasker.stopping = True
            return False
        rt.ui.wait_page = Mock(side_effect=wait)
        click, recover = Mock(return_value=True), Mock(return_value=True)
        self.assertFalse(rt.ui.enter_page(click, "destination", source="source", recover=recover))
        click.assert_called_once()
        recover.assert_not_called()

    def test_stop_during_late_wait_prevents_recovery(self):
        rt = self.runtime()
        rt.hit = Mock(return_value=False)
        def wait(*args, **kwargs):
            rt.context.tasker.stopping = True
            return False
        rt.ui.wait_page = Mock(side_effect=wait)
        click, recover = Mock(return_value=True), Mock(return_value=True)
        self.assertFalse(rt.ui.enter_page(click, "destination", source="source", recover=recover))
        recover.assert_not_called()
        click.assert_not_called()

    def test_main_recovery_does_not_require_static_background(self):
        rt = self.runtime()
        rt.hit = Mock(return_value=False)
        rt.ui.asset = Mock(return_value=None)
        rt.ui.stable = Mock(return_value=False)
        rt.ui.wait_page = Mock(return_value=True)
        rt.key = Mock(return_value=True)
        self.assertTrue(rt.main())
        rt.key.assert_called_once_with("menu", .35)
        rt.ui.stable.assert_not_called()

    def test_unconfirmed_reused_menu_falls_back_to_entrance_recovery(self):
        rt = self.runtime()
        rt.hit = Mock(return_value=True)
        rt.ui.wait_page = Mock(return_value=False)
        rt.ui.main = Mock(return_value=True)
        rt.ui.enter_page = Mock(return_value=True)
        self.assertTrue(rt.ui.menu())
        rt.ui.main.assert_called_once()
        self.assertEqual(rt.ui.enter_page.call_args.args[1], "MaaNikki_MenuReady")

    def test_main_recovery_waits_through_loading_without_escape(self):
        rt = self.runtime()
        state = {"loading": True}
        rt.hit = Mock(side_effect=lambda node: node == "MaaNikki_MainDetected" and not state["loading"])
        rt.ui.asset = Mock(side_effect=lambda name, **kwargs: name == "IconUILoading")
        def pause(seconds):
            state["loading"] = False
            return True
        rt.pause = Mock(side_effect=pause)
        rt.ui.wait_page = Mock(side_effect=lambda node, **kwargs: rt.hit(node))
        rt.key = Mock(return_value=True)
        self.assertTrue(rt.main())
        rt.key.assert_not_called()

    def test_legacy_wait_uses_shared_page_confirmation(self):
        rt = self.runtime()
        rt.ui.wait_page = Mock(return_value=True)
        self.assertTrue(rt.wait_hit("destination", 10))
        rt.ui.wait_page.assert_called_once_with("destination", seconds=10)

    def test_monthly_start_recovery_reopens_intro_without_recursive_submit(self):
        rt = self.runtime()
        source, destination = "MaaNikki_Monthly_Start", "MaaNikki_Monthly_PageReady"
        rt.context.get_node_data.side_effect = lambda node: {
            "action": {"param": {"custom_action_param": {"ready": destination, "source": source}}}
        } if node == source else {}
        state = {"page": source, "clicks": 0}
        rt.hit = Mock(side_effect=lambda node: node == state["page"])
        rt.recognize = Mock(return_value=SimpleNamespace(hit=True, box=[520, 600, 230, 100]))
        rt.ui.wait_page = Mock(side_effect=lambda page, **kwargs: rt.ui.page_matches(page))
        def action(node, pipeline_override):
            if pipeline_override[node]["action"] == "Click":
                state["clicks"] += 1
                state["page"] = "unknown" if state["clicks"] == 1 else destination
            return SimpleNamespace(success=True)
        def main():
            state["page"] = "MaaNikki_MainDetected"
            return True
        def key(*args):
            state["page"] = source
            return True
        rt.context.run_action.side_effect = action
        rt.ui.main = Mock(side_effect=main)
        rt.key = Mock(side_effect=key)
        self.assertTrue(rt.click_template(source))
        self.assertEqual(state["clicks"], 2)
        rt.key.assert_called_once_with("monthly", 0)
        rt.ui.main.assert_called_once()

    def test_page_diagnostics_include_retry_outcome_and_cropped_frame(self):
        import cv2
        import numpy as np
        rt = self.runtime()
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        frame[680:] = 255
        rt.capture = Mock(return_value=frame)
        with tempfile.TemporaryDirectory() as directory, patch("daily.settings.ROOT", Path(directory)):
            snapshot = GameUI.entry_failure_frame(rt.ui, 1)
            folder = Path(directory) / "logs/navigation"
            decoded = cv2.imdecode(np.frombuffer((folder / snapshot["image"]).read_bytes(), dtype=np.uint8),
                                   cv2.IMREAD_COLOR)
            self.assertEqual(decoded.shape, (680, 1280, 3))
            self.assertFalse(decoded.any())
            for status, events, stopped in (
                ("recovered", [{"type": "not_arrived", "attempt": 1, **snapshot},
                               {"type": "arrived", "attempt": 2}], False),
                ("failed", [{"type": "not_arrived", "attempt": 3}], False),
                ("stopped", [{"type": "not_arrived", "attempt": 1}], True),
            ):
                rt.context.tasker.stopping = stopped
                GameUI.report_entry(rt.ui, "source", "destination", events)
                path = max(folder.glob("*-page.json"))
                record = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(record["status"], status)
                self.assertEqual(record["events"], events)
                self.assertEqual(record["destination"], "destination")

    def test_transport_waits_for_detail_before_submitting_once(self):
        from navigation.teleport import Teleporter
        rt = self.runtime()
        state = {"page": "map", "reads": 0}
        rt.ui.roi = Mock(return_value=[1005, 655, 160, 35])
        rt.ui.stable = Mock(return_value=True)
        rt.ui.open_map = Mock(return_value=True)
        rt.capture = Mock(return_value=object())
        rt.text = Mock(return_value="")
        def detail_text(*args, **kwargs):
            if state["page"] == "opening_detail":
                state["reads"] += 1
                if state["reads"] >= 3:
                    state["page"] = "detail"
            return "传送" if state["page"] == "detail" else ""
        rt.ui.text = Mock(side_effect=detail_text)
        rt.hit = Mock(side_effect=lambda node:
            node == ("MaaNikki_Navigation_MapFeature" if state["page"] == "map" else
                     "MaaNikki_MainDetected" if state["page"] == "main" else "none"))
        def click(action, *, target):
            state["page"] = "opening_detail" if target == [640, 360] else "main"
            return True
        rt.action = Mock(side_effect=click)
        teleporter = Teleporter(rt, SimpleNamespace(previous=object()))
        teleporter.region = Mock()
        teleporter.zoom = Mock()
        teleporter.center = Mock(return_value=[640, 360])
        teleporter.transport({"name": "test", "x": 1, "y": 2})
        self.assertEqual([call.kwargs["target"] for call in rt.action.call_args_list],
                         [[640, 360], [1005, 655, 160, 35]])
        self.assertGreaterEqual(state["reads"], 3)
        self.assertEqual(state["page"], "main")
        teleporter.zoom.assert_called_once()

    def test_transport_candidate_waits_for_detail_and_never_scrolls_or_reclicks_marker(self):
        from navigation.teleport import Teleporter
        for detail_after in (4, None):
            with self.subTest(detail_after=detail_after):
                rt = self.runtime()
                state = {"page": "list", "reads": 0, "time": 0.0}
                button, panel = [1005, 655, 160, 33], [847, 324, 168, 168]
                rt.ui.roi = Mock(side_effect=lambda name:
                    button if name == "AreaBigMapTeleportButton" else panel)
                rt.capture = Mock(return_value=object())
                rt.ui.map_failure = Mock()
                rt.hit = Mock(return_value=False)
                def pause(seconds):
                    state["time"] += seconds
                    return True
                rt.pause = Mock(side_effect=pause)
                def text(*args, **kwargs):
                    if state["page"] == "opening_detail":
                        state["reads"] += 1
                        if detail_after and state["reads"] >= detail_after:
                            state["page"] = "detail"
                    return "传送" if state["page"] == "detail" else ""
                rt.ui.text = Mock(side_effect=text)
                def find(**kwargs):
                    self.assertFalse(kwargs["scroll"])
                    self.assertTrue(kwargs["exact"])
                    self.assertEqual(kwargs["text"], "星实之树")
                    if state["page"] != "list":
                        return False
                    if kwargs["click"]:
                        state["page"] = "opening_detail"
                        return True
                    return [852, 418, 75, 22]
                rt.ui.find = Mock(side_effect=find)
                rt.action = Mock(return_value=True)
                teleporter = Teleporter(rt, Mock())
                with patch("navigation.teleport.time.monotonic", side_effect=lambda: state["time"]):
                    teleporter.deadline = 120
                    if detail_after:
                        self.assertTrue(teleporter.confirm_transport({"name": "星实之树"}))
                        rt.action.assert_called_once_with("Click", target=button)
                        rt.ui.map_failure.assert_not_called()
                    else:
                        with self.assertRaisesRegex(NavigationError, "未找到传送按钮"):
                            teleporter.confirm_transport({"name": "星实之树"})
                        rt.action.assert_not_called()
                        rt.ui.map_failure.assert_called_once()
                self.assertEqual(sum(c.kwargs["click"] for c in rt.ui.find.call_args_list), 1)
                self.assertIn(.5, [c.args[0] for c in rt.pause.call_args_list])

    def test_transport_candidate_retries_only_visible_entry_and_rejects_locked_point(self):
        from navigation.teleport import Teleporter
        rt = self.runtime()
        state = {"clicks": 0, "time": 0.0}
        rt.ui.roi = Mock(return_value=[1005, 655, 160, 33])
        rt.capture = Mock(return_value=object())
        rt.hit = Mock(return_value=False)
        rt.ui.map_failure = Mock()
        def pause(seconds):
            state["time"] += seconds
            return True
        rt.pause = Mock(side_effect=pause)
        rt.ui.text = Mock(side_effect=lambda *args, **kwargs: "追踪" if state["clicks"] == 2 else "")
        def find(**kwargs):
            self.assertFalse(kwargs["scroll"])
            if kwargs["click"]:
                state["clicks"] += 1
            return True
        rt.ui.find = Mock(side_effect=find)
        rt.action = Mock()
        teleporter = Teleporter(rt, Mock())
        with patch("navigation.teleport.time.monotonic", side_effect=lambda: state["time"]):
            teleporter.deadline = 120
            with self.assertRaisesRegex(NavigationError, "尚未解锁"):
                teleporter.confirm_transport({"name": "星实之树"})
        self.assertEqual(state["clicks"], 2)
        rt.action.assert_not_called()

    def test_scroll_search_checks_current_view_then_top_and_each_lower_view(self):
        import numpy as np
        for start, target, reads, scrolls in (
                (1, 1, [1], []),
                (1, 0, [1, 0], [1800, 1800]),
                (1, 2, [1, 0, 1, 2], [1800, 1800, -600, -600]),
                (1, None, [1, 0, 1, 2], [1800, 1800, -600, -600, -600])):
            with self.subTest(target=target):
                rt = self.runtime()
                state = {"view": start}
                rng = np.random.default_rng(0)
                frames = [rng.integers(0, 256, (64, 64, 3), dtype=np.uint8) for _ in range(3)]
                observed, movements = [], []
                rt.capture = Mock(side_effect=lambda: frames[state["view"]])
                def recognize(*args, **kwargs):
                    observed.append(state["view"])
                    return SimpleNamespace(hit=state["view"] == target, box=[10, 10, 8, 8])
                def scroll(roi, amount):
                    movements.append(amount)
                    state["view"] = 0 if amount > 0 else min(2, state["view"]+1)
                    return True
                rt.recognize = Mock(side_effect=recognize)
                rt.ui.scroll = Mock(side_effect=scroll)
                rt.ui.click_box = Mock(return_value=True)
                self.assertEqual(rt.ui.find(roi=[4, 4, 40, 40], text="星海", exact=True),
                                 target is not None)
                self.assertEqual(observed, reads)
                self.assertEqual(movements, scrolls)
                self.assertEqual(rt.ui.click_box.call_count, int(target is not None))

    def test_region_menu_title_allows_search_when_destinations_are_offscreen(self):
        from navigation.teleport import Teleporter
        for province, wanted in (("星海", "星海"), ("伊赞之土", "巨木之森"), ("家园", "家园")):
            with self.subTest(wanted=wanted):
                rt = self.runtime()
                state = {"menu": False, "region": "微风绿野"}
                region_roi, list_roi = [1042, 59, 160, 27], [891, 69, 247, 617]
                rt.ui.roi = Mock(side_effect=lambda name:
                    region_roi if name == "AreaBigMapRegionName" else list_roi)
                rt.ui.open_map = Mock(return_value=True)
                rt.ui.asset = Mock(return_value=None)
                rt.hit = Mock(side_effect=lambda node:
                    node == "MaaNikki_Navigation_MapFeature" or
                    node == "MaaNikki_Navigation_RegionMenuReady" and state["menu"])
                rt.recognize = Mock(return_value=SimpleNamespace(hit=False, filtered_results=[]))
                def open_menu(box):
                    if box == region_roi:
                        state["menu"] = True
                    else:
                        state.update(menu=False, region=wanted)
                    return True
                def select(**kwargs):
                    self.assertTrue(state["menu"])
                    self.assertTrue(kwargs["scroll"])
                    if kwargs.get("asset"):
                        self.assertFalse(kwargs["click"])
                        return [893, 95, 33, 33]
                    if kwargs["text"] == wanted:
                        state.update(menu=False, region=wanted)
                    return True
                rt.ui.click_box = Mock(side_effect=open_menu)
                rt.ui.find = Mock(side_effect=select)
                rt.ui.text = Mock(return_value="暖暖的家")
                teleporter = Teleporter(rt, Mock())
                teleporter.check = Mock()
                teleporter.dark_text = Mock(side_effect=lambda roi:
                    state["region"] if roi == region_roi else "暖暖的家")
                teleporter.region({"province": province, "region": wanted})
                self.assertEqual(state, {"menu": False, "region": wanted})
                self.assertEqual(rt.ui.click_box.call_count, 2 if wanted == "家园" else 1)
                self.assertGreater(rt.ui.find.call_count, 0)
                rt.ui.asset.assert_not_called()
                rt.ui.report_entry.assert_not_called()

    def test_home_reuse_closes_menu_without_reselecting_even_with_custom_name(self):
        from navigation.teleport import Teleporter
        for home_name in ("暖暖的家", "我改过名字的小岛"):
            with self.subTest(home_name=home_name):
                rt = self.runtime()
                state = {"menu": False}
                rt.hit = Mock(side_effect=lambda node:
                    node == "MaaNikki_Navigation_MapFeature" or
                    node == "MaaNikki_Navigation_RegionMenuReady" and state["menu"])
                rt.ui.open_map = Mock(return_value=True)
                rt.ui.click_box = Mock(side_effect=lambda box: state.update(menu=True) or True)
                rt.ui.find = Mock(return_value=[893, 95, 33, 33])
                rt.ui.text = Mock(return_value=home_name)
                rt.click_template = Mock(side_effect=lambda *args, **kwargs: state.update(menu=False) or True)
                teleporter = Teleporter(rt, Mock())
                teleporter.check = Mock()
                teleporter.dark_text = Mock(return_value=home_name)
                teleporter.home_region([1042, 59, 160, 27], [891, 69, 247, 617])
                rt.ui.click_box.assert_called_once_with([1042, 59, 160, 27])
                rt.click_template.assert_called_once_with("MaaNikki_Navigation_RegionMenuClose",
                                                         attempts=1, wait_seconds=0, verify=False)
                self.assertFalse(state["menu"])
                self.assertEqual(teleporter.home_region_name, home_name)
                teleporter.home_region([1042, 59, 160, 27], [891, 69, 247, 617])
                self.assertEqual(rt.ui.click_box.call_count, 1)
                self.assertEqual(rt.ui.find.call_count, 1)
                self.assertEqual(rt.click_template.call_count, 1)
                # A rename invalidates the shortcut and must be verified again.
                renamed = home_name+"改名"
                teleporter.dark_text.return_value = renamed
                rt.ui.text.return_value = renamed
                teleporter.home_region([1042, 59, 160, 27], [891, 69, 247, 617])
                self.assertEqual(rt.ui.click_box.call_count, 2)
                self.assertEqual(rt.ui.find.call_count, 2)
                self.assertEqual(teleporter.home_region_name, renamed)

    def test_current_normal_region_is_not_reselected_and_open_menu_is_closed(self):
        from navigation.teleport import Teleporter
        for menu_open in (False, True):
            rt = self.runtime()
            state = {"menu": menu_open}
            rt.hit = Mock(side_effect=lambda node:
                node == "MaaNikki_Navigation_MapFeature" or
                node == "MaaNikki_Navigation_RegionMenuReady" and state["menu"])
            rt.ui.roi = Mock(return_value=[1042, 59, 160, 27])
            rt.ui.find = Mock()
            rt.ui.click_box = Mock()
            rt.click_template = Mock(side_effect=lambda *args, **kwargs: state.update(menu=False) or True)
            teleporter = Teleporter(rt, Mock())
            teleporter.check = Mock()
            teleporter.dark_text = Mock(return_value="星海")
            teleporter.region({"province": "星海", "region": "星海"})
            rt.ui.find.assert_not_called()
            rt.ui.click_box.assert_not_called()
            self.assertEqual(rt.click_template.call_count, int(menu_open))

    def test_region_close_waits_for_overlay_to_disappear_not_only_map_marker(self):
        from navigation.teleport import Teleporter
        for closes_on in (1, 2, None):
            with self.subTest(closes_on=closes_on):
                rt = self.runtime()
                state = {"menu": True, "clicks": 0}
                rt.hit = Mock(side_effect=lambda node:
                    node == "MaaNikki_Navigation_MapFeature" or
                    node == "MaaNikki_Navigation_RegionMenuReady" and state["menu"])
                def close(*args, **kwargs):
                    state["clicks"] += 1
                    if state["clicks"] == closes_on:
                        state["menu"] = False
                    return True
                rt.click_template = Mock(side_effect=close)
                rt.ui.wait_page = Mock(side_effect=lambda page, **kwargs: rt.ui.page_matches(page))
                teleporter = Teleporter(rt, Mock())
                teleporter.check = Mock()
                self.assertEqual(teleporter.close_region_menu(), closes_on is not None)
                self.assertEqual(state["clicks"], closes_on or 3)

    def test_search_can_locate_entry_without_clicking(self):
        import numpy as np
        rt = self.runtime()
        rt.capture = Mock(return_value=np.zeros((720, 1280, 3), dtype=np.uint8))
        rt.recognize = Mock(return_value=SimpleNamespace(hit=True, box=[10, 20, 8, 8]))
        rt.ui.click_box = Mock()
        rt.ui.scroll = Mock()
        self.assertEqual(rt.ui.find(roi=[0, 0, 40, 40], text="星海", click=False), [10, 20, 8, 8])
        rt.ui.click_box.assert_not_called()
        rt.ui.scroll.assert_not_called()

    def test_catalog_assets_have_pipeline_nodes_for_override_reads(self):
        nodes = {}
        for path in (ROOT / "resource/pipeline").glob("*.json"):
            nodes.update(json.loads(path.read_text(encoding="utf-8")))
        assets = json.loads((ROOT / "resource/image/game/catalog.json").read_text(encoding="utf-8"))["assets"]
        for name in assets:
            self.assertIn("MaaNikki_Asset_"+name.lower(), nodes, name)

    def test_template_retry_relocates_changed_box(self):
        rt = self.runtime()
        rt.hit = Mock(side_effect=lambda node: node == "source")
        rt.context.get_node_data.return_value = {
            "action": {"param": {"custom_action_param": {"ready": "destination", "source": "source"}}}}
        rt.recognize = Mock(side_effect=[SimpleNamespace(hit=True, box=[10, 20, 20, 20]),
                                         SimpleNamespace(hit=True, box=[40, 50, 20, 20])])
        rt.ui.wait_page = Mock(side_effect=[False, True])
        self.assertTrue(rt.click_template("entrance", wait_seconds=0))
        self.assertEqual([value["target"] for value in self.actions(rt)
                          if value["action"] == "Click"], [[20, 30], [50, 60]])

    def test_like_response_is_verified_without_toggling_twice(self):
        for confirmed in (True, False):
            rt = self.runtime()
            rt.main = Mock(return_value=True)
            rt.key = Mock(return_value=True)
            rt.wait_hit = Mock(return_value=True)
            rt.click_template = Mock(return_value=True)
            rt.ui.find_menu_entry = Mock(return_value=True)
            rt.ui.wait_page = Mock(return_value=True)
            rt.ui.enter_page = Mock(return_value=True)
            rt.ui.stable = Mock(return_value=True)
            game = Gameplay(rt)
            game.lookbook_like_count = Mock(side_effect=[178, 179] if confirmed else None,
                                           return_value=178)
            clock = iter(range(100))
            with patch("daily.gameplay.time.monotonic", side_effect=lambda: next(clock)):
                self.assertEqual(game.lookbook(), confirmed)
            self.assertEqual([c.args[0] for c in rt.click_template.call_args_list],
                             ["MaaNikki_Xinghai_SelectPhoto", "MaaNikki_Xinghai_Like"])

    def dig_runtime(self, repeat):
        rt = self.runtime()
        rt.context.get_node_data.return_value = {"enabled": repeat}
        rt.ui.find_menu_entry = Mock(return_value=True)
        rt.ui.wait_page = Mock(return_value=True)
        rt.ui.skip_reward = Mock(return_value=True)
        rt.main = Mock(return_value=True)
        rt.click_template = Mock(return_value=True)
        return rt

    def test_dig_collect_and_repeat_are_exclusive_dialog_choices(self):
        for repeat in (True, False):
            with self.subTest(repeat=repeat):
                rt = self.dig_runtime(repeat)
                choice = "MaaNikki_Dig_Again" if repeat else "MaaNikki_Dig_GatherConfirm"
                state = {"page": "dig"}
                def click(node, **kwargs):
                    if state["page"] == "dig":
                        self.assertEqual(node, "MaaNikki_Dig_Gather")
                        state["page"] = "choices"
                    else:
                        self.assertEqual(state["page"], "choices")
                        self.assertEqual(node, choice)
                        state["page"] = "dig" if repeat else "reward"
                    return True
                def dismiss(**kwargs):
                    self.assertEqual(state["page"], "reward")
                    state["page"] = "dig"
                    return True
                rt.click_template.side_effect = click
                rt.ui.skip_reward.side_effect = dismiss
                self.assertTrue(Gameplay(rt).dig())
                self.assertEqual([call.args[0] for call in rt.click_template.call_args_list],
                                 ["MaaNikki_Dig_Gather", choice])
                self.assertEqual([call.args[0] for call in rt.ui.wait_page.call_args_list],
                                 [choice, "MaaNikki_Dig_PageReady"])
                if repeat:
                    rt.ui.skip_reward.assert_not_called()
                else:
                    rt.ui.skip_reward.assert_called_once_with(node="MaaNikki_Dig_SkipReward")
                rt.main.assert_called_once()

    def test_dig_missing_configured_choice_does_not_collect_by_other_choice(self):
        for repeat in (True, False):
            with self.subTest(repeat=repeat):
                rt = self.dig_runtime(repeat)
                rt.ui.wait_page.return_value = False
                self.assertFalse(Gameplay(rt).dig())
                rt.click_template.assert_called_once_with("MaaNikki_Dig_Gather", attempts=1)
                rt.ui.skip_reward.assert_not_called()
                rt.main.assert_called_once()

    def test_dig_choice_input_failure_is_not_replayed(self):
        rt = self.dig_runtime(True)
        rt.click_template.side_effect = [True, False]
        self.assertFalse(Gameplay(rt).dig())
        self.assertEqual([call.args[0] for call in rt.click_template.call_args_list],
                         ["MaaNikki_Dig_Gather", "MaaNikki_Dig_Again"])
        self.assertEqual(rt.click_template.call_args.kwargs["attempts"], 1)
        rt.ui.skip_reward.assert_not_called()

    def test_dig_unconfirmed_reward_does_not_replay_either_choice(self):
        rt = self.dig_runtime(False)
        rt.ui.skip_reward.return_value = False
        self.assertFalse(Gameplay(rt).dig())
        self.assertEqual([call.args[0] for call in rt.click_template.call_args_list],
                         ["MaaNikki_Dig_Gather", "MaaNikki_Dig_GatherConfirm"])
        rt.main.assert_called_once()

    def test_dig_checks_page_after_dismissing_reward(self):
        rt = self.dig_runtime(False)
        rt.ui.wait_page.side_effect = [True, False]
        self.assertFalse(Gameplay(rt).dig())
        rt.ui.skip_reward.assert_called_once_with(node="MaaNikki_Dig_SkipReward")
        rt.main.assert_called_once()

    def test_dig_without_completed_reward_keeps_running_or_empty_result(self):
        for count in (0, 2):
            with self.subTest(count=count):
                rt = self.dig_runtime(True)
                rt.click_template.return_value = False
                rt.ui.roi = Mock(return_value=[1232, 122, 23, 13])
                rt.ui.text = Mock(return_value=f"{count}/4")
                self.assertEqual(Gameplay(rt).dig(), count > 0)
                rt.click_template.assert_called_once_with("MaaNikki_Dig_Gather", attempts=1)
                rt.ui.skip_reward.assert_not_called()

    def test_reward_dismissal_can_use_specific_marker(self):
        rt = self.runtime()
        rt.hit = Mock(return_value=True)
        rt.key = Mock(return_value=True)
        self.assertTrue(rt.ui.skip_reward(node="MaaNikki_Dig_SkipReward"))
        rt.hit.assert_called_once_with("MaaNikki_Dig_SkipReward")
        rt.key.assert_called_once_with("skip", .5)

    def test_pipeline_clicks_all_use_common_handler(self):
        count = 0
        for path in (ROOT / "resource/pipeline").glob("*.json"):
            for node in json.loads(path.read_text(encoding="utf-8")).values():
                self.assertNotEqual(node.get("action"), "Click", path.name)
                if node.get("custom_action") == "nikki.ui_click":
                    count += 1
                    self.assertEqual(node["action"], "Custom")
        self.assertGreaterEqual(count, 58)

    def test_configured_page_features_exist(self):
        nodes = {}
        for path in (ROOT / "resource/pipeline").glob("*.json"):
            nodes.update(json.loads(path.read_text(encoding="utf-8")))
        entrances = 0
        for name, node in nodes.items():
            config = node.get("custom_action_param") or {}
            if isinstance(config, dict) and config.get("ready"):
                entrances += 1
                self.assertIn(config["ready"], nodes, name)
                self.assertIn(config.get("source"), nodes, name)
        self.assertGreaterEqual(entrances, 10)
        for name in ("MaaNikki_ReturnMain", "MaaNikki_ReturnMainSecond", "MaaNikki_ReturnMainThird"):
            self.assertEqual(nodes[name]["custom_action"], "nikki.gameplay")
            self.assertEqual(nodes[name]["custom_action_param"], {"kind": "main"})

    def test_null_native_parameters_are_empty_objects(self):
        rt = self.runtime()
        for value in (None, "null", "{}", {}):
            rt.context.get_node_data.return_value = {
                "action": {"param": {"custom_action_param": value}}}
            self.assertEqual(parameters(rt.context, "button"), {})
        for value in ([], "[]", 123):
            rt.context.get_node_data.return_value = {
                "action": {"param": {"custom_action_param": value}}}
            with self.assertRaises(ValueError):
                parameters(rt.context, "button")

    def test_lookbook_dispatches_actual_click_with_null_parameters(self):
        rt = self.runtime()
        default = {
            "recognition": {"param": {"roi": [820, 420, 120, 90]}},
            "action": {"param": {"custom_action_param": None}}}
        rt.context.get_node_data.side_effect = lambda node: {
            "action": {"param": {"custom_action_param": {
                "ready": "MaaNikki_Xinghai_PhotoReady", "source": "MaaNikki_Xinghai_LookbookReady"}}}
        } if node == "MaaNikki_Xinghai_SelectPhoto" else default
        rt.main = Mock(return_value=True)
        rt.key = Mock(return_value=True)
        rt.wait_hit = Mock(return_value=True)
        rt.recognize = Mock(return_value=SimpleNamespace(hit=True, box=[874, 462, 26, 25]))
        rt.ui.find_menu_entry = Mock(return_value=True)
        rt.ui.wait_page = Mock(return_value=True)
        rt.ui.enter_page = Mock(return_value=True)
        rt.ui.stable = Mock(return_value=True)
        game = Gameplay(rt)
        game.lookbook_like_count = Mock(side_effect=[188, 189])
        self.assertTrue(game.lookbook())
        clicks = [value for value in self.actions(rt) if value["action"] == "Click"]
        self.assertEqual(clicks, [{"action": "Click", "target": [887, 474]}])

    def test_missing_menu_entry_never_scrolls_or_clicks(self):
        import numpy as np
        rt = self.runtime()
        rt.capture = Mock(return_value=np.zeros((720, 1280, 3), dtype=np.uint8))
        rt.recognize = Mock(return_value=SimpleNamespace(hit=False))
        rt.ui.menu = Mock(return_value=True)
        rt.ui.scroll = Mock(return_value=True)
        for node in ("MaaNikki_Dig_Open", "MaaNikki_Xinghai_OpenLookbook", "MaaNikki_Crown_Open"):
            self.assertFalse(rt.ui.find_menu_entry(node=node))
        rt.ui.scroll.assert_not_called()
        rt.context.run_action.assert_not_called()

    def test_menu_loading_does_not_start_entry_search(self):
        rt = self.runtime()
        rt.ui.menu = Mock(return_value=False)
        rt.click_template = Mock(return_value=True)
        self.assertFalse(rt.ui.find_menu_entry(node="MaaNikki_Dig_Open"))
        rt.click_template.assert_not_called()

    def test_callback_exceptions_return_failure(self):
        from registration import agent_action
        with patch("registration.AgentServer.custom_action", side_effect=lambda name: lambda cls: cls):
            @agent_action("offline.failure")
            class BrokenAction:
                def run(self, context, argv):
                    raise AttributeError("callback regression")
        context = SimpleNamespace(run_task=Mock())
        with patch("registration.traceback.print_exc") as trace:
            self.assertIs(BrokenAction().run(context, SimpleNamespace(node_name="test")), False)
        trace.assert_called_once()
        context.run_task.assert_called_once()

    def test_calendar_reuses_current_page_without_input(self):
        rt = self.runtime()
        rt.hit = Mock(return_value=True)
        rt.key = Mock(return_value=True)
        rt.ui.wait_page = Mock(return_value=True)
        rt.ui.main = Mock(return_value=True)
        self.assertTrue(rt.ui.calendar())
        rt.key.assert_not_called()
        rt.ui.main.assert_not_called()

    def test_daily_to_calendar_uses_one_back_without_world(self):
        rt = self.runtime()
        rt.hit = Mock(side_effect=lambda node: node == "MaaNikki_Zhaoxi_DailyReady")
        rt.capture = Mock(return_value=object())
        rt.recognize = Mock(side_effect=lambda node, **kwargs:
                            SimpleNamespace(hit=node == "MaaNikki_Zhaoxi_DailyReady"))
        rt.key = Mock(return_value=True)
        rt.ui.wait_page = Mock(return_value=True)
        rt.ui.main = Mock(return_value=True)
        self.assertTrue(rt.ui.calendar())
        rt.key.assert_called_once_with("menu", 0)
        rt.ui.main.assert_not_called()

    def test_realm_to_calendar_verifies_each_parent(self):
        rt = self.runtime()
        state = {"page": "MaaNikki_Bless_PageReady"}
        rt.hit = Mock(side_effect=lambda node: node == state["page"])
        rt.capture = Mock(return_value=object())
        rt.recognize = Mock(side_effect=lambda node, **kwargs:
                            SimpleNamespace(hit=node == "MaaNikki_Bless_PageReady"))
        def back(*args):
            state["page"] = GameUI.PAGE_PARENTS[state["page"]]
            return True
        rt.key = Mock(side_effect=back)
        rt.ui.wait_page = Mock(return_value=True)
        rt.ui.main = Mock(return_value=True)
        self.assertTrue(rt.ui.calendar())
        self.assertEqual(rt.key.call_count, 2)
        self.assertEqual([call.args[0] for call in rt.ui.wait_page.call_args_list],
                         ["MaaNikki_RealmPageReady", "MaaNikki_CalendarReady"])
        rt.ui.main.assert_not_called()

    def test_unknown_page_is_not_backed_out_using_guessed_parents(self):
        rt = self.runtime()
        rt.hit = Mock(return_value=False)
        rt.capture = Mock(return_value=object())
        rt.recognize = Mock(return_value=SimpleNamespace(hit=False))
        rt.key = Mock(return_value=True)
        self.assertFalse(rt.ui.back_to("MaaNikki_CalendarReady"))
        rt.key.assert_not_called()

    def test_handoff_uses_only_unambiguous_submitted_order(self):
        cases = [
            (["MaaNikki_Zhaoxi", "MaaNikki_RealmChallenge"], "calendar"),
            (["MaaNikki_Zhaoxi", "MaaNikki_Xinghai"], "calendar"),
            (["MaaNikki_Zhaoxi", "MaaNikki_MonthlyPass"], "main"),
            (["MaaNikki_Zhaoxi"], "main"),
            (["MaaNikki_Zhaoxi", "MaaNikki_Zhaoxi", "MaaNikki_Xinghai"], "main"),
        ]
        for entries, expected in cases:
            with patch.object(run_state, "PLAN", {"entries": entries}):
                self.assertEqual(run_state.next_page("MaaNikki_Zhaoxi"), expected)
        with patch.object(run_state, "PLAN", None):
            self.assertEqual(run_state.next_page("MaaNikki_Zhaoxi"), "main")

    def test_finish_preserves_calendar_for_next_known_task(self):
        rt = self.runtime()
        rt.ui.calendar = Mock(return_value=True)
        rt.main = Mock(return_value=True)
        with patch.object(run_state, "PLAN", {"entries": ["MaaNikki_RealmChallenge", "MaaNikki_Xinghai"]}):
            self.assertEqual(rt.finish("MaaNikki_RealmChallenge"), ("calendar", True))
        rt.main.assert_not_called()

    def test_daily_success_can_handoff_without_returning_to_world(self):
        from registration import AgentServer
        with patch.object(AgentServer, "custom_action", side_effect=lambda name: lambda cls: cls):
            from action.daily_tasks import DailyRun
        rt = self.runtime()
        run = DailyRun(rt.context, "zhaoxi")
        run.open_page = Mock(return_value=True)
        run.score = Mock(return_value=500)
        run.claim = Mock(return_value=True)
        run.log = Mock()
        run.finish = Mock(return_value=("calendar", True))
        run.save_report = Mock()
        run.main = Mock(return_value=True)
        self.assertTrue(run.run_daily())
        self.assertTrue(run.handoff_ok)
        self.assertFalse(run.return_main_ok)
        run.main.assert_not_called()

    def test_daily_energy_only_batch_keeps_calendar_route(self):
        from registration import AgentServer
        with patch.object(AgentServer, "custom_action", side_effect=lambda name: lambda cls: cls):
            from action.daily_tasks import DailyRun
        from daily.planner import Card, ZHAOXI
        run = DailyRun(self.runtime().context, "zhaoxi")
        run.open_page = Mock(return_value=True)
        run.score = Mock(return_value=300)
        run.scan = Mock(return_value=[Card(1, "祝福闪光", ZHAOXI[2], 1)])
        run.available = Mock(return_value=True)
        run.setting = Mock(return_value={"auto_complete": True})
        run.energy = Mock(return_value=True)
        run.check_group = Mock(return_value=500)
        run.claim = Mock(return_value=True)
        run.log = Mock()
        run.finish = Mock(return_value=("calendar", True))
        run.save_report = Mock()
        run.main = Mock(return_value=True)
        with patch("action.daily_tasks.realm_settings", return_value=("bless", {})):
            self.assertTrue(run.run_daily())
        run.energy.assert_called_once()
        run.main.assert_not_called()


if __name__ == "__main__":
    unittest.main()
