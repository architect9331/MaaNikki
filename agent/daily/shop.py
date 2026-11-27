"""Shared shop entry, diagnostics, and weekly free gift claims."""
from __future__ import annotations

from .settings import ROOT
from account_state import AccountState, period_key


class ShopWorkflow:
    def __init__(self, runtime):
        self.rt, self.ui = runtime, runtime.ui
        self.account = AccountState.for_runtime(ROOT, runtime)
        self.period = period_key(self.TASK)
        self.completed = self.account.completed_items(self.TASK, self.period)

    def confirm(self, name, outcome):
        self.account.confirm(self.TASK, name, outcome, period=self.period)
        self.completed[name] = outcome

    def fail(self, reason):
        self.rt.log(f"{self.LABEL}：{reason}，本项未完成。")
        if self.rt.stopped:
            return False
        self.rt.diagnostic("shop", reason, details={"entry": self.ENTRY, "completed": self.completed})
        return False

    def open_shop(self):
        if not self.ui.page_matches("MaaNikki_Shop_PageReady"):
            if not self.rt.main() or not self.ui.enter_page(
                    lambda: self.rt.key("shop", 0), "MaaNikki_Shop_PageReady",
                    source="MaaNikki_MainDetected", recover=self.rt.main, seconds=10):
                return False
        return self.ui.wait_page("MaaNikki_Shop_PageReady")

    def run(self):
        completed = False
        try:
            if self.account.complete(self.TASK):
                self.rt.log(f"{self.LABEL}：当前周期已完成，跳过。")
                completed = True
            else:
                completed = self.scan() if self.open() else self.fail("未能进入目标商城页面")
        finally:
            if not self.rt.stopped:
                _, ok = self.rt.finish(self.ENTRY)
                if not ok:
                    self.fail("未能返回后续任务需要的页面")
                    completed = False
        return completed


