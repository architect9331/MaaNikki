"""Route editor backend: record the player's own run and execute user routes."""
from __future__ import annotations

from contextlib import nullcontext
import ctypes
from datetime import datetime
import json
import math
import threading
import time as wall_time

import cv2
from file_io import write_json

from .route_test import RouteTest
from .runtime import RESOURCE, Runtime
from .settings import ROOT
from .startup import focus_game
from map_mask.matching import BigMapMatcher, MinimapMatcher, bigmap_pose
from map_mask.service import BIGMAP_SCALES
from navigation.controller import ForegroundInput
from navigation.matching import luma, match_map
from navigation.models import GameWindowClosed, NavigationError, maps
from navigation.overlay import RouteMinimapOverlay, resolve_route_overlay_mode
from navigation.vision import Locator, Pose
from task_control import TaskStopped, active_session


RECORDING_FILE = ROOT / "config/route_recording.json"
MAX_SAMPLES = 20000
# Sampling only has to outline the route; the editor simplifies it afterwards.
SAMPLE_INTERVAL = .2
SAMPLE_DISTANCE = .3
PUBLISH_INTERVAL = .5
# A longer unreliable stretch means the old position no longer helps the search.
LOST_RESET_SECONDS = 1.5
# Same distance the route teleport step uses to tell a transport from a short walk.
TELEPORT_DISTANCE = 15
VK_MOUSE = {0: 0x01, 1: 0x02, 2: 0x04, 3: 0x05, 4: 0x06}
HOTKEYS = {0x75: "point", 0x76: "collect", 0x77: "wait", 0x78: "fishing", 0x79: "fall"}
# Game actions replayed as key points, with how long each was held. Movement,
# jump and dive stay with the walk itself; a later name sharing a physical key
# with an earlier one is the same input and is not watched twice.
RECORDED_KEYS = ("attack", "ability_use", "capture", "bell", "sub_ability",
                 *(f"ability_{index}" for index in range(1, 9)))
# Same bounds the route key action accepts.
MAX_HOLD = 10.0


class KeyWatcher(threading.Thread):
    """Poll physical keys often enough to catch a tap between position samples."""

    def __init__(self, inputs, watched):
        super().__init__(daemon=True)
        self.inputs, self.watched = inputs, watched
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.user32.GetAsyncKeyState.restype = ctypes.c_short
        self.finished = threading.Event()
        self.lock = threading.Lock()
        self.events = []

    def run(self):
        held = {}
        while not self.finished.wait(.02):
            # Keys pressed in the editor or anywhere else are not part of the route.
            focused = self.inputs.foreground()
            now = wall_time.monotonic()
            for code, kind in self.watched.items():
                down = focused and bool(self.user32.GetAsyncKeyState(code) & 0x8000)
                if down and code not in held:
                    held[code] = now
                    # Game keys are kept on release, once their hold time is known.
                    if not kind.startswith("key:"):
                        self.emit({"kind": kind})
                        self.confirm(kind)
                elif not down and code in held:
                    pressed_at = held.pop(code)
                    if kind.startswith("key:"):
                        hold = min(MAX_HOLD, max(.1, round(now-pressed_at, 1)))
                        self.emit({"kind": "key", "key": kind[4:], "hold": hold})

    def emit(self, event):
        with self.lock:
            self.events.append(event)

    @staticmethod
    def confirm(kind):
        # Marks are pressed with the game in front, where the editor is not visible.
        if kind in HOTKEYS.values():
            import winsound
            winsound.Beep(1046, 70)

    def drain(self):
        with self.lock:
            events, self.events = self.events, []
        return events


def virtual_key(binding):
    return VK_MOUSE.get(binding.code) if binding.kind == "mouse" else binding.code


def _load_map_checkpoints(map_id: str) -> list[dict]:
    path = RESOURCE / "navigation/checkpoints.json"
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        items = raw.get("maps", {}).get(map_id, []) if isinstance(raw, dict) else []
        return [
            cp for cp in items
            if isinstance(cp, dict) and isinstance(cp.get("x"), (int, float)) and isinstance(cp.get("y"), (int, float))
        ]
    except Exception:
        return []


def _beep_confirm(freq=1318, duration=90):
    try:
        import winsound
        winsound.Beep(freq, duration)
    except Exception:
        pass


