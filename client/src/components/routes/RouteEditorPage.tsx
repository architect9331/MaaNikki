import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertCircle, ArrowDown, ArrowLeft, ArrowUp, ArrowUpDown, BoxSelect, Check, ChevronDown, ChevronRight,
  CirclePlay, ClipboardPaste, Copy, FileInput, FileOutput, FolderOpen, Footprints, Layers, ListPlus,
  Loader2, MapPin, Maximize, Minus, MousePointer2, Plus, Redo2, Route, SlidersHorizontal, Sparkles,
  Square, Trash2, Undo2, X,
} from 'lucide-react';
import clsx from 'clsx';
import { toast } from 'sonner';
import { revealItemInDir } from '@tauri-apps/plugin-opener';
import { invoke } from '@tauri-apps/api/core';
import { useAppStore } from '@/stores/appStore';
import { isTauri } from '@/utils/paths';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { primaryButton, secondaryButton, Segmented } from '@/components/ui/FeaturePage';
import { SwitchTrack } from '@/components/ui/SwitchTrack';
import {
  ACTIONS, MACROS, ROUTE_KEYS, actionMeta, nearestCheckpoint, newRoute, newRouteId, parseRouteFile,
  pointsFromRecording, routeIssues, routeLength, withAction,
  type Checkpoint, type RouteCollectionDocument, type RouteDocument, type RoutePoint,
} from '@/services/routeDocument';
import {
  clearTrial, deleteUserRoute, discardRecording, listRoutes, readLocalFile, readRoute, runRoute,
  saveUserRoute, startRecording, stopRouteSession, useRouteStudioStore, type RouteSummary,
} from '@/services/routeStudioService';
import { RouteMapCanvas, type MapCanvasHandle, type MapInfo } from './RouteMapCanvas';

const field = 'w-full rounded-lg border border-border bg-bg-primary px-2.5 py-1.5 text-xs outline-none focus:border-accent';
const HISTORY_LIMIT = 100;

/** Key points name their key, so a recorded run of attacks or bell presses reads at a glance. */
function pointLabel(point: RoutePoint) {
  const label = actionMeta(point.action).label;
  if (point.action === 'macro') {
    const mName = MACROS.find(([id]) => id === point.params?.id)?.[1] ?? String(point.params?.id ?? '');
    const h = point.params?.id === 'zhaoxi_attack' && typeof point.params?.facing_heading === 'number'
      ? ` ${Math.round(point.params.facing_heading)}°` : '';
    return `${label} · ${mName}${h}`;
  }
  if (point.action === 'fishing' && typeof point.params?.facing_heading === 'number') {
    return `${label} · ${Math.round(point.params.facing_heading)}°`;
  }
  if (point.action !== 'key') return label;
  const key = ROUTE_KEYS.find(([id]) => id === point.params?.key)?.[1] ?? String(point.params?.key ?? '');
  const hold = Number(point.params?.hold);
  const times = Number(point.params?.times);
  const h = point.params?.key === 'attack' && typeof point.params?.facing_heading === 'number'
    ? ` ${Math.round(point.params.facing_heading)}°` : '';
  return `${label} · ${key}${h}${hold >= 0.5 ? ` 长按 ${hold} 秒` : ''}${times > 1 ? ` ×${times}` : ''}`;
}

const HOTKEY_LEGEND = [['F6', '关键点'], ['F7', '采集'], ['F8', '等待'], ['F9', '钓鱼'], ['F10', '下落攻击']] as const;
const STATE_TEXT: Record<string, string> = {
  starting: '正在准备定位…',
  calibrating: '请按 M 打开大地图定位起点',
  calibrated: '大地图已定位，请按 M 返回游戏',
  recording: '正在录制',
  paused: '已暂停：菜单或加载中',
  lost: '小地图定位不上',
  waiting_focus: '已暂停：切回游戏继续',
  finished: '录制结束',
  failed: '录制失败',
};
const OUTCOME_BADGE: Record<string, { label: string; cls: string }> = {
  turnback: { label: '折返', cls: 'bg-warning/15 text-warning' },
  recovered: { label: '脱困', cls: 'bg-warning/20 text-warning' },
  action_missed: { label: '漏采', cls: 'bg-warning/15 text-warning' },
  failed: { label: '中断', cls: 'bg-error/15 text-error' },
};

/** Unexplored map areas are pure black in the reference image; let the page show through. */
function withoutVoid(image: HTMLImageElement) {
  const canvas = document.createElement('canvas');
  canvas.width = image.naturalWidth;
  canvas.height = image.naturalHeight;
  const context = canvas.getContext('2d', { willReadFrequently: true })!;
  context.drawImage(image, 0, 0);
  const pixels = context.getImageData(0, 0, canvas.width, canvas.height);
  for (let index = 0; index < pixels.data.length; index += 4) {
    if (pixels.data[index] <= 2 && pixels.data[index+1] <= 2 && pixels.data[index+2] <= 2) pixels.data[index+3] = 0;
  }
  context.putImageData(pixels, 0, 0);
  return canvas;
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <label className="block"><span className="mb-1 block text-xs text-text-secondary">{label}</span>{children}</label>;
}

function NumberField({ label, value, min, max, step = 1, onChange }: {
  label: string; value: number; min: number; max: number; step?: number; onChange: (value: number) => void;
}) {
  return <Field label={label}><input type="number" className={field} value={value} min={min} max={max} step={step}
    onChange={(event) => {
      const next = Number(event.target.value);
      if (Number.isFinite(next)) onChange(Math.min(max, Math.max(min, next)));
    }} /></Field>;
}

function CheckpointEditor({ value, known, map, fallback, onChange }: {
  value?: Checkpoint; known: Checkpoint[]; map: string; fallback: { x: number; y: number }; onChange: (value?: Checkpoint) => void;
}) {
  const custom = Boolean(value && !known.some((item) => item.name === value.name));
  const groups = useMemo(() => {
    const byRegion = new Map<string, Checkpoint[]>();
    for (const item of known) {
      const key = item.region || item.province || '默认区域';
      byRegion.set(key, [...(byRegion.get(key) ?? []), item]);
    }
    return [...byRegion.entries()];
  }, [known]);

  return <div className="space-y-2">
    <select className={field} value={custom ? '__custom' : value?.name ?? ''} onChange={(event) => {
      const name = event.target.value;
      if (!name) return onChange(undefined);
      if (name === '__custom') return onChange({ name: '', province: '', region: '', ...fallback });
      onChange(known.find((item) => item.name === name));
    }}>
      <option value="">不传送</option>
      {groups.length <= 1
        ? known.map((item) => <option key={item.name} value={item.name}>{item.name}</option>)
        : groups.map(([region, items]) => <optgroup key={region} label={region}>
          {items.map((item) => <option key={item.name} value={item.name}>{item.name}</option>)}
        </optgroup>)}
      <option value="__custom">其他自定义传送点…</option>
    </select>
    {custom && value && <div className="grid grid-cols-2 gap-2 rounded-lg bg-bg-tertiary p-2">
      <div className="col-span-2"><Field label="传送点名称（与游戏一致）"><input className={field} value={value.name} onChange={(event) => onChange({ ...value, name: event.target.value })} /></Field></div>
      <Field label="大区"><input className={field} value={value.province} placeholder={map === 'home' ? '家园' : '如 心愿原野'} onChange={(event) => onChange({ ...value, province: event.target.value })} /></Field>
      <Field label="区域"><input className={field} value={value.region} placeholder="地图左上角的区域名" onChange={(event) => onChange({ ...value, region: event.target.value })} /></Field>
      <NumberField label="地图 X" value={value.x} min={0} max={20000} step={0.1} onChange={(x) => onChange({ ...value, x })} />
      <NumberField label="地图 Y" value={value.y} min={0} max={20000} step={0.1} onChange={(y) => onChange({ ...value, y })} />
    </div>}
  </div>;
}

