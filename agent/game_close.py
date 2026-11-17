"""Close the game instance bound to the current controller.

The state machine has no MaaFramework or ctypes dependency so it can be
tested offline; Win32Api is the only part that touches the operating system.
"""
from __future__ import annotations

import configparser
from dataclasses import dataclass, field
import math
import os
from pathlib import Path, PureWindowsPath
import re
import time

# Same rule as interface.json controller.win32 and start_game.game_window().
GAME_CLASS = "UnrealWindow"
GAME_TITLE_PATTERN = r"^\s*(Infinity\s*Nikki|无限暖暖|無限暖暖)\s*$"
_GAME_TITLE = re.compile(GAME_TITLE_PATTERN)
# Client executables already listed in start_game.PROGRAMS.
KNOWN_IMAGES = ("InfinityNikki.exe", "X6Game.exe", "X6Game-Win64-Shipping.exe")
_KNOWN_IMAGES = frozenset(name.lower() for name in KNOWN_IMAGES)
# Official launcher executables, matched only against the registry-resolved path.
LAUNCHER_IMAGE = "launcher.exe"
LAUNCHER_HELPER_IMAGE = "xstarter.exe"
LAUNCHER_GRACE_SECONDS = 5.0
LAUNCHER_APPEAR_SECONDS = 10.0
LAUNCHER_APPEAR_POLL = 0.5
# Same registry/config sources as start_game.resolve_program().
LAUNCHER_INSTALL_NAMES = (
    "InfinityNikki Launcher",
    "InfinityNikkiBili Launcher",
    "InfinityNikkiGlobal Launcher",
)

COUNTDOWN_SECONDS = 15.0
COUNTDOWN_TICK = 1.0

GRACE_SECONDS = 15.0
FORCE_WAIT_SECONDS = 5.0
POLL_SECONDS = 0.1

SYNCHRONIZE = 0x00100000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
ERROR_ACCESS_DENIED = 5
WM_CLOSE = 0x0010

SUCCESS_STATUSES = frozenset({"closed", "forced_exit", "already_stopped",
                              "exited_during_countdown"})


@dataclass
class CloseResult:
    status: str
    message: str
    details: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in SUCCESS_STATUSES


@dataclass
class _Target:
    hwnd: int
    pid: int
    handle: object
    image: str
    created: int
    bound: bool
    can_terminate: bool

    def describe(self) -> dict:
        return {"hwnd": self.hwnd, "pid": self.pid, "image": self.image,
                "created": self.created, "bound": self.bound}


def bound_game_hwnd(controller):
    """The HWND the client connected to, mirroring navigation.controller."""
    try:
        info = controller.info
    except Exception:
        return None
    if not isinstance(info, dict) or info.get("type") != "win32":
        return None
    hwnd = info.get("hwnd") or info.get("handle")
    return hwnd if type(hwnd) is int and hwnd > 0 else None


def _denied(error) -> bool:
    return getattr(error, "winerror", None) == ERROR_ACCESS_DENIED


def _image_name(path) -> str:
    return PureWindowsPath(path).name.lower() if path else ""


def _is_game_window(api, hwnd) -> bool:
    return (api.window_class(hwnd) == GAME_CLASS
            and _GAME_TITLE.fullmatch(api.window_title(hwnd) or "") is not None)


