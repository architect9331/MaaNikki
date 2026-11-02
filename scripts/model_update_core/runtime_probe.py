"""Load candidate models with the application's own classes in the packaged runtime.

Run by the runtime Python, which holds the exact OpenCV and ONNX Runtime versions
users receive. No game, window or agent connection is involved. The modules are
loaded directly from their files, so package start-up code cannot touch logs or config.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import cv2
import numpy as np


def load(path, name):
    specification = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def read(path):
    return cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_COLOR)


def main():
    agent, root, evaluation = map(Path, sys.argv[1:4])
    report = {"opencv": cv2.__version__}
    wardrobe = load(agent / "wardrobe/model.py", "probe_wardrobe_model")
    model = wardrobe.WardrobeModel(root / "resource/model/wardrobe")
    report["wardrobe"] = {"version": model.version, "fingerprint": model.fingerprint, "cases": 0, "correct": 0, "wrong": []}
    path = evaluation / "wardrobe-probe-cases.json"
    for case in json.loads(path.read_text(encoding="utf-8")) if path.is_file() else ():
        image = read(case["image"])
        prediction = model.predict(image, [[0, 0, image.shape[1], image.shape[0]]], case["part"])[0]
        report["wardrobe"]["cases"] += 1
        if prediction["class_id"] == case["class_id"]:
            report["wardrobe"]["correct"] += 1
        else:
            report["wardrobe"]["wrong"].append({**case, "predicted": prediction["class_id"]})
    catalog = json.loads((root / "resource/data/blessing_catalog.json").read_text(encoding="utf-8"))
    cards = load(agent / "collection/card_models.py", "probe_card_models")
    models = cards.CardModels(root / "resource/model", catalog)
    report["blessing"] = {"versions": {kind: row["model_version"] for kind, row in models.metadata.items()},
                          "entries": len(models.entries), "cases": 0, "correct": 0, "wrong": []}
    path = evaluation / "blessing-probe-cases.json"
    for case in json.loads(path.read_text(encoding="utf-8")) if path.is_file() else ():
        # Saved in RGB by the evaluator through PIL; imdecode returns BGR as at runtime.
        image = read(case["image"])
        result = models.match_batch(image, [[0, 0, image.shape[1], image.shape[0]]], case["slot"])[0]["candidate"]
        report["blessing"]["cases"] += 1
        if (result["name"], result["color"]) == (case["name"], case["color"]):
            report["blessing"]["correct"] += 1
        else:
            report["blessing"]["wrong"].append({**case, "predicted": [result["name"], result["color"]]})
    print(json.dumps(report, ensure_ascii=True))


if __name__ == "__main__":
    main()
