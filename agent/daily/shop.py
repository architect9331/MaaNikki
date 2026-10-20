"""Shared shop entry, diagnostics, and weekly free gift claims."""
from __future__ import annotations

import json
from datetime import datetime

import cv2

from .settings import ROOT
from account_state import AccountState, period_key


class ShopWorkflow:
    def __init__(self, runtime):
        self.rt, self.ui = runtime, runtime.ui
        self.account = AccountState(ROOT)
        self.period = period_key(self.TASK)
        self.completed = self.account.completed_items(self.TASK, self.period)

    def confirm(self, name, outcome):
        self.account.confirm(self.TASK, name, outcome, period=self.period)
        self.completed[name] = outcome

    def fail(self, reason):
        self.rt.log(f"{self.LABEL}：{reason}，本项未完成。")
        if self.rt.stopped:
            return False
        try:
            folder = ROOT / "debug/shop"
            folder.mkdir(parents=True, exist_ok=True)
            name = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            record = {"reason": reason, "completed": self.completed}
            image = self.rt.capture()
            if image is not None:
                ok, png = cv2.imencode(".png", image[:690])
                if ok:
                    image_folder = ROOT / "debug/on_error"
                    image_folder.mkdir(parents=True, exist_ok=True)
                    image_name = name+"_"+self.ENTRY+".png"
                    (image_folder / image_name).write_bytes(png.tobytes())
                    record["image"] = "on_error/"+image_name
            (folder / (name+".json")).write_text(
                json.dumps(record, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        except (OSError, cv2.error):
            self.rt.log("商城现场记录未能保存。")
        return False

    def open_shop(self):
        if not self.ui.page_matches("MaaNikki_Shop_PageReady"):
            if not self.rt.main() or not self.ui.enter_page(
                    lambda: self.rt.key("shop", 0), "MaaNikki_Shop_PageReady",
                    source="MaaNikki_MainDetected", recover=self.rt.main, seconds=10):
                return False
        return self.ui.wait_page("MaaNikki_Shop_PageReady")

    def run(self):
        try:
            if self.account.complete(self.TASK):
                self.rt.log(f"{self.LABEL}：当前周期已完成，跳过。")
                return True
            return self.scan() if self.open() else self.fail("未能进入目标商城页面")
        finally:
            if not self.rt.stopped:
                _, ok = self.rt.finish(self.ENTRY)
                if not ok:
                    self.fail("未能返回后续任务需要的页面")
                    return False


class WeeklyShop(ShopWorkflow):
    TASK = "weekly_free_gifts"
    ENTRY = "MaaNikki_WeeklyFreeGifts"
    LABEL = "每周免费礼包"
    LIST_ROI = [285, 60, 930, 625]
    SCROLL_ROI = [300, 70, 910, 610]
    GIFTS = {"虹迹赠礼", "芳馨赠礼", "星实置业之礼"}

    def open(self):
        ready = "MaaNikki_Shop_GiftsReady"
        if self.ui.page_matches(ready) and self.ui.wait_page(ready):
            return True
        return self.open_shop() and self.ui.find(
            roi=[20, 100, 220, 575], node="MaaNikki_Shop_GiftsTab",
            scroll_roi=[20, 110, 215, 560])

    def list_top(self):
        x, y, w, h = self.SCROLL_ROI
        image = self.rt.capture()
        for _ in range(12):
            if image is None or not self.ui.scroll(self.SCROLL_ROI, 1800):
                return None
            following = self.rt.capture()
            if following is None:
                return None
            if self.ui.similarity(image[y:y+h, x:x+w], following[y:y+h, x:x+w]) > .995:
                return following
            image = following
        return None

    def claim(self, name, box):
        if not self.ui.enter_page(lambda: self.ui.click_box(box),
                "MaaNikki_Shop_DetailReady", source="MaaNikki_Shop_GiftsReady", attempts=1):
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
        sold_out = (self.rt.recognize("MaaNikki_Shop_SoldOut", {
            "roi": self.status_roi(box)}, image=image) if image is not None else None)
        if not sold_out or not sold_out.hit:
            return self.fail(f"{name}领取后未确认已售罄")
        self.confirm(name, "claimed")
        self.rt.log(f"每周免费礼包：已领取{name}。")
        return True

    @staticmethod
    def status_roi(box):
        x, y, w, _ = box
        return [max(285, round(x+w/2)-60), max(60, y-115), 120, 65]

    def scan(self):
        image = self.list_top()
        if image is None:
            return self.fail("未能确认礼包列表顶部")
        x, y, w, h = self.SCROLL_ROI
        unchanged = 0
        for _ in range(60):
            if self.rt.stopped:
                return False
            # Cached glyph matching locates all free labels in a single frame.
            # OCR is limited to the small title/status of each candidate card.
            boxes = self.ui.asset_boxes("ButtonShopFree", image=image, roi=self.LIST_ROI,
                                        threshold=.85, count=10, method=cv2.TM_CCOEFF_NORMED)
            claimed = False
            for box in sorted(boxes, key=lambda item: (item[1], item[0])):
                bx, by, bw, _ = box
                if by < 100:
                    continue  # The title of this partially clipped card is not visible.
                title_roi = [max(285, round(bx+bw/2)-82), by-42, 164, 36]
                name = "".join(self.rt.text(title_roi, ".+", image=image).split())
                if name not in self.GIFTS or name in self.completed:
                    continue
                sold_out = self.rt.recognize("MaaNikki_Shop_SoldOut", {
                    "roi": self.status_roi(box)}, image=image)
                if sold_out and sold_out.hit:
                    self.confirm(name, "sold_out")
                    self.rt.log(f"每周免费礼包：{name}本周已领取，跳过。")
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
                if not self.ui.scroll(self.SCROLL_ROI, -480):
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
