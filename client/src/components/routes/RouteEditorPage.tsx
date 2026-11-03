import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AlertCircle, ArrowDown, ArrowLeft, ArrowUp, Check, CirclePlay, Copy, Download, FolderOpen, Footprints, ListPlus, Loader2, Maximize, Minus, MousePointer2, Plus, Redo2, Route, Square, Trash2, Undo2, Upload, X } from 'lucide-react';
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
  ACTIONS, MACROS, ROUTE_KEYS, actionMeta, newRoute, newRouteId, parseRouteFile, pointsFromRecording,
  routeIssues, routeLength, withAction, type Checkpoint, type RouteDocument, type RoutePoint,
} from '@/services/routeDocument';
import {
  deleteUserRoute, discardRecording, listRoutes, readLocalFile, readRoute, runRoute, saveUserRoute, startRecording,
  stopRouteSession, useRouteStudioStore, type RouteSummary,
} from '@/services/routeStudioService';
import { RouteMapCanvas, type MapCanvasHandle, type MapInfo } from './RouteMapCanvas';

const field = 'w-full rounded-lg border border-border bg-bg-primary px-2.5 py-1.5 text-xs outline-none focus:border-accent';
const HISTORY_LIMIT = 100;

const HOTKEY_LEGEND = [['F6', '关键点'], ['F7', '采集'], ['F8', '等待'], ['F9', '钓鱼'], ['F10', '下落攻击']] as const;
const STATE_TEXT: Record<string, string> = {
  starting: '正在准备定位…', recording: '正在录制', paused: '已暂停：菜单或加载中', lost: '小地图定位不上',
  waiting_focus: '已暂停：切回游戏继续', finished: '录制结束', failed: '录制失败',
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
  const custom = value && !known.some((item) => item.name === value.name);
  return <div className="space-y-2">
    <select className={field} value={custom ? '__custom' : value?.name ?? ''} onChange={(event) => {
      const name = event.target.value;
      if (!name) return onChange(undefined);
      if (name === '__custom') return onChange({ name: '', province: '', region: '', ...fallback });
      onChange(known.find((item) => item.name === name));
    }}>
      <option value="">不传送</option>
      {known.map((item) => <option key={item.name} value={item.name}>{item.name}</option>)}
      <option value="__custom">其他传送点…</option>
    </select>
    {custom && <div className="grid grid-cols-2 gap-2 rounded-lg bg-bg-tertiary p-2">
      <div className="col-span-2"><Field label="传送点名称（与游戏一致）"><input className={field} value={value.name} onChange={(event) => onChange({ ...value, name: event.target.value })} /></Field></div>
      <Field label="大区"><input className={field} value={value.province} placeholder={map === 'home' ? '家园' : '如 奇迹大陆'} onChange={(event) => onChange({ ...value, province: event.target.value })} /></Field>
      <Field label="区域"><input className={field} value={value.region} placeholder="地图左上角的区域名" onChange={(event) => onChange({ ...value, region: event.target.value })} /></Field>
      <NumberField label="地图 X" value={value.x} min={0} max={20000} step={0.1} onChange={(x) => onChange({ ...value, x })} />
      <NumberField label="地图 Y" value={value.y} min={0} max={20000} step={0.1} onChange={(y) => onChange({ ...value, y })} />
    </div>}
  </div>;
}

