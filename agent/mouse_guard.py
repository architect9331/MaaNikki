"""Native mouse-input interception and pre-borrow user-activity yielding for background mode."""
from __future__ import annotations

import atexit
from contextlib import contextmanager, nullcontext
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import threading
import time as wall_time
import uuid


_CAPABILITY_MARKER = b"MAANIKKI_MOUSE_GUARD_NATIVE_V1\0"
_SHM_MAGIC = 0x4D4E_4D47  # "MNMG"
_SHM_VERSION = 1

_CMD_DISARM = 0
_CMD_ARM = 1
_CMD_RENEW = 2
_CMD_SHUTDOWN = 3

_STATE_DISARMED = 0
_STATE_ARMED = 1
_STATE_DEFERRED_BUTTON_DOWN = 2
_STATE_HOOK_FAILED = 3

_DEFAULT_LEASE_MS = 450
_DEFAULT_MOVE_YIELD_S = 0.35
_DEFAULT_BUTTON_YIELD_S = 1.50
_USER_IDLE_THRESHOLD_MS = 150
_STILL_SAMPLES_REQUIRED = 3

_PAGE_READWRITE = 0x04
_FILE_MAP_ALL_ACCESS = 0x000F_001F
_EVENT_MODIFY_STATE = 0x0002
_SYNCHRONIZE = 0x0010_0000
_WAIT_OBJECT_0 = 0x0000_0000
_CREATE_NO_WINDOW = 0x0800_0000
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_MOUSE_VKS = (0x01, 0x02, 0x04, 0x05, 0x06)  # L, R, M, X1, X2


class _SharedState(ctypes.Structure):
    _fields_ = [
        ("magic", wintypes.UINT),
        ("version", wintypes.UINT),
        ("cmd_seq", wintypes.UINT),
        ("ack_seq", wintypes.UINT),
        ("command", wintypes.UINT),
        ("state", wintypes.UINT),
        ("lease_ms", wintypes.UINT),
        ("owner_pid", wintypes.UINT),
        ("blocked_count", wintypes.UINT),
        ("passed_up_count", wintypes.UINT),
        ("hook_error", wintypes.UINT),
        ("reserved", wintypes.UINT * 5),
    ]


class _LastInputInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("dwTime", wintypes.DWORD),
    ]


def _make_win32_apis():
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    u32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    u32.GetAsyncKeyState.restype = ctypes.c_short
    u32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    u32.GetCursorPos.restype = wintypes.BOOL
    u32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    u32.SetCursorPos.restype = wintypes.BOOL
    u32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LastInputInfo)]
    u32.GetLastInputInfo.restype = wintypes.BOOL
    u32.ClipCursor.argtypes = [ctypes.POINTER(wintypes.RECT)]
    u32.ClipCursor.restype = wintypes.BOOL
    u32.GetClipCursor.argtypes = [ctypes.POINTER(wintypes.RECT)]
    u32.GetClipCursor.restype = wintypes.BOOL

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetTickCount.argtypes = []
    k32.GetTickCount.restype = wintypes.DWORD
    k32.CreateFileMappingW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPCWSTR,
    ]
    k32.CreateFileMappingW.restype = wintypes.HANDLE
    k32.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k32.OpenFileMappingW.restype = wintypes.HANDLE
    k32.MapViewOfFile.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_size_t,
    ]
    k32.MapViewOfFile.restype = wintypes.LPVOID
    k32.UnmapViewOfFile.argtypes = [wintypes.LPCVOID]
    k32.UnmapViewOfFile.restype = wintypes.BOOL
    k32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CreateEventW.restype = wintypes.HANDLE
    k32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k32.OpenEventW.restype = wintypes.HANDLE
    k32.SetEvent.argtypes = [wintypes.HANDLE]
    k32.SetEvent.restype = wintypes.BOOL
    k32.ResetEvent.argtypes = [wintypes.HANDLE]
    k32.ResetEvent.restype = wintypes.BOOL
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    return u32, k32


_WIN32_APIS: tuple | None = None
_WIN32_LOCK = threading.Lock()


def _apis():
    global _WIN32_APIS
    if os.name != "nt":
        return None
    with _WIN32_LOCK:
        if _WIN32_APIS is None:
            _WIN32_APIS = _make_win32_apis()
        return _WIN32_APIS


def _any_mouse_button_down(u32) -> bool:
    for vk in _MOUSE_VKS:
        if (u32.GetAsyncKeyState(vk) & 0x8000) != 0:
            return True
    return False


