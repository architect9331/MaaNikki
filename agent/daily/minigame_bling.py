"""Automated 'Shuttle Adventure' (穿梭大冒险) mini-game loop for daily 160k Bling farming.

Uses the terrain-aware route from Wishfield Street (心愿花街) to reach the NPC at
(10152.80, 10054.80) when not already nearby, then executes the Whimcycle hoop
course and settlement replay loop while monitoring for the daily Bling cap.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from datetime import datetime
import math
import re

import cv2
from file_io import write_json
from task_control import ForegroundChanged, active_session, task_time as time

from .route_test import RouteTest
from .runtime import RESOURCE, Runtime
from .settings import ROOT, foreground_inputs as inputs_allowed
from .startup import focus_game
from navigation.controller import BackgroundInput, ForegroundInput
from navigation.models import GameWindowClosed, NavigationError, maps
from navigation.vision import Locator

NPC_X = 10152.80
NPC_Y = 10054.80
NEAR_NPC_RADIUS_PX = 2.8
DEFAULT_ROUTE = "minigame_shuttle_adventure"
DIALOGUE_MODE_ROI = [65, 12, 140, 60]
DIALOGUE_SKIP_ROI = [1045, 635, 235, 85]
DIALOGUE_CHOICE_ROI = [760, 200, 500, 440]

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
_DIALOGUE_EXIT_RE = re.compile(r"不用了|再见|离开|下次再说|取消|稍候|改天|算了|结束")
_DIALOGUE_OPTION_RE = re.compile(r"开始|挑战|规则|介绍|说明|准备|继续|重新")
_DIALOGUE_MODE_RE = re.compile(r"手动|自动")
_DIALOGUE_SKIP_RE = re.compile(r"跳过")

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

    def _already_near_npc(self, max_radius_px: float = NEAR_NPC_RADIUS_PX) -> bool:
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
                if math.hypot(px - NPC_X, py - NPC_Y) <= max_radius_px:
                    return True
        except Exception:
            return False
        return False

    def _can_interact_with_npc(self) -> bool:
        frame = self.capture()
        if frame is None or not self.hit("MaaNikki_MainDetected", image=frame):
            return False
        pickup_roi = self.ui.roi("AreaPickup")
        return bool(
            self.ui.asset(
                "IconTalkFeature",
                image=frame,
                roi=pickup_roi,
                scale=2 / 3,
                gray=(210, 255),
                threshold=0.75,
            )
            or self.ui.asset(
                "IconPickupFeature",
                image=frame,
                roi=pickup_roi,
                scale=2 / 3,
                gray=(210, 255),
                threshold=0.75,
            )
        )

    def _dismiss_npc_dialogue(self, inputs: ForegroundInput) -> bool:
        """Exit active NPC dialogue cleanly without selecting '开始游戏'."""
        deadline = time.monotonic() + 10.0
        progressed = False
        unmatched_choice_frames = 0
        mode_only_frames = 0
        choice_clicks = 0
        while not self.stopped and time.monotonic() < deadline:
            frame = self.capture()
            if frame is None:
                return False
            generation = self._focus_generation
            if self.hit("MaaNikki_MainDetected", image=frame):
                return True

            # Prefer clicking an explicit exit choice when dialogue options are open;
            # never press 'interact' while choice rows are present.
            choices = self.recognize("MaaNikki_Daily_OCR", {"roi": DIALOGUE_CHOICE_ROI}, image=frame)
            choice_rows: list[tuple[str, list[int]]] = []
            exit_box = None
            if choices and choices.hit:
                rows = (
                    getattr(choices, "filtered_results", None)
                    or getattr(choices, "all_results", None)
                    or ([choices.best_result] if getattr(choices, "best_result", None) is not None else [])
                )
                for row in rows:
                    if row is None:
                        continue
                    score = getattr(row, "score", None)
                    if isinstance(score, (int, float)) and score < 0.45:
                        continue
                    txt = str(getattr(row, "text", "") or "").strip()
                    box = self._box_list(getattr(row, "box", None))
                    if not txt or box is None:
                        continue
                    choice_rows.append((txt, box))
                    if exit_box is None and _DIALOGUE_EXIT_RE.search(txt):
                        exit_box = box
            choice_menu_visible = bool(
                exit_box is not None
                or len(choice_rows) >= 2
                or any(_DIALOGUE_OPTION_RE.search(txt) for txt, _ in choice_rows)
            )
            if exit_box is not None:
                progressed = True
                unmatched_choice_frames = 0
                mode_only_frames = 0
                clicked = self._click_guarded(exit_box, generation, cursor_pos=choice_clicks > 0)
                if clicked is False:
                    return False
                if clicked is True:
                    choice_clicks += 1
                if not self.pause(0.6):
                    return False
                continue
            if choice_menu_visible:
                progressed = True
                mode_only_frames = 0
                if unmatched_choice_frames == 0:
                    unmatched_choice_frames += 1
                    if not self.pause(0.35):
                        return False
                    continue
                unmatched_choice_frames = 0
                bottom_box = max((box for _, box in choice_rows), key=lambda b: b[1])
                clicked = self._click_guarded(bottom_box, generation, cursor_pos=choice_clicks > 0)
                if clicked is False:
                    return False
                if clicked is True:
                    choice_clicks += 1
                if not self.pause(0.6):
                    return False
                continue
            unmatched_choice_frames = 0
            choice_clicks = 0

            has_skip = bool(self.text(DIALOGUE_SKIP_ROI, _DIALOGUE_SKIP_RE.pattern, image=frame))
            has_mode = bool(not has_skip and self.text(DIALOGUE_MODE_ROI, _DIALOGUE_MODE_RE.pattern, image=frame))
            if has_skip or has_mode:
                progressed = True
                if has_mode and mode_only_frames == 0:
                    # Wait one frame when '跳过' is absent so newly fading-in
                    # choice rows can be read before any 'interact' press.
                    mode_only_frames += 1
                    if not self.pause(0.35):
                        return False
                    continue
                mode_only_frames = 0
                inputs.hold("interact", 0.2, restore=False)
                if not self.pause(0.55):
                    return False
                continue
            mode_only_frames = 0

            if not progressed:
                break
            if not self.pause(0.35):
                return False
        if self.stopped:
            return False
        if progressed:
            return bool(self.hit("MaaNikki_MainDetected"))
        return True

    def _run_sequence(self, inputs: ForegroundInput, seq: tuple[tuple[str, float] | float, ...]) -> bool:
        for step in seq:
            if self.stopped:
                return False
            if isinstance(step, (int, float)):
                inputs.wait(float(step), restore=False)
            else:
                key_name, dur = step
                inputs.hold(key_name, float(dur), restore=False)
        return not self.stopped

    def _play_course(self, inputs: ForegroundInput, *, retry_started_at: float | None = None) -> bool:
        if self.stopped:
            return False
        if retry_started_at is None:
            inputs.hold("interact", 0.2, restore=False)
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
            inputs.wait(4.5)
            for hold_d, wait_after in ((1.2, 0.65), (1.1, 1.1), (0.5, 3.2)):
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

    def _click_guarded(self, box: list[int], generation: int, *, cursor_pos: bool = False) -> bool | None:
        """Click a settlement button without warping the cursor on scene entry.

        Returns True when clicked, False when stopped/failed, or None when foreground changed.
        """
        try:
            with self.ui.preserve_cursor():
                return bool(
                    self.action(
                        "Click",
                        target=box,
                        focus_generation=generation,
                        window_cursor_pos=cursor_pos,
                    )
                )
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
                sent_clicks = 0
                for _ in range(3):
                    if self.stopped:
                        return "stopped", 0.0
                    clicked = self._click_guarded(
                        info["retry_box"],
                        info["generation"],
                        cursor_pos=sent_clicks > 0,
                    )
                    if clicked is False:
                        return "stopped", 0.0
                    if clicked is None:
                        info = self._inspect_settlement()
                        if info["state"] != "failed":
                            break
                        continue
                    sent_clicks += 1
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
                sent_clicks = 0
                for _ in range(3):
                    if self.stopped:
                        return "stopped", 0.0
                    clicked = self._click_guarded(
                        info["ok_box"],
                        info["generation"],
                        cursor_pos=sent_clicks > 0,
                    )
                    if clicked is False:
                        return "stopped", 0.0
                    if clicked is None:
                        info = self._inspect_settlement()
                        if info["state"] == "continue":
                            return "retrying", time.monotonic()
                        if info["state"] != "retry_confirm":
                            break
                        continue
                    sent_clicks += 1
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
            sent_clicks = 0
            for _ in range(3):
                if self.stopped:
                    return False
                clicked = self._click_guarded(
                    info["cancel_box"],
                    info["generation"],
                    cursor_pos=sent_clicks > 0,
                )
                if clicked is False:
                    return False
                if clicked is None:
                    info = self._inspect_settlement()
                    if not info["has_retry_confirm"]:
                        break
                    continue
                sent_clicks += 1
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
            sent_clicks = 0
            for _ in range(3):
                if self.stopped:
                    return False
                clicked = self._click_guarded(
                    info["exit_box"],
                    info["generation"],
                    cursor_pos=sent_clicks > 0,
                )
                if clicked is False:
                    return False
                if clicked is None:
                    info = self._inspect_settlement()
                    if not info["has_failed"]:
                        break
                    continue
                sent_clicks += 1
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
                inputs.hold("interact", 0.2, restore=False)
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

    @contextmanager
    def _phase_inputs(self, inputs: ForegroundInput, *, background: bool):
        previous_inputs = self.navigation_inputs
        session = active_session() if background else None
        previous_adapter = session.adapter if session is not None else None
        observation = session.background_observation() if session is not None else nullcontext()
        with observation:
            self.navigation_inputs = inputs
            if session is not None:
                session.adapter = inputs
            try:
                yield
            finally:
                try:
                    inputs.release()
                except Exception:
                    pass
                if session is not None:
                    session.adapter = previous_adapter
                self.navigation_inputs = previous_inputs

    def _is_main_detected(self, info: dict) -> bool:
        if info["frame"] is not None and self.hit("MaaNikki_MainDetected", image=info["frame"]):
            return True
        if self._focus_generation != info["generation"]:
            return bool(self.hit("MaaNikki_MainDetected"))
        return False

    def _prepare_for_challenge(self, inputs: ForegroundInput, *, navigate: bool, stats: dict) -> dict:
        init_info = self._inspect_settlement()
        if init_info["state"] == "no_bling":
            stats["stop_reason"] = "insufficient_bling"
            self.log("当前噗灵不足以支付小游戏入场费，任务停止。")
            self._exit_failure_to_main(inputs, init_info)
            return {"action": "stop", "success": False}
        if init_info["state"] == "cap_reached":
            stats["stop_reason"] = "daily_cap_reached"
            self.log("检测到今日大世界噗灵获取已达上限，自动结束。")
            self._exit_failure_to_main(inputs, init_info)
            return {"action": "stop", "success": not self.stopped}

        retry_started_at: float | None = None
        if init_info["state"] in ("failed", "retry_confirm"):
            if not navigate:
                self.log("检测到当前处于挑战失败界面，直接消耗 100 噗灵重试开始挑战…")
                status, click_at = self._retry_from_failure(init_info)
                if status == "retrying":
                    retry_started_at = click_at
                elif status == "no_bling":
                    stats["stop_reason"] = "insufficient_bling"
                    self.log("当前噗灵不足以支付 100 噗灵重试费用，正在退出小游戏并停止任务。")
                    self._exit_failure_to_main(inputs)
                    return {"action": "stop", "success": False}
                elif status == "cap_reached":
                    stats["stop_reason"] = "daily_cap_reached"
                    self.log("检测到今日大世界噗灵获取已达上限，自动结束。")
                    self._exit_failure_to_main(inputs)
                    return {"action": "stop", "success": not self.stopped}
                elif status == "stopped" or self.stopped:
                    stats["stop_reason"] = "stopped"
                    return {"action": "stop", "success": False}
                elif not self._exit_failure_to_main(inputs):
                    stats["stop_reason"] = "retry_failed"
                    self.log("未能从挑战失败界面恢复，任务停止。")
                    return {"action": "stop", "success": False}
            else:
                self.log("检测到当前处于挑战失败界面，正在退出以确认 NPC 位置…")
                if not self._exit_failure_to_main(inputs, init_info):
                    stats["stop_reason"] = "exit_failed"
                    self.log("未能退出挑战失败界面，任务停止。")
                    return {"action": "stop", "success": False}

        if retry_started_at is None:
            if not self._dismiss_npc_dialogue(inputs):
                stats["stop_reason"] = "dialogue_exit_failed"
                self.log("未能退出当前对话界面，无法开始小游戏。")
                return {"action": "stop", "success": False}

        need_route = False
        if navigate and retry_started_at is None:
            if not self.main():
                if not (self._dismiss_npc_dialogue(inputs) and self.main()):
                    stats["stop_reason"] = "main_unavailable"
                    self.log("未能确认大世界主界面，无法前往小游戏 NPC。")
                    return {"action": "stop", "success": False}
            if self._already_near_npc():
                self.log("检测到已位于「穿梭大冒险」NPC 附近，直接开始挑战。")
            else:
                need_route = True

        return {
            "action": "continue",
            "retry_started_at": retry_started_at,
            "need_route": need_route,
        }

    def _run_route(self, route_name: str, *, reset_started: bool = False) -> bool:
        bg = self.background_mode()
        if not bg and not inputs_allowed():
            self.log("前往「穿梭大冒险」NPC 路线需要当前连接使用前台鼠标和键盘（Seize）。")
            return False
        if reset_started and not bg:
            session = active_session()
            if session is not None:
                session.started = False
        self.log("正在传送至「心愿花街」并前往「穿梭大冒险」小游戏 NPC…")
        nav = RouteTest(self.context)
        nav.dev_only = False
        nav.label = "前往穿梭大冒险"
        return bool(nav.run_test({"route": route_name, "mode": "movement"}))

    def _verify_post_route(self, inputs: ForegroundInput, nav_ok: bool) -> bool:
        if nav_ok:
            return True
        if (
            self._dismiss_npc_dialogue(inputs)
            and self.main()
            and (self._already_near_npc(max_radius_px=1.8) or self._can_interact_with_npc())
        ):
            self.log("路线复核中断，但已位于「奇米亚」交互范围内，继续开始挑战。")
            return True
        self.log("前往「穿梭大冒险」NPC 路线未完成，任务停止。")
        return False

    def _run_challenge_loop(
        self,
        inputs: ForegroundInput,
        *,
        max_rounds: int,
        retry_started_at: float | None,
        stats: dict,
        started_at: float,
    ) -> bool:
        consecutive_failures = 0
        self.log(f"开始「穿梭大冒险」自动刷噗灵（目标 {max_rounds} 局，预计耗时约 {int(max_rounds * 31.2 // 60)} 分钟）…")
        if retry_started_at is None:
            if not self._run_sequence(inputs, INIT_SEQUENCE):
                stats["stop_reason"] = "stopped"
                return False
            post_init = self._inspect_settlement()
            if post_init["state"] == "no_bling":
                stats["stop_reason"] = "insufficient_bling"
                self.log("当前噗灵不足以支付小游戏入场费，任务停止。")
                self._exit_failure_to_main(inputs, post_init)
                return False
            if post_init["state"] == "cap_reached":
                stats["stop_reason"] = "daily_cap_reached"
                self.log("检测到今日大世界噗灵获取已达上限，自动结束。")
                return not self.stopped

        while stats["completed_rounds"] < max_rounds:
            if self.stopped:
                stats["stop_reason"] = "stopped"
                break
            target_round = stats["completed_rounds"] + 1
            round_start = time.monotonic()
            if retry_started_at is None and (
                target_round == 1 or target_round % 5 == 0 or target_round == max_rounds
            ):
                est_bling = max(0, stats["completed_rounds"] * 600 - stats["failed_rounds"] * 100)
                self.log(f"正在进行「穿梭大冒险」第 {target_round}/{max_rounds} 局（已累计净得约 {est_bling:,} 噗灵）…")

            current_retry_start = retry_started_at
            retry_started_at = None
            if not self._play_course(inputs, retry_started_at=current_retry_start):
                stats["stop_reason"] = "stopped"
                break
            inputs.wait(5.0, restore=False)

            info = self._inspect_settlement()
            if info["state"] in ("failed", "retry_confirm"):
                stats["failed_rounds"] += 1
                consecutive_failures += 1
                outcome, retry_started_at = self._handle_failure_retry(
                    inputs, info, target_round, max_rounds, consecutive_failures
                )
                if outcome == "retrying":
                    continue
                stats["stop_reason"] = outcome
                if outcome == "daily_cap_reached":
                    self.log(f"检测到今日大世界噗灵获取已达上限（共完成 {stats['completed_rounds']} 局），自动结束。")
                    break
                return False

            if info["state"] == "cap_reached":
                stats["completed_rounds"] += 1
                consecutive_failures = 0
                stats["stop_reason"] = "daily_cap_reached"
                self.log(f"检测到今日大世界噗灵获取已达上限（共完成 {stats['completed_rounds']} 局），自动结束。")
                break
            if info["state"] == "no_bling":
                stats["stop_reason"] = "insufficient_bling"
                self.log("当前噗灵不足以支付小游戏入场费，任务停止。")
                self._exit_failure_to_main(inputs, info)
                return False

            stats["completed_rounds"] += 1
            consecutive_failures = 0
            if stats["completed_rounds"] < max_rounds:
                if not self._run_sequence(inputs, REPLAY_SEQUENCE):
                    stats["stop_reason"] = "stopped"
                    break
                post_info = self._inspect_settlement()
                if post_info["state"] == "cap_reached":
                    stats["stop_reason"] = "daily_cap_reached"
                    self.log(f"检测到今日大世界噗灵获取已达上限（共完成 {stats['completed_rounds']} 局），自动结束。")
                    break
                if post_info["state"] == "no_bling":
                    stats["stop_reason"] = "insufficient_bling"
                    self.log("当前噗灵不足以支付小游戏入场费，任务停止。")
                    self._exit_failure_to_main(inputs, post_info)
                    return False
                if post_info["state"] in ("failed", "retry_confirm"):
                    stats["completed_rounds"] = max(0, stats["completed_rounds"] - 1)
                    stats["failed_rounds"] += 1
                    consecutive_failures += 1
                    outcome, retry_started_at = self._handle_failure_retry(
                        inputs,
                        post_info,
                        stats["completed_rounds"] + 1,
                        max_rounds,
                        consecutive_failures,
                        timed_out=True,
                    )
                    if outcome == "retrying":
                        continue
                    stats["stop_reason"] = outcome
                    if outcome == "daily_cap_reached":
                        self.log(f"检测到今日大世界噗灵获取已达上限（共完成 {stats['completed_rounds']} 局），自动结束。")
                        break
                    return False
                if post_info["state"] == "continue" and self._is_main_detected(post_info):
                    self.log("检测到返回大世界，正在重新与 NPC 对话继续挑战…")
                    if not self._run_sequence(inputs, INIT_SEQUENCE):
                        stats["stop_reason"] = "stopped"
                        break
            elapsed = time.monotonic() - round_start
            self.log(f"第 {stats['completed_rounds']}/{max_rounds} 局完成，耗时 {elapsed:.1f} 秒。", detail=True)

        if not self.stopped:
            end_info = self._inspect_settlement()
            if end_info["state"] in ("failed", "retry_confirm"):
                self._exit_failure_to_main(inputs, end_info)
            elif not self._is_main_detected(end_info):
                self.main()
        total_min = (time.monotonic() - started_at) / 60.0
        if stats["failed_rounds"] > 0:
            self.log(
                f"「穿梭大冒险」刷噗灵结束：共完成 {stats['completed_rounds']} 局，中途失败重试 {stats['failed_rounds']} 次"
                f"（耗时 {total_min:.1f} 分钟）。"
            )
        else:
            self.log(f"「穿梭大冒险」刷噗灵结束：共完成 {stats['completed_rounds']} 局（耗时 {total_min:.1f} 分钟）。")
        return not self.stopped

    def run(self, settings: dict) -> bool:
        try:
            max_rounds = max(1, min(500, int(settings.get("rounds", 267))))
        except (TypeError, ValueError):
            max_rounds = 267
        navigate = bool(settings.get("navigate", True))
        route_name = str(settings.get("route") or DEFAULT_ROUTE).strip()
        global_background = self.background_mode()
        allow_background = bool(settings.get("background", True)) or global_background

        if not allow_background and not inputs_allowed():
            self.log("关闭后台运行时，小游戏刷噗灵需要当前连接使用前台鼠标和键盘（Seize）。")
            return False

        stats = {
            "completed_rounds": 0,
            "failed_rounds": 0,
            "stop_reason": "completed",
        }
        started_at = time.monotonic()
        active_inputs: list[ForegroundInput] = []
        try:
            self.game_keys.movement()
            if not allow_background:
                fg_inputs = ForegroundInput(self.controller, lambda: self.stopped, bindings=self.game_keys)
                active_inputs.append(fg_inputs)
                focus_game(self, fg_inputs)
                with self._phase_inputs(fg_inputs, background=False):
                    prep = self._prepare_for_challenge(fg_inputs, navigate=navigate, stats=stats)
                    if prep["action"] != "continue":
                        return bool(prep["success"])
                    retry_started_at = prep["retry_started_at"]
                    if prep["need_route"]:
                        nav_ok = self._run_route(route_name)
                        focus_game(self, fg_inputs)
                        if not self._verify_post_route(fg_inputs, nav_ok):
                            stats["stop_reason"] = "stopped" if self.stopped else "navigation_failed"
                            return False
                    return self._run_challenge_loop(
                        fg_inputs,
                        max_rounds=max_rounds,
                        retry_started_at=retry_started_at,
                        stats=stats,
                        started_at=started_at,
                    )

            session = active_session()
            initially_started = bool(session and session.started)
            bg_inputs = BackgroundInput(self.controller, lambda: self.stopped, bindings=self.game_keys)
            active_inputs.append(bg_inputs)
            if bg_inputs.capture_requires_foreground and not initially_started and not bg_inputs.foreground():
                fg_inputs = ForegroundInput(self.controller, lambda: self.stopped, bindings=self.game_keys)
                active_inputs.append(fg_inputs)
                focus_game(self, fg_inputs)

            interact_binding = self.game_keys.get("interact")
            mouse_interact = interact_binding.kind == "mouse"
            if bg_inputs.capture_requires_foreground:
                self.log("已开启后台挑战，但当前截图方式需要前台画面；切出游戏或最小化后将暂停，不会强行切回前台。")
            elif mouse_interact and global_background:
                self.log("已开启完整后台模式；当前交互键为鼠标按键，进入挑战交互期间将自动切回并保持游戏前台，结束后恢复后台模式。")
            elif mouse_interact:
                self.log("挑战阶段支持后台运行；当前交互键为鼠标按键，需要交互的步骤会等待游戏回到前台（最小化后自动暂停）。")
            elif navigate and global_background:
                self.log("已开启完整后台模式：前往「穿梭大冒险」NPC 与后续挑战结算均支持在后台运行（最小化窗口后自动暂停）。")
            elif navigate:
                self.log("已开启后台刷噗灵：若需跑图前往 NPC 将在前台完成，进入挑战与结算后支持切出游戏运行（最小化窗口后自动暂停）。")
            else:
                self.log("已开启后台刷噗灵：对话、挑战与结算支持切出游戏运行（最小化窗口后自动暂停，不会强行切回前台）。")

            need_route = False
            with self._phase_inputs(bg_inputs, background=True):
                with (self.foreground_task("小游戏交互键为鼠标按键") if global_background and mouse_interact else nullcontext()):
                    prep = self._prepare_for_challenge(bg_inputs, navigate=navigate, stats=stats)
                    if prep["action"] != "continue":
                        return bool(prep["success"])
                    retry_started_at = prep["retry_started_at"]
                    need_route = prep["need_route"]
                    if not need_route:
                        return self._run_challenge_loop(
                            bg_inputs,
                            max_rounds=max_rounds,
                            retry_started_at=retry_started_at,
                            stats=stats,
                            started_at=started_at,
                        )

            nav_ok = self._run_route(route_name, reset_started=not initially_started)
            if self.stopped:
                stats["stop_reason"] = "stopped"
                return False
            with self._phase_inputs(bg_inputs, background=True):
                if not self._verify_post_route(bg_inputs, nav_ok):
                    stats["stop_reason"] = "stopped" if self.stopped else "navigation_failed"
                    return False
                if nav_ok and not bg_inputs.capture_requires_foreground:
                    self.log("已到达「穿梭大冒险」NPC 附近，后续挑战与结算可在后台运行，现在可随时切出游戏。")
                with (self.foreground_task("小游戏交互键为鼠标按键") if global_background and mouse_interact else nullcontext()):
                    return self._run_challenge_loop(
                        bg_inputs,
                        max_rounds=max_rounds,
                        retry_started_at=None,
                        stats=stats,
                        started_at=started_at,
                    )
        except (NavigationError, GameWindowClosed, OSError, RuntimeError, ValueError, cv2.error) as error:
            if self.stopped and not isinstance(error, GameWindowClosed):
                stats["stop_reason"] = "stopped"
            else:
                stats["stop_reason"] = f"error:{error}"
                self.log(f"小游戏刷噗灵停止：{error}")
            return False
        finally:
            for item in active_inputs:
                try:
                    item.release()
                except Exception:
                    pass
            try:
                folder = ROOT / "logs/minigame_bling"
                folder.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                write_json(folder / f"{stamp}.json", {
                    "completed_rounds": stats["completed_rounds"],
                    "failed_rounds": stats["failed_rounds"],
                    "target_rounds": max_rounds,
                    "navigate": navigate,
                    "background": allow_background,
                    "stop_reason": stats["stop_reason"],
                })
            except Exception:
                pass
