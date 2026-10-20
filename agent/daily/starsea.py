"""Starsea hub, bell and crystal-location workflow."""
from __future__ import annotations
import math
from task_control import task_time as time
import json
from datetime import datetime
import cv2
from .settings import ROOT
from navigation.models import NavigationError, load_route, maps
from navigation.vision import Locator
from navigation.teleport import Teleporter
from navigation.controller import ForegroundInput
from .settings import foreground_inputs


CRYSTAL_CENTERS = ((3282.8, 2437.6), (1679, 2000), (2865, 2155), (2250, 1475),
                   (3082, 1893), (3140, 1960), (2434.4, 1659.2), (2375, 1605))
CRYSTAL_ROUTE_ORDER = (3, 5, 6, 2, 4, 1, 7, 8)
CRYSTAL_VIEWS = ((1164.5, 767), (1530, 1080), (839.5, 1000))
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


def select_partial_crystal_route(locations, routes):
    # A remaining marker can be far from the original three-marker centroid.
    # Match every visible marker to the actual route, including its segments.
    def distance_to_route(location, points):
        nearest = math.inf
        for index, end in enumerate(points):
            begin = points[max(0, index-1)] if end.action != "teleport" else end
            dx, dy = end.x-begin.x, end.y-begin.y
            length = dx*dx+dy*dy
            fraction = max(0, min(1, ((location[0]-begin.x)*dx+
                (location[1]-begin.y)*dy)/length)) if length else 0
            nearest = min(nearest, math.hypot(location[0]-begin.x-fraction*dx,
                                            location[1]-begin.y-fraction*dy))
        return nearest

    center = tuple(sum(location[axis] for location in locations)/len(locations) for axis in (0, 1))
    centers = {f"xinghai_crystal_{n}": (x/2, y/2)
               for n, (x, y) in enumerate(CRYSTAL_CENTERS, 1)}
    distances = []
    for route in routes:
        marker_distances = [distance_to_route(location, route.points) for location in locations]
        reference = centers[route.id]
        distances.append({"route": route.id, "distance": max(marker_distances),
                          "center_distance": math.hypot(center[0]-reference[0], center[1]-reference[1]),
                          "marker_distances": marker_distances})
    distances.sort(key=lambda item: (item["distance"], item["center_distance"]))
    # Some routes share collection segments. Either can cover the remaining
    # markers, so accept the nearest path without a runner-up margin; use the
    # original island centers only to break equal path distances.
    selected = distances[0] if distances[0]["distance"] < 25 else None
    return selected, distances