def _cursor_point(u32) -> tuple[int, int] | None:
    pt = wintypes.POINT()
    if not u32.GetCursorPos(ctypes.byref(pt)):
        return None
    return int(pt.x), int(pt.y)


def _last_input_age_ms(u32, k32) -> int | None:
    info = _LastInputInfo()
    info.cbSize = ctypes.sizeof(_LastInputInfo)
    if not u32.GetLastInputInfo(ctypes.byref(info)):
        return None
    now_tick = int(k32.GetTickCount())
    return (now_tick - int(info.dwTime)) & 0xFFFFFFFF


def _sleep_checked(seconds: float, stopped=None, ready=None) -> bool:
    if seconds <= 0:
        return not (stopped and stopped()) and (ready is None or bool(ready()))
    deadline = wall_time.monotonic() + seconds
    while True:
        if stopped and stopped():
            return False
        if ready is not None and not ready():
            return False
        rem = deadline - wall_time.monotonic()
        if rem <= 0:
            return True
        wall_time.sleep(min(0.015, rem))


def yield_for_user_mouse(
    *,
    stopped=None,
    ready=None,
    per_monitor_dpi=None,
    move_budget_s: float = _DEFAULT_MOVE_YIELD_S,
    button_budget_s: float = _DEFAULT_BUTTON_YIELD_S,
) -> bool:
    """Wait briefly when the user is holding a mouse button or actively sweeping the cursor."""
    pair = _apis()
    if pair is None:
        return True
    u32, k32 = pair
    dpi_ctx = per_monitor_dpi if per_monitor_dpi is not None else nullcontext
    button_deadline = wall_time.monotonic() + max(0.0, float(button_budget_s))
    move_deadline = wall_time.monotonic() + max(0.0, float(move_budget_s))

    saw_button_down = False
    while _any_mouse_button_down(u32):
        saw_button_down = True
        if wall_time.monotonic() >= button_deadline:
            return not (stopped and stopped()) and (ready is None or bool(ready()))
        if not _sleep_checked(0.015, stopped, ready):
            return False

    if saw_button_down:
        if not _sleep_checked(0.04, stopped, ready):
            return False
        while _any_mouse_button_down(u32) and wall_time.monotonic() < button_deadline:
            if not _sleep_checked(0.015, stopped, ready):
                return False

    age_ms = _last_input_age_ms(u32, k32)
    if age_ms is not None and age_ms >= _USER_IDLE_THRESHOLD_MS:
        return True

    with dpi_ctx():
        prev = _cursor_point(u32)
    if prev is None:
        return True

    saw_motion = False
    still_count = 0
    while wall_time.monotonic() < move_deadline:
        if not _sleep_checked(0.02, stopped, ready):
            return False
        if _any_mouse_button_down(u32):
            while _any_mouse_button_down(u32) and wall_time.monotonic() < button_deadline:
                if not _sleep_checked(0.015, stopped, ready):
                    return False
            if not _sleep_checked(0.04, stopped, ready):
                return False
            return not (stopped and stopped()) and (ready is None or bool(ready()))
        with dpi_ctx():
            cur = _cursor_point(u32)
        if cur is None:
            return True
        if cur == prev:
            if not saw_motion:
                return True
            still_count += 1
            if still_count >= _STILL_SAMPLES_REQUIRED:
                return True
        else:
            saw_motion = True
            still_count = 0
            prev = cur

    return not (stopped and stopped()) and (ready is None or bool(ready()))


_MARKER_CACHE: dict[str, tuple[int, int, bool]] = {}
_MARKER_LOCK = threading.Lock()


def _exe_supports_mouse_guard(exe_path: Path) -> bool:
    try:
        stat = exe_path.stat()
    except OSError:
        return False
    key = str(exe_path)
    sig = (int(stat.st_mtime_ns), int(stat.st_size))
    with _MARKER_LOCK:
        cached = _MARKER_CACHE.get(key)
        if cached is not None and cached[:2] == sig:
            return cached[2]

    found = False
    try:
        overlap = len(_CAPABILITY_MARKER)
        tail = b""
        with exe_path.open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                buf = tail + chunk
                if _CAPABILITY_MARKER in buf:
                    found = True
                    break
                tail = buf[-overlap:]
    except OSError:
        found = False

    with _MARKER_LOCK:
        _MARKER_CACHE[key] = (sig[0], sig[1], found)
    return found


