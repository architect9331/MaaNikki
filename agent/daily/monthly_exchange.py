"""Monthly crystal exchanges guarded by selected currency and exact price."""
from __future__ import annotations

import re

import cv2

from .shop import ShopWorkflow


class MonthlyExchange(ShopWorkflow):
    ENTRY = "MaaNikki_MonthlyCrystalExchange"
    LABEL = "每月五抽兑换"
    ITEMS = ("启示水晶", "共鸣水晶")
    LIST_ROI = [285, 120, 930, 550]
    BLUE = ([100, 45, 55], [140, 255, 255])

    def quiet_page(self, image):
        if image is None:
            return False
        quiet = self.rt.recognize("MaaNikki_Shop_QuietSelected", image=image)
        surge = self.rt.recognize("MaaNikki_Shop_SurgeSelected", image=image)
        return bool(quiet and quiet.hit and surge and not surge.hit)

    def open(self):
        if not self.open_shop() or not self.ui.find(
                roi=[20, 100, 220, 575], node="MaaNikki_Shop_ResonanceTab",
                scroll_roi=[20, 110, 215, 560]):
            return False
        if not self.rt.click_template("MaaNikki_Shop_QuietTab"):
            return False
        return self.quiet_page(self.rt.capture())

    def detail(self, image, name):
        ready = self.rt.recognize("MaaNikki_Shop_DetailReady", image=image)
        title = "".join(self.rt.text([545, 195, 160, 40], ".+", image=image).split())
        return bool(ready and ready.hit and title == name and self.quiet_page(image))

    def price(self, image, *, total=False):
        # Anchor OCR to the verified blue currency icon, then read the rightmost
        # number so a struck-through original price cannot become the charge.
        roi = [710, 468, 225, 38] if total else [335, 422, 195, 42]
        icon = "IconShopQuietTotal" if total else "IconShopQuietUnit"
        box = self.ui.asset(icon, image=image, roi=roi, color=self.BLUE,
                            threshold=.9, method=cv2.TM_CCORR_NORMED)
        if not box:
            return None
        x, y, w, h = roi
        left = box[0]+box[2]
        result = self.rt.recognize("MaaNikki_Daily_OCR", {
            "roi": [left, y, x+w-left, h], "expected": ".+"}, image=image)
        if not result or not result.hit or not result.filtered_results:
            return None
        last = max(result.filtered_results, key=lambda item: item.box[0]+item.box[2])
        # Require whole numeric tokens; concatenated or ambiguous text fails.
        raw = last.text.strip()
        if not re.fullmatch(r"[0-9]+(?:\s+[0-9]+)*", raw):
            return None
        return int(raw.split()[-1])

    def remaining(self, image):
        raw = "".join(self.rt.text([335, 200, 195, 35],
            r"^本月限购\s*[1-5]\s*[/／]\s*5$", image=image).split())
        match = re.fullmatch(r"本月限购([1-5])[/／]5", raw)
        return int(match[1]) if match else None

    def exchange(self, name, box):
        if not self.ui.enter_page(lambda: self.ui.click_box(box),
                "MaaNikki_Shop_DetailReady", source="MaaNikki_Shop_QuietReady", attempts=1):
            return self.fail(f"未能打开{name}详情")
        image = self.rt.capture()
        if image is None or not self.detail(image, name):
            return self.fail(f"{name}详情未确认静谧水滴页面或商品名称")
        remaining = self.remaining(image)
        if remaining is None or self.price(image) != 40:
            return self.fail(f"{name}的每月限购余量或静谧水滴单价40未确认")
        maximum = self.rt.recognize("MaaNikki_Shop_ExchangeMax", image=image)
        if not maximum or not maximum.hit or not self.rt.image_current(image):
            return self.fail(f"{name}最大数量按钮未确认")
        if not self.ui.click_box(list(maximum.box)) or not self.rt.pause(.3):
            return False
        # Use two independent frames after Max; confirmation is submitted once
        # and only using the box from the final fully verified frame.
        previous = None
        confirm_box = None
        for observation in range(2):
            image = self.rt.capture()
            if image is None or not self.detail(image, name):
                return self.fail(f"{name}设置数量后页面或币种发生变化")
            raw = self.rt.text([438, 480, 90, 40], ".+", image=image).strip()
            quantity = int(raw) if re.fullmatch(r"[1-5]", raw) else None
            cost = self.price(image, total=True)
            confirm = self.rt.recognize("MaaNikki_Shop_ExchangeConfirm", image=image)
            current = (quantity, cost)
            if (quantity is None or quantity > remaining or cost != quantity*40
                    or self.remaining(image) != remaining or self.price(image) != 40
                    or not confirm or not confirm.hit
                    or (previous is not None and current != previous)):
                return self.fail(f"{name}数量1–5、限购余量或静谧水滴消耗量=数量×40未确认")
            previous = current
            confirm_box = list(confirm.box)
            if observation == 0 and not self.rt.pause(.2):
                return False
        if not self.rt.image_current(image) or not self.ui.click_box(confirm_box):
            return False
        if not self.ui.skip_reward() or not self.ui.wait_page("MaaNikki_Shop_QuietReady"):
            return self.fail(f"{name}兑换后未确认返回静谧水滴页面")
        self.completed[name] = {"quantity": quantity, "cost": cost}
        self.rt.log(f"{self.LABEL}：已兑换{name}×{quantity}，消耗静谧水滴{cost}。")
        return True

    def scan(self):
        for name in self.ITEMS:
            image = self.rt.capture()
            if not self.quiet_page(image):
                return self.fail("未确认选中静谧水滴兑换，停止兑换")
            result = self.rt.recognize("MaaNikki_Daily_OCR", {
                "roi": self.LIST_ROI, "expected": "^"+name+"$"}, image=image)
            if not result or not result.hit:
                self.completed[name] = "not_visible"
                self.rt.log(f"{self.LABEL}：当前画面未找到{name}，按规则跳过，不滚动查找。")
                continue
            box = list(result.box)
            x, y, w, _ = box
            sold = self.rt.recognize("MaaNikki_Shop_SoldOut", {
                "roi": [max(285, round(x+w/2)-60), max(120, y-85), 120, 65]}, image=image)
            if sold and sold.hit:
                self.completed[name] = "sold_out"
                self.rt.log(f"{self.LABEL}：{name}已售罄，跳过。")
                continue
            if not self.rt.image_current(image) or not self.exchange(name, box):
                return False
        self.rt.log("每月五抽兑换检查完成。")
        return not self.rt.stopped
