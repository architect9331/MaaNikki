"""Aggregate route/waypoint telemetry and compute coordinate micro-adjustments.

Reads route telemetry JSON/JSONL from `debug/route_telemetry/`, `logs/navigation/`,
or exported Sentry log files, summarizes directional stability per waypoint, and
computes bounded coordinate adjustments toward the stable sector.
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
            elif suffix == ".json":
                try:
                    obj = json.loads(path.read_text(encoding="utf-8", errors="replace"))
                except (OSError, ValueError, TypeError):
                    continue
                if isinstance(obj, dict):
                    if obj.get("waypoints") and obj.get("route"):
                        yield obj
                    elif isinstance(obj.get("telemetry"), dict) and obj["telemetry"].get("waypoints"):
                        yield obj["telemetry"]
                elif isinstance(obj, list):
                    for item in obj:
                        if isinstance(item, dict) and item.get("waypoints") and item.get("route"):
                            yield item


def compute_waypoint_adjustment(samples: list[dict], target: list[float], radius: float) -> dict:
    total = len(samples)
    clean = [s for s in samples if s.get("outcome") == "clean"]
    missed = [s for s in samples if s.get("outcome") == "action_missed"]
    recovered = [s for s in samples if s.get("outcome") == "recovered"]
    failed = [s for s in samples if s.get("outcome") == "failed"]
    problem = missed + recovered + failed

    octant_stats: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "clean": 0, "problem": 0})
    for s in samples:
        direction = s.get("direction") or "UNKNOWN"
        octant_stats[direction]["total"] += 1
        if s.get("outcome") == "clean":
            octant_stats[direction]["clean"] += 1
        else:
            octant_stats[direction]["problem"] += 1

    # Compute mean offset of clean arrivals vs problem arrivals.
    def mean_offset(group: list[dict]) -> tuple[float, float]:
        valid = [
            (float(s["dx"]), float(s["dy"]))
            for s in group
            if isinstance(s.get("dx"), (int, float)) and isinstance(s.get("dy"), (int, float))
        ]
        if not valid:
            return 0.0, 0.0
        return (
            sum(x for x, _ in valid) / len(valid),
            sum(y for _, y in valid) / len(valid),
        )

    clean_dx, clean_dy = mean_offset(clean)
    prob_dx, prob_dy = mean_offset(problem)

    nudge_x, nudge_y = 0.0, 0.0
    reason = "stable"
    if problem and clean:
        # Shift toward the clean arrival centroid and away from the problem centroid.
        vec_x = clean_dx - prob_dx
        vec_y = clean_dy - prob_dy
        mag = math.hypot(vec_x, vec_y)
        problem_rate = len(problem) / total
        if mag >= 0.08 and problem_rate >= 0.15:
            scale = min(MAX_STEP_UNITS, mag * 0.5 * min(1.0, problem_rate * 2))
            nudge_x = vec_x / mag * scale
            nudge_y = vec_y / mag * scale
            reason = "shift_away_from_failure_direction"
    elif problem and not clean:
        # All recorded runs had friction/failure; shift opposite to the mean stuck/missed offset.
        mag = math.hypot(prob_dx, prob_dy)
        if mag >= 0.1:
            scale = min(MAX_STEP_UNITS, mag * 0.35)
            nudge_x = -prob_dx / mag * scale
            nudge_y = -prob_dy / mag * scale
            reason = "repel_all_problem_offset"

    # Clamp adjustment within half of the waypoint's radius so we never break segment topology.
    max_allowed = min(MAX_STEP_UNITS, max(0.15, radius * 0.4))
    nudge_mag = math.hypot(nudge_x, nudge_y)
    if nudge_mag > max_allowed:
        nudge_x = nudge_x / nudge_mag * max_allowed
        nudge_y = nudge_y / nudge_mag * max_allowed
        nudge_mag = max_allowed

    return {
        "total": total,
        "clean": len(clean),
        "recovered": len(recovered),
        "action_missed": len(missed),
        "failed": len(failed),
        "problem_rate": round(len(problem) / total, 4) if total else 0.0,
        "clean_mean_offset": [round(clean_dx, 3), round(clean_dy, 3)],
        "problem_mean_offset": [round(prob_dx, 3), round(prob_dy, 3)],
        "octants": dict(octant_stats),
        "nudge": [round(nudge_x, 3), round(nudge_y, 3)],
        "nudge_distance": round(nudge_mag, 3),
        "suggested_target": [round(target[0] + nudge_x, 3), round(target[1] + nudge_y, 3)],
        "reason": reason,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze route telemetry and suggest coordinate tuning.")
    parser.add_argument(
        "inputs",
        nargs="*",
        default=[str(ROOT / "debug/route_telemetry"), str(ROOT / "logs/navigation")],
        help="Telemetry files or directories to analyze.",
    )
    parser.add_argument("--route", help="Filter by route id.")
    parser.add_argument("--min-samples", type=int, default=5, help="Minimum samples per waypoint to recommend a shift.")
    parser.add_argument("--apply", action="store_true", help="Write recommended coordinate shifts to resource/routes/daily/*.json.")
    parser.add_argument("--json", action="store_true", help="Print full JSON report.")
    args = parser.parse_args()

    grouped: dict[str, dict[int, list[dict]]] = defaultdict(lambda: defaultdict(list))
    latest_targets: dict[tuple[str, int], tuple[list[float], float]] = {}

    for record in iter_telemetry_records([Path(p) for p in args.inputs]):
        route_id = record.get("route")
        if not isinstance(route_id, str) or (args.route and route_id != args.route):
            continue
        for wp in record.get("waypoints", []):
            if not isinstance(wp, dict):
                continue
            idx = wp.get("index")
            target = wp.get("target")
            radius = wp.get("radius", 3.0)
            if isinstance(idx, int) and isinstance(target, list) and len(target) == 2:
                grouped[route_id][idx].append(wp)
                latest_targets[(route_id, idx)] = ([float(target[0]), float(target[1])], float(radius))

    report = {}
    for route_id, by_index in sorted(grouped.items()):
        route_report = {}
        for idx, samples in sorted(by_index.items()):
            target, radius = latest_targets[(route_id, idx)]
            analysis = compute_waypoint_adjustment(samples, target, radius)
            analysis["target"] = target
            analysis["radius"] = radius
            route_report[idx] = analysis
        report[route_id] = route_report

        if args.apply:
            route_file = ROUTES_DIR / f"{route_id}.json"
            if route_file.is_file():
                doc = json.loads(route_file.read_text(encoding="utf-8"))
                changed = False
                for idx, analysis in route_report.items():
                    if (analysis["total"] >= args.min_samples
                            and analysis["nudge_distance"] >= 0.05
                            and 0 <= idx < len(doc.get("points", []))):
                        pt = doc["points"][idx]
                        if (abs(float(pt["x"]) - analysis["target"][0]) < 0.01
                                and abs(float(pt["y"]) - analysis["target"][1]) < 0.01):
                            pt["x"], pt["y"] = analysis["suggested_target"]
                            changed = True
                if changed:
                    route_file.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    if not report:
        print("尚无符合条件的路线遥测记录。")
        return

    for route_id, waypoints in report.items():
        print(f"=== 路线: {route_id} ===")
        for idx, info in waypoints.items():
            status = (
                f"建议微调 -> ({info['suggested_target'][0]:.3f}, {info['suggested_target'][1]:.3f}) "
                f"[dx={info['nudge'][0]:+.3f}, dy={info['nudge'][1]:+.3f}]"
                if info["total"] >= args.min_samples and info["nudge_distance"] >= 0.05
                else "保持当前坐标"
            )
            print(
                f"  #{idx:02d} ({info['target'][0]:.3f}, {info['target'][1]:.3f}) | "
                f"样本={info['total']} 顺畅={info['clean']} 脱困={info['recovered']} "
                f"漏采={info['action_missed']} 失败={info['failed']} | {status}"
            )


if __name__ == "__main__":
    main()
