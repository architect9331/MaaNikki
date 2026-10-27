"""Camera-cone direction from recorded map terrain, independent of route tasks."""
from __future__ import annotations

import cv2
import numpy as np
from scipy.signal import find_peaks
from task_control import task_time as time
from .frames import minimap_crop
from .matching import align_reference, luma
from .models import NavigationError


class CameraBackground:
    """Short-lived terrain translated with the player's local displacement."""

    MAX_SHIFT_PIXELS = 24

    def __init__(self):
        self.reset()

    def reset(self):
        self.origin = None
        self.image = None
        self.valid = None
        self.candidate = None
        self.created_at = 0.0

    def aligned(self, x, y, scale, shape):
        if self.origin is None:
            return None
        shift = ((self.origin[0]-x)/scale, (self.origin[1]-y)/scale)
        if (np.hypot(*shift) > self.MAX_SHIFT_PIXELS
                or time.monotonic()-self.created_at > 30 or self.image.shape != shape):
            self.reset()
            return None
        transform = np.float32([[1, 0, shift[0]], [0, 1, shift[1]]])
        size = (shape[1], shape[0])
        image = cv2.warpAffine(self.image, transform, size, flags=cv2.INTER_LINEAR)
        valid = cv2.warpAffine(self.valid.astype(np.float32), transform, size,
                               flags=cv2.INTER_LINEAR) >= .99
        return image, valid

    def remember(self, observed, x, y, heading, confidence, *, strong_edges):
        if self.image is not None:
            return
        if heading is None or confidence < .7 or not strong_edges:
            self.candidate = None
            return
        now = time.monotonic()
        previous = self.candidate
        self.candidate = (x, y, heading, now)
        if (previous is None or np.hypot(x-previous[0], y-previous[1]) > .5
                or abs((heading-previous[2]+180) % 360-180) > 5 or now-previous[3] > 2):
            return
        yy, xx = np.indices(observed.shape, dtype=np.float32)
        xx, yy = xx-observed.shape[1]/2, yy-observed.shape[0]/2
        angles = np.degrees(np.arctan2(xx, -yy))
        radius = np.hypot(xx, yy)
        # Only retain terrain outside the confirmed light cone and the player
        # icon. Subsequent turns can then expose a cone over unrecorded terrain.
        self.valid = ((np.abs((angles-heading+180) % 360-180) > 60)
                      & (radius > 18) & (radius < 76))
        self.image = observed.copy()
        self.origin = (x, y)
        self.created_at = now


