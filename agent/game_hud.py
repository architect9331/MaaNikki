"""Capture-excluded unified task HUD and forced-foreground reason overlay."""
from __future__ import annotations

import atexit
import ctypes
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import threading
import time

import numpy as np

from client_config import node_action_params
from message_log import write_message
from overlay_style import (
    MXU_CONFIG_FILE,
    PROC,
    ROOT_DIR,
    BitmapInfo,
    Blend,
    Paint,
    WindowClass,
    bind,
    checked,
    draw_hud_card,
    hud_top_center_anchor,
    measure_hud_card,
    resolve_overlay_theme,
    should_exclude_from_capture,
    sync_overlay_zorder,
)


STATIC_NODE_LABELS = {
    "MaaNikki_StartGame": "启动游戏",
    "MaaNikki_Assist": "探索辅助",
    "MaaNikki_Exploration": "探索辅助",
    "MaaNikki_RouteRecord": "录制路线",
    "MaaNikki_RouteRun": "自定义路线",
    "MaaNikki_OpenWorldCollect": "大世界材料采集",
    "MaaNikki_RouteTest": "路线测试",
    "MaaNikki_MinigameBling": "小游戏刷噗灵",
    "MaaNikki_Wardrobe": "读取衣柜",
    "MaaNikki_WardrobeRead": "读取衣柜",
    "MaaNikki_RenewalRead": "读取焕新",
    "MaaNikki_BackpackRead": "读取背包",
    "MaaNikki_GameKeys": "读取游戏键位",
    "MaaNikki_GameKeysRead": "读取游戏键位",
    "MaaNikki_Bless": "祝福闪光幻境",
    "MaaNikki_Jihua": "素材激化幻境",
    "MaaNikki_Monster": "魔物试炼幻境",
    "MaaNikki_RealmWeekly": "心之突破幻境",
    "MaaNikki_Fishing": "当前点位钓鱼",
    "MaaNikki_RunCoordination": "任务协调",
}

NODE_PREFIX_LABELS = (
    ("MaaNikki_Zhaoxi", "朝夕心愿"),
    ("MaaNikki_ClaimZhaoxi", "朝夕心愿"),
    ("MaaNikki_Xinghai", "星海拾光"),
    ("MaaNikki_ClaimXinghai", "星海拾光"),
    ("MaaNikki_Crown", "奇迹之冠巅峰赛"),
    ("MaaNikki_MonthlyPass", "奇迹之旅奖励"),
    ("MaaNikki_MonthlyCrystal", "每月五抽兑换"),
    ("MaaNikki_Monthly", "奇迹之旅"),
    ("MaaNikki_Mailbox", "邮箱领取"),
    ("MaaNikki_WeeklyFreeGifts", "每周免费礼包"),
    ("MaaNikki_Shop", "商店免费礼包"),
    ("MaaNikki_Pursuit", "祝福闪光收集"),
    ("MaaNikki_Blessing", "祝福闪光收集"),
    ("MaaNikki_Bless", "祝福闪光幻境"),
    ("MaaNikki_Jihua", "素材激化幻境"),
    ("MaaNikki_Monster", "魔物试炼幻境"),
    ("MaaNikki_Weekly", "心之突破幻境"),
    ("MaaNikki_Dig", "美鸭梨挖掘"),
    ("MaaNikki_Home", "家园日常"),
    ("MaaNikki_CollectStarlightCrystals", "收集星光结晶"),
    ("MaaNikki_Realm", "幻境挑战"),
    ("MaaNikki_Renewal", "读取焕新"),
    ("MaaNikki_Rejuvenate", "读取焕新"),
    ("MaaNikki_Backpack", "读取背包"),
    ("MaaNikki_Wardrobe", "读取衣柜"),
    ("MaaNikki_Assist", "探索辅助"),
    ("MaaNikki_StartGame", "启动游戏"),
)

_GENERIC_HELPER_NODES = {
    "",
    "MaaNikki_Daily_Action",
    "MaaNikki_Daily_Message",
    "MaaNikki_ReturnMain",
    "MaaNikki_ReturnMainSecond",
    "MaaNikki_ReturnMainThird",
    "MaaNikki_FocusLost",
    "MaaNikki_RouteOverlay",
    "MaaNikki_TaskHud",
    "MaaNikki_AutoAdjustResolution",
}

