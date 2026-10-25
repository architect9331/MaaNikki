"""MVP page workflows shared by standalone and daily-task execution."""
from __future__ import annotations

import re
import json
import cv2
from task_control import task_time as time

from .ui import catalog, fields
from .settings import ROOT


class Gameplay:
    # Minimum energy for one challenge, not the selected batch's total cost.
    MIN_ENERGY = {"jihua": 10, "bless": 40, "monster": 40, "weekly": 60}
    REALM_LEVEL_ROI = [145, 90, 410, 600]
    # The level list ends before the recognition ROI's right/bottom margins.
    REALM_SCROLL_ROI = [165, 105, 335, 545]

    def __init__(self, runtime):
        self.rt, self.ui = runtime, runtime.ui
        self.energy_exhausted = False
        self.quick_unavailable = False

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
        if not shortage:
            self.rt.diagnostic("realm_confirmation", "未能确认挑战提交", image=frame,
                                details={"kind": kind, "cancel": cancel})
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
        if not self.rt.click_template(node):
            return False
        # Like toggles: submit once, then leave without clicking again.
        self.rt.log("已点击照片点赞按钮。")
        return self.rt.main()

    def realm(self, kind, maximize=True, entered=False, weekly_node=None, allow_exhausted=False,
              return_page="main", fixed_count=None, *, allow_unavailable=False, before_submit=None):
        if fixed_count is not None and (type(fixed_count) is not int or fixed_count != 1):
            raise ValueError("固定挑战数量目前仅支持一次")
        self.energy_exhausted = False
        self.quick_unavailable = False
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
                return self.jihua(maximize, allow_exhausted, fixed_count=fixed_count)
            node = weekly_node if kind == "weekly" else f"MaaNikki_{prefix}_Level"
            if not self.select_realm_level(kind, node):
                return False
            if kind == "bless" and not self.rt.click_template("MaaNikki_Bless_Difficulty"):
                return False
            quick = "MaaNikki_Weekly_Quick_Qiggeda" if kind == "weekly" else f"MaaNikki_{prefix}_Quick"
            confirm = "MaaNikki_Weekly_Confirm_Qiggeda" if kind == "weekly" else f"MaaNikki_{prefix}_Confirm"
            def recover_level():
                return (self.ui.realm(kind) and self.select_realm_level(kind, node)
                        and (kind != "bless" or self.rt.click_template("MaaNikki_Bless_Difficulty")))
            can_skip = allow_unavailable and kind == "bless"
            if not self.ui.enter_page(lambda: self.rt.click_template(quick, attempts=1, wait_seconds=0),
                    confirm, source=quick, recover=None if can_skip else recover_level,
                    attempts=1 if can_skip else 3):
                cancel = "MaaNikki_Weekly_Cancel" if kind == "weekly" else f"MaaNikki_{prefix}_Cancel"
                # Only a failure before opening the submission dialog can
                # authorize another realm. An unknown page remains an error.
                if (can_skip and not self.rt.stopped
                        and self.ui.wait_page("MaaNikki_Bless_PageReady", seconds=2)
                        and not self.rt.hit(confirm) and not self.rt.hit(cancel)):
                    self.quick_unavailable = True
                    return False
                return self.confirm_unavailable(kind, allow_exhausted,
                                                cancel if self.rt.hit(cancel) else None)
            if fixed_count is not None and not self.rt.set_amount(kind, 0):
                return False
            if fixed_count is None and maximize and kind != "weekly" and not self.rt.click_template(f"MaaNikki_{prefix}_Max", wait_seconds=.2):
                return False
            # Daily supplementation uses the game's default minimum quantity.
            # Only the standalone energy-clearing task selects Max.
            if before_submit is not None:
                before_submit()
            if kind == "bless":
                self.invalidate_blessing_inventory("即将提交祝福闪光挑战，奖励数量待复核")
            if not self.rt.click_template(confirm):
                cancel = "MaaNikki_Weekly_Cancel" if kind == "weekly" else f"MaaNikki_{prefix}_Cancel"
                return self.confirm_unavailable(kind, allow_exhausted, cancel)
            result = self.ui.skip_reward()
            if result and kind == "bless":
                self.invalidate_blessing_inventory("已确认祝福闪光挑战奖励，新增库存数量待复核")
            self.rt.log("挑战奖励已领取。" if result else "挑战奖励领取结果待确认，请检查游戏画面。",
                        detail=bool(result and before_submit is not None))
            return result
        finally:
            if not entered and not self.rt.stopped:
                returned = self.ui.calendar() if return_page == "calendar" else self.rt.main()
                if not returned:
                    self.quick_unavailable = False
                    self.rt.log("未能返回后续步骤需要的页面，本项停止。")
                    return False

    def invalidate_blessing_inventory(self, reason):
        # Applies to ordinary farming and daily supplementation too. Resource
        # acquisition never silently leaves an older full snapshot reusable.
        from collection.storage import InventoryStore
        catalog = json.loads((ROOT / "resource/data/blessing_catalog.json").read_text(encoding="utf-8"))
        InventoryStore(ROOT, catalog).invalidate(reason, source="realm_bless")

    def select_realm_level(self, kind, node):
        if self.ui.find(roi=self.REALM_LEVEL_ROI, node=node, scroll_roi=self.REALM_SCROLL_ROI):
            return True
        if not self.rt.stopped:
            self.rt.log("未找到所选幻境关卡。")
            self.realm_level_diagnostic(kind, node)
        return False

    def realm_level_diagnostic(self, kind, node):
        """Keep the failed list before the caller returns to the calendar/main."""
        definition = fields(self.rt.context, node)
        self.rt.diagnostic("realm_level", "未找到目标幻境关卡", details={
            "kind": kind, "node": node, "expected": definition.get("expected"),
            "roi": definition.get("roi", self.REALM_LEVEL_ROI),
            "scroll_roi": self.REALM_SCROLL_ROI})

    def jihua(self, maximize, allow_exhausted=False, fixed_count=None):
        submitted = False

        def enter():
            nonlocal submitted
            submitted = self.rt.click_template("MaaNikki_Jihua_Go", attempts=1,
                                               wait_seconds=0, verify=False)
            return submitted

        try:
            if not self.ui.enter_page(enter, "MaaNikki_MainDetected",
                    source="MaaNikki_Jihua_PageReady", attempts=1, seconds=30):
                return False
            if not self.jihua_open_table() or not self.jihua_select_target() or not self.jihua_select_material():
                return False
            if fixed_count is not None and not self.rt.set_amount("jihua", 0):
                return False
            if fixed_count is None and maximize and not self.rt.click_template("MaaNikki_Jihua_NumMax", wait_seconds=.2):
                return False
            if not self.rt.click_template("MaaNikki_Jihua_NumConfirm"):
                return False
            if not self.rt.click_template("MaaNikki_Jihua_FinalConfirm"):
                return self.confirm_unavailable("jihua", allow_exhausted)
            return self.jihua_reward()
        finally:
            # This realm has a separate world map; closing its menus does not
            # make the calendar's realm entrance usable. Clean up failures too.
            if submitted and not self.rt.stopped and not self.jihua_leave():
                return False

    def jihua_leave(self):
        self.rt.log("素材激化结束，正在后退穿过传送门离开幻境。")
        if (self.rt.main() and self.rt.hold("backward", 3)
                and self.ui.wait_page("MaaNikki_MainDetected", seconds=30)
                and self.ui.calendar()
                and self.rt.click_template("MaaNikki_Jihua_Huanjing", attempts=1, wait_seconds=0)):
            # The ordinary HUD is also present inside this realm. A working
            # realm entrance proves we have actually returned to the world.
            self.rt.log("已离开素材激化幻境，幻境挑战入口已恢复。")
            return True
        if self.rt.stopped:
            return False
        self.rt.log("未确认通过传送门离境，正在传送到星海无界枢纽。")
        from .starsea import Starsea
        if Starsea(self.rt, self.ui.resource).hub():
            return True
        self.rt.log("未能离开素材激化幻境，本项停止。")
        return False

    def jihua_reward(self):
        # Animation skip and reward skip are separate layers.
        if not self.rt.wait_hit("MaaNikki_Energy_SkipReward", 10):
            return False
        if not self.rt.pause(.5) or not self.rt.key("skip", .5):
            return False
        return self.ui.skip_reward()

    def jihua_open_table(self):
        from .runtime import INTERACTION_ROI
        # World interaction labels move with the object/camera. Use the same
        # search area as other world prompts, rather than the old sample crop.
        prompt = self.ui.feature(text=r"素\s*材\s*激\s*化\s*台", roi=INTERACTION_ROI)
        steps = 0
        reason = "未找到素材激化台交互提示"
        for _ in range(6):
            # Check before the first move and allow a delayed prompt to settle
            # while stopped. Cap total movement at 2.5 seconds.
            if self.ui.wait_page(prompt, seconds=1.2):
                opened = self.ui.enter_page(lambda: self.rt.key("interact", 0),
                    "MaaNikki_Jihua_Target", source=prompt, recover=self.rt.main)
                if opened:
                    return True
                reason = "已识别素材激化台提示，但未能确认打开兑换界面"
                break
            if self.rt.stopped:
                return False
            if steps >= 5:
                break
            # HUD can briefly reappear before the actual loading screen starts.
            # Wait without inputs when it disappears, rather than aborting.
            if not self.rt.hit("MaaNikki_MainDetected"):
                self.rt.log("正在等待素材激化幻境加载完成。")
                if not self.ui.wait_page("MaaNikki_MainDetected", seconds=30):
                    reason = "等待素材激化幻境主界面恢复超时"
                    break
                # Loading may have ended beside the table; recheck its prompt
                # before moving so arrival does not cause an unnecessary step.
                if self.ui.wait_page(prompt, seconds=1.2):
                    opened = self.ui.enter_page(lambda: self.rt.key("interact", 0),
                        "MaaNikki_Jihua_Target", source=prompt, recover=self.rt.main)
                    if opened:
                        return True
                    reason = "已识别素材激化台提示，但未能确认打开兑换界面"
                    break
            if not self.rt.hold("forward", .5):
                return False
            steps += 1
        if not self.rt.stopped:
            self.rt.diagnostic("jihua_table", reason, details={
                "prompt_roi": INTERACTION_ROI, "approach_steps": steps,
                "planned_move_seconds": round(steps*.5, 2)})
            self.rt.log(reason+"，现场已保存。")
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

    def monthly_diagnostic(self, kind, stage, reason=None):
        """Retain the unfamiliar reward layer before ordinary page recovery."""
        if self.rt.stopped:
            return
        self.rt.diagnostic("monthly_"+stage, reason or "奖励页面状态待确认", details={"page": kind})

    def paradise_reward_return(self):
        def page_returned():
            with self.rt.observe():
                return (self.ui.page_matches("MaaNikki_Monthly_ParadiseReady")
                        and not self.rt.hit("MaaNikki_Monthly_ParadiseReward")
                        and not self.rt.hit("MaaNikki_Monthly_ParadiseContinue")
                        and not self.rt.hit("MaaNikki_RewardLayer"))

        deadline, dismissed = time.monotonic()+10, 0
        while not self.rt.stopped and time.monotonic() < deadline:
            with self.rt.observe(fresh=True):
                reward = self.rt.hit("MaaNikki_RewardLayer")
                continuation = self.rt.hit("MaaNikki_Monthly_ParadiseContinue")
                returned = not (reward or continuation) and page_returned()
            if reward or continuation:
                if dismissed >= 3:
                    break
                dismissed += 1
                self.rt.log("乐园构想：已识别奖励继续提示，尝试关闭奖励页面。")
                if not self.rt.key("skip", .5):
                    return False
            elif returned and dismissed:
                return self.ui.wait_page(page_returned)
            elif not self.rt.pause(.3):
                return False
        self.monthly_diagnostic("paradise", "return_unconfirmed", "领取后未能确认回到乐园构想")
        self.rt.log("乐园构想领取后未能确认返回，已记录奖励界面，尝试正常退出。")
        return False

    def monthly_tab_selected(self, page):
        """Travel tabs use a gold selected background and dark inactive tabs."""
        with self.rt.observe(fresh=True):
            frame = self.rt.frame()
            result = self.rt.recognize(f"MaaNikki_Monthly_{page}Tab", image=frame)
            if frame is None or not result or not result.hit:
                return False
            box = getattr(result.best_result, "box", None)
            if box is None or len(box) != 4:
                return False
            x, y, width, height = map(int, box)
            patch = frame[max(0, y):min(frame.shape[0], y+height),
                          max(0, x):min(frame.shape[1], x+width)]
            if patch.size == 0:
                return False
            # Bounds come from the existing monthly/task_tab.png selected
            # template; monthly/treasure_tab.png provides the inactive style.
            selected = cv2.inRange(cv2.cvtColor(patch, cv2.COLOR_BGR2HSV),
                                   (10, 40, 100), (35, 200, 255))
            return cv2.countNonZero(selected) / selected.size >= .45

    def monthly_rewards(self, kind):
        label = "乐园构想" if kind == "paradise" else "奇迹之旅"
        self.rt.log(f"已识别{label}，开始处理奖励。")
        claimed, task_claimed, treasure_available = False, False, False
        # Inspect Treasure first without claiming. Return after Task only
        # for an existing treasure reward or newly claimed task progress.
        pages = ("Task",) if kind == "paradise" else ("Treasure", "Task", "Treasure")
        for index, page in enumerate(pages):
            if index == 2 and not (treasure_available or task_claimed):
                continue
            node = ("MaaNikki_Monthly_ParadiseTaskTab" if kind == "paradise"
                    else f"MaaNikki_Monthly_{page}Tab")
            selection_node = "MaaNikki_Monthly_ParadiseEnabled" if kind == "paradise" else node
            if not (self.rt.context.get_node_data(selection_node) or {}).get("enabled", False):
                continue
            if index == 2:
                self.rt.log("正在检查并领取旅行秘宝奖励。")
            selected = kind == "travel" and self.monthly_tab_selected(page)
            if not selected and not self.rt.click_template(node):
                return False
            if not self.ui.wait_page("MaaNikki_Monthly_PageReady"):
                return False
            claim = ("MaaNikki_Monthly_ParadiseClaim" if kind == "paradise"
                     else f"MaaNikki_Monthly_Claim{page}")
            empty = False
            for attempt in range(3):
                with self.rt.observe(fresh=True):
                    available = self.rt.hit(claim)
                    if kind == "paradise" and not available and attempt == 2:
                        # With no claimable rewards, Paradise can show only
                        # ongoing-task glyphs and omit the bottom claim button.
                        empty = (self.ui.page_matches("MaaNikki_Monthly_ParadiseReady")
                                 and (self.rt.hit("MaaNikki_Monthly_ParadiseClaimEmpty")
                                      or self.rt.hit("MaaNikki_Monthly_ParadiseTaskOngoing"))
                                 and not self.rt.hit("MaaNikki_RewardLayer")
                                 and not self.rt.hit("MaaNikki_Monthly_ParadiseReward")
                                 and not self.rt.hit("MaaNikki_Monthly_ParadiseContinue"))
                if available:
                    if kind == "travel" and index == 0:
                        treasure_available = True
                        break
                    if not self.rt.click_template(claim, wait_seconds=.5):
                        return False
                    if kind == "paradise":
                        self.monthly_diagnostic(kind, "after_claim")
                        if not self.paradise_reward_return():
                            return False
                        claimed = True
                        break
                    rewarded = self.ui.skip_reward()
                    # Task progress need not produce an item reward layer;
                    # Treasure requires that layer as its receipt.
                    if not rewarded and page == "Treasure":
                        self.rt.log("奇迹之旅奖励未确认领取。")
                        return False
                    if self.rt.stopped:
                        return False
                    if not rewarded and not self.ui.wait_page("MaaNikki_Monthly_PageReady"):
                        self.rt.log("领取旅行任务后页面未能确认，本项停止。")
                        return False
                    claimed = True
                    if page == "Task":
                        task_claimed = True
                    break
                if attempt < 2 and not self.rt.pause(1):
                    return False
            else:
                if kind == "paradise" and not empty:
                    self.monthly_diagnostic(kind, "claim_unconfirmed", "未能确定一键领取按钮状态")
                    self.rt.log("乐园构想领取按钮状态未确认，已保存现场，本项未完成。")
                    return False
            if not self.rt.pause(.5):
                return False
        self.rt.log(f"{label}奖励已领取。" if claimed else f"{label}所选页签没有可领取的奖励。")
        return not self.rt.stopped

    def monthly(self):
        kind, label = "unknown", "奇迹之旅／乐园构想"
        completed = set()
        selected = {
            "travel": any((self.rt.context.get_node_data(f"MaaNikki_Monthly_{page}Tab") or {}).get("enabled", False)
                          for page in ("Task", "Treasure")),
            "paradise": (self.rt.context.get_node_data("MaaNikki_Monthly_ParadiseEnabled") or {}).get("enabled", False),
        }
        if not any(selected.values()):
            self.rt.log("未选择奇迹之旅或乐园构想奖励，跳过。")
            return True
        try:
            if not self.ui.monthly_page():
                self.rt.log("未能确认奇迹之旅或乐园构想页面。")
                return False
            for index in range(2):
                if self.ui.page_matches("MaaNikki_Monthly_ParadiseReady"):
                    kind, label = "paradise", "乐园构想"
                elif self.ui.page_matches("MaaNikki_Monthly_TravelReady"):
                    kind, label = "travel", "奇迹之旅"
                else:
                    self.monthly_diagnostic("unknown", "page_unconfirmed", "未能确定奖励页面类型")
                    return False
                if kind in completed:
                    self.monthly_diagnostic(kind, "switch_unconfirmed", "切换后仍停留在已处理页面")
                    return False
                if selected[kind] and not self.monthly_rewards(kind):
                    return False
                completed.add(kind)
                if index == 0:
                    other = "travel" if kind == "paradise" else "paradise"
                    if not selected[other]:
                        break
                    target = "Travel" if kind == "paradise" else "Paradise"
                    target_label = "奇迹之旅" if kind == "paradise" else "乐园构想"
                    self.rt.log(f"{label}处理完成，点击左下角切换到{target_label}。")
                    if not self.rt.click_template(f"MaaNikki_Monthly_To{target}"):
                        self.monthly_diagnostic(kind, "switch_unconfirmed", f"未能切换到{target_label}")
                        return False
            self.rt.log("所选奇迹之旅与乐园构想奖励已处理。")
            return True
        finally:
            if self.rt.stopped:
                return False
            if not self.rt.main():
                self.monthly_diagnostic(kind, "exit_unconfirmed", "未能返回主界面")
                self.rt.log(f"{label}处理后未能返回主界面，现场已记录，本项未正常结束。")
                return False
