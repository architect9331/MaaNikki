from __future__ import annotations

from file_io import write_json

import ctypes
from ctypes import wintypes
from datetime import datetime
import os
import re
import time as wall_time

from maa.custom_action import CustomAction
from client_config import parse_custom_param
from registration import agent_action
from task_control import GameWindowClosed, TaskStopped, active_session, task_time as time
from daily.runtime import Executors, ROOT
from daily.ui import find_expired_notice_confirm_box
from game_view import ALLOWED_FRAME_SIZES, GameFrameSizeError, is_clipped_game_frame, require_game_frame
from navigation.controller import is_window_unresponsive, probe_window_responsiveness


STARTUP_HUNG_TIMEOUT = 180.0
STARTUP_LOADING_HUNG_TIMEOUT = 240.0
ACCOUNT_PROMPT_HOLD_SECONDS = 8.0
ACCOUNT_MODAL_HOLD_SECONDS = 1.5
_STARTUP_OCR_ROI = [150, 25, 980, 690]
_RE_WELCOME = re.compile(r"欢迎[进進]入游[戏戲]|正在[登登][录錄]")
_RE_UPDATE_TITLE = re.compile(r"发现新版本|發現新版本")
_RE_UPDATE_BODY = re.compile(r"资源文件.*更新|資源文件.*更新|是否更新资源|是否更新資源")
_RE_RESTART_TEXT = re.compile(r"更新完成.*请重启|更新完成.*請重啟")
_RE_CONFIRM_BTN = re.compile(r"^确认$|^確認$")
_RE_ENTER = re.compile(r"点击.*进入游戏|点击.*开始游戏|[Tt]ap.*[Ee]nter|[Cc]lick.*[Ee]nter")
_RE_LOGIN = re.compile(r"^登录$|^登錄$")
_RE_ACCOUNT_MODAL = re.compile(
    r"验证码登录|密码登录|获取验证码|扫码登录|游戏APP.*扫码|驗證碼登錄|密碼登錄|獲取驗證碼|掃碼登錄"
)
_RE_ACCOUNT_PROMPT = re.compile(r"注册\s*/\s*登录|註冊\s*/\s*登錄|" + _RE_ACCOUNT_MODAL.pattern)
_RE_UPDATING = re.compile(
    r"热更新|熱更新|正在更新|资源更新|資源更新|检查更新|檢查更新|正在下载|正在下載|正在校验|正在校驗|正在解压|正在解壓|着色器"
)
_RE_VERSION_CHECK = re.compile(r"获取资源版本|獲取資源版本|资源版本信息|資源版本信息")
_RE_LOADING = re.compile(r"正在[进進]入奇[迹跡]大[陆陸]")
_RE_INTRO = re.compile(
    r"健康游戏忠告|抵制不良游戏|警告.*游戏前详阅|健康遊戲忠告|抵制不良遊戲|警告.*遊戲前詳閱|叠纸|疊紙|[Ii]nfold"
)


