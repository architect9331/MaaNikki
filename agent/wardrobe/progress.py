"""Acquisition-order checkpoints; existing clothes keep their relative order."""
from __future__ import annotations

import base64
from copy import deepcopy
import json

import cv2
import numpy as np

from collection.scroll_color import distinguish


# This model head describes ability items, not a separate S-switchable tab.
MODEL_ONLY_PARTS = {"413"}


def ordered_prefix(state, layout):
    """Reuse only the contiguous prefix actually read from the list's top.

    Old snapshots stored pixel positions. Convert those without inventing
    missing rows/cells; an interrupted page-gap report resumes before its gap.
    """
    items = state.get("items", [])
    if not items:
        return []
    if all(type(item.get("slot")) is int for item in items):
        ordered = sorted(items, key=lambda item: item["slot"])
        prefix = []
        for item in ordered:
            if item["slot"] != len(prefix):
                break
            prefix.append(deepcopy(item))
        return prefix
    ordered = sorted(items, key=lambda item: (item["position"][1], item["position"][0]))
    rows = []
    for item in ordered:
        y = item["position"][1]
        if not rows or abs(y - rows[-1][0]) >= 4:
            rows.append((y, []))
        rows[-1][1].append(item)
    prefix, previous = [], None
    for top, row in rows:
        if previous is not None and abs(top - previous - layout["row_pitch"]) > 4:
            break
        for item in sorted(row, key=lambda value: value["position"][0]):
            column = round((item["position"][0] - layout["first_x"]) / layout["column_pitch"])
            if column != len(prefix) % layout["columns"]:
                return prefix
            prefix.append(deepcopy(item))
        if len(row) != layout["columns"]:
            break
        previous = top
    return prefix


def identity(item):
    # Selected-card OCR and the model share item IDs, even when the selected
    # background changes the raw class prediction. Same-art IDs stay grouped.
    return tuple(sorted(item.get("item_ids", [])))


def head_anchor(items, layout):
    """Use one unfaded page, or the entire prefix when fewer items were read."""
    capacity = ((layout["grid"][3] - layout["row_pitch"]) // layout["row_pitch"]
                * layout["columns"])
    keys = [identity(item) for item in items]
    anchor = keys[:capacity]
    if not anchor or any(not key for key in anchor):
        return None
    occurrences = [start for start in range(len(keys) - len(anchor) + 1)
                   if keys[start:start + len(anchor)] == anchor]
    return anchor if occurrences == [0] else None


def find_head(known, anchor):
    matches = [start for start in sorted(known)
               if all(start + offset in known and identity(known[start + offset]) == key
                      for offset, key in enumerate(anchor))]
    return matches[0] if len(matches) == 1 else None


def head_capacity(layout):
    return ((layout["grid"][3] - layout["row_pitch"]) // layout["row_pitch"]
            * layout["columns"])


def card_body(frame, box):
    x, y, w, h = box
    # The same icon-body crop as shared scroll matching, excluding borders,
    # stars and dye badges. Appearance anchors must not depend on a class ID.
    return frame[y + 22:y + h - 30, x + 12:x + w - 12].copy()


def encode_head(observed, layout):
    encoded = []
    for slot in range(head_capacity(layout)):
        if slot not in observed:
            break
        ok, packed = cv2.imencode(".png", observed[slot])
        if not ok:
            break
        encoded.append(base64.b64encode(packed).decode("ascii"))
    return encoded


def decode_head(encoded, layout):
    if not isinstance(encoded, list) or not 0 < len(encoded) <= head_capacity(layout):
        return []
    decoded = []
    w, h = layout["card_size"]
    try:
        for value in encoded:
            raw = base64.b64decode(value, validate=True)
            patch = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
            if patch is None or patch.shape != (h - 52, w - 24, 3):
                return []
            decoded.append(patch)
    except (ValueError, TypeError, cv2.error):
        return []
    return decoded


