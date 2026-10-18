"""Coverage and conditional budgets for physically owned Eurekas."""
from __future__ import annotations

from itertools import combinations
import math

from .policy import normalize_scope


SLOTS = ("头部", "手部", "脚部")


def summarize_scan(snapshot, traversed, reason):
    """Keep physical traversal, identity/level and colour completeness separate."""
    scan = snapshot["scan"]
    total = sum(item["count"] for item in snapshot["items"])
    pending = sum(item["count"] for item in snapshot["items"] if not item.get("color"))
    visited = total + len(snapshot["unresolved"])
    excluded = sum(item["count"] for item in scan.get("excluded_owned", []))
    capacity = scan.get("reported_capacity")
    expected = capacity
    includes_excluded = False if capacity == visited else None
    if scan.get("view") == "normal_inventory" and capacity is not None:
        expected, includes_excluded = capacity - excluded, bool(excluded)
    elif (scan.get("view") == "decomposition" and excluded and capacity is not None
            and capacity - excluded == visited):
        expected, includes_excluded = capacity - excluded, True
    coverage = bool(traversed and expected == visited)
    identity = bool(coverage and not snapshot["unresolved"])
    colors = bool(identity and not pending)
    scan.update(complete=colors, traversed=bool(traversed), coverage_complete=coverage,
                identity_complete=identity, color_complete=colors, recorded_copies=total,
                visited_copies=visited, color_unresolved_copies=pending)
    scan.update(expected_collectible_capacity=expected, excluded_copies=excluded,
                capacity_includes_excluded=includes_excluded,
                total_owned_copies=visited + excluded if coverage else None)
    details = []
    if not coverage:
        details.append("库存数量或遍历范围待核对")
    if snapshot["unresolved"]:
        details.append(f"{len(snapshot['unresolved'])} 个部件或等级待确认")
    if pending:
        details.append(f"{pending} 个颜色待确认，已确认部件及等级计入一级目标")
    scan["reason"] = "整份列表、部件、等级及颜色均已核对" if colors else "；".join([reason, *details])
    for slot in SLOTS:
        scan["slots"][slot] = {"complete": coverage, "pages": len(scan.get("pages", [])),
                               "reason": ("普通仓库按部位核对，" if scan.get("view") == "normal_inventory"
                                          else "整屏混合部位扫描，") + scan["reason"]}


def remaining_cost(catalog, rarity, level, xp=0):
    costs = catalog["upgrade_costs"][str(rarity)]
    return (sum(costs["bling"][level:]), max(0, sum(costs["exp"][level:]) - xp))


def subsets(probabilities):
    for length in range(1, len(probabilities) + 1):
        for values in combinations(probabilities, length):
            yield (1 if length % 2 else -1), sum(values)


def expected_attempts(probabilities):
    # Unequal-probability coupon collection; not missing_count / one probability.
    return sum(sign / probability for sign, probability in subsets(probabilities))


def expected_usage(probabilities, start, count):
    """Sum survival over a stack without expanding thousands of physical copies."""
    total = 0.0
    for sign, probability in subsets(probabilities):
        if probability >= 1:
            total += sign if start == 0 else 0
        else:
            total += sign * (1 - probability) ** start * (1 - (1 - probability) ** count) / probability
    return max(0.0, min(float(count), total))


def budget_for_part(catalog, definition, items, goal, rainbow):
    rarity = definition["rarity"]
    maximum = catalog["upgrade_costs"][str(rarity)]["max_level"]
    owned = {item["color"] for item in items if item.get("color")}
    maxed = any(item["level"] == maximum for item in items)
    missing = [color for color in definition["colors"] if color not in owned]
    probabilities = {color: ((rainbow if color == "彩" else (1 - rainbow) / 4)
                              if rarity == 5 else 1 / len(definition["colors"]))
                     for color in definition["colors"]}
    candidates = [item for item in items if item["level"] < maximum]
    # Retain an actual original-colour copy. Level 3 does not require every copy
    # to be maxed; do not spend its sole low-level copy to roll another colour.
    if goal == 3 and rarity != 3:
        # An unconfirmed colour may be the sole original-colour copy. Keep it
        # until its colour is known rather than budgeting it as a spare roll.
        candidates = [item for item in candidates if item.get("color")]
        for color in owned:
            if not any(item["level"] == maximum and item.get("color") == color for item in items):
                matching = [item for item in candidates if item.get("color") == color]
                if matching:
                    retained = min(matching, key=lambda item: item["level"])
                    candidates = candidates.copy()
                    index = candidates.index(retained)
                    if retained["count"] > 1:
                        candidates[index] = {**retained, "count": retained["count"] - 1}
                    else:
                        candidates.pop(index)
    target = []
    if goal == 1 or goal == 2 and rarity != 5:
        target = [1.0] if not maxed else []
    elif goal == 2:
        target = [rainbow] if "彩" not in owned else []
    elif missing:
        target = [probabilities[color] for color in missing]
    elif not maxed:
        target = [1.0]
    candidates.sort(key=lambda item: item["level"], reverse=True)
    expected = expected_attempts(target) if target else 0.0
    fresh_bling, fresh_exp = remaining_cost(catalog, rarity, 0)
    # The kth existing copy is used only if the preceding rolls did not finish.
    bling, exp = expected * fresh_bling, expected * fresh_exp
    used = 0.0
    index = 0
    for item in candidates:
        cost_bling, cost_exp = remaining_cost(catalog, rarity, item["level"], item.get("xp", 0))
        chance = expected_usage(target, index, item["count"]) if target else 0.0
        used += chance
        bling -= chance * (fresh_bling - cost_bling)
        exp -= chance * (fresh_exp - cost_exp)
        index += item["count"]
    return {"maxed": maxed, "owned_colors": sorted(owned), "missing_colors": missing,
            "color_unresolved_copies": sum(item["count"] for item in items if not item.get("color")),
            "rolls": expected, "bling": max(0.0, bling), "exp": max(0.0, exp),
            "new_copies": max(0.0, expected - used), "available_copies": index,
            "target_probabilities": target}


