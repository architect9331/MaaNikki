"""Map transport: browse, select detail/list, confirm, then wait for arrival."""
from __future__ import annotations
import math
import re
from task_control import task_time as time
import cv2
from game_view import require_game_frame
from .models import NavigationError
from .vision import Locator
from .matching import luma, match_map

REGION_NAME = [1030, 48, 200, 45]
REGION_LIST = [885, 64, 310, 600]
PANEL = [843, 320, 175, 176]
BUTTON = [1005, 655, 160, 35]
TERRAIN = [0, 0, 1280, 720]
POINT_DETAIL_DELAY = .5


def terrain_pan(dx, dy):
    # Move most of the remaining distance, keeping both endpoints on terrain.
    factor = min(.8, 880/max(abs(dx), 1), 540/max(abs(dy), 1))
    vx, vy = -dx*factor, -dy*factor
    return ([round(530-vx/2), round(370-vy/2)],
            [round(530+vx/2), round(370+vy/2)])


def normalized(text):
    return re.sub(r"\s+", "", text)


class Teleporter:
    def __init__(self, runtime, locator: Locator, event=lambda value: None, inputs=None):
        self.rt, self.locator, self.event, self.inputs = runtime, locator, event, inputs
        self.deadline = 0
        self.hint = None
        self.last_frame = None
        self.last_selection_frame = None
        self.map_diagnostics = []
        self.home_region_name = None

    def check(self, browse=False):
        if self.inputs:
            self.inputs.check()
        if self.rt.stopped or time.monotonic() >= self.deadline:
            raise NavigationError("地图传送已停止或超时。")
        # Map browsing and selected-point details have different page features.
        if browse and not self.rt.hit("MaaNikki_Navigation_MapFeature"):
            raise NavigationError("未能确认地图浏览页面。")

    def region_name(self, expected=None):
        """Read the small map header without discarding its antialiased strokes."""
        frame = self.rt.frame()
        if frame is None:
            return ""
        x, y, w, h = self.rt.ui.roi("AreaBigMapRegionName")
        # Leave room around the glyphs instead of fitting text detection into
        # the original 27-pixel-high click region.
        left, top = max(0, x-4), max(0, y-6)
        right, bottom = min(frame.shape[1], x+w+4), min(frame.shape[0], y+h+6)
        roi = [left, top, right-left, bottom-top]
        raw = normalized(self.rt.ui.text(roi, image=frame))
        wanted = normalized(expected) if expected is not None else None
        readings = [{"method": "original", "text": raw}]
        value = raw
        if not raw or wanted is not None and raw != wanted:
            # Re-read the same pixels at a useful OCR size. A mismatch is not
            # an alias: only the exact requested name can confirm the region.
            patch = cv2.resize(frame[top:bottom, left:right], None, fx=3, fy=3,
                               interpolation=cv2.INTER_CUBIC)
            patch = cv2.copyMakeBorder(patch, 8, 8, 8, 8, cv2.BORDER_REPLICATE)
            enlarged = normalized(self.rt.text([0, 0, patch.shape[1], patch.shape[0]],
                                               image=patch))
            readings.append({"method": "enlarged", "text": enlarged})
            if not raw or wanted is not None and enlarged == wanted:
                value = enlarged
        self.rt.evidence.observe("map_region_name", roi=roi, expected=wanted,
                                 readings=readings, selected=value)
        return value

    def region_menu_visible(self):
        # An expanded province can push every destination out of view. The
        # menu title proves the dropdown is open independently of its scroll.
        return self.rt.hit("MaaNikki_Navigation_RegionMenuReady")

    def close_region_menu(self):
        self.check()
        def ready():
            return (self.rt.hit("MaaNikki_Navigation_MapFeature")
                    and not self.region_menu_visible())
        closed = self.rt.ui.enter_page(lambda: self.rt.click_template(
            "MaaNikki_Navigation_RegionMenuClose", attempts=1, wait_seconds=0, verify=False),
            ready, source=self.region_menu_visible, seconds=3)
        self.event({"type": "map_region_menu_closed", "success": closed})
        return closed

    def region(self, checkpoint):
        wanted = checkpoint["region"]
        province = checkpoint["province"]
        region_roi = self.rt.ui.roi("AreaBigMapRegionName")
        list_roi = self.rt.ui.roi("AreaBigMapRegionSelect")
        if wanted == "家园":
            # Home names are user-defined; upstream selects the home glyph.
            return self.home_region(region_roi, list_roi)

        def read_region():
            value = self.region_name(wanted)
            self.event({"type": "map_region_observed", "wanted": wanted, "actual": value})
            return value

        menu_visible = self.region_menu_visible

        def region_ready():
            return (self.rt.hit("MaaNikki_Navigation_MapFeature")
                    and not menu_visible() and read_region() == normalized(wanted))

        def open_region_menu():
            self.check()
            if not self.rt.ui.open_map(self.event):
                return False
            opened = self.rt.ui.enter_page(lambda: self.rt.ui.click_box(region_roi), menu_visible,
                source="MaaNikki_Navigation_MapFeature",
                recover=lambda: self.rt.ui.open_map(self.event), seconds=3)
            self.event({"type": "map_region_menu", "opened": opened})
            return opened

        def select_region():
            self.check()
            if province == "星海":
                return self.rt.ui.find(roi=list_roi, text=province, exact=True, scroll=True)
            result = self.rt.recognize("MaaNikki_Daily_OCR", {"roi": list_roi, "expected": ".+"})
            entries = {normalized(getattr(item, "text", "")) for item in
                       (getattr(result, "filtered_results", None) or [])}
            first = {"心愿原野": "纪念山地", "伊赞之土": "巨木之森"}.get(province)
            if province not in entries or first and first not in entries:
                if not self.rt.ui.find(roi=list_roi, text=province, exact=True, scroll=True):
                    return False
                if not self.rt.ui.wait_page(menu_visible, seconds=3):
                    return False
            return self.rt.ui.find(roi=list_roi, text=wanted, exact=True, scroll=True)

        try:
            if read_region() == normalized(wanted):
                if menu_visible() and not self.close_region_menu():
                    raise NavigationError("当前已在目标区域，但地图区域列表未能关闭。")
                if self.rt.ui.wait_page(region_ready, seconds=8):
                    return
            if not open_region_menu():
                raise NavigationError("地图区域列表未展开，未继续滚动或选择点位。")
            if self.rt.ui.enter_page(select_region, region_ready, source=menu_visible,
                                      recover=open_region_menu, seconds=8,
                                      diagnostic_context={"operation": "select_map_region",
                                                          "province": province, "wanted": wanted,
                                                          "header_roi": region_roi,
                                                          "list_roi": list_roi}):
                return
            raise NavigationError("地图区域切换未完成。")
        except NavigationError as error:
            if not self.rt.stopped:
                self.rt.ui.map_failure(0, str(error), self.event, stage="region")
            raise

    def home_region(self, region_roi, list_roi):
        color = ([10, 0, 190], [30, 80, 255])
        menu_visible = self.region_menu_visible
        current = self.region_name()
        def known_home_ready():
            return (self.rt.hit("MaaNikki_Navigation_MapFeature") and not menu_visible()
                    and self.region_name(self.home_region_name) == self.home_region_name)
        if current and current == self.home_region_name:
            if menu_visible() and not self.close_region_menu():
                raise NavigationError("当前已在家园，但地图区域列表未能关闭。")
            if self.rt.ui.wait_page(known_home_ready, seconds=3):
                self.event({"type": "map_region_reused", "actual": current})
                return
        def open_menu():
            self.check()
            return self.rt.ui.open_map(self.event) and self.rt.ui.enter_page(
                lambda: self.rt.ui.click_box(region_roi), menu_visible,
                source="MaaNikki_Navigation_MapFeature",
                recover=lambda: self.rt.ui.open_map(self.event), seconds=3)
        selected = False
        home_name = None
        def choose_home():
            nonlocal selected, home_name
            self.check()
            box = self.rt.ui.find(roi=list_roi, asset="IconBigMapHomeFeature",
                                 scale=2/3, threshold=.9, color=color, scroll=True, click=False)
            if not box:
                return False
            # Compare the home row with the current header, including renamed
            # homes. Reselecting an active region resets the map zoom.
            x, y, w, h = box
            text_x = x+w+4
            name_roi = [text_x, max(0, y-8), max(1, list_roi[0]+list_roi[2]-text_x), h+16]
            name = normalized(self.rt.ui.text(name_roi))
            home_name = name
            same = bool(current and name == current)
            self.event({"type": "map_home_observed", "actual": current, "entry": name, "reused": same})
            selected = self.close_region_menu() if same else self.rt.ui.click_box(box)
            if selected and same:
                self.event({"type": "map_region_reused", "actual": current})
            return selected
        def ready():
            return bool(selected and self.rt.hit("MaaNikki_Navigation_MapFeature") and not menu_visible())
        try:
            if open_menu() and self.rt.ui.enter_page(choose_home, ready, source=menu_visible,
                                                     recover=open_menu, seconds=8):
                # Remember only a confirmed home selection for this route.
                # Empty/changed names still use the glyph-based entrance.
                actual = self.region_name(home_name)
                self.home_region_name = home_name if home_name and actual == home_name else None
                self.event({"type": "map_home_selected"})
                return
            raise NavigationError("地图家园图标未找到或区域切换未完成。")
        except NavigationError as error:
            if not self.rt.stopped:
                self.rt.ui.map_failure(0, str(error), self.event, stage="region")
            raise

    def player_in_home(self):
        """Compare a fresh player map with the named home row without selecting it."""
        self.deadline = time.monotonic()+120
        self.check()
        if not self.rt.main() or not self.rt.ui.open_map(self.event):
            raise NavigationError("未能打开地图确认是否位于家园。")
        region_roi = self.rt.ui.roi("AreaBigMapRegionName")
        list_roi = self.rt.ui.roi("AreaBigMapRegionSelect")
        current = self.region_name()
        if not current:
            raise NavigationError("未能读取当前地图名称，无法确认是否位于家园。")
        if not self.rt.ui.enter_page(lambda: self.rt.ui.click_box(region_roi),
                self.region_menu_visible, source="MaaNikki_Navigation_MapFeature",
                recover=lambda: self.rt.ui.open_map(self.event), seconds=3):
            raise NavigationError("未能打开地图区域列表确认家园。")
        box = self.rt.ui.find(roi=list_roi, asset="IconBigMapHomeFeature", scale=2/3,
                              threshold=.9, color=([10, 0, 190], [30, 80, 255]),
                              scroll=True, click=False)
        if not box:
            raise NavigationError("未找到家园地图图标，无法确认当前地图。")
        x, y, w, h = box
        text_x = x+w+4
        name_roi = [text_x, max(0, y-8), max(1, list_roi[0]+list_roi[2]-text_x), h+16]
        name = normalized(self.rt.ui.text(name_roi))
        if not name:
            raise NavigationError("未能读取家园地图名称，无法确认当前地图。")
        in_home = current == name
        self.event({"type": "map_player_home", "actual": current, "entry": name,
                    "in_home": in_home})
        if not self.close_region_menu():
            raise NavigationError("确认家园后未能关闭地图区域列表。")
        return in_home

    def zoom(self):
        for _ in range(3):
            self.check()
            if self.rt.hit("MaaNikki_Navigation_MapMaxScale"):
                return
            if not self.rt.click_template("MaaNikki_Navigation_MapZoom", attempts=1):
                break
            self.rt.pause(.5)
        if not self.rt.hit("MaaNikki_Navigation_MapMaxScale"):
            raise NavigationError("未能放大地图，请确认没有点位详情遮挡缩放按钮。")

    def map_pose(self, frame):
        self.map_diagnostics = []
        require_game_frame(frame)
        scale = self.locator.spec.bigmap_scale*.25
        patch = cv2.resize(luma(frame), None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
        x, y, score, detail = match_map(self.locator.coarse, patch,
                                           peak_mask=self.locator.bigmap_mask, kernel=9)
        if not all(math.isfinite(v) for v in (x, y, score, detail)) or score <= 0 or detail <= 0:
            raise NavigationError("地图画面没有可用的定位特征，未继续拖动或选择点位。")
        pose = x*4, y*4, score, detail
        self.map_diagnostics = [{"method": "whole_map_local_peak", "x": pose[0], "y": pose[1],
                                 "score": score, "local_peak": detail}]
        return pose, 1

    def locate_map(self):
        self.check()
        self.last_frame = self.rt.capture()
        pose, _ = self.map_pose(self.last_frame)
        self.hint = pose[:2]
        self.event({"type": "bigmap_pose", "x": pose[0], "y": pose[1],
                    "score": pose[2], "local_peak": pose[3], "windows": self.map_diagnostics})
        return self.hint

    def refine_player_pose(self, frame, pose):
        """Cross-check a coarse global candidate using separate terrain tiles."""
        if pose[2] < .45 or pose[3] < .01:
            return pose
        scale = self.locator.spec.bigmap_scale
        candidates = []
        # These disjoint tiles avoid the player marker, map labels near the
        # center and fixed corner controls. Missing terrain can spoil one tile
        # without discarding a match supported by two others.
        for top in (160, 400):
            for left in (260, 500, 740):
                width, height = 240, 200
                patch = cv2.resize(luma(frame[top:top+height, left:left+width]),
                                   None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                dx, dy = (left+width/2-640)*scale, (top+height/2-360)*scale
                cx, cy = pose[0]+dx, pose[1]+dy
                x0 = max(0, math.floor(cx-patch.shape[1]/2-12))
                y0 = max(0, math.floor(cy-patch.shape[0]/2-12))
                x1 = min(self.locator.spec.width, math.ceil(cx+patch.shape[1]/2+12))
                y1 = min(self.locator.spec.height, math.ceil(cy+patch.shape[0]/2+12))
                try:
                    x, y, score, peak = match_map(
                        self.locator.map[y0:y1, x0:x1], patch, minimum_score=.65)
                except NavigationError:
                    continue
                center = x+x0-dx, y+y0-dy
                accepted = (score >= .65 and peak >= .01
                            and math.hypot(center[0]-pose[0], center[1]-pose[1]) <= 6)
                self.map_diagnostics.append({"method": "player_terrain_tile",
                    "roi": [left, top, width, height], "x": center[0], "y": center[1],
                    "score": score, "local_peak": peak, "accepted": accepted})
                if accepted:
                    candidates.append((*center, score, peak))
        if len(candidates) < 2 or any(math.hypot(a[0]-b[0], a[1]-b[1]) > 2
                                      for a in candidates for b in candidates):
            return pose
        return (sum(p[0] for p in candidates)/len(candidates),
                sum(p[1] for p in candidates)/len(candidates),
                min(p[2] for p in candidates), min(p[3] for p in candidates))

    def player_location(self):
        """A freshly opened map centers the player; never pan this view."""
        self.rt.log("导航位置需要复核，正在打开大地图定位角色。")
        previous = None
        self.deadline = time.monotonic()+30
        try:
            # Close any old map first: only a fresh opening establishes that
            # its viewport center is the player's current position.
            if not self.rt.main() or not self.rt.ui.open_map(self.event):
                raise NavigationError("未能打开大地图复核角色位置。")
            self.zoom()
            end = min(self.deadline, time.monotonic()+8)
            while time.monotonic() < end:
                self.check(browse=True)
                self.last_frame = self.rt.capture()
                self.map_diagnostics = []
                try:
                    pose, _ = self.map_pose(self.last_frame)
                    if pose[2] < .6:
                        pose = self.refine_player_pose(self.last_frame, pose)
                except NavigationError:
                    pose = None
                self.event({"type": "map_player_match", "pose": list(pose) if pose else None,
                            "windows": list(self.map_diagnostics)})
                if pose and pose[2] >= .6 and pose[3] >= .01:
                    self.event({"type": "map_player_observation", "x": pose[0], "y": pose[1],
                                "score": pose[2], "local_peak": pose[3]})
                    if previous and math.hypot(pose[0]-previous[0], pose[1]-previous[1]) <= 1.5:
                        return pose[:2]
                    previous = pose
                else:
                    previous = None
                if not self.rt.pause(.25):
                    break
            raise NavigationError("大地图角色位置未能稳定匹配，未继续移动。")
        except NavigationError as error:
            if not self.rt.stopped:
                self.rt.ui.map_failure(0, str(error), self.event, stage="relocalization")
            raise
        finally:
            if not self.rt.stopped and not self.rt.main():
                raise NavigationError("复核角色位置后未能返回主界面。")

    def center(self, x, y, force=False):
        while True:
            self.check()
            px, py = self.locate_map()
            dx, dy = (x-px)/self.locator.spec.bigmap_scale, (y-py)/self.locator.spec.bigmap_scale
            if math.hypot(dx, dy) < (20/2/self.locator.spec.bigmap_scale if force else 200*2/3):
                return [round(640+dx), round(360+dy)]
            begin, end = terrain_pan(dx, dy)
            self.event({"type": "map_pan", "begin": begin, "end": end})
            if not self.rt.action("Swipe", begin=begin, end=end, duration=300):
                raise NavigationError("地图拖动失败。")
            if not self.rt.pause(.15):
                raise NavigationError("地图传送已停止。")
            # Animated HUD decorations should not delay every terrain drag.
            self.rt.ui.stable(threshold=.995, seconds=1.5, roi=[300, 120, 650, 480])

    def confirm_transport(self, checkpoint):
        button = self.rt.ui.roi("AreaBigMapTeleportButton")
        panel = self.rt.ui.roi("AreaBigMapTeleporterSelect")
        observations = 0
        text = ""
        def detail_ready():
            nonlocal observations, text
            self.check()
            frame = self.rt.capture()
            self.last_selection_frame = frame
            text = normalized(self.rt.ui.text(button, image=frame)) if frame is not None else ""
            observations += 1
            self.event({"type": "teleport_button_observed", "attempt": observations, "text": text, "roi": button})
            return text in ("传送", "追踪")
        def find_entry(click=False):
            self.check()
            return self.rt.ui.find(roi=panel, text=checkpoint["name"], exact=True,
                                   color=([0, 0, 220], [180, 15, 255]),
                                   scroll=False, click=click)
        def select_entry():
            clicked = bool(find_entry(click=True))
            self.event({"type": "teleport_list_selected", "name": checkpoint["name"], "clicked": clicked})
            return clicked and self.rt.pause(POINT_DETAIL_DELAY)
        try:
            # Overlapping markers open a candidate list first. Only retry that
            # safe selection while the named entry is still visible; wait for
            # its detail button instead of reading the list's previous frame.
            if not self.rt.ui.enter_page(select_entry, detail_ready,
                    source=find_entry, attempts=2, seconds=4):
                raise NavigationError("选中点位后未找到传送按钮。")
            if text == "追踪":
                raise NavigationError("传送点尚未解锁。")
            clicked = self.rt.action("Click", target=button)
            self.event({"type": "teleport_button", "clicked": clicked, "target": button})
            if not clicked:
                raise NavigationError("传送按钮点击失败。")
            return True
        except NavigationError as error:
            if not self.rt.stopped:
                self.rt.ui.map_failure(0, str(error), self.event, stage="selection")
            raise

    def transport(self, checkpoint):
        self.deadline = time.monotonic()+120
        self.check()
        if not self.rt.ui.open_map(self.event):
            raise NavigationError("无法打开地图。")
        self.region(checkpoint)
        self.rt.pause(.5)
        self.zoom()
        def selection_visible():
            return (normalized(self.rt.ui.text(self.rt.ui.roi("AreaBigMapTeleportButton")))
                    in ("传送", "追踪") or bool(self.rt.text(
                        self.rt.ui.roi("AreaBigMapTeleporterSelect"), re.escape(checkpoint["name"]))))
        def restore_map():
            if not self.rt.ui.open_map(self.event):
                return False
            self.region(checkpoint)
            self.zoom()
            return True
        def select_checkpoint():
            self.check(browse=True)
            target = self.center(checkpoint["x"], checkpoint["y"])
            self.event({"type": "checkpoint_click", "name": checkpoint["name"], "target": target})
            return self.rt.action("Click", target=target)
        if not self.rt.ui.enter_page(select_checkpoint, selection_visible,
                source="MaaNikki_Navigation_MapFeature", recover=restore_map):
            raise NavigationError("传送点选择失败。")
        if not self.rt.ui.enter_page(lambda: self.confirm_transport(checkpoint),
                "MaaNikki_MainDetected", source=selection_visible, attempts=1, seconds=60):
            raise NavigationError("传送后未恢复主界面。")
        self.locator.reset()
        self.hint = checkpoint["x"], checkpoint["y"]
        self.event({"type": "teleport", "checkpoint": checkpoint})

    def prepare(self, route):
        origin = route.points[0]
        checkpoint = route.document.get("teleport")
        if checkpoint:
            if self.nearby(checkpoint):
                return
            self.transport(checkpoint)
            return
        pose = self.locator.locate(self.rt.capture(), (origin.x, origin.y))
        if not pose.position_reliable:
            raise NavigationError("路线起点的小地图位置未能可信匹配，尚未开始移动。")
        if math.hypot(pose.x-origin.x, pose.y-origin.y) > route.start_radius:
            raise NavigationError("当前不在路线起点，且路线没有登记传送点。")

    def nearby(self, checkpoint):
        """Skip transport only after two reliable main-page location reads.

        Do not seed the first search with the destination: that would match a
        local area around the wanted position rather than the player's position.
        A low-confidence/global-search failure falls back to normal transport.
        """
        previous = None
        self.locator.reset()
        for _ in range(2):
            if self.inputs:
                self.inputs.check()
            if self.rt.stopped:
                raise NavigationError("地图传送已停止。")
            frame = self.rt.capture()
            found = self.rt.recognize("MaaNikki_MainDetected", image=frame) if frame is not None else None
            if not found or not found.hit:
                self.locator.reset()
                return False
            try:
                hint = (previous.x, previous.y) if previous else None
                pose = self.locator.locate(frame, hint)
            except NavigationError:
                self.locator.reset()
                return False
            reliable = (pose.score >= .75 and pose.margin >= .05 and pose.heading is not None
                        and pose.position_reliable)
            near = math.hypot(pose.x-checkpoint["x"], pose.y-checkpoint["y"]) < 15
            if not reliable or not near or previous and math.hypot(pose.x-previous.x, pose.y-previous.y) > 1:
                self.locator.reset()
                return False
            previous = pose
            if not self.rt.pause(.2):
                raise NavigationError("地图传送已停止。")
        self.event({"type": "teleport_skipped_nearby", "checkpoint": checkpoint, **pose.dict()})
        return True
