"""Shared MaaNikki theme tokens, typography metrics, and Win32 overlay primitives."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import ctypes
from ctypes import wintypes as w
import json
import math
from pathlib import Path
import time

import cv2
import numpy as np


ROOT_DIR = Path(__file__).resolve().parents[1]
MXU_CONFIG_FILE = ROOT_DIR / "config/mxu-MaaNikki.json"
FONT_FAMILY = "Microsoft YaHei"
PROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

MODE_THEMES = {
    "light": {
        "bg_primary": "#faf6f0",
        "bg_secondary": "#fffdfa",
        "bg_tertiary": "#f4eee5",
        "text_primary": "#352c27",
        "text_secondary": "#5f534b",
        "text_muted": "#786b60",
        "border": "#e8dfd2",
        "border_strong": "#cfc2b0",
    },
    "dark": {
        "bg_primary": "#1f1c1d",
        "bg_secondary": "#2a2627",
        "bg_tertiary": "#332e2f",
        "text_primary": "#f6f0ea",
        "text_secondary": "#d3c9c0",
        "text_muted": "#ab9f96",
        "border": "#433c3c",
        "border_strong": "#655b59",
    },
}

ACCENT_PRESETS = {
    "rosegold": {"default": "#b4536f", "secondary": "#8f6a1f", "light": "#f8e9ec", "lightDark": "#3b2a30", "dark_default": "#f0a8bd", "dark_secondary": "#e0bd72"},
    "gold": {"default": "#8f6a1f", "secondary": "#b4536f", "light": "#f5ecd8", "lightDark": "#3a3122", "dark_default": "#e0bd72", "dark_secondary": "#f0a8bd"},
    "celeste": {"default": "#2e7d7a", "secondary": "#8f6a1f", "light": "#e3f1f0", "lightDark": "#233332", "dark_default": "#8bd3cf", "dark_secondary": "#e0bd72"},
    "meadow": {"default": "#3f7a5c", "secondary": "#8f6a1f", "light": "#e4f0e8", "lightDark": "#25332b", "dark_default": "#8fcfac", "dark_secondary": "#e0bd72"},
    "starsea": {"default": "#4a5a9c", "secondary": "#7a5aa6", "light": "#e8ebf6", "lightDark": "#272c40", "dark_default": "#a9b6ec", "dark_secondary": "#c6aeea"},
    "dream": {"default": "#7a5aa6", "secondary": "#b4536f", "light": "#efe9f6", "lightDark": "#32293f", "dark_default": "#c6aeea", "dark_secondary": "#f0a8bd"},
    "slate": {"default": "#5a6476", "secondary": "#8f6a1f", "light": "#e9ebef", "lightDark": "#2b2e34", "dark_default": "#b0bac9", "dark_secondary": "#e0bd72"},
}


class BitmapInfo(ctypes.Structure):
    _fields_ = [("size", w.DWORD), ("width", w.LONG), ("height", w.LONG),
                ("planes", w.WORD), ("bits", w.WORD), ("compression", w.DWORD),
                ("image_size", w.DWORD), ("x_resolution", w.LONG),
                ("y_resolution", w.LONG), ("colors", w.DWORD),
                ("important_colors", w.DWORD), ("color_table", w.DWORD)]


class Blend(ctypes.Structure):
    _fields_ = [("operation", w.BYTE), ("flags", w.BYTE),
                ("alpha", w.BYTE), ("format", w.BYTE)]


class WindowClass(ctypes.Structure):
    _fields_ = [("style", w.UINT), ("proc", PROC), ("extra", ctypes.c_int),
                ("window_extra", w.INT), ("instance", w.HINSTANCE), ("icon", w.HICON),
                ("cursor", w.HANDLE), ("background", w.HBRUSH), ("menu", w.LPCWSTR),
                ("name", w.LPCWSTR)]


class Paint(ctypes.Structure):
    _fields_ = [("dc", w.HDC), ("erase", w.BOOL), ("rect", w.RECT),
                ("restore", w.BOOL), ("update", w.BOOL), ("reserved", w.BYTE * 32)]


class TextSize(ctypes.Structure):
    _fields_ = [("cx", w.LONG), ("cy", w.LONG)]


def bind(library, name, result, *args):
    function = getattr(library, name)
    function.restype, function.argtypes = result, list(args)
    return function


def checked(result, operation):
    if not result:
        raise OSError(f"{operation}失败（Win32={ctypes.get_last_error()}）。")
    return result


def hex_to_bgr(value: str, fallback: tuple[int, int, int] = (189, 168, 240)) -> tuple[int, int, int]:
    text = str(value or "").strip().lstrip("#")
    if len(text) != 6:
        return fallback
    try:
        r = int(text[0:2], 16)
        g = int(text[2:4], 16)
        b = int(text[4:6], 16)
        return (b, g, r)
    except ValueError:
        return fallback


def bgr_to_colorref(bgr: tuple[int, int, int]) -> int:
    b, g, r = int(bgr[0]), int(bgr[1]), int(bgr[2])
    return ((b & 0xFF) << 16) | ((g & 0xFF) << 8) | (r & 0xFF)


def mix_bgr(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    u = max(0.0, min(1.0, float(t)))
    return (
        round(a[0] * (1.0 - u) + b[0] * u),
        round(a[1] * (1.0 - u) + b[1] * u),
        round(a[2] * (1.0 - u) + b[2] * u),
    )


def on_color_ref(bgr: tuple[int, int, int]) -> int:
    b, g, r = bgr[0] / 255.0, bgr[1] / 255.0, bgr[2] / 255.0
    lum = 0.0722 * b + 0.7152 * g + 0.2126 * r
    return 0xEAF0F6 if lum < 0.42 else 0x1D1C1F


@dataclass(frozen=True)
class OverlayTheme:
    mode: str
    accent_name: str
    accent_bgr: tuple[int, int, int]
    accent_ref: int
    secondary_bgr: tuple[int, int, int]
    secondary_ref: int
    hud_bg_bgr: tuple[int, int, int]
    hud_elevated_bgr: tuple[int, int, int]
    hud_border_bgr: tuple[int, int, int]
    hud_title_ref: int
    hud_body_ref: int
    hud_muted_ref: int
    success_bgr: tuple[int, int, int]
    success_bg_bgr: tuple[int, int, int]
    warning_bgr: tuple[int, int, int]
    warning_bg_bgr: tuple[int, int, int]
    error_bgr: tuple[int, int, int]
    accent_bg_bgr: tuple[int, int, int]
    card_bg_bgr: tuple[int, int, int]
    card_surface_bgr: tuple[int, int, int]
    card_border_bgr: tuple[int, int, int]
    card_pill_bgr: tuple[int, int, int]
    card_title_ref: int
    card_body_ref: int
    card_muted_ref: int
    card_accent_ref: int
    card_glyph_bgr: tuple[int, int, int]
    card_glyph_active_bgr: tuple[int, int, int]

    @property
    def card_accent_bgr(self) -> tuple[int, int, int]:
        return (
            (self.card_accent_ref >> 16) & 0xFF,
            (self.card_accent_ref >> 8) & 0xFF,
            self.card_accent_ref & 0xFF,
        )


_SYSTEM_DARK_CACHE: tuple[float, bool] | None = None
_THEME_CACHE: tuple[int, bool, OverlayTheme] | None = None


def _system_prefers_dark() -> bool:
    global _SYSTEM_DARK_CACHE
    now = time.monotonic()
    if _SYSTEM_DARK_CACHE is not None and now - _SYSTEM_DARK_CACHE[0] < 5.0:
        return _SYSTEM_DARK_CACHE[1]
    dark = False
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            dark = int(value) == 0
    except OSError:
        dark = False
    _SYSTEM_DARK_CACHE = (now, dark)
    return dark


def resolve_overlay_theme() -> OverlayTheme:
    """Resolve current MaaNikki mode + accent tokens from config/mxu-MaaNikki.json."""
    global _THEME_CACHE
    try:
        mtime = MXU_CONFIG_FILE.stat().st_mtime_ns
    except OSError:
        mtime = 0
    sys_dark = _system_prefers_dark()
    if _THEME_CACHE is not None and _THEME_CACHE[0] == mtime and _THEME_CACHE[1] == sys_dark:
        return _THEME_CACHE[2]
    mode = "dark" if sys_dark else "light"
    accent_name = "rosegold"
    custom_entry = None
    if mtime:
        try:
            cfg = json.loads(MXU_CONFIG_FILE.read_text(encoding="utf-8-sig"))
            settings = cfg.get("settings") if isinstance(cfg, dict) else None
            if isinstance(settings, dict):
                raw_mode = settings.get("theme")
                if raw_mode in {"light", "dark"}:
                    mode = raw_mode
                elif raw_mode == "system":
                    mode = "dark" if sys_dark else "light"
                raw_accent = str(settings.get("accentColor") or "").strip()
                if raw_accent:
                    accent_name = raw_accent
                for item in settings.get("customAccents") or ():
                    if isinstance(item, dict) and item.get("name") == accent_name and isinstance(item.get("colors"), dict):
                        custom_entry = item["colors"]
                        break
        except Exception:
            pass
    theme = _build_theme(mode, accent_name, custom_entry)
    _THEME_CACHE = (mtime, sys_dark, theme)
    return theme


def _build_theme(mode: str, accent_name: str, custom_colors: dict | None = None) -> OverlayTheme:
    preset = ACCENT_PRESETS.get(accent_name, ACCENT_PRESETS["rosegold"])
    if custom_colors:
        accent_light_hex = custom_colors.get("default") or preset["default"]
        accent_dark_hex = custom_colors.get("darkDefault") or accent_light_hex
        secondary_hex = custom_colors.get("secondary") or preset["dark_secondary"]
        tint_light_hex = custom_colors.get("light") or preset["light"]
        tint_dark_hex = custom_colors.get("lightDark") or preset["lightDark"]
    else:
        accent_light_hex = preset["default"]
        accent_dark_hex = preset["dark_default"]
        secondary_hex = preset["dark_secondary"] if mode == "dark" else preset["secondary"]
        tint_light_hex = preset["light"]
        tint_dark_hex = preset["lightDark"]

    dark_tokens = MODE_THEMES["dark"]
    light_tokens = MODE_THEMES["light"]
    accent_bgr = hex_to_bgr(accent_dark_hex, (189, 168, 240))
    custom_sec = (custom_colors.get("darkSecondary") or custom_colors.get("secondary")) if custom_colors else preset["dark_secondary"]
    secondary_bgr = hex_to_bgr(custom_sec or preset["dark_secondary"], (114, 189, 224))
    accent_dark_tint = hex_to_bgr(tint_dark_hex, (48, 42, 59))
    hud_base = hex_to_bgr(dark_tokens["bg_primary"], (29, 28, 31))
    hud_sec = hex_to_bgr(dark_tokens["bg_secondary"], (39, 38, 42))
    hud_border_base = hex_to_bgr(dark_tokens["border_strong"], (89, 91, 101))
    hud_bg = mix_bgr(hud_base, accent_dark_tint, 0.24)
    hud_elevated = mix_bgr(hud_sec, accent_dark_tint, 0.28)
    hud_border = mix_bgr(hud_border_base, accent_bgr, 0.42)

    is_dark = mode == "dark"
    mt = dark_tokens if is_dark else light_tokens
    card_accent_hex = accent_dark_hex if is_dark else accent_light_hex
    card_accent_bgr = hex_to_bgr(card_accent_hex, accent_bgr)
    card_sec_hex = secondary_hex if is_dark else (preset["secondary"] if not custom_colors else secondary_hex)
    card_bg = hex_to_bgr(mt["bg_primary"])
    card_surface = hex_to_bgr(mt["bg_tertiary"])
    card_border = mix_bgr(hex_to_bgr(mt["border_strong"]), card_accent_bgr, 0.22)
    card_pill = hex_to_bgr(tint_dark_hex if is_dark else tint_light_hex, card_surface)

    return OverlayTheme(
        mode=mode,
        accent_name=accent_name,
        accent_bgr=accent_bgr,
        accent_ref=bgr_to_colorref(accent_bgr),
        secondary_bgr=secondary_bgr,
        secondary_ref=bgr_to_colorref(secondary_bgr),
        hud_bg_bgr=hud_bg,
        hud_elevated_bgr=hud_elevated,
        hud_border_bgr=hud_border,
        hud_title_ref=bgr_to_colorref(hex_to_bgr(dark_tokens["text_primary"])),
        hud_body_ref=bgr_to_colorref(hex_to_bgr(dark_tokens["text_secondary"])),
        hud_muted_ref=bgr_to_colorref(hex_to_bgr(dark_tokens["text_muted"])),
        success_bgr=hex_to_bgr("#9ed8b8"),
        success_bg_bgr=(44, 53, 36),
        warning_bgr=hex_to_bgr("#f0ca8b"),
        warning_bg_bgr=(34, 49, 58),
        error_bgr=hex_to_bgr("#ffa4b0"),
        accent_bg_bgr=accent_dark_tint,
        card_bg_bgr=card_bg,
        card_surface_bgr=card_surface,
        card_border_bgr=card_border,
        card_pill_bgr=card_pill,
        card_title_ref=bgr_to_colorref(hex_to_bgr(mt["text_primary"])),
        card_body_ref=bgr_to_colorref(hex_to_bgr(mt["text_secondary"])),
        card_muted_ref=bgr_to_colorref(hex_to_bgr(mt["text_muted"])),
        card_accent_ref=bgr_to_colorref(hex_to_bgr(card_accent_hex, card_accent_bgr)),
        card_glyph_bgr=mix_bgr(hex_to_bgr(mt["text_muted"]), card_accent_bgr, 0.25),
        card_glyph_active_bgr=hex_to_bgr(card_sec_hex, card_accent_bgr),
    )


def should_exclude_from_capture(controller) -> bool:
    """Return True unless Maa is using window-bound Background/PrintWindow capture."""
    try:
        from maa.define import MaaWin32ScreencapMethodEnum
        info = getattr(controller, "info", None)
        method = info.get("screencap_method", 0) if isinstance(info, dict) else 0
        background = int(MaaWin32ScreencapMethodEnum.Background)
        window_capture = type(method) is int and method > 0 and not (method & ~background)
        return not window_capture
    except Exception:
        return True


def sync_overlay_zorder(
    user,
    hwnd,
    game_hwnd,
    *,
    want_topmost: bool,
    stay_above_game: bool = False,
) -> None:
    """Place the overlay topmost when active, or right above game_hwnd in background."""
    get_window = bind(user, "GetWindow", w.HWND, w.HWND, w.UINT)
    get_style = bind(user, "GetWindowLongPtrW", ctypes.c_ssize_t, w.HWND, ctypes.c_int)
    get_class = bind(user, "GetClassNameW", ctypes.c_int, w.HWND, w.LPWSTR, ctypes.c_int)
    is_visible = bind(user, "IsWindowVisible", w.BOOL, w.HWND)
    move = bind(user, "SetWindowPos", w.BOOL, w.HWND, w.HWND, *([ctypes.c_int] * 4), w.UINT)
    swp_flags = 0x0013  # SWP_NOACTIVATE | SWP_NOMOVE | SWP_NOSIZE
    is_topmost = bool(get_style(hwnd, -20) & 0x00000008)

    if want_topmost:
        if not is_topmost:
            move(hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, swp_flags)
        return

    if not stay_above_game:
        return

    if is_topmost:
        move(hwnd, ctypes.c_void_p(-2), 0, 0, 0, 0, swp_flags)

    # Walk upward from game_hwnd to find the nearest visible foreign non-topmost
    # window above it, ignoring hidden system/IME/tooltip windows in between so
    # an already-positioned overlay does not re-trigger SetWindowPos on every tick.
    class_buf = ctypes.create_unicode_buffer(128)
    anchor = None
    cur = get_window(game_hwnd, 3)  # GW_HWNDPREV
    hops = 0
    while cur and hops < 256:
        hops += 1
        if int(cur) == int(hwnd):
            return
        if is_visible(cur) and not (get_style(cur, -20) & 0x00000008):
            length = get_class(cur, class_buf, len(class_buf))
            if not (length > 0 and class_buf.value.startswith("MaaNikki")):
                anchor = cur
                break
        cur = get_window(cur, 3)

    if anchor is None and int(get_window(hwnd, 3) or 0) == 0:
        return
    insert_after = anchor if anchor else ctypes.c_void_p(0)  # HWND_TOP within non-topmost band
    move(hwnd, insert_after, 0, 0, 0, 0, swp_flags)


def _premul(bgr: tuple[int, int, int], alpha: float) -> tuple[int, int, int, int]:
    a = max(0.0, min(1.0, float(alpha)))
    return (
        int(round(bgr[0] * a)),
        int(round(bgr[1] * a)),
        int(round(bgr[2] * a)),
        int(round(255.0 * a)),
    )


def _rounded_mask(h, w_px, radius):
    yy, xx = np.ogrid[:h, :w_px]
    r = max(1.0, float(radius))
    dx = np.maximum(np.maximum(r - xx, xx - (w_px - 1 - r)), 0.0)
    dy = np.maximum(np.maximum(r - yy, yy - (h - 1 - r)), 0.0)
    dist = np.hypot(dx, dy)
    return np.clip(r + 0.5 - dist, 0.0, 1.0).astype(np.float32)


def _blend_rect(pixels, x0, y0, x1, y1, color_bgr, radius=0, opacity=1.0, border_bgr=None, border_px=1.5):
    height, width = pixels.shape[:2]
    cx0, cy0 = max(0, round(x0)), max(0, round(y0))
    cx1, cy1 = min(width, round(x1)), min(height, round(y1))
    if cx0 >= cx1 or cy0 >= cy1:
        return
    w_box, h_box = max(1, round(x1 - x0)), max(1, round(y1 - y0))
    yy, xx = np.ogrid[cy0 - round(y0):cy1 - round(y0), cx0 - round(x0):cx1 - round(x0)]
    r = max(1.0, min(float(radius), min(w_box, h_box) / 2.0))
    dx = np.maximum(np.maximum(r - xx, xx - (w_box - 1 - r)), 0.0)
    dy = np.maximum(np.maximum(r - yy, yy - (h_box - 1 - r)), 0.0)
    dist = np.hypot(dx, dy)
    outer = np.clip(r + 0.5 - dist, 0.0, 1.0).astype(np.float32) * float(opacity)
    alpha = outer[..., None]
    fill = np.asarray(color_bgr, dtype=np.float32)
    if border_bgr is not None:
        inner = np.clip(r - border_px + 0.5 - dist, 0.0, 1.0).astype(np.float32)[..., None]
        b_col = np.asarray(border_bgr, dtype=np.float32)
        col = fill * inner + b_col * (1.0 - inner)
    else:
        col = fill
    target = pixels[cy0:cy1, cx0:cx1].astype(np.float32)
    src_pre = np.empty_like(target)
    src_pre[..., :3] = col * alpha
    src_pre[..., 3:] = alpha * 255.0
    pixels[cy0:cy1, cx0:cx1] = np.clip(
        np.rint(src_pre + target * (1.0 - alpha)), 0, 255).astype(np.uint8)


def _blend_stamp(pixels, stamp, left, top, circle=None, sx=1.0, sy=1.0):
    height, width = pixels.shape[:2]
    sh, sw = stamp.shape[:2]
    x0, y0 = max(0, left), max(0, top)
    x1, y1 = min(width, left + sw), min(height, top + sh)
    if x0 >= x1 or y0 >= y1:
        return
    source = stamp[y0 - top:y1 - top, x0 - left:x1 - left].astype(np.uint16)
    if circle:
        cx, cy, radius = circle
        yy, xx = np.ogrid[y0:y1, x0:x1]
        inside = ((xx / sx - cx) ** 2 + (yy / sy - cy) ** 2 <= radius ** 2)
        source = source * inside[..., None]
    target = pixels[y0:y1, x0:x1]
    target[:] = source + (target.astype(np.uint16) * (255 - source[..., 3:]) + 127) // 255


@lru_cache(maxsize=64)
def tinted_sparkle(size: int, outer_bgr: tuple[int, int, int], inner_bgr: tuple[int, int, int]):
    extent = max(24, size * 4)
    unit = extent / 64.0
    canvas = np.zeros((extent, extent, 4), dtype=np.float32)

    def poly(rx, ry, inner_r):
        pts = []
        for idx in range(8):
            r = (rx if idx % 2 == 0 else inner_r) if idx % 4 == 0 else (ry if idx % 2 == 0 else inner_r)
            ang = idx * math.pi / 4.0
            pts.append(((32.0 + math.sin(ang) * r) * unit, (32.0 - math.cos(ang) * r) * unit))
        mask = np.zeros((extent, extent), dtype=np.uint8)
        cv2.fillPoly(mask, [np.round(pts).astype(np.int32)], 255, lineType=cv2.LINE_AA)
        return mask.astype(np.float32) / 255.0

    for mask, col in ((poly(25.0, 25.0, 6.2), outer_bgr), (poly(13.0, 13.0, 3.2), inner_bgr)):
        a = mask[..., None]
        canvas[..., :3] = np.asarray(col, dtype=np.float32) * a + canvas[..., :3] * (1.0 - a)
        canvas[..., 3:] = a * 255.0 + canvas[..., 3:] * (1.0 - a)
    result = cv2.resize(canvas, (size, size), interpolation=cv2.INTER_AREA)
    return np.clip(np.rint(result), 0, 255).astype(np.uint8)


def hud_scale(client_w: int, client_h: int) -> tuple[float, float, float]:
    sx = max(0.35, client_w / 1280.0)
    sy = max(0.35, client_h / 720.0)
    s = max(0.82, min(sx, sy))
    return sx, sy, s


def hud_top_center_anchor(client_w: int, client_h: int, box_w: int, anchor_y: int | None = None) -> tuple[int, int]:
    sx, sy, s = hud_scale(client_w, client_h)
    y = max(6, round(14 * sy)) if anchor_y is None else anchor_y
    margin = max(6, round(10 * s))
    x = max(margin, min(max(margin, client_w - box_w - margin), (client_w - box_w) // 2))
    return x, y


def hud_max_width(client_w: int, client_h: int) -> int:
    sx, _, s = hud_scale(client_w, client_h)
    if client_w >= 900:
        return max(round(280 * s), min(client_w - round(450 * sx), round(620 * s)))
    return max(180, client_w - round(24 * s))


def role_colors(theme: OverlayTheme, role: str, *, emphasized: bool = False) -> dict:
    if role == "forced":
        stripe = theme.warning_bgr
        chip_bg = theme.warning_bgr
        chip_ref = 0x1D1C1F
        border = mix_bgr(theme.hud_border_bgr, theme.warning_bgr, 0.78 if emphasized else 0.58)
        sub_ref = bgr_to_colorref(mix_bgr(hex_to_bgr("#f6f0ea"), theme.warning_bgr, 0.35))
        return {"stripe": stripe, "chip_bg": chip_bg, "chip_ref": chip_ref, "border": border,
                "sparkle_outer": theme.warning_bgr, "sparkle_inner": theme.accent_bgr, "sub_ref": sub_ref}
    if role == "paused":
        stripe = theme.warning_bgr
        chip_bg = theme.warning_bg_bgr
        chip_ref = bgr_to_colorref(theme.warning_bgr)
        border = mix_bgr(theme.hud_border_bgr, theme.warning_bgr, 0.48)
        return {"stripe": stripe, "chip_bg": chip_bg, "chip_ref": chip_ref, "border": border,
                "sparkle_outer": theme.warning_bgr, "sparkle_inner": theme.secondary_bgr, "sub_ref": theme.hud_body_ref}
    if role == "recovered":
        stripe = theme.success_bgr
        chip_bg = theme.success_bg_bgr
        chip_ref = bgr_to_colorref(theme.success_bgr)
        border = mix_bgr(theme.hud_border_bgr, theme.success_bgr, 0.56)
        return {"stripe": stripe, "chip_bg": chip_bg, "chip_ref": chip_ref, "border": border,
                "sparkle_outer": theme.success_bgr, "sparkle_inner": theme.secondary_bgr, "sub_ref": theme.hud_body_ref}
    stripe = theme.accent_bgr
    chip_bg = theme.accent_bg_bgr
    chip_ref = theme.accent_ref
    border = mix_bgr(theme.hud_border_bgr, theme.accent_bgr, 0.65 if emphasized else 0.32)
    return {"stripe": stripe, "chip_bg": chip_bg, "chip_ref": chip_ref, "border": border,
            "sparkle_outer": theme.accent_bgr, "sparkle_inner": theme.secondary_bgr, "sub_ref": theme.hud_body_ref}


def _gdi_text_extent(dc, gdi, text: str) -> tuple[int, int]:
    if not text:
        return 0, 0
    extent_fn = bind(gdi, "GetTextExtentPoint32W", w.BOOL, w.HDC, w.LPCWSTR, ctypes.c_int, ctypes.POINTER(TextSize))
    sz = TextSize()
    checked(extent_fn(dc, text, len(text), ctypes.byref(sz)), "测量遮罩文字宽度")
    return int(sz.cx), int(sz.cy)


def _fit_text(dc, gdi, text: str, max_px: int) -> str:
    if not text or max_px <= 16:
        return ""
    w_px, _ = _gdi_text_extent(dc, gdi, text)
    if w_px <= max_px:
        return text
    lo, hi, best = 0, len(text), ""
    while lo <= hi:
        mid = (lo + hi) // 2
        cand = text[:mid].rstrip() + ("…" if mid < len(text) else "")
        cw, _ = _gdi_text_extent(dc, gdi, cand)
        if cw <= max_px:
            best = cand
            lo = mid + 1
        else:
            hi = mid - 1
    return best or "…"


def _wrap_text_px(dc, gdi, text: str, max_px: int, max_lines: int = 2) -> list[str]:
    clean = " ".join((text or "").split())
    if not clean or max_px <= 24:
        return []
    lines: list[str] = []
    rest = clean
    while rest and len(lines) < max_lines:
        if _gdi_text_extent(dc, gdi, rest)[0] <= max_px:
            lines.append(rest)
            rest = ""
            break
        if len(lines) == max_lines - 1:
            lines.append(_fit_text(dc, gdi, rest, max_px))
            rest = ""
            break
        lo, hi, cut = 1, len(rest), 1
        while lo <= hi:
            mid = (lo + hi) // 2
            if _gdi_text_extent(dc, gdi, rest[:mid])[0] <= max_px:
                cut = mid
                lo = mid + 1
            else:
                hi = mid - 1
        chunk = rest[:cut].rstrip()
        if not chunk:
            chunk = rest[:1]
            cut = 1
        lines.append(chunk)
        rest = rest[cut:].lstrip()
    return lines


def measure_hud_card(
    dc,
    gdi,
    select_fn,
    delete_fn,
    client_w: int,
    client_h: int,
    *,
    badge: str = "",
    title: str = "",
    subtitle: str = "",
    max_width: int | None = None,
) -> dict:
    """Measure exact pixel dimensions & wrapped lines for the unified Top-Center HUD."""
    _, _, s = hud_scale(client_w, client_h)
    font_fn = bind(gdi, "CreateFontW", w.HFONT, *([ctypes.c_int] * 5), *([w.DWORD] * 8), w.LPCWSTR)
    f_title_h = max(12, round(14 * s))
    f_body_h = max(11, round(12 * s))
    f_chip_h = max(10, round(11 * s))
    f_title = f_body = f_chip = None
    old_font = None
    try:
        f_title = checked(font_fn(-f_title_h, 0, 0, 0, 700, 0, 0, 0, 1, 0, 0, 5, 0, FONT_FAMILY), "创建标题字体")
        f_body = checked(font_fn(-f_body_h, 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 5, 0, FONT_FAMILY), "创建正文字体")
        f_chip = checked(font_fn(-f_chip_h, 0, 0, 0, 700, 0, 0, 0, 1, 0, 0, 5, 0, FONT_FAMILY), "创建标签字体")
        cap_w = max(180, min(client_w - round(20 * s), max_width or hud_max_width(client_w, client_h)))
        pad_l = round(14 * s)
        pad_r = round(12 * s)
        pad_y = round(8 * s)
        spark_sz = max(12, round(14 * s))
        spark_gap = round(7 * s)
        chip_pad_x = round(7 * s)
        chip_gap = round(8 * s)
        row_gap = round(4 * s)

        content_x_off = pad_l + spark_sz + spark_gap
        max_inner_w = max(80, cap_w - content_x_off - pad_r)

        chip_w = chip_h = chip_text_h = 0
        if badge:
            old_font = checked(select_fn(dc, f_chip), "选择测量字体")
            bw, bh = _gdi_text_extent(dc, gdi, badge)
            chip_w = bw + chip_pad_x * 2
            chip_text_h = bh
            chip_h = max(round(18 * s), bh + round(3 * s))

        h_first = select_fn(dc, f_title)
        if old_font is None:
            old_font = checked(h_first, "选择测量字体")
        title_avail = max(48, max_inner_w - (chip_w + chip_gap if badge else 0))
        fit_title = _fit_text(dc, gdi, " ".join((title or "").split()), title_avail)
        tw, th = _gdi_text_extent(dc, gdi, fit_title)
        header_row_w = (chip_w + (chip_gap if badge and fit_title else 0) + tw)
        header_row_h = max(chip_h, th, spark_sz)

        sub_lines: list[str] = []
        sub_h_line = 0
        sub_max_w = 0
        if subtitle:
            select_fn(dc, f_body)
            sub_lines = _wrap_text_px(dc, gdi, subtitle, max_inner_w, max_lines=2)
            for line in sub_lines:
                lw, lh = _gdi_text_extent(dc, gdi, line)
                sub_max_w = max(sub_max_w, lw)
                sub_h_line = max(sub_h_line, lh)

        inner_w = max(header_row_w, sub_max_w, round(120 * s))
        box_w = min(cap_w, content_x_off + inner_w + pad_r)
        sub_block_h = len(sub_lines) * (sub_h_line + round(2 * s)) - (round(2 * s) if sub_lines else 0)
        box_h = pad_y * 2 + header_row_h + ((row_gap + sub_block_h) if sub_lines else 0)

        return {
            "width": int(box_w),
            "height": int(box_h),
            "s": s,
            "badge": badge,
            "title": fit_title,
            "sub_lines": sub_lines,
            "pad_l": pad_l,
            "pad_y": pad_y,
            "spark_sz": spark_sz,
            "spark_gap": spark_gap,
            "chip_w": chip_w,
            "chip_h": chip_h,
            "chip_text_h": chip_text_h,
            "chip_pad_x": chip_pad_x,
            "chip_gap": chip_gap,
            "header_row_h": header_row_h,
            "row_gap": row_gap,
            "title_h": th,
            "chip_font_h": f_chip_h,
            "title_font_h": f_title_h,
            "body_font_h": f_body_h,
            "sub_h_line": sub_h_line,
        }
    finally:
        if old_font:
            select_fn(dc, old_font)
        for font_handle in (f_title, f_body, f_chip):
            if font_handle:
                delete_fn(font_handle)


def draw_hud_card(
    pixels: np.ndarray,
    uint32_view: np.ndarray,
    dc,
    gdi,
    select_fn,
    delete_fn,
    x0: int,
    y0: int,
    layout: dict,
    theme: OverlayTheme,
    *,
    role: str = "accent",
    emphasized: bool = False,
    opacity: float = 0.88,
) -> tuple[int, int, int, int]:
    """Paint the measured unified HUD banner onto a BGRA buffer at (x0, y0)."""
    s = layout["s"]
    box_w = layout["width"]
    box_h = layout["height"]
    x1, y1 = x0 + box_w, y0 + box_h
    colors = role_colors(theme, role, emphasized=emphasized)
    radius = max(6, round(10 * s))

    if emphasized:
        glow_pad = max(2, round(2.5 * s))
        _blend_rect(
            pixels, x0 - glow_pad, y0 - glow_pad, x1 + glow_pad, y1 + glow_pad,
            colors["stripe"], radius=radius + glow_pad, opacity=0.22,
        )
    sh_dx, sh_dy = round(2 * s), round(3 * s)
    _blend_rect(
        pixels, x0 + sh_dx, y0 + sh_dy, x1 + sh_dx, y1 + sh_dy,
        (10, 9, 12), radius=radius, opacity=0.30,
    )
    _blend_rect(
        pixels, x0, y0, x1, y1,
        theme.hud_bg_bgr, radius=radius, opacity=opacity,
        border_bgr=colors["border"], border_px=max(1.0, (1.6 if emphasized else 1.2) * s),
    )

    stripe_x = x0 + round(5 * s)
    stripe_w = max(2, round(3 * s))
    stripe_pad = max(6, round(8 * s))
    if y1 - stripe_pad > y0 + stripe_pad:
        _blend_rect(
            pixels, stripe_x, y0 + stripe_pad, stripe_x + stripe_w, y1 - stripe_pad,
            colors["stripe"], radius=stripe_w / 2.0, opacity=0.95,
        )

    spark_sz = layout["spark_sz"]
    spark_x = x0 + layout["pad_l"]
    spark_y = y0 + layout["pad_y"] + max(0, (layout["header_row_h"] - spark_sz) // 2)
    spark = tinted_sparkle(spark_sz, colors["sparkle_outer"], colors["sparkle_inner"])
    _blend_stamp(pixels, spark, spark_x, spark_y)

    content_x = spark_x + spark_sz + layout["spark_gap"]
    header_top = y0 + layout["pad_y"]
    if layout["badge"]:
        chip_y0 = header_top + max(0, (layout["header_row_h"] - layout["chip_h"]) // 2)
        chip_r = max(4, round(6 * s))
        _blend_rect(
            pixels, content_x, chip_y0, content_x + layout["chip_w"], chip_y0 + layout["chip_h"],
            colors["chip_bg"], radius=chip_r, opacity=0.96,
            border_bgr=colors["stripe"] if role != "forced" else None,
            border_px=max(0.8, 1.0 * s),
        )

    font_fn = bind(gdi, "CreateFontW", w.HFONT, *([ctypes.c_int] * 5), *([w.DWORD] * 8), w.LPCWSTR)
    text_out = bind(gdi, "TextOutW", w.BOOL, w.HDC, ctypes.c_int, ctypes.c_int, w.LPCWSTR, ctypes.c_int)
    set_bk = bind(gdi, "SetBkMode", ctypes.c_int, w.HDC, ctypes.c_int)
    set_color = bind(gdi, "SetTextColor", w.DWORD, w.HDC, w.DWORD)
    flush = bind(gdi, "GdiFlush", w.BOOL)

    bx0 = max(0, x0 - round(4 * s))
    by0 = max(0, y0 - round(3 * s))
    bx1 = min(pixels.shape[1], x1 + round(5 * s))
    by1 = min(pixels.shape[0], y1 + round(6 * s))
    sub_px = pixels[by0:by1, bx0:bx1]
    alpha_before = sub_px[..., 3].copy()
    rgb_slice = uint32_view[by0:by1, bx0:bx1]
    rgb_before = (rgb_slice & 0x00FFFFFF).copy()

    created = []
    old_font = None
    try:
        def make_font(height_px, weight):
            h = checked(font_fn(-height_px, 0, 0, 0, weight, 0, 0, 0, 1, 0, 0, 5, 0, FONT_FAMILY), "创建遮罩文字字体")
            created.append(h)
            return h

        f_chip = make_font(layout["chip_font_h"], 700) if layout["badge"] else None
        f_title = make_font(layout["title_font_h"], 700) if layout["title"] else None
        f_body = make_font(layout["body_font_h"], 400) if layout["sub_lines"] else None
        set_bk(dc, 1)

        title_x = content_x
        if layout["badge"] and f_chip:
            old_font = checked(select_fn(dc, f_chip), "选择标签字体")
            set_color(dc, colors["chip_ref"])
            chip_y0 = header_top + max(0, (layout["header_row_h"] - layout["chip_h"]) // 2)
            chip_ty = chip_y0 + max(0, (layout["chip_h"] - (layout.get("chip_text_h") or layout["chip_font_h"])) // 2)
            checked(text_out(dc, content_x + layout["chip_pad_x"], chip_ty, layout["badge"], len(layout["badge"])),
                    "绘制遮罩标签")
            title_x = content_x + layout["chip_w"] + layout["chip_gap"]

        if layout["title"] and f_title:
            h_sel = select_fn(dc, f_title)
            if old_font is None:
                old_font = checked(h_sel, "选择标题字体")
            set_color(dc, theme.hud_title_ref)
            title_ty = header_top + max(0, (layout["header_row_h"] - layout["title_h"]) // 2)
            checked(text_out(dc, title_x, title_ty, layout["title"], len(layout["title"])), "绘制遮罩标题")

        if layout["sub_lines"] and f_body:
            h_sel = select_fn(dc, f_body)
            if old_font is None:
                old_font = checked(h_sel, "选择正文字体")
            set_color(dc, colors["sub_ref"])
            sy_cur = header_top + layout["header_row_h"] + layout["row_gap"]
            step_y = layout["sub_h_line"] + round(2 * s)
            for line in layout["sub_lines"]:
                checked(text_out(dc, content_x, sy_cur, line, len(line)), "绘制遮罩正文")
                sy_cur += step_y

        checked(flush(), "提交遮罩绘制")
        changed = (rgb_slice & 0x00FFFFFF) != rgb_before
        sub_px[..., 3] = np.where(changed, 255, alpha_before)
    finally:
        if old_font:
            select_fn(dc, old_font)
        for handle in created:
            delete_fn(handle)

    return (bx0, by0, bx1, by1)


def render_top_center_banner(
    pixels: np.ndarray,
    uint32_view: np.ndarray,
    dc,
    gdi,
    select_fn,
    delete_fn,
    client_w: int,
    client_h: int,
    *,
    badge: str = "",
    title: str = "",
    subtitle: str = "",
    role: str = "accent",
    emphasized: bool = False,
    opacity: float = 0.88,
    anchor_y: int | None = None,
    theme: OverlayTheme | None = None,
) -> tuple[int, int, int, int]:
    """Measure and draw one unified Top-Center HUD card onto a full-client surface."""
    active_theme = theme or resolve_overlay_theme()
    layout = measure_hud_card(
        dc, gdi, select_fn, delete_fn, client_w, client_h,
        badge=badge, title=title, subtitle=subtitle,
    )
    x0, y0 = hud_top_center_anchor(client_w, client_h, layout["width"], anchor_y=anchor_y)
    return draw_hud_card(
        pixels, uint32_view, dc, gdi, select_fn, delete_fn,
        x0, y0, layout, active_theme, role=role, emphasized=emphasized, opacity=opacity,
    )
