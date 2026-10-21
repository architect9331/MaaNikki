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
        bx, by, bw, bh, area = (int(value) for value in stats[label])
        if bx + bw < crop.shape[1] - 2 or by + bh < crop.shape[0] - 3:
            continue
        if bw < 14 or bh < 9:
            continue
        possible = True
        mask = (labels[by:by + bh, bx:bx + bw] == label).astype(np.uint8)
        if bw < 24 or bh < 12 or area / (bw * bh) < .60 or mask[-3:].mean() < .75:
            continue
        # Fill holes (the digits) while retaining the tab's rounded outline.
        # Everything outside this shape becomes white before digit OCR.
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        filled = np.zeros_like(mask)
        cv2.drawContours(filled, contours, -1, 1, cv2.FILLED)
        patch = crop[by:by + bh, bx:bx + bw].copy()
        ink = (patch.max(axis=2) < 195) & (filled > 0)
        if np.count_nonzero(ink) < 8:
            continue
        patch[filled == 0] = 255
        return {"state": "stack", "roi": [left + bx, top + by, bw, bh], "patch": patch}
    return {"state": "uncertain" if possible else "single", "roi": [left, top, crop.shape[1], crop.shape[0]]}


def read_count(owner, frame, box, locked, labels, generation, index):
    """No digit OCR for single copies; ambiguous badges never default to one."""
    owner.check_generation(generation)
    stats = owner.report.setdefault("quantity_reads", {"single": 0, "page_ocr": 0, "local_ocr": 0, "cached": 0})
    if locked:
        stats["single"] += 1
        return 1, {"source": "locked_single"}
    badge = stack_badge(frame, box)
    if badge["state"] == "single":
        stats["single"] += 1
        return 1, {"source": "no_stack_badge", "roi": badge["roi"]}
    x, y, w, h = badge["roi"]
    reads = []
    limit = owner.snapshot["scan"].get("reported_capacity") or 10000
    if badge["state"] == "stack":
        # An exact masked-pixel match reuses only a number already recognized
        # during this run. No approximate digit matching or saved counts.
        cache_key = (badge["patch"].shape, badge["patch"].tobytes())
        cached = owner.stack_count_cache.get(cache_key)
        if cached is not None and cached[0] <= limit:
            stats["cached"] += 1
            return cached[0], {"source": "identical_stack_badge", "roi": badge["roi"], "reference": cached[1]}

        def remember(value, evidence):
            if len(owner.stack_count_cache) >= 512:
                owner.stack_count_cache.pop(next(iter(owner.stack_count_cache)))
            owner.stack_count_cache[cache_key] = (value, index)
            return value, evidence

        # Reuse an already detected number only after the stack shape is known.
        values = []
        for item in labels:
            bx, by, bw, bh = list(item.box)
            text = getattr(item, "text", "").strip()
            if (x - 1 <= bx and bx + bw <= x + w + 1 and y - 2 <= by and by + bh <= y + h + 2
                    and float(getattr(item, "score", 0)) >= .95 and re.fullmatch(r"[1-9][0-9]{0,3}", text)
                    and 2 <= int(text) <= limit):
                values.append(int(text))
        if len(values) == 1:
            stats["page_ocr"] += 1
            return remember(values[0], {"source": "stack_page_ocr", "roi": badge["roi"]})
        gray = cv2.cvtColor(badge["patch"], cv2.COLOR_BGR2GRAY)
        low = float(np.percentile(gray, 5))
        enhanced = np.clip((gray.astype(np.float32) - low) * 255 / max(1, 255 - low), 0, 255).astype(np.uint8)
        for scale in (4, 6):
            # Recognition-only OCR avoids losing tiny digits in text detection.
            patch = cv2.resize(enhanced, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
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
            if score >= .90 and re.fullmatch(r"[1-9][0-9]{0,3}", text) and 2 <= int(text) <= limit:
                return remember(int(text), {"source": "stack_local_ocr", "roi": badge["roi"], "reads": reads})
    diagnostic = {"box": list(box), "badge": badge["state"], "roi": badge["roi"], "reads": reads}
    owner.report["quantity_failure"] = diagnostic
    x, y, w, h = box
    owner.image(f"quantity_{index:05d}_unknown.png", frame[y:y + h, x:x + w])
    write_json(owner.folder / f"quantity_{index:05d}_unknown.json", diagnostic)
    raise RuntimeError("堆叠标记或数量未能确认，已保存格子截图，未将其按一件计入库存")
