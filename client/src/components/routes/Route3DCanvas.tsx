import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import type { Checkpoint, RoutePoint, TrialWaypointReport } from '@/services/routeDocument';
import { readLocalFile } from '@/services/routeStudioService';
import type { MapCanvasHandle, MapInfo } from './RouteMapCanvas';

export interface StructureBox3D {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
  cx: number;
  cy: number;
  zBase: number;
  zTop: number;
  kind: number; // 1=roof/house, 2=bridge/dock, 3=platform/stair, 4=bldg/wall/ruin, 5=rock_mesa, 6=giant_tree
}

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
  structures: StructureBox3D[];
  colorImage: HTMLImageElement | null;
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

    const [b64, colorImg, structures] = await Promise.all([
      invoke<string>('read_local_file_base64', { filename: `resource/navigation/${info.image}` }),
      info.color_image
        ? invoke<string>('read_local_file_base64', { filename: `resource/navigation/${info.color_image}` })
            .then(
              (cb64) =>
                new Promise<HTMLImageElement | null>((resolve) => {
                  const el = new Image();
                  el.onload = () => resolve(el);
                  el.onerror = () => resolve(null);
                  el.src = `data:image/webp;base64,${cb64}`;
                }),
            )
            .catch(() => null)
        : Promise.resolve(null),
      info.structures
        ? readLocalFile(`resource/navigation/${info.structures}`)
            .then((sraw) => {
              const rows = JSON.parse(sraw.replace(/^\uFEFF/, '')) as number[][];
              return rows.map((r) => ({
                x0: Number(r[0]),
                y0: Number(r[1]),
                x1: Number(r[2]),
                y1: Number(r[3]),
                cx: (Number(r[0]) + Number(r[2])) * 0.5,
                cy: (Number(r[1]) + Number(r[3])) * 0.5,
                zBase: Number(r[4]),
                zTop: Number(r[5]),
                kind: Number(r[6] ?? 4),
              }));
            })
            .catch(() => [] as StructureBox3D[])
        : Promise.resolve([] as StructureBox3D[]),
    ]);

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
    const zOffset = Number(info.z_offset ?? -500.0);
    const zScaleEnc = Number(info.z_scale ?? 10.0);
    for (let i = 0; i < total; i++) {
      const r = rgba[i * 4];
      const g = rgba[i * 4 + 1];
      const b = rgba[i * 4 + 2];
      const hu16 = (r << 8) | g;
      const valid = b & 1;
      validMask[i] = valid;
      obsMask[i] = (b >> 1) & 1;
      slopeDeg[i] = (b >> 2) & 0x3f;
      zMap[i] = valid && hu16 > 0 ? hu16 / zScaleEnc + zOffset : NaN;
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
      structures,
      colorImage: colorImg,
    };
  })();
  terrainCache.set(mapId, promise);
  return promise;
}

function sampleTerrain(grid: TerrainGridData | null, x: number, y: number, fallbackZ = 0) {
  const safeFallback = Number.isFinite(fallbackZ) ? fallbackZ : 0;
  if (!grid) return { z: safeFallback, slope: 0, obs: false, valid: false };
  const gx = x * grid.pxScale;
  const gy = y * grid.pxScale;
  const x0 = Math.floor(gx);
  const y0 = Math.floor(gy);
  if (x0 < 0 || x0 >= grid.width || y0 < 0 || y0 >= grid.height) {
    return { z: safeFallback, slope: 0, obs: false, valid: false };
  }
  const x1 = Math.min(grid.width - 1, x0 + 1);
  const y1 = Math.min(grid.height - 1, y0 + 1);
  const fx = gx - x0;
  const fy = gy - y0;

  const i00 = y0 * grid.width + x0;
  const i10 = y0 * grid.width + x1;
  const i01 = y1 * grid.width + x0;
  const i11 = y1 * grid.width + x1;

  const v00 = grid.validMask[i00] ? grid.zMap[i00] : NaN;
  const v10 = grid.validMask[i10] ? grid.zMap[i10] : NaN;
  const v01 = grid.validMask[i01] ? grid.zMap[i01] : NaN;
  const v11 = grid.validMask[i11] ? grid.zMap[i11] : NaN;

  const nearIdx = Math.round(gy) * grid.width + Math.round(gx);
  const nearSafe = Math.max(0, Math.min(grid.width * grid.height - 1, nearIdx));

  if (Number.isFinite(v00) && Number.isFinite(v10) && Number.isFinite(v01) && Number.isFinite(v11)) {
    const zInterp =
      v00 * (1 - fx) * (1 - fy) +
      v10 * fx * (1 - fy) +
      v01 * (1 - fx) * fy +
      v11 * fx * fy;
    const sInterp =
      grid.slopeDeg[i00] * (1 - fx) * (1 - fy) +
      grid.slopeDeg[i10] * fx * (1 - fy) +
      grid.slopeDeg[i01] * (1 - fx) * fy +
      grid.slopeDeg[i11] * fx * fy;
    return {
      z: zInterp,
      slope: sInterp,
      obs: grid.obsMask[nearSafe] === 1,
      valid: true,
    };
  }

  const val = grid.zMap[nearSafe];
  if (!grid.validMask[nearSafe] || !Number.isFinite(val)) {
    return { z: safeFallback, slope: 0, obs: false, valid: false };
  }
  return {
    z: val,
    slope: grid.slopeDeg[nearSafe],
    obs: grid.obsMask[nearSafe] === 1,
    valid: true,
  };
}

function elevationColor(t: number): [number, number, number] {
  const c = Math.max(0, Math.min(1, Number.isFinite(t) ? t : 0.35));
  if (c < 0.2) {
    const u = c / 0.2;
    return [0.15 + 0.12 * u, 0.45 + 0.25 * u, 0.66 - 0.08 * u];
  }
  if (c < 0.52) {
    const u = (c - 0.2) / 0.32;
    return [0.27 + 0.35 * u, 0.70 + 0.08 * u, 0.58 - 0.25 * u];
  }
  if (c < 0.8) {
    const u = (c - 0.52) / 0.28;
    return [0.62 + 0.26 * u, 0.78 - 0.18 * u, 0.33 + 0.05 * u];
  }
  const u = (c - 0.8) / 0.2;
  return [0.88 + 0.10 * u, 0.60 + 0.35 * u, 0.38 + 0.52 * u];
}

function structureColors(
  kind: number,
  surfaceMode: 'map' | 'elevation' | 'walkability',
  normZ: number,
): { wall: [number, number, number]; roof: [number, number, number] } {
  if (surfaceMode === 'elevation') {
    const [er, eg, eb] = elevationColor(normZ);
    return { wall: [er * 0.82, eg * 0.82, eb * 0.82], roof: [er, eg, eb] };
  }
  if (surfaceMode === 'walkability') {
    if (kind === 2 || kind === 3) {
      return { wall: [0.16, 0.68, 0.48], roof: [0.22, 0.86, 0.58] };
    }
    if (kind === 1 || kind === 5) {
      return { wall: [0.76, 0.58, 0.22], roof: [0.94, 0.74, 0.28] };
    }
    return { wall: [0.78, 0.34, 0.24], roof: [0.92, 0.44, 0.30] };
  }
  switch (kind) {
    case 1: // building_roof
      return { wall: [0.88, 0.83, 0.75], roof: [0.76, 0.42, 0.34] };
    case 2: // bridge_dock
      return { wall: [0.62, 0.50, 0.38], roof: [0.76, 0.63, 0.46] };
    case 3: // platform_stair
      return { wall: [0.74, 0.73, 0.70], roof: [0.84, 0.83, 0.80] };
    case 5: // rock_mesa
      return { wall: [0.54, 0.51, 0.47], roof: [0.46, 0.62, 0.44] };
    case 6: // giant_tree
      return { wall: [0.48, 0.36, 0.28], roof: [0.30, 0.58, 0.36] };
    default: // bldg_structure (walls, arches, columns, ruins)
      return { wall: [0.80, 0.77, 0.71], roof: [0.72, 0.70, 0.65] };
  }
}

const MESH_N = 240;
const MAX_STRUCTS = 3200;

