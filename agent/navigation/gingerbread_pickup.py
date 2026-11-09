"""Left-side material pickup toast reader, adaptive stationary waiting, and map-data telemetry inference.

Reads the in-game left-side item acquisition toast region on 1280x720 frames during
Gingerbread-Man route execution, determines when all reachable materials at a stationary
stop have been collected, and compares actual pickups against Pear-Pal 3D map spawner data
to diagnose wait-time accuracy and effective pickup radius/elevation bounds.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
import math
from pathlib import Path
import re
from typing import Iterable
from task_control import task_time as time

from file_io import write_json

M_PER_MAP_PX = 0.90

# On 1280x720:
# - Circular item icon badge sits around x = 22..46
# - Item name & count ("发卡蚱蜢 × 1", "拉夫领鱼 × 1.8kg") starts at x ≈ 48 and extends up to x ≈ 275
# - Up to 4-5 toast rows stack vertically in y = 365..595
TOAST_TEXT_ROI = [44, 365, 236, 230]
TOAST_FULL_ROI = [15, 365, 265, 230]
TOAST_LEGACY_ROI = [15, 451, 147, 133]

BYPRODUCT_KEYWORDS = ("心得", "精粹", "种子", "噗灵", "经验", "探索度")
UI_NOISE_KEYWORDS = ("任务", "追踪", "距离", "前往", "点击", "按", "键", "查看", "地图")


@dataclass
class ToastObservation:
    raw_text: str
    name: str
    amount: int
    kind: str  # "material" | "byproduct"
    y_center: float
    weight_kg: float | None = None
    matched_catalog: bool = False


@dataclass
class ActiveToastSlot:
    name: str
    kind: str
    amount: int
    y_center: float
    first_seen: float
    last_seen: float


@dataclass
class NearbySpawnerInfo:
    id: str
    name: str
    kind: str
    x: float
    y: float
    z: float
    d_2d_m: float
    dz_m: float


class MaterialNameResolver:
    """Resolves OCR text from the left-side toast against known open-world material names."""

    def __init__(self, root: Path):
        self.root = root
        self.known_names: set[str] = set()
        self.name_to_kind: dict[str, str] = {}
        self._load_dictionaries()

    def _load_dictionaries(self) -> None:
        cat_path = self.root / "resource/navigation/materials_catalog_v1.json"
        if cat_path.is_file():
            try:
                raw = json.loads(cat_path.read_text(encoding="utf-8"))
                for entry in (raw.get("catalogs") or {}).values():
                    nm = str(entry.get("name") or "").strip()
                    kind = str(entry.get("kind") or "plant")
                    if not nm or nm == "钓鱼点":
                        continue
                    self._register_name(nm, kind)
                for plist in (raw.get("spawners") or {}).values():
                    for item in plist:
                        if isinstance(item, (list, tuple)):
                            cond = str(item[4]) if len(item) >= 5 else ""
                        elif isinstance(item, dict):
                            cond = str(item.get("condition") or item.get("note") or "")
                        else:
                            cond = ""
                        if "鱼" in cond or "获得" in cond:
                            for m in re.findall(r"[\u4e00-\u9fa5]{2,8}鱼", cond):
                                if m not in ("的鱼", "所有鱼", "常见鱼"):
                                    self._register_name(m, "fish")
            except Exception:
                pass
        ui_cat_path = self.root / "resource/image/game/catalog.json"
        if ui_cat_path.is_file():
            try:
                raw_ui = json.loads(ui_cat_path.read_text(encoding="utf-8"))
                for nm, info in (raw_ui.get("materials") or {}).items():
                    m_type = str((info or {}).get("type") or "plant")
                    self._register_name(nm, m_type)
            except Exception:
                pass

    def _register_name(self, name: str, kind: str) -> None:
        for part in name.split("/"):
            clean = part.strip()
            if len(clean) >= 2:
                self.known_names.add(clean)
                self.name_to_kind.setdefault(clean, kind)

    def resolve(self, raw_candidate: str, expected_names: Iterable[str] = ()) -> tuple[str, bool]:
        """Return (canonical_name, matched_known_catalog)."""
        cand = re.sub(r"[^\u4e00-\u9fa5]", "", raw_candidate)
        if len(cand) < 2:
            return "", False
        exp_list = []
        for e in expected_names:
            for part in str(e).split("/"):
                p = part.strip()
                if len(p) >= 2 and p not in exp_list:
                    exp_list.append(p)

        # 1. Exact match against expected or global catalog
        if cand in exp_list:
            return cand, True
        if cand in self.known_names:
            return cand, True

        # 2. Substring match against expected materials near this waypoint first
        for exp in exp_list:
            if (len(cand) >= 2 and cand in exp) or (len(exp) >= 2 and exp in cand):
                return exp, True
            if len(cand) == len(exp) and len(exp) >= 3:
                diff = sum(1 for a, b in zip(cand, exp) if a != b)
                if diff <= 1:
                    return exp, True

        # 3. Substring / 1-char fuzzy match against global catalog
        best_match = ""
        best_score = 0
        for known in self.known_names:
            if known in cand:
                if len(known) > best_score:
                    best_match = known
                    best_score = len(known)
            elif len(cand) >= 3 and cand in known:
                if len(cand) > best_score:
                    best_match = known
                    best_score = len(cand)
            elif len(cand) == len(known) and len(known) >= 3:
                diff = sum(1 for a, b in zip(cand, known) if a != b)
                if diff == 1 and len(known) - 1 > best_score:
                    best_match = known
                    best_score = len(known) - 1
        if best_match:
            return best_match, True
        return cand, False


class LeftMaterialToastReader:
    """Stateful reader for the left-side HUD item acquisition toast stack."""

    SLOT_EXPIRE_SECONDS = 2.8

    def __init__(self, runtime, resolver: MaterialNameResolver, expected_names: list[str] | None = None):
        self.rt = runtime
        self.resolver = resolver
        self.expected_names: list[str] = list(expected_names or [])
        self.active_slots: list[ActiveToastSlot] = []
        self.last_toast_activity_at: float = -math.inf
        self.last_material_pickup_at: float = -math.inf

    def set_expected_names(self, names: Iterable[str]) -> None:
        self.expected_names = [str(n).strip() for n in names if str(n).strip()]

    @staticmethod
    def _parse_row_text(raw_text: str, y_center: float, resolver: MaterialNameResolver,
                        expected_names: list[str]) -> ToastObservation | None:
        compact = re.sub(r"\s+", "", raw_text or "")
        if len(compact) < 2:
            return None
        if any(noise in compact for noise in UI_NOISE_KEYWORDS):
            return None
        # Check fish weight pattern first: e.g. "拉夫领鱼×1.8kg" or "纱裙鱼 2.1kg"
        fish_m = re.search(r"([\u4e00-\u9fa5]{2,10})[×xX*+]?(\d+(?:\.\d+)?)kg", compact, re.IGNORECASE)
        if fish_m:
            raw_nm = re.sub(r"^获得", "", fish_m.group(1))
            wt = float(fish_m.group(2))
            nm, matched = resolver.resolve(raw_nm, expected_names)
            return ToastObservation(
                raw_text=compact,
                name=nm or raw_nm,
                amount=1,
                kind="material",
                y_center=y_center,
                weight_kg=wt,
                matched_catalog=matched,
            )

        # Standard item pattern: e.g. "小绵菊×1", "发卡蚱蜢 x 2", "敏捷捕虫心得×1"
        qty_m = re.search(r"([\u4e00-\u9fa5]{2,12})(?:[×xX*+:]|\s*)(\d{1,3})$", compact)
        has_explicit_qty = qty_m is not None
        if qty_m:
            raw_nm = re.sub(r"^获得", "", qty_m.group(1))
            amt = max(1, min(20, int(qty_m.group(2))))
        else:
            raw_nm = re.sub(r"^获得", "", re.sub(r"[^\u4e00-\u9fa5]", "", compact))
            amt = 1

        if len(raw_nm) < 2:
            return None

        if any(bp in raw_nm for bp in BYPRODUCT_KEYWORDS):
            return ToastObservation(
                raw_text=compact,
                name=raw_nm,
                amount=amt,
                kind="byproduct",
                y_center=y_center,
                matched_catalog=False,
            )

        nm, matched = resolver.resolve(raw_nm, expected_names)
        # Accept if it matched a known material OR had an explicit "× N" toast suffix
        if not matched and not has_explicit_qty:
            return None

        return ToastObservation(
            raw_text=compact,
            name=nm or raw_nm,
            amount=amt,
            kind="material",
            y_center=y_center,
            matched_catalog=matched,
        )

    def read_observations(self, image=None) -> list[ToastObservation]:
        result = self.rt.recognize("MaaNikki_Daily_OCR", {"roi": TOAST_TEXT_ROI}, image=image)
        rows = []
        if result and result.hit:
            rows = getattr(result, "filtered_results", None) or getattr(result, "all_results", None) or [result.best_result]
        obs_list: list[ToastObservation] = []
        for idx, row in enumerate(rows or []):
            if row is None:
                continue
            txt = getattr(row, "text", "") or ""
            box = getattr(row, "box", None)
            if isinstance(box, (list, tuple)) and len(box) >= 4:
                y_c = float(box[1]) + float(box[3]) * 0.5
            else:
                y_c = float(TOAST_TEXT_ROI[1] + 28 * idx)
            parsed = self._parse_row_text(txt, y_c, self.resolver, self.expected_names)
            if parsed is not None:
                obs_list.append(parsed)
        obs_list.sort(key=lambda o: o.y_center)
        return obs_list

    def reset_baseline(self, image=None) -> None:
        """Seed currently visible toasts so pre-existing rows before arrival are not re-counted."""
        now = time.monotonic()
        self.active_slots = []
        for obs in self.read_observations(image=image):
            self.active_slots.append(
                ActiveToastSlot(
                    name=obs.name,
                    kind=obs.kind,
                    amount=obs.amount,
                    y_center=obs.y_center,
                    first_seen=now,
                    last_seen=now,
                )
            )
    def poll(self, image=None) -> tuple[list[ToastObservation], bool]:
        """Poll the left-side toast zone and return (newly_picked_materials, any_new_toast_activity)."""
        now = time.monotonic()
        self.active_slots = [
            s for s in self.active_slots if (now - s.last_seen) <= self.SLOT_EXPIRE_SECONDS
        ]
        observations = self.read_observations(image=image)
        if not observations:
            return [], False

        new_materials: list[ToastObservation] = []
        any_new_activity = False
        used_slot_indices: set[int] = set()

        for obs in observations:
            # Find matching active slot of the same name (or same vertical position within 0.6s for OCR jitter)
            best_si = -1
            best_dy = 999.0
            for si, slot in enumerate(self.active_slots):
                if si in used_slot_indices:
                    continue
                if slot.name == obs.name and slot.kind == obs.kind:
                    # Existing toast either stays in place (|dy| <= 14) or slides UP (obs.y_center < slot.y_center)
                    dy = slot.y_center - obs.y_center
                    if -14.0 <= dy <= 68.0 and abs(dy) < best_dy:
                        best_dy = abs(dy)
                        best_si = si
                elif slot.kind == obs.kind and abs(slot.y_center - obs.y_center) <= 10.0 and (now - slot.last_seen) <= 0.65:
                    best_dy = abs(slot.y_center - obs.y_center)
                    best_si = si

            if best_si >= 0:
                used_slot_indices.add(best_si)
                slot = self.active_slots[best_si]
                slot.y_center = obs.y_center
                slot.last_seen = now
                if obs.kind == "material" and obs.amount > slot.amount:
                    delta = obs.amount - slot.amount
                    slot.amount = obs.amount
                    any_new_activity = True
                    self.last_toast_activity_at = now
                    self.last_material_pickup_at = now
                    new_materials.append(
                        ToastObservation(
                            raw_text=obs.raw_text,
                            name=obs.name,
                            amount=delta,
                            kind="material",
                            y_center=obs.y_center,
                            weight_kg=obs.weight_kg,
                            matched_catalog=obs.matched_catalog,
                        )
                    )
            else:
                new_slot = ActiveToastSlot(
                    name=obs.name,
                    kind=obs.kind,
                    amount=obs.amount,
                    y_center=obs.y_center,
                    first_seen=now,
                    last_seen=now,
                )
                self.active_slots.append(new_slot)
                used_slot_indices.add(len(self.active_slots) - 1)
                any_new_activity = True
                self.last_toast_activity_at = now
                if obs.kind == "material":
                    self.last_material_pickup_at = now
                    new_materials.append(obs)

        return new_materials, any_new_activity


class GingerbreadRouteAnalyzer:
    """Queries Pear-Pal 3D map spawners around each waypoint and infers pickup timing & range."""

    def __init__(self, root: Path, map_id: str):
        self.root = root
        self.map_id = map_id
        self.resolver = MaterialNameResolver(root)
        self.spawners: list[dict] = []
        self.catalogs: dict[str, dict] = {}
        self.claimed_spawner_ids: set[str] = set()
        self.waypoint_reports: list[dict] = []
        self.in_stride_pickups: list[dict] = []
        self._load_map_spawners()
    def _load_map_spawners(self) -> None:
        cat_path = self.root / "resource/navigation/materials_catalog_v1.json"
        if not cat_path.is_file():
            return
        try:
            raw = json.loads(cat_path.read_text(encoding="utf-8"))
            self.catalogs = raw.get("catalogs") or {}
            self.spawners = list((raw.get("spawners") or {}).get(self.map_id) or [])
        except Exception:
            self.spawners = []

    def query_nearby_spawners(self, x: float, y: float, max_radius_m: float = 18.0,
                              claim_within_m: float = 13.8) -> list[NearbySpawnerInfo]:
        max_px = max_radius_m / M_PER_MAP_PX
        near_raw = []
        for idx, s in enumerate(self.spawners):
            if isinstance(s, (list, tuple)):
                if len(s) < 4:
                    continue
                sid = f"{self.map_id}_{idx}"
                cid = str(s[0])
                sx, sy, sz = float(s[1]), float(s[2]), float(s[3])
                raw_name = ""
            elif isinstance(s, dict):
                sid = str(s.get("id") or f"{self.map_id}_{idx}")
                cid = str(s.get("cid") or "")
                sx, sy, sz = float(s.get("x") or 0.0), float(s.get("y") or 0.0), float(s.get("z") or 0.0)
                raw_name = str(s.get("name") or "")
            else:
                continue
            if sid in self.claimed_spawner_ids:
                continue
            d_px = math.hypot(sx - x, sy - y)
            if d_px <= max_px:
                d_m = round(d_px * M_PER_MAP_PX, 2)
                cat = self.catalogs.get(cid) or {}
                near_raw.append((sid, str(cat.get("name") or raw_name or "未知"),
                                 str(cat.get("kind") or "plant"), sx, sy, sz, d_m))

        if not near_raw:
            return []
        # Estimate local ground elevation from nearest spawners
        near_raw.sort(key=lambda item: item[6])
        closest_z = [item[5] for item in near_raw[: min(5, len(near_raw))]]
        closest_z.sort()
        ref_z = closest_z[len(closest_z) // 2]

        results: list[NearbySpawnerInfo] = []
        for sid, nm, kind, sx, sy, sz, d_m in near_raw:
            dz_m = round(abs(sz - ref_z), 2)
            results.append(
                NearbySpawnerInfo(
                    id=sid, name=nm, kind=kind, x=sx, y=sy, z=sz, d_2d_m=d_m, dz_m=dz_m
                )
            )
            if d_m <= claim_within_m and dz_m <= 5.0:
                self.claimed_spawner_ids.add(sid)
        return results

    @staticmethod
    def conservative_budget_seconds(nearby: list[NearbySpawnerInfo], configured_s: float) -> tuple[float, float, float]:
        """Return (min_dwell_s, quiet_window_s, max_ceiling_s) for a stationary stop."""
        in_15m = [s for s in nearby if s.d_2d_m <= 15.0 and s.dz_m <= 5.5]
        plants = sum(1 for s in in_15m if s.kind == "plant")
        insects = sum(1 for s in in_15m if s.kind == "insect")
        animals = sum(1 for s in in_15m if s.kind == "animal")
        fish = sum(1 for s in in_15m if s.kind == "fish")
        has_slow = (insects + animals + fish) > 0

        map_est = (
            (3.5 + max(0, plants - 1) * 2.6 if plants else 0.0)
            + insects * 8.0
            + animals * 9.5
            + fish * 14.0
        )
        quiet_s = 7.0 if has_slow else 5.5
        min_dwell_s = 6.5 if has_slow else (4.5 if len(in_15m) >= 2 else 3.5)
        max_ceiling_s = min(120.0, max(configured_s * 1.4, map_est + 6.0, min_dwell_s + quiet_s))
        return round(min_dwell_s, 1), round(quiet_s, 1), round(max_ceiling_s, 1)
    def evaluate_waypoint(
        self,
        *,
        wp_index: int,
        x: float,
        y: float,
        configured_wait_s: float,
        waited_s: float,
        first_pickup_s: float | None,
        last_pickup_s: float | None,
        exit_reason: str,
        picked_events: list[dict],
        nearby: list[NearbySpawnerInfo],
    ) -> dict:
        in_8m = [s for s in nearby if s.d_2d_m <= 8.0 and s.dz_m <= 4.5]
        in_12m = [s for s in nearby if s.d_2d_m <= 12.0 and s.dz_m <= 4.5]
        in_15m = [s for s in nearby if s.d_2d_m <= 15.0 and s.dz_m <= 5.5]
        outer_12_15m = [s for s in in_15m if s.d_2d_m > 12.0]
        high_dz_15m = [s for s in nearby if s.d_2d_m <= 15.0 and s.dz_m > 4.0]

        expected_by_name: dict[str, int] = {}
        for s in in_15m:
            expected_by_name[s.name] = expected_by_name.get(s.name, 0) + 1

        picked_by_name: dict[str, int] = {}
        picked_total = 0
        for ev in picked_events:
            nm = str(ev.get("material") or "")
            amt = int(ev.get("amount") or 1)
            picked_by_name[nm] = picked_by_name.get(nm, 0) + amt
            picked_total += amt

        effective_finish_s = round(last_pickup_s, 2) if last_pickup_s is not None else 0.0
        recommended_wait_s = round(effective_finish_s + 2.0, 1) if picked_total > 0 else 0.0
        sec_per_item = round(effective_finish_s / picked_total, 2) if picked_total > 0 else None

        # 1. Time judgment
        if picked_total == 0:
            time_verdict = "无新拾取（可能今日已采或超出范围）"
        elif effective_finish_s > configured_wait_s + 1.2:
            diff = round(effective_finish_s - configured_wait_s, 1)
            time_verdict = f"原设定偏短（实测最后拾取于 {effective_finish_s}s，比原 {configured_wait_s}s 多需 {diff}s）"
        elif configured_wait_s > effective_finish_s + 4.5:
            saved = round(configured_wait_s - effective_finish_s, 1)
            time_verdict = f"保守设定充裕（{effective_finish_s}s 即拾取完毕，较设定 {configured_wait_s}s 富余 {saved}s）"
        else:
            time_verdict = f"时间吻合（实测 {effective_finish_s}s 拾取完毕，设定 {configured_wait_s}s）"

        # 2. Range / elevation judgment
        sorted_15m = sorted(in_15m, key=lambda s: s.d_2d_m)
        inferred_radius_m = None
        if picked_total > 0 and sorted_15m:
            idx = min(len(sorted_15m) - 1, max(0, picked_total - 1))
            inferred_radius_m = round(sorted_15m[idx].d_2d_m, 2)

        exp_cnt = len(in_15m)
        if exp_cnt == 0 and picked_total > 0:
            range_verdict = f"实测拾取 {picked_total} 个（含行进尾随或单株散点）"
        elif exp_cnt == 0:
            range_verdict = "地图 15m 内无未统计刷新点"
        elif picked_total >= exp_cnt:
            max_d = max((s.d_2d_m for s in in_15m), default=0.0)
            range_verdict = f"15m 范围完全覆盖（预计 {exp_cnt} 个，实测拾取 {picked_total} 个，最远 {max_d}m）"
        elif picked_total == 0:
            range_verdict = f"预计 15m 内有 {exp_cnt} 个（8m 内 {len(in_8m)} 个），实测未弹出提示（可能已采完或未触发）"
        else:
            missing = exp_cnt - picked_total
            if len(outer_12_15m) >= missing and picked_total >= len(in_12m):
                range_verdict = (
                    f"外圈 12–15m 有 {missing} 个未触发（实测拾取 {picked_total}/{exp_cnt} 个，"
                    f"推断有效半径约 {inferred_radius_m or 12.0}m）"
                )
            elif len(high_dz_15m) >= missing:
                range_verdict = (
                    f"高低差 >4m 的 {len(high_dz_15m)} 个点位疑似未触发（实测 {picked_total}/{exp_cnt} 个）"
                )
            else:
                range_verdict = (
                    f"部分覆盖（预计 15m 内 {exp_cnt} 个，12m 内 {len(in_12m)} 个，实测拾取 {picked_total} 个，"
                    f"对应半径约 {inferred_radius_m or 10.0}m）"
                )

        report = {
            "waypoint_index": wp_index,
            "x": round(x, 2),
            "y": round(y, 2),
            "configured_wait_s": round(configured_wait_s, 1),
            "waited_s": round(waited_s, 2),
            "first_pickup_s": round(first_pickup_s, 2) if first_pickup_s is not None else None,
            "last_pickup_s": effective_finish_s if last_pickup_s is not None else None,
            "recommended_wait_s": recommended_wait_s,
            "sec_per_item": sec_per_item,
            "exit_reason": exit_reason,
            "picked_total": picked_total,
            "picked_by_name": picked_by_name,
            "picked_events": picked_events,
            "expected_0_8m": len(in_8m),
            "expected_0_12m": len(in_12m),
            "expected_0_15m": exp_cnt,
            "expected_12_15m": len(outer_12_15m),
            "expected_high_dz": len(high_dz_15m),
            "expected_by_name": expected_by_name,
            "inferred_radius_m": inferred_radius_m,
            "time_verdict": time_verdict,
            "range_verdict": range_verdict,
            "nearby_spawners": [
                {"name": s.name, "kind": s.kind, "d_2d_m": s.d_2d_m, "dz_m": s.dz_m}
                for s in sorted_15m[:20]
            ],
        }
        self.waypoint_reports.append(report)
        return report
    def build_route_summary(self, route_id: str, route_title: str) -> dict:
        total_picked = sum(int(r.get("picked_total", 0)) for r in self.waypoint_reports)
        in_stride_cnt = sum(int(ev.get("amount", 1)) for ev in self.in_stride_pickups)
        grand_total_picked = total_picked + in_stride_cnt

        total_exp_15m = sum(int(r.get("expected_0_15m", 0)) for r in self.waypoint_reports)
        total_exp_12m = sum(int(r.get("expected_0_12m", 0)) for r in self.waypoint_reports)
        total_cfg_wait = round(sum(float(r.get("configured_wait_s", 0.0)) for r in self.waypoint_reports), 1)
        total_waited = round(sum(float(r.get("waited_s", 0.0)) for r in self.waypoint_reports), 1)
        total_effective_finish = round(
            sum(float(r.get("last_pickup_s") or 0.0) for r in self.waypoint_reports), 1
        )

        picked_by_name: dict[str, int] = {}
        for r in self.waypoint_reports:
            for nm, cnt in (r.get("picked_by_name") or {}).items():
                picked_by_name[nm] = picked_by_name.get(nm, 0) + int(cnt)
        for ev in self.in_stride_pickups:
            nm = str(ev.get("material") or "")
            if nm:
                picked_by_name[nm] = picked_by_name.get(nm, 0) + int(ev.get("amount", 1))

        active_wps = [r for r in self.waypoint_reports if int(r.get("picked_total", 0)) > 0]
        too_short_cnt = sum(
            1 for r in active_wps if float(r.get("last_pickup_s") or 0.0) > float(r.get("configured_wait_s", 0.0)) + 1.2
        )
        too_long_cnt = sum(
            1 for r in active_wps if float(r.get("configured_wait_s", 0.0)) > float(r.get("last_pickup_s") or 0.0) + 4.5
        )
        radii = [float(r["inferred_radius_m"]) for r in active_wps if r.get("inferred_radius_m") is not None]
        avg_inferred_r = round(sum(radii) / len(radii), 2) if radii else None
        max_inferred_r = round(max(radii), 2) if radii else None
        avg_sec_per_item = round(total_effective_finish / max(1, total_picked), 2) if total_picked > 0 else None

        if total_picked == 0 and in_stride_cnt == 0:
            overall_diagnosis = "本条路线未读取到左侧新材料提示（可能材料今日已采完、冷却中或未开启姜饼人能力）。"
        else:
            parts = []
            if too_short_cnt > 0:
                parts.append(f"{too_short_cnt} 个驻留点原设定时间偏短（已通过左侧提示自动延时补齐）")
            if too_long_cnt > 0:
                parts.append(f"{too_long_cnt} 个驻留点保守设定充裕（静默无新材料后已自动提前结束等待）")
            if not parts:
                parts.append("各驻留点等待时间与实测拾取耗时基本吻合")

            if total_exp_15m > 0:
                cov_15 = round(total_picked * 100.0 / total_exp_15m, 1)
                if total_picked < total_exp_15m and total_picked >= total_exp_12m > 0:
                    parts.append(
                        f"15m 预计 {total_exp_15m} 个 / 12m 预计 {total_exp_12m} 个，实测驻留拾取 {total_picked} 个（覆盖率 {cov_15}%），"
                        f"12–15m 外圈存在漏捡，建议安全估算半径设为 {max_inferred_r or 12.2}m"
                    )
                else:
                    parts.append(
                        f"地图 15m 预计 {total_exp_15m} 个，实测拾取 {grand_total_picked} 个（驻留 {total_picked} + 行进 {in_stride_cnt}），"
                        f"单件平均耗时约 {avg_sec_per_item or 2.5}s/个"
                    )
            overall_diagnosis = "；".join(parts) + "。"

        return {
            "timestamp": datetime.now().astimezone().isoformat(),
            "route_id": route_id,
            "route_title": route_title,
            "map_id": self.map_id,
            "waypoints_analyzed": len(self.waypoint_reports),
            "active_waypoints": len(active_wps),
            "total_picked": grand_total_picked,
            "stationary_picked": total_picked,
            "in_stride_picked": in_stride_cnt,
            "picked_by_name": picked_by_name,
            "expected_0_15m": total_exp_15m,
            "expected_0_12m": total_exp_12m,
            "configured_wait_total_s": total_cfg_wait,
            "effective_pickup_finish_total_s": total_effective_finish,
            "actual_waited_total_s": total_waited,
            "avg_sec_per_item": avg_sec_per_item,
            "avg_inferred_radius_m": avg_inferred_r,
            "max_inferred_radius_m": max_inferred_r,
            "too_short_waypoints": too_short_cnt,
            "too_long_waypoints": too_long_cnt,
            "overall_diagnosis": overall_diagnosis,
            "waypoints": self.waypoint_reports,
        }


def save_gingerbread_telemetry(root: Path, summary: dict, run_directory: Path | None = None) -> None:
    try:
        nav_dir = root / "logs/navigation"
        nav_dir.mkdir(parents=True, exist_ok=True)
        with (nav_dir / "gingerbread_telemetry.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(summary, ensure_ascii=False) + "\n")
        if run_directory is not None and run_directory.is_dir():
            write_json(run_directory / "gingerbread_report.json", summary)
    except OSError:
        pass
