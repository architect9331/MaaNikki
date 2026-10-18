"""File-based navigation trials with optional home workflow actions."""
from __future__ import annotations

import ctypes
from copy import deepcopy
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
from types import SimpleNamespace
from task_control import task_time as time

import cv2

from .runtime import Runtime, RESOURCE
from .settings import ROOT, foreground_inputs
from .startup import focus_game
from navigation.controller import ForegroundInput
from navigation.engine import Navigator
from navigation.models import NavigationError, maps, parse_route
from navigation.teleport import Teleporter
from navigation.vision import Locator


NAVIGATION_ACTIONS = {"", "teleport", "wait"}


def inputs_allowed():
    # The running instance can differ from the configuration's active tab.
    # Prefer the controller definition sent with this actual task submission.
    value = os.environ.get("PI_CONTROLLER")
    if value is None:
        return foreground_inputs()
    try:
        controller = json.loads(value)
        win32 = controller.get("win32", {})
        allowed = {"Seize", "SeizeWithBlockInput"}
        return (controller.get("type") == "Win32" and win32.get("mouse") in allowed
                and win32.get("keyboard") in allowed)
    except (ValueError, TypeError, AttributeError):
        return False


def test_document(resource: Path, settings: dict):
    """Read one fixed snapshot and validate before connecting or sending input."""
    filename, name = settings.get("file", ""), settings.get("route", "")
    mode = settings.get("mode", "movement")
    if mode not in {"movement", "home"}:
        raise NavigationError("路线测试模式无效。")
    home = mode == "home"
    if filename:
        if not isinstance(filename, str):
            raise NavigationError("路线文件路径无效。")
        path = Path(filename).expanduser()
        if not path.is_absolute():
            raise NavigationError("请选择本地路线 JSON 的完整路径。")
    else:
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
            raise NavigationError("请先选择路线 JSON 文件。")
        path = resource / "routes" / "daily" / (name+".json")
    if path.suffix.lower() != ".json" or not path.is_file() or path.stat().st_size > 1024*1024:
        raise NavigationError("路线文件不存在、不是 JSON 或超过 1 MB。")
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(document, dict) or not isinstance(document.get("points"), list):
        raise NavigationError("路线必须包含 points 点位列表。")
    if not isinstance(document.get("id"), str) or not document["id"].strip():
        raise NavigationError("路线需要有效的 id。")
    if not 1 <= len(document["points"]) <= 1000:
        raise NavigationError("路线点位数量必须在 1 到 1000 之间。")
    spec = maps(resource).get(document.get("map"))
    if spec is None:
        raise NavigationError("路线地图不受支持。")
    if home and document.get("map") != "home":
        raise NavigationError("家园测试路线必须使用家园地图。")

    def number(value, minimum, maximum):
        return type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum

    maximum_timeout = 900
    if not number(document.get("timeout", 120), 1, maximum_timeout) or not number(document.get("start_radius", 6), .1, 30):
        raise NavigationError(f"路线超时须为 1–{maximum_timeout} 秒，起点误差须为 0.1–30。")
    for index, point in enumerate(document["points"]):
        if not isinstance(point, dict):
            raise NavigationError(f"第 {index+1} 点格式无效。")
        action, param = point.get("action", ""), point.get("params", {})
        if not isinstance(action, str) or not isinstance(param, dict):
            raise NavigationError(f"第 {index+1} 点的动作或参数格式无效。")
        if action == "wait" and not number(param.get("seconds"), 0, 60):
            raise NavigationError(f"第 {index+1} 点的等待时间须为 0–60 秒。")
        if home:
            if action not in {"", "teleport", "fishing_star", "macro", "wait"}:
                raise NavigationError(f"第 {index+1} 点不是支持的家园动作。")
            if action == "macro" and param.get("id") != "starsea_fall":
                raise NavigationError("家园测试仅支持原地下落攻击宏。")
        if (not number(point.get("x"), 0, spec.width) or not number(point.get("y"), 0, spec.height)
                or not number(point.get("radius", 3), .1, 30) or type(point.get("jump", False)) is not bool):
            raise NavigationError(f"第 {index+1} 点的坐标、半径或跳跃设置无效。")
    checkpoints = [document.get("teleport")]
    checkpoints += [point.get("params", {}).get("checkpoint") for point in document["points"]
                    if point.get("action") == "teleport"]
    if any(checkpoint is None for checkpoint in checkpoints[1:]):
        raise NavigationError("路线的传送动作缺少传送点。")
    for checkpoint in checkpoints:
        if checkpoint is None:
            continue
        if (not isinstance(checkpoint, dict)
                or any(not isinstance(checkpoint.get(key), str) or not checkpoint[key].strip()
                       for key in ("name", "province", "region"))
                or not number(checkpoint.get("x"), 0, spec.width)
                or not number(checkpoint.get("y"), 0, spec.height)):
            raise NavigationError("传送点名称、区域或坐标无效。")
    model = parse_route(document)
    return model, spec


