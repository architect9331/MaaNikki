"""Independent player-arrow evidence and commanded camera-turn expectations."""
from __future__ import annotations

import math
import cv2
import numpy as np


def difference(target, current):
    return (target-current+180) % 360-180


def player_heading(reference, spec):
    """Read the cyan chevron at the minimap pivot, not nearby map markers.

    This is character orientation. It can corroborate the cone while holding
    forward, but is intentionally not a replacement for a stationary camera.
    """
    cx, cy = (round(value*1.5) for value in spec.center)
    radius = 22
    crop = reference[cy-radius:cy+radius+1, cx-radius:cx+radius+1]
    if crop.shape[:2] != (2*radius+1, 2*radius+1):
        return {"heading": None, "reason": "crop_unavailable"}
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (75, 60, 100), (105, 255, 255))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates = []
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < 16:
            continue
        hull = cv2.convexHull(contour)
        vertices = cv2.approxPolyDP(hull, .04*cv2.arcLength(hull, True), True).reshape(-1, 2)
        if len(vertices) != 3:
            continue
        points = vertices.astype(float)
        center = points.mean(axis=0)
        if np.linalg.norm(center-radius) > radius*.45:
            continue
        angles = []
        for index in range(3):
            a, b = points[(index+1) % 3]-points[index], points[(index+2) % 3]-points[index]
            cosine = np.dot(a, b)/max(1e-6, np.linalg.norm(a)*np.linalg.norm(b))
            angles.append(math.degrees(math.acos(float(np.clip(cosine, -1, 1)))))
        tip = int(np.argmin(angles))
        others = [angles[index] for index in range(3) if index != tip]
        # A chevron has one acute tip and two broader, nearly symmetric wings.
        # An equilateral triangle or cropped cyan marker supplies no direction.
        if not 20 <= angles[tip] <= 65 or min(others)-angles[tip] < 10 or abs(others[0]-others[1]) > 20:
            continue
        fill = area/max(cv2.contourArea(hull), 1)
        if fill < .4:
            continue
        base = (points[(tip+1) % 3]+points[(tip+2) % 3])/2
        vector = points[tip]-base
        candidates.append({"heading": math.degrees(math.atan2(vector[0], -vector[1])) % 360,
                           "tip_angle": angles[tip], "fill": fill,
                           "vertices": vertices.tolist()})
    return candidates[0] if len(candidates) == 1 else {
        "heading": None, "reason": "arrow_shape_ambiguous", "candidates": len(candidates)}


class HeadingContinuity:
    """A turn prediction is a reason to recheck, never a permanent direction lock."""
    def __init__(self):
        self.reset()

    def reset(self):
        self.reference = None
        self.expected = None
        self.command_angle = None
        self.pending = False
        self.feedback_valid = True

    def command(self, pixels, ratio):
        self.pending = True
        self.command_angle = pixels/ratio if ratio is not None else None
        self.expected = ((self.reference+self.command_angle) % 360
                         if self.reference is not None and self.command_angle is not None else None)
        self.feedback_valid = True

    def evidence(self, heading):
        expected = self.expected if self.pending else self.reference
        # Existing calibration accepts gains from .75 to 1.33. Account for
        # that measured uncertainty plus cone angular quantization/easing.
        tolerance = max(8.0, abs(self.command_angle or 0)/3)
        return {"reference": self.reference, "expected": self.expected,
                "command_angle": self.command_angle, "tolerance": tolerance,
                "agrees": expected is None or abs(difference(heading, expected)) <= tolerance}

    def accept(self, heading):
        evidence = self.evidence(heading)
        self.feedback_valid = self.feedback_valid and evidence["agrees"]
        self.reference = heading
        self.expected = self.command_angle = None
        self.pending = False
        return evidence
