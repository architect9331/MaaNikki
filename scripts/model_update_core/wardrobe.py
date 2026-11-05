"""Wardrobe catalog: extend deployed part labels from the decoded corpus."""
from __future__ import annotations

import json
from pathlib import Path
import shutil

from .common import WARDROBE, link_or_copy, log, read_json, sha256, write_json
from .gates import Gate


def load_model(directory: Path):
    return {
        "directory": directory,
        "labels": read_json(directory / "labels.json"),
        "parts": read_json(directory / "parts.json"),
        "coverage": read_json(directory / "coverage.json"),
        "contract": read_json(directory / "model-contract.json"),
    }


def align(labels, class_map, baseline_map=None):
    """Relate rebuilt corpus classes to deployed rows through item ids."""
    plans, conflicts = {}, []
    previous_group = {}
    for rows in (baseline_map or {}).values():
        for row in rows:
            for item in row["item_ids"]:
                previous_group[item] = row["visual_group_id"]
    for part, rows in sorted(class_map.items()):
        deployed = labels.get(part, [])
        item_row = {item: index for index, row in enumerate(deployed) for item in row["item_ids"]}
        claimed = {}
        plan = {"existing": [], "new": [], "changed_icon": [], "merged_items": [], "orphans": []}
        for row in rows:
            overlap = sorted({item_row[item] for item in row["item_ids"] if item in item_row})
            if len(overlap) > 1:
                conflicts.append({"part": part, "kind": "corpus_class_spans_rows", "class": row["visual_group_id"],
                                  "rows": overlap})
            elif not overlap:
                plan["new"].append(row)
            else:
                index = overlap[0]
                if index in claimed:
                    conflicts.append({"part": part, "kind": "row_split_by_corpus", "row": index,
                                      "classes": [claimed[index], row["visual_group_id"]]})
                    continue
                claimed[index] = row["visual_group_id"]
                plan["existing"].append((index, row))
                if set(row["item_ids"]) - set(deployed[index]["item_ids"]):
                    plan["merged_items"].append({"row": index, "item_ids": row["item_ids"]})
                if any(item in previous_group and previous_group[item] != row["visual_group_id"]
                       for item in row["item_ids"]):
                    plan["changed_icon"].append((index, row))
        plan["orphans"] = [index for index in range(len(deployed)) if index not in claimed]
        plans[part] = plan
    return plans, conflicts


def label_row(class_id, row):
    return {"class_id": class_id, "item_ids": row["item_ids"], "names": row["names"], "qualities": row["qualities"]}


def write_catalog_candidate(run, model, labels, class_map, missing_items, mode, summary):
    target = run.candidate / WARDROBE
    target.mkdir(parents=True, exist_ok=True)
    write_json(target / "labels.json", labels, compact=True)
    part_names = {str(row["part_id"]): row["part_name"] for rows in class_map.values() for row in rows}
    previous_parts = model["parts"]
    parts = {part: {"name": previous_parts.get(part, {}).get("name") or part_names[part],
                    "visual_classes": len(rows), "item_ids": sum(len(row["item_ids"]) for row in rows)}
             for part, rows in labels.items()}
    write_json(target / "parts.json", parts)
    covered = {item for rows in labels.values() for row in rows for item in row["item_ids"]}
    missing = [{key: row[key] for key in ("item_id", "name", "part_id", "part_name", "quality", "reason")}
               for row in missing_items if row["item_id"] not in covered]
    write_json(target / "coverage.json", {"parts": len(labels), "visual_classes": sum(map(len, labels.values())),
                                          "covered_item_ids": len(covered), "excluded_missing_items": missing})
    write_json(run.dir / "wardrobe-update.json", {"mode": mode, **summary})


