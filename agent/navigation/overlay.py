"""Capture-excluded Win32 route overlay with minimap trajectory and 3D AR ground guidance."""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import json
import math
import os
from pathlib import Path
import threading
import time

import cv2
import numpy as np

from map_mask.surface import BitmapInfo, Blend, _blend_rect, bind, checked
from message_log import write_message
from vision import read_image


PROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
NAV_ROOT = Path(__file__).resolve().parents[2] / "resource/navigation"

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


class _TerrainSampler:
    """Lazy sub-pixel bilinear terrain elevation reader backed by resource/navigation/terrain/<map_id>.png."""

    _cache: dict[str, "_TerrainSampler"] = {}
    _lock = threading.Lock()

    def __init__(self, map_id: str):
        self.px_scale = 0.5
        self.z_offset = -300.0
        self.z_scale = 64.0
        self.z_grid: np.ndarray | None = None
        self.valid_grid: np.ndarray | None = None
        self.h = 0
        self.w = 0
        try:
            manifest_path = NAV_ROOT / "terrain/manifest.json"
            if manifest_path.is_file():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                info = manifest.get(map_id)
                if info and info.get("image"):
                    self.px_scale = float(info.get("px_scale", 0.5))
                    self.z_offset = float(info.get("z_offset", -500.0))
                    self.z_scale = float(info.get("z_scale", 10.0))
                    img = read_image(NAV_ROOT / info["image"], cv2.IMREAD_COLOR)
                    if img is not None and img.ndim == 3:
                        self.h, self.w = img.shape[:2]
                        b = img[:, :, 0].astype(np.uint16)
                        g = img[:, :, 1].astype(np.uint16)
                        r = img[:, :, 2].astype(np.uint16)
                        hu16 = (r << 8) | g
                        valid = ((b & 1) != 0) & (hu16 > 0)
                        self.valid_grid = valid
                        self.z_grid = np.where(
                            valid,
                            hu16.astype(np.float32) / self.z_scale + self.z_offset,
                            np.nan,
                        )
        except Exception:
            self.z_grid = None
            self.valid_grid = None

    @classmethod
    def get(cls, map_id: str) -> "_TerrainSampler":
        with cls._lock:
            sampler = cls._cache.get(map_id)
            if sampler is None:
                sampler = cls(map_id)
                cls._cache[map_id] = sampler
            return sampler

    def sample_z(self, wx: float, wy: float, fallback: float = 0.0) -> float:
        if self.z_grid is None:
            return fallback
        gx = wx * self.px_scale
        gy = wy * self.px_scale
        x0 = int(math.floor(gx))
        y0 = int(math.floor(gy))
        if x0 < 0 or x0 >= self.w - 1 or y0 < 0 or y0 >= self.h - 1:
            ix = min(self.w - 1, max(0, int(round(gx))))
            iy = min(self.h - 1, max(0, int(round(gy))))
            v = float(self.z_grid[iy, ix])
            return fallback if math.isnan(v) else v
        fx = gx - x0
        fy = gy - y0
        z00 = float(self.z_grid[y0, x0])
        z10 = float(self.z_grid[y0, x0 + 1])
        z01 = float(self.z_grid[y0 + 1, x0])
        z11 = float(self.z_grid[y0 + 1, x0 + 1])
        if not (math.isnan(z00) or math.isnan(z10) or math.isnan(z01) or math.isnan(z11)):
            return (
                z00 * (1.0 - fx) * (1.0 - fy)
                + z10 * fx * (1.0 - fy)
                + z01 * (1.0 - fx) * fy
                + z11 * fx * fy
            )
        vals = [v for v in (z00, z10, z01, z11) if not math.isnan(v)]
        return sum(vals) / len(vals) if vals else fallback

    def sample_smooth(self, wx: float, wy: float, fallback: float = 0.0) -> float:
        z0 = self.sample_z(wx, wy, math.nan)
        if math.isnan(z0):
            return fallback
        acc = z0 * 2.0
        w_sum = 2.0
        for dx, dy in ((-1.6, 0.0), (1.6, 0.0), (0.0, -1.6), (0.0, 1.6)):
            zn = self.sample_z(wx + dx, wy + dy, math.nan)
            if not math.isnan(zn) and abs(zn - z0) <= 4.5:
                acc += zn
                w_sum += 1.0
        return acc / w_sum

    def sample_local_slope(self, wx: float, wy: float, ground_z: float) -> tuple[float, float]:
        """Return local terrain gradient (dz/dx_m, dz/dy_m) around (wx, wy) over a 4.5m baseline."""
        step_px = 5.0  # 4.5 meters in map space
        zx1 = self.sample_smooth(wx + step_px, wy, ground_z)
        zx0 = self.sample_smooth(wx - step_px, wy, ground_z)
        zy1 = self.sample_smooth(wx, wy + step_px, ground_z)
        zy0 = self.sample_smooth(wx, wy - step_px, ground_z)
        span_m = step_px * 2.0 * 0.90
        dz_dx = max(-0.35, min(0.35, (zx1 - zx0) / span_m))
        dz_dy = max(-0.35, min(0.35, (zy1 - zy0) / span_m))
        return dz_dx, dz_dy


