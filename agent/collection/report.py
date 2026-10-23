"""Persisted collection reports for offline viewing."""
from __future__ import annotations

import html
import json
import re

from file_io import write_json

from .blessing import SLOTS, summarize_scan
from .policy import definition_for, normalize_scope, protected_inventory, reconcile_exclusions, unregistered_inventory


TITLES = {1: "一级：每套、每部位至少一个满级，颜色不限",
          2: "二级：满足一级，并补齐所有支持彩色的部件",
          3: "三级：满足一级，并实际拥有每个部件的每种颜色"}


def amount(value):
    return f"{value:,.0f}"


def cell(value):
    return str(value).replace("|", "／").replace("\n", " ")


def report_html(markdown):
    def inline(value):
        escaped = html.escape(value)
        escaped = re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", escaped)
        return re.sub(r"\[([^]]+)\]\((https://[^\s)]+)\)",
                      r'<a href="\2" target="_blank" rel="noreferrer">\1</a>', escaped)

    body, table = [], False
    for line in markdown.splitlines():
        if line.startswith("|"):
            if not table:
                body.append("<div class=table-wrap><table>")
                table = True
            if re.fullmatch(r"[| :\-]+", line):
                continue
            body.append("<tr>" + "".join("<td>" + inline(value.strip()) + "</td>"
                                          for value in line.strip("|").split("|")) + "</tr>")
            continue
        if table:
            body.append("</table></div>")
            table = False
        if line.startswith("# "):
            body.append("<h1>" + inline(line[2:]) + "</h1>")
        elif line.startswith("## "):
            body.append("<h2>" + inline(line[3:]) + "</h2>")
        elif line:
            body.append("<p>" + inline(line) + "</p>")
    if table:
        body.append("</table></div>")
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>祝福闪光收集报告</title><style>body{max-width:1100px;margin:40px auto;'
            'padding:0 24px;background:#faf9f6;color:#222;font:15px/1.7 system-ui}'
            'h1{font-size:28px}h2{margin-top:36px}a{color:#336a69}.table-wrap{overflow-x:auto}'
            'table{width:100%;border-collapse:collapse;background:white}'
            'td{padding:9px 12px;border-bottom:1px solid #e3e0d8}tr:first-child{font-weight:600;'
            'background:#eeece6}strong{color:#286361}</style>' + "\n".join(body) + '</html>')


