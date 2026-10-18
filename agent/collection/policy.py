"""Shared, explicit catalogue capabilities; activity origin alone grants none."""
from __future__ import annotations


def definition_for(catalog, name):
    definition = next((item for item in catalog["sets"] if item["name"] == name), None)
    if definition is None:
        raise ValueError(f"未登记的祝福闪光：{name}")
    return definition


def capabilities(catalog, name):
    definition = definition_for(catalog, name)
    return {key: definition.get(key, True)
            for key in ("collection_target", "lock_mutable", "decomposable")}


def reconcile_exclusions(snapshot, catalog, *, traversed):
    """Prefer observed specials; otherwise allow the known 0/3-copy gap only.

    Capacity inference never fabricates per-slot inventory. It assumes the only
    special set is Moon Rabbit, awarded as three pieces, and records its basis.
    """
    scan = snapshot["scan"]
    scan["excluded_owned"] = []
    scan["excluded_owned_source"] = "unknown"
    if scan.get("excluded_observation_complete", False):
        owned = {}
        for item in snapshot["excluded_items"]:
            owned[item["name"]] = owned.get(item["name"], 0) + item["count"]
        for name, count in owned.items():
            definition = definition_for(catalog, name)
            if not 0 <= count <= definition["maximum_owned"]:
                raise ValueError(f"{name}实测数量超过图鉴上限，请重新扫描")
        scan["excluded_owned"] = [
            {"name": name, "count": count, "basis": "observed_normal_inventory", "observed": True,
             **capabilities(catalog, name)} for name, count in sorted(owned.items())]
        scan["excluded_owned_source"] = "observed_normal_inventory"
        return
    special = [item for item in catalog["sets"] if not item.get("collection_target", True)
               and not item.get("decomposable", True)]
    if (not traversed or scan.get("view") != "decomposition"
            or scan.get("reported_capacity") is None or len(special) != 1
            or special[0]["name"] != "月桂兔"):
        return
    visited = sum(item["count"] for item in snapshot["items"]) + len(snapshot["unresolved"])
    gap = scan["reported_capacity"] - visited
    if gap in (0, special[0]["maximum_owned"]):
        scan["excluded_owned"] = [{"name": special[0]["name"], "count": gap,
                                   "basis": "capacity_difference", "observed": False,
                                   **capabilities(catalog, special[0]["name"])}]
        scan["excluded_owned_source"] = "capacity_difference"


def normalize_scope(snapshot, catalog):
    """Keep special ownership separate without pretending it was scanned."""
    excluded = snapshot.setdefault("excluded_items", [])
    if not isinstance(excluded, list) or len(excluded) > 10000:
        raise ValueError("特殊闪光库存必须是列表")
    if any(not isinstance(item, dict) or not isinstance(item.get("name"), str) for item in excluded):
        raise ValueError("特殊闪光库存条目必须包含名称")
    regular = []
    for item in snapshot["items"]:
        flags = capabilities(catalog, item["name"])
        item.update(flags)
        if flags["collection_target"]:
            regular.append(item)
        else:
            item.setdefault("observed", True)
            excluded.append(item)
    snapshot["items"] = regular
    for item in excluded:
        flags = capabilities(catalog, item["name"])
        if flags["collection_target"]:
            raise ValueError("不能把普通追全目标当作特殊闪光排除")
        item.update(flags)
    snapshot.setdefault("locks", {"complete": False, "groups": [], "reason": "本次未读取锁定状态"})
    return snapshot