def _pin(api, hwnd, *, bound, need_terminate):
    """Hold a process handle first, then prove the window still belongs to it."""
    pid = api.window_pid(hwnd)
    if not pid:
        return CloseResult("ambiguous", "无法读取游戏窗口所属进程，未执行关闭。", {"hwnd": hwnd})
    base = SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
    attempts = ((base | PROCESS_TERMINATE, True), (base, False)) if need_terminate else ((base, False),)
    handle, can_terminate = None, False
    for access, terminate in attempts:
        try:
            handle, can_terminate = api.open_process(pid, access), terminate
            break
        except OSError as error:
            if not _denied(error):
                raise
    if handle is None:
        return CloseResult("access_denied",
                           "没有权限访问游戏进程（游戏可能以更高权限运行），未执行关闭。",
                           {"hwnd": hwnd, "pid": pid})
    try:
        # A recycled PID can no longer swap targets once this handle exists.
        if not api.is_window(hwnd) or api.window_pid(hwnd) != pid:
            api.close_handle(handle)
            return CloseResult("ambiguous", "游戏窗口在定位过程中发生变化，未执行关闭。",
                               {"hwnd": hwnd, "pid": pid})
        image = api.process_image(handle)
        created = api.process_creation_time(handle)
    except BaseException:
        api.close_handle(handle)
        raise
    # The controller binding is authoritative; a fallback candidate must also
    # be a known client executable.
    if not bound and _image_name(image) not in _KNOWN_IMAGES:
        api.close_handle(handle)
        return CloseResult("ambiguous", "找到的窗口不属于已知的无限暖暖客户端程序，未执行关闭。",
                           {"hwnd": hwnd, "pid": pid, "image": image})
    return _Target(hwnd, pid, handle, image, created, bound, can_terminate)


def _locate(api, bound_hwnd, *, need_terminate):
    if bound_hwnd and api.is_window(bound_hwnd):
        if not _is_game_window(api, bound_hwnd):
            return CloseResult("ambiguous", "当前连接的窗口不是无限暖暖游戏窗口，未执行关闭。",
                               {"hwnd": bound_hwnd})
        return _pin(api, bound_hwnd, bound=True, need_terminate=need_terminate)
    # Enumeration failures raise OSError and are reported, never read as "not running".
    windows = [hwnd for hwnd in api.enum_windows()
               if api.is_visible(hwnd) and _is_game_window(api, hwnd)]
    pids = sorted({pid for pid, name in api.list_processes() if name.lower() in _KNOWN_IMAGES})
    details = {"bound_hwnd": bound_hwnd, "windows": windows, "pids": pids}
    if not windows and not pids:
        return CloseResult("already_stopped", "未检测到运行中的无限暖暖，无需关闭。", details)
    if bound_hwnd:
        return CloseResult("ambiguous",
                           "连接的游戏窗口已经关闭，但仍检测到其他无限暖暖窗口或进程，无法确认归属，未执行关闭。",
                           details)
    if not windows:
        return CloseResult("ambiguous", "检测到无限暖暖进程但没有可确认的游戏窗口，未执行关闭。", details)
    if len(windows) > 1:
        return CloseResult("ambiguous", "检测到多个无限暖暖窗口，无法确定要关闭哪一个，未执行关闭。", details)
    return _pin(api, windows[0], bound=False, need_terminate=need_terminate)


def _wait_exit(api, handle, seconds, stopped, clock):
    deadline = clock() + seconds
    while True:
        remaining = deadline - clock()
        if api.wait_process(handle, max(0.0, min(POLL_SECONDS, remaining))):
            return "exited"
        if stopped():
            return "cancelled"
        if deadline - clock() <= 0:
            return "timeout"


def _countdown(api, handle, hwnd, *, seconds, stopped, on_countdown, clock,
               on_window_gone=None):
    """Pace the pre-close countdown on wait_process so runtime and tests share one clock.

    Lines are fixed at ceil(seconds): each completed tick decrements by a fixed
    step, so scheduling jitter stretches the total time, never the line count.
    Exit takes precedence over stop wherever both are examined. When the game
    window disappears mid-countdown while the process is still cleaning up,
    on_window_gone fires once so the remaining lines explain themselves.
    """
    if seconds <= 0 or on_countdown is None:
        return "done"
    remaining = float(seconds)
    window_gone_reported = False
    while remaining > 0:
        if api.wait_process(handle, 0):
            return "exited"
        if stopped():
            return "cancelled"
        on_countdown(int(math.ceil(remaining)))
        if hwnd and not window_gone_reported and on_window_gone is not None:
            try:
                gone = not api.is_window(hwnd)
            except OSError:
                gone = False  # A failed check must not invent a notification.
            if gone:
                window_gone_reported = True
                on_window_gone()
        if api.wait_process(handle, min(COUNTDOWN_TICK, remaining)):
            return "exited"
        remaining -= min(COUNTDOWN_TICK, remaining)
    return "done"


