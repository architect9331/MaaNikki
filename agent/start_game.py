"""Prepare the official launcher and game before submitting the daily queue."""
from __future__ import annotations

import configparser
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from threading import Timer


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "logs/startup" / (datetime.now().strftime("%Y%m%d-%H%M%S-%f")+"-launch.json")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime_threads import configure_worker_threads

configure_worker_threads(2)
from file_io import write_json
from client_config import mxu_config

for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if callable(_reconfigure):
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

PROGRAMS = ("InfinityNikki.exe", "X6Game.exe", "X6Game-Win64-Shipping.exe")


def resolve_focus_lost_mode(options=None) -> str:
    """Read the user's configured FocusLostBehavior ('pause' | 'return' | 'background')."""
    mapping = {"Pause": "pause", "Return": "return", "Background": "background"}
    sources = [options] if isinstance(options, dict) else []
    try:
        cfg = mxu_config(ROOT) or {}
        if isinstance(cfg.get("globalOptionValues"), dict):
            sources.append(cfg["globalOptionValues"])
    except Exception:
        pass
    for src in sources:
        opt = src.get("FocusLostBehavior")
        if isinstance(opt, str):
            if opt in mapping:
                return mapping[opt]
            if opt in {"pause", "return", "background"}:
                return opt
        elif isinstance(opt, dict):
            case_name = opt.get("caseName")
            if case_name in mapping:
                return mapping[case_name]
    return "pause"


def resolve_task_hud_mode(options=None) -> str:
    """Read the user's configured TaskHudMode ('adaptive' | 'always' | 'reason_only')."""
    mapping = {"Adaptive": "adaptive", "Always": "always", "ReasonOnly": "reason_only"}
    sources = [options] if isinstance(options, dict) else []
    try:
        cfg = mxu_config(ROOT) or {}
        if isinstance(cfg.get("globalOptionValues"), dict):
            sources.append(cfg["globalOptionValues"])
    except Exception:
        pass
    for src in sources:
        opt = src.get("TaskHudMode")
        if isinstance(opt, str):
            if opt in mapping:
                return mapping[opt]
            if opt in {"adaptive", "always", "reason_only"}:
                return opt
        elif isinstance(opt, dict):
            case_name = opt.get("caseName")
            if case_name in mapping:
                return mapping[case_name]
    return "adaptive"


