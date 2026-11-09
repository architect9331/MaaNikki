import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from 'react';
import { Compass, Eye, Layers, Mountain, Navigation, RotateCcw, Sparkles } from 'lucide-react';
import clsx from 'clsx';
import { invoke } from '@tauri-apps/api/core';
import { actionMeta, type Checkpoint, type RoutePoint, type TrialWaypointReport } from '@/services/routeDocument';
import { readLocalFile } from '@/services/routeStudioService';
import type { MapCanvasHandle, MapInfo } from './RouteMapCanvas';

interface TerrainGridData {
  mapId: string;
  width: number;
  height: number;
  pxScale: number;
  zMin: number;
  zMax: number;
  zMap: Float32Array;
  slopeDeg: Uint8Array;
  obsMask: Uint8Array;
  validMask: Uint8Array;
}

export interface SpawnerPoint3D {
  cid: string;
  name: string;
  kind: 'plant' | 'insect' | 'animal' | 'fish';
  x: number;
  y: number;
  z: number;
  cond?: string;
}

export interface MaterialCatalogMeta {
  id: string;
  name: string;
  kind: 'plant' | 'insect' | 'animal' | 'fish';
  count: number;
}

let cachedCatalogPromise: Promise<{
  catalogs: Record<string, MaterialCatalogMeta>;
  spawnersByMap: Record<string, SpawnerPoint3D[]>;
}> | null = null;

export function loadMaterialCatalog3D() {
  if (!cachedCatalogPromise) {
    cachedCatalogPromise = readLocalFile('resource/navigation/materials_catalog_v1.json').then((raw) => {
      const data = JSON.parse(raw.replace(/^\uFEFF/, ''));
      const catalogs: Record<string, MaterialCatalogMeta> = data.catalogs ?? {};
      const spawnersByMap: Record<string, SpawnerPoint3D[]> = {};
      for (const [mapId, rows] of Object.entries<unknown[]>(data.spawners ?? {})) {
        const list: SpawnerPoint3D[] = [];
        for (const row of rows) {
          if (Array.isArray(row)) {
            const cid = String(row[0]);
            const meta = catalogs[cid];
            if (!meta) continue;
            list.push({
              cid,
              name: meta.name,
              kind: meta.kind,
              x: Number(row[1]),
              y: Number(row[2]),
              z: Number(row[3]),
              cond: row[4] ? String(row[4]) : undefined,
            });
          }
        }
        spawnersByMap[mapId] = list;
      }
      return { catalogs, spawnersByMap };
    });
  }
  return cachedCatalogPromise;
}

const terrainCache = new Map<string, Promise<TerrainGridData>>();

function loadTerrainGrid(mapId: string): Promise<TerrainGridData> {
  const existing = terrainCache.get(mapId);
  if (existing) return existing;
  const promise = (async () => {
    const manifestRaw = await readLocalFile('resource/navigation/terrain/manifest.json');
    const manifest = JSON.parse(manifestRaw.replace(/^\uFEFF/, ''));
    const info = manifest[mapId];
    if (!info) throw new Error(`无 ${mapId} 地形网格数据`);
    const b64 = await invoke<string>('read_local_file_base64', {
      filename: `resource/navigation/${info.image}`,
    });
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
    const obsMask = new Uint8Array(total);
    const validMask = new Uint8Array(total);
    for (let i = 0; i < total; i++) {
      const r = rgba[i * 4];
      const g = rgba[i * 4 + 1];
      const b = rgba[i * 4 + 2];
      const hu16 = (r << 8) | g;
      const valid = b & 1;
      validMask[i] = valid;
      obsMask[i] = (b >> 1) & 1;
      slopeDeg[i] = (b >> 2) & 0x3f;
      zMap[i] = valid && hu16 > 0 ? hu16 / 10.0 - 500.0 : NaN;
    }
    return {
      mapId,
      width: w,
      height: h,
      pxScale: Number(info.px_scale ?? 0.5),
      zMin: Number(info.z_min ?? -250),
      zMax: Number(info.z_max ?? 200),
      zMap,
      slopeDeg,
      obsMask,
      validMask,
    };
  })();
  terrainCache.set(mapId, promise);
  return promise;
}

function sampleTerrain(grid: TerrainGridData | null, x: number, y: number, fallbackZ = 0) {
  const safeFallback = Number.isFinite(fallbackZ) ? fallbackZ : 0;
  if (!grid) return { z: safeFallback, slope: 0, obs: false, valid: false };
  const gx = Math.round(x * grid.pxScale);
  const gy = Math.round(y * grid.pxScale);
  if (gx < 0 || gx >= grid.width || gy < 0 || gy >= grid.height) {
    return { z: safeFallback, slope: 0, obs: false, valid: false };
  }
  const idx = gy * grid.width + gx;
  const val = grid.zMap[idx];
  if (!grid.validMask[idx] || !Number.isFinite(val)) {
    return { z: safeFallback, slope: 0, obs: false, valid: false };
  }
  return {
    z: val,
    slope: grid.slopeDeg[idx],
    obs: grid.obsMask[idx] === 1,
    valid: true,
  };
}

function elevationColor(t: number): [number, number, number] {
  const c = Math.max(0, Math.min(1, Number.isFinite(t) ? t : 0.35));
  if (c < 0.2) {
    const u = c / 0.2;
    return [Math.round(38 + 32 * u), Math.round(115 + 65 * u), Math.round(168 - 20 * u)];
  }
  if (c < 0.52) {
    const u = (c - 0.2) / 0.32;
    return [Math.round(70 + 88 * u), Math.round(180 + 22 * u), Math.round(148 - 65 * u)];
  }
  if (c < 0.8) {
    const u = (c - 0.52) / 0.28;
    return [Math.round(158 + 68 * u), Math.round(202 - 48 * u), Math.round(83 + 12 * u)];
  }
  const u = (c - 0.8) / 0.2;
  return [Math.round(226 + 24 * u), Math.round(154 + 88 * u), Math.round(95 + 135 * u)];
}

const KIND_COLOR: Record<string, string> = {
  plant: '#4ade80',
  insect: '#facc15',
  animal: '#f472b6',
  fish: '#38bdf8',
};
const KIND_NAME: Record<string, string> = {
  plant: '植物采集',
  insect: '昆虫捕虫',
  animal: '动物清洁',
  fish: '钓鱼点',
};

