import { Check, Loader2, Square, X } from 'lucide-react';
import clsx from 'clsx';
import { primaryButton, secondaryButton } from '@/components/ui/FeaturePage';
import { discardRecording, stopRouteSession, useRouteStudioStore } from '@/services/routeStudioService';
import type { Checkpoint, RouteDocument } from '@/services/routeDocument';
import type { MapInfo } from './RouteMapCanvas';
import { HOTKEY_LEGEND, STATE_TEXT } from './routeEditorShared';

const card = 'rounded-2xl border border-border bg-bg-secondary/95 shadow-lg backdrop-blur';

/** Floating bar at the bottom of the map: start a recording, follow a recording or trial, keep a take. */
export function RouteSessionBar({
  route, readOnly, maps, recordMap, recordTeleport, known, overlayCase, previewCount, tolerance, selected, selectedCount,
  onTolerance, onKeep, onStartRecording, onRecordMap, onStartTeleport,
}: {
  route: RouteDocument | null;
  readOnly: boolean;
  maps: MapInfo[];
  recordMap: string;
  recordTeleport?: Checkpoint;
  known: Checkpoint[];
  overlayCase: 'Full' | 'MinimapOnly' | 'Off';
  previewCount: number;
  tolerance: number;
  selected: number | null;
  selectedCount: number;
  onTolerance: (value: number) => void;
  onKeep: (mode: 'append' | 'insert' | 'replace') => void;
  onStartRecording: () => void;
  onRecordMap: (map: string) => void;
  onStartTeleport: (checkpoint?: Checkpoint) => void;
}) {
  const { status, recording, finished, trial } = useRouteStudioStore();
  const editable = Boolean(route && !readOnly);

  if (status === 'recording' || (status === 'starting' && recording)) {
    return <div className={clsx(card, 'px-4 py-3')}>
      <div className="flex items-center gap-3">
        <span className={clsx('h-2.5 w-2.5 animate-pulse rounded-full',
          recording?.state === 'recording' ? 'bg-error' : recording?.state === 'calibrated' ? 'bg-[#3f9d6a]' : 'bg-warning')} />
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
    </div>;
  }

  if (status === 'running' || status === 'starting' || status === 'stopping') {
    return <div className={clsx(card, 'flex items-center gap-3 px-4 py-2.5')}>
      <Loader2 className="h-4 w-4 animate-spin text-accent" />
      <div className="min-w-48">
        <p className="text-sm font-medium" role="status">
          {status === 'stopping' ? '正在停止…' : trial?.message || (status === 'running' ? '路线运行中，切出游戏会暂停' : '正在准备…')}
        </p>
        {status === 'running' && trial && <p className="text-xs tabular-nums text-text-muted">
          当前目标：第 {(trial.current_index ?? 0) + 1} 点（{overlayCase === 'Off' ? '游戏内路径遮罩已关闭' : overlayCase === 'MinimapOnly' ? '游戏内仅显示左上角小地图遮罩' : '游戏内实时显示 3D 地面 + 小地图路径遮罩'}）
        </p>}
      </div>
      <button onClick={stopRouteSession} disabled={status === 'stopping'} className={secondaryButton}><Square className="h-3.5 w-3.5" />停止</button>
    </div>;
  }

  if (finished) {
    const sameMap = editable && route!.map === finished.map;
    return <div className={clsx(card, 'flex flex-wrap items-center gap-2.5 px-4 py-3')}>
      <div>
        <p className="text-sm font-medium">录到 {finished.samples.length} 个采样，生成 {previewCount} 个点</p>
        {finished.message && <p className="text-xs text-warning">{finished.message}</p>}
      </div>
      <label className="flex items-center gap-2 text-xs text-text-secondary">点位更密
        <input type="range" min={0.2} max={4} step={0.1} value={tolerance} onChange={(event) => onTolerance(Number(event.target.value))} className="w-24 accent-[var(--color-accent)]" aria-label="简化强度" />更简洁</label>
      <button onClick={discardRecording} className={secondaryButton}><X className="h-3.5 w-3.5" />放弃</button>
      {sameMap && selectedCount > 1 && <button onClick={() => onKeep('replace')} className={secondaryButton}>替换所选 {selectedCount} 点</button>}
      {sameMap && selected !== null && <button onClick={() => onKeep('insert')} className={secondaryButton}>插入第 {selected+1} 点后</button>}
      <button onClick={() => onKeep('append')} className={primaryButton}><Check className="h-3.5 w-3.5" />{sameMap && route!.points.length ? '接到路线末尾' : '保存为路线'}</button>
    </div>;
  }

  const segment = Boolean(editable && route!.points.length > 0);
  return <div className={clsx(card, 'flex flex-wrap items-center gap-2 px-3 py-2')}>
    <select aria-label="录制地图" value={route?.map ?? recordMap} disabled={Boolean(route?.points.length)} onChange={(event) => onRecordMap(event.target.value)}
      className="rounded-lg border border-border bg-bg-primary px-2 py-1.5 text-xs outline-none">
      {maps.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}
    </select>
    {segment ? <span className="px-1 text-xs text-text-secondary">续录片段（开始后请按 M 打开大地图定位起点）</span>
      : <select aria-label="录制起点传送点" value={(route ? route.teleport?.name : recordTeleport?.name) ?? ''} disabled={readOnly}
        onChange={(event) => onStartTeleport(known.find((item) => item.name === event.target.value))}
        className="max-w-56 rounded-lg border border-border bg-bg-primary px-2 py-1.5 text-xs outline-none">
        <option value="">起点：非传送点（需开大地图定位）</option>
        {known.map((cp) => <option key={cp.name} value={cp.name}>起点传送：{cp.name}</option>)}
      </select>}
    <button onClick={onStartRecording} className="inline-flex items-center gap-2 rounded-lg bg-error px-3.5 py-1.5 text-xs font-medium text-white hover:opacity-90">
      <span className="h-2 w-2 rounded-full bg-white" />{segment ? '录制片段' : '开始录制'}
    </button>
  </div>;
}
