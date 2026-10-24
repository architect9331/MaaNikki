import { useMemo, useState } from 'react';
import { ArrowLeft, Check, RefreshCw, Search, AlertCircle } from 'lucide-react';
import clsx from 'clsx';
import { useAccountStateStore } from '@/stores/accountStateStore';
import { collectionSlots as slots, collectionKey as keyOf, getBlessingProgress } from '@/utils/blessingProgress';

export function BlessingProgressPage({ onClose }: { onClose: () => void }) {
  const { data, error, loading, refresh } = useAccountStateStore();
  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState('all');
  const snapshot = data?.blessing;
  const progress = useMemo(() => getBlessingProgress(data), [data]);
  const { sets, target, uncertain } = progress;
  const count = (name: string, colors: string[], slot?: string) => (slot ? [slot] : slots)
    .reduce((n, part) => n + colors.filter((color) => progress.owned.has(keyOf({ name, slot: part, color }))).length, 0);
  const completeSets = sets.filter((s) => count(s.name, s.colors) === s.colors.length * 3).length;
  const completeParts = sets.reduce((n, s) => n + slots.filter((slot) => count(s.name, s.colors, slot) === s.colors.length).length, 0);
  const visible = sets.filter((s) => {
    const complete = count(s.name, s.colors) === s.colors.length * 3;
    return `${s.name} ${s.source}`.includes(search.trim()) && (filter === 'all' || (filter === 'complete' ? complete : !complete));
  });
  const groups = [...new Set(visible.map((s) => s.source))].map((source) => ({ source, sets: visible.filter((s) => s.source === source) }));
  const updated = snapshot?.record?.updated_at;

  return (
    <div className="flex-1 min-h-0 flex flex-col bg-bg-primary text-text-primary">
      <div className="flex items-center gap-3 px-5 py-3 border-b border-border bg-bg-secondary">
        <button onClick={onClose} className="p-2 rounded-md hover:bg-bg-hover" title="返回任务列表" aria-label="返回任务列表"><ArrowLeft className="w-4 h-4" /></button>
        <h1 className="text-lg font-semibold flex-1">祝福闪光收集进度</h1>
        <span className="hidden md:inline text-xs text-text-muted">{updated ? `更新于 ${new Date(updated).toLocaleString('zh-CN', { hour12: false })}` : '尚无收集记录'}</span>
        <button onClick={() => void refresh().catch(() => {})} disabled={loading}
          className="p-2 rounded-md hover:bg-bg-hover disabled:opacity-50" title="刷新记录" aria-label="刷新记录"><RefreshCw className={clsx('w-4 h-4', loading && 'animate-spin')} /></button>
      </div>
      <div className="flex-1 min-h-0 overflow-auto p-4 space-y-4">
        {error && <div className="rounded-md border border-error/30 bg-error/5 p-3 text-xs text-error" role="alert">无法读取账号状态：{error}</div>}
        {!error && !progress.known && <div className="rounded-md border border-border bg-bg-secondary p-3 text-sm text-text-secondary">{loading ? '正在读取本地记录…' : '尚无完整的五星收藏记录，请先运行祝福闪光整理。'}</div>}
        {uncertain && <p className="flex items-center gap-1.5 text-xs text-warning"><AlertCircle className="w-3.5 h-3.5" />存在未确认操作或收藏保护待复核，以下展示上次已确认的记录，请重新运行祝福闪光整理核对。</p>}
        <div className="grid grid-cols-3 gap-3">
          {[
            ['已收集颜色', progress.owned.size, target],
            ['收齐部位', completeParts, sets.length * 3],
            ['收齐套组', completeSets, sets.length],
          ].map(([label, current, total]) => <div key={label} className="rounded-lg border border-border bg-bg-secondary px-4 py-3">
            <div className="text-[13px] font-medium text-text-secondary">{label}</div>
            <div className="mt-1.5 flex items-baseline gap-2"><strong className="text-[28px] leading-9 font-semibold tabular-nums">{progress.known ? current : '—'}</strong><span className="text-sm text-text-secondary">/ {total || '—'}</span><span className="ml-auto text-sm font-medium tabular-nums text-accent">{progress.known && Number(total) ? `${Math.round(Number(current) / Number(total) * 100)}%` : '—'}</span></div>
            <div className="mt-2.5 h-1.5 rounded-full bg-bg-tertiary overflow-hidden"><div className="h-full rounded-full bg-accent transition-[width]" style={{ width: `${progress.known && Number(total) ? Number(current) / Number(total) * 100 : 0}%` }} /></div>
          </div>)}
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-2 rounded-md border border-border bg-bg-secondary px-3 py-2 text-sm focus-within:border-accent/50">
            <Search className="w-4 h-4 text-text-muted" /><input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="搜索套组或副本" aria-label="搜索套组或副本" className="w-36 bg-transparent outline-none" />
          </label>
          <div className="inline-flex rounded-md border border-border bg-bg-secondary p-0.5">
            {[['all', '全部'], ['missing', '未收齐'], ['complete', '已收齐']].map(([value, label]) => <button key={value} onClick={() => setFilter(value)} aria-pressed={filter === value} className={clsx('rounded px-3 py-1.5 text-sm transition-colors', filter === value ? 'bg-accent/10 text-accent font-medium' : 'text-text-secondary hover:bg-bg-hover')}>{label}</button>)}
          </div>
          <div className="ml-auto flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-text-secondary">
            <span className="flex items-center gap-1.5"><span className="flex h-4 w-4 items-center justify-center rounded border border-accent/40 bg-accent/10"><Check className="w-3 h-3 text-accent" /></span>已收集</span>
            <span className="flex items-center gap-1.5"><span className="h-4 w-4 rounded border border-dashed border-border-strong bg-bg-primary" />{progress.known ? '未收集' : '未记录'}</span>
            <span className="text-text-muted">五星 · 满级 · 已锁定</span>
          </div>
        </div>
        <div className="grid grid-cols-1 min-[1280px]:grid-cols-2 gap-4 items-start">
          {groups.map((group) => {
            const all = sets.filter((s) => s.source === group.source);
            const groupCount = all.reduce((n, s) => n + count(s.name, s.colors), 0);
            const groupTarget = all.reduce((n, s) => n + s.colors.length * 3, 0);
            return <section key={group.source} className="rounded-lg border border-border bg-bg-secondary shadow-sm overflow-hidden">
              <div className="flex items-center gap-3 px-3 py-3 border-b border-border">
                <span className="h-4 w-1 shrink-0 rounded-full bg-accent/70" aria-hidden="true" />
                <h2 className="text-sm font-semibold flex-1">{group.source}</h2><span className="text-sm font-medium text-text-secondary tabular-nums">{progress.known ? groupCount : '—'} <span className="font-normal text-text-muted">/ {groupTarget}</span></span>
                <div className="w-16 h-1.5 rounded-full bg-bg-tertiary overflow-hidden"><div className="h-full rounded-full bg-accent" style={{ width: `${groupCount / groupTarget * 100}%` }} /></div>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full min-w-[620px] text-sm table-fixed">
                  <thead className="bg-bg-tertiary/40 text-text-secondary text-xs"><tr><th className="w-[92px] px-3 py-2 text-left font-normal">套组</th>{slots.map((slot) => <th key={slot} className="py-2 font-normal">{slot}</th>)}<th className="w-16 font-normal">进度</th></tr></thead>
                  <tbody>{group.sets.map((set) => {
                    const total = set.colors.length * 3;
                    const owned = count(set.name, set.colors);
                    return <tr key={set.name} className="border-t border-border/60 hover:bg-bg-hover/30 transition-colors">
                      <th scope="row" className="px-3 py-2 text-left font-medium truncate" title={set.name}>{set.name}</th>
                      {slots.map((slot) => <td key={slot} className="px-1 py-2"><div className="flex justify-center gap-[3px]">{set.colors.map((color) => {
                        const collected = progress.owned.has(keyOf({ name: set.name, slot, color }));
                        return <span key={color} title={`${set.name} · ${slot} · ${color}：${collected ? '已收集' : progress.known ? '未收集' : '未记录'}`}
                          aria-label={`${slot}${color}${collected ? '已收集' : progress.known ? '未收集' : '未记录'}`}
                          className={clsx('inline-flex shrink-0 flex-col items-center justify-center gap-0.5 w-[26px] h-9 rounded-md border', collected ? 'border-accent/35 bg-accent/10 text-text-primary' : 'border-dashed border-border-strong bg-bg-primary text-text-secondary')}>
                          <span className="text-sm leading-4 font-semibold">{color}</span>
                          <span className="flex h-2.5 items-center justify-center" aria-hidden="true">{collected ? <Check className="w-2.5 h-2.5 text-accent stroke-[2.5]" /> : <span className="w-1.5 h-px bg-border-strong" />}</span>
                        </span>;
                      })}</div></td>)}
                      <td className="px-2 text-center tabular-nums text-[13px]">
                        <span className={clsx('font-semibold', owned === total && progress.known ? 'text-accent' : 'text-text-primary')}>{progress.known ? owned : '—'}</span><span className="text-text-muted"> / {total}</span>
                      </td>
                    </tr>;
                  })}</tbody>
                </table>
              </div>
            </section>;
          })}
        </div>
        {data && !groups.length && <p className="py-6 text-center text-xs text-text-muted">没有符合条件的套组</p>}
      </div>
    </div>
  );
}
