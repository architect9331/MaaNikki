"""Go Now -> dismiss map dialogue -> coordinate selection -> travel -> bell."""
from __future__ import annotations

import math
import time

import cv2

from .planner import bell_notice, classify
from .runtime import parameters
from .starsea import Starsea
from navigation.models import NavigationError, load_route


GO = "MaaNikki_Xinghai_MeteorGo"
CLOSE = "MaaNikki_Xinghai_MeteorClose"
MAP = "MaaNikki_Navigation_MapFeature"
READY = "MaaNikki_Xinghai_DailyReady"


def meteor_routes(resource):
    routes = {}
    for index in range(1, 5):
        name = f"xinghai_meteor{index}"
        route = load_route(resource, name)
        if route is None:
            raise NavigationError(f"流星路线缺失：{name}。")
        target = route.document.get("editor_target", {})
        checkpoint = route.document.get("teleport", {})
        if (route.document.get("schema_version") != 3 or route.map_id != "starsea"
                or target.get("map") != "starsea"
                or target.get("coordinate_system") != "navigation_map_pixels"
                or checkpoint.get("province") != "星海" or checkpoint.get("region") != "星海"
                or not checkpoint.get("name") or any(point.action for point in route.points)):
            raise NavigationError(f"流星路线配置无效：{name}。")
        coordinates = [target["x"], target["y"], checkpoint["x"], checkpoint["y"]]
        coordinates += [value for point in route.points for value in (point.x, point.y, point.radius)]
        if not all(math.isfinite(float(value)) for value in coordinates):
            raise NavigationError(f"流星路线坐标无效：{name}。")
        routes[name] = route
    return routes


def select_meteor_route(location, routes):
    candidates = sorted(({
        "route": name,
        "distance": math.hypot(location[0]-route.document["editor_target"]["x"],
                               location[1]-route.document["editor_target"]["y"]),
    } for name, route in routes.items()), key=lambda item: item["distance"])
    selected = (candidates[0] if len(candidates) >= 2 and candidates[0]["distance"] < 25
                and candidates[1]["distance"]-candidates[0]["distance"] >= 8 else None)
    return selected, candidates