def circular_smooth(values, radius):
    return sum(np.roll(values, n)*(radius-abs(n))//radius for n in range(1-radius, radius))


def prominence(values):
    length = values.size
    peaks, info = find_peaks(np.tile(values, 3), height=0, prominence=10)
    heights = sorted((height for p, height in zip(peaks, info["peak_heights"]) if length <= p < 2*length), reverse=True)
    return float((heights[0]-heights[1])/heights[0]) if len(heights) > 1 and heights[0] else 1.0


def camera_heading(reference, map_image, spec, x, y, *, terrain=None):
    observed = luma(minimap_crop(reference, padding=2, spec=spec))
    scale = spec.scale/1.5
    radius = 102*scale
    left, top = max(0, int(x-radius*1.15)), max(0, int(y-radius*1.15))
    background = map_image[top:min(spec.height, int(y+radius*1.15)), left:min(spec.width, int(x+radius*1.15))]
    background = cv2.resize(background, None, fx=1/scale, fy=1/scale, interpolation=cv2.INTER_LINEAR)
    if min(background.shape) < 204:
        diagnostics = {"method": "camera_background_unavailable"}
        return None, 0.0, diagnostics
    try:
        aligned, valid = align_reference(background, observed)
    except NavigationError:
        return None, 0.0, {"method": "camera_background_unavailable"}
    incomplete = not valid.all()
    # Mask before the blur/remap/derivative chain. Masking only the final polar
    # gradient leaves the neutral fill boundary looking like a cone edge.
    kernel = np.ones((7, 7), np.uint8)
    gradient_valid = cv2.erode(valid.astype(np.uint8), kernel)
    live_pixels = 0
    cached = terrain.aligned(x, y, scale, observed.shape) if terrain is not None else None
    if incomplete and cached is not None:
        image, known = cached
        fill = known & ~valid
        aligned[fill] = image[fill]
        valid |= fill
        # Keep the seam between recorded and live brightness out of derivatives.
        gradient_valid |= cv2.erode(fill.astype(np.uint8), kernel)
        live_pixels = int(fill.sum())
    cone = np.clip((255-aligned.astype(float))/(255-observed.astype(float)+.1)*64, 0, 255).astype(np.uint8)
    cone[~valid] = 64
    cone = cv2.GaussianBlur(cone, (3, 3), 0)
    d = 204
    radial, angular = np.indices((d, d), dtype=np.float32)
    angle = angular*2*np.pi/d
    polar = cv2.remap(cone, d/2+radial/2*np.cos(angle), d/2+radial/2*np.sin(angle), cv2.INTER_LINEAR)
    polar = cv2.resize(polar[d*2//10:d*7//10].astype(np.float32), None, fx=2, fy=2)
    polar_valid = cv2.remap(gradient_valid, d/2+radial/2*np.cos(angle),
                            d/2+radial/2*np.sin(angle), cv2.INTER_NEAREST)
    polar_valid = cv2.resize(polar_valid[d*2//10:d*7//10], None, fx=2, fy=2,
                             interpolation=cv2.INTER_NEAREST)
    polar_valid = cv2.erode(polar_valid, np.ones((3, 3), np.uint8))
    gradient = cv2.Scharr(polar, cv2.CV_32F, 1, 0)
    gradient[polar_valid == 0] = 0
    width = d*2
    rising = np.bincount(find_peaks(gradient.ravel(), height=150, wlen=width)[0] % width, minlength=width)
    falling = np.bincount(find_peaks(-gradient.ravel(), height=150, wlen=width)[0] % width, minlength=width)
    left_edge, right_edge = np.maximum(rising-falling, 0), np.maximum(falling-rising, 0)
    profiles = []
    for shift in range(-3, 4):
        profile = left_edge*circular_smooth(np.roll(right_edge, -width//4+shift), 6)
        profile -= left_edge*circular_smooth(np.roll(right_edge, shift), 20)//5
        profiles.append(circular_smooth(profile, 6))
    profiles = np.maximum(profiles, 1)
    signal = profiles.max(axis=0)
    if prominence(signal) <= .3:
        signal = circular_smooth(signal*profiles.mean(axis=0)*profiles.min(axis=0), 4)
    if not rising.any() or not falling.any() or float(signal.max()) <= 1 or np.ptp(signal) <= 0:
        diagnostics = {"method": "cone_boundaries", "edges": 0,
                       "live_background_pixels": live_pixels}
        return None, 0.0, diagnostics
    # Polar coordinates begin east-clockwise; center is 45 degrees after
    # the rising cone boundary. Expose north-clockwise degrees to movement.
    heading = (int(signal.argmax())/width*360+135) % 360
    confidence = prominence(signal)
    boundary = int(signal.argmax())
    left_support = int(left_edge[(boundary+np.arange(-6, 7)) % width].max())
    right_support = int(right_edge[(boundary+width//4+np.arange(-9, 10)) % width].max())
    # A distinct peak can still be made by a few short terrain lines once a
    # missing boundary is excluded. Require radial evidence on both cone sides.
    # With no live terrain, missing map regions can leave short, sharp terrain
    # lines as the only candidate. Prominence alone cannot establish a cone.
    support_floor = polar.shape[0]*(.1 if live_pixels else .2)
    supported = not incomplete or min(left_support, right_support) >= support_floor
    diagnostics = {"method": "cone_boundaries", "heading": heading, "confidence": confidence,
                   "live_background_pixels": live_pixels,
                   "edge_support": [left_support, right_support], "edges_supported": supported}
    if not supported:
        if terrain is not None:
            terrain.candidate = None
        return None, 0.0, diagnostics
    if terrain is not None and incomplete:
        terrain.remember(observed, x, y, heading, confidence,
                         strong_edges=min(left_support, right_support) >= polar.shape[0]*.25)
    return float(heading), confidence, diagnostics
