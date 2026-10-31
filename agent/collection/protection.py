"""Read confirmed five-star keeper identities independently of stock quantities."""
from .blessing import SLOTS


def pending_reward(operation):
    """A realm submission can add items, but cannot consume or unlock keepers."""
    return (isinstance(operation, dict) and operation.get("kind") == "realm"
            and operation.get("realm") == "bless"
            and operation.get("status") == "submitted_unconfirmed"
            and operation.get("facts") is None)


def keeper_keys(snapshot, catalog):
    if snapshot.get("catalog_version") != catalog["version"]:
        raise ValueError("锁定记录的图鉴版本不一致")
    definitions = {item["name"]: item for item in catalog["sets"]
                   if item["rarity"] == 5 and item.get("collection_target", True)}

    def keys(rows):
        if not isinstance(rows, list):
            raise ValueError("已有锁定记录格式无效")
        result = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("已有锁定条目无效")
            name, slot, color = row.get("name"), row.get("slot"), row.get("color")
            if (not isinstance(name, str) or name not in definitions or slot not in SLOTS
                    or color not in definitions[name]["colors"]):
                raise ValueError("已有锁定的名称、部位或颜色无效")
            key = (name, slot, color)
            if key in result:
                raise ValueError("已有锁定记录重复")
            result.add(key)
        return result

    locks = snapshot.get("locks", {})
    if locks.get("complete") is True and snapshot["scan"].get("identity_complete"):
        maximum = [item for item in snapshot["items"]
                   if item["name"] in definitions and item["level"] == 10]
        if any(item.get("color") is None for item in maximum):
            raise ValueError("已有五星满级颜色尚未全部确认")
        owned = {(item["name"], item["slot"], item["color"]) for item in maximum}
        protected = keys([group for group in locks["groups"]
                          if group["name"] in definitions and group["level"] == 10
                          and group["locked_count"] > 0])
        if owned != protected:
            raise ValueError("已有五星满级颜色仍有未保护项")
        return protected

    progress = snapshot.get("pursuit")
    if isinstance(progress, dict) and progress.get("existing_colors_checked") is True:
        if progress.get("protection_valid", True) is not True:
            raise ValueError("上次收藏保护复核未通过")
        operation = progress.get("pending_operation")
        if operation is not None and not pending_reward(operation):
            raise ValueError("上次仍有未确认操作，需要重新核对")
        parts = progress.get("parts")
        if not isinstance(parts, list):
            raise ValueError("收集进度缺少部位记录")
        seen, rows = set(), []
        for part in parts:
            if not isinstance(part, dict):
                raise ValueError("收集部位记录无效")
            name, slot = part.get("name"), part.get("slot")
            if not isinstance(name, str) or name not in definitions or slot not in SLOTS:
                raise ValueError("收集部位未登记")
            if (name, slot) in seen:
                raise ValueError("收集部位重复")
            seen.add((name, slot))
            colors, missing = part.get("locked_colors"), part.get("missing_colors")
            if (not isinstance(colors, list) or not isinstance(missing, list)
                    or any(not isinstance(c, str) for c in colors + missing)
                    or len(set(colors + missing)) != len(colors + missing)
                    or set(colors + missing) != set(definitions[name]["colors"])):
                raise ValueError("收集颜色记录不完整")
            rows.extend({"name": name, "slot": slot, "color": color} for color in colors)
        if seen != {(name, slot) for name in definitions for slot in SLOTS}:
            raise ValueError("收集进度未覆盖三个部位")
        protected = keys(rows)
        if snapshot["scan"].get("identity_complete"):
            observed = {(item["name"], item["slot"], item["color"])
                        for item in snapshot["items"]
                        if item["name"] in definitions and item["level"] == 10}
            if observed != protected:
                raise ValueError("完整库存与已保护的五星颜色不一致")
        last = progress.get("latest_upgrade")
        if last is not None:
            if not isinstance(last, dict):
                raise ValueError("最近升级记录无效")
            if last.get("level") == 10 and (last.get("name"), last.get("slot"), last.get("color")) not in protected:
                raise ValueError("最近升级的满级颜色尚未保护")
        return protected

    # Only reward-only invalidation may carry this compact protection proof.
    if (snapshot.get("record", {}).get("source") == "realm_bless"
            and snapshot["scan"].get("workflow") == "inventory_invalidated"):
        proof = snapshot.get("protection")
        if isinstance(proof, dict) and proof.get("complete") is True:
            return keys(proof.get("keepers"))
    raise ValueError("没有完整的五星已有颜色保护记录")
