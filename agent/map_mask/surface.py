"""A persistent BGRA surface submitted directly to the Windows compositor."""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import numpy as np

from .icons import marker_icon, ui_glyph


class BitmapInfo(ctypes.Structure):
    _fields_ = [("size", w.DWORD), ("width", w.LONG), ("height", w.LONG),
                ("planes", w.WORD), ("bits", w.WORD), ("compression", w.DWORD),
                ("image_size", w.DWORD), ("x_resolution", w.LONG),
                ("y_resolution", w.LONG), ("colors", w.DWORD),
                ("important_colors", w.DWORD), ("color_table", w.DWORD)]


class Blend(ctypes.Structure):
    _fields_ = [("operation", w.BYTE), ("flags", w.BYTE),
                ("alpha", w.BYTE), ("format", w.BYTE)]


def bind(library, name, result, *args):
    function = getattr(library, name)
    function.restype, function.argtypes = result, list(args)
    return function


def checked(result, operation):
    if not result:
        raise OSError(f"{operation}失败（Win32={ctypes.get_last_error()}）。")
    return result


def _rounded_mask(h, w, radius):
    yy, xx = np.ogrid[:h, :w]
    r = max(1.0, float(radius))
    dx = np.maximum(np.maximum(r - xx, xx - (w - 1 - r)), 0.0)
    dy = np.maximum(np.maximum(r - yy, yy - (h - 1 - r)), 0.0)
    dist = np.hypot(dx, dy)
    return np.clip(r + 0.5 - dist, 0.0, 1.0).astype(np.float32)