def _premul(bgr: tuple[int, int, int], alpha: float) -> tuple[int, int, int, int]:
    a = max(0.0, min(1.0, float(alpha)))
    return (
        int(round(bgr[0] * a)),
        int(round(bgr[1] * a)),
        int(round(bgr[2] * a)),
        int(round(255.0 * a)),
    )


class WindowClass(ctypes.Structure):
    _fields_ = [("style", w.UINT), ("proc", PROC), ("extra", ctypes.c_int),
                ("window_extra", w.INT), ("instance", w.HINSTANCE), ("icon", w.HICON),
                ("cursor", w.HANDLE), ("background", w.HBRUSH), ("menu", w.LPCWSTR),
                ("name", w.LPCWSTR)]


class Paint(ctypes.Structure):
    _fields_ = [("dc", w.HDC), ("erase", w.BOOL), ("rect", w.RECT),
                ("restore", w.BOOL), ("update", w.BOOL), ("reserved", w.BYTE * 32)]


class MinimapHudSurface:
    """Full-client BGRA surface rendering both the 3D AR ground route and top-left minimap."""

    MM_REF_W = 420
    MM_REF_H = 230

    def __init__(self, client_w: int, client_h: int):
        self.client_w = max(1, client_w)
        self.client_h = max(1, client_h)
        self.sx = self.client_w / 1280.0
        self.sy = self.client_h / 720.0
        self.s = min(self.sx, self.sy)
        self.width = self.client_w
        self.height = self.client_h
        self.mm_w = max(64, min(self.client_w, round(self.MM_REF_W * self.sx)))
        self.mm_h = max(64, min(self.client_h, round(self.MM_REF_H * self.sy)))
        self.dirty_boxes: list[tuple[int, int, int, int]] = []
        self._smooth_pose: tuple[float, float, float] | None = None
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

    def _draw_text_pill(
        self,
        pixels: np.ndarray,
        x0: int,
        y0: int,
        x1: int,
        y1: int,
        title_line: str,
        sub_line: str = "",
        *,
        bg_bgr: tuple[int, int, int] = (38, 30, 26),
        border_bgr: tuple[int, int, int] = (120, 152, 180),
        title_color: int = 0xFFF6EA,
        font_scale: float = 1.0,
    ):
        sx, sy, s = self.sx, self.sy, self.s * font_scale
        x0 = max(2, min(self.width - 20, x0))
        y0 = max(2, min(self.height - 20, y0))
        x1 = max(x0 + 16, min(self.width - 2, x1))
        y1 = max(y0 + 14, min(self.height - 2, y1))
        _blend_rect(
            pixels, x0, y0, x1, y1, bg_bgr,
            radius=round(8 * s), opacity=0.86,
            border_bgr=border_bgr, border_px=max(1.0, 1.35 * s),
        )
        dc, gdi = self.dc, self.gdi
        text_out = bind(gdi, "TextOutW", w.BOOL, w.HDC, ctypes.c_int, ctypes.c_int, w.LPCWSTR, ctypes.c_int)
        font_fn = bind(gdi, "CreateFontW", w.HFONT, *([ctypes.c_int] * 5), *([w.DWORD] * 8), w.LPCWSTR)
        f_bold = f_sub = old_font = None
        try:
            sub_px = pixels[y0:y1, x0:x1]
            alpha_before = sub_px[..., 3].copy()
            rgb_slice = self.pixels[y0:y1, x0:x1]
            rgb_before = (rgb_slice & 0x00FFFFFF).copy()
            f_bold = checked(font_fn(-max(10, round(13 * s)), 0, 0, 0, 700, 0, 0, 0, 1, 0, 0, 5, 0, "Microsoft YaHei"), "创建遮罩标题字体")
            if sub_line:
                f_sub = checked(font_fn(-max(9, round(12 * s)), 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 5, 0, "Microsoft YaHei"), "创建遮罩正文字体")
            bind(gdi, "SetBkMode", ctypes.c_int, w.HDC, ctypes.c_int)(dc, 1)
            set_color = bind(gdi, "SetTextColor", w.DWORD, w.HDC, w.DWORD)
            tx = x0 + round(9 * sx * font_scale)
            if title_line:
                old_font = checked(self.select(dc, f_bold), "选择遮罩字体")
                set_color(dc, title_color)
                checked(text_out(dc, tx, y0 + round(4 * sy * font_scale), title_line, len(title_line)), "绘制遮罩标题")
            if sub_line and f_sub:
                handle = self.select(dc, f_sub)
                if old_font is None:
                    old_font = checked(handle, "选择遮罩副字体")
                set_color(dc, 0xD2E8F8)
                ty = y0 + round((23 if title_line else 5) * sy * font_scale)
                checked(text_out(dc, tx, ty, sub_line, len(sub_line)), "绘制遮罩副标题")
            checked(bind(gdi, "GdiFlush", w.BOOL)(), "提交遮罩文字")
            changed = (rgb_slice & 0x00FFFFFF) != rgb_before
            sub_px[..., 3] = np.where(changed, 255, alpha_before)
        finally:
            if old_font:
                self.select(dc, old_font)
            if f_bold:
                self.delete(f_bold)
            if f_sub:
                self.delete(f_sub)
        self.dirty_boxes.append((x0, y0, x1, y1))

    def _draw_3d_ar_route(
        self,
        pixels: np.ndarray,
        cur_x: float,
        cur_y: float,
        heading_deg: float,
        jump_z_m: float,
        points: tuple,
        current_index: int,
        map_id: str,
        now: float,
    ):
        if current_index < 0 or current_index >= len(points):
            return
        sampler = _TerrainSampler.get(map_id) if map_id else None
        ground_z = sampler.sample_smooth(cur_x, cur_y, 0.0) if sampler else 0.0
        dz_dx_m, dz_dy_m = sampler.sample_local_slope(cur_x, cur_y, ground_z) if sampler else (0.0, 0.0)

        rad = math.radians(heading_deg)
        fwd_x, fwd_y = math.sin(rad), -math.cos(rad)
        right_x, right_y = math.cos(rad), math.sin(rad)

        # Calibrated Unreal Engine third-person follow camera (90 deg horizontal FOV, 3.85m arm, 2.15m height, 10.4 deg pitch)
        cam_back_m = 3.85
        cam_h_m = 2.15 + jump_z_m
        pitch_rad = math.radians(10.4)
        cos_p, sin_p = math.cos(pitch_rad), math.sin(pitch_rad)
        focal = self.width * 0.50
        cx_scr, cy_scr = self.width * 0.50, self.height * 0.512

        def project_3d(wx: float, wy: float, wz: float, offset_x_m: float = 0.0, offset_y_m: float = 0.0):
            dx_m = (wx - cur_x) * 0.90 + offset_x_m
            dy_m = (wy - cur_y) * 0.90 + offset_y_m
            # Compensate for third-person SpringArm auto-pitching parallel to the local ground slope
            local_slope_z = dz_dx_m * dx_m + dz_dy_m * dy_m
            dz_m = (wz - ground_z - local_slope_z * 0.65) * 0.52
            cam_r = dx_m * right_x + dy_m * right_y
            horiz_f = dx_m * fwd_x + dy_m * fwd_y + cam_back_m
            vz = dz_m - cam_h_m
            cz = horiz_f * cos_p - vz * sin_p
            if cz <= 2.2:
                return None
            cam_u = horiz_f * sin_p + vz * cos_p
            scale = focal / cz
            sx_px = int(round(cx_scr + cam_r * scale))
            sy_px = int(round(cy_scr - cam_u * scale))
            return sx_px, sy_px, cz, scale

        # Build corner-rounded 3D path from character feet through upcoming waypoints (up to 44 meters)
        raw_chain = [(cur_x, cur_y, False, "")]
        for idx in range(current_index, min(len(points), current_index + 6)):
            pt = points[idx]
            act = pt[4] if len(pt) >= 5 else ""
            if idx > current_index and act == "teleport":
                break
            wx_pt, wy_pt = float(pt[0]), float(pt[1])
            if math.hypot(wx_pt - raw_chain[-1][0], wy_pt - raw_chain[-1][1]) * 0.90 < 0.35 and idx == current_index and idx + 1 < len(points):
                continue
            raw_chain.append((wx_pt, wy_pt, bool(pt[3]) if len(pt) >= 4 else False, act))

        # Subdivide with quadratic Bezier corner rounding at interior waypoints
        dense_2d: list[tuple[float, float, bool, str]] = []
        step_m = 0.75
        for i in range(len(raw_chain) - 1):
            x0, y0, _, _ = raw_chain[i]
            x1, y1, jmp1, act1 = raw_chain[i + 1]
            seg_len_m = math.hypot(x1 - x0, y1 - y0) * 0.90
            if seg_len_m < 0.15:
                continue
            n_sub = max(2, int(math.ceil(seg_len_m / step_m)))
            for s_i in range(n_sub + (1 if i == len(raw_chain) - 2 else 0)):
                t = s_i / n_sub
                dense_2d.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, jmp1, act1))

        # Apply 2 passes of Chaikin-style corner smoothing on (x, y) while keeping endpoints fixed
        if len(dense_2d) >= 5:
            for _ in range(2):
                smoothed = [dense_2d[0]]
                for i in range(1, len(dense_2d) - 1):
                    px = 0.25 * dense_2d[i - 1][0] + 0.50 * dense_2d[i][0] + 0.25 * dense_2d[i + 1][0]
                    py = 0.25 * dense_2d[i - 1][1] + 0.50 * dense_2d[i][1] + 0.25 * dense_2d[i + 1][1]
                    smoothed.append((px, py, dense_2d[i][2], dense_2d[i][3]))
                smoothed.append(dense_2d[-1])
                dense_2d = smoothed

        samples_3d = []  # (wx, wy, wz, tx, ty, cum_m, is_jump, act)
        cum_m = 0.0
        max_dist_m = 44.0
        raw_zs = []
        for wx, wy, _, _ in dense_2d:
            rz = sampler.sample_smooth(wx, wy, ground_z) if sampler else ground_z
            raw_zs.append(rz)
        # Smooth Z along curve to eliminate any residual micro-ripples
        smooth_zs = list(raw_zs)
        if len(raw_zs) >= 3:
            for i in range(1, len(raw_zs) - 1):
                smooth_zs[i] = 0.25 * raw_zs[i - 1] + 0.50 * raw_zs[i] + 0.25 * raw_zs[i + 1]

        for i in range(len(dense_2d)):
            wx, wy, seg_jump, seg_act = dense_2d[i]
            if i > 0:
                cum_m += math.hypot(wx - dense_2d[i - 1][0], wy - dense_2d[i - 1][1]) * 0.90
            if cum_m > max_dist_m:
                break
            if i + 1 < len(dense_2d):
                dx_t, dy_t = dense_2d[i + 1][0] - wx, dense_2d[i + 1][1] - wy
            elif i > 0:
                dx_t, dy_t = wx - dense_2d[i - 1][0], wy - dense_2d[i - 1][1]
            else:
                dx_t, dy_t = fwd_x, fwd_y
            t_len = max(1e-5, math.hypot(dx_t, dy_t))
            tx, ty = dx_t / t_len, dy_t / t_len
            blend_feet = min(1.0, cum_m / 4.0)
            wz = ground_z * (1.0 - blend_feet) + smooth_zs[i] * blend_feet
            samples_3d.append((wx, wy, wz, tx, ty, cum_m, seg_jump, seg_act))

        min_bx, min_by = self.width, self.height
        max_bx, max_by = 0, 0

        def mark_dirty(x0: int, y0: int, x1: int, y1: int, pad: int = 8):
            nonlocal min_bx, min_by, max_bx, max_by
            min_bx = min(min_bx, max(0, x0 - pad))
            min_by = min(min_by, max(0, y0 - pad))
            max_bx = max(max_bx, min(self.width, x1 + pad))
            max_by = max(max_by, min(self.height, y1 + pad))

        # 1. Project & draw 3D perspective ground ribbon + glowing edge rails
        half_w_m = 0.42
        proj_ribbon = []
        for wx, wy, wz, tx, ty, d_m, seg_jump, seg_act in samples_3d:
            # Perpendicular normal in map space: (-ty, tx)
            nx_m, ny_m = -ty * half_w_m, tx * half_w_m
            p_left = project_3d(wx, wy, wz + 0.08, -nx_m, -ny_m)
            p_ctr = project_3d(wx, wy, wz + 0.08, 0.0, 0.0)
            p_right = project_3d(wx, wy, wz + 0.08, nx_m, ny_m)
            if p_left and p_ctr and p_right:
                proj_ribbon.append((p_left, p_ctr, p_right, d_m, seg_jump, seg_act, wx, wy, wz, tx, ty))

        rail_w = max(2, round(2.2 * self.s))
        # Draw from far to near so closer translucent segments layer cleanly
        for i in range(len(proj_ribbon) - 2, -1, -1):
            l0, c0, r0, d0, jmp0, act0, *_ = proj_ribbon[i]
            l1, c1, r1, d1, *_ = proj_ribbon[i + 1]
            if d1 < 0.55:
                continue
            if (
                max(l0[0], r0[0], l1[0], r1[0]) < 0
                or min(l0[0], r0[0], l1[0], r1[0]) >= self.width
                or max(l0[1], r0[1], l1[1], r1[1]) < 0
                or min(l0[1], r0[1], l1[1], r1[1]) >= self.height
            ):
                continue
            fade = max(0.16, min(0.92, 1.0 - d0 / 45.0))
            if jmp0:
                base_bgr = (45, 215, 255)  # Electric gold-amber for jump segments
            elif act0:
                base_bgr = (165, 255, 75)  # Neon mint-emerald for action segments
            else:
                base_bgr = (255, 215, 45)  # Cyber cyan-blue for standard path

            quad = np.array([[l0[0], l0[1]], [r0[0], r0[1]], [r1[0], r1[1]], [l1[0], l1[1]]], dtype=np.int32)
            cv2.fillConvexPoly(pixels, quad, _premul(base_bgr, fade * 0.24), cv2.LINE_AA)
            cv2.line(pixels, (l0[0], l0[1]), (l1[0], l1[1]), _premul(base_bgr, fade * 0.82), rail_w, cv2.LINE_AA)
            cv2.line(pixels, (r0[0], r0[1]), (r1[0], r1[1]), _premul(base_bgr, fade * 0.82), rail_w, cv2.LINE_AA)
            mark_dirty(
                min(l0[0], r0[0], l1[0], r1[0]),
                min(l0[1], r0[1], l1[1], r1[1]),
                max(l0[0], r0[0], l1[0], r1[0]),
                max(l0[1], r0[1], l1[1], r1[1]),
                pad=rail_w + 4,
            )

        # 2. Draw animated flowing AR chevrons (>>>) along the 3D ground ribbon
        if len(samples_3d) >= 2:
            chev_spacing_m = 3.0
            phase_m = (now * 4.2) % chev_spacing_m
            chev_w = max(2, round(2.6 * self.s))
            for idx in range(len(samples_3d) - 1):
                wx0, wy0, wz0, tx, ty, d0, jmp0, _ = samples_3d[idx]
                wx1, wy1, wz1, _, _, d1, _, _ = samples_3d[idx + 1]
                if d1 <= d0:
                    continue
                # Check if a chevron tip falls within [d0, d1]
                k = math.ceil((max(1.2, d0) - phase_m) / chev_spacing_m)
                d_tip = phase_m + k * chev_spacing_m
                if d0 <= d_tip <= d1 and d_tip <= 38.0:
                    u = (d_tip - d0) / (d1 - d0)
                    cx_w = wx0 + (wx1 - wx0) * u
                    cy_w = wy0 + (wy1 - wy0) * u
                    cz_w = wz0 + (wz1 - wz0) * u + 0.10
                    nx_m, ny_m = -ty * 0.30, tx * 0.30
                    back_x_m, back_y_m = -tx * 0.68, -ty * 0.68
                    p_tip = project_3d(cx_w, cy_w, cz_w, 0.0, 0.0)
                    p_lw = project_3d(cx_w, cy_w, cz_w, back_x_m - nx_m, back_y_m - ny_m)
                    p_rw = project_3d(cx_w, cy_w, cz_w, back_x_m + nx_m, back_y_m + ny_m)
                    if p_tip and p_lw and p_rw:
                        fade = max(0.22, min(0.96, 1.0 - d_tip / 40.0))
                        c_bgr = (140, 245, 255) if jmp0 else (255, 250, 200)
                        col = _premul(c_bgr, fade * 0.92)
                        cv2.line(pixels, (p_lw[0], p_lw[1]), (p_tip[0], p_tip[1]), col, chev_w, cv2.LINE_AA)
                        cv2.line(pixels, (p_rw[0], p_rw[1]), (p_tip[0], p_tip[1]), col, chev_w, cv2.LINE_AA)
                        mark_dirty(
                            min(p_lw[0], p_rw[0], p_tip[0]),
                            min(p_lw[1], p_rw[1], p_tip[1]),
                            max(p_lw[0], p_rw[0], p_tip[0]),
                            max(p_lw[1], p_rw[1], p_tip[1]),
                            pad=chev_w + 4,
                        )

        # 3. Draw 3D Holographic Waypoint Pillars, Ground Rings & Floating Distance Badges
        badge_queue = []
        for wp_idx in range(min(len(points) - 1, current_index + 2), current_index - 1, -1):
            pt = points[wp_idx]
            act = pt[4] if len(pt) >= 5 else ""
            if wp_idx > current_index and act == "teleport":
                continue
            wx, wy = float(pt[0]), float(pt[1])
            dist_m = math.hypot(wx - cur_x, wy - cur_y) * 0.90
            if wp_idx > current_index and dist_m > 45.0:
                continue
            wz = sampler.sample_smooth(wx, wy, ground_z) if sampler else ground_z
            if dist_m < 3.2:
                wz = ground_z * (1.0 - dist_m / 3.2) + wz * (dist_m / 3.2)

            is_cur = wp_idx == current_index
            jmp = bool(pt[3]) if len(pt) >= 4 else False
            if act:
                b_bgr = ACTION_BGR.get(act, (165, 255, 75))
            elif jmp:
                b_bgr = (45, 215, 255)
            else:
                b_bgr = (255, 215, 55) if is_cur else (225, 190, 95)

            # Ground 3D arrival ring
            ring_r_m = min(2.2, max(0.65, float(pt[2] if len(pt) >= 3 else 1.0) * 0.85)) if is_cur else 0.55
            ring_pts = []
            for a_i in range(16):
                ang = (a_i / 16.0) * math.pi * 2.0
                pr = project_3d(wx, wy, wz + 0.08, math.cos(ang) * ring_r_m, math.sin(ang) * ring_r_m)
                if pr:
                    ring_pts.append([pr[0], pr[1]])
            if len(ring_pts) >= 6:
                arr = np.array(ring_pts, dtype=np.int32)
                ring_w = max(2, round((2.4 if is_cur else 1.6) * self.s))
                cv2.polylines(pixels, [arr], True, _premul(b_bgr, 0.82 if is_cur else 0.50), ring_w, cv2.LINE_AA)
                mark_dirty(int(arr[:, 0].min()), int(arr[:, 1].min()), int(arr[:, 0].max()), int(arr[:, 1].max()), pad=6)

            # Vertical holographic beacon pillar
            pillar_h_m = 2.45 if is_cur else 1.55
            p_base = project_3d(wx, wy, wz + 0.08)
            p_top = project_3d(wx, wy, wz + pillar_h_m)
            if p_base and p_top and 0 <= p_base[0] < self.width and 0 <= p_base[1] < self.height:
                bx, by = p_base[0], p_base[1]
                tx_p, ty_p = p_top[0], p_top[1]
                glow_w = max(4, round((6.0 if is_cur else 3.5) * self.s))
                core_w = max(2, round((2.5 if is_cur else 1.6) * self.s))
                cv2.line(pixels, (bx, by), (tx_p, ty_p), _premul(b_bgr, 0.32 if is_cur else 0.20), glow_w, cv2.LINE_AA)
                cv2.line(pixels, (bx, by), (tx_p, ty_p), _premul((255, 255, 255), 0.90 if is_cur else 0.62), core_w, cv2.LINE_AA)
                orb_r = max(4, round((5.5 if is_cur else 3.8) * self.s))
                cv2.circle(pixels, (tx_p, ty_p), orb_r + 2, _premul(b_bgr, 0.85), -1, cv2.LINE_AA)
                cv2.circle(pixels, (tx_p, ty_p), max(2, orb_r - 1), _premul((255, 255, 255), 0.95), -1, cv2.LINE_AA)
                mark_dirty(min(bx, tx_p), min(by, ty_p), max(bx, tx_p), max(by, ty_p), pad=orb_r + 8)
                if wp_idx <= current_index + 1:
                    act_label = ACTION_LABELS.get(act, act or ("跳跃" if jmp else "途经"))
                    tag = f"#{wp_idx + 1} {act_label} · {dist_m:.1f}m" if is_cur else f"#{wp_idx + 1} {act_label} · {dist_m:.0f}m"
                    badge_queue.append((tx_p, ty_p, tag, b_bgr, is_cur))

        if max_bx > min_bx and max_by > min_by:
            self.dirty_boxes.append((min_bx, min_by, max_bx, max_by))

        # Render floating text pills above pillar tops
        mm_excl_w = round(218 * self.sx)
        mm_excl_h = round(186 * self.sy)
        for tx_p, ty_p, tag, b_bgr, is_cur in badge_queue:
            f_scale = 0.92 if is_cur else 0.80
            pw = round((22 + len(tag) * 9.6) * self.sx * f_scale)
            ph = round(25 * self.sy * f_scale)
            px0 = tx_p - pw // 2
            py0 = ty_p - ph - round(10 * self.sy)
            # Keep outside top-left circular minimap area
            if px0 < mm_excl_w and py0 < mm_excl_h:
                continue
            self._draw_text_pill(
                pixels, px0, py0, px0 + pw, py0 + ph, tag,
                bg_bgr=(34, 24, 18), border_bgr=b_bgr,
                title_color=0xFFF8E8 if is_cur else 0xD8E6F2,
                font_scale=f_scale,
            )

        # 4. Off-Screen AR Turn Indicator when current target is outside camera horizontal FOV
        tgt_idx = current_index
        if math.hypot(float(points[tgt_idx][0]) - cur_x, float(points[tgt_idx][1]) - cur_y) * 0.90 < 0.85 and tgt_idx + 1 < len(points):
            tgt_idx += 1
        tgt = points[tgt_idx]
        tx_w, ty_w = float(tgt[0]), float(tgt[1])
        dist_tgt_m = math.hypot(tx_w - cur_x, ty_w - cur_y) * 0.90
        if dist_tgt_m >= 0.8:
            tgt_bearing = math.degrees(math.atan2(tx_w - cur_x, cur_y - ty_w)) % 360.0
            ang_err = (tgt_bearing - heading_deg + 180.0) % 360.0 - 180.0
            if abs(ang_err) > 27.0:
                is_right = ang_err > 0
                turn_txt = f"右转 {abs(ang_err):.0f}° · {dist_tgt_m:.1f}m >>>" if is_right else f"<<< 左转 {abs(ang_err):.0f}° · {dist_tgt_m:.1f}m"
                tw = round((24 + len(turn_txt) * 9.5) * self.sx * 0.92)
                th = round(28 * self.sy)
                ix0 = (self.width - tw - round(28 * self.sx)) if is_right else round(28 * self.sx)
                iy0 = round(self.height * 0.52)
                self._draw_text_pill(
                    pixels, ix0, iy0, ix0 + tw, iy0 + th, turn_txt,
                    bg_bgr=(36, 26, 20), border_bgr=(55, 205, 255),
                    title_color=0x90EEFF, font_scale=0.92,
                )

    def draw(self, snapshot: dict, now: float | None = None) -> int:
        if now is None:
            now = time.monotonic()
        # Clear only dirty regions from previous frame for <0.05ms full-screen turnaround
        if self.dirty_boxes:
            for x0, y0, x1, y1 in self.dirty_boxes:
                self.pixels[y0:y1, x0:x1] = 0
            self.dirty_boxes.clear()
        else:
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
            px_world, py_world = float(pose[0]), float(pose[1])
            heading = snapshot.get("heading")
            # Dead-reckon forward movement and jump arc at 30 FPS between observations
            jump_z_m = 0.0
            if snapshot.get("ar_active") and heading is not None:
                pose_time = float(snapshot.get("pose_time") or now)
                move_until = float(snapshot.get("move_until") or 0.0)
                vel = float(snapshot.get("velocity") or 0.0)
                if move_until > pose_time and now > pose_time and vel > 0.1:
                    dt_move = min(now - pose_time, move_until - pose_time, 0.85)
                    rad_h = math.radians(float(heading))
                    px_world += math.sin(rad_h) * vel * dt_move
                    py_world -= math.cos(rad_h) * vel * dt_move
                jump_start = float(snapshot.get("jump_start") or 0.0)
                if 0.0 < now - jump_start < 1.25:
                    jump_z_m = 2.2 * math.sin(math.pi * ((now - jump_start) / 1.25))

                h_val = float(heading)
                if self._smooth_pose is not None:
                    sx_prev, sy_prev, sh_prev = self._smooth_pose
                    dh = (h_val - sh_prev + 180.0) % 360.0 - 180.0
                    if math.hypot(px_world - sx_prev, py_world - sy_prev) <= 6.5 and abs(dh) <= 65.0:
                        px_world = sx_prev + (px_world - sx_prev) * 0.42
                        py_world = sy_prev + (py_world - sy_prev) * 0.42
                        h_val = (sh_prev + dh * 0.42) % 360.0
                self._smooth_pose = (px_world, py_world, h_val)

                self._draw_3d_ar_route(
                    pixels,
                    px_world,
                    py_world,
                    h_val,
                    jump_z_m,
                    points,
                    current_index,
                    str(snapshot.get("map_id") or ""),
                    now,
                )

            # Top-Left Minimap Overlay (drawn in mm_h x mm_w sub-region)
            mm_h, mm_w = self.mm_h, self.mm_w
            cx, cy = cx_ref * sx, cy_ref * sy
            rx, ry = r_ref * sx, r_ref * sy
            r_px = min(rx, ry)

            canvas_bgr = np.zeros((mm_h, mm_w, 3), dtype=np.uint8)
            canvas_alpha = np.zeros((mm_h, mm_w), dtype=np.uint8)

            def to_px(wx: float, wy: float) -> tuple[int, int]:
                mx = cx_ref + (wx - px_world) / map_scale
                my = cy_ref + (wy - py_world) / map_scale
                return round(mx * sx), round(my * sy)

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

            if 0 <= current_index < len(coords):
                p_cur = coords[current_index]
                c_pt = (round(cx), round(cy))
                cv2.line(canvas_bgr, c_pt, p_cur, (80, 230, 255), max(1, round(1.8 * s)), cv2.LINE_AA)
                cv2.line(canvas_alpha, c_pt, p_cur, 210, max(1, round(1.8 * s)), cv2.LINE_AA)

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

            yy, xx = np.ogrid[:mm_h, :mm_w]
            dist_circle = np.hypot(xx / sx - cx_ref, yy / sy - cy_ref)
            clip = np.clip(r_ref + 0.5 - dist_circle, 0.0, 1.0).astype(np.float32)
            alpha_f = (canvas_alpha.astype(np.float32) / 255.0) * clip
            mm_slice = pixels[:mm_h, :mm_w]
            mask_nz = alpha_f > 0.01
            mm_slice[mask_nz, :3] = np.clip(
                np.rint(canvas_bgr[mask_nz].astype(np.float32) * alpha_f[mask_nz, None]), 0, 255
            ).astype(np.uint8)
            mm_slice[mask_nz, 3] = np.clip(np.rint(alpha_f[mask_nz] * 255.0), 0, 255).astype(np.uint8)
            self.dirty_boxes.append((0, 0, mm_w, mm_h))

        if title_line or sub_line:
            pill_x0 = round(22 * sx)
            pill_y0 = round(154 * sy)
            pill_h = round((48 if (title_line and sub_line) else 30) * sy)
            text_len = max(len(title_line), len(sub_line))
            pill_w = min(self.mm_w - pill_x0 - round(6 * sx), max(round(176 * sx), round((28 + text_len * 12.2) * sx)))
            self._draw_text_pill(
                pixels,
                pill_x0,
                pill_y0,
                pill_x0 + pill_w,
                pill_y0 + pill_h,
                title_line,
                sub_line,
                border_bgr=border_bgr,
                title_color=title_color,
            )

        return len(self.dirty_boxes)