_TASK_ENTRY_LABELS: dict[str, str] | None = None
_HUD_CASE_TO_MODE = {
    "Adaptive": "adaptive",
    "Always": "always",
    "ReasonOnly": "reason_only",
    "adaptive": "adaptive",
    "always": "always",
    "reason_only": "reason_only",
}
_HUD_REGISTRY_LOCK = threading.Lock()
_HUD_BY_TASKER: dict[int, "GameHudOverlay"] = {}
_CAPTURE_INCOMPATIBLE = False


def _entry_labels() -> dict[str, str]:
    global _TASK_ENTRY_LABELS
    if _TASK_ENTRY_LABELS is not None:
        return _TASK_ENTRY_LABELS
    labels: dict[str, str] = {}
    tasks_dir = ROOT_DIR / "tasks"
    if tasks_dir.is_dir():
        for path in tasks_dir.glob("*.json"):
            try:
                doc = json.loads(path.read_text(encoding="utf-8-sig"))
            except Exception:
                continue
            for section in ("task", "pretask"):
                for item in doc.get(section, ()) or ():
                    if isinstance(item, dict) and item.get("entry") and item.get("label"):
                        labels[str(item["entry"])] = str(item["label"])
    _TASK_ENTRY_LABELS = labels
    return _TASK_ENTRY_LABELS


def resolve_task_label(node_name: str, current: str = "") -> str:
    """Resolve a human-readable MaaNikki task label from a pipeline node name."""
    name = str(node_name or "").strip()
    if name in _GENERIC_HELPER_NODES:
        return current
    entries = _entry_labels()
    if name in entries:
        return entries[name]
    if name in STATIC_NODE_LABELS:
        return STATIC_NODE_LABELS[name]
    for prefix, label in NODE_PREFIX_LABELS:
        if name.startswith(prefix):
            return label
    return current


def _mxu_mtime_ns() -> int:
    try:
        return MXU_CONFIG_FILE.stat().st_mtime_ns
    except OSError:
        return 0


def _read_hud_mode(context=None, fallback: str = "adaptive", *, prefer_global: bool = False) -> str:
    if context is not None and not prefer_global:
        try:
            raw = str(node_action_params(context, "MaaNikki_TaskHud").get("mode") or "").strip()
            if raw in _HUD_CASE_TO_MODE:
                return _HUD_CASE_TO_MODE[raw]
        except Exception:
            pass
    try:
        if MXU_CONFIG_FILE.is_file():
            cfg = json.loads(MXU_CONFIG_FILE.read_text(encoding="utf-8-sig"))
            opt = (cfg.get("globalOptionValues") or {}).get("TaskHudMode") if isinstance(cfg, dict) else None
            if isinstance(opt, dict):
                case = str(opt.get("caseName") or "").strip()
                if case in _HUD_CASE_TO_MODE:
                    return _HUD_CASE_TO_MODE[case]
    except Exception:
        pass
    return fallback


def _compact_step(text: str, *, task_label: str = "") -> str:
    line = " ".join(str(text or "").splitlines()).strip().rstrip("。.;；")
    if not line:
        return ""
    if task_label and line.startswith(task_label + "："):
        line = line[len(task_label) + 1:].strip()
    elif task_label and line.startswith(task_label + " · "):
        line = line[len(task_label) + 3:].strip()
    return line


def acquire_game_hud(
    tasker_key: int,
    game_hwnd: int,
    controller=None,
    context=None,
    *,
    exclude_from_capture: bool | None = None,
    mode_setting: str | None = None,
) -> "GameHudOverlay | None":
    """Return or create the per-tasker capture-excluded GameHudOverlay."""
    if _CAPTURE_INCOMPATIBLE or os.name != "nt" or not game_hwnd:
        return None
    exclude = should_exclude_from_capture(controller) if exclude_from_capture is None else bool(exclude_from_capture)
    mode = _read_hud_mode(context) if mode_setting is None else mode_setting
    with _HUD_REGISTRY_LOCK:
        existing = _HUD_BY_TASKER.get(tasker_key)
        if existing is not None:
            if existing.alive() and existing.game == int(game_hwnd) and existing.exclude_from_capture == exclude:
                existing.retain_scope(mode_setting=mode)
                return existing
            _HUD_BY_TASKER.pop(tasker_key, None)
            existing.close()
        hud = GameHudOverlay(int(game_hwnd), exclude_from_capture=exclude, mode_setting=mode, tasker_key=tasker_key)
        if not hud.alive():
            return None
        _HUD_BY_TASKER[tasker_key] = hud
        return hud


def release_game_hud(tasker_key: int, *, immediate: bool = False) -> None:
    with _HUD_REGISTRY_LOCK:
        hud = _HUD_BY_TASKER.get(tasker_key)
        if hud is None:
            return
        if immediate:
            _HUD_BY_TASKER.pop(tasker_key, None)
    if hud is None:
        return
    if immediate:
        hud.close()
    else:
        hud.schedule_idle_close(0.75)


