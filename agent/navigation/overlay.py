"""Lightweight capture-excluded Win32 minimap route overlay for navigation and recording."""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import math
import os
import threading
import time

import cv2
import numpy as np

from map_mask.surface import BitmapInfo, Blend, _blend_rect, bind, checked
from message_log import write_message


PROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

ACTION_LABELS = {
    "": "途经",
    "teleport": "传送",
    "wait": "等待",
    "collect": "采集",
    "insect": "捕虫",
    "fishing": "钓鱼",
    "fishing_star": "钓星",
    "bottle": "漂流瓶",
    "macro": "连招",
    "key": "按键",
    "photo": "拍照",
    "place": "摆放",
    "music": "音乐",
    "chat": "聊天",
    "delivery": "投递",
    "animal": "动物",
}

# BGR colors for minimap waypoint dots.
ACTION_BGR = {
    "": (245, 245, 245),
    "teleport": (240, 108, 124),
    "wait": (44, 138, 194),
    "collect": (106, 157, 63),
    "insect": (58, 162, 122),
    "fishing": (196, 143, 47),
    "fishing_star": (196, 143, 47),
    "bottle": (168, 168, 74),
    "macro": (122, 86, 208),
    "key": (115, 127, 138),
}


class WindowClass(ctypes.Structure):
    _fields_ = [("style", w.UINT), ("proc", PROC), ("extra", ctypes.c_int),
                ("window_extra", w.INT), ("instance", w.HINSTANCE), ("icon", w.HICON),
                ("cursor", w.HANDLE), ("background", w.HBRUSH), ("menu", w.LPCWSTR),
                ("name", w.LPCWSTR)]


class Paint(ctypes.Structure):
    _fields_ = [("dc", w.HDC), ("erase", w.BOOL), ("rect", w.RECT),
                ("restore", w.BOOL), ("update", w.BOOL), ("reserved", w.BYTE * 32)]


