"""A persistent BGRA surface submitted directly to the Windows compositor."""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import numpy as np

from .icons import marker_icon


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
            storage = (ctypes.c_uint32 * (width*height)).from_address(bits.value)
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

    def draw(self, points, hint, circle):
        self.pixels.fill(0)
        dc, gdi = self.dc, self.gdi
        brush = bind(gdi, "CreateSolidBrush", w.HBRUSH, w.DWORD)
        text = bind(gdi, "TextOutW", w.BOOL, w.HDC, ctypes.c_int, ctypes.c_int, w.LPCWSTR, ctypes.c_int)
        font = bind(gdi, "CreateFontW", w.HFONT, *([ctypes.c_int] * 5), *([w.DWORD] * 8), w.LPCWSTR)
        sx, sy = self.width/1280, self.height/720
        objects = []
        old_font = None

        def keep(handle, operation):
            checked(handle, operation)
            objects.append(handle)
            return handle

        try:
            label_font = keep(font(-max(12, round(14*sx)), 0, 0, 0, 500,
                                   0, 0, 0, 1, 0, 0, 4, 0, "Microsoft YaHei"), "创建遮罩字体")
            old_font = checked(self.select(dc, label_font), "选择遮罩字体")
            bind(gdi, "SetBkMode", ctypes.c_int, w.HDC, ctypes.c_int)(dc, 1)
            bind(gdi, "SetTextColor", w.DWORD, w.HDC, w.DWORD)(dc, 0xFFFFFF)
            for x, y, kind, count in points:
                x, y = x*sx, y*sy
                if count > 1 and not circle:
                    label = str(count)
                    checked(text(dc, round(x+13*sx), round(y-9*sy), label, len(label)),
                            "绘制点位数量")
            if hint:
                left = max(8, round(260*sx))
                backing = keep(brush(0x30251E), "创建状态背景")
                rect = w.RECT(left-8, round(26*sy), min(self.width, left+round(760*sx)), round(58*sy))
                fill = bind(self.user, "FillRect", ctypes.c_int, w.HDC, ctypes.POINTER(w.RECT), w.HBRUSH)
                checked(fill(dc, ctypes.byref(rect), backing), "绘制状态背景")
                checked(text(dc, left, round(30*sy), hint, len(hint)), "绘制地图状态")
            checked(bind(gdi, "GdiFlush", w.BOOL)(), "提交遮罩绘图")
            # GDI writes BGR but leaves alpha zero. Give only drawn pixels an
            # opaque alpha; untouched pixels remain fully transparent.
            opaque = (self.pixels & 0x00FFFFFF) != 0
            self.pixels[opaque] |= np.uint32(0xFF000000)
            pixels = self.pixels.view(np.uint8).reshape(self.height, self.width, 4)
            size = max(8, round((22 if circle else 34)*min(sx, sy)))
            for x, y, kind, _ in points:
                icon = marker_icon(kind, size)
                left, top = round(x*sx-size/2), round(y*sy-size/2)
                x0, y0 = max(0, left), max(0, top)
                x1, y1 = min(self.width, left+size), min(self.height, top+size)
                if x0 >= x1 or y0 >= y1:
                    continue
                source = icon[y0-top:y1-top, x0-left:x1-left].astype(np.uint16)
                if circle:
                    cx, cy, radius = circle
                    yy, xx = np.ogrid[y0:y1, x0:x1]
                    inside = ((xx/sx-cx)**2+(yy/sy-cy)**2 <= radius**2)
                    source *= inside[..., None]
                target = pixels[y0:y1, x0:x1]
                target[:] = source + (target.astype(np.uint16)*(255-source[..., 3:])+127)//255
            count = int(np.count_nonzero(pixels[..., 3]))
            if (points or hint) and count == 0:
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
