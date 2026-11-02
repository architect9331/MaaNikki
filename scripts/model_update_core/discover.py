"""Compare a decoded game item table with the deployed labels; list every gap."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
import re

from .common import WARDROBE, log, read_json, write_json


ICON = re.compile(r"T_Icon_mball_(S\d+)_(head|hand|foot)_(\d+)")
SLOTS = {"head": "头部", "hand": "手部", "foot": "脚部"}


def localized(run):
    path = run.config.get("localized_names")
    if not path:
        return {}
    return read_json(run.path(path))["tables"].get("TbItem", {})


def icon_package(reference):
    if not reference:
        return None
    path = reference.split("'", 1)[-1].rstrip("'").rsplit(".", 1)[0]
    return "X6Game/Content/" + path[6:] + ".uasset" if path.startswith("/Game/") else None


def item_records(run):
    source = read_json(run.path(run.config["item_table"]))
    names = localized(run)
    records = []
    for row in source["records"]:
        records.append({"item_id": row["field_1"], "major_type": row["major_type"], "part_id": row["minor_type"],
                        "name": (names.get(row["field_2"]) or {}).get("text") or row.get("field_3") or "",
                        "quality": row["quality"], "icon": row.get("field_10"), "release_date": row.get("release_date"),
                        "version_area": row.get("version_area")})
    return source.get("source", {}), records


def wardrobe_diff(run, records):
    directory = run.root / WARDROBE
    labels = read_json(directory / "labels.json")
    coverage = read_json(directory / "coverage.json")
    known = {item: (int(part), row) for part, rows in labels.items() for row in rows for item in row["item_ids"]}
    excluded = {row["item_id"]: row for row in coverage["excluded_missing_items"]}
    roster = {row["item_id"]: row for row in records if row["major_type"] == 2}
    added = [row for item, row in sorted(roster.items()) if item not in known and item not in excluded]
    retrying = [row for item, row in sorted(roster.items()) if item in excluded]
    removed = sorted(set(known) - set(roster))
    moved, renamed, requalified = [], [], []
    for item, (part, label) in sorted(known.items()):
        row = roster.get(item)
        if row is None:
            continue
        if row["part_id"] != part:
            moved.append({"item_id": item, "from": part, "to": row["part_id"]})
        if row["name"] and row["name"] not in label["names"]:
            renamed.append({"item_id": item, "labels": label["names"], "table": row["name"]})
        if row["quality"] not in label["qualities"]:
            requalified.append({"item_id": item, "labels": label["qualities"], "table": row["quality"]})
    new_parts = sorted({row["part_id"] for row in added} - {int(part) for part in labels})
    return {"roster_items": len(roster), "deployed_classes": sum(map(len, labels.values())),
            "deployed_items": len(known), "added": added, "previously_excluded": retrying,
            "removed_item_ids": removed, "moved": moved, "renamed": renamed, "requalified": requalified,
            "new_parts": new_parts, "added_by_part": dict(Counter(str(row["part_id"]) for row in added))}


def blessing_diff(run, records):
    metadata = read_json(run.root / "resource/model/blessing_identity.json")
    deployed = {(row["name"], row["slot"], row["color"]) for row in metadata["entries"]}
    icon_map = read_json(run.path(run.section("blessing")["icon_map"]))
    mapped = {(row["series"], row["slot"], row["variant"]): row for row in icon_map["records"]}
    deployed_icons = {key for key, row in mapped.items() if (row["name"], row["slot"], row["color"]) in deployed}
    table = {}
    for row in records:
        match = ICON.search(row["icon"] or "")
        if match:
            table[(match[1], SLOTS[match[2]])] = row
    files = {}
    icons = run.path(run.section("blessing")["icons"])
    if icons.is_dir():
        for path in icons.glob("*.png"):
            match = ICON.fullmatch(path.stem.removesuffix("_png"))
            if match:
                files[(match[1], SLOTS[match[2]], int(match[3]))] = path
    # Variant numbers come from exported files, since the table references only
    # the original colour (variant 1) of each part.
    new_icons = sorted(set(files) - deployed_icons)
    known_groups = {key[:2] for key in deployed_icons}
    new_groups = sorted(set(table) - known_groups)
    without_files = [key for key in new_groups if not any(icon[:2] == key for icon in files)]
    removed = sorted({key[:2] for key in deployed_icons} - set(table))
    return {"deployed_entries": len(metadata["entries"]), "deployed_groups": len(metadata["groups"]),
            "table_groups": len(table), "new_groups": [
                {"series": series, "slot": slot, "item_id": table[(series, slot)]["item_id"],
                 "name": table[(series, slot)]["name"], "rarity": table[(series, slot)]["quality"]}
                for series, slot in new_groups],
            "new_icons": [{"series": s, "slot": slot, "variant": v, "file": str(files[(s, slot, v)])}
                          for s, slot, v in new_icons],
            "groups_without_icon_files": [{"series": s, "slot": slot} for s, slot in without_files],
            "groups_absent_from_table": [{"series": s, "slot": slot} for s, slot in removed]}


def discover(run):
    source, records = item_records(run)
    result = {"item_table": source, "wardrobe": wardrobe_diff(run, records), "blessing": blessing_diff(run, records)}
    write_json(run.dir / "diff.json", result)
    wardrobe, blessing = result["wardrobe"], result["blessing"]
    summary = {"wardrobe_added": len(wardrobe["added"]), "wardrobe_previously_excluded": len(wardrobe["previously_excluded"]),
               "wardrobe_removed": len(wardrobe["removed_item_ids"]), "wardrobe_renamed": len(wardrobe["renamed"]),
               "wardrobe_moved": len(wardrobe["moved"]), "wardrobe_new_parts": wardrobe["new_parts"],
               "blessing_new_groups": len(blessing["new_groups"]), "blessing_new_icons": len(blessing["new_icons"]),
               "blessing_groups_without_icons": len(blessing["groups_without_icon_files"])}
    run.mark("discover", **summary)
    log("discover", **summary, report=str(run.dir / "diff.json"))
    return result


def blessing_label_template(run, diff):
    """Labels come from the game's own colour tables; anything they omit goes to a person."""
    path = run.dir / "blessing-labels.json"
    if path.exists():
        return read_json(path)
    configured = run.section("blessing").get("config_labels")
    table = read_json(run.path(configured))["icons"] if configured else {}
    groups = {(row["series"], row["slot"]): row for row in diff["blessing"]["new_groups"]}
    rows = []
    for icon in diff["blessing"]["new_icons"]:
        group = groups.get((icon["series"], icon["slot"]), {})
        known = table.get(f'{icon["series"]}|{icon["slot"]}|{icon["variant"]}', {})
        rows.append({**icon, "name": known.get("name") or group.get("name") or None,
                     "rarity": known.get("rarity") or group.get("rarity"), "color": known.get("color"),
                     "original": known["original"] if "original" in known else icon["variant"] == 1,
                     "item_id": known.get("item_id") or group.get("item_id"), "source": None,
                     "label_origin": "game_config" if known.get("color") else None})
    complete = bool(rows) and all(row["label_origin"] and row["name"] and row["rarity"] for row in rows)
    labels = {"instructions": "Rows without label_origin need name, rarity and color filled by hand; every colour must "
                              "exist on all three slots of a set. Check blessing-review.png, then set confirmed.",
              "confirmed": complete, "confirmed_by": "game config tables" if complete else None, "icons": rows}
    write_json(path, labels)
    return labels


