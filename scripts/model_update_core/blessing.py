"""Blessing identity retraining, plus level and badge checks over new icons.

The identity network retrains in about two minutes, so it has no head-only mode.
Level and badge classes do not depend on the blessing list: new icons only act
as new backgrounds. Those two models retrain when the check below finds a real
drop or the card UI itself changed, which also requires newly captured layers.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import subprocess
import sys

import cv2
import numpy as np
from PIL import Image

from .common import log, read_json, write_json
from .gates import Gate


SLOTS = ["头部", "手部", "脚部"]
SEED = 73
SYNTHETIC_EPOCHS = 16
ADAPTATION_EPOCHS = 10


def linear(value):
    return np.where(value <= .04045, value / 12.92, ((value + .055) / 1.055) ** 2.4)


def srgb(value):
    return np.where(value < .0031308, value * 12.92, 1.055 * np.maximum(value, 0) ** (1 / 2.4) - .055)


def sample(texture, uv):
    h, w = texture.shape[:2]
    xy = uv * [w, h] - .5
    lower = np.floor(xy).astype(int)
    fraction = xy - lower
    x0, y0 = np.clip(lower[:, 0], 0, w - 1), np.clip(lower[:, 1], 0, h - 1)
    x1, y1 = np.clip(lower[:, 0] + 1, 0, w - 1), np.clip(lower[:, 1] + 1, 0, h - 1)
    wx, wy = fraction[:, :1], fraction[:, 1:]
    return ((texture[y0, x0] * (1 - wx) + texture[y0, x1] * wx) * (1 - wy)
            + (texture[y1, x0] * (1 - wx) + texture[y1, x1] * wx) * wy)


def icon_uv(height, width):
    # The captured card draws its 140x140 icon quad from (6,7).
    yy, xx = np.mgrid[0:height, 0:width]
    uv = np.stack(((xx.ravel() + .5 - 6) / 140, (yy.ravel() + .5 - 7) / 140), axis=1)
    return uv, (uv[:, 0] >= 0) & (uv[:, 0] < 1) & (uv[:, 1] >= 0) & (uv[:, 1] < 1)


def read_icon(path):
    raw = np.asarray(Image.open(path).convert("RGBA"), dtype=np.float64) / 255
    raw[:, :, :3] = linear(raw[:, :, :3])
    return raw


def identity_layer(path):
    uv, inside = icon_uv(156, 147)
    layer = np.zeros((156 * 147, 4), np.float64)
    sampled = sample(read_icon(path), uv[inside])
    sampled[:, :3] = srgb(sampled[:, :3])
    layer[inside] = sampled
    return cv2.resize(layer.reshape(156, 147, 4)[22:123, 10:138], (96, 96), interpolation=cv2.INTER_AREA).astype(np.float32)


def attribute_layer(path):
    uv, inside = icon_uv(160, 150)
    sampled = sample(read_icon(path), uv[inside])
    sampled[:, :3] = srgb(sampled[:, :3])
    alpha = sampled[:, 3:4]
    rgb = linear(np.round(np.clip(srgb(sampled[:, :3] * alpha), 0, 1) * 255) / 255)
    layer = np.zeros((160 * 150, 4), np.float32)
    layer[inside] = np.concatenate((rgb, alpha), axis=1)
    return layer.reshape(160, 150, 4)


def key_of(entry):
    return entry["name"], entry["slot"], entry["color"]


class Catalogue:
    """Labels and layers of the baseline plus this run's confirmed new icons."""

    def __init__(self, run):
        self.run = run
        self.section = run.section("blessing")
        self.icon_map = read_json(run.path(self.section["icon_map"]))
        self.by_key = {key_of(row): (index, row) for index, row in enumerate(self.icon_map["records"])}
        self.metadata = read_json(run.root / "resource/model/blessing_identity.json")
        path = run.dir / "blessing-labels.json"
        self.new = read_json(path)["icons"] if path.is_file() else []
        self.entries = [dict(row) for row in self.metadata["entries"]]
        known = {key_of(row) for row in self.entries}
        for row in self.new:
            if key_of(row) in known:
                raise SystemExit(f"New icon duplicates an existing entry: {key_of(row)}")
            self.entries.append({key: row[key] for key in ("name", "slot", "color", "rarity")})
        self.baseline_keys = {key_of(row) for row in self.metadata["entries"]}
        self.files = {key_of(row): row["file"] for row in self.new}
        self.originals = list(self.metadata["original_colours"]) + [
            {"name": row["name"], "slot": row["slot"], "color": row["color"],
             "source": "base item icon_reference selects variant 1", "series": row["series"]}
            for row in self.new if row.get("original")]

    def identity_layers(self, entries):
        layers = np.load(self.run.path(self.section["identity_layers"]))
        stored = layers["icons"]
        icons = [stored[self.by_key[key_of(row)][1]["wiki_index"]] if key_of(row) not in self.files
                 else identity_layer(self.files[key_of(row)]) for row in entries]
        return np.stack(icons), layers["backgrounds"], layers["foregrounds"]

    def attribute_layers(self, entries):
        layers = np.load(self.run.path(self.section["attribute_layers"]))
        stored = layers["icons"]
        icons = [stored[self.by_key[key_of(row)][0]] if key_of(row) not in self.files
                 else attribute_layer(self.files[key_of(row)]) for row in entries]
        return np.stack(icons), {key: layers[key] for key in layers.files if key != "icons"}


