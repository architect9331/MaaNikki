import { Fragment, type ReactNode } from 'react';
import { ChevronRight, Settings2, X } from 'lucide-react';
import clsx from 'clsx';
import type { PlanMode } from '@/services/scoring/types';
import { REALM_GOAL_LABELS, type PlanGoal, type RealmGoal } from '@/utils/nikkiOptions';
import { useRealmQueue } from './RealmQueueCard';
import { HelpTip } from './ui/HelpTip';
import { Segmented, secondaryButton } from './ui/FeaturePage';

/**
 * Building blocks shared by every collection plan (制衣、焕新、五星闪光).
 * Each plan opens with the same panel — what the numbers are based on, what is
 * in scope, and where the realm challenge farms for it (read-only: the order,
 * its switches and the weekly realm are all set in the realm challenge task) — followed by the
 * same two columns: the items on the left, what they add up to on the right.
 */

const TONE_DOT = { ok: 'bg-success', warn: 'bg-warning', idle: 'bg-text-muted', busy: 'bg-accent animate-pulse' };
const MODES = [['all', '全收集'], ['selected', '自选']] as const;

function Row({ label, children, actions }: { label: string; children: ReactNode; actions?: ReactNode }) {
  return <div className="group/option flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-2.5">
    <span className="w-16 shrink-0 text-xs text-text-muted">{label}</span>
    <div className="flex min-w-56 flex-1 flex-wrap items-center gap-x-3 gap-y-1.5">{children}</div>
    {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
  </div>;
}

/** The realm challenge order as chips, so the whole queue is visible without leaving the plan. */
function QueueChain({ goal }: { goal: RealmGoal }) {
  const { enabledGoals } = useRealmQueue();
  return <span className="flex flex-wrap items-center gap-1 text-[11px]" aria-label="幻境挑战刷取顺序">
    {enabledGoals.map((item, index) => <Fragment key={item}>
      <span className={clsx('inline-flex items-center gap-1 rounded-md px-1.5 py-0.5',
        item === goal ? 'bg-accent/15 font-medium text-accent' : 'bg-bg-tertiary text-text-secondary')}>
        <span className="tabular-nums">{index + 1}</span>{REALM_GOAL_LABELS[item]}
      </span>
      <ChevronRight className="h-3 w-3 text-text-muted" aria-hidden="true" />
    </Fragment>)}
    <span className="rounded-md border border-dashed border-border px-1.5 py-0.5 text-text-muted">兜底副本</span>
  </span>;
}

export function PlanPanel({ tone = 'ok', basis, actions, goal, mode, onModeChange, modeDetail, selectDisabledReason,
  queueStatus, queueHint, advice }: {
  tone?: keyof typeof TONE_DOT;
  /** What this plan is calculated from, and how fresh that is. */
  basis: ReactNode;
  actions?: ReactNode;
  goal: PlanGoal;
  mode: PlanMode;
  onModeChange: (mode: PlanMode) => void;
  /** One line describing what the current scope covers. */
  modeDetail: ReactNode;
  /** When set, 自选 is unavailable for this kind of plan. */
  selectDisabledReason?: string;
  queueStatus: ReactNode;
  queueHint?: ReactNode;
  advice?: { title: ReactNode; detail?: ReactNode; warn?: boolean };
}) {
  const queue = useRealmQueue();
  const rank = queue.rank(goal);
  return <section className="divide-y divide-border overflow-hidden rounded-xl border border-border bg-bg-secondary">
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-2.5">
      <span aria-hidden="true" className={clsx('h-2 w-2 shrink-0 rounded-full', TONE_DOT[tone])} />
      <p className="min-w-56 flex-1 text-xs leading-5 text-text-secondary" role="status">{basis}</p>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
    <Row label="收集范围">
      {selectDisabledReason
        ? <span className="text-xs font-medium text-text-primary">全收集</span>
        : <Segmented label="收集范围" value={mode} options={MODES} onChange={onModeChange} />}
      <span className="min-w-40 flex-1 text-xs leading-5 text-text-secondary">{selectDisabledReason && mode === 'all' ? `${selectDisabledReason} ` : ''}{modeDetail}</span>
    </Row>
    <Row label="自动刷取" actions={
      <button type="button" onClick={queue.openOrderCard} className={secondaryButton}><Settings2 className="h-3.5 w-3.5" />刷取设置</button>
    }>
      <span className={clsx('inline-flex items-center gap-1 text-xs font-medium', rank ? 'text-accent' : 'text-text-primary')}>
        {rank ? `刷取顺序第 ${rank} 位` : `未开启（在幻境挑战的刷取顺序中打开「${REALM_GOAL_LABELS[goal]}」）`}
        {queueHint && <HelpTip content={queueHint} label="自动刷取说明" />}
      </span>
      <QueueChain goal={goal} />
      <span className="w-full text-xs leading-5 text-text-secondary">
        {queueStatus}
        {queue.hasTask && !queue.taskEnabled && <span className="text-warning"> · 主界面的「幻境挑战」任务当前未勾选，不会运行</span>}
      </span>
    </Row>
    {advice && <Row label="今日建议">
      <span className={clsx('text-xs font-medium', advice.warn ? 'text-warning' : 'text-text-primary')}>{advice.title}</span>
      {advice.detail && <span className="min-w-40 flex-1 text-xs leading-5 text-text-secondary">{advice.detail}</span>}
    </Row>}
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
        <h2 className="text-sm font-semibold">{title}</h2>
        {mode === 'selected'
          ? selectedCount > 0
            ? <button type="button" onClick={onClear} className="inline-flex items-center gap-1 text-xs text-text-secondary hover:text-accent"><X className="h-3 w-3" />清空已选 {selectedCount} 件</button>
            : <span className="text-[11px] text-text-muted">勾选要收集的条目</span>
          : <span className="text-[11px] text-text-muted">共 {includedCount.toLocaleString('zh-CN')} 件，仅供查看</span>}
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

/** Shown in place of a plan that cannot be calculated yet. */
export function PlanEmpty({ icon: Icon, children, action }: { icon: React.ComponentType<{ className?: string }>; children: ReactNode; action?: ReactNode }) {
  return <section className="rounded-xl border border-dashed border-border bg-bg-secondary px-6 py-10 text-center">
    <Icon className="mx-auto h-10 w-10 text-accent opacity-60" />
    <p className="mx-auto mt-3 max-w-md text-sm leading-6 text-text-secondary">{children}</p>
    {action && <div className="mt-4 flex justify-center">{action}</div>}
  </section>;
}

export const bulkButton = 'rounded-md border border-border px-2 py-0.5 hover:border-accent hover:text-accent';
export const planField = 'rounded-lg border border-border bg-bg-primary px-2.5 py-1.5 text-xs outline-none focus:border-accent';
export const planCheckbox = 'h-3.5 w-3.5 shrink-0 accent-accent';
