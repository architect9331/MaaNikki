"""Exchange monthly crystals using the shop's maximum quantity control."""
from __future__ import annotations

from .shop import ShopWorkflow


class MonthlyExchange(ShopWorkflow):
    ENTRY = "MaaNikki_MonthlyCrystalExchange"
    LABEL = "每月五抽兑换"
    ITEMS = ("启示水晶", "共鸣水晶")
    TITLE_NODES = {
        "启示水晶": "MaaNikki_Shop_RevelationTitle",
        "共鸣水晶": "MaaNikki_Shop_ResonanceTitle",
    }
    LIST_ROI = [285, 120, 930, 550]

    def open(self):
        if not self.open_shop() or not self.ui.find(
                roi=[20, 100, 220, 575], node="MaaNikki_Shop_ResonanceTab",
                scroll_roi=[20, 110, 215, 560]):
            return False
        return self.rt.click_template("MaaNikki_Shop_QuietTab")

    def exchange(self, name, box):
        if not self.ui.enter_page(lambda: self.ui.click_box(box),
                self.TITLE_NODES[name], source="MaaNikki_Shop_QuietReady", attempts=1):
            return self.fail(f"未能打开{name}详情")
        if not self.rt.click_template("MaaNikki_Shop_ExchangeMax", attempts=1,
                                      wait_seconds=.3, verify=False):
            return self.fail(f"未能选择{name}最大数量")
        # Submit once; the game controls the purchase limit and affordability.
        if not self.rt.click_template("MaaNikki_Shop_ExchangeConfirm", attempts=1,
                                      wait_seconds=0, verify=False):
            return self.fail(f"未能确认{name}兑换")
        if not self.ui.skip_reward() or not self.ui.wait_page("MaaNikki_Shop_QuietReady"):
            return self.fail(f"{name}兑换后未能领取奖励并返回商店")
        self.completed[name] = "exchanged"
        self.rt.log(f"{self.LABEL}：已按最大数量兑换{name}。")
        return True

    def scan(self):
        for name in self.ITEMS:
            image = self.rt.capture()
            if image is None:
                return False
            result = self.rt.recognize("MaaNikki_Daily_OCR", {
                "roi": self.LIST_ROI, "expected": "^"+name+"$"}, image=image)
            if not result or not result.hit:
                self.completed[name] = "already_exchanged"
                self.rt.log(f"{self.LABEL}：{name}本月已兑换，跳过。")
                continue
            box = list(result.box)
            x, y, w, _ = box
            sold = self.rt.recognize("MaaNikki_Shop_SoldOut", {
                "roi": [max(285, round(x+w/2)-60), max(120, y-85), 120, 65]}, image=image)
            if sold and sold.hit:
                self.completed[name] = "sold_out"
                self.rt.log(f"{self.LABEL}：{name}本月已兑换，跳过。")
                continue
            if not self.rt.image_current(image) or not self.exchange(name, box):
                return False
        self.rt.log("每月五抽兑换检查完成。")
        return not self.rt.stopped
