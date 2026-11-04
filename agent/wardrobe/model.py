"""Wardrobe identity catalog and legacy model bindings."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re

import cv2
import numpy as np


def _clean_name(value: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]", "", str(value or "")).replace("・", "·")


class WardrobeCatalog:
    """Fast Compendium name lookup backed by deployed wardrobe labels and scoring metadata."""

    def __init__(self, directory, scoring_path=None):
        contract = json.loads((directory / "model-contract.json").read_text(encoding="utf-8"))
        self.version = contract["version"]
        self.fingerprint = contract["label_fingerprint"]
        self.labels = json.loads((directory / "labels.json").read_text(encoding="utf-8"))
        self.parts = json.loads((directory / "parts.json").read_text(encoding="utf-8"))
        if scoring_path is None:
            scoring_path = directory.parents[1] / "data/wardrobe_scoring.json"
        scoring = json.loads(scoring_path.read_text(encoding="utf-8")) if scoring_path.is_file() else {}
        self.game_items = scoring.get("game_items") or {}
        self.by_part_name: dict[str, dict[str, dict]] = {}
        self.by_item_id: dict[int, dict] = {}
        self._build_index()

    def _build_index(self):
        class_by_part_item: dict[tuple[str, int], dict] = {}
        for part, entries in self.labels.items():
            for entry in entries:
                for iid in entry.get("item_ids", []):
                    class_by_part_item[(str(part), int(iid))] = entry

        for part in self.parts:
            by_cname: dict[str, dict] = {}
            part_game_items = [
                (gid, g) for gid, g in self.game_items.items()
                if str(g.get("part")) == str(part)
            ]
            part_game_items.sort(key=lambda kv: (kv[0] != kv[1].get("group"), int(kv[0])))
            for gid, g in part_game_items:
                iid = int(gid)
                cname = _clean_name(g.get("name", ""))
                if not cname:
                    continue
                label_entry = class_by_part_item.get((str(part), iid))
                class_id = int(label_entry["class_id"]) if label_entry else -1
                if cname not in by_cname:
                    rec = {
                        "class_id": class_id,
                        "item_ids": [iid],
                        "names": [g["name"]],
                        "qualities": [int(g.get("quality", 3))],
                    }
                    by_cname[cname] = rec
                else:
                    first_gid = str(by_cname[cname]["item_ids"][0])
                    first_g = self.game_items.get(first_gid, {})
                    if ((g.get("score_id"), g.get("group"), g.get("glow_reward"))
                            == (first_g.get("score_id"), first_g.get("group"), first_g.get("glow_reward"))):
                        if iid not in by_cname[cname]["item_ids"]:
                            by_cname[cname]["item_ids"].append(iid)
                self.by_item_id[iid] = by_cname[cname]

            for entry in self.labels.get(str(part), []):
                for name in entry.get("names", []):
                    cname = _clean_name(name)
                    if not cname:
                        continue
                    if cname not in by_cname:
                        rec = {
                            "class_id": int(entry["class_id"]),
                            "item_ids": list(entry["item_ids"]),
                            "names": [name],
                            "qualities": list(entry["qualities"]),
                        }
                        by_cname[cname] = rec
                    for iid in entry.get("item_ids", []):
                        self.by_item_id.setdefault(int(iid), by_cname[cname])

            self.by_part_name[str(part)] = by_cname

    def lookup(self, part: str, raw_name: str) -> dict | None:
        part = str(part)
        cname = _clean_name(raw_name)
        if not cname:
            return None
        table = self.by_part_name.get(part) or {}
        if cname in table:
            return {**deepcopy(table[cname]), "source": "compendium_ocr"}
        # Single-character OCR substitution recovery for names >= 3 chars when
        # strictly one same-length catalog entry in this part is within 1 char
        # and no other candidate is within 2 chars.
        if len(cname) >= 3:
            same_len = [
                (key, rec, sum(a != b for a, b in zip(cname, key)))
                for key, rec in table.items() if len(key) == len(cname)
            ]
            dist1 = [rec for _, rec, d in same_len if d == 1]
            dist2 = [rec for _, rec, d in same_len if d == 2]
            if len(dist1) == 1 and not dist2:
                return {**deepcopy(dist1[0]), "source": "compendium_ocr", "ocr_text": raw_name}
        return None

    def infer_suit_handhelds(self, parts_state: dict) -> list[dict]:
        """Infer base ability suit handhelds (TbClothesGallerySpecialAdd) when all sibling suit pieces are owned."""
        owned_gids: set[str] = set()
        existing_278: set[str] = set()
        for part, state in parts_state.items():
            if not isinstance(state, dict):
                continue
            for item in state.get("items") or []:
                for iid in item.get("item_ids") or []:
                    owned_gids.add(str(iid))
                    if str(part) == "278":
                        existing_278.add(str(iid))
        added = []
        for gid, g in sorted(self.game_items.items(), key=lambda kv: int(kv[0])):
            if str(g.get("part")) != "278" or gid in existing_278:
                continue
            if len(gid) != 10 or not gid.startswith("102078"):
                continue
            suffix = gid[-4:]
            siblings = [
                other for other, og in self.game_items.items()
                if other != gid and len(other) == 10
                and other.startswith("1020") and other.endswith(suffix)
                and str(og.get("part")) not in ("278", "413")
            ]
            if len(siblings) >= 6 and all(s in owned_gids for s in siblings):
                rec = self.by_item_id.get(int(gid)) or {
                    "class_id": -1,
                    "item_ids": [int(gid)],
                    "names": [g["name"]],
                    "qualities": [int(g.get("quality", 4))],
                }
                added.append({**deepcopy(rec), "source": "compendium_suit_inferred"})
                existing_278.add(gid)
        return added


class WardrobeModel:
    def __init__(self, directory):
        import onnxruntime as ort
        from PIL import Image, ImageDraw
        self.Image, self.ImageDraw = Image, ImageDraw
        contract = json.loads((directory / "model-contract.json").read_text(encoding="utf-8"))
        for name, metadata in contract["files"].items():
            if hashlib.sha256((directory / name).read_bytes()).hexdigest() != metadata["sha256"]:
                raise ValueError(f"衣柜模型文件校验失败：{name}")
        self.version = contract["version"]
        self.fingerprint = contract["label_fingerprint"]
        self.labels = json.loads((directory / "labels.json").read_text(encoding="utf-8"))
        self.parts = json.loads((directory / "parts.json").read_text(encoding="utf-8"))
        self.preprocess = json.loads((directory / "preprocess.json").read_text(encoding="utf-8"))
        self.selection_templates = [cv2.imdecode(np.fromfile(directory.parents[1] / "image/wardrobe" / name, np.uint8), cv2.IMREAD_COLOR)
                                    for name in ("normal_corner.png", "selected_corner.png")]
        with np.load(directory / "heads.npz") as packed:
            self.heads = {key: packed[key] for key in packed.files}
        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        self.session = ort.InferenceSession(str(directory / "backbone.onnx"), sess_options=options,
                                           providers=["CPUExecutionProvider"])

    def prepare(self, card):
        image = self.Image.fromarray(cv2.cvtColor(card, cv2.COLOR_BGR2RGB))
        # Maa's 1280x720 frame is 2/3 of the source UI. Restore the card canvas
        # before applying the exact training mask; low-resolution recipes were
        # evaluated separately when selecting this model.
        image = image.resize(tuple(self.preprocess["card_size"]), self.Image.Resampling.BILINEAR)
        draw = self.ImageDraw.Draw(image)
        for rectangle in self.preprocess["mask_rectangles"]:
            draw.rectangle(rectangle, fill=tuple(self.preprocess["mask_fill"]))
        return np.asarray(image.resize(tuple(self.preprocess["model_input_size"]), self.Image.Resampling.BILINEAR))

    def predict(self, frame, boxes, part):
        if not boxes:
            return []
        cards = [self.prepare(frame[y:y+h, x:x+w]) for x, y, w, h in boxes]
        blob = np.ascontiguousarray(np.stack(cards).transpose(0, 3, 1, 2), dtype=np.float32) / 255
        features = self.session.run(["embedding"], {"images": blob})[0]
        scores = features @ self.heads[str(part)].T * self.heads["scale"]
        if not np.isfinite(scores).all():
            raise ValueError("衣柜模型输出无效")
        rows = []
        for score in scores:
            order = np.argsort(score)[-5:][::-1]
            candidates = [{"class_id": int(i), "item_ids": self.labels[str(part)][i]["item_ids"],
                           "names": self.labels[str(part)][i]["names"], "logit": round(float(score[i]), 6)} for i in order]
            label = self.labels[str(part)][int(order[0])]
            rows.append({"class_id": int(order[0]), "item_ids": label["item_ids"], "names": label["names"],
                         "qualities": label["qualities"], "candidates": candidates,
                         "cosine_similarity": round(float(score[order[0]] / self.heads["scale"]), 6),
                         "source": "card_model"})
        return rows

    def correct_selection(self, frame, boxes, predictions, part, header):
        """The gold selected card has a different background from the corpus.

        Compare its decorative corner with both supplied UI styles (outside
        the garment). Only a unique selected corner and an exact catalogued
        header name can override its model identity. No extra click is needed.
        """
        if not boxes or any(image is None for image in self.selection_templates):
            return
        selected = []
        for i, (x, y, w, h) in enumerate(boxes):
            card = cv2.resize(frame[y:y+h, x:x+w], (155, 161), interpolation=cv2.INTER_LINEAR)
            corner = card[3:30, 2:17].astype(np.float32)
            errors = [float(np.mean((corner - template.astype(np.float32))**2)) for template in self.selection_templates]
            if errors[1] < errors[0]:
                selected.append(i)
        if len(selected) != 1 or not header:
            for index in selected:
                predictions[index].update(source="selected_card_unconfirmed", header=header)
            return
        clean = lambda value: re.sub(r"[^\w\u4e00-\u9fff]", "", value)
        names = (self.parts[part]["name"], self.parts[part]["name"].split("·")[-1])
        candidates = [entry for entry in self.labels[part] if any(clean(header) == clean(prefix + name)
                      for prefix in names for name in entry["names"])]
        if len(candidates) != 1:
            predictions[selected[0]].update(source="selected_card_unconfirmed", header=header)
            return
        entry = candidates[0]
        predictions[selected[0]].update({key: entry[key] for key in ("class_id", "item_ids", "names", "qualities")},
                                        source="selected_header_ocr", header=header)
