"""Inventory scan and collection planner task; never spends game resources."""
from datetime import datetime
import json
from pathlib import Path

import cv2
from maa.custom_action import CustomAction
from registration import agent_action
from task_control import TaskStopped
from daily.runtime import Executors, parameters
from daily.settings import ROOT
from collection.blessing import analyze, validate_estimate
from collection.report import load_snapshot, save_report, write_json
from collection.scanner import InventoryScanner


@agent_action("nikki.blessing_collection")
class BlessingCollectionAction(CustomAction):
    def run(self, context, argv):
        rt = Executors(context)
        options = parameters(context, argv.node_name)
        scanner = None
        folder = ROOT / "logs" / "blessing" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        folder.mkdir(parents=True, exist_ok=True)
        try:
            catalog = json.loads((ROOT / "resource/data/blessing_catalog.json").read_text(encoding="utf-8"))
            estimate = parameters(context, "MaaNikki_Blessing_Estimate")
            rainbow = float(estimate["rainbow_percent"]) / 100
            yield_rate = float(estimate["five_star_per_40"])
            validate_estimate(rainbow, yield_rate)
            if options.get("source") == "snapshot":
                snapshot = load_snapshot(Path(options.get("file", "")), catalog)
            else:
                scanner = InventoryScanner(rt, catalog, folder)
                snapshot = scanner.run()
            # Persist inventory before planning, including incomplete scans.
            write_json(folder / "inventory.json", snapshot)
            result = analyze(catalog, snapshot, rainbow, yield_rate, lambda: rt.stopped)
            report = save_report(folder, catalog, snapshot, result)
            scan = result["scan"]
            status = "扫描完成" if scan["complete"] else "部分扫描，结果待确认"
            recorded = sum(item["count"] for item in snapshot["items"])
            color_pending = sum(item["count"] for item in snapshot["items"] if not item.get("color"))
            rt.log(f"祝福闪光：{status}；已确认部件及等级 {recorded} 个，"
                   f"部件或等级待确认 {len(snapshot['unresolved'])} 个，颜色待确认 {color_pending} 个。")
            # Prefix the path so the client does not expand the entire report into the log.
            rt.log(f"详细报告：{report.with_suffix('.html')}（用浏览器打开）。")
            if scanner is not None and not rt.stopped:
                _, finished = rt.finish(argv.node_name)
                return finished
            return not rt.stopped
        except TaskStopped:
            if scanner is not None:
                scanner.snapshot["scan"].update(complete=False, reason="用户中断扫描")
                write_json(folder / "inventory.json", scanner.snapshot)
            raise
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, cv2.error) as error:
            if scanner is not None:
                scanner.snapshot["scan"].update(complete=False, reason=f"扫描未完成：{error}")
                write_json(folder / "inventory.json", scanner.snapshot)
            rt.log(f"祝福闪光分析未完成：{error}。已有内容保存于 {folder}。")
            if scanner is not None and not rt.stopped:
                rt.main()
            return False
