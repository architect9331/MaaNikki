"""Manual wardrobe read entry point."""
from datetime import datetime

from maa.custom_action import CustomAction

from daily.runtime import Runtime
from daily.settings import ROOT
from registration import agent_action
from task_control import TaskStopped
from wardrobe.scanner import WardrobeScanner


@agent_action("nikki.wardrobe_read")
class WardrobeReadAction(CustomAction):
    def run(self, context, argv):
        rt = Runtime(context)
        folder = ROOT / "logs/wardrobe" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        folder.mkdir(parents=True, exist_ok=True)
        scanner = None
        try:
            rt.log("衣柜读取开始。")
            scanner = WardrobeScanner(rt, folder)
            complete = scanner.run()
            count = scanner.snapshot["scanned_count"]
            rt.log(f"衣柜读取{'完成' if complete else '结束'}：{count} 件，{scanner.snapshot['completed_parts']} 个部位。")
            if not complete:
                rt.log(scanner.snapshot["reason"])
            _, finished = rt.finish(argv.node_name)
            return complete and finished
        except TaskStopped:
            if scanner is not None:
                try:
                    scanner.save("用户停止，本次读取进度已保存")
                except (OSError, ValueError) as error:
                    rt.log(f"衣柜账号进度保存失败：{error}。", detail=True)
            raise
        except Exception as error:
            if scanner is not None:
                try:
                    scanner.save(str(error))
                except (OSError, ValueError) as save_error:
                    rt.log(f"衣柜账号进度保存失败：{save_error}。", detail=True)
            if not rt.stopped:
                rt.diagnostic("wardrobe_read", str(error))
            rt.log(f"衣柜读取暂停：{error}。")
            return False
