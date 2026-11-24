"""Manual wardrobe, renewal (服装焕新), and backpack read entry points."""
from datetime import datetime

from maa.custom_action import CustomAction

from account_state import AccountState
from collection.backpack import rescan_backpack
from collection.infinite_heart import InfiniteHeartScanner
from collection.material_pursuit import load_catalogs, record_material_history
from daily.runtime import Runtime, parameters
from daily.settings import ROOT
from registration import agent_action
from task_control import TaskStopped
from wardrobe.renewal_scanner import RenewalScanner
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
            if not rt.stopped:
                rt.diagnostic("wardrobe_read", str(error))
            if scanner is not None:
                try:
                    scanner.save(str(error))
                except (OSError, ValueError) as save_error:
                    rt.log(f"衣柜账号进度保存失败：{save_error}。", detail=True)
            rt.log(f"衣柜读取暂停：{error}。")
            return False


@agent_action("nikki.renewal_read")
class RenewalReadAction(CustomAction):
    def run(self, context, argv):
        rt = Runtime(context)
        scanner = None
        try:
            mode = str(parameters(context, argv.node_name).get("mode", "full"))
            label = "焕新增量同步" if mode == "incremental" else "焕新读取"
            rt.log(f"{label}开始。")
            scanner = RenewalScanner(rt, mode=mode)
            complete = scanner.run()
            matched = scanner.summary.get("matched_groups", 0)
            added = scanner.summary.get("added_items", 0)
            updated_lv = scanner.summary.get("updated_levels", 0)
            unmatched = scanner.summary.get("unmatched_count", 0)
            if mode == "incremental":
                rt.log(
                    f"{label}{'完成' if complete else '结束'}：新增 {added} 件服装，更新 {updated_lv} 组等级（检查 {matched} 组"
                    + (f"，{unmatched} 张卡片未匹配" if unmatched else "")
                    + "）。"
                )
            else:
                rt.log(
                    f"{label}{'完成' if complete else '结束'}：已同步 {matched} 组服装等级，合入 {added} 件新录入衣柜记录"
                    + (f"（{unmatched} 张卡片未匹配到名称）" if unmatched else "")
                    + "。"
                )
            if not complete and scanner.summary.get("reason"):
                rt.log(scanner.summary["reason"])
            _, finished = rt.finish(argv.node_name)
            return complete and finished
        except TaskStopped:
            if scanner is not None:
                try:
                    scanner.save("用户停止，本次焕新读取进度已保存")
                except (OSError, ValueError) as error:
                    rt.log(f"焕新读取进度保存失败：{error}。", detail=True)
            raise
        except Exception as error:
            if not rt.stopped:
                rt.diagnostic("renewal_read", str(error))
            if scanner is not None:
                try:
                    scanner.save(str(error))
                except (OSError, ValueError) as save_error:
                    rt.log(f"焕新读取进度保存失败：{save_error}。", detail=True)
            rt.log(f"焕新读取暂停：{error}。")
            return False


@agent_action("nikki.backpack_read")
class BackpackReadAction(CustomAction):
    def run(self, context, argv):
        rt = Runtime(context)
        try:
            rt.log("背包读取开始。")
            account = AccountState.for_runtime(ROOT, rt)
            crafting, scoring = load_catalogs()
            inv = rescan_backpack(rt, account, crafting)
            if inv is None:
                rt.log("背包读取未完成。")
                return False
            record_material_history(account, crafting, scoring)
            _, finished = rt.finish(argv.node_name)
            return finished
        except TaskStopped:
            raise
        except Exception as error:
            if not rt.stopped:
                rt.diagnostic("backpack_read", str(error))
            rt.log(f"背包读取中断：{error}。")
            return False


@agent_action("nikki.infinite_heart_read")
class InfiniteHeartReadAction(CustomAction):
    def run(self, context, argv):
        rt = Runtime(context)
        folder = ROOT / "logs/infinite_heart" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        folder.mkdir(parents=True, exist_ok=True)
        try:
            rt.log("无限之心读取开始。")
            scanner = InfiniteHeartScanner(rt, folder)
            complete = scanner.run()
            summary = scanner.summary
            rt.log(
                f"无限之心读取{'完成' if complete else '结束'}：已识别 {summary.get('shards', 0)} 个碎片，"
                f"已解锁 {summary.get('unlocked', 0)} / {summary.get('total', 0)} 节点"
                + (f"（{summary['ambiguous']} 项待确认）" if summary.get("ambiguous") else "")
                + "。"
            )
            _, finished = rt.finish(argv.node_name)
            return complete and finished
        except TaskStopped:
            raise
        except Exception as error:
            if not rt.stopped:
                rt.diagnostic("infinite_heart_read", str(error))
            rt.log(f"无限之心读取中断：{error}。")
            return False