def ensure_unminimized(hwnd) -> bool:
    """Ensure a window is not minimized without stealing foreground focus."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    if not user32.IsWindow(hwnd):
        return False
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
        time.sleep(0.15)
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE fallback
            time.sleep(0.15)
    return bool(user32.IsWindow(hwnd) and not user32.IsIconic(hwnd))


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
        try:
            print(record["reason"], file=sys.stderr, flush=True)
        except Exception:
            pass
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

    def activate():
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

    activate()
    deadline, stable_since, last_attempt = time.monotonic()+3, None, time.monotonic()
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
            if now-last_attempt >= .5:
                activate()
                last_attempt = now
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
                and re.fullmatch(r"\s*(Infinity\s*Nikki|无限暖暖|無限暖暖)\s*", title.value)):
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
    launcher_names = (
        "InfinityNikki Launcher",
        "InfinityNikkiBili Launcher",
        "InfinityNikkiGlobal Launcher",
    )
    for launcher_name in launcher_names:
        reg_key = rf"Software\{launcher_name}"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_key) as key:
                folder = Path(winreg.QueryValueEx(key, "")[0])
                candidate = folder / "launcher.exe"
                if candidate.is_file():
                    return candidate.resolve()
        except OSError:
            pass
    local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
    for launcher_dir in launcher_names:
        config_path = local_app_data / launcher_dir / "config.ini"
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
        # DirectML OCR can initialize in a corrupted state during launcher startup and
        # emit "_ _" for every detected box; CPU inference is fast and deterministic.
        self.resource.use_cpu()
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

    def button(self, *, require_foreground=True):
        from maa.pipeline import JOCR
        if require_foreground and not self.windows.foreground(self.hwnd):
            return None
        if not require_foreground and (
            not self.windows.user32.IsWindow(self.hwnd) or self.windows.user32.IsIconic(self.hwnd)
        ):
            return None
        captured = self.controller.post_screencap().wait()
        if not captured.succeeded:
            return None
        self.image = captured.get()
        if self.image is None or (require_foreground and not self.windows.foreground(self.hwnd)):
            return None
        height, width = self.image.shape[:2]
        # Only the lower-right game button area, excluding top-bar launcher updates.
        roi = (width//2, height*3//4, width-width//2, height-height*3//4)
        job = self.tasker.post_recognition("OCR", JOCR(
            roi=roi, expected=[r"^\s*(启动(游戏)?|啟動(遊戲)?|开始游戏|更新(游戏)?|游戏更新|更新遊戲|遊戲更新|运行中|運行中|[Ss][Tt][Aa][Rr][Tt]( [Gg][Aa][Mm][Ee])?|[Uu][Pp][Dd][Aa][Tt][Ee]( [Gg][Aa][Mm][Ee])?|[Rr][Uu][Nn][Nn][Ii][Nn][Gg])\s*$"],
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

    def _background_click(self, box) -> bool:
        if self.image is None or not self.windows.user32.IsWindow(self.hwnd) or self.windows.user32.IsIconic(self.hwnd):
            return False
        cap_h, cap_w = self.image.shape[:2]
        if cap_w <= 0 or cap_h <= 0:
            return False
        u32 = self.windows.user32
        u32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        u32.ClientToScreen.restype = wintypes.BOOL
        u32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        u32.GetCursorPos.restype = wintypes.BOOL
        u32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        u32.SetCursorPos.restype = wintypes.BOOL
        u32.SendMessageTimeoutW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
            wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
        ]
        u32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
        rect = wintypes.RECT()
        if not u32.GetClientRect(self.hwnd, ctypes.byref(rect)):
            return False
        cw, ch = rect.right - rect.left, rect.bottom - rect.top
        if cw <= 0 or ch <= 0:
            return False
        cx = max(0, min(cw - 1, round((box[0] + box[2] / 2) * cw / cap_w)))
        cy = max(0, min(ch - 1, round((box[1] + box[3] / 2) * ch / cap_h)))
        pt = wintypes.POINT(cx, cy)
        if not u32.ClientToScreen(self.hwnd, ctypes.byref(pt)):
            return False
        lparam = ((cy & 0xFFFF) << 16) | (cx & 0xFFFF)
        res_out = ctypes.c_size_t(0)
        saved = wintypes.POINT()
        has_saved = bool(u32.GetCursorPos(ctypes.byref(saved)))
        u32.SendMessageTimeoutW(self.hwnd, 0x0006, 1, 0, 0x0002, 500, ctypes.byref(res_out))
        u32.SetCursorPos(int(pt.x), int(pt.y))
        try:
            time.sleep(0.01)
            u32.SendMessageTimeoutW(self.hwnd, 0x0200, 0, lparam, 0x0002, 500, ctypes.byref(res_out))
            time.sleep(0.02)
            ok = u32.SendMessageTimeoutW(self.hwnd, 0x0201, 0x0001, lparam, 0x0002, 500, ctypes.byref(res_out)) != 0
            time.sleep(0.05)
            u32.SendMessageTimeoutW(self.hwnd, 0x0202, 0, lparam, 0x0002, 500, ctypes.byref(res_out))
            return ok
        finally:
            if has_saved:
                u32.SetCursorPos(int(saved.x), int(saved.y))

    def action(self, state, box, *, background=False):
        from maa.pipeline import JClick
        if not box:
            return False
        if background and not self.windows.foreground(self.hwnd):
            return self._background_click(box)
        if not self.windows.foreground(self.hwnd):
            return False
        point = (box[0]+box[2]//2, box[1]+box[3]//2, 1, 1)
        # Use a window-bound Maa click, and guard focus again after mouse movement.
        if not self.controller.post_touch_move(point[0], point[1]).wait().succeeded:
            return False
        time.sleep(.15)
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


def launch_game(program, timeout, record, *, background=False):
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
            ready_win = ensure_unminimized(hwnd) if background else focus_window(hwnd)
            if not ready_win:
                if not windows.user32.IsWindow(hwnd):
                    runtime = None
                    time.sleep(.25)
                    continue
                raise RuntimeError("未能准备官方启动器窗口，请检查窗口最小化状态和权限后重试。")
            try:
                with startup_step(record, "connect_launcher"):
                    runtime = LauncherRuntime(windows, hwnd)
            except RuntimeError:
                if not windows.user32.IsWindow(hwnd):
                    runtime = None
                    time.sleep(.25)
                    continue
                raise
            record["stage"] = "launcher_button"
            save_record(record)
        now = time.monotonic()
        active_ok = (
            bool(hwnd and windows.user32.IsWindow(hwnd) and not windows.user32.IsIconic(hwnd))
            if background
            else bool(hwnd and windows.foreground(hwnd))
        )
        # Do not consume the active timeout or click when paused outside the active state.
        if not paused and (not hwnd or active_ok):
            remaining -= now-last_tick
        last_tick, paused = now, bool(hwnd and not active_ok)
        if runtime and hwnd and active_ok:
            button = runtime.button(require_foreground=not background)
            if button:
                state, box = button
                record["button"] = state
                record["stage"] = "updating" if state == "update" else "wait_game"
                # Update/start clicks are debounced; progress/running text is never clicked.
                if state != "running" and now-last_click[state] >= 8:
                    if clicks[state] < 3:
                        use_bg = background and clicks[state] == 0
                        if not use_bg and not windows.foreground(hwnd):
                            focus_window(hwnd)
                        if runtime.action(state, box, background=use_bg):
                            clicks[state] += 1
                            last_click[state] = now
        time.sleep(.25)
    record["clicks"] = clicks
    if runtime and runtime.image is not None:
        from diagnostics import save_image
        folder = ROOT / "logs/startup"
        folder.mkdir(parents=True, exist_ok=True)
        name = datetime.now().strftime("%Y%m%d-%H%M%S-%f")+"-launcher.png"
        save_image(folder / name, runtime.image, full_frame=False)
        record["screenshot"] = "logs/startup/"+name
    raise RuntimeError("等待官方启动器更新或启动游戏超时，请检查启动器提示；记录见 logs/startup。")


def _connect_game_controller(hwnd, background: bool):
    from maa.controller import Win32Controller
    from maa.define import MaaWin32InputMethodEnum, MaaWin32ScreencapMethodEnum
    if background:
        try:
            ctypes.WinDLL("combase").RoInitialize(1)
        except Exception:
            pass
        methods = (
            ("FramePool", MaaWin32ScreencapMethodEnum.FramePool),
            ("PrintWindow", MaaWin32ScreencapMethodEnum.PrintWindow),
            ("ScreenDC", MaaWin32ScreencapMethodEnum.ScreenDC),
        )
    else:
        methods = (("ScreenDC", MaaWin32ScreencapMethodEnum.ScreenDC),)
    for name, method in methods:
        ctrl = Win32Controller(
            hwnd, method,
            MaaWin32InputMethodEnum.Seize, MaaWin32InputMethodEnum.Seize,
        )
        ctrl.set_screenshot_target_short_side(720)
        if ctrl.post_connection().wait().succeeded:
            return ctrl, name
    raise RuntimeError("无法连接进入游戏所需的窗口。")


def prepare_game(timeout, record, options=None):
    """Keep the batch disconnected until hot updates and restarts are complete."""
    os.environ["MAAFW_BINARY_PATH"] = str(ROOT / "maafw")
    from maa.library import Library
    from maa.resource import Resource
    from maa.tasker import Tasker
    from registration import local_registration

    Library.open(ROOT / "maafw")
    Tasker.set_log_dir(str(ROOT / "logs/startup/framework"))
    Tasker.set_save_on_error(False)
    focus_mode = resolve_focus_lost_mode(options)
    hud_mode = resolve_task_hud_mode(options)
    background = focus_mode == "background"
    remaining = float(record.get("remaining_seconds", timeout))
    restarts = 0
    record.update(success=False, stage="enter_game", focus_mode=focus_mode)
    save_record(record)
    hwnd = game_window()
    while hwnd and remaining >= (1.0 if restarts == 0 else 5.0):
        preparing_at = time.monotonic()
        with startup_step(record, "focus_game"):
            ready_win = ensure_unminimized(hwnd) if background else focus_window(hwnd)
            if not ready_win:
                raise RuntimeError(
                    "游戏窗口处于最小化状态且无法恢复，请取消最小化后重试。"
                    if background
                    else "未能自动将无限暖暖切到前台，请检查窗口状态和权限后重试。"
                )
        with startup_step(record, "load_game_resource"):
            resource = Resource()
            resource.use_cpu()
            if not resource.post_bundle(ROOT / "resource").wait().succeeded:
                raise RuntimeError("无法加载进入游戏所需的资源。")
            with local_registration(resource) as handlers:
                from action import start_game as _start_game
                from action import gameplay as _gameplay
                from action import game_key as _game_key
                from recognition import ui_template as _ui_template
        with startup_step(record, "connect_game"):
            controller, cap_name = _connect_game_controller(hwnd, background)
            record.update(game_hwnd=hwnd, screencap=cap_name)
            tasker = Tasker()
            if not tasker.bind(resource, controller):
                raise RuntimeError("无法建立进入游戏的启动会话。")
        remaining -= time.monotonic()-preparing_at
        if remaining <= 0:
            break
        action = handlers["nikki.start_game"]
        record["stage"] = "enter_game"
        save_record(record)
        try:
            from tall_window import is_auto_adjust_resolution_enabled
            action_timeout = min(900, max(1, math.ceil(remaining)))
            result = tasker.post_task("MaaNikki_StartGame", {
                "MaaNikki_StartGame": {"custom_action_param": {
                    "timeout": action_timeout, "bootstrap": True,
                    "allow_restart": restarts < 3,
                    "already_running": bool(record.get("already_running")) and restarts == 0,
                }},
                "MaaNikki_AutoAdjustResolution": {"custom_action_param": {
                    "value": is_auto_adjust_resolution_enabled(options=options),
                }},
                "MaaNikki_FocusLost": {"custom_action_param": {"mode": focus_mode}},
                "MaaNikki_TaskHud": {"custom_action_param": {"mode": hud_mode}},
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
            if getattr(action, "window_closed", False) and restarts < 3 and remaining >= 5.0:
                prev_hwnd = hwnd
                wait_closed_at = time.monotonic()
                rebind_deadline = wait_closed_at + min(5.0, max(0.0, remaining - 2.0))
                rebound_hwnd = None
                while time.monotonic() < rebind_deadline:
                    candidate = game_window()
                    if candidate and candidate != prev_hwnd:
                        rebound_hwnd = candidate
                        break
                    time.sleep(.25)
                if rebound_hwnd:
                    restarts += 1
                    remaining -= time.monotonic() - wait_closed_at
                    del result, tasker, controller, resource, handlers, action
                    hwnd = rebound_hwnd
                    record.update(
                        stage="enter_game",
                        restarts=restarts,
                        previous_game_hwnd=prev_hwnd,
                        game_hwnd=hwnd,
                    )
                    save_record(record)
                    continue
            stop_reason = getattr(action, "stop_reason", None)
            if stop_reason:
                record["message_logged"] = True
                raise RuntimeError(stop_reason)
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
        options = json.loads(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].strip() else {}
        if not isinstance(options, dict):
            options = {}
        raw_timeout = (options.get("StartGameTimeout") or {}).get("seconds")
        timeout = (
            900
            if raw_timeout is None or (isinstance(raw_timeout, str) and not raw_timeout.strip())
            else int(raw_timeout)
        )
        if not 30 <= timeout <= 900:
            raise ValueError("启动等待上限应在30至900秒之间。")
        window = game_window()
        if window:
            record.update(success=True, already_running=True,
                          game_hwnd=window, game_class="UnrealWindow")
        else:
            raw_path = (options.get("StartGameProgram") or {}).get("path")
            program = resolve_program(raw_path if isinstance(raw_path, str) else "")
            launch_game(
                program,
                timeout,
                record,
                background=(resolve_focus_lost_mode(options) == "background"),
            )
        prepare_game(timeout, record, options=options)
        from tall_window import persist_session_16_9_handoff
        persist_session_16_9_handoff()
        code = 0
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError, ImportError, configparser.Error) as error:
        record["success"] = False
        record["reason"] = str(error)
        try:
            print(record["reason"], file=sys.stderr, flush=True)
        except Exception:
            pass
        if not record.get("message_logged"):
            try:
                from message_log import write_message
                write_message(record["reason"], record["reason"])
            except Exception:
                pass
        record.pop("message_logged", None)
    finally:
        if code != 0:
            try:
                from tall_window import restore_session_16_9_windows
                restore_session_16_9_windows()
            except Exception:
                pass
        save_record(record)
        if fault_log is not None:
            faulthandler.disable()
            fault_log.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