class Meteor(Starsea):
    def selected_card(self, card, roi):
        if not self.rt.hit(READY):
            return False
        text = self.rt.text(roi)
        current = classify("xinghai", text, card.slot)
        return bool(not bell_notice(text) and current.rule and current.rule.key == "meteor"
                    and current.remaining != 0 and not self.rt.hit("MaaNikki_Xinghai_DailyFinished"))

    def select_card(self, card):
        # slot comes from the daily scan. Reopen and select it, since another
        # scanned card or an earlier daily executor can have changed the page.
        if not (self.rt.hit(READY) and self.rt.ui.wait_page(READY) or
                self.rt.ui.calendar() and self.rt.click_template("MaaNikki_Xinghai_Entrance")):
            return False
        layout = parameters(self.rt.context, "MaaNikki_Xinghai_DailyLayout")
        centers, roi = layout["card_centers"], layout["detail_roi"]
        if type(card.slot) is not int or not 0 <= card.slot < len(centers):
            raise NavigationError("扫描记录中的流星卡片位置无效。")
        self.events.append({"type": "meteor_card", "slot": card.slot, "target": centers[card.slot]})
        return (self.rt.action("Click", target=centers[card.slot])
                and self.rt.ui.wait_page(lambda: self.selected_card(card, roi), seconds=8))

    def dialog_ready(self):
        # The bottom dialogue hides the normal map-page marker.
        return bool(self.rt.hit(CLOSE) and self.rt.text([90, 520, 410, 42], "呼唤流星"))

    def clean_map(self):
        return bool(self.rt.hit(MAP) and not self.rt.hit(CLOSE)
                    and not self.rt.text([90, 520, 410, 42], "呼唤流星"))

    def open_target(self, card):
        if not self.select_card(card):
            return False
        roi = parameters(self.rt.context, "MaaNikki_Xinghai_DailyLayout")["detail_roi"]
        return self.rt.ui.enter_page(lambda: self.rt.click_template(
            GO, attempts=1, wait_seconds=0, verify=False), self.dialog_ready,
            source=lambda: self.selected_card(card, roi), recover=lambda: self.select_card(card), seconds=8)

    def close_dialog(self):
        ok = self.rt.ui.enter_page(lambda: self.rt.click_template(
            CLOSE, attempts=1, wait_seconds=0, verify=False), self.clean_map,
            source=self.dialog_ready, seconds=3)
        self.events.append({"type": "meteor_dialog_closed", "success": ok})
        return ok

    def locate_target(self, teleporter, routes):
        # Wait for trustworthy coordinates to stop moving, rather than a fixed
        # five-second delay. Never pan/center before recording the Go Now target.
        deadline = time.monotonic()+10
        anchor, since = None, None
        while not self.rt.stopped and time.monotonic() < deadline:
            frame = self.rt.capture()
            teleporter.last_frame = frame
            pose, selected, candidates = None, None, []
            if frame is not None:
                checks = [self.rt.recognize(node, image=frame) for node in (MAP,
                    "MaaNikki_Navigation_MapMaxScale", CLOSE)]
                if checks[0] and checks[0].hit and checks[1] and checks[1].hit and not (
                        checks[2] and checks[2].hit):
                    try:
                        pose, _ = teleporter.map_pose(frame)
                        if pose[2] >= .6 and pose[3] >= .01:
                            selected, candidates = select_meteor_route(pose, routes)
                    except NavigationError:
                        pass
            self.events.append({"type": "meteor_center", "pose": list(pose) if pose else None,
                                "candidates": candidates, "selected": selected})
            if selected is not None:
                if anchor is None or selected["route"] != anchor[0] or math.hypot(
                        pose[0]-anchor[1], pose[1]-anchor[2]) > 3:
                    anchor, since = (selected["route"], pose[0], pose[1]), time.monotonic()
                elif time.monotonic()-since >= .6:
                    self.events.append({"type": "meteor_route_selection", **selected})
                    return selected["route"]
            else:
                anchor, since = None, None
            if not self.rt.pause(.25):
                break
        raise NavigationError("流星地图中心未稳定匹配已登记点位，未选择传送点。")

    def run(self, mode, card):
        try:
            if mode != "xinghai" or not card.rule or card.rule.key != "meteor":
                raise NavigationError("召唤流星必须从星海拾光的对应任务执行。")
            routes = meteor_routes(self.resource)
            with self.rt.input_guard():
                self.rt.log("正在打开召唤流星任务的立即前往入口。")
                if not self.open_target(card) or not self.close_dialog():
                    raise NavigationError("未能打开并关闭流星地图对话框。")
                teleporter = self.teleporter()
                # Map zoom controls keep the viewport center. Prepare scale
                # without panning away from the Go Now location.
                teleporter.deadline = time.monotonic()+20
                teleporter.zoom()
                name = self.locate_target(teleporter, routes)
                self.rt.log(f"已匹配流星点位 {name[-1]}，开始传送并沿路线前往。")
                arrived = self.rt.navigate(name, mode, card, meteor_travel=True)
                self.events.append({"type": "meteor_travel_result", "route": name,
                                    "arrived": bool(arrived)})
                if not arrived or self.rt.stopped:
                    return False
                # Travel is not daily-task credit. Ring only after the verified
                # navigator reaches the endpoint, without returning to the hub.
                ok = self.ring(at_hub=True)
                self.events.append({"type": "meteor_input_completed", "success": bool(ok)})
                if ok:
                    self.rt.log("召唤流星摇铃操作已结束，稍后统一核实积分。")
                return bool(ok)
        except (ValueError, KeyError, TypeError, OSError, RuntimeError, cv2.error) as error:
            self.events.append({"type": "stopped", "reason": str(error)})
            self.rt.log(f"召唤流星停止：{error}")
            if not self.rt.stopped:
                self.rt.ui.map_failure(0, str(error), self.events.append, stage="meteor")
            return False
        finally:
            self.report("meteor")
