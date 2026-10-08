from __future__ import annotations

import json
import cv2
from registration import agent_action
from maa.custom_action import CustomAction
from daily.gameplay import Gameplay
from daily.runtime import Executors


@agent_action("nikki.ui_click")
class UIClickAction(CustomAction):
    """Pipeline clicks and Python workflows use the same input handling."""

    def run(self, context, argv):
        runtime = Executors(context)
        try:
            options = argv.custom_action_param
            options = json.loads(options or "{}") if isinstance(options, str) else options
            options = options or {}
            target = options.get("target", True)
            target = list(argv.box) if target is True else target
            ready = options.get("ready")
            if ready:
                # Retries must recognize a fresh box, not argv's stale box.
                return runtime.click_template(argv.node_name, ready=ready, wait_seconds=0)
            return runtime.action("Click", target=target)
        except (ValueError, TypeError, KeyError, RuntimeError) as error:
            runtime.log(f"界面点击失败：{error}。")
            return False


@agent_action("nikki.gameplay")
class GameplayAction(CustomAction):
    def run(self, context, argv):
        runtime = Executors(context)
        try:
            options = argv.custom_action_param
            options = json.loads(options or "{}") if isinstance(options, str) else options
            kind = options["kind"]
            game = Gameplay(runtime)
            if kind == "main":
                return runtime.main()
            if kind in ("dig", "monthly", "lookbook"):
                return getattr(game, kind)()
            if kind in ("jihua", "bless", "monster"):
                return game.realm(kind, allow_exhausted=True,
                                  return_page=options.get("return_page", "main"))
            return False
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, cv2.error) as error:
            runtime.log(f"任务未完成：{error}。")
            if not runtime.stopped:
                runtime.main()
            return False