def navigation_route(model):
    """Keep the source snapshot intact and build the navigation-only plan."""
    document = deepcopy(model.document)
    skipped = []
    for index, point in enumerate(document["points"]):
        if point.get("action", "") not in NAVIGATION_ACTIONS:
            skipped.append({"index": index, "action": point.pop("action")})
            point.pop("params", None)
    return parse_route(document), skipped


class TestInputs(ForegroundInput):
    def focus_details(self):
        hwnd = self.user32.GetForegroundWindow()
        text = ctypes.create_unicode_buffer(1024)
        if hwnd:
            self.user32.GetWindowTextW(hwnd, text, len(text))
        return {"game_hwnd": self.hwnd, "foreground_hwnd": int(hwnd or 0), "foreground_title": text.value}


class TestTeleporter(Teleporter):
    def nearby(self, checkpoint):
        self.rt.phase("nearby", "正在检查传送点附近的位置。")
        return super().nearby(checkpoint)

    def transport(self, checkpoint):
        self.rt.phase("teleport", "正在打开地图并选择传送点："+checkpoint["name"]+"。")
        return super().transport(checkpoint)

    def region(self, checkpoint):
        self.rt.phase("region", "正在确认地图区域："+checkpoint["region"]+"。")
        return super().region(checkpoint)

    def center(self, x, y, force=False):
        self.rt.phase("checkpoint", "正在定位地图中的传送点。")
        return super().center(x, y, force)

    def confirm_transport(self, checkpoint):
        self.rt.phase("arrival", "正在确认传送并等待落地。")
        return super().confirm_transport(checkpoint)


