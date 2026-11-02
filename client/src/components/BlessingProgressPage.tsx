import { useEffect, useMemo, useState } from 'react';
import { ArrowLeft, Image as ImageIcon, RefreshCw, Search, AlertCircle, Sparkles } from 'lucide-react';
import clsx from 'clsx';
import { toast } from 'sonner';
import { collectionShareImage } from '@/utils/collectionShareImage';
import { useAccountStateStore } from '@/stores/accountStateStore';
import { collectionSlots as slots, collectionKey as keyOf, getBlessingProgress } from '@/utils/blessingProgress';
import type { ForecastPart } from '@/services/collectionForecast';
import { BlessingForecast, type RainbowPreset } from './BlessingForecast';

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

function Stat({ label, value, detail, warn }: { label: string; value: string; detail: string; warn?: boolean }) {
  return <div className="rounded-xl border border-border bg-bg-secondary px-4 py-3">
    <p className="text-xs text-text-secondary">{label}</p>
    <p className={clsx('mt-1 text-xl font-semibold tabular-nums', warn && 'text-warning')}>{value}</p>
    <p className="mt-0.5 text-xs leading-5 text-text-muted">{detail}</p>
  </div>;
}

export function BlessingProgressPage({ onClose }: { onClose: () => void }) {
  const { data, error, loading, refresh } = useAccountStateStore();
  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState('missing');
  const [community, setCommunity] = useState<{ rate: number; samples: number } | null>(null);
  const snapshot = data?.blessing;
  const progress = useMemo(() => getBlessingProgress(data), [data]);
  const { sets, target, uncertain, rewardPending } = progress;
  const resources = snapshot?.pursuit?.resources;
  const probability = data?.catalog?.probability;
  const rolls = data?.observations?.eureka_rolls;

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
    return { source: s.source ?? s.name, normalTotal: normal.length,
      normalMissing: normal.filter((color) => !progress.owned.has(keyOf({ name: s.name, slot, color }))).length,
      rainbowMissing: s.colors.includes('彩') && !progress.owned.has(keyOf({ name: s.name, slot, color: '彩' })) };
  })), [sets, progress.owned]);

  const presets = useMemo<RainbowPreset[]>(() => {
    const list: RainbowPreset[] = [];
    const catalogRate = probability?.iridescent ?? 0.054;
    if (community) list.push({ id: 'community', label: '用户统计', rate: community.rate, note: `MaaNikki 用户匿名上报，共 ${number(community.samples)} 次升满。` });
    list.push({ id: 'catalog', label: '网传', rate: catalogRate, note: `社区转述的 ${probability?.successes ?? 52}/${probability?.samples ?? 962}，未公开逐条记录。` });
    list.push({ id: 'low', label: '偏低', rate: probability?.scenarios?.[0] ?? 0.0412, note: '网传数据置信区间的下沿。' });
    list.push({ id: 'high', label: '偏高', rate: probability?.scenarios?.[2] ?? 0.0697, note: '网传数据置信区间的上沿。' });
    if (rolls?.total) list.push({ id: 'own', label: '本账号', rate: Math.max((rolls.colors['彩'] ?? 0) / rolls.total, 0.005), note: `本账号 ${rolls.total} 次升满，彩色 ${rolls.colors['彩'] ?? 0} 次。样本少时波动很大。` });
    return list;
  }, [community, probability, rolls]);
  // User statistics replace the second-hand figure once they hold more samples than it.
  const defaultPreset = community && community.samples > (probability?.samples ?? 962) ? 'community' : 'catalog';

  const history = useMemo(() => Object.entries(data?.observations?.collection_history ?? {})
    .map(([day, row]) => ({ day, owned: row.owned })).sort((a, b) => a.day.localeCompare(b.day)), [data]);
  const forecastInput = useMemo(() => ({
    parts, bling: resources?.bling ?? 0, particles: resources?.particles ?? 0, duplicates: resources?.duplicates ?? 0,
    embryos: resources?.embryos ?? 0, capacity: resources?.capacity?.[0] ?? progress.owned.size,
    capacityLimit: resources?.capacity?.[1] ?? 1000, decompose: true, blingPerRun: resources?.bling_per_run ?? 80000,
  }), [parts, resources, progress.owned.size]);

  const missingBySource = useMemo(() => {
    const values = new Map<string, number>();
    const rainbow = probability?.iridescent ?? 0.054;
    for (const part of parts) {
      values.set(part.source, (values.get(part.source) ?? 0)
        + part.normalMissing * (1 - rainbow) / part.normalTotal + (part.rainbowMissing ? rainbow : 0));
    }
    return values;
  }, [parts, probability]);
  const nextSource = [...missingBySource].sort((a, b) => b[1] - a[1])[0];
  const needBling = !!resources?.embryos && resources.bling != null && resources.bling < 72000;

  const visible = sets.filter((s) => {
    const owned = count(s.name, s.colors);
    const total = s.colors.length * 3;
    const onlyRainbow = owned < total && slots.every((slot) => s.colors.every((color) => color === '彩' || has(s.name, slot, color)));
    return `${s.name} ${s.source}`.includes(search.trim()) && (filter === 'all'
      || filter === 'complete' && owned === total || filter === 'missing' && owned < total || filter === 'rainbow' && onlyRainbow);
  });
  const groups = [...new Set(visible.map((s) => s.source))]
    .sort((a, b) => (missingBySource.get(b ?? '') ?? 0) - (missingBySource.get(a ?? '') ?? 0))
    .map((source) => ({ source, sets: visible.filter((s) => s.source === source) }));
  const updated = snapshot?.record?.updated_at;
  const particleTotal = (resources?.particles ?? 0) + (resources?.duplicates ?? 0) * 300;
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

  return (
    <div className="flex-1 min-h-0 flex flex-col bg-bg-primary text-text-primary">
      <div className="flex items-center gap-3 px-5 py-3 border-b border-border bg-bg-secondary">
        <button onClick={onClose} className="p-2 rounded-md hover:bg-bg-hover" title="返回任务列表" aria-label="返回任务列表"><ArrowLeft className="w-4 h-4" /></button>
        <h1 className="text-lg font-semibold flex-1">祝福闪光收集</h1>
        {data?.uid && <span className="text-xs tabular-nums text-text-muted">UID {data.uid}</span>}
        <span className="hidden md:inline text-xs text-text-muted">{updated ? `更新于 ${new Date(updated).toLocaleString('zh-CN', { hour12: false })}` : '尚无收集记录'}</span>
        {progress.known && <button onClick={() => void copyShareImage()}
          className="p-2 rounded-md hover:bg-bg-hover" title="复制分享图（不含 UID）" aria-label="复制分享图"><ImageIcon className="w-4 h-4" /></button>}
        <button onClick={() => void refresh().catch(() => {})} disabled={loading}
          className="p-2 rounded-md hover:bg-bg-hover disabled:opacity-50" title="刷新记录" aria-label="刷新记录"><RefreshCw className={clsx('w-4 h-4', loading && 'animate-spin')} /></button>
      </div>
      <div className="flex-1 min-h-0 overflow-auto p-4">
        <div className="mx-auto max-w-6xl space-y-4">
          {error && <div className="rounded-md border border-error/30 bg-error/5 p-3 text-xs text-error" role="alert">无法读取账号状态：{error}</div>}
          {!error && !progress.known && <div className="rounded-md border border-border bg-bg-secondary p-3 text-sm text-text-secondary">{loading ? '正在读取本地记录…' : '尚无完整的五星收藏记录。开启幻境挑战的五星全收集并运行一次，会先自动整理。'}</div>}
          {uncertain && <p className="flex items-center gap-1.5 text-xs text-warning"><AlertCircle className="w-3.5 h-3.5" />存在未确认操作或收藏保护待复核，以下展示上次已确认的记录，下次全收集会自动核对。</p>}
          {rewardPending && <p className="flex items-center gap-1.5 text-xs text-warning"><AlertCircle className="w-3.5 h-3.5" />上次刷取结果待核对，已收藏颜色保留，下次全收集将先处理仓库。</p>}

          <section className="grid gap-3 lg:grid-cols-[minmax(0,1.1fr)_minmax(0,2fr)]">
            <div className="rounded-xl border border-border bg-bg-secondary p-4">
              <p className="text-xs text-text-secondary">已收集颜色</p>
              <p className="mt-1 flex items-baseline gap-2"><strong className="text-4xl font-semibold tabular-nums">{progress.known ? progress.owned.size : '—'}</strong>
                <span className="text-sm text-text-secondary tabular-nums">/ {target || '—'}</span>
                <span className="ml-auto text-lg font-semibold tabular-nums text-accent">{progress.percentage ?? '—'}%</span></p>
              <div className="mt-3 h-2 overflow-hidden rounded-full bg-bg-tertiary"><div className="h-full rounded-full bg-accent transition-[width]" style={{ width: `${progress.percentage ?? 0}%` }} /></div>
              <dl className="mt-3 grid grid-cols-3 gap-2 text-xs text-text-secondary">
                <div><dt>收齐部位</dt><dd className="mt-0.5 text-sm font-medium tabular-nums text-text-primary">{completeParts}/{sets.length * 3}</dd></div>
                <div><dt>收齐套组</dt><dd className="mt-0.5 text-sm font-medium tabular-nums text-text-primary">{completeSets}/{sets.length}</dd></div>
                <div><dt>彩色</dt><dd className="mt-0.5 text-sm font-medium tabular-nums text-text-primary">{rainbowOwned}/{rainbowTotal}</dd></div>
              </dl>
              {progress.known && <p className="mt-3 flex items-start gap-1.5 rounded-lg bg-bg-primary px-2.5 py-2 text-xs leading-5 text-text-secondary"><Sparkles className="mt-0.5 h-3.5 w-3.5 shrink-0 text-accent" />
                {needBling ? '今天建议：噗灵不够升级已有胚子，全部体力刷噗灵。' : nextSource?.[1] ? `今天建议：刷「${nextSource[0]}」，这里的胚子最可能出新色。` : '颜色已收齐，可以把体力用在其他幻境了。'}</p>}
            </div>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
              <Stat label="噗灵" value={resources?.bling == null ? '待读取' : wan(resources.bling)} detail={resources?.bling == null ? '下次升级时读取' : `够升级 ${Math.floor(resources.bling / 72000)} 次`} warn={needBling} />
              <Stat label="可用粒子（含重复折算）" value={resources?.particles == null ? '待读取' : wan(particleTotal)} detail={resources?.particles == null ? '下次分解时读取' : `够升满 ${Math.floor(particleTotal / 360)} 次`} />
              <Stat label="待升级胚子" value={resources ? number(resources.embryos ?? 0) : '—'} detail="有胚子时优先升级" />
              <Stat label="满级重复" value={resources ? `约 ${number(resources.duplicates ?? 0)}` : '—'} detail="每件折合 300 粒子" />
              <Stat label="仓库" value={resources?.capacity ? `${number(resources.capacity[0])}/${number(resources.capacity[1])}` : '待读取'} detail="满了会分解一批再刷本" warn={!!resources?.capacity && resources.capacity[0] >= resources.capacity[1]} />
              <Stat label="本账号彩色" value={rolls?.total ? `${rolls.colors['彩'] ?? 0} 次` : '—'}
                detail={rolls?.total ? `升满 ${number(rolls.total)} 次，按 ${((probability?.iridescent ?? 0.054) * 100).toFixed(1)}% 约应出 ${(rolls.total * (probability?.iridescent ?? 0.054)).toFixed(1)} 次` : '升满后开始统计'} />
            </div>
          </section>

          {progress.known && <BlessingForecast input={forecastInput} presets={presets} defaultPreset={defaultPreset} history={history} />}

          <section className="space-y-3" aria-label="收集详情">
            <div className="flex flex-wrap items-center gap-3">
              <h2 className="text-sm font-semibold">收集详情</h2>
              <div className="inline-flex rounded-md border border-border bg-bg-secondary p-0.5">
                {[['missing', '未收齐'], ['rainbow', '只缺彩色'], ['complete', '已收齐'], ['all', '全部']].map(([value, label]) => <button key={value} onClick={() => setFilter(value)} aria-pressed={filter === value} className={clsx('rounded px-3 py-1 text-xs transition-colors', filter === value ? 'bg-accent/10 text-accent font-medium' : 'text-text-secondary hover:bg-bg-hover')}>{label}</button>)}
              </div>
              <label className="flex items-center gap-2 rounded-md border border-border bg-bg-secondary px-2.5 py-1 text-xs focus-within:border-accent/50">
                <Search className="w-3.5 h-3.5 text-text-muted" /><input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="搜索套组或副本" aria-label="搜索套组或副本" className="w-28 bg-transparent outline-none" />
              </label>
              <div className="ml-auto flex flex-wrap items-center gap-x-2.5 gap-y-1 text-xs text-text-secondary">
                {Object.keys(COLOR_FILL).map((color) => <span key={color} className="flex items-center gap-1"><ColorDot color={color} collected label={color} />{color}</span>)}
                <span className="flex items-center gap-1"><span className="h-3.5 w-3.5 rounded-full border border-dashed border-border-strong" />未收集</span>
              </div>
            </div>
            <div className="grid grid-cols-1 items-start gap-3 xl:grid-cols-2">
              {groups.map((group) => {
                const all = sets.filter((s) => s.source === group.source);
                const groupCount = all.reduce((n, s) => n + count(s.name, s.colors), 0);
                const groupTarget = all.reduce((n, s) => n + s.colors.length * 3, 0);
                return <section key={group.source} className="overflow-hidden rounded-xl border border-border bg-bg-secondary">
                  <div className="flex items-center gap-2.5 px-3 py-2.5">
                    <h3 className="flex-1 truncate text-sm font-medium">{group.source}</h3>
                    {group.source === nextSource?.[0] && !needBling && <span className="rounded bg-accent/10 px-1.5 py-0.5 text-[11px] text-accent">今日推荐</span>}
                    <span className="text-xs tabular-nums text-text-secondary">{progress.known ? groupCount : '—'} / {groupTarget}</span>
                    <div className="h-1 w-14 overflow-hidden rounded-full bg-bg-tertiary"><div className="h-full rounded-full bg-accent" style={{ width: `${groupCount / groupTarget * 100}%` }} /></div>
                  </div>
                  <table className="w-full table-fixed border-t border-border text-xs">
                    <thead className="text-text-muted"><tr><th className="w-24 px-3 py-1.5 text-left font-normal">套组</th>{slots.map((slot) => <th key={slot} className="py-1.5 font-normal">{slot}</th>)}</tr></thead>
                    <tbody>{group.sets.map((set) => <tr key={set.name} className="border-t border-border/50 hover:bg-bg-hover/30">
                      <th scope="row" className="truncate px-3 py-2 text-left text-sm font-normal" title={set.name}>{set.name}</th>
                      {slots.map((slot) => <td key={slot} className="py-2"><div className="flex justify-center gap-1">{set.colors.map((color) => {
                        const collected = has(set.name, slot, color);
                        return <ColorDot key={color} color={color} collected={collected} label={`${set.name} · ${slot} · ${color}色：${collected ? '已收集' : progress.known ? '未收集' : '未记录'}`} />;
                      })}</div></td>)}
                    </tr>)}</tbody>
                  </table>
                </section>;
              })}
            </div>
            {data && !groups.length && <p className="py-6 text-center text-xs text-text-muted">没有符合条件的套组</p>}
          </section>
        </div>
      </div>
    </div>
  );
}
