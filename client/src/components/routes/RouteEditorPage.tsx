import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowDown, ArrowLeft, ArrowUp, BoxSelect, Check, CircleDashed, CirclePlay, Copy, FileOutput, Footprints, Keyboard,
  ListPlus, Loader2, MapPin, Maximize, Minus, MousePointer2, Plus, Redo2, Route, Sparkles, Sprout, Undo2, X,
} from 'lucide-react';
import clsx from 'clsx';
import { toast } from 'sonner';
import { invoke } from '@tauri-apps/api/core';
import { flushSaveConfig, useAppStore } from '@/stores/appStore';
import { isTauri } from '@/utils/paths';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { primaryButton, secondaryButton, Segmented } from '@/components/ui/FeaturePage';
import {
  actionMeta, nearestCheckpoint, newRoute, newRouteId, parseRouteFile, pointsFromRecording, routeIssues, routeLength, withAction,
  type Checkpoint, type RouteCollectionDocument, type RouteDocument, type RoutePoint,
} from '@/services/routeDocument';
import {
  clearTrial, deleteUserRoute, discardRecording, listRoutes, readLocalFile, readRoute, runRoute,
  saveUserRoute, startRecording, useRouteStudioStore,
  type GenerateGingerbreadResponse, type RouteSummary,
} from '@/services/routeStudioService';
import { RouteMapCanvas, type MapCanvasHandle, type MapContextTarget, type MapInfo, type MapTool } from './RouteMapCanvas';
import { RouteGeneratorModal } from './RouteGeneratorPanel';
import { RouteLibrary } from './RouteLibrary';
import { RouteCollectionEditor } from './RouteCollectionEditor';
import { RouteSessionBar } from './RouteSessionBar';
import { RouteElevationProfile } from './RouteElevationProfile';
import { BatchInspector, PointInspector, RouteSettings } from './RouteInspector';
import { ContextMenu, type MenuEntry } from './RouteContextMenu';
import { ACTION_HOTKEYS, EDITOR_SHORTCUTS, OUTCOME_BADGE, pointLabel, withoutVoid } from './routeEditorShared';
import {
  annotateSpawners, buildElevationProfile, loadMaterialCatalog, loadTerrainGrid, sampleTerrain,
  type SpawnerPoint, type TerrainGrid,
} from './routeTerrain';

const HISTORY_LIMIT = 100;
type OverlayCase = 'Full' | 'MinimapOnly' | 'Off';

