"""Strict dominance checks for standalone clothing pieces under battle rules."""
from __future__ import annotations

_CACHE: dict[int, dict] = {}


def _build_index(scoring: dict) -> dict:
    key = id(scoring)
    cached = _CACHE.get(key)
    if cached is not None and cached.get("_ref") is scoring:
        return cached

    items = scoring.get("items") or {}
    clothes: dict[str, dict] = {}
    by_type: dict[int, list[str]] = {}
    for raw in items.values():
        if not isinstance(raw, dict):
            continue
        item_type = int(raw.get("type", 0))
        quality = int(raw.get("quality", 0))
        name = str(raw.get("name", ""))
        if item_type >= 200 or quality < 2 or name.isdigit():
            continue
        sid = str(raw["id"])
        clothes[sid] = raw
        by_type.setdefault(item_type, []).append(sid)

    suit_ids: set[str] = set()
    for suit in scoring.get("suits") or []:
        for iid in suit.get("items") or []:
            suit_ids.add(str(iid))

    conflicts: dict[str, set[str]] = {}
    for sid, raw in clothes.items():
        for eid in raw.get("excludes") or []:
            esid = str(eid)
            if esid in clothes and esid != sid:
                conflicts.setdefault(sid, set()).add(esid)
                conflicts.setdefault(esid, set()).add(sid)
        for et in raw.get("excludesTypes") or []:
            for esid in by_type.get(int(et), ()):
                if esid != sid:
                    conflicts.setdefault(sid, set()).add(esid)
                    conflicts.setdefault(esid, set()).add(sid)

    index = {
        "_ref": scoring,
        "clothes": clothes,
        "by_type": by_type,
        "suit_ids": suit_ids,
        "conflicts": conflicts,
    }
    if len(_CACHE) > 8:
        _CACHE.clear()
    _CACHE[key] = index
    return index


def _dominates(a: dict, b: dict, a_sid: str, b_sid: str, conflicts: dict[str, set[str]]) -> bool:
    if a_sid == b_sid or int(a.get("type", -1)) != int(b.get("type", -2)):
        return False
    if int(a.get("major", -1)) != int(b.get("major", -2)):
        return False
    a_props = a.get("props") or []
    b_props = b.get("props") or []
    if len(a_props) < 5 or len(b_props) < 5:
        return False
    if any(a_props[i] < b_props[i] for i in range(5)):
        return False
    a_tags = set(a.get("tags") or [])
    b_tags = set(b.get("tags") or [])
    if not b_tags.issubset(a_tags):
        return False
    a_conf = conflicts.get(a_sid, set())
    b_conf = conflicts.get(b_sid, set())
    if not a_conf.issubset(b_conf):
        return False
    return (
        any(a_props[i] > b_props[i] for i in range(5))
        or len(a_tags) > len(b_tags)
        or len(a_conf) < len(b_conf)
    )


def strictly_dominated_standalone_ids(
    scoring: dict,
    candidate_ids: list[str] | set[str],
    available_ids: set[str],
) -> set[str]:
    """Return candidate score IDs that are non-suit pieces strictly dominated by ``available_ids``."""
    index = _build_index(scoring)
    clothes = index["clothes"]
    by_type = index["by_type"]
    suit_ids = index["suit_ids"]
    conflicts = index["conflicts"]
    avail_by_type: dict[int, list[str]] = {}
    for sid in available_ids:
        item = clothes.get(str(sid))
        if item is not None:
            avail_by_type.setdefault(int(item["type"]), []).append(str(sid))

    dominated: set[str] = set()
    for raw_sid in candidate_ids:
        sid = str(raw_sid)
        if sid in suit_ids:
            continue
        item = clothes.get(sid)
        if item is None:
            continue
        for a_sid in avail_by_type.get(int(item["type"]), ()):
            if a_sid in by_type.get(int(item["type"]), ()) and _dominates(clothes[a_sid], item, a_sid, sid, conflicts):
                dominated.add(sid)
                break
    return dominated
