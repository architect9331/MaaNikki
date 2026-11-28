"""Region mapping and per-goal filter/priority helpers for collection planning."""
from __future__ import annotations

ALL_REGIONS: tuple[str, ...] = ("wishfield", "izhan", "xixiya")
DEFAULT_REGION_ORDER: tuple[str, ...] = ("xixiya", "izhan", "wishfield")

REGION_LABELS: dict[str, str] = {
    "wishfield": "心愿原野",
    "izhan": "伊赞之土",
    "xixiya": "西西亚",
}

BLESSING_SOURCE_REGION: dict[str, str] = {
    # 1.0 心愿原野
    "福鸣之井": "wishfield",
    "愿望梦境仓库": "wishfield",
    "涟漪之窖": "wishfield",
    "幽灵列车": "wishfield",
    "美妆工坊": "wishfield",
    "菲洛米亚女王行宫": "wishfield",
    "古铸剑遗址": "wishfield",
    "巨蛇遗迹": "wishfield",
    # 2.0 伊赞之土
    "母菇梦境": "izhan",
    "伊闪闪宝库": "izhan",
    "巨兽堡垒": "izhan",
    "甜霜梦屿": "izhan",
    "灵魂大潮": "izhan",
    "丰登庆典": "izhan",
    # 3.0 西西亚
    "报时鸟座钟": "xixiya",
}


def material_name_region(name: str | None) -> str | None:
    """Infer region ID from a realm material's Chinese name."""
    if not name:
        return None
    if "磐岩晶石" in name or "幻彩泡泡" in name:
        return "wishfield"
    if "大地晶珀" in name or "涡影泡泡" in name:
        return "izhan"
    if "创想绘片" in name or "弥思泡泡" in name:
        return "xixiya"
    return None


def material_region(meta: dict | None) -> str | None:
    """Infer region ID from a crafting_catalog material metadata entry."""
    if not isinstance(meta, dict):
        return None
    if meta.get("kind") not in ("monster_craft", "monster_glow"):
        return None
    return material_name_region(str(meta.get("name") or ""))


def sketch_region(sketch: dict, mat_defs: dict) -> str | None:
    """Infer region ID of a crafting sketch from its monster_craft crystal cost."""
    for mid in (sketch.get("costs") or {}):
        reg = material_region(mat_defs.get(str(mid)))
        if reg is not None:
            return reg
    return None


def curve_region(curve: dict, material_names: dict | None = None, mat_defs: dict | None = None) -> str | None:
    """Infer region ID of a clothing growth curve from its monster_glow bubble cost."""
    costs = curve.get("costs") or []
    for step in reversed(costs):
        if not isinstance(step, dict):
            continue
        for mid in step:
            key = str(mid)
            if mat_defs is not None:
                reg = material_region(mat_defs.get(key))
                if reg is not None:
                    return reg
            if material_names is not None:
                reg = material_name_region(str(material_names.get(key) or ""))
                if reg is not None:
                    return reg
    return None


def blessing_source_region(source: str | None) -> str | None:
    """Map a 5-star blessing realm source name to its region ID."""
    if not source:
        return None
    return BLESSING_SOURCE_REGION.get(str(source).strip())


def normalize_region_config(raw) -> dict:
    """Normalize saved per-goal region filter & priority settings."""
    valid = set(ALL_REGIONS)
    enabled: list[str] = []
    priority = "global"
    order: list[str] = []

    if isinstance(raw, dict):
        raw_enabled = raw.get("enabled")
        if isinstance(raw_enabled, list):
            for item in raw_enabled:
                if isinstance(item, str) and item in valid and item not in enabled:
                    enabled.append(item)
        if raw.get("priority") == "ordered":
            priority = "ordered"
        raw_order = raw.get("order")
        if isinstance(raw_order, list):
            for item in raw_order:
                if isinstance(item, str) and item in valid and item not in order:
                    order.append(item)

    if not enabled:
        enabled = list(ALL_REGIONS)
    for region in DEFAULT_REGION_ORDER:
        if region not in order:
            order.append(region)

    return {
        "enabled": enabled,
        "priority": priority,
        "order": order,
    }


def is_all_regions_enabled(cfg: dict) -> bool:
    return len(set(cfg.get("enabled") or ())) >= len(ALL_REGIONS)


def active_region_order(cfg: dict) -> list[str] | None:
    """Return ordered region list when region priority mode is enabled, else None."""
    if cfg.get("priority") != "ordered":
        return None
    return list(cfg.get("order") or DEFAULT_REGION_ORDER)


def region_rank(region: str | None, order: list[str] | None) -> int:
    """Return 0-based priority rank for ``region``, or 0 when priority mode is off."""
    if not order:
        return 0
    if region in order:
        return order.index(region)
    return len(order)
