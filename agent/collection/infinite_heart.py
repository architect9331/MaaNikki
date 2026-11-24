"""Infinite Heart (无限之心) GLOBAL-view decoder and automated game scanner.

At minimum zoom (GLOBAL / 最高视野), Infinity Nikki's UI clamps camera translation
to (0, 0) (`clampScale = max(scaleVal, 1.0) -> halfW = halfH = 0`) and renders
deterministic mural overlays driven by each node's `INSERTED_MAX` state.

This module decodes all 3 shards (心愿原野碎片 / 伊赞之土碎片 / 星海碎片) from a
single minimum-zoom screenshot per shard using pre-compiled 1280x720 artwork
stroke masks (`resource/model/infinite_heart/masks.npz` + `rules.json`) and
local parchment/background contrast + prefix-constrained maximum-likelihood
decoding, then persists results into `AccountState`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
import json
from pathlib import Path
import re
import time

import cv2
import numpy as np

from account_state import AccountState, account_lock
from daily.settings import ROOT
from data_paths import data_path
from file_io import write_json
from task_control import TaskStopped

MODEL_W = 1280
MODEL_H = 720

# Top-left page header & top-right shard name + currency ROIs at 1280x720
HEADER_TITLE_ROI = [60, 14, 170, 44]
SHARD_NAME_ROI = [1040, 72, 230, 52]
HUD_CURRENCY_ROI = [995, 14, 275, 44]

# Top-center shard tab click centers at 1280x720 (verified against WBP_Heart_MainPanel_PC & live HUD):
# Left: 心愿原野碎片 (ver 1), Middle: 星海碎片 (ver 3), Right: 伊赞之土碎片 (ver 2)
SHARD_TABS = (
    (1, "心愿原野碎片", [521, 38]),
    (3, "星海碎片", [640, 38]),
    (2, "伊赞之土碎片", [759, 38]),
)
# Bottom-left zoom-out (-) button & slider minimum click target at 1280x720
ZOOM_OUT_BTN = [88, 667]
ZOOM_MIN_TRACK = [114, 667]
CANVAS_CENTER = [640, 360]


@lru_cache(maxsize=1)
def load_heart_assets() -> tuple[dict, dict, dict[str, np.ndarray]]:
    catalog = json.loads(data_path("resource/data/infinite_heart_catalog.json", ROOT).read_text(encoding="utf-8"))
    rules = json.loads(data_path("resource/model/infinite_heart/rules.json", ROOT).read_text(encoding="utf-8"))
    raw_npz = np.load(data_path("resource/model/infinite_heart/masks.npz", ROOT))
    masks = {key: raw_npz[key] for key in raw_npz.files}
    return catalog, rules, masks


def _to_720_rgb(image_bgr: np.ndarray) -> np.ndarray:
    """Normalize a BGR screenshot (any 16:9 size) to 1280x720 RGB."""
    h, w = image_bgr.shape[:2]
    if (w, h) != (MODEL_W, MODEL_H):
        image_bgr = cv2.resize(image_bgr, (MODEL_W, MODEL_H), interpolation=cv2.INTER_AREA if w > MODEL_W else cv2.INTER_LINEAR)
    return cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)


def _unpack_art_mask(packed: np.ndarray) -> np.ndarray:
    bits = np.unpackbits(packed)[: MODEL_W * MODEL_H]
    return bits.reshape((MODEL_H, MODEL_W)).astype(bool)


def _local_delta_lum(rgb: np.ndarray, art_mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return (luminance, luminance - smooth_local_non_artwork_background) at 1280x720."""
    arr = rgb.astype(np.float32)
    lum = 0.2126 * arr[:, :, 0] + 0.7152 * arr[:, :, 1] + 0.0722 * arr[:, :, 2]
    # Dilate artwork slightly so anti-aliased stroke fringes don't contaminate background estimate.
    dilated = cv2.dilate(art_mask.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)
    bg_valid = (~dilated).astype(np.float32)
    num = cv2.GaussianBlur(lum * bg_valid, (0, 0), sigmaX=30.0, sigmaY=30.0)
    den = cv2.GaussianBlur(bg_valid, (0, 0), sigmaX=30.0, sigmaY=30.0)
    bg_lum = num / np.maximum(den, 1e-4)
    return lum, lum - bg_lum


