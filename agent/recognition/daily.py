"""Daily Inspiration (每日灵感) tab state recognizer for the v3.0 unified daily UI.

Starting in v3.0, 朝夕心愿 and 星海拾光 share the unified 每日灵感 screen with top
tabs (`Q [朝夕心愿] [星海拾光] E`) and a sliding score indicator along the right-side
vertical reward track. This recognizer anchors on live OCR boxes for the page title
and both tab labels, then compares their warm-gold active highlight contrast on the
same frame so tab detection never depends on theme-specific templates.
"""
from __future__ import annotations

import json
import cv2
import numpy as np

from maa.custom_recognition import CustomRecognition
from registration import agent_recognition


def _tab_highlight_metrics(image: np.ndarray, box: list[int]) -> tuple[float, float]:
    """Return (mean_luma, gold_ratio) over the tab plate surrounding an OCR label."""
    h, w = image.shape[:2]
    bx, by, bw, bh = [int(v) for v in box]
    x0 = max(0, bx - 44)
    x1 = min(w, bx + bw + 28)
    y0 = max(0, min(6, by - 14))
    y1 = min(h, max(by + bh + 18, 64))
    patch = image[y0:y1, x0:x1]
    if patch is None or patch.size == 0:
        return 0.0, 0.0
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    mean_v = float(hsv[:, :, 2].mean())
    gold_mask = cv2.inRange(hsv, (10, 38, 125), (36, 255, 255))
    gold_ratio = float((gold_mask > 0).mean())
    return mean_v, gold_ratio


@agent_recognition("nikki.daily_tab_ready")
class DailyTabReadyRecognition(CustomRecognition):
    """Confirm 每日灵感 is open with the requested tab (zhaoxi or xinghai) active."""

    def analyze(self, context, argv):
        image = argv.image
        if image is None or image.size == 0:
            return None
        param = json.loads(argv.custom_recognition_param or "{}")
        tab = param.get("tab")
        if tab not in ("zhaoxi", "xinghai"):
            return None

        title = context.run_recognition("MaaNikki_Daily_InspirationTitle", image)
        if not title or not title.hit:
            return None
        zhaoxi = context.run_recognition("MaaNikki_Daily_ZhaoxiTab", image)
        xinghai = context.run_recognition("MaaNikki_Daily_XinghaiTab", image)
        if not (zhaoxi and zhaoxi.hit and zhaoxi.box and xinghai and xinghai.hit and xinghai.box):
            return None

        z_luma, z_gold = _tab_highlight_metrics(image, list(zhaoxi.box))
        x_luma, x_gold = _tab_highlight_metrics(image, list(xinghai.box))

        if tab == "zhaoxi":
            target_box = [int(v) for v in zhaoxi.box]
            target_luma, other_luma = z_luma, x_luma
            target_gold, other_gold = z_gold, x_gold
        else:
            target_box = [int(v) for v in xinghai.box]
            target_luma, other_luma = x_luma, z_luma
            target_gold, other_gold = x_gold, z_gold

        luma_diff = target_luma - other_luma
        gold_diff = target_gold - other_gold

        # Active tab renders a luminous warm-gold plate (meanV ~177, gold_ratio ~0.72)
        # while the inactive tab sits over the dark top bar (meanV ~50, gold_ratio ~0.00).
        if luma_diff >= 45.0 and gold_diff >= 0.25:
            return CustomRecognition.AnalyzeResult(
                box=target_box,
                detail={
                    "tab": tab,
                    "selected": True,
                    "luma_diff": round(luma_diff, 1),
                    "gold_diff": round(gold_diff, 3),
                    "target_luma": round(target_luma, 1),
                    "target_gold": round(target_gold, 3),
                },
            )
        return CustomRecognition.AnalyzeResult(
            box=None,
            detail={
                "tab": tab,
                "selected": False,
                "luma_diff": round(luma_diff, 1),
                "gold_diff": round(gold_diff, 3),
            },
        )
