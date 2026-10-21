"""Read game-provided tooltip facts and reuse verified visual observations."""
from __future__ import annotations

from collections import OrderedDict
import re

import numpy as np


HEADER = r".+[·.．・:：]?\s*(?:[头頭]部|手部|[脚腳]部)\s*$"
TITLE = re.compile(r"^(.+?)[·.．・:：]?\s*(头部|手部|脚部)$")
LEVEL = re.compile(r"(?:等级|[Ll][Vv]|级)[\s.．·:：]*(10|[0-9])(?=\D|$)")
COLORS = {color + "色": color for color in "蓝绿紫红粉黄白彩"}
COLORS.update({"彩虹色": "彩", "炫彩色": "彩"})
COLOR_OCR_THRESHOLD = .70
COLOR_DECORATIONS = "●○◯〇•·"
TEXT_NORMALIZATION = str.maketrans("頭腳", "头脚")


def tooltip_facts(results, catalog, *, threshold=.85, color_threshold=None, allow_protected=False):
    """Accept exact catalogue names and spatially adjacent tooltip fields."""
    color_threshold = threshold if color_threshold is None else color_threshold
    raw = []
    for item in results:
        text = re.sub(r"\s+", "", getattr(item, "text", "")).translate(TEXT_NORMALIZATION)
        score = float(getattr(item, "score", 0))
        if not text:
            continue
        box = list(item.box)
        raw.append({"text": text, "score": score, "box": box})
    lines = [line for line in raw if line["score"] >= threshold]
    rows = []
    for line in sorted(lines, key=lambda value: (value["box"][1], value["box"][0])):
        center = line["box"][1] + line["box"][3] / 2
        row = next((row for row in rows if abs(row["center"] - center) <= 10), None)
        if row is None:
            row = {"center": center, "lines": []}
            rows.append(row)
        row["lines"].append(line)
    definitions = {item["name"]: item for item in catalog["sets"]}
    titles = {}

    def add_title(parts):
        match = TITLE.fullmatch("".join(part["text"] for part in parts))
        if not match:
            return
        name = match.group(1).rstrip("·.．・:：")
        if name in definitions or allow_protected and 1 <= len(name) <= 64:
            key = (name, match.group(2), min(part["box"][0] for part in parts),
                   min(part["box"][1] for part in parts))
            titles[key] = (name, match.group(2), parts)

    for line in lines:
        add_title([line])
    for row in rows:
        parts = sorted(row["lines"], key=lambda line: line["box"][0])
        # Do not join distant columns or unrelated strings on the same row.
        runs = []
        for part in parts:
            if not runs or part["box"][0] - (runs[-1][-1]["box"][0] + runs[-1][-1]["box"][2]) > 35:
                runs.append([])
            runs[-1].append(part)
        for run in runs:
            # A grid level can share the header's vertical baseline. It must
            # not swallow a valid title simply because the OCR boxes are near.
            for start in range(len(run)):
                for end in range(start + 2, min(len(run), start + 4) + 1):
                    add_title(run[start:end])
    if len(titles) != 1:
        return None, raw
    name, slot, title = next(iter(titles.values()))
    left = min(line["box"][0] for line in title)
    top = min(line["box"][1] for line in title)
    # Colour and level live on the right of this tooltip's header. Grid
    # labels just outside the panel must not become competing field values.
    fields = [line for line in raw if left + 180 <= line["box"][0] <= left + 305
              and line["box"][0] + line["box"][2] <= left + 312
              and top + 25 <= line["box"][1] <= top + 105]
    levels, colors, field_boxes = set(), set(), [line["box"] for line in title]
    for line in fields:
        found = LEVEL.search(line["text"]) if line["score"] >= threshold else None
        if found:
            levels.add(int(found.group(1)))
            field_boxes.append(line["box"])
        if line["score"] >= max(threshold, color_threshold):
            matched = {color for label, color in COLORS.items() if label in line["text"]}
        elif line["score"] >= color_threshold:
            # The colour dot can be included in the OCR line and lower its
            # aggregate confidence. Only a complete colour label, optionally
            # preceded by dot glyphs, qualifies for the lower field threshold.
            color = COLORS.get(line["text"].lstrip(COLOR_DECORATIONS))
            matched = {color} if color is not None else set()
        else:
            matched = set()
        if matched:
            colors.update(matched)
            field_boxes.append(line["box"])
    unknown = name not in definitions
    definition = definitions.get(name, {"rarity": 3, "colors": list("蓝绿紫红粉黄白彩")})
    maximum = catalog["upgrade_costs"][str(definition["rarity"])]["max_level"]
    if len(levels) != 1 or not 0 <= next(iter(levels)) <= maximum:
        return None, raw
    level = next(iter(levels))
    color = next(iter(colors)) if len(colors) == 1 else None
    if color not in definition["colors"] or color == "彩" and level != maximum:
        color = None
    facts = {"name": name, "slot": slot, "rarity": definition["rarity"], "level": level,
             "color": color, "header_box": [left, top], "field_boxes": field_boxes}
    if unknown:
        facts["protected_inventory"] = True
    return facts, raw


class VerifiedVariants:
    """Run-local cache of text-verified copies; never a replacement icon bank."""
    def __init__(self, matcher):
        self.matcher = matcher
        self.entries = OrderedDict()

    @staticmethod
    def key(facts):
        return facts["name"], facts["slot"], facts["color"], facts["level"]

    def find(self, frame, box, match, level):
        if not match["accepted"] or level is None:
            return None
        candidate = match["candidate"]
        key = self.key({**candidate, "level": level})
        entry = self.entries.get(key)
        if entry is None:
            return None
        group = self.matcher.group_lookup[(candidate["name"], candidate["slot"])]
        features = self.matcher.fingerprint(frame, box, group)
        if features is None:
            return None
        shape, color = features
        for stored_shape, stored_color, facts, evidence in entry:
            # Both shape and spatial colour must agree with an actual tooltip
            # observation. Matching the same public candidate is insufficient.
            if float(shape @ stored_shape) >= .995 and float(np.linalg.norm(color - stored_color)) <= .02:
                self.entries.move_to_end(key)
                return facts, evidence
        return None

    def remember(self, frame, box, facts, evidence):
        if facts.get("color") is None or facts.get("protected_inventory"):
            return
        group = self.matcher.group_lookup[(facts["name"], facts["slot"])]
        features = self.matcher.fingerprint(frame, box, group)
        if features is None:
            return
        key = self.key(facts)
        copies = self.entries.setdefault(key, [])
        copies.append((*features, dict(facts), dict(evidence)))
        del copies[:-2]
        self.entries.move_to_end(key)
        while len(self.entries) > 512:
            self.entries.popitem(last=False)

    def clear(self):
        self.entries.clear()