class MinimapHudSurface:
    """Compact BGRA surface covering the top-left minimap and status pill region."""

    REF_W = 420
    REF_H = 230

    def __init__(self, client_w: int, client_h: int):
        self.client_w = max(1, client_w)
        self.client_h = max(1, client_h)
        self.sx = self.client_w / 1280.0
        self.sy = self.client_h / 720.0
        self.s = min(self.sx, self.sy)
        self.width = max(64, min(self.client_w, round(self.REF_W * self.sx)))
        self.height = max(64, min(self.client_h, round(self.REF_H * self.sy)))
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        self.dc = self.bitmap = self.old_bitmap = None
        self.pixels = None
        self.select = bind(self.gdi, "SelectObject", w.HANDLE, w.HDC, w.HANDLE)
        self.delete = bind(self.gdi, "DeleteObject", w.BOOL, w.HANDLE)
        self.delete_dc = bind(self.gdi, "DeleteDC", w.BOOL, w.HDC)
        try:
            self.dc = checked(bind(self.gdi, "CreateCompatibleDC", w.HDC, w.HDC)(None),
                              "创建小地图路线遮罩绘图缓冲")
            info = BitmapInfo()
            info.size = BitmapInfo.color_table.offset
            info.width, info.height = self.width, -self.height
            info.planes, info.bits = 1, 32
            bits = ctypes.c_void_p()
            make_bitmap = bind(self.gdi, "CreateDIBSection", w.HBITMAP, w.HDC,
                               ctypes.POINTER(BitmapInfo), w.UINT,
                               ctypes.POINTER(ctypes.c_void_p), w.HANDLE, w.DWORD)
            self.bitmap = checked(make_bitmap(self.dc, ctypes.byref(info), 0,
                                             ctypes.byref(bits), None, 0), "创建小地图路线遮罩位图")
            self.old_bitmap = checked(self.select(self.dc, self.bitmap), "选择小地图路线遮罩位图")
            storage = (ctypes.c_uint32 * (self.width * self.height)).from_address(bits.value)
            self.pixels = np.ctypeslib.as_array(storage).reshape(self.height, self.width)
        except Exception:
            self.close()
            raise
        self.update = bind(self.user, "UpdateLayeredWindow", w.BOOL, w.HWND, w.HDC,
                           ctypes.POINTER(w.POINT), ctypes.POINTER(w.SIZE), w.HDC,
                           ctypes.POINTER(w.POINT), w.DWORD, ctypes.POINTER(Blend), w.DWORD)

    def close(self):
        self.pixels = None
        if self.old_bitmap:
            self.select(self.dc, self.old_bitmap)
            self.old_bitmap = None
        if self.bitmap:
            self.delete(self.bitmap)
            self.bitmap = None
        if self.dc:
            self.delete_dc(self.dc)
            self.dc = None

    def present(self, hwnd, origin):
        size, source, blend = w.SIZE(self.width, self.height), w.POINT(0, 0), Blend(0, 0, 255, 1)
        checked(self.update(hwnd, None, ctypes.byref(origin), ctypes.byref(size), self.dc,
                            ctypes.byref(source), 0, ctypes.byref(blend), 2), "显示小地图路线图层")

    def draw(self, snapshot: dict) -> int:
        self.pixels.fill(0)
        sx, sy, s = self.sx, self.sy, self.s
        pixels = self.pixels.view(np.uint8).reshape(self.height, self.width, 4)

        cx_ref, cy_ref = snapshot["center"]
        r_ref = max(12.0, float(snapshot["radius"]) - 3.0)
        map_scale = max(0.05, float(snapshot["scale"]))
        pose = snapshot.get("pose")
        points = snapshot.get("points") or ()
        current_index = int(snapshot.get("current_index", 0))
        title_line = str(snapshot.get("title") or "").strip()
        sub_line = str(snapshot.get("subtitle") or "").strip()
        border_bgr = snapshot.get("border_bgr") or (120, 152, 180)
        title_color = int(snapshot.get("title_color", 0xFFF6EA))

        if pose is not None and len(pose) == 2:
            px_world, py_world = pose
            cx, cy = cx_ref * sx, cy_ref * sy
            rx, ry = r_ref * sx, r_ref * sy
            r_px = min(rx, ry)

            canvas_bgr = np.zeros((self.height, self.width, 3), dtype=np.uint8)
            canvas_alpha = np.zeros((self.height, self.width), dtype=np.uint8)

            def to_px(wx: float, wy: float) -> tuple[int, int]:
                mx = cx_ref + (wx - px_world) / map_scale
                my = cy_ref + (wy - py_world) / map_scale
                return round(mx * sx), round(my * sy)

            # Draw completed, current, and upcoming route segments.
            coords = [to_px(pt[0], pt[1]) for pt in points]
            line_w = max(2, round(2.4 * s))
            active_w = max(2, round(3.0 * s))
            for idx in range(1, len(points)):
                p0, p1 = coords[idx - 1], coords[idx]
                is_teleport = len(points[idx]) >= 5 and points[idx][4] == "teleport"
                if is_teleport:
                    continue
                if idx < current_index:
                    cv2.line(canvas_bgr, p0, p1, (190, 185, 175), line_w, cv2.LINE_AA)
                    cv2.line(canvas_alpha, p0, p1, 115, line_w, cv2.LINE_AA)
                elif idx == current_index:
                    cv2.line(canvas_bgr, p0, p1, (90, 215, 255), active_w, cv2.LINE_AA)
                    cv2.line(canvas_alpha, p0, p1, 235, active_w, cv2.LINE_AA)
                else:
                    cv2.line(canvas_bgr, p0, p1, (130, 145, 235), line_w, cv2.LINE_AA)
                    cv2.line(canvas_alpha, p0, p1, 185, line_w, cv2.LINE_AA)

            # Draw line from player center to current target waypoint.
            if 0 <= current_index < len(coords):
                p_cur = coords[current_index]
                c_pt = (round(cx), round(cy))
                cv2.line(canvas_bgr, c_pt, p_cur, (80, 230, 255), max(1, round(1.8 * s)), cv2.LINE_AA)
                cv2.line(canvas_alpha, c_pt, p_cur, 210, max(1, round(1.8 * s)), cv2.LINE_AA)

            # Draw waypoints from ends toward current_index so current target stays on top.
            dot_r = max(3, round(3.8 * s))
            act_r = max(4, round(5.2 * s))
            for idx, pt in enumerate(points):
                if idx == current_index:
                    continue
                x_px, y_px = coords[idx]
                if idx < current_index:
                    cv2.circle(canvas_bgr, (x_px, y_px), max(2, dot_r - 1), (185, 185, 185), -1, cv2.LINE_AA)
                    cv2.circle(canvas_alpha, (x_px, y_px), max(2, dot_r - 1), 120, -1, cv2.LINE_AA)
                else:
                    action = pt[4] if len(pt) >= 5 else ""
                    jump = bool(pt[3]) if len(pt) >= 4 else False
                    col = ACTION_BGR.get(action, (235, 235, 235)) if action else ((61, 163, 232) if jump else (245, 245, 245))
                    r_use = act_r if action else dot_r
                    cv2.circle(canvas_bgr, (x_px, y_px), r_use + 1, (30, 26, 24), -1, cv2.LINE_AA)
                    cv2.circle(canvas_bgr, (x_px, y_px), r_use, col, -1, cv2.LINE_AA)
                    cv2.circle(canvas_alpha, (x_px, y_px), r_use + 1, 225, -1, cv2.LINE_AA)

            # Highlight current target waypoint (or draw rim indicator if outside minimap circle).
            if 0 <= current_index < len(points):
                tx, ty = coords[current_index]
                pt = points[current_index]
                rad_world = float(pt[2]) if len(pt) >= 3 else 1.0
                rad_px = max(act_r + 3, round((rad_world / map_scale) * s))
                dx, dy = (tx - cx) / max(1.0, sx), (ty - cy) / max(1.0, sy)
                dist_ref = math.hypot(dx, dy)
                if dist_ref <= r_ref - 2.0:
                    if rad_px <= r_px * 0.75:
                        cv2.circle(canvas_bgr, (tx, ty), rad_px, (80, 225, 255), max(1, round(1.5 * s)), cv2.LINE_AA)
                        cv2.circle(canvas_alpha, (tx, ty), rad_px, 165, max(1, round(1.5 * s)), cv2.LINE_AA)
                    cv2.circle(canvas_bgr, (tx, ty), act_r + 2, (255, 255, 255), -1, cv2.LINE_AA)
                    cv2.circle(canvas_bgr, (tx, ty), act_r, (55, 195, 255), -1, cv2.LINE_AA)
                    cv2.circle(canvas_alpha, (tx, ty), act_r + 2, 250, -1, cv2.LINE_AA)
                else:
                    ang = math.atan2(dy, dx)
                    rim_x = round((cx_ref + math.cos(ang) * (r_ref - 5.0)) * sx)
                    rim_y = round((cy_ref + math.sin(ang) * (r_ref - 5.0)) * sy)
                    cv2.circle(canvas_bgr, (rim_x, rim_y), act_r + 2, (255, 255, 255), -1, cv2.LINE_AA)
                    cv2.circle(canvas_bgr, (rim_x, rim_y), act_r, (55, 195, 255), -1, cv2.LINE_AA)
                    cv2.circle(canvas_alpha, (rim_x, rim_y), act_r + 2, 250, -1, cv2.LINE_AA)

            # Clip minimap drawing to the minimap circle and composite into premultiplied BGRA.
            yy, xx = np.ogrid[:self.height, :self.width]
            dist_circle = np.hypot(xx / sx - cx_ref, yy / sy - cy_ref)
            clip = np.clip(r_ref + 0.5 - dist_circle, 0.0, 1.0).astype(np.float32)
            alpha_f = (canvas_alpha.astype(np.float32) / 255.0) * clip
            pixels[..., :3] = np.clip(np.rint(canvas_bgr.astype(np.float32) * alpha_f[..., None]), 0, 255).astype(np.uint8)
            pixels[..., 3] = np.clip(np.rint(alpha_f * 255.0), 0, 255).astype(np.uint8)

        # Draw status pill below the minimap.
        if title_line or sub_line:
            pill_x0 = round(22 * sx)
            pill_y0 = round(154 * sy)
            pill_h = round((48 if (title_line and sub_line) else 30) * sy)
            text_len = max(len(title_line), len(sub_line))
            pill_w = min(self.width - pill_x0 - round(6 * sx), max(round(176 * sx), round((28 + text_len * 12.2) * sx)))
            _blend_rect(pixels, pill_x0, pill_y0, pill_x0 + pill_w, pill_y0 + pill_h,
                        (38, 30, 26), radius=round(9 * s), opacity=0.88,
                        border_bgr=border_bgr, border_px=max(1.0, 1.4 * s))
            dc, gdi = self.dc, self.gdi
            text_out = bind(gdi, "TextOutW", w.BOOL, w.HDC, ctypes.c_int, ctypes.c_int, w.LPCWSTR, ctypes.c_int)
            font_fn = bind(gdi, "CreateFontW", w.HFONT, *([ctypes.c_int] * 5), *([w.DWORD] * 8), w.LPCWSTR)
            f_bold = f_sub = old_font = None
            try:
                alpha_before = pixels[..., 3].copy()
                rgb_before = (self.pixels & 0x00FFFFFF).copy()
                f_bold = checked(font_fn(-max(11, round(13 * s)), 0, 0, 0, 700, 0, 0, 0, 1, 0, 0, 5, 0, "Microsoft YaHei"), "创建小地图遮罩标题字体")
                f_sub = checked(font_fn(-max(10, round(12 * s)), 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 5, 0, "Microsoft YaHei"), "创建小地图遮罩正文字体")
                bind(gdi, "SetBkMode", ctypes.c_int, w.HDC, ctypes.c_int)(dc, 1)
                set_color = bind(gdi, "SetTextColor", w.DWORD, w.HDC, w.DWORD)
                tx = pill_x0 + round(10 * sx)
                if title_line:
                    old_font = checked(self.select(dc, f_bold), "选择小地图遮罩字体")
                    set_color(dc, title_color)
                    checked(text_out(dc, tx, pill_y0 + round(5 * sy), title_line, len(title_line)), "绘制小地图标题")
                if sub_line:
                    handle = self.select(dc, f_sub)
                    if old_font is None:
                        old_font = checked(handle, "选择小地图遮罩副字体")
                    set_color(dc, 0xD2E8F8)
                    ty = pill_y0 + round((24 if title_line else 6) * sy)
                    checked(text_out(dc, tx, ty, sub_line, len(sub_line)), "绘制小地图副标题")
                checked(bind(gdi, "GdiFlush", w.BOOL)(), "提交小地图遮罩文字")
                changed = (self.pixels & 0x00FFFFFF) != rgb_before
                pixels[..., 3] = np.where(changed, 255, alpha_before)
            finally:
                if old_font:
                    self.select(dc, old_font)
                if f_bold:
                    self.delete(f_bold)
                if f_sub:
                    self.delete(f_sub)

        return int(np.count_nonzero(pixels[..., 3]))



