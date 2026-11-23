"""Collect the crystal tree using its two visible counters and a recorded loop."""
from __future__ import annotations

import re

import cv2

from navigation.models import NavigationError, NavigationMapMismatchError, load_route, maps
from navigation.session import create_navigator
from navigation.teleport import Teleporter
from navigation.vision import Locator
from task_control import TaskStopped, task_time as time
from .settings import ROOT, foreground_inputs
from .starsea import HUB, Starsea
from adventure_report import try_record_adventure_entry


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
            match = re.search(re.escape(caption) + r"(?:[:：]?(\d+)/(\d+)|[:：]?已达上限)$", text)
            if not match:
                return None
            if match.group(1) is None:
                values[key] = capacity
                continue
            count, total = map(int, match.groups())
            if total != capacity or not 0 <= count <= total:
                return None
            values[key] = count
        self.rt.evidence.observe("crystal_tree_counters", readings=readings, values=values)
        return values

    def open_panel(self, teleporter, route):
        # At maximum zoom, Starwish Crystal Tree (~48 px east of Unbound Hub at 720p)
        # is a distinct marker rather than grouped into Unbound Hub; wait for region
        # switch and zoom animation to settle before centering and clicking the tree.
        ring = route.points[1:]
        tree = (sum(p.x for p in ring) / len(ring), sum(p.y for p in ring) / len(ring))
        select_roi = self.rt.ui.roi("AreaBigMapTeleporterSelect")

        def prepare_zoomed_map():
            if not self.rt.main() or not self.rt.ui.open_map(self.events.append):
                return False
            teleporter.deadline = time.monotonic() + 60
            teleporter.region(HUB)
            if not self.rt.pause(.5):
                return False
            teleporter.zoom()
            return bool(self.rt.pause(.35))

        def find_tree_entry(click=False):
            return (self.rt.ui.find(roi=select_roi, text="星忆晶树", exact=True,
                                    color=([0, 0, 220], [180, 15, 255]),
                                    scroll=False, click=click)
                    or self.rt.ui.find(roi=select_roi, text="星忆晶树", exact=True,
                                       scroll=False, click=click))

        def selection_visible():
            return self.panel_visible() or bool(find_tree_entry(click=False))

        def click_tree():
            target = teleporter.center(*tree, allow_visible=True)
            return self.rt.action("Click", target=target)

        if not prepare_zoomed_map():
            raise NavigationError("未能打开星忆晶树地图。")
        if self.rt.ui.enter_page(
                click_tree, selection_visible,
                source="MaaNikki_Navigation_MapFeature",
                recover=prepare_zoomed_map, attempts=3, seconds=4):
            if self.panel_visible():
                return
            if self.rt.ui.enter_page(
                    lambda: bool(find_tree_entry(click=True)),
                    self.panel_visible, source=selection_visible, attempts=2, seconds=5):
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
                # At most 40 can fall per bell. When ready == 0 before ringing,
                # stored crystals must condense (or new ready crystals appear);
                # when ready > 0, ringing shakes canopy crystals to the ground
                # even if stored stays unchanged. Natural regeneration may add a
                # few crystals concurrently while ringing/opening the map.
                moved = after_bell["stored"] - state["stored"]
                if after_bell["ready"] == 0:
                    return (1 <= moved <= 40) or (state["ready"] > 0 and moved >= 0)
                return -20 <= moved <= 40
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
        checkpoint = route.document.get("teleport")
        map_mismatch_retries = 2 if checkpoint else 0
        mismatch_attempts = 0
        while True:
            try:
                create_navigator(self.rt, teleporter, event=self.events.append).follow(route)
                break
            except NavigationMapMismatchError as error:
                if self.rt.stopped or not map_mismatch_retries:
                    raise
                mismatch_attempts += 1
                teleporter.retry_after_map_mismatch(
                    checkpoint,
                    error,
                    retry=mismatch_attempts,
                    max_retries=map_mismatch_retries,
                )
        if self.rt.stopped:
            raise TaskStopped("星忆晶树收集已停止")

    def collect(self):
        initial_total = None
        collected_total = 0
        latest_state = None
        try:
            if not self.rt.background_mode() and not foreground_inputs():
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
                initial_total = sum(before.values())
                latest_state = before
                if not initial_total:
                    self.rt.log("星忆晶树没有存储或待收获的结晶，本次跳过。")
                    return True
                self.rt.log("正在前往星忆晶树，先收取已经落地的结晶。")
                self.follow(teleporter, approach)
                state = self.observe_counts(teleporter, approach)
                latest_state = state
                if sum(state.values()) > sum(before.values()):
                    self.events.append({"type": "tree_refreshed", "before": before, "after": state})
                    self.rt.log(
                        f"星忆晶树前往期间有新结晶凝结（存储 {state['stored']}/120，待收获 {state['ready']}/40），按最新数量继续收集。"
                    )
                else:
                    collected_total += sum(before.values()) - sum(state.values())
                # approach already completed one full ground lap around the tree;
                # any remaining ready crystals are hanging in the high canopy
                # (Fall_Ring = 1) or newly condensed onto the tree.
                need_bell = state["ready"] > 0
                shaken = False
                condensed_retry = False
                skipped_unchanged = False
                growth_resets = 0
                rounds = 0
                while sum(state.values()) > 0:
                    if self.rt.stopped:
                        return False
                    rounds += 1
                    if rounds > 12:
                        skipped_unchanged = True
                        self.events.append({"type": "tree_round_cap", **state})
                        self.rt.log(
                            f"星忆晶树已达到单次收集轮数上限（当前存储 {state['stored']}/120，待收获 {state['ready']}/40），跳过剩余结晶。"
                        )
                        break
                    if state["ready"] == 0 or need_bell:
                        condensed_retry = False
                        if state["ready"] == 0:
                            self.rt.log("落地结晶已收完，长按摇铃取出下一批（每次最多 40 个）。")
                        else:
                            self.rt.log("树冠仍有待收获结晶，长按摇铃震落后继续绕树收取。")
                        if not self.ring(at_hub=True):
                            return False
                        prev_before_bell = state
                        state = self.observe_counts(teleporter, approach, after_bell=state)
                        latest_state = state
                        if sum(state.values()) < sum(prev_before_bell.values()):
                            collected_total += sum(prev_before_bell.values()) - sum(state.values())
                        need_bell = False
                        shaken = True
                        if sum(state.values()) == 0:
                            break
                        # A crystal can be picked up immediately at the bell
                        # location. Re-check storage rather than run an empty lap.
                        if state["ready"] == 0:
                            continue
                    before = state
                    self.rt.log("正在绕树收集落地结晶。")
                    self.follow(teleporter, loop)
                    state = self.observe_counts(teleporter, approach)
                    latest_state = state
                    if sum(state.values()) > sum(before.values()):
                        growth_resets += 1
                        self.events.append({"type": "tree_refreshed", "before": before, "after": state})
                        if growth_resets > 2:
                            raise NavigationError("星忆晶树数量多次异常增长，未继续收集。")
                        self.rt.log(
                            f"绕树期间星忆晶树有新结晶凝结（存储 {state['stored']}/120，待收获 {state['ready']}/40），继续收集。"
                        )
                        condensed_retry = False
                        shaken = False
                        continue
                    if sum(state.values()) < sum(before.values()):
                        collected_total += sum(before.values()) - sum(state.values())
                        condensed_retry = False
                        if state["stored"] < before["stored"]:
                            shaken = False
                        continue
                    if state["ready"] > 0 and not shaken:
                        need_bell = True
                        continue
                    if state["stored"] < before["stored"] and not condensed_retry:
                        condensed_retry = True
                        if state["ready"] > 0:
                            need_bell = True
                        else:
                            self.rt.log("绕树期间有存储结晶新落地，再绕树一圈确认收取。")
                        continue
                    skipped_unchanged = True
                    self.events.append({"type": "tree_skipped_unchanged", **state})
                    self.rt.log(
                        f"星忆晶树绕树后未确认结晶继续减少（当前存储 {state['stored']}/120，待收获 {state['ready']}/40），跳过剩余结晶。"
                    )
                    break
                if not skipped_unchanged:
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
            if initial_total is not None and latest_state is not None:
                collected = max(collected_total, max(0, initial_total - sum(latest_state.values())))
                if collected > 0:
                    try_record_adventure_entry(
                        ROOT,
                        self.rt,
                        category="starsea",
                        task="crystal_tree",
                        title="星忆晶树收取",
                        detail=f"获取星光结晶 ×{collected}",
                        runs=1,
                        items={"星光结晶": collected},
                        status="completed" if sum(latest_state.values()) == 0 else "partial",
                    )
            self.report("crystal_tree")