def save_report(folder, catalog, snapshot, result):
    write_json(folder / "inventory.json", snapshot)
    write_json(folder / "analysis.json", result)
    scan = result["scan"]
    status = "完整扫描" if scan["complete"] else "部分扫描：覆盖率仅表示已确认拥有，未发现项待确认"
    if scan.get("identity_complete") and not scan["complete"]:
        status = "部件及等级扫描完整，部分颜色待确认；一级目标可按已确认库存计算"
    color_pending = sum(item["count"] for item in snapshot["items"] if not item.get("color"))
    verification = []
    if scan.get("verification_policy") == "tooltip_first_variant":
        verification = [f"游戏内悬停文字核对 {scan.get('tooltip_verified', 0)} 个，"
                        f"复用已核对重复条目 {scan.get('cached_verified', 0)} 个。"
                        "首次款式先以连续截图确认悬停提示出现及稳定，再核对名称、部位、等级及颜色文字；"
                        "确认结果还须通过独立截图的字段像素一致性检查。"
                        "布局或识别异常时扩大范围重读。"]
    assumptions = result["assumptions"]
    text = ["# 祝福闪光收集报告", "", f"{status}。", "",
            f"图鉴基线：{catalog['version']}，{len(catalog['sets'])} 套。",
            f"扫描时间：{snapshot['created']}。{scan['reason']}。",
            f"已记录 {len(snapshot['items'])} 个条目、{scan.get('recorded_copies', 0)} 个副本；"
            f"部件或等级待确认 {len(snapshot['unresolved'])} 个条目，颜色待确认 {color_pending} 个副本。",
            "颜色待确认的部件保留名称、部位及等级，计入一级目标；"
            "二级和三级仅计入已确认的颜色。", "",
            *verification, "",
            "## 估算依据", "",
            f"彩色概率：{assumptions['iridescent'] * 100:.3f}%（可调整；民间汇总参考，不是官方概率）。",
            f"困难副本每 40 能量的五星产量：{assumptions['five_star_per_40_energy']:g}。",
            assumptions["pool_selection"] + "；这些分布均为假设。",
            assumptions["strategy"] + "。", "",
            "活跃能量只估算常规五星副本；低星、周常和活动来源另列。"
            "噗灵与经验为新增升级成本，未抵扣当前持有材料，不含资源补充所需能量。"
            "三级保留现有低等级颜色，不要求每种颜色都满级。", "",
            "三级预算暂保留颜色未知的低等级副本，避免消耗尚未确认的唯一原色。",
            "扫描不完整时，以下缺口和预算均为临时规划，不能据此认定库存缺失。", ""]
    for goal in result["goals"]:
        text.extend(["## " + TITLES[goal["goal"]], "",
                     f"部件覆盖率：**{goal['confirmed_parts'] / goal['total_parts']:.1%}**"
                     f"（{goal['confirmed_parts']}/{goal['total_parts']}）。"])
        if not goal.get("complete", scan["complete"]):
            text.append("该目标仍有库存或识别待确认项，覆盖率表示已确认范围，缺口及预算为临时估算。")
        if goal["goal"] == 2:
            rainbow = sum("彩" in row["owned_colors"] for row in goal["parts"] if row["rarity"] == 5)
            text.append(f"其中彩色：{rainbow}/{sum(d['rarity'] == 5 for d in catalog['sets']) * 3}。")
        if goal["goal"] == 3:
            text.append(f"颜色覆盖率：**{goal['confirmed_colors'] / goal['total_colors']:.1%}**"
                        f"（{goal['confirmed_colors']}/{goal['total_colors']}）。")
        text.extend(["", f"预计新增升级：噗灵 **{amount(goal['bling'])}**，经验 **{amount(goal['exp'])}**。",
                     f"五星副本能量合计（条件估算）：**{amount(sum(p['energy'] for p in goal['pools']))}**。", "",
                     "| 五星副本 | 预计活跃能量 |", "| --- | ---: |"])
        text.extend(f"| {cell(pool['source'])} | {amount(pool['energy'])} |"
                    for pool in sorted(goal["pools"], key=lambda row: row["energy"], reverse=True)
                    if pool["energy"] > .01)
        text.extend(["", "| 待补部件 | 尚未确认的颜色 | 满级 | 建议 | 预计噗灵 |",
                     "| --- | --- | --- | --- | ---: |"])
        text.extend(f"| {cell(row['name'])}·{row['slot']} | {''.join(row['missing_colors']) or '齐全'}"
                    f"{'（另有 ' + str(row['color_unresolved_copies']) + ' 个颜色待确认）' if row.get('color_unresolved_copies') else ''} | "
                    f"{'已有' if row['maxed'] else '待补'} | {cell(row['source'])}；"
                    f"可升级副本 {row['available_copies']}，预计升级 {row['rolls']:.1f} 次 | {amount(row['bling'])} |"
                    for row in goal["parts"] if not row["confirmed"])
        text.append("")
    text.extend(["## 扫描记录", "", "| 部位 | 状态 | 页面数 |", "| --- | --- | ---: |"])
    for slot in SLOTS:
        state = scan["slots"].get(slot, {})
        text.append(f"| {slot} | {cell(state.get('reason', '未扫描'))} | {state.get('pages', 0)} |")
    text.extend(["", "## 数据限制与来源", ""])
    text.extend("- " + warning for warning in catalog["warnings"] + scan.get("warnings", []))
    text.append("")
    text.extend(f"- [{source['title']}]({source['url']})" for source in catalog["sources"])
    text.extend(["", "## 部件及等级已确认的库存", "", "| 部件 | 等级 | 颜色 | 副本数量 | 页面截图 |",
                 "| --- | ---: | --- | ---: | --- |"])
    text.extend(f"| {cell(item['name'])}·{item['slot']} | {item['level']} | {item.get('color') or '待确认'} | "
                f"{item['count']} | {cell(item.get('page', '导入'))} |" for item in snapshot["items"])
    text.extend(["", "部件或等级未确认项保存在 inventory.json 的 unresolved；"
                 "颜色待确认项保留在 items，color 为 null，候选分数及截图文件名随条目保存。"
                 "可修正该文件后用“读取库存快照”重新生成报告。"])
    markdown = "\n".join(text) + "\n"
    (folder / "report.md").write_text(markdown, encoding="utf-8")
    # Portable offline report: no CDN, no remote scripts, no inventory upload.
    (folder / "report.html").write_text(report_html(markdown), encoding="utf-8")
    return folder / "report.md"


def load_snapshot(path, catalog):
    if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("库存快照不存在或超过 20 MiB")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return validate_snapshot(data, catalog)


