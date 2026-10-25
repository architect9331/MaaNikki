"""Claim all mail attachments and verify the disabled claim button afterward."""
from __future__ import annotations

import cv2

from .ui import fields


class Mailbox:
    ENTRY = "MaaNikki_Mailbox"
    PAGE = "MaaNikki_Mailbox_PageReady"
    CLAIM = "MaaNikki_Mailbox_ClaimAll"

    def __init__(self, runtime):
        self.rt, self.ui = runtime, runtime.ui

    def fail(self, reason):
        if not self.rt.stopped:
            self.rt.diagnostic("mailbox", reason)
            self.rt.log(f"邮箱领取：{reason}，本项未完成。")
        return False

    def button_state(self):
        # The page title, label, and button color must use the same full frame.
        image = self.rt.frame()
        if image is None:
            return "unknown", None, image
        page = self.rt.recognize(self.PAGE, image=image)
        button = self.rt.recognize(self.CLAIM, image=image)
        if not page or not page.hit or not button or not button.hit:
            return "unknown", None, image
        x, y, w, h = fields(self.rt.context, self.CLAIM)["roi"]
        hsv = cv2.cvtColor(image[y:y+h, x:x+w], cv2.COLOR_BGR2HSV)
        gold = cv2.countNonZero(cv2.inRange(hsv, (10, 85, 130), (35, 255, 255))) / (w*h)
        gray = cv2.countNonZero(cv2.inRange(hsv, (0, 0, 85), (180, 65, 235))) / (w*h)
        state = "available" if gold >= .2 else "empty" if gold <= .03 and gray >= .6 else "unknown"
        self.rt.evidence.observe("mailbox_button", state=state, roi=[x, y, w, h],
                                 gold_fraction=gold, gray_fraction=gray)
        return state, list(button.box), image

    def button_ready(self):
        return self.button_state()[0] != "unknown"

    def empty(self):
        return self.button_state()[0] == "empty"

    def click_claim(self):
        state, box, image = self.button_state()
        return bool(state == "available" and self.rt.image_current(image)
                    and self.ui.click_box(box))

    def claim(self):
        if not self.ui.wait_page(self.button_ready):
            return self.fail("未能确认领取全部按钮状态")
        state, _, _ = self.button_state()
        if state == "empty":
            if not self.ui.wait_page(self.empty):
                return self.fail("未能确认邮箱已无可领取附件")
            self.rt.log("邮箱没有可领取的附件。")
            return True
        if state != "available":
            return self.fail("领取全部按钮状态未能稳定")
        if not self.ui.enter_page(self.click_claim, "MaaNikki_RewardLayer",
                                  source=self.PAGE, attempts=1, seconds=10):
            return self.fail("未能确认附件领取结果")
        if not self.ui.skip_reward() or not self.ui.wait_page(self.PAGE):
            return self.fail("领取后未能返回邮件页面")
        if not self.ui.wait_page(self.empty):
            return self.fail("领取后未能确认全部附件已领取")
        self.rt.log("邮箱附件已全部领取。")
        return True

    def run(self):
        completed = False
        try:
            if self.ui.find_menu_entry(node="MaaNikki_Mailbox_Open"):
                completed = self.claim()
            else:
                self.fail("未能进入邮件页面")
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, cv2.error) as error:
            self.fail(str(error))
        finally:
            if not self.rt.stopped:
                # Confirm mail -> menu -> world before the ordinary task handoff.
                returned = self.ui.back_to("MaaNikki_MainDetected") or self.rt.main()
                if returned:
                    _, returned = self.rt.finish(self.ENTRY)
                if not returned:
                    completed = self.fail("未能返回后续任务需要的页面")
        return completed and not self.rt.stopped
