"""A persistent BGRA surface submitted directly to the Windows compositor."""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import numpy as np

from overlay_style import (
    FONT_FAMILY,
    BitmapInfo,
    Blend,
    _blend_rect,
    _blend_stamp,
    _gdi_text_extent,
    _rounded_mask,
    bind,
    checked,
    mix_bgr,
    render_top_center_banner,
    resolve_overlay_theme,
)
from .icons import marker_icon, ui_glyph


def split_exploration_hint(hint: str) -> tuple[str, str, str]:
    clean = " ".join((hint or "").split())
    if not clean:
        return ("探索辅助", "", "accent")
    if "登录" in clean:
        return ("探索辅助", clean, "paused")
    for prefix in ("地图标记：", "地图标记:"):
        if clean.startswith(prefix):
            return ("地图标记", clean[len(prefix):].strip() or clean, "accent")
    return ("探索辅助", clean, "accent")


def _wrap_lines(text, width_chars=21, max_lines=3):
    lines = []
    for raw_line in (text or "").splitlines() or [""]:
        line = raw_line.strip()
        while len(line) > width_chars:
            lines.append(line[:width_chars])
            line = line[width_chars:]
            if len(lines) >= max_lines:
                lines[-1] = lines[-1][:-1] + "…"
                return lines
        if line:
            lines.append(line)
            if len(lines) >= max_lines:
                return lines
    return lines or ["暂无文字说明"]


