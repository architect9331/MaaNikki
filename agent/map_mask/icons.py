"""Cached antialiased marker artwork in premultiplied BGRA format."""
from __future__ import annotations

from functools import lru_cache
import math

import cv2
import numpy as np


@lru_cache(maxsize=32)
def marker_icon(kind, size):
    # Rasterize the small vector artwork once per display scale. The display
    # loop only blends these stamps; it never redraws vector paths per marker.
    extent = size*4
    unit = extent/64
    canvas = np.zeros((extent, extent, 4), dtype=np.float32)

    def shape(factor=1, offset=(0, 0)):
        if kind == "star":
            vertices = []
            for i in range(16):
                radius = 22 if i % 4 == 0 else 12 if i % 2 == 0 else 7
                angle = i*math.pi/8
                vertices.append((math.sin(angle)*radius, -math.cos(angle)*radius))
        else:
            segments = (
                ((0, -22), (3, -13), (16, -4), (16, 5)),
                ((16, 5), (16, 14), (8, 20), (0, 20)),
                ((0, 20), (-10, 20), (-16, 13), (-16, 5)),
                ((-16, 5), (-16, -5), (-3, -13), (0, -22)),
            )
            vertices = []
            for controls in segments:
                for t in np.linspace(0, 1, 16, endpoint=False):
                    weights = ((1-t)**3, 3*(1-t)**2*t, 3*(1-t)*t*t, t**3)
                    vertices.append(tuple(sum(p[axis]*v for p, v in zip(controls, weights))
                                          for axis in (0, 1)))
        path = np.round([(32*unit+(x*factor+offset[0])*unit,
                          32*unit+(y*factor+offset[1])*unit) for x, y in vertices]).astype(np.int32)
        mask = np.zeros((extent, extent), dtype=np.uint8)
        cv2.fillPoly(mask, [path], 255)
        return mask.astype(np.float32)/255

    def layer(mask, color, opacity=1):
        alpha = (mask*opacity)[..., None]
        # All layers, including the final downsampled edge, stay premultiplied.
        canvas[..., :3] = np.asarray(color)*alpha + canvas[..., :3]*(1-alpha)
        canvas[..., 3:] = alpha*255 + canvas[..., 3:]*(1-alpha)

    outer = shape()
    layer(cv2.GaussianBlur(shape(offset=(0, 2)), (0, 0), 2.2*unit), (28, 16, 32), .65)
    layer(cv2.GaussianBlur(outer, (0, 0), 2.5*unit),
          (228, 132, 255) if kind == "star" else (255, 208, 88), .45)
    layer(outer, (182, 85, 209) if kind == "star" else (196, 119, 37))
    layer(shape(.90), (255, 244, 255))
    yy = np.linspace(0, 1, extent, dtype=np.float32)[:, None, None]
    top = np.array((247, 200, 255) if kind == "star" else (255, 235, 144))
    bottom = np.array((168, 125, 245) if kind == "star" else (234, 157, 52))
    layer(shape(.75), top*(1-yy)+bottom*yy)
    if kind == "star":
        # A warm white center gives the star the game's luminous silhouette.
        layer(shape(.39), (220, 249, 255))
    else:
        highlight = np.zeros((extent, extent), dtype=np.uint8)
        cv2.ellipse(highlight, (round(27*unit), round(33*unit)),
                    (max(1, round(2*unit)), max(1, round(6*unit))), 28, 0, 360, 255, -1)
        layer(highlight.astype(np.float32)/255, (255, 255, 249), .85)
    result = cv2.resize(canvas, (size, size), interpolation=cv2.INTER_AREA)
    return np.clip(np.rint(result), 0, 255).astype(np.uint8)
