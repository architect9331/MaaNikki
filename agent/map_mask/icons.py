"""Cached antialiased marker and UI artwork in premultiplied BGRA format."""
from __future__ import annotations

from functools import lru_cache
import math

import cv2
import numpy as np


def _poly_mask(extent, unit, vertices, *, scale=1.0, center=(32.0, 32.0)):
    cx, cy = center
    pts = np.round([((cx + x * scale) * unit, (cy + y * scale) * unit)
                    for x, y in vertices]).astype(np.int32)
    mask = np.zeros((extent, extent), dtype=np.uint8)
    cv2.fillPoly(mask, [pts], 255, lineType=cv2.LINE_AA)
    return mask.astype(np.float32) / 255.0


def _circle_mask(extent, unit, radius, *, center=(32.0, 32.0)):
    yy, xx = np.ogrid[:extent, :extent]
    cx, cy = center[0] * unit, center[1] * unit
    dist = np.hypot(xx - cx, yy - cy)
    return np.clip(radius * unit + 0.5 - dist, 0.0, 1.0).astype(np.float32)


def _bezier_vertices(segments, steps=16):
    vertices = []
    for controls in segments:
        for t in np.linspace(0.0, 1.0, steps, endpoint=False):
            w0, w1, w2, w3 = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t * t, t ** 3
            vertices.append((
                controls[0][0] * w0 + controls[1][0] * w1 + controls[2][0] * w2 + controls[3][0] * w3,
                controls[0][1] * w0 + controls[1][1] * w1 + controls[2][1] * w2 + controls[3][1] * w3,
            ))
    return vertices


def _star_vertices(outer_r=21.0, inner_r=10.2, tilt=-0.18, points=5):
    vertices = []
    for i in range(points * 2):
        r = outer_r if i % 2 == 0 else inner_r
        angle = tilt + i * math.pi / points
        vertices.append((math.sin(angle) * r, -math.cos(angle) * r))
    return vertices


def _spark_vertices(rx=5.0, ry=5.0, inner=1.3, tilt=0.0):
    vertices = []
    for i in range(8):
        r = (rx if i % 2 == 0 else inner) if i % 4 == 0 else (ry if i % 2 == 0 else inner)
        angle = tilt + i * math.pi / 4
        vertices.append((math.sin(angle) * r, -math.cos(angle) * r))
    return vertices


def _blend(canvas, mask, color, opacity=1.0):
    alpha = np.clip(mask * opacity, 0.0, 1.0)[..., None]
    canvas[..., :3] = np.asarray(color, dtype=np.float32) * alpha + canvas[..., :3] * (1.0 - alpha)
    canvas[..., 3:] = alpha * 255.0 + canvas[..., 3:] * (1.0 - alpha)


