"""Daily backpack scanner for trial crystals, glow-up bubbles, threads, and bling."""
from __future__ import annotations

from datetime import datetime, timezone
import re

import cv2
import numpy as np

from account_state import AccountState, game_day


HEADER_ROI = [65, 15, 240, 48]
BLING_ROI = [1052, 16, 105, 34]
RIGHT_TITLE_ROI = [895, 80, 345, 52]
TAB_MATERIALS = [457, 36]
TAB_GROWTH = [539, 36]
GRID_SCROLL_TARGET = [420, 360]


def current_game_day() -> str:
    return game_day().isoformat()


def wardrobe_snapshot(role: dict) -> dict:
    wardrobe = role.get("wardrobe") or {}
    snapshot = wardrobe.get("inventory") if (wardrobe.get("inventory") or {}).get("complete") else (
        wardrobe.get("last_scan") or wardrobe.get("inventory") or {})
    return snapshot if isinstance(snapshot, dict) else {}


def wardrobe_snapshot_key(role: dict) -> str:
    snapshot = wardrobe_snapshot(role)
    if not snapshot:
        return ""
    return f"{snapshot.get('updated_at') or ''}:{snapshot.get('scanned_count') or 0}"


def normalize_name(text: str) -> str:
    return re.sub(r"[\s·・•.。:：\-_]", "", text or "")


def parse_badge_quantity(raw: str) -> int | None:
    cleaned = re.sub(r"\s+", "", raw or "").upper().replace("O", "0")
    match = re.search(r"(\d+(?:\.\d+)?)\s*([K万W]?)", cleaned)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2)
    if unit == "K":
        return int(round(value * 1000))
    if unit in ("万", "W"):
        return int(round(value * 10000))
    return int(round(value))


def detect_row_ys(frame: np.ndarray) -> list[int]:
    """Detect row pill Y positions aligned with the 8-column card right edges."""
    gray = cv2.cvtColor(frame[120:630, 35:820], cv2.COLOR_BGR2GRAY)
    mask = (gray > 218).astype(np.uint8) * 255
    num, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    ys = []
    for i in range(1, num):
        x, y, w, h, area = stats[i]
        if not (24 <= w <= 70 and 11 <= h <= 25 and area >= 180):
            continue
        right_x = x + 35 + w
        if any(abs(right_x - (127 + col * 96)) <= 5 for col in range(8)):
            ys.append(y + 120)
    if not ys:
        return []
    ys.sort()
    clusters = [[ys[0]]]
    for y in ys[1:]:
        if abs(y - clusters[-1][-1]) <= 22:
            clusters[-1].append(y)
        else:
            clusters.append([y])
    return [int(round(np.median(c))) for c in clusters]


def cell_has_pill(frame: np.ndarray, col: int, py: int) -> bool:
    cx = 46 + col * 96
    y0, y1 = max(0, py - 2), min(frame.shape[0], py + 20)
    x0, x1 = max(0, cx + 28), min(frame.shape[1], cx + 82)
    patch = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    return patch.size > 0 and float(np.percentile(patch, 85)) >= 210


def card_hsv_median(frame: np.ndarray, col: int, py: int) -> tuple[float, float, float]:
    cx = 46 + col * 96
    cy = py - 82
    y0, y1 = max(0, cy + 6), min(frame.shape[0], cy + 22)
    x0, x1 = max(0, cx + 6), min(frame.shape[1], cx + 76)
    strip = frame[y0:y1, x0:x1]
    if strip.size == 0:
        return 0.0, 0.0, 0.0
    hsv = cv2.cvtColor(strip, cv2.COLOR_BGR2HSV)
    return (
        float(np.median(hsv[:, :, 0])),
        float(np.median(hsv[:, :, 1])),
        float(np.median(hsv[:, :, 2])),
    )


def read_badge_qty(rt, frame: np.ndarray, col: int, py: int) -> int | None:
    cx = 46 + col * 96
    y0, y1 = max(0, py - 4), min(frame.shape[0], py + 21)
    x0, x1 = max(0, cx + 16), min(frame.shape[1], cx + 84)
    crop = frame[y0:y1, x0:x1]
    if crop.size == 0:
        return None
    upscaled = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    h, w = upscaled.shape[:2]
    res = rt.recognize("MaaNikki_Daily_OCR", {
        "roi": [0, 0, w, h],
        "expected": r"[0-9][0-9.kK万wW]*",
    }, image=upscaled)
    raw = rt.text([x0, y0, x1 - x0, y1 - y0], image=frame) if not (res and res.hit) else (
        getattr(res.best_result, "text", "") or "")
    return parse_badge_quantity(raw)


def read_top_bling(rt, frame: np.ndarray) -> int | None:
    x, y, w, h = BLING_ROI
    crop = frame[y:y + h, x:x + w]
    if crop.size == 0:
        return None
    upscaled = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    res = rt.recognize("MaaNikki_Daily_OCR", {
        "roi": [0, 0, w * 3, h * 3],
        "only_rec": True,
        "expected": r"^[0-9\s]+$",
    }, image=upscaled)
    if not res or not res.hit:
        return None
    digits = re.sub(r"\D", "", getattr(res.best_result, "text", "") or "")
    return int(digits) if digits else None