function PointInspector({ point, index, known, map, readOnly, onChange, onRemove, onWalk, onRun }: {
  point: RoutePoint; index: number; known: Checkpoint[]; map: string; readOnly: boolean;
  onChange: (point: RoutePoint) => void; onRemove: () => void; onWalk: () => void; onRun: () => void;
}) {
  const params = point.params ?? {};
  const set = (key: string, value: unknown) => {
    const nextParams = { ...params, [key]: value };
    if (value === undefined) delete nextParams[key];
    onChange({ ...point, params: nextParams });
  };
  const action = point.action ?? '';
  const showAttackHeading = (action === 'macro' && params.id === 'zhaoxi_attack') || (action === 'key' && params.key === 'attack');

  return <fieldset disabled={readOnly} className="space-y-3 p-3">
    <div className="flex items-center gap-1.5">
      <h3 className="mr-auto text-sm font-semibold">第 {index+1} 点</h3>
      <button type="button" onClick={onWalk} title="从这个点开始试走（只移动）" className="rounded-lg p-1.5 text-text-secondary hover:bg-bg-hover"><Footprints className="h-4 w-4" /></button>
      <button type="button" onClick={onRun} title="从这个点开始试跑（含动作）" className="rounded-lg p-1.5 text-text-secondary hover:bg-bg-hover"><CirclePlay className="h-4 w-4" /></button>
      <button type="button" onClick={onRemove} title="删除这个点（Delete）" className="rounded-lg p-1.5 text-text-secondary hover:bg-bg-hover hover:text-error"><Trash2 className="h-4 w-4" /></button>
    </div>
    <div>
      <span className="mb-1.5 block text-xs text-text-secondary">到这里后</span>
      <div className="flex flex-wrap gap-1.5">
        {ACTIONS.map((item) => <button key={item.id} type="button" title={item.hint} onClick={() => action !== item.id && onChange(withAction(point, item.id))}
          className={clsx('inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-xs transition-colors',
            action === item.id ? 'border-accent bg-accent/10 font-medium text-accent' : 'border-border text-text-secondary hover:bg-bg-hover')}>
          {item.color && <span className="h-2 w-2 rounded-full" style={{ background: item.color }} />}{item.label}
        </button>)}
      </div>
      <p className="mt-1.5 text-xs leading-5 text-text-muted">{actionMeta(action).hint}</p>
    </div>
    {action === 'wait' && <NumberField label="等待秒数" value={Number(params.seconds ?? 3)} min={0} max={60} step={0.5} onChange={(value) => set('seconds', value)} />}
    {action === 'insect' && <NumberField label="捕虫只数" value={Number(params.count ?? 1)} min={1} max={20} onChange={(value) => set('count', value)} />}
    {action === 'fishing' && <div className="grid grid-cols-2 gap-2">
      <NumberField label="最多抛竿" value={Number(params.cast_count ?? 3)} min={1} max={20} onChange={(value) => set('cast_count', value)} />
      <NumberField label="水面朝向（°）" value={Number(params.facing_heading ?? 0)} min={0} max={359} onChange={(value) => set('facing_heading', value)} />
    </div>}
    {action === 'macro' && <div className="space-y-2">
      <Field label="连招"><select className={field} value={String(params.id ?? '')} onChange={(event) => {
        const nextId = event.target.value;
        const nextParams: Record<string, unknown> = { ...params, id: nextId };
        if (nextId === 'starsea_fall') delete nextParams.facing_heading;
        else if (nextId === 'zhaoxi_attack' && nextParams.facing_heading === undefined) nextParams.facing_heading = 0;
        onChange({ ...point, params: nextParams });
      }}>
        {MACROS.map(([id, label]) => <option key={id} value={id}>{label}</option>)}
      </select></Field>
    </div>}

    {action === 'key' && <div className="grid grid-cols-2 gap-2">
      <div className="col-span-2"><Field label="按键"><select className={field} value={String(params.key ?? '')} onChange={(event) => {
        const nextKey = event.target.value;
        const nextParams: Record<string, unknown> = { ...params, key: nextKey };
        if (nextKey !== 'attack') delete nextParams.facing_heading;
        else if (nextParams.facing_heading === undefined) nextParams.facing_heading = 0;
        onChange({ ...point, params: nextParams });
      }}>
        {ROUTE_KEYS.map(([id, label]) => <option key={id} value={id}>{label}</option>)}
      </select></Field></div>
      <NumberField label="按住秒数" value={Number(params.hold ?? 0.1)} min={0} max={10} step={0.1} onChange={(value) => set('hold', value)} />
      <NumberField label="次数" value={Number(params.times ?? 1)} min={1} max={50} onChange={(value) => set('times', Math.round(value))} />
    </div>}
    {showAttackHeading && <div className="flex items-end gap-2">
      <div className="flex-1">
        <NumberField label="攻击朝向（° 可在地图拖动箭头）" value={Number(params.facing_heading ?? 0)} min={0} max={359}
          onChange={(value) => set('facing_heading', value)} />
      </div>
      {params.facing_heading !== undefined && <button type="button" onClick={() => set('facing_heading', undefined)}
        className="rounded-lg border border-border px-2 py-1.5 text-xs text-text-secondary hover:bg-bg-hover">不转向</button>}
    </div>}
    {action === 'teleport' && <Field label="传送到">
      <CheckpointEditor value={params.checkpoint as Checkpoint | undefined} known={known} map={map} fallback={point}
        onChange={(value) => set('checkpoint', value)} />
    </Field>}
    <div className="grid grid-cols-2 gap-2">
      <NumberField label="到达范围" value={point.radius} min={0.2} max={10} step={0.1} onChange={(radius) => onChange({ ...point, radius })} />
      <button type="button" role="switch" aria-checked={point.jump} onClick={() => onChange({ ...point, jump: !point.jump })}
        className="flex items-center justify-between self-end rounded-lg border border-border px-2.5 py-1.5 text-xs">跳跃通过<SwitchTrack checked={point.jump} compact /></button>
      <NumberField label="X" value={point.x} min={0} max={20000} step={0.1} onChange={(x) => onChange({ ...point, x })} />
      <NumberField label="Y" value={point.y} min={0} max={20000} step={0.1} onChange={(y) => onChange({ ...point, y })} />
    </div>
  </fieldset>;
}

function BatchInspector({ count, readOnly, onSetRadius, onSetJump, onDelete, onClear }: {
  count: number; readOnly: boolean;
  onSetRadius: (radius: number) => void;
  onSetJump: (jump: boolean) => void;
  onDelete: () => void;
  onClear: () => void;
}) {
  const [batchRadius, setBatchRadius] = useState(1.0);
  return <fieldset disabled={readOnly} className="space-y-2.5 p-3">
    <div className="flex items-center justify-between">
      <h3 className="text-sm font-semibold">已选 {count} 个点位</h3>
      <button type="button" onClick={onClear} className="text-xs text-text-muted hover:text-text-primary">取消多选</button>
    </div>
    <p className="text-xs text-text-muted">在地图上拖动任意选中点可整体平移全部所选点。</p>
    <div className="flex items-end gap-2">
      <div className="flex-1">
        <NumberField label="批量到达范围" value={batchRadius} min={0.2} max={10} step={0.1} onChange={setBatchRadius} />
      </div>
      <button type="button" onClick={() => onSetRadius(batchRadius)} className={secondaryButton}>应用范围</button>
    </div>
    <div className="grid grid-cols-3 gap-1.5">
      <button type="button" onClick={() => onSetJump(true)} className={clsx(secondaryButton, 'justify-center')}>设为跳跃</button>
      <button type="button" onClick={() => onSetJump(false)} className={clsx(secondaryButton, 'justify-center')}>取消跳跃</button>
      <button type="button" onClick={onDelete} className="inline-flex items-center justify-center gap-1 rounded-lg border border-error/40 bg-error/10 px-2.5 py-1.5 text-xs font-medium text-error hover:bg-error/20"><Trash2 className="h-3.5 w-3.5" />删除所选</button>
    </div>
  </fieldset>;
}

