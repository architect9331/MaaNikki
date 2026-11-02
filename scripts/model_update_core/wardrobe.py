"""Wardrobe identity: extend part heads over the deployed backbone, or use a full retrain.

Head-only updates keep every existing class id. ``prototypes`` adds new rows and
leaves old rows untouched. ``head-train`` also fits the affected parts' heads on
cached embeddings. Both reuse the deployed backbone, so its shards stay identical.
Class numbers are always resolved through item ids, because a rebuilt corpus may
number the same class differently.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import numpy as np
from PIL import Image

from .common import WARDROBE, link_or_copy, log, normalize_json, read_json, sha256, write_json
from .gates import Gate


SPLITS = ("val", "synthetic_test", "real_test")


def load_model(directory: Path):
    with np.load(directory / "heads.npz") as packed:
        heads = {key: packed[key] for key in packed.files}
    return {"directory": directory, "labels": read_json(directory / "labels.json"), "heads": heads,
            "contract": read_json(directory / "model-contract.json"),
            "preprocess": read_json(directory / "preprocess.json"), "backbone": backbone_digest(directory)}


def backbone_digest(directory: Path):
    names = sorted(path.name for path in directory.glob("weights-*.bin")) + ["backbone.onnx"]
    digest = hashlib.sha256()
    for name in names:
        digest.update(sha256(directory / name).encode())
    return digest.hexdigest()


class Corpus:
    def __init__(self, root: Path):
        self.root = root
        self.config = read_json(root / "preprocess/config.json")
        self.class_map = read_json(root / "class-map.json")
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from wardrobe_dataset import augment
        self.augment = augment

    def samples(self, part, split):
        path = self.root / "parts" / str(part) / f"{split}.jsonl"
        if not path.is_file():
            return []
        with path.open(encoding="utf-8") as stream:
            return [json.loads(line) for line in stream if line.strip()]

    def tensor(self, row, image=None):
        with (image or Image.open(self.root / row["image_path"])) as source:
            image = self.augment(source, row, self.config, 0)
        return np.asarray(image, dtype=np.float32).transpose(2, 0, 1) / 255

    def canonical_mixes(self, row):
        """Canonical view on every blank slot background.

        Replace only pixels exactly equal to the card's own slot blank, as in
        training, so a new class does not tie its prototype to one grid slot.
        """
        with Image.open(self.root / row["image_path"]) as image:
            pixels = np.asarray(image.convert("RGB")).copy()
        canonical = dict(row, split="real_test")
        blank = self.blank(row["slot"])
        exact = np.all(pixels == blank, axis=2)
        tensors = []
        for slot in range(12):
            mixed = pixels.copy()
            mixed[exact] = self.blank(slot)[exact]
            tensors.append(self.tensor(canonical, Image.fromarray(mixed)))
        return tensors

    def blank(self, slot):
        if not hasattr(self, "_blanks"):
            self._blanks = {}
        if slot not in self._blanks:
            with Image.open(self.root / "preprocess/blanks" / f"slot{slot:02d}.png") as image:
                self._blanks[slot] = np.asarray(image.convert("RGB")).copy()
        return self._blanks[slot]


class Embedder:
    def __init__(self, run, directory: Path, threads=8):
        import onnxruntime as ort
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        self.session = ort.InferenceSession(str(directory / "backbone.onnx"), sess_options=options,
                                            providers=["CPUExecutionProvider"])
        self.cache = run.path(run.section("wardrobe").get("embedding_cache", ".build/model-update/embedding-cache"))
        self.cache = self.cache / backbone_digest(directory)[:16]
        self.cache.mkdir(parents=True, exist_ok=True)

    def forward(self, tensors, batch=16):
        output = [self.session.run(["embedding"], {"images": np.stack(tensors[start:start + batch])})[0]
                  for start in range(0, len(tensors), batch)]
        return np.concatenate(output) if output else np.zeros((0, 768), np.float32)

    def samples(self, corpus, part, split):
        """Embed one split once; sample ids bind the icon pixels and recipe."""
        rows = corpus.samples(part, split)
        path = self.cache / f"{part}-{split}.npz"
        known = {}
        if path.is_file():
            with np.load(path) as packed:
                known = dict(zip(packed["ids"].tolist(), packed["vectors"]))
        pending = [row for row in rows if row["sample_id"] not in known]
        if pending:
            log("embedding", part=part, split=split, samples=len(pending))
            for start in range(0, len(pending), 256):
                chunk = pending[start:start + 256]
                vectors = self.forward([corpus.tensor(row) for row in chunk])
                known.update(zip((row["sample_id"] for row in chunk), vectors))
            ids = sorted(known)
            np.savez(path, ids=np.array(ids), vectors=np.stack([known[key] for key in ids]))
        if not rows:
            return rows, np.zeros((0, 768), np.float32)
        return rows, np.stack([known[row["sample_id"]] for row in rows])


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


def write_head_candidate(run, model, labels, heads, corpus, mode, summary):
    target = run.candidate / WARDROBE
    target.mkdir(parents=True, exist_ok=True)
    write_json(target / "labels.json", labels, compact=True)
    np.savez_compressed(target / "heads.npz", **heads)
    part_names = {str(row["part_id"]): row["part_name"] for rows in corpus.class_map.values() for row in rows}
    previous_parts = read_json(model["directory"] / "parts.json")
    parts = {part: {"name": previous_parts.get(part, {}).get("name") or part_names[part],
                    "visual_classes": len(rows), "item_ids": sum(len(row["item_ids"]) for row in rows)}
             for part, rows in labels.items()}
    write_json(target / "parts.json", parts)
    covered = {item for rows in labels.values() for row in rows for item in row["item_ids"]}
    missing = [{key: row[key] for key in ("item_id", "name", "part_id", "part_name", "quality", "reason")}
               for row in read_json(corpus.root / "missing-items.json") if row["item_id"] not in covered]
    write_json(target / "coverage.json", {"parts": len(labels), "visual_classes": sum(map(len, labels.values())),
                                          "covered_item_ids": len(covered), "excluded_missing_items": missing})
    write_json(run.dir / "wardrobe-update.json", {"mode": mode, **summary})


def fit_head(weights, train_vectors, train_targets, val_vectors, val_targets, scale, device, epochs=30):
    """Train one part's cosine head with fixed scale; keep the best validation epoch."""
    import torch
    from torch.nn import functional as F
    torch.manual_seed(20261102)
    matrix = torch.nn.Parameter(torch.from_numpy(weights.copy()).to(device))
    vectors = torch.from_numpy(train_vectors).to(device)
    targets = torch.from_numpy(train_targets).to(device)
    held = torch.from_numpy(val_vectors).to(device)
    held_targets = torch.from_numpy(val_targets).to(device)
    optimizer = torch.optim.AdamW([matrix], lr=1e-3, weight_decay=0.)
    def score():
        with torch.no_grad():
            logits = held @ F.normalize(matrix, dim=1).T * scale
            return int((logits.argmax(1) == held_targets).sum()), -float(F.cross_entropy(logits, held_targets))
    best, best_state = score(), matrix.detach().clone()
    for _ in range(epochs):
        order = torch.randperm(len(vectors), device=device)
        for start in range(0, len(order), 512):
            index = order[start:start + 512]
            loss = F.cross_entropy(vectors[index] @ F.normalize(matrix, dim=1).T * scale, targets[index])
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
        current = score()
        if current > best:
            best, best_state = current, matrix.detach().clone()
    return F.normalize(best_state, dim=1).cpu().numpy().astype(np.float32)