def blessing_contact_sheet(run, labels):
    if not labels["icons"]:
        return None
    from PIL import Image, ImageDraw, ImageFont
    columns, cell = 8, (150, 176)
    rows = (len(labels["icons"]) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * cell[0], rows * cell[1]), "#262636")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 13)
    except OSError:
        font = ImageFont.load_default()
    for index, row in enumerate(labels["icons"]):
        x, y = index % columns * cell[0], index // columns * cell[1]
        if Path(row["file"]).is_file():
            icon = Image.open(row["file"]).convert("RGBA").resize((140, 140))
            sheet.paste(icon, (x + 5, y + 2), icon)
        draw.text((x + 5, y + 144), f'{row["series"]} {row["slot"]} #{row["variant"]}', font=font, fill="white")
        draw.text((x + 5, y + 159), f'{row["name"] or "?"} {row["color"] or "?"}', font=font, fill="#adc4d9")
    path = run.dir / "blessing-review.png"
    sheet.save(path)
    return path


def blessing_gaps(run, labels):
    gaps = []
    for field in labels["icons"]:
        where = f'{field["series"]} {field["slot"]} #{field["variant"]}'
        if not Path(field["file"]).is_file():
            gaps.append({"model": "blessing", "kind": "icon_file_missing", "where": where})
        for key in ("name", "rarity", "color"):
            if field.get(key) in (None, ""):
                gaps.append({"model": "blessing", "kind": f"label_missing_{key}", "where": where})
    sets = {}
    for field in labels["icons"]:
        if field.get("name") and field.get("color"):
            sets.setdefault(field["name"], {}).setdefault(field["slot"], set()).add(field["color"])
    for name, slots in sets.items():
        colours = set().union(*slots.values())
        for slot in SLOTS.values():
            if slots.get(slot) != colours:
                gaps.append({"model": "blessing", "kind": "incomplete_set", "where": f"{name} {slot}",
                             "expected_colours": sorted(colours), "found": sorted(slots.get(slot, ()))})
    if labels["icons"] and not labels.get("confirmed"):
        gaps.append({"model": "blessing", "kind": "labels_not_confirmed", "where": "blessing-labels.json"})
    return gaps


