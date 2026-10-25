"""Stationary map comparisons for explicitly selected diagnostic routes."""
from __future__ import annotations

from datetime import datetime
import math
from pathlib import Path
import re

from diagnostics import save_image
from file_io import write_json
from task_control import task_time as time

from .models import NavigationError


def compare_map_positions(runtime, teleporter, point, event):
    """Record both map estimates without changing coordinates or locator history."""
    from daily.settings import ROOT

    label = str(point.params.get("sample", "point"))
    name = re.sub(r"[^a-zA-Z0-9_-]", "_", label)[:40] or "point"
    root = getattr(runtime, "directory", None) or ROOT / "logs" / "navigation"
    folder = Path(root) / (datetime.now().strftime("%Y%m%d-%H%M%S-%f")+"-map-compare-"+name)
    report = {"sample": label, "target": [point.x, point.y],
              "coordinate_system": "navigation_map_pixels", "completed": False,
              "before": [], "bigmap": [], "after": []}
    locator, inputs = teleporter.locator, teleporter.inputs
    generation = getattr(inputs, "generation", 0)
    map_attempted = False

    def pause(seconds):
        inputs.check()
        if not runtime.pause(seconds):
            raise NavigationError("地图定位对照已停止。")

    def small_samples(stage):
        for index in range(2):
            pause(.25)
            frame = runtime.capture()
            filename = f"{stage}-{index+1:02d}.png"
            if frame is not None:
                save_image(folder / filename, frame)
            ready = runtime.recognize("MaaNikki_MainDetected", image=frame) if frame is not None else None
            if not ready or not ready.hit:
                raise NavigationError("地图定位对照未能确认可移动主界面。")
            pose = locator.locate(frame, (point.x, point.y), commit=False,
                                  reset_continuity=True)
            report[stage].append({"time": datetime.now().isoformat(), "pose": pose.dict(),
                                  "diagnostics": dict(locator.heading_diagnostics), "image": filename})

    def delta(a, b):
        return {"dx": a[0]-b[0], "dy": a[1]-b[1], "distance": math.dist(a, b)}

    try:
        runtime.log(f"地图定位对照 {label}：正在记录小地图与大地图位置。")
        inputs.check()
        small_samples("before")
        map_attempted = True
        teleporter.deadline = time.monotonic()+30
        if not runtime.ui.open_map(event):
            raise NavigationError("地图定位对照未能打开地图。")
        teleporter.zoom()
        # Open a fresh player-centered map; never pan or select a map marker.
        for index in range(6):
            pause(.3)
            frame = runtime.capture()
            filename = f"bigmap-{index+1:02d}.png"
            if frame is not None:
                save_image(folder / filename, frame)
            for node in ("MaaNikki_Navigation_MapFeature", "MaaNikki_Navigation_MapMaxScale"):
                result = runtime.recognize(node, image=frame) if frame is not None else None
                if not result or not result.hit:
                    raise NavigationError("地图定位对照未能确认地图页面或最大缩放。")
            coarse, _ = teleporter.map_pose(frame)
            refined = teleporter.refine_player_pose(frame, coarse)
            windows = list(teleporter.map_diagnostics)
            accepted = [(w["x"], w["y"]) for w in windows[1:] if w.get("accepted")]
            refinement_used = (len(accepted) >= 2 and all(math.dist(a, b) <= 2
                               for a in accepted for b in accepted))
            sample = {"time": datetime.now().isoformat(), "coarse": list(coarse),
                      "refined": list(refined), "refinement_used": refinement_used,
                      "windows": windows, "image": filename}
            report["bigmap"].append(sample)
            if len(report["bigmap"]) >= 2:
                previous = report["bigmap"][-2]
                stable = all(math.dist(previous[key][:2], sample[key][:2]) <= .35
                             for key in ("coarse", "refined"))
                credible = all(p[2] >= .6 and p[3] >= .01 for p in (coarse, refined))
                if stable and credible:
                    break
        else:
            raise NavigationError("地图定位对照的大地图位置未稳定。")
        if not runtime.main():
            raise NavigationError("地图定位对照未能返回主界面。")
        map_attempted = False
        small_samples("after")
        samples = report["before"]+report["after"]
        positions = [(s["pose"]["x"], s["pose"]["y"]) for s in samples]
        spread = max(math.dist(a, b) for a in positions for b in positions)
        focus_changed = getattr(inputs, "generation", 0) != generation
        stationary = (spread <= .4 and not focus_changed
                      and all(s["pose"]["position_reliable"] for s in samples))
        before, after = positions[1], positions[-1]
        report.update(completed=True, stationary=stationary, minimap_spread=spread,
                      refinement_used=refinement_used,
                      focus_changed=focus_changed, minimap_after_minus_before=delta(after, before),
                      coarse_minus_before=delta(coarse[:2], before),
                      coarse_minus_after=delta(coarse[:2], after),
                      refined_minus_before=delta(refined[:2], before),
                      refined_minus_after=delta(refined[:2], after))
        result = report["refined_minus_after"]
        status = "采样位置稳定" if stationary else "采样期间位置或焦点有变化"
        method = "大地图细化位置" if refinement_used else "大地图粗定位（细化未通过）"
        runtime.log(f"地图定位对照 {label}：{method}减小地图，"
                    f"X {result['dx']:+.2f}，Y {result['dy']:+.2f}，"
                    f"距离 {result['distance']:.2f}；{status}。")
        return report
    except (NavigationError, OSError, RuntimeError, ValueError) as error:
        report["error"] = str(error)
        raise
    finally:
        # Flush partial samples before recovery, even if capture or map matching failed.
        try:
            write_json(folder / "comparison.json", report)
            event({"type": "map_position_comparison", "directory": str(folder), **report})
        finally:
            if map_attempted and not runtime.stopped and not runtime.main():
                raise NavigationError("地图定位对照未能恢复主界面。")