interface GLTerrainState {
  gl: WebGLRenderingContext;
  program: WebGLProgram;
  vbo: WebGLBuffer;
  ibo: WebGLBuffer;
  indexCount: number;
  vertsData: Float32Array;
  structVbo: WebGLBuffer;
  structIbo: WebGLBuffer;
  structVertsData: Float32Array;
  mapTex: WebGLTexture;
  hasMapTex: boolean;
  uScaleX: WebGLUniformLocation | null;
  uScaleY: WebGLUniformLocation | null;
  uMode: WebGLUniformLocation | null;
  bindVertLayout: () => void;
}

function initGLTerrain(canvas: HTMLCanvasElement): GLTerrainState | null {
  const gl = canvas.getContext('webgl', { antialias: true, alpha: false, depth: true });
  if (!gl) return null;

  const vsSource = `
    attribute vec3 a_pos;
    attribute vec2 a_uv;
    attribute vec3 a_tint;
    attribute float a_shade;
    uniform float u_scaleX;
    uniform float u_scaleY;
    varying vec2 v_uv;
    varying vec3 v_tint;
    varying float v_shade;
    void main() {
      v_uv = a_uv;
      v_tint = a_tint;
      v_shade = a_shade;
      float cz = max(6.0, a_pos.z);
      float near = 6.0;
      float far = 5500.0;
      float clipZ = cz * ((far + near) / (far - near)) - (2.0 * far * near) / (far - near);
      gl_Position = vec4(a_pos.x * u_scaleX, a_pos.y * u_scaleY, clipZ, cz);
    }
  `;

  const fsSource = `
    precision mediump float;
    varying vec2 v_uv;
    varying vec3 v_tint;
    varying float v_shade;
    uniform sampler2D u_mapTex;
    uniform float u_mode;
    void main() {
      if (v_uv.x < 0.0) {
        gl_FragColor = vec4(clamp(v_tint * v_shade, 0.0, 1.0), 1.0);
        return;
      }
      bool isRoof = v_uv.y >= 1.5;
      vec2 uv = vec2(v_uv.x, isRoof ? (v_uv.y - 2.0) : v_uv.y);
      vec4 tex = texture2D(u_mapTex, uv);
      bool hasColor = (tex.r + tex.g + tex.b) > 0.035 && tex.a > 0.08;
      vec3 base = hasColor ? tex.rgb : v_tint;
      if (u_mode > 0.5) {
        base = mix(base, v_tint, 0.68);
      } else if (isRoof) {
        base = mix(base, v_tint, 0.38);
      }
      gl_FragColor = vec4(clamp(base * v_shade, 0.0, 1.0), 1.0);
    }
  `;

  const compile = (type: number, src: string) => {
    const sh = gl.createShader(type)!;
    gl.shaderSource(sh, src);
    gl.compileShader(sh);
    return sh;
  };
  const vs = compile(gl.VERTEX_SHADER, vsSource);
  const fs = compile(gl.FRAGMENT_SHADER, fsSource);
  const program = gl.createProgram()!;
  gl.attachShader(program, vs);
  gl.attachShader(program, fs);
  gl.linkProgram(program);
  gl.useProgram(program);

  const numVerts = (MESH_N + 1) * (MESH_N + 1);
  const vertsData = new Float32Array(numVerts * 9);
  const vbo = gl.createBuffer()!;
  gl.bindBuffer(gl.ARRAY_BUFFER, vbo);
  gl.bufferData(gl.ARRAY_BUFFER, vertsData.byteLength, gl.DYNAMIC_DRAW);

  const indices = new Uint16Array(MESH_N * MESH_N * 6);
  let ptr = 0;
  for (let iy = 0; iy < MESH_N; iy++) {
    for (let ix = 0; ix < MESH_N; ix++) {
      const i00 = iy * (MESH_N + 1) + ix;
      const i10 = i00 + 1;
      const i01 = i00 + (MESH_N + 1);
      const i11 = i01 + 1;
      indices[ptr++] = i00;
      indices[ptr++] = i10;
      indices[ptr++] = i11;
      indices[ptr++] = i00;
      indices[ptr++] = i11;
      indices[ptr++] = i01;
    }
  }
  const ibo = gl.createBuffer()!;
  gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, ibo);
  gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, indices, gl.STATIC_DRAW);

  // 20 vertices (5 faces * 4 corners) and 30 indices (5 faces * 2 triangles) per 3D structure prism
  const structVertsData = new Float32Array(MAX_STRUCTS * 20 * 9);
  const structVbo = gl.createBuffer()!;
  gl.bindBuffer(gl.ARRAY_BUFFER, structVbo);
  gl.bufferData(gl.ARRAY_BUFFER, structVertsData.byteLength, gl.DYNAMIC_DRAW);

  const structIndices = new Uint16Array(MAX_STRUCTS * 30);
  let sPtr = 0;
  for (let i = 0; i < MAX_STRUCTS; i++) {
    const base = i * 20;
    for (let f = 0; f < 5; f++) {
      const fb = base + f * 4;
      structIndices[sPtr++] = fb;
      structIndices[sPtr++] = fb + 1;
      structIndices[sPtr++] = fb + 2;
      structIndices[sPtr++] = fb;
      structIndices[sPtr++] = fb + 2;
      structIndices[sPtr++] = fb + 3;
    }
  }
  const structIbo = gl.createBuffer()!;
  gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, structIbo);
  gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, structIndices, gl.STATIC_DRAW);

  const stride = 9 * 4;
  const locPos = gl.getAttribLocation(program, 'a_pos');
  const locUv = gl.getAttribLocation(program, 'a_uv');
  const locTint = gl.getAttribLocation(program, 'a_tint');
  const locShade = gl.getAttribLocation(program, 'a_shade');
  const bindVertLayout = () => {
    if (locPos >= 0) {
      gl.enableVertexAttribArray(locPos);
      gl.vertexAttribPointer(locPos, 3, gl.FLOAT, false, stride, 0);
    }
    if (locUv >= 0) {
      gl.enableVertexAttribArray(locUv);
      gl.vertexAttribPointer(locUv, 2, gl.FLOAT, false, stride, 3 * 4);
    }
    if (locTint >= 0) {
      gl.enableVertexAttribArray(locTint);
      gl.vertexAttribPointer(locTint, 3, gl.FLOAT, false, stride, 5 * 4);
    }
    if (locShade >= 0) {
      gl.enableVertexAttribArray(locShade);
      gl.vertexAttribPointer(locShade, 1, gl.FLOAT, false, stride, 8 * 4);
    }
  };
  gl.bindBuffer(gl.ARRAY_BUFFER, vbo);
  bindVertLayout();

  const mapTex = gl.createTexture()!;
  gl.bindTexture(gl.TEXTURE_2D, mapTex);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  const aniso =
    gl.getExtension('EXT_texture_filter_anisotropic') ||
    gl.getExtension('WEBKIT_EXT_texture_filter_anisotropic');
  if (aniso) {
    const maxAniso = gl.getParameter(aniso.MAX_TEXTURE_MAX_ANISOTROPY_EXT) || 4;
    gl.texParameterf(gl.TEXTURE_2D, aniso.TEXTURE_MAX_ANISOTROPY_EXT, Math.min(8, maxAniso));
  }

  return {
    gl,
    program,
    vbo,
    ibo,
    indexCount: indices.length,
    vertsData,
    structVbo,
    structIbo,
    structVertsData,
    mapTex,
    hasMapTex: false,
    uScaleX: gl.getUniformLocation(program, 'u_scaleX'),
    uScaleY: gl.getUniformLocation(program, 'u_scaleY'),
    uMode: gl.getUniformLocation(program, 'u_mode'),
    bindVertLayout,
  };
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
  const glCanvasRef = useRef<HTMLCanvasElement>(null);
  const overlayCanvasRef = useRef<HTMLCanvasElement>(null);
  const radarRef = useRef<HTMLCanvasElement>(null);
  const glStateRef = useRef<GLTerrainState | null>(null);

  const [size, setSize] = useState({ width: 900, height: 640 });
  const [grid, setGrid] = useState<TerrainGridData | null>(null);
  const [spawners, setSpawners] = useState<SpawnerPoint3D[]>([]);
  const [surfaceMode, setSurfaceMode] = useState<'map' | 'elevation' | 'walkability'>('map');
  const [zScale, setZScale] = useState<number>(1.0);
  const [showBuildings, setShowBuildings] = useState(true);
  const [showAura, setShowAura] = useState(true);
  const [showSpawners, setShowSpawners] = useState(true);
  const [pointerLocked, setPointerLocked] = useState(false);
  const pointerLockedRef = useRef(false);
  const lastDragRef = useRef<{ x: number; y: number } | null>(null);
  const [hudStat, setHudStat] = useState({ pitchDeg: 42, elevM: -190, yawDeg: 0 });

  const camRef = useRef({
    camX: 8960,
    camY: 8140,
    camBaseZ: null as number | null,
    heightPx: 175,
    yaw: 0.0,
    pitch: 0.72,
  });
  const rafDrawRef = useRef<number>(0);
  const drawFnRef = useRef<() => void>(() => {});

  const [hoverInfo, setHoverInfo] = useState<{
    title: string;
    subtitle: string;
    screenX: number;
    screenY: number;
  } | null>(null);

  const screenWaypointsRef = useRef<Array<{ index: number; sx: number; sy: number; depth: number; z: number; slope: number }>>([]);
  const screenSpawnersRef = useRef<Array<{ pt: SpawnerPoint3D; sx: number; sy: number; captured: boolean }>>([]);
  const radarBgRef = useRef<HTMLCanvasElement | null>(null);

  const scheduleDraw = useCallback(() => {
    if (rafDrawRef.current) return;
    rafDrawRef.current = requestAnimationFrame(() => {
      rafDrawRef.current = 0;
      drawFnRef.current();
    });
  }, []);

  useEffect(() => {
    if (glCanvasRef.current && !glStateRef.current) {
      glStateRef.current = initGLTerrain(glCanvasRef.current);
    }
  }, []);

  const setCursorLock = useCallback((active: boolean) => {
    pointerLockedRef.current = active;
    setPointerLocked(active);
    if (active) setHoverInfo(null);
    void invoke<[number, number]>('poll_cursor_lock_delta', { active }).catch(() => {});
  }, []);

  const togglePointerLock = useCallback(() => {
    setCursorLock(!pointerLockedRef.current);
  }, [setCursorLock]);

  // Native Win32 cursor-lock polling loop (eliminates WebView2 high-DPI movementX spike & cursor escape)
  useEffect(() => {
    if (!pointerLocked) {
      void invoke<[number, number]>('poll_cursor_lock_delta', { active: false }).catch(() => {});
      return;
    }
    let cancelled = false;
    let rafId = 0;
    let inFlight = false;

    const pollTick = () => {
      if (cancelled || !pointerLockedRef.current) return;
      if (!inFlight) {
        inFlight = true;
        invoke<[number, number]>('poll_cursor_lock_delta', { active: true })
          .then(([rawDx, rawDy]) => {
            inFlight = false;
            if (cancelled || !pointerLockedRef.current) return;
            const dx = Math.max(-85, Math.min(85, rawDx));
            const dy = Math.max(-85, Math.min(85, rawDy));
            if (dx !== 0 || dy !== 0) {
              camRef.current.yaw += dx * 0.0024;
              camRef.current.pitch = Math.max(0.14, Math.min(1.52, camRef.current.pitch + dy * 0.002));
              drawFnRef.current();
            }
          })
          .catch(() => {
            inFlight = false;
          });
      }
      rafId = requestAnimationFrame(pollTick);
    };

    rafId = requestAnimationFrame(pollTick);
    return () => {
      cancelled = true;
      if (rafId) cancelAnimationFrame(rafId);
      void invoke<[number, number]>('poll_cursor_lock_delta', { active: false }).catch(() => {});
    };
  }, [pointerLocked]);

  // Upload full-color official 2D map image (from terrain/<map_id>_color.webp, or fallback image) to WebGL + radar
  useEffect(() => {
    const glSt = glStateRef.current;
    const texSource = grid?.colorImage ?? image;
    if (!texSource) {
      if (glSt) glSt.hasMapTex = false;
      radarBgRef.current = null;
      return;
    }
    if (glSt) {
      const { gl, mapTex } = glSt;
      gl.bindTexture(gl.TEXTURE_2D, mapTex);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, texSource);
      glSt.hasMapTex = true;
    }
    const thumb = document.createElement('canvas');
    thumb.width = 176;
    thumb.height = 176;
    const tCtx = thumb.getContext('2d')!;
    tCtx.fillStyle = '#0f172a';
    tCtx.fillRect(0, 0, 176, 176);
    tCtx.drawImage(texSource, 0, 0, 176, 176);
    radarBgRef.current = thumb;
    scheduleDraw();
  }, [grid?.colorImage, image, scheduleDraw]);

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

  const lookAtPoint = useCallback(
    (tx: number, ty: number, dist?: number) => {
      const c = camRef.current;
      const d = dist ?? Math.max(120, c.heightPx / Math.sin(Math.max(0.28, c.pitch)));
      const pitch = Math.max(0.35, c.pitch);
      const horizBack = d * Math.cos(pitch);
      const fwdX = Math.sin(c.yaw);
      const fwdY = -Math.cos(c.yaw);
      c.camX = tx - fwdX * horizBack;
      c.camY = ty - fwdY * horizBack;
      c.heightPx = Math.max(45, Math.min(1400, d * Math.sin(pitch)));
      c.pitch = pitch;
      const sTarget = sampleTerrain(grid, tx, ty, NaN);
      c.camBaseZ = sTarget.valid && Number.isFinite(sTarget.z) ? sTarget.z : null;
      scheduleDraw();
    },
    [grid, scheduleDraw],
  );

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
      const dist = Math.min(1100, Math.max(150, Math.hypot(maxX - minX, maxY - minY) * 1.25));
      camRef.current.yaw = 0.0;
      camRef.current.pitch = 0.72;
      lookAtPoint(cx, cy, dist);
      return;
    }
    if (startTeleport) {
      lookAtPoint(startTeleport.x, startTeleport.y, 260);
      return;
    }
    if (teleports && teleports.length > 0) {
      const florawish = teleports.find((t) => t.region?.includes('花愿镇')) ?? teleports[0];
      lookAtPoint(florawish.x, florawish.y, 340);
      return;
    }
    if (map) {
      lookAtPoint(map.id === 'miraland' ? 8960 : map.width * 0.5, map.id === 'miraland' ? 7940 : map.height * 0.5, 420);
    }
  }, [points, preview, startTeleport, teleports, map, lookAtPoint]);

  useEffect(() => {
    fit();
  }, [map?.id, points.length === 0]);

  useImperativeHandle(
    ref,
    () => ({
      fit,
      zoom: (factor: number) => {
        camRef.current.heightPx = Math.max(35, Math.min(1500, camRef.current.heightPx / factor));
        scheduleDraw();
      },
      focus: (x: number, y: number) => {
        lookAtPoint(x, y, 210);
      },
    }),
    [fit, lookAtPoint, scheduleDraw],
  );

  const points3D = useMemo(() => {
    const activePts = preview?.length ? preview : points;
    let lastValidZ = grid ? (grid.zMin + grid.zMax) * 0.35 : -190;
    return activePts.map((pt, index) => {
      const s = sampleTerrain(grid, pt.x, pt.y, lastValidZ);
      if (s.valid) lastValidZ = s.z;
      return { ...pt, index, z: s.z, slope: s.slope, obs: s.obs };
    });
  }, [points, preview, grid]);

  const annotatedSpawners = useMemo(() => {
    if (!spawners.length) return { list: [], capturedCount: 0 };
    const auraPx = 14.5 / 0.9;
    const tgtQuery = (targetMaterial ?? '').trim();
    let capturedCount = 0;
    const list = spawners.map((sp) => {
      let captured = false;
      for (const wp of points3D) {
        if (Math.abs(sp.x - wp.x) <= auraPx && Math.abs(sp.y - wp.y) <= auraPx) {
          if (Math.hypot(sp.x - wp.x, sp.y - wp.y) <= auraPx && Math.abs(sp.z - wp.z) <= 6.2) {
            captured = true;
            break;
          }
        }
      }
      if (captured) capturedCount++;
      const isTarget = Boolean(
        tgtQuery && (sp.name.includes(tgtQuery) || (sp.cond && sp.cond.includes(tgtQuery))),
      );
      return { pt: sp, captured, isTarget };
    });
    return { list, capturedCount };
  }, [spawners, points3D, targetMaterial]);

  useEffect(() => {
    drawFnRef.current = () => {
      const overlayCvs = overlayCanvasRef.current;
      const glCvs = glCanvasRef.current;
      const glSt = glStateRef.current;
      if (!overlayCvs || !glCvs) return;
      const ctx = overlayCvs.getContext('2d');
      if (!ctx) return;

      const dpr = window.devicePixelRatio || 1;
      const targetW = Math.round(size.width * dpr);
      const targetH = Math.round(size.height * dpr);
      if (overlayCvs.width !== targetW || overlayCvs.height !== targetH) {
        overlayCvs.width = targetW;
        overlayCvs.height = targetH;
      }
      if (glCvs.width !== targetW || glCvs.height !== targetH) {
        glCvs.width = targetW;
        glCvs.height = targetH;
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, size.width, size.height);

      const cam = camRef.current;
      const fwdX = Math.sin(cam.yaw);
      const fwdY = -Math.cos(cam.yaw);
      const rightX = Math.cos(cam.yaw);
      const rightY = Math.sin(cam.yaw);
      const cosPitch = Math.cos(cam.pitch);
      const sinPitch = Math.sin(cam.pitch);

      const lookAhead = Math.max(55, Math.min(620, cam.heightPx * 1.15));
      const patchCenterX = cam.camX + fwdX * lookAhead;
      const patchCenterY = cam.camY + fwdY * lookAhead;

      const defaultZ = points3D[0]?.z ?? (grid ? (grid.zMin + grid.zMax) * 0.35 : -190);
      if (cam.camBaseZ === null || !Number.isFinite(cam.camBaseZ)) {
        const sInit = sampleTerrain(grid, patchCenterX, patchCenterY, defaultZ);
        if (sInit.valid) {
          cam.camBaseZ = sInit.z;
        }
      }
      const mPerPx = 0.9;
      // Keep camera reference altitude steady during mouse rotation; only glide up if ground directly under camera rises too close
      const underCam = sampleTerrain(grid, cam.camX, cam.camY, NaN);
      if (underCam.valid && Number.isFinite(underCam.z) && cam.camBaseZ !== null) {
        const camWorldZ = cam.camBaseZ + (cam.heightPx * mPerPx) / zScale;
        if (underCam.z + 10.0 > camWorldZ) {
          const reqBase = underCam.z + 10.0 - (cam.heightPx * mPerPx) / zScale;
          cam.camBaseZ += (reqBase - cam.camBaseZ) * 0.16;
        }
      }
      const groundZ = cam.camBaseZ ?? defaultZ;
      const fovScale = Math.min(size.width, size.height) * 0.92;

      const project = (wx: number, wy: number, wz: number) => {
        const vx = wx - cam.camX;
        const vy = wy - cam.camY;
        const vz = ((wz - groundZ) / mPerPx) * zScale - cam.heightPx;
        const camRight = vx * rightX + vy * rightY;
        const horizFwd = vx * fwdX + vy * fwdY;
        const cz = horizFwd * cosPitch - vz * sinPitch;
        if (cz <= 6) return null;
        const camUp = horizFwd * sinPitch + vz * cosPitch;
        const scale = fovScale / cz;
        return {
          sx: size.width * 0.5 + camRight * scale,
          sy: size.height * 0.5 - camUp * scale,
          depth: cz,
        };
      };

      const patchRadius = Math.max(115, Math.min(980, cam.heightPx * 1.68));
      const step = (patchRadius * 2) / MESH_N;
      const startX = Math.floor((patchCenterX - patchRadius) / step) * step;
      const startY = Math.floor((patchCenterY - patchRadius) / step) * step;
      const zSpan = Math.max(20, (grid?.zMax ?? 100) - (grid?.zMin ?? -250));
      const zMin = grid?.zMin ?? -250;
      const mapW = map?.width ?? 4096;
      const mapH = map?.height ?? 4096;

      if (glSt) {
        const {
          gl,
          vertsData,
          vbo,
          ibo,
          indexCount,
          structVertsData,
          structVbo,
          structIbo,
          mapTex,
          uScaleX,
          uScaleY,
          uMode,
          bindVertLayout,
        } = glSt;
        gl.viewport(0, 0, targetW, targetH);
        gl.clearColor(0.08, 0.13, 0.22, 1.0);
        gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
        gl.enable(gl.DEPTH_TEST);

        let vPtr = 0;
        for (let iy = 0; iy <= MESH_N; iy++) {
          const wy = startY + iy * step;
          const v = Math.max(0, Math.min(1, wy / mapH));
          for (let ix = 0; ix <= MESH_N; ix++) {
            const wx = startX + ix * step;
            const u = Math.max(0, Math.min(1, wx / mapW));
            const s = sampleTerrain(grid, wx, wy, groundZ);
            const sxRight = sampleTerrain(grid, wx + step, wy, s.z);
            const syDown = sampleTerrain(grid, wx, wy + step, s.z);

            const vx = wx - cam.camX;
            const vy = wy - cam.camY;
            const vz = ((s.z - groundZ) / mPerPx) * zScale - cam.heightPx;
            const camRight = vx * rightX + vy * rightY;
            const horizFwd = vx * fwdX + vy * fwdY;
            const cz = horizFwd * cosPitch - vz * sinPitch;
            const camUp = horizFwd * sinPitch + vz * cosPitch;

            const dzx = (sxRight.z - s.z) / (step * mPerPx);
            const dzy = (syDown.z - s.z) / (step * mPerPx);
            let shade = Math.max(0.76, Math.min(1.18, 1.0 + (-dzx * 0.35 - dzy * 0.28) * 0.32));
            if (s.valid && Math.floor(s.z / 15) !== Math.floor(sxRight.z / 15)) {
              shade *= 1.06;
            }

            let tr = 0.25;
            let tg = 0.55;
            let tb = 0.42;
            if (surfaceMode === 'walkability') {
              if (s.obs) [tr, tg, tb] = [0.16, 0.5, 0.88];
              else if (s.slope > 42) [tr, tg, tb] = [0.92, 0.26, 0.28];
              else if (s.slope > 34) [tr, tg, tb] = [0.95, 0.68, 0.2];
              else [tr, tg, tb] = [0.22, 0.76, 0.46];
            } else {
              [tr, tg, tb] = elevationColor((s.z - zMin) / zSpan);
            }

            vertsData[vPtr++] = camRight;
            vertsData[vPtr++] = camUp;
            vertsData[vPtr++] = cz;
            vertsData[vPtr++] = u;
            vertsData[vPtr++] = v;
            vertsData[vPtr++] = tr;
            vertsData[vPtr++] = tg;
            vertsData[vPtr++] = tb;
            vertsData[vPtr++] = shade;
          }
        }

        const texSource = grid?.colorImage ?? image;
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, mapTex);
        if (!glSt.hasMapTex && texSource) {
          gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, texSource);
          glSt.hasMapTex = true;
        }
        gl.uniform1f(uScaleX, fovScale / (size.width * 0.5));
        gl.uniform1f(uScaleY, fovScale / (size.height * 0.5));
        gl.uniform1f(uMode, surfaceMode === 'map' && glSt.hasMapTex ? 0.0 : 1.0);

        gl.bindBuffer(gl.ARRAY_BUFFER, vbo);
        bindVertLayout();
        gl.bufferSubData(gl.ARRAY_BUFFER, 0, vertsData);
        gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, ibo);
        gl.drawElements(gl.TRIANGLES, indexCount, gl.UNSIGNED_SHORT, 0);

        // Render 3D Buildings, Roofs, Bridges, Plazas, Stone Mesas, and Giant Trees
        if (showBuildings && grid?.structures?.length) {
          const cullDist = patchRadius * 1.08;
          let sCount = 0;
          let svPtr = 0;

          const pushVert = (
            wx: number,
            wy: number,
            wz: number,
            u: number,
            v: number,
            r: number,
            g: number,
            b: number,
            sh: number,
          ) => {
            const vx = wx - cam.camX;
            const vy = wy - cam.camY;
            const vz = ((wz - groundZ) / mPerPx) * zScale - cam.heightPx;
            const cr = vx * rightX + vy * rightY;
            const hf = vx * fwdX + vy * fwdY;
            const cz = hf * cosPitch - vz * sinPitch;
            const cu = hf * sinPitch + vz * cosPitch;
            structVertsData[svPtr++] = cr;
            structVertsData[svPtr++] = cu;
            structVertsData[svPtr++] = cz;
            structVertsData[svPtr++] = u;
            structVertsData[svPtr++] = v;
            structVertsData[svPtr++] = r;
            structVertsData[svPtr++] = g;
            structVertsData[svPtr++] = b;
            structVertsData[svPtr++] = sh;
          };

          const visibleStructs: StructureBox3D[] = [];
          for (let i = 0; i < grid.structures.length; i++) {
            const st = grid.structures[i];
            if (Math.abs(st.cx - patchCenterX) <= cullDist && Math.abs(st.cy - patchCenterY) <= cullDist) {
              visibleStructs.push(st);
            }
          }
          if (visibleStructs.length > MAX_STRUCTS) {
            visibleStructs.sort((a, b) => {
              const da = (a.cx - patchCenterX) ** 2 + (a.cy - patchCenterY) ** 2;
              const db = (b.cx - patchCenterX) ** 2 + (b.cy - patchCenterY) ** 2;
              return da - db;
            });
          }

          for (let i = 0; i < visibleStructs.length && sCount < MAX_STRUCTS; i++) {
            const st = visibleStructs[i];
            const normZ = (st.zTop - zMin) / zSpan;
            const { wall, roof } = structureColors(st.kind, surfaceMode, normZ);
            const [wr, wg, wb] = wall;
            const [rr, rg, rb] = roof;
            const taper = st.kind === 6 ? 0.14 : st.kind === 5 ? 0.1 : st.kind === 1 ? 0.04 : 0.0;
            const dx = (st.x1 - st.x0) * taper;
            const dy = (st.y1 - st.y0) * taper;
            const tx0 = st.x0 + dx;
            const tx1 = st.x1 - dx;
            const ty0 = st.y0 + dy;
            const ty1 = st.y1 - dy;
            const zb = st.zBase;
            const zt = st.zTop + 0.25;

            // Face 0: North wall (-Y), shade = 0.68
            pushVert(st.x0, st.y0, zb, -1, -1, wr, wg, wb, 0.68);
            pushVert(st.x1, st.y0, zb, -1, -1, wr, wg, wb, 0.68);
            pushVert(tx1, ty0, zt, -1, -1, wr, wg, wb, 0.74);
            pushVert(tx0, ty0, zt, -1, -1, wr, wg, wb, 0.74);

            // Face 1: South wall (+Y), shade = 0.92
            pushVert(st.x1, st.y1, zb, -1, -1, wr, wg, wb, 0.92);
            pushVert(st.x0, st.y1, zb, -1, -1, wr, wg, wb, 0.92);
            pushVert(tx0, ty1, zt, -1, -1, wr, wg, wb, 0.98);
            pushVert(tx1, ty1, zt, -1, -1, wr, wg, wb, 0.98);

            // Face 2: West wall (-X), shade = 0.74
            pushVert(st.x0, st.y1, zb, -1, -1, wr, wg, wb, 0.74);
            pushVert(st.x0, st.y0, zb, -1, -1, wr, wg, wb, 0.74);
            pushVert(tx0, ty0, zt, -1, -1, wr, wg, wb, 0.80);
            pushVert(tx0, ty1, zt, -1, -1, wr, wg, wb, 0.80);

            // Face 3: East wall (+X), shade = 0.86
            pushVert(st.x1, st.y0, zb, -1, -1, wr, wg, wb, 0.86);
            pushVert(st.x1, st.y1, zb, -1, -1, wr, wg, wb, 0.86);
            pushVert(tx1, ty1, zt, -1, -1, wr, wg, wb, 0.92);
            pushVert(tx1, ty0, zt, -1, -1, wr, wg, wb, 0.92);

            // Face 4: Roof / Bridge Deck / Mesa Top cap (v + 2.0 signals roof UV in shader)
            const u0 = Math.max(0, Math.min(1, tx0 / mapW));
            const u1 = Math.max(0, Math.min(1, tx1 / mapW));
            const v0 = Math.max(0, Math.min(1, ty0 / mapH)) + 2.0;
            const v1 = Math.max(0, Math.min(1, ty1 / mapH)) + 2.0;
            pushVert(tx0, ty0, zt, u0, v0, rr, rg, rb, 1.06);
            pushVert(tx1, ty0, zt, u1, v0, rr, rg, rb, 1.06);
            pushVert(tx1, ty1, zt, u1, v1, rr, rg, rb, 1.06);
            pushVert(tx0, ty1, zt, u0, v1, rr, rg, rb, 1.06);

            sCount++;
          }

          if (sCount > 0) {
            gl.bindBuffer(gl.ARRAY_BUFFER, structVbo);
            bindVertLayout();
            gl.bufferSubData(gl.ARRAY_BUFFER, 0, structVertsData.subarray(0, sCount * 20 * 9));
            gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, structIbo);
            gl.drawElements(gl.TRIANGLES, sCount * 30, gl.UNSIGNED_SHORT, 0);
          }
        }
      }

      // 2. Gingerbread 15m Aura Rings on Terrain Surface
      if (showAura && points3D.length > 0) {
        const auraPx = 15.0 / mPerPx;
        ctx.save();
        ctx.strokeStyle = 'rgba(251, 191, 36, 0.35)';
        ctx.fillStyle = 'rgba(251, 191, 36, 0.09)';
        ctx.lineWidth = 1.3;
        for (let i = 0; i < points3D.length; i++) {
          const wp = points3D[i];
          if (Math.hypot(wp.x - patchCenterX, wp.y - patchCenterY) > patchRadius * 1.35) continue;
          ctx.beginPath();
          let started = false;
          for (let s = 0; s <= 18; s++) {
            const ang = (s / 18) * Math.PI * 2;
            const p = project(wp.x + Math.cos(ang) * auraPx, wp.y + Math.sin(ang) * auraPx, wp.z + 0.4);
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
            ctx.fill();
            ctx.stroke();
          }
        }
        ctx.restore();
      }

      // 3. Draw 3D Material Spawner Nodes
      const nextScreenSpawners: Array<{ pt: SpawnerPoint3D; sx: number; sy: number; captured: boolean }> = [];
      if (showSpawners && annotatedSpawners.list.length > 0) {
        for (const item of annotatedSpawners.list) {
          const sp = item.pt;
          if (Math.abs(sp.x - patchCenterX) > patchRadius * 1.25 || Math.abs(sp.y - patchCenterY) > patchRadius * 1.25) {
            continue;
          }
          const proj = project(sp.x, sp.y, sp.z + 1.0);
          if (!proj || proj.sx < -20 || proj.sx > size.width + 20 || proj.sy < -20 || proj.sy > size.height + 20) {
            continue;
          }
          const r = item.isTarget ? 5.2 : item.captured ? 4.0 : 2.5;
          ctx.beginPath();
          ctx.arc(proj.sx, proj.sy, r, 0, Math.PI * 2);
          ctx.fillStyle = KIND_COLOR[sp.kind] ?? '#e2e8f0';
          ctx.globalAlpha = item.captured || item.isTarget ? 0.96 : 0.52;
          ctx.fill();
          if (item.captured || item.isTarget) {
            ctx.lineWidth = item.isTarget ? 2.0 : 1.2;
            ctx.strokeStyle = item.isTarget ? '#ffffff' : '#fef08a';
            ctx.stroke();
          }
          ctx.globalAlpha = 1.0;
          nextScreenSpawners.push({ pt: sp, sx: proj.sx, sy: proj.sy, captured: item.captured });
        }
      }
      screenSpawnersRef.current = nextScreenSpawners;

      // 4. Draw Warp Spire Beacons with Floating Nameplates
      if (teleports && teleports.length > 0) {
        const activeTp = startTeleport
          ? [startTeleport, ...teleports.filter((t) => t.name !== startTeleport.name)]
          : teleports;
        ctx.font = '600 11px system-ui, sans-serif';
        ctx.textAlign = 'center';
        let labelsDrawn = 0;
        for (const tp of activeTp) {
          const distFromPatch = Math.hypot(tp.x - patchCenterX, tp.y - patchCenterY);
          if (distFromPatch > patchRadius * 1.35 && tp.name !== startTeleport?.name) continue;
          const ts = sampleTerrain(grid, tp.x, tp.y, groundZ);
          const baseP = project(tp.x, tp.y, ts.z);
          const topP = project(tp.x, tp.y, ts.z + 24);
          if (!baseP || !topP) continue;
          const isStart = startTeleport?.name === tp.name;
          ctx.strokeStyle = isStart ? '#38bdf8' : 'rgba(147, 197, 253, 0.72)';
          ctx.lineWidth = isStart ? 3.0 : 1.8;
          ctx.beginPath();
          ctx.moveTo(baseP.sx, baseP.sy);
          ctx.lineTo(topP.sx, topP.sy);
          ctx.stroke();
          ctx.fillStyle = isStart ? '#38bdf8' : '#93c5fd';
          ctx.beginPath();
          ctx.arc(topP.sx, topP.sy, isStart ? 5.5 : 3.8, 0, Math.PI * 2);
          ctx.fill();
          if ((isStart || (distFromPatch < patchRadius * 0.85 && labelsDrawn < 14)) && tp.name) {
            labelsDrawn++;
            const label = isStart ? `★ ${tp.name}` : tp.name;
            const tw = ctx.measureText(label).width + 10;
            ctx.fillStyle = isStart ? 'rgba(3, 105, 161, 0.90)' : 'rgba(15, 23, 42, 0.78)';
            ctx.fillRect(topP.sx - tw * 0.5, topP.sy - 22, tw, 16);
            ctx.fillStyle = isStart ? '#e0f2fe' : '#f1f5f9';
            ctx.fillText(label, topP.sx, topP.sy - 10);
          }
        }
      }

      // 5. Draw 3D Route Trajectory + Waypoint Pillars
      const nextScreenWps: Array<{ index: number; sx: number; sy: number; depth: number; z: number; slope: number }> = [];
      if (points3D.length > 0) {
        for (let i = 0; i < points3D.length - 1; i++) {
          const a = points3D[i];
          const b = points3D[i + 1];
          const segSamples = 8;
          ctx.beginPath();
          let started = false;
          for (let s = 0; s <= segSamples; s++) {
            const t = s / segSamples;
            const wx = a.x + (b.x - a.x) * t;
            const wy = a.y + (b.y - a.y) * t;
            const linZ = a.z + (b.z - a.z) * t;
            const terr = sampleTerrain(grid, wx, wy, linZ);
            const wz = Math.max(linZ, terr.z) + 1.8;
            const p = project(wx, wy, wz);
            if (!p) continue;
            if (!started) {
              ctx.moveTo(p.sx, p.sy);
              started = true;
            } else {
              ctx.lineTo(p.sx, p.sy);
            }
          }
          if (started) {
            const dz = b.z - a.z;
            const dxy = Math.max(1, Math.hypot(b.x - a.x, b.y - a.y) * mPerPx);
            const grade = Math.abs(dz / dxy);
            ctx.strokeStyle = grade > 0.45 ? '#fb7185' : grade > 0.25 ? '#fbbf24' : '#38bdf8';
            ctx.lineWidth = preview?.length ? 2.4 : 3.2;
            ctx.stroke();
          }
        }

        for (let i = 0; i < points3D.length; i++) {
          const wp = points3D[i];
          const baseP = project(wp.x, wp.y, wp.z);
          const headP = project(wp.x, wp.y, wp.z + 5.5);
          if (!baseP || !headP) continue;
          const isSel = selected === i || selectedSet?.has(i);
          const isTrial = activeTrialIndex === i;
          ctx.strokeStyle = isSel ? '#f43f5e' : 'rgba(255,255,255,0.68)';
          ctx.lineWidth = isSel ? 2.4 : 1.5;
          ctx.beginPath();
          ctx.moveTo(baseP.sx, baseP.sy);
          ctx.lineTo(headP.sx, headP.sy);
          ctx.stroke();
          const radius = isSel || isTrial ? 7.2 : i === 0 || i === points3D.length - 1 ? 6.2 : 4.6;
          ctx.beginPath();
          ctx.arc(headP.sx, headP.sy, radius, 0, Math.PI * 2);
          ctx.fillStyle = isTrial ? '#a855f7' : isSel ? '#f43f5e' : i === 0 ? '#22c55e' : i === points3D.length - 1 ? '#f97316' : '#f8fafc';
          ctx.fill();
          ctx.lineWidth = 1.6;
          ctx.strokeStyle = '#0f172a';
          ctx.stroke();
          if (isSel || i === 0 || i === points3D.length - 1 || i % 4 === 0) {
            ctx.fillStyle = '#f8fafc';
            ctx.font = '600 11px system-ui, sans-serif';
            ctx.textAlign = 'center';
            ctx.fillText(`#${i + 1}`, headP.sx, headP.sy - radius - 4);
          }
          nextScreenWps.push({ index: i, sx: headP.sx, sy: headP.sy, depth: headP.depth, z: wp.z, slope: wp.slope });
        }
      }
      screenWaypointsRef.current = nextScreenWps;

      // 6. Live Pose Indicator
      if (pose) {
        const ps = sampleTerrain(grid, pose.x, pose.y, groundZ);
        const pp = project(pose.x, pose.y, ps.z + 4.0);
        if (pp) {
          ctx.fillStyle = '#ec4899';
          ctx.beginPath();
          ctx.arc(pp.sx, pp.sy, 7, 0, Math.PI * 2);
          ctx.fill();
          ctx.strokeStyle = '#ffffff';
          ctx.lineWidth = 2;
          ctx.stroke();
        }
      }

      // 7. 2D Mini-Radar in Bottom-Right
      const rCvs = radarRef.current;
      if (rCvs && map) {
        const rCtx = rCvs.getContext('2d');
        if (rCtx) {
          const rw = 176;
          const rh = 176;
          if (rCvs.width !== rw) rCvs.width = rw;
          if (rCvs.height !== rh) rCvs.height = rh;
          if (radarBgRef.current) {
            rCtx.drawImage(radarBgRef.current, 0, 0);
            rCtx.fillStyle = 'rgba(15, 23, 42, 0.20)';
            rCtx.fillRect(0, 0, rw, rh);
          } else {
            rCtx.fillStyle = '#0f172a';
            rCtx.fillRect(0, 0, rw, rh);
          }
          if (points3D.length > 1) {
            rCtx.strokeStyle = '#38bdf8';
            rCtx.lineWidth = 1.8;
            rCtx.beginPath();
            for (let i = 0; i < points3D.length; i++) {
              const rx = (points3D[i].x / map.width) * rw;
              const ry = (points3D[i].y / map.height) * rh;
              if (i === 0) rCtx.moveTo(rx, ry);
              else rCtx.lineTo(rx, ry);
            }
            rCtx.stroke();
          }
          const cx = (cam.camX / map.width) * rw;
          const cy = (cam.camY / map.height) * rh;
          const lookLen = 28;
          rCtx.fillStyle = 'rgba(56, 189, 248, 0.26)';
          rCtx.strokeStyle = '#38bdf8';
          rCtx.lineWidth = 1.4;
          rCtx.beginPath();
          rCtx.moveTo(cx, cy);
          rCtx.lineTo(cx + Math.sin(cam.yaw - 0.45) * lookLen, cy - Math.cos(cam.yaw - 0.45) * lookLen);
          rCtx.lineTo(cx + Math.sin(cam.yaw + 0.45) * lookLen, cy - Math.cos(cam.yaw + 0.45) * lookLen);
          rCtx.closePath();
          rCtx.fill();
          rCtx.stroke();
          rCtx.fillStyle = '#f43f5e';
          rCtx.beginPath();
          rCtx.arc(cx, cy, 3.6, 0, Math.PI * 2);
          rCtx.fill();
        }
      }

      setHudStat((prev) => {
        const pDeg = Math.round((cam.pitch * 180) / Math.PI);
        const eM = Math.round(groundZ);
        const yDeg = ((Math.round((cam.yaw * 180) / Math.PI) % 360) + 360) % 360;
        if (prev.pitchDeg === pDeg && prev.elevM === eM && prev.yawDeg === yDeg) return prev;
        return { pitchDeg: pDeg, elevM: eM, yawDeg: yDeg };
      });
    };
    scheduleDraw();
  }, [
    size,
    grid,
    image,
    map,
    points3D,
    annotatedSpawners,
    teleports,
    startTeleport,
    selected,
    selectedSet,
    activeTrialIndex,
    pose,
    surfaceMode,
    zScale,
    showBuildings,
    showAura,
    showSpawners,
    scheduleDraw,
  ]);

  // 60fps WASD + Q/E + Shift first-person flight loop + F/Tab/Esc cursor lock shortcut
  const pressedKeysRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    let rafId = 0;
    let lastTime = performance.now();

    const stepLoop = (now: number) => {
      const dt = Math.min(0.08, (now - lastTime) / 1000);
      lastTime = now;
      const keys = pressedKeysRef.current;
      if (keys.size > 0) {
        const c = camRef.current;
        const fast = keys.has('shift') ? 2.8 : 1.0;
        const moveSpd = Math.max(140, c.heightPx * 1.45) * fast * dt;
        const fwdX = Math.sin(c.yaw);
        const fwdY = -Math.cos(c.yaw);
        const rightX = Math.cos(c.yaw);
        const rightY = Math.sin(c.yaw);

        let dx = 0;
        let dy = 0;
        if (keys.has('w') || keys.has('arrowup')) {
          dx += fwdX;
          dy += fwdY;
        }
        if (keys.has('s') || keys.has('arrowdown')) {
          dx -= fwdX;
          dy -= fwdY;
        }
        if (keys.has('d') || keys.has('arrowright')) {
          dx += rightX;
          dy += rightY;
        }
        if (keys.has('a') || keys.has('arrowleft')) {
          dx -= rightX;
          dy -= rightY;
        }
        const len = Math.hypot(dx, dy);
        if (len > 0) {
          c.camX = Math.max(0, Math.min(map?.width ?? 16000, c.camX + (dx / len) * moveSpd));
          c.camY = Math.max(0, Math.min(map?.height ?? 16000, c.camY + (dy / len) * moveSpd));
        }
        if (keys.has('e') || keys.has(' ')) {
          c.heightPx = Math.min(1500, c.heightPx * Math.pow(2.1, dt * fast));
        }
        if (keys.has('q') || keys.has('c')) {
          c.heightPx = Math.max(35, c.heightPx / Math.pow(2.1, dt * fast));
        }
        drawFnRef.current();
        rafId = requestAnimationFrame(stepLoop);
      } else {
        rafId = 0;
      }
    };

    const startLoop = () => {
      if (!rafId) {
        lastTime = performance.now();
        rafId = requestAnimationFrame(stepLoop);
      }
    };

    const isInputFocused = () => {
      const tag = document.activeElement?.tagName.toLowerCase();
      return tag === 'input' || tag === 'textarea' || tag === 'select';
    };

    const onKeyDown = (ev: KeyboardEvent) => {
      if (isInputFocused()) return;
      const k = ev.key.toLowerCase();
      if (k === 'escape' && pointerLockedRef.current) {
        ev.preventDefault();
        setCursorLock(false);
        return;
      }
      if (k === 'f' || k === 'tab') {
        ev.preventDefault();
        togglePointerLock();
        return;
      }
      if (['w', 'a', 's', 'd', 'q', 'e', 'c', ' ', 'shift', 'arrowup', 'arrowdown', 'arrowleft', 'arrowright'].includes(k)) {
        if ([' ', 'arrowup', 'arrowdown', 'arrowleft', 'arrowright'].includes(k)) ev.preventDefault();
        pressedKeysRef.current.add(k);
        startLoop();
      }
    };

    const onKeyUp = (ev: KeyboardEvent) => {
      pressedKeysRef.current.delete(ev.key.toLowerCase());
    };
    const onBlur = () => {
      pressedKeysRef.current.clear();
      if (pointerLockedRef.current) setCursorLock(false);
    };

    window.addEventListener('keydown', onKeyDown);
    window.addEventListener('keyup', onKeyUp);
    window.addEventListener('blur', onBlur);
    return () => {
      window.removeEventListener('keydown', onKeyDown);
      window.removeEventListener('keyup', onKeyUp);
      window.removeEventListener('blur', onBlur);
      if (rafId) cancelAnimationFrame(rafId);
    };
  }, [map?.width, map?.height, setCursorLock, togglePointerLock]);

  const handleMouseDown = (ev: React.MouseEvent<HTMLCanvasElement>) => {
    const cvs = overlayCanvasRef.current;
    if (!cvs) return;
    if (ev.button === 2) {
      ev.preventDefault();
      togglePointerLock();
      return;
    }
    if (ev.button === 0) {
      if (pointerLockedRef.current) {
        setCursorLock(false);
        return;
      }
      const rect = cvs.getBoundingClientRect();
      const mx = ev.clientX - rect.left;
      const my = ev.clientY - rect.top;
      for (const wp of screenWaypointsRef.current) {
        if (Math.hypot(wp.sx - mx, wp.sy - my) <= 10) {
          onSelect(wp.index, ev.ctrlKey || ev.metaKey);
          return;
        }
      }
      lastDragRef.current = { x: ev.clientX, y: ev.clientY };
      setCursorLock(true);
    }
  };

  const handleMouseMove = (ev: React.MouseEvent<HTMLCanvasElement>) => {
    const cvs = overlayCanvasRef.current;
    if (!cvs) return;
    if (pointerLockedRef.current) {
      return;
    }
    if ((ev.buttons & 1) !== 0) {
      const prev = lastDragRef.current;
      lastDragRef.current = { x: ev.clientX, y: ev.clientY };
      if (prev) {
        const dx = Math.max(-55, Math.min(55, ev.clientX - prev.x));
        const dy = Math.max(-55, Math.min(55, ev.clientY - prev.y));
        camRef.current.yaw += dx * 0.0028;
        camRef.current.pitch = Math.max(0.14, Math.min(1.52, camRef.current.pitch + dy * 0.0024));
        scheduleDraw();
      }
      return;
    }
    lastDragRef.current = null;

    const rect = cvs.getBoundingClientRect();
    const mx = ev.clientX - rect.left;
    const my = ev.clientY - rect.top;
    for (const wp of screenWaypointsRef.current) {
      if (Math.hypot(wp.sx - mx, wp.sy - my) <= 10) {
        setHoverInfo({
          title: `航点 #${wp.index + 1}`,
          subtitle: `海拔 ${wp.z.toFixed(1)}m · 坡度 ${wp.slope.toFixed(1)}°`,
          screenX: mx,
          screenY: my,
        });
        return;
      }
    }
    for (const sp of screenSpawnersRef.current) {
      if (Math.hypot(sp.sx - mx, sp.sy - my) <= 7) {
        setHoverInfo({
          title: `${sp.pt.name} (${KIND_NAME[sp.pt.kind] ?? sp.pt.kind})`,
          subtitle: `海拔 ${sp.pt.z.toFixed(1)}m · ${sp.captured ? '✓ 15m 光环已覆盖' : '未进入当前路线光环'}`,
          screenX: mx,
          screenY: my,
        });
        return;
      }
    }
    setHoverInfo(null);
  };

  const handleWheel = (ev: React.WheelEvent<HTMLCanvasElement>) => {
    ev.preventDefault();
    const factor = ev.deltaY < 0 ? 0.86 : 1.16;
    camRef.current.heightPx = Math.max(35, Math.min(1500, camRef.current.heightPx * factor));
    scheduleDraw();
  };

  const handleRadarClick = (ev: React.MouseEvent<HTMLCanvasElement>) => {
    if (!map || !radarRef.current) return;
    const rect = radarRef.current.getBoundingClientRect();
    const rx = Math.max(0, Math.min(1, (ev.clientX - rect.left) / rect.width));
    const ry = Math.max(0, Math.min(1, (ev.clientY - rect.top) / rect.height));
    lookAtPoint(rx * map.width, ry * map.height);
  };

  return (
    <div
      ref={containerRef}
      style={{
        position: 'relative',
        width: '100%',
        height: '100%',
        overflow: 'hidden',
        background: '#080e1a',
        userSelect: 'none',
      }}
      onContextMenu={(e) => e.preventDefault()}
    >
      <canvas
        ref={glCanvasRef}
        style={{
          position: 'absolute',
          inset: 0,
          width: '100%',
          height: '100%',
          display: 'block',
          pointerEvents: 'none',
        }}
      />
      <canvas
        ref={overlayCanvasRef}
        style={{
          position: 'absolute',
          inset: 0,
          width: '100%',
          height: '100%',
          display: 'block',
          cursor: pointerLocked ? 'none' : 'crosshair',
        }}
        onMouseDown={handleMouseDown}
        onMouseMove={handleMouseMove}
        onMouseLeave={() => setHoverInfo(null)}
        onWheel={handleWheel}
      />

      {pointerLocked && (
        <div
          style={{
            position: 'absolute',
            left: '50%',
            top: '50%',
            transform: 'translate(-50%, -50%)',
            pointerEvents: 'none',
            width: 18,
            height: 18,
            borderRadius: '50%',
            border: '1.5px solid rgba(56, 189, 248, 0.85)',
            boxShadow: '0 0 8px rgba(56, 189, 248, 0.45)',
          }}
        />
      )}

      <div
        style={{
          position: 'absolute',
          top: 12,
          right: 12,
          display: 'flex',
          flexDirection: 'column',
          gap: 6,
          background: 'rgba(15, 23, 42, 0.88)',
          border: '1px solid rgba(148, 163, 184, 0.25)',
          borderRadius: 10,
          padding: '8px 10px',
          backdropFilter: 'blur(8px)',
          color: '#f8fafc',
          fontSize: 12,
          boxShadow: '0 8px 24px rgba(0,0,0,0.45)',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
          <button
            type="button"
            onClick={togglePointerLock}
            style={{
              padding: '3px 9px',
              borderRadius: 6,
              border: '1px solid rgba(56, 189, 248, 0.5)',
              cursor: 'pointer',
              fontSize: 11,
              fontWeight: 600,
              background: pointerLocked ? '#22c55e' : 'rgba(2, 132, 199, 0.35)',
              color: pointerLocked ? '#052e16' : '#e0f2fe',
            }}
          >
            {pointerLocked ? '● 视角已锁定 (Esc/点击解锁)' : '🎯 点击画面/按F锁定鼠标转视角'}
          </button>
          <span style={{ color: '#94a3b8', fontWeight: 600 }}>图层:</span>
          {(['map', 'elevation', 'walkability'] as const).map((m) => (
            <button
              key={m}
              type="button"
              onClick={() => setSurfaceMode(m)}
              style={{
                padding: '3px 8px',
                borderRadius: 6,
                border: 'none',
                cursor: 'pointer',
                fontSize: 11,
                fontWeight: 600,
                background: surfaceMode === m ? '#0284c7' : 'rgba(51, 65, 85, 0.7)',
                color: '#f8fafc',
              }}
            >
              {m === 'map' ? '原色彩图' : m === 'elevation' ? '海拔等高' : '坡度通行'}
            </button>
          ))}
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <label style={{ display: 'flex', alignItems: 'center', gap: 4, cursor: 'pointer' }}>
            <input type="checkbox" checked={showBuildings} onChange={(e) => setShowBuildings(e.target.checked)} />
            <span>3D 建筑风貌</span>
          </label>
          <label style={{ display: 'flex', alignItems: 'center', gap: 4, cursor: 'pointer' }}>
            <input type="checkbox" checked={showAura} onChange={(e) => setShowAura(e.target.checked)} />
            <span>15m 姜饼人光环</span>
          </label>
          <label style={{ display: 'flex', alignItems: 'center', gap: 4, cursor: 'pointer' }}>
            <input type="checkbox" checked={showSpawners} onChange={(e) => setShowSpawners(e.target.checked)} />
            <span>3D 材料点 ({annotatedSpawners.capturedCount} 命中)</span>
          </label>
          <label style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
            <span style={{ color: '#94a3b8' }}>起伏:</span>
            <input
              type="range"
              min="1.0"
              max="3.2"
              step="0.2"
              value={zScale}
              onChange={(e) => setZScale(parseFloat(e.target.value))}
              style={{ width: 60 }}
            />
          </label>
          <button
            type="button"
            onClick={fit}
            style={{
              padding: '2px 8px',
              borderRadius: 5,
              border: '1px solid rgba(148,163,184,0.35)',
              background: 'rgba(30, 41, 59, 0.85)',
              color: '#e2e8f0',
              fontSize: 11,
              cursor: 'pointer',
            }}
          >
            回中
          </button>
        </div>
      </div>

      <div
        style={{
          position: 'absolute',
          right: 12,
          bottom: 12,
          background: 'rgba(15, 23, 42, 0.9)',
          border: '1px solid rgba(56, 189, 248, 0.35)',
          borderRadius: 10,
          padding: 6,
          boxShadow: '0 8px 24px rgba(0,0,0,0.5)',
        }}
      >
        <div style={{ fontSize: 10, color: '#94a3b8', marginBottom: 4, display: 'flex', justifyContent: 'space-between' }}>
          <span>2D 鸟瞰雷达 (点击跳转)</span>
          <span style={{ color: '#38bdf8' }}>{hudStat.yawDeg}°</span>
        </div>
        <canvas
          ref={radarRef}
          width={176}
          height={176}
          onClick={handleRadarClick}
          style={{ width: 176, height: 176, borderRadius: 6, cursor: 'pointer', display: 'block' }}
        />
      </div>

      <div
        style={{
          position: 'absolute',
          left: 12,
          bottom: 12,
          background: 'rgba(15, 23, 42, 0.84)',
          border: '1px solid rgba(148, 163, 184, 0.2)',
          borderRadius: 8,
          padding: '6px 10px',
          color: '#cbd5e1',
          fontSize: 11,
          pointerEvents: 'none',
        }}
      >
        <span>
          操作: <b>左键/右键/F</b> 锁定指针转视角 (Esc解锁) · <b>WASD</b> 飞行 · <b>E/Q</b> 升降 · <b>Shift</b> 加速 · 仰角{' '}
          {hudStat.pitchDeg}° · 地面海拔 {hudStat.elevM}m
        </span>
      </div>

      {hoverInfo && !pointerLocked && (
        <div
          style={{
            position: 'absolute',
            left: Math.min(size.width - 220, hoverInfo.screenX + 14),
            top: Math.max(12, hoverInfo.screenY - 26),
            background: 'rgba(15, 23, 42, 0.94)',
            border: '1px solid rgba(56, 189, 248, 0.45)',
            borderRadius: 8,
            padding: '6px 10px',
            color: '#f8fafc',
            fontSize: 12,
            pointerEvents: 'none',
            boxShadow: '0 6px 18px rgba(0,0,0,0.45)',
          }}
        >
          <div style={{ fontWeight: 700, color: '#38bdf8' }}>{hoverInfo.title}</div>
          <div style={{ fontSize: 11, color: '#cbd5e1', marginTop: 2 }}>{hoverInfo.subtitle}</div>
        </div>
      )}
    </div>
  );
});
