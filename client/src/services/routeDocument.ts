/** Route documents for the editor: the same declarative JSON the navigator reads. */

export interface Checkpoint { name: string; province: string; region: string; x: number; y: number }

export interface RoutePoint {
  x: number;
  y: number;
  radius: number;
  jump: boolean;
  action?: string;
  params?: Record<string, unknown>;
}

export interface RouteDocument {
  schema_version: 3;
  id: string;
  title: string;
  map: string;
  start_radius: number;
  timeout: number;
  teleport?: Checkpoint;
  material?: string;
  count_materials?: string[];
  capacity?: number;
  points: RoutePoint[];
}

export interface RouteCollectionEntry { route: string; title: string; file?: string }

export interface RouteCollectionDocument {
  schema_version: 1;
  kind: 'route_collection';
  id: string;
  title: string;
  map?: string;
  continue_on_failure: boolean;
  routes: RouteCollectionEntry[];
}

export interface TrialWaypointReport {
  index: number;
  outcome: 'clean' | 'turnback' | 'recovered' | 'action_missed' | 'failed';
  target: [number, number];
  actual: [number, number] | null;
  configured_radius: number;
  radius: number;
  turnback_count: number;
  unstuck_count: number;
  action_ok: boolean | null;
}

export interface TrialSuggestion {
  index: number;
  outcome: string;
  reason: string;
  x: number;
  y: number;
  radius: number;
  actual: [number, number] | null;
}

export interface TrialState {
  started_at: string;
  state: 'starting' | 'running' | 'finished' | 'failed';
  mode: 'full' | 'movement';
  map: string;
  route: string;
  start_index: number;
  current_index: number;
  pose: { x: number; y: number; heading: number | null } | null;
  message: string;
  telemetry: {
    outcome: string;
    visited_count: number;
    point_count: number;
    turnback_waypoints: number;
    recovered_waypoints: number;
    missed_actions: number;
    failed_index: number | null;
    error: string | null;
    waypoints: TrialWaypointReport[];
  } | null;
  suggestions: TrialSuggestion[];
}

export interface RecordingEvent { kind: string; index: number; heading?: number; key?: string; hold?: number }

export interface Recording {
  map: string;
  state: 'starting' | 'recording' | 'paused' | 'lost' | 'waiting_focus' | 'finished' | 'failed';
  message: string;
  started_at: string;
  pose: { x: number; y: number; heading: number | null } | null;
  samples: [number, number][];
  events: RecordingEvent[];
}

export const ACTIONS = [
  { id: '', label: '途经', color: '', hint: '路过这里，不做额外动作。' },
  { id: 'teleport', label: '传送', color: '#7c6cf0', hint: '打开地图传送到指定传送点。' },
  { id: 'wait', label: '等待', color: '#c28a2c', hint: '停下来等一会儿。' },
  { id: 'collect', label: '采集', color: '#3f9d6a', hint: '拾取这里的全部交互物。' },
  { id: 'insect', label: '捕虫', color: '#7aa23a', hint: '使用捕虫能力捉虫，需要已装备。' },
  { id: 'fishing', label: '钓鱼', color: '#2f8fc4', hint: '面向水面自动钓鱼，需要已装备能力。' },
  { id: 'bottle', label: '漂流瓶', color: '#4aa8a8', hint: '尝试拾取漂流瓶，没有也会继续。' },
  { id: 'macro', label: '连招', color: '#d0567a', hint: '执行预设的连续操作。' },
  { id: 'key', label: '按键', color: '#8a7f73', hint: '按下一个游戏按键。' },
] as const;

/** Actions the agent accepts for user routes; keep in step with USER_ACTIONS in route_test.py. */
export const SUPPORTED_ACTIONS: ReadonlySet<string> = new Set(ACTIONS.map((action) => action.id));

export const MACROS = [['starsea_fall', '原地下落攻击'], ['zhaoxi_attack', '净化攻击']] as const;

export const ROUTE_KEYS = [
  ['interact', '交互／拾取'], ['jump', '跳跃'], ['falling', '急坠'], ['attack', '净化攻击'],
  ['capture', '捕虫动作'], ['bell', '摇铃'], ['ability_use', '使用／结束能力'], ['sub_ability', '能力衍生功能'],
  ...Array.from({ length: 8 }, (_, index) => [`ability_${index+1}`, `快捷能力 ${index+1}`] as const),
] as const;

// Daily-task actions that appear in built-in routes but need setup from their task.
const TASK_ACTIONS: Record<string, string> = {
  fishing_star: '钓星', photo: '拍照', place: '摆放', music: '音乐', chat: '聊天',
  delivery: '投递', animal: '动物', minigame: '小游戏',
};

export function actionMeta(id = '') {
  return ACTIONS.find((action) => action.id === id)
    ?? { id, label: TASK_ACTIONS[id] ?? id, color: '#8a7f73', hint: '这个动作只在内置任务里使用。' };
}

