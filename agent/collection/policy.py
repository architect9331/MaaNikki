"""Catalogue targets and observed protected inventory share capacity accounting."""
from __future__ import annotations


def protected_inventory(item):
    if not (isinstance(item, dict) and item.get("protected_inventory") is True
            and type(item.get("rarity")) is int and item["rarity"] == 3):
        return False
    proof = item.get("inventory_evidence")
    return (isinstance(proof, dict) and proof.get("basis") == "three_star_locked_card"
            and type(proof.get("rarity_score")) in (int, float) and .94 <= proof["rarity_score"] <= 1)


def definition_for(catalog, name, *, observed=None):
    definition = next((item for item in catalog["sets"] if item["name"] == name), None)
    if definition is None:
        if (protected_inventory(observed) and observed.get("name") == name
                and isinstance(name, str) and 1 <= len(name) <= 64
                and not any(ord(char) < 32 for char in name)):
            return {"name": name, "rarity": 3, "colors": list("蓝绿紫红粉黄白彩"),
                    "collection_target": False, "lock_mutable": False, "decomposable": False}
        raise ValueError(f"未登记的祝福闪光：{name}")
    return definition


def capabilities(catalog, name, *, observed=None):
    definition = definition_for(catalog, name, observed=observed)
    return {key: definition.get(key, True)
            for key in ("collection_target", "lock_mutable", "decomposable")}


def reconcile_exclusions(snapshot, catalog, *, traversed):
    """Retire capacity-gap guesses; every owned copy must be observed."""
    scan = snapshot["scan"]
    scan["excluded_owned"] = []
    scan["excluded_owned_source"] = "observed_normal_inventory" if traversed else "unknown"


def normalize_scope(snapshot, catalog):
    """Migrate observed legacy specials into ordinary inventory, retaining flags."""
    excluded = snapshot.setdefault("excluded_items", [])
    if not isinstance(excluded, list) or len(excluded) > 10000:
        raise ValueError("特殊闪光库存必须是列表")
    if any(not isinstance(item, dict) or not isinstance(item.get("name"), str) for item in excluded):
        raise ValueError("特殊闪光库存条目必须包含名称")
    if len(snapshot["items"]) + len(excluded) > 10000:
        raise ValueError("库存条目超过上限")
    for item in excluded:
        flags = capabilities(catalog, item["name"])
        if flags["collection_target"]:
            raise ValueError("不能把普通追全目标当作特殊闪光排除")
        item.update(flags)
    snapshot["items"].extend(excluded)
    snapshot["excluded_items"] = []
    for item in snapshot["items"]:
        item.update(capabilities(catalog, item["name"], observed=item))
    snapshot.setdefault("locks", {"complete": False, "groups": [], "reason": "本次未读取锁定状态"})
    return snapshot
