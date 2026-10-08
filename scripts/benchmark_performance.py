"""Offline recognition benchmark: python/python.exe -B scripts/benchmark_performance.py.

Uses reproducible synthetic scenes and real templates; never connects to the game.
Compare --output reports made before and after a change on the same machine.
"""
from pathlib import Path
from types import SimpleNamespace
import argparse
import hashlib
import importlib.util
import json
import os
import statistics
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
os.environ.setdefault("MAAFW_BINARY_PATH", str(ROOT / "maafw"))

import cv2
import numpy as np
from daily.runtime import Runtime
from daily.ui import catalog, fields


def benchmark(rounds=30, baseline_ui=None):
    nodes = {}
    for path in sorted((ROOT / "resource/pipeline").glob("*.json")):
        nodes.update(json.loads(path.read_text(encoding="utf-8")))
    context = SimpleNamespace(tasker=SimpleNamespace(stopping=False, controller=None),
                              get_node_data=lambda name: nodes.get(name, {}))
    runtime = Runtime(context)
    if baseline_ui:
        spec = importlib.util.spec_from_file_location("maanikki_baseline_ui", baseline_ui)
        baseline = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(baseline)
        runtime.ui = baseline.GameUI(runtime, ROOT / "resource")
    frame = np.random.default_rng(20261007).integers(0, 256, (720, 1280, 3), dtype=np.uint8)
    names = ("IconPickupFeature", "IconSkip", "IconSkipDialog", "IconDungeonFeature")
    examples = []
    for name in names:
        data = catalog(ROOT / "resource")["assets"][name]
        definition = fields(context, "MaaNikki_Asset_"+name.lower())
        path = definition.get("template", data["template"])
        path = path[0] if isinstance(path, list) else path
        roi = (runtime.ui.roi("AreaPickup") if name == "IconPickupFeature"
               else definition.get("roi") or data["roi"] or [0, 0, 1280, 720])
        scale = 2/3 if name == "IconPickupFeature" else 1
        template = cv2.imread(str(ROOT / "resource/image" / path), cv2.IMREAD_COLOR)
        if not path.startswith("nikki/") and scale != 1:
            template = cv2.resize(template, None, fx=scale, fy=scale)
        positive = np.zeros_like(frame)
        height, width = template.shape[:2]
        x, y = roi[0]+(roi[2]-width)//2, roi[1]+(roi[3]-height)//2
        positive[y:y+height, x:x+width] = template
        examples.extend((name, image, roi, scale) for image in (frame, positive))

    def scan():
        return [runtime.ui.asset_boxes(name, image=image, roi=roi, gray=(210, 255),
                                       scale=scale, threshold=.75)
                for name, image, roi, scale in examples]

    scan()  # Exclude imports and first-use initialization.
    timings, results = [], []
    with patch.object(cv2, "imread", wraps=cv2.imread) as reads:
        for _ in range(5):
            started = time.perf_counter()
            for _ in range(rounds):
                results.append(scan())
            timings.append((time.perf_counter()-started)*1000/rounds)
        reads_count = reads.call_count
    return {"scene": "four real UI templates, their normal ROIs, seeded negatives and embedded positives",
            "rounds": rounds*5, "recognitions": rounds*5*len(examples),
            "template_reads": reads_count,
            "median_scan_ms": round(statistics.median(timings), 3),
            "trial_scan_ms": [round(value, 3) for value in timings],
            "detections_per_scan": [len(boxes) for boxes in results[0]],
            "result_sha256": hashlib.sha256(json.dumps(results).encode()).hexdigest()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--baseline-ui", type=Path, help="Saved earlier daily/ui.py for an identical comparison")
    args = parser.parse_args()
    report = json.dumps(benchmark(baseline_ui=args.baseline_ui), ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report+"\n", encoding="utf-8")
    print(report)
