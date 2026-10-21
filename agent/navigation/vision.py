"""Shared minimap localization policy and trusted position history."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from task_control import task_time as time
import cv2
import numpy as np
from .camera import camera_heading
from .frames import minimap_crop, reference_frame
from .matching import MIN_KNOWN_COVERAGE, coverage, luma, match_map
from .models import MINIMAP_POSITION_SCORE, MapSpec, NavigationError


MIN_HEADING_CONFIDENCE = .3


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
        self.spec = spec
        if not spec.image.is_file():
            raise NavigationError("地图资源缺失。")
        self.map = cv2.imread(str(spec.image), cv2.IMREAD_GRAYSCALE)
        if self.map is None or self.map.shape != (spec.height, spec.width):
            raise NavigationError("地图图片尺寸不符合清单。")
        coarse = spec.image.with_name(spec.id+"_bigmap.png")
        mask = spec.image.with_name(spec.id+"_mask.png")
        self.coarse = (cv2.imread(str(coarse), cv2.IMREAD_GRAYSCALE) if coarse.is_file()
                       else cv2.resize(self.map, None, fx=.25, fy=.25))
        self.bigmap_mask = cv2.imread(str(mask), cv2.IMREAD_GRAYSCALE) if mask.is_file() else None
        self.previous = None
        self.position_time = time.monotonic()-30
        self.heading_diagnostics = {}
        self.last_frame = None

    def reset(self):
        self.previous = None
        self.position_time = time.monotonic()-30

    def accept(self, pose):
        if not pose.position_reliable:
            raise NavigationError("未通过校验的位置不能更新导航记录。")
        self.previous = pose
        self.position_time = time.monotonic()

    def camera_heading(self, reference, x, y):
        heading, confidence, self.heading_diagnostics = camera_heading(reference, self.map, self.spec, x, y)
        return heading, confidence

    def locate(self, frame, hint=None, search_radius=75, *, commit=True, reset_continuity=False,
               progress=None):
        self.last_frame = frame
        reference = reference_frame(frame)
        gray = luma(minimap_crop(reference))
        yy, xx = np.indices(gray.shape)
        radius_squared = (xx-100)**2+(yy-100)**2
        ring = (radius_squared <= 100**2) & (radius_squared > 13**2)
        scale = self.spec.scale/1.5
        patch = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
        mask = cv2.resize(ring.astype(np.uint8), (patch.shape[1], patch.shape[0]),
                          interpolation=cv2.INTER_NEAREST)
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
                result = match_map(local, sample, sample_mask, minimum_score=floor, edges=edges)
                mx, my, score, peak = result
                matches[name] = {"position": [mx+x+.25, my+y+.25], "score": score, "local_peak": peak}
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
        if selected is None or selected[2] < match_floor:
            edge_ring = (radius_squared <= 96**2) & (radius_squared > 16**2)
            edge_mask = cv2.resize(edge_ring.astype(np.uint8), (patch.shape[1], patch.shape[0]),
                                   interpolation=cv2.INTER_NEAREST)
            edge_patch = cv2.resize(gray, (patch.shape[1], patch.shape[0]), interpolation=cv2.INTER_AREA)
            edge = attempt("known_terrain_edges", edge_patch, edge_mask, match_floor, edges=True)
            if edge is not None and edge[2] >= match_floor:
                selected, method = edge, "known_terrain_edges"
            elif initial is not None:
                # A failed edge candidate has no vote over this fallback.
                lx, ly = initial[:2]
                direction, confidence = self.camera_heading(reference, lx+x+.25, ly+y+.25)
                matches["outside_camera_cone"] = {"heading": direction, "confidence": confidence,
                                                   "accepted": False}
                if progress:
                    progress()
                if direction is not None and confidence >= .5:
                    angles = np.degrees(np.arctan2(xx-100, 100-yy))
                    outside = edge_ring & (np.abs((angles-direction+180) % 360-180) > 55)
                    cone_mask = cv2.resize(outside.astype(np.uint8), (patch.shape[1], patch.shape[0]),
                                          interpolation=cv2.INTER_NEAREST)
                    cone_floor = max(.65, match_floor)
                    cone = attempt("outside_camera_cone", edge_patch, cone_mask, cone_floor)
                    diagnostics = matches["outside_camera_cone"]
                    diagnostics.update(heading=direction, confidence=confidence, accepted=False)
                    if cone is not None:
                        cx, cy, score, peak = cone
                        agreement = float(np.hypot(cx-lx, cy-ly))
                        known_fraction = coverage(local, patch.shape, cone_mask, (cx, cy))
                        accepted = (score >= cone_floor and peak >= .02 and agreement <= 2
                                    and known_fraction >= MIN_KNOWN_COVERAGE)
                        diagnostics.update(method_agreement=agreement, known_fraction=known_fraction, accepted=accepted)
                        if accepted:
                            selected, method = cone, "outside_camera_cone"
        px, py, score, detail = selected or (hint[0]-x-.25, hint[1]-y-.25, 0.0, 0.0)
        px, py = px+x+.25, py+y+.25
        candidate = [px, py]
        elapsed = time.monotonic()-self.position_time
        unreliable = selected is None or not np.isfinite(score) or (floor is not None and score < floor)
        rejected = bool(self.previous and not reset_continuity and elapsed <= 20
                        and np.hypot(px-self.previous.x, py-self.previous.y) >= 50*elapsed+.5)
        if rejected or unreliable:
            px, py = (self.previous.x, self.previous.y) if self.previous else hint
        heading, confidence = self.camera_heading(reference, px, py)
        self.heading_diagnostics.update(position_method="correlation_rejected" if unreliable else
            "speed_rejected" if rejected else "map_correlation", position_reliable=not (unreliable or rejected),
            candidate_position=candidate, position_elapsed=elapsed, score=score, local_peak=detail,
            position_match=method, position_matches=matches, position_score_floor=floor)
        # Every consumer receives the same trust decision, including local
        # insect approaches and teleport checks that do not use Navigator.
        if unreliable or rejected:
            self.heading_diagnostics.update(camera_method=self.heading_diagnostics.get("method"),
                                            method="position_unavailable")
            heading, confidence = None, 0.0
        elif heading is not None and confidence < MIN_HEADING_CONFIDENCE:
            self.heading_diagnostics["method"] = "heading_uncertain"
            heading = None
        pose = Pose(px, py, score, detail, heading, confidence, not (unreliable or rejected))
        if commit and pose.position_reliable:
            self.accept(pose)
        return pose
