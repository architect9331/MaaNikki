"""Batch rarity-capped auto-selection; never enumerate the decomposition grid."""
import json
import re

from daily.runtime import parameters
from daily.settings import ROOT
from task_control import task_time as time
from .blessing import SLOTS
from .ordered import merge_prefix
from .scanner import IncompleteObservation, clean_text


READY = "MaaNikki_Blessing_DecomposeReady"
FILTER = r"[4四]星及以下素材"
FIVE_STAR_FILTER = r"[5五]星及以下素材"


class LowRarityDecomposer:
    def __init__(self, owner):
        self.o, self.rt = owner, owner.rt
        node = "MaaNikki_Pursuit_DecomposeLayout"
        # An already-running app may retain the pipeline loaded before this
        # parameter-only node was added. Read its shipped layout as the base;
        # loaded task overrides still take precedence. No resource reload/input.
        pipeline = json.loads((ROOT / "resource/pipeline/blessing_pursuit.json").read_text(encoding="utf-8"))
        definition = pipeline.get(node, {})
        defaults = definition.get("custom_action_param", {})
        if not isinstance(defaults, dict):
            raise ValueError("本地分解布局配置格式无效")
        loaded = parameters(self.rt.context, node)
        self.layout = {**defaults, **loaded}
        required = ("count", "capacity", "particles", "gain", "filter", "filter_choices",
                    "auto_fill", "clear", "submit", "dialog")
        for field in required:
            box = self.layout.get(field)
            size = 2 if field == "clear" else 4
            if (not isinstance(box, list) or len(box) != size
                    or any(type(value) is not int or value < 0 for value in box)
                    or box[0] >= 1280 or box[1] >= 720
                    or size == 4 and (min(box[2:]) == 0 or box[0] + box[2] > 1280 or box[1] + box[3] > 720)):
                raise ValueError(f"分解布局字段 {field} 缺失或无效，请更新资源")
        self.o.report["decomposition_layout"] = {
            "local_fields": [field for field in required if field not in loaded],
            "effective": self.layout}
        if any(field not in loaded for field in required):
            self.rt.log("运行中的分解布局配置未完整加载，已读取本地资源补齐。", detail=True)
        self.generation = None

    def text(self, field, frame):
        value = clean_text(self.rt.text(self.layout[field], image=frame))
        self.o.report.setdefault("decomposition_ocr", {})[field] = value
        return value

    @staticmethod
    def number(text):
        text = re.sub(r"[,，]", "", text)
        return int(text) if re.fullmatch(r"\d+", text) else None

    def values(self, frame):
        count = re.fullmatch(r"消耗材料[（(](\d+)[/／](\d+)[）)]", self.text("count", frame))
        capacity = re.fullmatch(r"容量[:：]?(\d+)[/／](\d+)", self.text("capacity", frame))
        particles = self.number(self.text("particles", frame))
        if not count or not capacity or particles is None:
            return None
        amount, limit = map(int, count.groups())
        used, total = map(int, capacity.groups())
        if (limit != 100 or not 0 <= amount <= min(limit, used)
                or not 0 <= used <= 10000 or not 0 < total <= 10000):
            return None
        return {"count": amount, "capacity": used, "particles": particles,
                "filter": self.text("filter", frame), "gain": self.number(self.text("gain", frame))}

    def frame(self):
        frame = self.rt.capture()
        self.o.check_generation(self.generation)
        if frame is None or frame.shape[:2] != (720, 1280):
            raise IncompleteObservation("分解截图尺寸无效")
        return frame

    def read(self):
        previous = None
        for _ in range(10):
            frame = self.frame()
            ready = self.rt.recognize(READY, image=frame)
            if not ready or not ready.hit:
                raise IncompleteObservation("未确认祝福闪光分解页面")
            current = self.values(frame)
            if current is not None and current == previous:
                return current, frame
            previous = current
            if not self.rt.pause(.2):
                break
        raise IncompleteObservation("分解材料数量、容量或粒子信息未稳定，未继续提交")

    def click(self, field):
        self.o.check_generation(self.generation)
        if not self.rt.action("Click", target=self.layout[field]) or not self.rt.pause(.4):
            raise RuntimeError("分解操作中断：" + field)

    def await_result(self, before, *, five_star=False):
        deadline = time.monotonic() + 20
        confirmed_dialog = False
        dismissed_reward = False
        previous = None
        while time.monotonic() < deadline:
            frame = self.frame()
            # A possible confirmation is acted on only with a decomposition
            # prompt AND both choices, after the four-star filter was verified.
            prompt = self.text("dialog", frame)
            if (not confirmed_dialog and "分解" in prompt
                    and re.search(r"是否|确定|确认", prompt) and "取消" in prompt):
                if re.search(r"锁定" if five_star else r"[5五]星|锁定", prompt):
                    raise RuntimeError("分解确认提示涉及五星或锁定物品，已停止")
                result = self.rt.recognize("MaaNikki_Daily_OCR", {
                    "roi": self.layout["dialog"], "expected": "^(确定|确认)$"}, image=frame)
                if not result or not result.hit:
                    raise RuntimeError("分解确认弹窗按钮未识别，已停止")
                self.o.check_generation(self.generation)
                confirmed_dialog = True
                if not self.rt.action("Click", target=list(result.box)):
                    raise RuntimeError("分解确认提交不确定，已停止且不重试")
            elif not dismissed_reward and self.rt.hit("MaaNikki_RewardLayer"):
                dismissed_reward = True
                if not self.rt.ui.skip_reward():
                    raise RuntimeError("未能关闭分解奖励界面")
            else:
                ready = self.rt.recognize(READY, image=frame)
                current = self.values(frame) if ready and ready.hit else None
                if (current is not None and current == previous and current["count"] == 0
                        and current["capacity"] == before["capacity"] - before["count"]
                        and current["particles"] == before["particles"] + before["gain"]):
                    return current, frame
                previous = current
            if not self.rt.pause(.25):
                break
        raise RuntimeError("分解后容量或粒子变化未确认，已停止且不重复分解")

    def run(self):
        self.o.ensure_known_materials()
        self.o.report["phase"] = "decompose_low_rarity"
        self.o.report["decomposition"] = summary = {"complete": False, "batches": 0, "copies": 0, "particles": 0}
        self.o.organization["decomposition_pending"] = True
        self.o.checkpoint()
        self.rt.log("正在分解四星及以下素材。")
        last_notice = time.monotonic()
        if not self.o.open() or not self.rt.click_template("MaaNikki_Blessing_DecomposeEntrance"):
            raise IncompleteObservation("未能打开祝福闪光分解页面")
        self.generation = self.rt._focus_generation
        state, _ = self.read()
        # Discard any previous/manual selection before applying the rarity cap.
        if state["count"]:
            self.click("clear")
            state, _ = self.read()
            if state["count"]:
                raise RuntimeError("分解前未确认旧材料选择已清空")
        if not re.fullmatch(FILTER, state["filter"]):
            self.click("filter")
            if not self.rt.click_text("^" + FILTER + "$", self.layout["filter_choices"]):
                raise RuntimeError("未能选择四星及以下分解素材")
        for _ in range(101):
            state, _ = self.read()
            if state["count"] or not re.fullmatch(FILTER, state["filter"]):
                raise RuntimeError("分解筛选或空材料状态未确认")
            self.click("auto_fill")
            selected, frame = self.read()
            if not re.fullmatch(FILTER, selected["filter"]):
                raise RuntimeError("一键放入后分解星级筛选改变")
            if any(selected[key] != state[key] for key in ("capacity", "particles")):
                raise RuntimeError("放入分解材料期间容量或粒子发生变化，未提交分解")
            if selected["count"] == 0:
                self.finish_queue()
                summary["complete"] = True
                self.o.organization["decomposition_pending"] = False
                self.o.checkpoint()
                self.rt.log(f"四星及以下清理完成：共分解 {summary['copies']} 件，获得 {summary['particles']} 闪光粒子。")
                if not self.rt.ui.enter_page(lambda: self.rt.key(27, 0),
                        "MaaNikki_Blessing_InventoryReady", source=READY, attempts=1):
                    raise IncompleteObservation("分解结束未能返回祝福闪光列表")
                return
            if selected["gain"] is None or selected["gain"] <= 0:
                raise RuntimeError("预计分解粒子数量无法确认")
            self.o.check_generation(self.generation)
            operation = self.o.begin_change("decompose", None, {"source": "four_star_auto_fill"}, frame,
                                            rarity_max=4, **selected)
            self.o.submit_change(operation)
            self.click("submit")  # Never retry a destructive submission.
            after, frame = self.await_result(selected)
            summary["batches"] += 1
            summary["copies"] += selected["count"]
            summary["particles"] += selected["gain"]
            self.o.confirm_change(operation, capacity_after=after["capacity"], particles_after=after["particles"],
                                  after=self.o.image(f"decompose_{summary['batches']:03d}.png", frame))
            self.rt.log(f"分解第 {summary['batches']} 批：{selected['count']} 件四星及以下，获得 {selected['gain']} 闪光粒子。", detail=True)
            if time.monotonic() - last_notice >= 30:
                self.rt.log(f"正在分解四星及以下素材，已处理 {summary['copies']} 件。")
                last_notice = time.monotonic()
        raise RuntimeError("分解批次超过上限，已停止")

    def finish_queue(self):
        # Remove exhausted level-zero stacks from the old position hints.
        # Retain leveled entries: auto-fill eligibility may exclude them.
        # Locked anchors and five-star objects/unfinished upgrades retain IDs.
        for slot, rows in self.o.organization["slots"].items():
            kept, removed = [], 0
            for row in rows:
                if row["rarity"] <= 4 and row["level"] == 0 and not row["locked"] and row["mutable"]:
                    removed += 1
                else:
                    row["ordinal"] = max(0, row["ordinal"] - removed)
                    kept.append(row)
            self.o.organization["slots"][slot] = kept
        self.o.view.slot = None
        self.o.verified.clear()


