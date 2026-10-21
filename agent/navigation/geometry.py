"""Route geometry shared by movement, recovery and partial-marker selection."""
from __future__ import annotations

import math


def project_segment(point, start, end):
    dx, dy = end.x-start.x, end.y-start.y
    length = dx*dx+dy*dy
    fraction = max(0, min(1, ((point.x-start.x)*dx+(point.y-start.y)*dy)/length)) if length else 0
    return start.x+fraction*dx, start.y+fraction*dy


def segment_distance(point, start, end):
    x, y = project_segment(point, start, end)
    return math.hypot(point.x-x, point.y-y)


def route_distance(point, points):
    # Teleports break the polyline; the gap between two islands is not walkable.
    return min((segment_distance(point, points[max(0, i-1)] if end.action != "teleport" else end, end)
                for i, end in enumerate(points)), default=math.inf)
