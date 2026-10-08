from __future__ import annotations

import json

from registration import agent_action
from maa.custom_action import CustomAction

from daily.runtime import Runtime


@agent_action("nikki.game_key")
class GameKeyAction(CustomAction):
    def run(self, context, argv: CustomAction.RunArg) -> bool:
        runtime = Runtime(context)
        try:
            param = json.loads(argv.custom_action_param)
            name = param["name"]
            seconds = param.get("seconds")
            return runtime.hold(name, seconds) if seconds is not None else runtime.key(name, 0)
        except (ValueError, TypeError, KeyError) as error:
            runtime.log(f"游戏按键未执行：{error}")
            return False