class _NativeMouseGuard:
    """Shared-memory client for the native WH_MOUSE_LL mouse guard."""

    def __init__(self):
        self._lock = threading.RLock()
        self._initialized = False
        self._connected = False
        self._owns_channel = False
        self._armed = False
        self._h_map = None
        self._view = None
        self._shm: _SharedState | None = None
        self._wake_event = None
        self._ack_event = None
        self._proc: subprocess.Popen | None = None
        self._last_seq = 0
        atexit.register(self.close)

    def _ensure_connected(self) -> bool:
        with self._lock:
            if self._connected:
                if self._proc is not None and self._proc.poll() is not None:
                    self.close()
                else:
                    return True
            if self._initialized:
                return False
            self._initialized = True
            pair = _apis()
            if pair is None:
                return False
            _, k32 = pair

            channel = os.environ.get("MAANIKKI_MOUSE_GUARD_CHANNEL", "").strip()
            if channel:
                return self._open_existing(k32, channel)
            return self._spawn_standalone(k32)

    def _open_existing(self, k32, channel: str) -> bool:
        h_map = k32.OpenFileMappingW(_FILE_MAP_ALL_ACCESS, False, f"{channel}_shm")
        if not h_map:
            return False
        view = k32.MapViewOfFile(
            h_map, _FILE_MAP_ALL_ACCESS, 0, 0, ctypes.sizeof(_SharedState)
        )
        if not view:
            k32.CloseHandle(h_map)
            return False
        shm = _SharedState.from_address(view)
        if int(shm.magic) != _SHM_MAGIC or int(shm.version) != _SHM_VERSION:
            k32.UnmapViewOfFile(view)
            k32.CloseHandle(h_map)
            return False
        wake_ev = k32.OpenEventW(_SYNCHRONIZE | _EVENT_MODIFY_STATE, False, f"{channel}_wake")
        ack_ev = k32.OpenEventW(_SYNCHRONIZE | _EVENT_MODIFY_STATE, False, f"{channel}_ack")
        if not wake_ev or not ack_ev:
            if wake_ev:
                k32.CloseHandle(wake_ev)
            if ack_ev:
                k32.CloseHandle(ack_ev)
            k32.UnmapViewOfFile(view)
            k32.CloseHandle(h_map)
            return False

        self._h_map = h_map
        self._view = view
        self._shm = shm
        self._wake_event = wake_ev
        self._ack_event = ack_ev
        self._owns_channel = False
        self._last_seq = int(shm.cmd_seq)
        self._connected = True
        return True

    def _spawn_standalone(self, k32) -> bool:
        exe_path = Path(__file__).resolve().parents[1] / "MaaNikki.exe"
        if not exe_path.is_file() or not _exe_supports_mouse_guard(exe_path):
            return False

        prefix = f"Local\\MaaNikkiMG_{os.getpid()}_{uuid.uuid4().hex[:12]}"
        h_map = k32.CreateFileMappingW(
            _INVALID_HANDLE_VALUE,
            None,
            _PAGE_READWRITE,
            0,
            ctypes.sizeof(_SharedState),
            f"{prefix}_shm",
        )
        if not h_map:
            return False
        view = k32.MapViewOfFile(
            h_map, _FILE_MAP_ALL_ACCESS, 0, 0, ctypes.sizeof(_SharedState)
        )
        if not view:
            k32.CloseHandle(h_map)
            return False
        ctypes.memset(view, 0, ctypes.sizeof(_SharedState))
        shm = _SharedState.from_address(view)
        shm.magic = _SHM_MAGIC
        shm.version = _SHM_VERSION
        shm.cmd_seq = 1
        shm.ack_seq = 0
        shm.command = _CMD_DISARM

        wake_ev = k32.CreateEventW(None, False, False, f"{prefix}_wake")
        ack_ev = k32.CreateEventW(None, False, False, f"{prefix}_ack")
        if not wake_ev or not ack_ev:
            if wake_ev:
                k32.CloseHandle(wake_ev)
            if ack_ev:
                k32.CloseHandle(ack_ev)
            k32.UnmapViewOfFile(view)
            k32.CloseHandle(h_map)
            return False

        try:
            proc = subprocess.Popen(
                [str(exe_path), "--mouse-guard", prefix, str(os.getpid())],
                cwd=str(exe_path.parent),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_CREATE_NO_WINDOW,
            )
        except OSError:
            k32.CloseHandle(wake_ev)
            k32.CloseHandle(ack_ev)
            k32.UnmapViewOfFile(view)
            k32.CloseHandle(h_map)
            return False

        deadline = wall_time.monotonic() + 1.5
        ready = False
        while wall_time.monotonic() < deadline:
            if int(shm.ack_seq) == 1:
                ready = True
                break
            if proc.poll() is not None:
                break
            rem_ms = max(1, min(50, int((deadline - wall_time.monotonic()) * 1000)))
            k32.WaitForSingleObject(ack_ev, rem_ms)

        if not ready:
            try:
                proc.terminate()
            except OSError:
                pass
            k32.CloseHandle(wake_ev)
            k32.CloseHandle(ack_ev)
            k32.UnmapViewOfFile(view)
            k32.CloseHandle(h_map)
            return False

        self._h_map = h_map
        self._view = view
        self._shm = shm
        self._wake_event = wake_ev
        self._ack_event = ack_ev
        self._proc = proc
        self._owns_channel = True
        self._last_seq = 1
        self._connected = True
        return True

    def _next_seq(self) -> int:
        nxt = (max(self._last_seq, int(self._shm.cmd_seq)) + 1) & 0xFFFFFFFF
        if nxt == 0:
            nxt = 1
        self._last_seq = nxt
        return nxt

    def _send(self, command: int, *, lease_ms: int = _DEFAULT_LEASE_MS, timeout_ms: int = 80) -> bool:
        if not self._ensure_connected():
            return False
        pair = _apis()
        if pair is None or self._shm is None:
            return False
        _, k32 = pair

        with self._lock:
            if not self._connected:
                return False
            seq = self._next_seq()
            k32.ResetEvent(self._ack_event)
            self._shm.command = int(command)
            self._shm.lease_ms = int(lease_ms)
            self._shm.owner_pid = int(os.getpid())
            self._shm.cmd_seq = seq
            k32.SetEvent(self._wake_event)

            deadline = wall_time.monotonic() + (timeout_ms / 1000.0)
            while int(self._shm.ack_seq) != seq:
                rem_ms = int((deadline - wall_time.monotonic()) * 1000.0)
                if rem_ms <= 0:
                    return False
                wait_res = k32.WaitForSingleObject(self._ack_event, max(1, min(rem_ms, 20)))
                if int(self._shm.ack_seq) == seq:
                    break
                if wait_res == _WAIT_OBJECT_0:
                    continue
            return True

    def arm(self, lease_ms: int = _DEFAULT_LEASE_MS) -> str:
        with self._lock:
            if not self._send(_CMD_ARM, lease_ms=lease_ms, timeout_ms=80):
                self._armed = False
                return "unavailable"
            state = int(self._shm.state)
            if state == _STATE_ARMED:
                self._armed = True
                return "armed"
            self._armed = False
            if state == _STATE_DEFERRED_BUTTON_DOWN:
                return "deferred_button_down"
            return "failed"

    def renew(self, lease_ms: int = _DEFAULT_LEASE_MS) -> bool:
        with self._lock:
            if not self._armed or not self._connected:
                return False
            if not self._send(_CMD_RENEW, lease_ms=lease_ms, timeout_ms=30):
                return False
            self._armed = int(self._shm.state) == _STATE_ARMED
            return self._armed

    def disarm(self) -> None:
        with self._lock:
            was_armed = self._armed
            self._armed = False
            if not self._connected:
                return
            if not self._send(_CMD_DISARM, lease_ms=0, timeout_ms=100) and was_armed:
                # Best-effort fallback pulse in case the first wake raced.
                self._send(_CMD_DISARM, lease_ms=0, timeout_ms=60)

    def close(self) -> None:
        with self._lock:
            if self._armed:
                self.disarm()
            pair = _apis()
            k32 = pair[1] if pair is not None else None
            if self._owns_channel and self._connected and self._shm is not None:
                try:
                    self._send(_CMD_SHUTDOWN, lease_ms=0, timeout_ms=80)
                except Exception:
                    pass
            self._connected = False
            if self._proc is not None:
                try:
                    self._proc.wait(timeout=0.3)
                except Exception:
                    try:
                        self._proc.terminate()
                    except OSError:
                        pass
                self._proc = None
            if k32 is not None:
                if self._wake_event:
                    k32.CloseHandle(self._wake_event)
                if self._ack_event:
                    k32.CloseHandle(self._ack_event)
                if self._view:
                    k32.UnmapViewOfFile(self._view)
                if self._h_map:
                    k32.CloseHandle(self._h_map)
            self._wake_event = None
            self._ack_event = None
            self._view = None
            self._shm = None
            self._h_map = None


