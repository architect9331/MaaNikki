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
from task_control import ForegroundChanged, task_time as time

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

# 1280x720 baseline settlement region and verified button fallbacks:
# - '挑战失败' screen shows left '退出' [488,432,56,30] and right '重试' [714,432,56,30]
#   (keyboard 'interact' does not dismiss or retry this screen).
# - Clicking '重试' opens modal '是否消耗100噗灵重试？' with left '取消' [526,439,49,26]
#   and right '好的' [703,439,48,25]; confirming '好的' directly enters the 3-2-1
#   course countdown without returning to NPC dialogue.
# - Consecutive failures are capped so persistent keybinding/framerate issues
#   cannot drain Bling indefinitely.
SETTLEMENT_ROI = [220, 110, 840, 500]
FAIL_EXIT_BOX = [488, 432, 56, 30]
FAIL_RETRY_BOX = [714, 432, 56, 30]
CONFIRM_CANCEL_BOX = [526, 439, 49, 26]
CONFIRM_OK_BOX = [703, 439, 48, 25]
RETRY_START_WAIT_SECONDS = 5.0
MAX_CONSECUTIVE_FAILURES = 6

_CAP_RE = re.compile(r"已达.*上限|今日.*上限|获取上限|达到上限|无法获得更多")
_NO_BLING_RE = re.compile(r"噗灵不足|余额不足")
_FAIL_BANNER_RE = re.compile(r"挑战失败")
_RETRY_CONFIRM_RE = re.compile(r"消耗.*噗灵.*重试|是否.*重试")
_RETRY_BTN_RE = re.compile(r"^重试$")
_EXIT_BTN_RE = re.compile(r"^退出$")
_OK_BTN_RE = re.compile(r"^(好的|确认|确定)$")
_CANCEL_BTN_RE = re.compile(r"^取消$")

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

    def _play_course(self, inputs: ForegroundInput, *, retry_started_at: float | None = None) -> bool:
        if self.stopped:
            return False
        if retry_started_at is None:
            inputs.hold("interact", 0.2)
            inputs.wait(RETRY_START_WAIT_SECONDS, restore=False)
        else:
            remaining = max(
                0.0,
                min(RETRY_START_WAIT_SECONDS, RETRY_START_WAIT_SECONDS - (time.monotonic() - retry_started_at)),
            )
            if remaining > 0:
                inputs.wait(remaining, restore=False)
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

    @staticmethod
    def _box_list(raw) -> list[int] | None:
        if raw is None:
            return None
        try:
            vals = [int(round(float(v))) for v in list(raw)]
        except (TypeError, ValueError):
            return None
        return vals[:4] if len(vals) >= 4 and vals[2] > 0 and vals[3] > 0 else None

    def _inspect_settlement(self, frame=None) -> dict:
        if frame is None:
            frame = self.capture()
        info = {
            "state": "continue",
            "frame": frame,
            "generation": self._focus_generation,
            "has_retry_confirm": False,
            "has_failed": False,
            "retry_box": list(FAIL_RETRY_BOX),
            "exit_box": list(FAIL_EXIT_BOX),
            "ok_box": list(CONFIRM_OK_BOX),
            "cancel_box": list(CONFIRM_CANCEL_BOX),
        }
        if frame is None:
            return info
        result = self.recognize("MaaNikki_Daily_OCR", {"roi": SETTLEMENT_ROI}, image=frame)
        if not result or not result.hit:
            return info
        rows = (
            getattr(result, "filtered_results", None)
            or getattr(result, "all_results", None)
            or ([result.best_result] if getattr(result, "best_result", None) is not None else [])
        )
        texts: list[str] = []
        retry_box = exit_box = ok_box = cancel_box = None
        for row in rows:
            if row is None:
                continue
            score = getattr(row, "score", None)
            if isinstance(score, (int, float)) and score < 0.5:
                continue
            txt = str(getattr(row, "text", "") or "").strip()
            if not txt:
                continue
            texts.append(txt)
            box = self._box_list(getattr(row, "box", None))
            if box is None or not (380 <= box[1] <= 500):
                continue
            if box[0] >= 600:
                if _RETRY_BTN_RE.fullmatch(txt):
                    retry_box = box
                elif _OK_BTN_RE.fullmatch(txt):
                    ok_box = box
            elif box[0] < 640:
                if _EXIT_BTN_RE.fullmatch(txt):
                    exit_box = box
                elif _CANCEL_BTN_RE.fullmatch(txt):
                    cancel_box = box

        if retry_box is not None:
            info["retry_box"] = retry_box
        if exit_box is not None:
            info["exit_box"] = exit_box
        if ok_box is not None:
            info["ok_box"] = ok_box
        if cancel_box is not None:
            info["cancel_box"] = cancel_box

        joined = " ".join(texts)
        has_retry_confirm = bool(
            _RETRY_CONFIRM_RE.search(joined) or (ok_box is not None and cancel_box is not None)
        )
        has_failed = bool(
            _FAIL_BANNER_RE.search(joined) or (retry_box is not None and exit_box is not None)
        )
        info["has_retry_confirm"] = has_retry_confirm
        info["has_failed"] = has_failed
        if _NO_BLING_RE.search(joined):
            info["state"] = "no_bling"
        elif _CAP_RE.search(joined):
            info["state"] = "cap_reached"
        elif has_retry_confirm:
            info["state"] = "retry_confirm"
        elif has_failed:
            info["state"] = "failed"
        return info

    def _click_guarded(self, box: list[int], generation: int) -> bool | None:
        """Click a settlement button without warping the cursor on scene entry.

        Returns True when clicked, False when stopped/failed, or None when foreground changed.
        """
        try:
            with self.ui.preserve_cursor():
                return bool(self.action("Click", target=box, focus_generation=generation))
        except ForegroundChanged:
            return None

    def _retry_from_failure(self, info: dict | None = None) -> tuple[str, float]:
        """Click '重试' on '挑战失败' and '好的' on '是否消耗100噗灵重试？'.

        Returns (status, click_started_at) where status is one of:
        'retrying', 'no_bling', 'cap_reached', 'stopped', 'retry_failed'.
        """
        if info is None:
            info = self._inspect_settlement()

        for _ in range(2):
            if self.stopped:
                return "stopped", 0.0

            # Step 1: Click '重试' on '挑战失败' until '是否消耗100噗灵重试？' opens.
            if info["state"] == "failed":
                for _ in range(3):
                    if self.stopped:
                        return "stopped", 0.0
                    clicked = self._click_guarded(info["retry_box"], info["generation"])
                    if clicked is False:
                        return "stopped", 0.0
                    if clicked is None:
                        info = self._inspect_settlement()
                        if info["state"] != "failed":
                            break
                        continue
                    click_at = time.monotonic() - 0.1
                    poll_deadline = time.monotonic() + 2.5
                    clear_count = 0
                    while not self.stopped and time.monotonic() < poll_deadline:
                        if not self.pause(0.25):
                            return "stopped", 0.0
                        info = self._inspect_settlement()
                        if info["state"] in ("no_bling", "cap_reached", "retry_confirm"):
                            break
                        if info["state"] == "continue":
                            clear_count += 1
                            if clear_count >= 2 and time.monotonic() - click_at >= 0.8:
                                return "retrying", click_at
                        else:
                            clear_count = 0
                            if time.monotonic() - click_at >= 1.2:
                                break
                    if info["state"] != "failed":
                        break
                if info["state"] in ("no_bling", "cap_reached"):
                    return info["state"], 0.0
                if info["state"] == "failed":
                    return "retry_failed", 0.0

            # Step 2: Click '好的' on '是否消耗100噗灵重试？' to enter the 3-2-1 countdown.
            if info["state"] == "retry_confirm":
                for _ in range(3):
                    if self.stopped:
                        return "stopped", 0.0
                    clicked = self._click_guarded(info["ok_box"], info["generation"])
                    if clicked is False:
                        return "stopped", 0.0
                    if clicked is None:
                        info = self._inspect_settlement()
                        if info["state"] == "continue":
                            return "retrying", time.monotonic()
                        if info["state"] != "retry_confirm":
                            break
                        continue
                    # Runtime.action('Click') pauses 0.1s after MouseUp before returning.
                    click_at = time.monotonic() - 0.1
                    poll_deadline = time.monotonic() + 2.2
                    while not self.stopped and time.monotonic() < poll_deadline:
                        if not self.pause(0.3):
                            return "stopped", 0.0
                        info = self._inspect_settlement()
                        if info["state"] in ("no_bling", "cap_reached"):
                            return info["state"], 0.0
                        if info["state"] == "continue":
                            return "retrying", click_at
                        if info["state"] == "failed":
                            break
                        if time.monotonic() - click_at >= 1.2:
                            break
                    if info["state"] == "failed":
                        break
                if info["state"] == "failed":
                    continue
                if info["state"] in ("no_bling", "cap_reached"):
                    return info["state"], 0.0
                return "retry_failed", 0.0

            if info["state"] in ("no_bling", "cap_reached"):
                return info["state"], 0.0
            return ("stopped" if self.stopped else "retrying"), time.monotonic()

        return "retry_failed", 0.0

    def _exit_failure_to_main(self, inputs: ForegroundInput, info: dict | None = None) -> bool:
        """Dismiss retry confirmation / '挑战失败' via '取消' -> '退出' and return to overworld."""
        if self.stopped:
            return False
        if info is None:
            info = self._inspect_settlement()

        if info["has_retry_confirm"]:
            for _ in range(3):
                if self.stopped:
                    return False
                clicked = self._click_guarded(info["cancel_box"], info["generation"])
                if clicked is False:
                    return False
                if clicked is None:
                    info = self._inspect_settlement()
                    if not info["has_retry_confirm"]:
                        break
                    continue
                deadline = time.monotonic() + 2.0
                while not self.stopped and time.monotonic() < deadline:
                    if not self.pause(0.25):
                        return False
                    info = self._inspect_settlement()
                    if not info["has_retry_confirm"]:
                        break
                if not info["has_retry_confirm"]:
                    break

        if info["has_failed"]:
            for _ in range(3):
                if self.stopped:
                    return False
                clicked = self._click_guarded(info["exit_box"], info["generation"])
                if clicked is False:
                    return False
                if clicked is None:
                    info = self._inspect_settlement()
                    if not info["has_failed"]:
                        break
                    continue
                deadline = time.monotonic() + 2.5
                while not self.stopped and time.monotonic() < deadline:
                    if not self.pause(0.3):
                        return False
                    info = self._inspect_settlement()
                    if not info["has_failed"]:
                        break
                if not info["has_failed"]:
                    break

        deadline = time.monotonic() + 10.0
        last_interact = 0.0
        while not self.stopped and time.monotonic() < deadline:
            frame = self.capture()
            if frame is None:
                return False
            if self.hit("MaaNikki_MainDetected", image=frame):
                return self.main()
            now = time.monotonic()
            if now - last_interact >= 1.0:
                inputs.hold("interact", 0.2)
                last_interact = time.monotonic()
            if not self.pause(0.35):
                return False
        return self.main()

    def _handle_failure_retry(
        self,
        inputs: ForegroundInput,
        info: dict,
        target_round: int,
        max_rounds: int,
        consecutive_failures: int,
        *,
        timed_out: bool = False,
    ) -> tuple[str, float | None]:
        """Execute bounded retry on '挑战失败'; returns (action, retry_started_at)."""
        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            self.log(
                f"连续 {consecutive_failures} 次挑战失败，已停止重试并退出小游戏，请检查移动键位或画面帧率。"
            )
            self._exit_failure_to_main(inputs, info)
            return "too_many_failures", None

        reason_label = "挑战超时失败" if timed_out else "挑战失败"
        self.log(
            f"第 {target_round}/{max_rounds} 局{reason_label}，正在消耗 100 噗灵重试"
            f"（连续失败 {consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}）…"
        )
        status, click_at = self._retry_from_failure(info)
        if status == "retrying":
            return "retrying", click_at
        if status == "no_bling":
            self.log("当前噗灵不足以支付 100 噗灵重试费用，正在退出小游戏并停止任务。")
            self._exit_failure_to_main(inputs)
            return "insufficient_bling", None
        if status == "cap_reached":
            self._exit_failure_to_main(inputs)
            return "daily_cap_reached", None
        if status == "stopped" or self.stopped:
            return "stopped", None

        # Fallback: exit to overworld and re-enter through NPC dialogue if near NPC.
        if self._exit_failure_to_main(inputs) and self._run_sequence(inputs, INIT_SEQUENCE):
            return "retrying", None
        self.log("未能点击重试恢复挑战，任务停止。")
        return "retry_failed", None

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

        inputs = ForegroundInput(self.controller, lambda: self.stopped, bindings=self.game_keys)
        previous_inputs = self.navigation_inputs
        completed_rounds = 0
        failed_rounds = 0
        consecutive_failures = 0
        retry_started_at: float | None = None
        stop_reason = "completed"
        started_at = time.monotonic()
        try:
            focus_game(self, inputs)
            self.navigation_inputs = inputs

            init_info = self._inspect_settlement()
            if init_info["state"] == "no_bling":
                stop_reason = "insufficient_bling"
                self.log("当前噗灵不足以支付小游戏入场费，任务停止。")
                self._exit_failure_to_main(inputs, init_info)
                return False
            if init_info["state"] == "cap_reached":
                stop_reason = "daily_cap_reached"
                self.log("检测到今日大世界噗灵获取已达上限，自动结束。")
                self._exit_failure_to_main(inputs, init_info)
                return not self.stopped
            if init_info["state"] in ("failed", "retry_confirm"):
                if not navigate:
                    self.log("检测到当前处于挑战失败界面，直接消耗 100 噗灵重试开始挑战…")
                    status, click_at = self._retry_from_failure(init_info)
                    if status == "retrying":
                        retry_started_at = click_at
                    elif status == "no_bling":
                        stop_reason = "insufficient_bling"
                        self.log("当前噗灵不足以支付 100 噗灵重试费用，正在退出小游戏并停止任务。")
                        self._exit_failure_to_main(inputs)
                        return False
                    elif status == "cap_reached":
                        stop_reason = "daily_cap_reached"
                        self.log("检测到今日大世界噗灵获取已达上限，自动结束。")
                        self._exit_failure_to_main(inputs)
                        return not self.stopped
                    elif status == "stopped" or self.stopped:
                        stop_reason = "stopped"
                        return False
                    elif not self._exit_failure_to_main(inputs):
                        stop_reason = "retry_failed"
                        self.log("未能从挑战失败界面恢复，任务停止。")
                        return False
                else:
                    self.log("检测到当前处于挑战失败界面，正在退出以确认 NPC 位置…")
                    if not self._exit_failure_to_main(inputs, init_info):
                        stop_reason = "exit_failed"
                        self.log("未能退出挑战失败界面，任务停止。")
                        return False

            if navigate and retry_started_at is None:
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
                    focus_game(self, inputs)

            self.log(f"开始「穿梭大冒险」自动刷噗灵（目标 {max_rounds} 局，预计耗时约 {int(max_rounds * 31.2 // 60)} 分钟）…")
            if retry_started_at is None:
                if not self._run_sequence(inputs, INIT_SEQUENCE):
                    return False
                post_init = self._inspect_settlement()
                if post_init["state"] == "no_bling":
                    stop_reason = "insufficient_bling"
                    self.log("当前噗灵不足以支付小游戏入场费，任务停止。")
                    self._exit_failure_to_main(inputs, post_init)
                    return False
                if post_init["state"] == "cap_reached":
                    stop_reason = "daily_cap_reached"
                    self.log("检测到今日大世界噗灵获取已达上限，自动结束。")
                    return not self.stopped

            while completed_rounds < max_rounds:
                if self.stopped:
                    stop_reason = "stopped"
                    break
                target_round = completed_rounds + 1
                round_start = time.monotonic()
                if retry_started_at is None and (
                    target_round == 1 or target_round % 5 == 0 or target_round == max_rounds
                ):
                    est_bling = max(0, completed_rounds * 600 - failed_rounds * 100)
                    self.log(f"正在进行「穿梭大冒险」第 {target_round}/{max_rounds} 局（已累计净得约 {est_bling:,} 噗灵）…")

                current_retry_start = retry_started_at
                retry_started_at = None
                if not self._play_course(inputs, retry_started_at=current_retry_start):
                    stop_reason = "stopped"
                    break
                inputs.wait(5.0, restore=False)

                info = self._inspect_settlement()
                if info["state"] in ("failed", "retry_confirm"):
                    failed_rounds += 1
                    consecutive_failures += 1
                    outcome, retry_started_at = self._handle_failure_retry(
                        inputs, info, target_round, max_rounds, consecutive_failures
                    )
                    if outcome == "retrying":
                        continue
                    stop_reason = outcome
                    if outcome == "daily_cap_reached":
                        self.log(f"检测到今日大世界噗灵获取已达上限（共完成 {completed_rounds} 局），自动结束。")
                        break
                    return False

                if info["state"] == "cap_reached":
                    completed_rounds += 1
                    consecutive_failures = 0
                    stop_reason = "daily_cap_reached"
                    self.log(f"检测到今日大世界噗灵获取已达上限（共完成 {completed_rounds} 局），自动结束。")
                    break
                if info["state"] == "no_bling":
                    stop_reason = "insufficient_bling"
                    self.log("当前噗灵不足以支付小游戏入场费，任务停止。")
                    self._exit_failure_to_main(inputs, info)
                    return False

                completed_rounds += 1
                consecutive_failures = 0
                if completed_rounds < max_rounds:
                    if not self._run_sequence(inputs, REPLAY_SEQUENCE):
                        stop_reason = "stopped"
                        break
                    post_info = self._inspect_settlement()
                    if post_info["state"] == "cap_reached":
                        stop_reason = "daily_cap_reached"
                        self.log(f"检测到今日大世界噗灵获取已达上限（共完成 {completed_rounds} 局），自动结束。")
                        break
                    if post_info["state"] == "no_bling":
                        stop_reason = "insufficient_bling"
                        self.log("当前噗灵不足以支付小游戏入场费，任务停止。")
                        self._exit_failure_to_main(inputs, post_info)
                        return False
                    if post_info["state"] in ("failed", "retry_confirm"):
                        completed_rounds = max(0, completed_rounds - 1)
                        failed_rounds += 1
                        consecutive_failures += 1
                        outcome, retry_started_at = self._handle_failure_retry(
                            inputs,
                            post_info,
                            completed_rounds + 1,
                            max_rounds,
                            consecutive_failures,
                            timed_out=True,
                        )
                        if outcome == "retrying":
                            continue
                        stop_reason = outcome
                        if outcome == "daily_cap_reached":
                            self.log(f"检测到今日大世界噗灵获取已达上限（共完成 {completed_rounds} 局），自动结束。")
                            break
                        return False
                    if post_info["frame"] is not None and self.hit(
                        "MaaNikki_MainDetected", image=post_info["frame"]
                    ):
                        self.log("检测到返回大世界，正在重新与 NPC 对话继续挑战…")
                        if not self._run_sequence(inputs, INIT_SEQUENCE):
                            stop_reason = "stopped"
                            break
                elapsed = time.monotonic() - round_start
                self.log(f"第 {completed_rounds}/{max_rounds} 局完成，耗时 {elapsed:.1f} 秒。", detail=True)

            if not self.stopped:
                end_info = self._inspect_settlement()
                if end_info["state"] in ("failed", "retry_confirm"):
                    self._exit_failure_to_main(inputs, end_info)
                elif end_info["frame"] is None or not self.hit(
                    "MaaNikki_MainDetected", image=end_info["frame"]
                ):
                    self.main()
            total_min = (time.monotonic() - started_at) / 60.0
            if failed_rounds > 0:
                self.log(
                    f"「穿梭大冒险」刷噗灵结束：共完成 {completed_rounds} 局，中途失败重试 {failed_rounds} 次"
                    f"（耗时 {total_min:.1f} 分钟）。"
                )
            else:
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
                    "failed_rounds": failed_rounds,
                    "target_rounds": max_rounds,
                    "navigate": navigate,
                    "stop_reason": stop_reason,
                })
            except Exception:
                pass
