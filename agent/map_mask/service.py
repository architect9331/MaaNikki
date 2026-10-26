"""Latest-frame map matching; this module never sends game input."""
from __future__ import annotations

from dataclasses import replace
import math
import threading
import time

import cv2
import numpy as np
from maa.define import MaaWin32ScreencapMethodEnum

from daily.runtime import RESOURCE
from daily.settings import ROOT
from daily.ui import fields
from diagnostics import FailureEvidence
from message_log import write_message
from navigation.models import maps
from navigation.vision import Locator
from vision import prepared_template
from .matching import BigMapMatcher, minimap_hint
from .pearpal import PearPal
from .window import OverlayWindow


REGIONS = {
    "miraland": ("奇迹大陆", "纪念山地", "花愿镇", "微风绿野", "小石树田村", "石树田无人区",
                 "祈愿树林", "巨木之森", "伊地峡谷", "埋骨地", "磐城"),
    "wanxiang": ("万相境",),
}

# Settled maximum zoom measured from captured map terrain. Miraland's opening
# animation passes through the navigation scale (.47775) before enlarging a
# further 1.0827x; that transient scale cannot track the final map viewport.
BIGMAP_SCALES = {"miraland": .44125}


class Gate:
    def __init__(self, context, name):
        data = fields(context, name)
        template = data["template"]
        if isinstance(template, list):
            template = template[0]
        self.gray = data.get("gray_limit")
        self.color = data.get("color")
        self.roi = data["roi"]
        threshold = data.get("threshold", .8)
        self.threshold = float(threshold[0] if isinstance(threshold, list) else threshold)
        self.prepared = prepared_template(RESOURCE / "image" / template,
                                          gray=self.gray, color=self.color)

    def hit(self, frame):
        x, y, width, height = self.roi
        observed = frame[y:y+height, x:x+width]
        target, mask = self.prepared.image, self.prepared.mask
        if self.color:
            observed = cv2.inRange(cv2.cvtColor(observed, cv2.COLOR_BGR2HSV),
                                   np.array(self.color[0]), np.array(self.color[1]))
        elif self.gray:
            observed = cv2.threshold(cv2.cvtColor(observed, cv2.COLOR_BGR2GRAY),
                                     self.gray[0], self.gray[1], cv2.THRESH_BINARY)[1]
        if observed.shape[0] < target.shape[0] or observed.shape[1] < target.shape[1]:
            return False
        method = cv2.TM_CCORR_NORMED if self.gray or self.color else cv2.TM_CCOEFF_NORMED
        rates = cv2.matchTemplate(observed, target, method, mask=mask)
        return float(np.max(np.nan_to_num(rates, nan=-1, posinf=-1, neginf=-1))) >= self.threshold


