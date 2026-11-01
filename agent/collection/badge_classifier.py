"""Small, three-class colour/gradient model for normal-inventory badges.

The exported one-v-rest linear SVM needs only NumPy and OpenCV at runtime.
An accepted class must be outside its SVM margin, have no competing positive
class, and remain inside that class's training-feature envelope. These are
model-space checks, not calibrated probabilities or guarantees on new skins.
"""
from __future__ import annotations

import json

import cv2
import numpy as np


CLASSES = ("locked", "stack", "single")
FEATURE_VERSION = 1
FEATURE_SIZE = 3924


def badge_roi(box):
    x, y, w, h = box
    # Include wide count tabs and the lock, excluding the bottom/right rim.
    return [x + w - 51, y + h - 35, 48, 28]


def features(patch):
    """Preserve colour/antialiasing; HOG uses continuous grayscale gradients."""
    if patch.shape != (28, 48, 3) or patch.dtype != np.uint8:
        raise ValueError("祝福闪光角标图像尺寸无效")
    image = cv2.resize(patch, (48, 32), interpolation=cv2.INTER_LINEAR)
    hog = cv2.HOGDescriptor((48, 32), (8, 8), (4, 4), (4, 4), 9)
    gradients = hog.compute(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)).reshape(-1)
    gradients /= max(float(np.linalg.norm(gradients)), np.finfo(np.float32).eps)
    colour = cv2.cvtColor(cv2.resize(image, (24, 16), interpolation=cv2.INTER_AREA),
                         cv2.COLOR_BGR2RGB).astype(np.float32).reshape(-1) / 255
    colour /= np.sqrt(colour.size)
    vector = np.concatenate((gradients, colour))
    return vector / max(float(np.linalg.norm(vector)), np.finfo(np.float32).eps)


class BadgeClassifier:
    def __init__(self, path):
        with np.load(path, allow_pickle=False) as model:
            self.metadata = json.loads(str(model["metadata"].item()))
            self.weights = model["weights"].astype(np.float32)
            self.bias = model["bias"].astype(np.float32)
            self.centres = model["centres"].astype(np.float32)
            self.radii = model["radii"].astype(np.float32)
        if (not isinstance(self.metadata, dict) or self.metadata.get("schema_version") != 1
                or self.metadata.get("feature_version") != FEATURE_VERSION
                or self.metadata.get("classes") != list(CLASSES)
                or self.metadata.get("feature_size") != FEATURE_SIZE
                or not isinstance(self.metadata.get("model_version"), str)
                or self.weights.shape != (3, FEATURE_SIZE) or self.bias.shape != (3,)
                or self.centres.shape != (3, FEATURE_SIZE) or self.radii.shape != (3,)
                or any(not np.isfinite(value).all() for value in
                       (self.weights, self.bias, self.centres, self.radii))
                or np.any(self.radii <= 0)):
            raise ValueError("祝福闪光角标模型格式无效")

    def classify(self, frame, box):
        x, y, w, h = badge_roi(box)
        patch = frame[y:y + h, x:x + w]
        if patch.shape != (h, w, 3):
            return {"badge_state": None, "reason": "incomplete_crop", "roi": [x, y, w, h]}
        vector = features(patch)
        scores = self.weights @ vector + self.bias
        winner = int(np.argmax(scores))
        others = np.delete(scores, winner)
        distance = float(np.linalg.norm(vector - self.centres[winner]))
        reason = None
        if float(scores[winner]) < 1 or np.any(others >= 0):
            reason = "svm_margin"
        elif distance > float(self.radii[winner]):
            reason = "outside_training_envelope"
        return {
            "classifier": "colour_hog_linear_svm_v1", "roi": [x, y, w, h],
            "model_version": self.metadata["model_version"],
            "predicted": CLASSES[winner], "badge_state": CLASSES[winner] if reason is None else None,
            "scores": {name: float(value) for name, value in zip(CLASSES, scores)},
            "feature_distance": distance, "feature_radius": float(self.radii[winner]),
            "reason": reason,
        }