const DEFAULT_PARAMS: Record<string, Record<string, unknown>> = {
  wait: { seconds: 3 },
  insect: { count: 1 },
  fishing: { cast_count: 3 },
  macro: { id: 'starsea_fall' },
  key: { key: 'interact', hold: 0.1, times: 1 },
};

export function usesFacingHeading(point: RoutePoint): boolean {
  if (point.action === 'fishing') return true;
  if (point.action === 'macro') return point.params?.id === 'zhaoxi_attack';
  if (point.action === 'key') return point.params?.key === 'attack';
  return false;
}

/** Switch a point's action, keeping nothing from the old one that no longer applies. */
export function withAction(point: RoutePoint, action: string): RoutePoint {
  const next: RoutePoint = { x: point.x, y: point.y, radius: point.radius, jump: point.jump };
  if (!action) return next;
  next.action = action;
  const previousHeading = typeof point.params?.facing_heading === 'number' ? point.params.facing_heading : undefined;
  next.params = {
    ...DEFAULT_PARAMS[action],
    ...(action === 'fishing' && previousHeading !== undefined ? { facing_heading: previousHeading } : {}),
  };
  // Stopping to act needs a tighter arrival than passing through.
  next.radius = Math.min(point.radius, 0.5);
  return next;
}

export function nearestCheckpoint(x: number, y: number, checkpoints: Checkpoint[] = [], maxDistance = 28): Checkpoint | undefined {
  let best: Checkpoint | undefined;
  let bestDist = maxDistance;
  for (const cp of checkpoints) {
    const d = Math.hypot(cp.x - x, cp.y - y);
    if (d <= bestDist) {
      bestDist = d;
      best = cp;
    }
  }
  return best;
}

export function newRouteId() {
  return `route_${Date.now().toString(36)}`;
}

export function newRoute(map: string, title: string): RouteDocument {
  return { schema_version: 3, id: newRouteId(), title, map, start_radius: 6, timeout: 900, points: [] };
}

function perpendicular(point: [number, number], start: [number, number], end: [number, number]) {
  const dx = end[0]-start[0];
  const dy = end[1]-start[1];
  const length = Math.hypot(dx, dy);
  if (!length) return Math.hypot(point[0]-start[0], point[1]-start[1]);
  return Math.abs(dy*point[0]-dx*point[1]+end[0]*start[1]-end[1]*start[0])/length;
}

/** Douglas–Peucker over indices; the ends of each stretch always stay. */
function keepIndices(samples: [number, number][], from: number, to: number, tolerance: number, kept: Set<number>) {
  const stack: [number, number][] = [[from, to]];
  while (stack.length) {
    const [start, end] = stack.pop()!;
    let farthest = -1;
    let distance = tolerance;
    for (let index = start+1; index < end; index++) {
      const current = perpendicular(samples[index], samples[start], samples[end]);
      if (current > distance) {
        distance = current;
        farthest = index;
      }
    }
    if (farthest >= 0) {
      kept.add(farthest);
      stack.push([start, farthest], [farthest, end]);
    }
  }
}

// Same bound the route key action accepts.
const MAX_KEY_TIMES = 50;

function eventPoint(event: RecordingEvent, sample: [number, number], knownCheckpoints?: Checkpoint[]): Partial<RoutePoint> {
  switch (event.kind) {
    case 'jump': return { jump: true };
    case 'interact':
    case 'collect': return { action: 'collect', params: {}, radius: 0.5 };
    case 'wait': return { action: 'wait', params: { seconds: 3 }, radius: 0.5 };
    case 'fall': return { action: 'macro', params: { id: 'starsea_fall' }, radius: 0.5 };
    case 'fishing': return { action: 'fishing', radius: 0.5, params: {
      cast_count: 3, ...(event.heading === undefined ? {} : { facing_heading: event.heading }) } };
    case 'teleport': {
      const cp = nearestCheckpoint(sample[0], sample[1], knownCheckpoints, 28);
      return { action: 'teleport', params: cp ? { checkpoint: cp } : {}, radius: 1 };
    }
    case 'key': return {
      action: 'key',
      params: {
        key: event.key,
        hold: event.hold ?? 0.1,
        times: 1,
        ...(event.key === 'attack' && event.heading !== undefined ? { facing_heading: event.heading } : {}),
      },
      radius: 0.5,
    };
    default: return { radius: 0.6 };
  }
}

