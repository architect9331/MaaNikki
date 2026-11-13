from __future__ import annotations

from client_config import parse_custom_param
from registration import agent_action
from maa.custom_action import CustomAction

from daily.runtime import Runtime


# Read by the desktop client after the task; keep in step with gameKeysReadService.
RESULT_FILE = "config/game_keys_read.json"


@agent_action("nikki.game_key")
class GameKeyAction(CustomAction):
    def run(self, context, argv: CustomAction.RunArg) -> bool:
        runtime = Runtime(context)
        try:
            param = parse_custom_param(argv.custom_action_param)
            name = param["name"]
            seconds = param.get("seconds")
            return runtime.hold(name, seconds) if seconds is not None else runtime.key(name, 0)
        except (ValueError, TypeError, KeyError) as error:
            runtime.log(f"游戏按键未执行：{error}")
            return False


@agent_action("nikki.game_keys_read")
class GameKeysReadAction(CustomAction):
    """Read the in-game key settings page for the client to review and apply."""

    def run(self, context, argv: CustomAction.RunArg) -> bool:
        from daily.settings import ROOT
        from file_io import write_json
        from game_keys_reader import GameKeysReader
        from task_control import TaskStopped

        runtime = Runtime(context)
        reader = GameKeysReader(runtime)
        written = False
        try:
            runtime.log("开始读取游戏键位。")
            complete = reader.open() and reader.scan()
            report = reader.report(complete)
            write_json(ROOT / RESULT_FILE, report)
            written = True
            runtime.log(f"游戏键位读取{'完成' if complete else '结束'}：识别 {len(report['values'])} 项，"
                        f"{len(report['warnings'])} 条提示。")
            return complete and bool(report["values"])
        except TaskStopped:
            raise
        except Exception as error:
            if not runtime.stopped:
                runtime.diagnostic("game_keys_read", str(error))
                runtime.log(f"游戏键位读取未完成：{error}。")
            if not written and reader.found:
                try:
                    write_json(ROOT / RESULT_FILE, reader.report(False))
                except (OSError, ValueError):
                    pass
            return False
        finally:
            # Leave the settings pages even after a failed read.
            if not runtime.stopped:
                runtime.main()
