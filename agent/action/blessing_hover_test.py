"""User-operated comparison of initial waits through the production hover reader."""
from datetime import datetime
import json
from statistics import median
import time

import cv2
from maa.custom_action import CustomAction

from registration import agent_action
from task_control import TaskStopped
from daily.runtime import Executors, parameters
from daily.settings import ROOT
from collection.report import report_html, write_json
from collection.scanner import InventoryScanner, LEVEL, RescanSlot


WAITS_MS = (0, 60, 100, 140, 180, 240)
FIELDS = ("name", "slot", "color", "level")


def identity(facts):
    if facts is None or any(facts.get(key) is None for key in FIELDS):
        return None
    return {key: facts[key] for key in FIELDS}


def summarize(data):
    active = data["passes"][-1] if data["passes"] else {}
    observations = active.get("observations", [])
    references = active.get("references", [])
    count = len(active.get("boxes", []))
    baseline_ready = (count > 0 and len(references) == count
                      and all(item["confirmed"] for item in references))
    rows = []
    for wait in WAITS_MS:
        samples = [item for item in observations if item["wait_ms"] == wait]
        durations = [item["seconds"] for item in samples]
        matches = sum(item["agrees"] is True for item in samples)
        eligible = (data["complete"] and baseline_ready and len(samples) == count
                    and matches == count)
        rows.append({"wait_ms": wait, "samples": len(samples), "matches": matches,
                     "unknown": sum(item["identity"] is None for item in samples),
                     "mismatches": sum(item["agrees"] is False for item in samples),
                     "uncomparable": sum(item["agrees"] is None for item in samples),
                     "mean_seconds": round(sum(durations) / len(durations), 4) if durations else None,
                     "median_seconds": round(median(durations), 4) if durations else None,
                     "ocr_reads": sum((item["evidence"] or {}).get("reads", 0) for item in samples),
                     "pre_read_captures": sum((item["evidence"] or {}).get("pre_read_captures", 0)
                                               for item in samples),
                     "eligible": eligible})
    qualified = [row for row in rows if row["eligible"]]
    fastest = min(qualified, key=lambda row: row["mean_seconds"]) if qualified else None
    return {"rows": rows, "reference_confirmed": sum(item["confirmed"] for item in references),
            "sample_count": count, "recommended_wait_ms": fastest["wait_ms"] if fastest else None}


def save(folder, data):
    data["summary"] = summary = summarize(data)
    write_json(folder / "timing.json", data)
    recommendation = summary["recommended_wait_ms"]
    text = ["# 祝福闪光悬停等待对比", "", data["reason"], "",
            f"本轮采样 {summary['sample_count']} 个，较长等待的双次结果一致且信息完整 "
            f"{summary['reference_confirmed']} 个。",
            "建议初始等待：" + (f"{recommendation} 毫秒。" if recommendation is not None
                               else "暂无；采样未完成、基准未确认或各档存在识别差异。"),
            "建议仅适用于本次首屏样本，不代表所有库存均已验证；不会自动修改正式扫描参数。",
            "所有档位均使用正式扫描的提示消失、出现、文字稳定和 OCR 恢复逻辑；"
            "0 毫秒表示取消初始固定等待，仍会等待文字稳定。",
            "耗时包括移开旧提示、移动鼠标、截图、稳定检查、OCR 和截图保存；"
            "不包括基准读取和报告写入。基准分别使用 300、400 毫秒初始等待。",
            "颜色、名称、部位或等级缺失，以及与已确认基准不一致，均不能作为推荐依据。",
            "切出游戏后废弃本轮计时，保留记录并重新采样；报告汇总只使用最后一轮。", "",
            "| 初始等待(ms) | 样本 | 一致 | 不一致 | 信息不全 | 无可用基准 | 平均(s/个) | 中位(s/个) | OCR次数 | 稳定预读截图 | 可推荐 |",
            "|---|---|---|---|---|---|---|---|---|---|---|"]
    for row in summary["rows"]:
        text.append("| " + " | ".join(str(row[key]) for key in (
            "wait_ms", "samples", "matches", "mismatches", "unknown", "uncomparable",
            "mean_seconds", "median_seconds", "ocr_reads", "pre_read_captures", "eligible")) + " |")
    markdown = "\n".join(text) + "\n"
    (folder / "report.md").write_text(markdown, encoding="utf-8")
    (folder / "report.html").write_text(report_html(markdown), encoding="utf-8")
    return summary


def observe(scanner, box, generation, wait_ms, index):
    scanner.tooltip_initial_wait = wait_ms / 1000
    started = time.perf_counter()
    facts, evidence = scanner.read_details(box, generation, index)
    scanner.check_generation(generation)
    if scanner.rt.stopped:
        raise TaskStopped("用户停止悬停等待对比")
    return {"wait_ms": wait_ms, "seconds": round(time.perf_counter() - started, 4),
            "identity": identity(facts), "evidence": evidence}


