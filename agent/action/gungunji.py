"""On-demand Surprise-O-Matic (惊喜滚滚机) auto-pull action with bling safety guard."""
from __future__ import annotations

import re
from typing import Any

from maa.custom_action import CustomAction

from daily.runtime import Runtime, parameters
from registration import agent_action
from task_control import TaskStopped, task_time as time

# ---------------------------------------------------------------------------
# 1280x720 UI layout constants (calibrated against 3.0 Gungunji screenshots)
# ---------------------------------------------------------------------------
HEADER_ROI = [60, 10, 180, 48]
BLING_ROI = [1120, 10, 150, 42]
SINGLE_PULL_ROI = [280, 590, 220, 58]
SINGLE_COST_ROI = [330, 555, 135, 38]
TEN_PULL_ROI = [775, 590, 220, 58]
TEN_COST_ROI = [820, 555, 150, 38]
DAILY_REMAIN_ROI = [960, 662, 315, 42]
SKIP_ROI = [1120, 660, 155, 52]
CONTINUE_ROI = [1000, 662, 275, 48]
CONGRATS_ROI = [540, 155, 200, 60]
MODAL_ROI = [340, 175, 600, 380]
INTERACT_PROMPT_ROI = [660, 290, 190, 90]

# Safe blank point away from both 滚动一次 (x=388, y=617) and 滚动十次 (x=880, y=617).
SAFE_BLANK_CLICK = (640, 120)

DEFAULT_TEN_COST = 200_000
DEFAULT_SINGLE_COST = 20_000

_RE_HEADER = re.compile(r"惊喜滚滚机|滚滚机")
_RE_TEN_PULL = re.compile(r"滚动十次|十连|10\s*次")
_RE_SINGLE_PULL = re.compile(r"滚动一次|单抽|1\s*次")
_RE_FREE = re.compile(r"免费|免費")
_RE_DAILY_REMAIN = re.compile(r"今日剩[余餘]?滚动次数[：:]\s*(\d+)\s*/\s*(\d+)")
_RE_SKIP = re.compile(r"跳过|跳過")
_RE_CONTINUE = re.compile(r"空白[区區]域[继繼][续續]|点击空白|点[击擊]任意")
_RE_CONGRATS = re.compile(r"恭喜[获獲]得")
_RE_EMPTY_POOL = re.compile(r"已抽完|已集齐|已全部获得|奖池已空|暂无可抽取|已售罄")
_RE_INSUFFICIENT = re.compile(r"噗灵不足|噗靈不足|货币不足|余额不足")
_RE_CONFIRM = re.compile(r"^确认$|^確認$|^确定$|^知道了$")
_RE_INTERACT = re.compile(r"启动|啟動|惊喜滚滚机|滚滚机")


def _parse_bling(raw: str | None) -> int | None:
    """Parse an OCR'd bling counter string such as '1887626' or '125.4万'."""
    if not raw:
        return None
    cleaned = re.sub(r"[\s,，]", "", raw).upper().replace("O", "0")
    m = re.search(r"(\d+(?:\.\d+)?)\s*([万W]?)", cleaned)
    if not m:
        return None
    val = float(m.group(1))
    if m.group(2) in ("万", "W"):
        val *= 10_000
    return int(round(val))


def _box_center(box) -> tuple[int, int] | None:
    if not box or len(box) < 4:
        return None
    return int(box[0] + box[2] / 2), int(box[1] + box[3] / 2)


def _find_text_box(rt: Runtime, roi: list[int], pattern: re.Pattern[str], frame=None):
    res = rt.recognize("MaaNikki_Daily_OCR", {"roi": roi, "expected": pattern.pattern}, image=frame)
    if res and res.hit and getattr(res, "box", None):
        best = getattr(res, "best_result", None)
        return res.box, (getattr(best, "text", "") if best else "") or ""
    return None, ""