def _observe_window(api, target):
    """Timeout-time observation; "unknown" when the wrapper fails or the window
    handle now belongs to another process (recycled HWND — ownership unclear).

    IsWindow/IsHungAppWindow are BOOL APIs and cannot report per-call failure,
    so a plain False is a real observation, not an error.
    """
    try:
        if not api.is_window(target.hwnd):
            return "gone"
        if api.window_pid(target.hwnd) != target.pid:
            return "unknown"
        return "hung" if api.is_hung(target.hwnd) else "not_hung"
    except Exception:
        return "unknown"


def _timeout_message(close_requested, window_state, grace_seconds, target):
    if close_requested:
        lead = f"已发送关闭请求，进程 {grace_seconds:g} 秒内未退出"
    else:
        lead = f"未发送关闭请求（发送阶段窗口已不存在），进程 {grace_seconds:g} 秒内未退出"
    clause = {
        "not_hung": "窗口仍存在，未检测到无响应",
        "hung": "游戏窗口无响应",
        "gone": "游戏窗口已关闭",
        "unknown": "窗口状态观测失败",
    }.get(window_state, "窗口状态观测失败")
    image = _image_name(target.image) or target.image
    return (f"{lead}：{clause}（PID {target.pid}，映像 {image}）。"
            "游戏已保留；如需兜底，可开启『正常退出超时后强制结束』。")


def _close(api, target, *, force_on_timeout, stopped, before_close,
           grace_seconds, force_wait_seconds, clock, details):
    closed = lambda: CloseResult("closed", "无限暖暖已关闭。", details)
    details["close_requested"] = False
    if api.wait_process(target.handle, 0):
        return CloseResult("already_stopped", "游戏已经退出，无需关闭。", details)
    if stopped():
        return CloseResult("cancelled", "任务已停止，未发送关闭请求。", details)
    send = False
    if api.is_window(target.hwnd):
        if api.window_pid(target.hwnd) != target.pid:
            return CloseResult("ambiguous", "游戏窗口在关闭准备阶段发生变化，未执行关闭。", details)
        if before_close is not None:
            try:
                before_close(target.hwnd)
            except Exception as error:  # Window restoration must never block closing.
                details["before_close_error"] = f"{type(error).__name__}: {error}"
            details["restore_executed"] = True
        # Recheck after restoration: stop, exit, and window ownership may all
        # have changed while the session windows were being restored.
        if api.wait_process(target.handle, 0):
            return CloseResult("already_stopped", "游戏在关闭请求发出前已退出，无需关闭。", details)
        if stopped():
            return CloseResult("cancelled", "任务已停止，未发送关闭请求。", details)
        if api.is_window(target.hwnd):
            if api.window_pid(target.hwnd) != target.pid:
                return CloseResult("ambiguous", "游戏窗口在发送关闭请求前发生变化，未执行关闭。", details)
            try:
                api.post_close(target.hwnd)
            except OSError as error:
                if api.wait_process(target.handle, 0):
                    return closed()
                if _denied(error):
                    return CloseResult("access_denied",
                                       "没有权限向游戏窗口发送关闭请求（游戏可能以更高权限运行），未执行关闭。",
                                       details)
                raise
            details["close_requested"] = True
            send = True
        # else: the window vanished between the checks; fall through to waiting
        # without having sent anything (close_requested stays False).
    outcome = _wait_exit(api, target.handle, grace_seconds, stopped, clock)
    if outcome == "exited":
        return closed()
    if outcome == "cancelled":
        if send:
            return CloseResult("cancelled", "任务已停止；已请求游戏退出，未强制结束。", details)
        return CloseResult("cancelled", "任务已停止；未发送关闭请求，游戏未退出。", details)
    details["window_state_at_timeout"] = _observe_window(api, target)
    message = _timeout_message(details["close_requested"], details["window_state_at_timeout"],
                               grace_seconds, target)
    if not force_on_timeout:
        return CloseResult("timeout", message, details)
    if not target.can_terminate:
        return CloseResult("access_denied", "游戏未在限定时间内退出，且没有权限强制结束游戏进程。", details)
    if stopped():
        return CloseResult("cancelled",
                           "任务已停止；已请求游戏退出，未强制结束。" if details["close_requested"]
                           else "任务已停止，未发送关闭请求，未强制结束。", details)
    try:
        api.terminate(target.handle)
    except OSError as error:
        if api.wait_process(target.handle, 0):
            return closed()
        if _denied(error):
            return CloseResult("access_denied", "没有权限强制结束游戏进程。", details)
        raise
    details["forced"] = True
    outcome = _wait_exit(api, target.handle, force_wait_seconds, stopped, clock)
    if outcome == "exited":
        return CloseResult("forced_exit", f"游戏未在宽限期内退出，已强制结束进程（PID {target.pid}）。", details)
    if outcome == "cancelled":
        return CloseResult("cancelled", "已请求强制终止游戏进程，任务已停止，尚未确认退出。", details)
    return CloseResult("force_timeout",
                       f"已请求强制终止游戏进程，{force_wait_seconds:g} 秒内仍未确认退出（PID {target.pid}）；请手动检查进程状态。",
                       details)


