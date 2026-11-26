import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AlertCircle, BookOpen, Download, Loader2, RefreshCw, Search, Shirt, Sparkles, X } from 'lucide-react';
import { invoke } from '@tauri-apps/api/core';
import { toast } from 'sonner';
import clsx from 'clsx';
import { refreshResonance, resonanceLabel, type ResonanceHistory, useResonanceStore, useResonanceViewStore } from '@/services/resonanceService';
import { getPearpalLoginState, openPearpalLogin, usePearpalStore } from '@/services/pearpalService';
import { activeBanners, collectionOutfits, historyRows, resonanceSummary } from '@/services/resonanceData';
import { useSuitThumbMap, useSuitThumbsStatus } from '@/services/scoring/suitThumbs';
import { isTauri } from '@/utils/paths';
import { MapResourcesPanel } from './MapResourcesPanel';
import { PearpalStatusPill } from './PearpalAccount';
import { ResonanceControl } from './ResonanceControl';
import { FeaturePage, primaryButton, secondaryButton, Segmented, StatusBar } from './ui/FeaturePage';
import { qualityLabel, suitBackdropClass } from './WardrobeSuitDetailModal';

function timeLabel(value: number | null | undefined) {
  return value ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false }) : '尚未同步';
}
function pullTime(value: string) {
  if (/^\d+$/.test(value)) return timeLabel(Number(value) / (value.length > 10 ? 1000 : 1));
  return value;
}
function activeBannerCopy(item: ReturnType<typeof activeBanners>[number]) {
  const since = item.since > 0 ? ` · 距上次五星至少 ${item.since} 抽` : '';
  if (!item.total) {
    return { headline: '套装目录待更新', detail: `当前目录暂未收录该卡池套装${since}` };
  }
  if (!item.missing) {
    const progress = item.pulls > 0
      ? `共 ${item.total} 件 · ${item.pulls} 抽起`
      : item.bannerPulls > 0 ? `共 ${item.total} 件 · 已抽 ${item.bannerPulls} 次` : `共 ${item.total} 件 · 均已获得`;
    return { headline: '已全部集齐', detail: `${progress}${since}` };
  }
  const need = `${item.hasOwnAverage ? '按你的平均，' : ''}约需 ${item.need} 抽`;
  if (!item.hasPulled && !item.obtained) {
    return { headline: '尚未抽取', detail: `共 ${item.total} 件 · ${need}` };
  }
  const pulledNote = !item.pulls && item.bannerPulls > 0 ? `已抽 ${item.bannerPulls} 次 · ` : '';
  return { headline: `还差 ${item.missing} 件`, detail: `${pulledNote}${need}${since}` };
}
function bannerKindLabel(kind: number | undefined) {
  if (kind === 1) return '常驻池';
  if (kind === 2) return '限定五星池';
  if (kind === 3) return '限定四星池';
  if (kind === 5) return '星冕限定池';
  return '共鸣池';
}
const selectStyle = 'max-w-full rounded-lg border border-border bg-bg-secondary px-3 py-2 text-xs outline-none focus:border-accent';

