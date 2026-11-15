"""Shared minimap localization policy and trusted position history."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from task_control import task_time as time
import cv2
import numpy as np
from vision import read_image
from .camera import CameraBackground, camera_heading
from .heading import player_heading
from .frames import minimap_sample, reference_frame
from .matching import MIN_KNOWN_COVERAGE, coverage, match_map
from .models import MINIMAP_POSITION_SCORE, MapSpec, NavigationError


MIN_HEADING_CONFIDENCE = .3


def _detect_dark_border_shift(frame):
    """Detect an asymmetric black capture border in the 720p game frame."""
    height, width = frame.shape[:2]
    max_bar = 24
    probe = max_bar + 12

    def edge_bar(strip, *, axis, reverse=False):
        luma_strip = strip.mean(axis=2)
        means = luma_strip.mean(axis=axis)
        dark_ratios = (luma_strip <= 16.0).mean(axis=axis)
        if reverse:
            means = means[::-1]
            dark_ratios = dark_ratios[::-1]
        bar = 0
        for mean_val, dark_ratio in zip(means[:max_bar], dark_ratios[:max_bar]):
            if mean_val <= 4.5 and dark_ratio >= .95:
                bar += 1
            else:
                break
        if bar < 2:
            return 0
        interior = float(np.mean(means[bar:bar + 12]))
        if interior < float(np.mean(means[:bar])) + 10.0:
            return 0
        return bar

    left = edge_bar(frame[:, :probe], axis=0)
    right = edge_bar(frame[:, width - probe:], axis=0, reverse=True)
    top = edge_bar(frame[:probe, :], axis=1)
    bottom = edge_bar(frame[height - probe:, :], axis=1, reverse=True)
    dx = left if (left >= 2 and right == 0) else (-right if (right >= 2 and left == 0) else 0)
    dy = top if (top >= 2 and bottom == 0) else (-bottom if (bottom >= 2 and top == 0) else 0)
    return dx, dy


def _detect_minimap_circle(reference, cx, cy, search_pad=130):
    """Find the circular minimap bezel near `(cx, cy)` in 1080p coordinates."""
    x0, y0 = max(0, round(cx - search_pad)), max(0, round(cy - search_pad))
    x1, y1 = min(1920, round(cx + search_pad)), min(1080, round(cy + search_pad))
    crop = reference[y0:y1, x0:x1]
    if crop.size == 0:
        return None
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    circles = cv2.HoughCircles(
        cv2.GaussianBlur(gray, (5, 5), 1.2),
        cv2.HOUGH_GRADIENT, dp=1.0, minDist=20,
        param1=90, param2=30, minRadius=96, maxRadius=112,
    )
    if circles is None:
        return None
    best = min(circles[0], key=lambda c: np.hypot(c[0] + x0 - cx, c[1] + y0 - cy))
    return float(best[0] + x0), float(best[1] + y0), float(best[2])


def _detect_cyan_pivots(reference, cx, cy, search=18):
    """Return centroids of cyan minimap chevron candidates near `(cx, cy)`."""
    x0, y0 = max(0, round(cx - search)), max(0, round(cy - search))
    x1, y1 = min(1920, round(cx + search + 1)), min(1080, round(cy + search + 1))
    crop = reference[y0:y1, x0:x1]
    if crop.size == 0:
        return []
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (75, 45, 80), (110, 255, 255))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    centers = []
    for contour in contours:
        area = cv2.contourArea(contour)
        if not 25 <= area <= 220:
            continue
        moments = cv2.moments(contour)
        if moments["m00"] > 0:
            centers.append((moments["m10"] / moments["m00"] + x0,
                            moments["m01"] / moments["m00"] + y0))
    return centers


@dataclass(frozen=True)
class Pose:
    x: float
    y: float
    score: float
    margin: float
    heading: float | None
    heading_confidence: float
    position_reliable: bool = True

    def dict(self):
        return asdict(self)


class Locator:
    # Keep the existing tooling entry point; the implementation lives in the
    # shared map matcher so legacy callers also receive missing-tile fixes.
    match = staticmethod(match_map)

    def __init__(self, spec: MapSpec):
        self._base_spec = spec
        self.spec = spec
        if not spec.image.is_file():
            raise NavigationError("地图资源缺失。")
        self.map = read_image(spec.image, cv2.IMREAD_GRAYSCALE)
        if self.map is None or self.map.shape != (spec.height, spec.width):
            raise NavigationError("地图图片尺寸不符合清单。")
        coarse = spec.image.with_name(spec.id+"_bigmap.png")
        mask = spec.image.with_name(spec.id+"_mask.png")
        self.coarse = (read_image(coarse, cv2.IMREAD_GRAYSCALE) if coarse.is_file()
                       else cv2.resize(self.map, None, fx=.25, fy=.25))
        self.bigmap_mask = read_image(mask, cv2.IMREAD_GRAYSCALE) if mask.is_file() else None
        self.previous = None
        self.position_time = time.monotonic()-30
        self.heading_diagnostics = {}
        self.last_frame = None
        self.camera_background = CameraBackground()
        self._border_shift = (0, 0)
        self._center_calibrated = False
        self._circle_checked = False

    def reset(self):
        self.spec = self._base_spec
        self.previous = None
        self.position_time = time.monotonic()-30
        self.camera_background.reset()
        self._border_shift = (0, 0)
        self._center_calibrated = False
        self._circle_checked = False

    def sync_minimap_center(self, frame, reference=None):
        """Adapt minimap pivot when capture borders or HUD offsets shift the frame."""
        dx, dy = _detect_dark_border_shift(frame)
        bx, by = self._base_spec.center
        bx_1080, by_1080 = bx * 1.5, by * 1.5
        if dx or dy:
            if self._center_calibrated and (dx, dy) == self._border_shift:
                return self.spec
            ref = reference if reference is not None else reference_frame(frame)
            cand_x, cand_y = bx_1080 + dx * 1.5, by_1080 + dy * 1.5
            circ = _detect_minimap_circle(ref, cand_x, cand_y)
            circ_ok = circ is not None and np.hypot(circ[0] - cand_x, circ[1] - cand_y) <= 5.0
            pivots = _detect_cyan_pivots(ref, cand_x, cand_y, search=18)
            chev_ok = any(np.hypot(px - cand_x, py - cand_y) <= 5.0 for px, py in pivots)
            if circ_ok or chev_ok:
                self.spec = replace(self._base_spec, center=(round(bx + dx, 4), round(by + dy, 4)))
                self._border_shift = (dx, dy)
                self._center_calibrated = True
                self._circle_checked = True
            return self.spec
        if self._border_shift != (0, 0):
            self.spec = self._base_spec
            self._border_shift = (0, 0)
            self._center_calibrated = False
            self._circle_checked = False
        if not self._circle_checked:
            self._circle_checked = True
            ref = reference if reference is not None else reference_frame(frame)
            circ = _detect_minimap_circle(ref, bx_1080, by_1080)
            if circ is not None and np.hypot(circ[0] - bx_1080, circ[1] - by_1080) >= 3.5:
                pivots = _detect_cyan_pivots(ref, circ[0], circ[1], search=12)
                if len(pivots) == 1 and np.hypot(pivots[0][0] - circ[0], pivots[0][1] - circ[1]) <= 4.5:
                    self.spec = replace(
                        self._base_spec,
                        center=(round(pivots[0][0]) / 1.5, round(pivots[0][1]) / 1.5),
                    )
                    self._center_calibrated = True
        return self.spec

    def accept(self, pose):
        if not pose.position_reliable:
            raise NavigationError("未通过校验的位置不能更新导航记录。")
        self.previous = pose
        self.position_time = time.monotonic()

    def camera_heading(self, reference, x, y, *, use_terrain=False):
        heading, confidence, self.heading_diagnostics = camera_heading(
            reference, self.map, self.spec, x, y,
            terrain=self.camera_background if use_terrain else None)
        return heading, confidence

    def locate(self, frame, hint=None, search_radius=75, *, commit=True, reset_continuity=False,
               progress=None):
        self.last_frame = frame
        reference = reference_frame(frame)
        self.sync_minimap_center(frame, reference)
        patch = minimap_sample(frame, self.spec)
        scale = self.spec.scale/1.5
        anchor = ((patch.shape[1]-1)/2, (patch.shape[0]-1)/2)
        yy, xx = np.indices(patch.shape, dtype=np.float32)
        xx, yy = (xx-anchor[0])/scale, (yy-anchor[1])/scale
        radius_squared = xx**2+yy**2
        ring = (radius_squared <= (self.spec.radius*1.5)**2) & (radius_squared > 13**2)
        mask = ring.astype(np.uint8)
        if hint is None and self.previous:
            hint = self.previous.x, self.previous.y
        if hint is None:
            small = cv2.resize(patch, None, fx=.25, fy=.25, interpolation=cv2.INTER_NEAREST)
            px, py, _, _ = match_map(self.coarse, small, kernel=9, peak_mask=self.bigmap_mask)
            hint = px*4, py*4
        pad_x, pad_y = max(patch.shape[1]*1.3/2, search_radius), max(patch.shape[0]*1.3/2, search_radius)
        x, y = max(0, round(hint[0]-pad_x)), max(0, round(hint[1]-pad_y))
        local = self.map[y:min(self.spec.height, round(hint[1]+pad_y)),
                         x:min(self.spec.width, round(hint[0]+pad_x))]
        matches = {}

        def attempt(name, sample, sample_mask, floor, *, edges=False):
            try:
                result = match_map(local, sample, sample_mask, minimum_score=floor,
                                   edges=edges, anchor=anchor)
                mx, my, score, peak = result
                matches[name] = {"position": [mx+x, my+y], "score": score, "local_peak": peak}
                return result
            except (NavigationError, cv2.error) as error:
                matches[name] = {"error": str(error)}
                return None
            finally:
                if progress:
                    progress()

        floor = self.spec.position_score_floor
        match_floor = MINIMAP_POSITION_SCORE if floor is None else floor
        selected = initial = attempt("luma", patch, mask, match_floor)
        method = "luma"
        consensus_accepted = False
        edge_ring = (radius_squared <= 96**2) & (radius_squared > 16**2)

        def evaluate_outside_cone(lx, ly):
            direction, confidence = self.camera_heading(reference, lx+x, ly+y)
            matches["outside_camera_cone"] = {"heading": direction, "confidence": confidence,
                                               "accepted": False}
            if progress:
                progress()
            if direction is None or confidence < .5:
                return None, matches["outside_camera_cone"]
            angles = np.degrees(np.arctan2(xx, -yy))
            outside = edge_ring & (np.abs((angles-direction+180) % 360-180) > 55)
            cone_mask = outside.astype(np.uint8)
            cone = attempt("outside_camera_cone", patch, cone_mask, match_floor)
            diagnostics = matches["outside_camera_cone"]
            diagnostics.update(heading=direction, confidence=confidence, accepted=False)
            return (cone, cone_mask), diagnostics

        if selected is not None and selected[2] >= match_floor:
            # On high-contrast shorelines, the minimap flashlight cone can shift
            # full-ring correlation peaks by 1-2 px on rotation; refine using only
            # terrain outside the cone when doing so cleanly sharpens the match.
            lx, ly = initial[:2]
            cone_result, diagnostics = evaluate_outside_cone(lx, ly)
            if cone_result is not None:
                cone, cone_mask = cone_result
                if cone is not None:
                    cx, cy, score, peak = cone
                    agreement = float(np.hypot(cx-lx, cy-ly))
                    known_fraction = coverage(local, patch.shape, cone_mask, (cx+.5, cy+.5))
                    cone_floor = max(.65, match_floor)
                    accepted = (
                        agreement <= 4.0
                        and known_fraction >= MIN_KNOWN_COVERAGE
                        and score >= max(cone_floor, initial[2]+.02)
                        and peak >= max(.02, initial[3]*.75)
                    )
                    diagnostics.update(
                        method_agreement=agreement,
                        known_fraction=known_fraction,
                        refinement=True,
                        accepted=accepted,
                    )
                    if accepted:
                        selected, method = cone, "outside_camera_cone"
        else:
            edge_mask = edge_ring.astype(np.uint8)
            edge = attempt("known_terrain_edges", patch, edge_mask, match_floor, edges=True)
            if edge is not None and edge[2] >= match_floor:
                selected, method = edge, "known_terrain_edges"
            elif initial is not None:
                # A failed edge candidate has no vote over this fallback.
                lx, ly = initial[:2]
                cone_result, diagnostics = evaluate_outside_cone(lx, ly)
                if cone_result is not None:
                    cone, cone_mask = cone_result
                    cone_floor = max(.65, match_floor)
                    if cone is not None:
                        cx, cy, score, peak = cone
                        agreement = float(np.hypot(cx-lx, cy-ly))
                        edge_agreement = (
                            float(np.hypot(cx-edge[0], cy-edge[1]))
                            if edge is not None else float("inf")
                        )
                        known_fraction = coverage(local, patch.shape, cone_mask, (cx+.5, cy+.5))
                        # Dense checkpoint/building icons can cap normalized luma
                        # scores just below match_floor (~0.50-0.54) even at the
                        # true sub-pixel pose; accept when luma, Sobel terrain
                        # edges, and outside-cone all peak within 1.5 px with sharp
                        # prominence.
                        triple_consensus = (
                            agreement <= 1.5
                            and edge_agreement <= 1.5
                            and score >= max(.48, match_floor-.07)
                            and peak >= .05
                            and initial[2] >= max(.48, match_floor-.07)
                            and initial[3] >= .05
                            and edge is not None
                            and edge[2] >= .45
                            and edge[3] >= .06
                        )
                        accepted = (
                            agreement <= 2
                            and known_fraction >= MIN_KNOWN_COVERAGE
                            and (
                                (score >= cone_floor and peak >= .02)
                                or (
                                    score >= match_floor
                                    and peak >= .04
                                    and edge is not None
                                    and edge[2] >= .45
                                    and edge[3] >= .05
                                    and edge_agreement <= 2
                                )
                                or triple_consensus
                            )
                        )
                        diagnostics.update(
                            method_agreement=agreement,
                            edge_agreement=edge_agreement if np.isfinite(edge_agreement) else None,
                            known_fraction=known_fraction,
                            triple_consensus=triple_consensus,
                            accepted=accepted,
                        )
                        if accepted:
                            selected, method = cone, "outside_camera_cone"
                            consensus_accepted = triple_consensus
        px, py, score, detail = selected or (hint[0]-x, hint[1]-y, 0.0, 0.0)
        px, py = px+x, py+y
        candidate = [px, py]
        elapsed = time.monotonic()-self.position_time
        unreliable = (selected is None or not np.isfinite(score)
                      or (floor is not None and score < floor and not consensus_accepted))
        rejected = bool(self.previous and not reset_continuity and elapsed <= 20
                        and np.hypot(px-self.previous.x, py-self.previous.y) >= 50*elapsed+.5)
        if rejected or unreliable:
            px, py = (self.previous.x, self.previous.y) if self.previous else hint
        heading, confidence = self.camera_heading(
            reference, px, py, use_terrain=not (unreliable or rejected))
        self.heading_diagnostics["player_arrow"] = player_heading(reference, self.spec)
        self.heading_diagnostics.update(position_method="correlation_rejected" if unreliable else
            "speed_rejected" if rejected else "map_correlation", position_reliable=not (unreliable or rejected),
            candidate_position=candidate, position_elapsed=elapsed, score=score, local_peak=detail,
            position_match=method, position_matches=matches, position_score_floor=floor,
            position_sampling="native_centered_antialias", position_center=list(self.spec.center),
            position_patch_shape=list(patch.shape))
        # Every consumer receives the same trust decision, including local
        # insect approaches and teleport checks that do not use Navigator.
        if unreliable or rejected:
            self.heading_diagnostics.update(camera_method=self.heading_diagnostics.get("method"),
                                            method="position_unavailable")
            heading, confidence = None, 0.0
        elif heading is not None and confidence < MIN_HEADING_CONFIDENCE:
            self.heading_diagnostics["method"] = "heading_uncertain"
            heading = None
        if unreliable or rejected or heading is None:
            self._circle_checked = False
        pose = Pose(px, py, score, detail, heading, confidence, not (unreliable or rejected))
        if commit and pose.position_reliable:
            self.accept(pose)
        return pose
