"""Compact per-waypoint coordinate telemetry and directional tuning analysis."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
from threading import Lock

from client_config import mxu_config
from .geometry import project_segment
from .models import Point, Route

_LOCK = Lock()
_DIRECTORY = Path(__file__).resolve().parents[2] / "debug/route_telemetry"
OCTANTS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def route_telemetry_enabled(root: Path) -> bool:
    env = os.environ.get("PI_MAANIKKI_ROUTE_TELEMETRY")
    if env is not None:
        return env == "1"
    try:
        config = mxu_config(root) or {}
        value = config.get("settings", {}).get("helpImproveRouteTelemetry")
        return True if value is None else bool(value)
    except (OSError, ValueError, TypeError, AttributeError):
        return True


def blessing_stats_enabled(root: Path, pipeline_enabled: bool = True) -> bool:
    if not pipeline_enabled:
        return False
    env = os.environ.get("PI_MAANIKKI_BLESSING_STATS")
    if env is not None:
        return env == "1"
    try:
        config = mxu_config(root) or {}
        value = config.get("settings", {}).get("helpImproveBlessingStats")
        return True if value is None else bool(value)
    except (OSError, ValueError, TypeError, AttributeError):
        return True


def route_hash(route: Route) -> str:
    canonical = [
        [round(p.x, 3), round(p.y, 3), round(p.radius, 3), bool(p.jump), p.action or ""]
        for p in route.points
    ]
    payload = json.dumps(canonical, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(payload).hexdigest()[:10]


def direction_octant(dx: float, dy: float) -> str:
    if math.hypot(dx, dy) < 0.05:
        return "CENTER"
    # Map coordinates: +x is East, -y is North, +y is South.
    angle = math.degrees(math.atan2(dx, -dy)) % 360
    return OCTANTS[int((angle + 22.5) // 45) % 8]


def segment_metrics(
    x: float,
    y: float,
    start: tuple[float, float] | None,
    target: Point,
) -> tuple[float, float, float]:
    """Return (cross_track, along_track, segment_t) relative to start -> target.

    cross_track > 0 means right of the segment travel direction; < 0 means left.
    along_track > 0 means overshot past target; < 0 means stopped before target.
    """
    if start is None:
        return 0.0, 0.0, 1.0
    sx, sy = start
    vx, vy = target.x - sx, target.y - sy
    length = math.hypot(vx, vy)
    if length < 0.1:
        return 0.0, 0.0, 1.0
    ux, uy = vx / length, vy / length
    rel_x, rel_y = x - target.x, y - target.y
    along_track = rel_x * ux + rel_y * uy
    # Right-hand normal in screen/map coordinates (+x East, +y South): (-uy, ux)
    cross_track = rel_x * (-uy) + rel_y * ux
    proj_x, proj_y = project_segment(Point(x, y), Point(sx, sy), target)
    seg_t = math.hypot(proj_x - sx, proj_y - sy) / length
    return cross_track, along_track, max(0.0, min(1.0, seg_t))


def summarize_route_telemetry(
    route: Route,
    events: list[dict],
    *,
    executor: str = "",
    mode: str = "",
    success: bool = False,
    stage: str = "",
) -> dict:
    points = route.points
    count = len(points)
    r_hash = route_hash(route)

    # Track per-waypoint accumulators.
    wp_states = []
    for idx, pt in enumerate(points):
        wp_states.append({
            "index": idx,
            "target": [round(pt.x, 3), round(pt.y, 3)],
            "radius": round(pt.radius, 3),
            "action": pt.action or "",
            "jump": bool(pt.jump),
            "reached": False,
            "crossed": False,
            "actual": None,
            "stop_drift": 0.0,
            "drift_dx": 0.0,
            "drift_dy": 0.0,
            "overshoot_retries": 0,
            "unstuck_count": 0,
            "heading_rechecks": 0,
            "map_recoveries": 0,
            "forward_steps": 0,
            "max_cross_track": 0.0,
            "action_ok": None if not pt.action else False,
            "action_gain": 0,
            "stuck_poses": [],
        })

    current_idx = 0
    last_pose: tuple[float, float] | None = None
    segment_starts: dict[int, tuple[float, float]] = {}

    for ev in events:
        if not isinstance(ev, dict):
            continue
        kind = ev.get("type")
        ev_idx = ev.get("index")
        if isinstance(ev_idx, int) and 0 <= ev_idx < count:
            current_idx = ev_idx

        if kind == "pose":
            px, py = ev.get("x"), ev.get("y")
            if isinstance(px, (int, float)) and isinstance(py, (int, float)):
                last_pose = (float(px), float(py))
                if current_idx not in segment_starts:
                    segment_starts[current_idx] = last_pose

        elif kind == "forward":
            if 0 <= current_idx < count:
                state = wp_states[current_idx]
                state["forward_steps"] += 1
                before = ev.get("before")
                after = ev.get("after")
                if isinstance(before, (list, tuple)) and len(before) == 2 and current_idx not in segment_starts:
                    segment_starts[current_idx] = (float(before[0]), float(before[1]))
                if isinstance(after, (list, tuple)) and len(after) == 2:
                    last_pose = (float(after[0]), float(after[1]))
                    start = segment_starts.get(
                        current_idx,
                        (points[current_idx - 1].x, points[current_idx - 1].y)
                        if current_idx > 0 and points[current_idx].action != "teleport"
                        else None,
                    )
                    ct, _, _ = segment_metrics(last_pose[0], last_pose[1], start, points[current_idx])
                    if abs(ct) > abs(state["max_cross_track"]):
                        state["max_cross_track"] = ct

        elif kind == "arrival_check":
            if 0 <= current_idx < count:
                state = wp_states[current_idx]
                before_stop = ev.get("before_stop")
                after_stop = ev.get("after_stop")
                drift = ev.get("drift")
                if isinstance(drift, (int, float)):
                    state["stop_drift"] = float(drift)
                if (isinstance(before_stop, (list, tuple)) and len(before_stop) == 2
                        and isinstance(after_stop, (list, tuple)) and len(after_stop) == 2):
                    state["drift_dx"] = float(after_stop[0]) - float(before_stop[0])
                    state["drift_dy"] = float(after_stop[1]) - float(before_stop[1])
                    last_pose = (float(after_stop[0]), float(after_stop[1]))
                settled = ev.get("settled", True)
                rem = ev.get("remaining", 0.0)
                rad = ev.get("radius", points[current_idx].radius)
                if not settled or (isinstance(rem, (int, float)) and isinstance(rad, (int, float)) and rem > rad):
                    state["overshoot_retries"] += 1

        elif kind == "waypoint":
            if 0 <= current_idx < count:
                state = wp_states[current_idx]
                state["reached"] = True
                state["crossed"] = bool(ev.get("crossed", False))
                if isinstance(ev.get("radius"), (int, float)):
                    state["radius"] = round(float(ev["radius"]), 3)
                pose_dict = ev.get("pose")
                if isinstance(pose_dict, dict) and isinstance(pose_dict.get("x"), (int, float)):
                    ax, ay = float(pose_dict["x"]), float(pose_dict["y"])
                    state["actual"] = [round(ax, 3), round(ay, 3)]
                    last_pose = (ax, ay)
                    if current_idx + 1 < count and points[current_idx + 1].action != "teleport":
                        segment_starts[current_idx + 1] = (ax, ay)

        elif kind == "unstuck":
            if 0 <= current_idx < count:
                state = wp_states[current_idx]
                state["unstuck_count"] += 1
                if last_pose is not None and len(state["stuck_poses"]) < 4:
                    state["stuck_poses"].append([round(last_pose[0], 3), round(last_pose[1], 3)])

        elif kind in ("heading_recheck", "heading_recheck_started"):
            if 0 <= current_idx < count:
                wp_states[current_idx]["heading_rechecks"] += 1

        elif kind == "map_position_anchor":
            if 0 <= current_idx < count:
                wp_states[current_idx]["map_recoveries"] += 1

        elif kind == "obtained":
            if 0 <= current_idx < count:
                amt = ev.get("amount", 0)
                if isinstance(amt, int) and amt > 0:
                    wp_states[current_idx]["action_gain"] += amt

        elif kind == "bottle_result":
            if 0 <= current_idx < count and ev.get("success"):
                wp_states[current_idx]["action_gain"] += 1

        elif kind == "action_result":
            if 0 <= current_idx < count:
                wp_states[current_idx]["action_ok"] = bool(ev.get("success", False))
            if current_idx + 1 < count:
                current_idx += 1

    stopped_ev = next((ev for ev in reversed(events) if isinstance(ev, dict) and ev.get("type") == "stopped"), None)
    failed_index: int | None = None
    if stopped_ev is not None and 0 <= current_idx < count and not wp_states[current_idx]["reached"]:
        failed_index = current_idx
        state = wp_states[current_idx]
        if last_pose is not None:
            state["actual"] = [round(last_pose[0], 3), round(last_pose[1], 3)]
            if len(state["stuck_poses"]) < 4:
                state["stuck_poses"].append([round(last_pose[0], 3), round(last_pose[1], 3)])

    waypoints_out = []
    unstuck_total = 0
    missed_actions = 0
    recovered_waypoints = 0

    for idx, state in enumerate(wp_states):
        pt = points[idx]
        if not state["reached"] and idx != failed_index:
            # Waypoints after early quota completion or after failure were not visited.
            continue

        actual = state["actual"]
        if actual is None and last_pose is not None:
            actual = [round(last_pose[0], 3), round(last_pose[1], 3)]
        if actual is not None:
            ax, ay = actual[0], actual[1]
            dx = ax - pt.x
            dy = ay - pt.y
            dist = math.hypot(dx, dy)
            direction = direction_octant(dx, dy)
            start = segment_starts.get(
                idx,
                (points[idx - 1].x, points[idx - 1].y)
                if idx > 0 and pt.action != "teleport"
                else None,
            )
            cross_track, along_track, seg_t = segment_metrics(ax, ay, start, pt)
        else:
            dx = dy = dist = cross_track = along_track = 0.0
            seg_t = 0.0
            direction = "UNKNOWN"

        has_friction = (
            state["unstuck_count"] > 0
            or state["map_recoveries"] > 0
            or state["overshoot_retries"] > 0
        )
        if not state["reached"]:
            outcome = "failed"
        elif pt.action and pt.action not in ("wait", "teleport") and state["action_ok"] is False:
            outcome = "action_missed"
            missed_actions += 1
        elif has_friction:
            outcome = "recovered"
            recovered_waypoints += 1
        else:
            outcome = "clean"

        unstuck_total += state["unstuck_count"]
        waypoints_out.append({
            "index": idx,
            "target": state["target"],
            "radius": state["radius"],
            "action": state["action"],
            "jump": state["jump"],
            "reached": state["reached"],
            "crossed": state["crossed"],
            "outcome": outcome,
            "actual": actual,
            "dx": round(dx, 3),
            "dy": round(dy, 3),
            "dist": round(dist, 3),
            "direction": direction,
            "cross_track": round(cross_track, 3),
            "along_track": round(along_track, 3),
            "max_cross_track": round(state["max_cross_track"], 3),
            "segment_t": round(seg_t, 3),
            "stop_drift": round(state["stop_drift"], 3),
            "drift_dx": round(state["drift_dx"], 3),
            "drift_dy": round(state["drift_dy"], 3),
            "overshoot_retries": state["overshoot_retries"],
            "unstuck_count": state["unstuck_count"],
            "heading_rechecks": state["heading_rechecks"],
            "map_recoveries": state["map_recoveries"],
            "forward_steps": state["forward_steps"],
            "action_ok": state["action_ok"],
            "action_gain": state["action_gain"],
            **({"stuck_poses": state["stuck_poses"]} if state["stuck_poses"] else {}),
        })

    if stopped_ev is not None:
        route_outcome = "failed"
    elif not success and missed_actions > 0:
        route_outcome = "action_missed"
    elif not success:
        route_outcome = "incomplete"
    elif unstuck_total > 0 or recovered_waypoints > 0 or missed_actions > 0:
        route_outcome = "recovered"
    else:
        route_outcome = "clean"

    now = datetime.now().astimezone()
    return {
        "schema": 1,
        "timestamp": now.isoformat(timespec="milliseconds"),
        "route": route.id,
        "route_hash": r_hash,
        "map": route.map_id,
        "executor": executor or mode or "navigation",
        "mode": mode or "daily",
        "stage": stage or "route_execution",
        "success": bool(success),
        "outcome": route_outcome,
        "point_count": count,
        "visited_count": len(waypoints_out),
        "failed_index": failed_index,
        "unstuck_total": unstuck_total,
        "missed_actions": missed_actions,
        "recovered_waypoints": recovered_waypoints,
        "error": stopped_ev.get("reason") if stopped_ev else None,
        "waypoints": waypoints_out,
    }


def record_route_telemetry(
    root: Path,
    route: Route,
    events: list[dict],
    *,
    executor: str = "",
    mode: str = "",
    success: bool = False,
    stage: str = "",
) -> dict:
    summary = summarize_route_telemetry(
        route,
        events,
        executor=executor,
        mode=mode,
        success=success,
        stage=stage,
    )
    if not route_telemetry_enabled(root):
        return summary
    now = datetime.now().astimezone()
    try:
        with _LOCK:
            _DIRECTORY.mkdir(parents=True, exist_ok=True)
            path = _DIRECTORY / f"{now:%Y%m%d}-{os.getpid()}.jsonl"
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(summary, ensure_ascii=False, allow_nan=False) + "\n")
    except OSError:
        pass
    return summary
