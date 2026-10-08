"""Shared immutable template preparation; keys include file revision and parameters."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

from performance import count, measure


@dataclass(frozen=True)
class Template:
    image: np.ndarray
    mask: np.ndarray | None


@lru_cache(maxsize=64)
def _decode(path, revision, grayscale):
    del revision  # Part of the key: replacing a file invalidates its cached pixels.
    with measure("template.decode"):
        count("template.decodes")
        image = cv2.imread(path, cv2.IMREAD_GRAYSCALE if grayscale else cv2.IMREAD_UNCHANGED)
        if image is None:
            raise ValueError("任务图像资源缺失。")
        image.setflags(write=False)
        return image


@lru_cache(maxsize=128)
def _prepare(path, revision, scale, gray, color, grayscale):
    with measure("template.prepare"):
        count("template.preparations")
        image = _decode(path, revision, grayscale)
        mask = None
        if not grayscale:
            if image.ndim != 3:
                image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
            if image.shape[2] == 4:
                mask = (image[:, :, 3] > 128).astype(np.uint8)*255
            image = image[:, :, :3]
        if scale != 1:
            image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)
            if mask is not None:
                mask = cv2.resize(mask, (image.shape[1], image.shape[0]), interpolation=cv2.INTER_NEAREST)
        if color:
            image = cv2.inRange(cv2.cvtColor(image, cv2.COLOR_BGR2HSV),
                                np.array(color[0]), np.array(color[1]))
        elif gray:
            image = cv2.threshold(image if grayscale else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY),
                                  gray[0], gray[1], cv2.THRESH_BINARY)[1]
        if color or gray:
            x, y, width, height = cv2.boundingRect(image)
            image = image[y:y+height, x:x+width]
            if mask is not None:
                mask = mask[y:y+height, x:x+width]
        image.setflags(write=False)
        if mask is not None:
            mask.setflags(write=False)
        return Template(image, mask)


def prepared_template(path, *, scale=1, gray=None, color=None, grayscale=False):
    path = Path(path)
    count("template.requests")
    stat = path.stat()
    return _prepare(str(path), (stat.st_mtime_ns, stat.st_size), scale,
                    tuple(gray) if gray else None,
                    tuple(tuple(bound) for bound in color) if color else None, grayscale)


def clear_template_cache():
    _prepare.cache_clear()
    _decode.cache_clear()
