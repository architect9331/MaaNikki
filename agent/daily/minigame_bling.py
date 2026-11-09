"""Automated 'Shuttle Adventure' (穿梭大冒险) mini-game loop for daily 160k Bling farming.

Uses the terrain-aware route from Wishfield Street (心愿花街) to reach the NPC at
(10153.75, 10053.30) when not already nearby, then executes the Whimcycle hoop
course and settlement replay loop while monitoring for the daily Bling cap.
"""
from __future__ import annotations

from datetime import datetime
import math
import re

import cv2
from file_io import write_json
from task_control import task_time as time

from .route_test import RouteTest
from .runtime import RESOURCE, Runtime
from .settings import ROOT, foreground_inputs as inputs_allowed
from .startup import focus_game
from navigation.controller import ForegroundInput
from navigation.models import GameWindowClosed, NavigationError, maps
from navigation.vision import Locator

NPC_X = 10153.75
NPC_Y = 10053.30
NEAR_NPC_RADIUS_PX = 10.0
DEFAULT_ROUTE = "minigame_shuttle_adventure"

_CAP_RE = re.compile(r"已达.*上限|今日.*上限|获取上限|达到上限|无法获得更多")
_NO_BLING_RE = re.compile(r"噗灵不足|余额不足")

INIT_SEQUENCE: tuple[tuple[str, float] | float, ...] = (
    ("interact", 0.2),
    2.4,
    ("interact", 0.2),
    0.5,
    ("backward", 0.2),
    0.3,
    ("interact", 0.2),
    1.5,
    ("interact", 0.2),
    0.5,
    ("interact", 0.2),
    0.5,
    ("interact", 0.2),
    1.0,
)

REPLAY_SEQUENCE: tuple[tuple[str, float] | float, ...] = (
    ("interact", 0.2),
    1.8,
    ("interact", 0.2),
    0.7,
    ("interact", 0.2),
    0.7,
    ("interact", 0.2),
    1.5,
    ("interact", 0.2),
    0.7,
    ("interact", 0.2),
    0.7,
    ("interact", 0.2),
    1.2,
)


