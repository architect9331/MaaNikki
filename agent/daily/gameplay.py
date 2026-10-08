"""MVP page workflows shared by standalone and daily-task execution."""
from __future__ import annotations

import re
import time

from .ui import catalog, fields


class Gameplay:
    # Minimum energy for one challenge, not the selected batch's total cost.
    MIN_ENERGY = {"jihua": 10, "bless": 40, "monster": 40, "weekly": 60}

    def __init__(self, runtime):
        self.rt, self.ui = runtime, runtime.ui
        self.energy_exhausted = False

    def read_energy(self):
        """Read only on the confirmed calendar; an OCR miss is not zero."""
        if not self.rt.hit("MaaNikki_CalendarReady"):
            return None
        for _ in range(2):
            result = self.rt.recognize("MaaNikki_Energy_Remaining")
            raw = getattr(result.best_result, "text", "") if result and result.hit else ""
            match = re.fullmatch(r"\s*(\d+)\s*[/／]\s*(\d+)\s*", raw)
            if match:
                current, total = map(int, match.groups())
                if 0 <= current <= 9999 and 0 < total <= 9999:
                    return current
            if not self.rt.pause(.2):
                break
        return None

    def exhausted(self, allow_exhausted, energy=None):
        if self.rt.stopped:
            return False
        self.energy_exhausted = True
        remaining = f"剩余活跃能量 {energy} 点，" if energy is not None else ""
        self.rt.log(remaining + ("不足一次所选挑战，本次正常结束。" if allow_exhausted else
                                "不足一次所选挑战，本项未执行，不计入心愿完成。"))
        return allow_exhausted

    def confirm_unavailable(self, kind, allow_exhausted, cancel=None):
        """A missing/disabled button alone never proves energy exhaustion."""
        if self.rt.stopped:
            return False
        frame = self.rt.capture()
        shortage = bool(frame is not None and self.rt.text(
            [380, 200, 520, 340], r"(?:活跃)?能量(?:不足|不够)|体力(?:不足|不够)", image=frame))
        if cancel and not self.rt.click_template(cancel, attempts=1):
            self.rt.log("未能关闭挑战确认窗口，本项停止。")
            return False
        if shortage:
            return self.exhausted(allow_exhausted)
        # Re-read after returning safely, including when preflight OCR missed.
        if self.ui.calendar():
            energy = self.read_energy()
            if energy is not None and energy < self.MIN_ENERGY[kind]:
                return self.exhausted(allow_exhausted, energy)
        self.rt.log("未能确认挑战，且未确认活跃能量不足，请检查游戏画面。")
        return False

    def dig(self):
        try:
            if not self.ui.find_menu_entry(node="MaaNikki_Dig_Open"):
                self.rt.log("未找到美鸭梨挖掘入口。")
                return False
            if not self.rt.click_template("MaaNikki_Dig_Gather", attempts=1):
                raw = self.ui.text(self.ui.roi("AreaDigingNumText"))
                count = re.search(r"(\d+)\s*[/／]", raw)
                self.rt.log("仍在挖掘，本次没有可收获的奖励。" if count and int(count.group(1)) else
                            "没有正在挖掘的目标，请在游戏中设置。" if count else "无法读取当前挖掘状态。")
                return bool(count and int(count.group(1)) > 0)
            repeat = (self.rt.context.get_node_data("MaaNikki_Dig_Again") or {}).get("enabled", True)
            # These buttons are mutually exclusive choices in the same dialog.
            choice = "MaaNikki_Dig_Again" if repeat else "MaaNikki_Dig_GatherConfirm"
            label = "再次挖掘" if repeat else "确认收获"
            if not self.ui.wait_page(choice):
                self.rt.log(f"未确认挖掘收取界面的“{label}”按钮，本项停止。")
                return False
            self.rt.log(f"挖掘收取方式：{label}。")
            # Submit exactly one choice. Again returns directly to the dig page.
            if not self.rt.click_template(choice, attempts=1):
                return False
            if not repeat and not self.ui.skip_reward(node="MaaNikki_Dig_SkipReward"):
                self.rt.log("未确认挖掘奖励弹窗，本项未完成。")
                return False
            if not self.ui.wait_page("MaaNikki_Dig_PageReady"):
                self.rt.log("挖掘操作后未确认返回挖掘页面，本项未完成。")
                return False
            self.rt.log("已收取奖励并再次挖掘。" if repeat else "已收取挖掘奖励。")
            return True
        finally:
            if self.rt.stopped:
                return False
            if not self.rt.main():
                self.rt.log("挖掘处理后未能返回主界面，本项未正常结束。")
                return False

    def lookbook(self):
        if not self.ui.find_menu_entry(node="MaaNikki_Xinghai_OpenLookbook"):
            return False
        if not self.rt.click_template("MaaNikki_Xinghai_SelectPhoto"):
            self.rt.log("未能打开图册照片详情，本项停止。")
            return False
        # The dress/scene keeps animating. Wait only for the like control.
        node = "MaaNikki_Xinghai_Like"
        roi = fields(self.rt.context, node)["roi"]
        if not self.rt.wait_hit(node, 10) or not self.ui.stable(.98, roi=roi):
            self.rt.log("照片详情中的点赞按钮未能稳定识别，本项停止。")
            return False
        before = self.lookbook_like_count()
        if before is None:
            self.rt.log("未能读取照片点赞数，本项停止。")
            return False
        if not self.rt.click_template(node):
            return False
        deadline = time.monotonic()+5
        while not self.rt.stopped and time.monotonic() < deadline:
            count = self.lookbook_like_count()
            if count is not None and count > before:
                self.rt.log(f"照片点赞数已增加：{before} → {count}。")
                return self.rt.main()
            if not self.rt.pause(.3):
                break
        # Like toggles: an ambiguous response must never cause a second click.
        self.rt.log("点击后未确认点赞数增加，本项未完成。")
        return False

    def lookbook_like_count(self):
        previous = None
        for _ in range(3):
            if self.rt.stopped or not self.rt.hit("MaaNikki_Xinghai_PhotoReady"):
                return None
            result = self.rt.recognize("MaaNikki_Xinghai_LikeCount")
            raw = getattr(getattr(result, "best_result", None), "text", "").strip()
            count = int(raw) if result and result.hit and re.fullmatch(r"[0-9]{1,9}", raw) else None
            if count is not None and count == previous:
                return count
            previous = count
            if not self.rt.pause(.3):
                break
        return None

    def realm(self, kind, maximize=True, entered=False, weekly_node=None, allow_exhausted=False,
              return_page="main"):
        self.energy_exhausted = False
        prefix = {"jihua": "Jihua", "bless": "Bless", "monster": "Monster", "weekly": "Weekly"}[kind]
        try:
            if not entered and not self.rt.hit(f"MaaNikki_{prefix}_PageReady"):
                if not self.ui.calendar():
                    return False
                energy = self.read_energy()
                if energy is not None and energy < self.MIN_ENERGY[kind]:
                    return self.exhausted(allow_exhausted, energy)
            if not entered and not self.ui.realm(kind):
                return False
            if kind == "jihua":
                return self.jihua(maximize, allow_exhausted)
            node = weekly_node if kind == "weekly" else f"MaaNikki_{prefix}_Level"
            if not self.ui.find(roi=[145, 90, 410, 600], node=node):
                self.rt.log("未找到所选幻境关卡。")
                return False
            if kind == "bless" and not self.rt.click_template("MaaNikki_Bless_Difficulty"):
                return False
            quick = "MaaNikki_Weekly_Quick_Qiggeda" if kind == "weekly" else f"MaaNikki_{prefix}_Quick"
            confirm = "MaaNikki_Weekly_Confirm_Qiggeda" if kind == "weekly" else f"MaaNikki_{prefix}_Confirm"
            def recover_level():
                return (self.ui.realm(kind) and self.ui.find(roi=[145, 90, 410, 600], node=node)
                        and (kind != "bless" or self.rt.click_template("MaaNikki_Bless_Difficulty")))
            if not self.ui.enter_page(lambda: self.rt.click_template(quick, attempts=1, wait_seconds=0),
                    confirm, source=quick, recover=recover_level):
                cancel = "MaaNikki_Weekly_Cancel" if kind == "weekly" else f"MaaNikki_{prefix}_Cancel"
                return self.confirm_unavailable(kind, allow_exhausted,
                                                cancel if self.rt.hit(cancel) else None)
            if maximize and kind != "weekly" and not self.rt.click_template(f"MaaNikki_{prefix}_Max", wait_seconds=.2):
                return False
            # Daily supplementation uses the game's default minimum quantity.
            # Only the standalone energy-clearing task selects Max.
            if not self.rt.click_template(confirm):
                cancel = "MaaNikki_Weekly_Cancel" if kind == "weekly" else f"MaaNikki_{prefix}_Cancel"
                return self.confirm_unavailable(kind, allow_exhausted, cancel)
            result = self.ui.skip_reward()
            self.rt.log("挑战奖励已领取。" if result else "未确认挑战奖励，本项未完成。")
            return result
        finally:
            if not entered and not self.rt.stopped:
                returned = self.ui.calendar() if return_page == "calendar" else self.rt.main()
                if not returned:
                    self.rt.log("未能返回后续步骤需要的页面，本项停止。")
                    return False

    def jihua(self, maximize, allow_exhausted=False):
        if not self.rt.click_template("MaaNikki_Jihua_Go"):
            return False
        if not self.jihua_open_table() or not self.jihua_select_target() or not self.jihua_select_material():
            return False
        if maximize and not self.rt.click_template("MaaNikki_Jihua_NumMax", wait_seconds=.2):
            return False
        if not self.rt.click_template("MaaNikki_Jihua_NumConfirm"):
            return False
        if not self.rt.click_template("MaaNikki_Jihua_FinalConfirm"):
            return self.confirm_unavailable("jihua", allow_exhausted)
        return self.jihua_reward()

    def jihua_reward(self):
        # Animation skip and reward skip are separate layers.
        if not self.rt.wait_hit("MaaNikki_Energy_SkipReward", 10):
            return False
        if not self.rt.pause(.5) or not self.rt.key("skip", .5):
            return False
        return self.ui.skip_reward()

    def jihua_open_table(self):
        for _ in range(3):
            if not self.rt.hold("forward", .8) or not self.rt.pause(.2):
                return False
            if self.rt.text(self.ui.roi("AreaTextJihuatai"), "激化台"):
                return self.ui.enter_page(lambda: self.rt.key("interact", 0),
                    "MaaNikki_Jihua_Target", source=self.ui.feature(
                        text="激化台", roi=self.ui.roi("AreaTextJihuatai")), recover=self.rt.main)
        else:
            self.rt.log("未找到素材激化台。")
            return False

    def jihua_select_target(self):
        if not self.ui.find(roi=self.ui.roi("AreaJihuaTargetSelect"), node="MaaNikki_Jihua_Target"):
            return False
        return self.ui.wait_page(self.ui.feature(text=".+", roi=self.ui.roi("AreaJihuaCostSelect")))

    def jihua_select_material(self):
        most = (self.rt.context.get_node_data("MaaNikki_Jihua_MaterialMostOpen") or {}).get("enabled", False)
        selected = False
        if most:
            selected = (self.rt.action("Click", target=[93, 675]) and self.rt.pause(.2)
                        and self.ui.find(roi=[60, 585, 75, 85], text="数量", scroll=False)
                        and self.ui.click_asset("ButtonJihuaSort")
                        and self.ui.stable() and self.rt.action("Click", target=self.ui.roi("AreaJihuaFirstMaterial")))
        if not selected:
            if most:
                self.rt.log("未能按数量选择材料，尝试已设置的首选和备选材料。")
            options = fields(self.rt.context, "MaaNikki_Jihua_Material").get("expected", [])
            options = options if isinstance(options, list) else [options]
            metadata = catalog(self.ui.resource)["materials"]
            for label in options:
                label = {"小棉菊": "小绵菊"}.get(label, label)
                item = metadata.get(label)
                if not item or not item.get("jihua"):
                    self.rt.log(f"{label}不能作为激化材料，尝试备选。")
                    continue
                if self.ui.find(roi=self.ui.roi("AreaJihuaCostSelect"), asset=item["game_img"], scale=.5*2/3, threshold=.7):
                    selected = True
                    break
        if not selected:
            self.rt.log("未找到可用的激化材料。")
            return False
        return self.rt.pause(.5)

    def monthly(self):
        try:
            if not self.ui.monthly_page():
                self.rt.log("未能确认奇迹之旅页面。")
                return False
            claimed = False
            for page in ("Task", "Treasure"):
                node = f"MaaNikki_Monthly_{page}Tab"
                if not (self.rt.context.get_node_data(node) or {}).get("enabled", False):
                    continue
                if not self.rt.click_template(node):
                    return False
                if not self.ui.wait_page("MaaNikki_Monthly_PageReady"):
                    return False
                claim = f"MaaNikki_Monthly_Claim{page}"
                for attempt in range(3):
                    if self.rt.hit(claim):
                        if not self.rt.click_template(claim, wait_seconds=.5):
                            return False
                        rewarded = self.ui.skip_reward()
                        # Travel tasks can grant pass progress without an item
                        # reward layer. The source flow continues to Treasure;
                        # only Treasure requires that layer as its receipt.
                        if not rewarded and page == "Treasure":
                            self.rt.log("奇迹之旅奖励未确认领取。")
                            return False
                        if self.rt.stopped:
                            return False
                        if not rewarded and not self.ui.wait_page("MaaNikki_Monthly_PageReady"):
                            self.rt.log("领取旅行任务后页面未能确认，本项停止。")
                            return False
                        claimed = claimed or rewarded
                        break
                    if attempt < 2 and not self.rt.pause(1):
                        return False
                self.rt.pause(.5)
            self.rt.log("奇迹之旅奖励已领取。" if claimed else "奇迹之旅没有可领取的奖励。")
            return True
        finally:
            if self.rt.stopped:
                return False
            if not self.rt.main():
                self.rt.log("奇迹之旅处理后未能返回主界面，本项未正常结束。")
                return False
