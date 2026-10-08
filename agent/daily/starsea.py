"""Starsea hub, bell and crystal-location workflow."""
from __future__ import annotations
import math
import time
import json
from datetime import datetime
import cv2
from .settings import ROOT
from navigation.models import NavigationError, maps
from navigation.vision import Locator
from navigation.teleport import Teleporter
from navigation.controller import ForegroundInput
from .settings import foreground_inputs


CRYSTAL_CENTERS = ((3282.8, 2437.6), (1679, 2000), (2865, 2155), (2250, 1475),
                   (3082, 1893), (3140, 1960), (2434.4, 1659.2), (2375, 1605))
CRYSTAL_ROUTE_ORDER = (3, 5, 6, 2, 4, 1, 7, 8)
HUB = {"name": "无界枢纽", "province": "星海", "region": "星海", "x": 834.327, "y": 1019.073}


def marker_location(pose, boxes, scale):
    x = sum(b[0]+b[2]/2 for b in boxes)/len(boxes)
    y = sum(b[1]+b[3]/2 for b in boxes)/len(boxes)
    return pose[0]+(x-640)*scale, pose[1]+(y-360)*scale


def crystal_pan(dx, dy):
    # Keep both endpoints inside map terrain, outside buttons and side panels.
    factor = min(1, 880/max(abs(dx), 1), 540/max(abs(dy), 1))
    vx, vy = -dx*factor, -dy*factor
    begin = [round(530-vx/2), round(370-vy/2)]
    end = [round(530+vx/2), round(370+vy/2)]
    return begin, end


def select_crystal_route(location):
    distances = sorted([{"route": f"xinghai_crystal_{n}", "distance": math.hypot(
        location[0]-CRYSTAL_CENTERS[n-1][0]/2, location[1]-CRYSTAL_CENTERS[n-1][1]/2)}
        for n in CRYSTAL_ROUTE_ORDER], key=lambda item: item["distance"])
    # Close islands (7/8) must have a clear winner; never widen the tolerance.
    selected = distances[0] if distances[0]["distance"] < 25 and (
        distances[1]["distance"]-distances[0]["distance"] >= 8) else None
    return selected, distances


