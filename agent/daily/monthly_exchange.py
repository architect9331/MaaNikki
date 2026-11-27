"""Exchange monthly crystals using the shop's maximum quantity control."""
from __future__ import annotations

from .shop import ShopWorkflow
from .settings import ROOT
from adventure_report import try_record_adventure_entry


class MonthlyExchange(ShopWorkflow):
    TASK = "monthly_crystal_exchange"
    ENTRY = "MaaNikki_MonthlyCrystalExchange"
    LABEL = "每月五抽兑换"
    ITEMS = ("启示水晶", "共鸣水晶")
    TITLE_NODES = {
        "启示水晶": "MaaNikki_Shop_RevelationTitle",
        "共鸣水晶": "MaaNikki_Shop_ResonanceTitle",
    }
    SIDEBAR_ROI = [4, 85, 126, 595]
    SIDEBAR_SCROLL_ROI = [15, 120, 100, 520]
    LIST_ROI = [280, 90, 965, 610]

    def __init__(self, runtime):
        super().__init__(runtime)
        self.newly_exchanged: list[str] = []

    def open(self):
        if self.ui.page_matches("MaaNikki_Shop_QuietReady") and self.ui.wait_page("MaaNikki_Shop_QuietReady"):
            return True
        if not self.ui.page_matches("MaaNikki_Shop_ResonanceReady"):
            if not self.open_shop() or not self.ui.find(
                    roi=self.SIDEBAR_ROI, node="MaaNikki_Shop_ResonanceTab",
                    scroll_roi=self.SIDEBAR_SCROLL_ROI):
                return False
        if self.ui.page_matches("MaaNikki_Shop_QuietReady"):
            return True
        return self.rt.click_template("MaaNikki_Shop_QuietTab")

    @staticmethod
    def _card_status_roi(box):
        x, y, w, _ = box
        return [max(280, round(x + w / 2) - 70), max(90, y - 120), 140, 95]

    @staticmethod
    def _card_limit_roi(box):
        x, y, w, _ = box
        return [max(280, round(x + w / 2) - 85), max(88, y - 150), 170, 52]

    def exchange(self, name, box):
        if not self.ui.enter_page(lambda: self.ui.click_box(box),
                self.TITLE_NODES[name], source="MaaNikki_Shop_QuietReady", attempts=1):
            return self.fail(f"未能打开{name}详情")
        detail_image = self.rt.capture()
        if detail_image is not None:
            detail_sold = self.rt.recognize("MaaNikki_Shop_SoldOut", {
                "roi": [325, 220, 215, 175]}, image=detail_image)
            if (detail_sold and detail_sold.hit) or self.rt.text(
                    [325, 220, 215, 175], r"已售罄|本月限购\s*[0０]\s*[/／]\s*5", image=detail_image):
                self.ui.enter_page(self.rt.back, "MaaNikki_Shop_QuietReady",
                                   source=self.TITLE_NODES[name], attempts=2, seconds=5)
                self.confirm(name, "sold_out")
                self.rt.log(f"{self.LABEL}：{name}本月已兑换，跳过。", detail=True)
                return True
        if not self.rt.click_template("MaaNikki_Shop_ExchangeMax", attempts=1,
                                      wait_seconds=.3, verify=False):
            return self.fail(f"未能选择{name}最大数量")
        # Submit once; the game controls the purchase limit and affordability.
        if not self.rt.click_template("MaaNikki_Shop_ExchangeConfirm", attempts=1,
                                      wait_seconds=0, verify=False):
            return self.fail(f"未能确认{name}兑换")
        if not self.ui.skip_reward() or not self.ui.wait_page("MaaNikki_Shop_QuietReady"):
            return self.fail(f"{name}兑换后未能领取奖励并返回商店")
        image = self.rt.capture()
        if image is None:
            return self.fail(f"{name}兑换后未能复核额度")
        title = self.rt.recognize("MaaNikki_Daily_OCR", {
            "roi": self.LIST_ROI, "expected": "^"+name+"$"}, image=image)
        if title and title.hit:
            tbox = list(title.box)
            sold = self.rt.recognize("MaaNikki_Shop_SoldOut", {
                "roi": self._card_status_roi(tbox)}, image=image)
            zero_limit = self.rt.text(self._card_limit_roi(tbox), r"本月限购\s*[0０]\s*[/／]\s*5", image=image)
            if not ((sold and sold.hit) or zero_limit):
                return self.fail(f"{name}已兑换部分数量，剩余额度尚未完成")
        self.confirm(name, "exchanged")
        self.newly_exchanged.append(name)
        self.rt.log(f"{self.LABEL}：已按最大数量兑换{name}。")
        return True

    def scan(self):
        self.newly_exchanged = []
        for name in self.ITEMS:
            if name in self.completed:
                continue
            image = self.rt.capture()
            if image is None:
                return False
            # Safety guard: confirm we are still on 静谧水滴兑换 before touching any crystal card.
            quiet_ok = self.rt.recognize("MaaNikki_Shop_QuietReady", image=image)
            if not quiet_ok or not quiet_ok.hit:
                return self.fail("未能确认处于静谧水滴兑换页签")
            result = self.rt.recognize("MaaNikki_Daily_OCR", {
                "roi": self.LIST_ROI, "expected": "^"+name+"$"}, image=image)
            if not result or not result.hit:
                self.confirm(name, "already_exchanged")
                self.rt.log(f"{self.LABEL}：{name}本月已兑换，跳过。", detail=True)
                continue
            box = list(result.box)
            limit_text = self.rt.text(self._card_limit_roi(box), r"本月限购.*", image=image)
            if not limit_text:
                return self.fail(f"未在{name}卡片顶部确认本月限购标识，为防误花澎湃浪花已停止")
            sold = self.rt.recognize("MaaNikki_Shop_SoldOut", {
                "roi": self._card_status_roi(box)}, image=image)
            if (sold and sold.hit) or self.rt.text(
                    self._card_limit_roi(box), r"本月限购\s*[0０]\s*[/／]\s*5", image=image):
                self.confirm(name, "sold_out")
                self.rt.log(f"{self.LABEL}：{name}本月已兑换，跳过。", detail=True)
                continue
            if not self.rt.image_current(image) or not self.exchange(name, box):
                return False
        if self.newly_exchanged:
            try_record_adventure_entry(
                ROOT,
                self.rt,
                category="daily",
                task="monthly_exchange",
                title="每月五抽兑换",
                detail=f"已兑换：{'、'.join(self.newly_exchanged)}",
                runs=len(self.newly_exchanged),
            )
        self.rt.log("每月五抽兑换检查完成。")
        return not self.rt.stopped