def _slice_score(pts: np.ndarray, delta_lum: np.ndarray, version_id: int) -> tuple[float, float, int]:
    """Compute signed lit-vs-dark score in [-1, +1] and median delta-luminance for a sparse mask."""
    cnt = int(len(pts))
    if cnt == 0:
        return 0.0, 0.0, 0
    dvs = delta_lum[pts[:, 0], pts[:, 1]]
    med = float(np.median(dvs))
    if version_id == 3:
        # StarSea sits on deep dark space: unlit silhouettes have small ΔL (~0..+18), lit starlight glows ΔL > +45.
        per_px = np.clip((dvs - 28.0) / 30.0, -1.0, 1.0)
    else:
        # Wishing & LOE sit on warm parchment: unlit ink has ΔL < 0 (-35..-80), lit gold has ΔL > 0 (+25..+100).
        # Soft clamp makes thin 1px lit strokes (+8..+15) cleanly positive while parchment noise (±2) stays near 0.
        per_px = np.clip(dvs / 25.0, -1.0, 1.0)
    return float(np.mean(per_px)), med, cnt


def decode_shard_image(image_bgr: np.ndarray, version_id: int, debug_out: Path | None = None) -> dict:
    """Decode one shard's GLOBAL-view screenshot into per-node states & confidence scores.

    Returns dict with keys:
      - version_id: int
      - nodes: {str(node_id): {"state": "unlocked"|"locked"|"ambiguous", "level": int, "score": float, "px": int}}
      - stats: {"unlocked": int, "locked": int, "ambiguous": int, "total": int}
    """
    catalog, rules, masks = load_heart_assets()
    vid_key = str(version_id)
    shard_rules = rules["shards"][vid_key]
    art_key = {1: "wish_art", 2: "loe_art", 3: "ss_art"}[version_id]
    art_mask = _unpack_art_mask(masks[art_key])

    rgb = _to_720_rgb(image_bgr)
    _, delta_lum = _local_delta_lum(rgb, art_mask)

    ver_nodes = {n["id"]: n for n in catalog["nodes"] if n["version_id"] == version_id}
    decoded: dict[int, dict] = {}
    debug_overlay = np.zeros((MODEL_H, MODEL_W, 4), dtype=np.uint8) if debug_out is not None else None

    def paint(pts: np.ndarray, state: str):
        if debug_overlay is None or len(pts) == 0:
            return
        color = (40, 235, 95, 145) if state == "unlocked" else (
            (255, 195, 45, 145) if state == "ambiguous" else (245, 55, 55, 145)
        )
        debug_overlay[pts[:, 0], pts[:, 1]] = color

    # 1. Prefix-constrained chains (RefreshPolygonGroup / ProgressBar / Circle / Bird Mainline)
    for chain in shard_rules.get("prefix_chains", []):
        items = chain["nodes"]
        scored = []
        for item in items:
            pts = masks[item["mask"]]
            sc, med, cnt = _slice_score(pts, delta_lum, version_id)
            scored.append((int(item["node_id"]), pts, sc, med, cnt))
        n_len = len(scored)
        best_k, best_obj = 0, -1e18
        for k in range(n_len + 1):
            obj = 0.0
            for idx, (_, _, sc, _, cnt) in enumerate(scored):
                if cnt == 0:
                    continue
                # Cap per-slice weight so one huge petal can't drown out smaller nodes in the same chain.
                w = float(min(cnt, 160))
                obj += w * (sc if idx < k else -sc)
            if obj > best_obj:
                best_obj, best_k = obj, k
        for idx, (nid, pts, sc, med, cnt) in enumerate(scored):
            is_lit = idx < best_k
            max_lv = ver_nodes[nid]["max_level"] if nid in ver_nodes else 1
            state = "unlocked" if is_lit else "locked"
            decoded[nid] = {
                "state": state,
                "level": max_lv if is_lit else 0,
                "score": round(sc, 3),
                "px": cnt,
            }
            paint(pts, state)

    # 2. Independent single-node overlays
    for item in shard_rules.get("singles", []):
        nid = int(item["node_id"])
        pts = masks[item["mask"]]
        sc, med, cnt = _slice_score(pts, delta_lum, version_id)
        node_meta = ver_nodes.get(nid, {})
        obs = node_meta.get("observability", "exact")
        max_lv = int(node_meta.get("max_level", 1))
        is_lit = sc > 0.12
        if obs == "max_only" and not is_lit:
            # Multi-level node (0..10): unlit means level < 10 (could be 0..9).
            state, level = "ambiguous", 0
        else:
            state, level = ("unlocked", max_lv) if is_lit else ("locked", 0)
        # If this node was already in a chain (shouldn't happen, but merge conservatively), prefer stronger evidence
        decoded[nid] = {"state": state, "level": level, "score": round(sc, 3), "px": cnt}
        paint(pts, state)

    # 3. Group-all overlays (lights ONLY when ALL nodes in group are INSERTED_MAX)
    for grp in shard_rules.get("group_all", []):
        pts = masks[grp["mask"]]
        sc, med, cnt = _slice_score(pts, delta_lum, version_id)
        all_lit = sc > 0.15
        nids = [int(x) for x in grp["node_ids"]]
        for i, nid in enumerate(nids):
            if nid in decoded:
                continue
            max_lv = int(ver_nodes.get(nid, {}).get("max_level", 1))
            if all_lit:
                decoded[nid] = {"state": "unlocked", "level": max_lv, "score": round(sc, 3), "px": cnt}
            else:
                # If the group is unlit, the terminal node of a linear chain is definitely locked,
                # while earlier nodes may be partially unlocked -> mark ambiguous for user confirmation.
                is_terminal = i == len(nids) - 1 and all(
                    nids[j] in (ver_nodes.get(nids[j + 1], {}).get("predecessor_ids") or [])
                    for j in range(len(nids) - 1)
                )
                decoded[nid] = {
                    "state": "locked" if is_terminal else "ambiguous",
                    "level": 0,
                    "score": round(sc, 3),
                    "px": cnt,
                }
        paint(pts, "unlocked" if all_lit else "ambiguous")

    # 4. Shared-any root overlays (e.g. LOE 2010601 / 2010701 / 2010801 sharing Graph_103_BG5_01)
    for grp in shard_rules.get("shared_any", []):
        pts = masks[grp["mask"]]
        sc, med, cnt = _slice_score(pts, delta_lum, version_id)
        any_lit = sc > 0.12
        for nid in [int(x) for x in grp["node_ids"]]:
            if nid in decoded:
                continue
            succs = ver_nodes.get(nid, {}).get("successor_ids") or []
            if any(decoded.get(sid, {}).get("state") == "unlocked" for sid in succs):
                decoded[nid] = {"state": "unlocked", "level": 1, "score": round(sc, 3), "px": cnt}
            elif not any_lit:
                decoded[nid] = {"state": "locked", "level": 0, "score": round(sc, 3), "px": cnt}
            else:
                decoded[nid] = {"state": "ambiguous", "level": 0, "score": round(sc, 3), "px": cnt}
        paint(pts, "unlocked" if any_lit else "locked")

    # 5. Fill remaining nodes (unmapped / shared_ambiguous) using predecessor constraints
    for nid, meta in ver_nodes.items():
        if nid in decoded:
            continue
        preds = meta.get("predecessor_ids") or []
        if preds and all(decoded.get(pid, {}).get("state") == "locked" for pid in preds):
            decoded[nid] = {"state": "locked", "level": 0, "score": 0.0, "px": 0}
        else:
            decoded[nid] = {"state": "ambiguous", "level": 0, "score": 0.0, "px": 0}

    # 6. Dependency closure: if a node is unlocked, all of its predecessors MUST be unlocked.
    changed = True
    while changed:
        changed = False
        for nid, entry in decoded.items():
            if entry["state"] != "unlocked":
                continue
            for pid in ver_nodes.get(nid, {}).get("predecessor_ids") or []:
                if pid in decoded and decoded[pid]["state"] != "unlocked":
                    max_lv = int(ver_nodes.get(pid, {}).get("max_level", 1))
                    # Only promote non-multi-level predecessors cleanly; multi-level nodes only need Lv >= 1 as prereq
                    if max_lv == 1:
                        decoded[pid]["state"] = "unlocked"
                        decoded[pid]["level"] = 1
                        changed = True

    if debug_out is not None and debug_overlay is not None:
        debug_out.parent.mkdir(parents=True, exist_ok=True)
        alpha = debug_overlay[:, :, 3:4].astype(np.float32) / 255.0
        blended = (rgb.astype(np.float32) * (1.0 - alpha) + debug_overlay[:, :, :3].astype(np.float32) * alpha)
        cv2.imwrite(str(debug_out), cv2.cvtColor(np.clip(blended, 0, 255).astype(np.uint8), cv2.COLOR_RGB2BGR))

    stats = {
        "unlocked": sum(1 for v in decoded.values() if v["state"] == "unlocked"),
        "locked": sum(1 for v in decoded.values() if v["state"] == "locked"),
        "ambiguous": sum(1 for v in decoded.values() if v["state"] == "ambiguous"),
        "total": len(decoded),
    }
    return {
        "version_id": version_id,
        "nodes": {str(k): v for k, v in sorted(decoded.items())},
        "stats": stats,
    }


