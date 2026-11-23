"""Batch rarity-capped auto-selection; never enumerate the decomposition grid."""
import json
import re

from daily.runtime import parameters
from daily.settings import ROOT
from adventure_report import try_record_adventure_entry
from task_control import ForegroundChanged, task_time as time
from .scanner import FocusRetryBudget, IncompleteObservation, RescanSlot, clean_text


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
        self._last_read_focus_gen = None

    def text(self, field, frame):
        value = clean_text(self.rt.text(self.layout[field], image=frame))
        self.o.report.setdefault("decomposition_ocr", {})[field] = value
        return value

    @staticmethod
    def number(text):
        text = re.sub(r"[,，]", "", text)
        match = re.fullmatch(r"[A-Za-z]?(\d+)", text)
        return int(match.group(1)) if match else None

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

    def _park_point(self, attempt=0):
        # Alternate top-bar park points outside the grid so Unreal re-evaluates
        # hover state after page animations or background cursor restoration.
        base = [900, 32] if attempt % 2 == 0 else [860, 32]
        if hasattr(self.rt, "_pointer_guard_context"):
            from tall_window import clamp_capture_park_point
            hwnd, cap_size = self.rt._pointer_guard_context()
            return clamp_capture_park_point(hwnd, base, capture_size=cap_size)
        return base

    def _park_hover(self, attempt=0):
        park = self._park_point(attempt)
        if hasattr(self.rt, "action") and self.rt.action("TouchMove", target=park):
            return True
        ui = getattr(self.rt, "ui", None)
        return bool(ui and ui.unhover())

    def _capture_frame(self, attempt=0, *, park=False):
        if park and hasattr(self.rt, "capture_parked"):
            return self.rt.capture_parked(self._park_point(attempt))
        return self.rt.capture()

    def _prepare_auto_fill(self, filter_regex, state):
        budget = FocusRetryBudget(self.rt, max_stalled=4)
        while True:
            budget.begin()
            try:
                if state is None:
                    state = self._ensure_open_and_filter(filter_regex)
                self.click("auto_fill")
                selected, frame = self.read()
                if not re.fullmatch(filter_regex, selected["filter"]):
                    raise RuntimeError("一键放入后分解星级筛选改变")
                if any(selected[key] != state[key] for key in ("capacity", "particles")):
                    raise RuntimeError("放入分解材料期间容量或粒子发生变化，未提交分解")
                self.o.check_generation(self.generation)
                return selected, frame
            except (RescanSlot, ForegroundChanged):
                if self.o._has_live_pending() or self.rt.stopped or not budget.advance():
                    raise
                state = None

    def read(self):
        if self.rt.ui.dismiss_blessing_decompose_tutorial() is False:
            raise IncompleteObservation("祝福闪光分解引导提示未能关闭")
        self._park_hover(0)
        if not self.rt.pause(.06):
            raise IncompleteObservation("分解材料数量、容量或粒子信息未稳定，未继续提交")
        previous = None
        prev_gen = None
        hold_park = False
        for attempt in range(16):
            gen = self.o.current_generation()
            focus_gen = self.rt._focus_generation
            frame = self._capture_frame(attempt, park=hold_park)
            if frame is None or frame.shape[:2] != (720, 1280):
                if not self.rt.pause(.2):
                    break
                previous = None
                prev_gen = None
                continue
            ready = self.rt.recognize(READY, image=frame)
            if self.o.current_generation() != gen:
                self._park_hover(attempt + 1)
                previous = None
                prev_gen = None
                continue
            if not ready or not ready.hit:
                raise IncompleteObservation("未确认祝福闪光分解页面")
            current = self.values(frame)
            if self.o.current_generation() != gen:
                self._park_hover(attempt + 1)
                previous = None
                prev_gen = None
                continue
            if current is not None and current == previous and prev_gen == gen:
                self.generation = gen
                self._last_read_focus_gen = focus_gen
                if self.o.organization is not None:
                    limit = (self.o.organization.get("capacity") or [0, 1000])[1]
                    self.o.organization.update(
                        particles=current["particles"],
                        capacity=[current["capacity"], limit],
                        unverified_farm_runs=0,
                    )
                return current, frame
            if current is None:
                if self.rt.ui.dismiss_blessing_decompose_tutorial() is False:
                    raise IncompleteObservation("祝福闪光分解引导提示未能关闭")
                hold_park = True
                self._park_hover(attempt + 1)
            previous = current
            prev_gen = gen
            if not self.rt.pause(.2):
                break
        raise IncompleteObservation("分解材料数量、容量或粒子信息未稳定，未继续提交")

    def click(self, field, *, focus_generation=None, require_same_focus=True):
        self.o.check_generation(self.generation)
        if not self.rt.action("Click", target=self.layout[field], focus_generation=focus_generation):
            raise RuntimeError("分解操作中断：" + field)
        if not self.rt.pause(.4):
            if not self.rt.stopped and not require_same_focus:
                return
            if not self.rt.stopped and require_same_focus:
                self.o.check_generation(self.generation)
            raise RuntimeError("分解操作中断：" + field)
        if require_same_focus:
            self.o.check_generation(self.generation)

    def _ensure_open_and_filter(self, filter_regex):
        budget = FocusRetryBudget(self.rt, max_stalled=4)
        while True:
            budget.begin()
            try:
                self.rt.check_focus()
                frame = self.rt.capture()
                ready = frame is not None and frame.shape[:2] == (720, 1280) and self.rt.recognize(READY, image=frame)
                if not (ready and ready.hit):
                    if not self.o.open() or not self.rt.click_template("MaaNikki_Blessing_DecomposeEntrance"):
                        raise IncompleteObservation("未能打开祝福闪光分解页面")
                    # First-time spotlight tutorial bubble fades in slightly after
                    # the decompose view renders; allow a brief settle before probing.
                    if not self.rt.pause(.35):
                        raise IncompleteObservation("祝福闪光分解页面等待中断")
                if self.rt.ui.dismiss_blessing_decompose_tutorial() is False:
                    raise IncompleteObservation("祝福闪光分解引导提示未能关闭")
                state, _ = self.read()
                if state["count"]:
                    self.click("clear")
                    state, _ = self.read()
                    if state["count"]:
                        raise RuntimeError("分解前未确认旧材料选择已清空")
                if not re.fullmatch(filter_regex, state["filter"]):
                    if self.rt.ui.dismiss_blessing_decompose_tutorial() is False:
                        raise IncompleteObservation("祝福闪光分解引导提示未能关闭")
                    selected_filter = False
                    for filter_attempt in range(2):
                        self.click("filter")
                        if self.rt.click_text("^" + filter_regex + "$", self.layout["filter_choices"]):
                            selected_filter = True
                            break
                        if filter_attempt == 0:
                            dismissed = self.rt.ui.dismiss_blessing_decompose_tutorial()
                            if dismissed is False:
                                raise IncompleteObservation("祝福闪光分解引导提示未能关闭")
                            if dismissed:
                                state, _ = self.read()
                                if re.fullmatch(filter_regex, state["filter"]):
                                    selected_filter = True
                                    break
                                continue
                        break
                    if not selected_filter:
                        raise RuntimeError("未能选择指定分解星级筛选")
                    state, _ = self.read()
                if state["count"] or not re.fullmatch(filter_regex, state["filter"]):
                    raise RuntimeError("分解筛选或空材料状态未确认")
                return state
            except (RescanSlot, ForegroundChanged):
                if self.o._has_live_pending() or self.rt.stopped or not budget.advance():
                    raise

    def await_result(self, before, *, five_star=False):
        deadline = time.monotonic() + 20
        confirmed_dialog = False
        dismissed_reward = False
        previous = None
        hold_park = False
        attempt = 0
        while time.monotonic() < deadline:
            gen = self.o.current_generation()
            focus_gen = self.rt._focus_generation
            frame = self._capture_frame(attempt, park=hold_park)
            attempt += 1
            if frame is None or frame.shape[:2] != (720, 1280) or self.o.current_generation() != gen:
                if not self.rt.pause(.2):
                    break
                previous = None
                continue
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
                try:
                    clicked = self.rt.action("Click", target=list(result.box), focus_generation=focus_gen)
                except ForegroundChanged:
                    previous = None
                    continue
                if not clicked:
                    raise RuntimeError("分解确认提交不确定，已停止且不重试")
                confirmed_dialog = True
            elif not dismissed_reward and self.rt.hit("MaaNikki_RewardLayer"):
                if self.o.current_generation() != gen:
                    previous = None
                    continue
                dismissed_reward = True
                if not self.rt.ui.skip_reward():
                    raise RuntimeError("未能关闭分解奖励界面")
            else:
                ready = self.rt.recognize(READY, image=frame)
                current = self.values(frame) if ready and ready.hit else None
                if self.o.current_generation() != gen:
                    previous = None
                    continue
                if ready and ready.hit and current is None and not hold_park:
                    hold_park = True
                    self._park_hover(attempt)
                if (current is not None and current == previous and current["count"] == 0
                        and current["capacity"] == before["capacity"] - before["count"]
                        and current["particles"] == before["particles"] + before["gain"]):
                    self.generation = gen
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
        state = self._ensure_open_and_filter(FILTER)
        try:
            submit_budget = FocusRetryBudget(self.rt, max_stalled=4)
            batches_done = 0
            while batches_done < 101:
                submit_budget.begin()
                selected, frame = self._prepare_auto_fill(FILTER, state)
                if selected["count"] == 0:
                    self.finish_queue()
                    summary["complete"] = True
                    self.o.organization["decomposition_pending"] = False
                    self.o.checkpoint()
                    self.rt.log(f"四星及以下清理完成：共分解 {summary['copies']} 件，获得 {summary['particles']} 闪光粒子。")
                    self.rt.check_focus()
                    if not self.rt.ui.enter_page(lambda: self.rt.key(27, 0),
                            "MaaNikki_Blessing_InventoryReady", source=READY, attempts=3):
                        raise IncompleteObservation("分解结束未能返回祝福闪光列表")
                    return
                if selected["gain"] is None or selected["gain"] <= 0:
                    raise RuntimeError("预计分解粒子数量无法确认")
                submit_focus_gen = self._last_read_focus_gen
                operation = self.o.begin_change("decompose", None, {"source": "four_star_auto_fill"}, frame,
                                                rarity_max=4, **selected)
                try:
                    self.o.check_generation(self.generation)
                    self.rt.check_focus()
                    if submit_focus_gen is None or self.rt._focus_generation != submit_focus_gen:
                        raise ForegroundChanged("提交分解前焦点变化，重新核对")
                except (RescanSlot, ForegroundChanged):
                    self.o.reconcile_change("cancelled_before_input")
                    if not submit_budget.advance():
                        raise
                    state = None
                    continue
                self.o.submit_change(operation)
                try:
                    self.click("submit", focus_generation=submit_focus_gen, require_same_focus=False)  # Never retry a destructive submission.
                except (RescanSlot, ForegroundChanged):
                    self.o.reconcile_change("cancelled_before_input")
                    if not submit_budget.advance():
                        raise
                    state = None
                    continue
                after, frame = self.await_result(selected)
                state = after
                batches_done += 1
                submit_budget.stalled = 0
                summary["batches"] += 1
                summary["copies"] += selected["count"]
                summary["particles"] += selected["gain"]
                self.o.confirm_change(operation, capacity_after=after["capacity"], particles_after=after["particles"],
                                      after=self.o.image(f"decompose_{summary['batches']:03d}.jpg", frame))
                self.rt.log(f"分解第 {summary['batches']} 批：{selected['count']} 件四星及以下，获得 {selected['gain']} 闪光粒子。", detail=True)
                if time.monotonic() - last_notice >= 30:
                    self.rt.log(f"正在分解四星及以下素材，已处理 {summary['copies']} 件。")
                    last_notice = time.monotonic()
            raise RuntimeError("分解批次超过上限，已停止")
        finally:
            if summary["copies"] > 0 or summary["particles"] > 0:
                try_record_adventure_entry(
                    ROOT,
                    self.rt,
                    category="blessing",
                    task="decompose",
                    title="四星及以下闪光分解",
                    detail=f"分解 {summary['copies']} 件",
                    runs=summary["copies"],
                    items={"闪光粒子": summary["particles"]} if summary["particles"] > 0 else {},
                    status="completed" if summary["complete"] else "partial",
                )

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
    """Free warehouse space with one five-star auto-fill batch (at most 100 copies).

    Runs only when no known embryo awaits an upgrade. New colours are read from the
    upgrade page's text and locked before this step, and auto-fill never takes locked
    items, so what it selects is surplus provided the user keeps keepers locked.
    """

    def run(self):
        o = self.o
        summary = o.report["duplicate_decomposition"] = {"complete": False, "copies": 0, "particles": 0}
        if (o.pending is not None or o.organization.get("recovery", {}).get("slots")
                or o.organization.get("unfinished") or o.queued_rolls()):
            summary["reason"] = "仍有待升级的胚子或待核对的操作"
            self.rt.log("仍有待升级的胚子或待核对的操作，本轮未分解重复闪光。")
            return False
        o.ensure_known_materials()
        o.report["phase"] = "decompose_five_star_duplicates"
        self.rt.log("仓库已满，正在分解一批未锁定的五星及以下闪光。", ui_text="仓库已满，正在分解一批闪光。")
        selected, frame = self._prepare_auto_fill(FIVE_STAR_FILTER, None)
        if selected["count"]:
            if selected["gain"] is None or selected["gain"] <= 0:
                raise RuntimeError("预计分解粒子数量无法确认")
            submit_budget = FocusRetryBudget(self.rt, max_stalled=2)
            while selected["count"]:
                submit_budget.begin()
                submit_focus_gen = self._last_read_focus_gen
                operation = o.begin_change("decompose", None, {"source": "five_star_auto_fill"}, frame,
                                           rarity_max=5, **selected)
                try:
                    self.o.check_generation(self.generation)
                    self.rt.check_focus()
                    if submit_focus_gen is None or self.rt._focus_generation != submit_focus_gen:
                        raise ForegroundChanged("提交分解前焦点变化，重新核对")
                except (RescanSlot, ForegroundChanged):
                    o.reconcile_change("cancelled_before_input")
                    if not submit_budget.advance():
                        raise
                    selected, frame = self._prepare_auto_fill(FIVE_STAR_FILTER, None)
                    continue
                o.submit_change(operation)
                try:
                    self.click("submit", focus_generation=submit_focus_gen, require_same_focus=False)  # Never retry a destructive submission.
                except (RescanSlot, ForegroundChanged):
                    o.reconcile_change("cancelled_before_input")
                    if not submit_budget.advance():
                        raise
                    selected, frame = self._prepare_auto_fill(FIVE_STAR_FILTER, None)
                    continue
                after, frame = self.await_result(selected, five_star=True)
                summary.update(copies=selected["count"], particles=selected["gain"])
                o.confirm_change(operation, capacity_after=after["capacity"], particles_after=after["particles"],
                                 after=o.image("duplicates.jpg", frame))
                break
        summary["complete"] = True
        # Position hints may now be stale; ordered lookups and slot rebuilds repair them.
        o.view.slot = None
        o.verified.clear()
        o.checkpoint()
        self.rt.log(f"重复闪光分解完成：分解 {summary['copies']} 件，获得 {summary['particles']} 闪光粒子。")
        if summary["copies"] > 0 or summary["particles"] > 0:
            try_record_adventure_entry(
                ROOT,
                self.rt,
                category="blessing",
                task="decompose",
                title="重复五星闪光分解",
                detail=f"分解 {summary['copies']} 件",
                runs=summary["copies"],
                items={"闪光粒子": summary["particles"]} if summary["particles"] > 0 else {},
            )
        self.rt.check_focus()
        if not self.rt.ui.enter_page(lambda: self.rt.key(27, 0), "MaaNikki_Blessing_InventoryReady",
                                     source=READY, attempts=3):
            raise IncompleteObservation("分解结束未能返回祝福闪光列表")
        return selected["count"] > 0
