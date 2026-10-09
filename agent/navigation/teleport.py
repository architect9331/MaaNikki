"""Map transport: browse, select detail/list, confirm, then wait for arrival."""
from __future__ import annotations
import math
import re
import time
import cv2
from .models import NavigationError
from .vision import Locator, luma

REGION_NAME = [1030, 48, 200, 45]
REGION_LIST = [885, 64, 310, 600]
PANEL = [843, 320, 175, 176]
BUTTON = [1005, 655, 160, 35]
TERRAIN = [0, 0, 1280, 720]


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

    def check(self, browse=False):
        if self.inputs:
            self.inputs.check()
        if self.rt.stopped or time.monotonic() >= self.deadline:
            raise NavigationError("地图传送已停止或超时。")
        # Map browsing and selected-point details have different page features.
        if browse and not self.rt.hit("MaaNikki_Navigation_MapFeature"):
            raise NavigationError("未能确认地图浏览页面。")

    def dark_text(self, roi):
        return self.rt.ui.text(roi, color=([0, 0, 0], [180, 255, 180]))

    def region_menu_visible(self):
        # An expanded province can push every destination out of view. The
        # menu title proves the dropdown is open independently of its scroll.
        return self.rt.hit("MaaNikki_Navigation_RegionMenuReady")

    def region(self, checkpoint):
        wanted = checkpoint["region"]
        province = checkpoint["province"]
        region_roi = self.rt.ui.roi("AreaBigMapRegionName")
        list_roi = self.rt.ui.roi("AreaBigMapRegionSelect")
        if wanted == "家园":
            # Home names are user-defined; upstream selects the home glyph.
            return self.home_region(region_roi, list_roi)

        def read_region():
            value = normalized(self.dark_text(region_roi))
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
                if menu_visible() and not self.rt.ui.enter_page(
                        lambda: self.rt.key("menu", 0), region_ready, source=menu_visible, seconds=3):
                    raise NavigationError("当前已在目标区域，但地图区域列表未能关闭。")
                if self.rt.ui.wait_page(region_ready, seconds=8):
                    return
            if not open_region_menu():
                raise NavigationError("地图区域列表未展开，未继续滚动或选择点位。")
            if self.rt.ui.enter_page(select_region, region_ready, source=menu_visible,
                                      recover=open_region_menu, seconds=8):
                return
            raise NavigationError("地图区域切换未完成。")
        except NavigationError as error:
            if not self.rt.stopped:
                self.rt.ui.map_failure(0, str(error), self.event, stage="region")
            raise

    def home_region(self, region_roi, list_roi):
        color = ([10, 0, 190], [30, 80, 255])
        menu_visible = self.region_menu_visible
        current = normalized(self.dark_text(region_roi))
        def open_menu():
            self.check()
            return self.rt.ui.open_map(self.event) and self.rt.ui.enter_page(
                lambda: self.rt.ui.click_box(region_roi), menu_visible,
                source="MaaNikki_Navigation_MapFeature",
                recover=lambda: self.rt.ui.open_map(self.event), seconds=3)
        selected = False
        def choose_home():
            nonlocal selected
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
            same = bool(current and name == current)
            self.event({"type": "map_home_observed", "actual": current, "entry": name, "reused": same})
            selected = self.rt.key("menu", 0) if same else self.rt.ui.click_box(box)
            if selected and same:
                self.event({"type": "map_region_reused", "actual": current})
            return selected
        def ready():
            return bool(selected and self.rt.hit("MaaNikki_Navigation_MapFeature") and not menu_visible())
        try:
            if open_menu() and self.rt.ui.enter_page(choose_home, ready, source=menu_visible,
                                                     recover=open_menu, seconds=8):
                self.event({"type": "map_home_selected"})
                return
            raise NavigationError("地图家园图标未找到或区域切换未完成。")
        except NavigationError as error:
            if not self.rt.stopped:
                self.rt.ui.map_failure(0, str(error), self.event, stage="region")
            raise

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
        if frame is None or frame.shape[:2] != (720, 1280):
            raise NavigationError("地图截图尺寸不符合要求。")
        scale = self.locator.spec.bigmap_scale*.25
        patch = cv2.resize(luma(frame), None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
        x, y, score, detail = Locator.match(self.locator.coarse, patch,
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

    def center(self, x, y, force=False):
        while True:
            self.check()
            px, py = self.locate_map()
            dx, dy = (x-px)/self.locator.spec.bigmap_scale, (y-py)/self.locator.spec.bigmap_scale
            if math.hypot(dx, dy) < (20/2/self.locator.spec.bigmap_scale if force else 200*2/3):
                return [round(640+dx), round(360+dy)]
            # The map is half resolution and controller coordinates are 720p:
            # 0.6 * 2 (map pixels) * 2/3 (screen pixels) = 0.8.
            rate = self.locator.spec.bigmap_scale*.8
            vx, vy = round(max(-200, min(200, -dx*rate))), round(max(-200, min(200, -dy*rate)))
            self.event({"type": "map_pan", "begin": [640, 360], "end": [640+vx, 360+vy]})
            if not self.rt.action("Swipe", begin=[640, 360], end=[640+vx, 360+vy], duration=450):
                raise NavigationError("地图拖动失败。")
            self.rt.pause(.2)
            self.rt.ui.stable()

    def confirm_transport(self, checkpoint):
        self.rt.ui.stable()
        button = self.rt.ui.roi("AreaBigMapTeleportButton")
        panel = self.rt.ui.roi("AreaBigMapTeleporterSelect")
        for attempt in range(2):
            self.check()
            frame = self.rt.capture()
            self.last_selection_frame = frame
            text = normalized(self.rt.ui.text(button, image=frame))
            self.event({"type": "teleport_button_observed", "attempt": attempt+1, "text": text, "roi": button})
            if text == "追踪":
                raise NavigationError("传送点尚未解锁。")
            if text == "传送":
                clicked = self.rt.action("Click", target=button)
                self.event({"type": "teleport_button", "clicked": clicked, "target": button})
                if not clicked:
                    raise NavigationError("传送按钮点击失败。")
                return True
            if attempt == 0 and self.rt.ui.find(roi=panel, text=checkpoint["name"], exact=True,
                                               color=([0, 0, 220], [180, 15, 255])):
                continue
            raise NavigationError("选中点位后未找到传送按钮。")

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
        self.locator.previous = None
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
        if math.hypot(pose.x-origin.x, pose.y-origin.y) > route.start_radius:
            raise NavigationError("当前不在路线起点，且路线没有登记传送点。")

    def nearby(self, checkpoint):
        """Skip transport only after two reliable main-page location reads.

        Do not seed the first search with the destination: that would match a
        local area around the wanted position rather than the player's position.
        A low-confidence/global-search failure falls back to normal transport.
        """
        previous = None
        self.locator.previous = None
        for _ in range(2):
            if self.inputs:
                self.inputs.check()
            if self.rt.stopped:
                raise NavigationError("地图传送已停止。")
            frame = self.rt.capture()
            found = self.rt.recognize("MaaNikki_MainDetected", image=frame) if frame is not None else None
            if not found or not found.hit:
                self.locator.previous = None
                return False
            try:
                hint = (previous.x, previous.y) if previous else None
                pose = self.locator.locate(frame, hint)
            except NavigationError:
                self.locator.previous = None
                return False
            reliable = pose.score >= .75 and pose.margin >= .05 and pose.heading is not None
            near = math.hypot(pose.x-checkpoint["x"], pose.y-checkpoint["y"]) < 15
            if not reliable or not near or previous and math.hypot(pose.x-previous.x, pose.y-previous.y) > 1:
                self.locator.previous = None
                return False
            previous = pose
            if not self.rt.pause(.2):
                raise NavigationError("地图传送已停止。")
        self.event({"type": "teleport_skipped_nearby", "checkpoint": checkpoint, **pose.dict()})
        return True
