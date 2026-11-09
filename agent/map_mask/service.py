"""Latest-frame map matching; this module never sends game input."""
from __future__ import annotations

from dataclasses import replace
import json
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
from file_io import write_json
from message_log import write_message
from navigation.models import maps
from navigation.vision import Locator
from vision import prepared_template
from .matching import BigMapMatcher, MinimapMatcher, minimap_hint
from .pearpal import PearPal
from .window import OverlayWindow


REGIONS = {
    "miraland": (
        "奇迹大陆", "心愿原野", "伊赞之土", "纪念山地", "花愿镇", "微风绿野",
        "小石树田村", "石树田无人区", "祈愿树林", "巨木之森", "伊地峡谷",
        "埋骨地", "磐城", "蜗牛城", "蜗牛城壳顶", "菇菇聚落", "母菇林地",
        "壳壳岛", "巨藤林地", "巨兽堡垒", "遗忘旧街", "叶子河", "陶罐村",
        "巨人墓园", "安眠之林", "古木荫地", "巨森码头", "帕克亚湖心岛",
        "巨兽观测点", "岩村遗址", "巨人哨所遗址", "蜗牛农场", "蜗牛城瀑布",
    ),
    "wanxiang": (
        "万相境", "彩靥城", "彩靥城集市", "九华监", "彩靥城近郊", "迷花甸",
        "悲木空鸣地", "深谷幽泉", "万乐之飨", "彩乐轩", "忘忧楼", "连廊水榭",
        "九华阙",
    ),
    "firework": (
        "花焰群岛",
    ),
    "serenity": (
        "无忧岛",
    ),
    "danqing": (
        "丹青屿",
    ),
}

# Settled maximum zoom measured from captured map terrain. Miraland's opening
# animation passes through the navigation scale (.47775) before enlarging a
# further 1.0827x; that transient scale cannot track the final map viewport.
BIGMAP_SCALES = {"miraland": .44125}
STATE_FILE = ROOT / "cache/map-mask/state.json"


def _load_state():
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None, {}, {}, {}
    if not isinstance(raw, dict):
        return None, {}, {}, {}
    map_id = raw.get("map_id") if raw.get("map_id") in REGIONS else None
    player_seeds, bigmap_seeds, bigmap_scales = {}, {}, {}
    for target, source in ((player_seeds, raw.get("player")), (bigmap_seeds, raw.get("bigmap"))):
        if isinstance(source, dict):
            for key, val in source.items():
                if (key in REGIONS and isinstance(val, list) and len(val) == 2
                        and all(isinstance(v, (int, float)) and math.isfinite(v) for v in val)):
                    target[key] = (float(val[0]), float(val[1]))
    scales = raw.get("scales")
    if isinstance(scales, dict):
        for key, val in scales.items():
            if key in REGIONS and isinstance(val, (int, float)) and math.isfinite(val) and 0.1 < val < 20.0:
                bigmap_scales[key] = float(val)
    return map_id, player_seeds, bigmap_seeds, bigmap_scales