def validate_snapshot(data, catalog):
    """Validate and refresh completeness after importing or updating inventory."""
    if (not isinstance(data, dict) or data.get("schema_version") != 1
            or not isinstance(data.get("items"), list) or len(data["items"]) > 10000):
        raise ValueError("不支持的库存快照格式")
    if any(not isinstance(item, dict) or not isinstance(item.get("name"), str) for item in data["items"]):
        raise ValueError("库存条目必须包含名称")
    normalize_scope(data, catalog)
    for item in data["items"] + data["excluded_items"]:
        if not isinstance(item, dict):
            raise ValueError("库存条目必须是对象")
        definition = definition_for(catalog, item.get("name"), observed=item)
        if item.get("slot") not in SLOTS:
            raise ValueError("库存快照含未登记名称或部位")
        item["rarity"] = definition["rarity"]
        item.update(identity_known=True, color_known=bool(item.get("color")))
        maximum = catalog["upgrade_costs"][str(definition["rarity"])]["max_level"]
        if (type(item.get("level")) is not int or not 0 <= item["level"] <= maximum
                or type(item.get("count")) is not int or not 1 <= item["count"] <= 10000
                or item.get("color") is not None and item["color"] not in definition["colors"]
                or item.get("color") == "彩" and item["level"] != maximum):
            raise ValueError("库存快照含无效等级、颜色或数量")
        xp = item.setdefault("xp", 0)
        costs = catalog["upgrade_costs"][str(definition["rarity"])]["exp"]
        limit = costs[item["level"]] if item["level"] < maximum else 0
        if type(xp) is not int or not 0 <= xp <= limit:
            raise ValueError("库存快照含无效的等级内经验")
        if ("xp_known" in item and type(item["xp_known"]) is not bool
                or item.get("locked_count") is not None and (
                    type(item["locked_count"]) is not int or not 0 <= item["locked_count"] <= item["count"])):
            raise ValueError("库存快照含无效的经验或锁定数量")
        item.setdefault("xp_known", False)
        if item.get("protected_inventory") and (
                not protected_inventory(item) or item["lock_mutable"]
                or item.get("locked_count") != item["count"]
                or item.get("lock_state_known") is not True):
            raise ValueError("受保护三星库存缺少已确认的锁定状态")
        if item.get("catalog_unknown") and (
                not unregistered_inventory(item) or item["lock_mutable"]
                or item.get("lock_state_known") is not True or item.get("locked_count") is None):
            raise ValueError("图鉴外库存缺少星级或锁定状态识别依据")
    if not isinstance(data.get("scan"), dict) or not isinstance(data.get("created"), str):
        raise ValueError("库存快照缺少扫描信息")
    data.setdefault("unresolved", [])
    # Recompute after manual editing; stale complete flags cannot assert absence.
    scan = data["scan"]
    exclusions = scan.setdefault("excluded_owned", [])
    if not isinstance(exclusions, list):
        raise ValueError("特殊闪光持有数量记录必须为列表")
    locks = data["locks"]
    if (not isinstance(locks, dict) or type(locks.get("complete")) is not bool
            or not isinstance(locks.get("groups"), list)):
        raise ValueError("锁定状态汇总格式无效")
    lock_keys = set()
    for group in locks["groups"]:
        if not isinstance(group, dict):
            raise ValueError("锁定状态分组必须为对象")
        definition = definition_for(catalog, group.get("name"))
        maximum = catalog["upgrade_costs"][str(definition["rarity"])]["max_level"]
        if (group.get("slot") not in SLOTS or type(group.get("level")) is not int
                or not 0 <= group["level"] <= maximum
                or group.get("color") is not None and group["color"] not in definition["colors"]
                or type(group.get("locked_count")) is not int or group["locked_count"] < 0):
            raise ValueError("锁定状态分组数据无效")
        key = (group["name"], group["slot"], group.get("color"), group["level"])
        if key in lock_keys:
            raise ValueError("锁定状态分组重复")
        lock_keys.add(key)
        candidates = [item for item in data["items"] if (item["name"], item["slot"], item["level"]) == (
            group["name"], group["slot"], group["level"]) and (
                group.get("color") is None or item.get("color") in (None, group["color"]))]
        if locks["complete"] and group["locked_count"] > sum(item["count"] for item in candidates):
            raise ValueError("已确认锁定数量超过本次扫描库存，请重新核对")
    scan.setdefault("slots", {})
    if (not isinstance(data["unresolved"], list) or not isinstance(scan["slots"], dict)
            or any(slot not in SLOTS or not isinstance(state, dict) for slot, state in scan["slots"].items())
            or not isinstance(scan.get("pages", []), list)
            or scan.get("reported_capacity") is not None and (
                type(scan["reported_capacity"]) is not int or scan["reported_capacity"] < 0)
            or any(key in scan and type(scan[key]) is not bool for key in ("complete", "traversed"))
            or type(scan.get("excluded_observation_complete", False)) is not bool
            or not isinstance(scan.get("warnings", []), list)):
        raise ValueError("库存快照的扫描状态无效")
    traversed = bool(scan.get("traversed") or len(scan["slots"]) == 3
                     and all(state.get("complete") for state in scan["slots"].values()))
    reconcile_exclusions(data, catalog, traversed=traversed)
    summarize_scan(data, traversed, scan.get("reason") or (
        "导入库存已核对" if traversed else "导入的库存遍历范围仍待确认"))
    for item in data["items"]:
        item.update(identity_known=True, color_known=bool(item.get("color")))
    return data