def close_connected_game(api, bound_hwnd, *, force_on_timeout=False, stopped=lambda: False,
                         before_close=None, grace_seconds=GRACE_SECONDS,
                         force_wait_seconds=FORCE_WAIT_SECONDS, clock=time.monotonic,
                         countdown_seconds=COUNTDOWN_SECONDS, on_countdown=None,
                         bring_forward=None, on_window_gone=None):
    """Request a normal exit of one confirmed game process and wait for it."""
    target = None
    try:
        located = _locate(api, bound_hwnd, need_terminate=force_on_timeout)
        if isinstance(located, CloseResult):
            return located
        target = located
        details = target.describe()
        if bring_forward is not None:
            try:
                bring_forward()
            except Exception as error:  # Raising the window must never block closing.
                details["bring_forward_error"] = f"{type(error).__name__}: {error}"
        outcome = _countdown(api, target.handle, target.hwnd, seconds=countdown_seconds,
                             stopped=stopped, on_countdown=on_countdown, clock=clock,
                             on_window_gone=on_window_gone)
        if outcome == "exited":
            return CloseResult("exited_during_countdown",
                               "游戏在倒计时期间已自行退出，无需关闭。", details)
        if outcome == "cancelled":
            return CloseResult("cancelled", "任务已停止，未发送关闭请求。", details)
        return _close(api, target, force_on_timeout=force_on_timeout, stopped=stopped,
                      before_close=before_close, grace_seconds=grace_seconds,
                      force_wait_seconds=force_wait_seconds, clock=clock, details=details)
    except OSError as error:
        details = target.describe() if target else {"bound_hwnd": bound_hwnd}
        details["winerror"] = getattr(error, "winerror", None)
        status = "access_denied" if _denied(error) else "error"
        return CloseResult(status, f"关闭游戏时系统调用失败：{error}", details)
    finally:
        if target is not None:
            try:
                api.close_handle(target.handle)
            except OSError:
                pass


def launcher_eligible(result):
    """Only a run that actually sent a close request may close the launcher."""
    return bool(result.ok) and result.details.get("close_requested") is True


def _launcher_candidates():
    """Every existing launcher.exe path from the official registry/config sources."""
    found = []
    seen = set()

    def add(candidate):
        try:
            if candidate is not None and candidate.is_file():
                key = str(candidate).lower()
                if key not in seen:
                    seen.add(key)
                    found.append(candidate)
        except OSError:
            pass

    try:
        import winreg
        for name in LAUNCHER_INSTALL_NAMES:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"Software\{name}") as key:
                    folder = Path(winreg.QueryValueEx(key, "")[0])
                add(folder / LAUNCHER_IMAGE)
            except OSError:
                pass
    except Exception:
        pass
    try:
        local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
        for name in LAUNCHER_INSTALL_NAMES:
            config_path = local_app_data / name / "config.ini"
            if not config_path.is_file():
                continue
            config = configparser.ConfigParser(interpolation=None)
            config.read(config_path, encoding="utf-8-sig")
            directory = config.get("Download", "gameDir", fallback="").strip().strip('"')
            if directory:
                folder = Path(directory)
                for base in (folder, folder.parent):
                    add(base / LAUNCHER_IMAGE)
    except Exception:
        pass
    return found


