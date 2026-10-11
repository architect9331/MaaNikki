"""Foreground fishing sessions shared by a current-point task and assistance."""
from __future__ import annotations

from datetime import datetime
import json
import math
import re
from types import SimpleNamespace

from .runtime import RESOURCE, parameters
from .settings import ROOT, foreground_inputs
from .startup import focus_game
from navigation.ability import ability_matches
from navigation.controller import ForegroundInput
from navigation.fishing import Fishing, FishingState, cast_count
from navigation.models import NavigationError, load_route, maps


def selected_fishing_route(resource, name):
    if not isinstance(name, str) or not re.fullmatch(r"fishing_[a-z0-9_]+", name):
        raise NavigationError("请选择已登记的钓鱼路线。")
    route = load_route(resource, name)
    if route is None or route.document.get("schema_version") != 3 or route.id != name:
        raise NavigationError("钓鱼路线缺失或格式无效。")
    from navigation.daily_route import preflight
    preflight(resource, route, "fishing")
    spec = maps(resource).get(route.map_id)
    checkpoint = route.document.get("teleport", {})
    if (spec is None or route.map_id != "miraland"
            or any(point.action not in {"", "fishing", "teleport", "wait"} for point in route.points)
            or any(not checkpoint.get(key) for key in ("name", "province", "region"))):
        raise NavigationError("钓鱼路线的地图、传送点或动作配置无效。")
    locations = [(point.x, point.y) for point in route.points]
    locations.append((checkpoint.get("x"), checkpoint.get("y")))
    for point in route.points:
        if point.action == "teleport":
            target = point.params["checkpoint"]
            if any(not target.get(key) for key in ("name", "province", "region")):
                raise NavigationError("路线内传送点配置无效。")
            locations.append((target.get("x"), target.get("y")))
        if point.action == "wait":
            seconds = (point.params or {}).get("seconds")
            if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 <= seconds <= 60:
                raise NavigationError("钓鱼路线等待时间无效。")
    if (any(type(value) not in (int, float) or not math.isfinite(value) for xy in locations for value in xy)
            or any(not 0 <= x < spec.width or not 0 <= y < spec.height for x, y in locations)
            or any(not math.isfinite(point.radius) or point.radius <= 0 for point in route.points)
            or not math.isfinite(route.start_radius) or not 0 < route.start_radius <= 30
            or not math.isfinite(route.timeout) or route.timeout <= 0):
        raise NavigationError("钓鱼路线坐标或导航参数无效。")
    return route


def route_fishing(runtime):
    name = parameters(runtime.context, "MaaNikki_FishingRoute_Selection").get("value")
    route = selected_fishing_route(RESOURCE, name)
    cast_count(runtime.setting("fishing").get("route_cast_count"), None)
    runtime.log(f"开始路线钓鱼：{route.document.get('title', name)}。")
    card = SimpleNamespace(rule=SimpleNamespace(executor="fishing"), remaining=None)
    result = runtime.navigate(name, "fishing", card)
    if runtime.stopped:
        return False
    return bool(result and runtime.main())


def fishing_opportunity(runtime, inputs, frame):
    actions = SimpleNamespace(rt=runtime, inputs=inputs, event=lambda value: None)
    state = Fishing(actions, None).state(image=frame)
    return state in (FishingState.FINISH, FishingState.STRIKE,
                     FishingState.PULL_LINE, FishingState.REEL_IN)


def run_fishing(runtime, inputs, *, passive=False, max_cast_count=None, use_rhythms=False):
    events, previous = [], runtime.navigation_inputs
    fishing = Fishing(SimpleNamespace(rt=runtime, inputs=inputs, event=events.append,
        ability=lambda node: ability_matches(runtime, node)), None if passive else "fish")
    runtime.navigation_inputs = inputs
    result = {"passive": passive, "success": False}
    try:
        result["success"] = fishing.run(passive=passive, max_cast_count=max_cast_count,
                                         use_rhythms=use_rhythms)
        runtime.log("钓鱼完成："+", ".join(f"{name}×{count}" for name, count in fishing.materials.items())
                    if fishing.materials else "本次未识别到钓获。")
        return result["success"]
    except NavigationError as error:
        result["reason"] = str(error)
        raise
    finally:
        runtime.navigation_inputs = previous
        result.update(kind=fishing.kind, materials=fishing.materials, events=events)
        try:
            folder = ROOT / "logs/fishing"
            folder.mkdir(parents=True, exist_ok=True)
            filename = datetime.now().strftime("%Y%m%d-%H%M%S-%f")+".json"
            (folder / filename).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            runtime.log("钓鱼记录未能保存。")


def current_fishing(runtime):
    count = cast_count(parameters(runtime.context, "MaaNikki_Fishing_CastCount").get("value"), 3)
    if not foreground_inputs():
        runtime.log("自动钓鱼需要前台鼠标和键盘输入，请将两者设置为 Seize。")
        return False
    inputs = ForegroundInput(runtime.controller, lambda: runtime.stopped, bindings=runtime.game_keys)
    previous = runtime.navigation_inputs
    try:
        if previous:
            previous.check()
        else:
            focus_game(runtime, inputs)
        runtime.navigation_inputs = inputs
        if not runtime.main():
            return False
        return run_fishing(runtime, inputs, max_cast_count=count,
                           use_rhythms=runtime.setting("fishing")["use_rhythms"])
    finally:
        runtime.navigation_inputs = previous
        inputs.release()