def _save_state(map_id, player_seeds, bigmap_seeds, bigmap_scales):
    try:
        write_json(STATE_FILE, {
            "map_id": map_id if map_id in REGIONS else None,
            "player": {k: [round(v[0], 2), round(v[1], 2)] for k, v in player_seeds.items()},
            "bigmap": {k: [round(v[0], 2), round(v[1], 2)] for k, v in bigmap_seeds.items()},
            "scales": {k: round(v, 5) for k, v in bigmap_scales.items()},
        })
    except Exception:
        pass



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
        self.region_misses = 0
        self.minimap_seen_at = 0.0
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.latest = None
        self.mode = "hidden"
        initial_map, player_seeds, bigmap_seeds, bigmap_scales = _load_state()
        self._initial_state = (initial_map, player_seeds, bigmap_seeds, bigmap_scales)
        self.map_id = initial_map
        self.submitted_map = initial_map
        self.generation = 0
        self.next_submit = self.next_region = 0.0
        self.last_observed_at = 0.0
        capture_method = rt.controller.info.get("screencap_method", 0)
        background = int(MaaWin32ScreencapMethodEnum.Background)
        window_capture = (type(capture_method) is int and capture_method > 0
                          and not capture_method & ~background)
        # An independent overlay is outside a FramePool/PrintWindow capture of
        # the game. Keep it visible to desktop streaming/capture in this case.
        self.data = None
        self.window = None
        self.thread = threading.Thread(target=self._run, name="map-mask-matching", daemon=True)
        try:
            self.data = PearPal(ROOT)
            self.window = OverlayWindow(hwnd, exclude_from_capture=not window_capture,
                                        tutorial_provider=self.data.tutorial)
            self.thread.start()
        except Exception:
            if self.data:
                self.data.close()
            if self.window:
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
        if mode == "minimap":
            self.minimap_seen_at = now
        elif mode == "hidden" and self.mode == "minimap" and now - self.minimap_seen_at < .45:
            return
        if mode == "bigmap":
            if self.mode != "bigmap":
                self.next_region = 0
                self.region_misses = 0
            if now >= self.next_region:
                detected = self._read_region(frame)
                if detected:
                    self.map_id, self.region_seen_at, self.region_misses = detected, now, 0
                elif self.region_has_text:
                    self.region_misses += 1
                    if self.region_misses >= 2:
                        self.map_id = None
                elif self.map_id is None and now-self.region_seen_at > 3:
                    self.map_id = None
                self.next_region = now + (2 if self.map_id and not self.region_misses else .4)
        effective_map = None if mode == "bigmap" and self.region_misses > 0 else self.map_id
        maximum = mode == "bigmap" and self.gates["max"].hit(frame)
        with self.lock:
            # Measure continuity where screenshots arrive, not where the worker
            # consumes them: a global search can itself take over a second.
            interrupted = self.last_observed_at and now-self.last_observed_at > 1.2
            if interrupted or mode != self.mode or self.submitted_map != effective_map:
                self.generation += 1
                self.window.hide()
            self.last_observed_at = now
            self.mode = mode
            self.submitted_map = effective_map
            self.latest = frame.copy(), mode, effective_map, maximum, now, self.generation
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
                             if any(name in compact or (len(name) >= 4 and name[:3] in compact)
                                    for name in names)), None)
            if selected:
                break
        signature = tuple(readings), selected
        self.region_has_text = any(value for _, value in readings)
        if signature != self.last_region_reading:
            write_message(f"地图标记区域识别：roi={roi}, readings={readings}, map={selected}", None)
            self.last_region_reading = signature
        return selected

    def _run(self):
        last_known_map, player_seeds, bigmap_seeds, bigmap_scales = self._initial_state
        contexts = {}
        previous_mode = "hidden"
        pending = None
        hidden_since = None
        failures = 0
        active_generation = None
        bigmap_hint = None
        bigmap_confirmed = False
        pending_scale = None
        last_bigmap_at = 0.0
        next_recovery = 0.0
        pending_at = 0.0
        next_diagnostic = 0.0
        last_hint = None
        evidence = FailureEvidence()
        reference = None
        matching_failures = 0
        next_evidence = 0.0
        track_pos = None
        track_map = None
        track_at = 0.0
        last_minimap_render = None
        last_saved_at = 0.0
        state_dirty = False

        def get_context(mid):
            ctx = contexts.get(mid)
            if ctx is None:
                spec = self.specs[mid]
                loc = Locator(replace(spec, position_score_floor=.55,
                                      bigmap_scale=BIGMAP_SCALES.get(mid, spec.bigmap_scale)))
                bm = BigMapMatcher(loc, initial_scale=bigmap_scales.get(mid))
                mm = MinimapMatcher(loc)
                ctx = (loc, bm, mm)
                contexts[mid] = ctx
            return ctx

        try:
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
                    pending = bigmap_hint = None
                    bigmap_confirmed = False
                    pending_scale = None
                    reference = None
                    matching_failures = 0
                    active_generation = generation
                points, hint, circle = (), "", None
                diagnostic = {}
                index_sec = 0.0
                match_started = time.monotonic()
                try:
                    if mode == "hidden":
                        if hidden_since is None:
                            hidden_since = time.monotonic()
                        if time.monotonic()-hidden_since > 1.8:
                            pending = track_pos = last_minimap_render = None
                        continue
                    if hidden_since is not None and time.monotonic()-hidden_since > 1.8:
                        pending = track_pos = last_minimap_render = None
                    hidden_since = None
                    if not self.data.login.is_file():
                        hint = "请在设置的通用页面登录美鸭梨"
                    elif mode == "bigmap":
                        if map_id not in REGIONS:
                            hint = "地图标记：正在识别地图区域…"
                        else:
                            last_known_map = map_id
                            locator, bigmap_matcher, _ = get_context(map_id)
                            if previous_mode != "bigmap":
                                pending = None
                                bigmap_hint = bigmap_seeds.get(map_id) or player_seeds.get(map_id)
                                bigmap_confirmed = False
                            search_hint = bigmap_hint or bigmap_seeds.get(map_id) or player_seeds.get(map_id)
                            center, diagnostic = bigmap_matcher.locate(frame, search_hint,
                                                                       maximum=maximum)
                            index_sec = bigmap_matcher.consume_index_time()
                            scale = diagnostic.get("scale")
                            if (center is not None and not bigmap_confirmed and
                                    pending_scale is not None and
                                    abs(scale/pending_scale-1) > .005):
                                pending = None
                            if center is None:
                                hint = "地图标记：正在识别缩放与位置…"
                                pending = None
                                bigmap_hint = None
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
                                    bigmap_confirmed = False
                            elif (not bigmap_confirmed and diagnostic.get("agreed", 0) < 3
                                  and (pending is None or math.dist(center, pending) > 4)):
                                matching_failures = 0
                                reference = frame, dict(diagnostic)
                                pending = bigmap_hint = center
                                pending_scale = scale
                                bigmap_seeds[map_id] = center
                                state_dirty = True
                                last_bigmap_at = captured
                                hint = "地图标记：正在确认地图位置…"
                            else:
                                bigmap_confirmed = True
                                matching_failures = 0
                                reference = frame, dict(diagnostic)
                                pending = bigmap_hint = center
                                pending_scale = scale
                                last_bigmap_at = captured
                                bigmap_seeds[map_id] = center
                                if scale is not None:
                                    bigmap_scales[map_id] = float(scale)
                                if map_id not in player_seeds:
                                    player_seeds[map_id] = center
                                state_dirty = True
                                points, hint = self._project(locator, *center, scale, (640, 360),
                                                             (0, 65, 1280, 650), None)
                    else:
                        primary_map = (map_id if map_id in REGIONS else
                                       (last_known_map if last_known_map in REGIONS else "miraland"))
                        if previous_mode != "minimap":
                            pending = None
                            failures, next_recovery = 0, 0.0
                        locator, _, minimap_matcher = get_context(primary_map)
                        patch = minimap_matcher.sample(frame)
                        matched = None
                        active_track = (track_pos if (track_map == primary_map and
                                                      captured - track_at <= 2.5) else None)
                        local_seed = active_track or player_seeds.get(primary_map)
                        if local_seed is not None:
                            cand = minimap_matcher.locate_local(
                                patch, local_seed,
                                radius=85 if active_track else 200,
                                floor=0.48 if active_track else 0.54)
                            if cand is not None:
                                if active_track is not None:
                                    step = math.dist((cand[0], cand[1]), active_track)
                                    step_limit = max(20.0, 68.0 * max(0.0, captured - track_at))
                                    if step <= step_limit or (cand[2] >= 0.65 and cand[3] >= 0.02):
                                        matched = (primary_map, *cand, True)
                                else:
                                    matched = (primary_map, *cand, True)
                        if matched is None and bigmap_seeds.get(primary_map) is not None:
                            bseed = bigmap_seeds[primary_map]
                            if local_seed is None or math.dist(bseed, local_seed) > 60:
                                cand = minimap_matcher.locate_local(patch, bseed, radius=260, floor=0.54)
                                if cand is not None:
                                    matched = (primary_map, *cand, True)
                        if matched is None and time.monotonic() >= next_recovery:
                            cand = minimap_matcher.locate_global(patch, floor=0.56, min_peak=0.018)
                            if cand is not None:
                                matched = (primary_map, *cand, cand[2] >= 0.66 and cand[3] >= 0.022)
                            elif map_id not in REGIONS or failures >= 4:
                                other_map = "wanxiang" if primary_map == "miraland" else "miraland"
                                _, _, other_matcher = get_context(other_map)
                                other_cand = other_matcher.locate_global(
                                    other_matcher.sample(frame), floor=0.58, min_peak=0.020)
                                if other_cand is not None:
                                    matched = (other_map, *other_cand,
                                               other_cand[2] >= 0.66 and other_cand[3] >= 0.022)
                            next_recovery = time.monotonic() + (1.2 if map_id in REGIONS else 1.8)
                        if matched is None:
                            failures += 1
                            pending = None
                            diagnostic = {"search": "minimap_miss", "failures": failures, "reliable": False}
                            if (last_minimap_render is not None and
                                    last_minimap_render[0] == primary_map and
                                    captured - last_minimap_render[3] <= 1.8):
                                points, circle = last_minimap_render[1], last_minimap_render[2]
                                diagnostic["held_ms"] = round((captured - last_minimap_render[3]) * 1000)
                            else:
                                track_pos = None
                                hint = "地图标记：定位中，请稍候或重新打开大地图"
                        else:
                            found_map, mx, my, score, peak, mkind, immediate = matched
                            diagnostic = {"x": round(mx, 2), "y": round(my, 2),
                                          "score": round(score, 3), "peak": round(peak, 3),
                                          "search": mkind, "reliable": True}
                            if (not immediate and (pending is None or
                                    math.dist((mx, my), pending) > max(6.0, 65.0 * max(0.0, captured - pending_at)))):
                                pending = (mx, my)
                                pending_at = captured
                                hint = "地图标记：正在确认角色位置…"
                            else:
                                failures = 0
                                pending = (mx, my)
                                pending_at = captured
                                track_pos = (mx, my)
                                track_map = found_map
                                track_at = captured
                                player_seeds[found_map] = (mx, my)
                                last_known_map = found_map
                                state_dirty = True
                                if map_id != found_map:
                                    map_id = found_map
                                    with self.lock:
                                        self.map_id = self.submitted_map = found_map
                                locator, _, _ = get_context(found_map)
                                circle = (*locator.spec.center, locator.spec.radius)
                                cx, cy, radius = circle
                                points, hint = self._project(
                                    locator, mx, my, locator.spec.scale,
                                    (cx, cy), (cx-radius, cy-radius, cx+radius, cy+radius),
                                    circle)
                                last_minimap_render = (found_map, points, circle, captured)
                except Exception as error:
                    hint = "地图标记：定位暂不可用，请重新打开大地图"
                    if failures == 0:
                        write_message(f"地图遮罩定位失败（{type(error).__name__}）：{error}", None)
                    failures += 1
                    pending = None
                finally:
                    previous_mode = mode
                now_mono = time.monotonic()
                if state_dirty and now_mono - last_saved_at >= 3.0:
                    _save_state(last_known_map, player_seeds, bigmap_seeds, bigmap_scales)
                    last_saved_at = now_mono
                    state_dirty = False
                diagnostic["elapsed_ms"] = round((now_mono - match_started) * 1000)
                age = max(0.0, now_mono - captured - index_sec)
                diagnostic["frame_age_ms"] = round(age * 1000)
                if age > (1.2 if mode == "bigmap" else .8):
                    diagnostic["display_deferred"] = True
                    points, hint, circle = (), "地图标记：正在用最新画面确认位置…", None
                if hint != last_hint or now_mono >= next_diagnostic:
                    write_message(f"地图标记：mode={mode}, map={map_id}, projected={len(points)}, "
                                  f"hint={hint}, pose={diagnostic}, window={self.window.diagnostic()}", None)
                    last_hint, next_diagnostic = hint, now_mono + 10
                window_hint = "" if (mode == "minimap" and self.data.login.is_file()) else hint
                with self.lock:
                    if generation == self.generation and not self.stop.is_set():
                        self.window.publish(points, window_hint, circle)
        finally:
            if state_dirty:
                _save_state(last_known_map, player_seeds, bigmap_seeds, bigmap_scales)

    def _project(self, locator, x, y, scale, center, bounds, circle):
        cx, cy = center
        left, top, right, bottom = bounds
        candidates, hint = self.data.visible(locator.spec.id,
            (x+(left-cx)*scale, y+(top-cy)*scale, x+(right-cx)*scale, y+(bottom-cy)*scale),
            self.kinds)
        grouped = {}
        merge_r = 0.0 if circle else 8.0
        for point in candidates:
            sx, sy = cx+(point.x-x)/scale, cy+(point.y-y)/scale
            if circle and math.hypot(sx-cx, sy-cy) > circle[2]-6:
                continue
            key = (round(sx), round(sy), point.kind)
            if merge_r and key not in grouped:
                near = next((k for k in grouped
                             if k[2] == point.kind and math.hypot(sx-k[0], sy-k[1]) <= merge_r), None)
                if near is not None:
                    key = near
            grouped.setdefault(key, []).append(point)
        return tuple((*key, len(items), tuple(items)) for key, items in grouped.items()), hint
