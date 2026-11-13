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
from navigation.telemetry import record_route_telemetry
from navigation.teleport import Teleporter
from navigation.vision import Locator


NAVIGATION_ACTIONS = {"", "teleport", "wait"}
# Actions a user route may run. Others need task-specific setup from their daily task.
USER_ACTIONS = NAVIGATION_ACTIONS | {"collect", "insect", "fishing", "bottle", "macro", "key"}
USER_MACROS = {"starsea_fall", "zhaoxi_attack"}
TRIAL_FILE = ROOT / "config/route_trial.json"
TRIAL_PUBLISH_INTERVAL = 0.35


def build_trial_suggestions(waypoints: list[dict], start_offset: int = 0) -> list[dict]:
    """Build bounded coordinate & radius tuning suggestions from a trial telemetry summary."""
    suggestions = []
    for wp in waypoints:
        local_idx = int(wp.get("index", 0))
        route_idx = local_idx + start_offset
        outcome = wp.get("outcome") or "clean"
        if outcome == "clean":
            continue
        target = wp.get("target") or [0.0, 0.0]
        actual = wp.get("actual")
        cfg_rad = float(wp.get("configured_radius") or 1.0)
        precise = bool(wp.get("precise"))
        dx, dy = float(wp.get("dx") or 0.0), float(wp.get("dy") or 0.0)
        drift_dx, drift_dy = float(wp.get("drift_dx") or 0.0), float(wp.get("drift_dy") or 0.0)
        miss_dist = wp.get("closest_miss_dist")
        nudge_x = nudge_y = 0.0
        next_rad = cfg_rad
        reason = ""
        if outcome == "turnback":
            if precise and math.hypot(drift_dx, drift_dy) >= 0.12:
                nudge_x = round(max(-0.6, min(0.6, -drift_dx * 0.5)), 2)
                nudge_y = round(max(-0.6, min(0.6, -drift_dy * 0.5)), 2)
                reason = "刹车滑行折返，建议向进入方向微调点位"
            else:
                cand = max(cfg_rad + 0.35, float(miss_dist) * 1.15 if isinstance(miss_dist, (int, float)) else cfg_rad * 1.25)
                next_rad = round(min(max(3.0, cfg_rad + 0.2), cand, cfg_rad * 1.4), 2)
                if math.hypot(dx, dy) >= 0.2 and actual is not None:
                    nudge_x = round(max(-0.6, min(0.6, dx * 0.4)), 2)
                    nudge_y = round(max(-0.6, min(0.6, dy * 0.4)), 2)
                reason = "途经擦边折返，建议放宽到达范围或靠拢实测轨迹"
        elif outcome in ("recovered", "failed"):
            if actual is not None and math.hypot(dx, dy) >= 0.15 and math.hypot(dx, dy) <= max(cfg_rad * 2.5, 4.0):
                mag = math.hypot(dx, dy)
                scale = min(0.6, mag * 0.35)
                nudge_x = round(-dx / mag * scale, 2)
                nudge_y = round(-dy / mag * scale, 2)
            if not precise:
                next_rad = round(min(3.5, cfg_rad + 0.35), 2)
            reason = "此处发生卡地形或脱困，建议微调避开受阻侧"
        elif outcome == "action_missed":
            if actual is not None and math.hypot(dx, dy) >= 0.15 and math.hypot(dx, dy) <= 2.5:
                nudge_x = round(max(-0.5, min(0.5, -dx * 0.4)), 2)
                nudge_y = round(max(-0.5, min(0.5, -dy * 0.4)), 2)
            next_rad = round(max(0.3, min(cfg_rad, 0.5)), 2)
            reason = "交互或采集未确认，建议收紧范围或调整站位"
        if abs(nudge_x) >= 0.02 or abs(nudge_y) >= 0.02 or abs(next_rad - cfg_rad) >= 0.05:
            suggestions.append({
                "index": route_idx,
                "outcome": outcome,
                "reason": reason,
                "x": round(float(target[0]) + nudge_x, 2),
                "y": round(float(target[1]) + nudge_y, 2),
                "radius": next_rad,
                "actual": actual,
            })
    return suggestions


