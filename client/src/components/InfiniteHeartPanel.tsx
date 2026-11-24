import { useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertCircle,
  ArrowRight,
  Check,
  ChevronDown,
  GitBranch,
  Layers,
  Lock,
  Play,
  RotateCcw,
  Search,
  Sparkles,
  Square,
  Unlock,
} from 'lucide-react';
import clsx from 'clsx';
import { toast } from 'sonner';
import { useAccountStateStore } from '@/stores/accountStateStore';
import {
  loadInfiniteHeart,
  resolveNodeState,
  saveInfiniteHeartOverrides,
  type InfiniteHeartNodeMeta,
  type InfiniteHeartNodeStateKind,
  type InfiniteHeartOverride,
  type InfiniteHeartPayload,
} from '@/services/infiniteHeartService';
import {
  startInfiniteHeartRead,
  stopWardrobeRead,
  useWardrobeStore,
  WARDROBE_INSTANCE_ID,
} from '@/services/wardrobeService';
import { computeHeartFromNodes } from '@/services/scoring/heartPresets';
import { saveScoring } from '@/services/scoring/api';
import { useWardrobePlanStore } from '@/services/scoring/wardrobePlanStore';
import { isTauri } from '@/utils/paths';
import { LogsPanel } from './LogsPanel';
import { primaryButton, secondaryButton, Segmented, StatusBar, Tile } from './ui/FeaturePage';

const num = (value: number) => value.toLocaleString('zh-CN');
const formatTime = (value: string) => new Date(value).toLocaleString('zh-CN', { hour12: false });
const field = 'rounded-lg border border-border bg-bg-primary px-2.5 py-1.5 text-xs outline-none focus:border-accent';

type StatusFilter = 'all' | 'locked' | 'ready' | 'unlocked' | 'ambiguous';
type ViewMode = 'category' | 'dependency';

interface ResolvedNode {
  meta: InfiniteHeartNodeMeta;
  state: InfiniteHeartNodeStateKind;
  level: number;
  manual: boolean;
  ready: boolean;
  remainingTokens: number;
  remainingBling: number;
  chainTokens: number;
  missingPredCount: number;
}

interface ChainGroup {
  rootId: number;
  rootTitle: string;
  categoryLabel: string;
  items: { node: ResolvedNode; depth: number }[];
  unlockedCount: number;
  remainingTokens: number;
}

function computeRemainingTokens(node: InfiniteHeartNodeMeta, state: InfiniteHeartNodeStateKind, level: number): number {
  if (state === 'unlocked') return 0;
  const remLevels = Math.max(0, node.max_level - level);
  return node.tokens_each * remLevels;
}

function computeRemainingBling(node: InfiniteHeartNodeMeta, state: InfiniteHeartNodeStateKind, level: number): number {
  if (state === 'unlocked') return 0;
  const remLevels = Math.max(0, node.max_level - level);
  return node.costs_each
    .filter((c) => c.item_id === 2)
    .reduce((sum, c) => sum + c.amount * remLevels, 0);
}

/** Build connected dependency chains in topological depth order from roots. */
function buildDependencyChains(nodes: ResolvedNode[], byId: Map<number, ResolvedNode>): ChainGroup[] {
  const idSet = new Set(nodes.map((n) => n.meta.id));
  const visited = new Set<number>();
  const groups: ChainGroup[] = [];

  // Undirected connected components within the active filtered/shard set
  const adj = new Map<number, Set<number>>();
  for (const n of nodes) {
    const id = n.meta.id;
    if (!adj.has(id)) adj.set(id, new Set());
    for (const pid of n.meta.predecessor_ids) {
      if (idSet.has(pid)) {
        adj.get(id)!.add(pid);
        if (!adj.has(pid)) adj.set(pid, new Set());
        adj.get(pid)!.add(id);
      }
    }
    for (const sid of n.meta.successor_ids) {
      if (idSet.has(sid)) {
        adj.get(id)!.add(sid);
        if (!adj.has(sid)) adj.set(sid, new Set());
        adj.get(sid)!.add(id);
      }
    }
  }

  for (const seed of nodes) {
    const startId = seed.meta.id;
    if (visited.has(startId)) continue;
    const comp: number[] = [];
    const queue = [startId];
    visited.add(startId);
    while (queue.length) {
      const cur = queue.shift()!;
      comp.push(cur);
      for (const nb of adj.get(cur) ?? []) {
        if (!visited.has(nb)) {
          visited.add(nb);
          queue.push(nb);
        }
      }
    }
    const compSet = new Set(comp);
    // Find roots inside this component (nodes with no in-component predecessor)
    const roots = comp
      .filter((id) => {
        const meta = byId.get(id)!.meta;
        return !meta.predecessor_ids.some((pid) => compSet.has(pid));
      })
      .sort((a, b) => a - b);
    const ordered: { node: ResolvedNode; depth: number }[] = [];
    const placed = new Set<number>();

    const dfs = (id: number, depth: number) => {
      if (placed.has(id)) return;
      placed.add(id);
      const item = byId.get(id);
      if (!item) return;
      ordered.push({ node: item, depth });
      const succs = item.meta.successor_ids.filter((sid) => compSet.has(sid)).sort((a, b) => a - b);
      for (const sid of succs) dfs(sid, depth + 1);
    };

    for (const r of roots.length ? roots : [Math.min(...comp)]) {
      dfs(r, 0);
    }
    for (const id of comp.sort((a, b) => a - b)) {
      if (!placed.has(id)) dfs(id, 0);
    }

    const head = ordered[0]?.node;
    if (!head) continue;
    groups.push({
      rootId: head.meta.id,
      rootTitle: head.meta.title,
      categoryLabel: head.meta.category_label,
      items: ordered,
      unlockedCount: ordered.filter((x) => x.node.state === 'unlocked').length,
      remainingTokens: ordered.reduce((sum, x) => sum + x.node.remainingTokens, 0),
    });
  }

  // Sort: incomplete chains with remaining tokens first, then by rootId
  return groups.sort(
    (a, b) =>
      Number(b.remainingTokens > 0) - Number(a.remainingTokens > 0)
      || b.items.length - a.items.length
      || a.rootId - b.rootId,
  );
}

