"""Home star fishing state handling."""
from __future__ import annotations

from enum import Enum
import time

import cv2
import numpy as np

from game_keys import Binding
from .ability import Ability, WHITE
from .models import NavigationError


class FishingState(Enum):
    FINISH = 1
    STRIKE = 2
    PULL_LINE = 3
    REEL_IN = 4
    SKIP = 5
    UNKNOWN = 6


class FishingResult(Enum):
    SUCCESS = 0
    NO_FISH = 1
    WRONG_POSITION = 2


STATES = (("IconFishingFinish", FishingState.FINISH),
          ("IconFishingStrike", FishingState.STRIKE),
          ("IconFishingPullLine", FishingState.PULL_LINE),
          ("IconFishingPullLineHome", FishingState.PULL_LINE),
          ("IconFishingReelIn", FishingState.REEL_IN),
          ("IconSkip", FishingState.SKIP))
TENSION_COLOR = ([20, 50, 245], [30, 90, 255])


class StarFishing:
    def __init__(self, actions):
        self.actions, self.rt = actions, actions.rt
        self.ui, self.inputs = self.rt.ui, actions.inputs
        self.ability = Ability(actions, "采星", "IconAbilityStarCollect",
                               "MaaNikki_Navigation_StarCollectAbility", "star_collect", "home")
        self.materials = {}

    def configure(self):
        return self.ability.configure() and self.rt.main()

    def wait(self, seconds):
        if not self.rt.pause(seconds):
            raise NavigationError("钓星已停止。")
        self.inputs.check()

    def press(self, name, delay=0):
        self.inputs.check()
        if not self.rt.key(name, delay):
            raise NavigationError("钓星输入未成功。")

    def state(self):
        self.inputs.check()
        frame = self.rt.capture()
        area = self.ui.roi("AreaFishingIcons")
        for icon, state in STATES:
            if self.ui.asset(icon, image=frame, roi=area, threshold=.8, gray=(210, 255),
                             scale=1 if icon == "IconSkip" else 2/3):
                return state
        return FishingState.UNKNOWN

    def tension(self):
        self.inputs.check()
        frame = self.rt.capture()
        if frame is None:
            raise NavigationError("无法读取钓星进度。")
        x, y, w, h = self.ui.roi("AreaFishingDetection")
        mask = cv2.inRange(cv2.cvtColor(frame[y:y+h, x:x+w], cv2.COLOR_BGR2HSV),
                           np.array(TENSION_COLOR[0]), np.array(TENSION_COLOR[1]))
        return cv2.countNonZero(mask)

    def pull_direction(self, key, pixels):
        # The fishing minigame uses physical A/D/S.
        binding = Binding("key", ord(key))
        try:
            self.inputs._down(binding)
            while not self.rt.stopped:
                self.wait(.2)
                current = self.tension()
                if pixels-current > 5 or current == 0:
                    pixels = current
                    if not pixels:
                        break
                else:
                    pixels = current
                    break
        finally:
            if self.rt.stopped or not self.inputs.foreground():
                self.inputs.release()
            else:
                self.inputs._up(binding)
        return pixels

    def pull_line(self):
        self.rt.log("钓星：拉扯鱼线。")
        pixels = self.tension()
        while not self.rt.stopped:
            pixels = self.pull_direction("A", pixels)
            if self.state() != FishingState.PULL_LINE:
                break
            pixels = self.pull_direction("D", pixels)
            if self.state() != FishingState.PULL_LINE:
                break

    def reel_in(self):
        self.rt.log("钓星：收线。")
        while not self.rt.stopped:
            begin = time.monotonic()
            self.press("fishing_reel")
            if self.state() != FishingState.REEL_IN:
                break
            gap = time.monotonic()-begin
            if gap < .18:
                self.wait(.18-gap)

    def skip(self):
        while not self.rt.stopped:
            self.wait(.5)
            with self.rt.observe(fresh=True):
                skip = (self.ui.asset("IconSkip", threshold=.8, gray=(210, 255))
                        or self.rt.hit("MaaNikki_Energy_SkipReward"))
            if skip:
                self.press("interact")
            if self.rt.hit("MaaNikki_MainDetected"):
                self.materials["陨星"] += 1
                self.actions.event({"type": "star_fished", "count": self.materials["陨星"]})
                self.rt.log(f"获得陨星，本次累计 {self.materials['陨星']}。")
                return
        raise NavigationError("钓星已停止。")

    def wait_main(self, gap):
        while not self.rt.stopped and not self.rt.hit("MaaNikki_MainDetected"):
            self.wait(gap)

    def cast(self):
        started = False
        deadline = time.monotonic()+5
        while not self.rt.stopped:
            if time.monotonic() >= deadline:
                return FishingResult.WRONG_POSITION
            if self.state() == FishingState.FINISH:
                break
            if not started:
                started = True
                self.press("sub_ability")
            else:
                self.wait(.5)
        idle_deadline = time.monotonic()+30
        self.wait(2)
        if self.ui.asset("IconFishingNoFish", threshold=.9, scale=2/3,
                         color=([0, 0, 175], [20, 255, 255])):
            self.press("sub_ability")
            self.wait_main(.5)
            return FishingResult.NO_FISH
        unknown, strikes = 0, 0
        while not self.rt.stopped:
            if idle_deadline is not None and time.monotonic() >= idle_deadline:
                self.press("sub_ability")
                self.wait_main(.2)
                return FishingResult.WRONG_POSITION
            state = self.state()
            if state != FishingState.UNKNOWN:
                unknown = 0
                if state == FishingState.FINISH:
                    self.wait(.5)
                elif state == FishingState.STRIKE:
                    if strikes > 3:
                        return FishingResult.NO_FISH
                    idle_deadline = None
                    self.press(83)
                    strikes += 1
                elif state == FishingState.PULL_LINE:
                    self.pull_line()
                elif state == FishingState.REEL_IN:
                    self.reel_in()
                elif state == FishingState.SKIP:
                    self.skip()
                    break
            else:
                unknown += 1
                if unknown > 4:
                    self.skip()
                    break
                self.wait(.1)
        return FishingResult.SUCCESS

    def run(self):
        # Preserve the upstream key-presence check, including a zero-catch run.
        if "陨星" in self.materials:
            self.rt.log("之前已执行钓星，无需再钓。")
            return True
        if not self.ability.select():
            return False
        self.press("ability_use")
        self.wait(2)
        available = False
        for _ in range(3):
            if self.ui.asset("IconAbilityFish", roi=self.ui.roi("AreaSubAbilityButton"),
                             scale=1/3, color=WHITE, threshold=.8):
                available = True
                break
            self.wait(1)
        if not available:
            self.rt.log("当前位置无法钓星，继续路线。")
            self.press("ability_use")
            return True
        self.materials["陨星"] = 0
        for _ in range(5):
            self.inputs.check()
            result = self.cast()
            self.wait(.5)
            if result in {FishingResult.NO_FISH, FishingResult.WRONG_POSITION}:
                break
        self.wait(2)
        self.rt.log("结束采星能力。")
        self.press("ability_use")
        return True