def read_test_document(resource: Path, settings: dict):
    """Read a single file snapshot, including optional collection manifests."""
    filename, name = settings.get("file", ""), settings.get("route", "")
    if isinstance(filename, str) and not name and re.fullmatch(r"[a-zA-Z0-9_-]+", filename.strip()):
        name = filename.strip()
        filename = ""
    if filename:
        if not isinstance(filename, str):
            raise NavigationError("路线文件路径无效。")
        path = Path(filename).expanduser()
        if not path.is_absolute():
            path = ROOT / path
        if not path.is_file() and path.suffix.lower() == ".json":
            for candidate in (
                ROOT / "config/routes" / path.name,
                resource / "routes/gingerbread" / path.name,
                resource / "routes/daily" / path.name,
            ):
                if candidate.is_file():
                    path = candidate
                    break
    else:
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
            raise NavigationError("请先选择要运行的路线。")
        path = resource / "routes" / "daily" / (name+".json")
        if not path.is_file():
            path = resource / "routes" / "gingerbread" / (name+".json")
        if not path.is_file():
            path = ROOT / "config/routes" / (name+".json")
    if path.suffix.lower() != ".json" or not path.is_file() or path.stat().st_size > 1024*1024:
        raise NavigationError("路线文件不存在、不是 JSON 或超过 1 MB。")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def resolve_multi_route_document(resource: Path, settings: dict):
    """Resolve single or multi-selected routes into a route document or route_collection."""
    raw_files = settings.get("files")
    items: list[str] = []
    if isinstance(raw_files, list):
        items = [str(x).strip() for x in raw_files if str(x).strip()]
    elif isinstance(raw_files, str) and raw_files.strip():
        items = [p.strip() for p in re.split(r"[,;\n]+", raw_files) if p.strip()]
    else:
        f_val = settings.get("file", "")
        if isinstance(f_val, str) and any(sep in f_val for sep in (",", ";", "\n")):
            items = [p.strip() for p in re.split(r"[,;\n]+", f_val) if p.strip()]

    if not items:
        return read_test_document(resource, settings)
    if len(items) == 1:
        return read_test_document(resource, {**settings, "file": items[0], "route": ""})

    routes_list: list[dict] = []
    seen_ids: set[str] = set()
    for token in items:
        doc = read_test_document(resource, {"file": token})
        if isinstance(doc, dict) and doc.get("kind") == "route_collection":
            for entry in doc.get("routes") or []:
                if isinstance(entry, dict) and isinstance(entry.get("route"), str):
                    rid = entry["route"].strip()
                    if rid and rid not in seen_ids:
                        seen_ids.add(rid)
                        routes_list.append(dict(entry))
        elif isinstance(doc, dict) and isinstance(doc.get("id"), str):
            rid = doc["id"].strip()
            if rid and rid not in seen_ids:
                seen_ids.add(rid)
                entry_obj = {"route": rid, "title": str(doc.get("title") or rid)}
                if not re.fullmatch(r"[a-zA-Z0-9_-]+", token):
                    entry_obj["file"] = token
                routes_list.append(entry_obj)
    if not routes_list:
        raise NavigationError("请先选择至少一条要执行的采集路线。")
    return {
        "schema_version": 1,
        "kind": "route_collection",
        "id": "multi_selected_routes",
        "title": f"多选采集队列（共 {len(routes_list)} 条）",
        "continue_on_failure": True,
        "routes": routes_list,
    }


def from_point(document, start):
    """Trial a route from a chosen point; the start teleport no longer applies."""
    if not start:
        return document
    points = document.get("points") if isinstance(document, dict) else None
    if type(start) is not int or not isinstance(points, list) or not 0 < start < len(points):
        raise NavigationError("试跑的起始点无效。")
    document = {**document, "points": points[start:]}
    document.pop("teleport", None)
    return document