def close_all_huds() -> None:
    with _HUD_REGISTRY_LOCK:
        huds = list(_HUD_BY_TASKER.values())
        _HUD_BY_TASKER.clear()
    for hud in huds:
        try:
            hud.close()
        except Exception:
            pass


atexit.register(close_all_huds)


class _HudSurface:
    def __init__(self, *, exclude_from_capture: bool = True) -> None:
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.hwnd = None
        self._class_name = f"MaaNikkiTaskHud_{os.getpid()}_{id(self)}"
        self._atom = None
        self._screen_dc = None
        self._mem_dc = None
        self._scratch_dc = None
        self._scratch_bmp = None
        self._scratch_old = None
        self._bmp = None
        self._old = None
        self._pixels = None
        self._uint32_view = None
        self._w = 0
        self._h = 0
        self._exclude = bool(exclude_from_capture)
        self._visible = False
        self._select = bind(self.gdi, "SelectObject", w.HGDIOBJ, w.HDC, w.HGDIOBJ)
        self._delete = bind(self.gdi, "DeleteObject", w.BOOL, w.HGDIOBJ)
        self._create()

    def _create(self) -> None:
        try:
            try:
                set_thread_dpi = bind(self.user, "SetThreadDpiAwarenessContext", w.HANDLE, w.HANDLE)
                set_thread_dpi(ctypes.c_void_p(-4))
            except Exception:
                pass
            try:
                self.user.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
            except Exception:
                pass
            self._proc = PROC(self._wndproc)
            inst = bind(self.kernel, "GetModuleHandleW", w.HINSTANCE, w.LPCWSTR)(None)
            wc = WindowClass(
                style=0,
                proc=self._proc,
                extra=0,
                window_extra=0,
                instance=inst,
                icon=None,
                cursor=None,
                background=None,
                menu=None,
                name=self._class_name,
            )
            reg = bind(self.user, "RegisterClassW", w.ATOM, ctypes.POINTER(WindowClass))
            self._atom = checked(reg(ctypes.byref(wc)), "注册任务状态遮罩窗口类")
            create_ex = bind(
                self.user, "CreateWindowExW", w.HWND, w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID,
            )
            # WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE (0x080800A0)
            self.hwnd = checked(
                create_ex(0x080800A0, self._class_name, "MaaNikkiTaskHud", 0x80000000,
                          0, 0, 1, 1, None, None, inst, None),
                "创建任务状态遮罩窗口",
            )
            if self._exclude:
                affinity = bind(self.user, "SetWindowDisplayAffinity", w.BOOL, w.HWND, w.DWORD)
                affinity(self.hwnd, 0x00000011)
            get_dc = bind(self.user, "GetDC", w.HDC, w.HWND)
            create_dc = bind(self.gdi, "CreateCompatibleDC", w.HDC, w.HDC)
            self._screen_dc = checked(get_dc(None), "获取屏幕 DC")
            self._mem_dc = checked(create_dc(self._screen_dc), "创建遮罩内存 DC")
            self._scratch_dc = checked(create_dc(self._screen_dc), "创建测量 DC")
            self._scratch_bmp = checked(self._make_dib(1, 1)[0], "创建测量位图")
            self._scratch_old = checked(self._select(self._scratch_dc, self._scratch_bmp), "绑定测量位图")
        except Exception:
            self.destroy()
            raise

    def _make_dib(self, width: int, height: int):
        info = BitmapInfo()
        info.size, info.width, info.height = 40, max(1, int(width)), -max(1, int(height))
        info.planes, info.bits = 1, 32
        bits = ctypes.c_void_p()
        dib_fn = bind(self.gdi, "CreateDIBSection", w.HBITMAP, w.HDC, ctypes.POINTER(BitmapInfo),
                      w.UINT, ctypes.POINTER(ctypes.c_void_p), w.HANDLE, w.DWORD)
        bmp = checked(dib_fn(self._screen_dc, ctypes.byref(info), 0, ctypes.byref(bits), None, 0),
                      "创建任务状态遮罩 DIB")
        return bmp, bits

    def _ensure_canvas(self, width: int, height: int) -> None:
        if self._bmp is not None and self._w == width and self._h == height:
            return
        if self._old is not None and self._mem_dc is not None:
            self._select(self._mem_dc, self._old)
            self._old = None
        if self._bmp is not None:
            self._delete(self._bmp)
            self._bmp = None
        bmp, bits = self._make_dib(width, height)
        self._bmp = bmp
        self._old = checked(self._select(self._mem_dc, bmp), "绑定遮罩 DIB")
        buf = (ctypes.c_uint8 * (width * height * 4)).from_address(bits.value)
        arr = np.ctypeslib.as_array(buf).reshape(height, width, 4)
        self._pixels = arr
        self._uint32_view = arr.view(np.uint32).reshape(height, width)
        self._w, self._h = width, height

    def measure(self, client_w: int, client_h: int, *, badge: str, title: str, subtitle: str) -> dict:
        return measure_hud_card(
            self._scratch_dc, self.gdi, self._select, self._delete,
            client_w, client_h, badge=badge, title=title, subtitle=subtitle,
        )

    def paint_and_show(
        self,
        screen_x: int,
        screen_y: int,
        layout: dict,
        pad: int,
        theme,
        *,
        role: str,
        emphasized: bool,
        opacity: float,
        game_hwnd: int,
        want_topmost: bool,
        stay_above_game: bool,
    ) -> None:
        w_tot = layout["width"] + pad * 2
        h_tot = layout["height"] + pad * 2
        self._ensure_canvas(w_tot, h_tot)
        self._pixels.fill(0)
        draw_hud_card(
            self._pixels, self._uint32_view, self._mem_dc, self.gdi, self._select, self._delete,
            pad, pad, layout, theme, role=role, emphasized=emphasized, opacity=opacity,
        )
        pos = w.POINT(int(screen_x), int(screen_y))
        sz = w.SIZE(int(w_tot), int(h_tot))
        src = w.POINT(0, 0)
        blend = Blend(0, 0, 255, 1)
        ulw = bind(self.user, "UpdateLayeredWindow", w.BOOL, w.HWND, w.HDC, ctypes.POINTER(w.POINT),
                   ctypes.POINTER(w.SIZE), w.HDC, ctypes.POINTER(w.POINT), w.DWORD,
                   ctypes.POINTER(Blend), w.DWORD)
        checked(ulw(self.hwnd, None, ctypes.byref(pos), ctypes.byref(sz), self._mem_dc,
                    ctypes.byref(src), 0, ctypes.byref(blend), 2), "提交任务状态遮罩")
        if not self._visible:
            bind(self.user, "ShowWindow", w.BOOL, w.HWND, ctypes.c_int)(self.hwnd, 8)
            self._visible = True
        sync_overlay_zorder(self.user, self.hwnd, game_hwnd,
                            want_topmost=want_topmost, stay_above_game=stay_above_game)

    def hide(self) -> None:
        if self._visible and self.hwnd:
            bind(self.user, "ShowWindow", w.BOOL, w.HWND, ctypes.c_int)(self.hwnd, 0)
            self._visible = False

    def _wndproc(self, hwnd, msg, wp, lp):
        if msg == 0x0084:  # WM_NCHITTEST
            return -1  # HTTRANSPARENT
        if msg == 0x000F:  # WM_PAINT
            paint = Paint()
            begin = bind(self.user, "BeginPaint", w.HDC, w.HWND, ctypes.POINTER(Paint))
            end = bind(self.user, "EndPaint", w.BOOL, w.HWND, ctypes.POINTER(Paint))
            begin(hwnd, ctypes.byref(paint))
            end(hwnd, ctypes.byref(paint))
            return 0
        return bind(self.user, "DefWindowProcW", ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)(hwnd, msg, wp, lp)

    def destroy(self) -> None:
        if self.hwnd:
            bind(self.user, "DestroyWindow", w.BOOL, w.HWND)(self.hwnd)
            self.hwnd = None
        if self._old and self._mem_dc:
            self._select(self._mem_dc, self._old)
            self._old = None
        if self._scratch_old and self._scratch_dc:
            self._select(self._scratch_dc, self._scratch_old)
            self._scratch_old = None
        if self._bmp:
            self._delete(self._bmp)
            self._bmp = None
        if self._scratch_bmp:
            self._delete(self._scratch_bmp)
            self._scratch_bmp = None
        if self._mem_dc:
            bind(self.gdi, "DeleteDC", w.BOOL, w.HDC)(self._mem_dc)
            self._mem_dc = None
        if self._scratch_dc:
            bind(self.gdi, "DeleteDC", w.BOOL, w.HDC)(self._scratch_dc)
            self._scratch_dc = None
        if self._screen_dc:
            bind(self.user, "ReleaseDC", ctypes.c_int, w.HWND, w.HDC)(None, self._screen_dc)
            self._screen_dc = None
        if self._atom:
            inst = bind(self.kernel, "GetModuleHandleW", w.HINSTANCE, w.LPCWSTR)(None)
            bind(self.user, "UnregisterClassW", w.BOOL, w.LPCWSTR, w.HINSTANCE)(self._class_name, inst)
            self._atom = None
        self._visible = False