def scroll_to_bottom(rt) -> np.ndarray | None:
    previous = rt.capture()
    for _ in range(10):
        if rt.stopped or previous is None:
            return None
        if not rt.action("Scroll", target=GRID_SCROLL_TARGET, dx=0, dy=-12000) or not rt.pause(0.35):
            return None
        current = rt.capture()
        if current is None:
            return None
        diff = float(np.mean(cv2.absdiff(previous[130:600, 45:800], current[130:600, 45:800])))
        previous = current
        if diff < 2.2:
            break
    return previous


def scan_backpack_inventory(rt, crafting_catalog: dict) -> dict[str, int] | None:
    """Open backpack, scan trial crystals + thread on Tab 1 and bubbles on Tab 2."""
    if not rt.main():
        return None
    if not rt.ui.enter_page(
        lambda: rt.key("backpack"),
        rt.ui.feature(text="背包", roi=HEADER_ROI),
        source="MaaNikki_MainDetected",
        attempts=2,
    ):
        rt.log("背包扫描：未能打开背包界面，沿用已有材料库存记录。")
        return None

    name_to_id: dict[str, str] = {}
    crystal_ids: set[str] = set()
    bubble_ids: set[str] = set()
    for mid, meta in (crafting_catalog.get("materials") or {}).items():
        kind = meta.get("kind")
        if kind in ("monster_craft", "monster_glow", "jihua"):
            name_to_id[normalize_name(meta["name"])] = str(mid)
        if kind == "monster_craft":
            crystal_ids.add(str(mid))
        elif kind == "monster_glow" or str(mid) == "50361309":
            bubble_ids.add(str(mid))

    found: dict[str, int] = {}
    crystal_ocr_failures = 0
    bubble_ocr_failures = 0
    tab1_scanned = False
    tab2_scanned = False
    try:
        frame = rt.capture()
        if frame is None:
            return None
        bling = read_top_bling(rt, frame)
        if bling is not None:
            found["992617511"] = bling

        # 1. Tab 1 (背包 · 材料): scroll to bottom and scan upward for crystals & thread
        header_text = rt.text(HEADER_ROI, image=frame)
        if "养成" in header_text:
            if not rt.action("Click", target=TAB_MATERIALS) or not rt.pause(0.4):
                return None
        frame = scroll_to_bottom(rt)
        if frame is None:
            return None

        for page_step in range(2):
            row_ys = detect_row_ys(frame)
            if not row_ys:
                break
            tab1_scanned = True
            stop_upward = False
            for r_idx in range(len(row_ys) - 1, -1, -1):
                py = row_ys[r_idx]
                brown_count = 0
                blue_count = 0
                for col in range(7, -1, -1):
                    if not cell_has_pill(frame, col, py):
                        continue
                    h_med, s_med, _ = card_hsv_median(frame, col, py)
                    is_blue_crystal = 92 <= h_med <= 120 and s_med >= 26
                    is_brown_drop = 8 <= h_med <= 22 and s_med >= 34
                    is_bottom_tail = page_step == 0 and r_idx == len(row_ys) - 1
                    if is_brown_drop:
                        brown_count += 1
                    if is_blue_crystal:
                        blue_count += 1
                    if not (is_blue_crystal or is_bottom_tail):
                        continue
                    cx = 46 + col * 96
                    cy = max(120, py - 45)
                    if not rt.action("Click", target=[cx + 41, cy]) or not rt.pause(0.2):
                        return None
                    clicked_frame = rt.capture()
                    if clicked_frame is None:
                        return None
                    title = normalize_name(rt.text(RIGHT_TITLE_ROI, image=clicked_frame))
                    matched_id = name_to_id.get(title)
                    if not matched_id:
                        for key_norm, mid in name_to_id.items():
                            if key_norm and (key_norm in title or title in key_norm) and len(title) >= 3:
                                matched_id = mid
                                break
                    if matched_id:
                        qty = read_badge_qty(rt, clicked_frame, col, py)
                        if qty is not None:
                            found[matched_id] = qty
                        elif matched_id in crystal_ids:
                            crystal_ocr_failures += 1
                    elif is_blue_crystal and ("晶" in title or not title):
                        crystal_ocr_failures += 1
                if brown_count >= 4 and blue_count == 0 and r_idx < len(row_ys) - 1:
                    stop_upward = True
                    break
            if stop_upward or page_step == 1:
                break
            if not rt.action("Scroll", target=GRID_SCROLL_TARGET, dx=0, dy=4800) or not rt.pause(0.4):
                break
            frame = rt.capture()
            if frame is None:
                break

        if tab1_scanned and crystal_ocr_failures == 0:
            for mid in crystal_ids:
                found.setdefault(mid, 0)

        # 2. Tab 2 (背包 · 养成材料): scan purple 4-star bubble cards on first page
        if rt.action("Click", target=TAB_GROWTH) and rt.pause(0.45):
            frame2 = rt.capture()
            if frame2 is not None:
                row_ys2 = detect_row_ys(frame2)
                tab2_aborted = False
                for py in row_ys2:
                    for col in range(8):
                        if not cell_has_pill(frame2, col, py):
                            continue
                        h_med, s_med, _ = card_hsv_median(frame2, col, py)
                        if not (120 <= h_med <= 152 and s_med >= 26):
                            continue
                        cx = 46 + col * 96
                        cy = max(120, py - 45)
                        if not rt.action("Click", target=[cx + 41, cy]) or not rt.pause(0.2):
                            tab2_aborted = True
                            break
                        clicked_frame = rt.capture()
                        if clicked_frame is None:
                            tab2_aborted = True
                            break
                        title = normalize_name(rt.text(RIGHT_TITLE_ROI, image=clicked_frame))
                        matched_id = name_to_id.get(title)
                        if not matched_id:
                            for key_norm, mid in name_to_id.items():
                                if key_norm and (key_norm in title or title in key_norm) and len(title) >= 3:
                                    matched_id = mid
                                    break
                        if matched_id:
                            qty = read_badge_qty(rt, clicked_frame, col, py)
                            if qty is not None:
                                found[matched_id] = qty
                            elif matched_id in bubble_ids:
                                bubble_ocr_failures += 1
                        elif "泡泡" in title or not title:
                            bubble_ocr_failures += 1
                    if tab2_aborted:
                        break
                tab2_scanned = bool(row_ys2) and not tab2_aborted
        if tab2_scanned and bubble_ocr_failures == 0:
            for mid in bubble_ids:
                found.setdefault(mid, 0)
        return found
    finally:
        rt.main()


