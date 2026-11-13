/**
 * Terrain heights and material spawners for the route editor.
 *
 * Only the height grid is decoded here (no 3D meshes/structures): the editor uses it for the
 * elevation profile under the map and for the height-aware pickup-coverage check, which mirrors
 * the 15 m gingerbread pickup rule used by the generator.
 */
import { invoke } from '@tauri-apps/api/core';
import type { RoutePoint } from '@/services/routeDocument';
import { readLocalFile } from '@/services/routeStudioService';

export type MaterialKind = 'plant' | 'insect' | 'animal' | 'fish';

export interface SpawnerPoint { cid: string; name: string; kind: MaterialKind; x: number; y: number; z: number; cond?: string }
export interface MaterialCatalogMeta { id: string; name: string; kind: MaterialKind; count: number }

export const MATERIAL_KIND_COLOR: Record<MaterialKind, string> = {
  plant: '#4ade80',
  insect: '#facc15',
  animal: '#f472b6',
  fish: '#38bdf8',
};
export const MATERIAL_KIND_NAME: Record<MaterialKind, string> = {
  plant: '植物',
  insect: '昆虫',
  animal: '动物',
  fish: '钓鱼点',
};

/** Map units per metre used by the navigator (15 m pickup range = 15 / 0.9 map units). */
export const METERS_PER_UNIT = 0.9;
export const PICKUP_RANGE_M = 15.0;

let catalogPromise: Promise<{ catalogs: Record<string, MaterialCatalogMeta>; spawnersByMap: Record<string, SpawnerPoint[]> }> | null = null;

export function loadMaterialCatalog() {
  if (!catalogPromise) {
    catalogPromise = readLocalFile('resource/navigation/materials_catalog_v1.json').then((raw) => {
      const data = JSON.parse(raw.replace(/^\uFEFF/, ''));
      const catalogs: Record<string, MaterialCatalogMeta> = data.catalogs ?? {};
      const spawnersByMap: Record<string, SpawnerPoint[]> = {};
      for (const [mapId, rows] of Object.entries<unknown[]>(data.spawners ?? {})) {
        const list: SpawnerPoint[] = [];
        for (const row of rows) {
          if (!Array.isArray(row)) continue;
          const cid = String(row[0]);
          const meta = catalogs[cid];
          if (!meta) continue;
          list.push({
            cid, name: meta.name, kind: meta.kind,
            x: Number(row[1]), y: Number(row[2]), z: Number(row[3]),
            cond: row[4] ? String(row[4]) : undefined,
          });
        }
        spawnersByMap[mapId] = list;
      }
      return { catalogs, spawnersByMap };
    });
    catalogPromise.catch(() => { catalogPromise = null; });
  }
  return catalogPromise;
}

export interface TerrainGrid {
  width: number;
  height: number;
  pxScale: number;
  zMin: number;
  zMax: number;
  zMap: Float32Array;
  slopeDeg: Uint8Array;
  validMask: Uint8Array;
}

const terrainCache = new Map<string, Promise<TerrainGrid>>();

export function loadTerrainGrid(mapId: string): Promise<TerrainGrid> {
  const existing = terrainCache.get(mapId);
  if (existing) return existing;
  const promise = (async () => {
    const manifest = JSON.parse((await readLocalFile('resource/navigation/terrain/manifest.json')).replace(/^\uFEFF/, ''));
    const info = manifest[mapId];
    if (!info) throw new Error(`没有 ${mapId} 的地形数据`);
    const b64 = await invoke<string>('read_local_file_base64', { filename: `resource/navigation/${info.image}` });
    const img = await new Promise<HTMLImageElement>((resolve, reject) => {
      const el = new Image();
      el.onload = () => resolve(el);
      el.onerror = reject;
      el.src = `data:image/png;base64,${b64}`;
    });
    const w = img.naturalWidth;
    const h = img.naturalHeight;
    const cvs = document.createElement('canvas');
    cvs.width = w;
    cvs.height = h;
    const ctx = cvs.getContext('2d', { willReadFrequently: true })!;
    ctx.drawImage(img, 0, 0);
    const rgba = ctx.getImageData(0, 0, w, h).data;
    const total = w * h;
    const zMap = new Float32Array(total);
    const slopeDeg = new Uint8Array(total);
    const validMask = new Uint8Array(total);
    const zOffset = Number(info.z_offset ?? -500.0);
    const zScale = Number(info.z_scale ?? 10.0);
    for (let i = 0; i < total; i++) {
      const hu16 = (rgba[i * 4] << 8) | rgba[i * 4 + 1];
      const b = rgba[i * 4 + 2];
      const valid = b & 1;
      validMask[i] = valid;
      slopeDeg[i] = (b >> 2) & 0x3f;
      zMap[i] = valid && hu16 > 0 ? hu16 / zScale + zOffset : NaN;
    }
    return {
      width: w, height: h, pxScale: Number(info.px_scale ?? 0.5),
      zMin: Number(info.z_min ?? -250), zMax: Number(info.z_max ?? 200),
      zMap, slopeDeg, validMask,
    };
  })();
  terrainCache.set(mapId, promise);
  promise.catch(() => terrainCache.delete(mapId));
  return promise;
}