class WeeklyShop(ShopWorkflow):
    TASK = "weekly_free_gifts"
    ENTRY = "MaaNikki_WeeklyFreeGifts"
    LABEL = "每周免费礼包"
    SIDEBAR_ROI = [4, 85, 126, 595]
    SIDEBAR_SCROLL_ROI = [15, 120, 100, 520]
    LIST_ROI = [140, 85, 1085, 605]
    SCROLL_ROI = [145, 95, 1075, 585]
    # Keep wheel events well inside the scrollable grid so the bottom-right
    # return-to-top floating button never swallows them and the narrow left rail stays clear.
    SCROLL_INPUT_ROI = [300, 150, 720, 400]
    GIFTS = {"虹迹赠礼", "芳馨赠礼", "星实置业之礼"}
    GIFT_PATTERN = "虹迹赠礼|芳馨赠礼|星实置业之礼"

    def open(self):
        ready = "MaaNikki_Shop_GiftsReady"
        if self.ui.page_matches(ready) and self.ui.wait_page(ready):
            return True
        return self.open_shop() and self.ui.find(
            roi=self.SIDEBAR_ROI, node="MaaNikki_Shop_GiftsTab",
            scroll_roi=self.SIDEBAR_SCROLL_ROI)

    def list_top(self):
        x, y, w, h = self.SCROLL_ROI
        image = self.rt.capture()
        for _ in range(12):
            if image is None or not self.ui.scroll(self.SCROLL_INPUT_ROI, 1800):
                return None
            following = self.rt.capture()
            if following is None:
                return None
            if self.ui.similarity(image[y:y+h, x:x+w], following[y:y+h, x:x+w]) > .995:
                return following
            image = following
        return None

    def _is_card_sold_out(self, title_box, image):
        if image is None:
            return False
        sold_out = self.rt.recognize("MaaNikki_Shop_SoldOut", {
            "roi": self.status_roi(title_box)}, image=image)
        if sold_out and sold_out.hit:
            return True
        # Also recognize 剩余限购 0/1 or 本周限购 0/1 in the top badge of the card.
        return bool(self.rt.text(self.limit_roi(title_box), r"(?:剩余|本周)限购\s*[0０]\s*[/／]", image=image))

    def claim(self, name, title_box):
        click_target = self.card_click_box(title_box)
        if not self.ui.enter_page(lambda: self.ui.click_box(click_target),
                "MaaNikki_Shop_DetailReady", source="MaaNikki_Shop_GiftsReady", attempts=2):
            return self.fail(f"未能打开{name}详情")
        # All price checks and the confirm box must refer to the same fresh
        # detail frame. Never infer a zero total from the list's free badge.
        image = self.rt.capture()
        if image is None:
            return self.fail("未能读取商品详情")
        detail_name = self.rt.text([335, 375, 195, 48], ".+", image=image)
        detail_name = "".join(detail_name.split())
        free = self.rt.recognize("MaaNikki_Shop_DetailFree", image=image)
        zero = self.rt.recognize("MaaNikki_Shop_TotalZero", image=image)
        confirm = self.rt.recognize("MaaNikki_Shop_Confirm", image=image)
        if (detail_name != name or not free or not free.hit
                or not zero or not zero.hit or not confirm or not confirm.hit):
            return self.fail(f"{name}的名称、免费标识、合计0或确认按钮未能同时确认")
        if not self.rt.image_current(image) or not self.ui.click_box(list(confirm.box)):
            return False
        if not self.ui.skip_reward() or not self.ui.wait_page("MaaNikki_Shop_GiftsReady"):
            return self.fail(f"{name}领取后未能确认返回商城")
        # A reward layer is the receipt; the sold-out card confirms this week's
        # quota was consumed before moving on to a different gift.
        image = self.rt.capture()
        if image is None:
            return self.fail(f"{name}领取后未确认已售罄")
        refreshed = self.rt.recognize("MaaNikki_Daily_OCR", {
            "roi": self.LIST_ROI, "expected": f"^{name}$"}, image=image)
        check_box = list(refreshed.box) if (refreshed and refreshed.hit and refreshed.box) else title_box
        if not self._is_card_sold_out(check_box, image):
            return self.fail(f"{name}领取后未确认已售罄")
        self.confirm(name, "claimed")
        self.rt.log(f"每周免费礼包：已领取{name}。")
        return True

    @staticmethod
    def card_click_box(title_box):
        x, y, w, h = title_box
        return [max(140, x), max(95, y - 55), max(w, 72), h + 55]

    @staticmethod
    def status_roi(title_box):
        x, y, w, _ = title_box
        return [max(140, round(x + w / 2) - 70), max(85, y - 125), 140, 95]

    @staticmethod
    def limit_roi(title_box):
        x, y, w, _ = title_box
        return [max(140, round(x + w / 2) - 85), max(85, y - 155), 170, 52]

    def _find_gift_titles(self, image):
        """Find target weekly free gift titles directly via OCR, independent of price-bar art."""
        result = self.rt.recognize("MaaNikki_Daily_OCR", {
            "roi": self.LIST_ROI, "expected": self.GIFT_PATTERN}, image=image)
        if not result or not result.hit:
            return []
        items = list(getattr(result, "filtered_results", None) or [])
        if not items and getattr(result, "best_result", None) is not None:
            items = [result.best_result]
        found = []
        for item in items:
            text = "".join(str(getattr(item, "text", "") or "").split())
            box = list(getattr(item, "box", ()) or ())
            if text in self.GIFTS and len(box) == 4:
                found.append((text, box))
        found.sort(key=lambda pair: (pair[1][1], pair[1][0]))
        return found

    def scan(self):
        image = self.list_top()
        if image is None:
            return self.fail("未能确认礼包列表顶部")
        x, y, w, h = self.SCROLL_ROI
        unchanged = 0
        for _ in range(60):
            if self.rt.stopped:
                return False
            matches = self._find_gift_titles(image)
            self.rt.evidence.observe("shop.gift_candidates", boxes=[box for _, box in matches])
            claimed = False
            for name, box in matches:
                if name in self.completed:
                    continue
                _bx, by, _bw, _bh = box
                if by < 145 or by > 670:
                    continue  # Wait until the card's status badge and price bar are cleanly visible.
                if self._is_card_sold_out(box, image):
                    self.confirm(name, "sold_out")
                    self.rt.log(f"每周免费礼包：{name}本周已领取，跳过。", detail=True)
                else:
                    if not self.rt.image_current(image) or not self.claim(name, box):
                        return False
                    claimed = True
                    break  # Discard every remaining box after the page changes.
            if len(self.completed) == len(self.GIFTS):
                self.rt.log("三个每周免费礼包均已处理。")
                return True
            if claimed:
                image = self.rt.capture()
            else:
                if not self.ui.scroll(self.SCROLL_INPUT_ROI, -480):
                    return False
                following = self.rt.capture()
                if following is None:
                    return self.fail("未能读取滚动后的礼包列表")
                if self.ui.similarity(image[y:y+h, x:x+w], following[y:y+h, x:x+w]) > .995:
                    unchanged += 1
                    if unchanged >= 2:
                        missing = "、".join(sorted(self.GIFTS-self.completed.keys()))
                        return self.fail(f"列表已到底，仍未确认{missing}")
                else:
                    unchanged = 0
                image = following
            if image is None:
                return self.fail("未能读取礼包列表")
        return self.fail("礼包列表查找超过上限")