class LayeredSurface:
    def __init__(self, width, height):
        self.width, self.height = width, height
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        self.dc = self.bitmap = self.old_bitmap = None
        self.pixels = None
        self.select = bind(self.gdi, "SelectObject", w.HANDLE, w.HDC, w.HANDLE)
        self.delete = bind(self.gdi, "DeleteObject", w.BOOL, w.HANDLE)
        self.delete_dc = bind(self.gdi, "DeleteDC", w.BOOL, w.HDC)
        try:
            self.dc = checked(bind(self.gdi, "CreateCompatibleDC", w.HDC, w.HDC)(None),
                              "创建遮罩绘图缓冲")
            info = BitmapInfo()
            info.size = BitmapInfo.color_table.offset
            info.width, info.height = width, -height
            info.planes, info.bits = 1, 32
            bits = ctypes.c_void_p()
            make_bitmap = bind(self.gdi, "CreateDIBSection", w.HBITMAP, w.HDC,
                               ctypes.POINTER(BitmapInfo), w.UINT,
                               ctypes.POINTER(ctypes.c_void_p), w.HANDLE, w.DWORD)
            self.bitmap = checked(make_bitmap(self.dc, ctypes.byref(info), 0,
                                             ctypes.byref(bits), None, 0), "创建遮罩透明位图")
            self.old_bitmap = checked(self.select(self.dc, self.bitmap), "选择遮罩透明位图")
            storage = (ctypes.c_uint32 * (width * height)).from_address(bits.value)
            self.pixels = np.ctypeslib.as_array(storage).reshape(height, width)
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

    def draw(self, points, hint, circle, *, active_key=None, card=None, delegate_hint=False):
        self.pixels.fill(0)
        dc, gdi = self.dc, self.gdi
        text_out = bind(gdi, "TextOutW", w.BOOL, w.HDC, ctypes.c_int, ctypes.c_int, w.LPCWSTR, ctypes.c_int)
        font = bind(gdi, "CreateFontW", w.HFONT, *([ctypes.c_int] * 5), *([w.DWORD] * 8), w.LPCWSTR)
        sx, sy = self.width / 1280.0, self.height / 720.0
        s = min(sx, sy)
        pixels = self.pixels.view(np.uint8).reshape(self.height, self.width, 4)
        theme = resolve_overlay_theme()
        badge_rim = mix_bgr(theme.accent_bgr, (255, 255, 255), 0.28)
        objects = []
        old_font = None
        text_items = []

        def keep(handle, operation):
            checked(handle, operation)
            objects.append(handle)
            return handle

        try:
            size = max(10, round((24 if circle else 38) * s))
            for entry in points:
                x, y, kind, count = entry[:4]
                is_active = (not circle and active_key is not None
                             and (round(x), round(y), kind) == active_key)
                icon_sz = round(size * 1.12) if is_active else size
                icon = marker_icon(kind, icon_sz, active=is_active, badge=True)
                px, py = x * sx, y * sy
                _blend_stamp(pixels, icon, round(px - icon_sz / 2), round(py - icon_sz / 2),
                             circle=circle, sx=sx, sy=sy)
                if count > 1 and not circle:
                    label = str(count)
                    bw = max(round(18 * s), round((11 + 7 * len(label)) * s))
                    bh = max(14, round(17 * s))
                    bx0 = round(px + icon_sz * 0.16)
                    by0 = round(py - icon_sz * 0.46)
                    _blend_rect(pixels, bx0, by0, bx0 + bw, by0 + bh,
                                theme.hud_bg_bgr, radius=bh / 2, border_bgr=badge_rim, border_px=max(1.0, 1.3 * s))
                    tx = bx0 + max(3, round((bw - len(label) * 7.2 * s) / 2))
                    ty = by0 + max(1, round(1.5 * s))
                    text_items.append(("badge", tx, ty, theme.hud_title_ref, label))

            visible_hint = bool(hint and not delegate_hint)
            if visible_hint:
                h_badge, h_title, h_role = split_exploration_hint(hint)
                render_top_center_banner(
                    pixels,
                    self.pixels,
                    self.dc,
                    self.gdi,
                    self.select,
                    self.delete,
                    self.width,
                    self.height,
                    badge=h_badge,
                    title=h_title,
                    subtitle="",
                    role=h_role,
                    theme=theme,
                )
            self.card_layout = None
            if card and not circle:
                tut = card["tutorial"]
                ax, ay = card["anchor"][0] * sx, card["anchor"][1] * sy
                idx, total = card.get("index", 0), card.get("total", 1)
                hover = card.get("hover")
                cw = round(388 * s)
                pad = round(18 * s)
                lines = _wrap_lines(tut.content, width_chars=21, max_lines=3)
                has_diff = bool(tut.difficulty)
                has_img = tut.image is not None
                img_h = round(186 * s) if has_img else (round(76 * s) if tut.loading else 0)
                footer_rows = int(bool(tut.contributor)) + int(bool(tut.updated_at))
                ch = (round(74 * s) + (round(30 * s) if has_diff else 0)
                      + (img_h + round(12 * s) if img_h else 0)
                      + len(lines) * round(22 * s)
                      + (footer_rows * round(19 * s) + round(8 * s) if footer_rows else 0)
                      + round(14 * s))
                cx0 = round(ax + 24 * s)
                if cx0 + cw > self.width - round(16 * s):
                    cx0 = max(round(16 * s), round(ax - 24 * s - cw))
                cy0 = max(round(18 * s), min(self.height - ch - round(18 * s), round(ay - ch + 28 * s)))
                cx1, cy1 = cx0 + cw, cy0 + ch
                text_items = [item for item in text_items
                              if item[0] != "badge" or not (cx0 - 16 <= item[1] <= cx1 + 8
                                                            and cy0 - 16 <= item[2] <= cy1 + 8)]

                _blend_rect(pixels, cx0 + round(3 * s), cy0 + round(5 * s),
                            cx1 + round(3 * s), cy1 + round(5 * s),
                            (14, 11, 12), radius=round(14 * s), opacity=0.34)
                _blend_rect(pixels, cx0, cy0, cx1, cy1, theme.card_bg_bgr,
                            radius=round(14 * s), border_bgr=theme.card_border_bgr, border_px=max(1.0, 1.5 * s))
                bar_x = cx1 - round(7 * s)
                bar_col = mix_bgr(theme.card_border_bgr, theme.card_accent_bgr, 0.58)
                _blend_rect(pixels, bar_x, cy0 + round(44 * s), bar_x + max(2, round(3 * s)),
                            cy1 - round(24 * s), bar_col, radius=round(1.5 * s))

                head_icon = marker_icon(tut.kind, max(16, round(26 * s)), badge=False)
                _blend_stamp(pixels, head_icon, cx0 + pad, cy0 + round(14 * s))
                text_items.append(("title", cx0 + pad + round(32 * s), cy0 + round(16 * s),
                                   theme.card_title_ref, tut.kind_name))

                close_sz = max(14, round(20 * s))
                close_x = cx1 - pad - close_sz
                close_y = cy0 + round(17 * s)
                close_col = theme.card_accent_bgr if hover == "close" else theme.card_glyph_bgr
                _blend_stamp(pixels, ui_glyph("close", close_sz, active=(hover == "close"), color_bgr=close_col),
                             close_x, close_y)
                close_rect = (close_x - round(6 * s), close_y - round(6 * s),
                              close_x + close_sz + round(6 * s), close_y + close_sz + round(6 * s))
                prev_rect = next_rect = None
                if total > 1:
                    nav_w, nav_h = round(78 * s), round(22 * s)
                    nx0 = close_x - nav_w - round(12 * s)
                    ny0 = cy0 + round(16 * s)
                    _blend_rect(pixels, nx0, ny0, nx0 + nav_w, ny0 + nav_h,
                                theme.card_pill_bgr, radius=nav_h / 2,
                                border_bgr=theme.card_border_bgr, border_px=max(0.8, 1.0 * s))
                    prev_rect = (nx0, ny0, nx0 + round(24 * s), ny0 + nav_h)
                    next_rect = (nx0 + nav_w - round(24 * s), ny0, nx0 + nav_w, ny0 + nav_h)
                    text_items.append(("small_bold", nx0 + round(6 * s), ny0 + round(2 * s),
                                       theme.card_accent_ref if hover == "prev" else theme.card_muted_ref, "‹"))
                    page_txt = f"{idx + 1}/{total}"
                    text_items.append(("small", nx0 + round(24 * s), ny0 + round(3 * s),
                                       theme.card_title_ref, page_txt))
                    text_items.append(("small_bold", nx0 + nav_w - round(14 * s), ny0 + round(2 * s),
                                       theme.card_accent_ref if hover == "next" else theme.card_muted_ref, "›"))

                yc = cy0 + round(48 * s)
                sp_sz = max(12, round(16 * s))
                _blend_stamp(pixels, ui_glyph("sparkle", sp_sz, color_bgr=theme.card_accent_bgr),
                             cx0 + pad, yc + round(2 * s))
                text_items.append(("sub", cx0 + pad + round(22 * s), yc, theme.card_title_ref, tut.title))
                yc += round(26 * s)

                if has_diff:
                    diff_txt = f"获取难度：{tut.difficulty}"
                    pw = round((18 + len(diff_txt) * 12.5) * s)
                    ph = round(23 * s)
                    _blend_rect(pixels, cx0 + pad, yc, cx0 + pad + pw, yc + ph,
                                theme.card_pill_bgr, radius=ph / 2,
                                border_bgr=theme.card_border_bgr, border_px=max(0.8, 1.0 * s))
                    text_items.append(("small_bold", cx0 + pad + round(10 * s), yc + round(3 * s),
                                       theme.card_accent_ref, diff_txt))
                    yc += round(30 * s)
                img_w = cw - 2 * pad
                if has_img:
                    import cv2
                    src_img = tut.image
                    sh, sw = src_img.shape[:2]
                    scale_f = max(img_w / max(1, sw), img_h / max(1, sh))
                    rw, rh = max(img_w, round(sw * scale_f)), max(img_h, round(sh * scale_f))
                    resized = cv2.resize(src_img, (rw, rh),
                                         interpolation=cv2.INTER_AREA if scale_f < 1 else cv2.INTER_LINEAR)
                    ox, oy = (rw - img_w) // 2, (rh - img_h) // 2
                    crop = resized[oy:oy + img_h, ox:ox + img_w].astype(np.float32)
                    mask = _rounded_mask(img_h, img_w, round(8 * s))[..., None]
                    ix0, iy0 = cx0 + pad, yc
                    target = pixels[iy0:iy0 + img_h, ix0:ix0 + img_w].astype(np.float32)
                    src_pre = np.empty_like(target)
                    src_pre[..., :3] = crop * mask
                    src_pre[..., 3:] = mask * 255.0
                    pixels[iy0:iy0 + img_h, ix0:ix0 + img_w] = np.clip(
                        np.rint(src_pre + target * (1.0 - mask)), 0, 255).astype(np.uint8)
                    yc += img_h + round(12 * s)
                elif tut.loading:
                    _blend_rect(pixels, cx0 + pad, yc, cx0 + pad + img_w, yc + img_h,
                                theme.card_surface_bgr, radius=round(8 * s))
                    text_items.append(("small", cx0 + pad + round(102 * s), yc + round(29 * s),
                                       theme.card_muted_ref, "正在加载点位教程…"))
                    yc += img_h + round(12 * s)

                for line in lines:
                    text_items.append(("body", cx0 + pad, yc, theme.card_body_ref, line))
                    yc += round(22 * s)

                if footer_rows:
                    yc += round(6 * s)
                    if tut.contributor:
                        text_items.append(("small", cx0 + pad, yc, theme.card_muted_ref,
                                           f"贡献者：  {tut.contributor}"))
                        yc += round(19 * s)
                    if tut.updated_at:
                        text_items.append(("small", cx0 + pad, yc, theme.card_muted_ref,
                                           f"更新时间：{tut.updated_at}"))
                self.card_layout = {
                    "card": (cx0, cy0, cx1, cy1),
                    "close": close_rect,
                    "prev": prev_rect,
                    "next": next_rect,
                }

            if text_items:
                alpha_before = pixels[..., 3].copy()
                rgb_before = (self.pixels & 0x00FFFFFF).copy()
                fonts = {
                    "title": keep(font(-max(14, round(17 * s)), 0, 0, 0, 700,
                                       0, 0, 0, 1, 0, 0, 5, 0, FONT_FAMILY), "创建标题字体"),
                    "sub": keep(font(-max(13, round(14 * s)), 0, 0, 0, 700,
                                     0, 0, 0, 1, 0, 0, 5, 0, FONT_FAMILY), "创建副标题字体"),
                    "body": keep(font(-max(12, round(13 * s)), 0, 0, 0, 400,
                                      0, 0, 0, 1, 0, 0, 5, 0, FONT_FAMILY), "创建正文字体"),
                    "small_bold": keep(font(-max(11, round(12 * s)), 0, 0, 0, 600,
                                            0, 0, 0, 1, 0, 0, 5, 0, FONT_FAMILY), "创建标签字体"),
                    "small": keep(font(-max(10, round(11 * s)), 0, 0, 0, 400,
                                       0, 0, 0, 1, 0, 0, 5, 0, FONT_FAMILY), "创建辅助字体"),
                    "badge": keep(font(-max(10, round(11 * s)), 0, 0, 0, 700,
                                       0, 0, 0, 1, 0, 0, 5, 0, FONT_FAMILY), "创建角标字体"),
                }
                bind(gdi, "SetBkMode", ctypes.c_int, w.HDC, ctypes.c_int)(dc, 1)
                set_color = bind(gdi, "SetTextColor", w.DWORD, w.HDC, w.DWORD)
                current_style = None
                for style, tx, ty, color_ref, content_str in text_items:
                    if style != current_style:
                        handle = self.select(dc, fonts[style])
                        if old_font is None:
                            old_font = checked(handle, "选择遮罩字体")
                        current_style = style
                    set_color(dc, color_ref)
                    checked(text_out(dc, round(tx), round(ty), content_str, len(content_str)),
                            "绘制遮罩文字")
                checked(bind(gdi, "GdiFlush", w.BOOL)(), "提交遮罩绘图")
                changed = (self.pixels & 0x00FFFFFF) != rgb_before
                pixels[..., 3] = np.where(changed, 255, alpha_before)

            count = int(np.count_nonzero(pixels[..., 3]))
            if (points or visible_hint or card) and count == 0:
                raise OSError("遮罩绘图没有生成可见像素。")
            return count
        finally:
            if old_font:
                self.select(dc, old_font)
            for item in objects:
                self.delete(item)

    def present(self, hwnd, origin):
        size, source, blend = w.SIZE(self.width, self.height), w.POINT(0, 0), Blend(0, 0, 255, 1)
        checked(self.update(hwnd, None, ctypes.byref(origin), ctypes.byref(size), self.dc,
                            ctypes.byref(source), 0, ctypes.byref(blend), 2), "显示遮罩透明图层")
