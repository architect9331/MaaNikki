"""Dismiss the shared illustrated tutorial overlay without depending on its lesson."""
from __future__ import annotations

from task_control import ForegroundChanged, task_time as time


class TutorialOverlay:
    def __init__(self, runtime):
        self.rt = runtime

    def frame_visible(self, image):
        result = self.rt.recognize("MaaNikki_Tutorial_Frame", image=image)
        return bool(result and result.hit)

    def dismiss(self):
        """Return None if absent, True if dismissed, or False if still blocked."""
        with self.rt.observe():
            image = self.rt.frame()
            if image is None or not self.frame_visible(image):
                return None
            button = self.rt.recognize("MaaNikki_Tutorial_Button", image=image)
            if not button or not button.hit:
                return None

        self.rt.log("正在浏览功能教程。")
        # A tutorial is a bounded overlay operation. Its time is separate from
        # the ordinary page-transition budget; page count/content is unrestricted.
        deadline, clicks = time.monotonic()+30, 0
        while not self.rt.stopped and time.monotonic() < deadline:
            with self.rt.observe(fresh=True):
                image = self.rt.frame()
                if image is None:
                    return False
                if not self.frame_visible(image):
                    self.rt.log(f"功能教程已关闭，共点击 {clicks} 次。", detail=True)
                    return True
                button = self.rt.recognize("MaaNikki_Tutorial_Button", image=image)
                generation = self.rt._focus_generation
            if button and button.hit:
                try:
                    with self.rt.ui.preserve_cursor():
                        if not self.rt.action("Click", target=list(button.box),
                                              focus_generation=generation):
                            return False
                except ForegroundChanged:
                    # Reclassify after a user foreground switch, including the
                    # frame and button, instead of reusing the old click target.
                    continue
                clicks += 1
                label = getattr(getattr(button, "best_result", None), "text", "教程按钮")
                self.rt.log(f"教程：{label}（第 {clicks} 次点击）。", detail=True)
            if not self.rt.pause(.25):
                return False
        if not self.rt.stopped:
            self.rt.diagnostic("tutorial", "功能教程未能在等待时间内关闭。",
                               details={"clicks": clicks})
            self.rt.log("功能教程未能关闭，本项停止。")
        return False