def wardrobe_gaps(run, diff):
    corpus = run.path(run.section("wardrobe")["corpus"])
    gaps = []
    wanted = diff["wardrobe"]["added"] + diff["wardrobe"]["previously_excluded"]
    if not wanted:
        return gaps
    class_map = read_json(corpus / "class-map.json")
    by_item = {item: row for rows in class_map.values() for row in rows for item in row["item_ids"]}
    missing = {row["item_id"]: row for row in read_json(corpus / "missing-items.json")}
    for row in wanted:
        item = row["item_id"]
        known = by_item.get(item)
        base = {"model": "wardrobe", "item_id": item, "part_id": row["part_id"], "name": row["name"],
                "icon_package": icon_package(row["icon"])}
        if known is None:
            reason = missing.get(item, {}).get("reason") or "not in corpus: rebuild the corpus"
            gaps.append({**base, "kind": "icon_unavailable" if item in missing else "corpus_stale", "reason": reason})
            continue
        for view in (0, 1):
            if not (corpus / "cards" / known["representative_asset_id"] / f"view{view}.png").is_file():
                gaps.append({**base, "kind": "card_missing", "reason": f"view{view}"})
    return gaps


def assets(run, accept_gaps=None):
    run.require("discover")
    diff = read_json(run.dir / "diff.json")
    labels = blessing_label_template(run, diff)
    sheet = blessing_contact_sheet(run, labels)
    gaps = wardrobe_gaps(run, diff) + blessing_gaps(run, labels)
    for group in diff["blessing"]["groups_without_icon_files"]:
        gaps.append({"model": "blessing", "kind": "icon_export_missing", "where": f'{group["series"]} {group["slot"]}'})
    counts = dict(Counter(row["kind"] for row in gaps))
    write_json(run.dir / "gaps.json", {"total": len(gaps), "by_kind": counts, "accepted_reason": accept_gaps, "gaps": gaps})
    log("assets", gaps=len(gaps), by_kind=counts, report=str(run.dir / "gaps.json"),
        review_sheet=str(sheet) if sheet else None)
    run.mark("assets", gaps=len(gaps), by_kind=counts, accepted_reason=accept_gaps)
    if gaps and not accept_gaps:
        # Items without readable art stay excluded and named in coverage.json, but
        # only after someone has seen the list and said why that is acceptable.
        raise SystemExit(f"{len(gaps)} gaps remain; resolve them or pass --accept-gaps \"<reason>\"")
