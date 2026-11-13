import { useEffect, useMemo, useState } from 'react';
import { Image as ImageIcon, RefreshCw, Search, AlertCircle, CheckCircle2 } from 'lucide-react';
import { invoke } from '@tauri-apps/api/core';
import clsx from 'clsx';
import { toast } from 'sonner';
import { collectionShareImage } from '@/utils/collectionShareImage';
import { useAccountStateStore } from '@/stores/accountStateStore';
import { useAppStore } from '@/stores/appStore';
import { collectionSlots as slots, collectionKey as keyOf, getBlessingProgress } from '@/utils/blessingProgress';
import { realmCollectionWorkflow, realmReserveBling } from '@/utils/nikkiOptions';
import { useWardrobePlanStore } from '@/services/scoring/wardrobePlanStore';
import type { PlanMode } from '@/services/scoring/types';
import { isTauri } from '@/utils/paths';
import type { ForecastPart } from '@/services/collectionForecast';
import { BlessingForecast, type RainbowPreset } from './BlessingForecast';
import { Segmented, Stat, StatusBar } from './ui/FeaturePage';
import { PlanLayout, PlanListPanel, PlanModeSwitch, PlanQueueBar, planCheckbox, planField } from './CollectionPlan';

const SUMMARY_URL = 'https://stats.maanikki.cn/v1/summary';
const COLOR_FILL: Record<string, string> = {
  蓝: '#4f8fe0', 绿: '#4cae6b', 紫: '#9a6fdc', 粉: '#ec7fae', 黄: '#e6b53c', 红: '#e0625c', 白: '#d9dde3',
  彩: 'conic-gradient(#e0625c, #e6b53c, #4cae6b, #4f8fe0, #9a6fdc, #e0625c)',
};
const number = (value: number) => value.toLocaleString('zh-CN');
const wan = (value: number) => value >= 10000 ? `${(value / 10000).toLocaleString('zh-CN', { maximumFractionDigits: 1 })} 万` : number(value);

function ColorDot({ color, collected, label }: { color: string; collected: boolean; label: string }) {
  return <span role="img" aria-label={label} title={label}
    className={clsx('inline-block h-3.5 w-3.5 shrink-0 rounded-full', collected ? 'ring-1 ring-black/10' : 'border border-dashed border-border-strong')}
    style={collected ? { background: COLOR_FILL[color] ?? 'var(--color-accent)' } : undefined} />;
}

