import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AlertCircle, ArrowLeft, BookOpen, Download, Loader2, RefreshCw, Search } from 'lucide-react';
import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { toast } from 'sonner';
import clsx from 'clsx';
import { refreshResonance, resonanceLabel, type ResonanceHistory, useResonanceStore } from '@/services/resonanceService';
import { activeBanners, collectionOutfits, historyRows, resonanceSummary } from '@/services/resonanceData';
import { isTauri } from '@/utils/paths';
import { ResonanceControl } from './ResonanceControl';

function timeLabel(value: number | null | undefined) {
  return value ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false }) : '尚未同步';
}
function pullTime(value: string) {
  if (/^\d+$/.test(value)) return timeLabel(Number(value) / (value.length > 10 ? 1000 : 1));
  return value;
}
const selectStyle = 'max-w-full rounded-lg border border-border bg-bg-secondary px-3 py-2 text-xs outline-none focus:border-accent';

export function ResonancePage({ onClose }: { onClose: () => void }) {
  const { status, connectionError } = useResonanceStore();
  const [selected, setSelected] = useState('');
  const [data, setData] = useState<ResonanceHistory | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [action, setAction] = useState(false);
  const [login, setLogin] = useState(false);
  const [banner, setBanner] = useState('all');
  const [rarity, setRarity] = useState('all');
  const [search, setSearch] = useState('');
  const [view, setView] = useState<'records' | 'outfits'>('outfits');
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
  useEffect(() => { setBanner('all'); setRarity('all'); }, [accountKey, view]);
  useEffect(() => {
    if (!isTauri()) return;
    let disposed = false;
    const update = async () => {
      try {
        const result = await invoke<{ has_login: boolean }>('pearpal_status');
        if (!disposed) setLogin(result.has_login);
      } catch { /* Login actions report connection errors. */ }
    };
    void update();
    const subscription = listen('pearpal-login-changed', () => { void update(); void refreshResonance(); });
    return () => { disposed = true; void subscription.then((unlisten) => unlisten()).catch(() => {}); };
  }, []);

  // Keep a previous account's response out of the newly selected account view.
  const current = data?.history.account_key === accountKey ? data : null;
  const banners = useMemo(() => new Map(current?.banners.map((item) => [item.id, item]) ?? []), [current]);
  const knownBanners = useMemo(() => {
    if (!current) return [];
    return (view === 'records'
      ? Object.entries(current.history.banners).filter(([, entry]) => entry.pulls.length).map(([id]) => id)
      : Object.keys(current.history.notebook.pulls)).sort((a, b) => Number(b) - Number(a));
  }, [current, view]);
  const allOutfits = useMemo(() => current ? collectionOutfits(current, 'all') : [], [current]);
  const outfits = useMemo(() => current ? collectionOutfits(current, banner).filter((outfit) =>
    (rarity === 'all' || String(outfit.rarity) === rarity)
    && (completion === 'all' || (completion === 'complete' ? outfit.obtained === outfit.items.length : outfit.obtained < outfit.items.length))
    && (outfit.name + ' ' + outfit.parts.map((part) => part.name).join(' ')).includes(search.trim())) : [],
  [current, banner, rarity, completion, search]);
  const rows = useMemo(() => current ? historyRows(current).filter((row) =>
    (banner === 'all' || row.bannerId === banner) && (rarity === 'all' || String(row.rarity) === rarity)
    && (row.name + ' ' + row.item + ' ' + (banners.get(row.bannerId)?.name || row.bannerId)).includes(search.trim())) : [],
  [current, banner, rarity, search, banners]);
  const summary = useMemo(() => current ? resonanceSummary(current) : null, [current]);
  const active = useMemo(() => current ? activeBanners(current, allOutfits) : [], [current, allOutfits]);
  const distribution = summary?.distribution ?? [];
  const peak = Math.max(1, ...distribution.map((bar) => bar.count));
  const completeOutfits = allOutfits.filter((outfit) => outfit.obtained === outfit.items.length).length;
  const possibleGap = current && Object.values(current.history.banners).some((entry) => entry.possible_gap);
  const size = view === 'records' ? 50 : 12;
  const count = view === 'records' ? rows.length : outfits.length;
  const pages = Math.max(1, Math.ceil(count / size));
  const currentPage = Math.min(page, pages - 1);

  async function run(command: 'pearpal_open_login' | 'resonance_sync') {
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
  return <div className="flex min-h-0 flex-1 flex-col bg-bg-primary text-text-primary">
    <div className="flex items-center gap-3 border-b border-border bg-bg-secondary px-5 py-3">
      <button onClick={onClose} className="rounded-lg p-2 hover:bg-bg-hover" aria-label="返回任务列表"><ArrowLeft className="h-4 w-4" /></button>
      <h1 className="flex-1 text-lg font-semibold">共鸣记录</h1>
      <select value={accountKey} onChange={(event) => setSelected(event.target.value)} aria-label="记录账号" className={clsx(selectStyle, '!py-1.5')}>
        {!status.accounts.length && <option value="">尚无账号记录</option>}
        {status.accounts.map((item) => <option key={item.key} value={item.key}>UID {item.uid}</option>)}
      </select>
      <button disabled={!current || action} onClick={() => void exportHistory()} title="导出 JSON" aria-label="导出 JSON" className="rounded-lg p-2 hover:bg-bg-hover disabled:opacity-40"><Download className="h-4 w-4" /></button>
      <ResonanceControl showSettings={false} />
    </div>
    <div className="min-h-0 flex-1 overflow-auto p-4">
      <div className="mx-auto max-w-6xl space-y-4">
        <section className="flex flex-wrap items-center gap-3 rounded-xl border border-border bg-bg-secondary px-4 py-2.5">
          <span className={clsx('h-2 w-2 rounded-full', status.error || connectionError ? 'bg-warning' : status.enabled || status.busy ? 'bg-success' : 'bg-text-muted')} />
          <p className="text-sm font-medium" role="status">{resonanceLabel(status)}</p>
          <p className="min-w-40 flex-1 text-xs text-text-muted">{status.busy ? status.banner ? status.banner + (status.page ? ' · 第 ' + status.page + ' 页' : '') : '正在验证美鸭梨账号'
            : '最近同步 ' + timeLabel(account?.last_success) + (status.enabled && !!status.next_sync && !status.auth_paused ? ' · 下次 ' + timeLabel(status.next_sync) : '')}</p>
          <button disabled={!isTauri() || action} onClick={() => void run('pearpal_open_login')} className="rounded-lg border border-border px-3 py-1.5 text-xs hover:bg-bg-hover disabled:opacity-40">{login ? '切换美鸭梨账号' : '登录美鸭梨'}</button>
          <button disabled={!isTauri() || action || status.busy} onClick={() => void run('resonance_sync')} className="flex items-center gap-1.5 rounded-lg bg-accent px-3 py-1.5 text-xs text-on-accent hover:bg-accent-hover disabled:opacity-40"><RefreshCw className={clsx('h-3.5 w-3.5', status.busy && 'animate-spin')} />立即同步</button>
        </section>
        {(status.error || connectionError || error) && <p role="alert" className="flex items-center gap-2 rounded-xl border border-error/25 bg-error/5 p-3 text-sm text-error"><AlertCircle className="h-4 w-4 shrink-0" />{connectionError || error || status.error}</p>}

        {summary && <section className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.25fr)]">
          <div className="rounded-xl border border-border bg-bg-secondary p-4">
            <p className="text-xs text-text-secondary">累计共鸣</p>
            <p className="mt-1 flex items-baseline gap-2"><strong className="text-4xl font-semibold tabular-nums">{summary.total.toLocaleString('zh-CN')}</strong><span className="text-sm text-text-secondary">次起</span>
              <span className="ml-auto text-right text-xs text-text-secondary">集齐 <strong className="text-base font-semibold tabular-nums text-text-primary">{completeOutfits}</strong> / {allOutfits.length} 套</span></p>
            <div className="mt-4 grid grid-cols-3 gap-2">{[
              { label: '限定五星', value: summary.limitedFive.count, detail: '每件 ' + summary.limitedFive.average.toFixed(1) + ' 抽' },
              { label: '常驻五星', value: summary.standardFive.count, detail: '每件 ' + summary.standardFive.average.toFixed(1) + ' 抽' },
              { label: '限定四星', value: summary.limitedFour.count, detail: '每件 ' + summary.limitedFour.average.toFixed(1) + ' 抽' },
            ].map((item) => <div key={item.label} className="rounded-lg bg-bg-primary px-3 py-2.5">
              <p className="text-xs text-text-secondary">{item.label}</p>
              <p className="mt-1 text-xl font-semibold tabular-nums">{item.value}</p>
              <p className="mt-0.5 text-xs tabular-nums text-text-muted">{item.detail}</p>
            </div>)}</div>
            <p className="mt-3 text-xs leading-5 text-text-secondary">{summary.earlyShare > 0 && `${(summary.earlyShare * 100).toFixed(0)}% 的五星在 10 抽内出现，`}{(summary.hardShare * 100).toFixed(0)}% 要到 18 抽以后。五星最多 20 抽必出。</p>
          </div>
          <div className="rounded-xl border border-border bg-bg-secondary p-4">
            <div className="flex items-baseline gap-2"><h2 className="text-sm font-semibold">五星出在第几抽</h2><span className="text-xs tabular-nums text-text-muted">平均 {summary.fiveAverage.toFixed(1)} 抽</span>
              <span className="ml-auto h-4 text-xs tabular-nums text-text-secondary">{hoverBar ? `第 ${hoverBar.pulls} 抽：${hoverBar.count} 件` : best?.count ? `最常见：第 ${best.pulls} 抽（${best.count} 件）` : ''}</span></div>
            <div className="mt-3 flex h-36 items-end gap-0.5" role="img" aria-label={distribution.map((bar) => `第 ${bar.pulls} 抽 ${bar.count} 件`).join('，')} onMouseLeave={() => setHoverBar(null)}>
              {distribution.map((bar) => <div key={bar.pulls} onMouseEnter={() => setHoverBar(bar)} className="flex h-full min-w-0 flex-1 flex-col justify-end">
                <div className={clsx('rounded-t transition-colors', hoverBar?.pulls === bar.pulls ? 'bg-accent' : 'bg-accent/70')} style={{ height: `${bar.count / peak * 100}%`, minHeight: bar.count ? 2 : 0 }} />
              </div>)}
            </div>
            <div className="mt-1 flex gap-0.5 border-t border-border pt-1 text-[10px] tabular-nums text-text-muted">{distribution.map((bar) => <span key={bar.pulls} className="min-w-0 flex-1 text-center">{bar.pulls % 5 === 0 || bar.pulls === 1 ? bar.pulls : ''}</span>)}</div>
          </div>
        </section>}

        {!!active.length && <section aria-label="当前卡池">
          <h2 className="mb-2 text-sm font-semibold">当前卡池</h2>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">{active.map((item) => <button key={item.id} onClick={() => { setView('outfits'); setBanner(item.id); }}
            className="rounded-xl border border-border bg-bg-secondary p-4 text-left transition-colors hover:border-accent/60">
            <div className="flex items-baseline gap-2"><h3 className="min-w-0 flex-1 truncate text-sm font-medium">{item.name}</h3><span className="shrink-0 text-xs tabular-nums text-text-muted">剩 {item.left} 天</span></div>
            <p className="mt-2 text-xl font-semibold tabular-nums">{item.missing ? `还差 ${item.missing} 件` : '已全部集齐'}</p>
            <p className="mt-0.5 text-xs leading-5 text-text-secondary">{item.missing ? `按你的平均，约需 ${item.need} 抽` : '这期可以省下了'}{item.since > 0 && ` · 距上次五星至少 ${item.since} 抽`}</p>
          </button>)}</div>
        </section>}

        <section className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <div className="inline-flex rounded-lg border border-border bg-bg-secondary p-0.5">{[{ id: 'outfits' as const, label: '套装收藏' }, { id: 'records' as const, label: '逐条记录' }].map(({ id, label }) =>
              <button key={id} onClick={() => setView(id)} aria-pressed={view === id} className={clsx('rounded-lg px-3 py-1 text-xs transition-colors', view === id ? 'bg-accent/10 font-medium text-accent' : 'text-text-secondary hover:bg-bg-hover')}>{label}</button>)}</div>
            <select value={banner} onChange={(event) => setBanner(event.target.value)} aria-label="卡池" className={clsx(selectStyle, '!py-1.5 max-w-44')}><option value="all">全部卡池</option>{knownBanners.map((id) => <option key={id} value={id}>{banners.get(id)?.name || '共鸣 ' + id}</option>)}</select>
            <select value={rarity} onChange={(event) => setRarity(event.target.value)} aria-label="星级" className={clsx(selectStyle, '!py-1.5')}><option value="all">全部星级</option><option value="5">五星</option><option value="4">四星</option>{view === 'records' && <option value="3">三星</option>}</select>
            {view === 'outfits' && <select value={completion} onChange={(event) => setCompletion(event.target.value)} aria-label="套装进度" className={clsx(selectStyle, '!py-1.5')}><option value="all">全部进度</option><option value="incomplete">尚未集齐</option><option value="complete">已集齐</option></select>}
            <label className="flex min-w-36 flex-1 items-center gap-2 rounded-lg border border-border bg-bg-secondary px-3 focus-within:border-accent"><Search className="h-3.5 w-3.5 text-text-muted" /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder={view === 'outfits' ? '搜索套装或部件' : '搜索物品或卡池'} aria-label="搜索记录" className="min-w-0 flex-1 bg-transparent py-1.5 text-xs outline-none" /></label>
          </div>
          {possibleGap && view === 'records' && <p className="rounded-lg bg-warning/5 px-3 py-2 text-xs text-warning">部分卡池的历史记录可能存在间隔。</p>}
          {loading && !current ? <div className="flex items-center justify-center gap-2 p-16 text-sm text-text-muted"><Loader2 className="h-4 w-4 animate-spin" />正在读取记录</div>
            : !count ? <div className="flex flex-col items-center gap-3 rounded-xl border border-border bg-bg-secondary px-4 py-16 text-text-muted"><BookOpen className="h-8 w-8 opacity-40" /><p className="text-sm">{current ? '暂无符合条件的记录' : '登录美鸭梨，开始保存你的共鸣收藏'}</p></div>
            : view === 'outfits' ? <div className="grid grid-cols-1 items-start gap-3 xl:grid-cols-2">{outfits.slice(currentPage * size, currentPage * size + size).map((outfit) => {
              const full = outfit.obtained === outfit.items.length;
              const low = Math.min(...outfit.parts.flatMap((part) => part.pity));
              return <article key={outfit.id} className="rounded-xl border border-border bg-bg-secondary p-4">
                <div className="flex items-start gap-3">
                  <div className="min-w-0 flex-1">
                    <h3 className="truncate text-sm font-semibold" title={outfit.name}>{outfit.name}</h3>
                    <p className="mt-0.5 truncate text-xs text-text-muted"><span className={clsx('mr-1.5 tracking-tight', outfit.rarity === 5 ? 'text-warning' : 'text-accent')} aria-label={outfit.rarity + ' 星'}>{'★'.repeat(outfit.rarity)}</span>{outfit.banner}</p>
                  </div>
                  <div className="shrink-0 text-right">
                    <p className="text-sm font-semibold tabular-nums">{full ? (outfit.copies > 1 ? `集齐 ${outfit.copies} 套` : '已集齐') : `${outfit.obtained} / ${outfit.items.length}`}</p>
                    <p className="mt-0.5 text-xs tabular-nums text-text-muted">{outfit.spent ? `${outfit.spent} 抽 · 每件 ${outfit.average.toFixed(1)}` : '尚未抽取'}</p>
                  </div>
                </div>
                <ul className="mt-3 grid grid-cols-2 gap-1.5 sm:grid-cols-3">{outfit.parts.map((part) => {
                  const lucky = part.count > 0 && part.pity[0] === low && low <= (outfit.rarity === 5 ? 10 : 5);
                  return <li key={part.item} title={part.name + (part.count ? ` · 第 ${part.pity.join('、')} 抽` : ' · 未获得')}
                    className={clsx('flex min-w-0 items-center gap-1.5 rounded-lg px-2 py-1.5 text-xs', part.count ? 'bg-bg-primary' : 'border border-dashed border-border-strong text-text-muted')}>
                    <span className="min-w-0 flex-1 truncate">{part.name}</span>
                    {part.count > 1 && <span className="shrink-0 tabular-nums text-text-muted">×{part.count}</span>}
                    {part.count > 0 && <strong className={clsx('shrink-0 font-semibold tabular-nums', lucky && 'text-accent')}>{part.pity[0]}</strong>}
                  </li>;
                })}</ul>
              </article>;
            })}</div>
            : <div className="overflow-auto rounded-xl border border-border bg-bg-secondary"><table className="w-full text-xs"><thead className="text-left text-text-muted"><tr><th className="px-4 py-2.5 font-normal">抽取时间</th><th className="px-4 py-2.5 font-normal">共鸣结果</th><th className="px-4 py-2.5 font-normal">所属卡池</th></tr></thead><tbody>{rows.slice(currentPage * size, currentPage * size + size).map((row) => <tr key={row.key} className={clsx('border-t border-border/60 hover:bg-bg-hover/50', row.rarity === 5 && 'bg-warning/5')}><td className="whitespace-nowrap px-4 py-2.5 tabular-nums text-text-secondary">{pullTime(row.time)}</td><td className="px-4 py-2.5"><span className={clsx('mr-2 text-[11px] tabular-nums', row.rarity === 5 ? 'font-semibold text-warning' : row.rarity === 4 ? 'text-accent' : 'text-text-muted')}>{row.rarity || '—'}★</span><span title={row.item} className={clsx(row.rarity === 5 && 'font-medium')}>{row.name}</span></td><td className="px-4 py-2.5 text-text-secondary">{banners.get(row.bannerId)?.name || '共鸣 ' + row.bannerId}</td></tr>)}</tbody></table></div>}
          {count > size && <div className="flex items-center gap-3 text-xs text-text-secondary"><span className="mr-auto">{count} {view === 'records' ? '条记录' : '套'} · {currentPage + 1}/{pages} 页</span><button disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)} className="rounded-lg border border-border px-2.5 py-1 hover:bg-bg-hover disabled:opacity-30">上一页</button><button disabled={currentPage + 1 >= pages} onClick={() => setPage(currentPage + 1)} className="rounded-lg border border-border px-2.5 py-1 hover:bg-bg-hover disabled:opacity-30">下一页</button></div>}
        </section>
        <p className="px-1 text-[11px] leading-5 text-text-muted">套装和抽数来自美鸭梨手账，逐条记录来自游戏内共鸣历史。卡池最后一件四星之后的抽数看不到，所以累计和“距上次五星”都是下限。本地存档：config/accounts/CN/{current?.history.uid || '<UID>'}.json · 目录更新：{status.catalog_updated_at || '—'}</p>
      </div>
    </div>
  </div>;
}
