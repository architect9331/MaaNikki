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
from .geometry import project_segment, segment_distance
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

    profile = next(
        (ev for ev in events if isinstance(ev, dict) and ev.get("type") == "navigation_profile"),
        {},
    )
    radius_scale = float(profile.get("transit_radius_scale", 1.0))
    pass_scale = float(profile.get("transit_pass_radius_scale", radius_scale))

    wp_states = []
    for idx, pt in enumerate(points):
        precise = bool(pt.action) or idx == count - 1
        base_rad = min(pt.radius, 1.0) if pt.action == "bottle" else pt.radius
        eff_rad = base_rad * radius_scale if (not precise and base_rad <= 1.0) else base_rad
        eff_pass = (
            base_rad * max(radius_scale, pass_scale)
            if (not precise and base_rad <= 1.0)
            else base_rad
        )
        wp_states.append({
            "index": idx,
            "target": [round(pt.x, 3), round(pt.y, 3)],
            "configured_radius": round(pt.radius, 3),
            "radius": round(eff_rad, 3),
            "pass_radius": round(eff_pass, 3),
            "precise": precise,
            "action": pt.action or "",
            "jump": bool(pt.jump),
            "reached": False,
            "crossed": False,
            "actual": None,
            "stop_drift": 0.0,
            "drift_dx": 0.0,
            "drift_dy": 0.0,
            "overshoot_retries": 0,
            "turnback_count": 0,
            "turnback_ms": 0,
            "closest_miss_dist": None,
            "moving_turns": 0,
            "stationary_turns": 0,
            "turn_stop_ms": 0,
            "unstuck_count": 0,
            "heading_rechecks": 0,
            "map_recoveries": 0,
            "forward_steps": 0,
            "max_cross_track": 0.0,
            "action_ok": None if not pt.action else False,
            "action_gain": 0,
            "stuck_poses": [],
            "gb_report": None,
        })

    current_idx = 0
    last_pose: tuple[float, float] | None = None
    segment_starts: dict[int, tuple[float, float]] = {}
    total_obtained = 0

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
                    # Fallback pass-miss detection for older event streams without explicit turnback events.
                    if isinstance(before, (list, tuple)) and len(before) == 2:
                        bx, by = float(before[0]), float(before[1])
                        ax, ay = last_pose
                        sdx, sdy = ax - bx, ay - by
                        pt = points[current_idx]
                        straddled = (
                            (pt.x - bx) * sdx + (pt.y - by) * sdy > 0
                            and (pt.x - ax) * sdx + (pt.y - ay) * sdy <= 0
                        )
                        if straddled:
                            closest = segment_distance(pt, Point(bx, by), Point(ax, ay))
                            if closest > state["pass_radius"] and closest <= max(state["pass_radius"] * 2.5, state["radius"] + 2.0):
                                if state["closest_miss_dist"] is None or closest < state["closest_miss_dist"]:
                                    state["closest_miss_dist"] = round(closest, 3)

        elif kind == "camera_turn":
            if 0 <= current_idx < count:
                if ev.get("stationary"):
                    wp_states[current_idx]["stationary_turns"] += 1
                else:
                    wp_states[current_idx]["moving_turns"] += 1

        elif kind == "turn_stop":
            if 0 <= current_idx < count:
                secs = ev.get("stop_seconds", 0.0)
                if isinstance(secs, (int, float)) and secs > 0:
                    wp_states[current_idx]["turn_stop_ms"] += round(float(secs) * 1000)

        elif kind == "turnback":
            if 0 <= current_idx < count:
                state = wp_states[current_idx]
                state["turnback_count"] += 1
                cd = ev.get("closest_dist")
                if isinstance(cd, (int, float)) and ev.get("reason") == "pass_miss":
                    if state["closest_miss_dist"] is None or float(cd) < state["closest_miss_dist"]:
                        state["closest_miss_dist"] = round(float(cd), 3)

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
                if isinstance(ev.get("pass_radius"), (int, float)):
                    state["pass_radius"] = round(float(ev["pass_radius"]), 3)
                if isinstance(ev.get("turnback_seconds"), (int, float)):
                    state["turnback_ms"] = round(float(ev["turnback_seconds"]) * 1000)
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
            amt = ev.get("amount", 0)
            if isinstance(amt, int) and amt > 0:
                total_obtained += amt
                if 0 <= current_idx < count and ev.get("phase") != "transit":
                    wp_states[current_idx]["action_gain"] += amt

        elif kind == "gingerbread_waypoint_report":
            wp_idx = ev.get("waypoint_index", ev.get("wp_index"))
            target_idx = wp_idx if isinstance(wp_idx, int) and 0 <= wp_idx < count else current_idx
            if 0 <= target_idx < count:
                wp_states[target_idx]["gb_report"] = ev
                picked_tot = ev.get("picked_total")
                if isinstance(picked_tot, int) and picked_tot >= 0:
                    wp_states[target_idx]["action_gain"] = picked_tot

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
    turnback_waypoints = 0
    turnback_total = 0
    turnback_total_ms = 0
    stationary_turns_total = 0
    moving_turns_total = 0
    turn_stop_total_ms = 0
    gb_stops = 0
    gb_stationary_picked = 0
    gb_expected_15m = 0
    gb_configured_wait_s = 0.0
    gb_waited_s = 0.0
    gb_effective_pickup_s = 0.0
    gb_ceiling_stops = 0
    gb_zero_yield_stops = 0
    gb_picked_by_name_total: dict[str, int] = {}

    for idx, state in enumerate(wp_states):
        pt = points[idx]
        if not state["reached"] and idx != failed_index:
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

        tb_count = max(state["turnback_count"], state["overshoot_retries"])
        has_stuck_recovery = state["unstuck_count"] > 0 or state["map_recoveries"] > 0

        if not state["reached"]:
            outcome = "failed"
        elif pt.action and pt.action not in ("wait", "teleport") and state["action_ok"] is False:
            outcome = "action_missed"
            missed_actions += 1
        elif has_stuck_recovery:
            outcome = "recovered"
            recovered_waypoints += 1
        elif tb_count > 0:
            outcome = "turnback"
            turnback_waypoints += 1
        else:
            outcome = "clean"

        unstuck_total += state["unstuck_count"]
        turnback_total += tb_count
        turnback_total_ms += state["turnback_ms"]
        stationary_turns_total += state["stationary_turns"]
        moving_turns_total += state["moving_turns"]
        turn_stop_total_ms += state["turn_stop_ms"]

        gb = state["gb_report"]
        gb_fields: dict[str, Any] = {}
        if isinstance(gb, dict):
            gb_stops += 1
            p_tot = int(gb.get("picked_total") or 0)
            e_tot = int(gb.get("expected_0_15m") or 0)
            cfg_w = float(gb.get("configured_wait_s") or 0.0)
            act_w = float(gb.get("waited_s") or 0.0)
            first_s = gb.get("first_pickup_s")
            last_s = gb.get("last_pickup_s")
            inf_r = gb.get("inferred_radius_m")
            exit_r = str(gb.get("exit_reason") or "")
            gb_stationary_picked += p_tot
            gb_expected_15m += e_tot
            gb_configured_wait_s += cfg_w
            gb_waited_s += act_w
            if isinstance(last_s, (int, float)) and last_s > 0:
                gb_effective_pickup_s += float(last_s)
            if exit_r == "ceiling_reached":
                gb_ceiling_stops += 1
            if e_tot > 0 and p_tot == 0:
                gb_zero_yield_stops += 1
            picked_map = gb.get("picked_by_name") if isinstance(gb.get("picked_by_name"), dict) else {}
            expected_map = gb.get("expected_by_name") if isinstance(gb.get("expected_by_name"), dict) else {}
            for k, v in picked_map.items():
                if isinstance(v, int) and v > 0:
                    gb_picked_by_name_total[str(k)] = gb_picked_by_name_total.get(str(k), 0) + v
            gb_fields = {
                "gb_configured_wait_s": round(cfg_w, 2),
                "gb_waited_s": round(act_w, 2),
                "gb_first_pickup_s": round(float(first_s), 2) if isinstance(first_s, (int, float)) else None,
                "gb_last_pickup_s": round(float(last_s), 2) if isinstance(last_s, (int, float)) else None,
                "gb_exit_reason": exit_r,
                "gb_picked_total": p_tot,
                "gb_expected_15m": e_tot,
                "gb_inferred_radius_m": round(float(inf_r), 2) if isinstance(inf_r, (int, float)) else None,
                **({"gb_picked_by_name": picked_map} if picked_map else {}),
                **({"gb_expected_by_name": expected_map} if expected_map else {}),
            }

        waypoints_out.append({
            "index": idx,
            "target": state["target"],
            "configured_radius": state["configured_radius"],
            "radius": state["radius"],
            "pass_radius": state["pass_radius"],
            "precise": state["precise"],
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
            "turnback_count": tb_count,
            "turnback_ms": state["turnback_ms"],
            "closest_miss_dist": state["closest_miss_dist"],
            "moving_turns": state["moving_turns"],
            "stationary_turns": state["stationary_turns"],
            "turn_stop_ms": state["turn_stop_ms"],
            "unstuck_count": state["unstuck_count"],
            "heading_rechecks": state["heading_rechecks"],
            "map_recoveries": state["map_recoveries"],
            "forward_steps": state["forward_steps"],
            "action_ok": state["action_ok"],
            "action_gain": state["action_gain"],
            **({"stuck_poses": state["stuck_poses"]} if state["stuck_poses"] else {}),
            **gb_fields,
        })

    # Link each waypoint with the downstream segment (index + 1) to measure whether
    # a loose arrival/pass at waypoint[i] causes off-course drift on waypoint[i + 1].
    for i in range(len(waypoints_out) - 1):
        curr_wp = waypoints_out[i]
        next_wp = waypoints_out[i + 1]
        if next_wp["index"] == curr_wp["index"] + 1 and next_wp["action"] != "teleport":
            curr_wp["next_outcome"] = next_wp["outcome"]
            curr_wp["next_max_cross_track"] = next_wp["max_cross_track"]
            curr_wp["next_turnbacks"] = next_wp["turnback_count"]

    if stopped_ev is not None:
        route_outcome = "failed"
    elif not success and missed_actions > 0:
        route_outcome = "action_missed"
    elif not success:
        route_outcome = "incomplete"
    elif unstuck_total > 0 or recovered_waypoints > 0 or missed_actions > 0:
        route_outcome = "recovered"
    elif turnback_waypoints > 0:
        route_outcome = "turnback"
    else:
        route_outcome = "clean"

    now = datetime.now().astimezone()
    return {
        "schema": 2,
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
        "turnback_waypoints": turnback_waypoints,
        "turnback_total": turnback_total,
        "turnback_total_ms": turnback_total_ms,
        "stationary_turns_total": stationary_turns_total,
        "moving_turns_total": moving_turns_total,
        "turn_stop_total_ms": turn_stop_total_ms,
        "navigation_profile": {
            key: profile[key]
            for key in (
                "transit_radius_scale",
                "transit_pass_radius_scale",
                "heading_tolerance_far",
                "heading_tolerance_near",
                "heading_position_slack",
            )
            if key in profile
        },
        "error": stopped_ev.get("reason") if stopped_ev else None,
        **(
            {
                "gingerbread": {
                    "stops": gb_stops,
                    "picked_total": gb_stationary_picked + max(0, total_obtained - gb_stationary_picked),
                    "stationary_picked": gb_stationary_picked,
                    "in_stride_picked": max(0, total_obtained - gb_stationary_picked),
                    "expected_15m": gb_expected_15m,
                    "configured_wait_s": round(gb_configured_wait_s, 2),
                    "waited_s": round(gb_waited_s, 2),
                    "effective_pickup_s": round(gb_effective_pickup_s, 2),
                    "ceiling_stops": gb_ceiling_stops,
                    "zero_yield_stops": gb_zero_yield_stops,
                    **({"picked_by_name": gb_picked_by_name_total} if gb_picked_by_name_total else {}),
                }
            }
            if gb_stops > 0
            else {}
        ),
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
