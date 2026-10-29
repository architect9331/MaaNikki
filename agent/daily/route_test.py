"""File-based navigation trials with optional home or bottle actions."""
from __future__ import annotations

from file_io import write_json

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
from .settings import ROOT, foreground_inputs as inputs_allowed
from .startup import focus_game
from navigation.controller import ForegroundInput
from navigation.session import create_navigator
from navigation.models import NavigationError, maps, parse_route
from navigation.teleport import Teleporter
from navigation.vision import Locator


NAVIGATION_ACTIONS = {"", "teleport", "wait"}


def read_test_document(resource: Path, settings: dict):
    """Read a single file snapshot, including optional collection manifests."""
    filename, name = settings.get("file", ""), settings.get("route", "")
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
    return json.loads(path.read_text(encoding="utf-8-sig"))


def test_document(resource: Path, settings: dict, *, document=None):
    """Validate one fixed snapshot before connecting or sending input."""
    mode = settings.get("mode", "movement")
    if mode not in {"movement", "home", "bottle"}:
        raise NavigationError("路线测试模式无效。")
    home = mode == "home"
    bottle = mode == "bottle"
    if document is None:
        document = read_test_document(resource, settings)
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
    if bottle and document.get("map") != "starsea":
        raise NavigationError("漂流瓶测试路线必须使用星海地图。")

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
        if bottle and action not in NAVIGATION_ACTIONS | {"bottle"}:
            raise NavigationError(f"第 {index+1} 点不是支持的漂流瓶测试动作。")
        if home:
            if action not in {"", "teleport", "fishing_star", "macro", "wait"}:
                raise NavigationError(f"第 {index+1} 点不是支持的家园动作。")
            if action == "macro" and param.get("id") != "starsea_fall":
                raise NavigationError("家园测试仅支持原地下落攻击宏。")
        if (not number(point.get("x"), 0, spec.width) or not number(point.get("y"), 0, spec.height)
                or not number(point.get("radius", 3), .1, 30) or type(point.get("jump", False)) is not bool):
            raise NavigationError(f"第 {index+1} 点的坐标、半径或跳跃设置无效。")
    if bottle and not any(point.get("action") == "bottle" for point in document["points"]):
        raise NavigationError("漂流瓶测试路线缺少拾取点位。")
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
        # Falling is part of traversing the bubble route, rather than a task
        # interaction. Keep that movement macro in navigation-only runs.
        falling = (point.get("action") == "macro"
                   and point.get("params", {}).get("id") == "starsea_fall")
        if point.get("action", "") not in NAVIGATION_ACTIONS and not falling:
            action = point.pop("action")
            skipped.append({"index": index, "action": action})
            point.pop("params", None)
            if action == "bottle":
                # Compare the same settled pickup position in navigation mode,
                # while leaving the bottle interaction disabled.
                point.update(action="wait", params={"seconds": 0})
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

    def transport(self, checkpoint, *, on_map_ready=None, reuse_map=False):
        self.rt.phase("teleport", "正在打开地图并选择传送点："+checkpoint["name"]+"。")
        return super().transport(checkpoint, on_map_ready=on_map_ready, reuse_map=reuse_map)

    def region(self, checkpoint):
        self.rt.phase("region", "正在确认地图区域："+checkpoint["region"]+"。")
        return super().region(checkpoint)

    def center(self, x, y, force=False, *, allow_visible=False):
        self.rt.phase("checkpoint", "正在定位地图中的传送点。")
        return super().center(x, y, force, allow_visible=allow_visible)

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
        self.bottle_results = []
        self.review_points = []
        self.last_pose = None
        self.arrival_evidence = {}
        self.last_result = None
        self.collection_entry = None

    def capture(self):
        frame = super().capture()
        if frame is not None:
            self.last_capture = frame
        return frame

    def event(self, value):
        if value.get("type") == "pose":
            self.last_pose = value
        if value.get("type") == "waypoint" and self.review_points:
            index = value["index"]
            point = self.review_points[index]
            if point.get("action") == "bottle":
                # Bottle arrivals (including navigation-only waits) are settled
                # with movement released by Navigator.
                # Reuse its observation, without another capture or input pulse.
                evidence = {"index": index, "sample": point.get("params", {}).get("sample"),
                            "target": {key: point[key] for key in ("x", "y")},
                            "sample_position": point.get("params", {}).get("sample_position"),
                            "pose": self.last_pose, "frame": None}
                if self.directory and self.last_capture is not None:
                    try:
                        filename = f"point-{index+1:03d}.png"
                        from diagnostics import save_image
                        save_image(self.directory / filename, self.last_capture)
                        evidence["frame"] = filename
                    except (OSError, ValueError, cv2.error):
                        pass
                self.arrival_evidence = evidence
                self.event({"type": "bottle_arrival", **evidence})
        if value.get("type") == "bottle_result":
            value = {**value, "arrival": self.arrival_evidence}
            self.bottle_results.append(value)
            label = value.get("sample") or str(len(self.bottle_results))
            status = "已拾取" if value["success"] else "未确认拾取"
            self.progress_message = f"漂流瓶点位 {label}：{status}，继续下一个点位。"
        if self.directory:
            with (self.directory / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"time": time.time(), **value}, ensure_ascii=False)+"\n")
        if value.get("type") == "waypoint" and self.collection_entry is None:
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
        try:
            if os.environ.get("PI_MAANIKKI_DEV_MODE") != "1":
                raise NavigationError("请开启客户端设置中的开发模式后再测试路线。")
            source = read_test_document(RESOURCE, settings)
            if isinstance(source, dict) and source.get("kind") == "route_collection":
                return self.run_collection(settings, source)
            prepared = test_document(RESOURCE, settings, document=source)
        except (NavigationError, OSError, KeyError, TypeError, ValueError) as error:
            self.log("路线测试停止："+str(error))
            return False
        return self.run_single(settings, prepared=prepared)

    def run_collection(self, settings, source):
        """Freeze every route before input; record each run independently."""
        if settings.get("mode", "movement") != "movement":
            raise NavigationError("路线合集请使用“路线导航”模式。")
        entries = source.get("routes")
        if (source.get("schema_version") != 1 or source.get("map") != "starsea"
                or not isinstance(source.get("id"), str) or not source["id"].strip()
                or not isinstance(entries, list) or not 1 <= len(entries) <= 64
                or type(source.get("continue_on_failure", True)) is not bool):
            raise NavigationError("星海路线合集格式无效。")
        prepared = []
        names = set()
        for index, entry in enumerate(entries, 1):
            if not isinstance(entry, dict) or not isinstance(entry.get("route"), str):
                raise NavigationError(f"合集第 {index} 条需要指定内置路线名。")
            name = entry["route"]
            if name in names:
                raise NavigationError("路线合集中存在重复路线："+name)
            model, spec = test_document(RESOURCE, {"route": name, "mode": "movement"})
            if model.map_id != "starsea" or not model.document.get("teleport"):
                raise NavigationError("合集路线需要星海地图和起始传送点："+name)
            execution, _ = navigation_route(model)
            from navigation.daily_route import preflight
            preflight(RESOURCE, execution, "navigation")
            title = entry.get("title", name)
            if not isinstance(title, str) or not title.strip():
                raise NavigationError(f"合集第 {index} 条名称无效。")
            prepared.append((model, spec, title))
            names.add(name)

        directory = ROOT / "logs/navigation" / (datetime.now().strftime("%Y%m%d-%H%M%S-%f")+"-route-collection")
        directory.mkdir(parents=True)
        write_json(directory / "collection.json", source)
        summary = {"collection": source["id"], "success": False, "planned": len(prepared),
                   "started_at": datetime.now().astimezone().isoformat(), "routes": []}
        write_json(directory / "result.json", summary)
        self.log(f"星海跑图合集：共 {len(prepared)} 条路线，开始逐条运行。")
        try:
            for index, (model, spec, title) in enumerate(prepared, 1):
                if self.stopped:
                    summary["stopped"] = True
                    break
                self.log(f"星海跑图 {index}/{len(prepared)}：{title}。")
                runner = RouteTest(self.context)
                runner.collection_entry = {"collection": source["id"], "index": index,
                                           "total": len(prepared), "title": title}
                child = directory / f"route-{index:02d}"
                started = time.monotonic()
                ok = runner.run_single({"mode": "movement"}, prepared=(model, spec),
                                       directory=child, refocus=index == 1, force_teleport=True)
                details = runner.last_result or {"success": False, "error": "执行结果未保存"}
                summary["routes"].append({"index": index, "route": model.id, "title": title,
                                          "directory": child.name,
                                          "active_seconds": round(time.monotonic()-started, 3),
                                          **details})
                write_json(directory / "result.json", summary)
                self.log(f"星海跑图 {index}/{len(prepared)}：{title}，"+("完成。" if ok else "中断，已记录现场。"))
                if runner.navigation_halted:
                    self.navigation_halted = True
                if self.stopped or details.get("stopped") or runner.navigation_halted:
                    summary["stopped"] = True
                    break
                if details.get("fatal"):
                    summary.update(error=details["error"], error_type=details["error_type"],
                                   fatal=True)
                    self.log("星海跑图合集遇到程序异常，已停止后续路线。")
                    break
                if not ok and not source.get("continue_on_failure", True):
                    break
                if not ok and index < len(prepared):
                    # A failed route can leave a death screen or another
                    # blocking page. Restore it before attempting another map.
                    ready = runner.main()
                    summary["routes"][-1]["recovered_to_main"] = bool(ready)
                    runner.event({"type": "collection_recovery", "success": bool(ready)})
                    write_json(directory / "result.json", summary)
                    if self.stopped:
                        summary["stopped"] = True
                        break
                    if not ready:
                        summary.update(error="游戏页面未恢复，跑图合集已停止。",
                                       error_type="NavigationError", interrupted=True)
                        self.log("游戏页面未恢复，跑图合集已停止。")
                        break
        except (NavigationError, OSError, KeyError, TypeError, AttributeError, ValueError, RuntimeError, cv2.error) as error:
            if self.stopped:
                summary["stopped"] = True
            else:
                summary.update(error=str(error), error_type=type(error).__name__)
                self.log("星海跑图合集停止："+str(error))
        finally:
            summary["completed"] = len(summary["routes"])
            summary["passed"] = sum(bool(item["success"]) for item in summary["routes"])
            summary["success"] = (summary["completed"] == summary["planned"]
                                  and summary["passed"] == summary["planned"])
            summary["finished_at"] = datetime.now().astimezone().isoformat()
            write_json(directory / "result.json", summary)
            self.log(f"星海跑图合集结束：完成 {summary['passed']}/{summary['planned']} 条。"
                     +"日志："+str(directory),
                     ui_text=f"星海跑图合集结束：完成 {summary['passed']}/{summary['planned']} 条。")
        return summary["success"]

    def run_single(self, settings, *, prepared, directory=None, refocus=True, force_teleport=False):
        inputs = None
        previous_inputs = self.navigation_inputs
        mode = settings.get("mode", "movement")
        result = {"success": False, "mode": mode}
        try:
            if os.environ.get("PI_MAANIKKI_DEV_MODE") != "1":
                raise NavigationError("请开启客户端设置中的开发模式后再测试路线。")
            if self.stopped:
                raise NavigationError("路线测试已停止。")
            model, spec = prepared
            source_document = model.document
            self.review_points = source_document["points"]
            skipped = []
            if mode == "movement":
                model, skipped = navigation_route(model)
            result.update(route=model.id, skipped_actions=skipped)
            actions = None
            if mode == "home" or any(point.action for point in model.points):
                from navigation.daily_route import RouteActions, preflight
                executor = {"home": "home", "bottle": "bottle"}.get(mode, "navigation")
                preflight(RESOURCE, model, executor)
            if not inputs_allowed():
                raise NavigationError("路线测试需要当前连接使用前台鼠标和键盘（Seize）。")
            self.directory = directory or ROOT / "logs" / "navigation" / (datetime.now().strftime("%Y%m%d-%H%M%S-%f")+"-route-test")
            self.directory.mkdir(parents=True)
            write_json(self.directory / "route.json", source_document)
            write_json(self.directory / "execution.json", model.document)
            result["navigation_profile"] = {"transit_radius_scale": spec.transit_radius_scale,
                                            "transit_pass_radius_scale": spec.transit_pass_radius_scale,
                                            "heading_tolerance_far": spec.heading_tolerance_far,
                                            "heading_tolerance_near": spec.heading_tolerance_near,
                                            "heading_position_slack": spec.heading_position_slack}
            if self.collection_entry:
                result["collection_entry"] = self.collection_entry
                self.event({"type": "collection_entry", **self.collection_entry})
            if skipped:
                self.log(f"导航模式跳过任务动作：{skipped}。",
                         ui_text=f"本轮验证路线导航，跳过 {len(skipped)} 个任务动作。")
            locator = Locator(spec)
            inputs = TestInputs(self.controller, lambda: self.stopped,
                                lambda: self.hit("MaaNikki_MainDetected"), bindings=self.game_keys)
            if refocus:
                self.phase("focus_game", "正在自动聚焦游戏。")
                focus_game(self, inputs)
            else:
                inputs.check()
            self.navigation_inputs = inputs
            self.event({"type": "window_ready", **inputs.focus_details()})
            teleporter = TestTeleporter(self, locator, self.event, inputs)
            if mode == "home" or any(point.action for point in model.points):
                # The trial has no daily quota: every bottle stop must run.
                executor = {"home": "home", "bottle": "bottle_test"}.get(mode, "navigation")
                card = SimpleNamespace(rule=SimpleNamespace(executor=executor), remaining=None)
                actions = RouteActions(self, RESOURCE, model, executor, card, inputs, self.event)
                actions.teleporter = teleporter
            if mode == "home":
                self.phase("ability_selection", "正在准备采星能力。")
                if not self.main() or not actions.fishing.configure():
                    raise NavigationError("家园测试的采星能力配置未完成。")
            self.phase("prepare", "正在准备路线："+str(model.document.get("title", model.id))+"。")
            if mode == "bottle":
                planned = sum(point.action == "bottle" for point in model.points)
                result["bottle_points_planned"] = planned
                self.log(f"漂流瓶全点位：共 {planned} 个拾取点，拾取成功后继续跑完整条路线。")
            if force_teleport:
                teleporter.transport(model.document["teleport"])
            else:
                teleporter.prepare(model)
            self.phase("follow", "正在检查第 1 点并沿路线行走。")
            navigator = create_navigator(self, teleporter, actions=actions, event=self.event)
            pose = navigator.follow(model)
            success = actions.complete() if mode == "home" else True
            result.update(success=success, final=pose.dict())
            if not success:
                result["error"] = "家园路线已走完，但有任务动作未确认完成。"
                self.log(result["error"])
        except (NavigationError, OSError, KeyError, TypeError, AttributeError, ValueError, RuntimeError, cv2.error) as error:
            result.update(success=False, stage=self.stage, error_type=type(error).__name__,
                          error=str(error), stopped=self.stopped,
                          fatal=isinstance(error, (TypeError, KeyError, AttributeError)))
            if inputs:
                result.update(inputs.focus_details())
            self.log("路线测试停止："+str(error))
            if self.directory and self.last_capture is not None:
                try:
                    from diagnostics import save_image
                    save_image(self.directory / "failure.png", self.last_capture)
                except (OSError, ValueError, cv2.error):
                    pass
        finally:
            if mode == "bottle":
                result.update(bottle_points_attempted=len(self.bottle_results),
                              bottles_collected=sum(item["success"] for item in self.bottle_results),
                              bottle_results=self.bottle_results)
            if inputs:
                try:
                    inputs.release()
                    self.event({"type": "inputs_released"})
                except (OSError, ValueError, RuntimeError) as error:
                    result.update(success=False, release_error=str(error))
                    self.log("路线测试无法确认按键已全部释放，请检查游戏状态。")
                    self.navigation_halted = True
            self.navigation_inputs = previous_inputs
            self.last_result = result
            if self.directory:
                try:
                    write_json(self.directory / "result.json", result)
                    status = "路线测试完成。" if result["success"] else "路线测试结束，请查看异常提示。"
                    if mode == "bottle":
                        status += (f"已尝试 {len(self.bottle_results)} 个漂流瓶点，"
                                   f"确认拾取 {result['bottles_collected']} 次。")
                    self.log(status+"日志："+str(self.directory), ui_text=status)
                except (OSError, ValueError):
                    self.log("路线测试日志保存失败。")
        return result["success"]