def select_launcher_for_game(candidates, game_image):
    """Pick the launcher install that actually contains THIS game instance.

    Returns the candidate launcher path only when exactly one candidate install
    root is an ancestor of the game image; unrelated, empty, or ambiguous
    (nested channels) inputs return None so the caller skips instead of
    guessing which channel's launcher to close.
    """
    if not candidates or not game_image:
        return None
    game_low = str(PureWindowsPath(str(game_image))).lower()
    matched = {}
    for candidate in candidates:
        root = str(PureWindowsPath(str(candidate)).parent).lower().rstrip("\\") + "\\"
        if game_low.startswith(root):
            matched[root] = candidate
    if len(matched) == 1:
        return next(iter(matched.values()))
    return None


def resolve_official_launcher(game_image=None):
    """Resolve the official launcher for THIS game instance from the same
    registry/config sources the start task uses. Returns a Path or None; never raises."""
    return select_launcher_for_game(_launcher_candidates(), game_image)


def _match_launcher_windows(api, resolved):
    """Visible top-level windows owned by the resolved official launcher install.

    The visible launcher UI often runs from a versioned subfolder (observed:
    `<install>\\1.3.1\\xstarter.exe` spawned by the root bootstrap
    `<install>\\launcher.exe`), so anything named launcher.exe / XStarter.exe
    anywhere UNDER the registry-resolved install root belongs to this launcher.
    Identity stays anchored to the resolved install directory, never to a name
    seen anywhere on the machine.
    """
    root = str(resolved.parent).lower().rstrip("\\") + "\\"
    targets = []
    for hwnd in api.enum_windows():
        if not api.is_visible(hwnd):
            continue
        pid = api.window_pid(hwnd)
        if not pid:
            continue
        try:
            handle = api.open_process(pid, SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION)
        except OSError:
            continue
        try:
            image_path = api.process_image(handle)
        except OSError:
            api.close_handle(handle)
            continue
        image = PureWindowsPath(image_path)
        image_low = str(image).lower()
        name = _image_name(image_path)
        matched = (image_low == str(resolved).lower()
                   or (name in (LAUNCHER_IMAGE, LAUNCHER_HELPER_IMAGE)
                       and image_low.startswith(root)))
        if not matched:
            api.close_handle(handle)
            continue
        targets.append((hwnd, pid, handle))
    return targets