def ensure_backpack_scanned(rt, account: AccountState, crafting_catalog: dict) -> dict[str, int]:
    role = account.load()
    mats = role.get("materials") or {}
    inv = dict(mats.get("inventory") or {})
    today = current_game_day()
    if mats.get("backpack_scanned_day") == today and inv:
        return inv
    rt.log("正在打开背包核对今日制衣晶石与焕新泡泡库存。")
    scanned = scan_backpack_inventory(rt, crafting_catalog)
    if not scanned:
        return inv
    now = datetime.now(timezone.utc).isoformat()
    mat_defs = crafting_catalog.get("materials") or {}
    logs: list[str] = []

    def update(data):
        m = data.setdefault("materials", {})
        cur = m.setdefault("inventory", {})
        wb_key = wardrobe_snapshot_key(data)
        same_wardrobe = bool(wb_key) and m.get("wardrobe_baseline_key") == wb_key
        if wb_key and m.get("wardrobe_baseline_key") != wb_key:
            m["wardrobe_baseline_key"] = wb_key
            m["crafted_consumed"] = {}
        consumed_map = m.setdefault("crafted_consumed", {})

        glow_plan = m.get("glow_plan") if isinstance(m.get("glow_plan"), dict) else None
        glow_targets = glow_plan.get("targets") if (glow_plan and isinstance(glow_plan.get("targets"), dict)) else None

        for k, v in scanned.items():
            mid = str(k)
            new_qty = int(v)
            prev_qty = int(cur[mid]) if mid in cur else None
            meta = mat_defs.get(mid) or {}
            kind = meta.get("kind")

            if prev_qty is not None and new_qty < prev_qty:
                used = prev_qty - new_qty
                if kind == "monster_craft" and same_wardrobe:
                    consumed_map[mid] = int(consumed_map.get(mid, 0)) + used
                    logs.append(f"检测到 {meta.get('name', mid)} 库存减少 {used}（{prev_qty} -> {new_qty}），已自动核销对应制衣材料需求。")
                if (kind == "monster_glow" or mid == "50361309") and glow_targets and int(glow_targets.get(mid, 0)) > 0:
                    old_target = int(glow_targets[mid])
                    new_target = max(0, old_target - used)
                    if new_target > 0:
                        glow_targets[mid] = new_target
                    else:
                        glow_targets.pop(mid, None)
                    logs.append(f"检测到 {meta.get('name', mid)} 库存减少 {used}（已用于服装焕新），筹备目标已同步扣减为 {new_target}。")

            cur[mid] = new_qty

        if glow_plan and glow_targets is not None:
            has_remaining = any(int(t) > int(cur.get(str(gid), 0)) for gid, t in glow_targets.items() if int(t) > 0)
            glow_plan["completed"] = not has_remaining
            if not has_remaining:
                glow_plan["completed_at"] = now

        m["backpack_scanned_day"] = today
        m["inventory_updated_at"] = now

    account.mutate(update)
    for line in logs:
        rt.log(line)
    inv.update({str(k): int(v) for k, v in scanned.items()})
    rt.log(f"背包材料库存核对完成：已更新 {len(scanned)} 项材料数量。")
    return inv
