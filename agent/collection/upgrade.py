"""Repeated Eureka upgrades with individually confirmed, never retried spending."""
from __future__ import annotations

import re

import numpy as np

from task_control import ForegroundChanged
from .details import COLORS
from .locking import READY
from .scanner import LEVEL, FocusRetryBudget, IncompleteObservation, RescanSlot, clean_text


UPGRADE = "MaaNikki_Pursuit_UpgradeReady"
RESULT = "MaaNikki_Pursuit_UpgradeResult"
FILTER = r"[5五]星及以下素材"


class UpgradeUI:
    def __init__(self, owner):
        self.owner, self.rt = owner, owner.rt
        self.layout = owner.upgrade_layout
        self._last_read_focus_gen = None

    def upgrade_ready(self):
        # The fixed right-hand panel is present at every upgrade level. The
        # result overlay dims it and shows the stat summary until dismissed.
        with self.rt.observe():
            frame = self.rt.frame()
            return (frame is not None and int(frame[180:215, 948:1028].max()) >= 180
                    and self.rt.hit(UPGRADE) and not self.rt.hit(RESULT))

    def frame(self, generation, page=UPGRADE):
        frame = self.rt.capture()
        self.owner.check_generation(generation)
        if frame is None or frame.shape[:2] != (720, 1280):
            raise IncompleteObservation("升级截图尺寸无效")
        result = self.rt.recognize(page, image=frame)
        self.owner.check_generation(generation)
        if not result or not result.hit:
            raise IncompleteObservation("升级页面特征未确认")
        overlay = self.rt.recognize(RESULT, image=frame) if page == UPGRADE else None
        if (overlay and overlay.hit) or (page == UPGRADE and int(frame[180:215, 948:1028].max()) < 180):
            raise IncompleteObservation("升级结果弹层尚未关闭，未读取底下升级页")
        return frame

    def text(self, field, frame):
        return clean_text(self.rt.text(self.layout[field], image=frame))

    def read(self, generation, keys=("name", "level", "xp", "maxed")):
        previous = None
        for _ in range(8):
            self.rt.check_focus()
            focus_gen = self.rt._focus_generation
            frame = self.frame(generation)
            fields = {key: self.text(key, frame) for key in keys}
            self.owner.check_generation(generation)
            if fields == previous:
                self._last_read_focus_gen = focus_gen
                return fields, frame
            previous = fields
            if not self.rt.pause(.2):
                raise IncompleteObservation("升级信息核对中断")
        raise IncompleteObservation("升级页面信息未稳定")

    @staticmethod
    def integer(text):
        numbers = re.findall(r"\d+", re.sub(r"[,，\s]", "", text))
        return int(numbers[0]) if len(numbers) == 1 else None

    @staticmethod
    def experience(text):
        match = re.fullmatch(r"(?:经验)?(\d+)[/／](\d+)", text)
        if not match:
            return None
        current, total = map(int, match.groups())
        return current if 0 <= current <= total and total > 0 else None

    def return_list(self, generation=None):
        self.rt.check_focus()
        # Returning via Esc is non-destructive and preserves selection. Allow a
        # short ready recheck and bounded retries when post-level-up UI settles.
        if not self.rt.ui.enter_page(
                lambda: self.rt.key(27, 0),
                READY,
                source=self.upgrade_ready,
                recover=lambda: self.rt.ui.wait_page(self.upgrade_ready, seconds=2),
                attempts=3):
            raise IncompleteObservation("升级页面未能返回祝福闪光列表")
        self.rt.check_focus()

    def clear(self, generation):
        self.owner.check_generation(generation)
        if not self.rt.action("Click", target=self.layout["clear"]):
            raise RuntimeError("未能清空升级材料")
        fields, _ = self.read(generation, ("material_count",))
        if self.integer(fields["material_count"]) != 0:
            raise RuntimeError("未确认升级材料为空")

    def _parse_preview_levels(self, raw_level, frame, current_level, maximum):
        """Parse '等级 X » Y' even when OCR drops '»' between current and green preview levels."""
        levels = [int(value) for value in re.findall(r"\d+", raw_level)]
        if 1 <= len(levels) <= 2 and levels[0] == current_level and all(
            current_level <= value <= maximum for value in levels
        ):
            return levels
        x, y, w, h = self.layout["level"]
        patch = frame[y:y + h, x:x + w].astype(np.int16)
        b, g, r = patch[:, :, 0], patch[:, :, 1], patch[:, :, 2]
        green = (g - r > 15) & (g > 110) & (g - b > 10)
        xs = np.where(green)[1]
        if len(xs) >= 10:
            split_x = max(8, min(w - 8, int(xs.min()) - 4))
            left_val = self.integer(clean_text(self.rt.text([x, y, split_x, h], image=frame)))
            right_val = self.integer(clean_text(self.rt.text([x + split_x, y, w - split_x, h], image=frame)))
            if left_val == current_level and right_val is not None and current_level <= right_val <= maximum:
                return [left_val, right_val]
        raise RuntimeError("升级预览等级无效，未提交升级")

    def full_color(self, generation, name, maximum=10):
        fields, frame = self.read(generation, ("name", "level", "maxed", "color"))
        colors = {mapped for label, mapped in COLORS.items() if label in fields["color"]}
        if (fields["name"] != name or self.integer(fields["level"]) != maximum
                or "等级已满" not in fields["maxed"] or len(colors) != 1):
            raise RuntimeError("未同时确认名称、满级等级、等级已满提示和颜色")
        color = next(iter(colors))
        if color not in self.owner.definitions[name]["colors"]:
            raise RuntimeError("满级页颜色不在当前图鉴中")
        return color, frame

    def _recover_upgrade_page(self, current, *, step=0, evidence=None, maximum=10):
        self.rt.check_focus()
        if self.rt.hit(RESULT):
            if not self.rt.ui.enter_page(
                    lambda: self.rt.key(70, 0), self.upgrade_ready, source=RESULT, attempts=3):
                raise IncompleteObservation("切回后未能关闭升级完成界面，已保存进度")
        if not self.rt.ui.wait_page(self.upgrade_ready, seconds=3):
            raise IncompleteObservation("切回后未确认祝福闪光升级页面，已保存进度")
        new_gen = self.owner.current_generation()
        chk, _ = self.read(new_gen, ("name", "level", "maxed", "material_count"))
        if chk["name"] != current["name"]:
            raise IncompleteObservation("切回后升级对象或等级已变化，已保存进度")
        if self.owner.pending is None:
            mat_count = self.integer(chk["material_count"])
            if mat_count is None or mat_count > 0:
                self.clear(new_gen)
                chk, _ = self.read(new_gen, ("name", "level", "maxed", "material_count"))
        chk_level = self.integer(chk["level"])
        level_ok = (
            chk_level is not None and (
                chk_level == current["level"]
                or (
                    step == 0
                    and isinstance(evidence, dict)
                    and evidence.get("level_source") == "numeral_model"
                    and 0 <= chk_level < maximum
                    and "等级已满" not in chk["maxed"]
                )
            )
        )
        if chk["name"] != current["name"] or not level_ok:
            raise IncompleteObservation("切回后升级对象或等级已变化，已保存进度")
        return new_gen

    def _recover_selection_after_upgrade(self, facts, list_gen):
        frame = self.owner.frame(list_gen)
        levels = self.owner.ocr(self.owner.grid, frame, LEVEL)
        for box in self.owner.cards(frame, levels):
            if not self.owner.selected(frame, box):
                continue
            rechecked, = self.owner.recognize_cards(frame, [box], facts["slot"], purpose="selected_recheck")
            matched = rechecked["model_facts"]
            if (matched["name"] == facts["name"]
                    and matched["slot"] == facts["slot"]
                    and matched["rarity"] == facts["rarity"]
                    and matched["color"] == facts["color"]
                    and self.owner.is_maximum(matched)):
                self.owner.protect_selected(facts, list_gen)
                return
        if getattr(self.owner, "prepared", False):
            self.owner.rebuild_slot(facts["slot"])
            return
        self.owner.protect_selected(facts, list_gen)

    def run(self, box, facts, evidence, generation, *, once=False):
        self.owner.ensure_known_materials()
        maximum = self.owner.catalog["upgrade_costs"][str(facts["rarity"])]["max_level"]
        if (facts["rarity"] not in ((3, 4, 5) if once else (5,))
                or not 0 <= facts["level"] < maximum):
            raise RuntimeError("升级对象星级或等级不符合当前处理模式")
        self.owner.check_generation(generation)
        if not self.rt.ui.enter_page(
                lambda: self.rt.action("Click", target=self.layout["entrance"]),
                self.upgrade_ready, source=READY, attempts=1):
            raise IncompleteObservation("未确认祝福闪光升级页面")
        current = dict(facts)
        page_gen = self.owner.current_generation()
        focus_switched = page_gen != generation
        # Each auto-fill may stop at an intermediate level. Keep this selection
        # until maxed or short of resources; never infer colour from the overlay.
        for step in range(100):
            # A fresh upgrade page, or a confirmed completed upgrade, starts
            # with no selected materials. Only clear a nonempty rejected batch.
            prepared = None
            pre_budget = FocusRetryBudget(self.rt, max_stalled=4)
            while True:
                pre_budget.begin()
                try:
                    fields, frame = self.read(page_gen)
                    page_level = self.integer(fields["level"])
                    if (
                        step == 0
                        and fields["name"] == current["name"]
                        and page_level is not None
                        and page_level != current["level"]
                        and 0 <= page_level < maximum
                        and "等级已满" not in fields["maxed"]
                        and isinstance(evidence, dict)
                        and evidence.get("level_source") == "numeral_model"
                    ):
                        current["level"] = page_level
                        if getattr(self.owner, "active", None) is not None:
                            self.owner.active["level"] = page_level
                    if fields["name"] != current["name"] or page_level != current["level"]:
                        raise RuntimeError("升级页名称或当前等级与选中闪光不一致")
                    old_xp = self.experience(fields["xp"])
                    if not re.fullmatch(FILTER, self.text("material_filter", frame)):
                        if (not self.rt.action("Click", target=self.layout["material_filter"])
                                or not self.rt.click_text("^" + FILTER + "$", self.layout["filter_choices"])):
                            raise RuntimeError("未能选择五星及以下升级素材")
                    frame = self.frame(page_gen)
                    if not re.fullmatch(FILTER, self.text("material_filter", frame)):
                        raise RuntimeError("升级素材星级未确认，未一键放入")
                    # The page, target and filter are already verified. Small button
                    # text is not a reliable OCR gate; verify the resulting count below.
                    self.owner.check_generation(page_gen)
                    if (not self.rt.action("Click", target=self.layout["auto_fill"])
                            or not self.rt.pause(.5)):
                        raise RuntimeError("未能一键放入升级素材")
                    fields, frame = self.read(page_gen, ("name", "level", "bling", "cost", "material_count", "material_filter"))
                    submit_focus_gen = self._last_read_focus_gen
                    if fields["name"] != current["name"]:
                        raise RuntimeError("放入材料后升级对象发生变化")
                    levels = self._parse_preview_levels(fields["level"], frame, current["level"], maximum)
                    amount = self.integer(fields["material_count"])
                    if amount is None:
                        raise RuntimeError("升级材料数量无法读取")
                    bling = self.integer(fields["bling"])
                    if bling is not None and bling >= 0:
                        self.owner.observe_bling(bling)
                    if amount == 0:
                        self.return_list()
                        return {"status": "materials_shortage", "facts": current}
                    cost = self.integer(fields["cost"])
                    if bling is None or cost is None or cost <= 0:
                        raise RuntimeError("升级噗灵持有量或费用无法读取")
                    reserve = 0 if once else max(0, int(getattr(self.owner, "reserve_bling", 0) or 0))
                    if bling < cost + reserve:
                        self.clear(page_gen)
                        self.return_list()
                        return {"status": "bling_shortage", "bling": bling, "cost": cost,
                                "reserve_bling": reserve, "facts": current}
                    if not re.fullmatch(FILTER, fields["material_filter"]):
                        raise RuntimeError("提交前升级素材选项发生变化")
                    self.owner.check_generation(page_gen)
                    self.rt.check_focus()
                    if submit_focus_gen is None or self.rt._focus_generation != submit_focus_gen:
                        raise ForegroundChanged("提交前焦点变化，重新核对升级预览")
                    operation = self.owner.begin_change("upgrade", current, evidence, frame,
                                                        preview_level=levels[-1], cost=cost, bling_before=bling,
                                                        material_rarity_max=5, experience_before=old_xp)
                    try:
                        self.owner.check_generation(page_gen)
                        self.owner.submit_change(operation)
                        clicked = self.rt.action("Click", target=self.layout["submit"], focus_generation=submit_focus_gen)
                    except (RescanSlot, ForegroundChanged):
                        self.owner.reconcile_change("cancelled_before_input")
                        raise
                    if not clicked:
                        raise RuntimeError("升级提交结果不确定，已停止且不重试")
                    prepared = (operation, old_xp, levels[-1], submit_focus_gen)
                    break
                except (RescanSlot, ForegroundChanged):
                    if self.owner._has_live_pending() or self.rt.stopped or not pre_budget.advance():
                        raise
                    focus_switched = True
                    page_gen = self._recover_upgrade_page(
                        current, step=step, evidence=evidence, maximum=maximum
                    )
            if prepared is None:
                raise IncompleteObservation("切回后升级准备未完成，已保存进度")
            operation, old_xp, preview_level, submit_focus_gen = prepared

            updated = None
            expect_result_modal = preview_level > current["level"]
            post_budget = FocusRetryBudget(self.rt, max_stalled=4)
            first_post = True
            while True:
                post_budget.begin()
                try:
                    if expect_result_modal and first_post and not focus_switched:
                        if not self.rt.ui.wait_page(RESULT, seconds=20):
                            if not self.upgrade_ready():
                                raise RuntimeError("未确认升级完成界面，已停止且不重试")
                    elif not expect_result_modal:
                        def _xp_settled():
                            if self.rt.hit(RESULT):
                                return True
                            if not self.upgrade_ready():
                                return False
                            snap = self.rt.frame()
                            if snap is None:
                                return False
                            cur_lv = self.integer(self.text("level", snap))
                            cur_xp = self.experience(self.text("xp", snap))
                            return bool(
                                (cur_lv is not None and cur_lv > current["level"])
                                or (
                                    cur_lv == current["level"]
                                    and cur_xp is not None
                                    and old_xp is not None
                                    and cur_xp > old_xp
                                )
                            )
                        if not self.rt.ui.wait_page(_xp_settled, seconds=10):
                            if not self.upgrade_ready():
                                raise RuntimeError("未确认升级完成界面，已停止且不重试")
                    elif not self.upgrade_ready():
                        if not self.rt.ui.wait_page(RESULT, seconds=20):
                            if not self.upgrade_ready():
                                raise RuntimeError("未确认升级完成界面，已停止且不重试")
                    first_post = False
                    page_gen = self.owner.current_generation()
                    if page_gen != generation:
                        focus_switched = True
                    if self.rt.hit(RESULT):
                        if not self.rt.ui.enter_page(lambda: self.rt.key(70, 0), self.upgrade_ready,
                                                     source=RESULT, attempts=4):
                            raise RuntimeError("未能关闭升级完成界面")
                    fields, frame = self.read(page_gen)
                    level, xp = self.integer(fields["level"]), self.experience(fields["xp"])
                    if (fields["name"] != current["name"] or level is None
                            or not current["level"] <= level <= maximum
                            or level == current["level"] and (xp is None or old_xp is None or xp <= old_xp)):
                        raise RuntimeError("升级后未确认同一对象的等级或经验增长，停止且不重试")
                    updated = {**current, "level": level, "color": None}
                    if level == maximum and (not once or facts["rarity"] == 5):
                        updated["color"], frame = self.full_color(page_gen, current["name"], maximum)
                    break
                except IncompleteObservation:
                    first_post = False
                    if self.rt.stopped or not post_budget.advance(stalled=True) or not self.rt.hit(RESULT):
                        raise
                    page_gen = self.owner.current_generation()
                    if not self.rt.ui.enter_page(lambda: self.rt.key(70, 0), self.upgrade_ready,
                                                 source=RESULT, attempts=4):
                        raise RuntimeError("未能关闭升级完成界面")
                except (RescanSlot, ForegroundChanged):
                    first_post = False
                    if self.rt.stopped or not post_budget.advance():
                        raise
                    focus_switched = True
                    page_gen = self.owner.current_generation()
            if updated is None:
                raise RuntimeError("切回后未能确认升级结果，已停止且不重试")

            operation["result_image"] = self.owner.image(
                f"upgrade_result_{len(self.owner.journal['operations']):04d}.jpg", frame)
            operation["experience_after"] = xp
            self.owner.confirm_upgrade(operation, current, updated)
            if level == maximum or once:
                self.return_list()
                list_gen = self.owner.current_generation()
                if self.rt._focus_generation != submit_focus_gen or list_gen != page_gen:
                    focus_switched = True
                # Esc preserves the selected item when focus was uninterrupted.
                if level == maximum and facts["rarity"] == 5:
                    protect_budget = FocusRetryBudget(self.rt, max_stalled=1)
                    while True:
                        protect_budget.begin()
                        try:
                            if focus_switched:
                                self._recover_selection_after_upgrade(updated, list_gen)
                            else:
                                self.owner.protect_selected(updated, list_gen)
                            break
                        except (RescanSlot, ForegroundChanged):
                            if self.owner._has_live_pending() or self.rt.stopped or not protect_budget.advance():
                                raise
                            focus_switched = True
                            list_gen = self.owner.current_generation()
                return {"status": "upgraded", "facts": updated}
            current = updated
        raise RuntimeError("同一闪光连续升级次数超过上限，已保存进度")
