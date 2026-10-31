"""Summit challenge flow."""
from __future__ import annotations

from task_control import task_time as time
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
    def __init__(self, context):
        super().__init__(context)
        self.report_reward_claimed = False

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

    def challenge_talk_visible(self):
        # Prompt text can be readable before the old white-letter template
        # matches its fade-in. Restrict OCR to complete interaction labels.
        with self.observe(fresh=True):
            return bool(self.asset("IconTalkFeature") or self.text(
                self.ui.roi("AreaPickup"), r"^(?:开始挑战|继续挑战|对话)$"))

    def start_challenge_dialogue(self):
        self.log("正在等待巅峰赛入场过渡结束，再向前靠近挑战交互点。", detail=True)
        walked = 0.0
        deadline = time.monotonic() + 60
        controls_ready_since = None
        waiting = True
        while not self.stopped and time.monotonic() < deadline:
            # Stop on the first image or text candidate and confirm while
            # stationary, including after the final movement step.
            if self.challenge_talk_visible():
                self.log(f"检测到挑战交互提示，累计向前移动 {walked:.2f} 秒，原地确认。", detail=True)
                if self.ui.wait_page(self.challenge_talk_visible, seconds=1.5):
                    self.log(f"已确认挑战对话按钮，累计向前移动 {walked:.2f} 秒，开始交互。", detail=True)
                    self.require(self.key("interact", 0), "未能开始挑战对话")
                    return
            if walked >= 12:
                break
            # The HUD can briefly appear before the arena entrance animation,
            # then disappear for over five seconds. Require sustained controls
            # before walking, and wait through later fades within this deadline.
            if not self.hit("MaaNikki_MainDetected"):
                controls_ready_since = None
                if not waiting:
                    self.log("赛场界面正在过渡，暂停移动并等待控制恢复。", detail=True)
                    waiting = True
                self.require(self.pause(.25), "挑战已停止")
                continue
            if controls_ready_since is None:
                controls_ready_since = time.monotonic()
            if time.monotonic() - controls_ready_since < 1.5:
                self.require(self.pause(.25), "挑战已停止")
                continue
            if waiting:
                self.log("已持续确认赛场界面，开始向前靠近挑战交互点。", detail=True)
                waiting = False
            self.require(self.hold("forward", .25) and self.pause(.25), "前往挑战对话失败")
            walked += .25
        self.require(not self.stopped, "挑战已停止")
        if walked < 12:
            raise RuntimeError(f"等待赛场控制恢复或靠近交互点超时，累计向前移动 {walked:.2f} 秒")
        raise RuntimeError(f"累计向前移动 {walked:.2f} 秒后仍未找到挑战对话按钮")

    def open_crown(self):
        if not self.ui.find_menu_entry(node="MaaNikki_Crown_Open"):
            return False
        if self.hit("MaaNikki_Crown_ModeReady"):
            self.log("已进入奇迹之冠玩法选择页，正在选择搭配赛。", detail=True)
            # The label identifies the mode; the arena above it receives the
            # click. Keep the pointer there while the entrance animates.
            with self.ui.preserve_cursor():
                return self.ui.enter_page(
                    lambda: self.click_template("MaaNikki_Crown_MatchOpen", attempts=1,
                                                wait_seconds=0, verify=False),
                    "MaaNikki_Crown_MatchReady", source="MaaNikki_Crown_ModeReady",
                    recover=lambda: self.ui.find_menu_entry(node="MaaNikki_Crown_Open"),
                    seconds=10)
        return self.hit("MaaNikki_Crown_MatchReady")

    def settle_summit_entry(self):
        if self.hit("MaaNikki_Crown_ReportReady"):
            self.log("检测到巅峰赛速报，正在领取奖励并等待奖励动画。", detail=True)
            # Claim once. A missing reward layer must stop this entrance rather
            # than recover through the menu and submit another claim.
            self.require(self.click_template("MaaNikki_Crown_ReportClaim", attempts=1,
                                              wait_seconds=1),
                         "领取速报奖励后未能确认奖励弹层")
            self.report_reward_claimed = True
        if self.hit("MaaNikki_Crown_EntryRewardReady"):
            self.log("已确认速报奖励，正在关闭奖励弹层。", detail=True)
            self.require(self.click_template("MaaNikki_Crown_EntryRewardContinue", attempts=1,
                                              wait_seconds=.25),
                         "未能关闭速报奖励弹层并返回巅峰赛页面")
            self.report_reward_claimed = True
        return self.ui.wait_page("MaaNikki_Crown_SummitReady", seconds=10)

    def open_summit(self):
        if (self.hit("MaaNikki_Crown_SummitArrivalReady")
                and self.ui.wait_page("MaaNikki_Crown_SummitArrivalReady")):
            return self.settle_summit_entry()
        if not self.open_crown():
            return False
        if (self.hit("MaaNikki_Crown_SummitArrivalReady")
                and self.ui.wait_page("MaaNikki_Crown_SummitArrivalReady")):
            return self.settle_summit_entry()
        def enter_summit():
            # The current Match page has three portals; its old entrance
            # coordinates are no longer a Summit entrance. Use the Summit tab.
            if self.hit("MaaNikki_Crown_MatchTitle"):
                # The tab's selected and unselected glyphs differ. Its fixed
                # hit area is safe once the Match page title is confirmed;
                # enter_page still requires the Summit arrival feature.
                return self.click_template("MaaNikki_Crown_SummitTab", attempts=1,
                                           wait_seconds=0, verify=False)
            return self.area_click("AreaMiraCrownEntrance")
        if not self.ui.enter_page(enter_summit,
            "MaaNikki_Crown_SummitArrivalReady", source="MaaNikki_Crown_PageReady",
            recover=self.open_crown):
            return False
        if not self.settle_summit_entry():
            return False
        return True

    def challenge(self):
        self.log("正在进入奇迹之冠巅峰赛。", detail=True)
        self.require(self.open_summit(), "未能进入巅峰赛")
        quick_reward = self.report_reward_claimed
        if not quick_reward:
            quick_reward = self.wait_asset("ButtonMiraCrownQuickReward", click=True)
        if quick_reward and not self.report_reward_claimed:
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
        self.start_challenge_dialogue()

        while not self.stopped:
            self.require(self.skip_dialogue() and self.pause(.5), "挑战已停止")
            roi = self.ui.roi("AreaDialogSelection")
            if not (self.ui.find(roi=roi, text="继续挑战", scroll=False)
                    or self.ui.find(roi=roi, text="开始挑战", scroll=False)):
                return "rewards"
            self.require(self.skip_dialogue(), "挑战已停止")
            self.wait_asset("ButtonMiraCrownNextStep")
            self.pause(1)
            self.log("正在使用推荐搭配完成本关挑战。", detail=True)
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
            self.log("未开启赛事嘉奖与兑换，本次跳过。", detail=True)
            return
        self.require(self.open_summit(), "未能进入巅峰赛")
        award = self.ui.feature(text="赛事嘉奖", roi=[0, 0, 1280, 230])
        self.require(self.ui.enter_page(lambda: self.wait_asset("ButtonMiraCrownAward", click=True),
            award, source="MaaNikki_Crown_SummitReady", recover=self.open_summit), "未能进入赛事嘉奖")
        if self.wait_asset("ButtonMiraCrownRewardGet", click=True):
            self.reward_popup()
        else:
            self.log("当前暂无赛事嘉奖可领取。", detail=True)
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
        self.log("正在检查巅峰赛进度；切出游戏会暂停，切回后自动继续。")
        from .starsea import HUB, Starsea
        travel = Starsea(self, self.ui.resource)
        try:
            with self.input_guard():
                teleporter = travel.teleporter()
                if teleporter.player_in_home():
                    self.log("当前位于家园，正在传送到星海无界枢纽后检查奇迹之冠。", detail=True)
                    teleporter.transport(HUB)
                else:
                    self.require(self.main(), "确认当前地图后未能返回主界面")
        finally:
            travel.report("crown_world")
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
                    self.log("未开启赛事嘉奖与兑换，本次跳过。", detail=True)
                    return not self.stopped
                self.rewards()
                return self.main() and not self.stopped
            return state == "finished" and not self.stopped
