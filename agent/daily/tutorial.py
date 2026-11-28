"""Dismiss the shared illustrated tutorial overlay and timeout skip prompts."""
from __future__ import annotations

import re

import cv2
import numpy as np

from task_control import ForegroundChanged, task_time as time


# Top-center banner shown by the game after staying blocked in a mandatory
# spotlight tutorial ("检测到您在该引导停留过长时间，是否跳过该引导？" + × / pink ✓).
SKIP_TUTORIAL_TEXT_ROI = [350, 4, 520, 52]
_RE_SKIP_TUTORIAL = re.compile(r"是否[跳路][过過][该該]?引[导導]|停留[过過][长長][时時][间間]")


def _pink_check_mask(bgr: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    return ((hsv[:, :, 0] >= 160) | (hsv[:, :, 0] <= 12)) & (hsv[:, :, 1] >= 65) & (hsv[:, :, 2] >= 175)


def find_skip_tutorial_confirm_point(frame, rows) -> list[int] | None:
    """Locate the pink checkmark button on the top-center tutorial timeout banner.

    Requires both the timeout prompt text and the distinct pink circular ✓
    button strictly to the right of that text box (ignoring the gray × button).
    """
    if frame is None or frame.ndim != 3:
        return None
    h, w = frame.shape[:2]
    text_box = None
    for row in rows or ():
        if row is None:
            continue
        clean = str(getattr(row, "text", "") or "").strip()
        if not clean:
            continue
        score = getattr(row, "score", 1.0)
        if isinstance(score, (int, float)) and score < 0.45:
            continue
        box = getattr(row, "box", None)
        if not box or len(box) < 4:
            continue
        if _RE_SKIP_TUTORIAL.search(clean):
            text_box = [int(v) for v in box[:4]]
            break
    if text_box is None:
        return None
    bx, by, bw, bh = text_box
    text_right = bx + bw
    # The gray × circle sits ~28 px right of the text; the pink ✓ circle sits
    # ~65 px right (x in [text_right + 35, text_right + 110]).
    x0 = max(0, min(w - 1, text_right + 35))
    x1 = max(x0 + 1, min(w, text_right + 110))
    y0 = max(0, by - 10)
    y1 = max(y0 + 1, min(h, by + bh + 18))
    patch = frame[y0:y1, x0:x1]
    if patch.size == 0:
        return None
    ys, xs = np.nonzero(_pink_check_mask(patch))
    if len(xs) < 150:
        return None
    return [x0 + round(float(xs.mean())), y0 + round(float(ys.mean()))]


class TutorialOverlay:
    def __init__(self, runtime):
        self.rt = runtime

    def frame_visible(self, image):
        result = self.rt.recognize("MaaNikki_Tutorial_Frame", image=image)
        return bool(result and result.hit)

    def skip_banner_point(self, image) -> list[int] | None:
        """Cheaply detect the top-center forced-tutorial skip banner."""
        if image is None or image.ndim != 3 or image.shape[0] < 60 or image.shape[1] < 980:
            return None
        # Pixel pre-filter avoids running OCR on normal frames (< 0.1 ms).
        strip = image[8:52, 740:980]
        if int(np.count_nonzero(_pink_check_mask(strip))) < 200:
            return None
        ocr = self.rt.recognize(
            "MaaNikki_Daily_OCR",
            {"roi": SKIP_TUTORIAL_TEXT_ROI, "expected": ".+", "threshold": 0.4},
            image=image,
        )
        if not ocr:
            return None
        rows = (
            getattr(ocr, "all_results", None)
            or getattr(ocr, "filtered_results", None)
            or [getattr(ocr, "best_result", None)]
        )
        return find_skip_tutorial_confirm_point(image, rows)

    def _dismiss_skip_banner(self, point: list[int]) -> bool:
        self.rt.log("检测到强制引导跳过提示，正在点击跳过。")
        deadline = time.monotonic() + 8.0
        target = point
        last_click_at = -float("inf")
        clicks = 0
        clear = 0
        while not self.rt.stopped and time.monotonic() < deadline:
            now = time.monotonic()
            if target is not None and clicks < 3 and now - last_click_at >= 1.2:
                try:
                    with self.rt.ui.preserve_cursor():
                        if not self.rt.action("Click", target=target):
                            return False
                except ForegroundChanged:
                    continue
                clicks += 1
                last_click_at = time.monotonic()
                clear = 0
            if not self.rt.pause(.25):
                return False
            with self.rt.observe(fresh=True):
                image = self.rt.frame()
                if image is None:
                    return False
                target = self.skip_banner_point(image)
            if target is None:
                clear += 1
                if clear >= 2:
                    self.rt.log(f"强制引导已跳过，共点击 {clicks} 次。", detail=True)
                    return True
            else:
                clear = 0
        if not self.rt.stopped:
            self.rt.diagnostic("tutorial_skip", "强制引导跳过提示未能在等待时间内关闭。",
                               details={"clicks": clicks})
        return False

    def dismiss(self):
        """Return None if absent, True if dismissed, or False if still blocked."""
        with self.rt.observe():
            image = self.rt.frame()
            if image is None:
                return None
            illustrated = self.frame_visible(image)
            button = self.rt.recognize("MaaNikki_Tutorial_Button", image=image) if illustrated else None
            skip_point = None if (illustrated and button and button.hit) else self.skip_banner_point(image)

        if skip_point is not None:
            return self._dismiss_skip_banner(skip_point)
        if not illustrated or not button or not button.hit:
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
