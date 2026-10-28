"""Prepare the official launcher and game before submitting the daily queue."""
from __future__ import annotations

import configparser
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
import time
from threading import Timer


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "logs/startup" / (datetime.now().strftime("%Y%m%d-%H%M%S-%f")+"-launch.json")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from file_io import write_json

PROGRAMS = ("InfinityNikki.exe", "X6Game.exe", "X6Game-Win64-Shipping.exe")


def save_record(record):
    try:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        write_json(REPORT, record)
    except (OSError, ValueError):
        pass


@contextmanager
def startup_step(record, stage):
    """Bound native initialization, including cleanup that can block on a stuck job."""
    record.update(success=False, stage=stage)
    save_record(record)

    def timed_out():
        record.update(success=False, reason=f"启动初始化超时（{stage}），本轮停止。")
        save_record(record)
        # This is the owned prerequisite process, with no daily tasks queued.
        # Native destructors can block on the same job; leave the game untouched.
        os._exit(1)

    timer = Timer(30, timed_out)
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()


def focus_window(hwnd):
    """Activate once at startup, then verify without repeatedly stealing focus."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    if not user32.IsWindow(hwnd):
        return False
    if user32.GetForegroundWindow() == hwnd and not user32.IsIconic(hwnd):
        return True

    current_thread = kernel32.GetCurrentThreadId()
    foreground_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)
    attached = []
    try:
        # The helper is launched in the background. Share the foreground input
        # queue during activation so Windows can grant it foreground access.
        for thread in dict.fromkeys((foreground_thread, target_thread)):
            if thread and thread != current_thread and user32.AttachThreadInput(current_thread, thread, True):
                attached.append(thread)
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE; preserve maximized windows.
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        for thread in reversed(attached):
            user32.AttachThreadInput(current_thread, thread, False)

    deadline, stable_since = time.monotonic()+3, None
    while time.monotonic() < deadline:
        if not user32.IsWindow(hwnd):
            return False
        now = time.monotonic()
        if user32.GetForegroundWindow() == hwnd and not user32.IsIconic(hwnd):
            stable_since = now if stable_since is None else stable_since
            if now-stable_since >= .15:
                return True
        else:
            stable_since = None
        time.sleep(.05)
    return False


def game_window():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetClientRect.restype = wintypes.BOOL
    found = []

    @callback_type
    def visit(hwnd, _):
        title = ctypes.create_unicode_buffer(257)
        class_name = ctypes.create_unicode_buffer(257)
        if (user32.IsWindowVisible(hwnd)
                and user32.GetClassNameW(hwnd, class_name, len(class_name))
                and class_name.value == "UnrealWindow"
                and user32.GetWindowTextW(hwnd, title, len(title))
                and re.fullmatch(r"\s*(Infinity Nikki|无限暖暖)\s*", title.value)):
            rect = wintypes.RECT()
            if user32.IsIconic(hwnd):
                # Minimized Unreal windows may report an empty client area.
                found.append(int(hwnd))
            elif user32.GetClientRect(hwnd, ctypes.byref(rect)):
                width, height = rect.right-rect.left, rect.bottom-rect.top
                if width >= 640 and height >= 360:
                    found.append(int(hwnd))
        return True

    if not user32.EnumWindows(visit, 0):
        raise OSError("无法枚举游戏窗口。")
    return found[0] if found else None


def resolve_program(value):
    """Accept the official launcher, including migration from old client paths."""
    if value:
        path = Path(os.path.expandvars(value.strip().strip('"'))).expanduser()
        if path.is_file():
            if path.name.lower() == "launcher.exe":
                return path.resolve()
            if path.name.lower() in {name.lower() for name in PROGRAMS}:
                for folder in path.resolve().parents:
                    candidate = folder / "launcher.exe"
                    if candidate.is_file():
                        return candidate
        raise ValueError("请选择官方启动器 launcher.exe；不能直接运行游戏客户端。")
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\InfinityNikki Launcher") as key:
            folder = Path(winreg.QueryValueEx(key, "")[0])
            candidate = folder / "launcher.exe"
            if candidate.is_file():
                return candidate.resolve()
    except OSError:
        pass
    config_path = Path(os.environ.get("LOCALAPPDATA", "")) / "InfinityNikki Launcher/config.ini"
    config = configparser.ConfigParser(interpolation=None)
    if config_path.is_file():
        config.read(config_path, encoding="utf-8-sig")
        directory = config.get("Download", "gameDir", fallback="").strip().strip('"')
        if directory:
            folder = Path(directory)
            for base in (folder, folder.parent):
                candidate = base / "launcher.exe"
                if candidate.is_file():
                    return candidate.resolve()
    raise ValueError("未找到官方启动器，请在启动游戏任务中选择 launcher.exe。")


class LauncherWindows:
    def __init__(self, folder):
        self.folder = folder.resolve()
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user32.EnumWindows.argtypes = [self.callback_type, wintypes.LPARAM]
        self.user32.EnumWindows.restype = wintypes.BOOL
        self.user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user32.IsWindowVisible.restype = wintypes.BOOL
        self.user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.user32.GetClientRect.restype = wintypes.BOOL
        self.user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.user32.SetForegroundWindow.restype = wintypes.BOOL
        self.user32.IsIconic.argtypes = [wintypes.HWND]
        self.user32.IsIconic.restype = wintypes.BOOL
        self.kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel32.OpenProcess.restype = wintypes.HANDLE
        self.kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL

    def find(self):
        found = []

        @self.callback_type
        def visit(hwnd, _):
            rect = wintypes.RECT()
            if not (self.user32.IsWindowVisible(hwnd) and self.user32.GetClientRect(hwnd, ctypes.byref(rect))):
                return True
            width, height = rect.right-rect.left, rect.bottom-rect.top
            if width < 640 or height < 360:
                return True
            pid = wintypes.DWORD()
            self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            process = self.kernel32.OpenProcess(0x1000, False, pid.value)
            if process:
                try:
                    name, length = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
                    if self.kernel32.QueryFullProcessImageNameW(process, 0, name, ctypes.byref(length)):
                        path = Path(name.value)
                        if (path.name.lower() in {"xstarter.exe", "launcher.exe"}
                                and path.is_relative_to(self.folder)):
                            found.append((width*height, int(hwnd)))
                finally:
                    self.kernel32.CloseHandle(process)
            return True

        if not self.user32.EnumWindows(visit, 0):
            raise OSError("无法查找官方启动器窗口。")
        return max(found)[1] if found else None

    def foreground(self, hwnd):
        return self.user32.GetForegroundWindow() == hwnd and not self.user32.IsIconic(hwnd)

    def focus(self, hwnd):
        if not focus_window(hwnd):
            raise RuntimeError("未能自动将官方启动器切到前台，请检查窗口状态和权限后重试。")


class LauncherRuntime:
    """A separate Maa session because the game controller does not exist yet."""

    def __init__(self, windows, hwnd):
        from maa.controller import Win32Controller
        from maa.define import MaaWin32InputMethodEnum, MaaWin32ScreencapMethodEnum
        from maa.resource import Resource
        from maa.tasker import Tasker
        self.windows, self.hwnd = windows, hwnd
        self.image = None
        self.resource = Resource()
        if not self.resource.post_bundle(ROOT / "resource").wait().succeeded:
            raise RuntimeError("无法加载启动器识别所需的项目资源。")
        self.controller = Win32Controller(
            hwnd, MaaWin32ScreencapMethodEnum.PrintWindow,
            MaaWin32InputMethodEnum.Seize, MaaWin32InputMethodEnum.Seize)
        self.controller.set_screenshot_target_long_side(1280)
        if not self.controller.post_connection().wait().succeeded:
            raise RuntimeError("无法连接官方启动器窗口。")
        self.tasker = Tasker()
        if not self.tasker.bind(self.resource, self.controller):
            raise RuntimeError("无法建立官方启动器识别会话。")

    def button(self):
        from maa.pipeline import JOCR
        if not self.windows.foreground(self.hwnd):
            return None
        captured = self.controller.post_screencap().wait()
        if not captured.succeeded:
            return None
        self.image = captured.get()
        if self.image is None or not self.windows.foreground(self.hwnd):
            return None
        height, width = self.image.shape[:2]
        # Only the lower-right game button area, excluding top-bar launcher updates.
        roi = (width//2, height*3//4, width-width//2, height-height*3//4)
        job = self.tasker.post_recognition("OCR", JOCR(
            roi=roi, expected=[r"^\s*(启动(游戏)?|啟動(遊戲)?|开始游戏|更新(游戏)?|更新遊戲|运行中|運行中|[Ss][Tt][Aa][Rr][Tt]( [Gg][Aa][Mm][Ee])?|[Uu][Pp][Dd][Aa][Tt][Ee]( [Gg][Aa][Mm][Ee])?|[Rr][Uu][Nn][Nn][Ii][Nn][Gg])\s*$"],
            threshold=.6), self.image)
        detail = job.wait().get()
        if not detail or not detail.nodes:
            return None
        recognition = detail.nodes[-1].recognition
        if not recognition or not recognition.hit or recognition.best_result is None:
            return None
        text = re.sub(r"\s+", "", recognition.best_result.text).lower()
        state = ("update" if "更新" in text or "update" in text
                 else "running" if "运行" in text or "運行" in text or "running" in text
                 else "start")
        return state, recognition.box

    def action(self, state, box):
        from maa.pipeline import JClick
        if not box or not self.windows.foreground(self.hwnd):
            return False
        point = (box[0]+box[2]//2, box[1]+box[3]//2, 1, 1)
        # Use a window-bound Maa click, and guard focus again after mouse movement.
        if not self.controller.post_touch_move(point[0], point[1]).wait().succeeded:
            return False
        time.sleep(.2)
        if not self.windows.foreground(self.hwnd):
            return False
        current = self.button()
        if not current or current[0] != state or not current[1]:
            return False
        box = current[1]
        point = (box[0]+box[2]//2, box[1]+box[3]//2, 1, 1)
        if not self.windows.foreground(self.hwnd):
            return False
        return self.tasker.post_action("Click", JClick(target=point)).wait().succeeded


def launch_game(program, timeout, record):
    os.environ["MAAFW_BINARY_PATH"] = str(ROOT / "maafw")
    from maa.library import Library
    from maa.tasker import Tasker
    Library.open(ROOT / "maafw")
    Tasker.set_log_dir(str(ROOT / "logs/startup/framework"))
    Tasker.set_save_on_error(False)
    windows = LauncherWindows(program.parent)
    hwnd = windows.find()
    if not hwnd:
        os.startfile(str(program), cwd=str(program.parent))
    record["stage"] = "wait_launcher"
    save_record(record)
    runtime, remaining = None, float(timeout)
    last_tick = time.monotonic()
    last_click = {"start": -float("inf"), "update": -float("inf")}
    clicks = {"start": 0, "update": 0}
    paused = False
    while remaining > 0:
        game_hwnd = game_window()
        if game_hwnd:
            record.update(success=True, stage="game_window", clicks=clicks,
                          game_hwnd=game_hwnd, game_class="UnrealWindow",
                          remaining_seconds=max(0, remaining))
            save_record(record)
            return
        hwnd = windows.find()
        if hwnd and (runtime is None or runtime.hwnd != hwnd):
            windows.focus(hwnd)
            with startup_step(record, "connect_launcher"):
                runtime = LauncherRuntime(windows, hwnd)
            record["stage"] = "launcher_button"
            save_record(record)
        now = time.monotonic()
        foreground = bool(hwnd and windows.foreground(hwnd))
        # Do not consume the active timeout or click when the user switches windows.
        if not paused and (not hwnd or foreground):
            remaining -= now-last_tick
        last_tick, paused = now, bool(hwnd and not foreground)
        if runtime and hwnd and foreground:
            button = runtime.button()
            if button:
                state, box = button
                record["button"] = state
                record["stage"] = "updating" if state == "update" else "wait_game"
                # Update/start clicks are debounced; progress/running text is never clicked.
                if state != "running" and now-last_click[state] >= 10:
                    if clicks[state] < 3 and runtime.action(state, box):
                        clicks[state] += 1
                        last_click[state] = now
        time.sleep(.5)
    record["clicks"] = clicks
    if runtime and runtime.image is not None:
        from diagnostics import save_image
        folder = ROOT / "logs/startup"
        folder.mkdir(parents=True, exist_ok=True)
        name = datetime.now().strftime("%Y%m%d-%H%M%S-%f")+"-launcher.png"
        save_image(folder / name, runtime.image, full_frame=False)
        record["screenshot"] = "logs/startup/"+name
    raise RuntimeError("等待官方启动器更新或启动游戏超时，请检查启动器提示；记录见 logs/startup。")


def prepare_game(timeout, record):
    """Keep the batch disconnected until hot updates and restarts are complete."""
    os.environ["MAAFW_BINARY_PATH"] = str(ROOT / "maafw")
    from maa.library import Library
    from maa.controller import Win32Controller
    from maa.define import MaaWin32InputMethodEnum, MaaWin32ScreencapMethodEnum
    from maa.resource import Resource
    from maa.tasker import Tasker
    from registration import local_registration

    Library.open(ROOT / "maafw")
    Tasker.set_log_dir(str(ROOT / "logs/startup/framework"))
    Tasker.set_save_on_error(False)
    remaining = float(record.get("remaining_seconds", timeout))
    restarts = 0
    record.update(success=False, stage="enter_game")
    save_record(record)
    hwnd = game_window()
    while hwnd and remaining >= 30:
        preparing_at = time.monotonic()
        with startup_step(record, "focus_game"):
            if not focus_window(hwnd):
                raise RuntimeError("未能自动将无限暖暖切到前台，请检查窗口状态和权限后重试。")
        with startup_step(record, "load_game_resource"):
            resource = Resource()
            if not resource.post_bundle(ROOT / "resource").wait().succeeded:
                raise RuntimeError("无法加载进入游戏所需的资源。")
            with local_registration(resource) as handlers:
                from action import start_game as _start_game
                from action import gameplay as _gameplay
                from action import game_key as _game_key
                from recognition import ui_template as _ui_template
        record.update(game_hwnd=hwnd, screencap="ScreenDC")
        with startup_step(record, "connect_game"):
            # Startup pauses outside the foreground. ScreenDC avoids the
            # FramePool initialization crash observed in the helper process.
            controller = Win32Controller(
                hwnd, MaaWin32ScreencapMethodEnum.ScreenDC,
                MaaWin32InputMethodEnum.Seize, MaaWin32InputMethodEnum.Seize)
            controller.set_screenshot_target_short_side(720)
            if not controller.post_connection().wait().succeeded:
                raise RuntimeError("无法连接进入游戏所需的窗口。")
            tasker = Tasker()
            if not tasker.bind(resource, controller):
                raise RuntimeError("无法建立进入游戏的启动会话。")
        remaining -= time.monotonic()-preparing_at
        if remaining < 30:
            break
        action = handlers["nikki.start_game"]
        record["stage"] = "enter_game"
        save_record(record)
        try:
            result = tasker.post_task("MaaNikki_StartGame", {
                "MaaNikki_StartGame": {"custom_action_param": {
                    "timeout": min(900, int(remaining)), "bootstrap": True,
                    "allow_restart": restarts < 3,
                }},
            }).wait().get()
        finally:
            # A stop requested inside a local callback can complete the task
            # job before the callback returns. Drain the stop queue before the
            # owning Tasker is destroyed, including on failure or restart.
            with startup_step(record, "close_game_session"):
                action.callback_finished.wait()
                tasker.post_stop().wait()
            record["stage"] = "enter_game"
        remaining -= action.elapsed_seconds
        if not action.restart_requested:
            if result and result.status.succeeded:
                record.update(success=True, stage="ready", restarts=restarts,
                              game_hwnd=hwnd, game_class="UnrealWindow")
                return
            raise RuntimeError("尚未完成进入游戏，请检查 logs/startup 中的记录和截图。")

        restarts += 1
        record.update(stage="wait_restart", restarts=restarts, previous_game_hwnd=hwnd)
        save_record(record)
        # The old task has ended. Dispose its controller and rebuild the whole
        # local session on the new HWND; queued daily tasks do not exist yet.
        del result, tasker, controller, resource, handlers, action
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindow.restype = wintypes.BOOL
        previous_hwnd, disappeared = hwnd, False
        waiting_at = time.monotonic()
        deadline = waiting_at+remaining
        while time.monotonic() < deadline:
            disappeared = disappeared or not user32.IsWindow(previous_hwnd)
            candidate = game_window()
            if candidate and (candidate != previous_hwnd or disappeared):
                hwnd = candidate
                remaining -= time.monotonic()-waiting_at
                record.update(stage="enter_game", game_hwnd=hwnd)
                break
            if not disappeared and time.monotonic()-waiting_at >= 15:
                raise RuntimeError("重启确认后原游戏窗口仍未关闭，请检查游戏提示。")
            time.sleep(.25)
        else:
            raise RuntimeError("等待更新后的游戏窗口超时，请检查游戏是否已自动重启。")
    raise RuntimeError("启动等待时间已用完，请检查游戏更新或登录提示。")


def main():
    record = {"stage": "launch", "success": False}
    code = 1
    fault_log = None
    try:
        if os.name != "nt":
            raise RuntimeError("启动游戏任务仅支持 Windows。")
        save_record(record)
        import faulthandler
        fault_log = REPORT.with_suffix(".fault.log").open("w", encoding="utf-8")
        faulthandler.enable(file=fault_log)
        options = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
        timeout = int(options.get("StartGameTimeout", {}).get("seconds", 900))
        if not 30 <= timeout <= 900:
            raise ValueError("启动等待上限应在30至900秒之间。")
        window = game_window()
        if window:
            record.update(success=True, already_running=True,
                          game_hwnd=window, game_class="UnrealWindow")
        else:
            program = resolve_program(options.get("StartGameProgram", {}).get("path", ""))
            launch_game(program, timeout, record)
        prepare_game(timeout, record)
        code = 0
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError, ImportError, configparser.Error) as error:
        record["success"] = False
        record["reason"] = str(error)
    finally:
        save_record(record)
        if fault_log is not None:
            faulthandler.disable()
            fault_log.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