_GUARD_SINGLETON: _NativeMouseGuard | None = None
_GUARD_LOCK = threading.Lock()


def _guard() -> _NativeMouseGuard:
    global _GUARD_SINGLETON
    with _GUARD_LOCK:
        if _GUARD_SINGLETON is None:
            _GUARD_SINGLETON = _NativeMouseGuard()
        return _GUARD_SINGLETON


class BorrowLease:
    """Active cursor-borrow lease returned by guarded_borrowed_cursor."""

    def __init__(self, ok: bool, guard: _NativeMouseGuard | None = None, *, armed: bool = False):
        self.ok = bool(ok)
        self._guard = guard
        self._armed = bool(armed)

    def __bool__(self) -> bool:
        return self.ok

    @property
    def intercepted(self) -> bool:
        return self._armed

    def renew(self, lease_ms: int = _DEFAULT_LEASE_MS) -> bool:
        if not self.ok or not self._armed or self._guard is None:
            return False
        return self._guard.renew(lease_ms)


def _apply_clip_fallback(u32, clip_rect: tuple[int, int, int, int] | None):
    if clip_rect is None:
        return False, None
    prev = wintypes.RECT()
    has_prev = bool(u32.GetClipCursor(ctypes.byref(prev)))
    rect = wintypes.RECT(int(clip_rect[0]), int(clip_rect[1]), int(clip_rect[2]), int(clip_rect[3]))
    if not u32.ClipCursor(ctypes.byref(rect)):
        return False, None
    return True, (prev if has_prev else None)


