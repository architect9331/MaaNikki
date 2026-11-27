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
    SIDEBAR_RECOVERY_ROI = [1030, 185, 220, 42]

    def panel_visible(self, frame=None):
        return "星忆晶树" in self.rt.text(self.TITLE_ROI, image=frame).replace(" ", "")

    def is_recovering_without_counters(self, frame=None):
        if frame is None or not self.panel_visible(frame):
            return False
        sidebar = self.rt.text(self.SIDEBAR_RECOVERY_ROI, image=frame).replace(" ", "")
        if "星忆晶树寻回中" not in sidebar:
            return False
        for _key, caption, _capacity, roi in self.COUNTERS:
            raw = re.sub(r"\s+", "", self.rt.text(roi, image=frame)).replace("／", "/")
            if caption in raw or re.search(r"\d+/\d+", raw):
                return False
        return True

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

    def observe_counts(self, teleporter, route):
        self.open_panel(teleporter, route)
        state = None

        def ready():
            nonlocal state
            state = self.read_panel()
            return state is not None

        if not self.rt.ui.wait_page(ready, seconds=8):
            frame = self.rt.frame()
            if self.is_recovering_without_counters(frame):
                self.events.append({"type": "tree_recovering_no_counters"})
                self.rt.log("星忆晶树当前处于「寻回中」且未显示可收取计数，本次跳过。")
                if not self.rt.main():
                    raise NavigationError("星忆晶树详情未能关闭，未继续移动。")
                teleporter.locator.reset()
                return {"stored": 0, "ready": 0}
            raise NavigationError("无法读取星忆晶树存储量和待收获量，本次停止。")
        self.events.append({"type": "tree_counts", **state})
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

    # Stored crystals trickle onto the canopy at ~1 every 10s (~7 min per 40-crystal batch).
    CRYSTAL_CONDENSE_SECONDS = 10.5
    DRAIN_MAX_WAIT_SECONDS = 420

    def _wait_for_condense(self, seconds: int) -> bool:
        """Wait in short slices so Stop or focus pause interrupts promptly."""
        remaining = float(seconds)
        while remaining > 0:
            if self.rt.stopped:
                return False
            step = min(10.0, remaining)
            if not self.rt.pause(step):
                return False
            remaining -= step
        return not self.rt.stopped

    def _shake_and_sweep(self, teleporter, loop) -> bool:
        """Ring the bell once, let shaken crystals hit the ground, then run one pickup lap."""
        if not self.ring(at_hub=True):
            return False
        if not self.rt.pause(1.0):
            return False
        self.rt.log("正在绕树收集落地结晶。")
        self.follow(teleporter, loop)
        return True

    def collect(self, *, drain_storage: bool = False):
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

                if not drain_storage:
                    # Default quick mode: do not linger waiting ~7 minutes for stored
                    # crystals to trickle onto the canopy. Shake once, sweep the ground
                    # lap immediately (since map "待收获量" drops to 0 as soon as crystals
                    # leave the tree), and leave regardless of a few newly condensed ones.
                    if before["ready"] == 0:
                        self.rt.log(
                            f"星忆晶树当前树上暂无待收获结晶（存储 {before['stored']}/120 正在缓慢凝结），本次先跳过；如需持续等待清空可开启「清空星忆晶树存储」。"
                        )
                        return True
                    self.rt.log("正在前往星忆晶树，摇铃一次并绕树收取当前批次结晶。")
                    self.follow(teleporter, approach)
                    if not self._shake_and_sweep(teleporter, loop):
                        return False
                    collected_total = before["ready"]
                    latest_state = {"stored": before["stored"], "ready": 0}
                    if before["stored"] > 0:
                        self.rt.log(
                            f"星忆晶树本批结晶已收取完成（另有存储 {before['stored']}/120 将缓慢凝结，如需清空可开启「清空星忆晶树存储」）。"
                        )
                    else:
                        self.rt.log("星忆晶树本批结晶已收取完成。")
                    return True

                # Opt-in full drain mode: collect the initial batch, then wait for stored
                # crystals to refill the canopy (~7 min per 40) before each subsequent shake.
                self.rt.log("正在前往星忆晶树（清空存储模式），先收取当前批次结晶。")
                self.follow(teleporter, approach)
                if before["ready"] > 0:
                    self.rt.log("长按摇铃震落当前批次结晶并绕树收取。")
                    if not self._shake_and_sweep(teleporter, loop):
                        return False
                state = self.observe_counts(teleporter, approach)
                latest_state = state
                if sum(state.values()) > sum(before.values()):
                    self.events.append({"type": "tree_refreshed", "before": before, "after": state})
                    self.rt.log(
                        f"星忆晶树前往期间有新结晶凝结（存储 {state['stored']}/120，待收获 {state['ready']}/40），按最新数量继续收集。"
                    )
                else:
                    collected_total += sum(before.values()) - sum(state.values())

                condensed_retry = False
                skipped_unchanged = False
                growth_resets = 0
                rounds = 0
                while sum(state.values()) > 0:
                    if self.rt.stopped:
                        return False
                    rounds += 1
                    if rounds > 6:
                        skipped_unchanged = True
                        self.events.append({"type": "tree_round_cap", **state})
                        self.rt.log(
                            f"星忆晶树已达到单次清空轮数上限（当前存储 {state['stored']}/120，待收获 {state['ready']}/40），跳过剩余结晶。"
                        )
                        break
                    before_round = state
                    if state["stored"] > 0:
                        target_ready = min(40, state["stored"] + state["ready"])
                        need = max(0, target_ready - state["ready"])
                        if need > 0:
                            wait_s = min(
                                self.DRAIN_MAX_WAIT_SECONDS,
                                max(15, round(need * self.CRYSTAL_CONDENSE_SECONDS)),
                            )
                            self.events.append({"type": "tree_drain_wait", "wait_s": wait_s, **state})
                            self.rt.log(
                                f"星忆晶树仍有存储 {state['stored']}/120（当前待收获 {state['ready']}/40），在树下等待约 {wait_s} 秒待凝结后再摇铃收取。"
                            )
                            if not self._wait_for_condense(wait_s):
                                return False
                    self.rt.log("长按摇铃震落当前批次结晶并绕树收取。")
                    if not self._shake_and_sweep(teleporter, loop):
                        return False
                    state = self.observe_counts(teleporter, approach)
                    latest_state = state
                    if sum(state.values()) > sum(before_round.values()):
                        growth_resets += 1
                        self.events.append({"type": "tree_refreshed", "before": before_round, "after": state})
                        if growth_resets > 2:
                            raise NavigationError("星忆晶树数量多次异常增长，未继续收集。")
                        self.rt.log(
                            f"绕树期间星忆晶树有新结晶凝结（存储 {state['stored']}/120，待收获 {state['ready']}/40），继续收集。"
                        )
                        condensed_retry = False
                        continue
                    if sum(state.values()) < sum(before_round.values()):
                        collected_total += sum(before_round.values()) - sum(state.values())
                        condensed_retry = False
                        continue
                    if state["stored"] < before_round["stored"] and not condensed_retry:
                        condensed_retry = True
                        self.rt.log("绕树期间有存储结晶新凝结到树冠，再摇铃绕树一圈确认收取。")
                        continue
                    skipped_unchanged = True
                    self.events.append({"type": "tree_skipped_unchanged", **state})
                    self.rt.log(
                        f"星忆晶树摇铃绕树后未确认结晶继续减少（当前存储 {state['stored']}/120，待收获 {state['ready']}/40），跳过剩余结晶。"
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