def pool_energy(parts, copies_per_claim, stopped=lambda: False):
    """Joint stopping expectation, using Poissonized independent drop streams.

    One claim's modeled five-star drops are IID and uniform among six set/slot
    identities. Poisson time has the same expected stopping count as IID draws.
    Integrate the probability that any part is still incomplete. This avoids
    double-counting six parts obtained from the same reward pool.
    """
    active = [part for part in parts if part["target_probabilities"]]
    if not active:
        return 0.0
    slowest = min(min(part["target_probabilities"]) / 6 for part in active)
    end = -math.log(1e-9 / max(1, sum(len(p["target_probabilities"]) for p in active))) / slowest
    steps = 1024
    step = end / steps

    def unfinished(draws):
        completed = 1.0
        for part in active:
            n = part["available_copies"]
            absent = sum(sign * (1 - p) ** n * math.exp(-draws * p / 6)
                         for sign, p in subsets(part["target_probabilities"]))
            completed *= max(0.0, min(1.0, 1 - absent))
        return 1 - completed

    # Simpson quadrature; the omitted exponential tail is below 1e-9 in mass.
    area = unfinished(0) + unfinished(end)
    for index in range(1, steps):
        if index % 64 == 0 and stopped():
            raise RuntimeError("收集规划已停止")
        area += (4 if index % 2 else 2) * unfinished(index * step)
    return area * step / 3 / copies_per_claim * 40


def validate_estimate(rainbow, copies_per_claim):
    if not math.isfinite(rainbow) or not 0 < rainbow < 1:
        raise ValueError("彩色概率必须在 0% 与 100% 之间")
    if not math.isfinite(copies_per_claim) or not 0 < copies_per_claim <= 100:
        raise ValueError("每 40 活跃能量的五星数量必须大于 0 且不超过 100")


def analyze(catalog, snapshot, rainbow, copies_per_claim, stopped=lambda: False):
    validate_estimate(rainbow, copies_per_claim)
    normalize_scope(snapshot, catalog)
    definitions = [item for item in catalog["sets"] if item.get("collection_target", True)]
    indexed = {}
    for item in snapshot["items"]:
        if item.get("name") and item.get("slot") in SLOTS and item.get("level") is not None:
            indexed.setdefault((item["name"], item["slot"]), []).append(item)
    result = {"catalog_version": catalog["version"], "scan": snapshot["scan"],
              "assumptions": {"iridescent": rainbow, "five_star_per_40_energy": copies_per_claim,
                              "pool_selection": "同副本的两套 × 三部位等概率；非彩颜色均分剩余概率",
                              "strategy": "保留已拥有颜色；不回收副本，经验需求不折算成额外活跃能量"},
              "goals": []}
    for goal in (1, 2, 3):
        rows, pools = [], {}
        for definition in definitions:
            for slot in SLOTS:
                items = indexed.get((definition["name"], slot), [])
                part = budget_for_part(catalog, definition, items, goal, rainbow)
                confirmed = goal == 1 and part["maxed"] or goal == 2 and (
                    "彩" in part["owned_colors"] if definition["rarity"] == 5 else part["maxed"])
                if goal == 3:
                    confirmed = not part["missing_colors"] and part["maxed"]
                row = {"name": definition["name"], "slot": slot, "rarity": definition["rarity"],
                       "source": definition["source"], "confirmed": bool(confirmed), **part}
                rows.append(row)
                if definition["rarity"] == 5:
                    pools.setdefault(definition["source"], []).append(part)
        if any(len(parts) != 6 for parts in pools.values()):
            raise ValueError("五星副本图鉴需要完整的两套、六部件数据")
        energy = [{"source": source, "energy": pool_energy(parts, copies_per_claim, stopped)}
                  for source, parts in pools.items()]
        result["goals"].append({"goal": goal, "confirmed_parts": sum(row["confirmed"] for row in rows),
                                "complete": bool(snapshot["scan"].get(
                                    "identity_complete" if goal == 1 else "color_complete",
                                    snapshot["scan"].get("complete", False))),
                                "total_parts": len(rows),
                                "confirmed_colors": sum(len(row["owned_colors"]) for row in rows),
                                "total_colors": sum(len(d["colors"]) * 3 for d in definitions),
                                "bling": sum(row["bling"] for row in rows),
                                "exp": sum(row["exp"] for row in rows), "pools": energy, "parts": rows})
    return result