class Starsea:
    def __init__(self, runtime, resource):
        self.rt, self.resource = runtime, resource
        self.events = []

    def teleporter(self):
        if not foreground_inputs():
            raise NavigationError("星海传送需要前台鼠标和键盘输入，请设置为 Seize。")
        inputs = ForegroundInput(self.rt.controller, lambda: self.rt.stopped, bindings=self.rt.game_keys)
        return Teleporter(self.rt, Locator(maps(self.resource)["starsea"]), event=self.events.append, inputs=inputs)

    def report(self, operation):
        if not self.events:
            return
        try:
            directory = ROOT / "logs/navigation"
            directory.mkdir(parents=True, exist_ok=True)
            file = directory / (datetime.now().strftime("%Y%m%d-%H%M%S-%f")+"-starsea.json")
            file.write_text(json.dumps({"operation": operation, "events": self.events},
                                       ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            self.rt.log("星海导航记录未能保存。")

    def hub(self):
        try:
            with self.rt.input_guard():
                self.teleporter().transport(HUB)
            return True
        except (ValueError, KeyError, OSError, RuntimeError, cv2.error) as error:
            self.events.append({"type": "stopped", "reason": str(error)})
            self.rt.log(f"未能返回无界枢纽：{error}")
            return False
        finally:
            self.report("hub")

    def ring(self, at_hub=False, timed=False):
        try:
            if not at_hub:
                self.rt.log("正在前往无界枢纽，准备召唤摇铃。")
                if not self.hub():
                    return False
            self.rt.log("正在长按摇铃键（4 秒）。")
            ok = self.rt.hold("bell", 4)
            if ok and not timed:
                ok = self.rt.ui.wait_page("MaaNikki_MainDetected", seconds=10)
            self.events.append({"type": "bell", "input_completed": bool(ok), "duration": 4})
            if ok and not at_hub:
                self.rt.log("摇铃操作已结束，稍后统一核实积分。")
            return bool(ok)
        except (ValueError, KeyError, OSError, RuntimeError, cv2.error) as error:
            self.events.append({"type": "stopped", "reason": str(error)})
            self.rt.log(f"召唤摇铃停止：{error}")
            return False
        finally:
            if not at_hub:
                self.report("bell")

    def boxes(self, image, threshold=.75, count=3):
        # Exclude the permanent stars and counters in the right-side panel.
        return self.rt.ui.asset_boxes("T_UI_map_img_icon_star_02", image=image,
            roi=[60, 80, 930, 610], scale=2/3, threshold=threshold, count=count)

    def prepare_crystals(self, teleporter, center):
        if not self.rt.ui.open_map(self.events.append):
            raise NavigationError("未能准备结晶地图。")
        teleporter.deadline = time.monotonic()+60
        teleporter.zoom()
        self.crystal_view = center
        self.events.append({"type": "crystal_map_prepared", "center": list(center)})
        if not self.rt.main():
            raise NavigationError("准备地图后未能回到摇铃界面。")

    def locate_crystals(self, teleporter):
        # Region/zoom are prepared BEFORE the bell. Reopening can recenter on
        # the player, so use bounded large pans without full-screen stillness.
        # Retain the first complete marker frame for all position calculations.
        start = time.monotonic()
        pans = 0
        if not self.rt.key("map", 0):
            raise NavigationError("未能打开结晶地图。")
        while not self.rt.stopped and time.monotonic()-start < 5:
            frame = self.rt.capture()
            if frame is None:
                return None
            ready = self.rt.recognize("MaaNikki_Navigation_MapFeature", image=frame)
            maximum = self.rt.recognize("MaaNikki_Navigation_MapMaxScale", image=frame)
            if ready and ready.hit and maximum and maximum.hit:
                boxes = self.boxes(frame)
                teleporter.last_frame = frame
                try:
                    pose, _ = teleporter.map_pose(frame)
                except NavigationError:
                    # A visible map header can precede usable terrain pixels.
                    if not self.rt.pause(.1):
                        return None
                    continue
                if len(boxes) == 3:
                    if pose[2] >= .6 and pose[3] >= .01:
                        location = marker_location(pose, boxes, teleporter.locator.spec.bigmap_scale)
                        self.events.append({"type": "crystal_snapshot", "boxes": boxes,
                            "map_pose": list(pose), "location": list(location),
                            "elapsed": round(time.monotonic()-start, 3)})
                        return location
                if pans < 3 and pose[2] >= .6 and pose[3] >= .01:
                    dx = (self.crystal_view[0]-pose[0])/teleporter.locator.spec.bigmap_scale
                    dy = (self.crystal_view[1]-pose[1])/teleporter.locator.spec.bigmap_scale
                    if math.hypot(dx, dy) > 30:
                        begin, end = crystal_pan(dx, dy)
                        if not self.rt.action("Swipe", begin=begin, end=end, duration=250):
                            raise NavigationError("结晶地图快速移动失败。")
                        pans += 1
                        self.events.append({"type": "crystal_fast_pan", "begin": begin, "end": end})
                        if not self.rt.pause(.15):
                            return None
            if not self.rt.pause(.1):
                break
        self.events.append({"type": "crystal_snapshot_missing", "elapsed": round(time.monotonic()-start, 3)})
        return None

    def crystals(self, mode, card):
        with self.rt.input_guard():
            return self._crystals(mode, card)

    def _crystals(self, mode, card):
        try:
            teleporter = self.teleporter()
            teleporter.transport(HUB)
            self.rt.log("开始收集星光结晶，先准备地图，再摇铃并立即保存标记。")
            location, selected, distances = None, None, []
            # These three maximum-scale viewports cover all registered islands.
            # Each viewport gets its own bell window; never continue browsing
            # an expired window or substitute a stale map center for markers.
            for center in ((1164.5, 767), (1530, 1080), (839.5, 1000)):
                self.prepare_crystals(teleporter, center)
                if not self.ring(at_hub=True, timed=True):
                    return False
                location = self.locate_crystals(teleporter)
                if not self.rt.main():
                    return False
                if location is not None:
                    selected, distances = select_crystal_route(location)
                    self.events.append({"type": "crystal_route_selection", "location": list(location),
                                        "candidates": distances, "selected": selected})
                    if selected is not None:
                        break
                self.rt.log("本次未取得可确认的结晶位置，准备另一处地图视野后重新摇铃。")
            if selected is None:
                if teleporter.last_frame is not None:
                    try:
                        folder = ROOT / "logs/navigation"
                        folder.mkdir(parents=True, exist_ok=True)
                        path = folder / (datetime.now().strftime("%Y%m%d-%H%M%S-%f")+"-crystal-map.png")
                        if cv2.imwrite(str(path), teleporter.last_frame[:680]):
                            self.events.append({"type": "crystal_map_frame", "file": path.name})
                    except (OSError, cv2.error):
                        pass
                self.rt.log("结晶定位结果未匹配到已登记路线，本次停止收集。")
                return False
            name = selected["route"]
            self.rt.log("已识别星光结晶所在岛屿，开始收集。")
            return self.rt.navigate(name, mode, card)
        except (ValueError, KeyError, OSError, RuntimeError, cv2.error) as error:
            self.events.append({"type": "stopped", "reason": str(error)})
            self.rt.log(f"星光结晶收集停止：{error}")
            return False
        finally:
            self.report("crystals")