def train_heads(run, mode):
    section = run.section("wardrobe")
    corpus = Corpus(run.path(section["corpus"]))
    model = load_model(run.root / WARDROBE)
    expected = {key: model["preprocess"].get(key) for key in ("card_size", "model_input_size", "mask_rectangles", "mask_fill")}
    actual = {key: corpus.config.get(key) for key in expected}
    if expected != actual:
        raise SystemExit("Corpus preprocessing differs from the deployed model; a head-only update is invalid. Use --mode full")
    baseline_map = read_json(run.path(section["baseline_class_map"])) if section.get("baseline_class_map") else None
    plans, conflicts = align(model["labels"], corpus.class_map, baseline_map)
    if conflicts:
        write_json(run.dir / "wardrobe-conflicts.json", conflicts)
        raise SystemExit(f"{len(conflicts)} label conflicts need a corpus review or --mode full; see wardrobe-conflicts.json")
    embedder = Embedder(run, model["directory"])
    labels = json.loads(json.dumps(model["labels"]))
    heads = dict(model["heads"])
    scale = float(heads["scale"])
    affected = {}
    for part, plan in plans.items():
        rows = labels.setdefault(part, [])
        matrix = heads.get(part, np.zeros((0, 768), np.float32))
        prototypes = []
        for row in [row for _, row in plan["changed_icon"]] + plan["new"]:
            samples = [sample for sample in corpus.samples(part, "train") if sample["visual_group_id"] == row["visual_group_id"]]
            if not samples:
                raise SystemExit(f"Corpus has no training recipes for {row['visual_group_id']}")
            canonical = next(sample for sample in samples if sample["variant"] == 0)
            tensors = corpus.canonical_mixes(canonical) + [corpus.tensor(sample) for sample in samples]
            prototype = embedder.forward(tensors).mean(axis=0)
            prototypes.append(prototype / np.linalg.norm(prototype))
        changed = plan["changed_icon"]
        for offset, (index, _) in enumerate(changed):
            matrix = matrix.copy()
            matrix[index] = prototypes[offset]
        if plan["new"]:
            matrix = np.vstack([matrix, np.stack(prototypes[len(changed):])]).astype(np.float32)
            for row in plan["new"]:
                rows.append(label_row(len(rows), row))
        for index, row in plan["existing"]:
            # Items sharing an existing icon join that row; class ids stay stable.
            rows[index]["item_ids"] = sorted(set(rows[index]["item_ids"]) | set(row["item_ids"]))
            rows[index]["names"] = list(dict.fromkeys(rows[index]["names"] + row["names"]))
            rows[index]["qualities"] = sorted(set(rows[index]["qualities"]) | set(row["qualities"]))
        heads[part] = matrix
        if plan["new"] or changed or plan["merged_items"]:
            affected[part] = {"new": len(plan["new"]), "changed_icon": len(changed),
                              "merged_items": len(plan["merged_items"]), "orphans": len(plan["orphans"])}
    if mode == "head-train":
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
        for part in affected:
            if not affected[part]["new"] and not affected[part]["changed_icon"]:
                continue
            log("head_train", part=part, classes=len(labels[part]))
            train_rows, train_vectors = embedder.samples(corpus, part, "train")
            val_rows, val_vectors = embedder.samples(corpus, part, "val")
            resolve = row_resolver(labels[part], corpus.class_map[part])
            train_targets = np.array([resolve[row["class_id"]] for row in train_rows], dtype=np.int64)
            val_targets = np.array([resolve[row["class_id"]] for row in val_rows], dtype=np.int64)
            heads[part] = fit_head(heads[part], train_vectors, train_targets, val_vectors, val_targets, scale, device)
    summary = {"affected_parts": affected, "classes_added": sum(row["new"] for row in affected.values()),
               "backbone_sha": model["backbone"], "base_version": model["contract"]["version"]}
    write_head_candidate(run, model, labels, heads, corpus, mode, summary)
    run.mark("train_wardrobe", mode=mode, **{k: v for k, v in summary.items() if k != "affected_parts"})
    log("train_wardrobe", mode=mode, **summary)


