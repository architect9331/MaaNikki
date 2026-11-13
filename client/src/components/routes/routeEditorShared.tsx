import type { ReactNode } from 'react';
import { ACTIONS, MACROS, ROUTE_KEYS, actionMeta, type RoutePoint } from '@/services/routeDocument';

export const field = 'w-full rounded-lg border border-border bg-bg-primary px-2.5 py-1.5 text-xs outline-none focus:border-accent';
export const iconButton = 'rounded-lg p-1.5 text-text-secondary hover:bg-bg-hover hover:text-text-primary disabled:opacity-30 disabled:hover:bg-transparent';

/** Key points name their key, so a recorded run of attacks or bell presses reads at a glance. */
export function pointLabel(point: RoutePoint) {
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

export const HOTKEY_LEGEND = [['F6', '关键点'], ['F7', '采集'], ['F8', '等待'], ['F9', '钓鱼'], ['F10', '下落攻击']] as const;

/** Editor shortcuts, shown in the help popover. */
export const EDITOR_SHORTCUTS = [
  ['V', '选择 / 拖动'],
  ['A', '添加点：点空白追加，点线段插入'],
  ['B', '框选（也可按住 Shift 拖动）'],
  ['F', '显示整条路线'],
  ['← / →', '上一个 / 下一个点'],
  ['0–8', '给所选点设置动作（按右侧动作顺序，0 为途经）'],
  ['J', '切换所选点的跳跃'],
  ['Ctrl+A', '全选点位'],
  ['Delete', '删除所选点'],
  ['Ctrl+Z / Ctrl+Y', '撤销 / 重做'],
  ['右键', '点位、线段、传送点的快捷菜单'],
  ['中键拖动', '平移地图'],
  ['Esc', '取消选择并回到选择工具'],
] as const;

/** Number keys 1–8 map to the action list in its displayed order; 0 is plain pass-through. */
export const ACTION_HOTKEYS: Record<string, string> = Object.fromEntries(
  ACTIONS.map((item, index) => [String(index), item.id]),
);

export const STATE_TEXT: Record<string, string> = {
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

export const OUTCOME_BADGE: Record<string, { label: string; cls: string }> = {
  turnback: { label: '折返', cls: 'bg-warning/15 text-warning' },
  recovered: { label: '脱困', cls: 'bg-warning/20 text-warning' },
  action_missed: { label: '漏采', cls: 'bg-warning/15 text-warning' },
  failed: { label: '中断', cls: 'bg-error/15 text-error' },
};

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return <label className="block"><span className="mb-1 block text-xs text-text-secondary">{label}</span>{children}</label>;
}

export function NumberField({ label, value, min, max, step = 1, onChange }: {
  label: string; value: number; min: number; max: number; step?: number; onChange: (value: number) => void;
}) {
  return <Field label={label}><input type="number" className={field} value={value} min={min} max={max} step={step}
    onChange={(event) => {
      const next = Number(event.target.value);
      if (Number.isFinite(next)) onChange(Math.min(max, Math.max(min, next)));
    }} /></Field>;
}

/** Unexplored map areas are pure black in the reference image; let the page show through. */
export function withoutVoid(image: HTMLImageElement) {
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
