"""Repeated Eureka upgrades with individually confirmed, never retried spending."""
from __future__ import annotations

import re

from .details import COLORS
from .locking import READY
from .scanner import clean_text


UPGRADE = "MaaNikki_Pursuit_UpgradeReady"
RESULT = "MaaNikki_Pursuit_UpgradeResult"
FILTER = r"[5五]星及以下素材"


class UpgradeUI:
    def __init__(self, owner):
        self.owner, self.rt = owner, owner.rt
        self.layout = owner.upgrade_layout

    def upgrade_ready(self):
        # The result overlay leaves the upgrade title visible behind it.
        # Requiring the overlay to disappear prevents enter_page from treating
        # the destination as already open and skipping the F input.
        return self.rt.hit(UPGRADE) and not self.rt.hit(RESULT)

    def frame(self, generation, page=UPGRADE):
        frame = self.rt.capture()
        self.owner.check_generation(generation)
        if frame is None or frame.shape[:2] != (720, 1280):
            raise RuntimeError("升级截图尺寸无效")
        result = self.rt.recognize(page, image=frame)
        self.owner.check_generation(generation)
        if not result or not result.hit:
            raise RuntimeError("升级页面特征未确认")
        overlay = self.rt.recognize(RESULT, image=frame) if page == UPGRADE else None
        if overlay and overlay.hit:
            raise RuntimeError("升级结果弹层尚未关闭，未读取底下升级页")
        return frame

    def text(self, field, frame):
        return clean_text(self.rt.text(self.layout[field], image=frame))

    def read(self, generation, keys=("name", "level", "xp", "maxed")):
        previous = None
        for _ in range(8):
            frame = self.frame(generation)
            fields = {key: self.text(key, frame) for key in keys}
            self.owner.check_generation(generation)
            if fields == previous:
                return fields, frame
            previous = fields
            if not self.rt.pause(.2):
                raise RuntimeError("升级信息核对中断")
        raise RuntimeError("升级页面信息未稳定")

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

    def return_list(self, generation):
        self.owner.check_generation(generation)
        if not self.rt.ui.enter_page(lambda: self.rt.key(27, 0), READY,
                                     source=self.upgrade_ready, attempts=1):
            raise RuntimeError("升级页面未能返回祝福闪光列表")
        self.owner.check_generation(generation)

    def clear(self, generation):
        self.owner.check_generation(generation)
        if not self.rt.action("Click", target=self.layout["clear"]):
            raise RuntimeError("未能清空升级材料")
        fields, _ = self.read(generation, ("material_count",))
        if self.integer(fields["material_count"]) != 0:
            raise RuntimeError("未确认升级材料为空")

    def full_color(self, generation, name):
        fields, frame = self.read(generation, ("name", "level", "maxed", "color"))
        colors = {mapped for label, mapped in COLORS.items() if label in fields["color"]}
        if (fields["name"] != name or self.integer(fields["level"]) != 10
                or "等级已满" not in fields["maxed"] or len(colors) != 1):
            raise RuntimeError("未同时确认名称、十级、等级已满提示和颜色")
        color = next(iter(colors))
        if color not in self.owner.definitions[name]["colors"]:
            raise RuntimeError("满级页颜色不在当前图鉴中")
        return color, frame

    def run(self, box, facts, evidence, generation):
        self.owner.ensure_known_materials()
        if facts["rarity"] != 5 or not 0 <= facts["level"] < 10:
            raise RuntimeError("只允许升级未满级五星祝福闪光")
        self.owner.check_generation(generation)
        if not self.rt.ui.enter_page(
                lambda: self.rt.action("Click", target=self.layout["entrance"]),
                self.upgrade_ready, source=READY, attempts=1):
            raise RuntimeError("未确认祝福闪光升级页面")
        current = dict(facts)
        # Each auto-fill may stop at an intermediate level. Keep this selection
        # until maxed or short of resources; never infer colour from the overlay.
        for _ in range(100):
            # A fresh upgrade page, or a confirmed completed upgrade, starts
            # with no selected materials. Only clear a nonempty rejected batch.
            fields, frame = self.read(generation)
            if fields["name"] != current["name"] or self.integer(fields["level"]) != current["level"]:
                raise RuntimeError("升级页名称或当前等级与选中五星不一致")
            old_xp = self.experience(fields["xp"])
            if not re.fullmatch(FILTER, self.text("material_filter", frame)):
                if (not self.rt.action("Click", target=self.layout["material_filter"])
                        or not self.rt.click_text("^" + FILTER + "$", self.layout["filter_choices"])):
                    raise RuntimeError("未能选择五星及以下升级素材")
            frame = self.frame(generation)
            if not re.fullmatch(FILTER, self.text("material_filter", frame)):
                raise RuntimeError("升级素材星级未确认，未一键放入")
            # The page, target and filter are already verified. Small button
            # text is not a reliable OCR gate; verify the resulting count below.
            self.owner.check_generation(generation)
            if (not self.rt.action("Click", target=self.layout["auto_fill"])
                    or not self.rt.pause(.5)):
                raise RuntimeError("未能一键放入升级素材")
            fields, frame = self.read(generation, ("name", "level", "bling", "cost", "material_count", "material_filter"))
            if fields["name"] != current["name"]:
                raise RuntimeError("放入材料后升级对象发生变化")
            levels = [int(value) for value in re.findall(r"\d+", fields["level"])]
            if (not 1 <= len(levels) <= 2 or levels[0] != current["level"]
                    or any(not current["level"] <= value <= 10 for value in levels)):
                raise RuntimeError("升级预览等级无效，未提交升级")
            amount = self.integer(fields["material_count"])
            if amount is None:
                raise RuntimeError("升级材料数量无法读取")
            if amount == 0:
                self.return_list(generation)
                return {"status": "materials_shortage", "facts": current}
            bling, cost = self.integer(fields["bling"]), self.integer(fields["cost"])
            if bling is None or cost is None or cost <= 0:
                raise RuntimeError("升级噗灵持有量或费用无法读取")
            if bling < cost:
                self.clear(generation)
                self.return_list(generation)
                return {"status": "bling_shortage", "bling": bling, "cost": cost, "facts": current}
            if not re.fullmatch(FILTER, fields["material_filter"]):
                raise RuntimeError("提交前升级素材选项发生变化")
            self.owner.check_generation(generation)
            operation = self.owner.begin_change("upgrade", current, evidence, frame,
                                                preview_level=levels[-1], cost=cost, bling_before=bling)
            self.owner.check_generation(generation)
            if not self.rt.action("Click", target=self.layout["submit"]):
                raise RuntimeError("升级提交结果不确定，已停止且不重试")
            if not self.rt.ui.wait_page(RESULT, seconds=20):
                raise RuntimeError("未确认升级完成界面，已停止且不重试")
            self.owner.check_generation(generation)
            if not self.rt.ui.enter_page(lambda: self.rt.key(70, 0), self.upgrade_ready,
                                         source=RESULT, attempts=1):
                raise RuntimeError("未能关闭升级完成界面")
            fields, frame = self.read(generation)
            level, xp = self.integer(fields["level"]), self.experience(fields["xp"])
            if (fields["name"] != current["name"] or level is None
                    or not current["level"] <= level <= 10
                    or level == current["level"] and (xp is None or old_xp is None or xp <= old_xp)):
                raise RuntimeError("升级后未确认同一对象的等级或经验增长，停止且不重试")
            updated = {**current, "level": level, "color": None}
            if level == 10:
                updated["color"], frame = self.full_color(generation, current["name"])
            operation["result_image"] = self.owner.image(
                f"upgrade_result_{len(self.owner.journal['operations']):04d}.png", frame)
            operation["experience_after"] = xp
            self.owner.confirm_upgrade(operation, current, updated)
            if level == 10:
                self.return_list(generation)
                # Esc preserves the selected item even after its cell moved.
                self.owner.protect_selected(updated, generation)
                return {"status": "upgraded", "facts": updated}
            current = updated
        raise RuntimeError("同一闪光连续升级次数超过上限，已保存进度")
