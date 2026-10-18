"""Shared guarded entry for inventory-only processing and full collection."""
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
from collection.scanner import RescanSlot


@agent_action("nikki.blessing_pursuit")
class BlessingPursuitAction(CustomAction):
    def run(self, context, argv):
        rt = Executors(context)
        folder = ROOT / "logs/blessing-pursuit" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        folder.mkdir(parents=True, exist_ok=True)
        pursuit = None
        try:
            options = parameters(context, argv.node_name)
            mode = options.get("mode", "full_collection")
            if mode not in ("full_collection", "inventory_only"):
                raise ValueError("未知祝福闪光处理模式")
            catalog = json.loads((ROOT / "resource/data/blessing_catalog.json").read_text(encoding="utf-8"))
            pursuit = FiveStarInventory(rt, catalog, folder)
            runner = pursuit if mode == "inventory_only" else FiveStarPursuit(pursuit)
            ok = runner.run()
            progress_path = pursuit.report.get("inventory", {}).get("path") or pursuit.report.get("startup", {}).get("path")
            rt.log(f"五星处理记录：{folder / 'pursuit.json'}。"
                   f"收集进度：{progress_path or pursuit.store.last_scan_path}。", detail=True)
            if options.get("return_page") == "calendar":
                return rt.ui.calendar() and ok
            _, finished = rt.finish(argv.node_name)
            return finished and ok
        except TaskStopped:
            if pursuit is not None:
                pursuit.report.update(complete=False, phase="interrupted", reason="用户停止，保留已确认进度及未确认操作")
            raise
        except (RescanSlot, OSError, ValueError, KeyError, TypeError, RuntimeError, cv2.error) as error:
            reason = "焦点变化使当前升级或列表状态失效，请重新运行核对" if isinstance(error, RescanSlot) else str(error)
            if pursuit is not None:
                pursuit.report.update(complete=False, phase="interrupted", reason=reason)
                if pursuit.organization is not None:
                    pursuit.organization.update(complete=False, reason=reason)
                try:
                    pursuit.image("interrupted.png", rt.capture())
                except (OSError, RuntimeError, cv2.error):
                    pass
            rt.log(f"五星处理暂停：{reason}。已保存操作记录：{folder}。",
                   ui_text=f"五星处理暂停：{reason}。")
            return False
        finally:
            if pursuit is not None:
                try:
                    pursuit.log_progress("本轮结束" if pursuit.report.get("complete") else "本轮中断")
                    # Invalidate old stock after any uncertain/incomplete run.
                    # Saved collection progress remains valid if only the
                    # final navigation was interrupted; quantities stay unknown.
                    if pursuit.prepared and "inventory" not in pursuit.report:
                        pursuit.checkpoint()
                    else:
                        pursuit.save()
                except (OSError, ValueError, KeyError, TypeError) as error:
                    rt.log(f"五星追全进度保存失败：{error}。")
