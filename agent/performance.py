"""Bounded operation timings, written once per custom action (no screenshots)."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
import json
from pathlib import Path
import time
from uuid import uuid4


_ACTIVE = ContextVar("maanikki_performance", default=None)
_LIMITS = (1, 2, 5, 10, 20, 50, 100, 200, 300, 500, 1000, 2000, 5000, 10000)


class Performance:
    def __init__(self, action, directory):
        self.action, self.directory = action, Path(directory)
        self.started = time.perf_counter()
        self.filename = datetime.now().strftime("%Y%m%d-%H%M%S-%f")+"-"+uuid4().hex[:8]+".json"
        self.stages, self.counters = {}, {}
        self.succeeded = None
        self.last_saved = self.started

    def record(self, name, seconds):
        value = max(0, seconds*1000)
        stage = self.stages.setdefault(name, {"count": 0, "total_ms": 0.0,
                                             "max_ms": 0.0, "buckets": [0]*(len(_LIMITS)+1)})
        stage["count"] += 1
        stage["total_ms"] += value
        stage["max_ms"] = max(stage["max_ms"], value)
        bucket = next((i for i, limit in enumerate(_LIMITS) if value <= limit), len(_LIMITS))
        stage["buckets"][bucket] += 1

    def snapshot(self):
        stages = {}
        for name, stage in self.stages.items():
            total = 0
            upper = stage["max_ms"]
            for index, count in enumerate(stage["buckets"]):
                total += count
                if total >= stage["count"]*.95:
                    upper = _LIMITS[index] if index < len(_LIMITS) else stage["max_ms"]
                    break
            stages[name] = {"count": stage["count"],
                            "total_ms": round(stage["total_ms"], 3),
                            "mean_ms": round(stage["total_ms"]/stage["count"], 3),
                            "max_ms": round(stage["max_ms"], 3),
                            "p95_upper_ms": round(upper, 3)}
        return {"schema_version": 1, "action": self.action, "succeeded": self.succeeded,
                "elapsed_ms": round((time.perf_counter()-self.started)*1000, 3),
                "stages": stages, "counters": dict(self.counters),
                "note": "Stages can overlap; p95_upper_ms is a histogram estimate, not an exact percentile."}

    def save(self):
        # Logging errors must never interrupt gameplay or replace a task result.
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / self.filename
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.snapshot(), ensure_ascii=False, indent=2)+"\n",
                                 encoding="utf-8")
            temporary.replace(path)
        except OSError:
            pass
        finally:
            # An unwritable log directory must not cause a retry every frame.
            self.last_saved = time.perf_counter()


def count(name, amount=1):
    profile = _ACTIVE.get()
    if profile is not None:
        profile.counters[name] = profile.counters.get(name, 0)+amount


@contextmanager
def measure(name):
    profile = _ACTIVE.get()
    if profile is None:
        yield
        return
    started = time.perf_counter()
    try:
        yield
    finally:
        profile.record(name, time.perf_counter()-started)


def checkpoint():
    profile = _ACTIVE.get()
    if profile is not None and time.perf_counter()-profile.last_saved >= 60:
        profile.save()


@contextmanager
def action_profile(action):
    if _ACTIVE.get() is not None:
        yield None
        return
    profile = Performance(action, Path(__file__).resolve().parents[1] / "logs/performance")
    token = _ACTIVE.set(profile)
    try:
        yield profile
    finally:
        _ACTIVE.reset(token)
        profile.save()