class MapMask:
    def __init__(self, rt, hwnd, kinds):
        self.rt, self.kinds = rt, kinds
        self.gates = {key: Gate(rt.context, node) for key, node in (
            ("map", "MaaNikki_Navigation_MapFeature"), ("max", "MaaNikki_Navigation_MapMaxScale"),
            ("world", "MaaNikki_MainDetected"))}
        self.specs = maps(RESOURCE)
        # Asset ROIs already use the controller's 1280x720 coordinate system.
        self.region_roi = list(rt.ui.roi("AreaBigMapRegionName"))
        self.last_region_reading = None
        self.region_has_text = False
        self.region_seen_at = 0.0
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.latest = None
        self.mode = "hidden"
        self.map_id = None
        self.submitted_map = None
        self.generation = 0
        self.next_submit = self.next_region = 0.0
        self.last_observed_at = 0.0
        capture_method = rt.controller.info.get("screencap_method", 0)
        background = int(MaaWin32ScreencapMethodEnum.Background)
        window_capture = (type(capture_method) is int and capture_method > 0
                          and not capture_method & ~background)
        # An independent overlay is outside a FramePool/PrintWindow capture of
        # the game. Keep it visible to desktop streaming/capture in this case.
        self.window = OverlayWindow(hwnd, exclude_from_capture=not window_capture)
        self.data = None
        self.thread = threading.Thread(target=self._run, name="map-mask-matching", daemon=True)
        try:
            self.data = PearPal(ROOT)
            self.thread.start()
        except Exception:
            if self.data:
                self.data.close()
            self.window.close()
            raise

    def close(self):
        self.stop.set()
        self.wake.set()
        self.data.close()
        self.window.close()
        self.thread.join(timeout=1)

    def hide(self):
        with self.lock:
            if self.mode != "hidden":
                self.generation += 1
            self.mode, self.latest = "hidden", None
        self.window.hide()

    def observe(self, frame, *, foreground):
        if self.window.error:
            raise RuntimeError(self.window.error)
        now = time.monotonic()
        if not foreground:
            self.hide()
            return
        if now+.005 < self.next_submit:
            return
        self.next_submit = now + .05
        mode = ("bigmap" if self.gates["map"].hit(frame) else
                "minimap" if self.gates["world"].hit(frame) else "hidden")
        if mode == "bigmap":
            if self.mode != "bigmap":
                self.map_id = None
                self.next_region = 0
            if now >= self.next_region:
                detected = self._read_region(frame)
                if detected:
                    self.map_id, self.region_seen_at = detected, now
                elif self.region_has_text or now-self.region_seen_at > 3:
                    self.map_id = None
                self.next_region = now + (2 if self.map_id else .8)
        maximum = mode == "bigmap" and self.gates["max"].hit(frame)
        with self.lock:
            # Measure continuity where screenshots arrive, not where the worker
            # consumes them: a global search can itself take over a second.
            interrupted = self.last_observed_at and now-self.last_observed_at > 1
            if interrupted or mode != self.mode or self.submitted_map != self.map_id:
                self.generation += 1
                self.window.hide()
            self.last_observed_at = now
            self.mode = mode
            self.submitted_map = self.map_id
            self.latest = frame.copy(), mode, self.map_id, maximum, now, self.generation
        self.wake.set()

    def _read_region(self, frame):
        x, y, width, height = self.region_roi
        left, top = max(0, x-4), max(0, y-6)
        right = min(frame.shape[1], x+width+4)
        bottom = min(frame.shape[0], y+height+6)
        roi = [left, top, right-left, bottom-top]
        readings = []
        selected = None
        for method in ("original", "enlarged"):
            if method == "original":
                value = self.rt.text(roi, image=frame)
            else:
                patch = cv2.resize(frame[top:bottom, left:right], None, fx=3, fy=3,
                                   interpolation=cv2.INTER_CUBIC)
                patch = cv2.copyMakeBorder(patch, 8, 8, 8, 8, cv2.BORDER_REPLICATE)
                value = self.rt.text([0, 0, patch.shape[1], patch.shape[0]], image=patch)
            compact = "".join(value.split())
            readings.append((method, compact))
            selected = next((key for key, names in REGIONS.items()
                             if any(name in compact for name in names)), None)
            if selected:
                break
        signature = tuple(readings), selected
        self.region_has_text = any(value for _, value in readings)
        if signature != self.last_region_reading:
            write_message(f"地图标记区域识别：roi={roi}, readings={readings}, map={selected}", None)
            self.last_region_reading = signature
        return selected

    def _run(self):
        locator = None
        bigmap_matcher = None
        previous_mode = "hidden"
        seed = pending = None
        hidden_since = None
        failures = 0
        active_generation = None
        bigmap_hint = None
        bigmap_confirmed = False
        pending_scale = None
        last_bigmap_at = 0.0
        next_recovery = 0.0
        recovery_hint = None
        pending_at = 0.0
        next_diagnostic = 0.0
        last_hint = None
        evidence = FailureEvidence()
        reference = None
        matching_failures = 0
        next_evidence = 0.0
        while not self.stop.is_set():
            self.wake.wait(.2)
            self.wake.clear()
            if self.stop.is_set():
                break
            with self.lock:
                item, self.latest = self.latest, None
            if item is None:
                continue
            frame, mode, map_id, maximum, captured, generation = item
            if time.monotonic()-captured > .8:
                continue
            if active_generation != generation:
                pending = recovery_hint = bigmap_hint = None
                bigmap_confirmed = False
                pending_scale = None
                reference = None
                matching_failures = 0
                if locator:
                    if locator.previous:
                        seed = locator.previous.x, locator.previous.y
                    locator.reset()
                active_generation = generation
            points, hint, circle = (), "", None
            diagnostic = {}
            match_started = time.monotonic()
            try:
                if mode == "hidden":
                    if hidden_since is None:
                        hidden_since = time.monotonic()
                    if time.monotonic()-hidden_since > .8:
                        pending = recovery_hint = None
                        if locator:
                            if locator.previous:
                                seed = locator.previous.x, locator.previous.y
                            locator.reset()
                    continue
                if hidden_since is not None and time.monotonic()-hidden_since > .8:
                    pending = recovery_hint = None
                    if locator:
                        if locator.previous:
                            seed = locator.previous.x, locator.previous.y
                        locator.reset()
                hidden_since = None
                if not self.data.login.is_file():
                    hint = "请在设置的通用页面登录美鸭梨"
                elif map_id not in REGIONS:
                    hint = ("地图标记：正在识别地图区域…" if mode == "bigmap" else
                            "请打开奇迹大陆或万相境大地图校准")
                else:
                    if locator is None or locator.spec.id != map_id:
                        spec = self.specs[map_id]
                        locator = Locator(replace(spec, position_score_floor=.55,
                            bigmap_scale=BIGMAP_SCALES.get(map_id, spec.bigmap_scale)))
                        bigmap_matcher = BigMapMatcher(locator)
                        seed = pending = recovery_hint = None
                        bigmap_hint = None
                        bigmap_confirmed = False
                        pending_scale = None
                        failures, next_recovery = 0, 0.0
                    if mode == "bigmap":
                        if previous_mode != "bigmap":
                            pending = None
                            bigmap_hint = None
                            bigmap_confirmed = False
                        if bigmap_matcher is not None:
                            center, diagnostic = bigmap_matcher.locate(frame, bigmap_hint,
                                                                       maximum=maximum)
                            scale = diagnostic.get("scale")
                            if (center is not None and pending_scale is not None and
                                    abs(scale/pending_scale-1) > .005):
                                bigmap_confirmed = False
                                pending = None
                            if center is None:
                                hint = "地图标记：正在识别缩放与位置…"
                                pending = None
                                matching_failures += 1
                                if (matching_failures >= 3 and evidence.saved < 5
                                        and time.monotonic() >= next_evidence):
                                    if reference is not None:
                                        evidence.save("map_mask_reference", "定位丢失前的匹配画面",
                                            image=reference[0],
                                            details={"map": map_id, "pose": reference[1]})
                                    evidence.save("map_mask_lost", "连续三帧未通过地形定位",
                                        image=frame, details={"map": map_id, "pose": diagnostic})
                                    next_evidence = time.monotonic()+20
                                if captured-last_bigmap_at > .8:
                                    bigmap_hint = None
                                    bigmap_confirmed = False
                            elif not bigmap_confirmed and (pending is None or
                                                           math.dist(center, pending) > 4):
                                matching_failures = 0
                                reference = frame, dict(diagnostic)
                                pending = bigmap_hint = center
                                pending_scale = scale
                                # A terrain-confirmed candidate already suffices
                                # as a hint. Minimap matching still independently
                                # verifies the player if the map closes now.
                                seed = center
                                last_bigmap_at = captured
                                hint = "地图标记：正在确认地图位置…"
                            else:
                                # Each pose already passed independent terrain
                                # checks. Only initial calibration needs another
                                # frame; dragging must not restart that wait.
                                bigmap_confirmed = True
                                matching_failures = 0
                                reference = frame, dict(diagnostic)
                                pending = bigmap_hint = center
                                pending_scale = scale
                                last_bigmap_at = captured
                                # Used only as a search hint. A minimap match must confirm the player.
                                seed = center
                                points, hint = self._project(locator, *center, scale, (640, 360),
                                                             (0, 65, 1280, 650), None)
                    elif seed is None:
                        hint = "地图标记：请打开大地图，完成定位校准"
                    else:
                        if previous_mode != "minimap":
                            locator.reset()
                            pending = recovery_hint = None
                            failures, next_recovery = 0, 0.0
                        search_hint = ((locator.previous.x, locator.previous.y) if locator.previous
                                       else recovery_hint or seed)
                        pose = locator.locate(frame, hint=search_hint,
                                              search_radius=75 if locator.previous else 500, commit=False)
                        diagnostic = {"x": round(pose.x, 2), "y": round(pose.y, 2),
                                      "score": round(pose.score, 3), "peak": round(pose.margin, 3),
                                      "reliable": pose.position_reliable}
                        if not pose.position_reliable or pose.score < .55 or pose.margin < .01:
                            failures += 1
                            hint = "地图标记：定位中，请稍候或重新打开大地图"
                            pending = None
                            if failures >= 6 and locator.previous:
                                seed = locator.previous.x, locator.previous.y
                                locator.reset()
                            # Panning the big map does not locate the player. A
                            # throttled global hint recovers even far from its center.
                            if failures >= 3 and time.monotonic() >= next_recovery:
                                recovery_hint = minimap_hint(locator, frame)
                                next_recovery = time.monotonic()+3
                                diagnostic["recovery_hint"] = recovery_hint
                        elif pending is None or (locator.previous is None and
                                                 math.dist((pose.x, pose.y), pending) >
                                                 max(2, 50*(captured-pending_at))):
                            pending = pose.x, pose.y
                            pending_at = captured
                            hint = "地图标记：正在确认角色位置…"
                        else:
                            failures = 0
                            locator.accept(pose)
                            pending = pose.x, pose.y
                            pending_at = captured
                            seed = pending
                            circle = (*locator.spec.center, locator.spec.radius)
                            cx, cy, radius = circle
                            points, hint = self._project(locator, pose.x, pose.y, locator.spec.scale,
                                                         (cx, cy), (cx-radius, cy-radius, cx+radius, cy+radius),
                                                         circle)
            except Exception as error:
                hint = "地图标记：定位暂不可用，请重新打开大地图"
                if failures == 0:
                    write_message(f"地图遮罩定位失败（{type(error).__name__}）：{error}", None)
                failures += 1
                pending = None
            finally:
                previous_mode = mode
            diagnostic["elapsed_ms"] = round((time.monotonic()-match_started)*1000)
            age = time.monotonic()-captured
            diagnostic["frame_age_ms"] = round(age*1000)
            # Slow global results initialize the next local search. Only a
            # fresh confirming frame may put their markers on screen.
            if age > .8:
                diagnostic["display_deferred"] = True
                points, hint, circle = (), "地图标记：正在用最新画面确认位置…", None
            if hint != last_hint or time.monotonic() >= next_diagnostic:
                write_message(f"地图标记：mode={mode}, map={map_id}, projected={len(points)}, "
                              f"hint={hint}, pose={diagnostic}, window={self.window.diagnostic()}", None)
                last_hint, next_diagnostic = hint, time.monotonic()+10
            with self.lock:
                if generation == self.generation and not self.stop.is_set():
                    self.window.publish(points, hint, circle)

    def _project(self, locator, x, y, scale, center, bounds, circle):
        cx, cy = center
        left, top, right, bottom = bounds
        candidates, hint = self.data.visible(locator.spec.id,
            (x+(left-cx)*scale, y+(top-cy)*scale, x+(right-cx)*scale, y+(bottom-cy)*scale),
            self.kinds)
        grouped = {}
        for point in candidates:
            sx, sy = cx+(point.x-x)/scale, cy+(point.y-y)/scale
            if circle and math.hypot(sx-cx, sy-cy) > circle[2]-6:
                continue
            key = (round(sx), round(sy), point.kind)
            grouped[key] = grouped.get(key, 0) + 1
        return tuple((*key, count) for key, count in grouped.items()), hint
