"""Starsea hub, bell and crystal-location workflow."""
from __future__ import annotations

from file_io import write_json
import math
import re
from task_control import task_time as time
from datetime import datetime
import cv2
from vision import prepared_template
from .settings import ROOT
from navigation.models import NavigationError, Point, load_route, maps
from navigation.geometry import route_distance
from navigation.vision import Locator
from navigation.teleport import Teleporter
from navigation.controller import ForegroundInput
from .settings import foreground_inputs


CRYSTAL_CENTERS = ((3282.8, 2437.6), (1679, 2000), (2865, 2155), (2250, 1475),
                   (3082, 1893), (3140, 1960), (2434.4, 1659.2), (2375, 1605))
CRYSTAL_ROUTE_ORDER = (3, 5, 6, 2, 4, 1, 7, 8)
CRYSTAL_VIEWS = ((1164.5, 767), (1530, 1080), (839.5, 1000))
CRYSTAL_APPEAR_WAIT = 5
CRYSTAL_SCAN_LIMIT = 20
COLLECTION_STATISTICS = (
    ("star_soul", "星之魄", 3, [1105, 96, 80, 30]),
    ("starlight_crystal", "星光结晶", 30, [1105, 136, 80, 29]),
    ("starlight_dust", "星光微尘", 100, [1105, 168, 80, 29]),
)
CRYSTAL_DUPLICATE_RADIUS = 6
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


def crystal_panel_overlap(box):
    """Fixed controls can hide indicators as well as intercept their clicks."""
    x, y, w, h = box
    return any(x < px+pw and x+w > px and y < py+ph and y+h > py
               for px, py, pw, ph in ((1035, 50, 225, 185), (15, 610, 260, 100),
                                      (1170, 606, 85, 42)))


def crystal_border(box):
    x, y, w, h = box
    cx, cy = x+w/2, y+h/2
    return cx <= 80 or cx >= 1200 or cy <= 140 or cy >= 655


def merge_crystal_locations(known, locations):
    # Associate one-to-one within a frame so nearby distinct crystals stay distinct.
    pairs = sorted((math.dist(a, b), i, j) for i, a in enumerate(known)
                   for j, b in enumerate(locations))
    used_known, used_new = set(), set()
    for distance, i, j in pairs:
        if distance <= CRYSTAL_DUPLICATE_RADIUS and i not in used_known and j not in used_new:
            used_known.add(i)
            used_new.add(j)
    known.extend(location for j, location in enumerate(locations) if j not in used_new)


def select_crystal_route(location):
    """Legacy center lookup for offline callers; collection uses route geometry."""
    distances = sorted([{"route": f"xinghai_crystal_{n}", "distance": math.hypot(
        location[0]-CRYSTAL_CENTERS[n-1][0]/2, location[1]-CRYSTAL_CENTERS[n-1][1]/2)}
        for n in CRYSTAL_ROUTE_ORDER], key=lambda item: item["distance"])
    # Close islands (7/8) must have a clear winner; never widen the tolerance.
    selected = distances[0] if distances[0]["distance"] < 25 and (
        distances[1]["distance"]-distances[0]["distance"] >= 8) else None
    return selected, distances