export interface TerrainSample { z: number; slope: number; valid: boolean }

/** Bilinear height lookup; out-of-grid or unexplored cells are reported as invalid. */
export function sampleTerrain(grid: TerrainGrid | null, x: number, y: number): TerrainSample {
  if (!grid) return { z: NaN, slope: 0, valid: false };
  const gx = x * grid.pxScale;
  const gy = y * grid.pxScale;
  const x0 = Math.floor(gx);
  const y0 = Math.floor(gy);
  if (x0 < 0 || x0 >= grid.width || y0 < 0 || y0 >= grid.height) return { z: NaN, slope: 0, valid: false };
  const x1 = Math.min(grid.width - 1, x0 + 1);
  const y1 = Math.min(grid.height - 1, y0 + 1);
  const fx = gx - x0;
  const fy = gy - y0;
  const idx = [y0 * grid.width + x0, y0 * grid.width + x1, y1 * grid.width + x0, y1 * grid.width + x1];
  const z = idx.map((i) => (grid.validMask[i] ? grid.zMap[i] : NaN));
  if (z.every(Number.isFinite)) {
    const wts = [(1 - fx) * (1 - fy), fx * (1 - fy), (1 - fx) * fy, fx * fy];
    return {
      z: z[0] * wts[0] + z[1] * wts[1] + z[2] * wts[2] + z[3] * wts[3],
      slope: grid.slopeDeg[idx[0]] * wts[0] + grid.slopeDeg[idx[1]] * wts[1] + grid.slopeDeg[idx[2]] * wts[2] + grid.slopeDeg[idx[3]] * wts[3],
      valid: true,
    };
  }
  const near = Math.max(0, Math.min(grid.width * grid.height - 1, Math.round(gy) * grid.width + Math.round(gx)));
  const val = grid.zMap[near];
  if (!grid.validMask[near] || !Number.isFinite(val)) return { z: NaN, slope: 0, valid: false };
  return { z: val, slope: grid.slopeDeg[near], valid: true };
}

export interface ProfileSample { d: number; z: number; slope: number; valid: boolean; x: number; y: number; segment: number }
export interface ProfileMarker { index: number; d: number; z: number; valid: boolean }
export interface ElevationProfile {
  samples: ProfileSample[];
  markers: ProfileMarker[];
  /** Distances where a teleport breaks the walked path. */
  breaks: number[];
  length: number;
  zMin: number;
  zMax: number;
  ascent: number;
  descent: number;
}

const MAX_PROFILE_SAMPLES = 1600;

/** Terrain height along the walked path; teleport legs are not walked, so they break the line. */
export function buildElevationProfile(grid: TerrainGrid | null, points: RoutePoint[]): ElevationProfile | null {
  if (!grid || points.length < 1) return null;
  let walked = 0;
  for (let i = 1; i < points.length; i++) {
    if (points[i].action !== 'teleport') walked += Math.hypot(points[i].x - points[i - 1].x, points[i].y - points[i - 1].y);
  }
  const step = Math.max(1, walked / MAX_PROFILE_SAMPLES);
  const samples: ProfileSample[] = [];
  const markers: ProfileMarker[] = [];
  const breaks: number[] = [];
  let d = 0;
  const push = (x: number, y: number, segment: number) => {
    const s = sampleTerrain(grid, x, y);
    samples.push({ d, z: s.z, slope: s.slope, valid: s.valid, x, y, segment });
  };
  const first = sampleTerrain(grid, points[0].x, points[0].y);
  markers.push({ index: 0, d: 0, z: first.z, valid: first.valid });
  push(points[0].x, points[0].y, 0);
  for (let i = 1; i < points.length; i++) {
    const a = points[i - 1];
    const b = points[i];
    if (b.action === 'teleport') {
      breaks.push(d);
      const s = sampleTerrain(grid, b.x, b.y);
      markers.push({ index: i, d, z: s.z, valid: s.valid });
      samples.push({ d, z: NaN, slope: 0, valid: false, x: b.x, y: b.y, segment: i });
      push(b.x, b.y, i);
      continue;
    }
    const len = Math.hypot(b.x - a.x, b.y - a.y);
    const n = Math.max(1, Math.ceil(len / step));
    const start = d;
    for (let k = 1; k <= n; k++) {
      const t = k / n;
      d = start + len * t;
      push(a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t, i);
    }
    const s = sampleTerrain(grid, b.x, b.y);
    markers.push({ index: i, d, z: s.z, valid: s.valid });
  }
  let zMin = Infinity;
  let zMax = -Infinity;
  let ascent = 0;
  let descent = 0;
  let prev: ProfileSample | null = null;
  for (const s of samples) {
    if (!s.valid) { prev = null; continue; }
    zMin = Math.min(zMin, s.z);
    zMax = Math.max(zMax, s.z);
    if (prev) {
      const dz = s.z - prev.z;
      if (dz > 0) ascent += dz; else descent -= dz;
    }
    prev = s;
  }
  if (!Number.isFinite(zMin)) { zMin = grid.zMin; zMax = grid.zMax; }
  return { samples, markers, breaks, length: d, zMin, zMax, ascent, descent };
}