@agent_action("nikki.gungunji")
class GungunjiAction(CustomAction):
    """Run guarded repeated pulls on any Surprise-O-Matic (1.0 / 2.0 / 3.0)."""

    def _read_options(self, context) -> dict[str, Any]:
        mode_raw = str(parameters(context, "MaaNikki_Gungunji_PullMode").get("value", "ten")).lower()
        pull_mode = "single" if mode_raw == "single" else "ten"
        try:
            max_pulls = max(1, int(parameters(context, "MaaNikki_Gungunji_MaxPulls").get("value", 10)))
        except (TypeError, ValueError):
            max_pulls = 10
        try:
            reserve_bling = max(
                0,
                int(str(parameters(context, "MaaNikki_Gungunji_ReserveBling").get("value", 1_000_000)).replace(",", "")),
            )
        except (TypeError, ValueError):
            reserve_bling = 1_000_000
        return {
            "pull_mode": pull_mode,
            "max_pulls": max_pulls,
            "reserve_bling": reserve_bling,
            "default_cost": DEFAULT_SINGLE_COST if pull_mode == "single" else DEFAULT_TEN_COST,
        }

    def _read_bling(self, rt: Runtime, frame=None) -> int | None:
        raw = rt.text(BLING_ROI, expected=r"[\d,，.万W]+", image=frame)
        return _parse_bling(raw)

    def _read_pull_cost(self, rt: Runtime, pull_mode: str, default_cost: int, frame=None) -> int:
        roi = SINGLE_COST_ROI if pull_mode == "single" else TEN_COST_ROI
        raw = rt.text(roi, expected=".+", image=frame) or ""
        if _RE_FREE.search(raw):
            return 0
        parsed = _parse_bling(raw)
        if parsed is not None and 0 < parsed <= 500_000:
            return parsed
        return default_cost

    def _read_daily_remaining(self, rt: Runtime, frame=None) -> int | None:
        raw = rt.text(DAILY_REMAIN_ROI, expected=".+", image=frame) or ""
        m = _RE_DAILY_REMAIN.search(raw)
        if not m:
            return None
        return int(m.group(1))

    def _detect_pull_button(self, rt: Runtime, pull_mode: str, frame=None):
        roi = SINGLE_PULL_ROI if pull_mode == "single" else TEN_PULL_ROI
        pattern = _RE_SINGLE_PULL if pull_mode == "single" else _RE_TEN_PULL
        return _find_text_box(rt, roi, pattern, frame)

    def _is_main_screen(self, rt: Runtime, frame=None) -> bool:
        if _find_text_box(rt, HEADER_ROI, _RE_HEADER, frame)[0]:
            return bool(
                self._detect_pull_button(rt, "ten", frame)[0]
                or self._detect_pull_button(rt, "single", frame)[0]
            )
        return False

    def _ensure_machine_ui(self, rt: Runtime) -> bool:
        """Verify we are on the Gungunji screen; press interact once if standing in front."""
        frame = rt.capture()
        if self._is_main_screen(rt, frame):
            return True
        box, _ = _find_text_box(rt, INTERACT_PROMPT_ROI, _RE_INTERACT, frame)
        if box:
            rt.log("检测到滚滚机「启动」交互提示，正在进入抽取界面。")
            rt.key("interact", 0)
            for _ in range(16):
                if rt.stopped:
                    return False
                rt.pause(0.35)
                if self._is_main_screen(rt, rt.capture()):
                    return True
        return False

    def _detect_stop_condition(self, rt: Runtime, frame) -> str | None:
        modal_box, modal_txt = _find_text_box(rt, MODAL_ROI, _RE_INSUFFICIENT, frame)
        if modal_box:
            confirm_box, _ = _find_text_box(rt, MODAL_ROI, _RE_CONFIRM, frame)
            center = _box_center(confirm_box)
            if center:
                rt.action("Click", target=[center[0], center[1]])
                rt.pause(0.3)
            return f"噗灵不足（{modal_txt}）"
        empty_box, empty_txt = _find_text_box(rt, MODAL_ROI, _RE_EMPTY_POOL, frame)
        if empty_box:
            confirm_box, _ = _find_text_box(rt, MODAL_ROI, _RE_CONFIRM, frame)
            center = _box_center(confirm_box)
            if center:
                rt.action("Click", target=[center[0], center[1]])
                rt.pause(0.3)
            return f"奖池已抽空（{empty_txt}）"
        return None

    def _wait_and_clear_result(self, rt: Runtime, pull_mode: str) -> str:
        """Skip pull animation, dismiss all new-item/congrats screens, and return to main UI."""
        deadline = time.monotonic() + 30.0
        saw_transition = False
        while time.monotonic() < deadline and not rt.stopped:
            frame = rt.capture()
            stop_reason = self._detect_stop_condition(rt, frame)
            if stop_reason:
                return stop_reason

            # 1. Pull animation skip prompt (bottom-right: F 跳过)
            skip_box, _ = _find_text_box(rt, SKIP_ROI, _RE_SKIP, frame)
            if skip_box:
                saw_transition = True
                center = _box_center(skip_box)
                if center:
                    rt.action("Click", target=[center[0], center[1]])
                    rt.pause(0.45)
                    continue

            # 2. New item reveal / 恭喜获得 settlement (bottom-right: F / 空白区域继续)
            cont_box, _ = _find_text_box(rt, CONTINUE_ROI, _RE_CONTINUE, frame)
            congrats_box, _ = _find_text_box(rt, CONGRATS_ROI, _RE_CONGRATS, frame)
            if cont_box or congrats_box:
                saw_transition = True
                rt.action("Click", target=[SAFE_BLANK_CLICK[0], SAFE_BLANK_CLICK[1]])
                rt.pause(0.45)
                continue

            # 3. Returned to main Gungunji screen
            if self._is_main_screen(rt, frame):
                if saw_transition or time.monotonic() > deadline - 25.0:
                    return "ok"

            rt.pause(0.3)
        return "timeout"

    def run(self, context, argv):
        rt = Runtime(context)
        opts = self._read_options(context)
        pull_mode = opts["pull_mode"]
        max_pulls = opts["max_pulls"]
        reserve_bling = opts["reserve_bling"]
        default_cost = opts["default_cost"]
        mode_label = "单抽" if pull_mode == "single" else "十连"
        completed = 0
        total_spent = 0

        try:
            rt.log(
                f"滚滚机自动抽取开始：{mode_label}，最多 {max_pulls} 轮，"
                f"保留噗灵下限 {reserve_bling:,}。"
            )
            if not self._ensure_machine_ui(rt):
                rt.log("未识别到滚滚机抽取界面，请先站在滚滚机前（出现「启动」提示）或打开抽取界面后再开始。")
                return False

            for round_idx in range(1, max_pulls + 1):
                if rt.stopped:
                    break
                frame = rt.capture()
                stop_reason = self._detect_stop_condition(rt, frame)
                if stop_reason:
                    rt.log(f"滚滚机抽取停止：{stop_reason}（已完成 {completed} 轮{mode_label}）。")
                    break

                daily_rem = self._read_daily_remaining(rt, frame)
                if daily_rem is not None:
                    need_rolls = 1 if pull_mode == "single" else 10
                    if daily_rem < need_rolls:
                        rt.log(f"今日剩余滚动次数（{daily_rem}）不足本次{mode_label}，已自动停止。")
                        break

                pull_cost = self._read_pull_cost(rt, pull_mode, default_cost, frame)
                bling = self._read_bling(rt, frame)
                if bling is not None and pull_cost > 0:
                    if bling < pull_cost:
                        rt.log(f"当前噗灵（{bling:,}）不足本次{mode_label}（需 {pull_cost:,}），已停止。")
                        break
                    if reserve_bling > 0 and bling - pull_cost < reserve_bling:
                        rt.log(
                            f"当前噗灵（{bling:,}）扣除本次{mode_label}（{pull_cost:,}）后将低于保留下限"
                            f"（{reserve_bling:,}），已安全停止。"
                        )
                        break

                btn_box, _ = self._detect_pull_button(rt, pull_mode, frame)
                center = _box_center(btn_box)
                if center is None:
                    rt.log(f"第 {round_idx} 轮未找到「{'滚动一次' if pull_mode == 'single' else '滚动十次'}」按钮，已停止抽取。")
                    break

                cost_note = "免费" if pull_cost == 0 else f"消耗 {pull_cost:,}"
                rt.log(
                    f"滚滚机第 {round_idx}/{max_pulls} 轮{mode_label}（{cost_note}"
                    + (f"，当前噗灵 {bling:,}" if bling is not None else "")
                    + "）。"
                )
                rt.action("Click", target=[center[0], center[1]])
                rt.pause(0.55)

                outcome = self._wait_and_clear_result(rt, pull_mode)
                if outcome == "ok":
                    completed += 1
                    total_spent += pull_cost
                    continue
                if outcome == "timeout":
                    rt.log(f"第 {round_idx} 轮等待动画或结算超时，为避免误操作已停止。")
                    return completed > 0
                rt.log(f"滚滚机抽取停止：{outcome}（已完成 {completed} 轮{mode_label}）。")
                break

            rt.log(f"滚滚机自动抽取结束：共完成 {completed} 轮{mode_label}，预计消耗 {total_spent:,} 噗灵。")
            return True
        except TaskStopped:
            rt.log(f"滚滚机自动抽取已手动停止（已完成 {completed} 轮{mode_label}）。")
            raise
        except Exception as error:
            if not rt.stopped:
                rt.diagnostic("gungunji", str(error))
            rt.log(f"滚滚机自动抽取异常中断：{error}（已完成 {completed} 轮{mode_label}）。")
            return False
