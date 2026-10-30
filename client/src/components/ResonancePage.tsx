import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AlertCircle, ArrowLeft, BookOpen, Check, ChevronDown, Download, Layers3, List, Loader2, RefreshCw, Search, Sparkles } from 'lucide-react';
import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { toast } from 'sonner';
import clsx from 'clsx';
import { refreshResonance, resonanceLabel, type ResonanceHistory, useResonanceStore } from '@/services/resonanceService';
import { collectionOutfits, historyRows } from '@/services/resonanceData';
import { isTauri } from '@/utils/paths';
import { ResonanceControl } from './ResonanceControl';

function timeLabel(value: number | null | undefined) {
  return value ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false }) : '尚未同步';
}
function pullTime(value: string) {
  if (/^\d+$/.test(value)) return timeLabel(Number(value) / (value.length > 10 ? 1000 : 1));
  return value;
}
const selectStyle = 'max-w-full rounded-lg border border-border bg-bg-primary px-3 py-2 text-xs outline-none focus:border-accent';

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
  const gameTotal = current ? Object.values(current.history.banners).reduce((sum, entry) => sum + entry.pulls.length, 0) : 0;
  const notebookTotal = current ? Object.values(current.history.notebook.pulls).reduce((sum, entry) => sum + Object.keys(entry).length, 0) : 0;
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

  return <div className="flex min-h-0 flex-1 flex-col bg-bg-primary text-text-primary">
    <div className="flex items-center gap-3 border-b border-border bg-bg-secondary px-5 py-3">
      <button onClick={onClose} className="rounded-md p-2 hover:bg-bg-hover" aria-label="返回任务列表"><ArrowLeft className="h-4 w-4" /></button>
      <BookOpen className="h-5 w-5 text-accent" /><h1 className="flex-1 text-lg font-semibold">共鸣记录</h1>
      <ResonanceControl showSettings={false} />
    </div>
    <div className="min-h-0 flex-1 overflow-auto p-4 lg:p-6">
      <div className="mx-auto max-w-5xl space-y-4">
        <section className="relative overflow-hidden rounded-2xl border border-border bg-bg-secondary">
          <div className="pointer-events-none absolute -right-12 -top-24 h-72 w-72 rounded-full bg-accent/10 blur-3xl" />
          <div className="relative flex flex-wrap items-start justify-between gap-4 p-5">
            <div>
              <div className="mb-2 flex items-center gap-2 text-xs text-text-secondary"><Sparkles className="h-3.5 w-3.5 text-accent" />共鸣收藏簿</div>
              <h2 className="text-xl font-semibold tracking-tight">每一次相遇，都留在这里</h2>
              <div className="mt-3 flex flex-wrap items-center gap-2">
                <select value={accountKey} onChange={(event) => setSelected(event.target.value)} aria-label="记录账号" className={selectStyle}>
                  {!status.accounts.length && <option value="">尚无账号记录</option>}
                  {status.accounts.map((item) => <option key={item.key} value={item.key}>国服 · UID {item.uid}</option>)}
                </select>
                <span className="text-xs text-text-muted">按 UID 长期保存</span>
              </div>
            </div>
            <button disabled={!current || action} onClick={() => void exportHistory()} className="flex items-center gap-1.5 rounded-lg border border-border bg-bg-primary/70 px-3 py-2 text-xs hover:bg-bg-hover disabled:opacity-40"><Download className="h-3.5 w-3.5" />导出 JSON</button>
          </div>
          <div className="relative grid grid-cols-3 divide-x divide-border border-t border-border bg-bg-primary/30 px-1 py-4">
            {[[completeOutfits + ' / ' + allOutfits.length, '记录集齐套装'], [gameTotal.toLocaleString(), '逐抽历史'], [notebookTotal.toLocaleString(), '手账四 / 五星']].map(([value, label]) =>
              <div key={label} className="px-4"><div className="text-xl font-semibold tabular-nums lg:text-2xl">{value}</div><div className="mt-1 text-xs text-text-muted">{label}</div></div>)}
          </div>
        </section>
        <section className="rounded-xl border border-border bg-bg-secondary px-4 py-3">
          <div className="flex flex-wrap items-center gap-3">
            <span className={clsx('h-2 w-2 rounded-full', status.error || connectionError ? 'bg-warning' : status.enabled || status.busy ? 'bg-success' : 'bg-text-muted')} />
            <div className="min-w-40 flex-1">
              <p className="text-sm font-medium" role="status">{resonanceLabel(status)}</p>
              <p className="mt-1 text-xs text-text-muted">{status.busy ? status.banner ? status.banner + (status.page ? ' · 第 ' + status.page + ' 页' : '') : '正在验证美鸭梨账号' : '每 30 分钟同步两种来源 · 托盘中继续运行'}</p>
            </div>
            <button disabled={!isTauri() || action} onClick={() => void run('pearpal_open_login')} className="rounded-lg border border-border px-3 py-2 text-xs hover:bg-bg-hover disabled:opacity-40">{login ? '切换美鸭梨账号' : '登录美鸭梨'}</button>
            <button disabled={!isTauri() || action || status.busy} onClick={() => void run('resonance_sync')} className="flex items-center gap-1.5 rounded-lg bg-accent px-3 py-2 text-xs text-white hover:bg-accent-hover disabled:opacity-40"><RefreshCw className={clsx('h-3.5 w-3.5', status.busy && 'animate-spin')} />立即同步</button>
          </div>
          <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1 border-t border-border pt-2.5 text-[11px] text-text-muted">
            <span>所选账号最近完整同步：{timeLabel(account?.last_success)}</span>
            <span>手账更新：{timeLabel(current?.history.notebook.updated_at)}</span>
            {status.enabled && !!status.next_sync && !status.busy && !status.auth_paused && <span>下次同步：{timeLabel(status.next_sync)}</span>}
          </div>
        </section>
        {(status.error || connectionError || error) && <p role="alert" className="flex items-center gap-2 rounded-xl border border-error/25 bg-error/5 p-3 text-sm text-error"><AlertCircle className="h-4 w-4 shrink-0" />{connectionError || error || status.error}</p>}
        <section className="overflow-hidden rounded-xl border border-border bg-bg-secondary">
          <div className="flex items-center gap-1 border-b border-border px-3 pt-2">
            {[{ id: 'outfits' as const, label: '套装收藏', Icon: Layers3 }, { id: 'records' as const, label: '逐条记录', Icon: List }].map(({ id, label, Icon }) =>
              <button key={id} onClick={() => setView(id)} className={clsx('flex items-center gap-2 border-b-2 px-4 py-3 text-sm transition-colors', view === id ? 'border-accent font-semibold text-accent' : 'border-transparent text-text-muted hover:text-text-primary')}><Icon className="h-4 w-4" />{label}</button>)}
          </div>
          <div className="flex flex-wrap items-center gap-2 p-3">
            <select value={banner} onChange={(event) => setBanner(event.target.value)} aria-label="卡池" className={clsx(selectStyle, 'max-w-52')}><option value="all">全部卡池</option>{knownBanners.map((id) => <option key={id} value={id}>{banners.get(id)?.name || '共鸣 ' + id}</option>)}</select>
            <select value={rarity} onChange={(event) => setRarity(event.target.value)} aria-label="星级" className={selectStyle}><option value="all">全部星级</option><option value="5">五星</option><option value="4">四星</option>{view === 'records' && <option value="3">三星</option>}</select>
            {view === 'outfits' && <select value={completion} onChange={(event) => setCompletion(event.target.value)} aria-label="套装进度" className={selectStyle}><option value="all">全部进度</option><option value="incomplete">尚未集齐</option><option value="complete">已集齐</option></select>}
            <div className="flex min-w-36 flex-1 items-center gap-2 rounded-lg border border-border bg-bg-primary px-3 focus-within:border-accent"><Search className="h-3.5 w-3.5 text-text-muted" /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder={view === 'outfits' ? '搜索套装或部件' : '搜索物品或卡池'} aria-label="搜索记录" className="min-w-0 flex-1 bg-transparent py-2 text-xs outline-none" /></div>
          </div>
          <p className="border-b border-border px-4 pb-3 text-[11px] leading-5 text-text-muted">{view === 'outfits' ? '美鸭梨手账 · 按套装汇总四、五星部件，展开可查看每件的获取数量。' : '游戏内共鸣历史 · 按真实抽取时间排列，长期保存每次同步获取的记录。'}</p>
          {possibleGap && view === 'records' && <p className="border-b border-border bg-warning/5 px-4 py-2 text-xs text-warning">部分卡池的历史记录可能存在间隔。</p>}
          {loading && !current ? <div className="flex items-center justify-center gap-2 p-16 text-sm text-text-muted"><Loader2 className="h-4 w-4 animate-spin" />正在读取记录</div>
            : !count ? <div className="flex flex-col items-center gap-3 px-4 py-16 text-text-muted"><BookOpen className="h-8 w-8 opacity-40" /><p className="text-sm">{current ? '暂无符合条件的记录' : '登录美鸭梨，开始保存你的共鸣收藏'}</p></div>
            : view === 'outfits' ? <div className="grid grid-cols-1 gap-3 bg-bg-primary/30 p-3 md:grid-cols-2">{outfits.slice(currentPage * size, currentPage * size + size).map((outfit) => {
              const full = outfit.obtained === outfit.items.length;
              return <article key={outfit.id} className="overflow-hidden rounded-xl border border-border bg-bg-secondary">
                <div className={clsx('h-1', outfit.rarity === 5 ? 'bg-warning/60' : 'bg-accent/50')} />
                <div className="p-4">
                  <div className="flex items-start gap-3"><div className={clsx('flex h-10 w-10 shrink-0 items-center justify-center rounded-xl', outfit.rarity === 5 ? 'bg-warning/10 text-warning' : 'bg-accent/10 text-accent')}><Sparkles className="h-5 w-5" /></div>
                    <div className="min-w-0 flex-1"><h3 className="truncate text-sm font-semibold" title={outfit.name}>{outfit.name}</h3><p className="mt-1 text-[11px] text-text-muted">{outfit.rarity} 星套装 · {outfit.items.length} 个部件</p></div>
                    {full && <span className="flex shrink-0 items-center gap-1 rounded-full bg-success/10 px-2 py-1 text-[11px] text-success"><Check className="h-3 w-3" />已集齐</span>}
                  </div>
                  <div className="mb-2 mt-4 flex items-end justify-between"><span className="text-xs text-text-secondary">收集进度</span><span className="text-lg font-semibold tabular-nums">{outfit.obtained}<span className="ml-1 text-xs font-normal text-text-muted">/ {outfit.items.length}</span></span></div>
                  <div className="flex gap-1" role="progressbar" aria-label={outfit.name} aria-valuenow={outfit.obtained} aria-valuemin={0} aria-valuemax={outfit.items.length}>{outfit.parts.map((part) => <span key={part.item} title={part.name + (part.count ? ' · 已获得' : ' · 未记录')} className={clsx('h-1.5 flex-1 rounded-full', part.count ? outfit.rarity === 5 ? 'bg-warning/70' : 'bg-accent/65' : 'bg-bg-active')} />)}</div>
                  <details className="group mt-3"><summary className="flex cursor-pointer list-none items-center justify-between text-[11px] text-text-muted hover:text-text-primary"><span>{outfit.copies ? '已集齐 ' + outfit.copies + ' 套' : '查看部件'}</span><ChevronDown className="h-3.5 w-3.5 transition-transform group-open:rotate-180" /></summary>
                    <div className="mt-3 grid grid-cols-2 gap-x-3 gap-y-2 border-t border-border pt-3">{outfit.parts.map((part) => <div key={part.item} className={clsx('flex min-w-0 items-center gap-1.5 text-[11px]', part.count ? 'text-text-primary' : 'text-text-muted')}><span className={clsx('h-1.5 w-1.5 shrink-0 rounded-full', part.count ? 'bg-success' : 'bg-border')} /><span className="truncate" title={part.name}>{part.name}</span>{part.count > 0 && <span className="ml-auto shrink-0 tabular-nums text-text-muted">×{part.count}</span>}</div>)}</div>
                  </details>
                </div>
              </article>;
            })}</div>
            : <div className="overflow-auto"><table className="w-full text-xs"><thead className="bg-bg-primary/40 text-left text-text-muted"><tr><th className="px-4 py-2.5 font-medium">抽取时间</th><th className="px-4 py-2.5 font-medium">共鸣结果</th><th className="px-4 py-2.5 font-medium">所属卡池</th></tr></thead><tbody>{rows.slice(currentPage * size, currentPage * size + size).map((row) => <tr key={row.key} className="border-t border-border hover:bg-bg-hover/60"><td className="whitespace-nowrap px-4 py-3 tabular-nums text-text-secondary">{pullTime(row.time)}</td><td className="px-4 py-3"><div className="flex items-center gap-2"><span className={clsx('shrink-0 rounded px-1.5 py-0.5 text-[10px]', row.rarity === 5 ? 'bg-warning/10 text-warning' : row.rarity === 4 ? 'bg-accent/10 text-accent' : 'bg-bg-active text-text-muted')}>{row.rarity || '—'}★</span><span title={row.item}>{row.name}</span></div></td><td className="px-4 py-3 text-text-secondary">{banners.get(row.bannerId)?.name || '共鸣 ' + row.bannerId}</td></tr>)}</tbody></table></div>}
          <div className="flex items-center gap-3 border-t border-border px-4 py-3 text-xs text-text-secondary"><span className="mr-auto">{count} {view === 'records' ? '条记录' : '套'} · {currentPage + 1}/{pages} 页</span><button disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)} className="rounded-md px-2 py-1 hover:bg-bg-hover disabled:opacity-30">上一页</button><button disabled={currentPage + 1 >= pages} onClick={() => setPage(currentPage + 1)} className="rounded-md px-2 py-1 hover:bg-bg-hover disabled:opacity-30">下一页</button></div>
        </section>
        <p className="px-1 text-[11px] leading-5 text-text-muted">本地存档：config/accounts/CN/{current?.history.uid || '<UID>'}.json · 目录更新：{status.catalog_updated_at || '—'}</p>
      </div>
    </div>
  </div>;
}
