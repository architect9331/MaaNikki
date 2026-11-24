"""Automated 'Shuttle Adventure' (穿梭大冒险) mini-game loop for daily 160k Bling farming.

Uses the terrain-aware route from Wishfield Street (心愿花街) to reach the NPC at
(10152.80, 10054.80) when not already nearby, then executes the laser-gate
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
from adventure_report import try_record_adventure_entry
from navigation.controller import BackgroundInput, ForegroundInput
from navigation.heading import difference as heading_difference
from navigation.models import GameWindowClosed, NavigationError, maps
from navigation.vision import Locator

NPC_X = 10152.40
NPC_Y = 10054.90
# Qimiya stands inside the gazebo just north of the SW corridor interaction spot.
NPC_BOOTH_X = 10152.50
NPC_BOOTH_Y = 10053.50
NEAR_NPC_RADIUS_PX = 1.6
POST_ROUTE_NPC_RADIUS_PX = 1.6
# Historical telemetry (12/12 successes at x <= 10153.425; 3/3 east-side failures at x >= 10153.775)
# shows the east side of Qimiya's booth blocks interaction even inside a loose arrival radius.
NPC_INTERACT_MAX_X = 10153.55
# Exiting Shuttle Adventure returns Nikki to the SW dirt corridor (~2.5 px from NPC_X/NPC_Y,
# facing Qimiya's booth), just outside the F-interact trigger radius.
NPC_REENTER_APPROACH_MIN_RADIUS_PX = 1.2
NPC_REENTER_APPROACH_MAX_RADIUS_PX = 6.0
NPC_REENTER_APPROACH_MAX_STEPS = 4
NPC_REENTER_APPROACH_STEP_SECONDS = 0.35
NPC_REENTER_HEADING_MAX_DIFF_DEG = 45.0
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
COURSE_TIMER_ROI = [540, 18, 200, 60]
COURSE_TIMER_INITIAL_SECONDS = 25.0
COURSE_TIMER_MIN_VALID_SECONDS = 15.0
# Strictly below 25.0 so a static 00:25.000 shown before/during 3-2-1 cannot trigger early.
COURSE_TIMER_MAX_TICKING_SECONDS = 24.95
COURSE_TIMER_POLL_INTERVAL_SECONDS = 0.10
COURSE_TIMER_POLL_START_NORMAL_SECONDS = 3.0
COURSE_TIMER_POLL_START_RETRY_SECONDS = 1.0
# Calibrated in-game clock offsets after 00:25.000 starts ticking, preserving the
# 3dc2c02 laser-phase alignment (8.07 - 5.55 = 2.52s, 7.03 - 5.55 = 1.48s).
COURSE_GO_OFFSET_CAMERA_ASSIST_ON = 2.52
COURSE_GO_OFFSET_CAMERA_ASSIST_OFF = 1.48
ENTRY_FEE_BLING = 100
CLEAR_REWARD_BLING = 700
# Both tracks wait past the ~5.4-5.7s countdown unlock jitter so key holds
# execute undistorted against G1-G6 moving lasers while still finishing
# under the 15s hard-clear threshold (~11.5-13.4s).
COURSE_START_WAIT_CAMERA_ASSIST_ON = 8.07
COURSE_START_WAIT_CAMERA_ASSIST_OFF = 7.03
COURSE_STRAIGHT_CAMERA_ASSIST_ON = 3.50
COURSE_STRAIGHT_CAMERA_ASSIST_OFF = 0.0
COURSE_TURNS_CAMERA_ASSIST_ON: tuple[tuple[str, float, float], ...] = (
    ("right", 1.46, 1.56),
    ("right", 1.57, 0.92),
    ("left", 0.79, 1.80),
)
COURSE_TURNS_CAMERA_ASSIST_OFF: tuple[tuple[str, float, float], ...] = (
    ("left", 0.38, 2.58),
    ("right", 2.18, 1.37),
    ("right", 1.86, 2.80),
)
MAX_CONSECUTIVE_FAILURES = 6
REENTER_NPC_ON_CONSECUTIVE_FAILURE = 3
MAX_CONSECUTIVE_OVERWORLD_RETURNS = 3
AT_NPC_VERIFY_RADIUS_PX = 15.0
AT_NPC_VERIFY_MIN_SCORE = 0.45

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
_NPC_INTERACT_TEXT_RE = re.compile(r"奇米亚|穿梭大冒险|对话|交谈")
_COURSE_TIMER_TRANSLATION = str.maketrans({
    "O": "0", "o": "0", "D": "0", "Q": "0",
    "I": "1", "l": "1", "|": "1",
    "S": "5", "s": "5",
})
_COURSE_TIMER_RE = re.compile(
    r"(?<!\d)(\d{2})\s*[:：.\-]?\s*(\d{2})(?:\s*[.:：,\-]\s*(\d{1,3})|(\d{3}))(?!\d)"
)


def _parse_course_timer_text(text: str) -> float | None:
    """Parse a ticking top-center MM:SS.mmm Shuttle Adventure countdown string."""
    if not text:
        return None
    norm = text.strip().translate(_COURSE_TIMER_TRANSLATION)
    match = _COURSE_TIMER_RE.search(norm)
    if not match:
        return None
    mins = int(match.group(1))
    secs = int(match.group(2))
    frac_raw = match.group(3) or match.group(4)
    remaining = secs + int(frac_raw) / (10 ** len(frac_raw))
    if mins == 0 and COURSE_TIMER_MIN_VALID_SECONDS <= remaining <= COURSE_TIMER_MAX_TICKING_SECONDS:
        return remaining
    return None


def _mark_entry_paid(stats: dict) -> None:
    """Record one 100-Bling entry/retry fee when entering a round if not yet recorded."""
    if not stats.get("active_entry_paid"):
        stats["entries_paid"] = int(stats.get("entries_paid", 0)) + 1
        stats["active_entry_paid"] = True


def _refund_unentered_entry(stats: dict) -> None:
    """Revert the pending entry fee when a round never left the overworld or lacked Bling."""
    if stats.get("active_entry_paid"):
        stats["entries_paid"] = max(0, int(stats.get("entries_paid", 0)) - 1)
        stats["active_entry_paid"] = False


def _settle_entry(stats: dict) -> None:
    """Clear the pending flag once the current round has settled (win or fail)."""
    stats["active_entry_paid"] = False


def _calc_net_bling(stats: dict, *, include_pending: bool = True) -> int:
    """Return signed estimated net Bling: cleared rewards minus paid 100-Bling entries/retries."""
    wins = int(stats.get("completed_rounds") or 0)
    paid = int(stats.get("entries_paid") or 0)
    if not include_pending and stats.get("active_entry_paid"):
        paid = max(0, paid - 1)
    return wins * CLEAR_REWARD_BLING - paid * ENTRY_FEE_BLING

INIT_OPEN_WAIT_SECONDS = 2.4
INIT_SEQUENCE: tuple[tuple[str, float] | float, ...] = (
    ("interact", 0.2),
    INIT_OPEN_WAIT_SECONDS,
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
INIT_CONTINUE_SEQUENCE: tuple[tuple[str, float] | float, ...] = INIT_SEQUENCE[2:]

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
    """Executes the Shuttle Adventure laser-gate mini-game loop until target rounds or daily cap."""

    def _locate_near_npc(
        self,
        max_radius_px: float = NEAR_NPC_RADIUS_PX,
        *,
        min_score: float = 0.52,
        frame=None,
        enforce_front_zone: bool = False,
    ):
        try:
            spec = maps(RESOURCE).get("miraland")
            if spec is None:
                return None
            if frame is None:
                if not self.hit("MaaNikki_MainDetected"):
                    return None
                frame = self.capture()
                if frame is None:
                    return None
            elif not self.hit("MaaNikki_MainDetected", image=frame):
                return None
            locator = Locator(spec)
            pose = locator.locate(frame, hint=(NPC_X, NPC_Y), search_radius=45.0, commit=False)
            if pose and pose.position_reliable and pose.score >= min_score:
                if math.hypot(pose.x - NPC_X, pose.y - NPC_Y) <= max_radius_px:
                    if not enforce_front_zone or pose.x <= NPC_INTERACT_MAX_X:
                        return pose
        except Exception:
            return None
        return None

    def _already_near_npc(
        self,
        max_radius_px: float = NEAR_NPC_RADIUS_PX,
        *,
        min_score: float = 0.52,
        frame=None,
        enforce_front_zone: bool = False,
    ) -> bool:
        return (
            self._locate_near_npc(
                max_radius_px=max_radius_px,
                min_score=min_score,
                frame=frame,
                enforce_front_zone=enforce_front_zone,
            )
            is not None
        )

    def _verify_near_npc(self, *, attempts: int = 2) -> bool:
        for attempt in range(attempts):
            if self.stopped:
                return False
            if self._already_near_npc(
                max_radius_px=AT_NPC_VERIFY_RADIUS_PX,
                min_score=AT_NPC_VERIFY_MIN_SCORE,
            ):
                return True
            if attempt + 1 < attempts and not self.pause(0.25):
                return False
        return False

    def _can_interact_with_npc(self, frame=None) -> bool:
        if frame is None:
            frame = self.capture()
        if frame is None or not self.hit("MaaNikki_MainDetected", image=frame):
            return False
        pickup_roi = self.ui.roi("AreaPickup")
        if (
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
        ):
            return True
        return bool(self.text(pickup_roi, _NPC_INTERACT_TEXT_RE.pattern, image=frame))

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

            skip_text = self.text(DIALOGUE_SKIP_ROI, _DIALOGUE_SKIP_RE.pattern, image=frame)
            has_skip = bool(skip_text and "跳过全部" not in skip_text)
            has_mode = bool(self.text(DIALOGUE_MODE_ROI, _DIALOGUE_MODE_RE.pattern, image=frame))
            in_dialogue_hud = has_skip or has_mode

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
            has_explicit_pair = bool(
                exit_box is not None
                and any(_DIALOGUE_OPTION_RE.search(txt) for txt, _ in choice_rows)
            )
            choice_menu_visible = bool(
                has_explicit_pair
                or (
                    in_dialogue_hud
                    and (
                        exit_box is not None
                        or len(choice_rows) >= 2
                        or any(_DIALOGUE_OPTION_RE.search(txt) for txt, _ in choice_rows)
                    )
                )
            )
            if choice_menu_visible and exit_box is not None:
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

            if in_dialogue_hud:
                progressed = True
                if has_mode and not has_skip and mode_only_frames == 0:
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

    def _wait_sliced(self, inputs: ForegroundInput, target_time: float) -> bool:
        """Wait until ``target_time`` in <=4s slices to honor ForegroundInput.wait's 8s cap."""
        while not self.stopped:
            remaining = target_time - time.monotonic()
            if remaining <= 1e-6:
                return True
            inputs.wait(min(4.0, remaining), restore=False)
        return False

    def _detect_course_timer(self, frame) -> float | None:
        """Read the ticking top-center 00:25.000 countdown directly via OCR."""
        result = self.recognize("MaaNikki_Daily_OCR", {"roi": COURSE_TIMER_ROI}, image=frame)
        if not result or not result.hit:
            return None
        rows = (
            getattr(result, "filtered_results", None)
            or getattr(result, "all_results", None)
            or ([result.best_result] if getattr(result, "best_result", None) is not None else [])
        )
        for row in rows:
            if row is None:
                continue
            score = getattr(row, "score", None)
            if isinstance(score, (int, float)) and score < 0.40:
                continue
            txt = str(getattr(row, "text", "") or "").strip()
            parsed = _parse_course_timer_text(txt)
            if parsed is not None:
                return parsed
        return None

    def _wait_for_course_start(
        self,
        inputs: ForegroundInput,
        trigger_at: float,
        *,
        camera_assist: bool,
        is_retry: bool,
    ) -> tuple[str, float, float | None]:
        """Align movement start to the live ticking 25s countdown, falling back on misses."""
        fallback_wait = (
            COURSE_START_WAIT_CAMERA_ASSIST_ON
            if camera_assist
            else COURSE_START_WAIT_CAMERA_ASSIST_OFF
        )
        go_offset = (
            COURSE_GO_OFFSET_CAMERA_ASSIST_ON
            if camera_assist
            else COURSE_GO_OFFSET_CAMERA_ASSIST_OFF
        )
        poll_start_offset = (
            COURSE_TIMER_POLL_START_RETRY_SECONDS
            if is_retry
            else COURSE_TIMER_POLL_START_NORMAL_SECONDS
        )
        poll_deadline = trigger_at + fallback_wait

        if not self._wait_sliced(inputs, trigger_at + poll_start_offset):
            return "stopped", 0.0, None

        first_sample: tuple[float, float, float] | None = None
        aligned_t_go: float | None = None
        last_frame = None
        while not self.stopped and time.monotonic() < poll_deadline:
            t_before = time.monotonic()
            frame = self.capture()
            t_after = time.monotonic()
            if self.stopped:
                return "stopped", 0.0, None
            if frame is None:
                break
            last_frame = frame
            captured_at = (
                0.5 * (t_before + t_after)
                if (t_after - t_before) <= 0.15
                else (t_after - 0.015)
            )
            remaining = self._detect_course_timer(frame)
            if remaining is not None:
                t_go = captured_at - (COURSE_TIMER_INITIAL_SECONDS - remaining)
                if t_go >= trigger_at - 0.5:
                    if first_sample is None:
                        if (t_go + go_offset) - time.monotonic() >= 0.25:
                            first_sample = (captured_at, remaining, t_go)
                            sleep_s = min(
                                COURSE_TIMER_POLL_INTERVAL_SECONDS,
                                max(0.02, (t_go + go_offset) - time.monotonic() - 0.15),
                            )
                            if not self.pause(sleep_s):
                                return "stopped", 0.0, None
                            continue
                        aligned_t_go = t_go
                        break
                    _, prev_rem, prev_t_go = first_sample
                    if remaining < prev_rem and abs(t_go - prev_t_go) <= 0.30:
                        aligned_t_go = 0.5 * (prev_t_go + t_go)
                        break
                    first_sample = (captured_at, remaining, t_go)
                    sleep_s = min(
                        COURSE_TIMER_POLL_INTERVAL_SECONDS,
                        max(0.0, poll_deadline - time.monotonic()),
                    )
                    if sleep_s > 0 and not self.pause(sleep_s):
                        return "stopped", 0.0, None
                    continue
            sleep_s = min(
                COURSE_TIMER_POLL_INTERVAL_SECONDS,
                max(0.0, poll_deadline - time.monotonic()),
            )
            if sleep_s <= 0:
                break
            if not self.pause(sleep_s):
                return "stopped", 0.0, None

        if self.stopped:
            return "stopped", 0.0, None
        if aligned_t_go is None and first_sample is not None:
            aligned_t_go = first_sample[2]
        if aligned_t_go is not None:
            if not self._wait_sliced(inputs, aligned_t_go + go_offset):
                return "stopped", 0.0, None
            return "timer_aligned", time.monotonic() - trigger_at, aligned_t_go

        if not self._wait_sliced(inputs, trigger_at + fallback_wait):
            return "stopped", 0.0, None
        # Never execute the blind 11.6s movement sequence when the timer never appeared
        # and two consecutive frames confirm the game is still on the overworld main screen.
        if self._confirm_overworld_main(last_frame):
            return "not_in_course", time.monotonic() - trigger_at, None
        if self.stopped:
            return "stopped", 0.0, None
        return "fallback", time.monotonic() - trigger_at, None

    def _confirm_overworld_main(self, initial_frame=None) -> bool:
        """Confirm on two frames that the game is idle on the overworld main screen."""
        first = initial_frame if initial_frame is not None else self.capture()
        if self.stopped or first is None or not self.hit("MaaNikki_MainDetected", image=first):
            return False
        if not self.pause(0.25):
            return False
        second = self.capture()
        if self.stopped or second is None:
            return False
        return bool(self.hit("MaaNikki_MainDetected", image=second))

    def _play_course(
        self,
        inputs: ForegroundInput,
        *,
        camera_assist: bool = True,
        retry_started_at: float | None = None,
        stats: dict | None = None,
    ) -> str:
        if self.stopped:
            return "stopped"
        if retry_started_at is None:
            inputs.hold("interact", 0.2, restore=False)
            trigger_at = time.monotonic()
        else:
            trigger_at = retry_started_at
        align_mode, total_wait_s, aligned_t_go = self._wait_for_course_start(
            inputs,
            trigger_at,
            camera_assist=camera_assist,
            is_retry=retry_started_at is not None,
        )
        if align_mode == "stopped" or self.stopped:
            return "stopped"
        if align_mode == "not_in_course":
            self.log(
                f"起跑对齐：未检测到 25 秒倒计时且当前仍停留在主界面（已等待 {total_wait_s:.2f} 秒），"
                "已跳过本轮跑道按键以避免大世界误跑。",
                detail=True,
            )
            return "not_in_course"
        if stats is not None:
            if align_mode == "timer_aligned":
                stats["timer_aligned_rounds"] = int(stats.get("timer_aligned_rounds", 0)) + 1
            else:
                stats["fallback_start_rounds"] = int(stats.get("fallback_start_rounds", 0)) + 1
        if aligned_t_go is not None:
            unlock_s = aligned_t_go - trigger_at
            remaining_at_go = max(0.0, COURSE_TIMER_INITIAL_SECONDS - (time.monotonic() - aligned_t_go))
            self.log(
                f"起跑对齐：{align_mode}，倒计时解锁 {unlock_s:.2f} 秒，总等待 {total_wait_s:.2f} 秒"
                f"（起步剩余计时 {remaining_at_go:.2f} 秒）。",
                detail=True,
            )
        else:
            self.log(f"起跑对齐：{align_mode}，总等待 {total_wait_s:.2f} 秒。", detail=True)
        straight_seconds = (
            COURSE_STRAIGHT_CAMERA_ASSIST_ON
            if camera_assist
            else COURSE_STRAIGHT_CAMERA_ASSIST_OFF
        )
        turns = COURSE_TURNS_CAMERA_ASSIST_ON if camera_assist else COURSE_TURNS_CAMERA_ASSIST_OFF
        fwd = self.game_keys.get("forward")
        steer_keys = {
            "left": self.game_keys.get("left"),
            "right": self.game_keys.get("right"),
        }
        try:
            inputs._down(fwd)
            if straight_seconds > 0:
                inputs.wait(straight_seconds)
            for steer_name, hold_steer, wait_after in turns:
                if self.stopped:
                    return "stopped"
                steer_key = steer_keys[steer_name]
                inputs._down(steer_key)
                try:
                    inputs.wait(hold_steer)
                finally:
                    inputs._up(steer_key)
                if wait_after > 0:
                    inputs.wait(wait_after)
        finally:
            for steer_key in steer_keys.values():
                inputs._up(steer_key)
            inputs._up(fwd)
            inputs.release()
        return "stopped" if self.stopped else "played"

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

    def _click_guarded(self, box: list[int], generation: int, *, cursor_pos: bool = True) -> bool | None:
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

    def _should_retry_modal_in_foreground(self) -> bool:
        inputs = self._active_inputs()
        return bool(
            inputs is not None
            and getattr(inputs, "window_only", False)
            and not inputs.foreground()
        )

    @contextmanager
    def _temporary_modal_foreground(self, reason: str):
        session = active_session()
        inputs = self._active_inputs()
        with (session.suspend_background_observation() if session is not None else nullcontext()):
            if self.background_mode() and session is not None:
                with self.foreground_task(reason):
                    yield
                return
            if not self.background_mode() and inputs is not None and not inputs.foreground():
                self.log(f"{reason}，等待游戏回到前台后继续操作。")
            if session is not None:
                session.wait(inputs, require_foreground=True)
            yield

    def _poll_modal_transition(
        self,
        info: dict,
        *,
        click_at: float,
        poll_timeout: float,
        poll_interval: float,
        until,
        still_active=None,
        early_retry_after: float | None = None,
    ) -> tuple[bool | None, dict]:
        poll_deadline = time.monotonic() + poll_timeout
        while not self.stopped and time.monotonic() < poll_deadline:
            if not self.pause(poll_interval):
                return None, info
            info = self._inspect_settlement()
            if until(info, click_at):
                return True, info
            if (
                early_retry_after is not None
                and time.monotonic() - click_at >= early_retry_after
                and (still_active is None or still_active(info))
            ):
                break
        if self.stopped:
            return None, info
        return False, info

    def _click_modal_button(
        self,
        info: dict,
        box_key: str,
        *,
        max_clicks: int,
        poll_timeout: float,
        poll_interval: float,
        until,
        still_active=None,
        early_retry_after: float | None = None,
        force_cursor_pos: bool = True,
    ) -> tuple[bool | None, dict, float]:
        """Click a settlement button up to ``max_clicks`` times until ``until(info, click_at)`` is True.

        Stops sending clicks once ``still_active(info)`` becomes False so fade transitions
        (e.g., transient ``continue`` frames) finish debouncing without extra clicks.
        Returns ``(matched, latest_info, click_at)`` where ``matched`` is True on match,
        False on exhausted retries, or None when stopped/click-failed.
        """
        sent_clicks = 0
        stalled_focus_retries = 0
        last_focus_gen = info["generation"]
        click_at = 0.0
        while sent_clicks < max_clicks and not self.stopped:
            if still_active is not None and not still_active(info):
                break
            clicked = self._click_guarded(
                info[box_key],
                info["generation"],
                cursor_pos=force_cursor_pos or sent_clicks > 0,
            )
            if clicked is False:
                return None, info, 0.0
            if clicked is None:
                info = self._inspect_settlement()
                click_at = time.monotonic()
                if until(info, click_at):
                    return True, info, click_at
                if still_active is not None and not still_active(info):
                    matched, info = self._poll_modal_transition(
                        info,
                        click_at=click_at,
                        poll_timeout=poll_timeout,
                        poll_interval=poll_interval,
                        until=until,
                    )
                    return matched, info, click_at
                if info["generation"] == last_focus_gen:
                    stalled_focus_retries += 1
                    if stalled_focus_retries >= 3:
                        break
                else:
                    last_focus_gen = info["generation"]
                    stalled_focus_retries = 0
                continue
            sent_clicks += 1
            # Account for Runtime.action('Click') 0.1s pause plus window-input 0.03s post-release sleep.
            post_click_lag = 0.13 if self._use_window_input(self._active_inputs()) else 0.1
            click_at = time.monotonic() - post_click_lag
            matched, info = self._poll_modal_transition(
                info,
                click_at=click_at,
                poll_timeout=poll_timeout,
                poll_interval=poll_interval,
                until=until,
                still_active=still_active,
                early_retry_after=early_retry_after,
            )
            if matched is None or matched:
                return matched, info, click_at
        if self.stopped:
            return None, info, 0.0
        return False, info, click_at

    def _retry_from_failure_pass(
        self,
        info: dict,
        *,
        max_clicks: int,
        force_cursor_pos: bool = True,
    ) -> tuple[str, dict, float]:
        for _ in range(2):
            if self.stopped:
                return "stopped", info, 0.0

            # Step 1: Click '重试' on '挑战失败' until '是否消耗100噗灵重试？' opens.
            if info["state"] == "failed":
                clear_count = 0

                def _retry_step_done(cur: dict, clicked_at: float) -> bool:
                    nonlocal clear_count
                    if cur["state"] in ("no_bling", "cap_reached", "retry_confirm"):
                        return True
                    if cur["state"] == "continue":
                        clear_count += 1
                        return clear_count >= 2 and time.monotonic() - clicked_at >= 0.8
                    clear_count = 0
                    return False

                done, info, click_at = self._click_modal_button(
                    info,
                    "retry_box",
                    max_clicks=max_clicks,
                    poll_timeout=2.5,
                    poll_interval=0.25,
                    until=_retry_step_done,
                    still_active=lambda cur: cur["state"] == "failed",
                    early_retry_after=1.2,
                    force_cursor_pos=force_cursor_pos,
                )
                if done is None:
                    return "stopped", info, 0.0
                if info["state"] in ("no_bling", "cap_reached"):
                    return info["state"], info, 0.0
                if info["state"] == "continue" and not done:
                    if not self.pause(0.35):
                        return "stopped", info, 0.0
                    info = self._inspect_settlement()
                if info["state"] in ("no_bling", "cap_reached"):
                    return info["state"], info, 0.0
                if info["state"] == "continue":
                    return "retrying", info, click_at
                if info["state"] == "failed":
                    return "retry_failed", info, 0.0

            # Step 2: Click '好的' on '是否消耗100噗灵重试？' to enter the 3-2-1 countdown.
            if info["state"] == "retry_confirm":
                done, info, click_at = self._click_modal_button(
                    info,
                    "ok_box",
                    max_clicks=max_clicks,
                    poll_timeout=2.2,
                    poll_interval=0.3,
                    until=lambda cur, _: cur["state"] != "retry_confirm",
                    still_active=lambda cur: cur["state"] == "retry_confirm",
                    early_retry_after=1.2,
                    force_cursor_pos=force_cursor_pos,
                )
                if done is None:
                    return "stopped", info, 0.0
                if info["state"] in ("no_bling", "cap_reached"):
                    return info["state"], info, 0.0
                if info["state"] == "continue":
                    return "retrying", info, click_at
                if info["state"] == "failed":
                    continue
                return "retry_failed", info, 0.0

            if info["state"] in ("no_bling", "cap_reached"):
                return info["state"], info, 0.0
            return ("stopped" if self.stopped else "retrying"), info, time.monotonic()

        return "retry_failed", info, 0.0

    def _retry_from_failure(self, info: dict | None = None) -> tuple[str, float]:
        """Click '重试' on '挑战失败' and '好的' on '是否消耗100噗灵重试？'.

        Returns (status, click_started_at) where status is one of:
        'retrying', 'no_bling', 'cap_reached', 'stopped', 'retry_failed'.
        """
        if info is None:
            info = self._inspect_settlement()

        allow_fg_fallback = self._should_retry_modal_in_foreground()
        status, _, click_at = self._retry_from_failure_pass(
            info,
            max_clicks=2 if allow_fg_fallback else 3,
        )
        if status != "retry_failed" or self.stopped or not self._should_retry_modal_in_foreground():
            return status, click_at

        with self._temporary_modal_foreground("后台点击小游戏重试弹窗未响应"):
            info = self._inspect_settlement()
            if info["state"] in ("no_bling", "cap_reached"):
                return info["state"], 0.0
            if info["state"] == "continue":
                return "retrying", time.monotonic()
            status, _, click_at = self._retry_from_failure_pass(
                info,
                max_clicks=2,
                force_cursor_pos=True,
            )
            return status, click_at

    def _dismiss_failure_modals(
        self,
        info: dict,
        *,
        max_clicks: int,
        force_cursor_pos: bool = True,
    ) -> dict | None:
        if info["has_retry_confirm"]:
            done, info, _ = self._click_modal_button(
                info,
                "cancel_box",
                max_clicks=max_clicks,
                poll_timeout=2.0,
                poll_interval=0.25,
                until=lambda cur, _: not cur["has_retry_confirm"],
                still_active=lambda cur: cur["has_retry_confirm"],
                early_retry_after=1.2 if force_cursor_pos else None,
                force_cursor_pos=force_cursor_pos,
            )
            if done is None:
                return None

        if info["has_failed"]:
            done, info, _ = self._click_modal_button(
                info,
                "exit_box",
                max_clicks=max_clicks,
                poll_timeout=2.5,
                poll_interval=0.3,
                until=lambda cur, _: not cur["has_failed"],
                still_active=lambda cur: cur["has_failed"],
                early_retry_after=1.2 if force_cursor_pos else None,
                force_cursor_pos=force_cursor_pos,
            )
            if done is None:
                return None
        return info

    def _exit_failure_to_main(self, inputs: ForegroundInput, info: dict | None = None) -> bool:
        """Dismiss retry confirmation / '挑战失败' via '取消' -> '退出' and return to overworld."""
        if self.stopped:
            return False
        if info is None:
            info = self._inspect_settlement()

        allow_fg_fallback = self._should_retry_modal_in_foreground()
        info = self._dismiss_failure_modals(
            info,
            max_clicks=2 if allow_fg_fallback else 3,
        )
        if info is None or self.stopped:
            return False

        if (info["has_retry_confirm"] or info["has_failed"]) and self._should_retry_modal_in_foreground():
            with self._temporary_modal_foreground("后台点击小游戏退出弹窗未响应"):
                info = self._inspect_settlement()
                if info["has_retry_confirm"] or info["has_failed"]:
                    info = self._dismiss_failure_modals(
                        info,
                        max_clicks=2,
                        force_cursor_pos=True,
                    )
                    if info is None or self.stopped:
                        return False

        if info["has_retry_confirm"] or info["has_failed"]:
            return False

        # After clicking '退出', never spam blind interact keys while the overworld fades in,
        # which can re-talk to Qimiya and re-enter the minigame right before main() presses ESC.
        deadline = time.monotonic() + 10.0
        last_dialogue_check = 0.0
        while not self.stopped and time.monotonic() < deadline:
            frame = self.capture()
            if frame is None:
                return False
            if self.hit("MaaNikki_MainDetected", image=frame):
                return self.main()
            now = time.monotonic()
            if (
                not self.ui.asset("IconUILoading", image=frame, threshold=.99, method=cv2.TM_CCORR_NORMED)
                and now - last_dialogue_check >= 1.0
            ):
                if not self._dismiss_npc_dialogue(inputs):
                    return False
                last_dialogue_check = time.monotonic()
            if not self.pause(0.35):
                return False
        return self.main()

    @staticmethod
    def _npc_approach_metrics(pose) -> tuple[float, float] | None:
        """Return (min_dist_to_front_or_booth, min_heading_diff_deg) for a located pose."""
        if pose is None or pose.heading is None or getattr(pose, "heading_confidence", 0.0) < 0.5:
            return None
        targets = ((NPC_X, NPC_Y), (NPC_BOOTH_X, NPC_BOOTH_Y))
        dists = [math.hypot(tx - pose.x, ty - pose.y) for tx, ty in targets]
        diffs = [
            abs(heading_difference(math.degrees(math.atan2(tx - pose.x, -(ty - pose.y))) % 360.0, pose.heading))
            for tx, ty in targets
        ]
        return min(dists), min(diffs)

    def _approach_npc_for_interact(self, inputs: ForegroundInput) -> bool:
        """Nudge forward along the flat front corridor when exit respawn sits just outside F range."""
        if self.stopped:
            return False
        frame = self.capture()
        if frame is None or not self.hit("MaaNikki_MainDetected", image=frame):
            return False
        if self._can_interact_with_npc(frame=frame):
            return True
        pose = self._locate_near_npc(
            max_radius_px=NPC_REENTER_APPROACH_MAX_RADIUS_PX,
            min_score=0.52,
            frame=frame,
            enforce_front_zone=True,
        )
        metrics = self._npc_approach_metrics(pose)
        if pose is None or metrics is None:
            return False
        dist, heading_diff = metrics
        if dist <= NPC_REENTER_APPROACH_MIN_RADIUS_PX or heading_diff > NPC_REENTER_HEADING_MAX_DIFF_DEG:
            return False
        self.log("当前位于「奇米亚」正面但尚未进入交互范围，向前微调靠近…", detail=True)
        prev_x, prev_y, prev_dist = pose.x, pose.y, dist
        for _ in range(NPC_REENTER_APPROACH_MAX_STEPS):
            if self.stopped:
                return False
            inputs.hold("forward", NPC_REENTER_APPROACH_STEP_SECONDS, restore=False)
            if self.stopped:
                return False
            inputs.wait(0.25, restore=False)
            if self.stopped:
                return False
            frame = self.capture()
            if frame is None or not self.hit("MaaNikki_MainDetected", image=frame):
                return False
            if self._can_interact_with_npc(frame=frame):
                return True
            pose = self._locate_near_npc(
                max_radius_px=NPC_REENTER_APPROACH_MAX_RADIUS_PX,
                min_score=0.52,
                frame=frame,
                enforce_front_zone=True,
            )
            metrics = self._npc_approach_metrics(pose)
            if pose is None or metrics is None or pose.x > NPC_INTERACT_MAX_X:
                break
            new_dist, heading_diff = metrics
            moved = math.hypot(pose.x - prev_x, pose.y - prev_y)
            if (
                new_dist <= NPC_REENTER_APPROACH_MIN_RADIUS_PX
                or new_dist > prev_dist + 0.35
                or moved < 0.15
                or heading_diff > NPC_REENTER_HEADING_MAX_DIFF_DEG
            ):
                break
            prev_x, prev_y = pose.x, pose.y
            prev_dist = min(prev_dist, new_dist)
        return self._can_interact_with_npc()

    def _open_npc_dialogue(self, inputs: ForegroundInput, *, max_attempts: int = 2) -> str:
        """Press interact and confirm overworld HUD disappears before sending dialogue keys."""
        for attempt in range(max_attempts):
            if self.stopped:
                return "stopped"
            self._approach_npc_for_interact(inputs)
            if self.stopped:
                return "stopped"
            inputs.hold("interact", 0.2, restore=False)
            if self.stopped:
                return "stopped"
            wait_deadline = time.monotonic() + INIT_OPEN_WAIT_SECONDS
            opened = False
            while not self.stopped and time.monotonic() < wait_deadline:
                step = min(0.25, max(0.0, wait_deadline - time.monotonic()))
                if step > 0:
                    inputs.wait(step, restore=False)
                if self.stopped:
                    return "stopped"
                frame = self.capture()
                if frame is None:
                    return "stopped" if self.stopped else "not_entered"
                if not self.hit("MaaNikki_MainDetected", image=frame):
                    opened = True
                    break
            if self.stopped:
                return "stopped"
            if not opened:
                frame = self.capture()
                if frame is None:
                    return "stopped" if self.stopped else "not_entered"
                opened = not self.hit("MaaNikki_MainDetected", image=frame)
            if opened:
                remaining = wait_deadline - time.monotonic()
                if remaining > 0:
                    inputs.wait(remaining, restore=False)
                return "stopped" if self.stopped else "opened"
            if attempt + 1 < max_attempts:
                self.log("首次尝试与「奇米亚」对话未离开主界面，重试一次交互键…", detail=True)
        return "stopped" if self.stopped else "not_entered"

    def _enter_via_npc_dialogue(self, inputs: ForegroundInput, stats: dict) -> str:
        """Open Qimiya dialogue, verify entry, then finish menu selection before marking paid."""
        open_state = self._open_npc_dialogue(inputs)
        if open_state != "opened":
            if open_state == "not_entered":
                self.log("未能打开「奇米亚」对话（画面仍停留在主界面），已取消后续按键以避免大世界误操作。")
            return open_state
        if not self._run_sequence(inputs, INIT_CONTINUE_SEQUENCE):
            return "stopped"
        post_init = self._inspect_settlement()
        if post_init["state"] == "no_bling":
            self.log("当前噗灵不足以支付小游戏入场费，任务停止。")
            self._exit_failure_to_main(inputs, post_init)
            return "no_bling"
        if post_init["state"] == "cap_reached":
            return "cap_reached"
        _mark_entry_paid(stats)
        return "entered"

    def _log_run_summary(self, stats: dict, started_at: float) -> None:
        wins = int(stats["completed_rounds"] or 0)
        losses = int(stats["failed_rounds"] or 0)
        retries = int(stats["retry_count"] or 0)
        paid = int(stats["entries_paid"] or 0)
        if wins <= 0 and losses <= 0 and paid <= 0 and self.stopped:
            return
        net_bling = _calc_net_bling(stats, include_pending=True)
        total_min = (time.monotonic() - started_at) / 60.0
        if losses > 0 or retries > 0:
            self.log(
                f"「穿梭大冒险」刷噗灵结束：共完成 {wins} 局，失败 {losses} 次（付费重试 {retries} 次），"
                f"累计支付入场/重试费 {paid * ENTRY_FEE_BLING:,} 噗灵，预计净收益约 {net_bling:,} 噗灵"
                f"（耗时 {total_min:.1f} 分钟）。"
            )
        elif paid > wins:
            self.log(
                f"「穿梭大冒险」刷噗灵结束：共完成 {wins} 局，累计支付入场费 {paid * ENTRY_FEE_BLING:,} 噗灵，"
                f"预计净收益约 {net_bling:,} 噗灵（耗时 {total_min:.1f} 分钟）。"
            )
        else:
            self.log(
                f"「穿梭大冒险」刷噗灵结束：共完成 {wins} 局，预计净收益约 {net_bling:,} 噗灵"
                f"（耗时 {total_min:.1f} 分钟）。"
            )

    def _handle_failure_retry(
        self,
        inputs: ForegroundInput,
        info: dict,
        target_round: int,
        max_rounds: int,
        consecutive_failures: int,
        *,
        stats: dict,
        timed_out: bool = False,
    ) -> tuple[str, float | None]:
        """Execute bounded retry on '挑战失败'; returns (action, retry_started_at)."""
        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            self.log(
                f"连续 {consecutive_failures} 次挑战失败，已停止重试并退出小游戏，"
                "请确认任务的「视角水平辅助」选项与游戏内「设置 → 控制 → 视角水平辅助」一致，"
                "并检查移动键位、画面帧率或后台限帧状态。"
            )
            self._exit_failure_to_main(inputs, info)
            return "too_many_failures", None

        reason_label = "挑战超时失败" if timed_out else "挑战失败"
        cur_net = _calc_net_bling(stats, include_pending=True)
        if consecutive_failures == REENTER_NPC_ON_CONSECUTIVE_FAILURE:
            self.log(
                f"第 {target_round}/{max_rounds} 局{reason_label}（连续失败 {consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}，"
                f"当前已结算净收益约 {cur_net:,} 噗灵），正在退出并重新对话刷新挑战状态…"
            )
            if self._exit_failure_to_main(inputs, info):
                entry_state = self._enter_via_npc_dialogue(inputs, stats)
                if entry_state == "entered":
                    stats["retry_count"] = int(stats.get("retry_count", 0)) + 1
                    return "retrying", None
                if entry_state == "no_bling":
                    return "insufficient_bling", None
                if entry_state == "cap_reached":
                    return "daily_cap_reached", None
                if entry_state == "stopped":
                    return "stopped", None
                return "dialogue_not_opened", None
            if self.stopped:
                return "stopped", None
            self.log("未能退出小游戏刷新状态，继续尝试直接点击重试…", detail=True)
            info = self._inspect_settlement()
        else:
            self.log(
                f"第 {target_round}/{max_rounds} 局{reason_label}，正在消耗 100 噗灵重试"
                f"（连续失败 {consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}，当前已结算净收益约 {cur_net:,} 噗灵）…"
            )
        status, click_at = self._retry_from_failure(info)
        if status == "retrying":
            stats["retry_count"] = int(stats.get("retry_count", 0)) + 1
            _mark_entry_paid(stats)
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
        if self._exit_failure_to_main(inputs):
            entry_state = self._enter_via_npc_dialogue(inputs, stats)
            if entry_state == "entered":
                stats["retry_count"] = int(stats.get("retry_count", 0)) + 1
                return "retrying", None
            if entry_state == "no_bling":
                return "insufficient_bling", None
            if entry_state == "cap_reached":
                return "daily_cap_reached", None
            if entry_state == "stopped":
                return "stopped", None
            if entry_state == "not_entered":
                return "dialogue_not_opened", None
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
                    _mark_entry_paid(stats)
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
                if not (navigate and self.main()):
                    stats["stop_reason"] = "dialogue_exit_failed"
                    self.log("未能退出当前对话界面，无法开始小游戏。")
                    return {"action": "stop", "success": False}

        need_route = False
        if retry_started_at is None:
            if not self.main():
                if not (self._dismiss_npc_dialogue(inputs) and self.main()):
                    stats["stop_reason"] = "main_unavailable"
                    self.log("未能确认大世界主界面，无法开始小游戏。")
                    return {"action": "stop", "success": False}
            if navigate:
                if self._already_near_npc(enforce_front_zone=True) and (
                    self._can_interact_with_npc()
                    or self._already_near_npc(max_radius_px=1.0, enforce_front_zone=True)
                ):
                    self.log("检测到已位于「穿梭大冒险」NPC 交互范围内，直接开始挑战。")
                else:
                    need_route = True
            elif not self._verify_near_npc():
                stats["stop_reason"] = "not_near_npc"
                self.log(
                    "未检测到角色位于花愿镇「穿梭大冒险」NPC 附近，已停止任务；"
                    "请将「起点方式」改为自动传送并前往 NPC，或手动到达 NPC 旁后再试。"
                )
                return {"action": "stop", "success": False}

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

    def _wait_for_npc_interact(self, timeout_s: float = 1.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while not self.stopped:
            if self._can_interact_with_npc():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if not self.pause(min(0.25, remaining)):
                return False
        return False

    def _verify_post_route(self, inputs: ForegroundInput, nav_ok: bool) -> bool:
        if nav_ok:
            if self._wait_for_npc_interact(timeout_s=1.0):
                return True
            if self.stopped:
                return False
            if self._already_near_npc(
                max_radius_px=POST_ROUTE_NPC_RADIUS_PX,
                enforce_front_zone=True,
            ):
                self.log("未直接识别到交互图标，但已确认位于「奇米亚」正面交互区内，继续尝试对话。", detail=True)
                return True
            self.log("到达路线终点后未确认位于「奇米亚」正面交互范围内，任务停止。")
            return False
        if (
            self._dismiss_npc_dialogue(inputs)
            and self.main()
            and (
                (
                    self._already_near_npc(max_radius_px=3.5, enforce_front_zone=True)
                    and self._can_interact_with_npc()
                )
                or self._already_near_npc(max_radius_px=1.2, enforce_front_zone=True)
            )
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
        camera_assist: bool,
        retry_started_at: float | None,
        stats: dict,
        started_at: float,
    ) -> bool:
        consecutive_failures = 0
        consecutive_overworld_returns = 0
        assist_label = "已开启视角水平辅助" if camera_assist else "已关闭视角水平辅助"
        self.log(
            f"开始「穿梭大冒险」自动刷噗灵（目标 {max_rounds} 局，走法：{assist_label}，"
            f"预计耗时约 {int(max_rounds * 31.2 // 60)} 分钟）…"
        )
        if retry_started_at is None:
            entry_state = self._enter_via_npc_dialogue(inputs, stats)
            if entry_state == "stopped":
                stats["stop_reason"] = "stopped"
                return False
            if entry_state == "not_entered":
                stats["stop_reason"] = "dialogue_not_opened"
                return False
            if entry_state == "no_bling":
                stats["stop_reason"] = "insufficient_bling"
                return False
            if entry_state == "cap_reached":
                stats["stop_reason"] = "daily_cap_reached"
                self.log("检测到今日大世界噗灵获取已达上限，自动结束。")
                return not self.stopped

        try:
            while stats["completed_rounds"] < max_rounds:
                if self.stopped:
                    stats["stop_reason"] = "stopped"
                    break
                target_round = stats["completed_rounds"] + 1
                round_start = time.monotonic()
                if retry_started_at is None and (
                    target_round == 1 or target_round % 5 == 0 or target_round == max_rounds
                ):
                    settled_net = _calc_net_bling(stats, include_pending=False)
                    self.log(
                        f"正在进行「穿梭大冒险」第 {target_round}/{max_rounds} 局"
                        f"（已累计净收益约 {settled_net:,} 噗灵）…"
                    )

                current_retry_start = retry_started_at
                retry_started_at = None
                play_state = self._play_course(
                    inputs,
                    camera_assist=camera_assist,
                    retry_started_at=current_retry_start,
                    stats=stats,
                )
                if play_state == "stopped":
                    stats["stop_reason"] = "stopped"
                    break
                if play_state == "not_in_course":
                    _refund_unentered_entry(stats)
                    consecutive_overworld_returns += 1
                    near_npc = self._already_near_npc(
                        max_radius_px=AT_NPC_VERIFY_RADIUS_PX,
                        min_score=AT_NPC_VERIFY_MIN_SCORE,
                    )
                    if not near_npc:
                        stats["stop_reason"] = "not_near_npc"
                        self.log("检测到当前停留在非「穿梭大冒险」NPC 附近的大世界，未计入完成局数，任务停止。")
                        return False
                    if consecutive_overworld_returns >= MAX_CONSECUTIVE_OVERWORLD_RETURNS:
                        stats["stop_reason"] = "not_in_minigame"
                        self.log(
                            f"连续 {consecutive_overworld_returns} 轮未能进入小游戏挑战（仍停留在主界面），已停止任务以避免空跑。"
                        )
                        return False
                    self.log(
                        f"本轮未进入小游戏倒计时（仍处于大世界，{consecutive_overworld_returns}/{MAX_CONSECUTIVE_OVERWORLD_RETURNS}），"
                        "已拦截误跑并重新与 NPC 对话…"
                    )
                    entry_state = self._enter_via_npc_dialogue(inputs, stats)
                    if entry_state == "stopped":
                        stats["stop_reason"] = "stopped"
                        break
                    if entry_state == "not_entered":
                        stats["stop_reason"] = "dialogue_not_opened"
                        return False
                    if entry_state == "no_bling":
                        stats["stop_reason"] = "insufficient_bling"
                        return False
                    if entry_state == "cap_reached":
                        stats["stop_reason"] = "daily_cap_reached"
                        self.log(
                            f"检测到今日大世界噗灵获取已达上限（共完成 {stats['completed_rounds']} 局），自动结束。"
                        )
                        break
                    continue
                inputs.wait(5.0, restore=False)

                info = self._inspect_settlement()
                if info["state"] in ("failed", "retry_confirm"):
                    _settle_entry(stats)
                    stats["failed_rounds"] += 1
                    consecutive_failures += 1
                    consecutive_overworld_returns = 0
                    outcome, retry_started_at = self._handle_failure_retry(
                        inputs,
                        info,
                        target_round,
                        max_rounds,
                        consecutive_failures,
                        stats=stats,
                    )
                    if outcome == "retrying":
                        continue
                    stats["stop_reason"] = outcome
                    if outcome == "daily_cap_reached":
                        self.log(
                            f"检测到今日大世界噗灵获取已达上限（共完成 {stats['completed_rounds']} 局），自动结束。"
                        )
                        break
                    return False

                if info["state"] == "cap_reached":
                    _settle_entry(stats)
                    stats["completed_rounds"] += 1
                    consecutive_failures = 0
                    consecutive_overworld_returns = 0
                    stats["stop_reason"] = "daily_cap_reached"
                    self.log(f"检测到今日大世界噗灵获取已达上限（共完成 {stats['completed_rounds']} 局），自动结束。")
                    break
                if info["state"] == "no_bling":
                    _refund_unentered_entry(stats)
                    stats["stop_reason"] = "insufficient_bling"
                    self.log("当前噗灵不足以支付小游戏入场费，任务停止。")
                    self._exit_failure_to_main(inputs, info)
                    return False

                if info["state"] == "continue" and self._is_main_detected(info):
                    _refund_unentered_entry(stats)
                    consecutive_overworld_returns += 1
                    near_npc = self._already_near_npc(
                        max_radius_px=AT_NPC_VERIFY_RADIUS_PX,
                        min_score=AT_NPC_VERIFY_MIN_SCORE,
                        frame=info["frame"],
                    )
                    if not near_npc:
                        stats["stop_reason"] = "not_near_npc"
                        self.log("检测到当前停留在非「穿梭大冒险」NPC 附近的大世界，未计入完成局数，任务停止。")
                        return False
                    if consecutive_overworld_returns >= MAX_CONSECUTIVE_OVERWORLD_RETURNS:
                        stats["stop_reason"] = "not_in_minigame"
                        self.log(
                            f"连续 {consecutive_overworld_returns} 轮未能进入小游戏挑战（仍停留在主界面），已停止任务以避免空跑。"
                        )
                        return False
                    self.log(
                        f"本轮未确认进入小游戏结算（仍处于大世界，{consecutive_overworld_returns}/{MAX_CONSECUTIVE_OVERWORLD_RETURNS}），"
                        "不计入完成局数，正在重新与 NPC 对话…"
                    )
                    entry_state = self._enter_via_npc_dialogue(inputs, stats)
                    if entry_state == "stopped":
                        stats["stop_reason"] = "stopped"
                        break
                    if entry_state == "not_entered":
                        stats["stop_reason"] = "dialogue_not_opened"
                        return False
                    if entry_state == "no_bling":
                        stats["stop_reason"] = "insufficient_bling"
                        return False
                    if entry_state == "cap_reached":
                        stats["stop_reason"] = "daily_cap_reached"
                        self.log(
                            f"检测到今日大世界噗灵获取已达上限（共完成 {stats['completed_rounds']} 局），自动结束。"
                        )
                        break
                    continue

                stats["completed_rounds"] += 1
                _settle_entry(stats)
                consecutive_failures = 0
                returned_to_overworld = False
                if stats["completed_rounds"] < max_rounds:
                    if not self._run_sequence(inputs, REPLAY_SEQUENCE):
                        stats["stop_reason"] = "stopped"
                        break
                    post_info = self._inspect_settlement()
                    if post_info["state"] == "cap_reached":
                        stats["stop_reason"] = "daily_cap_reached"
                        self.log(
                            f"检测到今日大世界噗灵获取已达上限（共完成 {stats['completed_rounds']} 局），自动结束。"
                        )
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
                        consecutive_overworld_returns = 0
                        outcome, retry_started_at = self._handle_failure_retry(
                            inputs,
                            post_info,
                            stats["completed_rounds"] + 1,
                            max_rounds,
                            consecutive_failures,
                            stats=stats,
                            timed_out=True,
                        )
                        if outcome == "retrying":
                            continue
                        stats["stop_reason"] = outcome
                        if outcome == "daily_cap_reached":
                            self.log(
                                f"检测到今日大世界噗灵获取已达上限（共完成 {stats['completed_rounds']} 局），自动结束。"
                            )
                            break
                        return False
                    if post_info["state"] == "continue" and self._is_main_detected(post_info):
                        returned_to_overworld = True
                        consecutive_overworld_returns += 1
                        near_npc = self._already_near_npc(
                            max_radius_px=AT_NPC_VERIFY_RADIUS_PX,
                            min_score=AT_NPC_VERIFY_MIN_SCORE,
                            frame=post_info["frame"],
                        )
                        if not near_npc:
                            stats["completed_rounds"] = max(0, stats["completed_rounds"] - 1)
                            stats["stop_reason"] = "not_near_npc"
                            self.log(
                                "检测到返回大世界且不在花愿镇「穿梭大冒险」NPC 附近，已撤销本轮计数并停止任务。"
                            )
                            return False
                        if consecutive_overworld_returns >= MAX_CONSECUTIVE_OVERWORLD_RETURNS:
                            stats["completed_rounds"] = max(0, stats["completed_rounds"] - 1)
                            stats["stop_reason"] = "repeated_overworld_return"
                            self.log(
                                f"连续 {consecutive_overworld_returns} 轮在结算后退回大世界且未能保持连刷，已停止任务以避免空跑。"
                            )
                            return False
                        self.log("检测到返回大世界，正在重新与 NPC 对话继续挑战…")
                        entry_state = self._enter_via_npc_dialogue(inputs, stats)
                        if entry_state == "stopped":
                            stats["stop_reason"] = "stopped"
                            break
                        if entry_state == "not_entered":
                            stats["stop_reason"] = "dialogue_not_opened"
                            return False
                        if entry_state == "no_bling":
                            stats["stop_reason"] = "insufficient_bling"
                            return False
                        if entry_state == "cap_reached":
                            stats["stop_reason"] = "daily_cap_reached"
                            self.log(
                                f"检测到今日大世界噗灵获取已达上限（共完成 {stats['completed_rounds']} 局），自动结束。"
                            )
                            break
                    else:
                        _mark_entry_paid(stats)
                if not returned_to_overworld:
                    consecutive_overworld_returns = 0
                elapsed = time.monotonic() - round_start
                self.log(f"第 {stats['completed_rounds']}/{max_rounds} 局完成，耗时 {elapsed:.1f} 秒。", detail=True)

            if not self.stopped:
                end_info = self._inspect_settlement()
                if end_info["state"] in ("failed", "retry_confirm"):
                    self._exit_failure_to_main(inputs, end_info)
                elif not self._is_main_detected(end_info):
                    self.main()
        finally:
            self._log_run_summary(stats, started_at)
        return not self.stopped

    def run(self, settings: dict) -> bool:
        try:
            max_rounds = max(1, min(500, int(settings.get("rounds", 267))))
        except (TypeError, ValueError):
            max_rounds = 267
        raw_assist = settings.get("camera_assist", True)
        if isinstance(raw_assist, str):
            camera_assist = raw_assist.strip().lower() not in {"0", "false", "no", "off", "disabled"}
        else:
            camera_assist = bool(raw_assist)
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
            "retry_count": 0,
            "entries_paid": 0,
            "active_entry_paid": False,
            "timer_aligned_rounds": 0,
            "fallback_start_rounds": 0,
            "camera_assist": camera_assist,
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
                        camera_assist=camera_assist,
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
                            camera_assist=camera_assist,
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
                        camera_assist=camera_assist,
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
                wins = int(stats["completed_rounds"] or 0)
                losses = int(stats["failed_rounds"] or 0)
                retries = int(stats["retry_count"] or 0)
                paid = int(stats["entries_paid"] or 0)
                net_bling = _calc_net_bling(stats, include_pending=True)
                write_json(folder / f"{stamp}.json", {
                    "completed_rounds": wins,
                    "failed_rounds": losses,
                    "retry_count": retries,
                    "entries_paid": paid,
                    "net_bling": net_bling,
                    "timer_aligned_rounds": int(stats["timer_aligned_rounds"] or 0),
                    "fallback_start_rounds": int(stats["fallback_start_rounds"] or 0),
                    "camera_assist": stats["camera_assist"],
                    "target_rounds": max_rounds,
                    "navigate": navigate,
                    "background": allow_background,
                    "stop_reason": stats["stop_reason"],
                })
                if wins > 0 or losses > 0 or paid > 0:
                    detail_parts = [f"成功 {wins} 局"]
                    if losses:
                        detail_parts.append(f"失败 {losses} 次")
                    if retries:
                        detail_parts.append(f"重试 {retries} 次")
                    if net_bling <= 0:
                        detail_parts.append(f"净收益 {net_bling:,} 噗灵")
                    detail = " · ".join(detail_parts)
                    try_record_adventure_entry(
                        ROOT,
                        self,
                        category="minigame",
                        task="minigame_bling",
                        title="穿梭大冒险刷噗灵",
                        detail=detail,
                        runs=wins + losses,
                        items={"噗灵": net_bling} if net_bling > 0 else {},
                        meta={
                            "completed_rounds": wins,
                            "failed_rounds": losses,
                            "retry_count": retries,
                            "entries_paid": paid,
                            "net_bling": net_bling,
                        },
                        status=(
                            "completed"
                            if stats.get("stop_reason") in (None, "completed", "daily_cap_reached", "cap_reached")
                            else "partial"
                        ),
                    )
            except Exception:
                pass
