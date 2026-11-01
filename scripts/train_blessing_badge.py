"""Export the inventory badge classifier from confirmed local scan records.

Training only; this script never connects to the game or runs verification
tasks. Account identifiers, logs and sample images are not included in the
model. Install build/requirements-model-training.txt in a development Python.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

import cv2
import numpy as np
from sklearn.svm import LinearSVC


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
from collection.badge_classifier import CLASSES, FEATURE_VERSION, badge_roi, features


def samples(directory):
    """Use button-confirmed locks/singles and independently confirmed counts."""
    seen = {}
    for folder in sorted(directory.iterdir()):
        report = folder / "locking.json"
        if not report.is_file():
            continue
        scan = json.loads(report.read_text(encoding="utf-8"))
        # Whole-inventory capacity corroborates the single-copy annotations.
        complete = scan.get("complete") is True and scan.get("quantity_check", {}).get("complete") is True
        for path in sorted(folder.glob("card_*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            item = data.get("item", {})
            button = data.get("selected_lock_button") or {}
            quantity = item.get("count_evidence") or {}
            count = item.get("count")
            if data.get("error") or item.get("catalog_unknown"):
                continue
            confirmed = (data.get("selected_for_observation") is True
                         and button.get("strategy") == "shackle_gap_then_gold_background"
                         and button.get("state") == ("locked" if item.get("locked") else "unlocked"))
            label = None
            if confirmed and item.get("locked") is True:
                label = "locked"
            elif (confirmed and complete and item.get("locked") is False and count == 1
                  and quantity.get("source") == "no_stack_badge"):
                label = "single"
            elif (item.get("locked") is False and type(count) is int and count >= 2
                  and quantity.get("source") in ("stack_confirmed_ocr", "identical_stack_badge")):
                label = "stack"
            if label is None or not isinstance(data.get("card"), str):
                continue
            image_path = (folder / data["card"]).resolve()
            if not image_path.is_relative_to(folder.resolve()):
                raise ValueError("Sample image must stay inside its scan directory")
            image = cv2.imdecode(np.fromfile(image_path, np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                continue
            h, w = image.shape[:2]
            x, y, width, height = badge_roi([0, 0, w, h])
            # Keep the source card for geometrically grounded +/-1 px jitter.
            if x < 1 or y < 1 or x + width >= w or y + height >= h:
                continue
            crop = image[y:y + height, x:x + width]
            digest = hashlib.sha256(crop.tobytes()).hexdigest()
            if digest in seen:
                if seen[digest] != label:
                    raise ValueError("Identical badge has conflicting confirmed labels")
                continue
            seen[digest] = label
            yield image, label


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs", type=Path, default=ROOT / "logs/blessing-lock")
    parser.add_argument("--output", type=Path, default=ROOT / "resource/model/blessing_badge.npz")
    parser.add_argument("--version", required=True)
    parser.add_argument("--update-lock", action="store_true", help="Update build/resource-models.lock.json")
    args = parser.parse_args()
    vectors, labels = [], []
    counts = Counter()
    dataset_hash = hashlib.sha256()
    for image, label in samples(args.logs):
        counts[label] += 1
        h, w = image.shape[:2]
        x, y, width, height = badge_roi([0, 0, w, h])
        dataset_hash.update(label.encode("ascii"))
        dataset_hash.update(image[y:y + height, x:x + width].tobytes())
        # Capture normalization can move an edge by one pixel. Global gain
        # covers exposure drift while retaining the original colour ratios.
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                patch = image[y + dy:y + dy + height, x + dx:x + dx + width]
                for gain in ((.9, 1., 1.1) if dx == dy == 0 else (1.,)):
                    adjusted = np.clip(patch.astype(np.float32) * gain, 0, 255).astype(np.uint8)
                    vectors.append(features(adjusted))
                    labels.append(CLASSES.index(label))
    if set(counts) != set(CLASSES):
        raise ValueError(f"Confirmed samples required for every class: {dict(counts)}")
    matrix = np.asarray(vectors, dtype=np.float32)
    target = np.asarray(labels)
    # Standard one-v-rest maximum-margin model. Do not tune parameters to a
    # recorded failure or interpret decision scores as class probabilities.
    classifier = LinearSVC(C=1., class_weight="balanced", dual=False, random_state=0,
                           max_iter=10000)
    classifier.fit(matrix, target)
    centres = np.array([matrix[target == index].mean(axis=0) for index in range(3)], dtype=np.float32)
    radii = np.array([np.linalg.norm(matrix[target == index] - centres[index], axis=1).max()
                      for index in range(3)], dtype=np.float32)
    metadata = {
        "schema_version": 1, "feature_version": FEATURE_VERSION, "model_version": args.version,
        "classes": list(CLASSES), "feature_size": int(matrix.shape[1]),
        "samples": {name: counts[name] for name in CLASSES}, "augmented_samples": len(labels),
        "dataset_sha256": dataset_hash.hexdigest(),
        "training": {"algorithm": "LinearSVC one-v-rest", "C": 1., "class_weight": "balanced",
                     "scikit_learn": "1.4.2", "input": "BGR crop 48x28; continuous RGB and HOG",
                     "augmentation": "nine 1px translations; centre gain 0.9/1/1.1"},
        "label_source": "local inventory crops; lock-button confirmation and confirmed count OCR; singles require complete capacity audit",
        "acceptance": "winner score >= 1, competing scores < 0, distance <= maximum training-class radius",
        "limitations": "Small local sample set; margins are not calibrated confidence. Gameplay validation is performed by users.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(args.output, weights=classifier.coef_.astype(np.float32),
                        bias=classifier.intercept_.astype(np.float32), centres=centres, radii=radii,
                        metadata=np.array(json.dumps(metadata, ensure_ascii=False)))
    if args.update_lock:
        relative = args.output.resolve().relative_to(ROOT).as_posix()
        lock_path = ROOT / "build/resource-models.lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["files"] = [entry for entry in lock["files"] if entry["path"] != relative]
        lock["files"].append({"path": relative, "size": args.output.stat().st_size,
                              "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
                              "source": "MaaNikki confirmed inventory badge crops; scripts/train_blessing_badge.py"})
        lock_path.write_text(json.dumps(lock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"samples": metadata["samples"], "augmented_samples": len(labels),
                      "features": matrix.shape[1], "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
