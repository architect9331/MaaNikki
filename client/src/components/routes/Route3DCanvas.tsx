import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from 'react';
import { Compass, Eye, Layers, Mountain, RotateCcw, Sparkles } from 'lucide-react';
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
  if (!grid) return { z: fallbackZ, slope: 0, obs: false, valid: false };
  const gx = Math.round(x * grid.pxScale);
  const gy = Math.round(y * grid.pxScale);
  if (gx < 0 || gx >= grid.width || gy < 0 || gy >= grid.height) {
    return { z: fallbackZ, slope: 0, obs: false, valid: false };
  }
  const idx = gy * grid.width + gx;
  if (!grid.validMask[idx] || Number.isNaN(grid.zMap[idx])) {
    return { z: fallbackZ, slope: 0, obs: false, valid: false };
  }
  return {
    z: grid.zMap[idx],
    slope: grid.slopeDeg[idx],
    obs: grid.obsMask[idx] === 1,
    valid: true,
  };
}

function elevationColor(t: number): [number, number, number] {
  const c = Math.max(0, Math.min(1, t));
  if (c < 0.25) {
    const u = c / 0.25;
    return [Math.round(32 + 28 * u), Math.round(92 + 78 * u), Math.round(145 - 15 * u)];
  }
  if (c < 0.55) {
    const u = (c - 0.25) / 0.3;
    return [Math.round(60 + 95 * u), Math.round(170 + 25 * u), Math.round(130 - 55 * u)];
  }
  if (c < 0.82) {
    const u = (c - 0.55) / 0.27;
    return [Math.round(155 + 65 * u), Math.round(195 - 50 * u), Math.round(75 + 15 * u)];
  }
  const u = (c - 0.82) / 0.18;
  return [Math.round(220 + 28 * u), Math.round(145 + 95 * u), Math.round(90 + 145 * u)];
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
    readOnly,
    targetMaterial,
    onSelect,
  },
  ref,
) {
  const containerRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [size, setSize] = useState({ width: 900, height: 640 });
  const [grid, setGrid] = useState<TerrainGridData | null>(null);
  const [spawners, setSpawners] = useState<SpawnerPoint3D[]>([]);
  const [surfaceMode, setSurfaceMode] = useState<'map' | 'elevation' | 'walkability'>('map');
  const [zScale, setZScale] = useState<number>(1.8);
  const [showAura, setShowAura] = useState(true);
  const [showSpawners, setShowSpawners] = useState(true);
  const [dragging, setDragging] = useState(false);

  const [cam, setCam] = useState({
    targetX: 2000,
    targetY: 2000,
    yaw: -0.58,
    pitch: 0.78,
    distance: 290,
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
      const span = Math.max(85, Math.hypot(maxX - minX, maxY - minY) * 1.15);
      setCam((prev) => ({
        ...prev,
        targetX: cx,
        targetY: cy,
        distance: Math.min(1800, Math.max(110, span)),
      }));
      return;
    }
    if (startTeleport) {
      setCam((prev) => ({ ...prev, targetX: startTeleport.x, targetY: startTeleport.y, distance: 260 }));
      return;
    }
    if (map) {
      setCam((prev) => ({ ...prev, targetX: map.width * 0.5, targetY: map.height * 0.5, distance: 650 }));
    }
  }, [points, preview, startTeleport, map]);

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
          distance: Math.max(70, Math.min(2400, prev.distance / factor)),
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
    let lastValidZ = 0;
    if (grid) {
      lastValidZ = (grid.zMin + grid.zMax) * 0.5;
    }
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
    const viewR = Math.max(260, cam.distance * 1.15);
    const auraPx = 14.5 / 0.9; // ~16.1 map px
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

    // Background sky / abyss gradient
    const bgGrad = ctx.createLinearGradient(0, 0, 0, size.height);
    bgGrad.addColorStop(0, '#0f172a');
    bgGrad.addColorStop(0.55, '#162036');
    bgGrad.addColorStop(1, '#0b1120');
    ctx.fillStyle = bgGrad;
    ctx.fillRect(0, 0, size.width, size.height);

    const centerZ = sampleTerrain(
      grid,
      cam.targetX,
      cam.targetY,
      points3D[0]?.z ?? (grid ? (grid.zMin + grid.zMax) * 0.5 : 0),
    ).z;

    const cosYaw = Math.cos(cam.yaw);
    const sinYaw = Math.sin(cam.yaw);
    const cosPitch = Math.cos(cam.pitch);
    const sinPitch = Math.sin(cam.pitch);
    const fovScale = Math.min(size.width, size.height) * 0.92;
    const mPerPx = 0.9;

    const project = (wx: number, wy: number, wz: number) => {
      const dx = wx - cam.targetX;
      const dy = wy - cam.targetY;
      const dz = ((wz - centerZ) / mPerPx) * zScale;
      const rx = dx * cosYaw - dy * sinYaw;
      const ry = dx * sinYaw + dy * cosYaw;
      const cy = ry * sinPitch - dz * cosPitch;
      const cz = ry * cosPitch + dz * sinPitch + cam.distance;
      if (cz <= 8) return null;
      const scale = fovScale / cz;
      return {
        sx: size.width * 0.5 + rx * scale,
        sy: size.height * 0.54 + cy * scale,
        depth: cz,
        scale,
      };
    };

    // Build and sort 3D terrain mesh quads around camera target
    const N = dragging ? 56 : 78;
    const patchRadius = Math.max(85, Math.min(1300, cam.distance * 0.92));
    const step = (patchRadius * 2) / N;
    const startX = cam.targetX - patchRadius;
    const startY = cam.targetY - patchRadius;
    const zSpan = Math.max(20, (grid?.zMax ?? 100) - (grid?.zMin ?? -100));
    const zMin = grid?.zMin ?? -100;
    const mapPx = mapPixelsRef.current;

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
      contour: boolean;
    }
    const quads: Quad[] = [];
    for (let iy = 0; iy < N; iy++) {
      for (let ix = 0; ix < N; ix++) {
        const v00 = verts[iy * (N + 1) + ix];
        const v10 = verts[iy * (N + 1) + ix + 1];
        const v11 = verts[(iy + 1) * (N + 1) + ix + 1];
        const v01 = verts[(iy + 1) * (N + 1) + ix];
        if (!v00 || !v10 || !v11 || !v01) continue;
        if (!v00.valid && !v10.valid && !v11.valid && !v01.valid) continue;

        // Directional sun hillshading from NW
        const dzx = (v10.z - v00.z) / (step * mPerPx);
        const dzy = (v01.z - v00.z) / (step * mPerPx);
        const shade = Math.max(0.42, Math.min(1.28, 0.94 + (-dzx * 0.45 - dzy * 0.35) * 0.48));
        const avgZ = (v00.z + v10.z + v11.z + v01.z) * 0.25;
        const maxSlp = Math.max(v00.slope, v10.slope, v11.slope, v01.slope);
        const hasObs = v00.obs || v10.obs || v11.obs || v01.obs;

        let r = 55;
        let g = 115;
        let b = 95;
        if (surfaceMode === 'map' && mapPx) {
          const mx = Math.max(0, Math.min(mapPx.w - 1, Math.round(v00.wx)));
          const my = Math.max(0, Math.min(mapPx.h - 1, Math.round(v00.wy)));
          const pi = (my * mapPx.w + mx) * 4;
          if (mapPx.data[pi + 3] > 10) {
            r = mapPx.data[pi];
            g = mapPx.data[pi + 1];
            b = mapPx.data[pi + 2];
          } else {
            [r, g, b] = elevationColor((avgZ - zMin) / zSpan);
          }
        } else if (surfaceMode === 'walkability') {
          if (hasObs) {
            [r, g, b] = [42, 118, 210];
          } else if (maxSlp > 42) {
            [r, g, b] = [225, 68, 72];
          } else if (maxSlp > 34) {
            [r, g, b] = [235, 168, 52];
          } else {
            [r, g, b] = [52, 185, 112];
          }
        } else {
          [r, g, b] = elevationColor((avgZ - zMin) / zSpan);
        }

        r = Math.min(255, Math.round(r * shade));
        g = Math.min(255, Math.round(g * shade));
        b = Math.min(255, Math.round(b * shade));
        const contour = Math.floor(v00.z / 15) !== Math.floor(v11.z / 15);
        quads.push({
          depth: (v00.depth + v10.depth + v11.depth + v01.depth) * 0.25,
          x0: v00.sx, y0: v00.sy,
          x1: v10.sx, y1: v10.sy,
          x2: v11.sx, y2: v11.sy,
          x3: v01.sx, y3: v01.sy,
          fill: `rgb(${r},${g},${b})`,
          contour,
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
      if (q.contour) {
        ctx.strokeStyle = 'rgba(255,255,255,0.12)';
        ctx.lineWidth = 0.6;
        ctx.stroke();
      }
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
          ctx.fillStyle = i === selected ? 'rgba(56,189,248,0.18)' : isWait ? 'rgba(250,204,21,0.10)' : 'rgba(74,222,128,0.05)';
          ctx.fill();
          ctx.strokeStyle = i === selected ? 'rgba(56,189,248,0.85)' : isWait ? 'rgba(250,204,21,0.55)' : 'rgba(74,222,128,0.28)';
          ctx.lineWidth = i === selected ? 1.8 : 1.1;
          ctx.stroke();
        }
      }
    }

    // Draw 3D material spawners
    const drawnSpawners: Array<{ pt: SpawnerPoint3D; sx: number; sy: number; captured: boolean }> = [];
    for (const { pt, captured, isTarget } of nearbySpawners) {
      const pGround = project(pt.x, pt.y, pt.z);
      const pPin = project(pt.x, pt.y, pt.z + (captured ? 3.2 : 1.6));
      if (!pGround || !pPin) continue;
      const color = KIND_COLOR[pt.kind] ?? '#a78bfa';
      if (captured || isTarget) {
        ctx.beginPath();
        ctx.moveTo(pGround.sx, pGround.sy);
        ctx.lineTo(pPin.sx, pPin.sy);
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.2;
        ctx.stroke();
      }
      const rad = captured ? (isTarget ? 5.2 : 4.0) : 2.3;
      ctx.beginPath();
      ctx.arc(pPin.sx, pPin.sy, rad, 0, Math.PI * 2);
      ctx.fillStyle = captured ? color : 'rgba(203,213,225,0.45)';
      ctx.fill();
      if (captured) {
        ctx.strokeStyle = isTarget ? '#ffffff' : '#0f172a';
        ctx.lineWidth = isTarget ? 1.6 : 1.0;
        ctx.stroke();
      }
      drawnSpawners.push({ pt, sx: pPin.sx, sy: pPin.sy, captured });
    }
    screenSpawnersRef.current = drawnSpawners;

    // Draw nearby Warp Spire teleports as tall violet beacons
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
        ctx.strokeStyle = isStart ? '#a855f7' : 'rgba(168,85,247,0.65)';
        ctx.lineWidth = isStart ? 2.6 : 1.6;
        ctx.stroke();
        ctx.beginPath();
        ctx.arc(pTop.sx, pTop.sy, isStart ? 5.5 : 4.0, 0, Math.PI * 2);
        ctx.fillStyle = isStart ? '#c084fc' : '#9333ea';
        ctx.fill();
        ctx.strokeStyle = '#ffffff';
        ctx.lineWidth = 1.2;
        ctx.stroke();
      }
    }

    // Draw 3D route trajectory and waypoint pillars
    const drawnWaypoints: Array<{ index: number; sx: number; sy: number; depth: number; z: number; slope: number }> = [];
    if (points3D.length > 0) {
      // Ground shadow polyline
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
        ctx.strokeStyle = 'rgba(15,23,42,0.65)';
        ctx.lineWidth = 4.5;
        ctx.stroke();
      }

      // Elevated glowing trajectory ribbon
      ctx.beginPath();
      started = false;
      for (const wp of points3D) {
        const p = project(wp.x, wp.y, wp.z + 1.8);
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
        ctx.lineWidth = 2.8;
        ctx.stroke();
      }

      // Waypoint pillars and nodes
      for (const wp of points3D) {
        const pBase = project(wp.x, wp.y, wp.z);
        const pTop = project(wp.x, wp.y, wp.z + (wp.jump ? 5.2 : wp.action ? 4.2 : 2.2));
        if (!pBase || !pTop) continue;
        const isSel = selected === wp.index || Boolean(selectedSet?.has(wp.index));
        const isTrial = activeTrialIndex === wp.index;
        const meta = actionMeta(wp.action);

        ctx.beginPath();
        ctx.moveTo(pBase.sx, pBase.sy);
        ctx.lineTo(pTop.sx, pTop.sy);
        ctx.strokeStyle = isSel ? '#f43f5e' : wp.jump ? '#f59e0b' : meta.color || '#38bdf8';
        ctx.lineWidth = isSel ? 2.4 : 1.5;
        ctx.stroke();

        const r = isSel || isTrial ? 7.5 : wp.action || wp.jump ? 6.0 : 4.5;
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
        ctx.lineWidth = isSel ? 2.0 : 1.3;
        ctx.stroke();

        if (isSel || wp.index === 0 || wp.index === points3D.length - 1 || wp.action) {
          ctx.font = '600 10px system-ui, sans-serif';
          ctx.fillStyle = '#ffffff';
          ctx.textAlign = 'center';
          ctx.fillText(`${wp.index + 1}`, pTop.sx, pTop.sy - r - 4);
        }
        drawnWaypoints.push({ index: wp.index, sx: pTop.sx, sy: pTop.sy, depth: pTop.depth, z: wp.z, slope: wp.slope });
      }
    }
    screenWaypointsRef.current = drawnWaypoints;

    // Live player pose marker
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
  }, [size, grid, cam, surfaceMode, zScale, showAura, points3D, nearbySpawners, teleports, startTeleport, selected, selectedSet, activeTrialIndex, pose, dragging]);

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
        const nextYaw = dragRef.current.yaw - dx * 0.0075;
        const nextPitch = Math.max(0.22, Math.min(1.48, dragRef.current.pitch + dy * 0.0065));
        setCam((prev) => ({ ...prev, yaw: nextYaw, pitch: nextPitch }));
      } else {
        const panScale = cam.distance * 0.0018;
        const cosY = Math.cos(cam.yaw);
        const sinY = Math.sin(cam.yaw);
        const wx = -(dx * cosY + dy * sinY) * panScale;
        const wy = -(-dx * sinY + dy * cosY) * panScale;
        setCam((prev) => ({
          ...prev,
          targetX: dragRef.current!.targetX + wx,
          targetY: dragRef.current!.targetY + wy,
        }));
      }
      return;
    }

    // Hover hit-test on waypoints and spawners
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

  const capturedCount = useMemo(() => nearbySpawners.filter((s) => s.captured).length, [nearbySpawners]);

  return (
    <div ref={containerRef} className="relative h-full w-full select-none overflow-hidden bg-[#0f172a]">
      <canvas
        ref={canvasRef}
        onMouseDown={handleMouseDown}
        onMouseMove={handleMouseMove}
        onMouseUp={handleMouseUp}
        onMouseLeave={() => {
          dragRef.current = null;
          setDragging(false);
          setHoverInfo(null);
        }}
        onContextMenu={(ev) => ev.preventDefault()}
        onWheel={(ev) => {
          ev.preventDefault();
          const factor = ev.deltaY < 0 ? 1.16 : 1 / 1.16;
          setCam((prev) => ({
            ...prev,
            distance: Math.max(70, Math.min(2400, prev.distance / factor)),
          }));
        }}
        className="block h-full w-full cursor-grab active:cursor-grabbing"
      />

      {/* Top-right 3D surface & overlay controls */}
      <div className="absolute right-3 top-3 flex flex-wrap items-center gap-1.5 rounded-xl border border-white/15 bg-slate-900/90 px-2.5 py-1.5 text-xs text-slate-200 shadow-lg backdrop-blur">
        <button
          type="button"
          onClick={() => setSurfaceMode('map')}
          className={clsx('rounded-lg px-2 py-1 transition-colors', surfaceMode === 'map' ? 'bg-sky-500/25 font-medium text-sky-300' : 'hover:bg-white/10')}
        >
          <Layers className="mr-1 inline h-3.5 w-3.5" />实景贴图
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
          onClick={() => setCam((prev) => ({ ...prev, yaw: -0.58, pitch: 0.78 }))}
          title="重置 3D 视角朝向"
          className="rounded-lg p-1 text-slate-300 hover:bg-white/10"
        >
          <RotateCcw className="h-3.5 w-3.5" />
        </button>
      </div>

      {/* Bottom-left 3D manipulation hint */}
      <div className="pointer-events-none absolute bottom-3 left-3 rounded-lg border border-white/10 bg-slate-900/80 px-2.5 py-1 text-[11px] text-slate-300">
        左键拖动旋转 3D 视角 · 右键拖动平移地面 · 滚轮缩放 · 点击路线柱选点
        {!readOnly ? '' : ''}
      </div>

      {hoverInfo && (
        <div
          style={{ left: Math.min(size.width - 240, hoverInfo.screenX + 14), top: Math.max(12, hoverInfo.screenY - 38) }}
          className="pointer-events-none absolute z-20 rounded-lg border border-white/20 bg-slate-950/95 px-2.5 py-1.5 text-xs text-white shadow-xl"
        >
          <p className="font-semibold text-sky-300">{hoverInfo.title}</p>
          <p className="text-[11px] text-slate-300">{hoverInfo.subtitle}</p>
        </div>
      )}
    </div>
  );
});
