"""Collect the crystal tree using its two visible counters and a recorded loop."""
from __future__ import annotations

import re

import cv2

from navigation.models import NavigationError, load_route, maps
from navigation.session import create_navigator
from navigation.teleport import Teleporter
from navigation.vision import Locator
from task_control import TaskStopped, task_time as time
from .settings import foreground_inputs
from .starsea import HUB, Starsea


class CrystalTree(Starsea):
    # The supplied map detail panel shows these capacities. Both captions and
    # denominators must match; an unreadable value must never become zero.
    COUNTERS = (("stored", "存储量", 120, [110, 650, 300, 48]),
                ("ready", "待收获量", 40, [430, 650, 310, 48]))
    TITLE_ROI = [100, 520, 350, 48]

    def panel_visible(self, frame=None):
        return "星忆晶树" in self.rt.text(self.TITLE_ROI, image=frame).replace(" ", "")

    def read_panel(self):
        frame = self.rt.frame()
        if frame is None or not self.panel_visible(frame):
            return None
        values, readings = {}, {}
        for key, caption, capacity, roi in self.COUNTERS:
            raw = self.rt.text(roi, image=frame)
            text = re.sub(r"\s+", "", raw).replace("／", "/")
            readings[key] = raw
            match = re.search(re.escape(caption) + r"[:：]?(\d+)/(\d+)$", text)
            if not match:
                return None
            count, total = map(int, match.groups())
            if total != capacity or not 0 <= count <= total:
                return None
            values[key] = count
        self.rt.evidence.observe("crystal_tree_counters", readings=readings, values=values)
        return values

    def open_panel(self, teleporter, route):
        # The tree is inside the recorded lap. Nearby map icons may be grouped
        # with Unbound Hub, so both known map positions can open the same list.
        ring = route.points[1:]
        tree = (sum(p.x for p in ring) / len(ring), sum(p.y for p in ring) / len(ring))
        for position in (tree, (HUB["x"], HUB["y"])):
            if not self.rt.main() or not self.rt.ui.open_map(self.events.append):
                raise NavigationError("未能打开星忆晶树地图。")
            teleporter.deadline = time.monotonic() + 45
            teleporter.region(HUB)
            teleporter.zoom()
            target = teleporter.center(*position, allow_visible=True)

            def selection_visible():
                return self.panel_visible() or bool(self.rt.ui.find(
                    roi=self.rt.ui.roi("AreaBigMapTeleporterSelect"),
                    text="星忆晶树", exact=True, scroll=False, click=False))

            if not self.rt.ui.enter_page(
                    lambda: self.rt.action("Click", target=target), selection_visible,
                    source="MaaNikki_Navigation_MapFeature", attempts=1, seconds=3):
                continue
            if self.panel_visible():
                return
            if self.rt.ui.enter_page(
                    lambda: self.rt.ui.find(roi=self.rt.ui.roi("AreaBigMapTeleporterSelect"),
                                           text="星忆晶树", exact=True, scroll=False),
                    self.panel_visible, source=selection_visible, attempts=1, seconds=5):
                return
        raise NavigationError("未能打开星忆晶树详情，未继续摇铃或收集。")

    def observe_counts(self, teleporter, route, *, after_bell=None):
        self.open_panel(teleporter, route)
        state = None

        def ready():
            nonlocal state
            state = self.read_panel()
            if state is None:
                return False
            if after_bell is not None:
                # At most 40 can fall per bell. Passive pickups can reduce the
                # combined count, but a bell cannot create extra stored crystals.
                moved = after_bell["stored"] - state["stored"]
                return (0 < moved <= 40
                        and sum(state.values()) <= sum(after_bell.values()))
            return True

        if not self.rt.ui.wait_page(ready, seconds=8):
            message = "摇铃后未确认存储结晶落地" if after_bell is not None else "无法读取星忆晶树存储量和待收获量"
            raise NavigationError(message + "，本次停止。")
        self.events.append({"type": "tree_counts", **state, "after_bell": after_bell})
        self.rt.log(f"星忆晶树：存储 {state['stored']}/120，待收获 {state['ready']}/40。")
        if not self.rt.main():
            raise NavigationError("星忆晶树详情未能关闭，未继续移动。")
        teleporter.locator.reset()
        return state

    def follow(self, teleporter, route):
        teleporter.prepare(route)
        create_navigator(self.rt, teleporter, event=self.events.append).follow(route)
        if self.rt.stopped:
            raise TaskStopped("星忆晶树收集已停止")

    def collect(self):
        try:
            if not foreground_inputs():
                raise NavigationError("星忆晶树收集需要前台鼠标和键盘输入，请设置为 Seize。")
            approach = load_route(self.resource, "xinghai_crystal_tree")
            loop = load_route(self.resource, "xinghai_crystal_tree_loop")
            if approach is None or loop is None:
                raise NavigationError("星忆晶树收集路线无法读取。")
            with self.rt.input_guard() as inputs:
                teleporter = Teleporter(self.rt, Locator(maps(self.resource)["starsea"]),
                                        self.events.append, inputs)
                teleporter.transport(HUB)
                before = self.observe_counts(teleporter, approach)
                if not sum(before.values()):
                    self.rt.log("星忆晶树没有存储或待收获的结晶，本次跳过。")
                    return True
                self.rt.log("正在前往星忆晶树，先收取已经落地的结晶。")
                self.follow(teleporter, approach)
                state = self.observe_counts(teleporter, approach)
                if sum(state.values()) > sum(before.values()):
                    raise NavigationError("星忆晶树数量前后不一致，未继续收集。")
                if before["ready"] and sum(state.values()) >= sum(before.values()):
                    raise NavigationError("绕树后未确认结晶减少，未重复跑圈或摇铃。")
                while sum(state.values()) > 0:
                    if self.rt.stopped:
                        return False
                    if state["ready"] == 0:
                        self.rt.log("落地结晶已收完，长按摇铃取出下一批（每次最多 40 个）。")
                        if not self.ring(at_hub=True):
                            return False
                        state = self.observe_counts(teleporter, approach, after_bell=state)
                        # A crystal can be picked up immediately at the bell
                        # location. Re-check storage rather than run an empty lap.
                        if state["ready"] == 0:
                            continue
                    before = state
                    self.rt.log("正在绕树收集落地结晶。")
                    self.follow(teleporter, loop)
                    state = self.observe_counts(teleporter, approach)
                    if sum(state.values()) >= sum(before.values()):
                        raise NavigationError("绕树后未确认结晶减少，停止收集以避免重复空跑。")
                self.rt.log("星忆晶树存储与待收获结晶均已取完。")
                return True
        except TaskStopped:
            raise
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, cv2.error) as error:
            self.events.append({"type": "tree_stopped", "reason": str(error)})
            self.rt.log(f"星忆晶树收集未完成：{error}")
            if not self.rt.stopped:
                self.rt.diagnostic("crystal_tree", str(error), details={"events": self.events[-12:]})
            return False
        finally:
            self.report("crystal_tree")
