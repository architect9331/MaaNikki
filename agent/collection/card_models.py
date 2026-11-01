"""Page-batched Eureka identity, colour, level and badge inference on CPU."""
from __future__ import annotations

import json
from time import perf_counter

import cv2
import numpy as np


def badge_roi(box):
    x, y, w, h = box
    return [x + round(w * 60 / 95), y + round(h * 63 / 101),
            round(w * 34 / 95), round(h * 33 / 101)]


class CardModels:
    def __init__(self, directory, catalog):
        self.metadata = {}
        self.nets = {}
        for kind in ("identity", "badge", "level"):
            self.metadata[kind] = json.loads((directory / f"blessing_{kind}.json").read_text(encoding="utf-8"))
            if self.metadata[kind].get("schema_version") != 1:
                raise ValueError(f"祝福闪光{kind}模型元数据版本无效")
            net = cv2.dnn.readNetFromONNX(np.fromfile(directory / f"blessing_{kind}.onnx", np.uint8))
            net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
            net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
            self.nets[kind] = net
        identity = self.metadata["identity"]
        self.entries = identity["entries"]
        self.groups = [tuple(group) for group in identity["groups"]]
        lookup = {key: i for i, key in enumerate(self.groups)}
        self.class_groups = np.array([lookup[(r["name"], r["slot"])] for r in self.entries])
        self.original = {(r["name"], r["slot"]): r["color"] for r in identity["original_colours"]}
        expected = {(r["name"], slot, color, r["rarity"]) for r in catalog["sets"]
                    for slot in ("头部", "手部", "脚部") for color in r["colors"]}
        actual = {(r["name"], r["slot"], r["color"], r["rarity"]) for r in self.entries}
        if expected != actual or len(actual) != len(self.entries) or set(self.groups) != set(self.original):
            raise ValueError("祝福闪光模型标签与当前图鉴不一致")
        self.maximum = {int(rarity): costs["max_level"] for rarity, costs in catalog["upgrade_costs"].items()}
        badge = self.metadata["badge"]
        if badge["classes"] != ["locked", "stack", "single"] or self.metadata["level"]["classes"] != list(range(11)):
            raise ValueError("祝福闪光等级或角标类别顺序无效")
        self.centres = np.asarray(badge["centres"], np.float32)
        self.radii = np.asarray(badge["radii"], np.float32)
        if (self.centres.shape != (3, 32) or self.radii.shape != (3,)
                or not np.isfinite(self.centres).all() or not np.isfinite(self.radii).all()
                or np.any(self.radii <= 0)):
            raise ValueError("祝福闪光角标特征范围无效")
        self.last_timing = {}

    @staticmethod
    def cards(frame, boxes):
        cards = []
        for x, y, w, h in boxes:
            if (w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > frame.shape[1] or y + h > frame.shape[0]):
                raise ValueError("祝福闪光模型输入格子不完整")
            patch = frame[y:y + h, x:x + w]
            if patch.dtype != np.uint8 or patch.ndim != 3 or patch.shape[2] != 3:
                raise ValueError("祝福闪光模型需要完整彩色截图")
            cards.append(cv2.resize(patch, (95, 101), interpolation=cv2.INTER_AREA))
        return cards

    @staticmethod
    def blob(patches):
        return np.ascontiguousarray(np.stack(patches)[:, :, :, ::-1].transpose(0, 3, 1, 2), dtype=np.float32) / 255

    def forward(self, kind, patches, outputs):
        self.nets[kind].setInput(self.blob(patches))
        values = self.nets[kind].forward(outputs)
        if any(not np.isfinite(value).all() or len(value) != len(patches) for value in values):
            raise ValueError(f"祝福闪光{kind}模型输出无效")
        return values

    def badges(self, frame, boxes, *, cards=None):
        if not boxes:
            return []
        cards = self.cards(frame, boxes) if cards is None else cards
        logits, embeddings = self.forward("badge", [card[63:96, 60:94] for card in cards], ["logits", "embedding"])
        if logits.shape != (len(boxes), 3) or embeddings.shape != (len(boxes), 32):
            raise ValueError("祝福闪光角标输出尺寸无效")
        results = []
        for box, scores, vector in zip(boxes, logits, embeddings):
            index = int(scores.argmax())
            distance = float(np.linalg.norm(vector - self.centres[index]))
            reason = None if distance <= float(self.radii[index]) else "outside_training_envelope"
            name = self.metadata["badge"]["classes"][index]
            results.append({"classifier": "cropped_rgb_cnn", "box": list(box), "roi": badge_roi(box),
                            "model_version": self.metadata["badge"]["model_version"],
                            "predicted": name, "badge_state": name if reason is None else None,
                            "scores": {label: float(score) for label, score in zip(self.metadata["badge"]["classes"], scores)},
                            "feature_distance": distance, "feature_radius": float(self.radii[index]), "reason": reason})
        return results

    def match_batch(self, frame, boxes, slot):
        if not boxes:
            self.last_timing = {"cards": 0, "seconds": 0.0}
            return []
        eligible = np.array([i for i, (_, part) in enumerate(self.groups) if part == slot])
        if not len(eligible):
            raise ValueError("祝福闪光模型缺少当前部位")
        started = perf_counter()
        cards = self.cards(frame, boxes)
        patches = [cv2.resize(card[14:80, 7:89], (96, 96), interpolation=cv2.INTER_AREA) for card in cards]
        identities, colours = self.forward("identity", patches, ["identity_logits", "colour_logits"])
        if identities.shape != (len(boxes), len(self.groups)) or colours.shape != (len(boxes), len(self.entries)):
            raise ValueError("祝福闪光种类或颜色输出尺寸无效")
        identity_time = perf_counter() - started
        chosen = []
        originals = []
        for i, (identity, colour) in enumerate(zip(identities, colours)):
            group = int(eligible[identity[eligible].argmax()])
            indices = np.flatnonzero(self.class_groups == group)
            index = int(indices[colour[indices].argmax()])
            candidate = self.entries[index]
            chosen.append((group, index, candidate))
            if candidate["color"] == self.original[self.groups[group]]:
                originals.append(i)
        levels = {}
        level_started = perf_counter()
        if originals:
            outputs, = self.forward("level", [cards[i][75:96, 29:59] for i in originals], ["level_logits"])
            if outputs.shape != (len(originals), 11):
                raise ValueError("祝福闪光等级输出尺寸无效")
            for i, output in zip(originals, outputs):
                # Three-star Eureka cap at five. Restrict by the catalogue;
                # recolours themselves already establish maximum level.
                levels[i] = int(output[:self.maximum[chosen[i][2]["rarity"]] + 1].argmax())
        level_time = perf_counter() - level_started
        badge_started = perf_counter()
        badges = self.badges(frame, boxes, cards=cards)
        results = []
        for i, ((group, index, candidate), badge) in enumerate(zip(chosen, badges)):
            maximum = self.maximum[candidate["rarity"]]
            facts = {**candidate, "level": levels.get(i, maximum)}
            ranked = eligible[np.argsort(identities[i, eligible])[::-1]]
            indices = np.flatnonzero(self.class_groups == group)
            colour_ranked = indices[np.argsort(colours[i, indices])[::-1]]
            evidence = {"source": "page_card_models", "level_source": "numeral_model" if i in levels else "recolour_maximum",
                        "identity_logit_gap": float(identities[i, ranked[0]] - identities[i, ranked[1]]) if len(ranked) > 1 else None,
                        "colour_logit_gap": float(colours[i, colour_ranked[0]] - colours[i, colour_ranked[1]]) if len(colour_ranked) > 1 else None,
                        "model_versions": {key: value["model_version"] for key, value in self.metadata.items()}}
            results.append({"accepted": True, "identity_accepted": True, "color_accepted": True,
                            "candidate": dict(candidate), "model_facts": facts, "model_evidence": evidence,
                            "badge_classification": badge, "template": str(index), "source_format": "model"})
        self.last_timing = {"cards": len(boxes), "level_cards": len(originals),
                            "identity_seconds": round(identity_time, 6), "level_seconds": round(level_time, 6),
                            "badge_seconds": round(perf_counter() - badge_started, 6),
                            "seconds": round(perf_counter() - started, 6)}
        return results