class RouteMinimapOverlay:
    """Capture-excluded Win32 overlay showing active 3D AR route guidance and minimap trajectory."""

    def __init__(self, game_hwnd: int, spec, *, exclude_from_capture: bool = True):
        self.game = int(game_hwnd or 0)
        self.spec = spec
        self.exclude_from_capture = exclude_from_capture
        self.lock = threading.Lock()
        self.snapshot: dict | None = None
        self.updated_at = 0.0
        self.last_heading: float | None = None
        self.move_until: float = 0.0
        self.velocity: float = 0.0
        self.jump_start: float = 0.0
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.thread: threading.Thread | None = None
        if self.game > 0:
            self.thread = threading.Thread(target=self._thread_main, name="route-minimap-overlay", daemon=True)
            self.thread.start()
            self.ready.wait(1.5)

    def notify_camera_turn(self, delta_deg: float):
        if not math.isfinite(delta_deg):
            return
        with self.lock:
            if self.last_heading is not None:
                self.last_heading = (self.last_heading + float(delta_deg)) % 360.0
            if self.snapshot is not None and self.snapshot.get("heading") is not None:
                self.snapshot = {
                    **self.snapshot,
                    "heading": (float(self.snapshot["heading"]) + float(delta_deg)) % 360.0,
                }
                self.updated_at = time.monotonic()

    def notify_motion(self, velocity: float, duration: float, jumping: bool):
        now = time.monotonic()
        with self.lock:
            self.velocity = max(0.0, float(velocity))
            self.move_until = now + max(0.0, float(duration))
            if jumping and now - self.jump_start > 0.35:
                self.jump_start = now
            if self.snapshot is not None:
                self.snapshot = {
                    **self.snapshot,
                    "velocity": self.velocity,
                    "move_until": self.move_until,
                    "jump_start": self.jump_start,
                }
                self.updated_at = now

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
        now = time.monotonic()
        raw_h = getattr(pose, "heading", None)
        with self.lock:
            if raw_h is not None and math.isfinite(float(raw_h)):
                self.last_heading = float(raw_h) % 360.0
            eff_heading = self.last_heading
            self.snapshot = {
                "center": self.spec.center,
                "radius": self.spec.radius,
                "scale": self.spec.scale,
                "map_id": getattr(self.spec, "id", ""),
                "pose": (round(float(pose.x), 2), round(float(pose.y), 2)),
                "pose_time": now,
                "heading": eff_heading,
                "ar_active": eff_heading is not None,
                "velocity": self.velocity,
                "move_until": self.move_until,
                "jump_start": self.jump_start,
                "points": point_tuples,
                "current_index": idx,
                "title": header,
                "subtitle": subtitle,
            }
            self.updated_at = now

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
                "map_id": getattr(self.spec, "id", ""),
                "pose": (round(float(pose.x), 2), round(float(pose.y), 2)),
                "ar_active": False,
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
                "ar_active": False,
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
                return -1
            if message == 0x0021:
                return 3
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
                        ar_active = bool(snap.get("ar_active"))
                        sig = (
                            int(now * 28) if ar_active else None,
                            snap.get("pose"),
                            snap.get("heading"),
                            snap.get("current_index"),
                            snap.get("title"),
                            snap.get("subtitle"),
                            len(snap.get("points") or ()),
                            snap.get("border_bgr"),
                        )
                        if sig != rendered:
                            surface.draw(snap, now)
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
                self.stop.wait(1 / 30)
        finally:
            if hwnd:
                bind(user, "DestroyWindow", w.BOOL, w.HWND)(hwnd)
            if surface:
                surface.close()
            if registered:
                bind(user, "UnregisterClassW", w.BOOL, w.LPCWSTR, w.HINSTANCE)(class_name, module)
            if previous_dpi:
                dpi(previous_dpi)