class MinigameBlingRun(Runtime):
    """Executes the Shuttle Adventure bicycle mini-game loop until target rounds or daily cap."""

    def _already_near_npc(self) -> bool:
        try:
            spec = maps(RESOURCE).get("miraland")
            if spec is None or not self.hit("MaaNikki_MainDetected"):
                return False
            frame = self.capture()
            if frame is None:
                return False
            locator = Locator(spec)
            pose = locator.locate(frame, hint=(NPC_X, NPC_Y), search_radius=45.0)
            if pose and pose.position and pose.position_score >= 0.52:
                px, py = pose.position
                if math.hypot(px - NPC_X, py - NPC_Y) <= NEAR_NPC_RADIUS_PX:
                    return True
        except Exception:
            return False
        return False

    def _run_sequence(self, inputs: ForegroundInput, seq: tuple[tuple[str, float] | float, ...]) -> bool:
        for step in seq:
            if self.stopped:
                return False
            if isinstance(step, (int, float)):
                inputs.wait(float(step), restore=False)
            else:
                key_name, dur = step
                inputs.hold(key_name, float(dur))
        return not self.stopped

    def _play_course(self, inputs: ForegroundInput) -> bool:
        if self.stopped:
            return False
        inputs.hold("interact", 0.2)
        inputs.wait(5.0, restore=False)
        fwd = self.game_keys.get("forward")
        right = self.game_keys.get("right")
        try:
            inputs._down(fwd)
            inputs.wait(5.0)
            for hold_d, wait_after in ((0.7, 0.65), (1.1, 1.1), (0.5, 0.3), (0.9, 2.0)):
                if self.stopped:
                    return False
                inputs._down(right)
                try:
                    inputs.wait(hold_d)
                finally:
                    inputs._up(right)
                inputs.wait(wait_after)
        finally:
            inputs._up(right)
            inputs._up(fwd)
            inputs.release()
        return not self.stopped

    def _check_settlement_state(self) -> str:
        frame = self.capture()
        if frame is None:
            return "continue"
        text = self.text([220, 110, 840, 500], image=frame)
        if text:
            if _CAP_RE.search(text):
                return "cap_reached"
            if _NO_BLING_RE.search(text):
                return "no_bling"
        return "continue"

    def run(self, settings: dict) -> bool:
        if not inputs_allowed():
            self.log("小游戏刷噗灵需要当前连接使用前台鼠标和键盘（Seize）。")
            return False

        try:
            max_rounds = max(1, min(500, int(settings.get("rounds", 267))))
        except (TypeError, ValueError):
            max_rounds = 267
        navigate = bool(settings.get("navigate", True))
        route_name = str(settings.get("route") or DEFAULT_ROUTE).strip()

        if navigate:
            if not self.main():
                self.log("未能确认大世界主界面，无法前往小游戏 NPC。")
                return False
            if self._already_near_npc():
                self.log("检测到已位于「穿梭大冒险」NPC 附近，直接开始挑战。")
            else:
                self.log("正在传送至「心愿花街」并前往「穿梭大冒险」小游戏 NPC…")
                nav = RouteTest(self.context)
                nav.dev_only = False
                nav.label = "前往穿梭大冒险"
                if not nav.run_test({"route": route_name, "mode": "movement"}):
                    self.log("前往「穿梭大冒险」NPC 路线未完成，任务停止。")
                    return False

        inputs = ForegroundInput(self.controller, lambda: self.stopped, bindings=self.game_keys)
        previous_inputs = self.navigation_inputs
        completed_rounds = 0
        stop_reason = "completed"
        started_at = time.monotonic()
        try:
            focus_game(self, inputs)
            self.navigation_inputs = inputs
            self.log(f"开始「穿梭大冒险」自动刷噗灵（目标 {max_rounds} 局，预计耗时约 {int(max_rounds * 31.2 // 60)} 分钟）…")
            if not self._run_sequence(inputs, INIT_SEQUENCE):
                return False

            for round_idx in range(1, max_rounds + 1):
                if self.stopped:
                    stop_reason = "stopped"
                    break
                round_start = time.monotonic()
                if round_idx == 1 or round_idx % 5 == 0 or round_idx == max_rounds:
                    est_bling = (round_idx - 1) * 600
                    self.log(f"正在进行「穿梭大冒险」第 {round_idx}/{max_rounds} 局（已累计净得约 {est_bling:,} 噗灵）…")
                if not self._play_course(inputs):
                    stop_reason = "stopped"
                    break
                inputs.wait(5.0, restore=False)
                completed_rounds = round_idx
                state = self._check_settlement_state()
                if state == "cap_reached":
                    stop_reason = "daily_cap_reached"
                    self.log(f"检测到今日大世界噗灵获取已达上限（共完成 {completed_rounds} 局），自动结束。")
                    break
                if state == "no_bling":
                    stop_reason = "insufficient_bling"
                    self.log("当前噗灵不足以支付小游戏入场费，任务停止。")
                    return False
                if round_idx < max_rounds:
                    if not self._run_sequence(inputs, REPLAY_SEQUENCE):
                        stop_reason = "stopped"
                        break
                    if self.hit("MaaNikki_MainDetected"):
                        self.log("检测到返回大世界，正在重新与 NPC 对话继续挑战…")
                        if not self._run_sequence(inputs, INIT_SEQUENCE):
                            stop_reason = "stopped"
                            break
                elapsed = time.monotonic() - round_start
                self.log(f"第 {round_idx}/{max_rounds} 局完成，耗时 {elapsed:.1f} 秒。", detail=True)

            if not self.stopped and not self.hit("MaaNikki_MainDetected"):
                self.main()
            total_min = (time.monotonic() - started_at) / 60.0
            self.log(f"「穿梭大冒险」刷噗灵结束：共完成 {completed_rounds} 局（耗时 {total_min:.1f} 分钟）。")
            return not self.stopped
        except (NavigationError, GameWindowClosed, OSError, RuntimeError, ValueError, cv2.error) as error:
            stop_reason = f"error:{error}"
            self.log(f"小游戏刷噗灵停止：{error}")
            return False
        finally:
            self.navigation_inputs = previous_inputs
            try:
                inputs.release()
            except Exception:
                pass
            try:
                folder = ROOT / "logs/minigame_bling"
                folder.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                write_json(folder / f"{stamp}.json", {
                    "completed_rounds": completed_rounds,
                    "target_rounds": max_rounds,
                    "navigate": navigate,
                    "stop_reason": stop_reason,
                })
            except Exception:
                pass