function ToolButton({ active, disabled, title, onClick, children, tone }: {
  active?: boolean; disabled?: boolean; title: string; onClick: () => void; children: React.ReactNode; tone?: string;
}) {
  return <button type="button" onClick={onClick} disabled={disabled} title={title} aria-pressed={active}
    className={clsx('rounded-lg p-2 disabled:opacity-30', active ? (tone ?? 'bg-accent/10 text-accent') : 'hover:bg-bg-hover')}>
    {children}
  </button>;
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
  const [tool, setTool] = useState<MapTool>('select');
  const [filter, setFilter] = useState<'all' | 'actions'>('all');
  const [panel, setPanel] = useState<'point' | 'route'>('route');
  const [saved, setSaved] = useState(true);
  const [recordMap, setRecordMap] = useState('miraland');
  const [recordTeleport, setRecordTeleport] = useState<Checkpoint | undefined>(undefined);
  const [tolerance, setTolerance] = useState(1);
  const [showTeleports, setShowTeleports] = useState(true);
  const [showPathMask, setShowPathMask] = useState(false);
  const [showSpawners, setShowSpawners] = useState(false);
  const [showShortcuts, setShowShortcuts] = useState(false);
  const [profileCollapsed, setProfileCollapsed] = useState(false);
  const [probe, setProbe] = useState<{ x: number; y: number } | null>(null);
  const [menu, setMenu] = useState<{ x: number; y: number; target: MapContextTarget } | null>(null);
  const [grid, setGrid] = useState<TerrainGrid | null>(null);
  const [terrainStatus, setTerrainStatus] = useState<'idle' | 'loading' | 'missing'>('idle');
  const [spawners, setSpawners] = useState<SpawnerPoint[]>([]);
  const [showGenerator, setShowGenerator] = useState(false);
  const [generatedBatch, setGeneratedBatch] = useState<GenerateGingerbreadResponse | null>(null);
  const [generatedIdx, setGeneratedIdx] = useState(0);
  const [pendingDelete, setPendingDelete] = useState<RouteSummary | null>(null);
  const [history, setHistory] = useState<{ past: RouteDocument[]; future: RouteDocument[] }>({ past: [], future: [] });
  const canvas = useRef<MapCanvasHandle>(null);
  const dragStart = useRef<RouteDocument | null>(null);
  const { status, recording, finished, trial } = useRouteStudioStore();
  const busy = status !== 'idle';
  const overlayCase = useAppStore((s): OverlayCase => {
    const opt = s.globalOptionValues?.RouteOverlayMode;
    if (opt?.type === 'select' && (opt.caseName === 'Full' || opt.caseName === 'MinimapOnly' || opt.caseName === 'Off')) return opt.caseName;
    return 'Full';
  });
  const setOverlayCase = useCallback((nextCase: OverlayCase) => {
    useAppStore.getState().setGlobalOptionValue('RouteOverlayMode', { type: 'select', caseName: nextCase });
    void flushSaveConfig();
  }, []);

  const map = maps.find((item) => item.id === (route?.map ?? recording?.map ?? finished?.map ?? recordMap)) ?? null;
  const mapId = map?.id;
  const imagePath = map?.image;
  const knownCheckpoints = useMemo(() => {
    const items = new Map<string, Checkpoint>();
    for (const cp of catalogCheckpoints[mapId ?? ''] ?? []) items.set(cp.name, cp);
    for (const summary of routes.builtin) if (summary.teleport && summary.map === mapId) items.set(summary.teleport.name, summary.teleport);
    for (const summary of routes.user) if (summary.teleport && summary.map === mapId) items.set(summary.teleport.name, summary.teleport);
    return [...items.values()];
  }, [catalogCheckpoints, routes.builtin, routes.user, mapId]);

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

  useEffect(() => {
    if (!imagePath || !mapId) return;
    let cancelled = false;
    const show = (data: string, mime: string, color: boolean) => {
      const next = new Image();
      next.onload = () => {
        if (cancelled) return;
        const cvs = withoutVoid(next);
        if (color) (cvs as HTMLCanvasElement & { __isColorMap?: boolean }).__isColorMap = true;
        setImage(cvs);
      };
      next.src = `data:${mime};base64,${data}`;
    };
    setImage(null);
    invoke<string>('read_local_file_base64', { filename: `resource/navigation/terrain/${mapId}_color.webp` })
      .then((data) => show(data, 'image/webp', true))
      .catch(() => invoke<string>('read_local_file_base64', { filename: imagePath })
        .then((data) => show(data, 'image/png', false))
        .catch((error) => toast.error(String(error))));
    return () => { cancelled = true; };
  }, [imagePath, mapId]);

  // Terrain heights (elevation profile, point height) and material spawners (coverage layer).
  useEffect(() => {
    if (!mapId || !isTauri()) return;
    let cancelled = false;
    setGrid(null);
    setTerrainStatus('loading');
    loadTerrainGrid(mapId)
      .then((g) => { if (!cancelled) { setGrid(g); setTerrainStatus('idle'); } })
      .catch(() => { if (!cancelled) setTerrainStatus('missing'); });
    loadMaterialCatalog()
      .then(({ spawnersByMap }) => { if (!cancelled) setSpawners(spawnersByMap[mapId] ?? []); })
      .catch(() => { if (!cancelled) setSpawners([]); });
    return () => { cancelled = true; };
  }, [mapId]);

  const commit = useCallback((next: RouteDocument, previous?: RouteDocument) => {
    setRoute((current) => {
      const base = previous ?? current;
      if (base) setHistory((value) => ({ past: [...value.past, base].slice(-HISTORY_LIMIT), future: [] }));
      return next;
    });
    setSaved(false);
  }, []);
  const updateCollection = (next: RouteCollectionDocument) => {
    setCollection(next);
    setSaved(false);
  };

  useEffect(() => {
    const activeDoc = collection ?? route;
    if (!activeDoc || saved || readOnly) return;
    const timer = window.setTimeout(() => {
      saveUserRoute(activeDoc).then(() => { setSaved(true); void refresh(); }).catch((error) => toast.error(String(error)));
    }, 500);
    return () => window.clearTimeout(timer);
  }, [route, collection, saved, readOnly, refresh]);

  const clearSelection = useCallback(() => {
    setSelected(null);
    setSelectedSet(new Set());
  }, []);

  /** Every way of opening a document (library, new, import, generator, recording) goes through here. */
  const loadDocument = useCallback((doc: RouteDocument | RouteCollectionDocument, options: { readOnly?: boolean; saved?: boolean } = {}) => {
    clearTrial();
    if ('kind' in doc && doc.kind === 'route_collection') {
      setCollection(doc);
      setRoute(null);
    } else {
      setCollection(null);
      setRoute(doc as RouteDocument);
      setTimeout(() => canvas.current?.fit(), 60);
    }
    setReadOnly(Boolean(options.readOnly));
    setSaved(options.saved ?? false);
    setHistory({ past: [], future: [] });
    setPanel('route');
    setSelected(null);
    setSelectedSet(new Set());
  }, []);

  const open = useCallback(async (summary: RouteSummary) => {
    try {
      loadDocument(await readRoute(summary), { readOnly: summary.builtin, saved: true });
    } catch (error) {
      toast.error(`无法打开路线：${String(error)}`);
    }
  }, [loadDocument]);

  const undo = useCallback(() => {
    setHistory((value) => {
      const previous = value.past[value.past.length-1];
      if (!previous || !route) return value;
      setRoute(previous);
      setSaved(false);
      clearSelection();
      return { past: value.past.slice(0, -1), future: [route, ...value.future] };
    });
  }, [route, clearSelection]);
  const redo = useCallback(() => {
    setHistory((value) => {
      const next = value.future[0];
      if (!next || !route) return value;
      setRoute(next);
      setSaved(false);
      clearSelection();
      return { past: [...value.past, route], future: value.future.slice(1) };
    });
  }, [route, clearSelection]);

  const setPoints = useCallback((points: RoutePoint[]) => { if (route) commit({ ...route, points }); }, [route, commit]);
  const updatePoint = useCallback((index: number, next: RoutePoint) => {
    if (route) commit({ ...route, points: route.points.map((item, i) => (i === index ? next : item)) });
  }, [route, commit]);

  /** Select one point and show it in the inspector; optionally centre the map on it. */
  const selectPoint = useCallback((index: number | null, options: { toggle?: boolean; focus?: boolean } = {}) => {
    if (index === null) return clearSelection();
    if (options.toggle) {
      setSelectedSet((prev) => {
        const next = new Set(prev);
        if (selected !== null) next.add(selected);
        if (next.has(index)) next.delete(index); else next.add(index);
        return next;
      });
    } else if (!selectedSet.has(index)) {
      setSelectedSet(new Set());
    }
    setSelected(index);
    setPanel('point');
    const target = route?.points[index];
    if (options.focus && target) canvas.current?.focus(target.x, target.y);
  }, [clearSelection, route, selected, selectedSet]);

  const insertPoint = useCallback((after: number, point: RoutePoint) => {
    if (!route || readOnly) return;
    const points = [...route.points];
    points.splice(after + 1, 0, point);
    setPoints(points);
    setSelected(after + 1);
    setSelectedSet(new Set());
    setPanel('point');
  }, [route, readOnly, setPoints]);
  const removePoint = useCallback((index: number) => {
    if (!route || readOnly) return;
    commit({ ...route, points: route.points.filter((_, position) => position !== index) });
    setSelected(route.points.length > 1 ? Math.max(0, index-1) : null);
    setSelectedSet(new Set());
  }, [route, readOnly, commit]);
  const removeBatch = useCallback(() => {
    if (!route || readOnly || !selectedSet.size) return;
    commit({ ...route, points: route.points.filter((_, idx) => !selectedSet.has(idx)) });
    clearSelection();
  }, [route, readOnly, selectedSet, commit, clearSelection]);
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
    clearSelection();
    toast.success('已反转路线点位顺序');
  };

  const createRoute = () => {
    const mapForNew = map?.id ?? recordMap;
    const title = maps.find((item) => item.id === mapForNew)?.title ?? '';
    loadDocument({
      ...newRoute(mapForNew, `${title}路线 ${routes.user.filter((r) => r.kind !== 'route_collection').length+1}`),
      ...(recordTeleport ? { teleport: recordTeleport } : {}),
    });
    setTool('add');
  };
  const createCollection = () => loadDocument({
    schema_version: 1,
    kind: 'route_collection',
    id: `playlist_${Date.now().toString(36)}`,
    title: `路线清单 ${routes.user.filter((r) => r.kind === 'route_collection').length + 1}`,
    continue_on_failure: true,
    routes: [],
  });
  const saveAsCopy = () => {
    if (collection) {
      setCollection({ ...collection, id: `playlist_${Date.now().toString(36)}`, title: `${collection.title} 副本` });
    } else if (route) {
      setRoute({ ...route, id: newRouteId(), title: `${route.title} 副本` });
    } else return;
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
    const raw = JSON.parse(text.replace(/^\uFEFF/, ''));
    if (raw && raw.kind === 'route_collection' && Array.isArray(raw.routes)) {
      loadDocument({
        schema_version: 1,
        kind: 'route_collection',
        id: `playlist_${Date.now().toString(36)}`,
        title: typeof raw.title === 'string' && raw.title ? raw.title : '导入的路线清单',
        continue_on_failure: raw.continue_on_failure !== false,
        routes: raw.routes,
      });
      toast.success('已导入路线清单');
      return;
    }
    const next = parseRouteFile(text);
    if (!maps.some((item) => item.id === next.map)) throw new Error('路线的地图不受支持。');
    loadDocument(next);
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
    const mapCps = catalogCheckpoints[finished.map] ?? knownCheckpoints;
    const presetTp = !base ? recordTeleport : undefined;
    const autoStartTp = (!target.teleport && !presetTp && !target.points.length && finished.samples.length)
      ? nearestCheckpoint(finished.samples[0][0], finished.samples[0][1], mapCps, 20)
      : undefined;
    const chosenTp = target.teleport ?? presetTp ?? autoStartTp;
    let nextPoints: RoutePoint[];
    if (base && mode === 'replace' && selectedSet.size > 0) {
      const first = Math.min(...selectedSet);
      nextPoints = [...target.points.slice(0, first), ...preview, ...target.points.filter((_, idx) => idx > first && !selectedSet.has(idx))];
    } else if (base && mode === 'insert' && selected !== null) {
      nextPoints = [...target.points.slice(0, selected + 1), ...preview, ...target.points.slice(selected + 1)];
    } else {
      nextPoints = [...target.points, ...preview];
    }
    const next = { ...target, ...(chosenTp ? { teleport: chosenTp } : {}), points: nextPoints };
    if (base) {
      commit(next, base);
    } else {
      setCollection(null);
      setReadOnly(false);
      setHistory({ past: [], future: [] });
      setRoute(next);
      setSaved(false);
    }
    if (!target.teleport && chosenTp) toast.success(`已设起始传送点：${chosenTp.name}`);
    clearSelection();
    discardRecording();
    setTimeout(() => canvas.current?.fit(), 60);
  };

  const pathFor = async (doc: RouteDocument | RouteCollectionDocument) => {
    const path = readOnly ? routes.builtin.find((item) => item.id === doc.id)?.path : await saveUserRoute(doc);
    if (!path) throw new Error('找不到路线文件。');
    if (!readOnly) setSaved(true);
    return path;
  };
  const trialRun = async (mode: 'full' | 'movement', start = 0) => {
    const activeDoc = collection ?? route;
    if (!activeDoc) return;
    if (collection && !collection.routes.length) {
      toast.error('请先往清单里添加至少一条路线。');
      return;
    }
    if (route && errors.length) {
      toast.error(errors[0].text);
      if (errors[0].index !== undefined) selectPoint(errors[0].index, { focus: true });
      return;
    }
    try {
      await runRoute(await pathFor(activeDoc), mode, collection ? 0 : start);
    } catch (error) {
      toast.error(String(error));
    }
  };

  const applySuggestions = () => {
    if (!route || readOnly || !trial?.suggestions?.length) return;
    const byIdx = new Map(trial.suggestions.map((s) => [s.index, s]));
    commit({ ...route, points: route.points.map((pt, idx) => {
      const sug = byIdx.get(idx);
      return sug ? { ...pt, x: sug.x, y: sug.y, radius: sug.radius } : pt;
    }) });
    toast.success(`已应用 ${trial.suggestions.length} 处点位微调，可随时 Ctrl+Z 撤销`);
  };

  const addTaskForFile = (path: string, title: string) => {
    const state = useAppStore.getState();
    const instanceId = state.activeInstanceId;
    if (!instanceId) return;
    state.addTaskToInstance(instanceId, { name: 'custom_route', option: ['CustomRouteFile'] });
    const tasks = useAppStore.getState().instances.find((item) => item.id === instanceId)?.selectedTasks ?? [];
    const task = tasks[tasks.length-1];
    if (task) {
      state.setTaskOptionValue(instanceId, task.id, 'CustomRouteFile', { type: 'input', values: { file: path } });
      state.renameTask(instanceId, task.id, title);
    }
  };
  const addToTasks = async () => {
    const activeDoc = collection ?? route;
    if (!activeDoc) return;
    try {
      addTaskForFile(await pathFor(activeDoc), activeDoc.title);
      toast.success('已加入当前任务列表');
    } catch (error) {
      toast.error(String(error));
    }
  };

  const selectGeneratedCandidate = useCallback((batch: GenerateGingerbreadResponse, idx: number) => {
    const doc = batch.routes[idx];
    if (!doc) return;
    setGeneratedIdx(idx);
    loadDocument(doc);
  }, [loadDocument]);
  const saveAllGeneratedToPlaylist = async () => {
    if (!generatedBatch?.routes.length || !generatedBatch.collection) return;
    try {
      for (const r of generatedBatch.routes) await saveUserRoute(r);
      const path = await saveUserRoute(generatedBatch.collection);
      await refresh();
      addTaskForFile(path, generatedBatch.collection.title);
      toast.success(`已保存全部 ${generatedBatch.routes.length} 条路线并作为清单加入任务`);
    } catch (error) {
      toast.error(String(error));
    }
  };

  const shownPoints = preview ?? route?.points ?? [];
  const profile = useMemo(() => buildElevationProfile(grid, shownPoints), [grid, shownPoints]);
  const coverage = useMemo(
    () => (spawners.length && shownPoints.length ? annotateSpawners(grid, spawners, shownPoints, route?.material) : null),
    [grid, spawners, shownPoints, route?.material],
  );
  const point = route && selected !== null ? route.points[selected] ?? null : null;
  const ground = point ? sampleTerrain(grid, point.x, point.y) : null;
  const previousPoint = route && selected ? route.points[selected - 1] : null;
  const previousGround = previousPoint ? sampleTerrain(grid, previousPoint.x, previousPoint.y) : null;
  const length = route ? routeLength(route.points) : 0;
  const activeTrialForRoute = trial && route && trial.route === route.id ? trial : null;
  const wpOutcomeMap = useMemo(() => {
    const mapOut = new Map<number, string>();
    for (const wp of activeTrialForRoute?.telemetry?.waypoints ?? []) {
      if (wp.outcome && wp.outcome !== 'clean') mapOut.set(wp.index, wp.outcome);
    }
    return mapOut;
  }, [activeTrialForRoute]);
  const rows = route ? route.points.map((item, index) => ({ item, index })).filter(({ item }) => filter === 'all' || item.action || item.jump) : [];
  const activeDoc = collection ?? route;
  const editable = Boolean(route && !readOnly);

  /** Live drag preview without history spam; one history entry is written on release. */
  const dragUpdate = (points: RoutePoint[], done: boolean) => {
    if (!route) return;
    if (done) {
      commit({ ...route, points }, dragStart.current ?? undefined);
      dragStart.current = null;
    } else {
      setRoute({ ...route, points });
    }
  };
  const onMove = (index: number, x: number, y: number, done: boolean) => {
    if (!route) return;
    if (!dragStart.current) dragStart.current = route;
    const basePts = dragStart.current.points;
    const originPt = basePts[index];
    const dx = originPt ? Math.round((x - originPt.x) * 100) / 100 : 0;
    const dy = originPt ? Math.round((y - originPt.y) * 100) / 100 : 0;
    const movingBatch = selectedSet.size > 1 && selectedSet.has(index);
    dragUpdate(route.points.map((item, position) => {
      if (movingBatch && selectedSet.has(position) && basePts[position]) {
        return { ...item, x: Math.round((basePts[position].x + dx) * 100) / 100, y: Math.round((basePts[position].y + dy) * 100) / 100 };
      }
      return position === index ? { ...item, x, y } : item;
    }), done);
  };
  const onHeadingChange = (index: number, heading: number, done: boolean) => {
    if (!route) return;
    if (!dragStart.current) dragStart.current = route;
    dragUpdate(route.points.map((item, position) =>
      position === index ? { ...item, params: { ...(item.params ?? {}), facing_heading: heading } } : item), done);
  };
  const setStartTeleport = (cp?: Checkpoint) => {
    if (!route) {
      setRecordTeleport(cp);
      return;
    }
    const next = { ...route };
    if (cp) next.teleport = cp; else delete next.teleport;
    commit(next);
  };
  const teleportPoint = (cp: Checkpoint): RoutePoint => ({ x: cp.x, y: cp.y, radius: 1, jump: false, action: 'teleport', params: { checkpoint: cp } });
  const onPickTeleport = (cp: Checkpoint) => {
    if (readOnly) return;
    if (!route) {
      setRecordTeleport(cp);
      toast.success(`已设录制起始传送点：${cp.name}`);
    } else if (selected !== null && route.points[selected]?.action === 'teleport') {
      updatePoint(selected, { ...route.points[selected], x: cp.x, y: cp.y, params: { ...(route.points[selected].params ?? {}), checkpoint: cp } });
      toast.success(`已将第 ${selected + 1} 点传送设为：${cp.name}`);
    } else if (tool === 'add') {
      insertPoint(selected ?? route.points.length - 1, teleportPoint(cp));
      toast.success(`已插入传送点：${cp.name}`);
    } else {
      setStartTeleport(cp);
      toast.success(`已设起始传送点：${cp.name}`);
    }
  };

  const menuFor = (target: MapContextTarget): { title?: string; items: MenuEntry[] } => {
    if (!route) return { items: [] };
    const last = route.points.length - 1;
    if (target.kind === 'point') {
      const pt = route.points[target.index];
      const set = (action: string) => updatePoint(target.index, withAction(pt, action));
      return {
        title: `第 ${target.index + 1} 点 · ${pointLabel(pt)}`,
        items: [
          { label: '从这里试走（只移动）', onClick: () => void trialRun('movement', target.index), disabled: busy },
          { label: '从这里试跑（含动作）', onClick: () => void trialRun('full', target.index), disabled: busy },
          'separator',
          { label: pt.action === 'wait' ? '改回途经' : '到这里等待', hint: pt.action === 'wait' ? '0' : '2', onClick: () => set(pt.action === 'wait' ? '' : 'wait'), disabled: readOnly },
          { label: pt.action === 'collect' ? '改回途经' : '到这里采集', hint: pt.action === 'collect' ? '0' : '3', onClick: () => set(pt.action === 'collect' ? '' : 'collect'), disabled: readOnly },
          { label: pt.jump ? '取消跳跃' : '跳跃通过', hint: 'J', onClick: () => updatePoint(target.index, { ...pt, jump: !pt.jump }), disabled: readOnly },
          'separator',
          { label: '在后面复制一个点', onClick: () => insertPoint(target.index, { ...pt, x: pt.x + 2, y: pt.y + 2 }), disabled: readOnly },
          { label: '删除这个点', hint: 'Del', danger: true, onClick: () => removePoint(target.index), disabled: readOnly },
        ],
      };
    }
    if (target.kind === 'segment') {
      return {
        title: `第 ${target.after + 1} → ${target.after + 2} 点之间`,
        items: [{ label: '在这里插入点', onClick: () => insertPoint(target.after, { x: target.x, y: target.y, radius: 1, jump: false }), disabled: readOnly }],
      };
    }
    if (target.kind === 'teleport') {
      const cp = target.checkpoint;
      return {
        title: `流转之柱：${cp.name}`,
        items: [
          { label: '设为起始传送点', onClick: () => setStartTeleport(cp), disabled: readOnly },
          { label: selected !== null ? `在第 ${selected + 1} 点后插入传送` : '在末尾追加传送', onClick: () => insertPoint(selected ?? last, teleportPoint(cp)), disabled: readOnly },
        ],
      };
    }
    return {
      items: [
        { label: '在路线末尾追加点', onClick: () => insertPoint(last, { x: target.x, y: target.y, radius: 1, jump: false }), disabled: readOnly },
        { label: selected !== null ? `在第 ${selected + 1} 点后插入` : '设为第一个点', onClick: () => insertPoint(selected ?? -1, { x: target.x, y: target.y, radius: 1, jump: false }), disabled: readOnly },
        'separator',
        { label: '显示整条路线', hint: 'F', onClick: () => canvas.current?.fit() },
      ],
    };
  };

  useEffect(() => {
    const keydown = (event: KeyboardEvent) => {
      if (pendingDelete || menu || showGenerator) return;
      const target = event.target as HTMLElement;
      if (['INPUT', 'SELECT', 'TEXTAREA'].includes(target.tagName) || target.isContentEditable) return;
      const key = event.key.toLowerCase();
      const ctrl = event.ctrlKey || event.metaKey;
      if (ctrl && key === 'z') {
        event.preventDefault();
        if (event.shiftKey) redo(); else undo();
      } else if (ctrl && key === 'y') {
        event.preventDefault();
        redo();
      } else if (ctrl && key === 'a' && route?.points.length) {
        event.preventDefault();
        setSelectedSet(new Set(route.points.map((_, i) => i)));
        setSelected(0);
        setPanel('point');
      } else if ((event.key === 'Delete' || event.key === 'Backspace') && editable) {
        if (selectedSet.size > 1) { event.preventDefault(); removeBatch(); }
        else if (selected !== null) { event.preventDefault(); removePoint(selected); }
      } else if (event.key === 'Escape') {
        clearSelection();
        setTool('select');
        setShowShortcuts(false);
      } else if (ctrl || event.altKey || !route) {
        return;
      } else if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
        if (!route.points.length) return;
        event.preventDefault();
        const delta = event.key === 'ArrowLeft' ? -1 : 1;
        const next = selected === null ? (delta > 0 ? 0 : route.points.length - 1) : Math.max(0, Math.min(route.points.length - 1, selected + delta));
        selectPoint(next, { focus: true });
      } else if (key === 'v') setTool('select');
      else if (key === 'a' && editable) setTool((value) => value === 'add' ? 'select' : 'add');
      else if (key === 'b') setTool((value) => value === 'box' ? 'select' : 'box');
      else if (key === 'f') canvas.current?.fit();
      else if (key === 'j' && editable && point && selected !== null) updatePoint(selected, { ...point, jump: !point.jump });
      else if (event.key in ACTION_HOTKEYS && editable && point && selected !== null && selectedSet.size <= 1) {
        const action = ACTION_HOTKEYS[event.key];
        if ((point.action ?? '') !== action) updatePoint(selected, withAction(point, action));
      }
    };
    window.addEventListener('keydown', keydown);
    return () => window.removeEventListener('keydown', keydown);
  }, [undo, redo, removePoint, removeBatch, selectPoint, updatePoint, clearSelection, selected, selectedSet, point, route, editable, pendingDelete, menu, showGenerator]);

  return <div className="flex min-h-0 flex-1 flex-col bg-bg-primary text-text-primary">
    <header className="flex items-center gap-2 border-b border-border bg-bg-secondary px-4 py-2">
      <button onClick={onClose} title="返回任务列表" aria-label="返回任务列表" className="rounded-lg p-2 hover:bg-bg-hover"><ArrowLeft className="h-4 w-4" /></button>
      <Route className="h-5 w-5 shrink-0 text-accent-secondary" />
      {activeDoc ? <input aria-label="路线名称" value={activeDoc.title ?? ''} disabled={readOnly}
        onChange={(event) => {
          if (collection) updateCollection({ ...collection, title: event.target.value });
          else if (route) commit({ ...route, title: event.target.value });
        }}
        className="min-w-0 max-w-sm flex-1 rounded-lg border border-transparent bg-transparent px-2 py-1 text-base font-semibold outline-none hover:border-border focus:border-accent disabled:hover:border-transparent" />
        : <h1 className="text-lg font-semibold">路线编辑器</h1>}
      {activeDoc && (readOnly
        ? <span className="shrink-0 rounded-full bg-bg-tertiary px-2 py-0.5 text-xs text-text-secondary">内置 · 只读</span>
        : <span className="flex shrink-0 items-center gap-1 text-xs text-text-muted">{saved ? <><Check className="h-3 w-3" />已保存</> : <><Loader2 className="h-3 w-3 animate-spin" />保存中</>}</span>)}
      {route && <span className="ml-1 flex items-center">
        <button onClick={undo} disabled={!history.past.length} title="撤销（Ctrl+Z）" className="rounded-lg p-2 hover:bg-bg-hover disabled:opacity-30"><Undo2 className="h-4 w-4" /></button>
        <button onClick={redo} disabled={!history.future.length} title="重做（Ctrl+Y）" className="rounded-lg p-2 hover:bg-bg-hover disabled:opacity-30"><Redo2 className="h-4 w-4" /></button>
      </span>}
      <span className="ml-auto flex items-center gap-1.5">
        <Segmented label="游戏内路径遮罩" value={overlayCase} onChange={setOverlayCase}
          options={[['Full', '遮罩:3D+小地图'], ['MinimapOnly', '仅小地图'], ['Off', '关遮罩']]} />
        {activeDoc && <>
          <button onClick={() => void copyRoute()} title="复制内容，用来分享" className="rounded-lg p-2 hover:bg-bg-hover"><Copy className="h-4 w-4" /></button>
          <button onClick={() => void exportRoute()} title="导出为文件" className="rounded-lg p-2 hover:bg-bg-hover"><FileOutput className="h-4 w-4" /></button>
          {readOnly && <button onClick={saveAsCopy} className={secondaryButton}><Plus className="h-3.5 w-3.5" />另存为我的路线</button>}
          <button onClick={() => void addToTasks()} disabled={Boolean(route && errors.length)} className={secondaryButton}><ListPlus className="h-3.5 w-3.5" />加入任务</button>
          <button onClick={() => void trialRun('movement')} disabled={busy} title="只走路，不执行动作" className={secondaryButton}><Footprints className="h-3.5 w-3.5" />试走</button>
          <button onClick={() => void trialRun('full')} disabled={busy} className={primaryButton}><CirclePlay className="h-3.5 w-3.5" />试跑</button>
        </>}
      </span>
    </header>

    <div className="flex min-h-0 flex-1">
      <RouteLibrary routes={routes} maps={maps} activeId={activeDoc?.id}
        onOpen={(summary) => void open(summary)} onDelete={setPendingDelete}
        onNewRoute={createRoute} onNewCollection={createCollection}
        onImportFile={(file) => void importFile(file)} onPaste={() => void pasteRoute()}
        onGenerate={() => setShowGenerator(true)} />

      <main className="flex min-w-0 flex-1 flex-col">
        {collection ? <RouteCollectionEditor collection={collection} routes={routes} readOnly={readOnly} onChange={updateCollection} /> : <>
          <div className="relative min-h-0 flex-1">
            <RouteMapCanvas ref={canvas} map={map} image={image} points={route?.points ?? []} preview={preview}
              samples={recording?.samples ?? finished?.samples}
              pose={recording?.pose ?? (status === 'running' ? trial?.pose : null)}
              teleports={showTeleports ? knownCheckpoints : undefined}
              startTeleport={route ? route.teleport : recordTeleport}
              showPathMask={showPathMask}
              spawners={showSpawners ? coverage?.list : null}
              probe={probe}
              selected={selected}
              selectedSet={selectedSet}
              activeTrialIndex={status === 'running' ? trial?.current_index ?? null : null}
              trialWaypoints={activeTrialForRoute?.telemetry?.waypoints ?? null}
              tool={tool} readOnly={readOnly || !route}
              onSelect={(index, toggle) => selectPoint(index, { toggle })}
              onBoxSelect={(indices, additive) => {
                setSelectedSet((prev) => {
                  const next = additive ? new Set(prev) : new Set<number>();
                  for (const idx of indices) next.add(idx);
                  return next;
                });
                setSelected(indices[0] ?? null);
                if (indices.length) setPanel('point');
                if (tool === 'box') setTool('select');
              }}
              onMove={onMove}
              onHeadingChange={onHeadingChange}
              onInsert={(after, x, y) => insertPoint(after, { x, y, radius: 1, jump: false })}
              onPickTeleport={onPickTeleport}
              onContextMenu={(target, x, y) => {
                if (!route) return;
                if (target.kind === 'point') selectPoint(target.index);
                setMenu({ x, y, target });
              }} />

            <div className="absolute left-3 top-3 flex flex-col gap-1 rounded-xl border border-border bg-bg-secondary/95 p-1 shadow-sm">
              <ToolButton active={tool === 'select'} title="选择和拖动（V）：Shift+点击多选，Shift+拖动框选" onClick={() => setTool('select')}><MousePointer2 className="h-4 w-4" /></ToolButton>
              <ToolButton active={tool === 'add'} disabled={!editable} title="添加点（A）：点空白处追加，点线段插入" onClick={() => setTool('add')}><Plus className="h-4 w-4" /></ToolButton>
              <ToolButton active={tool === 'box'} disabled={!route} title="框选多个点位（B）" onClick={() => setTool('box')}><BoxSelect className="h-4 w-4" /></ToolButton>
              <span className="mx-1.5 my-0.5 h-px bg-border" />
              <ToolButton title="放大" onClick={() => canvas.current?.zoom(1.4)}><Plus className="h-4 w-4" /></ToolButton>
              <ToolButton title="缩小" onClick={() => canvas.current?.zoom(1/1.4)}><Minus className="h-4 w-4" /></ToolButton>
              <ToolButton title="显示整条路线（F）" onClick={() => canvas.current?.fit()}><Maximize className="h-4 w-4" /></ToolButton>
              <span className="mx-1.5 my-0.5 h-px bg-border" />
              <ToolButton active={showTeleports} tone="bg-[#7c6cf0]/15 text-[#7c6cf0]" title="流转之柱：点击设为起点传送，添加模式下插入传送点，右键查看更多" onClick={() => setShowTeleports((v) => !v)}><MapPin className="h-4 w-4" /></ToolButton>
              <ToolButton active={showPathMask} tone="bg-warning/15 text-warning" title="显示 15 米拾取范围" onClick={() => setShowPathMask((v) => !v)}><CircleDashed className="h-4 w-4" /></ToolButton>
              <ToolButton active={showSpawners} tone="bg-[#3f9d6a]/15 text-[#3f9d6a]" title="显示地图材料点：实心为路线能拾取，空心为够不到（会考虑高低差）" onClick={() => setShowSpawners((v) => !v)}><Sprout className="h-4 w-4" /></ToolButton>
              <span className="mx-1.5 my-0.5 h-px bg-border" />
              <ToolButton active={showShortcuts} title="快捷键与操作说明" onClick={() => setShowShortcuts((v) => !v)}><Keyboard className="h-4 w-4" /></ToolButton>
            </div>

            {showShortcuts && <div className="absolute left-16 top-3 z-20 w-72 rounded-xl border border-border bg-bg-secondary/95 p-3 text-xs shadow-lg backdrop-blur">
              <div className="mb-2 flex items-center justify-between font-medium">操作说明
                <button onClick={() => setShowShortcuts(false)} className="text-text-muted hover:text-text-primary"><X className="h-3.5 w-3.5" /></button>
              </div>
              <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
                {EDITOR_SHORTCUTS.map(([key, text]) => <div key={key} className="contents">
                  <dt><kbd className="rounded border border-border bg-bg-primary px-1 text-[11px]">{key}</kbd></dt>
                  <dd className="text-text-secondary">{text}</dd>
                </div>)}
              </dl>
            </div>}

            {generatedBatch && generatedBatch.routes.length > 0 && <div className="absolute left-16 right-3 top-3 z-10 flex flex-wrap items-center gap-1.5 rounded-xl border border-accent/35 bg-bg-secondary/95 px-2.5 py-1.5 text-xs shadow-lg backdrop-blur">
              <span className="flex items-center gap-1 font-medium text-accent"><Sparkles className="h-3.5 w-3.5" />智能方案：</span>
              {generatedBatch.summaries.map((s, idx) => <button key={s.id} type="button" onClick={() => selectGeneratedCandidate(generatedBatch, idx)}
                className={clsx('rounded-lg px-2 py-1 transition-colors', generatedIdx === idx ? 'bg-accent font-medium text-white' : 'bg-bg-primary text-text-secondary hover:bg-bg-hover')}>
                #{idx + 1} {s.checkpoint}（{s.target_count}个/{Math.round(s.estimated_seconds)}s）
              </button>)}
              {generatedBatch.routes.length > 1 && <button type="button" onClick={() => void saveAllGeneratedToPlaylist()}
                className="ml-1 inline-flex items-center gap-1 rounded-lg bg-accent/15 px-2.5 py-1 font-medium text-accent hover:bg-accent/25">
                <ListPlus className="h-3.5 w-3.5" />全部存为清单并加入任务
              </button>}
              <button type="button" onClick={() => setGeneratedBatch(null)} title="关闭候选栏" className="ml-auto rounded p-1 text-text-muted hover:text-text-primary"><X className="h-3.5 w-3.5" /></button>
            </div>}

            {readOnly && route && !generatedBatch && <p className="absolute left-1/2 top-3 -translate-x-1/2 rounded-full border border-border bg-bg-secondary/95 px-3 py-1 text-xs text-text-secondary shadow-sm">
              内置路线只能查看，点右上角「另存为我的路线」后可以修改
            </p>}
            {editable && route && !route.points.length && !finished && !busy && <p className="pointer-events-none absolute left-1/2 top-3 -translate-x-1/2 rounded-full border border-border bg-bg-secondary/95 px-3 py-1 text-xs text-text-secondary shadow-sm">
              在地图上点击添加第一个点，或在下方开始录制
            </p>}

            <div className="absolute bottom-4 left-1/2 w-max max-w-[calc(100%-2rem)] -translate-x-1/2">
              <RouteSessionBar route={route} readOnly={readOnly} maps={maps} recordMap={recordMap} recordTeleport={recordTeleport}
                known={knownCheckpoints} overlayCase={overlayCase} previewCount={preview?.length ?? 0}
                tolerance={tolerance} onTolerance={setTolerance} selected={selected} selectedCount={selectedSet.size}
                onKeep={keepRecording}
                onRecordMap={(next) => {
                  setRecordMap(next);
                  setRecordTeleport(undefined);
                  if (route && !route.points.length) {
                    const changed = { ...route, map: next };
                    delete changed.teleport;
                    setRoute(changed);
                  }
                }}
                onStartTeleport={setStartTeleport}
                onStartRecording={() => {
                  const targetMap = route?.map ?? recordMap;
                  const isSegment = Boolean(editable && route && route.points.length > 0);
                  const startTp = isSegment ? undefined : (route ? route.teleport : recordTeleport);
                  void startRecording(targetMap, { teleport: startTp, require_bigmap: isSegment || !startTp });
                }} />
            </div>
          </div>
          {(route || preview) && <RouteElevationProfile profile={profile} status={terrainStatus} points={shownPoints}
            selected={preview ? null : selected} selectedSet={preview ? new Set() : selectedSet}
            activeTrialIndex={status === 'running' ? trial?.current_index ?? null : null}
            collapsed={profileCollapsed} onToggle={() => setProfileCollapsed((v) => !v)}
            onSelect={(index) => { if (!preview) selectPoint(index, { focus: true }); }}
            onProbe={setProbe} />}
        </>}
      </main>

      {!collection && <aside className="flex w-80 shrink-0 flex-col border-l border-border bg-bg-secondary">
        {route ? <>
          <div className="flex items-center gap-2 border-b border-border px-3 py-2">
            <Segmented label="右侧面板" value={panel} onChange={setPanel}
              options={[['point', selectedSet.size > 1 ? `已选 ${selectedSet.size} 点` : selected !== null ? `第 ${selected + 1} 点` : `点位 ${route.points.length}`], ['route', '路线设置']]} />
            {errors.length > 0 && <button onClick={() => setPanel('route')} className="ml-auto rounded-full bg-error/10 px-2 py-0.5 text-xs text-error">{errors.length} 个问题</button>}
          </div>
          {panel === 'route' ? <div className="min-h-0 flex-1 overflow-auto">
            <RouteSettings route={route} readOnly={readOnly} known={knownCheckpoints} length={length}
              actions={route.points.filter((item) => item.action).length}
              coverage={coverage ? { capturedCount: coverage.capturedCount, total: coverage.list.length, targetCount: coverage.targetCount, targetCaptured: coverage.targetCaptured } : null}
              telemetry={status === 'idle' ? activeTrialForRoute?.telemetry ?? null : null}
              suggestions={activeTrialForRoute?.suggestions.length ?? 0}
              issues={issues}
              onChange={(next) => commit(next)} onReverse={reversePoints}
              onIssue={(index) => selectPoint(index, { focus: true })}
              onClearTrial={clearTrial} onApplySuggestions={applySuggestions} />
          </div> : <>
            {selectedSet.size > 1 ? <div className="border-b border-border">
              <BatchInspector count={selectedSet.size} readOnly={readOnly}
                onSetRadius={(radius) => setPoints(route.points.map((pt, i) => selectedSet.has(i) ? { ...pt, radius } : pt))}
                onSetJump={(jump) => setPoints(route.points.map((pt, i) => selectedSet.has(i) ? { ...pt, jump } : pt))}
                onDelete={removeBatch}
                onClear={() => setSelectedSet(new Set())} />
            </div> : point && selected !== null ? <div className="max-h-[60%] shrink-0 overflow-auto border-b border-border">
              <PointInspector point={point} index={selected} total={route.points.length} known={knownCheckpoints} map={route.map} readOnly={readOnly}
                ground={ground} previousGround={previousGround}
                onChange={(next) => updatePoint(selected, next)}
                onRemove={() => removePoint(selected)}
                onWalk={() => void trialRun('movement', selected)}
                onRun={() => void trialRun('full', selected)}
                onStep={(delta) => selectPoint(Math.max(0, Math.min(route.points.length - 1, selected + delta)), { focus: true })} />
            </div> : <p className="border-b border-border px-3 py-3 text-xs leading-5 text-text-muted">
              点地图上的点或下面的列表来编辑；{editable ? '按 A 进入添加模式，右键可插入或删除。' : '内置路线另存后才能修改。'}
            </p>}
            <div className="flex items-center justify-between px-3 pb-1 pt-2.5">
              <h3 className="text-xs font-semibold text-text-secondary">全部点位</h3>
              <Segmented label="点位筛选" value={filter} onChange={setFilter} options={[['all', '全部'], ['actions', '有动作']]} />
            </div>
            <ol className="min-h-0 flex-1 overflow-auto px-2 pb-2">
              {rows.map(({ item, index }) => {
                const badge = OUTCOME_BADGE[wpOutcomeMap.get(index) ?? ''];
                return <li key={index} className={clsx('group flex items-center rounded-lg text-xs', (selected === index || selectedSet.has(index)) ? 'bg-accent/10' : 'hover:bg-bg-hover')}>
                  <button onClick={(ev) => selectPoint(index, { toggle: ev.shiftKey, focus: true })} className="flex min-w-0 flex-1 items-center gap-2 px-2 py-1.5 text-left">
                    <span className="w-7 shrink-0 text-right tabular-nums text-text-muted">{index+1}</span>
                    <span className="h-2 w-2 shrink-0 rounded-full border border-border" style={item.action ? { background: actionMeta(item.action).color, borderColor: 'transparent' } : undefined} />
                    <span className={clsx('truncate', !item.action && 'text-text-secondary')}>{pointLabel(item)}{item.jump && <span className="ml-1 text-warning">· 跳跃</span>}</span>
                    {badge && <span className={clsx('ml-auto shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium', badge.cls)}>{badge.label}</span>}
                  </button>
                  {editable && <span className="hidden pr-1 group-hover:flex">
                    <button onClick={() => movePoint(index, -1)} title="上移" className="rounded p-1 text-text-muted hover:text-text-primary"><ArrowUp className="h-3 w-3" /></button>
                    <button onClick={() => movePoint(index, 1)} title="下移" className="rounded p-1 text-text-muted hover:text-text-primary"><ArrowDown className="h-3 w-3" /></button>
                  </span>}
                </li>;
              })}
            </ol>
          </>}
        </> : <div className="flex flex-1 flex-col justify-center gap-4 p-6 text-sm leading-6 text-text-secondary">
          <h2 className="text-base font-semibold text-text-primary">三步做一条重复路线</h2>
          <p><strong className="text-text-primary">1. 新建或录制</strong><br />左侧新建路线后直接在地图上点出路径；或在地图下方选好起点开始录制，在游戏里走一遍，跳跃、交互、攻击朝向和能力键会自动记下。</p>
          <p><strong className="text-text-primary">2. 在地图上调整</strong><br />拖动点位和朝向箭头，右键插入、删除或设置动作；底部的高度剖面能看出哪里爬坡、哪里落差大需要跳跃。</p>
          <p><strong className="text-text-primary">3. 试跑与串联</strong><br />从任意点试走或试跑，根据诊断一键微调；也可以用「智能生成」规划采集路线，或建清单把多条路线串起来。</p>
        </div>}
      </aside>}
    </div>

    {menu && route && (() => {
      const content = menuFor(menu.target);
      return <ContextMenu x={menu.x} y={menu.y} title={content.title} items={content.items} onClose={() => setMenu(null)} />;
    })()}
    <ConfirmDialog open={pendingDelete !== null} title="删除路线"
      message={`确定删除「${pendingDelete?.title || pendingDelete?.id}」吗？删除后无法恢复。`}
      cancelText="取消" confirmText="删除" destructive
      onCancel={() => setPendingDelete(null)}
      onConfirm={() => { if (pendingDelete) void remove(pendingDelete); }} />
    <RouteGeneratorModal
      open={showGenerator}
      onClose={() => setShowGenerator(false)}
      onGenerated={(res) => {
        setGeneratedBatch(res);
        setShowSpawners(true);
        selectGeneratedCandidate(res, 0);
      }}
    />
  </div>;
}
