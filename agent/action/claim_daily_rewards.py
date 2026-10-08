from __future__ import annotations

import json
import time
import cv2
import numpy as np
from registration import agent_action
from maa.custom_action import CustomAction
from daily.runtime import Executors


@agent_action("nikki.claim_daily_rewards")
class ClaimDailyRewardsAction(CustomAction):
    """Find circular available-reward markers, then require the reward layer."""

    def run(self, context, argv):
        rt = Executors(context)
        try:
            param = argv.custom_action_param
            param = json.loads(param or "{}") if isinstance(param, str) else param
            roi = param.get("roi", [1168, 150, 60, 345])
            maximum = param.get("max_claims", 5)
            if (not isinstance(roi, list) or len(roi) != 4 or any(type(v) is not int for v in roi)
                    or type(maximum) is not int or not 1 <= maximum <= 5):
                return False
            x, y, w, h = roi
            if x < 0 or y < 0 or w <= 0 or h <= 0 or x+w > 1280 or y+h > 720:
                return False
            attempted = []
            for _ in range(maximum):
                image = rt.capture()
                if image is None:
                    return False
                mask = cv2.inRange(cv2.cvtColor(image[y:y+h, x:x+w], cv2.COLOR_BGR2HSV),
                                   np.array([0, 0, 125]), np.array([30, 255, 255]))
                circles = cv2.HoughCircles(mask, cv2.HOUGH_GRADIENT, dp=1, minDist=100*2/3,
                                           param1=60, param2=8, minRadius=20, maxRadius=23)
                candidates = [] if circles is None else [
                    (x+round(cx), y+round(cy)) for cx, cy, _ in circles[0]
                    if not any((x+cx-px)**2+(y+cy-py)**2 < 18**2 for px, py in attempted)]
                if not candidates:
                    return True
                target = candidates[0]
                attempted.append(target)
                if (not rt.action("Click", target=list(target)) or not rt.ui.unhover()
                        or not rt.ui.skip_reward()):
                    rt.log("未确认每日奖励领取，本项停止。")
                    return False
                # These pages have perpetual particles and swinging bottles.
                # Verify return to the daily page, not full-screen stillness.
                deadline = time.monotonic()+5
                returned = False
                while not rt.stopped and time.monotonic() < deadline:
                    if rt.hit("MaaNikki_Zhaoxi_DailyReady") or rt.hit("MaaNikki_Xinghai_DailyReady"):
                        returned = True
                        break
                    if not rt.pause(.3):
                        break
                if not returned or not rt.ui.unhover() or not rt.pause(.3):
                    rt.log("领取后未能回到每日奖励页面，本项停止。")
                    return False
            return not rt.stopped
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, cv2.error) as error:
            rt.log(f"每日奖励领取停止：{error}")
            return False