def train_full(run):
    """The corpus trainer lives with the private corpus; it writes candidate files."""
    section = run.section("wardrobe")
    command = section.get("full_command")
    if not command:
        raise SystemExit("wardrobe.full_command is not configured")
    target = run.candidate / WARDROBE
    target.mkdir(parents=True, exist_ok=True)
    work = run.dir / "wardrobe-full"
    values = {"candidate": str(target), "work": str(work), "corpus": str(run.path(section["corpus"])),
              "python": sys.executable}
    subprocess.run([part.format(**values) for part in command], check=True, cwd=run.path("."))
    for path in target.glob("*.json"):
        normalize_json(path)
    run.mark("train_wardrobe", mode="full")


def row_resolver(labels_part, corpus_rows):
    """Corpus class id -> deployed row index (or None) through item ids."""
    item_row = {item: index for index, row in enumerate(labels_part) for item in row["item_ids"]}
    resolved = {}
    for row in corpus_rows:
        matches = {item_row[item] for item in row["item_ids"] if item in item_row}
        resolved[row["class_id"]] = next(iter(matches)) if len(matches) == 1 else None
    return resolved


def changed_parts(baseline, candidate):
    if baseline["backbone"] != candidate["backbone"]:
        return sorted(candidate["labels"])
    return sorted(part for part in candidate["labels"]
                  if part not in baseline["heads"] or not np.array_equal(baseline["heads"][part], candidate["heads"][part])
                  or baseline["labels"].get(part) != candidate["labels"][part])


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
    if mode != "full":
        contract.update(base_version=previous, version=f"{previous}+{mode}.{run.id}", label_fingerprint=labels_digest,
                        preprocess_sha256=sha256(directory / "preprocess.json"),
                        head_update={"mode": mode, "run": run.id, "backbone": "unchanged"})
    return contract


