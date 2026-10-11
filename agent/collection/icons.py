"""Batch Eureka icon matching against versioned public Wiki images."""
from __future__ import annotations

import json

import cv2
import numpy as np


class IconMatcher:
    SIZE = 40

    def __init__(self, directory, catalog, *, threshold=.72, margin=.035,
                 color_threshold=.80, color_margin=.025):
        index = json.loads((directory / "index.json").read_text(encoding="utf-8"))
        definitions = {definition["name"]: definition for definition in catalog["sets"]}
        self.entries = index["images"]
        self.threshold, self.margin = threshold, margin
        self.color_threshold, self.color_margin = color_threshold, color_margin
        self.maximum_levels = {int(rarity): costs["max_level"]
                               for rarity, costs in catalog["upgrade_costs"].items()}
        yy, xx = np.mgrid[:self.SIZE, :self.SIZE] / self.SIZE
        # Exclude rarity bars, levels, border and star overlays.
        self.mask = ((xx > .07) & (xx < .94) & (yy > .14) & (yy < .79)).astype(np.float32)
        descriptors, shapes, colors, alphas, grouped = [], [], [], [], {}
        for index, entry in enumerate(self.entries):
            definition = definitions[entry["name"]]
            if (entry["format"] != "icon" or entry["color"] not in definition["colors"]
                    or entry["rarity"] != definition["rarity"]):
                raise ValueError("图标索引与祝福闪光图鉴不一致，或不是透明原图")
            image = cv2.imdecode(np.fromfile(directory / entry["file"], np.uint8), cv2.IMREAD_UNCHANGED)
            if image is None or image.ndim != 3 or image.shape[2] != 4:
                raise ValueError(f"祝福闪光透明图标无法读取：{entry['file']}")
            # Original game textures occupy a square at the top of a 147x156
            # inventory cell; only public source images enter this catalogue.
            icon = cv2.resize(image, (147, 147), interpolation=cv2.INTER_AREA)
            canvas = np.zeros((156, 147, 3), np.uint8)
            alpha = icon[:, :, 3:4].astype(np.float32) / 255
            canvas[:147] = (icon[:, :, :3] * alpha).astype(np.uint8)
            descriptors.append(self.descriptor(canvas))
            shapes.append(self.shape_descriptor(canvas))
            colors.append(cv2.resize(canvas, (self.SIZE, self.SIZE), interpolation=cv2.INTER_AREA)
                          .astype(np.float32) / 255)
            opacity = np.zeros((156, 147), np.float32)
            opacity[:147] = alpha[:, :, 0]
            alphas.append(cv2.resize(opacity, (self.SIZE, self.SIZE), interpolation=cv2.INTER_AREA))
            grouped.setdefault((entry["name"], entry["slot"]), []).append(index)
        self.templates = np.ascontiguousarray(descriptors, dtype=np.float32)
        self.shape_templates = np.ascontiguousarray(shapes, dtype=np.float32)
        self.color_templates = np.asarray(colors, np.float32)
        self.alphas = np.asarray(alphas, np.float32)
        self.groups = list(grouped.values())
        self.group_lookup = {key: position for position, key in enumerate(grouped)}
        longest = max(map(len, self.groups))
        self.group_indices = np.asarray([group + [group[-1]] * (longest - len(group))
                                         for group in self.groups], np.int32)
        self.background_plane = np.stack((np.ones_like(xx), xx - .5, yy - .5), axis=-1).astype(np.float32)
        self.color_models = []
        for group in self.groups:
            opacity = self.alphas[group].max(axis=0)
            exterior = (opacity < .08) & (self.mask > 0)
            references = self.color_templates[group]
            saturation = (references.max(axis=-1) - references.min(axis=-1)) / np.maximum(
                references.max(axis=-1), .02)
            weights = self.mask * opacity ** 2 * (opacity > .2) * (.3 + saturation.max(axis=0))
            projector = (np.linalg.pinv(self.background_plane[exterior]).astype(np.float32)
                         if np.count_nonzero(exterior) >= 16 else None)
            variation = np.var(self.chroma(references), axis=0).sum(axis=-1)
            # Shared white highlights carry little evidence about a recolour.
            # Learn the informative regions from all public variants equally.
            distinctive = weights * (.05 + variation)
            self.color_models.append((exterior, projector, weights, distinctive))
        expected = {(d["name"], slot, color) for d in catalog["sets"]
                    for slot in ("头部", "手部", "脚部") for color in d["colors"]}
        actual = {(entry["name"], entry["slot"], entry["color"]) for entry in self.entries}
        if expected != actual or len(actual) != len(self.entries):
            raise ValueError("Wiki 图标索引不完整或包含重复项")

    def descriptor(self, image):
        image = cv2.resize(image, (self.SIZE, self.SIZE), interpolation=cv2.INTER_AREA).astype(np.float32) / 255
        # Local contrast removes the scene-dependent translucent background.
        # Keep colour channels: grayscale alone confuses recolour variants.
        contrast = image - cv2.GaussianBlur(image, (0, 0), 3)
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        dx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3) / 8
        dy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3) / 8
        values = np.concatenate((contrast, dx[:, :, None], dy[:, :, None]), axis=2)
        values *= self.mask[:, :, None]
        vector = values.ravel()
        return vector / max(float(np.linalg.norm(vector)), 1e-6)

    @staticmethod
    def chroma(image):
        # Preserve broad colour fields while removing overall brightness.
        # Shape matching's high-pass filter must not be used for this stage.
        mean = image.mean(axis=-1, keepdims=True)
        return (image - mean) / np.maximum(mean, .08)

    def shape_descriptor(self, image):
        image = cv2.resize(image, (self.SIZE, self.SIZE), interpolation=cv2.INTER_AREA).astype(np.float32) / 255
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        gray -= cv2.GaussianBlur(gray, (0, 0), 3)
        dx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3) / 8
        dy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3) / 8
        vector = (np.stack((gray, dx, dy), axis=-1) * self.mask[:, :, None]).ravel()
        return vector / max(float(np.linalg.norm(vector)), 1e-6)

    def rank_identity(self, row):
        scores = row[self.group_indices].max(axis=1)
        first, second = map(int, np.argsort(scores)[-2:][::-1])
        group, competing = self.groups[first], self.groups[second]
        return {"group": first, "best": group[int(np.argmax(row[group]))],
                "second": competing[int(np.argmax(row[competing]))],
                "score": float(scores[first]), "margin": float(scores[first] - scores[second])}

    def identity_agrees(self, color, shape):
        return (color["group"] == shape["group"] and color["score"] >= self.threshold
                and shape["score"] >= self.threshold
                and min(color["margin"], shape["margin"]) >= self.margin)

    def align_identity(self, crop, original_color, original_shape):
        # Bounded refinement for uncertain cells only; no account-specific
        # templates, and every transform competes against the entire bank.
        image = cv2.resize(crop, (self.SIZE, self.SIZE), interpolation=cv2.INTER_AREA)
        variants, transforms = [], []
        for scale in (.95, 1.0, 1.05):
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    matrix = cv2.getRotationMatrix2D(((self.SIZE - 1) / 2,) * 2, 0, scale)
                    matrix[:, 2] += [dx, dy]
                    variants.append(cv2.warpAffine(image, matrix, (self.SIZE, self.SIZE),
                                                   flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT_101))
                    transforms.append({"scale": scale, "dx": dx, "dy": dy})
        colors = np.asarray([self.descriptor(variant) for variant in variants]) @ self.templates.T
        shapes = np.asarray([self.shape_descriptor(variant) for variant in variants]) @ self.shape_templates.T
        choices = []
        for index, (color_row, shape_row) in enumerate(zip(colors, shapes)):
            color, shape = self.rank_identity(color_row), self.rank_identity(shape_row)
            if self.identity_agrees(color, shape):
                choices.append((min(color["score"], shape["score"]), index, color, shape))
        if not choices:
            return crop, original_color, original_shape, None
        _, index, color, shape = max(choices, key=lambda choice: choice[0])
        return variants[index], color, shape, transforms[index]

    def fingerprint(self, frame, box, group_index):
        x, y, w, h = box
        crop = frame[y:y + h, x:x + w]
        image = cv2.resize(crop, (self.SIZE, self.SIZE), interpolation=cv2.INTER_AREA).astype(np.float32) / 255
        exterior, projector, weights, _ = self.color_models[group_index]
        if projector is None:
            return None
        background = np.clip(self.background_plane @ (projector @ image[exterior]), 0, 1)
        alpha = self.alphas[self.groups[group_index]].max(axis=0)
        observed = np.clip(image - (1 - alpha[:, :, None]) * background, 0, 1)
        color = (self.chroma(observed) * np.sqrt(weights[:, :, None]
                 / max(float(weights.sum() * 3), 1e-6))).ravel()
        return self.shape_descriptor(crop), color

    def match_color(self, image, group_index, level, maximum):
        group = self.groups[group_index]
        indices = [index for index in group
                   if self.entries[index]["color"] != "彩" or level == maximum]
        if not indices:
            return None, {"accepted": False, "reason": "当前等级没有可匹配的颜色"}
        exterior, projector, weights, distinctive = self.color_models[group_index]
        if projector is None or np.count_nonzero(weights) < 16:
            return None, {"accepted": False, "reason": "图标本体或背景样本不足"}
        # Estimate the scene through transparent pixels; composite it out with
        # each public texture's alpha instead of suppressing the icon's colour.
        coefficients = projector @ image[exterior]
        background = np.clip(self.background_plane @ coefficients, 0, 1)
        observed = np.clip(image[None] - (1 - self.alphas[indices, :, :, None]) * background, 0, 1)
        difference = self.chroma(observed) - self.chroma(self.color_templates[indices])
        distances = np.sqrt((difference ** 2 * weights[None, :, :, None]).sum(axis=(1, 2, 3))
                            / max(float(weights.sum() * 3), 1e-6))
        scores = np.clip(1 - distances, 0, 1)
        order = np.argsort(scores)[::-1]
        best = int(order[0])
        score = float(scores[best])
        gap = float(score - scores[order[1]]) if len(order) > 1 else 1.0
        distinct_distances = np.sqrt((difference ** 2 * distinctive[None, :, :, None]).sum(axis=(1, 2, 3))
                                    / max(float(distinctive.sum() * 3), 1e-6))
        distinct_scores = np.clip(1 - distinct_distances, 0, 1)
        distinct_order = np.argsort(distinct_scores)[::-1]
        distinct_best = int(distinct_order[0])
        distinct_score = float(distinct_scores[distinct_best])
        distinct_gap = float(distinct_score - distinct_scores[distinct_order[1]]) if len(order) > 1 else 1.0
        accepted = bool(best == distinct_best and min(score, distinct_score) >= self.color_threshold
                        and max(gap, distinct_gap) >= self.color_margin)
        return indices[best], {"accepted": accepted, "score": round(score, 4), "margin": round(gap, 4),
                               "candidate": self.entries[indices[best]]["color"],
                               "runner_up": self.entries[indices[int(order[1])]]["color"] if len(order) > 1 else None,
                               "scores": {self.entries[index]["color"]: round(float(value), 4)
                                          for index, value in zip(indices, scores)},
                               "distinctive_scores": {self.entries[index]["color"]: round(float(value), 4)
                                                      for index, value in zip(indices, distinct_scores)},
                               "distinctive_margin": round(distinct_gap, 4),
                               "reason": "颜色已确认" if accepted else "颜色相近或本体颜色不一致"}

    def match_batch(self, frame, boxes, *, levels=None):
        if not boxes:
            return []
        crops = [frame[y:y + h, x:x + w] for x, y, w, h in boxes]
        descriptors = [self.descriptor(crop) for crop in crops]
        scores = np.asarray(descriptors, np.float32) @ self.templates.T
        shape_scores = np.asarray([self.shape_descriptor(crop) for crop in crops]) @ self.shape_templates.T
        results = []
        for position, row in enumerate(scores):
            # Compete only with other set/slot identities. A recolour of the
            # same part can never invalidate an otherwise certain identity.
            color_rank = self.rank_identity(row)
            shape_rank = self.rank_identity(shape_scores[position])
            crop, alignment = crops[position], None
            if not self.identity_agrees(color_rank, shape_rank):
                crop, color_rank, shape_rank, alignment = self.align_identity(crop, color_rank, shape_rank)
            group_index, best, second = color_rank["group"], color_rank["best"], color_rank["second"]
            score, gap = color_rank["score"], color_rank["margin"]
            identity_accepted = self.identity_agrees(color_rank, shape_rank)
            color_match = {"accepted": False, "reason": "部件未确认"}
            if identity_accepted:
                image = cv2.resize(crop, (self.SIZE, self.SIZE), interpolation=cv2.INTER_AREA)
                image = image.astype(np.float32) / 255
                level = levels[position] if levels is not None else None
                maximum = self.maximum_levels[self.entries[best]["rarity"]]
                color_best, color_match = self.match_color(image, group_index, level, maximum)
                if color_best is not None:
                    best = color_best
            entry = self.entries[best]
            results.append({"accepted": bool(identity_accepted and color_match["accepted"]),
                            "identity_accepted": identity_accepted, "color_accepted": color_match["accepted"],
                            "score": round(score, 4), "margin": round(gap, 4),
                            "candidate": {key: entry[key] for key in ("name", "slot", "color", "rarity")},
                            "runner_up": {key: self.entries[second][key] for key in ("name", "slot", "color")},
                            "template": entry["file"], "source_format": entry["format"],
                            "shape_match": {"score": round(shape_rank["score"], 4),
                                            "margin": round(shape_rank["margin"], 4),
                                            "agrees": color_rank["group"] == shape_rank["group"]},
                            "alignment": alignment,
                            "color_match": color_match})
        return results
