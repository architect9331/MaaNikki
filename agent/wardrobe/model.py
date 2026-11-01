"""Wardrobe identity embeddings and known-part classification."""
from __future__ import annotations

import hashlib
import json
import re

import cv2
import numpy as np


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