def evaluate(run, waivers=None):
    run.require("train_wardrobe")
    section = run.section("wardrobe")
    corpus = Corpus(run.path(section["corpus"]))
    baseline = load_model(run.root / WARDROBE)
    directory = assemble_candidate(run)
    candidate_contract = synchronize_contract(run, directory, run.state["stages"]["train_wardrobe"]["mode"])
    target = run.dir / "assembled" / WARDROBE / "model-contract.json"
    target.unlink(missing_ok=True)
    write_json(target, candidate_contract)
    candidate = load_model(directory)
    gate = Gate("wardrobe")
    labels = candidate["labels"]
    gate.check("head_rows_match_labels",
               all(candidate["heads"][part].shape == (len(rows), 768) for part, rows in labels.items()), {})
    gate.check("class_ids_contiguous",
               all([row["class_id"] for row in rows] == list(range(len(rows))) for rows in labels.values()), {})
    gate.check("heads_unit_norm_and_finite", all(
        np.isfinite(candidate["heads"][part]).all()
        and np.allclose(np.linalg.norm(candidate["heads"][part], axis=1), 1, atol=1e-3) for part in labels), {})
    gate.check("scale_unchanged_for_head_update", baseline["backbone"] != candidate["backbone"]
               or float(baseline["heads"]["scale"]) == float(candidate["heads"]["scale"]), {})
    overlap = [item for rows in labels.values() for row in rows for item in row["item_ids"]]
    gate.check("item_ids_unique", len(overlap) == len(set(overlap)), {"duplicates": len(overlap) - len(set(overlap))})
    # Keep class ids stable across a head update: each old row keeps its position.
    if baseline["backbone"] == candidate["backbone"]:
        moved = [(part, index) for part, rows in baseline["labels"].items() for index, row in enumerate(rows)
                 if not set(row["item_ids"]) <= set(labels[part][index]["item_ids"])]
        gate.check("existing_class_ids_stable", not moved, {"moved": moved[:20]})
    parts = changed_parts(baseline, candidate)
    base_embedder = Embedder(run, baseline["directory"])
    candidate_embedder = base_embedder if baseline["backbone"] == candidate["backbone"] else Embedder(run, directory)
    report = {"changed_parts": parts,
              "unchanged_parts": "identical backbone and head rows produce identical predictions; not re-evaluated",
              "parts": {}, "new_class_failures": []}
    totals = {split: {"old": dict(samples=0, baseline_correct=0, candidate_correct=0, regressions=0, fixes=0,
                                  baseline_top5=0, candidate_top5=0, stolen_by_new=0),
                      "new": dict(samples=0, correct=0, top5=0)} for split in SPLITS}
    for part in parts:
        base_labels = baseline["labels"].get(part, [])
        base_rows = row_resolver(base_labels, corpus.class_map.get(part, []))
        cand_rows = row_resolver(labels[part], corpus.class_map.get(part, []))
        new_row_start = len(base_labels) if baseline["backbone"] == candidate["backbone"] else None
        part_report = {}
        for split in SPLITS:
            rows, base_vectors = base_embedder.samples(corpus, part, split)
            if not rows:
                continue
            cand_vectors = base_vectors if candidate_embedder is base_embedder else candidate_embedder.samples(corpus, part, split)[1]
            cand_scores = cand_vectors @ candidate["heads"][part].T
            cand_top5 = np.argsort(cand_scores, axis=1)[:, -5:]
            cand_pred = cand_scores.argmax(1)
            if part in baseline["heads"]:
                base_scores = base_vectors @ baseline["heads"][part].T
                base_top5, base_pred = np.argsort(base_scores, axis=1)[:, -5:], base_scores.argmax(1)
            counts = {"old": dict(samples=0, baseline_correct=0, candidate_correct=0, regressions=0, fixes=0,
                                  baseline_top5=0, candidate_top5=0, stolen_by_new=0),
                      "new": dict(samples=0, correct=0, top5=0)}
            for index, row in enumerate(rows):
                expected = cand_rows.get(row["class_id"])
                prior = base_rows.get(row["class_id"])
                ok = expected is not None and int(cand_pred[index]) == expected
                top5 = expected is not None and expected in cand_top5[index]
                if prior is None:
                    counts["new"]["samples"] += 1
                    counts["new"]["correct"] += int(ok)
                    counts["new"]["top5"] += int(top5)
                    if not ok:
                        report["new_class_failures"].append({
                            "part": part, "split": split, "sample": row["sample_id"], "expected": expected,
                            "predicted": int(cand_pred[index]), "names": labels[part][int(cand_pred[index])]["names"]})
                    continue
                was = int(base_pred[index]) == prior
                counts["old"]["samples"] += 1
                counts["old"]["baseline_correct"] += int(was)
                counts["old"]["candidate_correct"] += int(ok)
                counts["old"]["regressions"] += int(was and not ok)
                counts["old"]["fixes"] += int(ok and not was)
                counts["old"]["baseline_top5"] += int(prior in base_top5[index])
                counts["old"]["candidate_top5"] += int(top5)
                if new_row_start is not None and not ok and int(cand_pred[index]) >= new_row_start:
                    counts["old"]["stolen_by_new"] += 1
            part_report[split] = counts
            for group, values in counts.items():
                for key, value in values.items():
                    totals[split][group][key] += value
            regression = {key: counts["old"][key] for key in ("samples", "baseline_correct", "candidate_correct", "regressions", "fixes")}
            gate.regression(f"part_{part}_{split}_existing", regression)
        report["parts"][part] = part_report
    report["totals"] = totals
    for split, group in totals.items():
        old = group["old"]
        gate.regression(f"{split}_existing", {key: old[key] for key in ("samples", "baseline_correct", "candidate_correct", "regressions", "fixes")})
        gate.new_classes(f"{split}_new_vs_existing", {"samples": group["new"]["samples"], "correct": group["new"]["correct"]},
                         {"samples": old["samples"], "correct": old["candidate_correct"]})
    # A new class that cannot recognise its own undegraded card has no usable head.
    canonical_failures = [row for row in report["new_class_failures"]
                          if row["split"] == "val" and row["sample"].endswith("_val00")]
    gate.check("new_classes_recognise_canonical_card", not canonical_failures, {"failures": canonical_failures[:50]})
    result = gate.result(waivers)
    write_json(run.evaluation / "wardrobe.json", report)
    write_json(run.evaluation / "wardrobe-gate.json", result)
    write_probe_cases(run, corpus, candidate, parts)
    run.mark("evaluate_wardrobe", status=result["status"], blocking=result["blocking"])
    log("evaluate_wardrobe", status=result["status"], blocking=result["blocking"], totals=totals)
    return result


def write_probe_cases(run, corpus, candidate, parts):
    """Sample cards for the packaged runtime: new classes first, then existing ones."""
    cases = []
    for part in parts or sorted(candidate["labels"])[:1]:
        resolve = row_resolver(candidate["labels"][part], corpus.class_map.get(part, []))
        rows = [row for row in corpus.samples(part, "val") if row["variant"] == 0 and resolve.get(row["class_id"]) is not None]
        # New classes are appended, so the highest rows come first.
        rows.sort(key=lambda row: -resolve[row["class_id"]])
        cases += [{"part": part, "image": str(corpus.root / row["image_path"]), "class_id": resolve[row["class_id"]]}
                  for row in rows[:24]]
    write_json(run.evaluation / "wardrobe-probe-cases.json", cases)