class RouteTest(Runtime):
    def __init__(self, context):
        super().__init__(context)
        self.directory = None
        self.stage = "preflight"
        self.last_capture = None
        self.progress_message = None

    def capture(self):
        frame = super().capture()
        if frame is not None:
            self.last_capture = frame
        return frame

    def event(self, value):
        if self.directory:
            with (self.directory / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"time": time.time(), **value}, ensure_ascii=False)+"\n")
        if value.get("type") == "waypoint":
            self.progress_message = f"路线测试：已到达第 {value['index']+1} 点，共 {value['total']+1} 点。"
        # UI log callbacks can block on a pipeline RPC. Defer them while
        # movement is held so logging cannot extend a pulse past its waypoint.
        inputs = self.navigation_inputs
        if self.progress_message and (inputs is None or not inputs.held and not inputs.buttons):
            self.log(self.progress_message)
            self.progress_message = None

    def phase(self, stage, message):
        self.stage = stage
        self.event({"type": "phase", "phase": stage})
        self.log("路线测试："+message)

    def run_test(self, settings):
        inputs = None
        previous_inputs = self.navigation_inputs
        mode = settings.get("mode", "movement")
        result = {"success": False, "mode": mode}
        try:
            if os.environ.get("PI_MAANIKKI_DEV_MODE") != "1":
                raise NavigationError("请开启客户端设置中的开发模式后再测试路线。")
            if self.stopped:
                raise NavigationError("路线测试已停止。")
            model, spec = test_document(RESOURCE, settings)
            source_document = model.document
            skipped = []
            if mode == "movement":
                model, skipped = navigation_route(model)
            result.update(route=model.id, skipped_actions=skipped)
            actions = None
            if mode == "home" or any(point.action for point in model.points):
                from navigation.daily_route import RouteActions, preflight
                preflight(RESOURCE, model, "home" if mode == "home" else "navigation")
            if not inputs_allowed():
                raise NavigationError("路线测试需要当前连接使用前台鼠标和键盘（Seize）。")
            self.directory = ROOT / "logs" / "navigation" / (datetime.now().strftime("%Y%m%d-%H%M%S-%f")+"-route-test")
            self.directory.mkdir(parents=True)
            (self.directory / "route.json").write_text(json.dumps(source_document, ensure_ascii=False, indent=2), encoding="utf-8")
            (self.directory / "execution.json").write_text(json.dumps(model.document, ensure_ascii=False, indent=2), encoding="utf-8")
            if skipped:
                self.log(f"导航模式跳过任务动作：{skipped}。",
                         ui_text=f"本轮验证路线导航，跳过 {len(skipped)} 个任务动作。")
            locator = Locator(spec)
            inputs = TestInputs(self.controller, lambda: self.stopped,
                                lambda: self.hit("MaaNikki_MainDetected"), bindings=self.game_keys)
            self.phase("focus_game", "正在自动聚焦游戏。")
            focus_game(self, inputs)
            self.navigation_inputs = inputs
            self.event({"type": "window_ready", **inputs.focus_details()})
            teleporter = TestTeleporter(self, locator, self.event, inputs)
            if mode == "home" or any(point.action for point in model.points):
                executor = "home" if mode == "home" else "navigation"
                card = SimpleNamespace(rule=SimpleNamespace(executor=executor), remaining=None)
                actions = RouteActions(self, RESOURCE, model, executor, card, inputs, self.event)
                actions.teleporter = teleporter
            if mode == "home":
                self.phase("ability_selection", "正在准备采星能力。")
                if not self.main() or not actions.fishing.configure():
                    raise NavigationError("家园测试的采星能力配置未完成。")
            self.phase("prepare", "正在准备路线："+str(model.document.get("title", model.id))+"。")
            teleporter.prepare(model)
            self.phase("follow", "正在检查第 1 点并沿路线行走。")
            navigator = Navigator(locator, inputs, self.capture, lambda: self.stopped, self.pause, self.event,
                                  point_action=actions, waypoint=actions.waypoint if actions else None,
                                  point_prepare=actions.prepare_point if actions else None,
                                  walking=self.ui.walking, page_detector=lambda frame: bool(
                                      (found := self.recognize("MaaNikki_MainDetected", image=frame)) and found.hit))
            pose = navigator.follow(model)
            success = actions.complete() if mode == "home" else True
            result.update(success=success, final=pose.dict())
            if not success:
                result["error"] = "家园路线已走完，但有任务动作未确认完成。"
                self.log(result["error"])
        except (NavigationError, OSError, KeyError, TypeError, ValueError, RuntimeError, cv2.error) as error:
            result.update(success=False, stage=self.stage, error_type=type(error).__name__,
                          error=str(error), stopped=self.stopped)
            if inputs:
                result.update(inputs.focus_details())
            self.log("路线测试停止："+str(error))
            if self.directory and self.last_capture is not None:
                try:
                    frame = self.last_capture.copy()
                    height, width = frame.shape[:2]
                    frame[round(height*.955):, round(width*.78):] = 0
                    cv2.imencode(".png", frame)[1].tofile(str(self.directory / "failure.png"))
                except (OSError, ValueError, cv2.error):
                    pass
        finally:
            if inputs:
                try:
                    inputs.release()
                    self.event({"type": "inputs_released"})
                except (OSError, ValueError, RuntimeError) as error:
                    result.update(success=False, release_error=str(error))
                    self.log("路线测试无法确认按键已全部释放，请检查游戏状态。")
                    self.navigation_halted = True
            self.navigation_inputs = previous_inputs
            if self.directory:
                try:
                    (self.directory / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                    status = "路线测试完成。" if result["success"] else "路线测试结束，请查看异常提示。"
                    self.log(status+"日志："+str(self.directory), ui_text=status)
                except OSError:
                    self.log("路线测试日志保存失败。")
        return result["success"]