@contextmanager
def guarded_borrowed_cursor(
    screen_x: int,
    screen_y: int,
    *,
    enabled: bool = True,
    clickable_check=None,
    per_monitor_dpi=None,
    stopped=None,
    ready=None,
    lease_ms: int = _DEFAULT_LEASE_MS,
    clip_rect: tuple[int, int, int, int] | None = None,
    yield_user: bool = True,
):
    """Yield for active user mouse input, intercept physical mouse events during borrow, and restore cleanly."""
    if not enabled:
        yield BorrowLease(True)
        return
    pair = _apis()
    if pair is None:
        yield BorrowLease(False)
        return
    u32, _ = pair
    if clickable_check is not None and not clickable_check(screen_x, screen_y):
        yield BorrowLease(False)
        return

    dpi_ctx = per_monitor_dpi if per_monitor_dpi is not None else nullcontext
    if yield_user:
        if not yield_for_user_mouse(stopped=stopped, ready=ready, per_monitor_dpi=dpi_ctx):
            yield BorrowLease(False)
            return
        if clickable_check is not None and not clickable_check(screen_x, screen_y):
            yield BorrowLease(False)
            return

    guard = _guard()
    armed = False
    arm_deadline = wall_time.monotonic() + _DEFAULT_BUTTON_YIELD_S
    status = guard.arm(lease_ms=lease_ms)
    while status == "deferred_button_down" and yield_user and wall_time.monotonic() < arm_deadline:
        rem = max(0.0, arm_deadline - wall_time.monotonic())
        if not yield_for_user_mouse(
            stopped=stopped,
            ready=ready,
            per_monitor_dpi=dpi_ctx,
            move_budget_s=min(0.15, rem),
            button_budget_s=rem,
        ):
            yield BorrowLease(False)
            return
        if clickable_check is not None and not clickable_check(screen_x, screen_y):
            yield BorrowLease(False)
            return
        status = guard.arm(lease_ms=lease_ms)

    if status == "armed":
        armed = True
    elif status == "deferred_button_down":
        # User kept holding a button past the yield budget; make sure hook stays off
        # and avoid ClipCursor so their active foreground drag isn't trapped.
        clip_rect = None

    clipped = False
    prev_clip = None
    if not armed and clip_rect is not None:
        clipped, prev_clip = _apply_clip_fallback(u32, clip_rect)

    saved = wintypes.POINT()
    restore_cursor = False
    try:
        with dpi_ctx():
            restore_cursor = bool(u32.GetCursorPos(ctypes.byref(saved)))
            u32.SetCursorPos(int(screen_x), int(screen_y))
    except Exception:
        if clipped:
            u32.ClipCursor(ctypes.byref(prev_clip) if prev_clip is not None else None)
        if armed:
            guard.disarm()
        raise

    try:
        yield BorrowLease(True, guard=guard, armed=armed)
    finally:
        try:
            if restore_cursor:
                with dpi_ctx():
                    u32.SetCursorPos(int(saved.x), int(saved.y))
        finally:
            if clipped:
                u32.ClipCursor(ctypes.byref(prev_clip) if prev_clip is not None else None)
            if armed:
                guard.disarm()
