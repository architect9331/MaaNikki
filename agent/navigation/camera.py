"""Camera-cone direction from recorded map terrain, independent of route tasks."""
from __future__ import annotations

import cv2
import numpy as np
from scipy.signal import find_peaks
from .frames import minimap_crop
from .matching import align_reference, luma
from .models import NavigationError


def circular_smooth(values, radius):
    return sum(np.roll(values, n)*(radius-abs(n))//radius for n in range(1-radius, radius))


def prominence(values):
    length = values.size
    peaks, info = find_peaks(np.tile(values, 3), height=0, prominence=10)
    heights = sorted((height for p, height in zip(peaks, info["peak_heights"]) if length <= p < 2*length), reverse=True)
    return float((heights[0]-heights[1])/heights[0]) if len(heights) > 1 and heights[0] else 1.0


def camera_heading(reference, map_image, spec, x, y):
    observed = luma(minimap_crop(reference, padding=2))
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
    cone = np.clip((255-aligned.astype(float))/(255-observed.astype(float)+.1)*64, 0, 255).astype(np.uint8)
    cone[~valid] = 64
    cone = cv2.GaussianBlur(cone, (3, 3), 0)
    d = 204
    radial, angular = np.indices((d, d), dtype=np.float32)
    angle = angular*2*np.pi/d
    polar = cv2.remap(cone, d/2+radial/2*np.cos(angle), d/2+radial/2*np.sin(angle), cv2.INTER_LINEAR)
    polar = cv2.resize(polar[d*2//10:d*7//10].astype(np.float32), None, fx=2, fy=2)
    polar_valid = cv2.remap(valid.astype(np.uint8), d/2+radial/2*np.cos(angle),
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
        diagnostics = {"method": "cone_boundaries", "edges": 0}
        return None, 0.0, diagnostics
    # Polar coordinates begin east-clockwise; center is 45 degrees after
    # the rising cone boundary. Expose north-clockwise degrees to movement.
    heading = (int(signal.argmax())/width*360+135) % 360
    confidence = prominence(signal)
    diagnostics = {"method": "cone_boundaries", "heading": heading, "confidence": confidence}
    return float(heading), confidence, diagnostics