def close_launcher(api, *, launcher_path, stopped=lambda: False,
                   grace_seconds=LAUNCHER_GRACE_SECONDS, clock=time.monotonic,
                   sleep=time.sleep, appear_seconds=LAUNCHER_APPEAR_SECONDS):
    """Best-effort close of the resolved official launcher window; never force-terminates.

    launcher_path comes from resolve_official_launcher() for THIS game instance.
    The launcher window can appear later than the game closes (observed slow
    startup and tray restore), so the match phase waits up to appear_seconds
    while honouring the stop signal. Every window is re-checked right before
    its request is sent; all opened handles are released via finally, and any
    failure becomes a result instead of propagating to the caller.
    """
    if not launcher_path:
        return CloseResult("already_stopped", "未能确认官方启动器身份，跳过关闭。", {})
    resolved = PureWindowsPath(str(launcher_path))
    details = {"launcher_path": str(resolved)}
    handles = []
    try:
        deadline = clock() + max(0.0, float(appear_seconds))
        targets = []
        while True:
            if stopped():
                return CloseResult("cancelled", "任务已停止，跳过关闭启动器。", details)
            try:
                targets = _match_launcher_windows(api, resolved)
            except OSError as error:
                return CloseResult("error", f"枚举窗口以关闭官方启动器时失败：{error}", details)
            handles.extend(handle for _, _, handle in targets)
            if targets or clock() >= deadline:
                break
            sleep(min(LAUNCHER_APPEAR_POLL, max(0.0, deadline - clock())))
        if not targets:
            details.update(waited_seconds=max(0.0, float(appear_seconds)))
            return CloseResult("already_stopped",
                               f"未找到匹配官方启动器路径的窗口（已等待 {appear_seconds:g} 秒），跳过。",
                               details)
        requested = failed_sends = 0
        live = []
        for hwnd, pid, handle in targets:
            if stopped():
                break
            try:
                alive = bool(api.is_window(hwnd) and api.window_pid(hwnd) == pid)
            except OSError:
                alive = False
            if not alive:
                continue
            try:
                api.post_close(hwnd)
            except OSError:
                failed_sends += 1
                continue
            requested += 1
            live.append(handle)
        details.update(requested=requested, send_failed=failed_sends)
        if stopped():
            return CloseResult("cancelled", "任务已停止，跳过关闭启动器。", details)
        if not requested:
            return CloseResult("error", "未能向官方启动器发送关闭请求。", details)
        deadline = clock() + grace_seconds
        pending = list(live)
        while pending:
            if stopped():
                details["unconfirmed"] = len(pending)
                return CloseResult("cancelled", "任务已停止，不再等待官方启动器退出。", details)
            remaining = deadline - clock()
            if remaining <= 0:
                break
            pending = [h for h in pending
                       if not api.wait_process(h, min(POLL_SECONDS, max(0.0, remaining)))]
        details["unconfirmed"] = len(pending)
        if pending:
            counts = f"（请求 {requested}"
            if failed_sends:
                counts += f"，发送失败 {failed_sends}"
            counts += f"，未确认 {len(pending)}）"
            return CloseResult("timeout", f"已请求官方启动器退出，但未在限定时间内确认{counts}；不强制结束。",
                               details)
        message = "官方启动器已关闭。"
        if failed_sends:
            message += f"（请求 {requested}，发送失败 {failed_sends}）"
        return CloseResult("closed", message, details)
    except OSError as error:
        return CloseResult("error", f"关闭官方启动器时系统调用失败：{error}", details)
    finally:
        for handle in handles:
            try:
                api.close_handle(handle)
            except OSError:
                pass


def bring_maannikki_forward(api):
    """Bring the MaaNikki GUI window forward; best-effort, never raises.

    Returns "activated", "not_found", or "activate_failed".
    """
    hwnd = _find_maannikki_window(api)
    if not hwnd:
        return "not_found"
    try:
        return "activated" if _activate_window(api, hwnd, 3.0) else "activate_failed"
    except Exception:
        return "activate_failed"


def _find_maannikki_window(api):
    # Parent first: in GUI mode the agent is spawned by MaaNikki.exe itself.
    try:
        parent = os.getppid() or 0
    except Exception:
        parent = 0
    if parent:
        image = None
        try:
            handle = api.open_process(parent, SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION)
            try:
                image = api.process_image(handle)
            finally:
                api.close_handle(handle)
        except OSError:
            image = None
        if image is not None and _image_name(image) == "maanikki.exe":
            # Parent is the GUI process; do not fall through to a global scan,
            # which could raise another instance's window.
            return _single_visible_titled_window(api, parent)
    return _single_visible_titled_window(api, None)


def _single_visible_titled_window(api, pid_filter):
    try:
        windows = api.enum_windows()
    except OSError:
        return None
    found = []
    for hwnd in windows:
        if not api.is_visible(hwnd):
            continue
        if not api.window_title(hwnd):
            continue
        pid = api.window_pid(hwnd)
        if not pid or (pid_filter is not None and pid != pid_filter):
            continue
        try:
            handle = api.open_process(pid, SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION)
        except OSError:
            continue
        try:
            image = api.process_image(handle)
        except OSError:
            api.close_handle(handle)
            continue
        api.close_handle(handle)
        if _image_name(image) == "maanikki.exe":
            found.append(hwnd)
    # Only a unique candidate is used; several instances stay untouched.
    return found[0] if len(found) == 1 else None


def _activate_window(api, hwnd, seconds):
    """Same technique as start_game.focus_window(); best effort, time-bounded."""
    deadline = time.monotonic() + seconds
    stable_since = None
    last_attempt = None
    while time.monotonic() < deadline:
        if not api.is_window(hwnd):
            return False
        if api.foreground_window() == hwnd and not api.is_iconic(hwnd):
            now = time.monotonic()
            stable_since = now if stable_since is None else stable_since
            if now - stable_since >= 0.15:
                return True
        else:
            stable_since = None
            now = time.monotonic()
            if last_attempt is None or now - last_attempt >= 0.5:
                _activate_once(api, hwnd)
                last_attempt = now
        time.sleep(0.05)
    return False