def _parse_hud_currencies(ocr_items: list[dict]) -> tuple[int | None, int | None]:
    """Parse top-right HUD numbers: [token_count (奇想星/星之魄), bling_count (噗灵)]."""
    nums: list[tuple[int, int]] = []
    for item in ocr_items:
        text = re.sub(r"[,，\s]", "", str(item.get("text") or ""))
        box = item.get("box") or [0, 0, 0, 0]
        for m in re.finditer(r"\d+", text):
            nums.append((int(box[0]), int(m.group(0))))
    nums.sort(key=lambda pair: pair[0])
    if len(nums) >= 2:
        return nums[0][1], nums[-1][1]
    if len(nums) == 1:
        # Left side is token count (<1140), right side is bling (>=1140)
        return (nums[0][1], None) if nums[0][0] < 1140 else (None, nums[0][1])
    return None, None


class InfiniteHeartScanner:
    """Navigates to 无限之心, zooms out to minimum view on all 3 shards, and decodes unlocks."""

    def __init__(self, runtime, log_dir: Path | None = None):
        self.rt = runtime
        self.log_dir = log_dir
        self.summary: dict = {}

    def _ocr_box(self, image, roi: list[int]) -> list[dict]:
        res = self.rt.context.run_recognition(
            "MaaNikki_InfiniteHeart_Ocr",
            image,
            pipeline_override={
                "MaaNikki_InfiniteHeart_Ocr": {
                    "recognition": "OCR",
                    "roi": roi,
                    "only_rec": False,
                    "action": "DoNothing",
                }
            },
        )
        if not res or not res.hit:
            return []
        raw = res.all_results or ([res.best_result] if res.best_result else [])
        out = []
        for entry in raw:
            text = getattr(entry, "text", None)
            box = getattr(entry, "box", None)
            if text:
                out.append({"text": str(text), "box": list(box) if box else [0, 0, 0, 0]})
        return out

    def _detect_shard_id(self, image) -> int | None:
        texts = self.rt.text(SHARD_NAME_ROI, image=image)
        if "心愿" in texts or "原野" in texts:
            return 1
        if "伊赞" in texts:
            return 2
        if "星海" in texts:
            return 3
        return None

    def _is_on_heart_page(self, image) -> bool:
        if self.rt.hit("MaaNikki_InfiniteHeartReady", image=image):
            return True
        texts = self.rt.text(HEADER_TITLE_ROI, image=image)
        return "无限之心" in texts

    def _ensure_min_zoom(self) -> bool:
        """Zoom all the way out to GLOBAL (minimum zoom) where camera translation is locked to (0, 0)."""
        if self.rt.stopped:
            raise TaskStopped()
        # Click the zoom-out '-' button 5 times (at most 4 zoom levels) + click left end of track.
        for _ in range(5):
            if self.rt.stopped:
                raise TaskStopped()
            if not self.rt.action("Click", target=ZOOM_OUT_BTN) or not self.rt.pause(0.16):
                return False
        if not self.rt.action("Click", target=ZOOM_MIN_TRACK):
            return False
        # Wait for the 0.45s zoom-settle animation to finish completely before capturing.
        return bool(self.rt.pause(0.75))

    def _enter_infinite_heart(self) -> bool:
        from wardrobe.scanner import PEAR_PAL_NEXT_PAGE, PEAR_PAL_PREV_PAGE

        frame = self.rt.capture()
        if frame is not None and self._is_on_heart_page(frame):
            return True
        if self.rt.ui.find_menu_entry(node="MaaNikki_InfiniteHeart_Entrance", scroll=True):
            self.rt.pause(1.5)
            frame = self.rt.capture()
            return frame is not None and self._is_on_heart_page(frame)
        if not self.rt.ui.menu():
            return False
        frame = self.rt.capture()
        for arrow in (PEAR_PAL_PREV_PAGE, PEAR_PAL_PREV_PAGE, PEAR_PAL_NEXT_PAGE, PEAR_PAL_NEXT_PAGE):
            if frame is not None and self.rt.hit("MaaNikki_InfiniteHeart_Entrance", image=frame):
                break
            if not self.rt.action("Click", target=arrow) or not self.rt.pause(0.45):
                return False
            frame = self.rt.capture()
            if frame is not None and not self.rt.hit("MaaNikki_MenuReady", image=frame):
                return self._is_on_heart_page(frame)
        if frame is None or not self.rt.hit("MaaNikki_InfiniteHeart_Entrance", image=frame):
            return False
        entered = self.rt.ui.enter_page(
            lambda: self.rt.click_template("MaaNikki_InfiniteHeart_Entrance", verify=False),
            "MaaNikki_InfiniteHeartReady",
            source="MaaNikki_MenuReady",
        )
        if not entered:
            return False
        # Wait past the initial opener animation (~1.5s)
        self.rt.pause(1.5)
        frame = self.rt.capture()
        return frame is not None and self._is_on_heart_page(frame)

    def run(self) -> bool:
        if self.rt.stopped:
            raise TaskStopped()
        account = AccountState.for_runtime(ROOT, self.rt)
        if not self._enter_infinite_heart():
            raise RuntimeError("未能进入「无限之心」界面，请确认游戏已停留在主界面或无限之心页面。")

        shards_result: dict[str, dict] = {}
        currencies: dict[str, int] = {}
        latest_bling: int | None = None

        for vid, shard_name, tab_target in SHARD_TABS:
            if self.rt.stopped:
                raise TaskStopped()
            frame = self.rt.capture()
            cur = self._detect_shard_id(frame) if frame is not None else None
            if cur != vid:
                if not self.rt.action("Click", target=tab_target) or not self.rt.pause(0.6):
                    raise TaskStopped()
                for _ in range(6):
                    if self.rt.stopped:
                        raise TaskStopped()
                    frame = self.rt.capture()
                    cur = self._detect_shard_id(frame) if frame is not None else None
                    if cur == vid:
                        break
                    self.rt.pause(0.3)
                if cur != vid:
                    self.rt.log(f"未检测到「{shard_name}」（可能尚未解锁该区域碎片），已跳过。")
                    continue

            self.rt.log(f"正在读取「{shard_name}」最高视野解锁状态...")
            if not self._ensure_min_zoom():
                raise TaskStopped()
            frame = self.rt.capture()
            if frame is None:
                raise RuntimeError(f"未能截取「{shard_name}」画面。")
            tok_cnt, bling_cnt = _parse_hud_currencies(self._ocr_box(frame, HUD_CURRENCY_ROI))
            if tok_cnt is not None:
                currencies[str(vid)] = tok_cnt
            if bling_cnt is not None:
                latest_bling = bling_cnt

            dbg = (self.log_dir / f"shard_{vid}.png") if self.log_dir else None
            decoded = decode_shard_image(frame, vid, debug_out=dbg)
            shards_result[str(vid)] = decoded
            st = decoded["stats"]
            self.rt.log(
                f"「{shard_name}」识别完成：已解锁 {st['unlocked']} / {st['total']}"
                + (f"，待确认 {st['ambiguous']}" if st["ambiguous"] else "")
                + (f"，当前持有 {tok_cnt}" if tok_cnt is not None else "")
                + "。"
            )

        if not shards_result:
            raise RuntimeError("未能读取任何无限之心碎片。")

        now = datetime.now(timezone.utc).isoformat()
        merged_nodes: dict[str, dict] = {}
        for sdata in shards_result.values():
            merged_nodes.update(sdata["nodes"])

        scan_payload = {
            "schema_version": 1,
            "updated_at": now,
            "complete": len(shards_result) == len(SHARD_TABS),
            "scanned_shards": sorted(int(k) for k in shards_result),
            "currencies": currencies,
            "bling": latest_bling,
            "nodes": merged_nodes,
        }

        with account_lock(ROOT):
            data = account.load()
            heart_section = data.setdefault("infinite_heart", {})
            heart_section["last_scan"] = scan_payload
            # Keep any existing user manual overrides intact!
            heart_section.setdefault("overrides", {})
            data["updated_at"] = now
            write_json(account.path, data)

        self.summary = {
            "shards": len(shards_result),
            "unlocked": sum(s["stats"]["unlocked"] for s in shards_result.values()),
            "total": sum(s["stats"]["total"] for s in shards_result.values()),
            "ambiguous": sum(s["stats"]["ambiguous"] for s in shards_result.values()),
        }
        return True