class RouteMinimapOverlay:
    """Capture-excluded Win32 overlay showing active route or recording on the in-game minimap."""

    def __init__(self, game_hwnd: int, spec, *, exclude_from_capture: bool = True):
        self.game = int(game_hwnd or 0)
        self.spec = spec
        self.exclude_from_capture = exclude_from_capture
        self.lock = threading.Lock()
        self.snapshot: dict | None = None
        self.updated_at = 0.0
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.thread: threading.Thread | None = None
        if self.game > 0:
            self.thread = threading.Thread(target=self._thread_main, name="route-minimap-overlay", daemon=True)
            self.thread.start()
            self.ready.wait(1.5)

    def publish_route(self, route, current_index: int, pose):
        if self.thread is None or route is None or pose is None or not getattr(pose, "position_reliable", False):
            self.hide()
            return
        pts = route.points
        idx = max(0, min(int(current_index), len(pts) - 1)) if pts else 0
        title = str(route.document.get("title") or route.id or "路线导航").strip()
        header = f"{title} · 第 {idx + 1}/{len(pts)} 点"
        subtitle = ""
        if pts:
            target = pts[idx]
            act_label = ACTION_LABELS.get(target.action or "", target.action or "途经")
            if target.jump and not target.action:
                act_label = "途经·跳跃"
            dist = math.hypot(target.x - pose.x, target.y - pose.y)
            subtitle = f"目标：{act_label}（距 {dist:.1f}）"
        point_tuples = tuple(
            (round(p.x, 2), round(p.y, 2), round(p.radius, 2), bool(p.jump), p.action or "")
            for p in pts
        )
        with self.lock:
            self.snapshot = {
                "center": self.spec.center,
                "radius": self.spec.radius,
                "scale": self.spec.scale,
                "pose": (round(float(pose.x), 2), round(float(pose.y), 2)),
                "points": point_tuples,
                "current_index": idx,
                "title": header,
                "subtitle": subtitle,
            }
            self.updated_at = time.monotonic()

    def publish_recording(self, samples: list, events: list, pose, state_text: str = "正在录制"):
        if self.thread is None or pose is None or not getattr(pose, "position_reliable", False):
            self.hide()
            return
        tail = samples[-60:] if len(samples) > 60 else list(samples)
        point_tuples = tuple((round(float(pt[0]), 2), round(float(pt[1]), 2), 1.0, False, "") for pt in tail)
        with self.lock:
            self.snapshot = {
                "center": self.spec.center,
                "radius": self.spec.radius,
                "scale": self.spec.scale,
                "pose": (round(float(pose.x), 2), round(float(pose.y), 2)),
                "points": point_tuples,
                "current_index": max(0, len(point_tuples) - 1),
                "title": f"{state_text} · {len(samples)} 采样 · {len(events)} 标记",
                "subtitle": "F6关键点 F7采集 F8等待 F9钓鱼 F10下落",
            }
            self.updated_at = time.monotonic()

    def publish_status(
        self,
        title: str,
        subtitle: str = "",
        *,
        border_bgr: tuple[int, int, int] = (120, 152, 180),
        title_color: int = 0xFFF6EA,
    ):
        if self.thread is None or (not title and not subtitle):
            self.hide()
            return
        with self.lock:
            self.snapshot = {
                "center": self.spec.center,
                "radius": self.spec.radius,
                "scale": self.spec.scale,
                "pose": None,
                "points": (),
                "current_index": 0,
                "title": title,
                "subtitle": subtitle,
                "border_bgr": border_bgr,
                "title_color": title_color,
            }
            self.updated_at = time.monotonic()

    def hide(self):
        with self.lock:
            self.snapshot = None
            self.updated_at = 0.0

    def close(self):
        self.hide()
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=0.8)
            self.thread = None

    def _thread_main(self):
        try:
            self._run()
        except Exception as error:
            write_message(f"小地图路线遮罩不可用（{type(error).__name__}）：{error}", None)
        finally:
            self.ready.set()

    def _run(self):
        user = ctypes.WinDLL("user32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        dpi = bind(user, "SetThreadDpiAwarenessContext", w.HANDLE, w.HANDLE)
        previous_dpi = dpi(ctypes.c_void_p(-4))
        module = bind(kernel, "GetModuleHandleW", w.HMODULE, w.LPCWSTR)(None)
        default = bind(user, "DefWindowProcW", ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        begin = bind(user, "BeginPaint", w.HDC, w.HWND, ctypes.POINTER(Paint))
        end = bind(user, "EndPaint", w.BOOL, w.HWND, ctypes.POINTER(Paint))
        hwnd = surface = None
        class_name = f"MaaNikkiRouteHud-{os.getpid()}-{id(self)}"
        registered = False

        @PROC
        def procedure(window, message, wp, lp):
            if message == 0x000F:
                ps = Paint()
                begin(window, ctypes.byref(ps))
                end(window, ctypes.byref(ps))
                return 0
            if message == 0x0014:
                return 1
            if message == 0x0084:
                return -1  # HTTRANSPARENT
            if message == 0x0021:
                return 3  # MA_NOACTIVATE
            return default(window, message, wp, lp)

        try:
            register = bind(user, "RegisterClassW", w.ATOM, ctypes.POINTER(WindowClass))
            checked(register(ctypes.byref(WindowClass(0, procedure, 0, 0, module, None, None,
                                                      None, None, class_name))), "注册小地图路线遮罩窗口")
            registered = True
            create = bind(user, "CreateWindowExW", w.HWND, w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
                          *([ctypes.c_int] * 4), w.HWND, w.HMENU, w.HINSTANCE, ctypes.c_void_p)
            exstyle = 0x08080088 | 0x00000020  # LAYERED | TOPMOST | TOOLWINDOW | NOACTIVATE | TRANSPARENT
            hwnd = checked(create(exstyle, class_name, "MaaNikki 路线导航遮罩", 0x80000000,
                                  0, 0, 1, 1, None, None, module, None), "创建小地图路线遮罩窗口")
            affinity = 0x11 if self.exclude_from_capture else 0
            bind(user, "SetWindowDisplayAffinity", w.BOOL, w.HWND, w.DWORD)(hwnd, affinity)
            get_client = bind(user, "GetClientRect", w.BOOL, w.HWND, ctypes.POINTER(w.RECT))
            to_screen = bind(user, "ClientToScreen", w.BOOL, w.HWND, ctypes.POINTER(w.POINT))
            foreground = bind(user, "GetForegroundWindow", w.HWND)
            valid = bind(user, "IsWindow", w.BOOL, w.HWND)
            iconic = bind(user, "IsIconic", w.BOOL, w.HWND)
            shown = bind(user, "IsWindowVisible", w.BOOL, w.HWND)
            show = bind(user, "ShowWindow", w.BOOL, w.HWND, ctypes.c_int)
            move = bind(user, "SetWindowPos", w.BOOL, w.HWND, w.HWND, *([ctypes.c_int] * 4), w.UINT)
            peek = bind(user, "PeekMessageW", w.BOOL, ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT)
            translate = bind(user, "TranslateMessage", w.BOOL, ctypes.POINTER(w.MSG))
            dispatch = bind(user, "DispatchMessageW", w.BOOL, ctypes.POINTER(w.MSG))
            self.ready.set()
            geometry = rendered = None
            next_topmost = 0.0

            while not self.stop.is_set():
                with self.lock:
                    snap, updated = self.snapshot, self.updated_at
                now = time.monotonic()
                fg = foreground()
                visible = (
                    snap is not None
                    and now - updated < 3.5
                    and bool(valid(self.game))
                    and not bool(iconic(self.game))
                    and fg in (self.game, hwnd)
                )
                if visible:
                    rect, origin = w.RECT(), w.POINT()
                    if get_client(self.game, ctypes.byref(rect)) and to_screen(self.game, ctypes.byref(origin)) and rect.right > 0 and rect.bottom > 0:
                        next_geom = (origin.x, origin.y, rect.right, rect.bottom)
                        if surface is None or (surface.client_w, surface.client_h) != (rect.right, rect.bottom):
                            if surface:
                                surface.close()
                            surface = MinimapHudSurface(rect.right, rect.bottom)
                            rendered = None
                        sig = (
                            snap.get("pose"),
                            snap.get("current_index"),
                            snap.get("title"),
                            snap.get("subtitle"),
                            len(snap.get("points") or ()),
                            snap.get("border_bgr"),
                        )
                        if sig != rendered:
                            surface.draw(snap)
                        if sig != rendered or next_geom != geometry or not shown(hwnd):
                            surface.present(hwnd, origin)
                            rendered, geometry = sig, next_geom
                        if not shown(hwnd):
                            show(hwnd, 4)
                        if now >= next_topmost:
                            move(hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, 0x0013)
                            next_topmost = now + 0.3
                    else:
                        visible = False
                if not visible and shown(hwnd):
                    show(hwnd, 0)
                    rendered = None
                msg = w.MSG()
                for _ in range(16):
                    if self.stop.is_set() or not peek(ctypes.byref(msg), hwnd, 0, 0, 1):
                        break
                    translate(ctypes.byref(msg))
                    dispatch(ctypes.byref(msg))
                self.stop.wait(1 / 45)
        finally:
            if hwnd:
                bind(user, "DestroyWindow", w.BOOL, w.HWND)(hwnd)
            if surface:
                surface.close()
            if registered:
                bind(user, "UnregisterClassW", w.BOOL, w.LPCWSTR, w.HINSTANCE)(class_name, module)
            if previous_dpi:
                dpi(previous_dpi)