class GameHudOverlay:
    """Thread-owned Top-Center status & forced-foreground reason banner."""

    def __init__(
        self,
        game_hwnd: int,
        *,
        exclude_from_capture: bool = True,
        mode_setting: str = "adaptive",
        tasker_key: int | None = None,
    ) -> None:
        self.game = int(game_hwnd)
        self.exclude_from_capture = bool(exclude_from_capture)
        self._tasker_key = tasker_key
        self._lock = threading.Lock()
        self._mode_setting = mode_setting if mode_setting in {"adaptive", "always", "reason_only"} else "adaptive"
        self._control_mode = "pause"
        self._task_label = ""
        self._stage_label = ""
        self._step_text = ""
        self._route_title = ""
        self._route_subtitle = ""
        self._route_role = "accent"
        self._route_active = False
        self._exploration_badge = ""
        self._exploration_title = ""
        self._exploration_subtitle = ""
        self._exploration_role = "accent"
        self._forced_active = False
        self._forced_reason = ""
        self._forced_role = "forced"
        self._forced_flash_reason = ""
        self._forced_flash_until = 0.0
        self._recovered_msg = ""
        self._recovered_until = 0.0
        self._paused = False
        self._pause_reason = ""
        self._scope_active = True
        self._idle_deadline = 0.0
        self._dirty = True
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._surface: _HudSurface | None = None
        self._failed = False
        self._thread = threading.Thread(target=self._run, name="MaaNikkiTaskHud", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2.0)

    def alive(self) -> bool:
        return not self._failed and not self._stop.is_set() and self._thread.is_alive()

    def retain_scope(self, *, mode_setting: str | None = None) -> None:
        changed = False
        with self._lock:
            if not self._scope_active or self._idle_deadline != 0.0:
                self._scope_active = True
                self._idle_deadline = 0.0
                changed = True
            if mode_setting in {"adaptive", "always", "reason_only"} and mode_setting != self._mode_setting:
                self._mode_setting = mode_setting
                changed = True
            if changed:
                self._dirty = True
        if changed:
            self._wake.set()

    def schedule_idle_close(self, delay: float = 0.75) -> None:
        with self._lock:
            self._scope_active = False
            self._idle_deadline = time.monotonic() + max(0.05, float(delay))
            self._forced_active = False
            self._paused = False
            self._dirty = True
        self._wake.set()

    def set_context(
        self,
        *,
        control_mode: str | None = None,
        task_label: str | None = None,
        stage_label: str | None = None,
        step_text: str | None = None,
        mode_setting: str | None = None,
    ) -> None:
        changed = False
        with self._lock:
            if control_mode is not None:
                new_ctrl = str(control_mode)
                if new_ctrl != self._control_mode:
                    self._control_mode = new_ctrl
                    changed = True
            if task_label is not None and str(task_label).strip():
                new_task = str(task_label).strip()
                if new_task != self._task_label:
                    self._task_label = new_task
                    changed = True
                    if stage_label is None and self._stage_label:
                        self._stage_label = ""
            if stage_label is not None:
                new_stage = str(stage_label).strip()
                if new_stage != self._stage_label:
                    self._stage_label = new_stage
                    changed = True
            if step_text is not None:
                new_step = str(step_text).strip()
                if new_step != self._step_text:
                    self._step_text = new_step
                    changed = True
            if mode_setting in {"adaptive", "always", "reason_only"} and mode_setting != self._mode_setting:
                self._mode_setting = mode_setting
                changed = True
            if changed:
                self._dirty = True
        if changed:
            self._wake.set()

    def set_task(self, task_label: str, *, stage: str | None = None, step: str | None = None) -> None:
        self.set_context(task_label=task_label, stage_label=stage, step_text=step)

    def set_stage(self, stage_label: str) -> None:
        self.set_context(stage_label=stage_label)

    def set_step(self, text: str) -> None:
        self.set_context(step_text=text)

    def set_forced(
        self,
        reason: str,
        *,
        active: bool = True,
        role: str = "forced",
        flash_seconds: float = 0.0,
    ) -> None:
        new_active = bool(active)
        clean_reason = str(reason).strip() if reason else ""
        new_role = role or "forced"
        changed = False
        with self._lock:
            target_reason = clean_reason or self._forced_reason
            if (new_active, target_reason, new_role, False) != (
                self._forced_active, self._forced_reason, self._forced_role, self._paused
            ):
                self._forced_active = new_active
                self._forced_reason = target_reason
                self._forced_role = new_role
                self._paused = False
                changed = True
            if flash_seconds > 0.0 and target_reason:
                self._forced_flash_reason = target_reason
                self._forced_flash_until = time.monotonic() + float(flash_seconds)
                changed = True
            if changed:
                self._dirty = True
        if changed:
            self._wake.set()

    def clear_forced(
        self,
        *,
        flash_reason: str = "",
        flash_seconds: float = 0.0,
        recovered_msg: str = "",
        recovered_seconds: float = 0.0,
    ) -> None:
        clean_flash = str(flash_reason).strip() if (flash_reason and flash_seconds > 0.0) else ""
        clean_rec = str(recovered_msg).strip() if (recovered_msg and recovered_seconds > 0.0) else ""
        now = time.monotonic() if (clean_flash or clean_rec) else 0.0
        changed = False
        with self._lock:
            if self._forced_active or self._forced_reason:
                self._forced_active = False
                self._forced_reason = ""
                changed = True
            if clean_flash:
                self._forced_flash_reason = clean_flash
                self._forced_flash_until = now + float(flash_seconds)
                changed = True
            if clean_rec:
                self._recovered_msg = clean_rec
                self._recovered_until = now + float(recovered_seconds)
                changed = True
            if changed:
                self._dirty = True
        if changed:
            self._wake.set()

    def set_paused(self, paused: bool, *, reason: str = "") -> None:
        new_paused = bool(paused)
        clean_reason = str(reason).strip() if reason else None
        with self._lock:
            new_reason = clean_reason if clean_reason is not None else ("" if not new_paused else self._pause_reason)
            if new_paused == self._paused and new_reason == self._pause_reason:
                return
            self._paused = new_paused
            self._pause_reason = new_reason
            self._dirty = True
        self._wake.set()

    def is_background_mode(self) -> bool:
        with self._lock:
            return self._control_mode == "background"

    def set_route(self, title: str, subtitle: str = "", *, role: str = "accent", active: bool = True) -> None:
        new_active = bool(active and (title or subtitle))
        new_title = str(title or "").strip()
        new_sub = str(subtitle or "").strip()
        new_role = role or "accent"
        with self._lock:
            if (new_active, new_title, new_sub, new_role) == (
                self._route_active, self._route_title, self._route_subtitle, self._route_role
            ):
                return
            self._route_active = new_active
            self._route_title = new_title
            self._route_subtitle = new_sub
            self._route_role = new_role
            self._dirty = True
        self._wake.set()

    def clear_route(self) -> None:
        with self._lock:
            if not self._route_active and not self._route_title and not self._route_subtitle:
                return
            self._route_active = False
            self._route_title = ""
            self._route_subtitle = ""
            self._route_role = "accent"
            self._dirty = True
        self._wake.set()

    def set_exploration_hint(
        self,
        title: str,
        subtitle: str = "",
        *,
        badge: str = "",
        role: str = "accent",
        active: bool = True,
    ) -> None:
        new_badge = str(badge or "").strip() if active else ""
        new_title = str(title or "").strip() if active else ""
        new_sub = str(subtitle or "").strip() if active else ""
        new_role = role or "accent"
        with self._lock:
            if (new_badge, new_title, new_sub, new_role) == (
                self._exploration_badge, self._exploration_title,
                self._exploration_subtitle, self._exploration_role,
            ):
                return
            self._exploration_badge = new_badge
            self._exploration_title = new_title
            self._exploration_subtitle = new_sub
            self._exploration_role = new_role
            self._dirty = True
        self._wake.set()

    def clear_exploration_hint(self) -> None:
        self.set_exploration_hint("", "", active=False)

    def disable_due_to_capture(self, reason: str = "") -> None:
        global _CAPTURE_INCOMPATIBLE
        if reason:
            write_message(f"状态提示遮罩触发黑帧保护，已自动停用（{reason}）。")
        _CAPTURE_INCOMPATIBLE = True
        self.close()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=1.5)

    def _compose(self, now: float, fg_game: bool, fg_hud: bool):
        with self._lock:
            task = self._task_label.strip()
            stage = self._stage_label.strip()
            step = _compact_step(self._step_text, task_label=task)
            mode_setting = self._mode_setting
            ctrl = self._control_mode
            route_on = self._route_active
            route_title = self._route_title
            route_sub = self._route_subtitle
            route_role = self._route_role
            exp_badge = self._exploration_badge
            exp_title = self._exploration_title
            exp_sub = self._exploration_subtitle
            exp_role = self._exploration_role
            forced_on = self._forced_active
            forced_reason = self._forced_reason
            flash_reason = self._forced_flash_reason
            flash_until = self._forced_flash_until
            rec_msg = self._recovered_msg
            rec_until = self._recovered_until
            paused = self._paused
            pause_reason = self._pause_reason

        header_task = f"{task} · {stage}" if (task and stage and stage != task and not stage.startswith(task)) else (task or stage or "自动任务")
        focused = bool(fg_game or fg_hud)

        if forced_on and forced_reason:
            if ctrl == "return":
                return ("强制拉回", f"{header_task} · 正在拉回游戏前台", forced_reason, "forced", True, 0.94, focused, True)
            return ("临时前台", forced_reason, f"当前任务：{header_task} · 阶段完成后自动恢复后台", "forced", True, 0.94, focused, True)

        if flash_reason and now < flash_until:
            return ("自动拉回", f"{header_task} · 因离开前台被拉回", flash_reason, "forced", True, 0.93, focused, True)

        if rec_msg and now < rec_until:
            return ("恢复后台", f"{header_task} · 已恢复后台运行", rec_msg, "recovered", False, 0.90, focused, True)

        if paused:
            return ("已暂停", f"{header_task} · 等待切回游戏", pause_reason or "游戏离开前台已自动暂停，切回窗口后自动继续", "paused", False, 0.89, False, True)

        if route_on and (route_title or route_sub):
            badge = {"background": "后台跑图", "studio": "路线工具"}.get(ctrl, "自动寻路")
            return (badge, route_title or header_task, route_sub or step, route_role or "accent", False, 0.88, focused, ctrl == "background")

        if exp_title:
            return (exp_badge or "探索辅助", exp_title, exp_sub, exp_role or "accent", False, 0.88, focused, False)

        if mode_setting == "reason_only":
            return None

        if ctrl == "background":
            return ("后台运行", header_task, step or "不抢焦点运行中，遇到需要真实鼠标的阶段会提示切回", "accent", False, 0.87, focused, True)

        if not focused:
            return None

        if ctrl == "studio":
            return ("路线工作台", header_task, step, "accent", False, 0.86, True, True)

        if ctrl == "observation":
            return ("探索辅助", header_task, step or "按截图键或切换目标可自动提示点位", "accent", False, 0.86, True, True)

        if mode_setting == "always":
            badge = "自动拉回" if ctrl == "return" else "自动运行"
            return (badge, header_task, step, "accent", False, 0.86, True, True)

        compact_title = f"{header_task} · {step}" if step else header_task
        return ("自动运行", compact_title, "", "accent", False, 0.84, True, True)

    def _next_timeout(self, visible: bool) -> float:
        now = time.monotonic()
        wait = 0.25 if visible else 0.35
        with self._lock:
            for ts in (self._forced_flash_until, self._recovered_until):
                if ts > now:
                    wait = min(wait, max(0.05, ts - now + 0.02))
            if not self._scope_active and self._idle_deadline > 0.0:
                if self._idle_deadline <= now:
                    return 0.0
                wait = min(wait, max(0.05, self._idle_deadline - now + 0.01))
        return wait

    def _run(self) -> None:
        try:
            surface = _HudSurface(exclude_from_capture=self.exclude_from_capture)
        except Exception:
            self._failed = True
            self._ready.set()
            return
        self._surface = surface
        self._ready.set()
        user = surface.user
        msg = w.MSG()
        peek = bind(user, "PeekMessageW", w.BOOL, ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT)
        trans = bind(user, "TranslateMessage", w.BOOL, ctypes.POINTER(w.MSG))
        disp = bind(user, "DispatchMessageW", ctypes.c_ssize_t, ctypes.POINTER(w.MSG))
        is_window = bind(user, "IsWindow", w.BOOL, w.HWND)
        is_iconic = bind(user, "IsIconic", w.BOOL, w.HWND)
        get_fg = bind(user, "GetForegroundWindow", w.HWND)
        get_rect = bind(user, "GetClientRect", w.BOOL, w.HWND, ctypes.POINTER(w.RECT))
        to_screen = bind(user, "ClientToScreen", w.BOOL, w.HWND, ctypes.POINTER(w.POINT))
        move_win = bind(user, "SetWindowPos", w.BOOL, w.HWND, w.HWND, *([ctypes.c_int] * 4), w.UINT)

        last_paint_sig = None
        last_layout = None
        last_pos = None
        last_z = None
        next_z_sync = 0.0
        cfg_mtime_ns = _mxu_mtime_ns()
        next_cfg_poll = 0.0
        try:
            while not self._stop.is_set():
                while peek(ctypes.byref(msg), None, 0, 0, 1):
                    trans(ctypes.byref(msg))
                    disp(ctypes.byref(msg))
                self._wake.clear()
                now = time.monotonic()
                if now >= next_cfg_poll:
                    next_cfg_poll = now + 0.35
                    cur_mtime = _mxu_mtime_ns()
                    if cur_mtime != cfg_mtime_ns:
                        cfg_mtime_ns = cur_mtime
                        with self._lock:
                            self._mode_setting = _read_hud_mode(None, self._mode_setting, prefer_global=True)
                with self._lock:
                    idle_expired = (not self._scope_active) and self._idle_deadline > 0.0 and now >= self._idle_deadline
                    self._dirty = False
                if idle_expired or not is_window(self.game):
                    break

                rect, origin = w.RECT(), w.POINT(0, 0)
                if is_iconic(self.game) or not get_rect(self.game, ctypes.byref(rect)) or not to_screen(self.game, ctypes.byref(origin)):
                    surface.hide()
                    last_paint_sig = None
                    last_pos = None
                    last_z = None
                    next_z_sync = 0.0
                else:
                    cw, ch = int(rect.right - rect.left), int(rect.bottom - rect.top)
                    fg = get_fg()
                    fg_game = fg == self.game
                    fg_hud = bool(surface.hwnd and fg == surface.hwnd)
                    spec = self._compose(now, fg_game, fg_hud) if (cw > 0 and ch > 0) else None
                    if spec is None:
                        surface.hide()
                        last_paint_sig = None
                        last_pos = None
                        last_z = None
                        next_z_sync = 0.0
                    else:
                        badge, title, subtitle, role, emphasized, opacity, want_top, stay_above = spec
                        if not want_top and not stay_above:
                            surface.hide()
                            last_paint_sig = None
                            last_pos = None
                            last_z = None
                            next_z_sync = 0.0
                        else:
                            theme = resolve_overlay_theme()
                            sig = (badge, title, subtitle, role, emphasized, round(opacity, 2), cw, ch, theme.mode, theme.accent_name)
                            if sig != last_paint_sig or not surface._visible:
                                layout = surface.measure(cw, ch, badge=badge, title=title, subtitle=subtitle)
                                pad = max(6, round(6 * layout["s"]))
                                x_rel, y_rel = hud_top_center_anchor(cw, ch, layout["width"])
                                sx_pos, sy_pos = int(origin.x + x_rel - pad), int(origin.y + y_rel - pad)
                                surface.paint_and_show(
                                    sx_pos, sy_pos, layout, pad, theme,
                                    role=role, emphasized=emphasized, opacity=opacity,
                                    game_hwnd=self.game, want_topmost=want_top, stay_above_game=stay_above,
                                )
                                last_paint_sig, last_layout = sig, (layout, pad)
                                last_pos = (sx_pos, sy_pos)
                                last_z = (want_top, stay_above, fg)
                                next_z_sync = now + 0.25
                            elif last_layout is not None:
                                layout, pad = last_layout
                                x_rel, y_rel = hud_top_center_anchor(cw, ch, layout["width"])
                                sx_pos, sy_pos = int(origin.x + x_rel - pad), int(origin.y + y_rel - pad)
                                if (sx_pos, sy_pos) != last_pos:
                                    move_win(surface.hwnd, None, sx_pos, sy_pos, 0, 0, 0x0015)  # SWP_NOACTIVATE|SWP_NOZORDER|SWP_NOSIZE
                                    last_pos = (sx_pos, sy_pos)
                                z_sig = (want_top, stay_above, fg)
                                if z_sig != last_z or (not want_top and stay_above and now >= next_z_sync):
                                    sync_overlay_zorder(user, surface.hwnd, self.game,
                                                        want_topmost=want_top, stay_above_game=stay_above)
                                    last_z = z_sig
                                    next_z_sync = now + 0.25

                timeout = self._next_timeout(surface._visible)
                if timeout <= 0.0:
                    break
                self._wake.wait(timeout)
        finally:
            surface.destroy()
            if self._tasker_key is not None:
                with _HUD_REGISTRY_LOCK:
                    if _HUD_BY_TASKER.get(self._tasker_key) is self:
                        _HUD_BY_TASKER.pop(self._tasker_key, None)
