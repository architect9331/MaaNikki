"""Shared fishing flow for route actions and manual exploration assistance."""
from __future__ import annotations

from enum import Enum
from datetime import datetime
from task_control import task_time as time
import re

import cv2
import numpy as np

from game_keys import Binding
from daily.settings import ROOT
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


def cast_count(value, default):
    if value is None or value == "":
        return default
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if type(value) is not int or value <= 0:
        raise NavigationError("钓鱼抛竿次数必须为正整数。")
    return value


def fish_name(text):
    for line in text.splitlines():
        match = re.fullmatch(r"(.+?)[×xX]([0-9]+(?:\.[0-9]+)?)kg", re.sub(r"\s+", "", line))
        if match:
            return match.group(1)
    return None


class Fishing:
    def __init__(self, actions, kind="fish"):
        if kind not in (None, "fish", "home"):
            raise NavigationError("钓鱼类型无效。")
        self.kind = kind
        self.actions, self.rt = actions, actions.rt
        self.ui, self.inputs = self.rt.ui, actions.inputs
        self.ability = (Ability(actions, "采星", "IconAbilityStarCollect",
                               "MaaNikki_Navigation_StarCollectAbility", "star_collect", "home")
                        if kind == "home" else Ability(actions, "钓鱼", "IconAbilityFishing",
                               "MaaNikki_Navigation_FishingAbility", "fishing", "fishing"))
        self.materials = {}
        self.last_state_frame = None
        self.last_state_details = None
        self.previous_state = None

    @property
    def label(self):
        return "钓星" if self.kind == "home" else "钓鱼"

    @property
    def event_prefix(self):
        return "star_fishing" if self.kind == "home" else "fishing"

    def detect_kind(self):
        frame = self.last_state_frame
        self.kind = ("home" if frame is not None and self.ui.asset("IconFishingHomeFeature",
            image=frame, roi=self.ui.roi("AreaFishingIcons"), scale=2/3, gray=(210, 255), threshold=.8)
            else "fish")
        self.actions.event({"type": "fishing_type", "kind": self.kind})

    def switch_fishing(self):
        self.press("sub_ability" if self.kind == "home" else "ability_use")

    def configure(self):
        return self.ability.configure() and self.rt.main()

    def wait(self, seconds):
        if not self.rt.pause(seconds):
            raise NavigationError(f"{self.label}已停止。")
        self.inputs.check()

    def press(self, name, delay=0):
        self.inputs.check()
        if not self.rt.key(name, delay):
            raise NavigationError(f"{self.label}输入未成功。")

    def state(self, image=None):
        self.inputs.check()
        frame = self.rt.capture() if image is None else image
        self.last_state_frame = frame
        area = self.ui.roi("AreaFishingIcons")
        found, source, text = FishingState.UNKNOWN, None, ""
        for icon, state in STATES:
            if frame is not None and self.ui.asset(icon, image=frame, roi=area, threshold=.8, gray=(210, 255),
                             scale=1 if icon == "IconSkip" else 2/3):
                found, source = state, icon
                break
        if frame is not None and found == FishingState.UNKNOWN:
            text = re.sub(r"\s+", "", self.ui.text(area, image=frame))
            for label, state in (("提竿", FishingState.STRIKE), ("调整发力", FishingState.PULL_LINE),
                                 ("拉扯鱼线", FishingState.PULL_LINE), ("收线", FishingState.REEL_IN),
                                 ("收竿", FishingState.FINISH), ("跳过", FishingState.SKIP)):
                if label in text:
                    found, source = state, "text"
                    break
        self.last_state_details = {"state": found.name, "source": source, "text": text, "roi": area}
        if found != self.previous_state:
            self.actions.event({"type": self.event_prefix+"_state", **self.last_state_details})
            self.previous_state = found
        return found

    def fail_state(self, reason):
        record = {"type": self.event_prefix+"_unconfirmed", "reason": reason,
                  "recognition": self.last_state_details}
        try:
            frame = self.last_state_frame
            if frame is not None and frame.shape[:2] == (720, 1280):
                folder = ROOT / "logs/navigation"
                folder.mkdir(parents=True, exist_ok=True)
                filename = datetime.now().strftime("%Y%m%d-%H%M%S-%f")+"-fishing-state.png"
                # Fishing prompts are at the bottom; retain them and mask UID.
                frame = frame.copy()
                frame[700:, :240] = 0
                ok, png = cv2.imencode(".png", frame)
                if ok:
                    (folder / filename).write_bytes(png.tobytes())
                    record["file"] = filename
        except (OSError, cv2.error):
            record["image_status"] = "save_failed"
        self.actions.event(record)
        self.rt.log(reason+" 识别信息已记录，本轮停止。")
        # An unclassified fishing page must not reach ordinary Esc recovery.
        self.rt.navigation_halted = True
        raise NavigationError(reason)

    def tension(self):
        self.inputs.check()
        frame = self.rt.capture()
        if frame is None:
            raise NavigationError(f"无法读取{self.label}进度。")
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
        self.rt.log(f"{self.label}：拉扯鱼线。")
        pixels = self.tension()
        while not self.rt.stopped:
            pixels = self.pull_direction("A", pixels)
            if self.state() != FishingState.PULL_LINE:
                break
            pixels = self.pull_direction("D", pixels)
            if self.state() != FishingState.PULL_LINE:
                break

    def reel_in(self):
        self.rt.log(f"{self.label}：收线。")
        while not self.rt.stopped:
            begin = time.monotonic()
            self.press("fishing_reel")
            if self.state() != FishingState.REEL_IN:
                break
            gap = time.monotonic()-begin
            if gap < .18:
                self.wait(.18-gap)

    def skip(self):
        deadline = time.monotonic()+30
        while not self.rt.stopped and time.monotonic() < deadline:
            self.wait(.5)
            with self.rt.observe(fresh=True):
                skip = (self.ui.asset("IconSkip", threshold=.8, gray=(210, 255))
                        or self.rt.hit("MaaNikki_Energy_SkipReward"))
            if skip:
                self.press("interact")
            if self.rt.hit("MaaNikki_MainDetected"):
                self.record_material()
                return
        if not self.rt.stopped:
            self.state()
            self.fail_state(f"{self.label}奖励结束后未确认返回。")
        raise NavigationError(f"{self.label}已停止。")

    def record_material(self):
        name = "陨星" if self.kind == "home" else None
        if self.kind != "home":
            result = self.rt.recognize("MaaNikki_Daily_OCR", {"roi": self.ui.roi("AreaMaterialGetText")})
            if result and result.hit:
                rows = getattr(result, "filtered_results", []) or [result.best_result]
                name = fish_name("\n".join(getattr(row, "text", "") for row in rows))
        if not name:
            self.actions.event({"type": "fishing_material_unconfirmed"})
            self.rt.log("钓鱼流程已结束，但未识别到鱼名和重量提示。")
            return
        self.materials[name] = self.materials.get(name, 0)+1
        event = {"type": "star_fished" if self.kind == "home" else "fish_caught",
                 "material": name, "count": self.materials[name]}
        self.actions.event(event)
        self.rt.log(f"获得{name}，本次累计 {self.materials[name]}。")

    def wait_main(self, gap):
        deadline = time.monotonic()+5
        while not self.rt.stopped and not self.rt.hit("MaaNikki_MainDetected"):
            if time.monotonic() >= deadline:
                self.state()
                self.fail_state(f"收竿后未确认返回{self.label}入口界面。")
            self.wait(gap)

    def cast(self):
        started = False
        deadline = time.monotonic()+5
        while not self.rt.stopped:
            if time.monotonic() >= deadline:
                self.fail_state(f"已触发{self.label}，但未识别到{self.label}操作提示。")
            pending = self.state()
            if pending != FishingState.UNKNOWN:
                break
            if not started:
                started = True
                self.switch_fishing()
            else:
                self.wait(.5)
        if self.rt.stopped:
            raise NavigationError(f"{self.label}已停止。")
        if self.kind is None:
            self.detect_kind()
        idle_deadline = time.monotonic()+30
        # A fish can already be biting when the entrance is first recognized.
        # Only the waiting-for-bite phase needs the original settling delay.
        if pending == FishingState.FINISH:
            self.wait(2)
            pending = None
        if self.ui.asset("IconFishingNoFish", threshold=.9, scale=2/3,
                         color=([0, 0, 175], [20, 255, 255])):
            self.switch_fishing()
            self.wait_main(.5)
            return FishingResult.NO_FISH
        unknown_since, strikes = None, 0
        while not self.rt.stopped:
            if idle_deadline is not None and time.monotonic() >= idle_deadline:
                self.switch_fishing()
                self.wait_main(.2)
                return FishingResult.WRONG_POSITION
            state = pending if pending is not None else self.state()
            pending = None
            if state != FishingState.UNKNOWN:
                unknown_since = None
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
                if unknown_since is None:
                    unknown_since = time.monotonic()
                if time.monotonic()-unknown_since >= 3:
                    self.fail_state(f"{self.label}操作提示持续未识别。")
                self.wait(.1)
        return FishingResult.SUCCESS

    def run(self, *, passive=False, max_cast_count=None, use_rhythms=False):
        limit = cast_count(max_cast_count, None)
        if self.kind is None:
            if not passive:
                raise NavigationError("未指定主动钓鱼类型。")
            if self.state() == FishingState.UNKNOWN:
                return False
            self.detect_kind()
        if self.kind == "home" and passive:
            self.materials.setdefault("陨星", 0)
        elif self.kind == "home":
            if not self.prepare_home():
                return True
        elif not passive:
            if use_rhythms:
                rhythms = Ability(self.actions, "万物交响", "IconAbilityRhythms",
                                  "MaaNikki_Navigation_RhythmsAbility", "rhythms", "fishing")
                if rhythms.select():
                    self.press("ability_use")
                    self.wait(2)
                    if not self.ui.wait_page("MaaNikki_MainDetected", seconds=20):
                        raise NavigationError("打窝能力结束后未确认返回主页。")
                # Configuring bait can replace the remembered wheel slot.
                self.ability.slot = None
            if not self.ability.select():
                return False
        return self.run_casts(limit)

    def run_casts(self, limit=None):
        for _ in range(limit if limit is not None else (5 if self.kind == "home" else 3)):
            self.inputs.check()
            if self.kind == "home" and self.home_exhausted():
                self.actions.event({"type": "star_fishing_skipped", "reason": "meteors_exhausted"})
                self.rt.log("可感应陨星已耗尽，停止钓星。")
                break
            result = self.cast()
            self.wait(.5)
            if result in {FishingResult.NO_FISH, FishingResult.WRONG_POSITION}:
                break
        if self.kind == "home":
            self.wait(2)
            self.rt.log("结束采星能力。")
            self.exit_home()
        return True

    def exit_home(self):
        # Only toggle off a freshly observed active mode. An unconditional
        # right click can reopen it if the game already returned to the world.
        returned = self.ui.leave_star_collect()
        self.actions.event({"type": "star_collect_exit", "success": bool(returned)})
        if not returned:
            self.state()
            self.fail_state("结束采星后未确认返回普通主界面。")

    def home_exhausted(self, image=None):
        # A disabled refresh glyph still belongs to star sensing, not the world.
        # Require its title on the same frame before declaring global depletion.
        image = self.rt.capture() if image is None else image
        found = self.rt.recognize("MaaNikki_Home_StarCollectExhausted", image=image) if image is not None else None
        return bool(found and found.hit)

    def prepare_home(self):
        # Preserve the upstream key-presence check, including a zero-catch run.
        if "陨星" in self.materials:
            self.rt.log("之前已执行钓星，无需再钓。")
            return False
        if not self.ability.select():
            raise NavigationError("采星能力选择未完成。")
        # Do not inspect the secondary action until the mode actually opens.
        # Retry only while a fresh frame still confirms the ordinary world.
        with self.ui.preserve_cursor():
            entered = self.ui.enter_page(lambda: self.rt.key("ability_use"),
                "MaaNikki_Home_StarCollectReady", source=self.ui.star_collect_world_ready)
        self.actions.event({"type": "star_collect_enter", "success": bool(entered)})
        if not entered:
            raise NavigationError("未确认进入采星界面，尚未检查钓星副能力。")
        self.wait(2)
        available = False
        area = self.ui.roi("AreaSubAbilityButton")
        frame = None
        for attempt in range(1, 4):
            frame = self.rt.capture()
            if frame is not None and self.home_exhausted(frame):
                self.materials["陨星"] = 0
                self.actions.event({"type": "star_fishing_skipped", "reason": "meteors_exhausted"})
                self.rt.log("可感应陨星已耗尽，跳过钓星并继续路线。")
                self.exit_home()
                return False
            box = self.ui.asset("IconAbilityFish", image=frame, roi=area,
                                color=WHITE, threshold=.8) if frame is not None else None
            self.actions.event({"type": "star_fishing_availability", "attempt": attempt,
                                "available": bool(box), "box": box, "roi": area,
                                "threshold": .8, "scale": 1})
            if box:
                available = True
                break
            self.wait(1)
        if not available:
            record = {"type": "star_fishing_skipped", "reason": "subability_not_detected"}
            try:
                if frame is not None and frame.shape[:2] == (720, 1280):
                    folder = ROOT / "logs/navigation"
                    folder.mkdir(parents=True, exist_ok=True)
                    filename = datetime.now().strftime("%Y%m%d-%H%M%S-%f")+"-fishing-availability.png"
                    ok, png = cv2.imencode(".png", frame[:680])
                    if ok:
                        (folder / filename).write_bytes(png.tobytes())
                        record.update(file=filename, roi=[0, 0, 1280, 680])
            except (OSError, cv2.error):
                record["image_status"] = "save_failed"
            self.actions.event(record)
            self.rt.log("未识别到钓星副能力图标，跳过当前点，继续路线。")
            self.exit_home()
            return False
        self.materials["陨星"] = 0
        return True


class StarFishing(Fishing):
    def __init__(self, actions):
        super().__init__(actions, "home")
