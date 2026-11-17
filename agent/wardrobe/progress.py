"""Acquisition-order checkpoints; existing clothes keep their relative order."""
from __future__ import annotations

from copy import deepcopy


# This model head describes ability items, not a separate S-switchable tab.
MODEL_ONLY_PARTS = {"413"}


def ordered_prefix(state, layout):
    """Reuse only the contiguous prefix actually read from the list's top.

    Old snapshots stored pixel positions. Convert those without inventing
    missing rows/cells; an interrupted page-gap report resumes before its gap.
    """
    raw_items = state.get("items", [])
    if not raw_items:
        return []
    if any(str(item.get("source") or "").startswith("rejuvenate") for item in raw_items):
        return []
    items = [
        item for item in raw_items
        if not (
            item.get("source") == "compendium_suit_inferred"
            and type(item.get("slot")) is not int
            and not (isinstance(item.get("position"), (list, tuple)) and len(item["position"]) >= 2)
        )
    ]
    if not items:
        return []
    if all(type(item.get("slot")) is int for item in items):
        ordered = sorted(items, key=lambda item: item["slot"])
        prefix = []
        for item in ordered:
            if item["slot"] != len(prefix):
                break
            prefix.append(deepcopy(item))
        return prefix
    if not all(isinstance(item.get("position"), (list, tuple)) and len(item["position"]) >= 2 for item in items):
        return []
    ordered = sorted(items, key=lambda item: (item["position"][1], item["position"][0]))
    rows = []
    for item in ordered:
        y = item["position"][1]
        if not rows or abs(y - rows[-1][0]) >= 4:
            rows.append((y, []))
        rows[-1][1].append(item)
    prefix, previous = [], None
    for top, row in rows:
        if previous is not None and abs(top - previous - layout["row_pitch"]) > 4:
            break
        for item in sorted(row, key=lambda value: value["position"][0]):
            column = round((item["position"][0] - layout["first_x"]) / layout["column_pitch"])
            if column != len(prefix) % layout["columns"]:
                return prefix
            prefix.append(deepcopy(item))
        if len(row) != layout["columns"]:
            break
        previous = top
    return prefix
