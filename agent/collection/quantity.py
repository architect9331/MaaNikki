"""Read copy counts only inside the normal inventory's white stack badge."""
import re

import cv2
import numpy as np

from file_io import write_json


def stack_badge(frame, box):
    """Separate the broad count tab from artwork, the card rim and lock glyph.

    The tab touches the right edge above the coloured bottom rim. Its width
    grows with the number of digits; do not crop a fixed single-digit area.
    """
    x, y, w, h = box
    left, top = x + w - 51, y + h - 28
    crop = frame[top:y + h - 7, left:x + w - 3]
    low, high = crop.min(axis=2), crop.max(axis=2)
    light = ((low > 215) & (high.astype(np.int16) - low < 40)).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(light, connectivity=8)
    possible = False
    for label in range(1, count):
        bx, by, bw, bh, _ = (int(value) for value in stats[label])
        if bx + bw < crop.shape[1] - 2 or by + bh < crop.shape[0] - 3:
            continue
        if bw < 14 or bh < 9:
            continue
        possible = True
        mask = (labels[by:by + bh, bx:bx + bw] == label).astype(np.uint8)
        if bw < 24 or bh < 12:
            continue
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        hull = cv2.convexHull(np.vstack([contour.reshape(-1, 2) for contour in contours]))
        outline = np.zeros_like(mask)
        cv2.drawContours(outline, [hull], -1, 1, cv2.FILLED)
        # Validate the backing's silhouette, not its remaining white pixels:
        # multi-digit ink reduces the white area without changing the tab.
        # Digits such as 18 can also touch its bottom edge.
        if outline.mean() < .60 or outline[-3:].mean() < .75:
            continue
        # Preserve holes and open notches alike: either can be part of a digit.
        # Only the area outside the confirmed tab outline becomes white.
        patch = crop[by:by + bh, bx:bx + bw].copy()
        patch[outline == 0] = 255
        # Bright artwork can join the tab's component and widen its outline.
        # Trace all non-backing pixels: a dark fragment may reach the left/top
        # only through pale or coloured artwork, which a dark-only mask breaks.
        # Digits do not reach these sides or the right card border (they can
        # touch the bottom edge).
        low, high = patch.min(axis=2), patch.max(axis=2)
        backing = (low > 215) & (high.astype(np.int16) - low < 40)
        _, marks = cv2.connectedComponents((~backing).astype(np.uint8), connectivity=8)
        artwork = np.setdiff1d(np.concatenate((marks[0], marks[:, 0], marks[:, -1])), [0])
        patch[np.isin(marks, artwork)] = 255
        ink = (patch.max(axis=2) < 195) & (outline > 0)
        if np.count_nonzero(ink) < 8:
            continue
        col_counts = np.count_nonzero(ink, axis=0)
        cols = np.where(col_counts >= 1)[0]
        splits = np.split(cols, np.where(np.diff(cols) > 2)[0] + 1) if len(cols) else []
        digit_groups = []
        for g in splits:
            if not len(g):
                continue
            if int(col_counts[g].max()) >= 7:
                digit_groups.append(g)
            else:
                patch[:, g] = 255
                ink[:, g] = False
        if np.count_nonzero(ink) < 8:
            continue
        ink_x = np.where(ink)[1]
        groups = len(digit_groups) if digit_groups else 1
        return {"state": "stack", "roi": [left + bx, top + by, bw, bh], "patch": patch,
                "ink_span": [left + bx + int(ink_x.min()), left + bx + int(ink_x.max()) + 1],
                "ink_groups": groups}
    return {"state": "uncertain" if possible else "single", "roi": [left, top, crop.shape[1], crop.shape[0]]}