def find_visual_head(observed, reference, layout):
    """Match ordered icon pixels using existing scroll shape/colour limits.

    Every reference patch must match. Competing sequence positions retain the
    shared .04 shape margin and colour tie-break; there is no majority rule
    over model predictions and no named-identity acceptance threshold.
    """
    if not reference:
        return None, {"basis": "images", "reason": "missing_reference"}

    def edges(patch):
        gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY).astype(np.float32)
        return gray - cv2.GaussianBlur(gray, (0, 0), 3)

    templates = [edges(patch) for patch in reference]
    if any(float(template.std()) < 2 for template in templates):
        return None, {"basis": "images", "reason": "insufficient_texture"}
    features = {slot: edges(patch) for slot, patch in observed.items()}
    candidates = []
    for start in sorted(observed):
        if not all(start + offset in observed for offset in range(len(reference))):
            continue
        scores = [float(cv2.matchTemplate(features[start + offset], template,
                        cv2.TM_CCOEFF_NORMED)[0, 0]) for offset, template in enumerate(templates)]
        if not all(np.isfinite(score) and score >= .80 for score in scores):
            continue
        candidates.append({"start": start, "score": float(np.mean(scores)),
                           "minimum_score": min(scores)})
    candidates.sort(key=lambda value: value["score"], reverse=True)
    diagnostic = {"basis": "images", "reference_cards": len(reference),
                  "candidates": [{key: round(value, 4) if isinstance(value, float) else value
                                  for key, value in row.items()} for row in candidates]}
    if not candidates:
        return None, {**diagnostic, "reason": "no_shape_match"}
    if len(candidates) == 1 or candidates[0]["score"] - candidates[1]["score"] >= .04:
        return candidates[0]["start"], {**diagnostic, "reason": "distinct_shape"}
    plausible = [row for row in candidates if candidates[0]["score"] - row["score"] < .04]
    template = np.concatenate(reference, axis=1)
    searches = [np.concatenate([observed[row["start"] + offset] for offset in range(len(reference))],
                               axis=1) for row in plausible]
    height = template.shape[0]
    chosen, color = distinguish(template, np.concatenate(searches, axis=0),
        [index * height for index in range(len(plausible))],
        np.concatenate([np.maximum(value, 0) ** 2 for value in templates], axis=1),
        threshold=.80, margin=.025)
    return (plausible[chosen // height]["start"] if chosen is not None else None), {
        **diagnostic, "reason": "colour_tie_break", "color": color}


def legacy_heads(directory, previous, layout, fingerprint, uid):
    """Migrate first-page evidence from the original pre-resume scan reports.

    A failed newer resume can contain OLD item records and NEW screenshots;
    it is not evidence of the old head. Only original scans whose saved
    ordered prefix equals the checkpoint may supply legacy reference pixels.
    Future references live inside the account JSON, independent of log cleanup.
    """
    pending = {part: ordered_prefix(state, layout) for part, state in previous.items()}
    pending = {part: items for part, items in pending.items() if items}
    found = {}
    for folder in sorted(directory.iterdir(), key=lambda path: path.name, reverse=True):
        path = folder / "scan.json"
        try:
            if not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
                continue
            report = json.loads(path.read_text(encoding="utf-8"))
            if (report.get("resumed_from") or report.get("label_fingerprint") != fingerprint
                    or report.get("uid", uid) != uid):
                continue
            for part, items in list(pending.items()):
                old = ordered_prefix(report.get("parts", {}).get(part, {}), layout)
                if [identity(item) for item in old] != [identity(item) for item in items]:
                    continue
                page = next((page for page in report.get("scan", {}).get("pages", [])
                             if page.get("part") == part and page.get("page") == 0
                             and page.get("offset") == 0), None)
                if not page:
                    continue
                image_path = folder / f"{part}_0000.jpg"
                if not image_path.is_file():
                    continue
                image = cv2.imdecode(np.fromfile(image_path, np.uint8), cv2.IMREAD_COLOR)
                gx, gy, gw, gh = layout["grid"]
                if image is None or image.shape != (gh, gw, 3):
                    continue
                observed = {}
                for x, y, w, h in page.get("boxes", []):
                    slot = (round((y - layout["first_y"]) / layout["row_pitch_exact"]) * layout["columns"]
                            + round((x - layout["first_x"]) / layout["column_pitch"]))
                    observed[slot] = card_body(image, [x - gx, y - gy, w, h])
                encoded = encode_head(observed, layout)
                if len(encoded) != min(head_capacity(layout), len(items)):
                    continue
                found[part] = encoded
                del pending[part]
        except (OSError, ValueError, TypeError, KeyError, cv2.error):
            continue
        if not pending:
            break
    return found
