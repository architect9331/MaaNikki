"""Shared ability-wheel setup and selection for route actions."""
from __future__ import annotations

import re
import cv2

from .models import NavigationError

WHITE = ([0, 0, 230], [180, 60, 255])
SLOTS = ((1063, 257), (1205, 433), (1199, 640), (1048, 829),
         (641, 829), (498, 640), (485, 433), (610, 257))


class Ability:
    def __init__(self, actions, label, icon, node, ability_id, mode):
        self.actions, self.rt = actions, actions.rt
        self.ui, self.inputs = self.rt.ui, actions.inputs
        self.label, self.icon, self.node = label, icon, node
        self.ability_id, self.mode, self.slot = ability_id, mode, None

    def configure(self):
        if self.actions.ability(self.node):
            return True
        if not self.ui.ability_page():
            raise NavigationError(f"未能进入能力配置，尚未开始{self.label}。")
        slot = self.find_slot()
        if slot is None:
            plan = self.rt.setting(self.mode).get("ability_plan", 3)
            if type(plan) is not int or plan not in (1, 2, 3):
                raise NavigationError(f"{self.label}能力方案设置无效，请选择方案一、二或三。")
            if not self.change_plan(plan):
                return False
            slot = self.find_slot()
            if slot is None:
                slot = 8
                if self.selected_plan() != plan:
                    raise NavigationError("未能确认允许修改的能力方案，未更改能力轮盘。")
                x, y = SLOTS[slot-1]
                save = self.ui.feature(asset="ButtonAbilitySave", threshold=.75)
                def permitted_plan():
                    return bool(self.ui.asset("IconAbilityFeature", threshold=.99)
                                and self.selected_plan() == plan and not save())
                def restore_plan():
                    return self.rt.main() and self.ui.ability_page() and self.change_plan(plan)
                if not self.ui.enter_page(lambda: self.rt.action(
                        "Click", target=[round(x*2/3), round(y*2/3)]), save,
                        source=permitted_plan, recover=restore_plan):
                    return False
                if not self.ui.asset("ButtonAbilityConfig"):
                    text = self.ui.text(self.ui.roi("AreaAbilityChange"))
                    if len(text.split()) < 8 and not self.ui.click_asset("ButtonAbilityChangeList"):
                        return False
                if not self.ui.find(roi=self.ui.roi("AreaAbilityChange"), text=self.label):
                    raise NavigationError(f"能力配置列表中未找到{self.label}，请检查该能力是否已解锁。")
                if not self.ui.wait_page(save) or not self.ui.click_asset("ButtonAbilitySave"):
                    return False
                if (not self.ui.wait_page(asset="IconAbilityFeature") or self.selected_plan() != plan
                        or self.find_slot() != slot):
                    raise NavigationError(f"未能确认所选方案已装备{self.label}，本项停止。")
                self.actions.event({"type": "ability_configured", "ability": self.ability_id, "plan": plan, "slot": slot})
        self.slot = slot
        return True

    def select(self):
        if self.actions.ability(self.node):
            return True
        if self.slot is None and not self.configure():
            return False
        if not self.rt.main() or not self.rt.key(f"ability_{self.slot}", .2):
            return False
        for _ in range(5):
            if not self.rt.pause(.5):
                return False
            if self.actions.ability(self.node):
                self.actions.event({"type": "ability_selected", "ability": self.ability_id, "slot": self.slot})
                return True
        raise NavigationError(f"已切换{self.label}快捷键，但未能确认当前能力图标。")

    def find_slot(self):
        frame = self.rt.capture()
        if frame is None:
            raise NavigationError("无法读取能力轮盘，未更改能力配置。")
        for index, (x, y) in enumerate(SLOTS, 1):
            area = [round((x-50)*2/3), round((y-50)*2/3), 67, 67]
            if self.ui.asset(self.icon, image=frame, roi=area, scale=2/3, color=WHITE,
                             threshold=.8, method=cv2.TM_CCORR_NORMED):
                return index
        return None

    @staticmethod
    def parse_plan(text):
        match = re.fullmatch(r"自定义方案([一二三123])", re.sub(r"\s+", "", text))
        return {"一": 1, "二": 2, "三": 3, "1": 1, "2": 2, "3": 3}.get(match.group(1)) if match else None

    def selected_plan(self):
        return self.parse_plan(self.ui.text(self.ui.roi("AreaAbilityPlanChangeButton")))

    def change_plan(self, plan):
        if not self.ui.wait_page(asset="IconAbilityFeature"):
            raise NavigationError("未能确认能力配置页面，未更改能力轮盘。")
        if self.selected_plan() != plan:
            header = self.ui.roi("AreaAbilityPlanChangeButton")
            entry = self.ui.roi(f"AreaAbilityPlan{plan}Button")
            ability = self.ui.feature(asset="IconAbilityFeature", threshold=.99,
                                      method=cv2.TM_CCORR_NORMED)
            def plan_entry_visible():
                return self.parse_plan(self.ui.text(entry)) == plan
            def open_plan_list():
                return self.ui.ability_page() and self.ui.enter_page(
                    lambda: self.rt.action("Click", target=header), plan_entry_visible,
                    source=ability, recover=self.ui.ability_page)
            if not open_plan_list():
                raise NavigationError("未能确认能力方案列表，未更改能力轮盘。")
            def selected():
                return bool(ability() and self.selected_plan() == plan)
            if not self.ui.enter_page(lambda: self.rt.action("Click", target=entry), selected,
                                       source=plan_entry_visible, recover=open_plan_list):
                return False
        if self.ui.wait_page(lambda: self.selected_plan() == plan):
            self.actions.event({"type": "ability_plan_selected", "plan": plan})
            return True
        raise NavigationError("未能确认已切换到所选能力方案，未更改能力轮盘。")
