"""Ephemeral coordination scoped to the actual client-submitted task batch."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
import os
import time

from .planner import Card


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
    uid: str
    realm_task_id: int | None = None
    upgrade_card: Card | None = None
    realm_kind: str | None = None
    collection_mode: bool = False
    failure_evidence: list = field(default_factory=list)


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


def planned_realm():
    """Saved realm options only guide scheduling within an actual later stage."""
    if not can_review():
        return None
    from .settings import realm_settings
    try:
        preferred, overrides = realm_settings()
    except (OSError, ValueError, KeyError, TypeError):
        # Missing optional scheduling hints must not block other daily wishes.
        return None
    collection = overrides.get("MaaNikki_RealmChallenge", {}).get(
        "custom_action_param", {}).get("five_star_collection") is True
    names = overrides.get("MaaNikki_RealmEnergy", {}).get("next", [])
    return {"kind": "bless" if collection else preferred,
            "collection": collection or "MaaNikki_BlessingPursuit" in names}


def defer(runtime, task_id, score, energy_card=None, *, upgrade_card=None):
    if not can_review() or runtime.stopped:
        return False
    claim_node = "MaaNikki_ClaimZhaoxiRewards"
    from .runtime import parameters
    claim = runtime.context.get_node_data(claim_node) or {}
    _pending[key(runtime)] = PendingReview(
        source_task_id=task_id, score=score, energy_remaining=energy_card.remaining if energy_card else None,
        claim_overrides={claim_node: {"enabled": claim.get("enabled", True),
                                     "custom_action_param": deepcopy(parameters(runtime.context, claim_node))}},
        events=deepcopy(runtime.events), scans=deepcopy(runtime.scans), created=time.monotonic(),
        upgrade_card=deepcopy(upgrade_card), uid=runtime.account_uid(),
        failure_evidence=deepcopy(list(runtime.evidence.records)))
    return True


def begin_realm(runtime, task_id, *, kind=None, collection=False):
    pending = _pending.get(key(runtime))
    if pending:
        if pending.uid != runtime.account_uid():
            clear(runtime)
            raise ValueError("朝夕待复核记录属于另一 UID，请重新运行本轮任务")
        if pending.source_task_id >= task_id or time.monotonic()-pending.created >= 3600:
            clear(runtime)
            raise ValueError("朝夕待复核记录已过期或任务顺序不一致，请重新运行本轮任务")
        pending.realm_task_id = task_id
        pending.realm_kind, pending.collection_mode = kind, collection
    else:
        clear(runtime)


def take(runtime, task_id):
    pending = _pending.pop(key(runtime), None)
    if pending and pending.uid != runtime.account_uid():
        raise ValueError("待复核记录属于另一 UID，本次未继续领奖")
    if pending and (pending.realm_task_id != task_id or time.monotonic()-pending.created >= 3600):
        raise ValueError("待复核记录与本次幻境不匹配或已过期，未继续领奖")
    return pending