function PointInspector({ point, index, known, map, readOnly, onChange, onRemove, onRun }: {
  point: RoutePoint; index: number; known: Checkpoint[]; map: string; readOnly: boolean;
  onChange: (point: RoutePoint) => void; onRemove: () => void; onRun: () => void;
}) {
  const params = point.params ?? {};
  const set = (key: string, value: unknown) => onChange({ ...point, params: { ...params, [key]: value } });
  const action = point.action ?? '';
  return <fieldset disabled={readOnly} className="space-y-3 p-3">
    <div className="flex items-center gap-2">
      <h3 className="mr-auto text-sm font-semibold">第 {index+1} 点</h3>
      <button type="button" onClick={onRun} title="从这个点开始试跑" className="rounded-lg p-1.5 text-text-secondary hover:bg-bg-hover"><CirclePlay className="h-4 w-4" /></button>
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
    {action === 'macro' && <Field label="连招"><select className={field} value={String(params.id ?? '')} onChange={(event) => set('id', event.target.value)}>
      {MACROS.map(([id, label]) => <option key={id} value={id}>{label}</option>)}
    </select></Field>}
    {action === 'key' && <div className="grid grid-cols-2 gap-2">
      <div className="col-span-2"><Field label="按键"><select className={field} value={String(params.key ?? '')} onChange={(event) => set('key', event.target.value)}>
        {ROUTE_KEYS.map(([id, label]) => <option key={id} value={id}>{label}</option>)}
      </select></Field></div>
      <NumberField label="按住秒数" value={Number(params.hold ?? 0.1)} min={0} max={10} step={0.1} onChange={(value) => set('hold', value)} />
      <NumberField label="次数" value={Number(params.times ?? 1)} min={1} max={50} onChange={(value) => set('times', Math.round(value))} />
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

export function RouteEditorPage({ onClose }: { onClose: () => void }) {
  const [maps, setMaps] = useState<MapInfo[]>([]);
  const [image, setImage] = useState<HTMLCanvasElement | null>(null);
  const [routes, setRoutes] = useState<{ user: RouteSummary[]; builtin: RouteSummary[] }>({ user: [], builtin: [] });
  const [route, setRoute] = useState<RouteDocument | null>(null);
  const [readOnly, setReadOnly] = useState(false);
  const [selected, setSelected] = useState<number | null>(null);
  const [tool, setTool] = useState<'select' | 'add'>('select');
  const [filter, setFilter] = useState<'all' | 'actions'>('all');
  const [saved, setSaved] = useState(true);
  const [recordMap, setRecordMap] = useState('miraland');
  const [tolerance, setTolerance] = useState(1);
  const [showBuiltin, setShowBuiltin] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<RouteSummary | null>(null);
  const [history, setHistory] = useState<{ past: RouteDocument[]; future: RouteDocument[] }>({ past: [], future: [] });
  const canvas = useRef<MapCanvasHandle>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const dragStart = useRef<RouteDocument | null>(null);
  const { status, recording, finished } = useRouteStudioStore();
  const busy = status !== 'idle';

  const map = maps.find((item) => item.id === (route?.map ?? recording?.map ?? finished?.map ?? recordMap)) ?? null;
  const knownCheckpoints = useMemo(() => {
    const items = new Map<string, Checkpoint>();
    for (const summary of routes.builtin) if (summary.teleport && summary.map === map?.id) items.set(summary.teleport.name, summary.teleport);
    return [...items.values()];
  }, [routes.builtin, map?.id]);

  const refresh = useCallback(() => listRoutes().then(setRoutes).catch((error) => toast.error(String(error))), []);
  useEffect(() => {
    if (!isTauri()) return;
    void refresh();
    readLocalFile('resource/navigation/maps.json').then((text) => {
      const items = JSON.parse(text).maps as { id: string; title: string; width: number; height: number; image: string }[];
      setMaps(items.map((item) => ({ id: item.id, title: item.title, width: item.width, height: item.height,
        // The full Miraland map is too heavy for an image element; its quarter scale reads fine.
        image: `resource/navigation/${item.width > 8192 ? item.image.replace('.png', '_bigmap.png') : item.image}` })));
    }).catch((error) => toast.error(String(error)));
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

  // Every edit is one undo step and saves shortly after, so nothing needs a save button.
  const commit = useCallback((next: RouteDocument, previous?: RouteDocument) => {
    setRoute((current) => {
      const base = previous ?? current;
      if (base) setHistory((value) => ({ past: [...value.past, base].slice(-HISTORY_LIMIT), future: [] }));
      return next;
    });
    setSaved(false);
  }, []);

  useEffect(() => {
    if (!route || saved || readOnly) return;
    const timer = window.setTimeout(() => {
      saveUserRoute(route).then(() => { setSaved(true); void refresh(); }).catch((error) => toast.error(String(error)));
    }, 500);
    return () => window.clearTimeout(timer);
  }, [route, saved, readOnly, refresh]);

  const open = useCallback(async (summary: RouteSummary) => {
    try {
      const document = await readRoute(summary);
      setRoute(document);
      setReadOnly(summary.builtin);
      setSelected(null);
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
      return { past: [...value.past, route], future: value.future.slice(1) };
    });
  }, [route]);

  const setPoints = (points: RoutePoint[]) => route && commit({ ...route, points });
  const removePoint = useCallback((index: number) => {
    if (!route) return;
    commit({ ...route, points: route.points.filter((_, position) => position !== index) });
    setSelected(route.points.length > 1 ? Math.max(0, index-1) : null);
  }, [route, commit]);
  const movePoint = (index: number, delta: number) => {
    if (!route) return;
    const points = [...route.points];
    const target = index+delta;
    if (target < 0 || target >= points.length) return;
    [points[index], points[target]] = [points[target], points[index]];
    setPoints(points);
    setSelected(target);
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
      } else if ((event.key === 'Delete' || event.key === 'Backspace') && selected !== null && !readOnly) {
        event.preventDefault();
        removePoint(selected);
      } else if (event.key === 'Escape') {
        setSelected(null);
        setTool('select');
      } else if (event.key.toLowerCase() === 'a' && !event.ctrlKey) {
        setTool((value) => value === 'add' ? 'select' : 'add');
      }
    };
    window.addEventListener('keydown', keydown);
    return () => window.removeEventListener('keydown', keydown);
  }, [undo, redo, removePoint, selected, readOnly, pendingDelete]);

  const createRoute = (mapId = map?.id ?? recordMap) => {
    const title = maps.find((item) => item.id === mapId)?.title ?? '';
    const next = newRoute(mapId, `${title}路线 ${routes.user.length+1}`);
    setRoute(next);
    setReadOnly(false);
    setSelected(null);
    setSaved(false);
    setHistory({ past: [], future: [] });
    setTool('add');
    return next;
  };
  const saveAsCopy = () => {
    if (!route) return;
    setRoute({ ...route, id: newRouteId(), title: `${route.title} 副本` });
    setReadOnly(false);
    setSaved(false);
  };
  const remove = async (summary: RouteSummary) => {
    setPendingDelete(null);
    // Close it first, or a pending autosave could write the deleted route back.
    if (route?.id === summary.id) setRoute(null);
    await deleteUserRoute(summary.id).catch((error) => toast.error(String(error)));
    void refresh();
  };
  const importFile = async (file?: File) => {
    if (!file) return;
    try {
      const next = parseRouteFile(await file.text());
      if (!maps.some((item) => item.id === next.map)) throw new Error('路线的地图不受支持。');
      setRoute(next);
      setReadOnly(false);
      setSelected(null);
      setSaved(false);
      setHistory({ past: [], future: [] });
      setTimeout(() => canvas.current?.fit(), 60);
      toast.success('已导入，将保存到我的路线');
    } catch (error) {
      toast.error(`导入失败：${error instanceof Error ? error.message : String(error)}`);
    }
  };
  const copyRoute = async () => {
    if (!route) return;
    await navigator.clipboard.writeText(JSON.stringify(route, null, 2));
    toast.success('路线已复制，可以直接发给别人');
  };

  const issues = useMemo(() => route ? routeIssues(route, map ?? undefined) : [], [route, map]);
  const errors = issues.filter((issue) => issue.level === 'error');
  const preview = useMemo(() => finished ? pointsFromRecording(finished, tolerance) : null, [finished, tolerance]);

  // Show the whole recording as soon as it ends, wherever the view was.
  useEffect(() => {
    if (finished) setTimeout(() => canvas.current?.fit(), 60);
  }, [finished]);

  const keepRecording = () => {
    if (!finished || !preview) return;
    const base = route && !readOnly && route.map === finished.map ? route : null;
    const target = base ?? newRoute(finished.map, `${maps.find((item) => item.id === finished.map)?.title ?? ''}路线 ${routes.user.length+1}`);
    if (!base) {
      setHistory({ past: [], future: [] });
      setReadOnly(false);
    }
    commit({ ...target, points: [...target.points, ...preview] }, base ?? undefined);
    setSelected(null);
    discardRecording();
    setTimeout(() => canvas.current?.fit(), 60);
  };

  const trial = async (mode: 'full' | 'movement', start = 0) => {
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
  const addToTasks = async () => {
    if (!route || readOnly) return;
    const path = await saveUserRoute(route);
    const state = useAppStore.getState();
    const instanceId = state.activeInstanceId;
    if (!instanceId) return;
    state.addTaskToInstance(instanceId, { name: 'custom_route', option: ['CustomRouteFile'] });
    const tasks = useAppStore.getState().instances.find((item) => item.id === instanceId)?.selectedTasks ?? [];
    const task = tasks[tasks.length-1];
    if (task) {
      state.setTaskOptionValue(instanceId, task.id, 'CustomRouteFile', { type: 'input', values: { file: path } });
      state.renameTask(instanceId, task.id, route.title);
    }
    toast.success('已加入当前任务列表');
  };

  const point = route && selected !== null ? route.points[selected] : null;
  const rows = route ? route.points.map((item, index) => ({ item, index })).filter(({ item }) => filter === 'all' || item.action || item.jump) : [];
  const length = route ? routeLength(route.points) : 0;

  return <div className="flex min-h-0 flex-1 flex-col bg-bg-primary text-text-primary">
    <header className="flex items-center gap-2 border-b border-border bg-bg-secondary px-4 py-2">
      <button onClick={onClose} title="返回任务列表" aria-label="返回任务列表" className="rounded-lg p-2 hover:bg-bg-hover"><ArrowLeft className="h-4 w-4" /></button>
      <Route className="h-5 w-5 text-accent-secondary" />
      {route ? <input aria-label="路线名称" value={route.title ?? ''} disabled={readOnly} onChange={(event) => commit({ ...route, title: event.target.value })}
        className="min-w-0 max-w-sm flex-1 rounded-lg border border-transparent bg-transparent px-2 py-1 text-base font-semibold outline-none hover:border-border focus:border-accent disabled:hover:border-transparent" />
        : <h1 className="text-lg font-semibold">路线编辑器</h1>}
      {route && !readOnly && <span className="flex items-center gap-1 text-xs text-text-muted">{saved ? <><Check className="h-3 w-3" />已保存</> : <><Loader2 className="h-3 w-3 animate-spin" />保存中</>}</span>}
      <span className="ml-auto flex items-center gap-1.5">
        {route && <>
          <button onClick={undo} disabled={!history.past.length} title="撤销（Ctrl+Z）" className="rounded-lg p-2 hover:bg-bg-hover disabled:opacity-30"><Undo2 className="h-4 w-4" /></button>
          <button onClick={redo} disabled={!history.future.length} title="重做（Ctrl+Y）" className="rounded-lg p-2 hover:bg-bg-hover disabled:opacity-30"><Redo2 className="h-4 w-4" /></button>
          <button onClick={() => void copyRoute()} title="复制路线内容，用来分享" className="rounded-lg p-2 hover:bg-bg-hover"><Copy className="h-4 w-4" /></button>
          {readOnly ? <button onClick={saveAsCopy} className={primaryButton}><Plus className="h-3.5 w-3.5" />另存为我的路线</button> : <button onClick={() => void addToTasks()} disabled={Boolean(errors.length)} className={secondaryButton}><ListPlus className="h-3.5 w-3.5" />加入任务</button>}
          <button onClick={() => void trial('movement')} disabled={busy} title="只走路，不执行动作" className={secondaryButton}><Footprints className="h-3.5 w-3.5" />试走</button>
          <button onClick={() => void trial('full')} disabled={busy} className={primaryButton}><CirclePlay className="h-3.5 w-3.5" />试跑</button>
        </>}
      </span>
    </header>

    <div className="flex min-h-0 flex-1">
      <aside className="flex w-56 shrink-0 flex-col border-r border-border bg-bg-secondary">
        <div className="flex gap-1.5 p-2.5">
          <button onClick={() => createRoute()} className={clsx(primaryButton, 'flex-1 justify-center')}><Plus className="h-3.5 w-3.5" />新建路线</button>
          <button onClick={() => fileInput.current?.click()} title="导入路线文件" className={secondaryButton}><Upload className="h-3.5 w-3.5" /></button>
          <input ref={fileInput} type="file" accept=".json" className="hidden" onChange={(event) => { void importFile(event.target.files?.[0]); event.target.value = ''; }} />
        </div>
        <div className="min-h-0 flex-1 overflow-auto px-2 pb-2">
          {!routes.user.length && <p className="px-2 py-6 text-center text-xs leading-5 text-text-muted">还没有路线。<br />录制一段，或新建后在地图上点出来。</p>}
          {routes.user.map((item) => <div key={item.id} className={clsx('group mb-1 flex items-center rounded-lg', route?.id === item.id ? 'bg-accent/10 text-accent' : 'hover:bg-bg-hover')}>
            <button onClick={() => void open(item)} className="min-w-0 flex-1 px-2.5 py-2 text-left">
              <span className="block truncate text-xs font-medium">{item.title || item.id}</span>
              <span className="block text-[11px] text-text-muted">{maps.find((value) => value.id === item.map)?.title} · {item.points} 点</span>
            </button>
            <button onClick={() => void revealItemInDir(item.path)} title="打开所在文件夹" className="hidden p-1.5 text-text-muted hover:text-text-primary group-hover:block"><FolderOpen className="h-3.5 w-3.5" /></button>
            <button onClick={() => setPendingDelete(item)} title="删除"className="hidden p-1.5 pr-2 text-text-muted hover:text-error group-hover:block"><Trash2 className="h-3.5 w-3.5" /></button>
          </div>)}
          <button onClick={() => setShowBuiltin(!showBuiltin)} className="mt-2 flex w-full items-center justify-between rounded-lg px-2.5 py-1.5 text-xs text-text-secondary hover:bg-bg-hover">
            内置路线（参考）<span className="tabular-nums text-text-muted">{routes.builtin.length}</span>
          </button>
          {showBuiltin && routes.builtin.map((item) => <button key={item.id} onClick={() => void open(item)}
            className={clsx('mb-0.5 block w-full truncate rounded-lg px-2.5 py-1.5 text-left text-xs', route?.id === item.id ? 'bg-accent/10 text-accent' : 'text-text-secondary hover:bg-bg-hover')}>{item.title || item.id}</button>)}
        </div>
      </aside>

      <main className="relative min-w-0 flex-1">
        <RouteMapCanvas ref={canvas} map={map} image={image} points={route?.points ?? []} preview={preview} samples={recording?.samples ?? finished?.samples}
          pose={recording?.pose} selected={selected} tool={tool} readOnly={readOnly || !route}
          onSelect={setSelected}
          onMove={(index, x, y, done) => {
            if (!route) return;
            if (!dragStart.current) dragStart.current = route;
            const points = route.points.map((item, position) => position === index ? { ...item, x, y } : item);
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
          }} />

        <div className="absolute left-3 top-3 flex flex-col gap-1 rounded-xl border border-border bg-bg-secondary/95 p-1 shadow-sm">
          <button onClick={() => setTool('select')} title="选择和拖动" aria-pressed={tool === 'select'} className={clsx('rounded-lg p-2', tool === 'select' ? 'bg-accent/10 text-accent' : 'hover:bg-bg-hover')}><MousePointer2 className="h-4 w-4" /></button>
          <button onClick={() => setTool('add')} disabled={!route || readOnly} title="添加点（A）：点地图追加，点线段插入" aria-pressed={tool === 'add'} className={clsx('rounded-lg p-2 disabled:opacity-30', tool === 'add' ? 'bg-accent/10 text-accent' : 'hover:bg-bg-hover')}><Plus className="h-4 w-4" /></button>
          <span className="mx-1.5 my-0.5 h-px bg-border" />
          <button onClick={() => canvas.current?.zoom(1.4)} title="放大" className="rounded-lg p-2 hover:bg-bg-hover"><Plus className="h-4 w-4" /></button>
          <button onClick={() => canvas.current?.zoom(1/1.4)} title="缩小" className="rounded-lg p-2 hover:bg-bg-hover"><Minus className="h-4 w-4" /></button>
          <button onClick={() => canvas.current?.fit()} title="显示整条路线" className="rounded-lg p-2 hover:bg-bg-hover"><Maximize className="h-4 w-4" /></button>
        </div>

        {readOnly && <p className="absolute left-1/2 top-3 -translate-x-1/2 rounded-full border border-border bg-bg-secondary/95 px-3 py-1 text-xs text-text-secondary shadow-sm">内置路线只能查看，另存后可以修改</p>}

        <div className="absolute bottom-4 left-1/2 w-max max-w-[calc(100%-2rem)] -translate-x-1/2">
          {status === 'recording' || status === 'starting' && recording ? <div className="rounded-2xl border border-border bg-bg-secondary/95 px-4 py-3 shadow-lg">
            <div className="flex items-center gap-3">
              <span className={clsx('h-2.5 w-2.5 rounded-full', recording?.state === 'recording' ? 'animate-pulse bg-error' : 'bg-warning')} />
              <div className="min-w-40">
                <p className="text-sm font-medium" role="status">{STATE_TEXT[recording?.state ?? 'starting']}</p>
                <p className="text-xs tabular-nums text-text-muted">{recording?.message || `${recording?.samples.length ?? 0} 个采样 · ${recording?.events.length ?? 0} 个标记`}</p>
              </div>
              <button onClick={stopRouteSession} className={primaryButton}><Square className="h-3.5 w-3.5" />结束录制</button>
            </div>
            <p className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-border pt-2 text-xs text-text-secondary">
              <span className="text-text-muted">跳跃和交互自动记录</span>
              {HOTKEY_LEGEND.map(([key, label]) => <span key={key}><kbd className="rounded border border-border bg-bg-primary px-1 text-[11px]">{key}</kbd> {label}</span>)}
            </p>
          </div> : status === 'running' || status === 'starting' || status === 'stopping' ? <div className="flex items-center gap-3 rounded-2xl border border-border bg-bg-secondary/95 px-4 py-2.5 shadow-lg">
            <Loader2 className="h-4 w-4 animate-spin text-accent" />
            <p className="text-sm" role="status">{status === 'running' ? '路线运行中，切出游戏会暂停' : status === 'stopping' ? '正在停止…' : '正在准备…'}</p>
            <button onClick={stopRouteSession} disabled={status === 'stopping'} className={secondaryButton}><Square className="h-3.5 w-3.5" />停止</button>
          </div> : finished ? <div className="flex flex-wrap items-center gap-3 rounded-2xl border border-border bg-bg-secondary/95 px-4 py-3 shadow-lg">
            <div>
              <p className="text-sm font-medium">录到 {finished.samples.length} 个采样，生成 {preview?.length ?? 0} 个点</p>
              {finished.message && <p className="text-xs text-warning">{finished.message}</p>}
            </div>
            <label className="flex items-center gap-2 text-xs text-text-secondary">点位更密
              <input type="range" min={0.2} max={4} step={0.1} value={tolerance} onChange={(event) => setTolerance(Number(event.target.value))} className="w-28 accent-[var(--color-accent)]" aria-label="简化强度" />更简洁</label>
            <button onClick={discardRecording} className={secondaryButton}><X className="h-3.5 w-3.5" />放弃</button>
            <button onClick={keepRecording} className={primaryButton}><Check className="h-3.5 w-3.5" />{route && !readOnly && route.map === finished.map && route.points.length ? '接到路线后面' : '保存为路线'}</button>
          </div> : <div className="flex items-center gap-2 rounded-2xl border border-border bg-bg-secondary/95 px-3 py-2 shadow-lg">
            <select aria-label="录制地图" value={route?.map ?? recordMap} disabled={Boolean(route?.points.length)} onChange={(event) => {
              setRecordMap(event.target.value);
              if (route && !route.points.length) setRoute({ ...route, map: event.target.value });
            }} className="rounded-lg border border-border bg-bg-primary px-2 py-1.5 text-xs outline-none">
              {maps.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}
            </select>
            <button onClick={() => void startRecording(route?.map ?? recordMap)} className="inline-flex items-center gap-2 rounded-lg bg-error px-3.5 py-1.5 text-xs font-medium text-white hover:opacity-90">
              <span className="h-2 w-2 rounded-full bg-white" />开始录制
            </button>
          </div>}
        </div>
      </main>

      <aside className="flex w-80 shrink-0 flex-col border-l border-border bg-bg-secondary">
        {route ? <>
          <div className="space-y-2 border-b border-border p-3">
            <div className="flex items-baseline gap-4 text-xs text-text-secondary">
              <span><strong className="text-base font-semibold tabular-nums text-text-primary">{route.points.length}</strong> 个点</span>
              <span><strong className="text-base font-semibold tabular-nums text-text-primary">{route.points.filter((item) => item.action).length}</strong> 个动作</span>
              <span><strong className="text-base font-semibold tabular-nums text-text-primary">{Math.round(length)}</strong> 距离</span>
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
          {point && selected !== null && <div className="border-b border-border">
            <PointInspector point={point} index={selected} known={knownCheckpoints} map={route.map} readOnly={readOnly}
              onChange={(next) => setPoints(route.points.map((item, index) => index === selected ? next : item))}
              onRemove={() => removePoint(selected)} onRun={() => void trial('full', selected)} />
          </div>}
          <div className="flex items-center justify-between px-3 pb-1 pt-2.5">
            <h3 className="text-xs font-semibold text-text-secondary">点位</h3>
            <Segmented label="点位筛选" value={filter} onChange={setFilter} options={[['all', '全部'], ['actions', '有动作']]} />
          </div>
          <ol className="min-h-0 flex-1 overflow-auto px-2 pb-2">
            {rows.map(({ item, index }) => <li key={index} className={clsx('group flex items-center rounded-lg text-xs', selected === index ? 'bg-accent/10' : 'hover:bg-bg-hover')}>
              <button onClick={() => {
                setSelected(index);
                canvas.current?.focus(item.x, item.y);
              }} className="flex min-w-0 flex-1 items-center gap-2 px-2 py-1.5 text-left">
                <span className="w-7 shrink-0 text-right tabular-nums text-text-muted">{index+1}</span>
                <span className="h-2 w-2 shrink-0 rounded-full border border-border" style={item.action ? { background: actionMeta(item.action).color, borderColor: 'transparent' } : undefined} />
                <span className={clsx('truncate', !item.action && 'text-text-secondary')}>{actionMeta(item.action).label}{item.jump && <span className="ml-1 text-warning">· 跳跃</span>}</span>
              </button>
              {!readOnly && <span className="hidden pr-1 group-hover:flex">
                <button onClick={() => movePoint(index, -1)} title="上移" className="rounded p-1 text-text-muted hover:text-text-primary"><ArrowUp className="h-3 w-3" /></button>
                <button onClick={() => movePoint(index, 1)} title="下移" className="rounded p-1 text-text-muted hover:text-text-primary"><ArrowDown className="h-3 w-3" /></button>
              </span>}
            </li>)}
          </ol>
          <button onClick={() => {
            const blob = new Blob([JSON.stringify(route, null, 2)], { type: 'application/json' });
            const link = document.createElement('a');
            link.href = URL.createObjectURL(blob);
            link.download = `${route.title || route.id}.json`;
            link.click();
            URL.revokeObjectURL(link.href);
          }} className="flex items-center justify-center gap-1.5 border-t border-border py-2 text-xs text-text-secondary hover:bg-bg-hover"><Download className="h-3.5 w-3.5" />导出路线文件</button>
        </> : <div className="flex flex-1 flex-col justify-center gap-4 p-6 text-sm leading-6 text-text-secondary">
          <h2 className="text-base font-semibold text-text-primary">三步做一条路线</h2>
          <p><strong className="text-text-primary">1. 录制</strong><br />选好地图，点开始录制，然后在游戏里正常走一遍。跳跃和交互会自动记下来。</p>
          <p><strong className="text-text-primary">2. 调整</strong><br />在地图上拖动点位，点选后设置要做的动作，随时撤销。</p>
          <p><strong className="text-text-primary">3. 试跑</strong><br />可以从任意一点开始试跑，满意后加入任务列表。</p>
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