/** Turn a raw recording into editable points. Pressed keys always keep their position. */
export function pointsFromRecording(
  recording: Pick<Recording, 'samples' | 'events'>,
  tolerance: number,
  knownCheckpoints?: Checkpoint[],
): RoutePoint[] {
  const { samples, events } = recording;
  if (!samples.length) return [];
  const anchors = [...new Set([0, samples.length-1, ...events.map((event) => event.index)])]
    .filter((index) => index >= 0 && index < samples.length).sort((a, b) => a-b);
  const kept = new Set(anchors);
  for (let index = 0; index+1 < anchors.length; index++) keepIndices(samples, anchors[index], anchors[index+1], tolerance, kept);
  const byIndex = new Map<number, RecordingEvent[]>();
  for (const event of events) byIndex.set(event.index, [...byIndex.get(event.index) ?? [], event]);
  const points: RoutePoint[] = [];
  for (const index of [...kept].sort((a, b) => a-b)) {
    const [x, y] = samples[index];
    const base: RoutePoint = { x, y, radius: 1, jump: false };
    const marks = byIndex.get(index) ?? [];
    const jump = marks.some((mark) => mark.kind === 'jump');
    const actions = marks.filter((mark) => mark.kind !== 'jump');
    if (!actions.length) {
      points.push({ ...base, jump });
      continue;
    }
    // Several marks at one sample still run in the order they were pressed.
    actions.forEach((mark, position) => points.push({
      ...base,
      ...eventPoint(mark, [x, y], knownCheckpoints),
      jump: jump && position === 0,
    }));
  }
  // Repeats at one spot become one point: interact presses are one collect,
  // which loops itself, and equal taps of a key (attacks, say) one key press
  // counted several times.
  const merged: RoutePoint[] = [];
  for (const point of points) {
    const previous = merged[merged.length-1];
    const near = previous && Math.hypot(point.x-previous.x, point.y-previous.y) < 1;
    if (near && point.action === 'collect' && previous.action === 'collect') continue;
    if (near && point.action === 'key' && previous.action === 'key' && !point.jump
      && point.params?.key === previous.params?.key && point.params?.hold === previous.params?.hold
      && Number(previous.params?.times) < MAX_KEY_TIMES) {
      previous.params = {
        ...previous.params,
        times: Number(previous.params?.times)+1,
        ...(previous.params?.facing_heading === undefined && point.params?.facing_heading !== undefined
          ? { facing_heading: point.params.facing_heading } : {}),
      };
      continue;
    }
    merged.push(point);
  }
  return merged;
}

export function routeLength(points: RoutePoint[]) {
  let total = 0;
  for (let index = 1; index < points.length; index++) {
    if (points[index].action === 'teleport') continue;
    total += Math.hypot(points[index].x-points[index-1].x, points[index].y-points[index-1].y);
  }
  return total;
}

export interface RouteIssue { level: 'error' | 'warn'; text: string; index?: number }

// Beyond this a straight walk between two points is almost certainly a missing teleport.
const LONG_LEG = 120;

/** What would stop the route, in terms the user can fix from the editor. */
export function routeIssues(route: RouteDocument, bounds?: { width: number; height: number }): RouteIssue[] {
  const issues: RouteIssue[] = [];
  if (!route.points.length) return [{ level: 'error', text: '还没有点位。录制一段，或在地图上添加。' }];
  if (route.points.length > 1000) issues.push({ level: 'error', text: '点位不能超过 1000 个，可以调高简化强度。' });
  if (!route.teleport) issues.push({ level: 'warn', text: '没有起始传送点，运行前要先站到第 1 点附近。' });
  route.points.forEach((point, index) => {
    if (bounds && (point.x < 0 || point.y < 0 || point.x > bounds.width || point.y > bounds.height)) {
      issues.push({ level: 'error', index, text: `第 ${index+1} 点超出了地图范围。` });
    }
    const action = point.action ?? '';
    if (!SUPPORTED_ACTIONS.has(action)) {
      issues.push({ level: 'error', index, text: `第 ${index+1} 点的动作「${actionMeta(action).label}」只能在内置任务里使用。` });
    }
    if (action === 'teleport' && !point.params?.checkpoint) {
      issues.push({ level: 'error', index, text: `第 ${index+1} 点是传送，请选择传送点。` });
    }
    const previous = route.points[index-1];
    if (previous && action !== 'teleport' && Math.hypot(point.x-previous.x, point.y-previous.y) > LONG_LEG) {
      issues.push({ level: 'warn', index, text: `第 ${index} 到第 ${index+1} 点相距很远，是不是少了一次传送？` });
    }
  });
  return issues;
}

/** Checks an imported file before it enters the editor. */
export function parseRouteFile(text: string): RouteDocument {
  const value = JSON.parse(text.replace(/^﻿/, ''));
  if (!value || !Array.isArray(value.points) || typeof value.map !== 'string') throw new Error('这不是路线文件。');
  const points = value.points.map((point: RoutePoint) => {
    if (!Number.isFinite(point?.x) || !Number.isFinite(point?.y)) throw new Error('路线里有无效的点位。');
    return { ...point, radius: Number.isFinite(point.radius) ? point.radius : 1, jump: point.jump === true };
  });
  return { schema_version: 3, start_radius: 6, timeout: 900, ...value, id: newRouteId(),
    title: typeof value.title === 'string' && value.title ? value.title : '导入的路线', points };
}