def test_document(resource: Path, settings: dict, *, document=None):
    """Validate one fixed snapshot before connecting or sending input."""
    mode = settings.get("mode", "movement")
    if mode not in {"movement", "home", "bottle", "full"}:
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
        if action == "wait" and not number(param.get("seconds"), 0, 180):
            raise NavigationError(f"第 {index+1} 点的等待时间须为 0–180 秒。")
        if mode == "full" and (action not in USER_ACTIONS
                               or action == "macro" and param.get("id") not in USER_MACROS):
            raise NavigationError(f"第 {index+1} 点的动作不能用于自定义路线。")
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
    dev_only = True
    label = "路线测试"
    default_mode = "movement"

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
        self.telemetry_events = []
        self.start_offset = 0
        self.current_local_index = 0
        self.trial_document = None
        self.trial_published_at = 0.0

    def publish_trial(self, *, force=False):
        if self.trial_document is None:
            return
        now = time.monotonic()
        if not force and now - self.trial_published_at < TRIAL_PUBLISH_INTERVAL:
            return
        self.trial_published_at = now
        try:
            write_json(TRIAL_FILE, self.trial_document)
        except OSError:
            pass

    def capture(self):
        frame = super().capture()
        if frame is not None:
            self.last_capture = frame
        return frame

    def event(self, value):
        self.telemetry_events.append(value)
        if value.get("type") == "pose":
            self.last_pose = value
            if self.trial_document is not None and value.get("position_reliable", True):
                self.trial_document["pose"] = {
                    "x": round(float(value["x"]), 2),
                    "y": round(float(value["y"]), 2),
                    "heading": value.get("heading"),
                }
                self.trial_document["current_index"] = self.start_offset + self.current_local_index
                self.publish_trial()
        if value.get("type") == "waypoint":
            self.current_local_index = int(value["index"]) + 1
            if self.trial_document is not None:
                self.trial_document["current_index"] = self.start_offset + self.current_local_index
                self.trial_document["message"] = f"已到达第 {self.start_offset + int(value['index']) + 1} 点"
                self.publish_trial(force=True)
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
            self.progress_message = f"{self.label}：已到达第 {value['index']+1} 点，共 {value['total']+1} 点。"
        # UI log callbacks can block on a pipeline RPC. Defer them while
        # movement is held so logging cannot extend a pulse past its waypoint.
        inputs = self.navigation_inputs
        if self.progress_message and (inputs is None or not inputs.held and not inputs.buttons):
            self.log(self.progress_message)
            self.progress_message = None

    def phase(self, stage, message):
        self.stage = stage
        self.event({"type": "phase", "phase": stage})
        if self.trial_document is not None:
            self.trial_document["state"] = "running"
            self.trial_document["message"] = message
            self.publish_trial(force=True)
        self.log(f"{self.label}：{message}")

    def prepare_route(self, settings, actions, inputs):
        """Task-specific setup after arriving at the route start, before walking."""

    def run_test(self, settings):
        try:
            if self.dev_only and os.environ.get("PI_MAANIKKI_DEV_MODE") != "1":
                raise NavigationError("请开启客户端设置中的开发模式后再测试路线。")
            settings = {"mode": self.default_mode, **settings}
            self.start_offset = int(settings.get("start", 0) or 0)
            source = resolve_multi_route_document(RESOURCE, settings)
            if isinstance(source, dict) and source.get("kind") == "route_collection":
                return self.run_collection(settings, source)
            source = from_point(source, self.start_offset)
            prepared = test_document(RESOURCE, settings, document=source)
        except (NavigationError, OSError, KeyError, TypeError, ValueError) as error:
            self.log(f"{self.label}停止：{error}")
            return False
        return self.run_single(settings, prepared=prepared)

    def run_collection(self, settings, source):
        """Freeze every route before input; record each run independently."""
        mode = settings.get("mode", self.default_mode)
        if self.dev_only and mode != "movement":
            raise NavigationError("路线合集请使用“路线导航”模式。")
        if mode not in {"movement", "full"}:
            raise NavigationError("路线合集模式无效。")
        entries = source.get("routes")
        if (source.get("schema_version") != 1
                or (self.dev_only and source.get("map") != "starsea")
                or not isinstance(source.get("id"), str) or not source["id"].strip()
                or not isinstance(entries, list) or not 1 <= len(entries) <= 128
                or type(source.get("continue_on_failure", True)) is not bool):
            raise NavigationError("路线合集格式无效。")
        prepared = []
        names = set()
        for index, entry in enumerate(entries, 1):
            if not isinstance(entry, dict) or not isinstance(entry.get("route"), str):
                raise NavigationError(f"合集第 {index} 条需要指定路线名。")
            name = entry["route"]
            if name in names:
                raise NavigationError("路线合集中存在重复路线："+name)
            entry_file = entry.get("file", "")
            if not entry_file and not self.dev_only:
                user_candidate = ROOT / "config/routes" / f"{name}.json"
                if user_candidate.is_file():
                    entry_file = str(user_candidate)
            child_settings = {"file": entry_file, "mode": mode} if entry_file else {"route": name, "mode": mode}
            model, spec = test_document(RESOURCE, child_settings)
            if self.dev_only and (model.map_id != "starsea" or not model.document.get("teleport")):
                raise NavigationError("合集路线需要星海地图和起始传送点："+name)
            execution = navigation_route(model)[0] if mode == "movement" else model
            from navigation.daily_route import preflight
            preflight(RESOURCE, execution, "navigation")
            title = entry.get("title") or model.document.get("title") or name
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
        collection_title = str(source.get("title") or "路线清单").strip()
        self.log(f"{collection_title}：共 {len(prepared)} 条路线，开始逐条运行。")
        try:
            consecutive_empty_routes = 0
            session_total_picked = 0
            for index, (model, spec, title) in enumerate(prepared, 1):
                if self.stopped:
                    summary["stopped"] = True
                    break
                self.log(f"{collection_title} {index}/{len(prepared)}：{title}。")
                runner = type(self)(self.context)
                runner.collection_entry = {"collection": source["id"], "index": index,
                                           "total": len(prepared), "title": title}
                child = directory / f"route-{index:02d}"
                started = time.monotonic()
                child_run_settings = {
                    "mode": mode,
                    "empty_streak": consecutive_empty_routes,
                    **({k: settings[k] for k in ("adaptive_exit", "quiet_seconds", "initial_wait_seconds",
                                                 "gingerbread_summon", "gingerbread_ability_plan")
                        if k in settings}),
                }
                ok = runner.run_single(child_run_settings, prepared=(model, spec),
                                       directory=child, refocus=index == 1,
                                       force_teleport=bool(model.document.get("teleport")))
                details = runner.last_result or {"success": False, "error": "执行结果未保存"}
                gb_info = details.get("gingerbread") if isinstance(details.get("gingerbread"), dict) else None
                if gb_info is not None:
                    picked_now = int(gb_info.get("total_picked") or 0)
                    session_total_picked += picked_now
                    if int(gb_info.get("expected_0_15m") or 0) >= 4 and picked_now == 0:
                        consecutive_empty_routes += 1
                        if consecutive_empty_routes == 1 and session_total_picked == 0:
                            hint = "提示：首条路线未读取到新材料拾取提示，请确认已召唤小姜饼人随行，或该区域材料尚在冷却中。"
                            self.log(hint, ui_text=hint)
                        elif consecutive_empty_routes == 3 and session_total_picked == 0:
                            hint = "注意：已连续 3 条路线未读取到拾取提示，请确认右下角已切换并召唤小姜饼人随行。"
                            self.log(hint, ui_text=hint)
                    elif picked_now > 0:
                        consecutive_empty_routes = 0
                summary["routes"].append({"index": index, "route": model.id, "title": title,
                                          "directory": child.name,
                                          "active_seconds": round(time.monotonic()-started, 3),
                                          **details})
                write_json(directory / "result.json", summary)
                self.log(f"{collection_title} {index}/{len(prepared)}：{title}，"+("完成。" if ok else "中断，已记录现场。"))
                if runner.navigation_halted:
                    self.navigation_halted = True
                if self.stopped or details.get("stopped") or runner.navigation_halted:
                    summary["stopped"] = True
                    break
                if details.get("fatal"):
                    summary.update(error=details["error"], error_type=details["error_type"],
                                   fatal=True)
                    self.log(f"{collection_title}遇到程序异常，已停止后续路线。")
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
                        summary.update(error=f"游戏页面未恢复，{collection_title}已停止。",
                                       error_type="NavigationError", interrupted=True)
                        self.log(f"游戏页面未恢复，{collection_title}已停止。")
                        break
        except (NavigationError, OSError, KeyError, TypeError, AttributeError, ValueError, RuntimeError, cv2.error) as error:
            if self.stopped:
                summary["stopped"] = True
            else:
                summary.update(error=str(error), error_type=type(error).__name__)
                self.log(f"{collection_title}停止：{error}")
        finally:
            summary["completed"] = len(summary["routes"])
            summary["passed"] = sum(bool(item["success"]) for item in summary["routes"])
            summary["success"] = (summary["completed"] == summary["planned"]
                                  and summary["passed"] == summary["planned"])
            summary["finished_at"] = datetime.now().astimezone().isoformat()
            gb_list = [item["gingerbread"] for item in summary["routes"] if isinstance(item.get("gingerbread"), dict)]
            if gb_list:
                tot_picked = sum(int(g.get("total_picked", 0)) for g in gb_list)
                tot_exp15 = sum(int(g.get("expected_0_15m", 0)) for g in gb_list)
                tot_cfg_w = round(sum(float(g.get("configured_wait_total_s", 0.0)) for g in gb_list), 1)
                tot_eff_w = round(sum(float(g.get("effective_pickup_finish_total_s", 0.0)) for g in gb_list), 1)
                tot_act_w = round(sum(float(g.get("actual_waited_total_s", 0.0)) for g in gb_list), 1)
                short_wps = sum(int(g.get("too_short_waypoints", 0)) for g in gb_list)
                long_wps = sum(int(g.get("too_long_waypoints", 0)) for g in gb_list)
                merged_items: dict[str, int] = {}
                for g in gb_list:
                    for nm, cnt in (g.get("picked_by_name") or {}).items():
                        merged_items[nm] = merged_items.get(nm, 0) + int(cnt)
                summary["gingerbread_collection_summary"] = {
                    "routes_count": len(gb_list),
                    "total_picked": tot_picked,
                    "expected_0_15m": tot_exp15,
                    "configured_wait_total_s": tot_cfg_w,
                    "effective_pickup_finish_total_s": tot_eff_w,
                    "actual_waited_total_s": tot_act_w,
                    "too_short_waypoints": short_wps,
                    "too_long_waypoints": long_wps,
                    "picked_by_name": merged_items,
                }
                top_items_str = "、".join(
                    f"{k}×{v}" for k, v in sorted(merged_items.items(), key=lambda kv: -kv[1])[:10]
                ) or "无"
                self.log(
                    f"【姜饼人采集汇总】共 {len(gb_list)} 条路线：实测拾取 {tot_picked} 个（地图15m预计 {tot_exp15} 个），"
                    f"有效拾取耗时 {tot_eff_w}s / 实际驻留 {tot_act_w}s / 路线原设 {tot_cfg_w}s "
                    f"（原设偏短 {short_wps} 处，保守富余 {long_wps} 处）。明细：{top_items_str}"
                )
            write_json(directory / "result.json", summary)
            self.log(f"{collection_title}结束：完成 {summary['passed']}/{summary['planned']} 条。"
                     +"日志："+str(directory),
                     ui_text=f"{collection_title}结束：完成 {summary['passed']}/{summary['planned']} 条。")
        return summary["success"]

    def run_single(self, settings, *, prepared, directory=None, refocus=True, force_teleport=False):
        inputs = model = navigator = actions = None
        previous_inputs = self.navigation_inputs
        self.telemetry_events = []
        self.current_local_index = 0
        mode = settings.get("mode", self.default_mode)
        result = {"success": False, "mode": mode}
        try:
            if self.dev_only and os.environ.get("PI_MAANIKKI_DEV_MODE") != "1":
                raise NavigationError("请开启客户端设置中的开发模式后再测试路线。")
            if self.stopped:
                raise NavigationError(f"{self.label}已停止。")
            model, spec = prepared
            source_document = model.document
            self.review_points = source_document["points"]
            self.trial_document = {
                "schema_version": 1,
                "started_at": datetime.now().astimezone().isoformat(),
                "state": "starting",
                "mode": mode,
                "map": model.map_id,
                "route": model.id,
                "start_index": self.start_offset,
                "current_index": self.start_offset,
                "pose": None,
                "message": "正在准备…",
                "telemetry": None,
                "suggestions": [],
            }
            self.publish_trial(force=True)
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
                raise NavigationError(f"{self.label}需要当前连接使用前台鼠标和键盘（Seize）。")
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
                actions.gb_config = {
                    k: settings[k]
                    for k in ("adaptive_exit", "quiet_seconds", "initial_wait_seconds", "empty_streak")
                    if k in settings
                }
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
            self.prepare_route(settings, actions, inputs)
            self.phase("follow", "正在检查第 1 点并沿路线行走。")
            navigator = create_navigator(self, teleporter, actions=actions, event=self.event)
            pose = navigator.follow(model)
            success = actions.complete() if mode == "home" else True
            result.update(success=success, final=pose.dict())
            if not success:
                result["error"] = "家园路线已走完，但有任务动作未确认完成。"
                self.log(result["error"])
        except (NavigationError, OSError, KeyError, TypeError, AttributeError, ValueError, RuntimeError, cv2.error) as error:
            self.telemetry_events.append({"type": "stopped", "stage": self.stage,
                                          "error_type": type(error).__name__, "reason": str(error)})
            result.update(success=False, stage=self.stage, error_type=type(error).__name__,
                          error=str(error), stopped=self.stopped,
                          fatal=isinstance(error, (TypeError, KeyError, AttributeError)))
            if inputs:
                result.update(inputs.focus_details())
            self.log(f"{self.label}停止：{error}")
            if self.directory and self.last_capture is not None:
                try:
                    from diagnostics import save_image
                    save_image(self.directory / "failure.png", self.last_capture)
                except (OSError, ValueError, cv2.error):
                    pass
        finally:
            active_route = getattr(navigator, "active_route", None) or model
            if actions is not None and hasattr(actions, "gingerbread_summary"):
                gb_summary = actions.gingerbread_summary()
                if gb_summary and gb_summary.get("waypoints_analyzed", 0) > 0:
                    from navigation.gingerbread_pickup import save_gingerbread_telemetry
                    save_gingerbread_telemetry(ROOT, gb_summary, self.directory)
                    result["gingerbread"] = gb_summary
                    self.log(f"【姜饼人实测推断】{gb_summary['overall_diagnosis']}")
            if active_route is not None and not self.stopped:
                telemetry = record_route_telemetry(
                    ROOT, active_route, self.telemetry_events,
                    executor=actions.executor if actions else mode,
                    mode=f"test_{mode}", success=bool(result.get("success")), stage=self.stage)
                result["telemetry"] = telemetry
                if self.trial_document is not None:
                    shifted_wps = [
                        {**wp, "index": int(wp.get("index", 0)) + self.start_offset}
                        for wp in telemetry.get("waypoints", [])
                    ]
                    self.trial_document["telemetry"] = {
                        **telemetry,
                        "failed_index": (
                            int(telemetry["failed_index"]) + self.start_offset
                            if telemetry.get("failed_index") is not None else None
                        ),
                        "waypoints": shifted_wps,
                    }
                    self.trial_document["suggestions"] = build_trial_suggestions(
                        telemetry.get("waypoints", []), self.start_offset)
            if self.trial_document is not None:
                self.trial_document["state"] = "finished" if result.get("success") else "failed"
                if result.get("error"):
                    self.trial_document["message"] = str(result["error"])
                elif result.get("success"):
                    self.trial_document["message"] = "试跑完成"
                self.publish_trial(force=True)
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
                    self.log(f"{self.label}无法确认按键已全部释放，请检查游戏状态。")
                    self.navigation_halted = True
            self.navigation_inputs = previous_inputs
            self.last_result = result
            if self.directory:
                try:
                    write_json(self.directory / "result.json", result)
                    status = f"{self.label}完成。" if result["success"] else f"{self.label}结束，请查看异常提示。"
                    if mode == "bottle":
                        status += (f"已尝试 {len(self.bottle_results)} 个漂流瓶点，"
                                   f"确认拾取 {result['bottles_collected']} 次。")
                    self.log(status+"日志："+str(self.directory), ui_text=status)
                except (OSError, ValueError):
                    self.log(f"{self.label}日志保存失败。")
        return result["success"]
