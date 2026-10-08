"""Ability setup and nearby material tracking for insect route points."""
from __future__ import annotations
import math
import re
import time
import cv2
import numpy as np
from daily.ui import catalog
from .models import NavigationError
from .engine import Motion, Navigator

WHITE = ([0, 0, 230], [180, 60, 255])
SLOTS = ((1063, 257), (1205, 433), (1199, 640), (1048, 829),
         (641, 829), (498, 640), (485, 433), (610, 257))


class Insect:
    def __init__(self, actions):
        self.actions, self.rt = actions, actions.rt
        self.ui, self.inputs = self.rt.ui, actions.inputs
        self.previous_circle = None
        self.tracking = False

    def select(self):
        if self.actions.ability("MaaNikki_Navigation_InsectAbility"):
            return True
        if not self.ui.ability_page():
            raise NavigationError("未能进入能力配置，尚未开始捕虫。")
        slot = self.find_slot()
        if slot is None:
            plan = self.rt.setting("zhaoxi").get("ability_plan", 3)
            if type(plan) is not int or plan not in (1, 2, 3):
                raise NavigationError("捕虫能力方案设置无效，请选择方案一、二或三。")
            if not self.change_plan(plan):
                return False
            # Reuse an existing slot in the permitted plan before changing it.
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
                if not self.ui.find(roi=self.ui.roi("AreaAbilityChange"), text="捕虫"):
                    raise NavigationError("能力配置列表中未找到捕虫，请检查该能力是否已解锁。")
                if not self.ui.wait_page(save) or not self.ui.click_asset("ButtonAbilitySave"):
                    return False
                if (not self.ui.wait_page(asset="IconAbilityFeature") or self.selected_plan() != plan
                        or self.find_slot() != slot):
                    raise NavigationError("未能确认所选方案已装备捕虫，本项停止。")
                self.actions.event({"type": "ability_configured", "ability": "insect", "plan": plan, "slot": slot})
        if not self.rt.main() or not self.rt.key(f"ability_{slot}", .2):
            return False
        for _ in range(5):
            if not self.rt.pause(.5):
                return False
            if self.actions.ability("MaaNikki_Navigation_InsectAbility"):
                self.actions.event({"type": "ability_selected", "ability": "insect", "slot": slot})
                return True
        raise NavigationError("已切换捕虫快捷键，但未能确认当前能力图标。")

    def find_slot(self):
        frame = self.rt.capture()
        if frame is None:
            raise NavigationError("无法读取能力轮盘，未更改能力配置。")
        slot = None
        for index, (x, y) in enumerate(SLOTS, 1):
            area = [round((x-50)*2/3), round((y-50)*2/3), 67, 67]
            if self.ui.asset("IconAbilityInsect", image=frame, roi=area, scale=2/3, color=WHITE,
                             threshold=.8, method=cv2.TM_CCORR_NORMED):
                slot = index
                break
        return slot

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

    def track(self):
        if self.tracking:
            return True
        item = catalog(self.ui.resource)["materials"].get(self.actions.material)
        if not item or not item.get("track") or item.get("type") != "insect":
            raise NavigationError("此昆虫没有可用的精确追踪信息。")
        material_types = self.ui.feature(asset="IconMaterialTypeInsect",
            roi=self.ui.roi("AreaBigMapMaterialTypeSelect"), color=WHITE, scale=2/3, threshold=.75)
        if (not self.ui.open_map(self.actions.event) or not self.ui.enter_page(
                lambda: self.rt.click_template("MaaNikki_Navigation_MapFeature", attempts=1, wait_seconds=0),
                material_types, source="MaaNikki_Navigation_MapFeature",
                recover=lambda: self.ui.open_map(self.actions.event))):
            return False
        if not self.ui.find(roi=self.ui.roi("AreaBigMapMaterialTypeSelect"), asset="IconMaterialTypeInsect",
                            color=WHITE, scale=2/3, threshold=.75):
            return False
        if not self.rt.pause(.2):
            return False
        if not self.ui.find(roi=self.ui.roi("AreaBigMapMaterialSelect"), asset=item["game_img"],
                            scale=.45*2/3, threshold=.8):
            return False
        roi = self.ui.roi("AreaBigMapMaterialTrackConfirm")
        if not self.ui.wait_page(self.ui.feature(text="精确追踪|取消追踪", roi=roi)):
            raise NavigationError("未能确认昆虫精确追踪页面。")
        text = self.ui.text(roi).replace(" ", "")
        if text == "精确追踪":
            # Tracking toggles must be verified rather than blindly clicked twice.
            if not self.rt.action("Click", target=roi) or not self.ui.wait_page(
                    self.ui.feature(text="取消追踪", roi=roi)):
                return False
        elif text != "取消追踪":
            raise NavigationError("该昆虫尚未开启精确追踪。")
        self.tracking = self.rt.main()
        return self.tracking

    def degree(self, frame):
        reference = cv2.resize(frame, (1920, 1080), interpolation=cv2.INTER_LINEAR)
        mini = reference[22:222, 81:281]
        mask = cv2.inRange(cv2.cvtColor(mini, cv2.COLOR_BGR2HSV), np.array([13, 90, 160]), np.array([15, 200, 255]))
        cv2.circle(mask, (100, 100), 10, 255, -1)
        circles = cv2.HoughCircles(cv2.GaussianBlur(mask, (3, 3), 1), cv2.HOUGH_GRADIENT,
                                   dp=1, minDist=10, param1=60, param2=8, minRadius=14, maxRadius=18)
        if circles is None:
            return None
        anchor = self.previous_circle if self.previous_circle is not None else (100, 100)
        chosen = min(circles[0], key=lambda c: math.hypot(c[0]-anchor[0], c[1]-anchor[1]))
        self.previous_circle = chosen[:2]
        if math.hypot(chosen[0]-100, chosen[1]-100) > 30:
            self.previous_circle = None
            return None
        return math.degrees(math.atan2(chosen[0]-100, 100-chosen[1])) % 360

    def active(self, frame):
        x, y, w, h = self.ui.roi("AreaAbilityButton")
        mask = cv2.inRange(cv2.cvtColor(frame[y:y+h, x:x+w], cv2.COLOR_BGR2HSV),
                           np.array([0, 80, 200]), np.array([30, 110, 255]))
        return cv2.countNonZero(mask) > 200*(2/3)**2

    def catch(self, count):
        movement = self.rt.game_keys.movement()
        binding = self.rt.game_keys.get("capture")
        if binding.kind == "key" and binding.code in movement:
            raise NavigationError("捕虫动作不能与移动方向使用同一按键。")
        if not self.track():
            return False
        navigator = Navigator(self.actions.teleporter.locator, self.inputs, self.rt.capture,
                              lambda: self.rt.stopped, self.rt.pause, self.actions.event,
                              page_detector=lambda frame: bool(
                                  (result := self.rt.recognize("MaaNikki_MainDetected", image=frame)) and result.hit))
        route_navigator = self.actions.navigator
        if route_navigator is not None:
            navigator.turn_ratio = route_navigator.turn_ratio
            self.actions.event({"type": "camera_calibration_reused", "pixels_per_degree": navigator.turn_ratio})
        motion = Motion(self.inputs)
        navigator.motion = motion
        initial = self.actions.count
        end = time.monotonic()+60
        failed, lit = 0, 0
        baseline = self.actions.notification()
        try:
            while not self.rt.stopped and time.monotonic() < end and self.actions.count-initial < count:
                self.inputs.check()
                motion.tick()
                self.inputs._down(binding)
                frame = self.rt.capture()
                motion.tick()
                if frame is None:
                    return False
                if self.active(frame):
                    motion.stop()
                    failed = 0
                    lit += 1
                    if lit >= 2:
                        self.inputs._up(binding)
                        if not self.rt.main() or not self.rt.pause(.5):
                            return False
                        self.actions.obtained(baseline, seconds=1, interaction=True)
                        self.previous_circle = None
                        self.rt.pause(.8)
                        baseline = self.actions.notification()
                        lit = 0
                else:
                    lit = 0
                    degree = self.degree(frame)
                    if degree is None:
                        motion.stop()
                        failed += 1
                        if failed > 5:
                            self.rt.log("附近没有可追踪的昆虫，继续后续点位。")
                            break
                        motion.wait(.1)
                    else:
                        failed = 0
                        pose = self.actions.teleporter.locator.locate(frame)
                        # Do not approach on a partially corrected heading.
                        navigator.turn(degree, pose, stationary=True, tolerance=3)
                        motion.forward(.35)
                motion.wait(.05)
            return self.actions.count > initial
        finally:
            if route_navigator is not None:
                route_navigator.turn_ratio = navigator.turn_ratio
            motion.close()
