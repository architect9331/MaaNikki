"""Guarded realm collection step: organize when needed, then farm and upgrade."""
from datetime import datetime
import json

import cv2
from maa.custom_action import CustomAction

from registration import agent_action
from task_control import TaskStopped
from daily.runtime import Executors, parameters
from daily.settings import ROOT
from collection.pursuit import FiveStarPursuit
from collection.inventory_workflow import FiveStarInventory
from collection.organize import organize_inventory
from collection.scanner import RescanSlot
from collection.storage import InventoryStore

ORGANIZE_OPTIONS = {"auto": None, "always": False, "always_unlock": True}


def fallback_realm(context, rt, node):
    """Spend the energy on the configured realm; settlement still reports the failure."""
    return context.override_pipeline({
        node: {"next": ["MaaNikki_Bless"]},
        "MaaNikki_Bless": {"next": ["MaaNikki_RealmReviewFailed"],
                           "on_error": ["MaaNikki_RealmReviewFailed"],
                           "custom_action_param": {"kind": "bless", "return_page": "calendar"}}})


def organization_issue(rt, catalog):
    try:
        InventoryStore(ROOT, catalog, rt).load_organization()
    except (OSError, ValueError, KeyError, TypeError) as error:
        return str(error)
    return None


@agent_action("nikki.blessing_pursuit")
class BlessingPursuitAction(CustomAction):
    def run(self, context, argv):
        rt = Executors(context)
        folder = ROOT / "logs/blessing-pursuit" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        folder.mkdir(parents=True, exist_ok=True)
        pursuit = None
        try:
            options = parameters(context, argv.node_name)
            catalog = json.loads((ROOT / "resource/data/blessing_catalog.json").read_text(encoding="utf-8"))
            organize = parameters(context, "MaaNikki_Pursuit_OrganizeOption").get("mode", "auto")
            if organize not in ORGANIZE_OPTIONS:
                raise ValueError("未知祝福闪光整理选项")
            issue = None if organize != "auto" else organization_issue(rt, catalog)
            if organize != "auto" or issue is not None:
                rt.log("正在整理全部祝福闪光：读取头部、手部、脚部并锁定每款每色的满级收藏。",
                       ui_text="正在整理全部祝福闪光。")
                if issue is not None:
                    rt.log(f"需要整理的原因：{issue}。", detail=True)
                result = organize_inventory(rt, catalog, allow_unlock=bool(ORGANIZE_OPTIONS[organize]))
                if not result["published"] and organization_issue(rt, catalog) is not None:
                    rt.log("整理未完成且没有可用记录，本轮改用「祝福闪光目标」刷取，下次运行重新整理。",
                           ui_text="整理未完成，本轮改刷备用副本。")
                    return fallback_realm(context, rt, argv.node_name)
                if not result["published"]:
                    rt.log("本次整理未完成，继续使用上一份有效记录。")
            pursuit = FiveStarInventory(rt, catalog, folder)
            decompose = parameters(context, "MaaNikki_Pursuit_DecomposeOption").get("enabled") is True
            ok = FiveStarPursuit(pursuit, decompose_duplicates=decompose).run()
            if not ok and not rt.stopped:
                rt.diagnostic("blessing_pursuit", "祝福闪光处理未完成")
            progress_path = pursuit.report.get("inventory", {}).get("path") or pursuit.report.get("startup", {}).get("path")
            rt.log(f"五星处理记录：{folder / 'pursuit.json'}。"
                   f"收集进度：{progress_path or pursuit.store.reference(pursuit.store.last_scan_path)}。", detail=True)
            if options.get("return_page") == "calendar":
                return rt.ui.calendar() and ok
            _, finished = rt.finish(argv.node_name)
            return finished and ok
        except TaskStopped as error:
            if pursuit is not None:
                pursuit.record_interruption(error, reason="用户停止，进度已保存")
            raise
        except (RescanSlot, OSError, ValueError, KeyError, TypeError, RuntimeError, cv2.error) as error:
            reason = "焦点变化使当前升级或列表状态失效，请重新运行核对" if isinstance(error, RescanSlot) else str(error)
            if not rt.stopped:
                rt.diagnostic("blessing_pursuit", reason)
            if pursuit is not None:
                pursuit.record_interruption(error, reason=reason)
                try:
                    pursuit.image("interrupted.png", rt.capture())
                except (OSError, RuntimeError, cv2.error):
                    pass
            rt.log(f"五星处理暂停：{reason}。已保存操作记录：{folder}。",
                   ui_text=f"五星处理暂停：{reason}。")
            # A failure before any realm submission leaves the energy unspent.
            farmed = pursuit is not None and any(
                op.get("kind") == "realm" and op.get("input_attempted") is not False
                for op in pursuit.report["operations"])
            if not rt.stopped and not farmed:
                rt.log("本轮尚未刷取，改用「祝福闪光目标」刷取，已保存的进度下次继续。",
                       ui_text="全收集流程未完成，本轮改刷备用副本。")
                return fallback_realm(context, rt, argv.node_name)
            return False
        finally:
            if pursuit is not None:
                try:
                    pursuit.log_progress("本轮结束" if pursuit.report.get("complete") else "本轮中断")
                    # Save confirmed keepers and scoped recovery independently
                    # of stale, untracked material quantities.
                    if pursuit.prepared and "inventory" not in pursuit.report:
                        pursuit.checkpoint()
                    else:
                        pursuit.save()
                except (OSError, ValueError, KeyError, TypeError) as error:
                    rt.log(f"五星追全进度保存失败：{error}。")