/** 祝福闪光 tab of 收集进度: the same 全收集 / 自选 plan, list, progress block and queue action. */
export function BlessingProgressPanel({ refreshToken }: { refreshToken: number }) {
  const { data, error, loading, refresh } = useAccountStateStore();
  const realmValues = useAppStore((state) => {
    const inst = state.instances.find((i) => i.id === state.activeInstanceId) ?? state.instances[0];
    return inst?.selectedTasks.find((t) => t.taskName === 'realm_challenge')?.optionValues;
  });
  const { payload, materials, load, updateMaterials } = useWardrobePlanStore();
  useEffect(() => { void load(`${data?.uid ?? ''}:${refreshToken}`); }, [data?.uid, refreshToken, load]);
  const planMode: PlanMode = materials.blessing_plan?.mode === 'selected' ? 'selected' : 'all';
  const chosen = useMemo(() => new Set(materials.blessing_plan?.sets ?? []), [materials.blessing_plan]);
  const [limit, setLimit] = useState(30);
  const savePlan = (mode: PlanMode, names: Set<string> = chosen) => {
    if (!payload) {
      toast.error('请先在「我的衣柜」读取一次衣柜以确认当前账号，再保存自选计划。');
      return;
    }
    updateMaterials((previous) => ({ ...previous, blessing_plan: { mode, sets: [...names] } }));
  };
  const toggleSets = (names: string[], on: boolean) => {
    const next = new Set(chosen);
    for (const name of names) { if (on) next.add(name); else next.delete(name); }
    savePlan('selected', next);
  };
  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState('missing');
  const [community, setCommunity] = useState<{ rate: number; samples: number } | null>(null);
  const [syncingPearpal, setSyncingPearpal] = useState(false);
  const snapshot = data?.blessing;
  const progress = useMemo(() => getBlessingProgress(data), [data]);
  const { sets, target, uncertain, rewardPending, partEmbryos, pearpalCheck } = progress;
  const resources = snapshot?.pursuit?.resources;
  const probability = data?.catalog?.probability;
  const rolls = data?.observations?.eureka_rolls;
  const isFarmOnly = realmValues?.CollectionWorkflow
    ? realmCollectionWorkflow(realmValues) === 'FarmOnly'
    : snapshot?.pursuit?.mode === 'farm_only';
  const configuredReserveBling = realmValues?.CollectionReserveBling
    ? realmReserveBling(realmValues)
    : Math.max(0, Number(resources?.reserve_bling ?? 0) || 0);
  const effectiveReserveBling = isFarmOnly ? 0 : configuredReserveBling;
  const decomposeEnabled = !isFarmOnly && (realmValues?.CollectionDecompose?.type === 'switch' ? realmValues.CollectionDecompose.value : true);

  const syncPearpal = async () => {
    if (!isTauri() || syncingPearpal) return;
    setSyncingPearpal(true);
    try {
      await invoke('pearpal_sync_notebook');
      await refresh();
      toast.success('已同步美鸭梨手账祝福闪光图鉴并完成核对');
    } catch (reason) {
      toast.error(String(reason));
    } finally {
      setSyncingPearpal(false);
    }
  };

  useEffect(() => {
    let cancelled = false;
    void fetch(SUMMARY_URL).then((response) => response.ok ? response.json() : null).then((summary: { colors?: { color: string; count: number }[] } | null) => {
      const samples = summary?.colors?.reduce((sum, row) => sum + row.count, 0) ?? 0;
      const rainbow = summary?.colors?.find((row) => row.color === '彩')?.count ?? 0;
      if (!cancelled && samples) setCommunity({ rate: rainbow / samples, samples });
    }).catch(() => { /* The page works offline; only this preset is absent. */ });
    return () => { cancelled = true; };
  }, []);

  const has = (name: string, slot: string, color: string) => progress.owned.has(keyOf({ name, slot, color }));
  const count = (name: string, colors: string[], slot?: string) => (slot ? [slot] : slots)
    .reduce((n, part) => n + colors.filter((color) => has(name, part, color)).length, 0);
  const completeSets = sets.filter((s) => count(s.name, s.colors) === s.colors.length * 3).length;
  const completeParts = sets.reduce((n, s) => n + slots.filter((slot) => count(s.name, s.colors, slot) === s.colors.length).length, 0);
  const rainbowOwned = sets.reduce((n, s) => n + slots.filter((slot) => has(s.name, slot, '彩')).length, 0);
  const rainbowTotal = sets.filter((s) => s.colors.includes('彩')).length * 3;

  const parts = useMemo<ForecastPart[]>(() => sets.flatMap((s) => slots.map((slot) => {
    const normal = s.colors.filter((color) => color !== '彩');
    return {
      source: s.source ?? s.name,
      normalTotal: normal.length,
      normalMissing: normal.filter((color) => !progress.owned.has(keyOf({ name: s.name, slot, color }))).length,
      rainbowMissing: s.colors.includes('彩') && !progress.owned.has(keyOf({ name: s.name, slot, color: '彩' })),
      embryos: partEmbryos[`${s.name}/${slot}`] ?? 0,
    };
  })), [sets, progress.owned, partEmbryos]);

  const presets = useMemo<RainbowPreset[]>(() => {
    const list: RainbowPreset[] = [];
    const catalogRate = probability?.iridescent ?? 0.05;
    list.push({ id: 'catalog', label: '基准', rate: catalogRate, note: '综合 MaaNikki 用户上报与社区统计的 5% 基准估算值。' });
    if (community) list.push({ id: 'community', label: '用户统计', rate: community.rate, note: `MaaNikki 用户匿名上报，共 ${number(community.samples)} 次升满。` });
    list.push({ id: 'low', label: '偏低', rate: probability?.scenarios?.[0] ?? 0.0367, note: '统计数据置信区间的下沿。' });
    list.push({ id: 'high', label: '偏高', rate: probability?.scenarios?.[2] ?? 0.0623, note: '统计数据置信区间的上沿。' });
    if (rolls?.total) list.push({ id: 'own', label: '本账号', rate: Math.max((rolls.colors['彩'] ?? 0) / rolls.total, 0.005), note: `本账号 ${rolls.total} 次升满，彩色 ${rolls.colors['彩'] ?? 0} 次。样本少时波动很大。` });
    return list;
  }, [community, probability, rolls]);
  const defaultPreset = 'catalog';

  const history = useMemo(() => Object.entries(data?.observations?.collection_history ?? {})
    .map(([day, row]) => ({ day, owned: row.owned })).sort((a, b) => a.day.localeCompare(b.day)), [data]);
  const forecastInput = useMemo(() => ({
    parts, bling: resources?.bling ?? 0, reserveBling: effectiveReserveBling,
    particles: resources?.particles ?? 0, duplicates: resources?.duplicates ?? 0,
    embryos: resources?.embryos ?? 0, capacity: resources?.capacity?.[0] ?? progress.owned.size,
    capacityLimit: resources?.capacity?.[1] ?? 1000, decompose: decomposeEnabled, blingPerRun: resources?.bling_per_run ?? 80000,
  }), [parts, resources, progress.owned.size, effectiveReserveBling, decomposeEnabled]);

  const missingBySource = useMemo(() => {
    const values = new Map<string, number>();
    const rainbow = probability?.iridescent ?? 0.05;
    for (const part of parts) {
      const k = Math.max(0, part.embryos ?? 0);
      const q = part.normalTotal > 0 ? (1 - rainbow) / part.normalTotal : 0;
      const marginal = part.normalMissing * q * Math.pow(1 - q, k)
        + (part.rainbowMissing ? rainbow * Math.pow(1 - rainbow, k) : 0);
      values.set(part.source, (values.get(part.source) ?? 0) + marginal);
    }
    return values;
  }, [parts, probability]);
  const nextSource = [...missingBySource].sort((a, b) => b[1] - a[1])[0];
  const inScope = (name: string) => planMode === 'all' || chosen.has(name);
  const scopeSets = sets.filter((s) => inScope(s.name));
  const scopeOwned = scopeSets.reduce((n, s) => n + count(s.name, s.colors), 0);
  const scopeTarget = scopeSets.reduce((n, s) => n + s.colors.length * 3, 0);
  const scopeComplete = scopeSets.filter((s) => count(s.name, s.colors) === s.colors.length * 3).length;
  // 自选 weighs only the chosen sets' missing colours, as the agent does.
  const scopeNext = (() => {
    if (planMode === 'all') return nextSource;
    const values = new Map<string, number>();
    const rainbow = probability?.iridescent ?? 0.05;
    for (const s of scopeSets) for (const slot of slots) {
      const normal = s.colors.filter((color) => color !== '彩');
      const missing = normal.filter((color) => !has(s.name, slot, color)).length;
      const k = Math.max(0, partEmbryos[`${s.name}/${slot}`] ?? 0);
      const q = normal.length > 0 ? (1 - rainbow) / normal.length : 0;
      const marginal = missing * q * Math.pow(1 - q, k)
        + (s.colors.includes('彩') && !has(s.name, slot, '彩') ? rainbow * Math.pow(1 - rainbow, k) : 0);
      const source = s.source ?? s.name;
      values.set(source, (values.get(source) ?? 0) + marginal);
    }
    return [...values].sort((a, b) => b[1] - a[1])[0];
  })();
  const particleTotal = (resources?.particles ?? 0) + (resources?.duplicates ?? 0) * 300;
  const spendableBling = resources?.bling == null ? null : Math.max(0, resources.bling - effectiveReserveBling);
  const affordableUpgrades = spendableBling == null ? 0 : Math.floor(spendableBling / 72000);
  const embryosToUpgrade = Math.min(
    resources?.embryos ?? 0,
    resources?.particles == null ? (resources?.embryos ?? 0) : Math.max(1, Math.floor(particleTotal / 360)),
  );
  const needBling = !isFarmOnly && !!resources?.embryos && spendableBling != null && affordableUpgrades < embryosToUpgrade;

  const visible = sets.filter((s) => {
    const owned = count(s.name, s.colors);
    const total = s.colors.length * 3;
    const onlyRainbow = owned < total && slots.every((slot) => s.colors.every((color) => color === '彩' || has(s.name, slot, color)));
    return `${s.name} ${s.source}`.includes(search.trim()) && (filter === 'all'
      || filter === 'complete' && owned === total || filter === 'missing' && owned < total || filter === 'rainbow' && onlyRainbow);
  });
  const updated = snapshot?.record?.updated_at;
  const copyShareImage = async () => {
    try {
      const blob = await collectionShareImage({ owned: progress.owned.size, target, percentage: progress.percentage ?? 0,
        completeSets, totalSets: sets.length, rainbowOwned, rainbowTotal });
      await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })]);
      toast.success('分享图已复制，可以直接粘贴');
    } catch {
      toast.error('复制分享图失败');
    }
  };

  const listed = [...visible].sort((a, b) => (missingBySource.get(b.source ?? '') ?? 0) - (missingBySource.get(a.source ?? '') ?? 0));
  const scopeLabel = planMode === 'all' ? `全收集 · ${number(sets.length)} 个套组` : `自选 · ${number(scopeSets.length)} 个套组`;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2 text-xs text-text-muted">
        <span className="mr-auto">{updated ? `闪光记录更新于 ${new Date(updated).toLocaleString('zh-CN', { hour12: false })}` : pearpalCheck.updatedAt ? `美鸭梨手账 ${new Date(pearpalCheck.updatedAt).toLocaleString('zh-CN', { hour12: false })}` : '尚无收集记录'}</span>
        {isTauri() && (
          <button onClick={() => void syncPearpal()} disabled={syncingPearpal || loading}
            className="flex items-center gap-1 rounded-lg border border-border bg-bg-secondary px-2.5 py-1 text-xs text-text-secondary hover:bg-bg-hover disabled:opacity-50"
            title="从美鸭梨奇想手账获取图鉴已解锁颜色，用于核对本地仓库防漂移">
            <RefreshCw className={clsx('w-3.5 h-3.5', syncingPearpal && 'animate-spin')} /><span>美鸭梨核对</span>
          </button>
        )}
        {progress.known && <button onClick={() => void copyShareImage()}
          className="flex items-center gap-1 rounded-lg border border-border bg-bg-secondary px-2.5 py-1 text-xs text-text-secondary hover:bg-bg-hover" title="复制分享图（不含 UID）">
          <ImageIcon className="w-3.5 h-3.5" />分享图</button>}
      </div>
      {error && <div className="rounded-lg border border-error/30 bg-error/5 p-3 text-xs text-error" role="alert">无法读取账号状态：{error}</div>}
      {!error && !progress.known && <div className="rounded-lg border border-border bg-bg-secondary p-3 text-sm text-text-secondary">{loading ? '正在读取本地记录…' : '尚无完整的五星收藏记录。把五星闪光加入幻境挑战顺序并运行一次，会先自动整理；也可点击上方「美鸭梨核对」预览图鉴已解锁颜色。'}</div>}
      {!error && progress.known && !progress.localKnown && pearpalCheck.synced && (
        <p className="flex items-center gap-1.5 text-xs text-text-secondary">
          <AlertCircle className="w-3.5 h-3.5 text-accent" />
          当前展示美鸭梨手账图鉴已解锁颜色（不含胚子数量、噗灵与粒子存量）；首次运行全收集时仍需扫描游戏内仓库。
        </p>
      )}
      {progress.localKnown && pearpalCheck.synced && (
        <p className={clsx('flex items-center gap-1.5 text-xs', pearpalCheck.pearpalOnlyCount > 0 ? 'text-warning' : 'text-text-secondary')}>
          {pearpalCheck.pearpalOnlyCount > 0
            ? <><AlertCircle className="w-3.5 h-3.5 shrink-0" /><span>美鸭梨图鉴核对发现 {pearpalCheck.pearpalOnlyCount} 个图鉴已解锁但本地仓库记录未包含的颜色（可能为手动升级或曾分解旧收藏），下次运行全收集将自动核对相关部位。</span></>
            : <><CheckCircle2 className="w-3.5 h-3.5 shrink-0 text-success" /><span>美鸭梨图鉴核对一致（图鉴已解锁 {pearpalCheck.pearpalCount} 色，未发现漂移遗漏）。</span></>}
        </p>
      )}
      {uncertain && <p className="flex items-center gap-1.5 text-xs text-warning"><AlertCircle className="w-3.5 h-3.5" />存在未确认操作或收藏保护待复核，以下展示上次已确认的记录，下次全收集会自动核对。</p>}
      {rewardPending && <p className="flex items-center gap-1.5 text-xs text-warning"><AlertCircle className="w-3.5 h-3.5" />上次刷取结果待核对，已收藏颜色保留，下次全收集将先处理仓库。</p>}

      <PlanModeSwitch value={planMode} onChange={(next) => savePlan(next)} allLabel="全部未收集的五星闪光颜色"
        allDetail={progress.known ? `已收集 ${number(progress.owned.size)} / ${number(target)} 色，按出新色概率跨套组挑选副本。` : '首次运行会先整理祝福闪光仓库，再按出新色概率挑选副本。'}
        selectedDetail="只追勾选套组的缺色：选本只看这些套组，收齐后交给顺序里的下一项。" />
      <PlanQueueBar goal="glow"
        status={<>{scopeLabel} · {isFarmOnly ? '只刷胚子' : '全流程托管'} · {progress.known ? (scopeTarget - scopeOwned > 0 ? `还差 ${number(scopeTarget - scopeOwned)} 色` : '所选范围已收齐') : '尚无收集记录'}</>}
        hint="执行方式、保留噗灵与爆仓分解在主界面幻境挑战设置中调整。所选范围收齐前会优先刷闪光，建议把急需的材料排在前面。"
        disabled={planMode === 'selected' && !scopeSets.length} />
      {progress.known && <StatusBar tone={needBling ? 'warn' : 'ok'} title={needBling ? '今天建议刷噗灵' : scopeNext?.[1] ? `今天建议刷「${scopeNext[0]}」` : '所选范围颜色已收齐'}
        detail={needBling
          ? (effectiveReserveBling > 0
            ? `扣除保留噗灵（${wan(effectiveReserveBling)}）后不够升级已有胚子，全部体力刷噗灵。`
            : '噗灵不够升级已有胚子，全部体力刷噗灵。')
          : scopeNext?.[1]
            ? (isFarmOnly
              ? '已结合当前仓库已有满级色与未升级胚子数量折算，刷此副本期望新色最高。'
              : effectiveReserveBling > 0 && spendableBling != null && spendableBling < 72000
                ? `这里的胚子最可能出新色；当前噗灵扣除保留（${wan(effectiveReserveBling)}）后不足升级，刷出胚子后将暂存待后续补足噗灵。`
                : '这里的胚子最可能出新色。')
            : '可以把体力用在其他目标了。'} />}

      <PlanLayout
        list={<PlanListPanel title={planMode === 'all' ? '全收集包含的套组' : '想追全哪些套组'} mode={planMode}
          includedCount={scopeSets.length} selectedCount={chosen.size} onClear={() => savePlan('selected', new Set())}
          filters={<>
            <label className="flex min-w-36 flex-1 items-center gap-1.5"><Search className="h-3.5 w-3.5 text-text-muted" />
              <input className={clsx(planField, 'w-full')} value={search} onChange={(e) => { setSearch(e.target.value); setLimit(30); }} placeholder="搜索套组或副本" aria-label="搜索套组或副本" />
            </label>
            <Segmented label="筛选收集状态" value={filter} onChange={(next) => { setFilter(next); setLimit(30); }}
              options={[['missing', '未收齐'], ['rainbow', '只缺彩色'], ['complete', '已收齐'], ['all', '全部']]} />
          </>}
          bulk={<><span>把当前列表全部：</span>
            <button type="button" onClick={() => toggleSets(listed.map((s) => s.name), true)} className="rounded-md border border-border px-2 py-0.5 hover:border-accent hover:text-accent">勾选</button>
            <button type="button" onClick={() => toggleSets(listed.map((s) => s.name), false)} className="rounded-md border border-border px-2 py-0.5 hover:border-accent hover:text-accent">取消勾选</button></>}
          empty={data && !listed.length ? '没有符合条件的套组' : undefined}
          more={{ shown: limit, total: listed.length, onMore: () => setLimit((value) => value + 30) }}>
          {listed.slice(0, limit).map((set) => {
            const owned = count(set.name, set.colors);
            const total = set.colors.length * 3;
            const picked = planMode === 'selected' && chosen.has(set.name);
            return <div key={set.name} className={clsx('flex items-center gap-2 px-4 py-2 text-xs', picked && 'bg-accent/5')}>
              {planMode === 'selected' && <input type="checkbox" aria-label={`选择${set.name}`} checked={picked} className={planCheckbox}
                onChange={(e) => toggleSets([set.name], e.target.checked)} />}
              <div className="w-28 min-w-0 shrink-0">
                <p className="truncate font-medium text-text-primary" title={set.name}>{set.name}</p>
                <p className="truncate text-[11px] text-text-muted">{set.source}{set.source === scopeNext?.[0] && !needBling && inScope(set.name) ? ' · 今日推荐' : ''}</p>
              </div>
              <div className="flex min-w-0 flex-1 flex-wrap justify-around gap-x-2 gap-y-1">
                {slots.map((slot) => <span key={slot} className="flex items-center gap-1" title={slot}>
                  {set.colors.map((color) => {
                    const collected = has(set.name, slot, color);
                    return <ColorDot key={color} color={color} collected={collected} label={`${set.name} · ${slot} · ${color}色：${collected ? '已收集' : progress.known ? '未收集' : '未记录'}`} />;
                  })}
                </span>)}
              </div>
              <span className={clsx('w-12 shrink-0 text-right tabular-nums', owned === total ? 'text-success' : 'text-text-secondary')}>{progress.known ? owned : '—'}/{total}</span>
            </div>;
          })}
        </PlanListPanel>}
        summary={<>
          <div className="rounded-xl border border-border bg-bg-secondary p-4">
            <p className="flex items-center justify-between text-xs text-text-secondary"><span>{planMode === 'all' ? '已收集颜色' : '所选套组已收集颜色'}</span><span>{scopeLabel}</span></p>
            <p className="mt-1 flex items-baseline gap-2"><strong className="text-4xl font-semibold tabular-nums">{progress.known ? scopeOwned : '—'}</strong>
              <span className="text-sm text-text-secondary tabular-nums">/ {scopeTarget || '—'}</span>
              <span className="ml-auto text-lg font-semibold tabular-nums text-accent">{progress.known && scopeTarget ? Math.round(scopeOwned / scopeTarget * 100) : '—'}%</span></p>
            <div className="mt-3 h-2 overflow-hidden rounded-full bg-bg-tertiary"><div className="h-full rounded-full bg-accent transition-[width]" style={{ width: `${scopeTarget ? scopeOwned / scopeTarget * 100 : 0}%` }} /></div>
            <dl className="mt-3 grid grid-cols-3 gap-2 text-xs text-text-secondary">
              <div><dt>收齐套组</dt><dd className="mt-0.5 text-sm font-medium tabular-nums text-text-primary">{scopeComplete}/{scopeSets.length}</dd></div>
              <div><dt>全部收齐部位</dt><dd className="mt-0.5 text-sm font-medium tabular-nums text-text-primary">{completeParts}/{sets.length * 3}</dd></div>
              <div><dt>全部彩色</dt><dd className="mt-0.5 text-sm font-medium tabular-nums text-text-primary">{rainbowOwned}/{rainbowTotal}</dd></div>
            </dl>
            {planMode === 'selected' && !scopeSets.length && <p className="mt-3 text-xs text-text-secondary">在左侧勾选想追全的套组，这里会显示所选范围的进度与推荐副本。</p>}
          </div>
          <div className="grid grid-cols-2 gap-3">
            <Stat label="噗灵" value={resources?.bling == null ? '待读取' : wan(resources.bling)}
              detail={resources?.bling == null ? '下次升级时读取' : effectiveReserveBling > 0 ? `够升级 ${affordableUpgrades} 次（保留 ${wan(effectiveReserveBling)}）` : `够升级 ${affordableUpgrades} 次`}
              warn={needBling || (!isFarmOnly && spendableBling != null && spendableBling < 72000)} />
            <Stat label="可用粒子（含重复折算）" value={resources?.particles == null ? '待读取' : wan(particleTotal)} detail={resources?.particles == null ? '下次分解时读取' : `够升满 ${Math.floor(particleTotal / 360)} 次`} />
            <Stat label="待升级胚子" value={resources ? number(resources.embryos ?? 0) : '—'} detail="有胚子时优先升级" />
            <Stat label="满级重复" value={resources ? `约 ${number(resources.duplicates ?? 0)}` : '—'} detail="每件折合 300 粒子" />
            <Stat label="仓库" value={resources?.capacity ? `${number(resources.capacity[0])}/${number(resources.capacity[1])}` : '待读取'} detail="满了会分解一批再刷本" warn={!!resources?.capacity && resources.capacity[0] >= resources.capacity[1]} />
            <Stat label="本账号彩色" value={rolls?.total ? `${rolls.colors['彩'] ?? 0} 次` : '—'}
              detail={rolls?.total ? `升满 ${number(rolls.total)} 次，按 ${((probability?.iridescent ?? 0.05) * 100).toFixed(1)}% 约应出 ${(rolls.total * (probability?.iridescent ?? 0.05)).toFixed(1)} 次` : '还没有记录，全收集下次升满后开始统计'} />
          </div>
        </>}
      />

      {progress.known && <BlessingForecast input={forecastInput} presets={presets} defaultPreset={defaultPreset} history={history} />}
    </div>
  );
}