@agent_action("nikki.blessing_hover_test")
class BlessingHoverTestAction(CustomAction):
    def run(self, context, argv):
        rt = Executors(context)
        folder = ROOT / "logs/blessing-hover" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        folder.mkdir(parents=True, exist_ok=True)
        data = {"created": datetime.now().isoformat(timespec="seconds"), "complete": False,
                "reason": "尚未完成", "passes": []}
        try:
            options = parameters(context, argv.node_name)
            requested = int(options.get("samples", 14))
            if requested not in (14, 28):
                raise ValueError("采样数量只能为 14 或 28")
            catalog = json.loads((ROOT / "resource/data/blessing_catalog.json").read_text(encoding="utf-8"))
            scanner = InventoryScanner(rt, catalog, folder)
            rt.log("悬停等待对比：开始首屏采样，各档保留相同的文字稳定检查。")
            index = 0
            while not rt.stopped:
                current = {"boxes": [], "references": [], "observations": [], "abandoned": False}
                data["passes"].append(current)
                try:
                    if not scanner.open():
                        raise RuntimeError("未能打开祝福闪光分解页面")
                    scanner._page_frame, scanner._tooltip_roi = None, None
                    rt.check_focus()
                    generation = rt._focus_generation
                    frame = scanner.top()
                    scanner.check_generation(generation)
                    if frame is None or frame.shape[:2] != (720, 1280) or not scanner.ready(frame):
                        raise RuntimeError("未确认分解页顶部或截图不是 1280×720")
                    scanner._page_frame = frame
                    boxes = scanner.cards(frame, scanner.ocr(scanner.grid, frame, LEVEL))
                    scanner.check_generation(generation)
                    if not boxes:
                        raise RuntimeError("首屏没有识别到完整物品格子")
                    count = min(requested, len(boxes))
                    # Cover upper/lower rows even for the smaller sample option.
                    selected = [boxes[round(i * (len(boxes) - 1) / (count - 1))]
                                for i in range(count)] if count > 1 else boxes[:1]
                    current["boxes"] = selected
                    current["page_image"] = scanner.image(f"page_{len(data['passes']):03d}.png", frame)
                    save(folder, data)
                    for card, box in enumerate(selected):
                        reference_reads = []
                        for wait in (300, 400):
                            index += 1
                            reference_reads.append(observe(scanner, box, generation, wait, index))
                        baseline = reference_reads[0]["identity"]
                        confirmed = baseline is not None and baseline == reference_reads[1]["identity"]
                        current["references"].append({"card": card + 1, "confirmed": confirmed,
                                                      "reads": reference_reads})
                        # Rotate ordering so one wait is not always first after warmup.
                        offset = card % len(WAITS_MS)
                        order = WAITS_MS[offset:] + WAITS_MS[:offset]
                        if card % 2:
                            order = tuple(reversed(order))
                        for wait in order:
                            index += 1
                            observation = observe(scanner, box, generation, wait, index)
                            observation.update(card=card + 1, agrees=(observation["identity"] == baseline
                                                                     if confirmed else None))
                            current["observations"].append(observation)
                            save(folder, data)
                        if (card + 1) % 7 == 0:
                            rt.log(f"悬停等待对比：已完成 {card + 1}/{count} 个物品。")
                    scanner.check_generation(generation)
                    data.update(complete=True, reason="首屏对比完成")
                    break
                except RescanSlot:
                    current["abandoned"] = True
                    data["reason"] = "前台恢复，废弃受暂停影响的计时并重新采样"
                    save(folder, data)
                    rt.log("悬停等待对比：已恢复游戏，重新采样首屏。")
            if rt.stopped:
                raise TaskStopped("用户停止悬停等待对比")
            summary = save(folder, data)
            recommended = summary["recommended_wait_ms"]
            rt.log("悬停等待对比完成；" + (f"本次样本最快合格档位 {recommended} 毫秒。"
                                            if recommended is not None else "存在未确认项，暂不推荐等待值。"))
            rt.log(f"详细报告：{folder / 'report.html'}（用浏览器打开）。")
            _, finished = rt.finish(argv.node_name)
            return finished
        except TaskStopped:
            data.update(complete=False, reason="用户中断，已保存部分结果")
            save(folder, data)
            rt.log(f"悬停等待对比已停止。详细报告：{folder / 'report.html'}。")
            raise
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, cv2.error) as error:
            data.update(complete=False, reason=f"对比未完成：{error}")
            save(folder, data)
            rt.log(f"悬停等待对比未完成：{error}。详细报告：{folder / 'report.html'}。")
            if not rt.stopped:
                rt.main()
            return False
