"""Independent, idempotent maximum-level Eureka locking task."""
from datetime import datetime
import json

import cv2
from maa.custom_action import CustomAction

from registration import agent_action
from task_control import TaskStopped
from daily.runtime import Executors, parameters
from daily.settings import ROOT
from collection.organization import BlessingOrganizer
from collection.report import write_json
from collection.storage import InventoryStore
from collection.scanner import RescanSlot


@agent_action("nikki.blessing_lock")
class BlessingLockAction(CustomAction):
    def run(self, context, argv):
        rt = Executors(context)
        folder = ROOT / "logs/blessing-lock" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        folder.mkdir(parents=True, exist_ok=True)
        locker = None
        store = None
        inventory_saved = False
        try:
            catalog = json.loads((ROOT / "resource/data/blessing_catalog.json").read_text(encoding="utf-8"))
            options = parameters(context, argv.node_name)
            store = InventoryStore(ROOT, catalog)
            store.invalidate("祝福闪光整理已开始，完整结束前禁止全收集", source="organization")
            locker = BlessingOrganizer(rt, catalog, folder,
                                    allow_unlock=options.get("unlock_nonconforming", False))
            locker.run()
            snapshot = locker.capture_inventory()
            snapshot, published = store.save_scan(snapshot, folder, source="locking")
            inventory_saved = True
            write_json(folder / "inventory.json", snapshot)
            locker.report["inventory"] = {"published": published, "path": str(store.path),
                                           "snapshot": "inventory.json", "reason": snapshot["scan"]["reason"]}
            if not published:
                locker.report.update(complete=False, reason="锁定整理已完成，但普通仓库数量核对未通过")
            locker.save()
            operations = locker.report["operations"]
            added = sum(op["status"] == "confirmed" and op["kind"] == "lock" for op in operations)
            removed = sum(op["status"] == "confirmed" and op["kind"] == "unlock" for op in operations)
            status = "祝福闪光整理完成" if published else "整理结束，仓库数量核对异常，请重新整理"
            rt.log(f"{status}：头部、手部、脚部共新增锁定 {added} 个，解锁 {removed} 个。"
                   f"报告：{folder / 'locking.json'}。",
                   ui_text=f"{status}：新增锁定 {added} 个，解锁 {removed} 个。")
            rt.log(f"统一库存 JSON：{store.path if published else store.last_scan_path}。"
                   + ("可供后续任务复用。" if published else "数量核对未通过，未覆盖原有完整库存，请重新整理。"),
                   detail=True)
            _, finished = rt.finish(argv.node_name)
            return finished and published
        except TaskStopped:
            if locker is not None:
                locker.report.update(complete=False,
                                     reason="用户停止；已提交但未确认的操作请检查报告，重新运行会重新核对已有锁定")
            raise
        except (RescanSlot, OSError, ValueError, KeyError, TypeError, RuntimeError, cv2.error) as error:
            reason = "前台变化使整理位置失效，请重新运行祝福闪光整理" if isinstance(error, RescanSlot) else str(error)
            if locker is not None:
                locker.report.update(complete=False, reason=reason)
                try:
                    locker.image("interrupted.png", rt.capture())
                except (OSError, RuntimeError, cv2.error):
                    pass  # Saving the operation history must survive capture failure.
            rt.log(f"祝福闪光整理暂停：{reason}。操作记录：{folder}。",
                   ui_text=f"祝福闪光整理暂停：{reason}。")
            return False
        finally:
            if locker is not None:
                try:
                    if store is not None and not inventory_saved:
                        partial = locker.inventory_snapshot if locker.inventory_snapshot is not None else locker.snapshot
                        partial["scan"].update(complete=False, traversed=False,
                                               reason="锁定整理或普通仓库数量核对未完成")
                        for state in partial["scan"]["slots"].values():
                            state["complete"] = False
                        if isinstance(partial.get("locks"), dict):
                            partial["locks"]["complete"] = False
                        partial, _ = store.save_scan(partial, folder, source="locking")
                        write_json(folder / "inventory.json", partial)
                        locker.report["inventory"] = {
                            "published": False, "path": str(store.last_scan_path),
                            "snapshot": "inventory.json", "reason": partial["scan"]["reason"]}
                except (OSError, ValueError, KeyError, TypeError) as error:
                    rt.log(f"部分库存保存失败：{error}。")
                try:
                    locker.save()
                except (OSError, ValueError, TypeError) as error:
                    rt.log(f"锁定报告保存失败：{error}。")