def read_count(owner, frame, box, locked, labels, generation, index, *, classification=None):
    """No digit OCR for single copies; ambiguous badges never default to one."""
    owner.check_generation(generation)
    stats = owner.report.setdefault("quantity_reads", {"single": 0, "page_ocr": 0, "local_ocr": 0, "cached": 0})
    # Locking splits one copy out of a stack; locked cards never stack.
    if locked:
        stats["single"] += 1
        return 1, {"source": "locked_single"}
    kind = (classification.get("badge_state") if isinstance(classification, dict)
            and classification.get("box") == list(box) else None)
    # A clicked button is stronger evidence of lock state. Discard the model
    # result if it disagrees with the caller's independently verified state.
    if kind == "locked":
        kind = None
    if kind == "single":
        stats["single"] += 1
        return 1, {"source": "badge_classifier_single", "roi": classification["roi"],
                   "model_version": classification["model_version"]}
    badge = stack_badge(frame, box)
    if kind == "stack" and badge["state"] != "stack":
        # Classification establishes that a count exists. Segmentation is an
        # OCR aid, not a second vote that can silently turn a stack into one.
        x, y, w, h = badge["roi"]
        badge = {"state": "stack", "roi": [x, y, w, h],
                 "patch": frame[y:y + h, x:x + w].copy()}
    if badge["state"] == "single":
        stats["single"] += 1
        return 1, {"source": "no_stack_badge", "roi": badge["roi"]}
    x, y, w, h = badge["roi"]
    reads = []
    values = []
    page_reads = []
    limit = owner.snapshot["scan"].get("reported_capacity") or 10000
    if badge["state"] == "stack":
        # An exact masked-pixel match reuses only a number already recognized
        # during this run. No approximate digit matching or saved counts.
        cache_key = (badge["patch"].shape, badge["patch"].tobytes())
        cached = owner.stack_count_cache.get(cache_key)
        if cached is not None and cached[0] <= limit:
            stats["cached"] += 1
            return cached[0], {"source": "identical_stack_badge", "roi": badge["roi"], "reference": cached[1],
                               "patch": owner.image(f"quantity_{index:05d}_{cached[0]}.png", badge["patch"])}

        def remember(value, evidence):
            if len(owner.stack_count_cache) >= 512:
                owner.stack_count_cache.pop(next(iter(owner.stack_count_cache)))
            owner.stack_count_cache[cache_key] = (value, index)
            evidence["patch"] = owner.image(f"quantity_{index:05d}_{value}.png", badge["patch"])
            return value, evidence

        ink_span = badge.get("ink_span")
        ink_groups = badge.get("ink_groups", 1)
        # Reuse an already detected number only after the stack shape is known.
        for item in labels:
            bx, by, bw, bh = list(item.box)
            text = getattr(item, "text", "").strip()
            if (x - 1 <= bx and bx + bw <= x + w + 1 and y - 2 <= by and by + bh <= y + h + 2
                    and float(getattr(item, "score", 0)) >= .95 and re.fullmatch(r"[1-9][0-9]{0,3}", text)):
                page_reads.append({"text": text, "score": float(item.score), "box": [bx, by, bw, bh]})
                # Full-page text detection can box only the wider trailing digit
                # and leave a thin leading "1" outside the box. Accept a page
                # read only when its box and digit count cover the segmented ink.
                if (len(text) >= ink_groups
                        and (ink_span is None or (bx <= ink_span[0] and bx + bw >= ink_span[1]))):
                    values.append(int(text))
        page_values = set(values)
        local_values = []
        candidates = set(page_values)
        gray = cv2.cvtColor(badge["patch"], cv2.COLOR_BGR2GRAY)
        # The count already contrasts with its white backing. Preserve the
        # antialiasing; stretching its histogram amplifies faint artwork too.
        for scale in (4, 6, 3, 8):
            # Recognition-only OCR avoids losing tiny digits in text detection.
            patch = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            patch = cv2.copyMakeBorder(patch, 4, 4, 4, 4, cv2.BORDER_CONSTANT, value=255)
            image = np.full_like(frame, 255)
            ph, pw = patch.shape
            image[:ph, :pw] = cv2.cvtColor(patch, cv2.COLOR_GRAY2BGR)
            owner.check_generation(generation)
            result = owner.rt.recognize("MaaNikki_Daily_OCR", {
                "roi": [0, 0, pw, ph], "only_rec": True,
                "expected": [r"^[1-9][0-9]{0,3}$"], "threshold": .90}, image=image)
            owner.check_generation(generation)
            stats["local_ocr"] += 1
            item = getattr(result, "best_result", None) if result and result.hit else None
            text = getattr(item, "text", "").strip()
            score = float(getattr(item, "score", 0))
            reads.append({"text": text, "score": score, "scale": scale})
            if score >= .90 and len(text) >= ink_groups and re.fullmatch(r"[1-9][0-9]{0,3}", text):
                value = int(text)
                local_values.append(value)
                candidates.add(value)
                # Page detection can drop a leading digit. Require agreement,
                # never prefer the larger/longer count merely for its suffix.
                if (2 <= value <= limit and len(candidates) == 1
                        and local_values.count(value) + int(value in page_values) >= 2):
                    stats["page_ocr"] += int(value in page_values)
                    return remember(value, {"source": "stack_confirmed_ocr", "roi": badge["roi"],
                                             "page_values": values, "page_reads": page_reads, "reads": reads})
    diagnostic = {"box": list(box), "badge": badge["state"], "roi": badge["roi"],
                  "page_values": values, "page_reads": page_reads, "reads": reads}
    if badge["state"] == "stack":
        diagnostic["patch"] = owner.image(f"quantity_{index:05d}_patch.png", badge["patch"])
    owner.report["quantity_failure"] = diagnostic
    x, y, w, h = box
    owner.image(f"quantity_{index:05d}_unknown.png", frame[y:y + h, x:x + w])
    write_json(owner.folder / f"quantity_{index:05d}_unknown.json", diagnostic)
    raise RuntimeError("堆叠标记或数量未能确认，已保存格子截图，未将其按一件计入库存")