def load_real(run, name, groups, entries):
    group_index = {tuple(group): index for index, group in enumerate(groups)}
    colour_index = {key_of(row): index for index, row in enumerate(entries)}
    folder = run.path(run.section("blessing")["logs"]) / name
    records = []
    for path in sorted(folder.glob("card_*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        item = record["item"]
        group = group_index.get((item["name"], item["slot"]))
        if group is None or record.get("error"):
            continue
        image = cv2.imdecode(np.fromfile(folder / record["card"], np.uint8), cv2.IMREAD_COLOR)
        h, w = image.shape[:2]
        crop = image[round(h * .14):round(h * .79), round(w * .07):round(w * .94)]
        crop = cv2.resize(crop, (96, 96), interpolation=cv2.INTER_AREA)
        records.append({"image": cv2.cvtColor(crop, cv2.COLOR_BGR2RGB), "group": group,
                        "colour": colour_index.get((item["name"], item["slot"], item.get("color")), -1),
                        "slot": SLOTS.index(item["slot"]), "item": item, "card": record["card"]})
    return records


class IdentityTrainer:
    def __init__(self, run, catalogue, entries, seed=SEED):
        import torch
        from torch.nn import functional as F
        self.torch, self.F = torch, F
        torch.manual_seed(seed)
        np.random.seed(seed)
        torch.set_num_threads(4)
        hub = run.section("blessing").get("torch_hub")
        if hub:
            torch.hub.set_dir(str(run.path(hub)))
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.run, self.entries = run, entries
        self.groups = list(dict.fromkeys((row["name"], row["slot"]) for row in entries))
        group_index = {key: index for index, key in enumerate(self.groups)}
        self.class_group = torch.tensor([group_index[(row["name"], row["slot"])] for row in entries], device=self.device)
        self.group_slot = torch.tensor([SLOTS.index(slot) for _, slot in self.groups], device=self.device)
        self.rarity = torch.tensor([row["rarity"] - 3 for row in entries], device=self.device)
        icons, backgrounds, foregrounds = catalogue.identity_layers(entries)
        tensor = lambda value: torch.from_numpy(value).permute(0, 3, 1, 2).float().to(self.device)
        self.icons, self.background, self.foreground = tensor(icons), tensor(backgrounds), tensor(foregrounds)
        self.mean = torch.tensor([.485, .456, .406], device=self.device)[None, :, None, None]
        self.std = torch.tensor([.229, .224, .225], device=self.device)[None, :, None, None]

    def model(self, pretrained=True):
        from torch import nn
        from torchvision.models import MobileNet_V3_Small_Weights, mobilenet_v3_small

        class HierarchicalIcons(nn.Module):
            def __init__(inner, groups, entries):
                super().__init__()
                base = mobilenet_v3_small(weights=MobileNet_V3_Small_Weights.DEFAULT if pretrained else None)
                inner.features = base.features
                inner.pool = nn.AdaptiveAvgPool2d(1)
                inner.dropout = nn.Dropout(.2)
                inner.identity = nn.Linear(576, groups)
                inner.colour = nn.Linear(576, entries)

            def forward(inner, image):
                vector = inner.dropout(inner.pool(inner.features(image)).flatten(1))
                return inner.identity(vector), inner.colour(vector)

        return HierarchicalIcons(len(self.groups), len(self.entries)).to(self.device)

    def augment(self, images, strength=1.):
        torch, device = self.torch, self.device
        n = images.shape[0]
        affine = torch.eye(2, 3, device=device)[None].repeat(n, 1, 1)
        scale = 1 + (torch.rand(n, device=device) - .5) * .10 * strength
        affine[:, 0, 0] = scale
        affine[:, 1, 1] = scale
        affine[:, :, 2] = (torch.rand(n, 2, device=device) - .5) * .06 * strength
        images = self.F.grid_sample(images, self.F.affine_grid(affine, images.shape, align_corners=False),
                                    padding_mode="border", align_corners=False)
        # Exposure and contrast vary without hue jitter: colour is a label.
        gain = 1 + (torch.rand(n, 1, 1, 1, device=device) - .5) * .24 * strength
        contrast = 1 + (torch.rand(n, 1, 1, 1, device=device) - .5) * .20 * strength
        return (((images - .5) * contrast + .5) * gain).clamp(0, 1)

    def compose(self, labels):
        torch, device = self.torch, self.device
        n = len(labels)
        icon = self.icons[labels]
        backing = self.background[(torch.rand(n, device=device) < .25).long()]
        front = self.foreground[self.rarity[labels]]
        rgb = icon[:, :3] * icon[:, 3:4] + backing[:, :3] * (1 - icon[:, 3:4])
        alpha = icon[:, 3:4] + backing[:, 3:4] * (1 - icon[:, 3:4])
        rgb = front[:, :3] + rgb * (1 - front[:, 3:4])
        alpha = front[:, 3:4] + alpha * (1 - front[:, 3:4])
        context = self.F.interpolate(torch.rand(n, 3, 2, 2, device=device) * .16 + .07, size=(96, 96),
                                     mode="bilinear", align_corners=False)
        image = self.augment(rgb + context * (1 - alpha))
        return (image + torch.randn_like(image) * .008).clamp(0, 1)

    def synthetic(self, n):
        labels = self.torch.randint(len(self.entries), (n,), device=self.device)
        groups = self.class_group[labels]
        return self.compose(labels), groups, labels, self.group_slot[groups]

    def loss(self, model, batch):
        image, group, colour, slot = batch
        identities, colours = model((image - self.mean) / self.std)
        identities = identities.masked_fill(self.group_slot[None] != slot[:, None], -10000.)
        colours = colours.masked_fill(self.class_group[None] != group[:, None], -10000.)
        known = colour >= 0
        return self.F.cross_entropy(identities, group) + self.F.cross_entropy(colours[known], colour[known])

    def stage(self, model, epochs, real=None):
        torch = self.torch
        samples = len(self.entries) * (24 if real is None else 12)
        optimizer = torch.optim.AdamW(model.parameters(), lr=.0003 if real is None else .00008, weight_decay=.0001)
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, epochs)
        for epoch in range(epochs):
            model.train()
            total = 0.
            steps = math.ceil(samples / 128)
            for _ in range(steps):
                if real is None:
                    batch = self.synthetic(128)
                else:
                    ids = torch.randint(real[0].shape[0], (64,), device=self.device)
                    observed = (self.augment(real[0][ids], .5), real[1][ids], real[2][ids], real[3][ids])
                    batch = tuple(torch.cat(pair) for pair in zip(self.synthetic(64), observed))
                optimizer.zero_grad(set_to_none=True)
                loss = self.loss(model, batch)
                loss.backward()
                optimizer.step()
                total += float(loss.detach())
            scheduler.step()
            log("identity_epoch", phase="synthetic" if real is None else "adaptation", epoch=epoch + 1, loss=round(total / steps, 5))

    def real_arrays(self, records):
        torch, device = self.torch, self.device
        return (torch.from_numpy(np.stack([row["image"] for row in records])).permute(0, 3, 1, 2).float().to(device) / 255,
                torch.tensor([row["group"] for row in records], device=device),
                torch.tensor([row["colour"] for row in records], device=device),
                torch.tensor([row["slot"] for row in records], device=device))

    def export(self, model, onnx_path, metadata_path, base_metadata, originals, version):
        torch = self.torch
        from torch import nn

        class Portable(nn.Module):
            def __init__(inner, wrapped):
                super().__init__()
                inner.model = wrapped
                inner.register_buffer("mean", torch.tensor([.485, .456, .406])[None, :, None, None])
                inner.register_buffer("std", torch.tensor([.229, .224, .225])[None, :, None, None])

            def forward(inner, image):
                return inner.model((image - inner.mean) / inner.std)

        Path(onnx_path).parent.mkdir(parents=True, exist_ok=True)
        portable = Portable(model.cpu().eval()).eval()
        torch.onnx.export(portable, torch.zeros(1, 3, 96, 96), str(onnx_path), opset_version=11,
                          input_names=["image"], output_names=["identity_logits", "colour_logits"],
                          dynamic_axes={"image": {0: "batch"}, "identity_logits": {0: "batch"}, "colour_logits": {0: "batch"}})
        metadata = dict(base_metadata)
        wanted = {key_of(row) for row in self.entries}
        metadata.update(model_version=version, entries=self.entries, groups=[list(group) for group in self.groups],
                        original_colours=[{"name": row["name"], "slot": row["slot"], "color": row["color"]}
                                          for row in originals if key_of(row) in wanted])
        write_json(metadata_path, metadata)


def train_identity(run, catalogue, entries, onnx_path, metadata_path, version, use_real_new=False, seed=SEED):
    """Layer-synthesis stage followed by adaptation on retained real cards."""
    torch_module = __import__("torch")
    trainer = IdentityTrainer(run, catalogue, entries, seed)
    section = run.section("blessing")
    groups = trainer.groups
    known_groups = {(row["name"], row["slot"]) for row in catalogue.metadata["entries"]}
    real = []
    for name in section["real_training_runs"]:
        real += load_real(run, name, groups, entries)
    if not use_real_new:
        # Few same-account cards of a brand-new class would bias its colour head.
        real = [row for row in real if (row["item"]["name"], row["item"]["slot"]) in known_groups]
    model = trainer.model()
    log("identity_train", entries=len(entries), groups=len(groups), real_cards=len(real), device=str(trainer.device))
    trainer.stage(model, SYNTHETIC_EPOCHS)
    trainer.stage(model, ADAPTATION_EPOCHS, trainer.real_arrays(real))
    Path(onnx_path).parent.mkdir(parents=True, exist_ok=True)
    torch_module.save(model.state_dict(), Path(onnx_path).with_suffix(".pt"))
    trainer.export(model, onnx_path, metadata_path, catalogue.metadata, catalogue.originals, version)


def train(run, use_real_new=False):
    catalogue = Catalogue(run)
    work = run.dir / "identity"
    train_identity(run, catalogue, catalogue.entries, work / "blessing_identity.onnx", work / "blessing_identity.json",
                   f"{run.id}-mobilenetv3-game-adapted", use_real_new)
    run.link_candidate("resource/model/blessing_identity.onnx", work / "blessing_identity.onnx")
    run.link_candidate("resource/model/blessing_identity.json", work / "blessing_identity.json")
    write_catalog(run, catalogue)
    run.mark("train_identity", entries=len(catalogue.entries), new=len(catalogue.new))


def write_catalog(run, catalogue):
    """Runtime refuses a model whose labels differ from the catalogue, so they ship together."""
    if not catalogue.new:
        return
    catalog = read_json(run.root / "resource/data/blessing_catalog.json")
    sets = {row["name"]: row for row in catalog["sets"]}
    for row in catalogue.new:
        definition = sets.get(row["name"])
        if definition is None:
            definition = {"name": row["name"], "colors": [], "rarity": row["rarity"], "source": row.get("source")}
            sets[row["name"]] = definition
            catalog["sets"].append(definition)
        if row["color"] not in definition["colors"]:
            definition["colors"].append(row["color"])
    catalog["version"] = run.id
    write_json(run.candidate / "resource/data/blessing_catalog.json", catalog)


class Net:
    """ONNX Runtime with OpenCV's calling style. Development OpenCV 4.9 miscomputes the
    level graph (181/205 where ONNX Runtime and the packaged 4.10 agree on 205/205);
    package.py still runs the real OpenCV path through the runtime probe."""

    def __init__(self, path):
        import onnxruntime as ort
        self.session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        self.input = self.session.get_inputs()[0].name

    def setInput(self, blob):
        self.blob = blob

    def forward(self, outputs=None):
        if outputs is None:
            return self.session.run(None, {self.input: self.blob})[0]
        return self.session.run(list(outputs), {self.input: self.blob})


class Identity:
    def __init__(self, onnx_path, metadata):
        self.net = Net(onnx_path)
        self.entries = metadata["entries"]
        self.groups = [tuple(group) for group in metadata["groups"]]
        lookup = {key: index for index, key in enumerate(self.groups)}
        self.class_group = np.array([lookup[(row["name"], row["slot"])] for row in self.entries])
        self.slot_groups = {slot: np.array([i for i, (_, part) in enumerate(self.groups) if part == slot]) for slot in SLOTS}

    def predict(self, images, slots):
        """Runtime decision rule: slot-limited identity, then colour within identity."""
        output = []
        for start in range(0, len(images), 128):
            self.net.setInput(images[start:start + 128])
            identities, colours = self.net.forward(["identity_logits", "colour_logits"])
            for index, slot in enumerate(slots[start:start + 128]):
                eligible = self.slot_groups[SLOTS[slot]]
                group = int(eligible[identities[index, eligible].argmax()])
                members = np.flatnonzero(self.class_group == group)
                output.append(self.entries[int(members[colours[index, members].argmax()])])
        return output


def paired(rows, baseline_predictions, candidate_predictions, match):
    counts = dict(samples=0, baseline_correct=0, candidate_correct=0, regressions=0, fixes=0)
    for row, before, after in zip(rows, baseline_predictions, candidate_predictions):
        was, now = match(row, before), match(row, after)
        counts["samples"] += 1
        counts["baseline_correct"] += int(was)
        counts["candidate_correct"] += int(now)
        counts["regressions"] += int(was and not now)
        counts["fixes"] += int(now and not was)
    return counts


def identity_matches(row, predicted):
    return (predicted["name"], predicted["slot"]) == (row["name"], row["slot"])


def joint_matches(row, predicted):
    return identity_matches(row, predicted) and predicted["color"] == row["color"]


def evaluate_identity(run, gate, report, samples_per_class=16):
    baseline_metadata = read_json(run.root / "resource/model/blessing_identity.json")
    candidate_metadata = read_json(run.resolved("resource/model/blessing_identity.json"))
    baseline = Identity(run.root / "resource/model/blessing_identity.onnx", baseline_metadata)
    candidate = Identity(run.resolved("resource/model/blessing_identity.onnx"), candidate_metadata)
    catalogue = Catalogue(run)
    old_keys = {key_of(row) for row in baseline_metadata["entries"]}
    gate.check("identity_entries_keep_existing", old_keys <= {key_of(row) for row in candidate_metadata["entries"]}, {})
    trainer = IdentityTrainer(run, catalogue, candidate_metadata["entries"], seed=991)
    torch = trainer.torch
    labels = torch.arange(len(candidate_metadata["entries"]), device=trainer.device).repeat_interleave(samples_per_class)
    with torch.no_grad():
        images = torch.cat([trainer.compose(labels[start:start + 256]) for start in range(0, len(labels), 256)]).cpu().numpy()
    entries = [candidate_metadata["entries"][i] for i in labels.cpu().tolist()]
    slots = [SLOTS.index(row["slot"]) for row in entries]
    candidate_predictions = candidate.predict(images, slots)
    old_index = [i for i, row in enumerate(entries) if key_of(row) in old_keys]
    new_index = [i for i, row in enumerate(entries) if key_of(row) not in old_keys]
    baseline_predictions = baseline.predict(images[old_index], [slots[i] for i in old_index])
    old_rows = [entries[i] for i in old_index]
    old_candidate = [candidate_predictions[i] for i in old_index]
    joint = paired(old_rows, baseline_predictions, old_candidate, joint_matches)
    gate.regression("identity_synthetic_existing", joint)
    new = {"samples": len(new_index), "correct": sum(joint_matches(entries[i], candidate_predictions[i]) for i in new_index),
           "identity_correct": sum(identity_matches(entries[i], candidate_predictions[i]) for i in new_index)}
    gate.new_classes("identity_synthetic_new_vs_existing", {"samples": new["samples"], "correct": new["correct"]},
                     {"samples": joint["samples"], "correct": joint["candidate_correct"]})
    per_new = {}
    for i in new_index:
        key = "·".join(key_of(entries[i]))
        per_new.setdefault(key, [0, 0])
        per_new[key][0] += joint_matches(entries[i], candidate_predictions[i])
        per_new[key][1] += 1
    report["identity"] = {"synthetic_existing": joint, "synthetic_new": new, "synthetic_new_by_class": per_new}
    # Real retained cards: identity everywhere, colour only where the log knows it.
    real = {"existing_identity": None, "existing_joint": None, "new": dict(samples=0, identity_correct=0, joint_correct=0)}
    rows = []
    for name in run.section("blessing")["real_review_runs"]:
        rows += load_real(run, name, trainer.groups, candidate_metadata["entries"])
    if rows:
        images = np.stack([row["image"].transpose(2, 0, 1) for row in rows]).astype(np.float32) / 255
        slots = [row["slot"] for row in rows]
        after = candidate.predict(images, slots)
        known_groups = set(baseline.groups)
        index = [i for i, row in enumerate(rows) if (row["item"]["name"], row["item"]["slot"]) in known_groups]
        before = baseline.predict(images[index], [slots[i] for i in index]) if index else []
        truth = [rows[i]["item"] for i in index]
        real["existing_identity"] = paired(truth, before, [after[i] for i in index], identity_matches)
        coloured = [position for position, i in enumerate(index) if rows[i]["colour"] >= 0]
        real["existing_joint"] = paired([truth[p] for p in coloured], [before[p] for p in coloured],
                                        [after[index[p]] for p in coloured], joint_matches)
        gate.regression("identity_real_existing", real["existing_identity"])
        gate.regression("identity_real_existing_colour", real["existing_joint"])
        for i, row in enumerate(rows):
            if (row["item"]["name"], row["item"]["slot"]) not in known_groups:
                real["new"]["samples"] += 1
                real["new"]["identity_correct"] += identity_matches(row["item"], after[i])
                real["new"]["joint_correct"] += int(row["colour"] >= 0 and joint_matches(row["item"], after[i]))
    report["identity"]["real"] = real
    report["manual"] = []
    if catalogue.new:
        report["manual"].append("resource/image/blessing/index.json holds public Wiki icons used by the template fallback; "
                                "add the new icons from a public source when available. Game exports must not enter the repository.")
    return catalogue


class Attributes:
    def __init__(self, run, catalogue, entries):
        import torch
        self.torch = torch
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        icons, layers = catalogue.attribute_layers(entries)
        to_tensor = lambda value: (torch.tensor(value, device=self.device).permute(0, 3, 1, 2).float() if value.ndim == 4
                                   else torch.tensor(value, device=self.device).permute(2, 0, 1).float()[None])
        self.icons = to_tensor(icons)
        self.layers = {key: to_tensor(value) for key, value in layers.items()}
        catalog = read_json(run.resolved("resource/data/blessing_catalog.json"))
        originals = {(row["name"], row["slot"]): row["color"] for row in catalogue.originals}
        special = {row["name"] for row in catalog["sets"] if row.get("lock_mutable") is False}
        self.rarity = torch.tensor([row["rarity"] - 3 for row in entries], device=self.device)
        self.maximum = torch.tensor([catalog["upgrade_costs"][str(row["rarity"])]["max_level"] for row in entries], device=self.device)
        self.original = torch.tensor([row["color"] == originals.get((row["name"], row["slot"])) for row in entries], device=self.device)
        self.special = torch.tensor([row["name"] in special for row in entries], device=self.device)
        scale = lambda size, output, start: self._area(size, output, start)
        self.area_y, self.area_x = scale(160, 101, 5.5), scale(150, 95, 5.)

    def _area(self, size, output, start):
        torch = self.torch
        left = torch.arange(output, device=self.device, dtype=torch.float32) * 1.5 + start
        source = torch.arange(size, device=self.device, dtype=torch.float32)
        return (torch.minimum(left[:, None] + 1.5, source[None] + 1) - torch.maximum(left[:, None], source[None])).clamp(min=0) / 1.5

    @staticmethod
    def over(front, back):
        return Attributes._cat(front[:, :3] + back[:, :3] * (1 - front[:, 3:4]), front[:, 3:4] + back[:, 3:4] * (1 - front[:, 3:4]))

    @staticmethod
    def _cat(rgb, alpha):
        import torch
        return torch.cat((rgb, alpha), dim=1)

    def cards(self, ids):
        """Same state rules as training: recolours sit at maximum level; specials are locked."""
        torch, device = self.torch, self.device
        n = len(ids)
        states = torch.randint(3, (n,), device=device)
        states[self.special[ids]] = 0
        maximum = self.maximum[ids]
        levels = torch.floor(torch.rand(n, device=device) * (maximum + 1)).long()
        levels[~self.original[ids]] = maximum[~self.original[ids]]
        levels[self.special[ids]] = maximum[self.special[ids]]
        stack = states == 1
        levels[stack] = torch.where(self.original[ids][stack] & (torch.rand(int(stack.sum()), device=device) < .5), 0, maximum[stack])
        image = self.over(self.icons[ids], self.layers["backgrounds"][torch.randint(2, (n,), device=device)])
        image = self.over(self.layers["stars"][self.rarity[ids]], image)
        image = self.over(self.layers["levels"][levels], image)
        locked = self.over(self.layers["lock"].expand(n, -1, -1, -1), image)
        stacked = self.over(self.layers["count_digits"][torch.randint(2, (n,), device=device)],
                            self.over(self.layers["count_back"].expand(n, -1, -1, -1), image))
        image = torch.where((states == 0)[:, None, None, None], locked, torch.where(stack[:, None, None, None], stacked, image))
        context = self.torch.nn.functional.interpolate(torch.rand(n, 3, 2, 2, device=device) * .16 + .07, size=(160, 150),
                                                       mode="bilinear", align_corners=False)
        image = image[:, :3] + context * (1 - image[:, 3:4])
        image = torch.matmul(torch.matmul(self.area_y, image), self.area_x.T)
        pixels = (image.clamp(0, 1) * 255).round().to(torch.uint8).permute(0, 2, 3, 1).cpu().numpy()
        return pixels, levels.cpu().numpy(), states.cpu().numpy()


def forward_level(net, cards, maximum):
    patches = np.ascontiguousarray(cards[:, 75:96, 29:59].transpose(0, 3, 1, 2), dtype=np.float32) / 255
    output = []
    for start in range(0, len(patches), 128):
        net.setInput(patches[start:start + 128])
        output.append(net.forward())
    logits = np.concatenate(output)
    return np.array([row[:limit + 1].argmax() for row, limit in zip(logits, maximum)])


def forward_badge(net, cards, metadata):
    patches = np.ascontiguousarray(cards[:, 63:96, 60:94].transpose(0, 3, 1, 2), dtype=np.float32) / 255
    logits, vectors = [], []
    for start in range(0, len(patches), 128):
        net.setInput(patches[start:start + 128])
        a, b = net.forward(["logits", "embedding"])
        logits.append(a)
        vectors.append(b)
    logits, vectors = np.concatenate(logits), np.concatenate(vectors)
    predicted = logits.argmax(1)
    centres, radii = np.asarray(metadata["centres"]), np.asarray(metadata["radii"])
    return predicted, np.linalg.norm(vectors - centres[predicted], axis=1) <= radii[predicted]


def evaluate_attributes(run, catalogue, gate, report, samples=2048):
    entries = read_json(run.resolved("resource/model/blessing_identity.json"))["entries"]
    attributes = Attributes(run, catalogue, entries)
    torch = attributes.torch
    torch.manual_seed(1009)
    np.random.seed(1009)
    old = [i for i, row in enumerate(entries) if key_of(row) in catalogue.baseline_keys]
    new = [i for i, row in enumerate(entries) if key_of(row) not in catalogue.baseline_keys]
    level_baseline = Net(run.root / "resource/model/blessing_level.onnx")
    level_candidate = Net(run.resolved("resource/model/blessing_level.onnx"))
    badge_baseline = Net(run.root / "resource/model/blessing_badge.onnx")
    badge_candidate = Net(run.resolved("resource/model/blessing_badge.onnx"))
    badge_old_meta = read_json(run.root / "resource/model/blessing_badge.json")
    badge_new_meta = read_json(run.resolved("resource/model/blessing_badge.json"))
    results = {}
    cases = []
    for name, pool in (("existing_icons", old), ("new_icons", new)):
        if not pool:
            results[name] = None
            continue
        ids = torch.tensor(pool, device=attributes.device)[torch.randint(len(pool), (samples,), device=attributes.device)]
        cards, levels, states = [], [], []
        for start in range(0, samples, 96):
            with torch.no_grad():
                a, b, c = attributes.cards(ids[start:start + 96])
            cards.append(a)
            levels.append(b)
            states.append(c)
        cards, levels, states = np.concatenate(cards), np.concatenate(levels), np.concatenate(states)
        maximum = attributes.maximum[ids].cpu().numpy()
        level_mask = attributes.original[ids].cpu().numpy() & ~attributes.special[ids].cpu().numpy()
        level_now = forward_level(level_candidate, cards[level_mask], maximum[level_mask])
        level_was = forward_level(level_baseline, cards[level_mask], maximum[level_mask])
        badge_now, accepted_now = forward_badge(badge_candidate, cards, badge_new_meta)
        badge_was, accepted_was = forward_badge(badge_baseline, cards, badge_old_meta)
        expected_level = levels[level_mask]
        results[name] = {
            "level": paired(expected_level.tolist(), level_was.tolist(), level_now.tolist(), lambda a, b: a == b),
            "badge": paired(states.tolist(), badge_was.tolist(), badge_now.tolist(), lambda a, b: a == b),
            "badge_envelope_accepted": {"baseline": int(accepted_was.sum()), "candidate": int(accepted_now.sum()), "samples": samples}}
        if name == "new_icons" or not new:
            cases_dir = run.evaluation / "blessing-cards"
            cases_dir.mkdir(parents=True, exist_ok=True)
            for index in range(min(24, samples)):
                entry = entries[int(ids[index])]
                path = cases_dir / f"{name}-{index:02d}.png"
                Image.fromarray(cards[index]).save(path)
                cases.append({"image": str(path), "slot": entry["slot"], "name": entry["name"], "color": entry["color"]})
    report["attributes"] = results
    for name, row in results.items():
        if row is None:
            continue
        gate.regression(f"level_{name}", row["level"])
        gate.regression(f"badge_{name}", row["badge"])
    if results["new_icons"]:
        for kind in ("level", "badge"):
            new_row, old_row = results["new_icons"][kind], results["existing_icons"][kind]
            gate.new_classes(f"{kind}_new_icons_vs_existing",
                             {"samples": new_row["samples"], "correct": new_row["candidate_correct"]},
                             {"samples": old_row["samples"], "correct": old_row["candidate_correct"]})
    write_json(run.evaluation / "blessing-probe-cases.json", cases)


def evaluate(run, waivers=None):
    gate = Gate("blessing")
    report = {}
    catalogue = evaluate_identity(run, gate, report)
    evaluate_attributes(run, catalogue, gate, report)
    result = gate.result(waivers)
    write_json(run.evaluation / "blessing.json", report)
    write_json(run.evaluation / "blessing-gate.json", result)
    run.mark("evaluate_blessing", status=result["status"], blocking=result["blocking"])
    log("evaluate_blessing", status=result["status"], blocking=result["blocking"],
        identity={key: value for key, value in report["identity"].items() if key != "synthetic_new_by_class"},
        attributes=report["attributes"])
    return result


def retrain_attributes(run, kind):
    """Delegates to private layer tooling, since new card UI needs a fresh capture."""
    catalogue = Catalogue(run)
    command = run.section("blessing").get("attribute_retrain_command")
    if not command:
        raise SystemExit("blessing.attribute_retrain_command is not configured")
    work = run.dir / "attributes"
    work.mkdir(parents=True, exist_ok=True)
    records = list(catalogue.icon_map["records"])
    originals = list(catalogue.icon_map["original_colours"])
    for row in catalogue.new:
        entry = {key: row[key] for key in ("file", "series", "variant", "name", "slot", "color", "rarity")}
        records.append(entry)
        if row.get("original"):
            originals.append(entry)
    icon_map = work / "source-icon-map.json"
    write_json(icon_map, {**catalogue.icon_map, "records": records, "original_colours": originals})
    catalog_path = run.resolved("resource/data/blessing_catalog.json")
    values = {"python": sys.executable, "work": str(work), "icon_map": str(icon_map), "kind": kind, "catalog": str(catalog_path)}
    subprocess.run([part.format(**values) for part in command], check=True, cwd=run.path("."))
    for suffix in ("onnx", "json"):
        run.link_candidate(f"resource/model/blessing_{kind}.{suffix}", work / f"blessing_{kind}.{suffix}")
    run.mark(f"train_{kind}", work=str(work))