export function InfiniteHeartPanel({ refreshToken }: { refreshToken: number }) {
  const account = useAccountStateStore((state) => state.data);
  const refreshAccount = useAccountStateStore((state) => state.refresh);
  const { payload: scoringPayload, load: loadScoring } = useWardrobePlanStore();
  const { status, mode, error } = useWardrobeStore();

  const [data, setData] = useState<InfiniteHeartPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [overrides, setOverrides] = useState<Record<string, InfiniteHeartOverride>>({});
  const [shardId, setShardId] = useState<number>(2);
  const [categoryFilter, setCategoryFilter] = useState<string>('all');
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');
  const [viewMode, setViewMode] = useState<ViewMode>('category');
  const [search, setSearch] = useState('');
  const [showLogs, setShowLogs] = useState(false);
  const [highlightId, setHighlightId] = useState<number | null>(null);
  const rowRefs = useRef<Map<number, HTMLDivElement>>(new Map());

  const heartUpdatedAt = account?.infinite_heart?.last_scan?.updated_at ?? '';

  useEffect(() => {
    let alive = true;
    setLoading(true);
    loadInfiniteHeart()
      .then((res) => {
        if (!alive) return;
        setData(res);
        setOverrides(res.overrides ?? {});
        setLoadError('');
        // Default to first scanned shard, or 伊赞之土 (2) / 心愿原野 (1)
        const scanned = res.last_scan?.scanned_shards;
        if (scanned?.length && !scanned.includes(shardId)) {
          setShardId(scanned[0]);
        }
      })
      .catch((err) => {
        if (!alive) return;
        setLoadError(String(err instanceof Error ? err.message : err));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [account?.uid, heartUpdatedAt, refreshToken]);

  useEffect(() => {
    setCategoryFilter('all');
  }, [shardId]);

  const busy = ['starting', 'reading', 'stopping'].includes(status);
  const readingHeart = busy && mode === 'infinite_heart';

  const scan = data?.last_scan ?? null;
  const catalog = data?.catalog ?? null;

  // Resolve all 383 nodes with dependency readiness & cumulative chain costs
  const { allResolved, byId } = useMemo(() => {
    const empty = { allResolved: [] as ResolvedNode[], byId: new Map<number, ResolvedNode>() };
    if (!catalog) return empty;
    const rawMap = new Map<number, { meta: InfiniteHeartNodeMeta; state: InfiniteHeartNodeStateKind; level: number; manual: boolean }>();
    for (const meta of catalog.nodes) {
      const resolved = resolveNodeState(meta, scan, overrides);
      rawMap.set(meta.id, { meta, ...resolved });
    }

    const ancestorCostMemo = new Map<number, number>();
    const calcChainTokens = (id: number, stack = new Set<number>()): number => {
      if (ancestorCostMemo.has(id)) return ancestorCostMemo.get(id)!;
      if (stack.has(id)) return 0;
      const cur = rawMap.get(id);
      if (!cur || cur.state === 'unlocked') return 0;
      // Collect full set of locked/ambiguous ancestors (including self) via BFS so diamond DAGs aren't double-counted
      const seen = new Set<number>([id]);
      const q = [id];
      let sum = 0;
      while (q.length) {
        const nid = q.shift()!;
        const entry = rawMap.get(nid);
        if (!entry || entry.state === 'unlocked') continue;
        sum += computeRemainingTokens(entry.meta, entry.state, entry.level);
        for (const pid of entry.meta.predecessor_ids) {
          if (!seen.has(pid)) {
            seen.add(pid);
            q.push(pid);
          }
        }
      }
      ancestorCostMemo.set(id, sum);
      return sum;
    };

    const list: ResolvedNode[] = [];
    const map = new Map<number, ResolvedNode>();
    for (const entry of rawMap.values()) {
      const missingPreds = entry.meta.predecessor_ids.filter((pid) => {
        const p = rawMap.get(pid);
        return p && p.state !== 'unlocked' && p.level < 1;
      });
      const ready = entry.state !== 'unlocked' && missingPreds.length === 0;
      const node: ResolvedNode = {
        meta: entry.meta,
        state: entry.state,
        level: entry.level,
        manual: entry.manual,
        ready,
        remainingTokens: computeRemainingTokens(entry.meta, entry.state, entry.level),
        remainingBling: computeRemainingBling(entry.meta, entry.state, entry.level),
        chainTokens: calcChainTokens(entry.meta.id),
        missingPredCount: missingPreds.length,
      };
      list.push(node);
      map.set(entry.meta.id, node);
    }
    return { allResolved: list, byId: map };
  }, [catalog, scan, overrides]);

  // Per-shard summaries
  const shardStats = useMemo(() => {
    const out = new Map<number, {
      unlocked: number;
      total: number;
      ready: number;
      ambiguous: number;
      remainingTokens: number;
      remainingBling: number;
      extraCosts: Map<string, number>;
    }>();
    for (const v of catalog?.versions ?? []) {
      out.set(v.id, {
        unlocked: 0,
        total: 0,
        ready: 0,
        ambiguous: 0,
        remainingTokens: 0,
        remainingBling: 0,
        extraCosts: new Map(),
      });
    }
    for (const n of allResolved) {
      const st = out.get(n.meta.version_id);
      if (!st) continue;
      st.total++;
      if (n.state === 'unlocked') st.unlocked++;
      else {
        if (n.ready) st.ready++;
        if (n.state === 'ambiguous') st.ambiguous++;
        st.remainingTokens += n.remainingTokens;
        st.remainingBling += n.remainingBling;
        const remLv = Math.max(0, n.meta.max_level - n.level);
        for (const c of n.meta.costs_each) {
          if (c.item_id !== 2) {
            st.extraCosts.set(c.name, (st.extraCosts.get(c.name) ?? 0) + c.amount * remLv);
          }
        }
      }
    }
    return out;
  }, [catalog, allResolved]);

  const activeShard = catalog?.versions.find((v) => v.id === shardId) ?? catalog?.versions[0] ?? null;
  const activeStats = activeShard ? shardStats.get(activeShard.id) ?? null : null;
  const shardNodes = useMemo(
    () => allResolved.filter((n) => n.meta.version_id === activeShard?.id),
    [allResolved, activeShard?.id],
  );

  // Per-category summaries for active shard
  const categories = useMemo(() => {
    const map = new Map<string, { key: string; label: string; unlocked: number; total: number; ready: number; remainingTokens: number }>();
    for (const n of shardNodes) {
      let c = map.get(n.meta.category);
      if (!c) {
        c = { key: n.meta.category, label: n.meta.category_label, unlocked: 0, total: 0, ready: 0, remainingTokens: 0 };
        map.set(n.meta.category, c);
      }
      c.total++;
      if (n.state === 'unlocked') c.unlocked++;
      else {
        if (n.ready) c.ready++;
        c.remainingTokens += n.remainingTokens;
      }
    }
    return [...map.values()].sort((a, b) => b.remainingTokens - a.remainingTokens || b.total - a.total);
  }, [shardNodes]);

  const q = search.trim().toLowerCase();
  const filteredNodes = useMemo(() => {
    return shardNodes.filter((n) => {
      if (categoryFilter !== 'all' && n.meta.category !== categoryFilter) return false;
      if (statusFilter === 'unlocked' && n.state !== 'unlocked') return false;
      if (statusFilter === 'locked' && n.state === 'unlocked') return false;
      if (statusFilter === 'ready' && !n.ready) return false;
      if (statusFilter === 'ambiguous' && n.state !== 'ambiguous') return false;
      if (q) {
        const hay = `${n.meta.title} ${n.meta.desc} ${n.meta.category_label}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }, [shardNodes, categoryFilter, statusFilter, q]);

  const groupedByCategory = useMemo(() => {
    const map = new Map<string, { key: string; label: string; items: ResolvedNode[]; unlocked: number; remainingTokens: number }>();
    for (const n of filteredNodes) {
      let grp = map.get(n.meta.category);
      if (!grp) {
        grp = { key: n.meta.category, label: n.meta.category_label, items: [], unlocked: 0, remainingTokens: 0 };
        map.set(n.meta.category, grp);
      }
      grp.items.push(n);
      if (n.state === 'unlocked') grp.unlocked++;
      else grp.remainingTokens += n.remainingTokens;
    }
    return [...map.values()];
  }, [filteredNodes]);

  const dependencyChains = useMemo(
    () => (viewMode === 'dependency' ? buildDependencyChains(filteredNodes, byId) : []),
    [viewMode, filteredNodes, byId],
  );

  function persistOverrides(next: Record<string, InfiniteHeartOverride>) {
    setOverrides(next);
    const key = data?.account_key;
    if (!key) {
      toast.info('当前未绑定游戏账号，先读取一次衣柜或无限之心即可持久化保存手动标记。');
      return;
    }
    void saveInfiniteHeartOverrides(key, next)
      .then(() => void refreshAccount().catch(() => {}))
      .catch((err) => toast.error(String(err)));
  }

  function toggleNodeManual(node: ResolvedNode) {
    const nextState: 'unlocked' | 'locked' = node.state === 'unlocked' ? 'locked' : 'unlocked';
    const next: Record<string, InfiniteHeartOverride> = {
      ...overrides,
      [String(node.meta.id)]: {
        state: nextState,
        level: nextState === 'unlocked' ? node.meta.max_level : 0,
      },
    };
    persistOverrides(next);
  }

  function setNodeLevelManual(node: ResolvedNode, level: number) {
    const clamped = Math.max(0, Math.min(node.meta.max_level, Math.round(level)));
    const next: Record<string, InfiniteHeartOverride> = {
      ...overrides,
      [String(node.meta.id)]: {
        state: clamped >= node.meta.max_level ? 'unlocked' : 'locked',
        level: clamped,
      },
    };
    persistOverrides(next);
  }

  function clearAllOverrides() {
    persistOverrides({});
    toast.success('已恢复为最高视野识别结果');
  }

  async function syncToPairingCalculator() {
    if (!data?.account_key) {
      toast.error('请先读取一次游戏账号数据后再同步搭配加成。');
      return;
    }
    try {
      if (!scoringPayload && account?.uid) {
        await loadScoring(`${account.uid}:heart-sync:${Date.now()}`);
      }
      const unlockedPresetIds: number[] = [];
      const flatLevels = [0, 0, 0, 0, 0];
      for (const n of allResolved) {
        if (n.meta.heart_preset_id != null && n.state === 'unlocked') {
          unlockedPresetIds.push(n.meta.heart_preset_id);
        }
        if (n.meta.flat_attr_index != null) {
          flatLevels[n.meta.flat_attr_index] = n.state === 'unlocked' ? 10 : n.level;
        }
      }
      const nextHeart = computeHeartFromNodes({ unlocked: unlockedPresetIds, flatLevels });
      const latestSettings = useWardrobePlanStore.getState().settings;
      const nextSettings = {
        ...latestSettings,
        heart: nextHeart,
        updated_at: new Date().toISOString(),
      };
      await saveScoring(data.account_key, nextSettings);
      useWardrobePlanStore.setState({ settings: nextSettings });
      toast.success(`已同步 ${unlockedPresetIds.length} 项搭配加成与五维造型等级至搭配计算器`);
    } catch (err) {
      toast.error(`同步搭配加成失败：${err instanceof Error ? err.message : String(err)}`);
    }
  }

  function jumpToNode(targetId: number) {
    const target = byId.get(targetId);
    if (!target) return;
    if (target.meta.version_id !== shardId) {
      setShardId(target.meta.version_id);
    }
    setCategoryFilter('all');
    setStatusFilter('all');
    setSearch('');
    setHighlightId(targetId);
    setTimeout(() => {
      const el = rowRefs.current.get(targetId);
      el?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }, 60);
    setTimeout(() => setHighlightId((cur) => (cur === targetId ? null : cur)), 2400);
  }

  const totalUnlocked = allResolved.filter((n) => n.state === 'unlocked').length;
  const totalNodes = allResolved.length;
  const totalAmbiguous = allResolved.filter((n) => n.state === 'ambiguous').length;
  const heldTokens = activeShard && scan?.currencies ? scan.currencies[String(activeShard.id)] : undefined;
  const tokenDeficit =
    heldTokens != null && activeStats ? Math.max(0, activeStats.remainingTokens - heldTokens) : null;
  const overrideCount = Object.keys(overrides).length;

  const readBtn = (
    <button
      type="button"
      disabled={!isTauri() || (busy && !readingHeart) || status === 'stopping'}
      onClick={() => (readingHeart ? stopWardrobeRead() : void startInfiniteHeartRead())}
      className={readingHeart ? secondaryButton : primaryButton}
    >
      {readingHeart ? <Square className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
      {readingHeart ? '停止读取' : scan ? '重新读取' : '读取无限之心'}
    </button>
  );

  const errorHere = (error && mode === 'infinite_heart') || loadError;
  const tone = readingHeart ? 'busy' : errorHere ? 'warn' : scan ? 'ok' : 'idle';
  const statusTitle = readingHeart
    ? status === 'starting'
      ? '连接游戏中'
      : status === 'stopping'
        ? '正在停止读取'
        : '正在读取无限之心'
    : scan
      ? scan.complete
        ? '无限之心解锁状态已读取'
        : '已读取部分无限之心碎片'
      : '尚未读取无限之心';
  const statusDetail = errorHere
    ? errorHere
    : scan
      ? <>
          已解锁 <strong className="tabular-nums text-text-primary">{totalUnlocked}</strong> / {totalNodes} 节点
          {totalAmbiguous > 0 && <span className="text-warning"> · {totalAmbiguous} 项待确认</span>}
          {scan.updated_at && <> · 更新于 {formatTime(scan.updated_at)}</>}
        </>
      : '自动进入「无限之心」并将三个碎片缩放到最高视野，按游戏内节点渲染规则精确提取全部解锁进度与剩余奇想星需求。';

  function renderNodeRow(node: ResolvedNode, depth = 0) {
    const { meta, state, level, ready, remainingTokens, remainingBling, chainTokens, manual } = node;
    const isHighlighted = highlightId === meta.id;
    const tokenName = activeShard?.token_name ?? '奇想星';
    const isMulti = meta.max_level > 1;
    const extraCosts = meta.costs_each.filter((c) => c.item_id !== 2);

    return (
      <div
        key={meta.id}
        ref={(el) => {
          if (el) rowRefs.current.set(meta.id, el);
          else rowRefs.current.delete(meta.id);
        }}
        style={depth > 0 ? { marginLeft: `${Math.min(depth, 5) * 18}px` } : undefined}
        className={clsx(
          'flex flex-col gap-1.5 rounded-lg border px-3 py-2.5 text-xs transition-colors',
          isHighlighted
            ? 'border-accent bg-accent/15 ring-1 ring-accent'
            : state === 'unlocked'
              ? 'border-border/60 bg-bg-primary/50 text-text-secondary'
              : ready
                ? 'border-accent/40 bg-bg-primary hover:border-accent/70'
                : 'border-border bg-bg-primary/80 hover:bg-bg-hover/40',
        )}
      >
        <div className="flex flex-wrap items-center gap-2">
          {/* Manual toggle button */}
          <button
            type="button"
            onClick={() => toggleNodeManual(node)}
            title={state === 'unlocked' ? '点击手动标为未解锁' : '点击手动标为已解锁'}
            className={clsx(
              'flex h-5 w-5 shrink-0 items-center justify-center rounded border transition-colors',
              state === 'unlocked'
                ? 'border-success bg-success text-white'
                : state === 'ambiguous'
                  ? 'border-warning bg-warning/15 text-warning'
                  : ready
                    ? 'border-accent bg-accent/10 text-accent'
                    : 'border-border bg-bg-secondary text-text-muted',
            )}
          >
            {state === 'unlocked' ? (
              <Check className="h-3.5 w-3.5" />
            ) : state === 'ambiguous' ? (
              <AlertCircle className="h-3.5 w-3.5" />
            ) : ready ? (
              <Unlock className="h-3 w-3" />
            ) : (
              <Lock className="h-3 w-3" />
            )}
          </button>

          <span className={clsx('font-medium', state === 'unlocked' ? 'text-text-secondary line-through decoration-text-muted/50' : 'text-text-primary')}>
            {meta.title}
          </span>

          {/* Status badge */}
          <span
            className={clsx(
              'rounded px-1.5 py-0.5 text-[10px] font-medium',
              state === 'unlocked'
                ? 'bg-success/15 text-success'
                : state === 'ambiguous'
                  ? 'bg-warning/15 text-warning'
                  : ready
                    ? 'bg-accent/15 text-accent'
                    : 'bg-bg-secondary text-text-muted',
            )}
          >
            {state === 'unlocked' ? '已解锁' : state === 'ambiguous' ? '待确认' : ready ? '可直接解锁' : '需先解锁前置'}
          </span>

          {viewMode === 'dependency' && (
            <span className="rounded bg-bg-secondary px-1.5 py-0.5 text-[10px] text-text-muted">
              {meta.category_label}
            </span>
          )}

          {manual && (
            <span className="rounded bg-bg-secondary px-1.5 py-0.5 text-[10px] text-text-muted">
              手动
            </span>
          )}

          {/* Multi-level selector (5 Wishfield styling nodes 0..10) */}
          {isMulti && (
            <select
              value={level}
              onChange={(e) => setNodeLevelManual(node, Number(e.target.value))}
              className="rounded border border-border bg-bg-secondary px-1.5 py-0.5 text-[11px] tabular-nums text-text-primary outline-none focus:border-accent"
            >
              {Array.from({ length: meta.max_level + 1 }, (_, lv) => (
                <option key={lv} value={lv}>
                  {lv === 0 ? '0 级 (未解锁)' : lv === meta.max_level ? `${lv} 级 (满级)` : `${lv} / ${meta.max_level} 级`}
                </option>
              ))}
            </select>
          )}

          {/* Right side cost summary */}
          <div className="ml-auto flex flex-wrap items-center gap-2 text-[11px] tabular-nums">
            {state !== 'unlocked' ? (
              <>
                {remainingTokens > 0 && (
                  <span className="font-medium text-accent">
                    {remainingTokens} {tokenName}
                  </span>
                )}
                {remainingBling > 0 && (
                  <span className="text-text-secondary">{num(remainingBling)} 噗灵</span>
                )}
                {extraCosts.map((c) => (
                  <span key={c.item_id} className="text-text-secondary">
                    {num(c.amount * Math.max(0, meta.max_level - level))} {c.name}
                  </span>
                ))}
                {!ready && chainTokens > remainingTokens && (
                  <span
                    className="rounded bg-bg-secondary px-1.5 py-0.5 text-[10px] text-text-muted"
                    title="包含所有尚未解锁的前置节点在内的累计消耗"
                  >
                    含前置共 {chainTokens} {tokenName}
                  </span>
                )}
              </>
            ) : (
              <span className="text-text-muted">
                {meta.tokens_each * meta.max_level > 0 ? `已耗 ${meta.tokens_each * meta.max_level} ${tokenName}` : '免费节点'}
              </span>
            )}
          </div>
        </div>

        {/* Description & extra unlock conditions */}
        {(meta.desc || meta.conditions.length > 0) && (
          <div className="pl-7 text-[11px] leading-4 text-text-secondary">
            {meta.desc && <span>{meta.desc}</span>}
            {meta.conditions.length > 0 && (
              <span className="ml-2 text-text-muted">
                解锁条件：{meta.conditions.join('；')}
              </span>
            )}
          </div>
        )}

        {/* Prerequisite & downstream dependency chips */}
        {(meta.predecessor_ids.length > 0 || meta.successor_ids.length > 0) && (
          <div className="flex flex-wrap items-center gap-1.5 pl-7 text-[11px]">
            {meta.predecessor_ids.length > 0 && (
              <>
                <span className="text-text-muted">前置：</span>
                {meta.predecessor_ids.map((pid) => {
                  const p = byId.get(pid);
                  if (!p) return null;
                  const ok = p.state === 'unlocked' || p.level >= 1;
                  return (
                    <button
                      key={pid}
                      type="button"
                      onClick={() => jumpToNode(pid)}
                      className={clsx(
                        'inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] transition-colors hover:underline',
                        ok ? 'bg-success/10 text-success' : 'bg-warning/10 text-warning',
                      )}
                    >
                      {ok ? '✓' : '✗'} {p.meta.title}
                    </button>
                  );
                })}
              </>
            )}
            {meta.successor_ids.length > 0 && (
              <>
                <span className="ml-1 inline-flex items-center gap-0.5 text-text-muted">
                  <ArrowRight className="h-3 w-3" />后续：
                </span>
                {meta.successor_ids.slice(0, 5).map((sid) => {
                  const s = byId.get(sid);
                  if (!s) return null;
                  return (
                    <button
                      key={sid}
                      type="button"
                      onClick={() => jumpToNode(sid)}
                      className="rounded bg-bg-secondary px-1.5 py-0.5 text-[10px] text-text-secondary hover:bg-bg-hover hover:text-text-primary"
                    >
                      {s.meta.title}
                    </button>
                  );
                })}
                {meta.successor_ids.length > 5 && (
                  <span className="text-[10px] text-text-muted">+{meta.successor_ids.length - 5} 项</span>
                )}
              </>
            )}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <StatusBar tone={tone} title={statusTitle} detail={statusDetail}>
        {scan && (
          <button
            type="button"
            onClick={() => void syncToPairingCalculator()}
            className={secondaryButton}
            title="将已解锁的心愿原野与星海搭配潜力加成一键同步到搭配计算器"
          >
            <Sparkles className="h-3.5 w-3.5" />
            同步搭配加成
          </button>
        )}
        <button
          type="button"
          onClick={() => setShowLogs((value) => !value)}
          aria-expanded={showLogs || readingHeart}
          className={secondaryButton}
        >
          读取日志
          <ChevronDown className={clsx('h-3.5 w-3.5 transition-transform', (showLogs || readingHeart) && 'rotate-180')} />
        </button>
        {readBtn}
      </StatusBar>

      {(showLogs || readingHeart) && (
        <section className="overflow-hidden rounded-xl border border-border bg-bg-secondary">
          <div className="h-44"><LogsPanel instanceId={WARDROBE_INSTANCE_ID} /></div>
        </section>
      )}

      {loading && !catalog ? (
        <section className="rounded-xl border border-border bg-bg-secondary p-8 text-center text-xs text-text-secondary">
          正在加载无限之心节点数据...
        </section>
      ) : !catalog || !activeShard || !activeStats ? (
        <section className="rounded-xl border border-dashed border-border bg-bg-secondary p-8 text-center text-xs text-text-secondary">
          {loadError || '未找到无限之心目录数据。'}
        </section>
      ) : (
        <>
          {/* 3-Shard Switcher */}
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
            {catalog.versions.map((v) => {
              const st = shardStats.get(v.id);
              const pct = st && st.total > 0 ? Math.round((st.unlocked / st.total) * 100) : 0;
              const selected = v.id === activeShard.id;
              return (
                <button
                  key={v.id}
                  type="button"
                  onClick={() => setShardId(v.id)}
                  className={clsx(
                    'flex flex-col gap-1.5 rounded-xl border p-3 text-left transition-colors',
                    selected
                      ? 'border-accent bg-accent/10'
                      : 'border-border bg-bg-secondary hover:bg-bg-hover/60',
                  )}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className={clsx('text-sm font-semibold', selected ? 'text-accent' : 'text-text-primary')}>
                      {v.name}
                    </span>
                    <span className="text-xs font-medium tabular-nums text-text-secondary">
                      {st?.unlocked ?? 0} / {st?.total ?? v.node_count}
                    </span>
                  </div>
                  <div className="h-1.5 overflow-hidden rounded-full bg-bg-primary">
                    <div
                      className={clsx('h-full rounded-full transition-all', selected ? 'bg-accent' : 'bg-success')}
                      style={{ width: `${pct}%` }}
                    />
                  </div>
                  <div className="flex items-center justify-between text-[11px] tabular-nums text-text-muted">
                    <span>
                      {st && st.remainingTokens > 0
                        ? `还需 ${num(st.remainingTokens)} ${v.token_name}`
                        : `已全解锁 (${v.total_tokens} ${v.token_name})`}
                    </span>
                    <span>{pct}%</span>
                  </div>
                </button>
              );
            })}
          </div>

          {/* Resource & Progress Summary Tiles */}
          <section className="space-y-3 rounded-xl border border-border bg-bg-secondary p-4">
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              <Tile
                label={`全解锁还需${activeShard.token_name}`}
                value={
                  <>
                    {num(activeStats.remainingTokens)}
                    <span className="ml-1 text-xs font-normal text-text-muted">
                      / {num(activeShard.total_tokens)}
                    </span>
                  </>
                }
                detail={
                  heldTokens != null
                    ? `当前持有 ${num(heldTokens)}${tokenDeficit != null && tokenDeficit > 0 ? ` · 缺口 ${num(tokenDeficit)}` : ' · 已足够'}`
                    : undefined
                }
              />
              <Tile
                label="还需噗灵"
                value={num(activeStats.remainingBling)}
                detail={
                  activeStats.extraCosts.size > 0
                    ? [...activeStats.extraCosts.entries()].map(([k, v]) => `另需 ${num(v)} ${k}`).join(' · ')
                    : undefined
                }
              />
              <Tile
                label="节点解锁进度"
                value={
                  <>
                    {activeStats.unlocked}
                    <span className="ml-1 text-xs font-normal text-text-muted">/ {activeStats.total}</span>
                  </>
                }
                detail={
                  activeStats.ambiguous > 0
                    ? `其中 ${activeStats.ambiguous} 项可手动核对`
                    : `${Math.round((activeStats.unlocked / Math.max(1, activeStats.total)) * 100)}% 已完成`
                }
              />
              <Tile
                label="当前可直接解锁"
                value={
                  <>
                    {activeStats.ready}
                    <span className="ml-1 text-xs font-normal text-text-muted">项</span>
                  </>
                }
                detail="前置依赖已全部满足"
              />
            </div>

            {/* Category Progress Grid */}
            <div className="space-y-2 border-t border-border pt-3">
              <div className="flex items-center justify-between">
                <span className="text-xs font-medium text-text-secondary">
                  按类别解锁进度（点击类别筛选条目）
                </span>
                {categoryFilter !== 'all' && (
                  <button
                    type="button"
                    onClick={() => setCategoryFilter('all')}
                    className="text-xs text-accent hover:underline"
                  >
                    查看全部类别
                  </button>
                )}
              </div>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
                {categories.map((cat) => {
                  const pct = cat.total > 0 ? Math.round((cat.unlocked / cat.total) * 100) : 0;
                  const active = categoryFilter === cat.key;
                  const done = cat.unlocked === cat.total;
                  return (
                    <button
                      key={cat.key}
                      type="button"
                      onClick={() => setCategoryFilter(active ? 'all' : cat.key)}
                      className={clsx(
                        'flex flex-col gap-1 rounded-lg border px-2.5 py-2 text-left transition-colors',
                        active
                          ? 'border-accent bg-accent/10'
                          : 'border-border bg-bg-primary hover:bg-bg-hover/60',
                      )}
                    >
                      <div className="flex items-center justify-between gap-1 text-xs">
                        <span className="truncate font-medium text-text-primary">{cat.label}</span>
                        <span className={clsx('shrink-0 tabular-nums', done ? 'text-success' : 'text-text-secondary')}>
                          {cat.unlocked}/{cat.total}
                        </span>
                      </div>
                      <div className="h-1 overflow-hidden rounded-full bg-bg-secondary">
                        <div
                          className={clsx('h-full rounded-full', done ? 'bg-success' : 'bg-accent')}
                          style={{ width: `${pct}%` }}
                        />
                      </div>
                      <div className="flex items-center justify-between text-[10px] tabular-nums text-text-muted">
                        <span>
                          {done
                            ? '已全解锁'
                            : `还需 ${cat.remainingTokens} ${activeShard.token_name}`}
                        </span>
                        {cat.ready > 0 && <span className="text-accent">{cat.ready} 可解锁</span>}
                      </div>
                    </button>
                  );
                })}
              </div>
            </div>

            {/* Filter & View Bar */}
            <div className="flex flex-wrap items-center gap-2 border-t border-border pt-3">
              <Segmented<ViewMode>
                label="查看方式"
                value={viewMode}
                onChange={setViewMode}
                options={[
                  ['category', '按类别条目'],
                  ['dependency', '按依赖链路'],
                ]}
              />

              <Segmented<StatusFilter>
                label="解锁状态"
                value={statusFilter}
                onChange={setStatusFilter}
                options={[
                  ['all', '全部'],
                  ['locked', '未解锁'],
                  ['ready', '可解锁'],
                  ['unlocked', '已解锁'],
                  ...(activeStats.ambiguous > 0 ? [['ambiguous', `待确认(${activeStats.ambiguous})`] as const] : []),
                ]}
              />

              <label className="relative ml-auto flex min-w-44 items-center">
                <Search className="pointer-events-none absolute left-2.5 h-3.5 w-3.5 text-text-muted" />
                <input
                  type="text"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder="搜索节点名称或效果..."
                  className={clsx(field, 'w-full pl-8')}
                />
              </label>

              {overrideCount > 0 && (
                <button
                  type="button"
                  onClick={clearAllOverrides}
                  className="inline-flex items-center gap-1 rounded-lg border border-border bg-bg-primary px-2.5 py-1.5 text-xs text-text-secondary hover:bg-bg-hover"
                  title="清除手动修改，恢复为最高视野识别结果"
                >
                  <RotateCcw className="h-3 w-3" />
                  重置手动({overrideCount})
                </button>
              )}
            </div>

            {/* Structured Node List */}
            {filteredNodes.length === 0 ? (
              <div className="rounded-lg border border-dashed border-border py-10 text-center text-xs text-text-muted">
                当前筛选条件下没有匹配的节点。
              </div>
            ) : viewMode === 'category' ? (
              <div className="space-y-4">
                {groupedByCategory.map((group) => (
                  <div key={group.key} className="space-y-1.5">
                    <div className="flex items-center justify-between px-1 text-xs">
                      <span className="inline-flex items-center gap-1.5 font-semibold text-text-primary">
                        <Layers className="h-3.5 w-3.5 text-accent" />
                        {group.label}
                        <span className="font-normal tabular-nums text-text-muted">
                          ({group.unlocked} / {group.items.length})
                        </span>
                      </span>
                      <span className="tabular-nums text-[11px] text-text-muted">
                        {group.remainingTokens > 0
                          ? `本类还需 ${num(group.remainingTokens)} ${activeShard.token_name}`
                          : '本类已全解锁'}
                      </span>
                    </div>
                    <div className="space-y-1.5">
                      {group.items.map((node) => renderNodeRow(node, 0))}
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <div className="space-y-4">
                {dependencyChains.map((chain) => (
                  <div key={chain.rootId} className="space-y-1.5 rounded-xl border border-border/70 bg-bg-primary/40 p-3">
                    <div className="flex flex-wrap items-center justify-between gap-2 pb-1 text-xs">
                      <span className="inline-flex items-center gap-1.5 font-semibold text-text-primary">
                        <GitBranch className="h-3.5 w-3.5 text-accent" />
                        {chain.rootTitle}
                        <span className="rounded bg-bg-secondary px-1.5 py-0.5 text-[10px] font-normal text-text-muted">
                          {chain.categoryLabel}
                        </span>
                        <span className="font-normal tabular-nums text-text-muted">
                          {chain.unlockedCount} / {chain.items.length} 节点
                        </span>
                      </span>
                      <span className="tabular-nums text-[11px] text-text-muted">
                        {chain.remainingTokens > 0
                          ? `整条链路还需 ${num(chain.remainingTokens)} ${activeShard.token_name}`
                          : '整条链路已全解锁'}
                      </span>
                    </div>
                    <div className="space-y-1.5">
                      {chain.items.map(({ node, depth }) => renderNodeRow(node, depth))}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}