interface Route3DCanvasProps {
  map: MapInfo | null;
  image: HTMLCanvasElement | null;
  points: RoutePoint[];
  preview?: RoutePoint[] | null;
  samples?: [number, number][] | null;
  pose?: { x: number; y: number; heading: number | null } | null;
  teleports?: Checkpoint[];
  startTeleport?: Checkpoint;
  selected: number | null;
  selectedSet?: Set<number>;
  activeTrialIndex?: number | null;
  trialWaypoints?: TrialWaypointReport[] | null;
  readOnly: boolean;
  targetMaterial?: string;
  onSelect: (index: number | null, toggle?: boolean) => void;
}

export const Route3DCanvas = forwardRef<MapCanvasHandle, Route3DCanvasProps>(function Route3DCanvas(
  {
    map,
    image,
    points,
    preview,
    pose,
    teleports,
    startTeleport,
    selected,
    selectedSet,
    activeTrialIndex,
    targetMaterial,
    onSelect,
  },
  ref,
) {
  const containerRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const radarRef = useRef<HTMLCanvasElement>(null);
  const [size, setSize] = useState({ width: 900, height: 640 });
  const [grid, setGrid] = useState<TerrainGridData | null>(null);
  const [spawners, setSpawners] = useState<SpawnerPoint3D[]>([]);
  const [surfaceMode, setSurfaceMode] = useState<'map' | 'elevation' | 'walkability'>('map');
  const [zScale, setZScale] = useState<number>(1.8);
  const [showAura, setShowAura] = useState(true);
  const [showSpawners, setShowSpawners] = useState(true);
  const [dragging, setDragging] = useState(false);

  // yaw=0 means looking due North (top of 2D map), pitch=0.82 rad (~47 deg looking down from above)
  const [cam, setCam] = useState({
    targetX: 8960,
    targetY: 7940,
    yaw: 0.0,
    pitch: 0.82,
    distance: 320,
  });

  const [hoverInfo, setHoverInfo] = useState<{
    title: string;
    subtitle: string;
    screenX: number;
    screenY: number;
  } | null>(null);

  const screenWaypointsRef = useRef<Array<{ index: number; sx: number; sy: number; depth: number; z: number; slope: number }>>([]);
  const screenSpawnersRef = useRef<Array<{ pt: SpawnerPoint3D; sx: number; sy: number; captured: boolean }>>([]);
  const mapPixelsRef = useRef<{ data: Uint8ClampedArray; w: number; h: number } | null>(null);

  useEffect(() => {
    if (!image) {
      mapPixelsRef.current = null;
      return;
    }
    try {
      const ctx = image.getContext('2d', { willReadFrequently: true });
      if (ctx) {
        const imgData = ctx.getImageData(0, 0, image.width, image.height);
        mapPixelsRef.current = { data: imgData.data, w: image.width, h: image.height };
      }
    } catch {
      mapPixelsRef.current = null;
    }
  }, [image]);

  useEffect(() => {
    if (!map?.id) return;
    let cancelled = false;
    loadTerrainGrid(map.id)
      .then((g) => {
        if (!cancelled) setGrid(g);
      })
      .catch(() => {
        if (!cancelled) setGrid(null);
      });
    loadMaterialCatalog3D()
      .then(({ spawnersByMap }) => {
        if (!cancelled) setSpawners(spawnersByMap[map.id] ?? []);
      })
      .catch(() => {
        if (!cancelled) setSpawners([]);
      });
    return () => {
      cancelled = true;
    };
  }, [map?.id]);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const obs = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect;
      if (rect && rect.width > 10 && rect.height > 10) {
        setSize({ width: Math.round(rect.width), height: Math.round(rect.height) });
      }
    });
    obs.observe(el);
    return () => obs.disconnect();
  }, []);

  const fit = useCallback(() => {
    const activePts = preview?.length ? preview : points;
    if (activePts.length > 0) {
      let minX = Infinity;
      let maxX = -Infinity;
      let minY = Infinity;
      let maxY = -Infinity;
      for (const p of activePts) {
        if (p.x < minX) minX = p.x;
        if (p.x > maxX) maxX = p.x;
        if (p.y < minY) minY = p.y;
        if (p.y > maxY) maxY = p.y;
      }
      const cx = (minX + maxX) * 0.5;
      const cy = (minY + maxY) * 0.5;
      const span = Math.max(110, Math.hypot(maxX - minX, maxY - minY) * 1.25);
      setCam((prev) => ({
        ...prev,
        targetX: cx,
        targetY: cy,
        distance: Math.min(1400, Math.max(130, span)),
      }));
      return;
    }
    if (startTeleport) {
      setCam((prev) => ({ ...prev, targetX: startTeleport.x, targetY: startTeleport.y, distance: 280 }));
      return;
    }
    if (teleports && teleports.length > 0) {
      const florawish = teleports.find((t) => t.region?.includes('花愿镇')) ?? teleports[0];
      setCam((prev) => ({ ...prev, targetX: florawish.x, targetY: florawish.y, distance: 380 }));
      return;
    }
    if (map) {
      setCam((prev) => ({
        ...prev,
        targetX: map.id === 'miraland' ? 8960 : map.width * 0.5,
        targetY: map.id === 'miraland' ? 7940 : map.height * 0.5,
        distance: 450,
      }));
    }
  }, [points, preview, startTeleport, teleports, map]);

  useEffect(() => {
    fit();
  }, [map?.id, points.length === 0]);

  useImperativeHandle(
    ref,
    () => ({
      fit,
      zoom: (factor: number) => {
        setCam((prev) => ({
          ...prev,
          distance: Math.max(70, Math.min(2200, prev.distance / factor)),
        }));
      },
      focus: (x: number, y: number) => {
        setCam((prev) => ({
          ...prev,
          targetX: x,
          targetY: y,
          distance: Math.min(prev.distance, 220),
        }));
      },
    }),
    [fit],
  );

  // Enrich route points with ground Z and slope
  const points3D = useMemo(() => {
    const activePts = preview?.length ? preview : points;
    let lastValidZ = grid ? (grid.zMin + grid.zMax) * 0.35 : -190;
    return activePts.map((pt, index) => {
      const s = sampleTerrain(grid, pt.x, pt.y, lastValidZ);
      if (s.valid) lastValidZ = s.z;
      return {
        ...pt,
        index,
        z: s.z,
        slope: s.slope,
        obs: s.obs,
      };
    });
  }, [points, preview, grid]);

  // Filter nearby spawners and determine which ones are captured by the route's 15m Gingerbread aura
  const nearbySpawners = useMemo(() => {
    if (!showSpawners || !spawners.length) return [];
    const viewR = Math.max(280, cam.distance * 1.15);
    const auraPx = 14.5 / 0.9;
    const result: Array<{ pt: SpawnerPoint3D; captured: boolean; isTarget: boolean }> = [];
    const tgtQuery = (targetMaterial ?? '').trim();
    for (const sp of spawners) {
      if (Math.abs(sp.x - cam.targetX) > viewR || Math.abs(sp.y - cam.targetY) > viewR) continue;
      let captured = false;
      for (const wp of points3D) {
        const d = Math.hypot(sp.x - wp.x, sp.y - wp.y);
        if (d <= auraPx && Math.abs(sp.z - wp.z) <= 6.2) {
          captured = true;
          break;
        }
      }
      const isTarget = Boolean(
        tgtQuery && (sp.name.includes(tgtQuery) || (sp.cond && sp.cond.includes(tgtQuery))),
      );
      result.push({ pt: sp, captured, isTarget });
    }
    return result;
  }, [spawners, showSpawners, cam.targetX, cam.targetY, cam.distance, points3D, targetMaterial]);

  const centerSample = useMemo(
    () =>
      sampleTerrain(
        grid,
        cam.targetX,
        cam.targetY,
        points3D[0]?.z ?? (grid ? (grid.zMin + grid.zMax) * 0.35 : -190),
      ),
    [grid, cam.targetX, cam.targetY, points3D],
  );

  // Main 3D render loop
  useEffect(() => {
    const cvs = canvasRef.current;
    if (!cvs) return;
    const ctx = cvs.getContext('2d');
    if (!ctx) return;
    const dpr = window.devicePixelRatio || 1;
    cvs.width = size.width * dpr;
    cvs.height = size.height * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    // Bright sky-blue / atmospheric horizon background
    const bgGrad = ctx.createLinearGradient(0, 0, 0, size.height);
    bgGrad.addColorStop(0, '#1e293b');
    bgGrad.addColorStop(0.45, '#1e3a5f');
    bgGrad.addColorStop(1, '#0f172a');
    ctx.fillStyle = bgGrad;
    ctx.fillRect(0, 0, size.width, size.height);

    const centerZ = centerSample.z;
    const cosYaw = Math.cos(cam.yaw);
    const sinYaw = Math.sin(cam.yaw);
    const cosPitch = Math.cos(cam.pitch);
    const sinPitch = Math.sin(cam.pitch);
    const fovScale = Math.min(size.width, size.height) * 0.95;
    const mPerPx = 0.9;

    // Right-side-up 3D camera projection:
    // +right is screen right; +depthGround is away from camera (North when yaw=0); +up is elevation above centerZ.
    const project = (wx: number, wy: number, wz: number) => {
      const dx = wx - cam.targetX;
      const dy = wy - cam.targetY;
      const up = ((wz - centerZ) / mPerPx) * zScale;
      const right = dx * cosYaw - dy * sinYaw;
      const depthGround = -dx * sinYaw - dy * cosYaw;
      const cz = cam.distance + depthGround * cosPitch - up * sinPitch;
      if (cz <= 12) return null;
      const screenUp = depthGround * sinPitch + up * cosPitch;
      const scale = fovScale / cz;
      return {
        sx: size.width * 0.5 + right * scale,
        sy: size.height * 0.54 - screenUp * scale,
        depth: cz,
        scale,
      };
    };

    const N = dragging ? 60 : 84;
    const patchRadius = Math.max(90, Math.min(1250, cam.distance * 0.92));
    const step = (patchRadius * 2) / N;
    const startX = cam.targetX - patchRadius;
    const startY = cam.targetY - patchRadius;
    const zSpan = Math.max(20, (grid?.zMax ?? 100) - (grid?.zMin ?? -250));
    const zMin = grid?.zMin ?? -250;
    const mapPx = mapPixelsRef.current;
    const texScaleX = mapPx && map ? mapPx.w / map.width : 1;
    const texScaleY = mapPx && map ? mapPx.h / map.height : 1;

    const verts: Array<{ sx: number; sy: number; depth: number; z: number; slope: number; obs: boolean; valid: boolean; wx: number; wy: number } | null> =
      new Array((N + 1) * (N + 1));

    for (let iy = 0; iy <= N; iy++) {
      const wy = startY + iy * step;
      for (let ix = 0; ix <= N; ix++) {
        const wx = startX + ix * step;
        const s = sampleTerrain(grid, wx, wy, centerZ);
        const p = project(wx, wy, s.z);
        verts[iy * (N + 1) + ix] = p
          ? { sx: p.sx, sy: p.sy, depth: p.depth, z: s.z, slope: s.slope, obs: s.obs, valid: s.valid, wx, wy }
          : null;
      }
    }

    interface Quad {
      depth: number;
      x0: number; y0: number;
      x1: number; y1: number;
      x2: number; y2: number;
      x3: number; y3: number;
      fill: string;
      stroke: string | null;
    }
    const quads: Quad[] = [];
    for (let iy = 0; iy < N; iy++) {
      for (let ix = 0; ix < N; ix++) {
        const v00 = verts[iy * (N + 1) + ix];
        const v10 = verts[iy * (N + 1) + ix + 1];
        const v11 = verts[(iy + 1) * (N + 1) + ix + 1];
        const v01 = verts[(iy + 1) * (N + 1) + ix];
        if (!v00 || !v10 || !v11 || !v01) continue;

        const dzx = (v10.z - v00.z) / (step * mPerPx);
        const dzy = (v01.z - v00.z) / (step * mPerPx);
        const shade = Math.max(0.55, Math.min(1.28, 0.98 + (-dzx * 0.42 - dzy * 0.35) * 0.42));
        const avgZ = (v00.z + v10.z + v11.z + v01.z) * 0.25;
        const maxSlp = Math.max(v00.slope, v10.slope, v11.slope, v01.slope);
        const hasObs = v00.obs || v10.obs || v11.obs || v01.obs;
        const midX = (v00.wx + v11.wx) * 0.5;
        const midY = (v00.wy + v11.wy) * 0.5;

        let r = 68;
        let g = 148;
        let b = 112;
        if (surfaceMode === 'map' && mapPx) {
          const mx = Math.max(0, Math.min(mapPx.w - 1, Math.round(midX * texScaleX)));
          const my = Math.max(0, Math.min(mapPx.h - 1, Math.round(midY * texScaleY)));
          const pi = (my * mapPx.w + mx) * 4;
          if (mapPx.data[pi + 3] > 15) {
            // Boost color vibrancy slightly so roads, rivers, roofs, and forests pop clearly in 3D
            const mr = mapPx.data[pi];
            const mg = mapPx.data[pi + 1];
            const mb = mapPx.data[pi + 2];
            const lum = (mr + mg + mb) / 3;
            r = Math.max(0, Math.min(255, Math.round(lum + (mr - lum) * 1.28)));
            g = Math.max(0, Math.min(255, Math.round(lum + (mg - lum) * 1.28)));
            b = Math.max(0, Math.min(255, Math.round(lum + (mb - lum) * 1.28)));
          } else if (v00.valid) {
            [r, g, b] = elevationColor((avgZ - zMin) / zSpan);
          } else {
            continue;
          }
        } else if (surfaceMode === 'walkability') {
          if (!v00.valid && !v11.valid) continue;
          if (hasObs) [r, g, b] = [42, 128, 225];
          else if (maxSlp > 42) [r, g, b] = [235, 68, 72];
          else if (maxSlp > 34) [r, g, b] = [242, 175, 52];
          else [r, g, b] = [56, 195, 118];
        } else {
          if (!v00.valid && !v11.valid) continue;
          [r, g, b] = elevationColor((avgZ - zMin) / zSpan);
        }

        r = Math.min(255, Math.round(r * shade));
        g = Math.min(255, Math.round(g * shade));
        b = Math.min(255, Math.round(b * shade));
        const isContour = v00.valid && Math.floor(v00.z / 12) !== Math.floor(v11.z / 12);
        const isGridLine = ix % 8 === 0 || iy % 8 === 0;
        quads.push({
          depth: (v00.depth + v10.depth + v11.depth + v01.depth) * 0.25,
          x0: v00.sx, y0: v00.sy,
          x1: v10.sx, y1: v10.sy,
          x2: v11.sx, y2: v11.sy,
          x3: v01.sx, y3: v01.sy,
          fill: `rgb(${r},${g},${b})`,
          stroke: isContour ? 'rgba(255,255,255,0.22)' : isGridLine ? 'rgba(15,23,42,0.14)' : null,
        });
      }
    }
    quads.sort((a, b) => b.depth - a.depth);

    for (const q of quads) {
      ctx.beginPath();
      ctx.moveTo(q.x0, q.y0);
      ctx.lineTo(q.x1, q.y1);
      ctx.lineTo(q.x2, q.y2);
      ctx.lineTo(q.x3, q.y3);
      ctx.closePath();
      ctx.fillStyle = q.fill;
      ctx.fill();
      if (q.stroke) {
        ctx.strokeStyle = q.stroke;
        ctx.lineWidth = 0.7;
        ctx.stroke();
      }
    }

    // Draw Camera Pivot Target Reticle on the 3D ground so user always sees camera center & cardinal N/E/S/W axes
    const pCenter = project(cam.targetX, cam.targetY, centerZ + 0.4);
    if (pCenter) {
      const axisLen = Math.max(18, cam.distance * 0.065);
      const pN = project(cam.targetX, cam.targetY - axisLen, centerZ + 0.4);
      const pE = project(cam.targetX + axisLen, cam.targetY, centerZ + 0.4);
      if (pN) {
        ctx.beginPath();
        ctx.moveTo(pCenter.sx, pCenter.sy);
        ctx.lineTo(pN.sx, pN.sy);
        ctx.strokeStyle = 'rgba(248,113,113,0.85)';
        ctx.lineWidth = 2;
        ctx.stroke();
      }
      if (pE) {
        ctx.beginPath();
        ctx.moveTo(pCenter.sx, pCenter.sy);
        ctx.lineTo(pE.sx, pE.sy);
        ctx.strokeStyle = 'rgba(56,189,248,0.85)';
        ctx.lineWidth = 2;
        ctx.stroke();
      }
      ctx.beginPath();
      ctx.arc(pCenter.sx, pCenter.sy, 4.5, 0, Math.PI * 2);
      ctx.strokeStyle = 'rgba(255,255,255,0.8)';
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }

    // Draw 15m Gingerbread-Man aura rings draped on 3D terrain around dwell/anchor points
    if (showAura && points3D.length > 0) {
      const auraPx = 15.0 / mPerPx;
      for (let i = 0; i < points3D.length; i++) {
        const wp = points3D[i];
        const isWait = wp.action === 'wait' || i === selected || i === points3D.length - 1;
        if (!isWait && i % 2 !== 0) continue;
        ctx.beginPath();
        let started = false;
        const ringSteps = 24;
        for (let s = 0; s <= ringSteps; s++) {
          const ang = (Math.PI * 2 * s) / ringSteps;
          const rx = wp.x + Math.cos(ang) * auraPx;
          const ry = wp.y + Math.sin(ang) * auraPx;
          const rz = sampleTerrain(grid, rx, ry, wp.z).z + 0.5;
          const p = project(rx, ry, rz);
          if (!p) continue;
          if (!started) {
            ctx.moveTo(p.sx, p.sy);
            started = true;
          } else {
            ctx.lineTo(p.sx, p.sy);
          }
        }
        if (started) {
          ctx.closePath();
          ctx.fillStyle = i === selected ? 'rgba(56,189,248,0.22)' : isWait ? 'rgba(250,204,21,0.14)' : 'rgba(74,222,128,0.06)';
          ctx.fill();
          ctx.strokeStyle = i === selected ? 'rgba(56,189,248,0.9)' : isWait ? 'rgba(250,204,21,0.65)' : 'rgba(74,222,128,0.32)';
          ctx.lineWidth = i === selected ? 2.0 : 1.2;
          ctx.stroke();
        }
      }
    }

    // Draw 3D material spawners
    const drawnSpawners: Array<{ pt: SpawnerPoint3D; sx: number; sy: number; captured: boolean }> = [];
    for (const { pt, captured, isTarget } of nearbySpawners) {
      const pGround = project(pt.x, pt.y, pt.z);
      const pPin = project(pt.x, pt.y, pt.z + (captured ? 3.5 : 1.6));
      if (!pGround || !pPin) continue;
      const color = KIND_COLOR[pt.kind] ?? '#a78bfa';
      if (captured || isTarget) {
        ctx.beginPath();
        ctx.moveTo(pGround.sx, pGround.sy);
        ctx.lineTo(pPin.sx, pPin.sy);
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.4;
        ctx.stroke();
      }
      const rad = captured ? (isTarget ? 5.5 : 4.2) : 2.5;
      ctx.beginPath();
      ctx.arc(pPin.sx, pPin.sy, rad, 0, Math.PI * 2);
      ctx.fillStyle = captured ? color : 'rgba(226,232,240,0.55)';
      ctx.fill();
      if (captured) {
        ctx.strokeStyle = isTarget ? '#ffffff' : '#0f172a';
        ctx.lineWidth = isTarget ? 1.8 : 1.1;
        ctx.stroke();
      }
      drawnSpawners.push({ pt, sx: pPin.sx, sy: pPin.sy, captured });
    }
    screenSpawnersRef.current = drawnSpawners;

    // Draw nearby Warp Spire teleports as tall violet beacons with floating 3D landmark nameplates
    if (teleports) {
      for (const cp of teleports) {
        if (Math.abs(cp.x - cam.targetX) > patchRadius || Math.abs(cp.y - cam.targetY) > patchRadius) continue;
        const cz = sampleTerrain(grid, cp.x, cp.y, centerZ).z;
        const pBase = project(cp.x, cp.y, cz);
        const pTop = project(cp.x, cp.y, cz + 14);
        if (!pBase || !pTop) continue;
        const isStart = startTeleport?.name === cp.name;
        ctx.beginPath();
        ctx.moveTo(pBase.sx, pBase.sy);
        ctx.lineTo(pTop.sx, pTop.sy);
        ctx.strokeStyle = isStart ? '#d8b4fe' : 'rgba(192,132,252,0.8)';
        ctx.lineWidth = isStart ? 2.8 : 1.8;
        ctx.stroke();
        ctx.beginPath();
        ctx.arc(pTop.sx, pTop.sy, isStart ? 6.0 : 4.5, 0, Math.PI * 2);
        ctx.fillStyle = isStart ? '#c084fc' : '#9333ea';
        ctx.fill();
        ctx.strokeStyle = '#ffffff';
        ctx.lineWidth = 1.4;
        ctx.stroke();

        // Draw readable landmark label
        ctx.font = isStart ? '600 11px system-ui, sans-serif' : '500 10px system-ui, sans-serif';
        ctx.textAlign = 'center';
        ctx.strokeStyle = 'rgba(15,23,42,0.88)';
        ctx.lineWidth = 3;
        ctx.strokeText(cp.name, pTop.sx, pTop.sy - 9);
        ctx.fillStyle = isStart ? '#f3e8ff' : '#e2e8f0';
        ctx.fillText(cp.name, pTop.sx, pTop.sy - 9);
      }
    }

    // Draw 3D route trajectory and waypoint pillars
    const drawnWaypoints: Array<{ index: number; sx: number; sy: number; depth: number; z: number; slope: number }> = [];
    if (points3D.length > 0) {
      ctx.beginPath();
      let started = false;
      for (const wp of points3D) {
        const p = project(wp.x, wp.y, wp.z + 0.3);
        if (!p) continue;
        if (!started) {
          ctx.moveTo(p.sx, p.sy);
          started = true;
        } else {
          ctx.lineTo(p.sx, p.sy);
        }
      }
      if (started) {
        ctx.strokeStyle = 'rgba(15,23,42,0.7)';
        ctx.lineWidth = 5.0;
        ctx.stroke();
      }

      ctx.beginPath();
      started = false;
      for (const wp of points3D) {
        const p = project(wp.x, wp.y, wp.z + 2.0);
        if (!p) continue;
        if (!started) {
          ctx.moveTo(p.sx, p.sy);
          started = true;
        } else {
          ctx.lineTo(p.sx, p.sy);
        }
      }
      if (started) {
        ctx.strokeStyle = '#38bdf8';
        ctx.lineWidth = 3.0;
        ctx.stroke();
      }

      for (const wp of points3D) {
        const pBase = project(wp.x, wp.y, wp.z);
        const pTop = project(wp.x, wp.y, wp.z + (wp.jump ? 5.5 : wp.action ? 4.5 : 2.4));
        if (!pBase || !pTop) continue;
        const isSel = selected === wp.index || Boolean(selectedSet?.has(wp.index));
        const isTrial = activeTrialIndex === wp.index;
        const meta = actionMeta(wp.action);

        ctx.beginPath();
        ctx.moveTo(pBase.sx, pBase.sy);
        ctx.lineTo(pTop.sx, pTop.sy);
        ctx.strokeStyle = isSel ? '#f43f5e' : wp.jump ? '#f59e0b' : meta.color || '#38bdf8';
        ctx.lineWidth = isSel ? 2.6 : 1.6;
        ctx.stroke();

        const r = isSel || isTrial ? 7.8 : wp.action || wp.jump ? 6.2 : 4.6;
        ctx.beginPath();
        ctx.arc(pTop.sx, pTop.sy, r, 0, Math.PI * 2);
        ctx.fillStyle = isSel
          ? '#f43f5e'
          : isTrial
            ? '#22c55e'
            : wp.index === 0
              ? '#10b981'
              : meta.color || (wp.jump ? '#f59e0b' : '#0ea5e9');
        ctx.fill();
        ctx.strokeStyle = '#ffffff';
        ctx.lineWidth = isSel ? 2.2 : 1.4;
        ctx.stroke();

        if (isSel || wp.index === 0 || wp.index === points3D.length - 1 || wp.action) {
          ctx.font = '600 10px system-ui, sans-serif';
          ctx.textAlign = 'center';
          ctx.strokeStyle = 'rgba(15,23,42,0.85)';
          ctx.lineWidth = 2.5;
          ctx.strokeText(`${wp.index + 1}`, pTop.sx, pTop.sy - r - 4);
          ctx.fillStyle = '#ffffff';
          ctx.fillText(`${wp.index + 1}`, pTop.sx, pTop.sy - r - 4);
        }
        drawnWaypoints.push({ index: wp.index, sx: pTop.sx, sy: pTop.sy, depth: pTop.depth, z: wp.z, slope: wp.slope });
      }
    }
    screenWaypointsRef.current = drawnWaypoints;

    if (pose) {
      const pz = sampleTerrain(grid, pose.x, pose.y, centerZ).z;
      const p = project(pose.x, pose.y, pz + 3.0);
      if (p) {
        ctx.beginPath();
        ctx.arc(p.sx, p.sy, 7.5, 0, Math.PI * 2);
        ctx.fillStyle = '#22c55e';
        ctx.fill();
        ctx.strokeStyle = '#ffffff';
        ctx.lineWidth = 2;
        ctx.stroke();
      }
    }

    // Draw 2D Mini-Radar Overview with Camera Position & Viewing Frustum Cone
    const radar = radarRef.current;
    if (radar && map) {
      const rCtx = radar.getContext('2d');
      if (rCtx) {
        const rw = radar.width;
        const rh = radar.height;
        rCtx.clearRect(0, 0, rw, rh);
        rCtx.fillStyle = '#0f172a';
        rCtx.fillRect(0, 0, rw, rh);
        if (image) {
          rCtx.drawImage(image, 0, 0, rw, rh);
        }
        const sx = rw / map.width;
        const sy = rh / map.height;

        if (points3D.length > 1) {
          rCtx.beginPath();
          points3D.forEach((p, idx) => {
            if (idx === 0) rCtx.moveTo(p.x * sx, p.y * sy);
            else rCtx.lineTo(p.x * sx, p.y * sy);
          });
          rCtx.strokeStyle = '#38bdf8';
          rCtx.lineWidth = 1.8;
          rCtx.stroke();
        }

        const tx = cam.targetX * sx;
        const ty = cam.targetY * sy;
        // Camera is behind target along (+sinYaw, +cosYaw), looking towards (-sinYaw, -cosYaw)
        const lookAng = Math.atan2(-cosYaw, -sinYaw);
        const coneLen = Math.max(18, Math.min(55, cam.distance * sx * 1.4));
        rCtx.beginPath();
        rCtx.moveTo(tx, ty);
        rCtx.arc(tx, ty, coneLen, lookAng - 0.52, lookAng + 0.52);
        rCtx.closePath();
        rCtx.fillStyle = 'rgba(56,189,248,0.28)';
        rCtx.fill();
        rCtx.strokeStyle = 'rgba(56,189,248,0.85)';
        rCtx.lineWidth = 1.2;
        rCtx.stroke();

        rCtx.beginPath();
        rCtx.arc(tx, ty, 3.5, 0, Math.PI * 2);
        rCtx.fillStyle = '#f43f5e';
        rCtx.fill();
        rCtx.strokeStyle = '#ffffff';
        rCtx.lineWidth = 1.2;
        rCtx.stroke();
      }
    }
  }, [size, grid, cam, surfaceMode, zScale, showAura, points3D, nearbySpawners, teleports, startTeleport, selected, selectedSet, activeTrialIndex, pose, dragging, centerSample.z, image, map]);

  // 60fps WASD + Q/E + Shift free-flight camera controller
  const pressedKeysRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    let rafId = 0;
    let lastTime = performance.now();

    const stepLoop = (now: number) => {
      const dt = Math.min(0.06, (now - lastTime) / 1000);
      lastTime = now;
      const keys = pressedKeysRef.current;
      if (keys.size > 0) {
        let forward = 0;
        let right = 0;
        let zoomDir = 0;
        if (keys.has('w') || keys.has('arrowup')) forward += 1;
        if (keys.has('s') || keys.has('arrowdown')) forward -= 1;
        if (keys.has('d') || keys.has('arrowright')) right += 1;
        if (keys.has('a') || keys.has('arrowleft')) right -= 1;
        if (keys.has('q') || keys.has(' ')) zoomDir += 1;
        if (keys.has('e') || keys.has('c')) zoomDir -= 1;
        const boost = keys.has('shift') ? 2.6 : 1.0;

        if (forward !== 0 || right !== 0 || zoomDir !== 0) {
          setCam((prev) => {
            const speed = Math.max(95, prev.distance * 0.95) * boost * dt;
            const cosY = Math.cos(prev.yaw);
            const sinY = Math.sin(prev.yaw);
            // Forward vector in map coordinates is (-sinY, -cosY); Right vector is (cosY, -sinY)
            const dx = (-sinY * forward + cosY * right) * speed;
            const dy = (-cosY * forward - sinY * right) * speed;
            const maxW = map?.width ?? 16000;
            const maxH = map?.height ?? 16000;
            const nextDist = Math.max(70, Math.min(2200, prev.distance * (1 + zoomDir * 1.15 * dt)));
            return {
              ...prev,
              targetX: Math.max(0, Math.min(maxW, prev.targetX + dx)),
              targetY: Math.max(0, Math.min(maxH, prev.targetY + dy)),
              distance: nextDist,
            };
          });
        }
        rafId = requestAnimationFrame(stepLoop);
      } else {
        rafId = 0;
        setDragging(false);
      }
    };

    const onKeyDown = (ev: KeyboardEvent) => {
      const tag = (ev.target as HTMLElement)?.tagName;
      if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
      if (ev.ctrlKey || ev.metaKey) return;
      const k = ev.key.toLowerCase();
      if (['w', 'a', 's', 'd', 'q', 'e', 'c', ' ', 'shift', 'arrowup', 'arrowdown', 'arrowleft', 'arrowright'].includes(k)) {
        if (k === ' ' || k.startsWith('arrow')) ev.preventDefault();
        pressedKeysRef.current.add(k);
        setDragging(true);
        if (!rafId) {
          lastTime = performance.now();
          rafId = requestAnimationFrame(stepLoop);
        }
      }
    };

    const onKeyUp = (ev: KeyboardEvent) => {
      const k = ev.key.toLowerCase();
      pressedKeysRef.current.delete(k);
      if (pressedKeysRef.current.size === 0) setDragging(false);
    };

    const onBlur = () => {
      pressedKeysRef.current.clear();
      setDragging(false);
    };

    window.addEventListener('keydown', onKeyDown);
    window.addEventListener('keyup', onKeyUp);
    window.addEventListener('blur', onBlur);
    return () => {
      if (rafId) cancelAnimationFrame(rafId);
      window.removeEventListener('keydown', onKeyDown);
      window.removeEventListener('keyup', onKeyUp);
      window.removeEventListener('blur', onBlur);
    };
  }, [map?.width, map?.height]);

  const dragRef = useRef<{
    mode: 'orbit' | 'pan';
    startX: number;
    startY: number;
    moved: boolean;
    yaw: number;
    pitch: number;
    targetX: number;
    targetY: number;
  } | null>(null);

  const handleMouseDown = (ev: React.MouseEvent<HTMLCanvasElement>) => {
    const isPan = ev.button === 2 || ev.button === 1 || ev.altKey;
    dragRef.current = {
      mode: isPan ? 'pan' : 'orbit',
      startX: ev.clientX,
      startY: ev.clientY,
      moved: false,
      yaw: cam.yaw,
      pitch: cam.pitch,
      targetX: cam.targetX,
      targetY: cam.targetY,
    };
    setDragging(true);
  };

  const handleMouseMove = (ev: React.MouseEvent<HTMLCanvasElement>) => {
    const rect = ev.currentTarget.getBoundingClientRect();
    const mx = ev.clientX - rect.left;
    const my = ev.clientY - rect.top;

    if (dragRef.current) {
      const dx = ev.clientX - dragRef.current.startX;
      const dy = ev.clientY - dragRef.current.startY;
      if (Math.hypot(dx, dy) > 4) dragRef.current.moved = true;
      if (dragRef.current.mode === 'orbit') {
        const nextYaw = dragRef.current.yaw + dx * 0.0075;
        const nextPitch = Math.max(0.26, Math.min(1.52, dragRef.current.pitch + dy * 0.0065));
        setCam((prev) => ({ ...prev, yaw: nextYaw, pitch: nextPitch }));
      } else {
        const panScale = cam.distance * 0.0018;
        const cosY = Math.cos(cam.yaw);
        const sinY = Math.sin(cam.yaw);
        const wx = (-dx * cosY - dy * sinY) * panScale;
        const wy = (dx * sinY - dy * cosY) * panScale;
        setCam((prev) => ({
          ...prev,
          targetX: dragRef.current!.targetX + wx,
          targetY: dragRef.current!.targetY + wy,
        }));
      }
      return;
    }

    for (const wp of screenWaypointsRef.current) {
      if (Math.hypot(mx - wp.sx, my - wp.sy) <= 10) {
        const pt = points3D[wp.index];
        setHoverInfo({
          title: `第 ${wp.index + 1} 点 · ${actionMeta(pt?.action).label}`,
          subtitle: `坐标 (${pt?.x.toFixed(1)}, ${pt?.y.toFixed(1)}) · 海拔 ${wp.z.toFixed(1)}m · 坡度 ${wp.slope}°`,
          screenX: mx,
          screenY: my,
        });
        return;
      }
    }
    for (const sp of screenSpawnersRef.current) {
      if (Math.hypot(mx - sp.sx, my - sp.sy) <= 8) {
        setHoverInfo({
          title: `${sp.pt.name} (${KIND_NAME[sp.pt.kind] ?? sp.pt.kind})${sp.captured ? ' · [路线已覆盖]' : ''}`,
          subtitle: `海拔 ${sp.pt.z.toFixed(1)}m${sp.pt.cond ? ` · ${sp.pt.cond}` : ''}`,
          screenX: mx,
          screenY: my,
        });
        return;
      }
    }
    if (hoverInfo) setHoverInfo(null);
  };

  const handleMouseUp = (ev: React.MouseEvent<HTMLCanvasElement>) => {
    const st = dragRef.current;
    dragRef.current = null;
    setDragging(false);
    if (!st || st.moved || ev.button !== 0) return;
    const rect = ev.currentTarget.getBoundingClientRect();
    const mx = ev.clientX - rect.left;
    const my = ev.clientY - rect.top;
    for (const wp of screenWaypointsRef.current) {
      if (Math.hypot(mx - wp.sx, my - wp.sy) <= 12) {
        onSelect(wp.index, ev.shiftKey);
        return;
      }
    }
    onSelect(null);
  };

  const handleRadarJump = (ev: React.MouseEvent<HTMLCanvasElement>) => {
    if (!map) return;
    const rect = ev.currentTarget.getBoundingClientRect();
    const rx = (ev.clientX - rect.left) / rect.width;
    const ry = (ev.clientY - rect.top) / rect.height;
    setCam((prev) => ({
      ...prev,
      targetX: Math.max(0, Math.min(map.width, rx * map.width)),
      targetY: Math.max(0, Math.min(map.height, ry * map.height)),
    }));
  };

  const capturedCount = useMemo(() => nearbySpawners.filter((s) => s.captured).length, [nearbySpawners]);
  const pitchDeg = Math.round((cam.pitch * 180) / Math.PI);

  return (
    <div ref={containerRef} className="relative h-full w-full select-none overflow-hidden bg-[#0f172a]">
      <canvas
        ref={canvasRef}
        onMouseDown={handleMouseDown}
        onMouseMove={handleMouseMove}
        onMouseUp={handleMouseUp}
        onMouseLeave={() => {
          dragRef.current = null;
          if (pressedKeysRef.current.size === 0) setDragging(false);
          setHoverInfo(null);
        }}
        onContextMenu={(ev) => ev.preventDefault()}
        onWheel={(ev) => {
          ev.preventDefault();
          const factor = ev.deltaY < 0 ? 1.18 : 1 / 1.18;
          setCam((prev) => ({
            ...prev,
            distance: Math.max(70, Math.min(2200, prev.distance / factor)),
          }));
        }}
        className="block h-full w-full cursor-grab active:cursor-grabbing"
      />

      {/* Top-right 3D surface, camera angle presets & overlay controls */}
      <div className="absolute right-3 top-3 flex flex-wrap items-center gap-1.5 rounded-xl border border-white/15 bg-slate-900/90 px-2.5 py-1.5 text-xs text-slate-200 shadow-lg backdrop-blur">
        <button
          type="button"
          onClick={() => setSurfaceMode('map')}
          className={clsx('rounded-lg px-2 py-1 transition-colors', surfaceMode === 'map' ? 'bg-sky-500/25 font-medium text-sky-300' : 'hover:bg-white/10')}
        >
          <Layers className="mr-1 inline h-3.5 w-3.5" />彩色实景地图
        </button>
        <button
          type="button"
          onClick={() => setSurfaceMode('elevation')}
          className={clsx('rounded-lg px-2 py-1 transition-colors', surfaceMode === 'elevation' ? 'bg-emerald-500/25 font-medium text-emerald-300' : 'hover:bg-white/10')}
        >
          <Mountain className="mr-1 inline h-3.5 w-3.5" />海拔等高线
        </button>
        <button
          type="button"
          onClick={() => setSurfaceMode('walkability')}
          className={clsx('rounded-lg px-2 py-1 transition-colors', surfaceMode === 'walkability' ? 'bg-amber-500/25 font-medium text-amber-300' : 'hover:bg-white/10')}
        >
          <Compass className="mr-1 inline h-3.5 w-3.5" />可行走诊断
        </button>
        <span className="mx-0.5 h-3.5 w-px bg-white/15" />
        <button
          type="button"
          onClick={() => setZScale((v) => (v < 1.5 ? 2.0 : v < 2.5 ? 3.2 : 1.0))}
          title="切换 3D 地形垂直起伏倍率"
          className="rounded-lg px-2 py-1 text-slate-300 hover:bg-white/10"
        >
          高度 {zScale === 1.0 ? '1x' : zScale === 2.0 ? '2x' : '3x'}
        </button>
        <button
          type="button"
          onClick={() => setShowAura((v) => !v)}
          className={clsx('rounded-lg px-2 py-1 transition-colors', showAura ? 'bg-amber-400/20 text-amber-300' : 'text-slate-400 hover:bg-white/10')}
        >
          <Sparkles className="mr-1 inline h-3.5 w-3.5" />15m光环
        </button>
        <button
          type="button"
          onClick={() => setShowSpawners((v) => !v)}
          className={clsx('rounded-lg px-2 py-1 transition-colors', showSpawners ? 'bg-emerald-400/20 text-emerald-300' : 'text-slate-400 hover:bg-white/10')}
        >
          <Eye className="mr-1 inline h-3.5 w-3.5" />收集物 ({capturedCount})
        </button>
        <button
          type="button"
          onClick={() => setCam((prev) => ({ ...prev, yaw: 0.0, pitch: 0.82 }))}
          title="回正朝北 45° 鸟瞰视角"
          className="inline-flex items-center gap-1 rounded-lg px-2 py-1 text-slate-300 hover:bg-white/10"
        >
          <RotateCcw className="h-3.5 w-3.5" />正北鸟瞰
        </button>
      </div>

      {/* Bottom-right 2D Mini-Radar with live Camera Frustum Cone */}
      <div className="absolute bottom-3 right-3 flex flex-col items-end gap-1">
        <div className="overflow-hidden rounded-xl border border-white/20 bg-slate-950/90 p-1 shadow-xl backdrop-blur">
          <div className="mb-1 flex items-center justify-between px-1 text-[10px] text-slate-300">
            <span className="flex items-center gap-1 font-medium text-sky-300">
              <Navigation className="h-3 w-3" style={{ transform: `rotate(${(-cam.yaw * 180) / Math.PI}deg)` }} />
              相机雷达（点图瞬移）
            </span>
            <span>俯角 {pitchDeg}° · 海拔 {centerSample.z.toFixed(0)}m</span>
          </div>
          <canvas
            ref={radarRef}
            width={176}
            height={176}
            onMouseDown={(ev) => {
              if (ev.buttons === 1) handleRadarJump(ev);
            }}
            onMouseMove={(ev) => {
              if (ev.buttons === 1) handleRadarJump(ev);
            }}
            className="block h-44 w-44 cursor-crosshair rounded-lg border border-white/10"
          />
        </div>
      </div>

      {/* Bottom-left WASD & mouse controls legend */}
      <div className="pointer-events-none absolute bottom-3 left-3 rounded-xl border border-white/15 bg-slate-900/85 px-3 py-1.5 text-[11px] text-slate-200 shadow-lg">
        <kbd className="rounded bg-white/15 px-1 py-0.5 font-mono text-[10px]">WASD</kbd> 飞行漫游 ·{' '}
        <kbd className="rounded bg-white/15 px-1 py-0.5 font-mono text-[10px]">Shift</kbd> 加速 ·{' '}
        <kbd className="rounded bg-white/15 px-1 py-0.5 font-mono text-[10px]">Q/E</kbd> 升降视距 · 左键旋转 · 右键平移 · 滚轮缩放
      </div>

      {hoverInfo && (
        <div
          style={{ left: Math.min(size.width - 250, hoverInfo.screenX + 14), top: Math.max(12, hoverInfo.screenY - 38) }}
          className="pointer-events-none absolute z-20 rounded-lg border border-white/20 bg-slate-950/95 px-2.5 py-1.5 text-xs text-white shadow-xl"
        >
          <p className="font-semibold text-sky-300">{hoverInfo.title}</p>
          <p className="text-[11px] text-slate-300">{hoverInfo.subtitle}</p>
        </div>
      )}
    </div>
  );
});