def _inspect_startup_screen(runtime, frame) -> dict:
    """Classify a startup frame in at most one OCR pass, checking MainDetected first."""
    state = {
        "main": False,
        "reward": False,
        "restart_box": None,
        "update_dialog": False,
        "update_confirm_box": None,
        "notice_confirm_box": None,
        "enter_box": None,
        "login_box": None,
        "account_prompt": False,
        "account_modal": False,
        "welcome_banner": False,
        "updating": False,
        "version_check": False,
        "intro": False,
        "loading": False,
    }
    if frame is None:
        return state
    if runtime.hit("MaaNikki_MainDetected", image=frame):
        state["main"] = True
        return state
    if runtime.hit("MaaNikki_RewardLayer", image=frame):
        state["reward"] = True
        return state
    ocr = runtime.recognize(
        "MaaNikki_Daily_OCR",
        {"roi": _STARTUP_OCR_ROI, "expected": ".+", "threshold": 0.4},
        image=frame,
    )
    rows = (
        (getattr(ocr, "all_results", None) or getattr(ocr, "filtered_results", None) or [getattr(ocr, "best_result", None)])
        if ocr
        else []
    )
    has_update_title = False
    has_update_body = False
    update_btn = None
    has_restart_text = False
    restart_btn = None
    for row in rows:
        if row is None:
            continue
        raw_text = str(getattr(row, "text", "") or "")
        clean = raw_text.strip()
        if not clean:
            continue
        score = getattr(row, "score", 1.0)
        if isinstance(score, (int, float)) and score < 0.4:
            continue
        box = getattr(row, "box", None)
        if not box or len(box) < 4:
            continue
        bx, by, bw, bh = int(box[0]), int(box[1]), int(box[2]), int(box[3])
        cx, cy = bx + bw * 0.5, by + bh * 0.5
        if cy <= 130 and _RE_WELCOME.search(clean):
            state["welcome_banner"] = True
        if 210 <= cy <= 310 and 440 <= cx <= 840 and _RE_UPDATE_TITLE.search(clean):
            has_update_title = True
        if 285 <= cy <= 435 and 440 <= cx <= 840 and _RE_UPDATE_BODY.search(clean):
            has_update_body = True
        if 415 <= cy <= 515 and _RE_CONFIRM_BTN.search(clean):
            if 630 <= cx <= 860 and update_btn is None:
                update_btn = [bx, by, bw, bh]
            if 525 <= cx <= 755 and restart_btn is None:
                restart_btn = [bx, by, bw, bh]
        if 315 <= cy <= 405 and 455 <= cx <= 825 and _RE_RESTART_TEXT.search(clean):
            has_restart_text = True
        if 345 <= cy <= 710 and state["enter_box"] is None and _RE_ENTER.search(clean):
            state["enter_box"] = [bx, by, bw, bh]
        if 325 <= cy <= 705 and 310 <= cx <= 970 and state["login_box"] is None and _RE_LOGIN.search(clean):
            state["login_box"] = [bx, by, bw, bh]
        if 130 <= cy <= 680:
            if _RE_ACCOUNT_MODAL.search(clean):
                state["account_modal"] = True
                state["account_prompt"] = True
            elif _RE_ACCOUNT_PROMPT.search(clean):
                state["account_prompt"] = True
        if cy >= 540:
            if _RE_UPDATING.search(clean):
                state["updating"] = True
            if _RE_VERSION_CHECK.search(clean):
                state["version_check"] = True
            if _RE_LOADING.search(clean):
                state["loading"] = True
        if 165 <= cy <= 625 and _RE_INTRO.search(clean):
            state["intro"] = True
    if has_restart_text and restart_btn is not None:
        state["restart_box"] = restart_btn
    if has_update_title:
        state["update_dialog"] = True
        if has_update_body and update_btn is not None:
            state["update_confirm_box"] = update_btn
    if state["restart_box"] is None and not state["update_dialog"]:
        state["notice_confirm_box"] = find_expired_notice_confirm_box(rows)
    return state
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_U32 = None
_K32 = None


def _startup_user32():
    global _U32
    if os.name != "nt":
        return None
    if _U32 is None:
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        u32.IsWindow.argtypes = [wintypes.HWND]
        u32.IsWindow.restype = wintypes.BOOL
        u32.IsHungAppWindow.argtypes = [wintypes.HWND]
        u32.IsHungAppWindow.restype = wintypes.BOOL
        u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u32.GetWindowThreadProcessId.restype = wintypes.DWORD
        u32.SendMessageTimeoutW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        u32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
        _U32 = u32
    return _U32


def _startup_kernel32():
    global _K32
    if os.name != "nt":
        return None
    if _K32 is None:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.GetExitCodeProcess.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        _K32 = k32
    return _K32


def _is_window_process_alive(u32, hwnd) -> bool:
    if u32 is None or not hwnd or not u32.IsWindow(hwnd):
        return False
    k32 = _startup_kernel32()
    if k32 is None:
        return True
    pid = wintypes.DWORD(0)
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return True
    proc = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not proc:
        return bool(u32.IsWindow(hwnd))
    try:
        code = wintypes.DWORD(0)
        if k32.GetExitCodeProcess(proc, ctypes.byref(code)):
            return code.value == _STILL_ACTIVE and bool(u32.IsWindow(hwnd))
        return bool(u32.IsWindow(hwnd))
    finally:
        k32.CloseHandle(proc)


def _is_window_unresponsive(u32, hwnd) -> bool:
    if u32 is None or not hwnd:
        return False
    return is_window_unresponsive(u32, hwnd)


