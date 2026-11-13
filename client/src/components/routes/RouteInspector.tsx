import { useMemo, useState } from 'react';
import { ChevronLeft, ChevronRight, CirclePlay, Footprints, Trash2 } from 'lucide-react';
import clsx from 'clsx';
import { secondaryButton } from '@/components/ui/FeaturePage';
import { SwitchTrack } from '@/components/ui/SwitchTrack';
import { ACTIONS, MACROS, ROUTE_KEYS, actionMeta, withAction, type Checkpoint, type RouteDocument, type RouteIssue, type RoutePoint } from '@/services/routeDocument';
import { Field, NumberField, field, iconButton } from './routeEditorShared';
import type { TerrainSample } from './routeTerrain';

export function CheckpointEditor({ value, known, map, fallback, onChange }: {
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

export function BatchInspector({ count, readOnly, onSetRadius, onSetJump, onDelete, onClear }: {
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

export function PointInspector({ point, index, total, known, map, readOnly, ground, previousGround, onChange, onRemove, onWalk, onRun, onStep }: {
  point: RoutePoint; index: number; total: number; known: Checkpoint[]; map: string; readOnly: boolean;
  ground?: TerrainSample | null; previousGround?: TerrainSample | null;
  onChange: (point: RoutePoint) => void; onRemove: () => void; onWalk: () => void; onRun: () => void;
  onStep: (delta: number) => void;
}) {
  const params = point.params ?? {};
  const set = (key: string, value: unknown) => {
    const nextParams = { ...params, [key]: value };
    if (value === undefined) delete nextParams[key];
    onChange({ ...point, params: nextParams });
  };
  const action = point.action ?? '';
  const showAttackHeading = (action === 'macro' && params.id === 'zhaoxi_attack') || (action === 'key' && params.key === 'attack');
  const rise = ground?.valid && previousGround?.valid ? ground.z - previousGround.z : null;

  return <fieldset disabled={readOnly} className="space-y-3 p-3">
    <div className="flex items-center gap-0.5">
      <button type="button" onClick={() => onStep(-1)} disabled={index <= 0} title="上一个点（←）" className={iconButton}><ChevronLeft className="h-4 w-4" /></button>
      <h3 className="text-sm font-semibold tabular-nums">第 {index+1} 点<span className="ml-1 text-xs font-normal text-text-muted">/ {total}</span></h3>
      <button type="button" onClick={() => onStep(1)} disabled={index >= total-1} title="下一个点（→）" className={clsx(iconButton, 'mr-auto')}><ChevronRight className="h-4 w-4" /></button>
      <button type="button" onClick={onWalk} title="从这个点开始试走（只移动）" className={iconButton}><Footprints className="h-4 w-4" /></button>
      <button type="button" onClick={onRun} title="从这个点开始试跑（含动作）" className={iconButton}><CirclePlay className="h-4 w-4" /></button>
      <button type="button" onClick={onRemove} title="删除这个点（Delete）" className={clsx(iconButton, 'hover:text-error')}><Trash2 className="h-4 w-4" /></button>
    </div>
    {ground?.valid && <p className="rounded-lg bg-bg-tertiary px-2.5 py-1.5 text-xs tabular-nums text-text-secondary">
      地面高度 {ground.z.toFixed(1)} · 坡度 {ground.slope.toFixed(0)}°
      {rise !== null && point.action !== 'teleport' && <span className={clsx('ml-1', rise > 0 ? 'text-warning' : 'text-[#2f8fc4]')}>
        · 比上一点{rise >= 0 ? '高' : '低'} {Math.abs(rise).toFixed(1)}
      </span>}
    </p>}
    <div>
      <span className="mb-1.5 block text-xs text-text-secondary">到这里后</span>
      <div className="flex flex-wrap gap-1.5">
        {ACTIONS.map((item, order) => <button key={item.id} type="button" title={`${item.hint}（快捷键 ${order}）`} onClick={() => action !== item.id && onChange(withAction(point, item.id))}
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
    {action === 'macro' && <Field label="连招"><select className={field} value={String(params.id ?? '')} onChange={(event) => {
      const nextId = event.target.value;
      const nextParams: Record<string, unknown> = { ...params, id: nextId };
      if (nextId === 'starsea_fall') delete nextParams.facing_heading;
      else if (nextId === 'zhaoxi_attack' && nextParams.facing_heading === undefined) nextParams.facing_heading = 0;
      onChange({ ...point, params: nextParams });
    }}>
      {MACROS.map(([id, label]) => <option key={id} value={id}>{label}</option>)}
    </select></Field>}

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
    {action === 'teleport' && <Field label="传送到（也可在地图上点流转之柱）">
      <CheckpointEditor value={params.checkpoint as Checkpoint | undefined} known={known} map={map} fallback={point}
        onChange={(value) => set('checkpoint', value)} />
    </Field>}
    <div className="grid grid-cols-2 gap-2">
      <NumberField label="到达范围" value={point.radius} min={0.2} max={10} step={0.1} onChange={(radius) => onChange({ ...point, radius })} />
      <button type="button" role="switch" aria-checked={point.jump} title="快捷键 J" onClick={() => onChange({ ...point, jump: !point.jump })}
        className="flex items-center justify-between self-end rounded-lg border border-border px-2.5 py-1.5 text-xs">跳跃通过<SwitchTrack checked={point.jump} compact /></button>
      <NumberField label="X" value={point.x} min={0} max={20000} step={0.1} onChange={(x) => onChange({ ...point, x })} />
      <NumberField label="Y" value={point.y} min={0} max={20000} step={0.1} onChange={(y) => onChange({ ...point, y })} />
    </div>
  </fieldset>;
}

export interface CoverageSummary { capturedCount: number; total: number; targetCount: number; targetCaptured: number }

/** Route-level properties: start teleport, run limits, material counting, diagnostics and issues. */
export function RouteSettings({
  route, readOnly, known, length, actions, coverage, telemetry, suggestions, issues,
  onChange, onReverse, onIssue, onClearTrial, onApplySuggestions,
}: {
  route: RouteDocument;
  readOnly: boolean;
  known: Checkpoint[];
  length: number;
  actions: number;
  coverage: CoverageSummary | null;
  telemetry: { visited_count: number; point_count: number; turnback_waypoints: number; recovered_waypoints: number; missed_actions: number } | null;
  suggestions: number;
  issues: RouteIssue[];
  onChange: (route: RouteDocument) => void;
  onReverse: () => void;
  onIssue: (index: number) => void;
  onClearTrial: () => void;
  onApplySuggestions: () => void;
}) {
  const stat = (value: number | string, label: string) => <span>
    <strong className="text-base font-semibold tabular-nums text-text-primary">{value}</strong> {label}
  </span>;
  return <div className="space-y-3 p-3">
    <div className="flex items-center gap-3 text-xs text-text-secondary">
      {stat(route.points.length, '个点')}
      {stat(actions, '个动作')}
      {stat(Math.round(length), '距离')}
      {!readOnly && route.points.length >= 2 && <button onClick={onReverse} className="ml-auto rounded-lg px-2 py-1 text-text-muted hover:bg-bg-hover hover:text-text-primary">反转顺序</button>}
    </div>
    {issues.length > 0 && <ul className="space-y-1">
      {issues.slice(0, 6).map((issue) => <li key={issue.text}>
        <button onClick={() => issue.index !== undefined && onIssue(issue.index)}
          className={clsx('flex w-full items-start gap-1.5 rounded-lg px-2 py-1 text-left text-xs leading-5', issue.level === 'error' ? 'bg-error/10 text-error' : 'bg-warning/10 text-warning')}>
          {issue.text}
        </button>
      </li>)}
    </ul>}
    {telemetry && <div className="space-y-1.5 rounded-lg border border-border bg-bg-tertiary p-2.5 text-xs">
      <div className="flex items-center justify-between">
        <span className="font-medium text-text-primary">上次试跑诊断</span>
        <button type="button" onClick={onClearTrial} className="text-text-muted hover:text-text-primary">清除</button>
      </div>
      <p className="text-text-secondary">
        走过 {telemetry.visited_count}/{telemetry.point_count} 点
        {telemetry.turnback_waypoints > 0 && ` · 折返 ${telemetry.turnback_waypoints}`}
        {telemetry.recovered_waypoints > 0 && ` · 脱困 ${telemetry.recovered_waypoints}`}
        {telemetry.missed_actions > 0 && ` · 漏采 ${telemetry.missed_actions}`}
      </p>
      {!readOnly && suggestions > 0 && <button type="button" onClick={onApplySuggestions}
        className="inline-flex w-full items-center justify-center gap-1.5 rounded-lg bg-accent/15 px-2.5 py-1.5 font-medium text-accent hover:bg-accent/25">
        一键应用微调建议（{suggestions} 处）
      </button>}
    </div>}
    {coverage && <p className="rounded-lg bg-bg-tertiary px-2.5 py-1.5 text-xs text-text-secondary">
      地图材料点：路线可拾取 <strong className="tabular-nums text-text-primary">{coverage.capturedCount}</strong> 个
      {coverage.targetCount > 0 && <>，其中目标素材 <strong className="tabular-nums text-text-primary">{coverage.targetCaptured}/{coverage.targetCount}</strong></>}
    </p>}
    <Field label="起始传送点">
      <fieldset disabled={readOnly}>
        <CheckpointEditor value={route.teleport} known={known} map={route.map} fallback={route.points[0] ?? { x: 0, y: 0 }}
          onChange={(teleport) => {
            const next = { ...route };
            if (teleport) next.teleport = teleport; else delete next.teleport;
            onChange(next);
          }} />
      </fieldset>
    </Field>
    <fieldset disabled={readOnly} className="grid grid-cols-2 gap-2">
      <NumberField label="起点允许误差" value={route.start_radius ?? 6} min={0.5} max={30} step={0.5} onChange={(start_radius) => onChange({ ...route, start_radius })} />
      <NumberField label="路线超时（秒）" value={route.timeout ?? 900} min={30} max={900} step={30} onChange={(timeout) => onChange({ ...route, timeout })} />
      <Field label="目标素材名（可选）"><input className={field} value={route.material ?? ''} placeholder="如 苔之幽"
        onChange={(e) => {
          const next = { ...route };
          if (e.target.value.trim()) next.material = e.target.value.trim(); else delete next.material;
          onChange(next);
        }} /></Field>
      <NumberField label="采集达标上限（0不限）" value={route.capacity ?? 0} min={0} max={500} step={1}
        onChange={(v) => {
          const next = { ...route };
          if (v > 0) next.capacity = Math.round(v); else delete next.capacity;
          onChange(next);
        }} />
      <div className="col-span-2">
        <Field label="伴生计入素材（逗号分隔）"><input className={field} value={(route.count_materials ?? []).join(', ')} placeholder="如 发光花蕊, 荧光草"
          onChange={(e) => {
            const items = e.target.value.split(/[,，]/).map((s) => s.trim()).filter(Boolean);
            const next = { ...route };
            if (items.length) next.count_materials = items; else delete next.count_materials;
            onChange(next);
          }} /></Field>
      </div>
    </fieldset>
  </div>;
}