def _blend_rect(pixels, x0, y0, x1, y1, color_bgr, radius=0, opacity=1.0, border_bgr=None, border_px=1.5):
    H, W = pixels.shape[:2]
    cx0, cy0 = max(0, round(x0)), max(0, round(y0))
    cx1, cy1 = min(W, round(x1)), min(H, round(y1))
    if cx0 >= cx1 or cy0 >= cy1:
        return
    w_box, h_box = round(x1 - x0), round(y1 - y0)
    yy, xx = np.ogrid[cy0 - round(y0):cy1 - round(y0), cx0 - round(x0):cx1 - round(x0)]
    r = max(1.0, float(radius))
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
    H, W = pixels.shape[:2]
    sh, sw = stamp.shape[:2]
    x0, y0 = max(0, left), max(0, top)
    x1, y1 = min(W, left + sw), min(H, top + sh)
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

    def draw(self, points, hint, circle, *, active_key=None, card=None):
        self.pixels.fill(0)
        dc, gdi = self.dc, self.gdi
        text_out = bind(gdi, "TextOutW", w.BOOL, w.HDC, ctypes.c_int, ctypes.c_int, w.LPCWSTR, ctypes.c_int)
        font = bind(gdi, "CreateFontW", w.HFONT, *([ctypes.c_int] * 5), *([w.DWORD] * 8), w.LPCWSTR)
        sx, sy = self.width / 1280.0, self.height / 720.0
        s = min(sx, sy)
        pixels = self.pixels.view(np.uint8).reshape(self.height, self.width, 4)
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
                                (54, 42, 68), radius=bh / 2, border_bgr=(184, 219, 240), border_px=max(1.0, 1.3 * s))
                    tx = bx0 + max(3, round((bw - len(label) * 7.2 * s) / 2))
                    ty = by0 + max(1, round(1.5 * s))
                    text_items.append(("badge", tx, ty, 0xEFF7FF, label))

            if hint:
                left = max(8, round(260 * sx))
                _blend_rect(pixels, left - round(12 * sx), round(24 * sy),
                            min(self.width - 8, left + round(460 * sx)), round(58 * sy),
                            (38, 30, 26), radius=round(8 * s), opacity=0.88,
                            border_bgr=(118, 142, 168), border_px=max(1.0, 1.2 * s))
                text_items.append(("body", left, round(31 * sy), 0xEAF3FA, hint))
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
                            (28, 22, 18), radius=round(14 * s), opacity=0.32)
                _blend_rect(pixels, cx0, cy0, cx1, cy1, (225, 236, 244),
                            radius=round(14 * s), border_bgr=(184, 198, 212), border_px=max(1.0, 1.5 * s))
                bar_x = cx1 - round(7 * s)
                _blend_rect(pixels, bar_x, cy0 + round(44 * s), bar_x + max(2, round(3 * s)),
                            cy1 - round(24 * s), (150, 166, 182), radius=round(1.5 * s))

                head_icon = marker_icon(tut.kind, max(16, round(26 * s)), badge=False)
                _blend_stamp(pixels, head_icon, cx0 + pad, cy0 + round(14 * s))
                text_items.append(("title", cx0 + pad + round(32 * s), cy0 + round(16 * s),
                                   0x465870, tut.kind_name))

                close_sz = max(14, round(20 * s))
                close_x = cx1 - pad - close_sz
                close_y = cy0 + round(17 * s)
                _blend_stamp(pixels, ui_glyph("close", close_sz, active=(hover == "close")),
                             close_x, close_y)
                close_rect = (close_x - round(6 * s), close_y - round(6 * s),
                              close_x + close_sz + round(6 * s), close_y + close_sz + round(6 * s))
                prev_rect = next_rect = None
                if total > 1:
                    nav_w, nav_h = round(78 * s), round(22 * s)
                    nx0 = close_x - nav_w - round(12 * s)
                    ny0 = cy0 + round(16 * s)
                    _blend_rect(pixels, nx0, ny0, nx0 + nav_w, ny0 + nav_h,
                                (202, 217, 229), radius=nav_h / 2)
                    prev_rect = (nx0, ny0, nx0 + round(24 * s), ny0 + nav_h)
                    next_rect = (nx0 + nav_w - round(24 * s), ny0, nx0 + nav_w, ny0 + nav_h)
                    text_items.append(("small_bold", nx0 + round(6 * s), ny0 + round(2 * s),
                                       0x364860 if hover == "prev" else 0x5A6E84, "‹"))
                    page_txt = f"{idx + 1}/{total}"
                    text_items.append(("small", nx0 + round(24 * s), ny0 + round(3 * s),
                                       0x4A5C72, page_txt))
                    text_items.append(("small_bold", nx0 + nav_w - round(14 * s), ny0 + round(2 * s),
                                       0x364860 if hover == "next" else 0x5A6E84, "›"))

                yc = cy0 + round(48 * s)
                sp_sz = max(12, round(16 * s))
                _blend_stamp(pixels, ui_glyph("sparkle", sp_sz), cx0 + pad, yc + round(2 * s))
                text_items.append(("sub", cx0 + pad + round(22 * s), yc, 0x465870, tut.title))
                yc += round(26 * s)

                if has_diff:
                    diff_txt = f"获取难度：{tut.difficulty}"
                    pw = round((18 + len(diff_txt) * 12.5) * s)
                    ph = round(23 * s)
                    _blend_rect(pixels, cx0 + pad, yc, cx0 + pad + pw, yc + ph,
                                (202, 217, 229), radius=ph / 2)
                    text_items.append(("small_bold", cx0 + pad + round(10 * s), yc + round(3 * s),
                                       0x4A5C72, diff_txt))
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
                                (212, 224, 233), radius=round(8 * s))
                    text_items.append(("small", cx0 + pad + round(102 * s), yc + round(29 * s),
                                       0x687A8E, "正在加载点位教程…"))
                    yc += img_h + round(12 * s)

                for line in lines:
                    text_items.append(("body", cx0 + pad, yc, 0x3A4B60, line))
                    yc += round(22 * s)

                if footer_rows:
                    yc += round(6 * s)
                    if tut.contributor:
                        text_items.append(("small", cx0 + pad, yc, 0x6A7D92,
                                           f"贡献者：  {tut.contributor}"))
                        yc += round(19 * s)
                    if tut.updated_at:
                        text_items.append(("small", cx0 + pad, yc, 0x6A7D92,
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
                    "title": keep(font(-max(14, round(18 * s)), 0, 0, 0, 700,
                                       0, 0, 0, 1, 0, 0, 5, 0, "Microsoft YaHei"), "创建标题字体"),
                    "sub": keep(font(-max(13, round(15 * s)), 0, 0, 0, 700,
                                     0, 0, 0, 1, 0, 0, 5, 0, "Microsoft YaHei"), "创建副标题字体"),
                    "body": keep(font(-max(12, round(15 * s)), 0, 0, 0, 500,
                                      0, 0, 0, 1, 0, 0, 5, 0, "Microsoft YaHei"), "创建正文字体"),
                    "small_bold": keep(font(-max(11, round(13 * s)), 0, 0, 0, 600,
                                            0, 0, 0, 1, 0, 0, 5, 0, "Microsoft YaHei"), "创建标签字体"),
                    "small": keep(font(-max(11, round(12 * s)), 0, 0, 0, 400,
                                       0, 0, 0, 1, 0, 0, 5, 0, "Microsoft YaHei"), "创建辅助字体"),
                    "badge": keep(font(-max(10, round(11 * s)), 0, 0, 0, 700,
                                       0, 0, 0, 1, 0, 0, 5, 0, "Microsoft YaHei"), "创建角标字体"),
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
            if (points or hint or card) and count == 0:
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
