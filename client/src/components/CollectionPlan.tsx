import type { ReactNode } from 'react';
import { Bot, Check, ListChecks, Square, Sparkles, X } from 'lucide-react';
import clsx from 'clsx';
import type { PlanMode } from '@/services/scoring/types';

/**
 * Building blocks shared by every collection plan (制衣、换新、五星闪光):
 * the same 全收集 / 自选 switch, the same "hand to the automatic task" bar,
 * the same item list shell and the same two-column layout.
 */

export function PlanModeSwitch({ value, onChange, allLabel, allDetail, selectedDetail, selectDisabledReason }: {
  value: PlanMode;
  onChange: (mode: PlanMode) => void;
  allLabel: string;
  allDetail: ReactNode;
  selectedDetail: ReactNode;
  /** When set, 自选 is shown but unavailable for this kind of plan. */
  selectDisabledReason?: string;
}) {
  const options = [
    { id: 'all' as const, icon: Sparkles, title: '全收集', label: allLabel, detail: allDetail, disabled: false },
    { id: 'selected' as const, icon: ListChecks, title: '自选', label: '只收集勾选的', detail: selectDisabledReason ?? selectedDetail, disabled: !!selectDisabledReason },
  ];
  return <div role="radiogroup" aria-label="收集范围" className="grid grid-cols-1 gap-2 sm:grid-cols-2">
    {options.map(({ id, icon: Icon, title, label, detail, disabled }) => {
      const active = value === id;
      return <button key={id} type="button" role="radio" aria-checked={active} disabled={disabled} onClick={() => onChange(id)}
        className={clsx('rounded-xl border px-3 py-2.5 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-55',
          active ? 'border-accent bg-accent/10' : 'border-border bg-bg-secondary hover:bg-bg-hover')}>
        <span className="flex items-center gap-2 text-sm font-semibold">
          <Icon className={clsx('h-4 w-4 shrink-0', active ? 'text-accent' : 'text-text-muted')} />
          {title}<span className="truncate text-xs font-normal text-text-secondary">{label}</span>
          {active && <Check className="ml-auto h-4 w-4 shrink-0 text-accent" />}
        </span>
        <span className="mt-1 block text-xs leading-5 text-text-secondary">{detail}</span>
      </button>;
    })}
  </div>;
}

/** One wording for every plan: hand it to the automatic task, or stop. */
export function PlanTaskBar({ taskLabel, active, status, onStart, onStop, disabled, hint }: {
  taskLabel: string;
  active: boolean;
  status: ReactNode;
  onStart: () => void;
  onStop: () => void;
  disabled?: boolean;
  hint?: ReactNode;
}) {
  return <section className={clsx('flex flex-wrap items-center gap-x-3 gap-y-2 rounded-xl border px-4 py-3',
    active ? 'border-accent/40 bg-accent/5' : 'border-border bg-bg-secondary')}>
    <Bot className={clsx('h-4 w-4 shrink-0', active ? 'text-accent' : 'text-text-muted')} />
    <div className="min-w-48 flex-1">
      <p className="text-sm font-medium" role="status">{active ? `已交给「${taskLabel}」自动收集` : `未交给自动任务 · 「${taskLabel}」`}</p>
      <p className="mt-0.5 text-xs leading-5 text-text-secondary">{status}</p>
      {hint && <p className="mt-0.5 text-[11px] leading-5 text-text-muted">{hint}</p>}
    </div>
    {active
      ? <button type="button" onClick={onStop} className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-xs hover:bg-bg-hover">
        <Square className="h-3.5 w-3.5" />停止自动收集
      </button>
      : <button type="button" onClick={onStart} disabled={disabled} className="inline-flex items-center gap-1.5 rounded-lg bg-accent px-3 py-1.5 text-xs font-medium text-on-accent hover:bg-accent-hover disabled:opacity-40">
        <Bot className="h-3.5 w-3.5" />交给自动任务
      </button>}
  </section>;
}

/** Item list shell: in 全收集 it previews what is included, in 自选 rows carry checkboxes. */
export function PlanListPanel({ title, mode, includedCount, selectedCount, onClear, filters, bulk, empty, children, more }: {
  title: string;
  mode: PlanMode;
  includedCount: number;
  selectedCount: number;
  onClear: () => void;
  filters?: ReactNode;
  bulk?: ReactNode;
  empty?: ReactNode;
  children?: ReactNode;
  more?: { shown: number; total: number; onMore: () => void };
}) {
  return <section className="overflow-hidden rounded-xl border border-border bg-bg-secondary">
    <div className="space-y-2 border-b border-border px-4 py-3">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold">{title}</h3>
        {mode === 'selected'
          ? selectedCount > 0
            ? <button type="button" onClick={onClear} className="inline-flex items-center gap-1 text-xs text-text-secondary hover:text-accent"><X className="h-3 w-3" />清空已选 {selectedCount} 件</button>
            : <span className="text-[11px] text-text-muted">勾选要收集的条目</span>
          : <span className="text-[11px] text-text-muted">全收集共 {includedCount.toLocaleString('zh-CN')} 件，下方仅供查看</span>}
      </div>
      {filters && <div className="flex flex-wrap gap-2">{filters}</div>}
      {mode === 'selected' && bulk && <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-text-secondary">{bulk}</div>}
    </div>
    {empty
      ? <p className="px-4 py-10 text-center text-xs text-text-secondary">{empty}</p>
      : <div className="max-h-[560px] divide-y divide-border overflow-auto">
        {children}
        {more && more.total > more.shown && <button type="button" onClick={more.onMore}
          className="w-full py-2 text-xs text-text-secondary hover:bg-bg-hover">加载更多（{more.shown} / {more.total}）</button>}
      </div>}
  </section>;
}

export function PlanLayout({ list, summary }: { list: ReactNode; summary: ReactNode }) {
  return <div className="grid grid-cols-1 items-start gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)]">
    {list}
    <div className="space-y-4 lg:sticky lg:top-0">{summary}</div>
  </div>;
}

export const planField = 'rounded-lg border border-border bg-bg-primary px-2.5 py-1.5 text-xs outline-none focus:border-accent';
export const planCheckbox = 'h-3.5 w-3.5 shrink-0 accent-accent';
