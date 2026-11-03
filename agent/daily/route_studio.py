"""Route editor backend: record the player's own run and execute user routes."""
from __future__ import annotations

from contextlib import nullcontext
import ctypes
from datetime import datetime
import threading
import time as wall_time

from file_io import write_json

from .route_test import RouteTest
from .runtime import RESOURCE, Runtime
from .settings import ROOT
from .startup import focus_game
from navigation.controller import ForegroundInput
from navigation.models import GameWindowClosed, NavigationError, maps
from navigation.vision import Locator
from task_control import active_session


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
        watcher = None
        try:
            inputs = ForegroundInput(self.controller, lambda: self.stopped, bindings=self.game_keys)
            focus_game(self, inputs)
            locator = Locator(spec)
            watched = {virtual_key(self.game_keys.get("jump")): "jump",
                       virtual_key(self.game_keys.get("interact")): "interact", **HOTKEYS}
            for name in RECORDED_KEYS:
                watched.setdefault(virtual_key(self.game_keys.get(name)), f"key:{name}")
            watcher = KeyWatcher(inputs, watched)
            watcher.start()
            with observation:
                self.loop(inputs, locator, watcher)
            self.document["state"] = "finished"
            return True
        except GameWindowClosed:
            self.document.update(state="finished", message="游戏已关闭，录制已结束。")
            return True
        except (NavigationError, OSError, RuntimeError, ValueError) as error:
            self.document.update(state="failed", message=str(error))
            self.log("路线录制停止："+str(error))
            return False
        finally:
            if watcher:
                watcher.finished.set()
            self.publish(force=True)

    def loop(self, inputs, locator, watcher):
        samples, events = self.document["samples"], self.document["events"]
        last, lost_since, gap = None, None, True
        self.log(f"开始录制 {locator.spec.title} 路线，直接在游戏里走一遍即可。", detail=True)
        while not self.stopped:
            started = wall_time.monotonic()
            pressed = watcher.drain()
            if not inputs.foreground():
                self.status("waiting_focus", "切回游戏后继续录制。")
                self.pause(.2)
                continue
            frame = self.capture()
            found = frame is not None and self.recognize("MaaNikki_MainDetected", image=frame)
            if not found or not found.hit:
                # Maps, menus and loading screens are not part of the walk. A
                # teleport passes through them, so search the map afresh afterwards.
                gap = True
                self.status("paused", "打开菜单或加载时暂停录制。")
                self.pause(.2)
                continue
            if gap:
                locator.reset()
            pose = locator.locate(frame, reset_continuity=gap)
            if not pose.position_reliable:
                now = wall_time.monotonic()
                lost_since = lost_since or now
                if now-lost_since >= LOST_RESET_SECONDS:
                    gap = True
                self.status("lost", f"小地图定位不上，请确认当前确实在「{locator.spec.title}」。")
                self.pause(.1)
                continue
            lost_since = None
            position = [round(pose.x, 2), round(pose.y, 2)]
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
                if item["kind"] == "fishing" and pose.heading is not None:
                    event["heading"] = round(pose.heading, 1) % 360
                events.append(event)
            self.document["pose"] = {"x": position[0], "y": position[1], "heading": pose.heading}
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
