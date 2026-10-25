"""Periodic deadlines shared by screenshot-driven resident helpers."""
from __future__ import annotations

import math


class PeriodicSchedule:
    """Keep checks on one clock; skip missed periods instead of queuing them."""
    def __init__(self, intervals, cooldowns, *, now=0.0):
        if (not intervals or any(not math.isfinite(value) or value <= 0
                                 for value in (*intervals.values(), *cooldowns.values()))):
            raise ValueError("常驻检查间隔必须为正数。")
        self.intervals = dict(intervals)
        self.cooldowns = dict(cooldowns)
        self.epoch = now
        self.next_check = dict.fromkeys(intervals, now)

    def due(self, now):
        return [name for name, deadline in self.next_check.items() if now >= deadline]

    def checked(self, name, now, *, acted=False):
        if acted:
            self.next_check[name] = now+self.cooldowns[name]
            return
        interval = self.intervals[name]
        # A coalesced check can run slightly early. Advance beyond its original
        # deadline too, so it cannot be selected again for the same period.
        completed = max(now, self.next_check[name])
        periods = math.floor((completed-self.epoch)/interval+1e-9)+1
        self.next_check[name] = self.epoch+periods*interval

    def deadline(self, names=None):
        return min(self.next_check[name] for name in
                   (self.next_check if names is None else names))
