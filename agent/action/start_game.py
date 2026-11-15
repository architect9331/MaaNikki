from __future__ import annotations

from file_io import write_json

import ctypes
from ctypes import wintypes
from datetime import datetime
import os
import time as wall_time

from maa.custom_action import CustomAction
from client_config import parse_custom_param
from registration import agent_action
from task_control import GameWindowClosed, TaskStopped, active_session, task_time as time
from daily.runtime import Executors, ROOT
from game_view import ALLOWED_FRAME_SIZES, GameFrameSizeError, is_clipped_game_frame, require_game_frame


STARTUP_HUNG_TIMEOUT = 180.0
STARTUP_LOADING_HUNG_TIMEOUT = 240.0
_WM_NULL = 0x0000
_SMTO_BLOCK = 0x0001
_SMTO_ABORTIFHUNG = 0x0002
_ERROR_TIMEOUT = 1460
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
    if u32.IsHungAppWindow(hwnd):
        return True
    ctypes.set_last_error(0)
    msg_res = ctypes.c_size_t(0)
    ok = u32.SendMessageTimeoutW(
        hwnd,
        _WM_NULL,
        0,
        0,
        _SMTO_BLOCK | _SMTO_ABORTIFHUNG,
        80,
        ctypes.byref(msg_res),
    )
    if ok != 0:
        return False
    return ctypes.get_last_error() == _ERROR_TIMEOUT and bool(u32.IsWindow(hwnd))


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
                        if u32.IsHungAppWindow(hwnd):
                            cur.window_hung()
                        break
                    wall_time.sleep(.2)
                cur.generation += 1
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
                            session.window_hung()
                        wall_time.sleep(.2)
                    session.generation += 1
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
        entered, logged_in, update_attempts = 0, 0, 0
        update_waiting = False
        waiting_logged = False
        intro_logged = False
        loading_logged = False
        unknown_since = None

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
            # Read only after the ordinary HUD has been established by the
            # startup flow. Binding also refreshes the desktop account view.
            try:
                for _ in range(2):
                    if runtime.read_account_uid(runtime.frame()) is not None:
                        break
                    if not runtime.pause(.15) or runtime.capture() is None:
                        break
                runtime.account_uid()
            except TaskStopped:
                raise
            except (OSError, ValueError) as error:
                # Startup also serves tasks which do not persist role data.
                # Account-bound tasks confirm identity at their own entrance.
                runtime.log(f"账号识别待重试：{error}。", detail=True)
            if bootstrap:
                self.elapsed_seconds = time.monotonic()-started_at
                return True
            _, finished = runtime.finish("MaaNikki_StartGame")
            return finished

        def transition_ready():
            with runtime.observe():
                if runtime.hit("MaaNikki_StartGame_RestartConfirm"):
                    return True
                if runtime.hit("MaaNikki_StartGame_UpdateTitle"):
                    return runtime.hit("MaaNikki_StartGame_UpdateConfirm")
                return (runtime.hit("MaaNikki_MainDetected")
                        or runtime.hit("MaaNikki_RewardLayer")
                        or runtime.ui.asset("IconUILoading", threshold=.99)
                        or runtime.hit("MaaNikki_StartGame_Loading")
                        or runtime.hit("MaaNikki_StartGame_Updating")
                        or runtime.hit("MaaNikki_StartGame_AccountPrompt")
                        or runtime.hit("MaaNikki_StartGame_Enter"))

        def advance(node, destination):
            def click():
                nonlocal entered, logged_in
                if runtime.stopped or time.monotonic() >= deadline:
                    return False
                if node == "MaaNikki_StartGame_Enter":
                    if entered >= 3:
                        return False
                    entered += 1
                    runtime.log(f"点击进入游戏（{entered}/3）。", detail=True)
                else:
                    if logged_in >= 3:
                        return False
                    logged_in += 1
                    runtime.log(f"点击登录（{logged_in}/3）。", detail=True)
                return runtime.click_template(node, attempts=1, wait_seconds=0, verify=False)

            # Use the shared page-transition budget for each input response.
            # Downloads and loading remain bounded by the outer startup deadline.
            return runtime.ui.enter_page(
                click, destination, source=node,
                attempts=3-entered if node == "MaaNikki_StartGame_Enter" else 3-logged_in,
                seconds=min(5, max(.1, deadline-time.monotonic())))

        def entered_game():
            with runtime.observe():
                if runtime.hit("MaaNikki_StartGame_RestartConfirm"):
                    return True
                if runtime.hit("MaaNikki_StartGame_UpdateTitle"):
                    return runtime.hit("MaaNikki_StartGame_UpdateConfirm")
                return not runtime.hit("MaaNikki_StartGame_Enter") and transition_ready()

        def confirm_update():
            nonlocal update_attempts
            if update_attempts >= 3:
                return False
            update_attempts += 1
            return runtime.click_template(
                "MaaNikki_StartGame_UpdateConfirm", attempts=1, wait_seconds=0, verify=False)

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
        if runtime.capture() is None:
            return False if runtime.stopped else incomplete("未获取到游戏启动画面")

        while not runtime.stopped and time.monotonic() < deadline:
            with runtime.observe(fresh=True):
                restart = runtime.hit("MaaNikki_StartGame_RestartConfirm")
                update = not restart and runtime.hit("MaaNikki_StartGame_UpdateTitle")
                reward = not restart and not update and runtime.hit("MaaNikki_RewardLayer")
                main = not restart and not update and runtime.hit("MaaNikki_MainDetected")
                enter = not update and not main and runtime.hit("MaaNikki_StartGame_Enter")
                login = not update and not main and not enter and runtime.hit("MaaNikki_StartGame_Login")
                account_prompt = (
                    not restart and not update and not reward and not main
                    and not enter and not login
                    and runtime.hit("MaaNikki_StartGame_AccountPrompt")
                )
                updating = (
                    not restart and not update and not reward and not main
                    and not enter and not login and not account_prompt
                    and runtime.hit("MaaNikki_StartGame_Updating")
                )
                intro = (not restart and not update and not reward and not main
                         and not enter and not login and not account_prompt and not updating
                         and runtime.hit("MaaNikki_StartGame_Intro"))
                loading = (not restart and not update and not reward and not main
                           and not enter and not login and not account_prompt and not updating
                           and not intro and runtime.hit("MaaNikki_StartGame_Loading"))
            if (restart or update or reward or main or enter or login
                    or account_prompt or updating or intro or loading):
                unknown_since = None
            if startup_state is not None:
                startup_state["loading_active"] = bool(
                    loading or updating or update or update_waiting
                )
            if restart:
                if not bootstrap or options.get("allow_restart", True) is not True:
                    # The regular batch must not continue on a window that is
                    # about to disappear. Startup normally handles this earlier.
                    return incomplete("更新需要重启，请重新运行启动游戏任务")
                runtime.log("资源更新完成，正在确认重启并等待新的游戏窗口。")
                result = runtime.recognize("MaaNikki_StartGame_RestartConfirm")
                if not result or not result.hit:
                    continue
                box = list(result.box)
                point = [box[0]+box[2]//2, box[1]+box[3]//2]
                if not runtime.action("TouchMove", target=point) or not runtime.pause(.2):
                    return incomplete("未能点击更新重启确认")
                result = runtime.recognize("MaaNikki_StartGame_RestartConfirm")
                if not result or not result.hit:
                    continue
                box = list(result.box)
                self.elapsed_seconds = time.monotonic()-started_at
                # Record intent before submitting: the game can close before
                # the click response. Do not capture, unhover or wait on it again.
                self.restart_requested = True
                runtime.action("Click", ui_click=False, target=[
                    box[0]+box[2]//2, box[1]+box[3]//2])
                return True
            elif update:
                if not update_waiting:
                    runtime.log("检测到客户端资源更新，正在确认并等待下载完成。")
                    update_waiting = True
                if runtime.hit("MaaNikki_StartGame_UpdateConfirm"):
                    if update_attempts >= 3:
                        return incomplete("客户端资源更新确认未生效")
                    # Wait for the known prompt to leave; download duration is
                    # bounded by the startup deadline, with no Esc recovery.
                    if not runtime.ui.enter_page(
                            confirm_update,
                            lambda: not runtime.hit("MaaNikki_StartGame_UpdateConfirm"),
                            source="MaaNikki_StartGame_UpdateConfirm", attempts=3,
                            seconds=min(5, max(.1, deadline-time.monotonic()))):
                        return incomplete("客户端资源更新确认未生效")
            elif reward:
                if not runtime.key("skip", .5):
                    return incomplete()
            elif main:
                if runtime.ui.wait_page("MaaNikki_MainDetected"):
                    runtime.log("已进入游戏主页。" if waiting_logged
                                else "游戏已在主页，跳过启动流程，继续后续任务。")
                    return complete()
            elif enter:
                if entered >= 3:
                    return incomplete("点击进入游戏后仍停留在入口页面")
                if not advance("MaaNikki_StartGame_Enter", entered_game):
                    return incomplete()
            elif login:
                if logged_in >= 3:
                    return incomplete("点击登录后仍停留在登录页面")
                if not advance("MaaNikki_StartGame_Login", transition_ready):
                    return incomplete()
            elif account_prompt:
                return incomplete("游戏账号未登录，请先手动完成账号登录后再运行")
            elif updating:
                if not update_waiting:
                    runtime.log("检测到客户端正在热更新或校验资源，等待更新完成。")
                    update_waiting = True
            elif intro:
                if not intro_logged:
                    runtime.log("正在播放健康提示或光敏警告，等待启动画面结束。", detail=True)
                    intro_logged = True
            elif loading:
                if not loading_logged:
                    runtime.log("正在加载奇迹大陆，等待进入主页。", detail=True)
                    loading_logged = True
            elif any(runtime.hit(node) for node in (
                    "MaaNikki_MenuReady", "MaaNikki_CalendarReady", "MaaNikki_Monthly_PageReady")):
                if runtime.main():
                    return complete()
                return incomplete()
            elif not enter and not login:
                # A confirmed download owns its unrecognized transition frames.
                # Keep polling startup pages so RestartConfirm can hand the old
                # window back to bootstrap; generic Esc recovery cannot do that.
                if (update_waiting or (intro_logged and not entered)
                        or runtime.ui.asset("IconUILoading", threshold=.99)):
                    unknown_since = None
                else:
                    # Allow a short unrecognized transition before invoking
                    # the ordinary, bounded recovery used by other tasks.
                    now = time.monotonic()
                    unknown_since = now if unknown_since is None else unknown_since
                    if now-unknown_since >= 3:
                        runtime.log("当前为未知页面，正在尝试正常恢复到游戏主页。")
                        if runtime.main():
                            runtime.log("已恢复到游戏主页，继续后续任务。")
                            return complete()
                        # Recovery can overlap a cold-start transition. The
                        # login/enter screen needs the startup handler, not Esc.
                        with runtime.observe(fresh=True):
                            startup_ready = (transition_ready()
                                             or runtime.hit("MaaNikki_StartGame_Login")
                                             or runtime.hit("MaaNikki_StartGame_Intro"))
                        if startup_ready:
                            unknown_since = None
                            runtime.log("已识别启动页面，继续等待登录或进入游戏。", detail=True)
                            continue
                        return incomplete("无法从未知页面恢复到游戏主页，请手动返回主页后重试")
            if not waiting_logged:
                runtime.log("正在等待进入游戏；切出窗口会暂停，切回后继续。")
                waiting_logged = True
            if not runtime.pause(.5):
                return False
        return incomplete("进入游戏主页超时，请检查登录或更新提示")