export function ResonancePage({ onClose }: { onClose: () => void }) {
  const { status, connectionError } = useResonanceStore();
  const [selected, setSelected] = useState('');
  const [data, setData] = useState<ResonanceHistory | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [action, setAction] = useState(false);
  const pearpalStatus = usePearpalStore((s) => s.status);
  const pearpalBusy = usePearpalStore((s) => s.busy);
  const pearpalState = getPearpalLoginState(pearpalStatus);
  const [banner, setBanner] = useState('all');
  const [rarity, setRarity] = useState('all');
  const [search, setSearch] = useState('');
  const view = useResonanceViewStore((s) => s.view);
  const setView = useResonanceViewStore((s) => s.setView);
  const [completion, setCompletion] = useState('all');
  const [page, setPage] = useState(0);
  const [hoverBar, setHoverBar] = useState<{ pulls: number; count: number } | null>(null);
  const revision = useRef(0);
  const accountKey = selected || status.active_account || status.accounts[0]?.key || '';
  const account = status.accounts.find((item) => item.key === accountKey);
  const refreshHistory = useCallback(async () => {
    const request = ++revision.current;
    if (!accountKey || !account) { setData(null); setLoading(false); return; }
    setLoading(true);
    try {
      const value = await invoke<ResonanceHistory>('resonance_history', { accountKey });
      if (revision.current === request) { setData(value); setError(''); }
    } catch (reason) {
      if (revision.current === request) { setData(null); setError(String(reason)); }
    } finally { if (revision.current === request) setLoading(false); }
  }, [accountKey, account?.total, account?.notebook_total, account?.last_success, status.busy]);

  useEffect(() => { void refreshHistory(); return () => { revision.current++; }; }, [refreshHistory]);
  useEffect(() => { setPage(0); }, [accountKey, banner, rarity, search, view, completion]);
  useEffect(() => { setBanner('all'); setRarity('all'); }, [accountKey]);

  // Keep a previous account's response out of the newly selected account view.
  const current = data?.history.account_key === accountKey ? data : null;
  const { status: thumbStatus } = useSuitThumbsStatus();
  const outfitThumbLookup = current?.outfit_thumbs ?? {};
  const banners = useMemo(() => new Map(current?.banners.map((item) => [item.id, item]) ?? []), [current]);
  const bannerMeta = useMemo(() => {
    const map = new Map<string, {
      outfitIds: string[];
      outfitNames: string[];
      thumbKeys: string[];
      maxRarity: number;
      optionLabel: string;
      primaryName: string;
    }>();
    if (!current) return map;
    for (const entry of current.banners) {
      const sorted = [...entry.outfits]
        .map((oid) => ({ oid, info: current.outfits[oid] }))
        .sort((a, b) => (b.info?.rarity ?? 0) - (a.info?.rarity ?? 0));
      const outfitIds = sorted.map((item) => item.oid);
      const outfitNames = sorted.map((item) => item.info?.name).filter((name): name is string => Boolean(name));
      const thumbKeys = outfitIds.map((oid) => outfitThumbLookup[oid]).filter((key): key is string => Boolean(key));
      const maxRarity = sorted[0]?.info?.rarity ?? (entry.kind === 3 ? 4 : 5);
      const primaryName = outfitNames[0] ?? '';
      const optionLabel = entry.kind === 1
        ? `${entry.name}（常驻）`
        : outfitNames.length > 0
          ? `${entry.name} · ${outfitNames.slice(0, 2).join(' / ')}`
          : entry.name;
      map.set(entry.id, { outfitIds, outfitNames, thumbKeys, maxRarity, optionLabel, primaryName });
    }
    return map;
  }, [current, outfitThumbLookup]);
  const active = useMemo(() => current ? activeBanners(current) : [], [current]);
  const knownBanners = useMemo(() => {
    if (!current) return [];
    const ids = view === 'records'
      ? Object.entries(current.history.banners).filter(([, entry]) => entry.pulls.length).map(([id]) => id)
      : [...new Set([
          ...Object.entries(current.history.notebook.pulls).filter(([, pool]) => Object.keys(pool || {}).length).map(([id]) => id),
          ...active.map((item) => item.id),
        ])];
    return ids.sort((a, b) => Number(b) - Number(a));
  }, [current, view, active]);
  const allOutfits = useMemo(() => current ? collectionOutfits(current, 'all') : [], [current]);
  const q = search.trim();
  const outfits = useMemo(() => current ? collectionOutfits(current, banner).filter((outfit) =>
    (rarity === 'all' || String(outfit.rarity) === rarity)
    && (completion === 'all' || (completion === 'complete' ? outfit.obtained === outfit.items.length : outfit.obtained < outfit.items.length))
    && (!q || (outfit.name + ' ' + outfit.banner + ' ' + outfit.parts.map((part) => part.name).join(' ')).includes(q))) : [],
  [current, banner, rarity, completion, q]);
  const rows = useMemo(() => current ? historyRows(current).filter((row) => {
    const meta = bannerMeta.get(row.bannerId);
    return (banner === 'all' || row.bannerId === banner)
      && (rarity === 'all' || String(row.rarity) === rarity)
      && (!q || (row.name + ' ' + row.item + ' ' + (banners.get(row.bannerId)?.name || row.bannerId) + ' ' + (meta?.outfitNames.join(' ') ?? '')).includes(q));
  }) : [],
  [current, banner, rarity, q, banners, bannerMeta]);
  const summary = useMemo(() => current ? resonanceSummary(current) : null, [current]);
  const distribution = summary?.distribution ?? [];
  const peak = Math.max(1, ...distribution.map((bar) => bar.count));
  const completeOutfits = allOutfits.filter((outfit) => outfit.obtained === outfit.items.length).length;
  const possibleGap = current && Object.values(current.history.banners).some((entry) => entry.possible_gap);
  const size = view === 'records' ? 50 : 12;
  const count = view === 'records' ? rows.length : outfits.length;
  const pages = Math.max(1, Math.ceil(count / size));
  const currentPage = Math.min(page, pages - 1);
  const pageOutfits = useMemo(
    () => (view === 'outfits' ? outfits.slice(currentPage * size, currentPage * size + size) : []),
    [view, outfits, currentPage, size],
  );
  const pageRows = useMemo(
    () => (view === 'records' ? rows.slice(currentPage * size, currentPage * size + size) : []),
    [view, rows, currentPage, size],
  );
  const bannerPullCounts = useMemo(() => {
    const counts = new Map<string, number>();
    if (!current) return counts;
    for (const [id, pool] of Object.entries(current.history.notebook.pulls)) {
      const vals = Object.values(pool || {});
      if (vals.length) counts.set(id, Math.max(...vals.map((r) => r.pool_cnt)) + 1);
    }
    for (const [id, entry] of Object.entries(current.history.banners)) {
      if (!counts.has(id) && entry.pulls.length) counts.set(id, entry.pulls.length);
    }
    return counts;
  }, [current]);
  const visibleThumbKeys = useMemo(() => {
    const keys: string[] = [];
    for (const item of active) {
      for (const oid of item.outfitIds.slice(0, 2)) {
        const key = outfitThumbLookup[oid];
        if (key) keys.push(key);
      }
    }
    for (const id of knownBanners) {
      const key = bannerMeta.get(id)?.thumbKeys[0];
      if (key) keys.push(key);
    }
    if (banner !== 'all') {
      const selectedMeta = bannerMeta.get(banner);
      if (selectedMeta) keys.push(...selectedMeta.thumbKeys.slice(0, 4));
    }
    for (const outfit of pageOutfits) {
      const key = outfitThumbLookup[outfit.id];
      if (key) keys.push(key);
    }
    for (const row of pageRows) {
      const key = bannerMeta.get(row.bannerId)?.thumbKeys[0];
      if (key) keys.push(key);
    }
    return keys;
  }, [active, knownBanners, banner, bannerMeta, outfitThumbLookup, pageOutfits, pageRows]);
  const thumbMap = useSuitThumbMap(visibleThumbKeys, thumbStatus);
  const selectedBannerInfo = banner !== 'all' ? banners.get(banner) ?? null : null;
  const selectedBannerMeta = banner !== 'all' ? bannerMeta.get(banner) ?? null : null;

  async function run(command: 'resonance_sync') {
    setAction(true);
    try { await invoke(command); await refreshResonance(); }
    catch (reason) { toast.error(String(reason)); }
    finally { setAction(false); }
  }
  async function exportHistory() {
    if (!accountKey || !current) return;
    setAction(true);
    try {
      const { save } = await import('@tauri-apps/plugin-dialog');
      const path = await save({ defaultPath: 'MaaNikki-共鸣记录-CN-' + current.history.uid + '-' + new Date().toISOString().slice(0, 10) + '.json',
        filters: [{ name: 'JSON', extensions: ['json'] }] });
      if (!path) return;
      const content = await invoke<string>('resonance_export', { accountKey });
      const { writeTextFile } = await import('@tauri-apps/plugin-fs');
      await writeTextFile(path, content);
      toast.success('两种来源的共鸣记录已导出');
    } catch (reason) { toast.error(String(reason)); }
    finally { setAction(false); }
  }

  const best = [...distribution].sort((a, b) => b.count - a.count)[0];
  return <FeaturePage icon={BookOpen} title="共鸣记录" onBack={onClose} actions={<>
      <PearpalStatusPill />
      <select value={accountKey} onChange={(event) => setSelected(event.target.value)} aria-label="记录账号" className={clsx(selectStyle, '!py-1.5')}>
        {!status.accounts.length && <option value="">尚无账号记录</option>}
        {status.accounts.map((item) => <option key={item.key} value={item.key}>UID {item.uid}</option>)}
      </select>
      <button disabled={!current || action} onClick={() => void exportHistory()} title="导出 JSON" aria-label="导出 JSON" className="rounded-lg p-2 hover:bg-bg-hover disabled:opacity-40"><Download className="h-4 w-4" /></button>
    </>}>
        <StatusBar tone={status.error || connectionError ? 'warn' : status.busy ? 'busy' : status.enabled ? 'ok' : 'idle'} title={resonanceLabel(status)}
          detail={status.busy ? status.banner ? status.banner + (status.page ? ' · 第 ' + status.page + ' 页' : '') : '正在验证美鸭梨账号'
            : '最近同步 ' + timeLabel(account?.last_success) + (status.enabled && !!status.next_sync && !status.auth_paused ? ' · 下次 ' + timeLabel(status.next_sync) : '')}>
          <ResonanceControl />
          <button
            disabled={!isTauri() || action || pearpalBusy}
            onClick={() => void openPearpalLogin({ switchAccount: pearpalState !== 'logged_out' })}
            className={
              pearpalState === 'logged_out'
                ? primaryButton
                : pearpalState === 'expired'
                  ? clsx(secondaryButton, 'border-warning/50 bg-warning/10 font-medium text-warning hover:bg-warning/20')
                  : secondaryButton
            }
          >
            {pearpalState === 'logged_in' ? '切换美鸭梨账号' : pearpalState === 'expired' ? '重新登录美鸭梨' : '登录美鸭梨'}
          </button>
          <button
            disabled={!isTauri() || action || status.busy}
            onClick={() => void run('resonance_sync')}
            className={pearpalState === 'logged_in' ? primaryButton : secondaryButton}
          >
            <RefreshCw className={clsx('h-3.5 w-3.5', status.busy && 'animate-spin')} />立即同步
          </button>
        </StatusBar>
        {(status.error || connectionError || error) && <p role="alert" className="flex items-center gap-2 rounded-xl border border-error/25 bg-error/5 p-3 text-sm text-error"><AlertCircle className="h-4 w-4 shrink-0" />{connectionError || error || status.error}</p>}

        {summary && <section className="grid gap-3.5 lg:grid-cols-[minmax(0,1.05fr)_minmax(0,1.25fr)]">
          <div className="relative overflow-hidden rounded-2xl border border-border bg-bg-secondary p-5 shadow-xs">
            <div className="pointer-events-none absolute -right-12 -top-12 h-40 w-40 rounded-full bg-accent/10 blur-2xl" />
            <div className="relative flex flex-wrap items-end justify-between gap-3">
              <div>
                <p className="text-xs font-medium text-text-secondary">累计共鸣</p>
                <p className="mt-1 flex items-baseline gap-2">
                  <strong className="text-4xl font-bold tracking-tight tabular-nums">{summary.total.toLocaleString('zh-CN')}</strong>
                  <span className="text-sm text-text-secondary">次起</span>
                </p>
              </div>
              <div className="min-w-32 rounded-xl border border-border/70 bg-bg-primary/75 px-3 py-2 text-right">
                <p className="text-[11px] text-text-muted">套装集齐</p>
                <p className="mt-0.5 text-sm font-semibold tabular-nums">
                  <span className="text-success">{completeOutfits}</span>
                  <span className="mx-1 text-text-muted">/</span>
                  <span>{allOutfits.length} 套</span>
                </p>
                {allOutfits.length > 0 && (
                  <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-border">
                    <div className="h-full rounded-full bg-success transition-all" style={{ width: `${(completeOutfits / allOutfits.length) * 100}%` }} />
                  </div>
                )}
              </div>
            </div>
            <div className="relative mt-4 grid grid-cols-3 gap-2.5">{[
              { label: '限定五星', value: summary.limitedFive.count, detail: '每件 ' + summary.limitedFive.average.toFixed(1) + ' 抽', bar: 'bg-amber-500' },
              { label: '常驻五星', value: summary.standardFive.count, detail: '每件 ' + summary.standardFive.average.toFixed(1) + ' 抽', bar: 'bg-amber-400/75' },
              { label: '限定四星', value: summary.limitedFour.count, detail: '每件 ' + summary.limitedFour.average.toFixed(1) + ' 抽', bar: 'bg-accent' },
            ].map((item) => <div key={item.label} className="relative overflow-hidden rounded-xl border border-border/70 bg-bg-primary/75 px-3 py-2.5">
              <span aria-hidden="true" className={clsx('absolute inset-x-0 top-0 h-0.5', item.bar)} />
              <p className="text-[11px] text-text-secondary">{item.label}</p>
              <p className="mt-1 text-xl font-semibold tabular-nums tracking-tight">{item.value}</p>
              <p className="mt-0.5 text-[11px] tabular-nums text-text-muted">{item.detail}</p>
            </div>)}</div>
            <div className="relative mt-3.5 flex flex-wrap items-center gap-1.5 text-[11px]">
              {summary.earlyShare > 0 && (
                <span className="inline-flex items-center gap-1 rounded-full border border-success/30 bg-success/10 px-2.5 py-0.5 font-medium text-success">
                  <Sparkles className="h-3 w-3" />10 抽内出金 {(summary.earlyShare * 100).toFixed(0)}%
                </span>
              )}
              <span className="inline-flex items-center rounded-full border border-warning/30 bg-warning/10 px-2.5 py-0.5 text-warning">
                18 抽后保底 {(summary.hardShare * 100).toFixed(0)}%
              </span>
              <span className="text-text-muted">五星最多 20 抽必出</span>
            </div>
          </div>

          <div className="flex flex-col rounded-2xl border border-border bg-bg-secondary p-5 shadow-xs">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-sm font-semibold">五星出货分布</h2>
              <span className="rounded-md bg-accent/10 px-2 py-0.5 text-xs font-medium tabular-nums text-accent">
                平均 {summary.fiveAverage.toFixed(1)} 抽
              </span>
              <span className="ml-auto rounded-md bg-bg-primary px-2 py-0.5 text-xs tabular-nums text-text-secondary">
                {hoverBar ? `第 ${hoverBar.pulls} 抽：${hoverBar.count} 件` : best?.count ? `峰值：第 ${best.pulls} 抽（${best.count} 件）` : '暂无五星记录'}
              </span>
            </div>
            <div className="relative mt-4 flex h-36 flex-1 items-end gap-1" role="img" aria-label={distribution.map((bar) => `第 ${bar.pulls} 抽 ${bar.count} 件`).join('，')} onMouseLeave={() => setHoverBar(null)}>
              <div aria-hidden="true" className="pointer-events-none absolute inset-0 flex flex-col justify-between">
                <span className="border-t border-dashed border-border/60" />
                <span className="border-t border-dashed border-border/50" />
                <span className="border-t border-dashed border-border/40" />
                <span />
              </div>
              {distribution.map((bar) => {
                const activeBar = hoverBar?.pulls === bar.pulls;
                const zoneClass = bar.pulls <= 10
                  ? (activeBar ? 'bg-success' : 'bg-success/75')
                  : bar.pulls >= 18
                    ? (activeBar ? 'bg-warning' : 'bg-warning/75')
                    : (activeBar ? 'bg-accent' : 'bg-accent/75');
                return <div key={bar.pulls} onMouseEnter={() => setHoverBar(bar)} className="relative z-10 flex h-full min-w-0 flex-1 flex-col justify-end">
                  <div className={clsx('rounded-t-md transition-all duration-150', zoneClass, activeBar && 'shadow-sm')} style={{ height: `${bar.count / peak * 100}%`, minHeight: bar.count ? 3 : 0 }} />
                </div>;
              })}
            </div>
            <div className="mt-1 flex gap-1 border-t border-border pt-1.5 text-[10px] tabular-nums text-text-muted">{distribution.map((bar) => <span key={bar.pulls} className="min-w-0 flex-1 text-center">{bar.pulls % 5 === 0 || bar.pulls === 1 ? bar.pulls : ''}</span>)}</div>
            <div className="mt-2.5 flex flex-wrap items-center gap-3 text-[11px] text-text-muted">
              <span className="inline-flex items-center gap-1.5"><span className="h-2 w-2 rounded-full bg-success/80" />1–10 抽提前</span>
              <span className="inline-flex items-center gap-1.5"><span className="h-2 w-2 rounded-full bg-accent/80" />11–17 抽常规</span>
              <span className="inline-flex items-center gap-1.5"><span className="h-2 w-2 rounded-full bg-warning/80" />18–20 抽保底</span>
            </div>
          </div>
        </section>}

        {!!active.length && <section aria-label="当前卡池" className="space-y-2.5">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold">当前开放卡池</h2>
            <span className="text-[11px] text-text-muted">点击卡片查看该卡池收集明细</span>
          </div>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">{active.map((item) => {
            const copy = activeBannerCopy(item);
            const mainKey = item.outfitIds[0] ? outfitThumbLookup[item.outfitIds[0]] : undefined;
            const subKey = item.outfitIds[1] ? outfitThumbLookup[item.outfitIds[1]] : undefined;
            const mainThumb = mainKey ? thumbMap[mainKey] : undefined;
            const subThumb = subKey ? thumbMap[subKey] : undefined;
            const primaryRarity = item.kind === 3 ? 4 : 5;
            const pct = item.total > 0 ? Math.min(100, (item.obtained / item.total) * 100) : 0;
            const isSelected = banner === item.id;
            return <button key={item.id} onClick={() => { setView('outfits'); setBanner(item.id); setRarity('all'); setCompletion('all'); setSearch(''); }}
              className={clsx(
                'group relative min-h-[142px] overflow-hidden rounded-2xl border bg-bg-secondary text-left shadow-xs transition-all duration-200 hover:-translate-y-0.5 hover:border-accent/60 hover:shadow-md',
                isSelected ? 'border-accent ring-1 ring-accent/30' : 'border-border',
              )}>
              <div className={clsx('pointer-events-none absolute inset-y-0 right-0 w-44 overflow-hidden', suitBackdropClass(primaryRarity))}>
                {subThumb && (
                  <img
                    src={subThumb}
                    alt=""
                    aria-hidden="true"
                    className="absolute bottom-0 right-12 h-[84%] w-24 object-contain object-bottom opacity-80 transition-transform duration-300 group-hover:scale-[1.03]"
                  />
                )}
                {mainThumb && (
                  <img
                    src={mainThumb}
                    alt=""
                    aria-hidden="true"
                    className="absolute bottom-0 right-1 z-10 h-[96%] w-26 object-contain object-bottom drop-shadow-xs transition-transform duration-300 group-hover:scale-[1.05]"
                  />
                )}
                <div className="absolute inset-y-0 left-0 z-20 w-16 bg-gradient-to-r from-bg-secondary via-bg-secondary/70 to-transparent" />
              </div>
              <div className="relative z-30 flex h-full flex-col justify-between p-4 pr-36">
                <div>
                  <div className="flex items-center gap-1.5">
                    <span className={clsx(
                      'shrink-0 rounded-md px-1.5 py-0.5 text-[10px] font-medium',
                      primaryRarity === 5 ? 'bg-amber-500/15 text-amber-600 dark:text-amber-400' : 'bg-accent/12 text-accent',
                    )}>
                      {bannerKindLabel(item.kind)}
                    </span>
                    <span className="rounded-md bg-bg-primary/90 px-1.5 py-0.5 text-[10px] tabular-nums text-text-muted">剩 {item.left} 天</span>
                  </div>
                  <h3 className="mt-1.5 truncate text-sm font-semibold">{item.name}</h3>
                  {!!item.outfits.length && (
                    <p className="mt-0.5 truncate text-[11px] font-medium text-accent/90" title={item.outfits.join(' · ')}>
                      {item.outfits.slice(0, 2).join(' · ')}
                    </p>
                  )}
                </div>
                <div className="mt-3">
                  <p className="text-lg font-bold tracking-tight tabular-nums">{copy.headline}</p>
                  <p className="mt-0.5 text-[11px] leading-4 text-text-secondary">{copy.detail}</p>
                  {item.total > 0 && (
                    <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-border/80">
                      <div className={clsx('h-full rounded-full transition-all', !item.missing ? 'bg-success' : 'bg-accent')} style={{ width: `${pct}%` }} />
                    </div>
                  )}
                </div>
              </div>
            </button>;
          })}</div>
        </section>}

        <section className="space-y-3">
          <div className="flex flex-wrap items-center gap-2 rounded-2xl border border-border bg-bg-secondary p-3 shadow-xs">
            <Segmented label="记录视图" value={view} onChange={(next) => { setView(next); setBanner('all'); setRarity('all'); }} options={[['outfits', '套装收藏'], ['records', '逐条记录'], ['map_resources', '大地图资源']] as const} />
            {view !== 'map_resources' && <>
              <select value={banner} onChange={(event) => setBanner(event.target.value)} aria-label="卡池" className={clsx(selectStyle, '!py-1.5 max-w-64')}><option value="all">全部卡池（{knownBanners.length}）</option>{knownBanners.map((id) => <option key={id} value={id}>{bannerMeta.get(id)?.optionLabel || banners.get(id)?.name || '共鸣 ' + id}</option>)}</select>
              <select value={rarity} onChange={(event) => setRarity(event.target.value)} aria-label="星级" className={clsx(selectStyle, '!py-1.5')}><option value="all">全部星级</option><option value="8">星冕</option><option value="5">五星</option><option value="4">四星</option>{view === 'records' && <option value="3">三星</option>}</select>
              {view === 'outfits' && <select value={completion} onChange={(event) => setCompletion(event.target.value)} aria-label="套装进度" className={clsx(selectStyle, '!py-1.5')}><option value="all">全部进度</option><option value="incomplete">尚未集齐</option><option value="complete">已集齐</option></select>}
              <label className="flex min-w-36 flex-1 items-center gap-2 rounded-lg border border-border bg-bg-primary px-3 focus-within:border-accent"><Search className="h-3.5 w-3.5 text-text-muted" /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder={view === 'outfits' ? '搜索套装、卡池或部件' : '搜索物品、套装或卡池'} aria-label="搜索记录" className="min-w-0 flex-1 bg-transparent py-1.5 text-xs outline-none" /></label>
            </>}
          </div>

          {view !== 'map_resources' && knownBanners.length > 1 && (
            <div className="flex items-stretch gap-2 overflow-x-auto pb-1" role="region" aria-label="按套装立绘切换卡池">
              <button
                type="button"
                onClick={() => setBanner('all')}
                className={clsx(
                  'flex w-28 shrink-0 flex-col items-center justify-center rounded-xl border px-3 py-2 text-center transition-all',
                  banner === 'all'
                    ? 'border-accent bg-accent/12 text-accent shadow-xs ring-1 ring-accent/25'
                    : 'border-border bg-bg-secondary text-text-secondary hover:border-accent/40 hover:bg-bg-hover',
                )}
              >
                <BookOpen className="h-4 w-4" />
                <span className="mt-1 text-xs font-semibold">全部卡池</span>
                <span className="text-[10px] tabular-nums opacity-75">共 {knownBanners.length} 个</span>
              </button>
              {knownBanners.map((id) => {
                const info = banners.get(id);
                const meta = bannerMeta.get(id);
                const thumbKey = meta?.thumbKeys[0];
                const thumbUrl = thumbKey ? thumbMap[thumbKey] : undefined;
                const pulls = bannerPullCounts.get(id);
                const selected = banner === id;
                return (
                  <button
                    key={id}
                    type="button"
                    onClick={() => setBanner(selected ? 'all' : id)}
                    title={meta?.optionLabel || info?.name || id}
                    className={clsx(
                      'group flex w-48 shrink-0 items-center gap-2.5 rounded-xl border p-2 text-left transition-all',
                      selected
                        ? 'border-accent bg-accent/10 shadow-xs ring-1 ring-accent/25'
                        : 'border-border bg-bg-secondary hover:border-accent/40 hover:bg-bg-hover/60',
                    )}
                  >
                    <div className={clsx('relative h-12 w-9 shrink-0 overflow-hidden rounded-lg border border-border/70', suitBackdropClass(meta?.maxRarity ?? 4))}>
                      {thumbUrl ? (
                        <img src={thumbUrl} alt="" aria-hidden="true" loading="lazy" decoding="async" className="h-full w-full object-contain object-bottom px-0.5 pt-0.5 transition-transform group-hover:scale-105" />
                      ) : (
                        <div className="flex h-full w-full items-center justify-center text-text-muted">
                          <Shirt className="h-3.5 w-3.5 opacity-35" />
                        </div>
                      )}
                    </div>
                    <div className="min-w-0 flex-1">
                      <p className={clsx('truncate text-xs font-semibold', selected ? 'text-accent' : 'text-text-primary')}>
                        {info?.kind === 1 ? info.name : (meta?.primaryName || info?.name || '共鸣 ' + id)}
                      </p>
                      <p className="mt-0.5 truncate text-[10px] text-text-muted">
                        {info?.kind === 1 ? '常驻共鸣池' : (info?.name || '共鸣 ' + id)}
                      </p>
                      {pulls ? (
                        <p className="mt-0.5 text-[10px] font-medium tabular-nums text-text-secondary">{pulls} 抽</p>
                      ) : (
                        <p className="mt-0.5 text-[10px] text-text-muted">开放中</p>
                      )}
                    </div>
                  </button>
                );
              })}
            </div>
          )}

          {view !== 'map_resources' && selectedBannerInfo && selectedBannerMeta && (
            <div className="flex flex-wrap items-center gap-3.5 rounded-2xl border border-accent/35 bg-gradient-to-r from-accent/8 via-bg-secondary to-bg-secondary p-3.5 shadow-xs">
              <div className="flex shrink-0 items-center gap-2">
                {selectedBannerMeta.outfitIds.slice(0, 4).map((oid) => {
                  const key = outfitThumbLookup[oid];
                  const url = key ? thumbMap[key] : undefined;
                  const info = current?.outfits[oid];
                  return (
                    <div
                      key={oid}
                      title={info?.name}
                      className={clsx('relative h-20 w-14 overflow-hidden rounded-xl border border-border/70 shadow-xs', suitBackdropClass(info?.rarity ?? selectedBannerMeta.maxRarity))}
                    >
                      {url ? (
                        <img src={url} alt={info?.name ?? ''} className="h-full w-full object-contain object-bottom px-0.5 pt-1" />
                      ) : (
                        <div className="flex h-full w-full items-center justify-center text-text-muted">
                          <Shirt className="h-4 w-4 opacity-35" />
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="rounded-md bg-accent/15 px-2 py-0.5 text-[11px] font-medium text-accent">
                    {bannerKindLabel(selectedBannerInfo.kind)}
                  </span>
                  <strong className="truncate text-base font-semibold">{selectedBannerInfo.name}</strong>
                  {bannerPullCounts.get(selectedBannerInfo.id) && (
                    <span className="rounded-md bg-bg-primary px-2 py-0.5 text-[11px] tabular-nums text-text-secondary">
                      累计 {bannerPullCounts.get(selectedBannerInfo.id)} 抽
                    </span>
                  )}
                </div>
                {!!selectedBannerMeta.outfitNames.length && (
                  <p className="mt-1 truncate text-xs text-text-secondary">
                    主打套装：<strong className="font-medium text-text-primary">{selectedBannerMeta.outfitNames.join(' · ')}</strong>
                  </p>
                )}
                {!!selectedBannerInfo.runs?.length && (
                  <p className="mt-0.5 text-[11px] tabular-nums text-text-muted">
                    开放时间：{selectedBannerInfo.runs.map((run) => `${run.start} ~ ${run.end}`).join(' / ')}
                  </p>
                )}
              </div>
              <button
                type="button"
                onClick={() => setBanner('all')}
                className="inline-flex shrink-0 items-center gap-1 rounded-xl border border-border bg-bg-secondary px-3 py-1.5 text-xs text-text-secondary hover:bg-bg-hover hover:text-text-primary"
              >
                <X className="h-3.5 w-3.5" />全部卡池
              </button>
            </div>
          )}
          {view === 'map_resources'
            ? <MapResourcesPanel onLogin={isTauri() ? () => void openPearpalLogin({ switchAccount: pearpalState !== 'logged_out' }) : undefined} />
            : <>
              {possibleGap && view === 'records' && <p className="rounded-lg bg-warning/5 px-3 py-2 text-xs text-warning">部分卡池的历史记录可能存在间隔。</p>}
              {loading && !current ? <div className="flex items-center justify-center gap-2 p-16 text-sm text-text-muted"><Loader2 className="h-4 w-4 animate-spin" />正在读取记录</div>
            : !count ? <div className="flex flex-col items-center gap-3 rounded-xl border border-border bg-bg-secondary px-4 py-16 text-text-muted">
                <BookOpen className="h-8 w-8 opacity-40" />
                <p className="text-sm">
                  {current
                    ? '暂无符合条件的记录'
                    : pearpalState === 'expired'
                      ? '美鸭梨登录已过期，重新登录后可继续同步共鸣收藏'
                      : pearpalState === 'logged_in'
                        ? '已登录美鸭梨，点击下方按钮立即同步你的共鸣记录'
                        : '登录美鸭梨，开始保存你的共鸣收藏'}
                </p>
                {!current && isTauri() && (
                  pearpalState === 'logged_in' ? (
                    <button
                      type="button"
                      disabled={action || status.busy}
                      onClick={() => void run('resonance_sync')}
                      className={primaryButton}
                    >
                      <RefreshCw className={clsx('h-3.5 w-3.5', status.busy && 'animate-spin')} />
                      立即同步共鸣记录
                    </button>
                  ) : (
                    <button
                      type="button"
                      disabled={pearpalBusy}
                      onClick={() => void openPearpalLogin({ switchAccount: pearpalState === 'expired' })}
                      className={primaryButton}
                    >
                      {pearpalState === 'expired' ? '重新登录美鸭梨' : '登录美鸭梨'}
                    </button>
                  )
                )}
              </div>
            : view === 'outfits' ? <div className="grid grid-cols-1 items-start gap-3.5 xl:grid-cols-2">{pageOutfits.map((outfit) => {
              const full = outfit.obtained === outfit.items.length;
              const low = Math.min(...outfit.parts.flatMap((part) => part.pity));
              const thumbKey = outfitThumbLookup[outfit.id];
              const thumbUrl = thumbKey ? thumbMap[thumbKey] : undefined;
              const pct = outfit.items.length ? (outfit.obtained / outfit.items.length) * 100 : 0;
              return <article key={outfit.id} className="group flex items-start gap-4 rounded-2xl border border-border bg-bg-secondary p-4 shadow-xs transition-all hover:border-accent/40 hover:shadow-md">
                <div className={clsx('relative aspect-[5/7] w-24 shrink-0 overflow-hidden rounded-xl border border-border/70 sm:w-28', suitBackdropClass(outfit.rarity))}>
                  {thumbUrl ? (
                    <img
                      src={thumbUrl}
                      alt={outfit.name}
                      loading="lazy"
                      decoding="async"
                      className={clsx(
                        'h-full w-full object-contain object-bottom px-1 pt-1.5 drop-shadow-xs transition-transform duration-300 group-hover:scale-[1.04]',
                        outfit.obtained === 0 && 'grayscale-[50%] opacity-65',
                      )}
                    />
                  ) : (
                    <div className="flex h-full w-full items-center justify-center text-text-muted">
                      <Shirt className="h-7 w-7 opacity-30" />
                    </div>
                  )}
                  <span className={clsx(
                    'absolute left-1.5 top-1.5 rounded-md border border-border/60 bg-bg-secondary/85 px-1.5 py-0.5 text-[10px] font-semibold backdrop-blur-xs',
                    outfit.rarity >= 8 ? 'text-fuchsia-600 dark:text-fuchsia-400' : outfit.rarity === 5 ? 'text-amber-600 dark:text-amber-400' : 'text-accent',
                  )}>
                    {qualityLabel(outfit.rarity)}
                  </span>
                  {full && (
                    <span className="absolute bottom-1.5 left-1.5 rounded-md border border-success/30 bg-success/15 px-1.5 py-0.5 text-[10px] font-medium text-success backdrop-blur-xs">
                      {outfit.copies > 1 ? `×${outfit.copies}套` : '已集齐'}
                    </span>
                  )}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex items-start gap-2.5">
                    <div className="min-w-0 flex-1">
                      <h3 className="truncate text-sm font-semibold" title={outfit.name}>{outfit.name}</h3>
                      <div className="mt-1 flex items-center gap-1.5 text-xs text-text-muted">
                        <span className={clsx('tracking-tight', outfit.rarity >= 8 ? 'font-medium text-fuchsia-500' : outfit.rarity === 5 ? 'text-amber-500' : 'text-accent')} aria-label={outfit.rarity >= 8 ? '星冕品质' : outfit.rarity + ' 星'}>{outfit.rarity >= 8 ? '✦ 星冕' : '★'.repeat(outfit.rarity)}</span>
                        {outfit.banner && (
                          <button
                            type="button"
                            onClick={() => outfit.bannerId && setBanner(outfit.bannerId)}
                            className="truncate rounded-md bg-bg-primary px-1.5 py-0.5 text-[11px] text-text-secondary transition-colors hover:bg-accent/10 hover:text-accent"
                            title={`筛选卡池：${outfit.banner}`}
                          >
                            {outfit.banner}
                          </button>
                        )}
                      </div>
                    </div>
                    <div className="shrink-0 text-right">
                      <p className={clsx('text-sm font-semibold tabular-nums', full && 'text-success')}>{full ? (outfit.copies > 1 ? `集齐 ${outfit.copies} 套` : '已集齐') : `${outfit.obtained} / ${outfit.items.length}`}</p>
                      <p className="mt-0.5 text-[11px] tabular-nums text-text-muted">{outfit.spent ? `${outfit.spent} 抽 · 均 ${outfit.average.toFixed(1)}` : '尚未抽取'}</p>
                    </div>
                  </div>
                  <div className="mt-2 h-1 overflow-hidden rounded-full bg-border/80">
                    <div className={clsx('h-full rounded-full transition-all', full ? 'bg-success' : 'bg-accent')} style={{ width: `${pct}%` }} />
                  </div>
                  <ul className="mt-2.5 grid grid-cols-2 gap-1.5 sm:grid-cols-3">{outfit.parts.map((part) => {
                    const lucky = part.count > 0 && part.pity[0] === low && low <= (outfit.rarity >= 5 ? 10 : 5);
                    return <li key={part.item} title={part.name + (part.count ? ` · 第 ${part.pity.join('、')} 抽` : ' · 未获得')}
                      className={clsx('flex min-w-0 items-center gap-1.5 rounded-lg px-2 py-1.5 text-xs', part.count ? 'border border-border/50 bg-bg-primary/90' : 'border border-dashed border-border-strong text-text-muted')}>
                      <span className="min-w-0 flex-1 truncate">{part.name}</span>
                      {part.count > 1 && <span className="shrink-0 tabular-nums text-text-muted">×{part.count}</span>}
                      {part.count > 0 && <strong className={clsx('shrink-0 font-semibold tabular-nums', lucky && 'text-success')}>{part.pity[0]}</strong>}
                    </li>;
                  })}</ul>
                </div>
              </article>;
            })}</div>
            : <div className="overflow-auto rounded-2xl border border-border bg-bg-secondary shadow-xs"><table className="w-full text-xs"><thead className="bg-bg-primary/60 text-left text-text-muted"><tr><th className="px-4 py-2.5 font-medium">抽取时间</th><th className="px-4 py-2.5 font-medium">共鸣结果</th><th className="px-4 py-2.5 font-medium">所属卡池</th></tr></thead><tbody>{pageRows.map((row) => {
              const bInfo = banners.get(row.bannerId);
              const meta = bannerMeta.get(row.bannerId);
              const thumbKey = meta?.thumbKeys[0];
              const thumbUrl = thumbKey ? thumbMap[thumbKey] : undefined;
              return <tr key={row.key} className={clsx('border-t border-border/60 transition-colors hover:bg-bg-hover/50', row.rarity >= 8 ? 'bg-fuchsia-500/6' : row.rarity === 5 && 'bg-amber-500/6')}>
                <td className={clsx('whitespace-nowrap px-4 py-2 tabular-nums text-text-secondary', row.rarity >= 8 ? 'border-l-2 border-l-fuchsia-500' : row.rarity === 5 && 'border-l-2 border-l-amber-500')}>{pullTime(row.time)}</td>
                <td className="px-4 py-2"><span className={clsx('mr-2 inline-block rounded px-1.5 py-0.5 text-[10px] font-semibold tabular-nums', row.rarity >= 8 ? 'bg-fuchsia-500/15 text-fuchsia-600 dark:text-fuchsia-400' : row.rarity === 5 ? 'bg-amber-500/15 text-amber-600 dark:text-amber-400' : row.rarity === 4 ? 'bg-accent/12 text-accent' : 'bg-bg-primary text-text-muted')}>{qualityLabel(row.rarity)}</span><span title={row.item} className={clsx(row.rarity >= 5 && 'font-medium text-text-primary')}>{row.name}</span></td>
                <td className="px-4 py-2 text-text-secondary">
                  <button
                    type="button"
                    onClick={() => setBanner(row.bannerId)}
                    title="点击筛选此卡池"
                    className="group inline-flex max-w-full items-center gap-2 text-left hover:text-text-primary"
                  >
                    <span className={clsx('relative h-9 w-7 shrink-0 overflow-hidden rounded-md border border-border/70', suitBackdropClass(meta?.maxRarity ?? 4))}>
                      {thumbUrl ? (
                        <img src={thumbUrl} alt="" aria-hidden="true" className="h-full w-full object-contain object-bottom" />
                      ) : (
                        <span className="flex h-full w-full items-center justify-center text-text-muted">
                          <Shirt className="h-3 w-3 opacity-35" />
                        </span>
                      )}
                    </span>
                    <span className="min-w-0">
                      <span className="block truncate font-medium group-hover:text-accent">{bInfo?.name || '共鸣 ' + row.bannerId}</span>
                      {meta?.primaryName && bInfo?.kind !== 1 && (
                        <span className="block truncate text-[10px] text-text-muted">{meta.outfitNames.slice(0, 2).join(' · ')}</span>
                      )}
                    </span>
                  </button>
                </td>
              </tr>;
            })}</tbody></table></div>}
              {count > size && <div className="flex items-center gap-3 text-xs text-text-secondary"><span className="mr-auto">{count} {view === 'records' ? '条记录' : '套'} · {currentPage + 1}/{pages} 页</span><button disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)} className="rounded-lg border border-border px-2.5 py-1 hover:bg-bg-hover disabled:opacity-30">上一页</button><button disabled={currentPage + 1 >= pages} onClick={() => setPage(currentPage + 1)} className="rounded-lg border border-border px-2.5 py-1 hover:bg-bg-hover disabled:opacity-30">下一页</button></div>}
            </>}
        </section>
        <p className="px-1 text-[11px] leading-5 text-text-muted">套装和抽数来自美鸭梨手账，逐条记录来自游戏内共鸣历史。卡池最后一件四星之后的抽数看不到，所以累计和“距上次五星”都是下限。本地存档：config/accounts/CN/{current?.history.uid || '<UID>'}.json · 目录更新：{status.catalog_updated_at || '—'}</p>
  </FeaturePage>;
}
