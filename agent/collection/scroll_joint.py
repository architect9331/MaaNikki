"""Resolve scroll ambiguity with one shared translation across the card columns."""
from __future__ import annotations

import cv2
import numpy as np

from .scroll_color import distinguish


def translation(before, after, first, second, boxes, layout, *, threshold, margin,
                neighbourhood, color_threshold, color_margin):
    """Match the same reference pixels at every geometrically possible offset.

    A repeated icon may have several matches on its own. All columns still
    undergo exactly the same translation. Compare their correlation profiles
    together, retaining the caller's shape/colour floors and separation limits.
    A repeated complete reference row remains ambiguous and is rejected.
    """
    gx, gy, gw, gh = layout["grid"]
    width, height = layout["card_size"]
    maximum = gh - height - layout.get("row_pitch", 123)
    columns = layout["columns"]
    diagnostic = {"accepted": False, "maximum_delta": maximum, "reference_boxes": []}
    if maximum < 0:
        return None, {**diagnostic, "reason": "insufficient_overlap"}

    # A visible card to the right proves the preceding columns are occupied.
    # Infer geometry only; the full colour/edge patches must still match.
    rows = []
    for box in sorted(boxes, key=lambda value: (value[1], value[0])):
        row = next((row for row in rows if abs(row[0][1] - box[1]) < 12), None)
        if row is None:
            row = []
            rows.append(row)
        row.append(box)
    inferred = []
    anchors = list(boxes)
    for row in rows:
        present = {round((box[0] - layout["first_x"]) / layout["column_pitch"]) for box in row}
        top = round(float(np.median([box[1] for box in row])))
        for column in range(max(present) + 1):
            if column not in present:
                box = [round(layout["first_x"] + column * layout["column_pitch"]), top, width, height]
                anchors.append(box)
                inferred.append(box)
    diagnostic["inferred_boxes"] = inferred

    profiles = [[] for _ in range(columns)]
    patches = []
    for box in anchors:
        bx, by, _, _ = box
        column = round((bx - layout["first_x"]) / layout["column_pitch"])
        left, right, top, bottom = bx + 12, bx + width - 12, by + 22, by + height - 30
        # Use a fixed set of pixels for all offsets, so larger translations
        # cannot get a better score merely by dropping disagreeing cards.
        # Icon bodies in the lower overlap band stay visible throughout the
        # full search range, even if the card's top border leaves the screen.
        if (not 0 <= column < columns or left < gx or right > gx + gw
                or top - maximum < gy or bottom > gy + gh - 8):
            continue
        template = first[top:bottom, left:right]
        if float(np.std(template)) < 2:
            continue
        search = second[top - maximum:bottom, left:right]
        scores = cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED).ravel()[::-1]
        profiles[column].append(np.nan_to_num(scores, nan=-1, posinf=-1, neginf=-1))
        patches.append((left, right, top, bottom, box))
    diagnostic["reference_boxes"] = [list(patch[4]) for patch in patches]
    if any(not values for values in profiles):
        return None, {**diagnostic, "reason": "missing_reference_column"}

    # Equal weight per normalised card, then per column. A bright or richly
    # textured icon cannot overpower a column that does not agree.
    scores = np.stack([np.mean(values, axis=0) for values in profiles])
    joint = scores.mean(axis=0)
    joint[np.any(scores < threshold, axis=0)] = -1
    order = np.argsort(joint)[::-1]
    best = int(order[0])
    competing = [int(value) for value in order if abs(int(value) - best) > neighbourhood]
    gap = float(joint[best] - joint[competing[0]]) if competing else 1.0
    diagnostic.update(delta=best, score=round(float(joint[best]), 4), margin=round(gap, 4),
                      column_scores=[round(float(value), 4) for value in scores[:, best]],
                      minimum_score=threshold, minimum_margin=margin)
    if competing:
        diagnostic.update(competing_delta=competing[0],
                          competing_score=round(float(joint[competing[0]]), 4))
    if joint[best] < threshold:
        return None, {**diagnostic, "reason": "low_joint_score"}
    if gap >= margin:
        return best, {**diagnostic, "accepted": True, "reason": "distinct_joint_shape"}

    offsets = []
    for value in order:
        value = int(value)
        if joint[value] < max(threshold, float(joint[best]) - margin):
            break
        if all(abs(value - other) > neighbourhood for other in offsets):
            offsets.append(value)
    # Pack the SAME reference patches for every candidate, reusing the
    # existing brightness-normalised colour metric without classifying icons.
    template = np.concatenate([before[top:bottom, left:right]
                               for left, right, top, bottom, _ in patches], axis=1)
    candidates = [np.concatenate([after[top - delta:bottom - delta, left:right]
                                 for left, right, top, bottom, _ in patches], axis=1)
                  for delta in offsets]
    search = np.concatenate(candidates, axis=0)
    weights = []
    for left, right, top, bottom, box in patches:
        weight = np.maximum(first[top:bottom, left:right], 0) ** 2
        weight[max(0, box[1] + height - 35 - top):,
               max(0, box[0] + width - 30 - left):] = 0
        weights.append(weight)
    block_height = template.shape[0]
    chosen, color = distinguish(template, search,
                                [index * block_height for index in range(len(offsets))],
                                np.concatenate(weights, axis=1),
                                threshold=color_threshold, margin=color_margin)
    diagnostic["color"] = color
    diagnostic["color_candidate_deltas"] = offsets
    if chosen is None:
        return None, {**diagnostic, "reason": "ambiguous_joint_match"}
    delta = offsets[chosen // block_height]
    return delta, {**diagnostic, "accepted": True, "gray_best_delta": best, "delta": delta,
                   "score": round(float(joint[delta]), 4),
                   "column_scores": [round(float(value), 4) for value in scores[:, delta]],
                   "reason": "distinct_joint_color"}