@agent_action("nikki.start_game", stop_on_failure=True)
class StartGameAction(CustomAction):
    def run(self, context, argv):
        self.restart_requested = False
        self.window_closed = False
        self.elapsed_seconds = 0.0
        self.stop_reason = None
        wall_started_at = wall_time.monotonic()
        try:
            return self._run_startup(context, argv, wall_started_at)
        except GameWindowClosed as error:
            self.window_closed = True
            self.elapsed_seconds = max(self.elapsed_seconds, wall_time.monotonic() - wall_started_at)
            if not self.stop_reason:
                self.stop_reason = str(error) or "连接的游戏窗口已经关闭，请重新连接。"
            return False
        except TaskStopped as error:
            self.elapsed_seconds = max(self.elapsed_seconds, wall_time.monotonic() - wall_started_at)
            reason = str(error).strip()
            if not self.stop_reason and reason and reason != "任务已停止。":
                self.stop_reason = reason
            raise

    def _run_startup(self, context, argv, wall_started_at):
        runtime = Executors(context)
        options = parse_custom_param(argv.custom_action_param)
        raw_timeout = options.get("timeout", 900)
        timeout = (
            900
            if raw_timeout is None or (isinstance(raw_timeout, str) and not raw_timeout.strip())
            else int(raw_timeout)
        )
        bootstrap = options.get("bootstrap") is True
        already_running = options.get("already_running") is True
        min_timeout = 1 if bootstrap else 30
        if not min_timeout <= timeout <= 900:
            self.stop_reason = "启动等待上限应在30至900秒之间。"
            runtime.log(self.stop_reason)
            context.tasker.post_stop()
            return False

        u32 = _startup_user32()
        session = active_session()
        orig_session_wait = session.wait if session is not None else None
        orig_adapter_hung = None
        if session is not None:
            if session.adapter is None:
                session.ensure_adapter()
            orig_adapter_hung = session.adapter.hung
        orig_check_focus = runtime.check_focus
        in_hung_guard = False
        wait_entered_at = None
        startup_state = {"loading_active": False}

        def _ensure_session_adapter():
            nonlocal orig_adapter_hung, startup_adapter_hung
            cur = active_session()
            if cur is None:
                return None, None
            if cur.adapter is None:
                cur.ensure_adapter()
                if cur is session and orig_adapter_hung is None:
                    orig_adapter_hung = cur.adapter.hung
                    startup_adapter_hung = _wrap_hung_fn(orig_adapter_hung, cur.adapter)
                    cur.adapter.hung = startup_adapter_hung
            return cur, cur.adapter

        def _hung_budget_cap() -> float:
            return STARTUP_LOADING_HUNG_TIMEOUT if startup_state["loading_active"] else STARTUP_HUNG_TIMEOUT

        def wait_startup_responsive():
            nonlocal in_hung_guard
            if in_hung_guard or u32 is None:
                return
            cur, adapter = _ensure_session_adapter()
            if cur is None or cur._background_observation or adapter is None or not adapter.hwnd:
                return
            hwnd = adapter.hwnd
            if not _is_window_process_alive(u32, hwnd):
                cur.window_closed()
            if not _is_window_unresponsive(u32, hwnd):
                return
            in_hung_guard = True
            hung_begin = wall_time.monotonic()
            remaining_budget = (wall_started_at + timeout + cur.paused_seconds) - hung_begin
            max_wait = min(_hung_budget_cap(), max(20.0, remaining_budget))
            try:
                for inputs in list(cur.inputs):
                    inputs.release()
                logged_stall = False
                while _is_window_unresponsive(u32, hwnd):
                    if cur.stopped() or context.tasker.stopping:
                        raise TaskStopped("任务已停止。")
                    if not _is_window_process_alive(u32, hwnd):
                        cur.window_closed()
                    elapsed_hung = wall_time.monotonic() - hung_begin
                    if not logged_stall and elapsed_hung >= 5.0:
                        logged_stall = True
                        runtime.log("游戏窗口正在加载资源（暂时未响应），继续等待恢复。", detail=True)
                    if elapsed_hung >= max_wait:
                        probe = probe_window_responsiveness(u32, hwnd, force_probe=True)
                        if probe["unresponsive"]:
                            probe["waited_seconds"] = round(elapsed_hung, 2)
                            probe["timeout_seconds"] = round(max_wait, 2)
                            probe["startup_loading"] = bool(startup_state["loading_active"])
                            cur.window_hung(details=probe)
                        break
                    wall_time.sleep(.2)
                cur.bump_generation(paused=True)
                runtime.invalidate_frame()
            finally:
                in_hung_guard = False

        def _wrap_hung_fn(raw_hung_fn, adapter_obj):
            def _wrapped_hung():
                nonlocal in_hung_guard
                if raw_hung_fn is None or not raw_hung_fn():
                    return False
                if session is None or session._background_observation or in_hung_guard or u32 is None:
                    return True
                hwnd = getattr(adapter_obj, "hwnd", 0) if adapter_obj is not None else 0
                if not hwnd:
                    return True
                in_hung_guard = True
                hung_begin = wall_time.monotonic()
                in_flight_pause = (
                    max(0.0, hung_begin - wait_entered_at)
                    if session.waiting and wait_entered_at is not None
                    else 0.0
                )
                remaining_budget = (
                    wall_started_at + timeout + session.paused_seconds + in_flight_pause
                ) - hung_begin
                max_wait = min(_hung_budget_cap(), max(20.0, remaining_budget))
                try:
                    for inputs in list(session.inputs):
                        inputs.release()
                    logged_stall = False
                    while raw_hung_fn():
                        if session.stopped() or context.tasker.stopping:
                            raise TaskStopped("任务已停止。")
                        if not _is_window_process_alive(u32, hwnd):
                            session.window_closed()
                        elapsed_hung = wall_time.monotonic() - hung_begin
                        if not logged_stall and elapsed_hung >= 5.0:
                            logged_stall = True
                            runtime.log("游戏窗口正在加载资源（暂时未响应），继续等待恢复。", detail=True)
                        if elapsed_hung >= max_wait:
                            details = (
                                adapter_obj.hung_details()
                                if hasattr(adapter_obj, "hung_details")
                                else {}
                            )
                            details["waited_seconds"] = round(elapsed_hung, 2)
                            details["timeout_seconds"] = round(max_wait, 2)
                            details["startup_loading"] = bool(startup_state["loading_active"])
                            session.window_hung(details=details)
                        wall_time.sleep(.2)
                    session.bump_generation(paused=True)
                    runtime.invalidate_frame()
                    return False
                finally:
                    in_hung_guard = False
            return _wrapped_hung

        startup_adapter_hung = (
            _wrap_hung_fn(orig_adapter_hung, session.adapter)
            if session is not None and session.adapter is not None and orig_adapter_hung is not None
            else None
        )

        def startup_session_wait(adapter_arg=None, *, require_foreground=False, require_capturable=False):
            nonlocal wait_entered_at
            if session is not None and not session.waiting:
                wait_startup_responsive()
            custom_orig_hung = None
            if (
                session is not None
                and adapter_arg is not None
                and adapter_arg is not session.adapter
                and hasattr(adapter_arg, "hung")
            ):
                custom_orig_hung = adapter_arg.hung
                adapter_arg.hung = _wrap_hung_fn(custom_orig_hung, adapter_arg)
            entered_outer = wait_entered_at is None
            if entered_outer:
                wait_entered_at = wall_time.monotonic()
            try:
                return orig_session_wait(
                    adapter_arg,
                    require_foreground=require_foreground,
                    require_capturable=require_capturable,
                )
            finally:
                if entered_outer:
                    wait_entered_at = None
                if custom_orig_hung is not None:
                    adapter_arg.hung = custom_orig_hung

        def startup_check_focus():
            wait_startup_responsive()
            return orig_check_focus()

        def startup_pause(seconds):
            runtime.invalidate_frame()
            wait_startup_responsive()
            orig_check_focus()
            pause_end = time.monotonic() + seconds
            while not runtime.stopped and time.monotonic() < pause_end:
                if runtime.navigation_inputs:
                    runtime.navigation_inputs.check()
                wait_startup_responsive()
                orig_check_focus()
                remaining_sleep = pause_end - time.monotonic()
                if remaining_sleep > 0:
                    wall_time.sleep(min(0.1, remaining_sleep))
            wait_startup_responsive()
            return not runtime.stopped

        runtime.check_focus = startup_check_focus
        runtime.pause = startup_pause
        if session is not None and orig_session_wait is not None:
            session.wait = startup_session_wait
        if session is not None and session.adapter is not None and orig_adapter_hung is not None:
            session.adapter.hung = startup_adapter_hung
        try:
            return self._run_startup_loop(
                context, options, runtime, timeout, bootstrap, already_running, startup_state
            )
        finally:
            if session is not None and orig_session_wait is not None:
                session.wait = orig_session_wait
            if session is not None and session.adapter is not None and orig_adapter_hung is not None:
                session.adapter.hung = orig_adapter_hung

    def _run_startup_loop(
        self, context, options, runtime, timeout, bootstrap, already_running, startup_state=None
    ):
        started_at = time.monotonic()
        deadline = started_at+timeout
        entered, logged_in, update_attempts, notice_attempts = 0, 0, 0, 0
        update_waiting = False
        notice_logged = False
        waiting_logged = False
        intro_logged = False
        loading_logged = False
        auto_login_logged = False
        version_check_logged = False
        unknown_since = None
        transition_grace_until = 0.0
        account_prompt_since = None
        account_prompt_hits = 0
        account_prompt_modal = False

        def incomplete(reason="尚未完成进入游戏"):
            self.stop_reason = reason
            if not runtime.stopped:
                folder = ROOT / "logs/startup"
                folder.mkdir(parents=True, exist_ok=True)
                name = datetime.now().strftime("%Y%m%d-%H%M%S-%f")+"-enter"
                record = {
                    "stage": "enter_game", "success": False, "reason": reason,
                    "enter_attempts": entered, "login_attempts": logged_in,
                    "update_attempts": update_attempts,
                }
                captured = runtime.controller.post_screencap().wait()
                image = captured.get() if captured.succeeded else None
                record["evidence"] = runtime.diagnostic("startup", reason, image=image, details=record)
                write_json(folder / (name+".json"), record)
                runtime.log(f"{reason}，本轮停止；记录：logs/startup/{name}.json。")
                context.tasker.post_stop()
            return False

        def complete():
            if bootstrap:
                self.elapsed_seconds = time.monotonic()-started_at
                return True
            # Read only after the ordinary HUD has been established by the
            # startup flow. Bind directly without repeating main() confirmation.
            try:
                from account_state import AccountState
                uid = runtime.read_account_uid(runtime.frame())
                if uid is None and runtime.pause(.15):
                    uid = runtime.read_account_uid(runtime.capture())
                if uid:
                    AccountState.bind(ROOT, uid)
                    runtime._account_uid = uid
                    runtime._account_generation = runtime._focus_generation
                    runtime._account_pause_generation = runtime._pause_generation
                    session = active_session()
                    if session is not None:
                        session.account_uid = uid
                        session.account_generation = session.generation
                        session.account_pause_generation = session.pause_generation
            except TaskStopped:
                raise
            except (OSError, ValueError) as error:
                # Startup also serves tasks which do not persist role data.
                # Account-bound tasks confirm identity at their own entrance.
                runtime.log(f"账号识别待重试：{error}。", detail=True)
            from daily.run_state import next_page
            if next_page("MaaNikki_StartGame") == "calendar":
                _, finished = runtime.finish("MaaNikki_StartGame")
                return finished
            return not runtime.stopped

        def confirm_main_fast():
            if not runtime.pause(.12):
                return False
            frame2 = runtime.capture()
            if frame2 is None or not runtime.hit("MaaNikki_MainDetected", image=frame2):
                return False
            if runtime.hit("MaaNikki_RewardLayer", image=frame2):
                return False
            return True

        def click_box_and_wait_exit(box, kind):
            nonlocal entered, logged_in, transition_grace_until, unknown_since
            if runtime.stopped or time.monotonic() >= deadline:
                return False
            if kind == "enter":
                entered += 1
                runtime.log(f"点击进入游戏（{entered}/3）。", detail=True)
            else:
                logged_in += 1
                runtime.log(f"点击登录（{logged_in}/3）。", detail=True)
            pt = [box[0] + box[2] // 2, box[1] + box[3] // 2]
            if not runtime.action("Click", ui_click=False, target=pt):
                return False
            wait_end = min(deadline, time.monotonic() + 2.6)
            absent_streak = 0
            while not runtime.stopped and time.monotonic() < wait_end:
                if not runtime.pause(.2):
                    return False
                nxt = runtime.capture()
                if nxt is None:
                    return False
                st = _inspect_startup_screen(runtime, nxt)
                still_here = bool(st["enter_box"] if kind == "enter" else st["login_box"])
                progressed = bool(
                    st["main"]
                    or st["reward"]
                    or st["restart_box"]
                    or st["update_dialog"]
                    or st["loading"]
                    or st["updating"]
                    or st["version_check"]
                    or (kind == "login" and st["enter_box"])
                    or runtime.ui.asset("IconUILoading", image=nxt, threshold=.99)
                )
                if not still_here and progressed:
                    unknown_since = None
                    transition_grace_until = min(deadline, time.monotonic() + 25.0)
                    return True
                if not still_here:
                    absent_streak += 1
                    if absent_streak >= 2:
                        unknown_since = None
                        transition_grace_until = min(deadline, time.monotonic() + 25.0)
                        return True
                else:
                    absent_streak = 0
            return False

        def confirm_update(box):
            nonlocal update_attempts
            if update_attempts >= 3:
                return False
            update_attempts += 1
            pt = [box[0] + box[2] // 2, box[1] + box[3] // 2]
            if not runtime.action("Click", ui_click=False, target=pt):
                return False
            wait_end = min(deadline, time.monotonic() + 4.0)
            while not runtime.stopped and time.monotonic() < wait_end:
                if not runtime.pause(.25):
                    return False
                nxt = runtime.capture()
                if nxt is None:
                    return False
                st = _inspect_startup_screen(runtime, nxt)
                if not st["update_confirm_box"]:
                    return True
            return False

        def dismiss_notice(box):
            nonlocal notice_attempts, notice_logged, unknown_since
            if notice_attempts >= 5:
                return False
            notice_attempts += 1
            if not notice_logged:
                runtime.log("检测到道具过期提示弹窗，正在点击确认关闭。")
                notice_logged = True
            if not runtime.ui.click_box(box):
                return False
            wait_end = min(deadline, time.monotonic() + 3.5)
            while not runtime.stopped and time.monotonic() < wait_end:
                if not runtime.pause(.25):
                    return False
                nxt = runtime.capture()
                if nxt is None:
                    return False
                st = _inspect_startup_screen(runtime, nxt)
                if not st["notice_confirm_box"]:
                    unknown_since = None
                    return True
            return False

        # The splash or health warning can open at 16:9 or the launcher's aspect
        # ratio and transiently resize while Unreal applies saved display settings.
        # Guard captures across this initial window; when attaching to an already
        # running game window, allow only a brief settle window for focus/restore.
        frame_wait_seconds = 3 if already_running else 30
        frame_deadline = min(deadline, started_at + frame_wait_seconds)
        frame_wait_logged = False
        mismatch_since = None

        def capture_startup_frame():
            nonlocal frame_wait_logged, mismatch_since, unknown_since
            while not runtime.stopped:
                runtime.invalidate_frame()
                runtime.check_focus()
                captured = runtime.controller.post_screencap().wait()
                image = captured.get() if captured.succeeded else None
                if runtime.check_focus():
                    continue
                if image is None or image.size == 0:
                    return None
                ready_to_adjust = False
                if (image.shape[1], image.shape[0]) not in ALLOWED_FRAME_SIZES or is_clipped_game_frame(image):
                    now_mono = time.monotonic()
                    ready_to_adjust = (
                        already_running
                        or not bootstrap
                        or entered > 0
                        or logged_in > 0
                        or update_attempts > 0
                        or now_mono >= started_at + 6.0
                        or (
                            now_mono >= started_at + 2.5
                            and float(image.mean()) > 8.0
                            and float(image.std()) > 5.0
                        )
                    )
                    if ready_to_adjust:
                        from tall_window import ensure_16_9_window
                        adjusted = ensure_16_9_window(
                            runtime.controller,
                            context=context,
                            notify=runtime.log,
                            write_log=False,
                        )
                    else:
                        from tall_window import heal_16_9_window
                        adjusted = heal_16_9_window(runtime.controller)
                    if adjusted is not None:
                        image = adjusted
                try:
                    require_game_frame(image)
                except GameFrameSizeError as error:
                    now_err = time.monotonic()
                    if mismatch_since is None:
                        mismatch_since = now_err
                    # Unreal can re-apply saved windowed dimensions right after login or
                    # clicking Enter Game, racing the first borderless resize attempt.
                    # Give active transitions a bounded retry window before failing.
                    from tall_window import is_resolution_adjustment_blocked
                    retry_deadline = min(deadline, max(frame_deadline, mismatch_since + 6.0))
                    if now_err >= retry_deadline or (ready_to_adjust and is_resolution_adjustment_blocked()):
                        self.stop_reason = str(error)
                        runtime.evidence.remember_frame(image, runtime._focus_generation)
                        runtime.validate_frame(image)
                    unknown_since = None
                    if not frame_wait_logged:
                        runtime.log("正在等待游戏启动画面切换至 16:9 分辨率。")
                        frame_wait_logged = True
                    if not runtime.pause(.25):
                        return None
                    continue
                mismatch_since = None
                runtime.evidence.remember_frame(image, runtime._focus_generation)
                runtime.validate_frame(image)
                return image
            return None

        runtime.capture = capture_startup_frame
        initial_frame = runtime.capture()
        if initial_frame is None:
            return False if runtime.stopped else incomplete("未获取到游戏启动画面")
        if not already_running and bootstrap:
            transition_grace_until = min(deadline, started_at + 18.0)

        next_frame = initial_frame
        while not runtime.stopped and time.monotonic() < deadline:
            cur_frame = next_frame if next_frame is not None else runtime.capture()
            next_frame = None
            if cur_frame is None:
                return False if runtime.stopped else incomplete("未获取到游戏启动画面")
            st = _inspect_startup_screen(runtime, cur_frame)
            restart_box = st["restart_box"]
            restart = restart_box is not None
            update = not restart and st["update_dialog"]
            update_confirm_box = st["update_confirm_box"] if update else None
            notice_confirm_box = None if (restart or update) else st["notice_confirm_box"]
            notice = notice_confirm_box is not None
            reward = not restart and not update and not notice and st["reward"]
            main = not restart and not update and not notice and not reward and st["main"]
            enter_box = None if (restart or update or notice or reward or main) else st["enter_box"]
            enter = enter_box is not None
            login_box = None if (restart or update or notice or reward or main or enter) else st["login_box"]
            login = login_box is not None
            account_prompt = (
                not restart and not update and not notice and not reward and not main
                and not enter and not login and st["account_prompt"]
            )
            account_modal = account_prompt and st["account_modal"]
            welcome_banner = account_prompt and not account_modal and st["welcome_banner"]
            updating = (
                not restart and not update and not notice and not reward and not main
                and not enter and not login and not account_prompt and st["updating"]
            )
            intro = (
                not restart and not update and not notice and not reward and not main
                and not enter and not login and not account_prompt and not updating and st["intro"]
            )
            loading = (
                not restart and not update and not notice and not reward and not main
                and not enter and not login and not account_prompt and not updating
                and not intro and st["loading"]
            )
            version_check = (
                not restart and not update and not notice and not reward and not main
                and not enter and not login
                and (not account_prompt or not account_modal)
                and not updating and not intro and not loading and st["version_check"]
            )
            if (restart or update or notice or reward or main or enter or login
                    or account_prompt or updating or intro or loading or version_check):
                unknown_since = None
            if loading or updating or version_check:
                transition_grace_until = min(deadline, max(transition_grace_until, time.monotonic() + 20.0))
            if not account_prompt or welcome_banner or version_check:
                account_prompt_since = None
                account_prompt_hits = 0
                account_prompt_modal = False
            if startup_state is not None:
                startup_state["loading_active"] = bool(
                    loading or updating or update or update_waiting
                )

            if restart:
                if not bootstrap or options.get("allow_restart", True) is not True:
                    return incomplete("更新需要重启，请重新运行启动游戏任务")
                runtime.log("资源更新完成，正在确认重启并等待新的游戏窗口。")
                point = [restart_box[0]+restart_box[2]//2, restart_box[1]+restart_box[3]//2]
                if not runtime.action("TouchMove", target=point) or not runtime.pause(.15):
                    return incomplete("未能点击更新重启确认")
                recheck = _inspect_startup_screen(runtime, runtime.capture())
                rbox = recheck["restart_box"]
                if not rbox:
                    continue
                self.elapsed_seconds = time.monotonic()-started_at
                self.restart_requested = True
                runtime.action("Click", ui_click=False, target=[
                    rbox[0]+rbox[2]//2, rbox[1]+rbox[3]//2])
                return True
            elif update:
                if not update_waiting:
                    runtime.log("检测到客户端资源更新，正在确认并等待下载完成。")
                    update_waiting = True
                if update_confirm_box is not None:
                    if update_attempts >= 3:
                        return incomplete("客户端资源更新确认未生效")
                    if not confirm_update(update_confirm_box) and update_attempts >= 3:
                        return incomplete("客户端资源更新确认未生效")
            elif notice:
                if notice_attempts >= 5:
                    return incomplete("道具过期提示弹窗确认未生效")
                if not dismiss_notice(notice_confirm_box) and notice_attempts >= 5:
                    return incomplete("道具过期提示弹窗确认未生效")
                continue
            elif reward:
                if not runtime.key("skip", .35):
                    return incomplete()
            elif main:
                if confirm_main_fast():
                    runtime.log("已进入游戏主页。" if waiting_logged
                                else "游戏已在主页，跳过启动流程，继续后续任务。")
                    return complete()
            elif enter:
                if entered >= 3:
                    return incomplete("点击进入游戏后仍停留在入口页面")
                click_box_and_wait_exit(enter_box, "enter")
                continue
            elif login:
                if logged_in >= 3:
                    return incomplete("点击登录后仍停留在登录页面")
                click_box_and_wait_exit(login_box, "login")
                continue
            elif account_prompt:
                if welcome_banner or version_check:
                    if not auto_login_logged:
                        runtime.log("检测到账号正在自动登录，等待进入游戏界面。", detail=True)
                        auto_login_logged = True
                else:
                    now = time.monotonic()
                    if account_prompt_since is None or (account_modal and not account_prompt_modal):
                        account_prompt_since = now
                        account_prompt_hits = 0
                        account_prompt_modal = account_modal
                    account_prompt_hits += 1
                    min_hits = 2 if account_modal else 3
                    base_hold = ACCOUNT_MODAL_HOLD_SECONDS if account_modal else ACCOUNT_PROMPT_HOLD_SECONDS
                    required_hold = min(base_hold, max(1.0, (deadline - started_at) * 0.25))
                    if account_prompt_hits >= min_hits and now - account_prompt_since >= required_hold:
                        return incomplete("游戏账号未登录，请先手动完成账号登录后再运行")
            elif updating:
                if not update_waiting:
                    runtime.log("检测到客户端正在热更新或校验资源，等待更新完成。")
                    update_waiting = True
            elif version_check:
                if not version_check_logged:
                    runtime.log("正在获取资源版本信息，等待登录状态完成。", detail=True)
                    version_check_logged = True
            elif intro:
                if not intro_logged:
                    runtime.log("正在播放健康提示或光敏警告，等待启动画面结束。", detail=True)
                    intro_logged = True
            elif loading:
                if not loading_logged:
                    runtime.log("正在加载奇迹大陆，等待进入主页。", detail=True)
                    loading_logged = True
            elif any(runtime.hit(node, image=cur_frame) for node in (
                    "MaaNikki_MenuReady", "MaaNikki_CalendarReady", "MaaNikki_Monthly_PageReady")):
                if runtime.main():
                    return complete()
                return incomplete()
            elif not enter and not login:
                if (update_waiting or (intro_logged and not entered)
                        or time.monotonic() < transition_grace_until
                        or runtime.ui.asset("IconUILoading", image=cur_frame, threshold=.99)):
                    unknown_since = None
                else:
                    now = time.monotonic()
                    unknown_since = now if unknown_since is None else unknown_since
                    if now-unknown_since >= 4:
                        runtime.log("当前为未知页面，正在尝试恢复到游戏主页。")
                        recovered_startup = False
                        for _ in range(4):
                            if runtime.stopped:
                                return False
                            probe_f = runtime.capture()
                            if probe_f is None:
                                break
                            probe_st = _inspect_startup_screen(runtime, probe_f)
                            if any(
                                probe_st[k]
                                for k in (
                                    "main", "reward", "restart_box", "update_dialog",
                                    "notice_confirm_box",
                                    "enter_box", "login_box", "account_prompt",
                                    "updating", "version_check", "intro", "loading",
                                )
                            ):
                                next_frame = probe_f
                                unknown_since = None
                                recovered_startup = True
                                break
                            if not runtime.key("menu", .4):
                                break
                        if recovered_startup:
                            continue
                        if runtime.main():
                            runtime.log("已恢复到游戏主页，继续后续任务。")
                            return complete()
                        return incomplete("无法从未知页面恢复到游戏主页，请手动返回主页后重试")
            if not waiting_logged:
                runtime.log(
                    "正在后台等待进入游戏（请保持窗口不最小化）。"
                    if runtime.background_mode()
                    else "正在等待进入游戏；切出窗口会暂停，切回后继续。"
                )
                waiting_logged = True
            if not runtime.pause(.25):
                return False
        if account_prompt_hits > 0:
            return incomplete("游戏账号未登录，请先手动完成账号登录后再运行")
        return incomplete("进入游戏主页超时，请检查登录或更新提示")
