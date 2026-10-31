"""Bounded in-memory observations; publish evidence only at failure boundaries."""
from __future__ import annotations

from collections import deque
from datetime import datetime
from functools import lru_cache
import json
from pathlib import Path
import re
import time
from uuid import uuid4

import cv2
import numpy as np

from file_io import write_json
from message_log import write_message


ROOT = Path(__file__).resolve().parents[1]


@lru_cache(maxsize=1)
def image_policy():
    return json.loads((ROOT / "resource/data/diagnostic_images.json").read_text(encoding="utf-8"))


def diagnostic_image(frame, *, roi=None, full_frame=True):
    """Mask in full-frame coordinates before applying an explicit crop."""
    if frame is None or not frame.size:
        raise ValueError("诊断截图为空。")
    image = frame.copy()
    height, width = image.shape[:2]
    masked = None
    if full_frame:
        policy = image_policy()
        _, rh = policy["reference_size"]
        x, y, w, h = policy["uid_roi"]
        # Scale with height, matching Maa's uniform short-side scaling.
        scale = height/rh
        left, top = round(x*scale), round(y*scale)
        right, bottom = min(width, round((x+w)*scale)), min(height, round((y+h)*scale))
        image[top:bottom, left:right] = 0
        masked = [left, top, right-left, bottom-top]
    if roi is not None:
        x, y, w, h = roi
        if min(x, y) < 0 or min(w, h) <= 0 or x+w > width or y+h > height:
            raise ValueError("诊断裁剪区域超出截图。")
        image = image[y:y+h, x:x+w]
    return image, masked


def save_image(path, frame, *, roi=None, full_frame=True):
    path = Path(path)
    image, _ = diagnostic_image(frame, roi=roi, full_frame=full_frame)
    png = path.suffix.lower() == ".png"
    ok, encoded = cv2.imencode(".png" if png else ".jpg", image,
        [cv2.IMWRITE_PNG_COMPRESSION, 1] if png else [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        raise OSError("诊断截图编码失败。")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+"."+uuid4().hex+".tmp")
    try:
        temporary.write_bytes(encoded.tobytes())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def brief(value, depth=0):
    """Keep diagnostics JSON-safe and bounded, including native OCR result values."""
    if depth > 5:
        return str(value)[:160]
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        import math
        return round(value, 5) if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return value[:600]
    if isinstance(value, dict):
        return {str(key)[:80]: brief(item, depth+1) for key, item in list(value.items())[:24]}
    if isinstance(value, (list, tuple)):
        return [brief(item, depth+1) for item in value[:24]]
    return str(value)[:160]


class FailureEvidence:
    def __init__(self):
        self.frame = None
        self.frame_at = None
        self.generation = None
        self.observations = deque(maxlen=24)
        self.saved = 0
        self.records = deque(maxlen=24)
        self._last_saved = None

    def remember_frame(self, image, generation):
        # One shared reference; never copy screenshots or queue native captures.
        self.frame = image
        self.frame_at = time.monotonic()
        self.generation = generation

    def observe(self, kind, **detail):
        try:
            self.observations.append({"time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                                      "kind": kind, **brief(detail)})
        except Exception:
            pass

    def release_frame(self):
        self.frame = None
        self.frame_at = None

    def save(self, stage, reason, *, image=None, details=None, generation=None):
        """Never capture, send input, or raise over the original task failure."""
        try:
            frame = self.frame if image is None else image
            key = (stage, str(reason), id(frame), self.frame_at)
            if key == self._last_saved:
                return None
            now = datetime.now().astimezone()
            label = re.sub(r"[^a-zA-Z0-9_-]", "_", stage)[:72]
            stem = now.strftime("%Y%m%d-%H%M%S-%f")+"-"+label+"-"+uuid4().hex[:8]
            folder = ROOT / "debug/on_error"
            folder.mkdir(parents=True, exist_ok=True)
            record = {"time": now.isoformat(timespec="milliseconds"), "stage": stage,
                      "reason": str(reason)[:1200], "details": brief(details or {}),
                      "observations": list(self.observations), "image_status": "unavailable",
                      "frame_source": "last_observed" if image is None else "provided",
                      "frame_age_ms": round((time.monotonic()-self.frame_at)*1000)
                                      if image is None and self.frame_at is not None else None,
                      "focus_generation": self.generation,
                      "focus_changed": generation is not None and generation != self.generation}
            if frame is not None and frame.size:
                height, width = frame.shape[:2]
                record["frame_size"] = [width, height]
                path = save_image(folder / (stem+".jpg"), frame)
                record.update(image="on_error/"+path.name, image_status="saved",
                              uid_roi_reference=image_policy()["uid_roi"], crop_roi=None)
            write_json(folder / (stem+".json"), record)
            # Existing Sentry collection reads .log files and on_error images.
            # Include the same metadata in that channel; JSON is also kept locally.
            write_message("失败现场："+json.dumps(record, ensure_ascii=False, allow_nan=False), None)
            self.saved += 1
            self._last_saved = key
            reference = {"image": record.get("image"), "image_status": record["image_status"],
                         "record": "on_error/"+stem+".json"}
            self.records.append(reference)
            return reference
        except Exception as error:
            write_message(f"失败现场保存未完成：{type(error).__name__}: {error}", None)
            return {"image_status": "save_failed"}

    def republish(self, references):
        """Carry an earlier stage's evidence into the failing task's attachment window."""
        folder = (ROOT / "debug/on_error").resolve()
        for reference in references:
            try:
                path = (ROOT / "debug" / reference["record"]).resolve()
                if path.parent != folder or path.suffix != ".json":
                    continue
                original = json.loads(path.read_text(encoding="utf-8"))
                image = (ROOT / "debug" / original["image"]).resolve()
                if image.parent != folder or image.suffix != ".jpg":
                    continue
                frame = cv2.imdecode(np.frombuffer(image.read_bytes(), dtype=np.uint8),
                                     cv2.IMREAD_COLOR)
                if frame is None:
                    continue
                self.save("daily_deferred_origin", original["reason"], image=frame, details={
                    "source_record": reference["record"], "source_time": original["time"],
                    "source_stage": original["stage"], "source_details": original["details"]})
            except (OSError, ValueError, KeyError, TypeError, cv2.error) as error:
                write_message(f"朝夕前置失败现场保留未完成：{type(error).__name__}: {error}", None)
