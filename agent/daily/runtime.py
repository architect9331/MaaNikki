from __future__ import annotations

import json
import math
from pathlib import Path
from datetime import datetime
import re
import time
import cv2
from contextlib import contextmanager

from .planner import Card
from .settings import ROOT, foreground_inputs
from game_keys import GameKeys
from performance import count, measure


RESOURCE = ROOT / "resource"


def parameters(context, name: str) -> dict:
    node = context.get_node_data(name) or {}
    action = node.get("action", {})
    value = (action.get("param", {}).get("custom_action_param", {}) if isinstance(action, dict)
             else node.get("custom_action_param", {}))
    value = json.loads(value) if isinstance(value, str) else value
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} 的动作参数必须是对象。")
    return value


class Runtime:
    OBSERVATION_MAX_AGE = .25

    def __init__(self, context):
        self.context = context
        self.controller = context.tasker.controller
        self.navigation_inputs = None
        self.navigation_halted = False
        self.game_keys = GameKeys(context)
        self._observations = []
        from .ui import GameUI
        self.ui = GameUI(self, RESOURCE)

    @property
    def stopped(self):
        return self.context.tasker.stopping or self.navigation_halted

    def pause(self, seconds):
        self.invalidate_frame()
        deadline = time.monotonic() + seconds
        with measure("wait"):
            while not self.stopped and time.monotonic() < deadline:
                if self.navigation_inputs:
                    self.navigation_inputs.check()
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        return not self.stopped

    @contextmanager
    def observe(self, *, fresh=False):
        """Share pixels only within one explicit, read-only decision."""
        if self._observations and not fresh:
            yield
            return
        state = {"image": None, "captured": False, "captured_at": 0.0}
        self._observations.append(state)
        try:
            yield
        finally:
            self._observations.pop()

    def invalidate_frame(self):
        for state in self._observations:
            state.update(image=None, captured=False)

    def frame(self):
        if self.stopped:
            return None
        if not self._observations:
            return self.capture()
        state = self._observations[-1]
        expired = state["captured"] and time.monotonic()-state["captured_at"] > self.OBSERVATION_MAX_AGE
        if not state["captured"] or expired:
            if expired:
                count("capture.expired")
            state["image"] = self.capture()
            state["captured"] = True
            state["captured_at"] = time.monotonic()
        else:
            count("capture.reused")
        return state["image"]

    def log(self, text):
        if not self.stopped:
            self.context.run_task("MaaNikki_Daily_Message", {
                "MaaNikki_Daily_Message": {"focus": {"Node.Action.Starting": text}}})

    def action(self, action, *, ui_click=True, **fields):
        if self.stopped:
            return False
        self.invalidate_frame()
        if self.navigation_inputs:
            self.navigation_inputs.check()
        if action == "Click" and ui_click:
            target = fields.get("target")
            if not isinstance(target, (list, tuple)) or len(target) not in (2, 4):
                raise ValueError("界面点击目标必须是坐标或矩形。")
            point = list(target[:2])
            if len(target) == 4:
                point = [target[0]+target[2]/2, target[1]+target[3]/2]
            if any(type(value) not in (int, float) or not math.isfinite(value) for value in point):
                raise ValueError("界面点击坐标无效。")
            point = [round(value) for value in point]
            # Maa's rectangular Click target is random inside the box. UI
            # icons can have dead edges, and Seize must settle after moving.
            if not self.action("TouchMove", target=point) or not self.pause(.3):
                return False
            fields = {**fields, "target": point}
        with measure("input.action"):
            result = self.context.run_action("MaaNikki_Daily_Action", pipeline_override={
                "MaaNikki_Daily_Action": {"action": action, **fields}})
        succeeded = result is not None and result.success
        if succeeded and action == "Click" and ui_click:
            return self.pause(.1) and self.ui.unhover()
        return succeeded

    def key(self, value, delay=0.35):
        # Integer keys are physical UI shortcuts / legacy physical recordings.
        # Never remap Ctrl+A to the configured left-movement key.
        if self.stopped:
            return False
        if isinstance(value, str):
            try:
                binding = self.game_keys.get(value)
            except ValueError as error:
                self.log(str(error))
                return False
            if binding.kind == "mouse":
                self.invalidate_frame()
                with self.input_guard() as inputs:
                    clicked = inputs.click_mouse(binding.code)
                return clicked and self.pause(delay)
            value = binding.code
        return self.action("ClickKey", key=value) and self.pause(delay)

    def hold(self, key, seconds):
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 <= seconds <= 8 or self.stopped:
            return False
        self.invalidate_frame()
        binding = self.game_keys.get(key) if isinstance(key, str) else None
        if binding and (binding.kind == "mouse" or foreground_inputs()):
            with self.input_guard() as inputs:
                if key in {"forward", "left", "backward", "right"}:
                    self.game_keys.movement()
                try:
                    inputs._down(binding)
                    inputs.wait(seconds)
                    return not self.stopped
                finally:
                    inputs.release()
        key = binding.code if binding else key
        if not 0 <= seconds <= 8 or not self.action("KeyDown", key=key):
            return False
        try:
            return self.pause(seconds)
        finally:
            # Release even when the user presses Stop.
            self.controller.post_key_up(key).wait()

    def capture(self):
        self.invalidate_frame()
        if self.stopped:
            return None
        if self.navigation_inputs:
            self.navigation_inputs.check()
        with measure("capture"):
            result = self.controller.post_screencap().wait()
            image = result.get() if result.succeeded else None
            if image is None or image.size == 0:
                count("capture.failed")
                return None
            return image

    def recognize(self, node, fields=None, image=None):
        image = self.frame() if image is None else image
        if image is None or image.size == 0:
            return None
        with measure("recognition.native"):
            return self.context.run_recognition(node, image, {node: fields} if fields else None)

    def hit(self, node):
        result = self.recognize(node)
        return bool(result and result.hit)

    def input_text(self, content):
        if self.stopped:
            return False
        self.invalidate_frame()
        with measure("input.text"):
            return self.controller.post_input_text(content).wait().succeeded

    def text(self, roi, expected=".+", image=None):
        result = self.recognize("MaaNikki_Daily_OCR", {"roi": roi, "expected": expected}, image)
        if not result or not result.hit:
            return ""
        results = getattr(result, "filtered_results", []) or [result.best_result]
        return " ".join(getattr(item, "text", "") for item in results)

    def click_text(self, words, roi=None):
        fields = {"expected": words, "roi": roi or [0, 0, 1280, 720]}
        result = self.recognize("MaaNikki_Daily_OCR", fields)
        return bool(result and result.hit and self.ui.click_box(list(result.box)) and self.pause(0.5))

    def click_template(self, node, attempts=3, wait_seconds=None, *, ready=None, verify=True):
        config = parameters(self.context, node)
        ready = ready or config.get("ready")
        if ready and verify:
            return self.ui.enter_page(lambda: self.click_template(
                node, attempts=1, wait_seconds=0, verify=False), ready,
                source=config.get("source"), attempts=attempts,
                seconds=config.get("ready_timeout", 5),
                recover=(lambda: self.ui.recover_page(config["source"]))
                if config.get("source") else None)
        if wait_seconds is None:
            delay = (self.context.get_node_data(node) or {}).get("post_delay", 500)
            if isinstance(delay, (list, tuple)) and delay:
                delay = max(delay)
            wait_seconds = delay / 1000 if type(delay) in (int, float) else .5
        if not math.isfinite(wait_seconds) or not 0 <= wait_seconds <= 60:
            raise ValueError("按钮等待时间无效。")
        for index in range(attempts):
            result = self.recognize(node)
            if result and result.hit:
                target = config.get("target", True)
                box = list(result.box) if target is True else target
                return self.action("Click", target=box) and self.pause(wait_seconds)
            if index+1 < attempts and not self.pause(1):
                break
        return False

    def wait_hit(self, node, seconds=5):
        """Compatibility entry point for the shared feature confirmation."""
        return self.ui.wait_page(node, seconds=seconds)

    def main(self):
        return self.ui.main()

    def finish(self, entry):
        """Leave a verified page suited to the next submitted task."""
        from .run_state import next_page
        page = next_page(entry)
        ok = self.ui.calendar() if page == "calendar" else self.main()
        if ok and page == "calendar":
            self.log("任务衔接：已保留奇想日历，下一项可直接继续。")
        return page, ok

    @contextmanager
    def input_guard(self):
        """Guard all nested page operations as well as movement; never refocus on loss."""
        from navigation.controller import ForegroundInput
        previous = self.navigation_inputs
        inputs = previous or ForegroundInput(self.controller, lambda: self.stopped, bindings=self.game_keys)
        self.navigation_inputs = inputs
        try:
            inputs.check()
            yield inputs
        finally:
            if not inputs.foreground():
                self.navigation_halted = True
            self.navigation_inputs = previous
            if previous is None:
                inputs.release()

    def run(self, entry, overrides=None):
        if self.stopped:
            return False
        self.invalidate_frame()
        with measure("task.child"):
            result = self.context.clone().run_task(entry, overrides or {})
        return result is not None and result.status.succeeded and not self.stopped

    def setting(self, mode):
        prefix = f"MaaNikki_{mode.capitalize()}_Daily"
        result = dict(parameters(self.context, prefix + "Settings"))
        # Maa replaces custom_action_param rather than deep-merging it. Keep
        # independently selectable options in separate parameter-only nodes.
        flags = [("Auto", "auto_complete"), ("Photo", "delete_photo")]
        if mode == "xinghai":
            flags += [("Bubble", "bubble_equipped")]
        else:
            flags += [("AbilityPlan", "ability_plan")]
        flags += [("Energy", "allow_energy")] if mode == "zhaoxi" else [("Chat", "allow_chat"), ("ChatText", "chat_text")]
        for suffix, key in flags:
            value = parameters(self.context, prefix + suffix)
            if "value" in value:
                result[key] = value["value"]
        return result

    def routes(self, name):
        from navigation.models import load_route
        route = load_route(RESOURCE, name)
        return route.document if route else None

    def navigate(self, name, mode=None, card=None, progress=None, *, meteor_travel=False):
        if not foreground_inputs():
            self.log("路线导航需要前台鼠标和键盘输入，请将两者设置为 Seize。")
            return False
        route = self.routes(name)
        if route is None:
            self.log(f"路线无法读取：{name}。")
            return False
        from navigation.controller import ForegroundInput
        from navigation.engine import Navigator
        from navigation.models import NavigationError, maps, parse_route
        from navigation.vision import Locator
        inputs = actions = locator = teleporter = None
        stage = "map_loading"
        events = [{"type": "initializing", "stage": stage}]
        previous_inputs = self.navigation_inputs
        try:
            model = parse_route(route, name)
            if meteor_travel and route["schema_version"] != 3:
                raise NavigationError("流星移动路线必须使用已验证的坐标路线格式。")
            locator = Locator(maps(RESOURCE)[model.map_id])
            stage = "window_binding"
            inputs = ForegroundInput(self.controller, lambda: self.stopped,
                                     lambda: self.hit("MaaNikki_MainDetected"), bindings=self.game_keys)
            stage = "focus_game"
            if previous_inputs:
                # A running parent guard must stop on focus loss, not refocus.
                previous_inputs.check()
                inputs.check()
            else:
                from .startup import focus_game
                focus_game(self, inputs)
            self.navigation_inputs = inputs
            events.append({"type": "window_ready", "hwnd": inputs.hwnd, "foreground": inputs.foreground()})
            if route["schema_version"] == 3:
                from navigation.daily_route import RouteActions, preflight
                from navigation.teleport import Teleporter
                if mode is None or card is None:
                    raise NavigationError("此路线需要从相应日常任务执行。")
                stage = "action_validation"
                preflight(RESOURCE, model, card.rule.executor)
                if meteor_travel:
                    if (mode != "xinghai" or card.rule.executor != "meteor" or model.map_id != "starsea"
                            or any(point.action for point in model.points) or not route.get("teleport")):
                        raise NavigationError("流星移动路线配置无效。")
                else:
                    actions = RouteActions(self, RESOURCE, model, mode, card, inputs, events.append)
                    if progress is not None:
                        actions.count = progress
                if card.rule.executor == "bubble" and not self.setting(mode).get("bubble_equipped", False):
                    raise NavigationError("请先装备泡泡漂浮套装，并在星海任务设置中确认。")
                stage = "teleport"
                teleporter = Teleporter(self, locator, events.append, inputs)
                if actions:
                    actions.teleporter = teleporter
                if actions and card.rule.executor == "home":
                    stage = "ability_selection"
                    if not actions.fishing.configure():
                        raise NavigationError("采星能力配置未完成，本项尚未开始。")
                    stage = "teleport"
                if meteor_travel:
                    # Keep the map opened by Go Now; transport handles an arbitrary viewport.
                    teleporter.transport(route["teleport"])
                else:
                    teleporter.prepare(model)
                stage = "ability_selection"
                if card.rule.executor == "insect" and not actions.select_insect():
                    raise NavigationError("捕虫能力配置未完成，本项尚未开始。")
            navigator = Navigator(locator, inputs, self.capture, lambda: self.stopped, self.pause, events.append,
                                  point_action=actions, waypoint=actions.waypoint if actions else None,
                                  walking=self.ui.walking, page_detector=lambda frame: bool(
                                      (result := self.recognize("MaaNikki_MainDetected", image=frame)) and result.hit),
                                  point_prepare=actions.prepare_point if actions and any(
                                      point.action == "teleport" for point in model.points) else None)
            self.log("正在沿路线前往任务位置，请保持游戏前台。")
            stage = "route_execution"
            navigator.follow(model)
            result = actions.complete() if actions else True
            if actions and not result:
                self.log(f"路线动作结束，获得 {actions.count}，目标 {actions.quota}。")
            self.navigation_count = actions.count if actions else 0
            events.append({"type": "execution_result", "success": result, "count": self.navigation_count})
            return result
        except (NavigationError, KeyError, OSError, TypeError, ValueError, RuntimeError, cv2.error) as error:
            if actions:
                self.navigation_count = actions.count
            events.append({"type": "stopped", "stage": stage, "error_type": type(error).__name__, "reason": str(error)})
            # Save the actual recognition frame, before map closure or task
            # cleanup can replace it. Crops exclude account information.
            selection_view = (stage == 'teleport' and teleporter is not None
                              and teleporter.last_selection_frame is not None)
            frame = ((teleporter.last_selection_frame if selection_view else teleporter.last_frame)
                     if stage == 'teleport' and teleporter else (locator.last_frame if locator else None))
            if frame is not None and frame.shape[:2] == (720, 1280):
                try:
                    from datetime import datetime
                    folder = ROOT / 'logs' / 'navigation'
                    folder.mkdir(parents=True, exist_ok=True)
                    map_view = stage == 'teleport' and teleporter is not None and teleporter.last_frame is not None
                    roi = ([830, 105, 440, 600] if selection_view else
                           [240, 180, 590, 365] if map_view else [54, 14, 134, 134])
                    x, y, w, h = roi
                    filename = datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'-'+(
                        'map-details' if selection_view else 'bigmap' if map_view else 'minimap')+'.png'
                    if cv2.imwrite(str(folder / filename), frame[y:y+h, x:x+w]):
                        events.append({'type': 'failure_frame', 'file': filename, 'roi': roi,
                                       'heading_diagnostics': dict(locator.heading_diagnostics) if locator else {}})
                except (OSError, ValueError, cv2.error):
                    self.log('导航失败时的地图裁图未能保存。')
            if isinstance(error, NavigationError):
                self.log(f"导航停止：{error}")
            else:
                self.log("导航未能完成，本项已停止；具体原因已记录在导航日志中。")
            if inputs and not inputs.foreground():
                self.navigation_halted = True
            return False
        finally:
            self.navigation_inputs = previous_inputs
            if inputs:
                try:
                    inputs.release()
                except (OSError, RuntimeError, ValueError) as error:
                    events.append({"type": "release_failed", "error_type": type(error).__name__, "reason": str(error)})
                    self.log("无法确认导航按键已全部释放，本轮停止，请检查游戏状态。")
                    self.navigation_halted = True
            from datetime import datetime
            folder = ROOT / "logs" / "navigation"
            try:
                folder.mkdir(parents=True, exist_ok=True)
                (folder / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".json")).write_text(
                    json.dumps({"route": name, "events": events}, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError:
                self.log("导航日志保存失败，请检查日志目录权限和磁盘空间。")


PROMPTS = {"music": "更改音乐", "bottle": "漂流瓶|查看", "delivery": "投递",
           "animal": "变身", "chat": "坐下", "bell": "召唤摇铃|摇动星之铃|摇铃"}
INTERACTION_ROI = [265, 97, 897, 534]


class Executors(Runtime):
    def available(self, mode: str, card: Card) -> bool:
        rule = card.rule
        if not rule or not rule.executor:
            return False
        options = self.setting(mode)
        if not options.get("auto_complete", True):
            return False
        if rule.energy:
            return options.get("allow_energy", True)
        if rule.key == "like" and not (self.context.get_node_data("MaaNikki_Xinghai_LookbookDetected") or {}).get("enabled", True):
            return False
        if rule.key == "chat" and not options.get("allow_chat", False):
            return False
        if rule.key == "bubble" and not options.get("bubble_equipped", False):
            return False
        if rule.executor == "crystal":
            return any(self.usable_route(self.routes(f"xinghai_crystal_{index}"), "crystal") for index in range(1, 9))
        if rule.executor == "meteor":
            from .meteor import meteor_routes
            try:
                return mode == "xinghai" and bool(meteor_routes(RESOURCE))
            except (ValueError, KeyError, TypeError, OSError):
                return False
        if rule.route:
            route = self.routes(rule.route)
            if route:
                if route.get("schema_version") == 3 and not self.usable_route(route, rule.executor):
                    return False
                if rule.executor in ("plant", "insect", "fight", "bubble"):
                    if route.get("schema_version") == 2:
                        # A coordinate walking route alone must not pretend to
                        # collect, fight or use an ability.
                        return False
                    capacity = route.get("capacity")
                    needed = card.remaining if card.remaining is not None else {"plant": 5, "insect": 3, "fight": 1, "bubble": 1}[rule.executor]
                    return (rule.executor == "bubble" and route.get("schema_version") == 3 or
                            type(capacity) is int and capacity >= needed)
                return True
            prompt = PROMPTS.get(rule.executor)
            return bool(prompt and self.main() and self.text(INTERACTION_ROI, prompt))
        return True

    @staticmethod
    def usable_route(value, executor):
        if not value:
            return False
        if value.get("schema_version") != 3:
            return False
        from navigation.daily_route import preflight
        from navigation.models import NavigationError, parse_route
        try:
            preflight(RESOURCE, parse_route(value), executor)
            return True
        except (NavigationError, KeyError, TypeError):
            return False

    def execute(self, mode: str, card: Card):
        rule = card.rule
        if not self.main() or self.stopped:
            return False
        if rule.executor == "crystal":
            return self.crystals(mode, card)
        if rule.executor == "bell":
            return self.ring_bell()
        if rule.executor == "meteor":
            from .meteor import Meteor
            return Meteor(self, RESOURCE).run(mode, card)
        if rule.route and self.routes(rule.route):
            route = self.routes(rule.route)
            if route.get("schema_version") == 3:
                return self.navigate(rule.route, mode, card)
            if not self.navigate(rule.route):
                self.log(f"{rule.label}的路线起点或途经画面不匹配，停止本项。")
                return False
        elif rule.route and rule.executor not in PROMPTS:
            return False
        if rule.executor in ("plant", "insect", "fight", "bubble"):
            # Their calibrated route includes collection/ability/combat inputs.
            return self.main()
        if rule.executor == "photo":
            result = self.photo(mode)
        elif rule.executor == "like":
            result = self.run("MaaNikki_Xinghai_OpenEscMenu")
        elif rule.executor == "dig":
            result = self.run("MaaNikki_Dig")
        elif rule.executor == "place":
            result = self.place()
        elif rule.executor == "music":
            result = self.interaction("music")
        elif rule.executor == "bottle":
            result = self.interaction("bottle")
        elif rule.executor == "delivery":
            result = self.interaction("delivery")
        elif rule.executor == "chat":
            result = self.chat(mode)
        elif rule.executor == "animal":
            result = self.animal(card)
        else:
            return False
        return self.main() and result

    def hub(self):
        from .starsea import Starsea
        return Starsea(self, RESOURCE).hub()

    def crystals(self, mode, card):
        from .starsea import Starsea
        return Starsea(self, RESOURCE).crystals(mode, card)

    def ring_bell(self):
        from .starsea import Starsea
        return Starsea(self, RESOURCE).ring()

    def interaction(self, kind):
        if kind == "bottle":
            return (self.ui.wait_pickup(2) and self.key("interact", .5)
                    and self.ui.wait_page("MaaNikki_Daily_BottleClose")
                    and self.click_template("MaaNikki_Daily_BottleClose"))
        if not self.ui.wait_text(PROMPTS[kind], INTERACTION_ROI, attempts=3):
            return False
        if kind == "music":
            # No dedicated music-page marker is available; submit only once.
            return self.key("interact", 1)
        if kind == "delivery":
            return self.delivery_page() and self.delivery_confirm()
        return False

    def delivery_page(self):
        source = self.ui.feature(text=PROMPTS["delivery"], roi=INTERACTION_ROI)
        destination = self.ui.feature(text="^确认投递$", roi=self.ui.roi("AreaDialog"))
        return self.ui.enter_page(lambda: self.key("interact", 0), destination, source=source,
                                  recover=lambda: self.main() and self.ui.wait_page(source))

    def delivery_confirm(self):
        roi = self.ui.roi("AreaDialog")
        destination = self.ui.feature(text="^确认$", roi=roi)
        return (self.ui.enter_page(lambda: self.ui.find(roi=roi, text="确认投递", exact=True, scroll=False),
                    destination, source=self.ui.feature(text="^确认投递$", roi=roi),
                    recover=self.delivery_page)
                and self.ui.find(roi=roi, text="确认", exact=True, scroll=False))

    def photo_open(self):
        return self.ui.enter_page(lambda: self.key("photo", 0), "MaaNikki_Daily_PhotoReady",
                                  source="MaaNikki_MainDetected", recover=self.main)

    def photo_shutter(self):
        if not self.key("shutter", .8):
            return False
        if self.wait_hit("MaaNikki_Daily_PhotoTaken", 10):
            return True
        self.log("未能确认照片已拍摄，请检查相机界面和快门键设置。")
        return False

    def photo(self, mode):
        if not self.photo_open() or not self.photo_shutter():
            return False
        # Only this newly produced photo can be deleted, never an album item.
        if self.setting(mode).get("delete_photo", False):
            return self.click_template("MaaNikki_Daily_PhotoDelete") and self.click_template("MaaNikki_Daily_PhotoDeleteConfirm")
        return True

    def chat(self, mode):
        for attempt in range(3):
            if self.ui.asset("IconSetdownFeature", roi=INTERACTION_ROI, scale=2/3):
                break
            if attempt == 2 or not self.pause(1):
                return False
        if not self.key("interact", 2):
            return False
        # Recovery must restore the seat before reopening the chat channel.
        def recover_seat():
            return (self.main() and self.ui.wait_page(self.ui.feature(
                asset="IconSetdownFeature", roi=INTERACTION_ROI, scale=2/3))
                    and self.key("interact", 2))
        if not self.ui.enter_page(lambda: self.key("chat", 0), "MaaNikki_Daily_GroupChat",
                                  source="MaaNikki_MainDetected", recover=recover_seat):
            return False
        content = str(self.setting(mode).get("chat_text", "1"))[:40]
        if not content or not self.input_text(content):
            return False
        # Chat opening is rebindable; the text editor's Send is always Enter.
        return self.key(13, .5) and self.main() and self.key("interact", .5)

    def animal(self, card, times=None):
        if times is None:
            times = card.remaining if card.remaining is not None else (3 if card.rule.key == "animal_three" else 1)
        if type(times) is not int or not 1 <= times <= 3:
            return False
        for _ in range(times):
            if not self.ui.wait_text("变身", INTERACTION_ROI, attempts=10) or not self.key("interact", 2):
                return False
        roi = self.ui.roi("AreaDialog")
        return (self.key("exit", 0) and self.ui.wait_page(text="^确认$", roi=roi)
                and self.ui.find(roi=roi, text="确认", exact=True, scroll=False))

    def select_item(self):
        def item_browser_ready():
            # The category icon is also present while configuring quick slots.
            # It cannot prove that Finish Settings has actually been clicked.
            return (self.hit("MaaNikki_Daily_ItemCategory")
                    and not self.hit("MaaNikki_Daily_ItemFinish"))

        def open_wheel():
            return self.main() and self.ui.enter_page(lambda: self.hold("item", 3),
                "MaaNikki_Daily_ItemSetting", source="MaaNikki_MainDetected", recover=self.main)
        def open_settings():
            return open_wheel() and self.ui.enter_page(lambda: self.click_template(
                "MaaNikki_Daily_ItemSetting", attempts=1, wait_seconds=0), "MaaNikki_Daily_ItemFinish",
                source="MaaNikki_Daily_ItemSetting", recover=open_wheel)
        if not (open_settings() and self.ui.enter_page(lambda: self.click_template(
                    "MaaNikki_Daily_ItemFinish", attempts=1, wait_seconds=0), item_browser_ready,
                    source="MaaNikki_Daily_ItemFinish", recover=open_settings)
                and self.click_template("MaaNikki_Daily_ItemCategory")
                and self.pause(.5) and self.action("Click", target=self.ui.roi("AreaItemFirstItem"))
                and self.pause(.5)):
            return False
        with self.observe(fresh=True):
            # Selecting an already deployed ornament recalls it and leaves the
            # browser open. Select it once more to enter placement, as upstream
            # does, but never repeat a lantern confirmation or placement input.
            recalled = (item_browser_ready() and not self.hit("MaaNikki_Daily_LanternConfirm")
                        and not self.placement_ready())
        if recalled:
            self.log("摆饰选择面板仍打开，可能已收回原有摆饰，再选择一次进入放置。")
            return self.action("Click", target=self.ui.roi("AreaItemFirstItem")) and self.pause(.5)
        return True

    def placement_ready(self):
        # The category tab and minimap can remain visible in placement mode.
        # Require the actual placement control instead of their disappearance.
        return bool(self.text([0, 500, 1210, 200], r"放置|摆放"))

    def place(self):
        from navigation.controller import relative_camera
        # Selecting the item / confirming a lantern can activate placement
        # immediately. Do not move away from UI controls after these inputs.
        with self.ui.preserve_cursor():
            if not foreground_inputs() or not self.select_item():
                self.log("摆饰停止：未能选择摆饰。")
                return False
            lantern = self.hit("MaaNikki_Daily_LanternConfirm")
            if lantern:
                if (not self.click_template("MaaNikki_Daily_LanternConfirm")
                        or not self.ui.find(roi=self.ui.roi("AreaDialog"), text="确认", exact=True, scroll=False)):
                    return False
            if not self.ui.wait_page(self.placement_ready, seconds=20):
                self.log("摆饰停止：选中物品后未出现放置操作提示。")
                self.save_placement_failure("placement_not_ready")
                self.main()
                return False
        if not relative_camera(self.controller, 0, -133) or not self.pause(.3):
            return False
        recover_before = bool(self.text([0, 500, 1210, 200], r"收回|回收"))
        for attempt in range(6):
            if not self.key("place", .3):
                return False
            deadline = time.monotonic()+4
            confirmations = 0
            rejected = False
            while not self.stopped and time.monotonic() < deadline:
                with self.observe(fresh=True):
                    rejected = self.hit("MaaNikki_Daily_CantPlace")
                    preview = self.placement_ready()
                    returned = self.hit("MaaNikki_MainDetected")
                    success = bool(self.text([250, 100, 780, 420], r"放置成功|已放置"))
                    recover_ready = bool(self.text([0, 500, 1210, 200], r"收回|回收"))
                if rejected:
                    break
                confirmed = success or returned and not preview or recover_ready and not recover_before
                confirmations = confirmations+1 if confirmed else 0
                if confirmations >= 2:
                    self.log("已确认摆饰放置完成，稍后核实任务积分。")
                    return self.main()
                if not self.pause(.3):
                    return False
            # Retry only after an explicit cannot-place response. An ambiguous
            # submitted placement must not create another object.
            if not rejected:
                self.log("摆饰停止：点击后未能确认放置结果。")
                self.save_placement_failure("placement_unconfirmed")
                self.main()
                return False
            if not relative_camera(self.controller, -133, 0) or not self.pause(.3):
                return False
            deadline = time.monotonic()+3
            while not self.stopped and self.hit("MaaNikki_Daily_CantPlace"):
                if time.monotonic() >= deadline or not self.pause(.3):
                    self.log("无法放置提示未消失，本项停止，避免重复提交。")
                    self.main()
                    return False
        self.log("当前朝向均无法放置摆饰，本项停止。")
        self.save_placement_failure("cannot_place")
        self.key("recover", .3)
        self.main()
        return False

    def save_placement_failure(self, stage):
        try:
            frame = self.capture()
            if frame is None:
                return
            directory = ROOT / "logs/daily"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / (datetime.now().strftime("%Y%m%d-%H%M%S-%f")+f"-place-{stage}.png")
            cv2.imwrite(str(path), frame[:680])
        except (OSError, cv2.error):
            pass

    def energy(self, card, preferred, overrides):
        from .gameplay import Gameplay
        if card.rule.key == "energy":
            self.log("累计活跃能量条件不在朝夕中额外增加挑战次数。")
            return False
        kind = card.rule.key
        clone = self.context.clone()
        if overrides and not clone.override_pipeline(overrides):
            self.log("幻境设置未能应用，本次不消耗活跃能量。")
            return False
        # Supplement a specific realm card with its default minimum, never Max.
        child = Executors(clone)
        try:
            return Gameplay(child).realm(kind, maximize=False, return_page="calendar")
        finally:
            self.navigation_halted |= child.navigation_halted

    def set_amount(self, kind, minimum_energy):
        count_node = "MaaNikki_Daily_JihuaCount" if kind == "jihua" else "MaaNikki_Daily_QuickCount"
        data = self.context.get_node_data(count_node) or {}
        recognition = data.get("recognition")
        roi = recognition.get("param", {}).get("roi") if isinstance(recognition, dict) else data.get("roi")
        if not roi:
            return False
        result = self.recognize(count_node)
        raw = getattr(result.best_result, "text", "") if result and result.hit else ""
        if not re.fullmatch(r"\s*\d+\s*", raw):
            self.log("无法读取挑战数量，请补充数量弹窗截图；本次未确认消耗。")
            return False
        current = int(raw)
        if not 1 <= current <= 999:
            return False
        desired = 1
        if minimum_energy:
            text = self.text([400, 215, 470, 215])
            match = re.search(r"(?:消耗|投入|注入)\D{0,8}(\d+)\s*(?:点|活跃能量)", text)
            if not match or int(match.group(1)) <= 0 or int(match.group(1)) % current:
                self.log("无法读取本次活跃能量消耗，不自动调整挑战次数。")
                return False
            desired = math.ceil(minimum_energy / (int(match.group(1)) // current))
        if current != desired:
            result = self.recognize(count_node)
            if not result or not result.hit or not self.action("Click", target=list(result.box)):
                return False
            if not self.action("KeyDown", key=17):
                return False
            try:
                if not self.key(65, 0.1):
                    return False
            finally:
                self.controller.post_key_up(17).wait()
            if not self.input_text(str(desired)) or not self.pause(0.3):
                return False
            adjusted = self.recognize(count_node)
            if not adjusted or not adjusted.hit or getattr(adjusted.best_result, "text", "").strip() != str(desired):
                self.log("挑战数量未能设置为要求值，取消本项。")
                return False
        self.log(f"本次用于朝夕补分：{desired} 次，不使用最大次数。")
        return True
