"""Terrain-aware route generator for Gingerbread-Man aura auto-collection.

Combines Pear-Pal 3D material spawn coordinates (x, y, z), Warp Spire checkpoints,
and unpacked 3D terrain height/slope/obstacle grids to synthesize high-yield
single-material farming routes and all-in-one sweep routes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import heapq
import json
import math
from pathlib import Path
import time
from typing import Iterable

import cv2
import numpy as np

from map_mask.pearpal import API, WORLDS, decode, localized_text, request

MAP_TO_WORLD = {v: k for k, v in WORLDS.items()}
GINGERBREAD_GROUPS = {
    17: "animal",
    18: "insect",
    19: "plant",
    20: "fish",
}
KIND_LABELS = {
    "plant": "植物采集",
    "insect": "昆虫采集",
    "animal": "动物清洁",
    "fish": "钓鱼点",
}
CM_PER_MAP_PX = 90.0
M_PER_MAP_PX = 0.90


@dataclass(frozen=True)
class GingerbreadSpec:
    """Exact reverse-engineered kinematics and 3D spherical range of the Gingerbread-Man ability.

    Derived from Patch 2872 Lua/Blueprint CDOs:
      - BP_ChristmasGingerbreadDetector_C: 3D SphereOverlapActors with BP_ScanRadius = 1500 cm (15.0m)
      - ChristmasGingerbreadProcessor: BP_ReplanTickInterval = 0.5s, BP_QueueCapacity = 2, sorted by 3D distance
      - BP_ChristmasGingerbreadMan_C:
          * MovingToTarget: BP_CollectMoveTotalTime = 2.0s (speed = dist / 2.0), BP_ArriveRadius = 100 cm (1.0m),
            MaxFollowDistance = 2000 cm (20.0m)
          * Collecting: Plant = 1.0s, Insect = 3.0s, Clean (Animal) = 4.0s, Fishing = 7.0s
          * CollectSuccess: AnimNotify_GingerbreadCollectComplete -> AnimNotify_GingerbreadCollectMontageExit (~0.55s),
            chaining directly to the next queued target without returning to Nikki.
    """
    aura_radius_m: float = 15.0
    safe_radius_m: float = 14.0
    pass_radius_m: float = 13.5
    max_dz_m: float = 10.5
    min_anchor_sep_m: float = 20.0
    run_speed_mps: float = 6.5
    teleport_overhead_s: float = 10.5
    replan_interval_s: float = 0.5
    move_total_time_s: float = 2.0
    arrive_radius_m: float = 1.0
    collect_exit_s: float = 0.55
    plant_collect_s: float = 1.0
    insect_collect_s: float = 3.35
    animal_collect_s: float = 4.5
    fish_collect_s: float = 8.2
    plant_first_s: float = 2.8
    plant_extra_s: float = 2.2
    insect_s: float = 5.4
    animal_s: float = 6.6
    fish_s: float = 9.8
    fish_casts_per_spot: int = 1

    @property
    def safe_radius_px(self) -> float:
        return self.safe_radius_m / M_PER_MAP_PX

    @property
    def pass_radius_px(self) -> float:
        return self.pass_radius_m / M_PER_MAP_PX

    @property
    def aura_radius_px(self) -> float:
        return self.aura_radius_m / M_PER_MAP_PX

    def item_collect_seconds(self, kind: str) -> float:
        if kind == "plant":
            return self.plant_collect_s
        if kind == "insect":
            return self.insect_collect_s
        if kind == "animal":
            return self.animal_collect_s
        return self.fish_collect_s * max(1, self.fish_casts_per_spot)

    def move_flight_seconds(self, dist_m: float) -> float:
        if dist_m <= self.arrive_radius_m:
            return 0.15
        return self.move_total_time_s * (dist_m - self.arrive_radius_m) / dist_m

    def service_seconds(
        self,
        points: Iterable["MaterialPoint"],
        anchor_xyz: tuple[float, float, float] | None = None,
    ) -> float:
        pts = list(points)
        if not pts:
            return 0.0
        if anchor_xyz is not None:
            ax, ay, az = anchor_xyz
        else:
            ax = sum(p.x for p in pts) / len(pts)
            ay = sum(p.y for p in pts) / len(pts)
            az = sum(p.z for p in pts) / len(pts)

        # ChristmasGingerbreadProcessor:SortTasks orders tasks by ascending 3D distance from Nikki
        ordered = sorted(
            pts,
            key=lambda p: (
                ((p.x - ax) * M_PER_MAP_PX) ** 2
                + ((p.y - ay) * M_PER_MAP_PX) ** 2
                + (p.z - az) ** 2
            ),
        )
        cur_x, cur_y, cur_z = ax, ay, az
        total_s = 0.0
        for p in ordered:
            dx_m = (p.x - cur_x) * M_PER_MAP_PX
            dy_m = (p.y - cur_y) * M_PER_MAP_PX
            dz_m = p.z - cur_z
            step_d_m = math.sqrt(dx_m * dx_m + dy_m * dy_m + dz_m * dz_m)
            move_s = self.move_flight_seconds(step_d_m)
            collect_s = self.item_collect_seconds(p.kind)
            total_s += move_s + collect_s + self.collect_exit_s
            if step_d_m > self.arrive_radius_m:
                ratio = (step_d_m - self.arrive_radius_m) / step_d_m
                cur_x += (p.x - cur_x) * ratio
                cur_y += (p.y - cur_y) * ratio
                cur_z += (p.z - cur_z) * ratio
        return total_s

    def net_wait_seconds(
        self,
        points: list["MaterialPoint"],
        chord_m: float = 16.0,
        anchor_xyz: tuple[float, float, float] | None = None,
        is_start_point: bool = False,
    ) -> float:
        if not points:
            return 0.0
        total_service = self.service_seconds(points, anchor_xyz=anchor_xyz)
        has_slow = any(p.kind in ("insect", "animal", "fish") for p in points)
        if is_start_point:
            # Includes 1.0s DelaySpawnTime + 0.25s initial replan latency at teleport spawn
            net = total_service + 1.25
        else:
            # While Nikki walks the last ~14m into the 15m sphere (~2.1s), the 0.5s replan tick
            # already dispatches Gingerbread Man toward the first item in-stride.
            approach_overlap_s = min(1.1, 0.45 * max(0.0, chord_m / max(1.0, self.run_speed_mps)))
            net = total_service - approach_overlap_s
        min_floor = 4.8 if has_slow else (2.4 if len(points) == 1 else 3.2)
        return round(min(150.0, max(min_floor, net)), 1)


@dataclass(frozen=True)
class MaterialPoint:
    id: str
    catalog_id: str
    name: str
    kind: str
    map_id: str
    x: float
    y: float
    z: float
    tag: str = ""
    condition: str = ""


@dataclass(frozen=True)
class SpireCheckpoint:
    name: str
    province: str
    region: str
    map_id: str
    x: float
    y: float
    z: float


@dataclass
class AnchorCluster:
    x: float
    y: float
    z: float
    points: list[MaterialPoint] = field(default_factory=list)
    score: float = 0.0
    target_count: int = 0
    wait_seconds: float = 0.0


@dataclass
class GeneratedRoutePlan:
    route_doc: dict
    checkpoint: SpireCheckpoint
    covered_points: list[MaterialPoint]
    target_count: int
    total_count: int
    counts_by_name: dict[str, int]
    counts_by_kind: dict[str, int]
    walk_distance_m: float
    estimated_seconds: float
    efficiency_per_min: float


class PearPalMaterialCatalog:
    """Fetches and caches all open-world material spawners and 3D spire elevations."""

    def __init__(self, root: Path):
        self.root = root
        self.builtin_path = root / "resource/navigation/materials_catalog_v1.json"
        self.cache_path = root / "cache/map-mask/materials_catalog_v1.json"
        self.checkpoints_path = root / "resource/navigation/checkpoints.json"
        self.catalogs: dict[str, dict] = {}
        self.name_to_ids: dict[str, list[str]] = {}
        self.points_by_map: dict[str, list[MaterialPoint]] = {}
        self.spires_by_map: dict[str, list[SpireCheckpoint]] = {}

    def load(self, refresh: bool = False) -> None:
        raw_data = None
        if not refresh and self.cache_path.is_file():
            if time.time() - self.cache_path.stat().st_mtime < 86400 * 30:
                try:
                    raw_data = json.loads(self.cache_path.read_text(encoding="utf-8"))
                except Exception:
                    raw_data = None
        if raw_data is None and not refresh and self.builtin_path.is_file():
            try:
                raw_data = json.loads(self.builtin_path.read_text(encoding="utf-8"))
            except Exception:
                raw_data = None
        if raw_data is None:
            raw_data = self._fetch_from_pearpal()
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps(raw_data, ensure_ascii=False), encoding="utf-8"
            )
        self._populate(raw_data)

    def _fetch_from_pearpal(self) -> dict:
        cat_res = decode(request(f"{API}/v1/strategy/map/catalog/list", {"client_id": 1106}))
        catalogs = {}
        for group in cat_res.get("data", {}).get("list", []):
            gid = group.get("id")
            if gid not in GINGERBREAD_GROUPS:
                continue
            kind = GINGERBREAD_GROUPS[gid]
            for entry in group.get("catalogs", []):
                cid = str(entry.get("id"))
                name = localized_text(entry.get("name"))
                if cid and name:
                    catalogs[cid] = {
                        "id": cid,
                        "name": name,
                        "kind": kind,
                        "group_id": gid,
                        "count": int(entry.get("count") or 0),
                    }

        spawners_by_map = {}
        pillars_by_map = {}
        for world_id, map_id in WORLDS.items():
            res = decode(
                request(
                    f"{API}/v1/strategy/map/spawner/list",
                    {"client_id": 1106, "world_id": world_id, "catalog_type_id": []},
                )
            )
            pts = []
            pillars = []
            for item in res.get("list", []):
                if item.get("stage_id") or item.get("stageId"):
                    continue
                item_world = str(item.get("world_id") or item.get("worldId") or world_id)
                if item_world != world_id:
                    continue
                cat = item.get("catalog") or item.get("catalog_id") or item.get("catalogId")
                if cat is None and item.get("catalog_ids"):
                    cat = item["catalog_ids"][0]
                if isinstance(cat, list):
                    cat = cat[0] if cat else None
                if isinstance(cat, dict):
                    cat = cat.get("id")
                cid = str(cat)
                try:
                    x = round(float(item["x"]) / CM_PER_MAP_PX, 2)
                    y = round(float(item["y"]) / CM_PER_MAP_PX, 2)
                    z = round(float(item.get("z", 0.0)) / 100.0, 2)
                except (KeyError, TypeError, ValueError):
                    continue
                if cid == "10":
                    pillars.append({"id": str(item.get("id")), "x": x, "y": y, "z": z})
                elif cid in catalogs:
                    cond = localized_text(item.get("description"))
                    pts.append({
                        "id": str(item.get("id")),
                        "cid": cid,
                        "x": x,
                        "y": y,
                        "z": z,
                        "tag": str(item.get("tag") or ""),
                        "cond": cond,
                    })
            spawners_by_map[map_id] = pts
            pillars_by_map[map_id] = pillars
        return {
            "schema_version": 1,
            "catalogs": catalogs,
            "spawners": spawners_by_map,
            "pillars": pillars_by_map,
        }

    def _populate(self, data: dict) -> None:
        self.catalogs = data.get("catalogs", {})
        self.name_to_ids.clear()
        for cid, info in self.catalogs.items():
            full_name = info["name"]
            self.name_to_ids.setdefault(full_name, []).append(cid)
            for part in full_name.split("/"):
                part = part.strip()
                if part and part != full_name:
                    self.name_to_ids.setdefault(part, []).append(cid)

        self.points_by_map.clear()
        for map_id, rows in data.get("spawners", {}).items():
            parsed = []
            for idx, r in enumerate(rows):
                if isinstance(r, list):
                    cid = str(r[0])
                    rx, ry, rz = float(r[1]), float(r[2]), float(r[3])
                    rcond = str(r[4]) if len(r) > 4 else ""
                    rid = f"{map_id}_{idx}"
                    rtag = ""
                else:
                    cid = str(r["cid"])
                    rx, ry, rz = float(r["x"]), float(r["y"]), float(r["z"])
                    rcond = r.get("cond", "")
                    rid = str(r.get("id", f"{map_id}_{idx}"))
                    rtag = r.get("tag", "")
                cinfo = self.catalogs.get(cid)
                if not cinfo:
                    continue
                parsed.append(
                    MaterialPoint(
                        id=rid,
                        catalog_id=cid,
                        name=cinfo["name"],
                        kind=cinfo["kind"],
                        map_id=map_id,
                        x=rx,
                        y=ry,
                        z=rz,
                        tag=rtag,
                        condition=rcond,
                    )
                )
            self.points_by_map[map_id] = parsed

        ckpt_raw = json.loads(self.checkpoints_path.read_text(encoding="utf-8")).get("maps", {})
        pillars_raw = data.get("pillars", {})
        self.spires_by_map.clear()
        for map_id, ckpts in ckpt_raw.items():
            pillars = pillars_raw.get(map_id, [])
            spires = []
            for ck in ckpts:
                cx, cy = float(ck["x"]), float(ck["y"])
                best_z = 0.0
                best_d = 25.0
                for p in pillars:
                    px = float(p[0]) if isinstance(p, list) else float(p["x"])
                    py = float(p[1]) if isinstance(p, list) else float(p["y"])
                    pz = float(p[2]) if isinstance(p, list) else float(p["z"])
                    d = math.hypot(px - cx, py - cy)
                    if d < best_d:
                        best_d = d
                        best_z = pz
                spires.append(
                    SpireCheckpoint(
                        name=ck["name"],
                        province=ck["province"],
                        region=ck["region"],
                        map_id=map_id,
                        x=cx,
                        y=cy,
                        z=best_z,
                    )
                )
            self.spires_by_map[map_id] = spires

    def resolve_catalog_ids(self, queries: Iterable[str]) -> set[str]:
        cids, _ = self.resolve_target_filter(queries)
        return cids

    @staticmethod
    def parse_fish_species(condition: str) -> tuple[str, ...]:
        if not condition:
            return ()
        txt = condition.replace("可能在该钓鱼点出现的鱼类：", "").replace("可能在该钓鱼点出现的鱼类:", "")
        return tuple(
            p.strip()
            for p in txt.replace("、", "/").replace("，", "/").replace(",", "/").split("/")
            if p.strip()
        )

    def resolve_target_filter(self, queries: Iterable[str]) -> tuple[set[str], set[str]]:
        matched: set[str] = set()
        fish_species: set[str] = set()
        known_fish: set[str] = set()
        for pts in self.points_by_map.values():
            for p in pts:
                if p.kind == "fish" and p.condition:
                    known_fish.update(self.parse_fish_species(p.condition))
        for q in queries:
            q = q.strip()
            if not q:
                continue
            if q in known_fish:
                matched.add("99")
                fish_species.add(q)
                continue
            if q in self.catalogs:
                matched.add(q)
                continue
            if q in KIND_LABELS or q in GINGERBREAD_GROUPS.values():
                for cid, info in self.catalogs.items():
                    if info["kind"] == q or KIND_LABELS.get(info["kind"]) == q:
                        matched.add(cid)
                continue
            if q in self.name_to_ids:
                matched.update(self.name_to_ids[q])
                continue
            for name, cids in self.name_to_ids.items():
                if q in name:
                    matched.update(cids)
            for fsp in known_fish:
                if q in fsp:
                    matched.add("99")
                    fish_species.add(fsp)
        return matched, fish_species


MAP_SEA_Z = {
    "miraland": -246.5,
    "serenity": -17.2,
    "danqing": -248.0,
    "wanxiang": -180.0,
    "firework": 1.5,
}


class TerrainWalkabilityGrid:
    """Hybrid 3D terrain + 2D map water/obstacle walkability and local A* pathfinder."""

    def __init__(self, root: Path, map_id: str, width: int, height: int):
        self.root = root
        self.map_id = map_id
        self.width = width
        self.height = height
        self.px_scale = 1.0
        self.z_map: np.ndarray | None = None
        self.slope_deg: np.ndarray | None = None
        self.obs_mask: np.ndarray | None = None
        self.near_obs_mask: np.ndarray | None = None
        self.wide_obs_mask: np.ndarray | None = None
        self.water_mask: np.ndarray | None = None
        self.sea_z: float = MAP_SEA_Z.get(map_id, -999.0)
        self._load_grids()

    def _load_grids(self) -> None:
        npz_path = self.root / ".local/terrain_grids" / f"{self.map_id}.npz"
        if npz_path.is_file():
            try:
                data = np.load(npz_path)
                z = data["z_map"].astype(np.float32)
                self.z_map = z
                self.slope_deg = data["slope_deg"].astype(np.float32)
                self.obs_mask = data["obs_mask"].astype(bool)
                self.px_scale = float(data.get("px_scale", 1.0))
                if self.map_id not in MAP_SEA_Z:
                    valid_z = z[~np.isnan(z)]
                    if valid_z.size > 0:
                        self.sea_z = float(np.percentile(valid_z, 3.5))
            except Exception:
                self.z_map = None

        terrain_png = self.root / "resource/navigation/terrain" / f"{self.map_id}.png"
        manifest_path = self.root / "resource/navigation/terrain/manifest.json"
        if terrain_png.is_file():
            try:
                raw = np.fromfile(str(terrain_png), dtype=np.uint8)
                bgr = cv2.imdecode(raw, cv2.IMREAD_COLOR)
                if bgr is not None:
                    b_ch = bgr[:, :, 0]
                    png_obs = ((b_ch >> 1) & 1) > 0
                    if self.z_map is None:
                        g_ch = bgr[:, :, 1].astype(np.uint16)
                        r_ch = bgr[:, :, 2].astype(np.uint16)
                        hu16 = (r_ch << 8) | g_ch
                        valid = (b_ch & 1) > 0
                        z_off, z_sc = -300.0, 64.0
                        self.px_scale = 0.25 if self.map_id == "miraland" else 0.5
                        if manifest_path.is_file():
                            m_data = json.loads(manifest_path.read_text(encoding="utf-8"))
                            if self.map_id in m_data:
                                self.px_scale = float(m_data[self.map_id].get("px_scale", self.px_scale))
                                z_off = float(m_data[self.map_id].get("z_offset", z_off))
                                z_sc = float(m_data[self.map_id].get("z_scale", z_sc))
                        self.z_map = np.where(valid & (hu16 > 0), hu16.astype(np.float32) / z_sc + z_off, np.nan)
                        self.slope_deg = ((b_ch >> 2) & 0x3F).astype(np.float32)
                        self.obs_mask = png_obs
                    elif self.obs_mask is not None:
                        if png_obs.shape == self.obs_mask.shape:
                            self.obs_mask |= png_obs
                        else:
                            up_obs = cv2.resize(
                                png_obs.astype(np.uint8),
                                (self.obs_mask.shape[1], self.obs_mask.shape[0]),
                                interpolation=cv2.INTER_NEAREST,
                            ) > 0
                            self.obs_mask |= up_obs
            except Exception:
                if self.z_map is None:
                    self.z_map = None

        # Build 2D water mask from reference map image when 3D grid is absent or map is compact
        img_path = self.root / "resource/navigation/maps" / f"{self.map_id}.png"
        if img_path.is_file() and (self.z_map is None or self.width * self.height <= 4096 * 4096):
            try:
                raw = np.fromfile(str(img_path), dtype=np.uint8)
                bgr = cv2.imdecode(raw, cv2.IMREAD_COLOR)
                if bgr is not None:
                    b, r = bgr[:, :, 0].astype(np.int16), bgr[:, :, 2].astype(np.int16)
                    self.water_mask = (b - r >= 28) & (b >= 95) & (r <= 145)
            except Exception:
                self.water_mask = None

        if self.obs_mask is not None:
            base_u8 = self.obs_mask.astype(np.uint8)
            k_near = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
            k_wide = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
            self.near_obs_mask = cv2.dilate(base_u8, k_near, iterations=1) > 0
            self.wide_obs_mask = cv2.dilate(base_u8, k_wide, iterations=1) > 0

    def elevation(self, x: float, y: float, default: float = float("nan")) -> float:
        if self.z_map is None:
            return default
        gh, gw = self.z_map.shape
        gx = int(round(x * self.px_scale))
        gy = int(round(y * self.px_scale))
        if 0 <= gx < gw and 0 <= gy < gh:
            v = float(self.z_map[gy, gx])
            if not math.isnan(v):
                return v
        return default

    def slope(self, x: float, y: float) -> float:
        if self.slope_deg is None:
            return 0.0
        gh, gw = self.slope_deg.shape
        gx = int(round(x * self.px_scale))
        gy = int(round(y * self.px_scale))
        if 0 <= gx < gw and 0 <= gy < gh:
            return float(self.slope_deg[gy, gx])
        return 0.0

    def is_water(self, x: float, y: float) -> bool:
        if self.water_mask is not None:
            wh, ww = self.water_mask.shape
            ix, iy = int(round(x)), int(round(y))
            if 0 <= ix < ww and 0 <= iy < wh and self.water_mask[iy, ix]:
                return True
        z = self.elevation(x, y)
        if not math.isnan(z) and z < self.sea_z - 0.5:
            return True
        return False

    def is_obstacle(self, x: float, y: float) -> bool:
        if self.obs_mask is None:
            return False
        gh, gw = self.obs_mask.shape
        gx = int(round(x * self.px_scale))
        gy = int(round(y * self.px_scale))
        if 0 <= gx < gw and 0 <= gy < gh:
            return bool(self.obs_mask[gy, gx])
        return False

    def has_obstacle_near(self, x: float, y: float, clearance_px: float = 2.4) -> bool:
        mask = self.wide_obs_mask if clearance_px >= 3.6 else self.near_obs_mask
        if mask is None:
            return self.is_obstacle(x, y)
        gh, gw = mask.shape
        gx = int(round(x * self.px_scale))
        gy = int(round(y * self.px_scale))
        if 0 <= gx < gw and 0 <= gy < gh:
            return bool(mask[gy, gx])
        return False

    def is_walkable(self, x: float, y: float, ref_z: float | None = None, max_slope: float = 42.0) -> bool:
        if not (4.0 <= x < self.width - 4.0 and 4.0 <= y < self.height - 4.0):
            return False
        if self.is_water(x, y) or self.is_obstacle(x, y):
            return False
        slp = self.slope(x, y)
        if slp > max_slope:
            return False
        if ref_z is not None:
            gz = self.elevation(x, y)
            if not math.isnan(gz) and abs(gz - ref_z) <= 6.0 and slp > max_slope:
                return False
        return True

    def find_walkable_anchor(
        self,
        points: list[MaterialPoint],
        spec: GingerbreadSpec,
        existing_anchors: list[tuple[float, float, float]] | None = None,
    ) -> tuple[float, float, float]:
        """Find a safe walkable ground anchor within safe_radius_m (3D) covering the cluster,
        maximizing separation from existing_anchors to prevent overlapping 15m spheres."""
        cx = sum(p.x for p in points) / len(points)
        cy = sum(p.y for p in points) / len(points)
        cz = float(np.median([p.z for p in points]))
        gz = self.elevation(cx, cy, default=cz)
        anchor_z = gz if (not math.isnan(gz) and abs(gz - cz) <= 6.5) else cz

        def count_3d_covered(tx: float, ty: float, tz: float, tol_m: float = 0.0) -> int:
            r_lim = spec.safe_radius_m + tol_m
            cnt = 0
            for p in points:
                dz = abs(p.z - tz)
                if dz > spec.max_dz_m:
                    continue
                dx_m = (p.x - tx) * M_PER_MAP_PX
                dy_m = (p.y - ty) * M_PER_MAP_PX
                if dx_m * dx_m + dy_m * dy_m + dz * dz <= r_lim * r_lim:
                    cnt += 1
            return cnt

        def min_sep_m(tx: float, ty: float, tz: float) -> float:
            if not existing_anchors:
                return 999.0
            best = 999.0
            for ax, ay, az in existing_anchors:
                if abs(tz - az) <= 10.5:
                    d_m = math.hypot(tx - ax, ty - ay) * M_PER_MAP_PX
                    if d_m < best:
                        best = d_m
            return best

        if not existing_anchors:
            if self.is_walkable(cx, cy, ref_z=anchor_z, max_slope=34.0) and not self.has_obstacle_near(cx, cy, clearance_px=3.0):
                if count_3d_covered(cx, cy, anchor_z) == len(points):
                    return round(cx, 2), round(cy, 2), round(anchor_z, 2)

        best_pt = (round(cx, 2), round(cy, 2), round(anchor_z, 2))
        best_score = -1e9
        max_r = int(math.ceil(spec.safe_radius_px))
        radii = [0] + list(range(1, max_r + 1, 2))
        for r in radii:
            steps = 1 if r == 0 else max(8, int(round(2 * math.pi * r / 2.0)))
            for s in range(steps):
                ang = 2.0 * math.pi * s / steps
                tx = cx if r == 0 else cx + r * math.cos(ang)
                ty = cy if r == 0 else cy + r * math.sin(ang)
                if not self.is_walkable(tx, ty, ref_z=anchor_z, max_slope=38.0):
                    continue
                tz = self.elevation(tx, ty, default=cz)
                if math.isnan(tz) or abs(tz - cz) > 6.5:
                    tz = cz
                covered = count_3d_covered(tx, ty, tz, tol_m=0.35)
                if covered < len(points) and len(points) > 1:
                    continue
                slp_pen = self.slope(tx, ty) * 0.08
                obs_pen = 65.0 if self.has_obstacle_near(tx, ty, clearance_px=3.0) else (20.0 if self.has_obstacle_near(tx, ty, clearance_px=5.0) else 0.0)
                sep_term = 0.0
                if existing_anchors:
                    sep_m = min_sep_m(tx, ty, tz)
                    if sep_m < 17.5:
                        sep_term = -500.0 + sep_m * 20.0
                    else:
                        sep_term = min(sep_m, 26.5) * 2.2
                sc = covered * 100.0 + sep_term - r * 0.32 - slp_pen - obs_pen
                if sc > best_score:
                    best_score = sc
                    best_pt = (round(tx, 2), round(ty, 2), round(tz, 2))
            if not existing_anchors and best_score >= len(points) * 100.0 - 15.0:
                break
        return best_pt

    def find_path_astar(
        self,
        start: tuple[float, float, float],
        goal: tuple[float, float, float],
        step_px: float = 3.0,
        relaxed: bool = False,
    ) -> list[tuple[float, float, float, bool]] | None:
        """Find a smooth walkable path from start to goal using 3D terrain A*."""
        sx, sy, sz = start
        gx, gy, gz = goal
        dist = math.hypot(gx - sx, gy - sy)
        if dist <= step_px * 1.5:
            jmp = (gz - sz) >= 0.85
            return [(round(gx, 2), round(gy, 2), round(gz, 2), jmp)]
        if dist > 315.0:
            return None
        if dist > 170.0 and step_px < 4.0:
            step_px = 4.0

        # Check if straight line is completely clear and gentle with safety clearance
        n_samples = max(2, int(math.ceil(dist / 2.0)))
        line_ok = True
        prev_z = sz
        for i in range(1, n_samples + 1):
            t = i / n_samples
            mx = sx + (gx - sx) * t
            my = sy + (gy - sy) * t
            if not self.is_walkable(mx, my, max_slope=36.0) or self.has_obstacle_near(mx, my, clearance_px=2.2):
                line_ok = False
                break
            mz = self.elevation(mx, my, default=sz + (gz - sz) * t)
            if abs(mz - prev_z) > 2.2:
                line_ok = False
                break
            prev_z = mz
        if line_ok:
            jmp = 0.55 <= (gz - sz) <= 2.5
            return [(round(gx, 2), round(gy, 2), round(gz, 2), jmp)]

        # Run grid A* in local bounding box with margin
        margin = 55.0
        min_x = max(4.0, min(sx, gx) - margin)
        max_x = min(self.width - 5.0, max(sx, gx) + margin)
        min_y = max(4.0, min(sy, gy) - margin)
        max_y = min(self.height - 5.0, max(sy, gy) + margin)
        cols = max(2, int(round((max_x - min_x) / step_px)) + 1)
        rows = max(2, int(round((max_y - min_y) / step_px)) + 1)

        def to_cell(x: float, y: float) -> tuple[int, int]:
            c = max(0, min(cols - 1, int(round((x - min_x) / step_px))))
            r = max(0, min(rows - 1, int(round((y - min_y) / step_px))))
            return c, r

        def to_xy(c: int, r: int) -> tuple[float, float]:
            return min_x + c * step_px, min_y + r * step_px

        sc, sr = to_cell(sx, sy)
        gc, gr = to_cell(gx, gy)
        open_heap: list[tuple[float, float, int, int]] = [(0.0, 0.0, sc, sr)]
        g_score: dict[tuple[int, int], float] = {(sc, sr): 0.0}
        came_from: dict[tuple[int, int], tuple[int, int]] = {}
        neighbors = [
            (-1, 0, 1.0), (1, 0, 1.0), (0, -1, 1.0), (0, 1, 1.0),
            (-1, -1, 1.414), (1, -1, 1.414), (-1, 1, 1.414), (1, 1, 1.414),
        ]

        max_slp = 56.0 if relaxed else 45.0
        max_step_dz = 5.0 if relaxed else 3.2
        visited = 0
        reached = False
        while open_heap and visited < 6500:
            _, cost, c, r = heapq.heappop(open_heap)
            if (c, r) == (gc, gr):
                reached = True
                break
            if cost > g_score.get((c, r), 1e18):
                continue
            visited += 1
            cx, cy = to_xy(c, r)
            cz = self.elevation(cx, cy, default=sz)
            for dc, dr, d_mult in neighbors:
                nc, nr = c + dc, r + dr
                if not (0 <= nc < cols and 0 <= nr < rows):
                    continue
                nx, ny = to_xy(nc, nr)
                if (nc, nr) != (gc, gr) and not self.is_walkable(nx, ny, max_slope=max_slp):
                    continue
                nz = self.elevation(nx, ny, default=cz)
                dz = nz - cz if not (math.isnan(nz) or math.isnan(cz)) else 0.0
                if abs(dz) > max_step_dz:
                    continue
                slp = self.slope(nx, ny)
                obs_pen = 0.0
                if (nc, nr) != (gc, gr):
                    if self.has_obstacle_near(nx, ny, clearance_px=2.4):
                        obs_pen = 2.4
                    elif self.has_obstacle_near(nx, ny, clearance_px=4.2):
                        obs_pen = 0.75
                step_cost = d_mult * step_px * (1.0 + obs_pen + max(0.0, slp - 15.0) * 0.04 + max(0.0, dz) * 0.35)
                ng = cost + step_cost
                if ng < g_score.get((nc, nr), 1e18):
                    g_score[(nc, nr)] = ng
                    came_from[(nc, nr)] = (c, r)
                    h = math.hypot(gc - nc, gr - nr) * step_px
                    heapq.heappush(open_heap, (ng + h, ng, nc, nr))

        if not reached:
            return None

        cells = [(gc, gr)]
        cur = (gc, gr)
        while cur in came_from:
            cur = came_from[cur]
            cells.append(cur)
        cells.reverse()

        raw_pts = []
        for idx, (c, r) in enumerate(cells):
            if idx == 0:
                px, py, pz = sx, sy, sz
            elif idx == len(cells) - 1:
                px, py, pz = gx, gy, gz
            else:
                px, py = to_xy(c, r)
                pz = self.elevation(px, py, default=sz)
            raw_pts.append((px, py, pz))

        return self._simplify_path(raw_pts)

    def _simplify_path(
        self, raw_pts: list[tuple[float, float, float]]
    ) -> list[tuple[float, float, float, bool]]:
        if len(raw_pts) <= 2:
            gx, gy, gz = raw_pts[-1]
            sz = raw_pts[0][2]
            jmp = 0.55 <= (gz - sz) <= 2.5
            return [(round(gx, 2), round(gy, 2), round(gz, 2), jmp)]

        # Line-of-sight greedy string pulling + max segment length ~24 px (~21.6 m)
        simplified = [raw_pts[0]]
        i = 0
        n = len(raw_pts)
        while i < n - 1:
            best_j = i + 1
            for j in range(min(n - 1, i + 10), i + 1, -1):
                p0, p1 = raw_pts[i], raw_pts[j]
                seg_d = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
                if seg_d > 24.0:
                    continue
                # Check if intermediate A* grid cells are collinear (e.g. straight walk along a path)
                max_dev = 0.0
                if seg_d > 1e-3:
                    ux = (p1[0] - p0[0]) / seg_d
                    uy = (p1[1] - p0[1]) / seg_d
                    for k in range(i + 1, j):
                        pk = raw_pts[k]
                        dev = abs((pk[0] - p0[0]) * (-uy) + (pk[1] - p0[1]) * ux)
                        if dev > max_dev:
                            max_dev = dev
                is_collinear = max_dev <= 0.65
                orig_near_obs = any(
                    self.has_obstacle_near(raw_pts[k][0], raw_pts[k][1], clearance_px=2.0)
                    for k in range(i, j + 1)
                )
                clear = True
                steps = max(2, int(math.ceil(seg_d / 1.8)))
                for s in range(1, steps):
                    t = s / steps
                    mx = p0[0] + (p1[0] - p0[0]) * t
                    my = p0[1] + (p1[1] - p0[1]) * t
                    if not self.is_walkable(mx, my, max_slope=44.0):
                        clear = False
                        break
                    if not is_collinear and not orig_near_obs and self.has_obstacle_near(mx, my, clearance_px=2.0):
                        clear = False
                        break
                if clear:
                    best_j = j
                    break
            simplified.append(raw_pts[best_j])
            i = best_j

        out = []
        for k in range(1, len(simplified)):
            prev_z = simplified[k - 1][2]
            x, y, z = simplified[k]
            dz = (z - prev_z) if not (math.isnan(z) or math.isnan(prev_z)) else 0.0
            jmp = 0.55 <= dz <= 2.5
            out.append((round(x, 2), round(y, 2), round(z, 2), jmp))
        return out


class MaterialRouteGenerator:
    """Synthesizes Gingerbread-Man material routes and route collections."""

    def __init__(self, root: Path, spec: GingerbreadSpec | None = None):
        self.root = root
        self.spec = spec or GingerbreadSpec()
        self.catalog = PearPalMaterialCatalog(root)
        self.catalog.load()
        from navigation.models import maps
        self.map_specs = maps(root / "resource")
        self._grids: dict[str, TerrainWalkabilityGrid] = {}

    def grid(self, map_id: str) -> TerrainWalkabilityGrid:
        if map_id not in self._grids:
            ms = self.map_specs[map_id]
            self._grids[map_id] = TerrainWalkabilityGrid(self.root, map_id, ms.width, ms.height)
        return self._grids[map_id]

    def _build_anchors_around_spire(
        self,
        spire: SpireCheckpoint,
        map_points: list[MaterialPoint],
        target_cids: set[str] | None,
        kind_weights: dict[str, float],
        max_spire_dist_px: float = 210.0,
    ) -> tuple[list[AnchorCluster], list[MaterialPoint]]:
        tgrid = self.grid(spire.map_id)
        spire_gz = tgrid.elevation(spire.x, spire.y, default=spire.z)
        spire_on_ground = math.isnan(spire_gz) or abs(spire.z - spire_gz) <= 8.5

        local_points: list[MaterialPoint] = []
        for p in map_points:
            d = math.hypot(p.x - spire.x, p.y - spire.y)
            if d > max_spire_dist_px:
                continue
            pgz = tgrid.elevation(p.x, p.y, default=p.z)
            if spire_on_ground:
                if not math.isnan(pgz) and abs(p.z - pgz) > 8.5:
                    continue
                if abs(p.z - spire.z) > 45.0:
                    continue
            else:
                if abs(p.z - spire.z) > 22.0:
                    continue
            local_points.append(p)

        if not local_points:
            return [], []

        def point_weight(pt: MaterialPoint) -> float:
            if target_cids is not None:
                return 10.0 if pt.catalog_id in target_cids else 0.15
            return kind_weights.get(pt.kind, 1.0)

        n_pts = len(local_points)
        px_arr = np.array([p.x for p in local_points], dtype=np.float32)
        py_arr = np.array([p.y for p in local_points], dtype=np.float32)
        pz_arr = np.array([p.z for p in local_points], dtype=np.float32)
        w_arr = np.array([point_weight(p) for p in local_points], dtype=np.float32)
        srv_arr = np.array(
            [
                1.0 if p.kind == "plant" else (
                    self.spec.insect_s if p.kind == "insect" else (
                        self.spec.animal_s if p.kind == "animal" else self.spec.fish_s
                    )
                )
                for p in local_points
            ],
            dtype=np.float32,
        )
        is_tgt_arr = np.array(
            [True if target_cids is None else (p.catalog_id in target_cids) for p in local_points],
            dtype=bool,
        )

        # Build candidate sphere centers: target points + close pair midpoints + local centroids
        cand_idx = np.where(is_tgt_arr)[0]
        if len(cand_idx) == 0:
            return [], []
        tx = px_arr[cand_idx]
        ty = py_arr[cand_idx]
        tz = pz_arr[cand_idx]
        cx_list = list(tx)
        cy_list = list(ty)
        cz_list = list(tz)
        r_m = self.spec.safe_radius_m
        max_dz = self.spec.max_dz_m

        if len(cand_idx) <= 320:
            tdx_m = (tx[:, None] - tx[None, :]) * M_PER_MAP_PX
            tdy_m = (ty[:, None] - ty[None, :]) * M_PER_MAP_PX
            tdz_m = np.abs(tz[:, None] - tz[None, :])
            td3d_m = np.sqrt(tdx_m * tdx_m + tdy_m * tdy_m + tdz_m * tdz_m)
            # Pairwise midpoints for all pairs within 2 * safe_radius_m
            iu, ju = np.triu_indices(len(cand_idx), k=1)
            pair_mask = (td3d_m[iu, ju] <= r_m * 2.0) & (tdz_m[iu, ju] <= max_dz * 1.4)
            for idx_p in np.where(pair_mask)[0][:750]:
                ii, jj = int(iu[idx_p]), int(ju[idx_p])
                cx_list.append((tx[ii] + tx[jj]) * 0.5)
                cy_list.append((ty[ii] + ty[jj]) * 0.5)
                cz_list.append((tz[ii] + tz[jj]) * 0.5)
            # Local neighborhood centroids
            for rad_c in (r_m, r_m * 1.38):
                nb_mat = (td3d_m <= rad_c) & (tdz_m <= max_dz)
                for ii in range(len(cand_idx)):
                    nbs = np.where(nb_mat[ii])[0]
                    if len(nbs) >= 3:
                        cx_list.append(float(np.mean(tx[nbs])))
                        cy_list.append(float(np.mean(ty[nbs])))
                        cz_list.append(float(np.mean(tz[nbs])))

        cx_arr = np.array(cx_list, dtype=np.float32)
        cy_arr = np.array(cy_list, dtype=np.float32)
        cz_arr = np.array(cz_list, dtype=np.float32)

        dx_m = (px_arr[:, None] - cx_arr[None, :]) * M_PER_MAP_PX
        dy_m = (py_arr[:, None] - cy_arr[None, :]) * M_PER_MAP_PX
        dz_m = np.abs(pz_arr[:, None] - cz_arr[None, :])
        d3d_sq = dx_m * dx_m + dy_m * dy_m + dz_m * dz_m
        cover_mat = (d3d_sq <= (r_m * r_m)) & (dz_m <= max_dz)

        active = np.ones(n_pts, dtype=bool)
        anchors: list[AnchorCluster] = []
        existing_xyz: list[tuple[float, float, float]] = []
        min_sep_m = self.spec.min_anchor_sep_m
        claim_r_m = min(self.spec.aura_radius_m - 0.4, r_m + 0.5)

        while np.any(active & is_tgt_arr) and len(anchors) < 24:
            cur_w = np.where(active, w_arr, 0.0)
            raw_scores = cover_mat.T @ cur_w
            new_cnts = cover_mat.T @ active.astype(np.int32)
            tgt_counts = cover_mat.T @ (active & is_tgt_arr).astype(np.int32)
            if target_cids is None:
                cur_srv = np.where(active, srv_arr, 0.0)
                est_srv = cover_mat.T @ cur_srv
                cand_scores = raw_scores / (3.4 + 0.30 * est_srv)
            else:
                cand_scores = raw_scores.copy()
            cand_scores = np.where(tgt_counts > 0, cand_scores, -1.0)

            if existing_xyz:
                ex_arr = np.array(existing_xyz, dtype=np.float32)
                adx_m = (cx_arr[:, None] - ex_arr[None, :, 0]) * M_PER_MAP_PX
                ady_m = (cy_arr[:, None] - ex_arr[None, :, 1]) * M_PER_MAP_PX
                adz_m = np.abs(cz_arr[:, None] - ex_arr[None, :, 2])
                min_d_anc = np.min(np.sqrt(adx_m * adx_m + ady_m * ady_m + adz_m * adz_m), axis=1)
                overlap_pen = 1.0 - 0.40 * np.clip((28.0 - min_d_anc) / 10.0, 0.0, 0.85)
                cand_scores = cand_scores * overlap_pen + 1e-3 * np.minimum(min_d_anc, 30.0)
                if target_cids is None:
                    cand_scores = np.where(min_d_anc >= min_sep_m, cand_scores, -1.0)
                    cand_scores = np.where((new_cnts >= 3) | (min_d_anc >= 24.0), cand_scores, -1.0)
                else:
                    pref_scores = np.where(min_d_anc >= min_sep_m, cand_scores, -1.0)
                    if np.max(pref_scores) > 0.0:
                        cand_scores = pref_scores
                    else:
                        cand_scores = np.where(min_d_anc >= 16.5, cand_scores, -1.0)

            best_c = int(np.argmax(cand_scores))
            if cand_scores[best_c] <= 0.0:
                break

            grp_idx = np.where(active & cover_mat[:, best_c])[0]
            best_group = [local_points[i] for i in grp_idx]
            ax, ay, az = tgrid.find_walkable_anchor(
                best_group, self.spec, existing_anchors=existing_xyz
            )
            if existing_xyz:
                min_after_m = min(
                    math.sqrt(((ax - ex) * M_PER_MAP_PX) ** 2 + ((ay - ey) * M_PER_MAP_PX) ** 2 + (az - ez) ** 2)
                    for ex, ey, ez in existing_xyz
                )
                if min_after_m < (min_sep_m - 1.5 if target_cids is None else 15.0):
                    active[grp_idx] = False
                    continue

            dist_3d_m = np.sqrt(
                ((px_arr - ax) * M_PER_MAP_PX) ** 2
                + ((py_arr - ay) * M_PER_MAP_PX) ** 2
                + (pz_arr - az) ** 2
            )
            dz_a = np.abs(pz_arr - az)
            cov_mask = active & (dist_3d_m <= claim_r_m) & (dz_a <= max_dz)
            if not np.any(cov_mask & is_tgt_arr):
                cov_mask = np.zeros(n_pts, dtype=bool)
                cov_mask[grp_idx] = True

            cov_indices = np.where(cov_mask)[0]
            covered_now = [local_points[i] for i in cov_indices]
            tgt_pts = [local_points[i] for i in cov_indices if is_tgt_arr[i]]
            wait_pts = covered_now if target_cids is None else tgt_pts
            anchors.append(
                AnchorCluster(
                    x=ax,
                    y=ay,
                    z=az,
                    points=covered_now,
                    score=float(np.sum(w_arr[cov_indices])),
                    target_count=len(tgt_pts),
                    wait_seconds=self.spec.net_wait_seconds(wait_pts, anchor_xyz=(ax, ay, az)),
                )
            )
            existing_xyz.append((ax, ay, az))
            active[cov_indices] = False

        return anchors, local_points

    def _plan_spire_route(
        self,
        spire: SpireCheckpoint,
        map_points: list[MaterialPoint],
        target_cids: set[str] | None,
        kind_weights: dict[str, float],
        max_anchors: int = 8,
        max_route_seconds: float = 135.0,
        route_id_prefix: str = "gingerbread",
        route_index: int = 1,
        primary_material_name: str = "",
        max_spire_dist_px: float = 210.0,
        relaxed_astar: bool = False,
    ) -> GeneratedRoutePlan | None:
        anchors, local_points = self._build_anchors_around_spire(
            spire, map_points, target_cids, kind_weights, max_spire_dist_px=max_spire_dist_px
        )
        if not anchors:
            return None

        tgrid = self.grid(spire.map_id)
        cur_pos = (spire.x, spire.y, spire.z)
        remaining = list(anchors)
        selected_anchors: list[AnchorCluster] = []
        path_segments: list[list[tuple[float, float, float, bool]]] = []
        total_walk_m = 0.0
        total_time_s = self.spec.teleport_overhead_s
        max_step_px = max(145.0, max_spire_dist_px + 10.0)
        max_step_dz = 48.0 if relaxed_astar else 25.0

        while remaining and len(selected_anchors) < max_anchors:
            best_idx = -1
            best_ratio = -1.0
            best_seg: list[tuple[float, float, float, bool]] | None = None
            best_seg_m = 0.0
            best_dt = 0.0

            for idx, anc in enumerate(remaining):
                dist_px = math.hypot(anc.x - cur_pos[0], anc.y - cur_pos[1])
                if dist_px > max_step_px or abs(anc.z - cur_pos[2]) > max_step_dz:
                    continue
                est_walk_m = dist_px * M_PER_MAP_PX
                est_dt = est_walk_m / self.spec.run_speed_mps + anc.wait_seconds + 0.5
                if total_time_s + est_dt > max_route_seconds and selected_anchors:
                    continue
                ratio = anc.score / max(2.5, est_dt)
                if ratio <= best_ratio:
                    continue
                seg = tgrid.find_path_astar(cur_pos, (anc.x, anc.y, anc.z), relaxed=relaxed_astar)
                if not seg and not selected_anchors and target_cids is not None:
                    seg = tgrid.find_path_astar(cur_pos, (anc.x, anc.y, anc.z), relaxed=True)
                if not seg:
                    continue
                # Compute true segment walk length
                pts_chain = [cur_pos[:2]] + [(s[0], s[1]) for s in seg]
                seg_px = sum(
                    math.hypot(pts_chain[k + 1][0] - pts_chain[k][0], pts_chain[k + 1][1] - pts_chain[k][1])
                    for k in range(len(pts_chain) - 1)
                )
                seg_m = seg_px * M_PER_MAP_PX
                dt = seg_m / self.spec.run_speed_mps + anc.wait_seconds
                if total_time_s + dt > max_route_seconds and selected_anchors:
                    continue
                true_ratio = anc.score / max(2.5, dt)
                if true_ratio > best_ratio:
                    best_ratio = true_ratio
                    best_idx = idx
                    best_seg = seg
                    best_seg_m = seg_m
                    best_dt = dt

            if best_idx < 0 or best_seg is None:
                break
            # Stop adding distant low-yield anchors once we already have a strong core route
            if selected_anchors and best_ratio < 0.28:
                break

            chosen = remaining.pop(best_idx)
            selected_anchors.append(chosen)
            path_segments.append(best_seg)
            total_walk_m += best_seg_m
            total_time_s += best_dt
            cur_pos = (chosen.x, chosen.y, chosen.z)

        if not selected_anchors:
            return None
        return self._assemble_route_plan(
            spire=spire,
            selected_anchors=selected_anchors,
            path_segments=path_segments,
            local_points=local_points,
            target_cids=target_cids,
            walk_m=total_walk_m,
            route_id_prefix=route_id_prefix,
            route_index=route_index,
            primary_material_name=primary_material_name,
        )

    def _assemble_route_plan(
        self,
        spire: SpireCheckpoint,
        selected_anchors: list[AnchorCluster],
        path_segments: list[list[tuple[float, float, float, bool]]],
        local_points: list[MaterialPoint],
        target_cids: set[str] | None,
        walk_m: float,
        route_id_prefix: str,
        route_index: int,
        primary_material_name: str,
    ) -> GeneratedRoutePlan:
        # Build waypoint list: #00 is the Warp Spire spawn point
        waypoints: list[dict] = [
            {
                "x": round(spire.x, 2),
                "y": round(spire.y, 2),
                "radius": 2.5,
                "jump": False,
                "action": "",
                "params": {},
            }
        ]
        wp_z: list[float] = [spire.z]
        anchor_wp_indices: list[int] = []

        for anc_i, (anc, seg) in enumerate(zip(selected_anchors, path_segments)):
            if (
                anc_i == 0
                and len(seg) == 1
                and math.hypot(seg[0][0] - spire.x, seg[0][1] - spire.y) * M_PER_MAP_PX <= 3.0
            ):
                waypoints[0]["x"] = round(seg[0][0], 2)
                waypoints[0]["y"] = round(seg[0][1], 2)
                waypoints[0]["radius"] = 2.2
                wp_z[0] = seg[0][2]
                anchor_wp_indices.append(0)
                continue
            for idx, (px, py, pz, jmp) in enumerate(seg):
                is_anchor = idx == len(seg) - 1
                waypoints.append(
                    {
                        "x": round(px, 2),
                        "y": round(py, 2),
                        "radius": 2.2 if is_anchor else 2.5,
                        "jump": bool(jmp),
                        "action": "",
                        "params": {},
                    }
                )
                wp_z.append(pz)
                if is_anchor:
                    anchor_wp_indices.append(len(waypoints) - 1)

        wp_items: dict[int, list[MaterialPoint]] = {i: [] for i in range(len(waypoints))}
        covered_ids: set[str] = set()
        for anc, wp_i in zip(selected_anchors, anchor_wp_indices):
            for p in anc.points:
                if p.id not in covered_ids:
                    covered_ids.add(p.id)
                    wp_items[wp_i].append(p)

        claim_r_m = min(self.spec.aura_radius_m - 0.4, self.spec.safe_radius_m + 0.5)
        pass_r_m = self.spec.pass_radius_m
        max_dz = self.spec.max_dz_m

        # 1. Stationary anchors claim all uncovered points within their 3D sphere first
        for p in local_points:
            if p.id in covered_ids:
                continue
            if target_cids is not None and p.catalog_id not in target_cids and p.kind != "plant":
                continue
            best_wi = -1
            best_d_m = claim_r_m
            for wi in anchor_wp_indices:
                dz = abs(p.z - wp_z[wi])
                if dz > max_dz:
                    continue
                d_2d_m = math.hypot(p.x - waypoints[wi]["x"], p.y - waypoints[wi]["y"]) * M_PER_MAP_PX
                d_3d_m = math.hypot(d_2d_m, dz)
                if d_3d_m <= best_d_m:
                    best_d_m = d_3d_m
                    best_wi = wi
            if best_wi >= 0:
                covered_ids.add(p.id)
                wp_items[best_wi].append(p)

        # 2. Promote a non-anchor waypoint (e.g. spire #00) to a stationary stop ONLY if >= min_anchor_sep_m
        #    from all existing stops AND it covers a dense cluster (>=3 plants or >=2 target items)
        stop_wis: list[int] = list(dict.fromkeys(anchor_wp_indices))
        for wi, wp in enumerate(waypoints):
            if wi in stop_wis:
                continue
            if any(
                math.sqrt(
                    ((wp["x"] - waypoints[sw]["x"]) * M_PER_MAP_PX) ** 2
                    + ((wp["y"] - waypoints[sw]["y"]) * M_PER_MAP_PX) ** 2
                    + (wp_z[wi] - wp_z[sw]) ** 2
                )
                < self.spec.min_anchor_sep_m
                for sw in stop_wis
            ):
                continue
            cand_pts = [
                p
                for p in local_points
                if p.id not in covered_ids
                and abs(p.z - wp_z[wi]) <= max_dz
                and math.hypot(
                    math.hypot(p.x - wp["x"], p.y - wp["y"]) * M_PER_MAP_PX,
                    abs(p.z - wp_z[wi]),
                )
                <= claim_r_m
                and (target_cids is None or p.catalog_id in target_cids or p.kind == "plant")
            ]
            tgt_here = [p for p in cand_pts if target_cids is not None and p.catalog_id in target_cids]
            slow_here = [p for p in cand_pts if p.kind != "plant" and (target_cids is None or p.catalog_id in target_cids)]
            if len(cand_pts) >= 3 or len(tgt_here) >= 2 or len(slow_here) >= 1:
                stop_wis.append(wi)
                for p in cand_pts:
                    covered_ids.add(p.id)
                    wp_items[wi].append(p)

        # 3. Remaining single/pair roadside plants within pass_radius_m of walking segments are picked up in-stride
        in_stride_items: list[MaterialPoint] = []
        for p in local_points:
            if p.id in covered_ids or p.kind != "plant":
                continue
            for k in range(len(waypoints) - 1):
                x0, y0, z0 = waypoints[k]["x"], waypoints[k]["y"], wp_z[k]
                x1, y1, z1 = waypoints[k + 1]["x"], waypoints[k + 1]["y"], wp_z[k + 1]
                vx, vy, vz = x1 - x0, y1 - y0, z1 - z0
                seg_len_sq = vx * vx + vy * vy
                t = max(0.0, min(1.0, ((p.x - x0) * vx + (p.y - y0) * vy) / seg_len_sq)) if seg_len_sq > 1e-6 else 0.0
                cx, cy, cz = x0 + t * vx, y0 + t * vy, z0 + t * vz
                dz = abs(p.z - cz)
                if dz <= max_dz and math.hypot(math.hypot(p.x - cx, p.y - cy) * M_PER_MAP_PX, dz) <= pass_r_m:
                    covered_ids.add(p.id)
                    in_stride_items.append(p)
                    break

        # Set wait action ONLY on stationary stop waypoints
        stop_wi_set = set(stop_wis)
        total_wait_s = 0.0
        all_covered: list[MaterialPoint] = list(in_stride_items)
        final_waypoints: list[dict] = []
        for wi, wp in enumerate(waypoints):
            items = wp_items.get(wi, [])
            if wi not in stop_wi_set or not items:
                final_waypoints.append(wp)
                continue
            all_covered.extend(items)
            dwell_items = (
                items
                if target_cids is None
                else [p for p in items if p.catalog_id in target_cids or p.kind == "plant"]
            )
            wait_s = (
                self.spec.net_wait_seconds(dwell_items, anchor_xyz=(wp["x"], wp["y"], wp_z[wi]))
                if dwell_items
                else 0.0
            )
            if wait_s <= 0.0:
                final_waypoints.append(wp)
                continue
            total_wait_s += wait_s
            wp_copy = dict(wp)
            wp_copy["action"] = "wait"
            wp_copy["params"] = {"seconds": round(min(150.0, wait_s), 1)}
            wp_copy["radius"] = 2.0
            final_waypoints.append(wp_copy)
        waypoints = final_waypoints

        # Ensure final waypoint has a conservative wait if it didn't already so trailing Gingerbread catches finish
        if waypoints[-1]["action"] == "" and wp_items.get(len(waypoints) - 1):
            waypoints[-1]["action"] = "wait"
            waypoints[-1]["params"] = {"seconds": 5.0}
            total_wait_s += 5.0

        counts_by_name: dict[str, int] = {}
        counts_by_kind: dict[str, int] = {}
        target_count = 0
        for p in all_covered:
            counts_by_name[p.name] = counts_by_name.get(p.name, 0) + 1
            counts_by_kind[p.kind] = counts_by_kind.get(p.kind, 0) + 1
            if target_cids is None or p.catalog_id in target_cids:
                target_count += 1

        est_seconds = round(
            self.spec.teleport_overhead_s + walk_m / self.spec.run_speed_mps + total_wait_s, 1
        )
        eff_per_min = round(target_count * 60.0 / max(10.0, est_seconds), 2)
        timeout_s = int(min(900, max(120, round(est_seconds * 2.2 + 45))))

        sorted_names = sorted(counts_by_name.keys(), key=lambda k: -counts_by_name[k])
        main_mat = primary_material_name or (sorted_names[0] if sorted_names else "")
        route_id = f"{route_id_prefix}_{spire.map_id}_{route_index:02d}"
        title_prefix = main_mat if primary_material_name else "姜饼人全收集"
        title = f"【{title_prefix}】{spire.region}·{spire.name}（{target_count}个/{int(round(est_seconds))}秒）"

        route_doc = {
            "schema_version": 3,
            "id": route_id,
            "title": title,
            "map": spire.map_id,
            "start_radius": 6,
            "timeout": timeout_s,
            "teleport": {
                "name": spire.name,
                "province": spire.province,
                "region": spire.region,
                "x": round(spire.x, 3),
                "y": round(spire.y, 3),
            },
            "material": main_mat,
            "count_materials": sorted_names[:16],
            "capacity": min(500, max(1, len(all_covered))),
            "points": waypoints,
        }
        return GeneratedRoutePlan(
            route_doc=route_doc,
            checkpoint=spire,
            covered_points=all_covered,
            target_count=target_count,
            total_count=len(all_covered),
            counts_by_name=counts_by_name,
            counts_by_kind=counts_by_kind,
            walk_distance_m=round(walk_m, 1),
            estimated_seconds=est_seconds,
            efficiency_per_min=eff_per_min,
        )

    def generate_for_materials(
        self,
        materials: Iterable[str],
        *,
        map_ids: Iterable[str] | None = None,
        regions: Iterable[str] | None = None,
        top_k: int = 5,
        quota: int | None = None,
        max_anchors: int = 8,
        max_route_seconds: float = 220.0,
        route_id_prefix: str = "gb_target",
    ) -> list[GeneratedRoutePlan]:
        queries = [m.strip() for m in materials if m.strip()]
        target_cids, fish_species = self.catalog.resolve_target_filter(queries)
        if not target_cids:
            raise ValueError(f"未在美鸭梨目录中找到匹配的材料：{', '.join(queries)}")

        primary_name = queries[0] if len(queries) == 1 else "、".join(queries[:2])
        active_maps = list(map_ids) if map_ids else list(WORLDS.values())
        region_set = {r.strip() for r in regions if r.strip()} if regions else None
        kind_weights = {"plant": 1.0, "insect": 1.5, "animal": 1.8, "fish": 2.0}

        def prepare_map_points(raw_pts: list[MaterialPoint]) -> list[MaterialPoint]:
            if not fish_species:
                return raw_pts
            out = []
            for p in raw_pts:
                if p.catalog_id == "99":
                    sp_tuple = self.catalog.parse_fish_species(p.condition)
                    if any(sp in sp_tuple for sp in fish_species):
                        out.append(
                            MaterialPoint(
                                id=p.id,
                                catalog_id="99",
                                name=primary_name,
                                kind=p.kind,
                                map_id=p.map_id,
                                x=p.x,
                                y=p.y,
                                z=p.z,
                                tag=p.tag,
                                condition=p.condition,
                            )
                        )
                    else:
                        out.append(
                            MaterialPoint(
                                id=p.id,
                                catalog_id="99_other",
                                name=p.name,
                                kind=p.kind,
                                map_id=p.map_id,
                                x=p.x,
                                y=p.y,
                                z=p.z,
                                tag=p.tag,
                                condition=p.condition,
                            )
                        )
                else:
                    out.append(p)
            return out

        candidates: list[GeneratedRoutePlan] = []
        for search_r, relax in ((210.0, False), (295.0, True)):
            for map_id in active_maps:
                pts = prepare_map_points(self.catalog.points_by_map.get(map_id, []))
                if not any(p.catalog_id in target_cids for p in pts):
                    continue
                scored_spires = []
                for spire in self.catalog.spires_by_map.get(map_id, []):
                    if region_set and spire.region not in region_set and spire.province not in region_set:
                        continue
                    near_target = sum(
                        1 for p in pts
                        if p.catalog_id in target_cids and abs(p.x - spire.x) <= search_r and abs(p.y - spire.y) <= search_r and math.hypot(p.x - spire.x, p.y - spire.y) <= search_r
                    )
                    if near_target > 0:
                        scored_spires.append((near_target, spire))
                scored_spires.sort(key=lambda x: x[0], reverse=True)
                for _, spire in scored_spires[: max(top_k * 3, 10)]:
                    plan = self._plan_spire_route(
                        spire=spire,
                        map_points=pts,
                        target_cids=target_cids,
                        kind_weights=kind_weights,
                        max_anchors=max_anchors,
                        max_route_seconds=max_route_seconds,
                        route_id_prefix=route_id_prefix,
                        route_index=1,
                        primary_material_name=primary_name,
                        max_spire_dist_px=search_r,
                        relaxed_astar=relax,
                    )
                    if plan and plan.target_count > 0:
                        candidates.append(plan)
            if candidates:
                break

        # Deduplicate overlapping routes (avoid two adjacent spires farming the exact same points)
        candidates.sort(key=lambda r: (r.efficiency_per_min, r.target_count, r.total_count), reverse=True)
        selected: list[GeneratedRoutePlan] = []
        claimed_point_ids: set[str] = set()
        accumulated_target = 0

        for cand in candidates:
            new_pts = [p for p in cand.covered_points if p.id not in claimed_point_ids]
            new_tgt = sum(1 for p in new_pts if p.catalog_id in target_cids)
            if new_tgt < max(1, int(cand.target_count * 0.6)):
                continue
            idx = len(selected) + 1
            cand.route_doc["id"] = f"{route_id_prefix}_{cand.checkpoint.map_id}_{idx:02d}"
            selected.append(cand)
            claimed_point_ids.update(p.id for p in cand.covered_points)
            accumulated_target += new_tgt
            if len(selected) >= top_k or (quota is not None and accumulated_target >= quota):
                break

        return selected

    def generate_max_yield(
        self,
        *,
        map_ids: Iterable[str] | None = None,
        regions: Iterable[str] | None = None,
        top_k: int = 5,
        kind_weights: dict[str, float] | None = None,
        max_anchors: int = 10,
        max_route_seconds: float = 260.0,
        route_id_prefix: str = "gb_sweep",
        title_label: str = "",
    ) -> list[GeneratedRoutePlan]:
        weights = kind_weights or {"plant": 1.0, "insect": 1.8, "animal": 2.2, "fish": 2.4}
        active_maps = list(map_ids) if map_ids else list(WORLDS.values())
        region_set = {r.strip() for r in regions if r.strip()} if regions else None
        candidates: list[GeneratedRoutePlan] = []

        for map_id in active_maps:
            pts = [p for p in self.catalog.points_by_map.get(map_id, []) if weights.get(p.kind, 0.0) > 0]
            if not pts:
                continue
            spires = self.catalog.spires_by_map.get(map_id, [])
            if region_set:
                spires = [s for s in spires if s.region in region_set or s.province in region_set]
            if not spires:
                continue
            # Rank spires by local weighted density within 160 px first to only run A* on top hotspots
            scored_spires = []
            for sp in spires:
                local_w = sum(
                    weights.get(p.kind, 1.0)
                    for p in pts
                    if abs(p.x - sp.x) <= 165.0 and abs(p.y - sp.y) <= 165.0 and abs(p.z - sp.z) <= 35.0
                )
                if local_w >= 4.0:
                    scored_spires.append((local_w, sp))
            scored_spires.sort(key=lambda x: x[0], reverse=True)

            for _, spire in scored_spires[: max(top_k * 3, 18)]:
                plan = self._plan_spire_route(
                    spire=spire,
                    map_points=pts,
                    target_cids=None,
                    kind_weights=weights,
                    max_anchors=max_anchors,
                    max_route_seconds=max_route_seconds,
                    route_id_prefix=route_id_prefix,
                    route_index=1,
                    primary_material_name=title_label,
                )
                if plan and plan.total_count >= 4:
                    candidates.append(plan)

        candidates.sort(key=lambda r: (r.efficiency_per_min, r.total_count), reverse=True)
        selected: list[GeneratedRoutePlan] = []
        claimed_ids: set[str] = set()
        for cand in candidates:
            new_cnt = sum(1 for p in cand.covered_points if p.id not in claimed_ids)
            if new_cnt < max(4, int(cand.total_count * 0.65)):
                continue
            idx = len(selected) + 1
            cand.route_doc["id"] = f"{route_id_prefix}_{cand.checkpoint.map_id}_{idx:02d}"
            selected.append(cand)
            claimed_ids.update(p.id for p in cand.covered_points)
            if len(selected) >= top_k:
                break
        return selected

    @staticmethod
    def build_route_collection(
        plans: list[GeneratedRoutePlan],
        collection_id: str,
        title: str,
    ) -> dict:
        return {
            "schema_version": 1,
            "kind": "route_collection",
            "id": collection_id,
            "title": title,
            "continue_on_failure": True,
            "routes": [
                {
                    "route": p.route_doc["id"],
                    "title": p.route_doc["title"],
                }
                for p in plans
            ],
        }


def generate_from_request_dict(root: Path, req: dict) -> dict:
    """Entry point for UI on-demand route generation."""
    mode = str(req.get("mode") or "target")
    radius = float(req.get("radius") or 14.0)
    top_k = max(1, min(15, int(req.get("top_k") or 3)))
    quota = int(req["quota"]) if req.get("quota") else None
    prefix = str(req.get("prefix") or "gb_custom")
    map_ids = [m for m in (req.get("maps") or []) if m] or None
    regions = [r for r in (req.get("regions") or []) if r] or None
    materials = [m for m in (req.get("materials") or []) if m]

    spec = GingerbreadSpec(
        safe_radius_m=radius,
        pass_radius_m=min(14.5, max(12.0, radius - 0.5)),
    )
    gen = MaterialRouteGenerator(root, spec=spec)
    if mode == "target" and materials:
        plans = gen.generate_for_materials(
            materials,
            map_ids=map_ids,
            regions=regions,
            top_k=top_k,
            quota=quota,
            route_id_prefix=prefix,
        )
        col_title = f"【按需生成】{'/'.join(materials[:2])}（{len(plans)}条）"
    else:
        category = str(req.get("category") or "all")
        weights_map = {
            "plant": {"plant": 2.0, "insect": 0.0, "animal": 0.0, "fish": 0.0},
            "insect": {"plant": 0.2, "insect": 3.0, "animal": 0.0, "fish": 0.0},
            "animal": {"plant": 0.2, "insect": 0.0, "animal": 3.5, "fish": 0.0},
            "fish": {"plant": 0.1, "insect": 0.0, "animal": 0.0, "fish": 4.0},
        }
        plans = gen.generate_max_yield(
            map_ids=map_ids,
            regions=regions,
            top_k=top_k,
            kind_weights=weights_map.get(category),
            route_id_prefix=prefix,
            title_label=str(req.get("title_label") or "按需大丰收"),
        )
        col_title = f"【按需大丰收】综合采集清单（{len(plans)}条）"

    col_doc = gen.build_route_collection(plans, f"{prefix}_col", col_title) if plans else None
    return {
        "routes": [p.route_doc for p in plans],
        "collection": col_doc,
        "summaries": [
            {
                "id": p.route_doc["id"],
                "title": p.route_doc["title"],
                "map": p.checkpoint.map_id,
                "region": p.checkpoint.region,
                "checkpoint": p.checkpoint.name,
                "target_count": p.target_count,
                "total_count": p.total_count,
                "counts_by_name": p.counts_by_name,
                "counts_by_kind": p.counts_by_kind,
                "walk_distance_m": p.walk_distance_m,
                "estimated_seconds": p.estimated_seconds,
                "efficiency_per_min": p.efficiency_per_min,
                "covered_points": [
                    {"name": cp.name, "kind": cp.kind, "x": cp.x, "y": cp.y, "z": cp.z}
                    for cp in p.covered_points
                ],
            }
            for p in plans
        ],
    }
