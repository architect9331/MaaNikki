"""Ephemeral coordination scoped to the actual client-submitted task batch."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import os
import time


def run_plan():
    try:
        value = json.loads(os.environ.get("PI_MAANIKKI_RUN_PLAN", "{}"))
        entries = value.get("entries")
        if (value.get("schema_version") != 1 or not isinstance(value.get("id"), str)
                or not 1 <= len(value["id"]) <= 100 or not isinstance(entries, list)
                or not 1 <= len(entries) <= 200 or any(not isinstance(v, str) for v in entries)):
            return None
        return value
    except (TypeError, ValueError, AttributeError):
        return None


PLAN = run_plan()


def next_page(entry):
    """Use only the actual, unambiguous submitted order; never saved settings."""
    if not PLAN or PLAN["entries"].count(entry) != 1:
        return "main"
    entries = PLAN["entries"]
    index = entries.index(entry)+1
    if index < len(entries) and entries[index] in (
            "MaaNikki_Zhaoxi", "MaaNikki_Xinghai", "MaaNikki_RealmChallenge"):
        return "calendar"
    return "main"


@dataclass
class PendingReview:
    source_task_id: int
    score: int
    energy_remaining: int | None
    claim_overrides: dict
    events: list
    scans: list
    created: float
    realm_task_id: int | None = None


_pending: dict[tuple[str, str], PendingReview] = {}


def key(runtime):
    return (PLAN["id"], runtime.controller.uuid) if PLAN else None


def clear(runtime):
    _pending.pop(key(runtime), None)


def can_review():
    if PLAN is None:
        return False
    entries = PLAN["entries"]
    # Ambiguous duplicate stages are left independent, rather than guessing
    # which daily settings and realm execution should be joined.
    return (entries.count("MaaNikki_Zhaoxi") == entries.count("MaaNikki_RealmChallenge") == 1
            and entries.index("MaaNikki_Zhaoxi") < entries.index("MaaNikki_RealmChallenge"))


def defer(runtime, task_id, score, energy_card):
    if not can_review() or runtime.stopped:
        return False
    claim_node = "MaaNikki_ClaimZhaoxiRewards"
    from .runtime import parameters
    claim = runtime.context.get_node_data(claim_node) or {}
    _pending[key(runtime)] = PendingReview(
        source_task_id=task_id, score=score, energy_remaining=energy_card.remaining,
        claim_overrides={claim_node: {"enabled": claim.get("enabled", True),
                                     "custom_action_param": deepcopy(parameters(runtime.context, claim_node))}},
        events=deepcopy(runtime.events), scans=deepcopy(runtime.scans), created=time.monotonic())
    return True


def begin_realm(runtime, task_id):
    pending = _pending.get(key(runtime))
    if pending:
        if pending.source_task_id >= task_id or time.monotonic()-pending.created >= 3600:
            clear(runtime)
            raise ValueError("朝夕待复核记录已过期或任务顺序不一致，请重新运行本轮任务")
        pending.realm_task_id = task_id
    else:
        clear(runtime)


def take(runtime, task_id):
    pending = _pending.pop(key(runtime), None)
    if pending and (pending.realm_task_id != task_id or time.monotonic()-pending.created >= 3600):
        raise ValueError("待复核记录与本次幻境不匹配或已过期，未继续领奖")
    return pending