def _activate_once(api, hwnd):
    current_thread = api.current_thread_id()
    foreground_thread = api.window_thread(api.foreground_window())
    target_thread = api.window_thread(hwnd)
    attached = []
    try:
        # Share the foreground input queue so Windows can grant foreground access.
        for thread in dict.fromkeys((foreground_thread, target_thread)):
            if thread and thread != current_thread and api.attach_thread_input(current_thread, thread, True):
                attached.append(thread)
        if api.is_iconic(hwnd):
            api.show_window(hwnd, 9)  # SW_RESTORE; preserve maximized windows.
        api.bring_to_top(hwnd)
        api.set_foreground(hwnd)
    finally:
        for thread in reversed(attached):
            api.attach_thread_input(current_thread, thread, False)


class Win32Api:
    """Thin ctypes binding; every failure raises OSError carrying the Win32 code."""

    WAIT_OBJECT_0 = 0x0
    WAIT_TIMEOUT = 0x102
    TH32CS_SNAPPROCESS = 0x2
    ERROR_NO_MORE_FILES = 18

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

        self._entry_type = PROCESSENTRY32W
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def bind(dll, name, argtypes, restype):
            func = getattr(dll, name)
            func.argtypes, func.restype = argtypes, restype
            return func

        HWND, DWORD, BOOL, HANDLE = wintypes.HWND, wintypes.DWORD, wintypes.BOOL, wintypes.HANDLE
        self._is_window = bind(user32, "IsWindow", [HWND], BOOL)
        self._is_visible = bind(user32, "IsWindowVisible", [HWND], BOOL)
        self._is_hung = bind(user32, "IsHungAppWindow", [HWND], BOOL)
        self._window_pid = bind(user32, "GetWindowThreadProcessId",
                                [HWND, ctypes.POINTER(DWORD)], DWORD)
        self._class_name = bind(user32, "GetClassNameW", [HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int)
        self._window_text = bind(user32, "GetWindowTextW", [HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int)
        self._enum_windows = bind(user32, "EnumWindows", [self._callback_type, wintypes.LPARAM], BOOL)
        self._post_message = bind(user32, "PostMessageW",
                                  [HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], BOOL)
        self._open_process = bind(kernel32, "OpenProcess", [DWORD, BOOL, DWORD], HANDLE)
        self._image_name = bind(kernel32, "QueryFullProcessImageNameW",
                                [HANDLE, DWORD, wintypes.LPWSTR, ctypes.POINTER(DWORD)], BOOL)
        self._process_times = bind(kernel32, "GetProcessTimes",
                                   [HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4, BOOL)
        self._wait = bind(kernel32, "WaitForSingleObject", [HANDLE, DWORD], DWORD)
        self._terminate = bind(kernel32, "TerminateProcess", [HANDLE, wintypes.UINT], BOOL)
        self._close_handle = bind(kernel32, "CloseHandle", [HANDLE], BOOL)
        self._snapshot = bind(kernel32, "CreateToolhelp32Snapshot", [DWORD, DWORD], HANDLE)
        self._first = bind(kernel32, "Process32FirstW", [HANDLE, ctypes.POINTER(PROCESSENTRY32W)], BOOL)
        self._next = bind(kernel32, "Process32NextW", [HANDLE, ctypes.POINTER(PROCESSENTRY32W)], BOOL)
        self._invalid_handle = ctypes.c_void_p(-1).value
        self._get_foreground = bind(user32, "GetForegroundWindow", [], HWND)
        self._set_foreground = bind(user32, "SetForegroundWindow", [HWND], BOOL)
        self._bring_to_top = bind(user32, "BringWindowToTop", [HWND], BOOL)
        self._attach_thread_input = bind(user32, "AttachThreadInput", [DWORD, DWORD, BOOL], BOOL)
        self._show_window = bind(user32, "ShowWindow", [HWND, ctypes.c_int], BOOL)
        self._is_iconic = bind(user32, "IsIconic", [HWND], BOOL)
        self._current_thread = bind(kernel32, "GetCurrentThreadId", [], DWORD)

    def _error(self, code=None):
        return self._ctypes.WinError(self._ctypes.get_last_error() if code is None else code)

    def is_window(self, hwnd):
        return bool(self._is_window(hwnd))

    def is_visible(self, hwnd):
        return bool(self._is_visible(hwnd))

    def is_hung(self, hwnd):
        return bool(self._is_hung(hwnd))

    def window_pid(self, hwnd):
        pid = self._wintypes.DWORD()
        if not self._window_pid(hwnd, self._ctypes.byref(pid)):
            return 0
        return int(pid.value)

    def window_thread(self, hwnd):
        return int(self._window_pid(hwnd, None))

    def window_class(self, hwnd):
        buffer = self._ctypes.create_unicode_buffer(257)
        return buffer.value if self._class_name(hwnd, buffer, len(buffer)) else ""

    def window_title(self, hwnd):
        buffer = self._ctypes.create_unicode_buffer(257)
        return buffer.value if self._window_text(hwnd, buffer, len(buffer)) else ""

    def enum_windows(self):
        found = []

        @self._callback_type
        def visit(hwnd, _):
            found.append(int(hwnd))
            return True

        if not self._enum_windows(visit, 0):
            raise self._error()
        return found

    def list_processes(self):
        snapshot = self._snapshot(self.TH32CS_SNAPPROCESS, 0)
        if not snapshot or snapshot == self._invalid_handle:
            raise self._error()
        try:
            entry = self._entry_type()
            entry.dwSize = self._ctypes.sizeof(entry)
            found = []
            ok = self._first(snapshot, self._ctypes.byref(entry))
            while ok:
                found.append((int(entry.th32ProcessID), entry.szExeFile))
                ok = self._next(snapshot, self._ctypes.byref(entry))
            code = self._ctypes.get_last_error()
            if code != self.ERROR_NO_MORE_FILES:
                raise self._error(code)
            return found
        finally:
            self._close_handle(snapshot)

    def open_process(self, pid, access):
        handle = self._open_process(access, False, int(pid))
        if not handle:
            raise self._error()
        return handle

    def process_image(self, handle):
        buffer = self._ctypes.create_unicode_buffer(32768)
        length = self._wintypes.DWORD(len(buffer))
        if not self._image_name(handle, 0, buffer, self._ctypes.byref(length)):
            raise self._error()
        return buffer.value

    def process_creation_time(self, handle):
        times = [self._wintypes.FILETIME() for _ in range(4)]
        if not self._process_times(handle, *(self._ctypes.byref(item) for item in times)):
            raise self._error()
        return (int(times[0].dwHighDateTime) << 32) | int(times[0].dwLowDateTime)

    def wait_process(self, handle, seconds):
        result = self._wait(handle, max(0, int(seconds * 1000)))
        if result == self.WAIT_OBJECT_0:
            return True
        if result == self.WAIT_TIMEOUT:
            return False
        raise self._error()

    def post_close(self, hwnd):
        # PostMessage never blocks on an unresponsive window.
        if not self._post_message(hwnd, WM_CLOSE, 0, 0):
            raise self._error()

    def terminate(self, handle):
        if not self._terminate(handle, 1):
            raise self._error()

    def close_handle(self, handle):
        if handle and not self._close_handle(handle):
            raise self._error()

    def foreground_window(self):
        return int(self._get_foreground())

    def set_foreground(self, hwnd):
        return bool(self._set_foreground(hwnd))

    def bring_to_top(self, hwnd):
        return bool(self._bring_to_top(hwnd))

    def attach_thread_input(self, first, second, attach):
        return bool(self._attach_thread_input(int(first), int(second), bool(attach)))

    def show_window(self, hwnd, command):
        return bool(self._show_window(hwnd, int(command)))

    def is_iconic(self, hwnd):
        return bool(self._is_iconic(hwnd))

    def current_thread_id(self):
        return int(self._current_thread())
