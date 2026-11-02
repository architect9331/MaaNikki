"""Foreground fishing sessions shared by a current-point task and assistance."""
from __future__ import annotations

from file_io import write_json

from datetime import datetime
from types import SimpleNamespace

from .runtime import parameters
from .settings import ROOT, foreground_inputs
from .startup import focus_game
from navigation.ability import ability_matches
from navigation.controller import ForegroundInput
from navigation.fishing import Fishing, FishingState, cast_count
from navigation.models import NavigationError


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
            write_json(folder / filename, result)
        except (OSError, ValueError):
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