export interface AnnotatedSpawner { pt: SpawnerPoint; captured: boolean; isTarget: boolean }

/**
 * Which material spawners the route reaches. Same rule the previous 3D view used: with wait
 * points only the stops count (gingerbread pickups happen while standing), otherwise every
 * waypoint does; plants can also be picked while passing a segment. Height matters, so a
 * spawner on a cliff above the path is not counted.
 */
export function annotateSpawners(
  grid: TerrainGrid | null, spawners: SpawnerPoint[], points: RoutePoint[], targetMaterial?: string,
): { list: AnnotatedSpawner[]; capturedCount: number; targetCount: number; targetCaptured: number } {
  const query = (targetMaterial ?? '').trim();
  const empty = { list: [] as AnnotatedSpawner[], capturedCount: 0, targetCount: 0, targetCaptured: 0 };
  if (!spawners.length) return empty;
  let lastZ = grid ? (grid.zMin + grid.zMax) * 0.35 : 0;
  const wps = points.map((pt) => {
    const s = sampleTerrain(grid, pt.x, pt.y);
    if (s.valid) lastZ = s.z;
    return { ...pt, z: s.valid ? s.z : lastZ };
  });
  const range = PICKUP_RANGE_M / METERS_PER_UNIT;
  const stops = wps.some((wp) => wp.action === 'wait') ? wps.filter((wp) => wp.action === 'wait') : wps;
  let capturedCount = 0;
  let targetCount = 0;
  let targetCaptured = 0;
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const wp of wps) {
    minX = Math.min(minX, wp.x); maxX = Math.max(maxX, wp.x);
    minY = Math.min(minY, wp.y); maxY = Math.max(maxY, wp.y);
  }
  const list = spawners.map((sp) => {
    let captured = false;
    const near = sp.x >= minX - range && sp.x <= maxX + range && sp.y >= minY - range && sp.y <= maxY + range;
    if (near) for (const wp of stops) {
      if (Math.abs(sp.x - wp.x) > range || Math.abs(sp.y - wp.y) > range) continue;
      const dz = Math.abs(sp.z - wp.z);
      const d2 = Math.hypot(sp.x - wp.x, sp.y - wp.y) * METERS_PER_UNIT;
      if (dz <= 10.5 && Math.hypot(d2, dz) <= PICKUP_RANGE_M) { captured = true; break; }
    }
    if (near && !captured && sp.kind === 'plant' && wps.length >= 2) {
      for (let k = 0; k < wps.length - 1; k++) {
        const p0 = wps[k];
        const p1 = wps[k + 1];
        if (p1.action === 'teleport') continue;
        const vx = p1.x - p0.x;
        const vy = p1.y - p0.y;
        const lenSq = vx * vx + vy * vy;
        const t = lenSq > 1e-6 ? Math.max(0, Math.min(1, ((sp.x - p0.x) * vx + (sp.y - p0.y) * vy) / lenSq)) : 0;
        const cz = p0.z + t * (p1.z - p0.z);
        const dz = Math.abs(sp.z - cz);
        const d2 = Math.hypot(sp.x - (p0.x + t * vx), sp.y - (p0.y + t * vy)) * METERS_PER_UNIT;
        if (dz <= 10.5 && Math.hypot(d2, dz) <= 13.5) { captured = true; break; }
      }
    }
    const isTarget = Boolean(query && (sp.name.includes(query) || sp.cond?.includes(query)));
    if (captured) capturedCount++;
    if (isTarget) {
      targetCount++;
      if (captured) targetCaptured++;
    }
    return { pt: sp, captured, isTarget };
  });
  return { list, capturedCount, targetCount, targetCaptured };
}
