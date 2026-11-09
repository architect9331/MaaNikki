"""Aggregate route/waypoint telemetry and compute coordinate & tolerance adjustments.

Reads route telemetry JSON/JSONL from `debug/route_telemetry/`, `logs/navigation/`,
or exported Sentry log files, summarizes directional stability and turn-back vs.
off-course drift per waypoint, and computes bounded coordinate and radius adjustments.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUTES_DIR = ROOT / "resource/routes/daily"
MAX_STEP_UNITS = 0.6


def _iter_sentry_result_records(results: list[dict]):
    import re

    chunks_by_group: dict[tuple[str, str], list[str]] = defaultdict(list)
    waypoint_by_group: dict[tuple[str, str], dict[int, dict]] = defaultdict(dict)
    route_by_group: dict[tuple[str, str], str] = {}
    meta_by_group: dict[tuple[str, str], dict] = defaultdict(dict)

    for row in results:
        if not isinstance(row, dict):
            continue
        vals = row.get("values") if isinstance(row.get("values"), dict) else row
        msg = vals.get("message")
        if not isinstance(msg, str) or not msg.strip():
            continue
        trace = str(vals.get("trace") or "")
        ts = str(vals.get("timestamp") or "")[:19]
        group_key = (trace, ts)

        rm = re.search(r'"route"\s*:\s*"([^"]+)"', msg)
        if rm:
            route_by_group[group_key] = rm.group(1)
        tm = re.search(r'"timestamp"\s*:\s*"([^"]+)"', msg)
        if tm:
            meta_by_group[group_key]["started_at"] = tm.group(1)
        om = re.search(r'"outcome"\s*:\s*"([^"]+)"', msg)
        if om and '"waypoints":[' in msg:
            meta_by_group[group_key]["outcome"] = om.group(1)

        try:
            parsed = json.loads(msg)
            if isinstance(parsed, dict):
                if isinstance(parsed.get("waypoints"), list) and parsed.get("route"):
                    yield parsed
                    continue
                if isinstance(parsed.get("index"), int) and isinstance(parsed.get("target"), list):
                    waypoint_by_group[group_key][parsed["index"]] = parsed
                    if isinstance(parsed.get("route"), str):
                        route_by_group[group_key] = parsed["route"]
                    continue
        except (ValueError, TypeError):
            pass
        chunks_by_group[group_key].append(msg)

    def _chunk_sort_key(c: str) -> tuple[int, int]:
        is_not_start = 0 if (c.startswith('{"') and '"waypoints":[' in c) else 1
        m = re.search(r'"index":(\d+)', c)
        return (is_not_start, int(m.group(1)) if m else 9999)

    for group_key, chunks in chunks_by_group.items():
        starts = [c for c in chunks if c.startswith('{"') and '"waypoints":[' in c]
        others = [c for c in chunks if not (c.startswith('{"') and '"waypoints":[' in c)]
        joined_ok = False
        if len(starts) == 1 and others:
            joined = "".join(sorted(chunks, key=_chunk_sort_key))
            try:
                parsed = json.loads(joined)
                if isinstance(parsed, dict) and parsed.get("waypoints") and parsed.get("route"):
                    yield parsed
                    joined_ok = True
                    waypoint_by_group.pop(group_key, None)
                    continue
            except (ValueError, TypeError):
                pass
            if len(others) <= 5:
                import itertools
                for perm in itertools.permutations(others):
                    try:
                        parsed = json.loads(starts[0] + "".join(perm))
                        if isinstance(parsed, dict) and parsed.get("waypoints") and parsed.get("route"):
                            yield parsed
                            joined_ok = True
                            waypoint_by_group.pop(group_key, None)
                            break
                    except (ValueError, TypeError):
                        continue
        if not joined_ok:
            for c in chunks:
                for match in re.finditer(r'\{[^{}]*"index"\s*:\s*\d+[^{}]*"target"\s*:\s*\[[^\]]+\][^{}]*\}', c):
                    try:
                        w = json.loads(match.group(0))
                        if isinstance(w, dict) and isinstance(w.get("index"), int):
                            waypoint_by_group[group_key].setdefault(w["index"], w)
                    except (ValueError, TypeError):
                        continue

    for group_key, wps_dict in waypoint_by_group.items():
        route_id = route_by_group.get(group_key)
        if route_id and wps_dict:
            for idx, w in wps_dict.items():
                if idx + 1 in wps_dict:
                    nw = wps_dict[idx + 1]
                    if w.get("next_outcome") is None:
                        w["next_outcome"] = nw.get("outcome")
                    if w.get("next_turnbacks") is None:
                        w["next_turnbacks"] = int(nw.get("turnback_count") or nw.get("overshoot_retries") or 0)
                    if w.get("next_max_cross_track") is None:
                        w["next_max_cross_track"] = float(nw.get("max_cross_track") or 0.0)
            meta = meta_by_group.get(group_key, {})
            yield {
                "route": route_id,
                "started_at": meta.get("started_at") or f"{group_key[0]}:{group_key[1]}",
                "outcome": meta.get("outcome") or "",
                "waypoints": [wps_dict[i] for i in sorted(wps_dict)],
            }



def iter_telemetry_records(paths: list[Path]):
    for base in paths:
        if not base.exists():
            continue
        files = [base] if base.is_file() else sorted(base.rglob("*"))
        for path in files:
            if not path.is_file():
                continue
            suffix = path.suffix.lower()
            if suffix == ".jsonl":
                try:
                    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        obj = json.loads(line)
                        if isinstance(obj, dict):
                            if obj.get("waypoints") and obj.get("route"):
                                yield obj
                            elif isinstance(obj.get("body"), str):
                                body = json.loads(obj["body"])
                                if isinstance(body, dict) and body.get("waypoints"):
                                    yield body
                except (OSError, ValueError, TypeError):
                    continue
            elif suffix in (".json", ".txt"):
                try:
                    obj = json.loads(path.read_text(encoding="utf-8", errors="replace"))
                except (OSError, ValueError, TypeError):
                    continue
                if isinstance(obj, dict):
                    if obj.get("waypoints") and obj.get("route"):
                        yield obj
                    elif isinstance(obj.get("telemetry"), dict) and obj["telemetry"].get("waypoints"):
                        yield obj["telemetry"]
                    elif isinstance(obj.get("results"), list):
                        yield from _iter_sentry_result_records(obj["results"])
                elif isinstance(obj, list):
                    for item in obj:
                        if isinstance(item, dict) and item.get("waypoints") and item.get("route"):
                            yield item


def compute_waypoint_adjustment(samples: list[dict], target: list[float], configured_radius: float) -> dict:
    total = len(samples)
    clean = [s for s in samples if s.get("outcome") == "clean"]
    turnbacks = [
        s for s in samples
        if s.get("outcome") == "turnback" or (s.get("turnback_count") or 0) > 0 or (s.get("overshoot_retries") or 0) > 0
    ]
    missed = [s for s in samples if s.get("outcome") == "action_missed"]
    recovered = [s for s in samples if s.get("outcome") == "recovered"]
    failed = [s for s in samples if s.get("outcome") == "failed"]
    problem = missed + recovered + failed

    octant_stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "clean": 0, "turnback": 0, "problem": 0}
    )
    for s in samples:
        direction = s.get("direction") or "UNKNOWN"
        octant_stats[direction]["total"] += 1
        outcome = s.get("outcome")
        if outcome == "clean":
            octant_stats[direction]["clean"] += 1
        elif outcome == "turnback":
            octant_stats[direction]["turnback"] += 1
        else:
            octant_stats[direction]["problem"] += 1

    def mean_pair(group: list[dict], kx: str, ky: str) -> tuple[float, float]:
        valid = [
            (float(s[kx]), float(s[ky]))
            for s in group
            if isinstance(s.get(kx), (int, float)) and isinstance(s.get(ky), (int, float))
        ]
        if not valid:
            return 0.0, 0.0
        return (
            sum(x for x, _ in valid) / len(valid),
            sum(y for _, y in valid) / len(valid),
        )

    def has_local_arrival(s: dict) -> bool:
        actual = s.get("actual")
        if not isinstance(actual, list) or len(actual) != 2:
            return False
        if bool(s.get("reached")):
            return True
        dist = s.get("dist")
        return isinstance(dist, (int, float)) and float(dist) <= max(configured_radius * 2.5, 2.5)

    local_clean = [s for s in clean if has_local_arrival(s)]
    local_turnbacks = [s for s in turnbacks if has_local_arrival(s)]
    local_problem = [s for s in problem if has_local_arrival(s)]

    clean_dx, clean_dy = mean_pair(local_clean, "dx", "dy")
    prob_dx, prob_dy = mean_pair(local_problem, "dx", "dy")
    drift_dx, drift_dy = mean_pair(samples, "drift_dx", "drift_dy")

    nudge_x, nudge_y = 0.0, 0.0
    reason = "stable"
    if local_problem and local_clean:
        vec_x = clean_dx - prob_dx
        vec_y = clean_dy - prob_dy
        mag = math.hypot(vec_x, vec_y)
        problem_rate = len(local_problem) / total
        if mag >= 0.08 and problem_rate >= 0.15:
            scale = min(MAX_STEP_UNITS, mag * 0.5 * min(1.0, problem_rate * 2))
            nudge_x = vec_x / mag * scale
            nudge_y = vec_y / mag * scale
            reason = "shift_away_from_failure_direction"
    elif local_problem and not local_clean:
        mag = math.hypot(prob_dx, prob_dy)
        if mag >= 0.1:
            scale = min(MAX_STEP_UNITS, mag * 0.35)
            nudge_x = -prob_dx / mag * scale
            nudge_y = -prob_dy / mag * scale
            reason = "repel_all_problem_offset"

    # Analyze tolerance (radius) vs. turn-backs and downstream off-course drift.
    turnback_rate = len(turnbacks) / total if total else 0.0
    mean_turnback_ms = (
        round(sum(int(s.get("turnback_ms") or 0) for s in turnbacks) / len(turnbacks))
        if turnbacks else 0
    )
    miss_dists = sorted(
        float(s["closest_miss_dist"])
        for s in samples
        if isinstance(s.get("closest_miss_dist"), (int, float))
    )
    precise = any(bool(s.get("precise")) for s in samples)
    eff_radii = [float(s["radius"]) for s in samples if isinstance(s.get("radius"), (int, float))]
    effective_radius = sum(eff_radii) / len(eff_radii) if eff_radii else configured_radius

    downstream_samples = [s for s in samples if s.get("next_outcome") is not None]
    downstream_problems = [
        s for s in downstream_samples
        if s.get("next_outcome") in ("recovered", "failed", "action_missed")
        or (s.get("next_turnbacks") or 0) > 0
    ]
    downstream_problem_rate = (
        len(downstream_problems) / len(downstream_samples) if downstream_samples else 0.0
    )

    suggested_radius = configured_radius
    radius_reason = "keep_current_radius"

    if turnback_rate >= 0.15 and not missed:
        if downstream_problem_rate <= 0.10:
            # Excessive turn-backs without downstream off-course penalties.
            if miss_dists:
                p85 = miss_dists[min(len(miss_dists) - 1, int(len(miss_dists) * 0.85))]
                ratio = (p85 + 0.08) / max(effective_radius, 0.1)
                candidate = configured_radius * min(1.35, max(1.1, ratio))
            else:
                candidate = configured_radius * 1.2
            max_radius_cap = max(3.0, configured_radius + 0.2)
            suggested_radius = round(
                min(max(configured_radius + 0.35, candidate), max_radius_cap, configured_radius * 1.35), 2
            )
            if suggested_radius > configured_radius + 0.03:
                radius_reason = "widen_to_reduce_turnbacks"
        # If braking overshoot or consistent landing cluster on a precise/high-turnback point caused the turn-back,
        # align the target with the stopping cluster or precompensate braking drift.
        if precise or downstream_problem_rate <= 0.10:
            if precise and reason == "stable" and math.hypot(drift_dx, drift_dy) >= 0.12:
                nudge_x = -drift_dx * 0.5
                nudge_y = -drift_dy * 0.5
                reason = "precompensate_braking_overshoot"
            elif reason == "stable" and turnback_rate >= 0.30 and (local_clean or local_turnbacks):
                arr_dx, arr_dy = mean_pair(local_clean + local_turnbacks, "dx", "dy")
                if math.hypot(arr_dx, arr_dy) >= 0.25:
                    nudge_x = arr_dx * 0.5
                    nudge_y = arr_dy * 0.5
                    reason = "align_with_arrival_cluster"
    elif not precise and turnback_rate < 0.08 and len(downstream_samples) >= 4:
        # Check if passing far from this transit waypoint causes the next segment to drift off-course.
        edge_passes = [
            s for s in downstream_samples
            if isinstance(s.get("dist"), (int, float)) and float(s["dist"]) > effective_radius * 0.65
        ]
        inner_passes = [
            s for s in downstream_samples
            if isinstance(s.get("dist"), (int, float)) and float(s["dist"]) <= effective_radius * 0.65
        ]
        if edge_passes and inner_passes:
            edge_ct = sum(abs(float(s.get("next_max_cross_track") or 0.0)) for s in edge_passes) / len(edge_passes)
            inner_ct = sum(abs(float(s.get("next_max_cross_track") or 0.0)) for s in inner_passes) / len(inner_passes)
            edge_bad = sum(1 for s in edge_passes if s.get("next_outcome") not in ("clean", None)) / len(edge_passes)
            inner_bad = sum(1 for s in inner_passes if s.get("next_outcome") not in ("clean", None)) / len(inner_passes)
            if (edge_ct - inner_ct >= 0.35 or edge_bad - inner_bad >= 0.25) and configured_radius > 0.6:
                suggested_radius = round(max(0.5, configured_radius * 0.8), 2)
                radius_reason = "tighten_to_prevent_next_segment_drift"

    max_allowed = min(MAX_STEP_UNITS, max(0.15, configured_radius * 0.4))
    nudge_mag = math.hypot(nudge_x, nudge_y)
    if nudge_mag > max_allowed:
        nudge_x = nudge_x / nudge_mag * max_allowed
        nudge_y = nudge_y / nudge_mag * max_allowed
        nudge_mag = max_allowed

    return {
        "total": total,
        "clean": len(clean),
        "turnback": len(turnbacks),
        "recovered": len(recovered),
        "action_missed": len(missed),
        "failed": len(failed),
        "turnback_rate": round(turnback_rate, 4),
        "mean_turnback_ms": mean_turnback_ms,
        "closest_miss_dist_p85": (
            round(miss_dists[min(len(miss_dists) - 1, int(len(miss_dists) * 0.85))], 3)
            if miss_dists else None
        ),
        "downstream_problem_rate": round(downstream_problem_rate, 4),
        "problem_rate": round(len(problem) / total, 4) if total else 0.0,
        "clean_mean_offset": [round(clean_dx, 3), round(clean_dy, 3)],
        "problem_mean_offset": [round(prob_dx, 3), round(prob_dy, 3)],
        "mean_stop_drift": [round(drift_dx, 3), round(drift_dy, 3)],
        "octants": dict(octant_stats),
        "nudge": [round(nudge_x, 3), round(nudge_y, 3)],
        "nudge_distance": round(nudge_mag, 3),
        "suggested_target": [round(target[0] + nudge_x, 3), round(target[1] + nudge_y, 3)],
        "reason": reason,
        "configured_radius": round(configured_radius, 3),
        "effective_radius": round(effective_radius, 3),
        "suggested_radius": suggested_radius,
        "radius_reason": radius_reason,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze route telemetry and suggest coordinate & tolerance tuning."
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        default=[str(ROOT / "debug/route_telemetry"), str(ROOT / "logs/navigation")],
        help="Telemetry files or directories to analyze.",
    )
    parser.add_argument("--route", help="Filter by route id.")
    parser.add_argument("--min-samples", type=int, default=5, help="Minimum samples per waypoint to recommend a shift.")
    parser.add_argument("--apply", action="store_true", help="Write recommended coordinate & radius shifts to resource/routes/daily/*.json.")
    parser.add_argument("--json", action="store_true", help="Print full JSON report.")
    args = parser.parse_args()

    grouped: dict[str, dict[int, list[dict]]] = defaultdict(lambda: defaultdict(list))
    by_sig: dict[tuple, dict] = {}

    for record in iter_telemetry_records([Path(p) for p in args.inputs]):
        route_id = record.get("route")
        if not isinstance(route_id, str) or (args.route and route_id != args.route):
            continue
        wps = record.get("waypoints", [])
        if not isinstance(wps, list) or not wps:
            continue
        started_at = str(record.get("timestamp") or record.get("started_at") or "")
        a0 = tuple(wps[0].get("actual") or []) if isinstance(wps[0], dict) else ()
        a1 = tuple(wps[1].get("actual") or []) if len(wps) > 1 and isinstance(wps[1], dict) else ()
        sig = (route_id, a0, a1) if len(wps) >= 2 else (route_id, a0, started_at[:19])
        if sig not in by_sig or len(wps) > len(by_sig[sig].get("waypoints", [])):
            by_sig[sig] = record

    deduped = sorted(
        by_sig.values(),
        key=lambda r: str(r.get("timestamp") or r.get("started_at") or ""),
    )
    for record in deduped:
        route_id = record["route"]
        wps = record.get("waypoints", [])
        by_i = {
            wp["index"]: wp
            for wp in wps
            if isinstance(wp, dict)
            and isinstance(wp.get("index"), int)
            and isinstance(wp.get("target"), list)
            and len(wp["target"]) == 2
        }
        for idx, wp in sorted(by_i.items()):
            if idx + 1 in by_i:
                nw = by_i[idx + 1]
                if wp.get("next_outcome") is None:
                    wp["next_outcome"] = nw.get("outcome")
                if wp.get("next_turnbacks") is None:
                    wp["next_turnbacks"] = int(nw.get("turnback_count") or nw.get("overshoot_retries") or 0)
                if wp.get("next_max_cross_track") is None:
                    wp["next_max_cross_track"] = float(nw.get("max_cross_track") or 0.0)
            grouped[route_id][idx].append(wp)

    report = {}
    for route_id, by_index in sorted(grouped.items()):
        route_file = ROUTES_DIR / f"{route_id}.json"
        doc = json.loads(route_file.read_text(encoding="utf-8")) if route_file.is_file() else None
        pts = doc.get("points", []) if isinstance(doc, dict) else []
        route_report = {}
        for idx, samples in sorted(by_index.items()):
            base_t = [float(samples[0]["target"][0]), float(samples[0]["target"][1])]
            base_r = float(samples[0].get("configured_radius", samples[0].get("radius", 3.0)))
            norm_samples = []
            for s in samples:
                sc = dict(s)
                act = sc.get("actual")
                if isinstance(act, list) and len(act) == 2:
                    sc["dx"] = float(act[0]) - base_t[0]
                    sc["dy"] = float(act[1]) - base_t[1]
                norm_samples.append(sc)
            analysis = compute_waypoint_adjustment(norm_samples, base_t, base_r)
            analysis["target"] = base_t
            route_report[idx] = analysis
        report[route_id] = route_report

        if args.apply and isinstance(doc, dict) and pts:
            changed = False
            for idx, analysis in route_report.items():
                if analysis["total"] < args.min_samples or not (0 <= idx < len(pts)):
                    continue
                pt = pts[idx]
                cur_t = [float(pt["x"]), float(pt["y"])]
                cur_r = float(pt.get("radius", 3.0))
                base_t = analysis["target"]
                base_r = analysis["configured_radius"]
                already_dist = math.hypot(cur_t[0] - base_t[0], cur_t[1] - base_t[1])
                if analysis["nudge_distance"] >= 0.05:
                    if already_dist < 0.01:
                        pt["x"], pt["y"] = analysis["suggested_target"]
                        changed = True
                    else:
                        ux = analysis["nudge"][0] / analysis["nudge_distance"]
                        uy = analysis["nudge"][1] / analysis["nudge_distance"]
                        already_along = (cur_t[0] - base_t[0]) * ux + (cur_t[1] - base_t[1]) * uy
                        if (
                            already_along > 0.01
                            and (analysis["nudge_distance"] - already_along) >= 0.05
                            and math.hypot(analysis["suggested_target"][0] - cur_t[0], analysis["suggested_target"][1] - cur_t[1]) >= 0.05
                        ):
                            pt["x"], pt["y"] = analysis["suggested_target"]
                            changed = True
                cur_r_samples = [
                    s for s in by_index[idx]
                    if abs(float(s.get("configured_radius", s.get("radius", 3.0))) - cur_r) < 0.03
                ]
                if abs(analysis["suggested_radius"] - base_r) >= 0.05:
                    if abs(cur_r - base_r) < 0.02 and abs(analysis["suggested_radius"] - cur_r) >= 0.05:
                        pt["radius"] = analysis["suggested_radius"]
                        changed = True
                    elif analysis["radius_reason"] == "widen_to_reduce_turnbacks" and analysis["suggested_radius"] > cur_r + 0.04:
                        pt["radius"] = analysis["suggested_radius"]
                        changed = True
                    elif (
                        analysis["radius_reason"] == "tighten_to_prevent_next_segment_drift"
                        and len(cur_r_samples) >= args.min_samples
                        and abs(analysis["suggested_radius"] - cur_r) >= 0.05
                    ):
                        pt["radius"] = analysis["suggested_radius"]
                        changed = True
            if changed:
                route_file.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.apply:
        for rf in ROUTES_DIR.glob("*.json"):
            try:
                b_doc = json.loads(rf.read_text(encoding="utf-8"))
            except Exception:
                continue
            sc_name = b_doc.get("fishing_shortcut") if isinstance(b_doc, dict) else None
            if sc_name and isinstance(sc_name, str):
                sc_file = ROUTES_DIR / f"{sc_name}.json"
                if sc_file.is_file():
                    sc_doc = json.loads(sc_file.read_text(encoding="utf-8"))
                    b_pts = b_doc.get("points", [])
                    sc_pts = sc_doc.get("points", [])
                    idx = next((i for i, p in enumerate(b_pts) if p.get("action") == "fishing_star"), None)
                    if idx is not None and len(sc_pts) > idx:
                        if sc_pts[:idx + 1] != b_pts[:idx + 1]:
                            sc_pts[:idx + 1] = [dict(p) for p in b_pts[:idx + 1]]
                            sc_file.write_text(json.dumps(sc_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    if not report:
        print("尚无符合条件的路线遥测记录。")
        return

    for route_id, waypoints in report.items():
        print(f"=== 路线: {route_id} ===")
        for idx, info in waypoints.items():
            notes = []
            if info["total"] >= args.min_samples and info["nudge_distance"] >= 0.05:
                notes.append(
                    f"坐标 -> ({info['suggested_target'][0]:.3f}, {info['suggested_target'][1]:.3f}) "
                    f"[{info['reason']}]"
                )
            if info["total"] >= args.min_samples and abs(info["suggested_radius"] - info["configured_radius"]) >= 0.05:
                notes.append(
                    f"半径 {info['configured_radius']:.2f} -> {info['suggested_radius']:.2f} "
                    f"[{info['radius_reason']}]"
                )
            status = "；".join(notes) if notes else "保持当前坐标与半径"
            print(
                f"  #{idx:02d} ({info['target'][0]:.3f}, {info['target'][1]:.3f}) r={info['configured_radius']:.2f} | "
                f"样本={info['total']} 顺畅={info['clean']} 回头={info['turnback']} "
                f"脱困={info['recovered']} 漏采={info['action_missed']} 失败={info['failed']} | {status}"
            )


if __name__ == "__main__":
    main()