export function RouteEditorPage({ onClose }: { onClose: () => void }) {
  const [maps, setMaps] = useState<MapInfo[]>([]);
  const [catalogCheckpoints, setCatalogCheckpoints] = useState<Record<string, Checkpoint[]>>({});
  const [image, setImage] = useState<HTMLCanvasElement | null>(null);
  const [routes, setRoutes] = useState<{ user: RouteSummary[]; builtin: RouteSummary[] }>({ user: [], builtin: [] });
  const [route, setRoute] = useState<RouteDocument | null>(null);
  const [collection, setCollection] = useState<RouteCollectionDocument | null>(null);
  const [readOnly, setReadOnly] = useState(false);
  const [selected, setSelected] = useState<number | null>(null);
  const [selectedSet, setSelectedSet] = useState<Set<number>>(new Set());
  const [tool, setTool] = useState<'select' | 'add' | 'box'>('select');
  const [filter, setFilter] = useState<'all' | 'actions'>('all');
  const [saved, setSaved] = useState(true);
  const [recordMap, setRecordMap] = useState('miraland');
  const [recordTeleport, setRecordTeleport] = useState<Checkpoint | undefined>(undefined);
  const [tolerance, setTolerance] = useState(1);
  const [showBuiltin, setShowBuiltin] = useState(false);
  const [showTeleports, setShowTeleports] = useState(true);
  const [showRouteConfig, setShowRouteConfig] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<RouteSummary | null>(null);
  const [history, setHistory] = useState<{ past: RouteDocument[]; future: RouteDocument[] }>({ past: [], future: [] });
  const canvas = useRef<MapCanvasHandle>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const dragStart = useRef<RouteDocument | null>(null);
  const { status, recording, finished, trial } = useRouteStudioStore();
  const busy = status !== 'idle';

  const map = maps.find((item) => item.id === (route?.map ?? recording?.map ?? finished?.map ?? recordMap)) ?? null;
  const knownCheckpoints = useMemo(() => {
    const items = new Map<string, Checkpoint>();
    for (const cp of catalogCheckpoints[map?.id ?? ''] ?? []) items.set(cp.name, cp);
    for (const summary of routes.builtin) if (summary.teleport && summary.map === map?.id) items.set(summary.teleport.name, summary.teleport);
    for (const summary of routes.user) if (summary.teleport && summary.map === map?.id) items.set(summary.teleport.name, summary.teleport);
    return [...items.values()];
  }, [catalogCheckpoints, routes.builtin, routes.user, map?.id]);

  const refresh = useCallback(() => listRoutes().then(setRoutes).catch((error) => toast.error(String(error))), []);
  useEffect(() => {
    if (!isTauri()) return;
    void refresh();
    readLocalFile('resource/navigation/maps.json').then((text) => {
      const items = JSON.parse(text).maps as { id: string; title: string; width: number; height: number; image: string }[];
      setMaps(items.map((item) => ({ id: item.id, title: item.title, width: item.width, height: item.height,
        image: `resource/navigation/${item.width > 8192 ? item.image.replace('.png', '_bigmap.png') : item.image}` })));
    }).catch((error) => toast.error(String(error)));
    readLocalFile('resource/navigation/checkpoints.json').then((text) => {
      const parsed = JSON.parse(text) as { maps?: Record<string, Checkpoint[]> };
      if (parsed?.maps) setCatalogCheckpoints(parsed.maps);
    }).catch(() => {});
  }, [refresh]);

  const imagePath = map?.image;
  useEffect(() => {
    if (!imagePath) return;
    let cancelled = false;
    setImage(null);
    invoke<string>('read_local_file_base64', { filename: imagePath }).then((data) => {
      const next = new Image();
      next.onload = () => { if (!cancelled) setImage(withoutVoid(next)); };
      next.src = `data:image/png;base64,${data}`;
    }).catch((error) => toast.error(String(error)));
    return () => { cancelled = true; };
  }, [imagePath]);

  const commit = useCallback((next: RouteDocument, previous?: RouteDocument) => {
    setRoute((current) => {
      const base = previous ?? current;
      if (base) setHistory((value) => ({ past: [...value.past, base].slice(-HISTORY_LIMIT), future: [] }));
      return next;
    });
    setSaved(false);
  }, []);

  useEffect(() => {
    const activeDoc = collection ?? route;
    if (!activeDoc || saved || readOnly) return;
    const timer = window.setTimeout(() => {
      saveUserRoute(activeDoc).then(() => { setSaved(true); void refresh(); }).catch((error) => toast.error(String(error)));
    }, 500);
    return () => window.clearTimeout(timer);
  }, [route, collection, saved, readOnly, refresh]);

  const open = useCallback(async (summary: RouteSummary) => {
    try {
      const document = await readRoute(summary);
      clearTrial();
      if ('kind' in document && document.kind === 'route_collection') {
        setCollection(document);
        setRoute(null);
        setReadOnly(summary.builtin);
        setSelected(null);
        setSelectedSet(new Set());
        setSaved(true);
        setHistory({ past: [], future: [] });
        return;
      }
      setCollection(null);
      setRoute(document as RouteDocument);
      setReadOnly(summary.builtin);
      setSelected(null);
      setSelectedSet(new Set());
      setSaved(true);
      setHistory({ past: [], future: [] });
      setTimeout(() => canvas.current?.fit(), 60);
    } catch (error) {
      toast.error(`无法打开路线：${String(error)}`);
    }
  }, []);

  const undo = useCallback(() => {
    setHistory((value) => {
      const previous = value.past[value.past.length-1];
      if (!previous || !route) return value;
      setRoute(previous);
      setSaved(false);
      setSelected(null);
      setSelectedSet(new Set());
      return { past: value.past.slice(0, -1), future: [route, ...value.future] };
    });
  }, [route]);
  const redo = useCallback(() => {
    setHistory((value) => {
      const next = value.future[0];
      if (!next || !route) return value;
      setRoute(next);
      setSaved(false);
      setSelected(null);
      setSelectedSet(new Set());
      return { past: [...value.past, route], future: value.future.slice(1) };
    });
  }, [route]);

  const setPoints = (points: RoutePoint[]) => route && commit({ ...route, points });
  const removePoint = useCallback((index: number) => {
    if (!route) return;
    commit({ ...route, points: route.points.filter((_, position) => position !== index) });
    setSelected(route.points.length > 1 ? Math.max(0, index-1) : null);
    setSelectedSet(new Set());
  }, [route, commit]);
  const removeBatch = useCallback(() => {
    if (!route || !selectedSet.size) return;
    commit({ ...route, points: route.points.filter((_, idx) => !selectedSet.has(idx)) });
    setSelected(null);
    setSelectedSet(new Set());
  }, [route, selectedSet, commit]);
  const movePoint = (index: number, delta: number) => {
    if (!route) return;
    const points = [...route.points];
    const target = index+delta;
    if (target < 0 || target >= points.length) return;
    [points[index], points[target]] = [points[target], points[index]];
    setPoints(points);
    setSelected(target);
  };
  const reversePoints = () => {
    if (!route || route.points.length < 2 || readOnly) return;
    setPoints([...route.points].reverse());
    setSelected(null);
    setSelectedSet(new Set());
    toast.success('已反转路线点位顺序');
  };

  useEffect(() => {
    const keydown = (event: KeyboardEvent) => {
      if (pendingDelete) return;
      const target = event.target as HTMLElement;
      if (['INPUT', 'SELECT', 'TEXTAREA'].includes(target.tagName)) return;
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z') {
        event.preventDefault();
        if (event.shiftKey) redo(); else undo();
      } else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'y') {
        event.preventDefault();
        redo();
      } else if ((event.key === 'Delete' || event.key === 'Backspace') && !readOnly) {
        if (selectedSet.size > 1) {
          event.preventDefault();
          removeBatch();
        } else if (selected !== null) {
          event.preventDefault();
          removePoint(selected);
        }
      } else if (event.key === 'Escape') {
        setSelected(null);
        setSelectedSet(new Set());
        setTool('select');
      } else if (event.key.toLowerCase() === 'a' && !event.ctrlKey) {
        setTool((value) => value === 'add' ? 'select' : 'add');
      } else if (event.key.toLowerCase() === 'b' && !event.ctrlKey) {
        setTool((value) => value === 'box' ? 'select' : 'box');
      }
    };
    window.addEventListener('keydown', keydown);
    return () => window.removeEventListener('keydown', keydown);
  }, [undo, redo, removePoint, removeBatch, selected, selectedSet, readOnly, pendingDelete]);

  const createRoute = (mapId = map?.id ?? recordMap) => {
    const title = maps.find((item) => item.id === mapId)?.title ?? '';
    const next = {
      ...newRoute(mapId, `${title}路线 ${routes.user.filter((r) => r.kind !== 'route_collection').length+1}`),
      ...(recordTeleport ? { teleport: recordTeleport } : {}),
    };
    clearTrial();
    setCollection(null);
    setRoute(next);
    setReadOnly(false);
    setSelected(null);
    setSelectedSet(new Set());
    setSaved(false);
    setHistory({ past: [], future: [] });
    setTool('add');
    return next;
  };
  const createCollection = () => {
    const next: RouteCollectionDocument = {
      schema_version: 1,
      kind: 'route_collection',
      id: `playlist_${Date.now().toString(36)}`,
      title: `路线清单 ${routes.user.filter((r) => r.kind === 'route_collection').length + 1}`,
      continue_on_failure: true,
      routes: [],
    };
    clearTrial();
    setRoute(null);
    setCollection(next);
    setReadOnly(false);
    setSelected(null);
    setSelectedSet(new Set());
    setSaved(false);
  };
  const saveAsCopy = () => {
    if (!route) return;
    setRoute({ ...route, id: newRouteId(), title: `${route.title} 副本` });
    setReadOnly(false);
    setSaved(false);
  };
  const remove = async (summary: RouteSummary) => {
    setPendingDelete(null);
    if (route?.id === summary.id) setRoute(null);
    if (collection?.id === summary.id) setCollection(null);
    await deleteUserRoute(summary.id).catch((error) => toast.error(String(error)));
    void refresh();
  };

  const importRouteText = (text: string) => {
    const raw = JSON.parse(text.replace(/^﻿/, ''));
    if (raw && raw.kind === 'route_collection' && Array.isArray(raw.routes)) {
      const next: RouteCollectionDocument = {
        schema_version: 1,
        kind: 'route_collection',
        id: `playlist_${Date.now().toString(36)}`,
        title: typeof raw.title === 'string' && raw.title ? raw.title : '导入的路线清单',
        continue_on_failure: raw.continue_on_failure !== false,
        routes: raw.routes,
      };
      clearTrial();
      setRoute(null);
      setCollection(next);
      setReadOnly(false);
      setSaved(false);
      toast.success('已导入路线清单');
      return;
    }
    const next = parseRouteFile(text);
    if (!maps.some((item) => item.id === next.map)) throw new Error('路线的地图不受支持。');
    clearTrial();
    setCollection(null);
    setRoute(next);
    setReadOnly(false);
    setSelected(null);
    setSelectedSet(new Set());
    setSaved(false);
    setHistory({ past: [], future: [] });
    setTimeout(() => canvas.current?.fit(), 60);
    toast.success('已导入，将保存到我的路线');
  };

  const importFile = async (file?: File) => {
    if (!file) return;
    try {
      importRouteText(await file.text());
    } catch (error) {
      toast.error(`导入失败：${error instanceof Error ? error.message : String(error)}`);
    }
  };
  const pasteRoute = async () => {
    try {
      const text = await navigator.clipboard.readText();
      if (!text.trim()) throw new Error('剪贴板为空。');
      importRouteText(text);
    } catch (error) {
      toast.error(`粘贴失败：${error instanceof Error ? error.message : String(error)}`);
    }
  };
  const exportRoute = async () => {
    const activeDoc = collection ?? route;
    if (!activeDoc) return;
    try {
      const { save } = await import('@tauri-apps/plugin-dialog');
      const path = await save({ defaultPath: `${activeDoc.title || activeDoc.id}.json`, filters: [{ name: '路线文件', extensions: ['json'] }] });
      if (!path) return;
      const { writeTextFile } = await import('@tauri-apps/plugin-fs');
      await writeTextFile(path, JSON.stringify(activeDoc, null, 2));
      toast.success('路线已导出');
    } catch (error) {
      toast.error(`导出失败：${String(error)}`);
    }
  };
  const copyRoute = async () => {
    const activeDoc = collection ?? route;
    if (!activeDoc) return;
    await navigator.clipboard.writeText(JSON.stringify(activeDoc, null, 2));
    toast.success('内容已复制，可以直接分享');
  };

  const issues = useMemo(() => route ? routeIssues(route, map ?? undefined) : [], [route, map]);
  const errors = issues.filter((issue) => issue.level === 'error');
  const preview = useMemo(
    () => finished ? pointsFromRecording(finished, tolerance, catalogCheckpoints[finished.map] ?? knownCheckpoints) : null,
    [finished, tolerance, catalogCheckpoints, knownCheckpoints],
  );

  useEffect(() => {
    if (finished) setTimeout(() => canvas.current?.fit(), 60);
  }, [finished]);

  const keepRecording = (mode: 'append' | 'insert' | 'replace' = 'append') => {
    if (!finished || !preview) return;
    const base = route && !readOnly && route.map === finished.map ? route : null;
    const target = base ?? newRoute(finished.map, `${maps.find((item) => item.id === finished.map)?.title ?? ''}路线 ${routes.user.length+1}`);
    if (!base) {
      setHistory({ past: [], future: [] });
      setReadOnly(false);
      setCollection(null);
    }
    const mapCps = catalogCheckpoints[finished.map] ?? knownCheckpoints;
    const presetTp = !base ? recordTeleport : undefined;
    const autoStartTp = (!target.teleport && !presetTp && !target.points.length && finished.samples.length)
      ? nearestCheckpoint(finished.samples[0][0], finished.samples[0][1], mapCps, 20)
      : undefined;
    const chosenTp = target.teleport ?? presetTp ?? autoStartTp;
    let nextPoints: RoutePoint[];
    if (base && mode === 'replace' && selectedSet.size > 0) {
      const sorted = [...selectedSet].sort((a, b) => a - b);
      const first = sorted[0];
      const keptBefore = target.points.slice(0, first);
      const keptAfter = target.points.filter((_, idx) => idx > first && !selectedSet.has(idx));
      nextPoints = [...keptBefore, ...preview, ...keptAfter];
    } else if (base && mode === 'insert' && selected !== null) {
      nextPoints = [...target.points.slice(0, selected + 1), ...preview, ...target.points.slice(selected + 1)];
    } else {
      nextPoints = [...target.points, ...preview];
    }
    commit({
      ...target,
      ...(chosenTp ? { teleport: chosenTp } : {}),
      points: nextPoints,
    }, base ?? undefined);
    if (!target.teleport && chosenTp) toast.success(`已设起始传送点：${chosenTp.name}`);
    setSelected(null);
    setSelectedSet(new Set());
    discardRecording();
    setTimeout(() => canvas.current?.fit(), 60);
  };

  const trialRun = async (mode: 'full' | 'movement', start = 0) => {
    if (collection) {
      if (!collection.routes.length) {
        toast.error('请先往清单里添加至少一条路线。');
        return;
      }
      try {
        const path = await saveUserRoute(collection);
        setSaved(true);
        await runRoute(path, mode, 0);
      } catch (error) {
        toast.error(String(error));
      }
      return;
    }
    if (!route) return;
    if (errors.length) {
      toast.error(errors[0].text);
      if (errors[0].index !== undefined) setSelected(errors[0].index);
      return;
    }
    try {
      const path = readOnly ? routes.builtin.find((item) => item.id === route.id)?.path : await saveUserRoute(route);
      if (!path) throw new Error('找不到路线文件。');
      setSaved(true);
      await runRoute(path, mode, start);
    } catch (error) {
      toast.error(String(error));
    }
  };

  const applySuggestions = () => {
    if (!route || readOnly || !trial?.suggestions?.length) return;
    const byIdx = new Map(trial.suggestions.map((s) => [s.index, s]));
    const nextPoints = route.points.map((pt, idx) => {
      const sug = byIdx.get(idx);
      return sug ? { ...pt, x: sug.x, y: sug.y, radius: sug.radius } : pt;
    });
    commit({ ...route, points: nextPoints });
    toast.success(`已应用 ${trial.suggestions.length} 处点位微调，可随时 Ctrl+Z 撤销`);
  };

  const addToTasks = async () => {
    const activeDoc = collection ?? route;
    if (!activeDoc || readOnly) return;
    const path = await saveUserRoute(activeDoc);
    const state = useAppStore.getState();
    const instanceId = state.activeInstanceId;
    if (!instanceId) return;
    state.addTaskToInstance(instanceId, { name: 'custom_route', option: ['CustomRouteFile'] });
    const tasks = useAppStore.getState().instances.find((item) => item.id === instanceId)?.selectedTasks ?? [];
    const task = tasks[tasks.length-1];
    if (task) {
      state.setTaskOptionValue(instanceId, task.id, 'CustomRouteFile', { type: 'input', values: { file: path } });
      state.renameTask(instanceId, task.id, activeDoc.title);
    }
    toast.success('已加入当前任务列表');
  };

  const point = route && selected !== null ? route.points[selected] : null;
  const rows = route ? route.points.map((item, index) => ({ item, index })).filter(({ item }) => filter === 'all' || item.action || item.jump) : [];
  const length = route ? routeLength(route.points) : 0;
  const activeTrialForRoute = trial && route && trial.route === route.id ? trial : null;
  const wpOutcomeMap = useMemo(() => {
    const mapOut = new Map<number, string>();
    for (const wp of activeTrialForRoute?.telemetry?.waypoints ?? []) {
      if (wp.outcome && wp.outcome !== 'clean') mapOut.set(wp.index, wp.outcome);
    }
    return mapOut;
  }, [activeTrialForRoute]);

  const activeDoc = collection ?? route;

  return <div className="flex min-h-0 flex-1 flex-col bg-bg-primary text-text-primary">
    <header className="flex items-center gap-2 border-b border-border bg-bg-secondary px-4 py-2">
      <button onClick={onClose} title="返回任务列表" aria-label="返回任务列表" className="rounded-lg p-2 hover:bg-bg-hover"><ArrowLeft className="h-4 w-4" /></button>
      <Route className="h-5 w-5 text-accent-secondary" />
      {activeDoc ? <input aria-label="路线名称" value={activeDoc.title ?? ''} disabled={readOnly}
        onChange={(event) => {
          if (collection) { setCollection({ ...collection, title: event.target.value }); setSaved(false); }
          else if (route) commit({ ...route, title: event.target.value });
        }}
        className="min-w-0 max-w-sm flex-1 rounded-lg border border-transparent bg-transparent px-2 py-1 text-base font-semibold outline-none hover:border-border focus:border-accent disabled:hover:border-transparent" />
        : <h1 className="text-lg font-semibold">路线编辑器</h1>}
      {activeDoc && !readOnly && <span className="flex items-center gap-1 text-xs text-text-muted">{saved ? <><Check className="h-3 w-3" />已保存</> : <><Loader2 className="h-3 w-3 animate-spin" />保存中</>}</span>}
      <span className="ml-auto flex items-center gap-1.5">
        {route && <>
          <button onClick={undo} disabled={!history.past.length} title="撤销（Ctrl+Z）" className="rounded-lg p-2 hover:bg-bg-hover disabled:opacity-30"><Undo2 className="h-4 w-4" /></button>
          <button onClick={redo} disabled={!history.future.length} title="重做（Ctrl+Y）" className="rounded-lg p-2 hover:bg-bg-hover disabled:opacity-30"><Redo2 className="h-4 w-4" /></button>
        </>}
        {activeDoc && <>
          <button onClick={() => void copyRoute()} title="复制内容，用来分享" className="rounded-lg p-2 hover:bg-bg-hover"><Copy className="h-4 w-4" /></button>
          {readOnly ? <button onClick={saveAsCopy} className={primaryButton}><Plus className="h-3.5 w-3.5" />另存为我的路线</button>
            : <button onClick={() => void addToTasks()} disabled={Boolean(route && errors.length)} className={secondaryButton}><ListPlus className="h-3.5 w-3.5" />加入任务</button>}
          <button onClick={() => void trialRun('movement')} disabled={busy} title="只走路，不执行动作" className={secondaryButton}><Footprints className="h-3.5 w-3.5" />试走</button>
          <button onClick={() => void trialRun('full')} disabled={busy} className={primaryButton}><CirclePlay className="h-3.5 w-3.5" />试跑</button>
        </>}
      </span>
    </header>

    <div className="flex min-h-0 flex-1">
      <aside className="flex w-56 shrink-0 flex-col border-r border-border bg-bg-secondary">
        <div className="grid grid-cols-2 gap-1.5 p-2.5">
          <button onClick={() => createRoute()} className={clsx(primaryButton, 'justify-center')}><Plus className="h-3.5 w-3.5" />新建路线</button>
          <button onClick={() => createCollection()} title="把多条路线打包按顺序运行" className={clsx(secondaryButton, 'justify-center')}><Layers className="h-3.5 w-3.5" />新建清单</button>
          <button onClick={() => fileInput.current?.click()} title="从路线文件导入" className={clsx(secondaryButton, 'justify-center')}><FileInput className="h-3.5 w-3.5" />导入</button>
          <button onClick={() => void pasteRoute()} title="从剪贴板直接粘贴路线 JSON" className={clsx(secondaryButton, 'justify-center')}><ClipboardPaste className="h-3.5 w-3.5" />粘贴</button>
          <button onClick={() => void exportRoute()} disabled={!activeDoc} title={activeDoc ? '把当前路线或清单保存为文件' : '先打开一条路线'} className={clsx(secondaryButton, 'col-span-2 justify-center')}><FileOutput className="h-3.5 w-3.5" />导出为文件</button>
          <input ref={fileInput} type="file" accept=".json" className="hidden" onChange={(event) => { void importFile(event.target.files?.[0]); event.target.value = ''; }} />
        </div>
        <div className="min-h-0 flex-1 overflow-auto px-2 pb-2">
          {!routes.user.length && <p className="px-2 py-6 text-center text-xs leading-5 text-text-muted">还没有路线。<br />录制一段，或新建后在地图上点出来。</p>}
          {routes.user.map((item) => <div key={item.id} className={clsx('group mb-1 flex items-center rounded-lg', activeDoc?.id === item.id ? 'bg-accent/10 text-accent' : 'hover:bg-bg-hover')}>
            <button onClick={() => void open(item)} className="min-w-0 flex-1 px-2.5 py-2 text-left">
              <span className="flex items-center gap-1 truncate text-xs font-medium">
                {item.kind === 'route_collection' && <Layers className="h-3 w-3 shrink-0 text-accent-secondary" />}
                <span className="truncate">{item.title || item.id}</span>
              </span>
              <span className="block text-[11px] text-text-muted">
                {item.kind === 'route_collection' ? `路线清单 · ${item.points} 条` : `${maps.find((value) => value.id === item.map)?.title ?? item.map} · ${item.points} 点`}
              </span>
            </button>
            <button onClick={() => void revealItemInDir(item.path)} title="打开所在文件夹" className="hidden p-1.5 text-text-muted hover:text-text-primary group-hover:block"><FolderOpen className="h-3.5 w-3.5" /></button>
            <button onClick={() => setPendingDelete(item)} title="删除" className="hidden p-1.5 pr-2 text-text-muted hover:text-error group-hover:block"><Trash2 className="h-3.5 w-3.5" /></button>
          </div>)}
          <button onClick={() => setShowBuiltin(!showBuiltin)} className="mt-2 flex w-full items-center justify-between rounded-lg px-2.5 py-1.5 text-xs text-text-secondary hover:bg-bg-hover">
            内置路线（参考）<span className="tabular-nums text-text-muted">{routes.builtin.length}</span>
          </button>
          {showBuiltin && routes.builtin.map((item) => <button key={item.id} onClick={() => void open(item)}
            className={clsx('mb-0.5 block w-full truncate rounded-lg px-2.5 py-1.5 text-left text-xs', activeDoc?.id === item.id ? 'bg-accent/10 text-accent' : 'text-text-secondary hover:bg-bg-hover')}>{item.title || item.id}</button>)}
        </div>
      </aside>

      <main className="relative min-w-0 flex-1">
        {collection ? <div className="flex h-full flex-col gap-4 overflow-auto p-6">
          <div className="max-w-2xl space-y-4">
            <div className="flex items-center justify-between rounded-xl border border-border bg-bg-secondary p-4">
              <div>
                <h2 className="text-sm font-semibold">多路线顺序清单</h2>
                <p className="mt-0.5 text-xs text-text-muted">按顺序自动执行多条路线；某条受阻时可自动恢复主界面并继续下一条。</p>
              </div>
              <button type="button" role="switch" aria-checked={collection.continue_on_failure}
                onClick={() => { setCollection({ ...collection, continue_on_failure: !collection.continue_on_failure }); setSaved(false); }}
                className="flex items-center gap-2 rounded-lg border border-border bg-bg-primary px-3 py-1.5 text-xs">
                单条失败继续下一条<SwitchTrack checked={collection.continue_on_failure} compact />
              </button>
            </div>
            <div className="rounded-xl border border-border bg-bg-secondary p-4">
              <div className="mb-3 flex items-center justify-between">
                <h3 className="text-xs font-semibold text-text-secondary">已包含路线（{collection.routes.length}）</h3>
                <select className="rounded-lg border border-border bg-bg-primary px-2.5 py-1 text-xs outline-none" value=""
                  onChange={(event) => {
                    const id = event.target.value;
                    if (!id) return;
                    const found = [...routes.user, ...routes.builtin].find((r) => r.id === id && r.kind !== 'route_collection');
                    if (!found) return;
                    if (collection.routes.some((r) => r.route === found.id)) {
                      toast.error('该路线已在清单中');
                      return;
                    }
                    setCollection({
                      ...collection,
                      routes: [...collection.routes, { route: found.id, title: found.title || found.id, ...(found.builtin ? {} : { file: found.path }) }],
                    });
                    setSaved(false);
                  }}>
                  <option value="">+ 添加一条路线到清单…</option>
                  {routes.user.filter((r) => r.kind !== 'route_collection').map((r) => <option key={r.id} value={r.id}>{r.title || r.id}</option>)}
                  {routes.builtin.map((r) => <option key={r.id} value={r.id}>[内置] {r.title || r.id}</option>)}
                </select>
              </div>
              {!collection.routes.length ? <p className="py-6 text-center text-xs text-text-muted">还没有添加路线，从右上角下拉框选择要串联执行的路线。</p>
                : <ol className="space-y-1.5">
                  {collection.routes.map((entry, idx) => <li key={entry.route} className="flex items-center gap-2 rounded-lg border border-border bg-bg-primary px-3 py-2 text-xs">
                    <span className="w-5 tabular-nums text-text-muted">{idx + 1}.</span>
                    <span className="flex-1 truncate font-medium">{entry.title || entry.route}</span>
                    <button onClick={() => {
                      if (idx === 0) return;
                      const list = [...collection.routes];
                      [list[idx - 1], list[idx]] = [list[idx], list[idx - 1]];
                      setCollection({ ...collection, routes: list });
                      setSaved(false);
                    }} className="rounded p-1 text-text-muted hover:bg-bg-hover hover:text-text-primary"><ArrowUp className="h-3.5 w-3.5" /></button>
                    <button onClick={() => {
                      if (idx + 1 >= collection.routes.length) return;
                      const list = [...collection.routes];
                      [list[idx + 1], list[idx]] = [list[idx], list[idx + 1]];
                      setCollection({ ...collection, routes: list });
                      setSaved(false);
                    }} className="rounded p-1 text-text-muted hover:bg-bg-hover hover:text-text-primary"><ArrowDown className="h-3.5 w-3.5" /></button>
                    <button onClick={() => {
                      setCollection({ ...collection, routes: collection.routes.filter((_, i) => i !== idx) });
                      setSaved(false);
                    }} className="rounded p-1 text-text-muted hover:bg-bg-hover hover:text-error"><Trash2 className="h-3.5 w-3.5" /></button>
                  </li>)}
                </ol>}
            </div>
          </div>
        </div> : <>

          <RouteMapCanvas ref={canvas} map={map} image={image} points={route?.points ?? []} preview={preview}
            samples={recording?.samples ?? finished?.samples}
            pose={recording?.pose ?? (status === 'running' ? trial?.pose : null)}
            teleports={showTeleports ? knownCheckpoints : undefined}
            startTeleport={route ? route.teleport : recordTeleport}
            selected={selected}
            selectedSet={selectedSet}
            activeTrialIndex={status === 'running' ? trial?.current_index ?? null : null}
            trialWaypoints={activeTrialForRoute?.telemetry?.waypoints ?? null}
            tool={tool} readOnly={readOnly || !route}
            onSelect={(index, toggle) => {
              if (index === null) {
                setSelected(null);
                setSelectedSet(new Set());
                return;
              }
              if (toggle) {
                setSelectedSet((prev) => {
                  const next = new Set(prev);
                  if (selected !== null) next.add(selected);
                  if (next.has(index)) next.delete(index); else next.add(index);
                  return next;
                });
                setSelected(index);
              } else {
                if (!selectedSet.has(index)) setSelectedSet(new Set());
                setSelected(index);
              }
            }}
            onBoxSelect={(indices, additive) => {
              setSelectedSet((prev) => {
                const next = additive ? new Set(prev) : new Set<number>();
                for (const idx of indices) next.add(idx);
                return next;
              });
              setSelected(indices[0] ?? null);
              if (tool === 'box') setTool('select');
            }}
            onMove={(index, x, y, done) => {
              if (!route) return;
              if (!dragStart.current) dragStart.current = route;
              const basePts = dragStart.current.points;
              const originPt = basePts[index];
              const dx = originPt ? Math.round((x - originPt.x) * 100) / 100 : 0;
              const dy = originPt ? Math.round((y - originPt.y) * 100) / 100 : 0;
              const movingBatch = selectedSet.size > 1 && selectedSet.has(index);
              const points = route.points.map((item, position) => {
                if (movingBatch && selectedSet.has(position) && basePts[position]) {
                  return { ...item, x: Math.round((basePts[position].x + dx) * 100) / 100, y: Math.round((basePts[position].y + dy) * 100) / 100 };
                }
                return position === index ? { ...item, x, y } : item;
              });
              if (done) {
                commit({ ...route, points }, dragStart.current);
                dragStart.current = null;
              } else {
                setRoute({ ...route, points });
              }
            }}
            onHeadingChange={(index, heading, done) => {
              if (!route) return;
              if (!dragStart.current) dragStart.current = route;
              const points = route.points.map((item, position) =>
                position === index ? { ...item, params: { ...(item.params ?? {}), facing_heading: heading } } : item);
              if (done) {
                commit({ ...route, points }, dragStart.current);
                dragStart.current = null;
              } else {
                setRoute({ ...route, points });
              }
            }}
            onInsert={(after, x, y) => {
              if (!route) return;
              const points = [...route.points];
              points.splice(after+1, 0, { x, y, radius: 1, jump: false });
              setPoints(points);
              setSelected(after+1);
            }}
            onPickTeleport={(cp) => {
              if (readOnly) return;
              if (!route) {
                setRecordTeleport(cp);
                toast.success(`已设录制起始传送点：${cp.name}`);
                return;
              }
              if (selected !== null && route.points[selected]?.action === 'teleport') {
                const points = route.points.map((pt, i) =>
                  i === selected ? { ...pt, x: cp.x, y: cp.y, params: { ...(pt.params ?? {}), checkpoint: cp } } : pt);
                commit({ ...route, points });
                toast.success(`已将第 ${selected + 1} 点传送设为：${cp.name}`);
              } else if (tool === 'add') {
                const after = selected ?? route.points.length - 1;
                const points = [...route.points];
                points.splice(after + 1, 0, { x: cp.x, y: cp.y, radius: 1, jump: false, action: 'teleport', params: { checkpoint: cp } });
                commit({ ...route, points });
                setSelected(after + 1);
                toast.success(`已插入传送点：${cp.name}`);
              } else {
                commit({ ...route, teleport: cp });
                toast.success(`已设起始传送点：${cp.name}`);
              }
            }} />

          <div className="absolute left-3 top-3 flex flex-col gap-1 rounded-xl border border-border bg-bg-secondary/95 p-1 shadow-sm">
            <button onClick={() => setTool('select')} title="选择和拖动（Shift+点击或框选可多选）" aria-pressed={tool === 'select'} className={clsx('rounded-lg p-2', tool === 'select' ? 'bg-accent/10 text-accent' : 'hover:bg-bg-hover')}><MousePointer2 className="h-4 w-4" /></button>
            <button onClick={() => setTool('add')} disabled={!route || readOnly} title="添加点（A）：点地图追加，点线段插入" aria-pressed={tool === 'add'} className={clsx('rounded-lg p-2 disabled:opacity-30', tool === 'add' ? 'bg-accent/10 text-accent' : 'hover:bg-bg-hover')}><Plus className="h-4 w-4" /></button>
            <button onClick={() => setTool('box')} disabled={!route} title="框选多个点位（B）" aria-pressed={tool === 'box'} className={clsx('rounded-lg p-2 disabled:opacity-30', tool === 'box' ? 'bg-accent/10 text-accent' : 'hover:bg-bg-hover')}><BoxSelect className="h-4 w-4" /></button>
            <span className="mx-1.5 my-0.5 h-px bg-border" />
            <button onClick={() => canvas.current?.zoom(1.4)} title="放大" className="rounded-lg p-2 hover:bg-bg-hover"><Plus className="h-4 w-4" /></button>
            <button onClick={() => canvas.current?.zoom(1/1.4)} title="缩小" className="rounded-lg p-2 hover:bg-bg-hover"><Minus className="h-4 w-4" /></button>
            <button onClick={() => canvas.current?.fit()} title="显示整条路线" className="rounded-lg p-2 hover:bg-bg-hover"><Maximize className="h-4 w-4" /></button>
            <span className="mx-1.5 my-0.5 h-px bg-border" />
            <button onClick={() => setShowTeleports((prev) => !prev)} title="显示流转之柱传送点（点击可设为起点传送或插入传送点）" aria-pressed={showTeleports}
              className={clsx('rounded-lg p-2', showTeleports ? 'bg-[#7c6cf0]/15 text-[#7c6cf0]' : 'hover:bg-bg-hover')}><MapPin className="h-4 w-4" /></button>
          </div>

          {readOnly && <p className="absolute left-1/2 top-3 -translate-x-1/2 rounded-full border border-border bg-bg-secondary/95 px-3 py-1 text-xs text-text-secondary shadow-sm">内置路线只能查看，另存后可以修改</p>}

          <div className="absolute bottom-4 left-1/2 w-max max-w-[calc(100%-2rem)] -translate-x-1/2">
            {status === 'recording' || status === 'starting' && recording ? <div className="rounded-2xl border border-border bg-bg-secondary/95 px-4 py-3 shadow-lg">
              <div className="flex items-center gap-3">
                <span className={clsx('h-2.5 w-2.5 rounded-full',
                  recording?.state === 'recording' ? 'animate-pulse bg-error'
                    : recording?.state === 'calibrated' ? 'animate-pulse bg-[#3f9d6a]'
                      : 'animate-pulse bg-warning')} />
                <div className="min-w-48">
                  <p className="text-sm font-medium" role="status">{STATE_TEXT[recording?.state ?? 'starting']}</p>
                  <p className="text-xs tabular-nums text-text-muted">{recording?.message || `${recording?.samples.length ?? 0} 个采样 · ${recording?.events.length ?? 0} 个标记`}</p>
                </div>
                <button onClick={stopRouteSession} className={primaryButton}><Square className="h-3.5 w-3.5" />结束录制</button>
              </div>
              <p className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-border pt-2 text-xs text-text-secondary">
                <span className="text-text-muted">跳跃、交互、攻击（含朝向）、摇铃和能力键自动记录</span>
                {HOTKEY_LEGEND.map(([key, label]) => <span key={key}><kbd className="rounded border border-border bg-bg-primary px-1 text-[11px]">{key}</kbd> {label}</span>)}
              </p>
            </div> : status === 'running' || status === 'starting' || status === 'stopping' ? <div className="flex items-center gap-3 rounded-2xl border border-border bg-bg-secondary/95 px-4 py-2.5 shadow-lg">
              <Loader2 className="h-4 w-4 animate-spin text-accent" />
              <div className="min-w-48">
                <p className="text-sm font-medium" role="status">
                  {status === 'stopping' ? '正在停止…' : trial?.message || (status === 'running' ? '路线运行中，切出游戏会暂停' : '正在准备…')}
                </p>
                {status === 'running' && trial && <p className="text-xs tabular-nums text-text-muted">
                  当前目标：第 {(trial.current_index ?? 0) + 1} 点（游戏内小地图同步显示路线遮罩）
                </p>}
              </div>
              <button onClick={stopRouteSession} disabled={status === 'stopping'} className={secondaryButton}><Square className="h-3.5 w-3.5" />停止</button>
            </div> : finished ? <div className="flex flex-wrap items-center gap-2.5 rounded-2xl border border-border bg-bg-secondary/95 px-4 py-3 shadow-lg">
              <div>
                <p className="text-sm font-medium">录到 {finished.samples.length} 个采样，生成 {preview?.length ?? 0} 个点</p>
                {finished.message && <p className="text-xs text-warning">{finished.message}</p>}
              </div>
              <label className="flex items-center gap-2 text-xs text-text-secondary">点位更密
                <input type="range" min={0.2} max={4} step={0.1} value={tolerance} onChange={(event) => setTolerance(Number(event.target.value))} className="w-24 accent-[var(--color-accent)]" aria-label="简化强度" />更简洁</label>
              <button onClick={discardRecording} className={secondaryButton}><X className="h-3.5 w-3.5" />放弃</button>
              {route && !readOnly && route.map === finished.map && selectedSet.size > 1 && <button onClick={() => keepRecording('replace')} className={secondaryButton}>替换所选 {selectedSet.size} 点</button>}
              {route && !readOnly && route.map === finished.map && selected !== null && <button onClick={() => keepRecording('insert')} className={secondaryButton}>插入第 {selected+1} 点后</button>}
              <button onClick={() => keepRecording('append')} className={primaryButton}><Check className="h-3.5 w-3.5" />{route && !readOnly && route.map === finished.map && route.points.length ? '接到路线末尾' : '保存为路线'}</button>
            </div> : <div className="flex flex-wrap items-center gap-2 rounded-2xl border border-border bg-bg-secondary/95 px-3 py-2 shadow-lg">
              <select aria-label="录制地图" value={route?.map ?? recordMap} disabled={Boolean(route?.points.length)} onChange={(event) => {
                setRecordMap(event.target.value);
                setRecordTeleport(undefined);
                if (route && !route.points.length) {
                  const next = { ...route, map: event.target.value };
                  delete next.teleport;
                  setRoute(next);
                }
              }} className="rounded-lg border border-border bg-bg-primary px-2 py-1.5 text-xs outline-none">
                {maps.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}
              </select>
              {route && !readOnly && route.points.length > 0 ? (
                <span className="px-1 text-xs text-text-secondary">续录片段（开始后请按 M 打开大地图定位起点）</span>
              ) : (
                <select aria-label="录制起点传送点"
                  value={(route ? route.teleport?.name : recordTeleport?.name) ?? ''}
                  disabled={readOnly}
                  onChange={(event) => {
                    const cp = knownCheckpoints.find((item) => item.name === event.target.value);
                    if (route) {
                      const next = { ...route };
                      if (cp) next.teleport = cp; else delete next.teleport;
                      commit(next);
                    } else {
                      setRecordTeleport(cp);
                    }
                  }}
                  className="max-w-56 rounded-lg border border-border bg-bg-primary px-2 py-1.5 text-xs outline-none">
                  <option value="">起点：非传送点（需开大地图定位）</option>
                  {knownCheckpoints.map((cp) => <option key={cp.name} value={cp.name}>起点传送：{cp.name}</option>)}
                </select>
              )}
              <button onClick={() => {
                const targetMap = route?.map ?? recordMap;
                const isSegment = Boolean(route && !readOnly && route.points.length > 0);
                const startTp = isSegment ? undefined : (route ? route.teleport : recordTeleport);
                void startRecording(targetMap, { teleport: startTp, require_bigmap: isSegment || !startTp });
              }} className="inline-flex items-center gap-2 rounded-lg bg-error px-3.5 py-1.5 text-xs font-medium text-white hover:opacity-90">
                <span className="h-2 w-2 rounded-full bg-white" />开始录制
              </button>
            </div>}
          </div>
        </>}
      </main>

      <aside className="flex w-80 shrink-0 flex-col border-l border-border bg-bg-secondary">
        {route ? <>
          <div className="space-y-2 border-b border-border p-3">
            <div className="flex items-center gap-3 text-xs text-text-secondary">
              <span><strong className="text-base font-semibold tabular-nums text-text-primary">{route.points.length}</strong> 个点</span>
              <span><strong className="text-base font-semibold tabular-nums text-text-primary">{route.points.filter((item) => item.action).length}</strong> 个动作</span>
              <span><strong className="text-base font-semibold tabular-nums text-text-primary">{Math.round(length)}</strong> 距离</span>
              {!readOnly && route.points.length >= 2 && <button onClick={reversePoints} title="反转整条路线的点位顺序" className="ml-auto rounded-lg p-1 text-text-muted hover:bg-bg-hover hover:text-text-primary"><ArrowUpDown className="h-3.5 w-3.5" /></button>}
            </div>
            <Field label="起始传送点">
              <fieldset disabled={readOnly}>
                <CheckpointEditor value={route.teleport} known={knownCheckpoints} map={route.map} fallback={route.points[0] ?? { x: 0, y: 0 }}
                  onChange={(teleport) => {
                    const next = { ...route };
                    if (teleport) next.teleport = teleport; else delete next.teleport;
                    commit(next);
                  }} />
              </fieldset>
            </Field>
            <div>
              <button type="button" onClick={() => setShowRouteConfig((v) => !v)} className="flex w-full items-center gap-1.5 text-xs text-text-secondary hover:text-text-primary">
                <SlidersHorizontal className="h-3.5 w-3.5" />
                <span>路线参数与采集计数</span>
                {showRouteConfig ? <ChevronDown className="ml-auto h-3.5 w-3.5" /> : <ChevronRight className="ml-auto h-3.5 w-3.5" />}
              </button>
              {showRouteConfig && <fieldset disabled={readOnly} className="mt-2 grid grid-cols-2 gap-2 rounded-lg bg-bg-tertiary p-2.5">
                <NumberField label="起点允许误差" value={route.start_radius ?? 6} min={0.5} max={30} step={0.5} onChange={(start_radius) => commit({ ...route, start_radius })} />
                <NumberField label="路线超时（秒）" value={route.timeout ?? 900} min={30} max={900} step={30} onChange={(timeout) => commit({ ...route, timeout })} />
                <Field label="目标素材名（可选）"><input className={field} value={route.material ?? ''} placeholder="如 苔之幽"
                  onChange={(e) => {
                    const next = { ...route };
                    if (e.target.value.trim()) next.material = e.target.value.trim(); else delete next.material;
                    commit(next);
                  }} /></Field>
                <NumberField label="采集达标上限（0不限）" value={route.capacity ?? 0} min={0} max={500} step={1}
                  onChange={(v) => {
                    const next = { ...route };
                    if (v > 0) next.capacity = Math.round(v); else delete next.capacity;
                    commit(next);
                  }} />
                <div className="col-span-2">
                  <Field label="伴生计入素材（逗号分隔）"><input className={field} value={(route.count_materials ?? []).join(', ')} placeholder="如 发光花蕊, 荧光草"
                    onChange={(e) => {
                      const items = e.target.value.split(/[,，]/).map((s) => s.trim()).filter(Boolean);
                      const next = { ...route };
                      if (items.length) next.count_materials = items; else delete next.count_materials;
                      commit(next);
                    }} /></Field>
                </div>
              </fieldset>}
            </div>
            {status === 'idle' && activeTrialForRoute?.telemetry && <div className="space-y-1.5 rounded-lg border border-border bg-bg-tertiary p-2.5 text-xs">
              <div className="flex items-center justify-between">
                <span className="font-medium text-text-primary">上次试跑诊断</span>
                <button type="button" onClick={clearTrial} className="text-text-muted hover:text-text-primary"><X className="h-3.5 w-3.5" /></button>
              </div>
              <p className="text-text-secondary">
                走过 {activeTrialForRoute.telemetry.visited_count}/{activeTrialForRoute.telemetry.point_count} 点
                {activeTrialForRoute.telemetry.turnback_waypoints > 0 && ` · 折返 ${activeTrialForRoute.telemetry.turnback_waypoints}`}
                {activeTrialForRoute.telemetry.recovered_waypoints > 0 && ` · 脱困 ${activeTrialForRoute.telemetry.recovered_waypoints}`}
                {activeTrialForRoute.telemetry.missed_actions > 0 && ` · 漏采 ${activeTrialForRoute.telemetry.missed_actions}`}
              </p>
              {!readOnly && activeTrialForRoute.suggestions.length > 0 && <button type="button" onClick={applySuggestions}
                className="inline-flex w-full items-center justify-center gap-1.5 rounded-lg bg-accent/15 px-2.5 py-1.5 font-medium text-accent hover:bg-accent/25">
                <Sparkles className="h-3.5 w-3.5" />一键应用微调建议（{activeTrialForRoute.suggestions.length} 处）
              </button>}
            </div>}
            {issues.length > 0 && <ul className="space-y-1">
              {issues.slice(0, 4).map((issue) => <li key={issue.text}>
                <button onClick={() => {
                  if (issue.index === undefined) return;
                  setSelected(issue.index);
                  const target = route.points[issue.index];
                  canvas.current?.focus(target.x, target.y);
                }} className={clsx('flex w-full items-start gap-1.5 rounded-lg px-2 py-1 text-left text-xs leading-5', issue.level === 'error' ? 'bg-error/10 text-error' : 'bg-warning/10 text-warning')}>
                  <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />{issue.text}
                </button>
              </li>)}
            </ul>}
          </div>
          {selectedSet.size > 1 ? <div className="border-b border-border">
            <BatchInspector count={selectedSet.size} readOnly={readOnly}
              onSetRadius={(radius) => setPoints(route.points.map((pt, i) => selectedSet.has(i) ? { ...pt, radius } : pt))}
              onSetJump={(jump) => setPoints(route.points.map((pt, i) => selectedSet.has(i) ? { ...pt, jump } : pt))}
              onDelete={removeBatch}
              onClear={() => setSelectedSet(new Set())} />
          </div> : point && selected !== null && <div className="border-b border-border">
            <PointInspector point={point} index={selected} known={knownCheckpoints} map={route.map} readOnly={readOnly}
              onChange={(next) => setPoints(route.points.map((item, index) => index === selected ? next : item))}
              onRemove={() => removePoint(selected)}
              onWalk={() => void trialRun('movement', selected)}
              onRun={() => void trialRun('full', selected)} />
          </div>}
          <div className="flex items-center justify-between px-3 pb-1 pt-2.5">
            <h3 className="text-xs font-semibold text-text-secondary">点位</h3>
            <Segmented label="点位筛选" value={filter} onChange={setFilter} options={[['all', '全部'], ['actions', '有动作']]} />
          </div>
          <ol className="min-h-0 flex-1 overflow-auto px-2 pb-2">
            {rows.map(({ item, index }) => {
              const badge = OUTCOME_BADGE[wpOutcomeMap.get(index) ?? ''];
              return <li key={index} className={clsx('group flex items-center rounded-lg text-xs', (selected === index || selectedSet.has(index)) ? 'bg-accent/10' : 'hover:bg-bg-hover')}>
                <button onClick={(ev) => {
                  if (ev.shiftKey) {
                    setSelectedSet((prev) => {
                      const next = new Set(prev);
                      if (selected !== null) next.add(selected);
                      if (next.has(index)) next.delete(index); else next.add(index);
                      return next;
                    });
                  } else {
                    setSelectedSet(new Set());
                  }
                  setSelected(index);
                  canvas.current?.focus(item.x, item.y);
                }} className="flex min-w-0 flex-1 items-center gap-2 px-2 py-1.5 text-left">
                  <span className="w-7 shrink-0 text-right tabular-nums text-text-muted">{index+1}</span>
                  <span className="h-2 w-2 shrink-0 rounded-full border border-border" style={item.action ? { background: actionMeta(item.action).color, borderColor: 'transparent' } : undefined} />
                  <span className={clsx('truncate', !item.action && 'text-text-secondary')}>{pointLabel(item)}{item.jump && <span className="ml-1 text-warning">· 跳跃</span>}</span>
                  {badge && <span className={clsx('ml-auto shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium', badge.cls)}>{badge.label}</span>}
                </button>
                {!readOnly && <span className="hidden pr-1 group-hover:flex">
                  <button onClick={() => movePoint(index, -1)} title="上移" className="rounded p-1 text-text-muted hover:text-text-primary"><ArrowUp className="h-3 w-3" /></button>
                  <button onClick={() => movePoint(index, 1)} title="下移" className="rounded p-1 text-text-muted hover:text-text-primary"><ArrowDown className="h-3 w-3" /></button>
                </span>}
              </li>;
            })}
          </ol>
        </> : <div className="flex flex-1 flex-col justify-center gap-4 p-6 text-sm leading-6 text-text-secondary">
          <h2 className="text-base font-semibold text-text-primary">三步做一条重复路线</h2>
          <p><strong className="text-text-primary">1. 录制</strong><br />选好地图与起点（若起点非传送点，开始录制后按 M 打开大地图定位起点），在游戏里走一遍。跳跃、交互、净化攻击（含朝向）、摇铃和能力键会自动记下，中途传送也会自动匹配流转之柱。</p>
          <p><strong className="text-text-primary">2. 调整</strong><br />在地图上点击流转之柱可设起点传送，拖动点位或朝向箭头，支持框选批量修改与片段重录。</p>
          <p><strong className="text-text-primary">3. 试跑与串联</strong><br />从任意点开始试走或试跑（小地图实时叠加路线遮罩），根据试跑诊断一键微调，或建路线清单多条串联执行。</p>
        </div>}
      </aside>
    </div>
    <ConfirmDialog open={pendingDelete !== null} title="删除路线"
      message={`确定删除「${pendingDelete?.title || pendingDelete?.id}」吗？删除后无法恢复。`}
      cancelText="取消" confirmText="删除" destructive
      onCancel={() => setPendingDelete(null)}
      onConfirm={() => { if (pendingDelete) void remove(pendingDelete); }} />
  </div>;
}
