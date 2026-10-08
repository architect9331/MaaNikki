"""Summit challenge flow."""
from __future__ import annotations

import time
import re

from .runtime import Runtime, parameters


def parse_progress(raw):
    # OCR can attach the decorative star to the number (e.g. "*21/24").
    matches = re.findall(r"(?<!\d)(\d+)\s*[/／]\s*(\d+)(?!\d)", raw)
    if len(matches) != 1:
        return None
    current, total = map(int, matches[0])
    return (current, total) if 0 <= current <= total <= 999 and total > 0 else None


class Crown(Runtime):
    def require(self, result, message):
        if not result:
            raise RuntimeError(message)
        return result

    def asset(self, name):
        options = {"threshold": .98}
        if name in ("IconSkipDialog", "IconTalkFeature", "IconClickSkip", "IconSkip"):
            options["gray"] = (210, 255)
            options["threshold"] = {"IconSkipDialog": .73, "IconTalkFeature": .75,
                                    "IconClickSkip": .8, "IconSkip": .73}[name]
        if name == "IconTalkFeature":
            options["roi"] = self.ui.roi("AreaPickup")
            options["scale"] = 2/3
        elif name == "ButtonMiraCrownRank":
            options["gray"] = (50, 255)
        elif name == "ButtonMiraCrownSkipAll":
            options.update(threshold=.8, color=([20, 20, 180], [30, 100, 255]))
        elif name in ("ButtonMiraCrownShop", "ButtonMiraCrownAward"):
            options.update(threshold=.9, color=([0, 0, 190], [40, 40, 255]))
        return self.ui.asset(name, **options)

    def wait_asset(self, name, attempts=3, click=False):
        def visible():
            return self.asset(name)
        visible.__name__ = name
        if not self.ui.wait_page(visible, seconds=attempts):
            return False
        if not click:
            return True
        box = self.asset(name)
        return bool(box and self.action("Click", target=box))

    def area_click(self, name):
        return self.action("Click", target=self.ui.roi(name))

    def skip_dialogue(self):
        # Confirm absence twice: a transition frame can temporarily hide the prompt.
        while not self.stopped:
            if not self.pause(.5):
                return False
            if not self.asset("IconSkipDialog"):
                if not self.pause(.5):
                    return False
                if not self.asset("IconSkipDialog"):
                    return True
            if not self.key("interact", 0):
                return False
        return False

    def reward_popup(self):
        if self.wait_asset("IconClickSkip"):
            return self.pause(1) and self.key("interact", 0)
        return False

    def open_summit(self):
        if self.hit("MaaNikki_Crown_SummitReady") and self.ui.wait_page("MaaNikki_Crown_SummitReady"):
            return True
        if not self.ui.find_menu_entry(node="MaaNikki_Crown_Open"):
            return False
        if not self.ui.enter_page(lambda: self.area_click("AreaMiraCrownEntrance"),
            "MaaNikki_Crown_SummitReady", source="MaaNikki_Crown_PageReady",
            recover=lambda: self.ui.recover_page("MaaNikki_Crown_PageReady")):
            return False
        self.wait_asset("ButtonMiraCrownRank", attempts=1, click=True)
        return True

    def challenge(self):
        self.log("正在进入奇迹之冠巅峰赛。")
        self.require(self.open_summit(), "未能进入巅峰赛")
        quick_reward = self.wait_asset("ButtonMiraCrownQuickReward", click=True)
        if quick_reward:
            self.require(self.pause(5), "挑战已停止")
            self.reward_popup()
        def start_visible():
            return self.asset("ButtonMiraCrownStartChallenge")
        def choose_door():
            return self.ui.enter_page(lambda: self.area_click(
                "AreaMiraCrownSecondDoor" if quick_reward else "AreaMiraCrownThirdDoor"),
                start_visible, source="MaaNikki_Crown_SummitReady", recover=self.open_summit)
        if not choose_door():
            self.require(self.ui.wait_page("MaaNikki_Crown_SummitReady"), "未能确认巅峰赛挑战页面")
            self.log("当前入口没有可开始的挑战，本次结束。")
            # Preserve the early-finish branch; do not enter the reward/shop flow.
            return "finished"
        self.require(self.ui.enter_page(lambda: self.wait_asset("ButtonMiraCrownStartChallenge", click=True),
            "MaaNikki_MainDetected", source=start_visible,
            recover=lambda: self.open_summit() and choose_door(), seconds=30), "未进入巅峰赛内部")
        for _ in range(3):
            self.require(self.hold("forward", .8) and self.pause(.2), "前往挑战对话失败")
            if self.wait_asset("IconTalkFeature"):
                self.require(self.key("interact", 0), "未能开始挑战对话")
                break
        else:
            raise RuntimeError("未找到挑战对话按钮")

        while not self.stopped:
            self.require(self.skip_dialogue() and self.pause(.5), "挑战已停止")
            roi = self.ui.roi("AreaDialogSelection")
            if not (self.ui.find(roi=roi, text="继续挑战", scroll=False)
                    or self.ui.find(roi=roi, text="开始挑战", scroll=False)):
                return "rewards"
            self.require(self.skip_dialogue(), "挑战已停止")
            self.wait_asset("ButtonMiraCrownNextStep")
            self.pause(1)
            self.log("正在使用推荐搭配完成本关挑战。")
            self.require(self.ui.find(roi=self.ui.roi("AreaMiraCrownAutoMatchButton"),
                                      text="推荐搭配", scroll=False), "未找到推荐搭配按钮")
            self.pause(5)
            self.require(self.wait_asset("ButtonMiraCrownNextStep", click=True), "未找到下一步按钮")
            self.require(self.wait_asset("ButtonMiraCrownConfirmMatch", click=True), "未找到确认搭配按钮")
            deadline = time.monotonic() + 20
            while not self.stopped and time.monotonic() < deadline:
                self.wait_asset("ButtonMiraCrownSkipAll", attempts=1, click=True)
                if self.asset("IconClickSkip"):
                    self.key("interact", 0)
                    break
                self.pause(1)
            else:
                if not self.stopped:
                    self.key("interact", 0)
            self.pause(2)
            self.reward_popup()
            if self.hit("MaaNikki_MainDetected"):
                return "rewards"
        return "stopped"

    def rewards(self):
        if not parameters(self.context, "MaaNikki_Crown_Rewards").get("value", False):
            self.log("未开启赛事嘉奖与兑换，本次跳过。")
            return
        self.require(self.open_summit(), "未能进入巅峰赛")
        award = self.ui.feature(text="赛事嘉奖", roi=[0, 0, 1280, 230])
        self.require(self.ui.enter_page(lambda: self.wait_asset("ButtonMiraCrownAward", click=True),
            award, source="MaaNikki_Crown_SummitReady", recover=self.open_summit), "未能进入赛事嘉奖")
        if self.wait_asset("ButtonMiraCrownRewardGet", click=True):
            self.reward_popup()
        else:
            self.log("当前暂无赛事嘉奖可领取。")
        self.require(self.ui.enter_page(lambda: self.key("menu", 0), "MaaNikki_Crown_SummitReady",
            source=award, recover=self.open_summit), "未能关闭赛事嘉奖")
        shop = self.ui.feature(text="晶石兑换", roi=[0, 0, 1280, 230])
        self.require(self.ui.enter_page(lambda: self.wait_asset("ButtonMiraCrownShop", click=True),
            shop, source="MaaNikki_Crown_SummitReady", recover=self.open_summit), "未能进入晶石兑换")
        raw = parameters(self.context, "MaaNikki_Crown_Targets").get("value", "启示水晶,共鸣水晶")
        targets = [part.strip() for part in raw.replace("，", ",").split(",") if part.strip()]
        for target in dict.fromkeys(targets):
            if self.stopped:
                return
            self.require(self.ui.find(roi=self.ui.roi("AreaMiraCrownShopItemList"), text=target,
                                      exact=True, scroll=False), f"未找到“{target}”的兑换按钮")
            if not self.wait_asset("ButtonMiraCrownShopMax"):
                self.log(f"“{target}”当前无法继续兑换，跳过。")
                continue
            self.require(self.wait_asset("ButtonMiraCrownShopMax", click=True), "未找到最大数量按钮")
            self.require(self.wait_asset("ButtonMiraCrownShopConfirm", click=True), "未找到兑换确认按钮")
            self.reward_popup()
            self.log(f"已兑换“{target}”。")
            self.pause(.5)

    def run_crown(self):
        self.log("正在检查巅峰赛进度，请保持游戏前台。")
        self.require(self.ui.calendar(), "未能打开奇想日历")
        previous, progress = None, None
        roi = self.ui.roi("AreaMiraCrownOverview")
        for _ in range(6):
            frame = self.capture()
            raw = self.text(roi, image=frame) if frame is not None else ""
            value = parse_progress(raw)
            if value is not None and value == previous:
                progress = value
                break
            previous = value
            if not self.pause(.4):
                break
        self.require(progress is not None, "未能读取巅峰赛进度")
        current, total = progress
        self.log(f"奇迹之冠巅峰赛进度：{current}/{total}。")
        if current > 12:
            self.log("本期巅峰赛已挑战，本次跳过。")
            return True
        with self.input_guard():
            state = self.challenge()
            if state == "rewards":
                if not parameters(self.context, "MaaNikki_Crown_Rewards").get("value", False):
                    self.log("未开启赛事嘉奖与兑换，本次跳过。")
                    return not self.stopped
                self.rewards()
                return self.main() and not self.stopped
            return state == "finished" and not self.stopped