@lru_cache(maxsize=64)
def marker_icon(kind: str, size: int, *, active: bool = False, badge: bool = True):
    # Supersample vector layers at 4x and downsample with INTER_AREA into
    # premultiplied BGRA so runtime frame drawing only blends cached stamps.
    extent = max(32, size * 4)
    unit = extent / 64.0
    canvas = np.zeros((extent, extent, 4), dtype=np.float32)
    yy, xx = np.mgrid[:extent, :extent].astype(np.float32) / extent

    if badge:
        pin_center = (32.0, 29.5)
        tip = _poly_mask(extent, unit, [(-7.5, 19.0), (0.0, 29.8), (7.5, 19.0)], center=(32.0, 32.0))
        outer_disc = np.maximum(_circle_mask(extent, unit, 23.8, center=pin_center), tip)
        shadow = cv2.GaussianBlur(outer_disc, (0, 0), 2.4 * unit)
        _blend(canvas, np.roll(shadow, max(1, round(1.8 * unit)), axis=0), (22, 16, 28), 0.58)
        if active:
            glow = cv2.GaussianBlur(outer_disc, (0, 0), 3.0 * unit)
            _blend(canvas, glow, (152, 128, 248), 0.55)
        outline_col = (96, 74, 158) if active else (48, 54, 74)
        ring_col = (158, 138, 244) if active else (184, 219, 240)
        _blend(canvas, outer_disc, outline_col)
        inner_tip = _poly_mask(extent, unit, [(-6.2, 18.5), (0.0, 28.0), (6.2, 18.5)], center=(32.0, 32.0))
        ring_mask = np.maximum(_circle_mask(extent, unit, 22.4, center=pin_center), inner_tip)
        _blend(canvas, ring_mask, ring_col)
        core_mask = _circle_mask(extent, unit, 19.6, center=pin_center)
        bg_grad = np.array((62, 68, 92), dtype=np.float32) * (1.0 - yy[..., None]) + (
            np.array((36, 40, 58), dtype=np.float32) * yy[..., None])
        _blend(canvas, core_mask, bg_grad)
        rim = np.clip(core_mask - _circle_mask(extent, unit, 17.6, center=(32.0, 28.2)), 0.0, 1.0)
        _blend(canvas, rim * np.clip((yy - 0.35) * 2.0, 0.0, 1.0),
               (128, 112, 196) if active else (88, 98, 132), 0.55)
        center = (32.0, 29.5)
        scale = 0.68
    else:
        center = (32.0, 32.0)
        scale = 0.96
    if kind == "star":
        main_pts = _star_vertices(23.5, 11.2, tilt=-0.16, points=5)
        rays_pts = _spark_vertices(21.0, 21.0, inner=4.2, tilt=0.45)
        sil = np.maximum(
            _poly_mask(extent, unit, main_pts, scale=1.08 * scale, center=center),
            _poly_mask(extent, unit, rays_pts, scale=0.96 * scale, center=center),
        )
        if not badge:
            _blend(canvas, cv2.GaussianBlur(sil, (0, 0), 2.0 * unit), (68, 44, 88), 0.45)
        _blend(canvas, sil, (248, 242, 255))
        body = np.maximum(
            _poly_mask(extent, unit, main_pts, scale=0.92 * scale, center=center),
            _poly_mask(extent, unit, rays_pts, scale=0.80 * scale, center=center),
        )
        grad = (np.array((214, 152, 255), dtype=np.float32) * (1.0 - xx[..., None] * 0.7)
                + np.array((255, 196, 152), dtype=np.float32) * (xx[..., None] * 0.7))
        grad = grad * (1.0 - yy[..., None] * 0.35) + np.array((232, 118, 206), dtype=np.float32) * (yy[..., None] * 0.35)
        _blend(canvas, body, grad)
        inner_ring = _poly_mask(extent, unit, main_pts, scale=0.60 * scale, center=center)
        _blend(canvas, inner_ring, (254, 248, 255), 0.92)
        core = _poly_mask(extent, unit, main_pts, scale=0.46 * scale, center=center)
        core_grad = (np.array((206, 108, 228), dtype=np.float32) * (1.0 - yy[..., None])
                     + np.array((224, 112, 158), dtype=np.float32) * yy[..., None])
        _blend(canvas, core, core_grad)
        gem = _poly_mask(extent, unit, _spark_vertices(8.5, 8.5, 2.2, tilt=-0.16), scale=scale, center=center)
        _blend(canvas, gem, (255, 255, 255), 0.96)
        sp1 = _poly_mask(extent, unit, _spark_vertices(5.2, 5.2, 1.2),
                         scale=scale, center=(center[0] - 11.5 * scale, center[1] - 10.0 * scale))
        sp2 = _poly_mask(extent, unit, _spark_vertices(4.2, 4.2, 1.0),
                         scale=scale, center=(center[0] + 12.5 * scale, center[1] + 8.5 * scale))
        _blend(canvas, np.maximum(sp1, sp2), (255, 252, 255), 0.92)
    elif kind == "chest":
        box_pts = [(-18, -5), (-15, -16), (0, -20), (15, -16), (18, -5), (16, 17), (-16, 17)]
        outer = _poly_mask(extent, unit, box_pts, scale=1.08 * scale, center=center)
        _blend(canvas, outer, (226, 246, 255))
        frame = _poly_mask(extent, unit, box_pts, scale=0.94 * scale, center=center)
        _blend(canvas, frame, (62, 156, 236))
        lid = _poly_mask(extent, unit, [(-15, -5), (-12, -14), (0, -17), (12, -14), (15, -5)],
                         scale=0.90 * scale, center=center)
        base = _poly_mask(extent, unit, [(-15, -2), (15, -2), (13, 14), (-13, 14)],
                          scale=0.90 * scale, center=center)
        _blend(canvas, lid, (86, 128, 218))
        _blend(canvas, base, (56, 86, 168))
        band_h = _poly_mask(extent, unit, [(-16, -6), (16, -6), (16, -1), (-16, -1)],
                            scale=0.94 * scale, center=center)
        band_l = _poly_mask(extent, unit, [(-10, -15), (-6, -15), (-6, 14), (-10, 14)],
                            scale=0.90 * scale, center=center)
        band_r = _poly_mask(extent, unit, [(6, -15), (10, -15), (10, 14), (6, 14)],
                            scale=0.90 * scale, center=center)
        _blend(canvas, np.maximum(band_h, np.maximum(band_l, band_r)), (118, 218, 255))
        clasp = _poly_mask(extent, unit, [(0, -9), (6, -3), (0, 5), (-6, -3)], scale=scale, center=center)
        _blend(canvas, clasp, (240, 252, 255))
        gem = _poly_mask(extent, unit, [(0, -6.5), (3.6, -3), (0, 2.2), (-3.6, -3)], scale=scale, center=center)
        _blend(canvas, gem, (255, 224, 96))
    else:
        drop_segs = (
            ((0, -23), (5, -13), (17, -3), (17, 7)),
            ((17, 7), (17, 16), (9, 22), (0, 22)),
            ((0, 22), (-10, 22), (-17, 15), (-17, 6)),
            ((-17, 6), (-17, -4), (-4, -13), (0, -23)),
        )
        drop_pts = _bezier_vertices(drop_segs, steps=16)
        outer = _poly_mask(extent, unit, drop_pts, scale=1.08 * scale, center=center)
        mini = _circle_mask(extent, unit, 3.6 * scale,
                            center=(center[0] + 13.5 * scale, center[1] - 12.0 * scale))
        _blend(canvas, np.maximum(outer, mini), (255, 246, 250))
        body = _poly_mask(extent, unit, drop_pts, scale=0.93 * scale, center=center)
        grad = (np.array((255, 228, 118), dtype=np.float32) * (1.0 - yy[..., None] * 0.75)
                + np.array((238, 88, 138), dtype=np.float32) * (yy[..., None] * 0.75))
        grad = grad * (1.0 - xx[..., None] * 0.4) + np.array((246, 116, 212), dtype=np.float32) * (xx[..., None] * 0.4)
        _blend(canvas, body, grad)
        swirl_outer = _circle_mask(extent, unit, 11.2 * scale,
                                   center=(center[0] - 0.5 * scale, center[1] + 5.8 * scale))
        swirl_inner = _circle_mask(extent, unit, 8.2 * scale,
                                   center=(center[0] + 1.8 * scale, center[1] + 4.0 * scale))
        _blend(canvas, np.clip(swirl_outer - swirl_inner, 0.0, 1.0) * body, (255, 246, 220), 0.88)
        orb = _circle_mask(extent, unit, 6.2 * scale,
                           center=(center[0] + 0.8 * scale, center[1] + 5.5 * scale))
        _blend(canvas, orb, (242, 112, 168), 0.85)
        star = _poly_mask(extent, unit, _spark_vertices(7.2, 7.2, 1.8),
                          scale=scale, center=(center[0] - 2.0 * scale, center[1] + 3.2 * scale))
        _blend(canvas, star, (255, 255, 255), 0.95)
        _blend(canvas, _circle_mask(extent, unit, 2.3 * scale,
                                    center=(center[0] + 13.5 * scale, center[1] - 12.0 * scale)),
               (255, 224, 152), 0.95)

    result = cv2.resize(canvas, (size, size), interpolation=cv2.INTER_AREA)
    return np.clip(np.rint(result), 0, 255).astype(np.uint8)


@lru_cache(maxsize=32)
def ui_glyph(kind: str, size: int, *, active: bool = False):
    extent = max(24, size * 4)
    unit = extent / 64.0
    canvas = np.zeros((extent, extent, 4), dtype=np.float32)
    if kind == "close":
        pts = _spark_vertices(24.0, 24.0, inner=6.0, tilt=math.pi / 4)
        color = (72, 92, 156) if active else (92, 114, 142)
        _blend(canvas, _poly_mask(extent, unit, pts), color)
    elif kind == "sparkle":
        pts = _spark_vertices(24.0, 24.0, inner=6.5, tilt=0.0)
        _blend(canvas, _poly_mask(extent, unit, pts), (156, 174, 192))
        inner = _spark_vertices(13.0, 13.0, inner=3.5, tilt=math.pi / 4)
        _blend(canvas, _poly_mask(extent, unit, inner), (186, 202, 218))
    result = cv2.resize(canvas, (size, size), interpolation=cv2.INTER_AREA)
    return np.clip(np.rint(result), 0, 255).astype(np.uint8)