class DuplicateDecomposer(LowRarityDecomposer):
    """One-click decomposition of five-star copies that a fresh read proves surplus.

    Surplus means unlocked and either level 10 with a locked level-10 copy of the same
    set, slot and colour, or below level 10 on a part whose colours are all locked.
    Auto-fill cannot exclude a single item, so any other unlocked five-star, a missing
    keeper or an unregistered item cancels the whole step. Locked items rely on the
    game's own exclusion, which the existing rarity-capped auto-fill also relies on.
    """

    def stop(self, reason):
        self.o.report["duplicate_decomposition"].update(reason=reason)
        self.rt.log(f"未分解五星重复项：{reason}。", ui_text=f"未分解五星重复项：{reason}。")

    def plan(self):
        """Return unlocked copy counts (surplus, all), or None when anything is unproven."""
        o = self.o
        o.inventory_read = True
        try:
            surplus = unlocked = 0
            for slot in SLOTS:
                o.view.slot = None
                rows, _ = o.view.scan(slot, quantities=True)
                free = [row for row in rows if not row["locked"]]
                unknown = sorted({row["name"] for row in free if row.get("catalog_unknown")})
                if unknown:
                    self.stop("发现未锁定的图鉴外闪光：" + "、".join(unknown))
                    return None
                keepers = {o.key(row) for row in rows if row["rarity"] == 5 and row["maximum"]
                           and row["collectible"] and row["locked"]}
                missing = {key for key in o.protected if key[1] == slot} - keepers
                if missing:
                    o.request_recheck(slot, "已确认的收藏未见锁定")
                    self.stop(f"{slot}有 {len(missing)} 个已确认收藏未见锁定，下次运行先核对")
                    return None
                for row in free:
                    unlocked += row["count"]
                    if row["rarity"] != 5:
                        continue
                    colors = o.definitions[row["name"]]["colors"]
                    proven = (row["collectible"] and row["mutable"] and (
                        o.key(row) in keepers if row["maximum"]
                        else all((row["name"], slot, color) in keepers for color in colors)))
                    if not proven:
                        self.stop(f"{row['name']}·{slot}（{row['level']} 级）仍可能需要保留")
                        return None
                    surplus += row["count"]
                old = o.organization["slots"][slot]
                # Identities and queue priorities follow the shared prefix merge.
                merged, priority = merge_prefix(old, rows, len(old), o.organization["next_priority"])
                o.organization["slots"][slot] = merged
                o.organization["next_priority"] = priority
                if (o.organization["unfinished"] in {row["id"] for row in old}
                        and o.organization["unfinished"] not in {row["id"] for row in merged}):
                    o.organization["unfinished"] = None
                o.checkpoint()
            return surplus, unlocked
        finally:
            o.inventory_read = False
            o.view.slot = None
            o.verified.clear()

    def forget_surplus(self):
        # Position hints only: a copy auto-fill skipped reappears as a new prefix.
        for slot, rows in self.o.organization["slots"].items():
            kept, removed = [], 0
            for row in rows:
                if row["rarity"] == 5 and not row["locked"]:
                    removed += 1
                    if row["id"] == self.o.organization["unfinished"]:
                        self.o.organization["unfinished"] = None
                else:
                    row["ordinal"] = max(0, row["ordinal"] - removed)
                    kept.append(row)
            self.o.organization["slots"][slot] = kept

    def run(self):
        o = self.o
        summary = o.report["duplicate_decomposition"] = {
            "complete": False, "batches": 0, "copies": 0, "particles": 0}
        if o.pending is not None or o.organization.get("recovery", {}).get("slots"):
            self.stop("仍有待核对的操作")
            return
        o.ensure_known_materials()
        o.report["phase"] = "decompose_five_star_duplicates"
        self.rt.log("正在核对五星重复项：确认每个颜色都有锁定保留件。", ui_text="正在核对五星重复项。")
        if not o.open():
            raise IncompleteObservation("未能打开祝福闪光列表")
        plan = self.plan()
        if plan is None:
            return
        surplus, allowed = plan
        summary.update(surplus=surplus, unlocked=allowed)
        if not surplus:
            summary["complete"] = True
            self.rt.log("没有可分解的五星重复项。")
            return
        if not o.open() or not self.rt.click_template("MaaNikki_Blessing_DecomposeEntrance"):
            raise IncompleteObservation("未能打开祝福闪光分解页面")
        self.generation = self.rt._focus_generation
        state, _ = self.read()
        if state["count"]:
            self.click("clear")
            state, _ = self.read()
            if state["count"]:
                raise RuntimeError("分解前未确认旧材料选择已清空")
        if not re.fullmatch(FIVE_STAR_FILTER, state["filter"]):
            self.click("filter")
            if not self.rt.click_text("^" + FIVE_STAR_FILTER + "$", self.layout["filter_choices"]):
                raise RuntimeError("未能选择五星及以下分解素材")
        for _ in range(101):
            state, _ = self.read()
            if state["count"] or not re.fullmatch(FIVE_STAR_FILTER, state["filter"]):
                raise RuntimeError("分解筛选或空材料状态未确认")
            self.click("auto_fill")
            selected, frame = self.read()
            if not re.fullmatch(FIVE_STAR_FILTER, selected["filter"]):
                raise RuntimeError("一键放入后分解星级筛选改变")
            if any(selected[key] != state[key] for key in ("capacity", "particles")):
                raise RuntimeError("放入分解材料期间容量或粒子发生变化，未提交分解")
            if selected["count"] == 0:
                break
            if summary["copies"] + selected["count"] > allowed:
                self.click("clear")
                raise RuntimeError("一键放入数量超过核对的未锁定数量，已清空且未提交")
            if selected["gain"] is None or selected["gain"] <= 0:
                raise RuntimeError("预计分解粒子数量无法确认")
            self.o.check_generation(self.generation)
            operation = o.begin_change("decompose", None, {"source": "five_star_auto_fill"}, frame,
                                       rarity_max=5, **selected)
            o.submit_change(operation)
            self.click("submit")  # Never retry a destructive submission.
            after, frame = self.await_result(selected, five_star=True)
            summary["batches"] += 1
            summary["copies"] += selected["count"]
            summary["particles"] += selected["gain"]
            o.confirm_change(operation, capacity_after=after["capacity"], particles_after=after["particles"],
                             after=o.image(f"duplicates_{summary['batches']:03d}.png", frame))
        else:
            raise RuntimeError("分解批次超过上限，已停止")
        summary["complete"] = True
        self.forget_surplus()
        o.view.slot = None
        o.verified.clear()
        o.checkpoint()
        left = surplus - summary["copies"]
        self.rt.log(f"五星重复分解完成：共分解 {summary['copies']} 件，获得 {summary['particles']} 闪光粒子。"
                    + (f"还有约 {left} 件未被一键放入，已保留。" if left > 0 else ""))
        if not self.rt.ui.enter_page(lambda: self.rt.key(27, 0), "MaaNikki_Blessing_InventoryReady",
                                     source=READY, attempts=1):
            raise IncompleteObservation("分解结束未能返回祝福闪光列表")