def select_crystal_route_by_path(locations, routes):
    """Choose a route covering every marker, for any supported marker count."""
    distances = []
    for route in routes:
        marker_distances = [route_distance(Point(*location), route.points) for location in locations]
        distances.append({"route": route.id, "distance": max(marker_distances),
                          "mean_distance": sum(marker_distances)/len(marker_distances),
                          "marker_distances": marker_distances})
    distances.sort(key=lambda item: (item["distance"], item["mean_distance"]))
    # Some routes share collection segments. Either can cover the remaining
    # markers, so accept the nearest path without a runner-up margin. Equal
    # worst distances use the mean marker distance, then the fixed route order.
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
        self.crystal_locations = []
        self.crystal_remaining = None
        self.crystal_counter_votes = {}
        self.crystal_counter_conflict = False
        self.crystal_seen = 0
        self.crystal_edge_visits = []
        self.collection_start = None

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
            write_json(file, {"operation": operation, "events": self.events})
        except (OSError, ValueError):
            self.rt.log("星海导航记录未能保存。")

    def bottles(self, mode, card):
        """Try the configured routes in order, carrying confirmed pickups."""
        try:
            first = load_route(self.resource, card.rule.route)
            if first is None:
                raise NavigationError("漂流瓶首条路线无法读取。")
            fallback = first.document.get("fallback_routes", [])
            if (not isinstance(fallback, list)
                    or any(not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", name)
                           for name in fallback)):
                raise NavigationError("漂流瓶后续路线列表无效。")
            names = [card.rule.route, *fallback]
            if len(set(names)) != len(names):
                raise NavigationError("漂流瓶路线列表不能重复。")
            from navigation.daily_route import preflight
            for name in names:
                route = first if name == card.rule.route else load_route(self.resource, name)
                if (route is None or route.map_id != "starsea"
                        or route.document.get("schema_version") != 3):
                    raise NavigationError(f"漂流瓶路线无效：{name}。")
                preflight(self.resource, route, "bottle")
            quota = card.remaining if card.remaining is not None else 1
            if type(quota) is not int or not 1 <= quota <= 100:
                raise NavigationError("漂流瓶任务剩余数量无法可靠读取。")
            count = 0
            for index, name in enumerate(names):
                if self.rt.stopped or not self.rt.main():
                    return False
                self.rt.log(f"漂流瓶：正在尝试路线 {index+1}/{len(names)}。")
                # navigate may fail before constructing RouteActions; do not
                # inherit another task's count in that case.
                self.rt.navigation_count = count
                ok = self.rt.navigate(name, mode, card, progress=count)
                count = self.rt.navigation_count
                self.events.append({"type": "bottle_route_result", "route": name,
                                    "success": bool(ok), "count": count, "quota": quota})
                if self.rt.stopped:
                    return False
                if ok:
                    return True
                # Confirmed pickups must not be repeated after a later failure.
                if count >= quota:
                    return self.rt.main()
                if index+1 < len(names):
                    self.rt.log("本条路线未完成漂流瓶拾取，返回主界面后尝试下一条路线。")
            self.rt.log("漂流瓶路线均已尝试，仍未确认完成拾取。")
            return False
        except (ValueError, KeyError, OSError, RuntimeError, TypeError, cv2.error) as error:
            self.events.append({"type": "stopped", "reason": str(error)})
            self.rt.log(f"漂流瓶查找停止：{error}")
            return False
        finally:
            self.report("bottle")

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
        # Search all four edges, then exclude fixed controls and their counters.
        boxes = self.rt.ui.asset_boxes("T_UI_map_img_icon_star_02", image=image,
            roi=[20, 80, 1240, 620], scale=2/3, threshold=threshold, count=8)
        return [box for box in boxes if not crystal_panel_overlap(box)][:count]

    def crystal_markers(self, frame, boxes):
        real, edges, uncertain, readings = [], [], [], []
        ring = prepared_template(self.resource / "image/nikki/common/crystal_edge_ring.png")
        for box in boxes:
            if not crystal_border(box):
                real.append(box)
                continue
            x, y, w, h = box
            cx, cy = round(x+w/2), round(y+h/2)
            patch = frame[max(0, cy-30):cy+30, max(0, cx-30):cx+30]
            score = 0.0
            if patch.shape[0] >= ring.image.shape[0] and patch.shape[1] >= ring.image.shape[1]:
                rates = cv2.matchTemplate(patch, ring.image, cv2.TM_CCOEFF_NORMED, mask=ring.mask)
                _, score, _, _ = cv2.minMaxLoc(rates)
            # The annulus excludes the star and direction arrow. Only a confirmed
            # circular indicator is clickable; ambiguous border glyphs need a pan.
            hit = math.isfinite(score) and score >= .6
            (edges if hit else uncertain).append(box)
            readings.append({"box": box, "score": score if math.isfinite(score) else None, "hit": hit})
        return real, edges, uncertain, readings

    def read_collection_statistics(self, stage):
        """Confirm each map counter on two consecutive fresh screenshots."""
        result, previous, streaks = {}, {}, {}
        readings = []
        deadline = time.monotonic()+2.5
        while not self.rt.stopped and time.monotonic() < deadline:
            frame = self.rt.capture()
            if frame is None:
                break
            ready = self.rt.recognize("MaaNikki_Navigation_MapFeature", image=frame)
            if not ready or not ready.hit:
                break
            reading = {}
            for key, label, total, roi in COLLECTION_STATISTICS:
                text = self.rt.text(roi, image=frame)
                match = re.fullmatch(r"\s*(\d{1,3})\s*[/／]\s*"+str(total)+r"\s*", text)
                value = int(match[1]) if match and int(match[1]) <= total else None
                reading[key] = {"text": text, "collected": value}
                streaks[key] = streaks.get(key, 0)+1 if value is not None and previous.get(key) == value else 1
                previous[key] = value
                if value is not None and streaks[key] >= 2:
                    result[key] = {"collected": value, "total": total}
                else:
                    result.pop(key, None)
            readings.append(reading)
            if len(result) == len(COLLECTION_STATISTICS) or not self.rt.pause(.15):
                break
        self.events.append({"type": "map_collection_statistics", "stage": stage,
                            "values": result, "readings": readings})
        return result

    def report_collection_statistics(self):
        if self.rt.stopped:
            return False
        if not self.rt.ui.open_map(self.events.append):
            self.rt.log("跑图后未能打开地图读取收集统计。")
            return self.rt.main()
        result = self.read_collection_statistics("after")
        totals, changes = [], []
        for key, label, total, roi in COLLECTION_STATISTICS:
            value = result.get(key)
            totals.append(f"{label} {value['collected']}/{total}" if value else f"{label} 待确认")
            before = (self.collection_start or {}).get(key)
            if value and before and value["collected"] >= before["collected"]:
                changes.append(f"{label} +{value['collected']-before['collected']}")
        self.rt.log("今日收集："+"，".join(totals)+"。")
        if changes:
            self.rt.log("本趟新增："+"，".join(changes)+"。")
        return self.rt.main()

    def prepare_crystals(self, center):
        self.crystal_view = center
        self.events.append({"type": "crystal_view_prepared", "center": list(center)})

    def locate_crystals(self, teleporter):
        # Visible markers govern their lifetime. Keep a bounded total scan and
        # carry coordinates across bells; every click uses a fresh stable frame.
        start = time.monotonic()
        pans, clicks = 0, 0
        snapshot = None
        previous, stable_since = None, start
        counter_reads = 0
        last_markers_at, empty_frames = start, 0
        self.crystal_selection = None
        self.crystal_marker_count = 0
        teleporter.last_frame = None
        teleporter.map_diagnostics = []
        scan = {"type": "crystal_view_scan", "center": list(self.crystal_view),
                "max_markers": 0, "view_confirmed": False, "capture_failed": False,
                "reason": "scan_timeout", "edge_clicks": 0}
        self.crystal_scans.append(scan)
        self.events.append(scan)
        # The map animation takes about .65s before the first feature hit;
        # wait_page then needs a second fresh confirmation at least .25s later.
        # This is an upper bound, not an added delay.
        scan["map_opened"] = self.rt.ui.open_map(self.events.append, attempts=1, seconds=1.5)
        if not scan["map_opened"]:
            scan["reason"] = "map_unavailable"
            self.events.append({"type": "crystal_snapshot_missing", "reason": "map_unavailable",
                                "elapsed": round(time.monotonic()-start, 3)})
            return None
        while not self.rt.stopped and time.monotonic()-start < CRYSTAL_SCAN_LIMIT:
            frame = self.rt.capture()
            if frame is None:
                scan["capture_failed"] = True
                return None
            ready = self.rt.recognize("MaaNikki_Navigation_MapFeature", image=frame)
            maximum = self.rt.recognize("MaaNikki_Navigation_MapMaxScale", image=frame)
            scan["map_ready"] = bool(ready and ready.hit)
            scan["maximum_scale"] = bool(maximum and maximum.hit)
            if ready and ready.hit and not (maximum and maximum.hit):
                teleporter.deadline = start+CRYSTAL_SCAN_LIMIT
                teleporter.zoom()
                previous, stable_since = None, time.monotonic()
                continue
            if ready and ready.hit and maximum and maximum.hit:
                # Avoid pairing terrain coordinates with markers mid-animation.
                patch = frame[240:590, 280:1000]
                now = time.monotonic()
                if self.rt.ui.similarity(previous, patch) <= .995:
                    stable_since = now
                previous = patch.copy()
                if now-stable_since < .15:
                    if not self.rt.pause(.1):
                        return None
                    continue
                boxes = self.boxes(frame)
                if boxes:
                    last_markers_at, empty_frames = time.monotonic(), 0
                else:
                    empty_frames += 1
                    if (time.monotonic()-start >= CRYSTAL_APPEAR_WAIT
                            and time.monotonic()-last_markers_at >= 1.5 and empty_frames >= 2):
                        scan["reason"] = "markers_disappeared"
                        break
                real, edges, uncertain, edge_readings = self.crystal_markers(frame, boxes)
                scan["max_markers"] = max(scan["max_markers"], len(boxes))
                self.crystal_seen = max(self.crystal_seen, len(boxes))
                if (counter_reads < 2 and self.crystal_remaining is None
                        and not self.crystal_counter_conflict):
                    counter_reads += 1
                    text = self.rt.text([1105, 96, 80, 30], image=frame)
                    match = re.fullmatch(r"\s*([0-3])\s*[/／]\s*3\s*", text)
                    if match:
                        remaining = 3-int(match[1])
                        votes = self.crystal_counter_votes
                        votes[remaining] = votes.get(remaining, 0)+1
                        # Confirm a reduced target on separate stable frames.
                        if len(votes) == 1 and votes[remaining] >= 2:
                            self.crystal_remaining = remaining
                    # The first row counts star souls / route markers, not the
                    # 30 crystals shown on the second row.
                    self.events.append({"type": "crystal_marker_counter", "text": text,
                                        "material": "星之魄",
                                        "remaining": self.crystal_remaining})
                if (self.crystal_remaining is not None
                        and self.crystal_seen > self.crystal_remaining):
                    self.events.append({"type": "crystal_counter_conflict",
                        "remaining": self.crystal_remaining, "seen": self.crystal_seen})
                    self.crystal_remaining = None
                    self.crystal_counter_conflict = True
                teleporter.last_frame = frame
                try:
                    pose, _ = teleporter.map_pose(frame)
                except NavigationError:
                    # A visible map header can precede usable terrain pixels.
                    if not self.rt.pause(.1):
                        return None
                    continue
                if pose[2] >= .6 and pose[3] >= .01:
                    scale = teleporter.locator.spec.bigmap_scale
                    locations = [marker_location(pose, [box], scale) for box in real]
                    before = len(self.crystal_locations)
                    merge_crystal_locations(self.crystal_locations, locations)
                    total = len(self.crystal_locations)
                    if total > 3:
                        raise NavigationError("结晶坐标存在冲突，请查看定位记录。")
                    if self.crystal_remaining is not None and total > self.crystal_remaining:
                        self.events.append({"type": "crystal_counter_conflict",
                            "remaining": self.crystal_remaining, "known": total})
                        self.crystal_remaining = None
                        self.crystal_counter_conflict = True
                    if total > before:
                        self.rt.log(f"已定位结晶路线标记 {total} 个。", detail=True)
                    # Reading all remaining crystals is essential when an unseen
                    # indicator may be behind the region switcher.
                    target = self.crystal_remaining if self.crystal_remaining is not None else 3
                    complete = total > 0 and total == target
                    selected, distances = None, []
                    if complete:
                        if self.crystal_routes is None:
                            self.crystal_routes = [load_route(self.resource, f"xinghai_crystal_{n}")
                                                   for n in CRYSTAL_ROUTE_ORDER]
                            if any(route is None for route in self.crystal_routes):
                                raise NavigationError("星光结晶跑图路线无法读取。")
                        selected, distances = select_crystal_route_by_path(
                            self.crystal_locations, self.crystal_routes)
                    snapshot = {"type": "crystal_snapshot", "boxes": real, "edge_boxes": edges,
                        "uncertain_boxes": uncertain, "edge_readings": edge_readings, "marker_count": total,
                        "marker_locations": [list(p) for p in self.crystal_locations],
                        "frame_locations": locations, "remaining": self.crystal_remaining,
                        "complete": complete, "map_pose": list(pose), "matching": "route_path",
                        "candidates": distances, "selected": selected,
                        "elapsed": round(time.monotonic()-start, 3)}
                    self.events.append(snapshot)
                    if selected is not None:
                        location = tuple(sum(p[i] for p in self.crystal_locations)/total for i in (0, 1))
                        self.crystal_selection = selected, distances
                        self.crystal_marker_count = total
                        scan["reason"] = "selected"
                        return location
                    dx = (self.crystal_view[0]-pose[0])/scale
                    dy = (self.crystal_view[1]-pose[1])/scale
                    at_view = math.hypot(dx, dy) <= 30
                    if at_view:
                        scan["view_confirmed"] = True
                    if time.monotonic()-start >= CRYSTAL_SCAN_LIMIT:
                        break
                    # Source viewport + indicator position identifies a tried
                    # transition even after revisiting it through another star.
                    clickable = [box for box in sorted(edges, key=lambda b: (b[0], b[1]))
                        if not any(math.dist(pose[:2], old_pose) < 12
                                   and math.dist((box[0], box[1]), old_box[:2]) < 18
                                   for old_pose, old_box in self.crystal_edge_visits)]
                    if clickable and clicks < 4:
                        box = clickable[0]
                        self.events.append({"type": "crystal_edge_click", "box": box,
                                            "map_pose": list(pose), "known": total})
                        if not self.rt.ui.click_box(box):
                            raise NavigationError("结晶边缘提示点击失败。")
                        self.crystal_edge_visits.append((pose[:2], box))
                        clicks += 1
                        scan["edge_clicks"] = clicks
                        if clicks == 1:
                            self.rt.log("点击结晶边缘提示，定位实际位置。")
                        previous, stable_since = None, time.monotonic()
                        continue
                    # No usable indicator: expose targets hidden by fixed UI.
                    # Fixed views are a fallback, never used instead of a click.
                    if (counter_reads < 2 and self.crystal_remaining is None
                            and not self.crystal_counter_conflict):
                        if not self.rt.pause(.1):
                            return None
                        continue
                    if math.hypot(dx, dy) > 30:
                        if pans >= 3:
                            scan["reason"] = "pan_limit"
                            break
                        begin, end = crystal_pan(dx, dy)
                        if not self.rt.action("Swipe", begin=begin, end=end, duration=250):
                            raise NavigationError("结晶地图快速移动失败。")
                        pans += 1
                        self.events.append({"type": "crystal_fast_pan", "begin": begin, "end": end})
                        previous, stable_since = None, time.monotonic()
                        if not self.rt.pause(.15):
                            return None
                        continue
                    scan["reason"] = "click_limit" if clickable else "viewport_exhausted"
                    break
            if not self.rt.pause(.1):
                break
        if not self.rt.stopped:
            self.rt.diagnostic("crystal_route_match", "未取得能够可靠匹配路线的结晶地图", details={
                "scan": scan, "snapshot": snapshot, "map_matching": teleporter.map_diagnostics})
        self.events.append({"type": "crystal_snapshot_missing", "reason": scan["reason"],
                            "elapsed": round(time.monotonic()-start, 3)})
        return None

    def crystals(self, mode, card):
        with self.rt.input_guard():
            return self._crystals(mode, card)

    def _crystals(self, mode, card):
        try:
            teleporter = self.teleporter()
            self.collection_start = None
            def record_start():
                self.collection_start = self.read_collection_statistics("before")
            teleporter.transport(HUB, on_map_ready=record_start)
            self.rt.log("开始收集星光结晶，摇铃后打开地图定位路线。")
            self.crystal_scans = []
            self.crystal_locations = []
            self.crystal_remaining = None
            self.crystal_counter_votes = {}
            self.crystal_counter_conflict = False
            self.crystal_seen = 0
            self.crystal_edge_visits = []
            location, selected, distances = None, None, []
            # Follow clickable indicators first. The fixed views expose markers
            # occluded by controls; each gets one fresh-bell retry on timeout.
            for index, center in enumerate(CRYSTAL_VIEWS):
                for attempt in range(2):
                    self.prepare_crystals(center)
                    if not self.ring(at_hub=True, timed=True):
                        return False
                    location = self.locate_crystals(teleporter)
                    if location is not None:
                        selected, distances = self.crystal_selection
                        self.events.append({"type": "crystal_route_selection", "location": list(location),
                                            "marker_count": self.crystal_marker_count,
                                            "candidates": distances, "selected": selected})
                        # Hand off the current open map to the route teleporter.
                        self.events.append({"type": "crystal_map_handoff", "route": selected["route"]})
                        break
                    if not self.rt.main():
                        return False
                    reason = self.crystal_scans[-1]["reason"]
                    if (self.rt.stopped or attempt > 0 or reason not in
                            {"map_unavailable", "markers_disappeared", "scan_timeout", "click_limit"}):
                        break
                    self.rt.log("本轮结晶定位未完成，重新摇铃继续定位。")
                if selected is not None:
                    break
                if index+1 < len(CRYSTAL_VIEWS):
                    self.rt.log("切换地图视野，继续定位剩余结晶。")
            if selected is None:
                # Absence only counts after every known viewport was reached
                # on a reliable map frame. Partial markers still need routing.
                if (not self.rt.stopped and self.crystal_remaining in (None, 0)
                        and not self.crystal_locations
                        and not any(scan["max_markers"] for scan in self.crystal_scans)
                        and all(any(scan["center"] == list(center) and scan["view_confirmed"]
                                    and not scan["capture_failed"] for scan in self.crystal_scans)
                                for center in CRYSTAL_VIEWS)):
                    self.events.append({"type": "crystals_already_collected"})
                    self.rt.log("各结晶视野均未发现路线标记，读取地图收集统计。")
                    return self.report_collection_statistics()
                if teleporter.last_frame is not None:
                    try:
                        folder = ROOT / "logs/navigation"
                        folder.mkdir(parents=True, exist_ok=True)
                        path = folder / (datetime.now().strftime("%Y%m%d-%H%M%S-%f")+"-crystal-map.png")
                        from diagnostics import save_image
                        if save_image(path, teleporter.last_frame):
                            self.events.append({"type": "crystal_map_frame", "file": path.name})
                    except (OSError, cv2.error):
                        pass
                if any(scan["max_markers"] for scan in self.crystal_scans):
                    self.rt.log(f"已定位结晶路线标记 {len(self.crystal_locations)} 个，"
                                "结晶位置或路线仍待确认，本次停止收集。")
                else:
                    self.rt.log("未能完成结晶地图定位检查，本次停止收集。")
                return False
            name = selected["route"]
            self.rt.log(f"已发现 {self.crystal_marker_count} 个结晶路线标记，"
                        f"匹配路线 {name}，开始完整跑图收集。")
            if not self.rt.navigate(name, mode, card, crystal_map_open=True):
                return False
            return self.report_collection_statistics()
        except (ValueError, KeyError, OSError, RuntimeError, cv2.error) as error:
            self.events.append({"type": "stopped", "reason": str(error)})
            self.rt.log(f"星光结晶收集停止：{error}")
            if not self.rt.stopped:
                self.rt.diagnostic("crystal_collection", str(error), details={"events": self.events[-8:]})
            return False
        finally:
            self.report("crystals")