class RouteRecorder(Runtime):
    """Only observes: it reads the screen and keyboard and never sends game input."""

    def record(self, settings):
        spec = maps(RESOURCE).get(settings.get("map"))
        if spec is None:
            self.log("请先选择要录制的地图。")
            return False
        self.document = {"schema_version": 1, "map": spec.id, "state": "starting", "message": "",
                         "started_at": datetime.now().astimezone().isoformat(),
                         "pose": None, "samples": [], "events": []}
        self.published_at = 0
        self.publish(force=True)
        session = active_session()
        observation = session.background_observation() if session else nullcontext()
        watcher = overlay = None
        try:
            inputs = ForegroundInput(self.controller, lambda: self.stopped, bindings=self.game_keys)
            focus_game(self, inputs)
            locator = Locator(spec)
            overlay_mode = resolve_route_overlay_mode(getattr(self, "context", None))
            overlay = RouteMinimapOverlay(inputs.hwnd, spec, initial_mode=overlay_mode) if getattr(inputs, "hwnd", 0) else None
            watched = {virtual_key(self.game_keys.get("jump")): "jump",
                       virtual_key(self.game_keys.get("interact")): "interact", **HOTKEYS}
            for name in RECORDED_KEYS:
                watched.setdefault(virtual_key(self.game_keys.get(name)), f"key:{name}")
            watcher = KeyWatcher(inputs, watched)
            watcher.start()
            with observation:
                self.loop(inputs, locator, watcher, overlay, settings=settings)
            self.document["state"] = "finished"
            return True
        except GameWindowClosed:
            self.document.update(state="finished", message="游戏已关闭，录制已结束。")
            return True
        except (NavigationError, OSError, RuntimeError, ValueError) as error:
            # Ending the recording stops the task, which can surface as an
            # error from whatever was waiting; what was recorded is complete.
            if isinstance(error, TaskStopped) or self.stopped:
                self.document.update(state="finished", message="")
                return True
            self.document.update(state="failed", message=str(error))
            self.log("路线录制停止："+str(error))
            return False
        finally:
            if overlay:
                overlay.close()
            if watcher:
                watcher.finished.set()
            self.publish(force=True)

    @staticmethod
    def _check_teleporter_pose(frame, locator: Locator, minimap_matcher: MinimapMatcher,
                               checkpoints: list[dict], configured_tp: dict | None,
                               prev_match: tuple | None, *, allow_catalog: bool = True):
        if configured_tp and isinstance(configured_tp.get("x"), (int, float)) and isinstance(configured_tp.get("y"), (int, float)):
            tx, ty = float(configured_tp["x"]), float(configured_tp["y"])
            pose = locator.locate(frame, hint=(tx, ty), search_radius=36, commit=False, reset_continuity=True)
            if pose.position_reliable and pose.score >= 0.68 and math.hypot(pose.x - tx, pose.y - ty) <= 26:
                if prev_match is not None and math.hypot(pose.x - prev_match[0].x, pose.y - prev_match[0].y) <= 1.5:
                    locator.accept(pose)
                    return pose, configured_tp, None
                return None, None, (pose, configured_tp)
        if not allow_catalog or not checkpoints:
            return None, None, None
        patch = minimap_matcher.sample(frame)
        cand = minimap_matcher.locate_global(patch, floor=0.70, min_peak=0.022)
        if cand is not None:
            gx, gy, _, _, _ = cand
            nearest = min(checkpoints, key=lambda cp: math.hypot(gx - float(cp["x"]), gy - float(cp["y"])))
            cx, cy = float(nearest["x"]), float(nearest["y"])
            if math.hypot(gx - cx, gy - cy) <= 22:
                pose = locator.locate(frame, hint=(cx, cy), search_radius=28, commit=False, reset_continuity=True)
                if (pose.position_reliable and pose.score >= 0.72 and pose.margin >= 0.04
                        and math.hypot(pose.x - cx, pose.y - cy) <= 22):
                    if prev_match is not None and math.hypot(pose.x - prev_match[0].x, pose.y - prev_match[0].y) <= 1.5:
                        locator.accept(pose)
                        return pose, nearest, None
                    return None, None, (pose, nearest)
        return None, None, None

    @staticmethod
    def _locate_bigmap_frame(frame, locator: Locator, bigmap_matcher: BigMapMatcher, hint=None):
        center, _ = bigmap_matcher.locate(frame, hint=hint)
        if center is not None:
            return float(center[0]), float(center[1])
        scales = list(dict.fromkeys([BIGMAP_SCALES.get(locator.spec.id, locator.spec.bigmap_scale), locator.spec.bigmap_scale]))
        for s in scales:
            center, _ = bigmap_pose(locator, frame, hint=hint, scale=s)
            if center is not None:
                return float(center[0]), float(center[1])
        # Fallback for island maps where peripheral tiles fall in ocean/void.
        gray = luma(frame)[140:580, 260:1020]
        baseline = locator.spec.bigmap_scale
        best = None
        for i in range(16):
            scale = baseline * (1.095 ** i)
            patch = cv2.resize(gray, None, fx=scale * 0.25, fy=scale * 0.25, interpolation=cv2.INTER_AREA)
            if (patch.shape[0] < 24 or patch.shape[1] < 32
                    or patch.shape[0] + 8 >= locator.coarse.shape[0]
                    or patch.shape[1] + 8 >= locator.coarse.shape[1]):
                continue
            try:
                mx, my, score, peak = match_map(locator.coarse, patch, peak_mask=locator.bigmap_mask,
                                                kernel=7, minimum_score=0.58)
                if all(math.isfinite(v) for v in (mx, my, score, peak)) and score >= 0.62 and peak >= 0.02:
                    if best is None or score > best[0]:
                        best = (score, (mx * 4.0, my * 4.0))
            except (NavigationError, cv2.error):
                pass
        return best[1] if best is not None else None

    def loop(self, inputs, locator, watcher, overlay=None, *, settings=None):
        settings = settings or {}
        samples, events = self.document["samples"], self.document["events"]
        checkpoints = _load_map_checkpoints(locator.spec.id)
        configured_tp = settings.get("teleport") if isinstance(settings.get("teleport"), dict) else None
        require_bigmap = bool(settings.get("require_bigmap"))
        minimap_matcher = MinimapMatcher(locator)
        bigmap_matcher = BigMapMatcher(locator)
        last, lost_since, gap = None, None, True
        calibrated_seed = None
        prev_bigmap_center = None
        prev_tp_match = None
        self.log(f"开始录制 {locator.spec.title} 路线，直接在游戏里走一遍即可。", detail=True)
        while not self.stopped:
            started = wall_time.monotonic()
            pressed = watcher.drain()
            if not inputs.foreground():
                if overlay:
                    overlay.hide()
                self.status("waiting_focus", "切回游戏后继续录制。")
                self.pause(.2)
                continue
            frame = self.capture()
            if frame is None:
                self.pause(.1)
                continue
            found_main = self.recognize("MaaNikki_MainDetected", image=frame)
            is_main = bool(found_main and found_main.hit)
            if not is_main:
                gap = True
                prev_tp_match = None
                found_map = self.recognize("MaaNikki_Navigation_MapFeature", image=frame)
                if found_map and found_map.hit:
                    if overlay and calibrated_seed is None:
                        overlay.publish_status(
                            "正在从大地图定位…",
                            "请勿拖动地图，若未识别可放大地图",
                            border_bgr=(70, 185, 255),
                        )
                    center = self._locate_bigmap_frame(
                        frame, locator, bigmap_matcher,
                        hint=prev_bigmap_center or calibrated_seed or (tuple(last) if last else None),
                    )
                    if center is not None:
                        if prev_bigmap_center is not None and math.hypot(center[0] - prev_bigmap_center[0], center[1] - prev_bigmap_center[1]) <= 2.5:
                            first_confirm = calibrated_seed is None
                            calibrated_seed = (round(center[0], 2), round(center[1], 2))
                            self.document["pose"] = {"x": calibrated_seed[0], "y": calibrated_seed[1], "heading": None}
                            if first_confirm:
                                _beep_confirm()
                                self.log(f"大地图定位成功：({calibrated_seed[0]}, {calibrated_seed[1]})，请返回大世界。", detail=True)
                            if overlay:
                                overlay.publish_status(
                                    "大地图定位成功！请按 M 返回游戏",
                                    f"已锁定坐标 ({calibrated_seed[0]:.0f}, {calibrated_seed[1]:.0f})，返回大世界即开始录制",
                                    border_bgr=(110, 215, 95),
                                    title_color=0xC8F7C5,
                                )
                            self.status("calibrated", "大地图定位成功！请按 M 或 Esc 返回大世界开始录制。")
                        else:
                            prev_bigmap_center = center
                            if overlay:
                                overlay.publish_status(
                                    "正在确认大地图位置…",
                                    "请保持大地图静止片刻",
                                    border_bgr=(70, 185, 255),
                                )
                            self.status("calibrating", "正在确认大地图位置，请勿拖动地图…")
                    else:
                        prev_bigmap_center = None
                        if overlay:
                            overlay.publish_status(
                                "正在从大地图定位…",
                                "请勿拖动地图，若未识别可放大地图",
                                border_bgr=(70, 185, 255),
                            )
                        self.status("calibrating", "正在从大地图识别位置…若未识别可放大地图（请勿拖动偏离角色）。")
                    self.pause(.15)
                    continue
                prev_bigmap_center = None
                if overlay:
                    overlay.hide()
                self.status("paused", "打开菜单或加载时暂停录制。")
                self.pause(.2)
                continue

            prev_bigmap_center = None
            pose = None

            # Initial starting point acquisition before the first sample is recorded.
            if not samples:
                if calibrated_seed is not None:
                    bx, by = calibrated_seed
                    pose = locator.locate(frame, hint=calibrated_seed, search_radius=75, reset_continuity=True)
                    if not pose.position_reliable or math.hypot(pose.x - bx, pose.y - by) > 60:
                        cand = minimap_matcher.locate_local(minimap_matcher.sample(frame), calibrated_seed, radius=48, floor=0.45)
                        if cand is not None and math.hypot(cand[0] - bx, cand[1] - by) <= 42:
                            pose = Pose(cand[0], cand[1], cand[2], cand[3], pose.heading, pose.heading_confidence, True)
                        else:
                            pose = Pose(bx, by, 0.8, 0.05, pose.heading, pose.heading_confidence, True)
                        locator.accept(pose)
                    # Ignore keys pressed while closing the big map.
                    pressed = []
                elif configured_tp and not require_bigmap:
                    tp_pose, matched_tp, prev_tp_match = self._check_teleporter_pose(
                        frame, locator, minimap_matcher, checkpoints, configured_tp, prev_tp_match,
                        allow_catalog=False,
                    )
                    if tp_pose is not None:
                        pose = tp_pose
                        calibrated_seed = (round(pose.x, 2), round(pose.y, 2))
                        tp_name = (matched_tp or {}).get("name") or "流转之柱"
                        self.log(f"已确认起点传送点：{tp_name}", detail=True)
                    else:
                        tp_title = configured_tp.get("name") or "流转之柱"
                        msg = f"未检测到起点「{tp_title}」，请先传送或按 M 打开大地图定位起点。"
                        sub = f"请按 M 打开大地图定位，或先传送至「{tp_title}」"
                        if overlay:
                            overlay.publish_status("请按 M 打开大地图定位起点", sub, border_bgr=(70, 185, 255))
                        self.status("calibrating", msg)
                        self.pause(.15)
                        continue
                else:
                    msg = "起点未设传送点，请按 M 打开大地图（勿拖动）完成起点定位后再开始移动。"
                    sub = "起点非传送点，打开大地图确认位置后自动开录"
                    if overlay:
                        overlay.publish_status("请按 M 打开大地图定位起点", sub, border_bgr=(70, 185, 255))
                    self.status("calibrating", msg)
                    self.pause(.15)
                    continue
            elif gap:
                # Reacquiring after a menu, big-map view, or mid-route teleport.
                if lost_since is None and last is not None:
                    stay = locator.locate(frame, hint=tuple(last), search_radius=45, commit=False, reset_continuity=True)
                    if stay.position_reliable and stay.score >= 0.66 and math.hypot(stay.x - last[0], stay.y - last[1]) < TELEPORT_DISTANCE:
                        locator.accept(stay)
                        pose = stay
                if pose is None and calibrated_seed is not None:
                    bx, by = calibrated_seed
                    cand_pose = locator.locate(frame, hint=calibrated_seed, search_radius=85, commit=False, reset_continuity=True)
                    if cand_pose.position_reliable and math.hypot(cand_pose.x - bx, cand_pose.y - by) <= 65:
                        locator.accept(cand_pose)
                        pose = cand_pose
                    else:
                        cand = minimap_matcher.locate_local(minimap_matcher.sample(frame), calibrated_seed, radius=52, floor=0.46)
                        if cand is not None and math.hypot(cand[0] - bx, cand[1] - by) <= 45:
                            pose = Pose(cand[0], cand[1], cand[2], cand[3], cand_pose.heading, cand_pose.heading_confidence, True)
                            locator.accept(pose)
                if pose is None:
                    tp_pose, _, prev_tp_match = self._check_teleporter_pose(
                        frame, locator, minimap_matcher, checkpoints, None, prev_tp_match, allow_catalog=True,
                    )
                    if tp_pose is not None:
                        pose = tp_pose
                        calibrated_seed = (round(pose.x, 2), round(pose.y, 2))
                if pose is None:
                    if overlay:
                        overlay.publish_status("请按 M 打开大地图重新定位", "传送或跟丢后打开大地图即可恢复定位", border_bgr=(70, 185, 255))
                    self.status("lost", f"小地图定位不上，请按 M 打开大地图重新定位。")
                    self.pause(.15)
                    continue
            else:
                seed = (locator.previous.x, locator.previous.y) if locator.previous else tuple(last)
                radius = 75 if lost_since is None else 135
                pose = locator.locate(frame, hint=seed, search_radius=radius, reset_continuity=False)
                if not pose.position_reliable:
                    cand = minimap_matcher.locate_local(
                        minimap_matcher.sample(frame), seed, radius=90 if lost_since is None else 145, floor=0.48,
                    )
                    if cand is not None and math.hypot(cand[0] - seed[0], cand[1] - seed[1]) <= (18 if lost_since is None else 45):
                        pose = Pose(cand[0], cand[1], cand[2], cand[3], pose.heading, pose.heading_confidence, True)
                        locator.accept(pose)

            if not pose.position_reliable:
                now = wall_time.monotonic()
                lost_since = lost_since or now
                if now - lost_since >= LOST_RESET_SECONDS:
                    calibrated_seed = None
                if overlay:
                    overlay.publish_status("小地图定位不上", "可按 M 打开大地图重新定位", border_bgr=(70, 140, 245))
                self.status("lost", f"小地图定位不上，可按 M 打开大地图重新定位。")
                self.pause(.1)
                continue
            lost_since = None
            position = [round(pose.x, 2), round(pose.y, 2)]
            calibrated_seed = (position[0], position[1])
            teleported = bool(last and gap and max(abs(pose.x-last[0]), abs(pose.y-last[1])) >= TELEPORT_DISTANCE)
            gap = False
            moved = last is None or teleported or max(abs(pose.x-last[0]), abs(pose.y-last[1])) >= SAMPLE_DISTANCE
            if teleported or pressed or moved:
                if len(samples) >= MAX_SAMPLES:
                    raise NavigationError("这段录制太长了，请先结束并保存。")
                samples.append(position)
                last = position
            index = len(samples)-1
            if teleported:
                events.append({"kind": "teleport", "index": index})
            for item in pressed:
                event = {**item, "index": index}
                if (item["kind"] == "fishing"
                        or (item["kind"] == "key" and item.get("key") == "attack")) and pose.heading is not None:
                    event["heading"] = round(pose.heading, 1) % 360
                events.append(event)
            self.document["pose"] = {"x": position[0], "y": position[1], "heading": pose.heading}
            if overlay:
                overlay.publish_recording(samples, events, pose)
            self.status("recording", "")
            self.pause(max(0, SAMPLE_INTERVAL-(wall_time.monotonic()-started)))

    def status(self, state, message):
        self.document.update(state=state, message=message)
        self.publish()

    def publish(self, *, force=False):
        now = wall_time.monotonic()
        if not force and now-self.published_at < PUBLISH_INTERVAL:
            return
        self.published_at = now
        try:
            write_json(RECORDING_FILE, self.document)
        except OSError:
            self.log("录制进度保存失败，稍后重试。", detail=True)


class UserRouteRun(RouteTest):
    """A route file the user made, with all of its actions."""
    dev_only = False
    label = "自定义路线"
    default_mode = "full"


class OpenWorldCollectRun(UserRouteRun):
    """Sequential open-world material farming with Gingerbread left-side toast telemetry."""
    dev_only = False
    label = "大世界材料采集"
    default_mode = "full"