class Starsea:
    def __init__(self, runtime, resource):
        self.rt, self.resource = runtime, resource
        self.events = []
        self.crystal_scans = []
        self.crystal_routes = None
        self.crystal_selection = None
        self.crystal_marker_count = 0

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
        # Keep terrain and all visible markers from the same frame.
        start = time.monotonic()
        pans = 0
        snapshot = None
        self.crystal_selection = None
        self.crystal_marker_count = 0
        scan = {"type": "crystal_view_scan", "center": list(self.crystal_view),
                "max_markers": 0, "view_confirmed": False, "capture_failed": False}
        self.crystal_scans.append(scan)
        self.events.append(scan)
        if not self.rt.key("map", 0):
            raise NavigationError("未能打开结晶地图。")
        while not self.rt.stopped and time.monotonic()-start < 5:
            frame = self.rt.capture()
            if frame is None:
                scan["capture_failed"] = True
                return None
            ready = self.rt.recognize("MaaNikki_Navigation_MapFeature", image=frame)
            maximum = self.rt.recognize("MaaNikki_Navigation_MapMaxScale", image=frame)
            if ready and ready.hit and maximum and maximum.hit:
                boxes = self.boxes(frame)
                scan["max_markers"] = max(scan["max_markers"], len(boxes))
                teleporter.last_frame = frame
                try:
                    pose, _ = teleporter.map_pose(frame)
                except NavigationError:
                    # A visible map header can precede usable terrain pixels.
                    if not self.rt.pause(.1):
                        return None
                    continue
                if 1 <= len(boxes) <= 3 and pose[2] >= .6 and pose[3] >= .01:
                    scale = teleporter.locator.spec.bigmap_scale
                    location = marker_location(pose, boxes, scale)
                    locations = [marker_location(pose, [box], scale) for box in boxes]
                    if len(boxes) == 3:
                        selected, distances = select_crystal_route(location)
                        matching = "centroid"
                    else:
                        if self.crystal_routes is None:
                            self.crystal_routes = [load_route(self.resource, f"xinghai_crystal_{n}")
                                                   for n in CRYSTAL_ROUTE_ORDER]
                            if any(route is None for route in self.crystal_routes):
                                raise NavigationError("星光结晶跑图路线无法读取。")
                        selected, distances = select_partial_crystal_route(locations, self.crystal_routes)
                        matching = "route_path"
                    snapshot = {"type": "crystal_snapshot", "boxes": boxes,
                        "marker_count": len(boxes), "marker_locations": locations,
                        "map_pose": list(pose), "location": list(location), "matching": matching,
                        "candidates": distances, "selected": selected,
                        "elapsed": round(time.monotonic()-start, 3)}
                    if selected is not None:
                        self.events.append(snapshot)
                        self.crystal_selection = selected, distances
                        self.crystal_marker_count = len(boxes)
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
                if pose[2] >= .6 and pose[3] >= .01:
                    distance = math.hypot(self.crystal_view[0]-pose[0], self.crystal_view[1]-pose[1])
                    if distance/teleporter.locator.spec.bigmap_scale <= 30:
                        scan["view_confirmed"] = True
            if not self.rt.pause(.1):
                break
        if snapshot is not None:
            self.events.append(snapshot)
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
            for index, center in enumerate(CRYSTAL_VIEWS):
                self.prepare_crystals(teleporter, center)
                if not self.ring(at_hub=True, timed=True):
                    return False
                location = self.locate_crystals(teleporter)
                if location is not None:
                    selected, distances = self.crystal_selection
                    self.events.append({"type": "crystal_route_selection", "location": list(location),
                                        "marker_count": self.crystal_marker_count,
                                        "candidates": distances, "selected": selected})
                    if selected is not None:
                        # The route's teleporter accepts this open map and
                        # locates its viewport before selecting the checkpoint.
                        self.events.append({"type": "crystal_map_handoff", "route": selected["route"]})
                        break
                if not self.rt.main():
                    return False
                if index+1 < len(CRYSTAL_VIEWS):
                    self.rt.log("本次未取得可确认的结晶位置，准备另一处地图视野后重新摇铃。")
            if selected is None:
                # Absence only counts after every known viewport was reached
                # on a reliable map frame. Partial markers still need routing.
                if (not self.rt.stopped and len(self.crystal_scans) == len(CRYSTAL_VIEWS)
                        and all(scan["view_confirmed"] and not scan["capture_failed"]
                                and scan["max_markers"] == 0 for scan in self.crystal_scans)):
                    self.events.append({"type": "crystals_already_collected"})
                    self.rt.log("各结晶视野均未发现标记，星光结晶今日已收集，跳过跑图收集。")
                    return True
                if teleporter.last_frame is not None:
                    try:
                        folder = ROOT / "logs/navigation"
                        folder.mkdir(parents=True, exist_ok=True)
                        path = folder / (datetime.now().strftime("%Y%m%d-%H%M%S-%f")+"-crystal-map.png")
                        if cv2.imwrite(str(path), teleporter.last_frame[:680]):
                            self.events.append({"type": "crystal_map_frame", "file": path.name})
                    except (OSError, cv2.error):
                        pass
                if any(scan["max_markers"] for scan in self.crystal_scans):
                    self.rt.log("已发现星光结晶标记，但未能可靠匹配跑图路线，本次停止收集。")
                else:
                    self.rt.log("未能完成结晶地图定位检查，本次停止收集。")
                return False
            name = selected["route"]
            self.rt.log(f"已发现 {self.crystal_marker_count} 个星光结晶标记，"
                        f"匹配路线 {name}，开始完整跑图收集。")
            return self.rt.navigate(name, mode, card)
        except (ValueError, KeyError, OSError, RuntimeError, cv2.error) as error:
            self.events.append({"type": "stopped", "reason": str(error)})
            self.rt.log(f"星光结晶收集停止：{error}")
            return False
        finally:
            self.report("crystals")
