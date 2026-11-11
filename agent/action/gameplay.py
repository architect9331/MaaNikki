from __future__ import annotations

import cv2
from client_config import parse_custom_param
from registration import agent_action
from task_control import TaskStopped
from maa.custom_action import CustomAction
from daily.gameplay import Gameplay
from daily.runtime import Executors


@agent_action("nikki.ui_click")
class UIClickAction(CustomAction):
    """Pipeline clicks and Python workflows use the same input handling."""

    def run(self, context, argv):
        runtime = Executors(context)
        try:
            options = parse_custom_param(argv.custom_action_param)
            target = options.get("target", True)
            resumed = runtime.check_focus()
            if target is True and resumed and not options.get("ready"):
                return runtime.click_template(argv.node_name, wait_seconds=0)
            target = list(argv.box) if target is True else target
            ready = options.get("ready")
            if ready:
                # Retries must recognize a fresh box, not argv's stale box.
                return runtime.click_template(argv.node_name, ready=ready, wait_seconds=0)
            return runtime.action("Click", target=target)
        except TaskStopped:
            raise
        except (ValueError, TypeError, KeyError, RuntimeError) as error:
            runtime.log(f"界面点击失败：{error}。")
            return False


@agent_action("nikki.gameplay")
class GameplayAction(CustomAction):
    def run(self, context, argv):
        runtime = Executors(context)
        try:
            options = parse_custom_param(argv.custom_action_param)
            kind = options["kind"]
            game = Gameplay(runtime)
            if kind == "weekly_shop":
                from daily.shop import WeeklyShop
                return WeeklyShop(runtime).run()
            if kind == "monthly_exchange":
                from daily.monthly_exchange import MonthlyExchange
                return MonthlyExchange(runtime).run()
            if kind == "mailbox":
                from daily.mailbox import Mailbox
                return Mailbox(runtime).run()
            if kind == "home":
                from daily.home import Home
                return Home(runtime).run()
            if kind == "fishing":
                from daily.fishing import current_fishing
                return current_fishing(runtime)
            if kind == "main":
                return runtime.main()
            if kind in ("dig", "monthly", "lookbook"):
                return getattr(game, kind)()
            if kind in ("jihua", "bless", "monster"):
                return game.realm(kind, allow_exhausted=True,
                                  return_page=options.get("return_page", "main"))
            return False
        except TaskStopped:
            raise
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, cv2.error) as error:
            runtime.log(f"任务未完成：{error}。")
            if not runtime.stopped:
                runtime.main()
            return False