def train_heads(run, mode="catalog"):
    section = run.section("wardrobe")
    corpus_root = run.path(section["corpus"])
    class_map = read_json(corpus_root / "class-map.json")
    missing_items = read_json(corpus_root / "missing-items.json") if (corpus_root / "missing-items.json").is_file() else []
    model = load_model(run.root / WARDROBE)
    baseline_map = read_json(run.path(section["baseline_class_map"])) if section.get("baseline_class_map") else None
    plans, conflicts = align(model["labels"], class_map, baseline_map)
    if conflicts:
        write_json(run.dir / "wardrobe-conflicts.json", conflicts)
        raise SystemExit(f"{len(conflicts)} label conflicts need a corpus review; see wardrobe-conflicts.json")
    labels = json.loads(json.dumps(model["labels"]))
    affected = {}
    for part, plan in plans.items():
        rows = labels.setdefault(part, [])
        for row in plan["new"]:
            rows.append(label_row(len(rows), row))
        for index, row in plan["existing"]:
            rows[index]["item_ids"] = sorted(set(rows[index]["item_ids"]) | set(row["item_ids"]))
            rows[index]["names"] = list(dict.fromkeys(rows[index]["names"] + row["names"]))
            rows[index]["qualities"] = sorted(set(rows[index]["qualities"]) | set(row["qualities"]))
        if plan["new"] or plan["changed_icon"] or plan["merged_items"]:
            affected[part] = {"new": len(plan["new"]), "changed_icon": len(plan["changed_icon"]),
                              "merged_items": len(plan["merged_items"]), "orphans": len(plan["orphans"])}
    summary = {"affected_parts": affected, "classes_added": sum(row["new"] for row in affected.values()),
               "base_version": model["contract"]["version"]}
    write_catalog_candidate(run, model, labels, class_map, missing_items, mode, summary)
    run.mark("train_wardrobe", mode=mode, **{k: v for k, v in summary.items() if k != "affected_parts"})
    log("train_wardrobe", mode=mode, **summary)


def train_full(run):
    train_heads(run, mode="full")


def assemble_candidate(run):
    """Complete candidate directory; unchanged files link to the baseline."""
    source, overlay = run.root / WARDROBE, run.candidate / WARDROBE
    target = run.dir / "assembled" / WARDROBE
    if target.exists():
        shutil.rmtree(target)
    for path in source.iterdir():
        link_or_copy(overlay / path.name if (overlay / path.name).exists() else path, target / path.name)
    for path in overlay.glob("*"):
        if not (target / path.name).exists():
            link_or_copy(path, target / path.name)
    return target


def synchronize_contract(run, directory, mode):
    """Bind the candidate's files and labels into model-contract.json."""
    contract = read_json(directory / "model-contract.json")
    labels_digest = sha256(directory / "labels.json")
    names = [name for name in contract["files"] if (directory / name).exists()]
    names += sorted(path.name for path in directory.iterdir()
                    if path.name not in names and path.name != "model-contract.json" and path.is_file())
    contract["files"] = {name: {"bytes": (directory / name).stat().st_size, "sha256": sha256(directory / name)}
                         for name in names}
    previous = contract.get("base_version", contract["version"])
    contract.update(base_version=previous, version=f"{previous}+{mode}.{run.id}", label_fingerprint=labels_digest)
    return contract


def evaluate(run, waivers=None):
    run.require("train_wardrobe")
    baseline = load_model(run.root / WARDROBE)
    directory = assemble_candidate(run)
    candidate_contract = synchronize_contract(run, directory, run.state["stages"]["train_wardrobe"]["mode"])
    target = run.dir / "assembled" / WARDROBE / "model-contract.json"
    target.unlink(missing_ok=True)
    write_json(target, candidate_contract)
    candidate = load_model(directory)
    gate = Gate("wardrobe")
    labels = candidate["labels"]
    gate.check("class_ids_contiguous",
               all([row["class_id"] for row in rows] == list(range(len(rows))) for rows in labels.values()), {})
    overlap = [item for rows in labels.values() for row in rows for item in row["item_ids"]]
    gate.check("item_ids_unique", len(overlap) == len(set(overlap)), {"duplicates": len(overlap) - len(set(overlap))})
    moved = [(part, index) for part, rows in baseline["labels"].items() for index, row in enumerate(rows)
             if not set(row["item_ids"]) <= set(labels[part][index]["item_ids"])]
    gate.check("existing_class_ids_stable", not moved, {"moved": moved[:20]})
    changed_parts = sorted(part for part in labels if baseline["labels"].get(part) != labels[part])
    report = {"changed_parts": changed_parts, "total_parts": len(labels), "total_items": len(overlap)}
    result = gate.result(waivers)
    write_json(run.evaluation / "wardrobe.json", report)
    write_json(run.evaluation / "wardrobe-gate.json", result)
    run.mark("evaluate_wardrobe", status=result["status"], blocking=result["blocking"])
    log("evaluate_wardrobe", status=result["status"], blocking=result["blocking"], changed_parts=changed_parts)
    return result
