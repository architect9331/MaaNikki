import { useMemo, useState, type ReactNode } from 'react';
import {
  AlertTriangle,
  CalendarClock,
  Check,
  ChevronDown,
  ChevronRight,
  GripVertical,
  Hammer,
  Loader2,
  Shield,
  Sparkles,
  Shirt,
} from 'lucide-react';
import clsx from 'clsx';
import { toast } from 'sonner';
import { DndContext, KeyboardSensor, PointerSensor, closestCenter, useSensor, useSensors, type DragEndEvent } from '@dnd-kit/core';
import { SortableContext, arrayMove, sortableKeyboardCoordinates, useSortable, verticalListSortingStrategy } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { useAppStore } from '@/stores/appStore';
import { useAccountStateStore } from '@/stores/accountStateStore';
import { openCollectionHub, type CollectionTab } from '@/stores/collectionHubStore';
import { useForecastAssumptionsStore } from '@/stores/forecastAssumptionsStore';
import { useRealmForecast } from '@/services/useRealmForecast';
import type { QueueGoalEstimate } from '@/services/realmForecast';
import { getBlessingProgress } from '@/utils/blessingProgress';
import {
  REALM_GOAL_LABELS,
  WEEKLY_TARGETS,
  doubleEnabled,
  doubleJihuaMode,
  doubleTypes,
  realmCollectionWorkflow,
  realmFarmBling,
  realmFarmThread,
  realmQueue,
  realmQueueOption,
  realmReserveBling,
  weeklyTargets,
  type RealmGoal,
  type RealmQueueEntry,
} from '@/utils/nikkiOptions';
import type { OptionValue } from '@/types/interface';
import { QueueCombinedTrendPanel, compactNumber } from './RealmForecastPanel';
import { SwitchTrack } from './ui/SwitchTrack';

export const REALM_QUEUE_CARD_ID = 'realm-queue-card';
const GOAL_INFO: Record<RealmGoal, { icon: typeof Shirt; detail: string; tab?: CollectionTab }> = {
  weekly: { icon: CalendarClock, detail: '本周仍有挑战次数时，依次挑战所选关卡各一次' },
  renewal: { icon: Shirt, detail: '按「焕新」计划刷泡泡、丝线与噗灵', tab: 'renewal' },
  crafting: { icon: Hammer, detail: '按「制衣」计划刷魔物试炼晶石', tab: 'crafting' },
  glow: { icon: Sparkles, detail: '按「祝福闪光」计划选本追新色，所选范围收齐后跳过', tab: 'blessing' },
};
const FALLBACK_LABELS: Record<string, string> = {
  Bless: '祝福闪光幻境',
  Jihua: '素材激化幻境',
  Monster: '魔物试炼幻境',
};

const formatEnergy = (energy: number) => `${compactNumber(energy)} 体力`;

function EstimatePill({ estimate, computing }: { estimate: QueueGoalEstimate; computing: boolean }) {
  if (estimate.status === 'needs_data') {
    return (
      <span className="mt-1 inline-flex items-center gap-1 rounded-md bg-bg-tertiary px-1.5 py-0.5 text-[11px] text-text-muted">
        {estimate.note ?? '待同步记录'}
      </span>
    );
  }
  if (estimate.status === 'computing') {
    return (
      <span className="mt-1 inline-flex items-center gap-1 rounded-md bg-bg-tertiary px-1.5 py-0.5 text-[11px] text-text-muted">
        <Loader2 className="h-3 w-3 animate-spin" />
        正在估算体力与天数…
      </span>
    );
  }
  if (estimate.status === 'stalled') {
    return (
      <span className="mt-1 inline-flex items-center gap-1 rounded-md bg-warning/10 px-1.5 py-0.5 text-[11px] text-warning">
        {estimate.note ?? '仓库已满需处理'}
      </span>
    );
  }

  const energy = estimate.ownEnergy ?? 0;
  const days = estimate.ownDays;
  const queuedDay = estimate.queuedFinishDay;
  const hasQueuedDelay = estimate.enabled && queuedDay !== null && days !== null && queuedDay > days;

  const tooltip =
    estimate.ownDaysLow != null && estimate.ownDaysHigh != null && estimate.ownEnergyLow != null && estimate.ownEnergyHigh != null
      ? `八成区间：${estimate.ownDaysLow}～${estimate.ownDaysHigh} 天 · ${compactNumber(estimate.ownEnergyLow)}～${compactNumber(estimate.ownEnergyHigh)} 体力`
      : estimate.note;

  if (estimate.status === 'done' || (energy === 0 && days === 0)) {
    const label =
      estimate.goal === 'weekly'
        ? `本周已完成 · 0 体力（每周 ${estimate.weeklyPerWeek ?? 0} 体力）`
        : estimate.goal === 'glow' && estimate.note
          ? `${estimate.note} · 0 体力 · 0 天`
          : '已备齐 · 0 体力 · 0 天';
    return (
      <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[11px] tabular-nums" title={tooltip}>
        <span className={clsx('rounded-md px-1.5 py-0.5 font-medium', estimate.enabled ? 'bg-success/10 text-success' : 'bg-bg-tertiary text-text-muted')}>
          {estimate.enabled ? label : `未开启 · ${label}`}
        </span>
        {estimate.note && estimate.goal !== 'glow' && <span className="text-text-muted">{estimate.note}</span>}
      </div>
    );
  }

  const daysText = days === null ? '10 年以上' : `约 ${days} 天`;
  const mainText = `预期 ${formatEnergy(energy)} · ${daysText}`;

  return (
    <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[11px] tabular-nums" title={tooltip}>
      <span
        className={clsx(
          'inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 font-medium',
          estimate.enabled ? 'bg-accent/10 text-accent' : 'bg-bg-tertiary text-text-muted',
        )}
      >
        {estimate.enabled ? mainText : `未开启 · 自身${mainText}`}
        {computing && estimate.goal === 'glow' && <Loader2 className="h-3 w-3 animate-spin opacity-70" />}
      </span>
      {estimate.goal === 'weekly' && estimate.weeklyPerWeek != null && (
        <span className="text-text-muted">每周固定 {estimate.weeklyPerWeek} 体力</span>
      )}
      {hasQueuedDelay && (
        <span className="rounded-md bg-bg-tertiary px-1.5 py-0.5 text-text-secondary">
          按顺序约第 {queuedDay} 天完成
        </span>
      )}
      {estimate.enabled && estimate.goal === 'glow' && queuedDay === null && days !== null && (
        <span className="rounded-md bg-bg-tertiary px-1.5 py-0.5 text-text-secondary">
          按顺序需等前置目标完成
        </span>
      )}
      {estimate.note && estimate.goal !== 'weekly' && <span className="text-text-muted">{estimate.note}</span>}
    </div>
  );
}

/** The realm challenge task of the current profile and its goal queue. */
export function useRealmQueue() {
  const instances = useAppStore((state) => state.instances);
  const activeInstanceId = useAppStore((state) => state.activeInstanceId);
  const setTaskOptionValue = useAppStore((state) => state.setTaskOptionValue);
  const toggleTaskExpanded = useAppStore((state) => state.toggleTaskExpanded);
  const setCurrentPage = useAppStore((state) => state.setCurrentPage);
  const instance = instances.find((item) => item.id === activeInstanceId) ?? instances[0];
  const task = instance?.selectedTasks.find((item) => item.taskName === 'realm_challenge');
  const entries = useMemo(() => realmQueue(task?.optionValues), [task?.optionValues]);
  const enabledGoals = entries.filter((entry) => entry.enabled).map((entry) => entry.goal);

  const save = (next: RealmQueueEntry[]) => {
    if (!instance || !task) {
      toast.error('当前配置中还没有「幻境挑战」任务，请先在主界面添加。');
      return false;
    }
    setTaskOptionValue(instance.id, task.id, 'RealmQueue', realmQueueOption(next));
    return true;
  };
  const rank = (goal: RealmGoal) => {
    const index = enabledGoals.indexOf(goal);
    return index < 0 ? null : index + 1;
  };
  const openOrderCard = () => {
    if (!instance || !task) {
      toast.error('当前配置中还没有「幻境挑战」任务，请先在主界面添加。');
      return;
    }
    setCurrentPage('main');
    if (!task.expanded) toggleTaskExpanded(instance.id, task.id);
    window.setTimeout(
      () => document.getElementById(REALM_QUEUE_CARD_ID)?.scrollIntoView({ behavior: 'smooth', block: 'center' }),
      300,
    );
  };
  return { hasTask: !!task, taskEnabled: !!task?.enabled, entries, enabledGoals, rank, save, openOrderCard };
}

function QueueRow({
  entry,
  rank,
  disabled,
  warn,
  estimate,
  computing,
  detailOverride,
  onToggle,
  children,
}: {
  entry: RealmQueueEntry;
  rank: number | null;
  disabled: boolean;
  warn: boolean;
  estimate: QueueGoalEstimate;
  computing: boolean;
  detailOverride?: string;
  onToggle: (enabled: boolean) => void;
  children?: ReactNode;
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: entry.goal,
    disabled,
  });
  const info = GOAL_INFO[entry.goal];
  const Icon = info.icon;
  return (
    <div
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      className={clsx(
        'flex items-center gap-2 rounded-lg border px-2.5 py-2 text-xs',
        entry.enabled ? 'border-accent/40 bg-accent/5' : 'border-border bg-bg-secondary',
        isDragging && 'z-10 shadow-lg',
      )}
    >
      <button
        type="button"
        aria-label={`拖动调整${REALM_GOAL_LABELS[entry.goal]}的顺序`}
        disabled={disabled}
        className="cursor-grab touch-none rounded p-0.5 text-text-muted hover:bg-bg-hover disabled:cursor-not-allowed"
        {...attributes}
        {...listeners}
      >
        <GripVertical className="h-4 w-4" />
      </button>
      <span
        className={clsx(
          'flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold tabular-nums',
          rank ? 'bg-accent text-on-accent' : 'bg-bg-tertiary text-text-muted',
        )}
      >
        {rank ?? '–'}
      </span>
      <Icon className={clsx('h-4 w-4 shrink-0', entry.enabled ? 'text-accent' : 'text-text-muted')} />
      <div className="min-w-0 flex-1">
        <p className="font-semibold text-text-primary">{REALM_GOAL_LABELS[entry.goal]}</p>
        <p className="truncate text-[11px] text-text-secondary">{detailOverride ?? info.detail}</p>
        <EstimatePill estimate={estimate} computing={computing} />
        {children}
        {warn && (
          <p className="mt-0.5 flex items-center gap-1 text-[11px] text-warning">
            <AlertTriangle className="h-3 w-3 shrink-0" />
            所选范围收齐前会一直刷闪光，排在后面的目标要等收齐后才会轮到
          </p>
        )}
      </div>
      {info.tab && (
        <button
          type="button"
          onClick={() => openCollectionHub(info.tab!)}
          className="inline-flex shrink-0 items-center text-[11px] text-text-secondary hover:text-accent"
        >
          查看计划
          <ChevronRight className="h-3 w-3" />
        </button>
      )}
      <button
        type="button"
        role="switch"
        aria-checked={entry.enabled}
        aria-label={`开启${REALM_GOAL_LABELS[entry.goal]}`}
        disabled={disabled}
        onClick={() => onToggle(!entry.enabled)}
        className="inline-flex shrink-0 items-center rounded-full focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50"
      >
        <SwitchTrack checked={entry.enabled} />
      </button>
    </div>
  );
}

const WORKFLOW_MODES = [
  { id: 'Managed', title: '全流程托管', detail: '自动整理、按预期新色刷本、分解、升级并锁定满级新颜色' },
  { id: 'FarmOnly', title: '只刷胚子', detail: '按已收集颜色与仓库胚子数算期望选本刷取，升级与管理交给自己' },
] as const;

/** The weekly realm levels, picked inside its queue row; at least one stays selected. */
function WeeklyTargetPicker({
  selected,
  disabled,
  onChange,
}: {
  selected: string[];
  disabled: boolean;
  onChange: (next: string[]) => void;
}) {
  return (
    <div className="mt-1.5 flex flex-wrap items-center gap-1.5" role="group" aria-label="每周幻境目标">
      {WEEKLY_TARGETS.map(({ id, label }) => {
        const on = selected.includes(id);
        const locked = on && selected.length <= 1;
        return (
          <button
            key={id}
            type="button"
            aria-pressed={on}
            disabled={disabled || locked}
            title={locked ? '至少保留一个目标' : undefined}
            onClick={() =>
              onChange(
                on
                  ? selected.filter((item) => item !== id)
                  : WEEKLY_TARGETS.map((t) => t.id).filter((item) => item === id || selected.includes(item)),
              )
            }
            className={clsx(
              'inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[11px] transition-colors disabled:cursor-not-allowed',
              on
                ? 'border-accent/50 bg-accent/10 font-medium text-accent'
                : 'border-border text-text-secondary hover:border-accent hover:text-accent',
              disabled && 'opacity-60',
            )}
          >
            {on && <Check className="h-3 w-3" />}
            {label}
          </button>
        );
      })}
    </div>
  );
}

/** 幻境挑战「刷取顺序」: ordered goals with switches, live energy/days estimates, then the fixed fallback realm. */
export function RealmQueueCard({
  instanceId,
  taskId,
  values,
  disabled,
}: {
  instanceId: string;
  taskId: string;
  values: Record<string, OptionValue>;
  disabled: boolean;
}) {
  const setTaskOptionValue = useAppStore((state) => state.setTaskOptionValue);
  const { dailyEnergy, setDailyEnergy } = useForecastAssumptionsStore();
  const [showTrend, setShowTrend] = useState(false);

  const entries = realmQueue(values);
  const enabled = entries.filter((entry) => entry.enabled).map((entry) => entry.goal);
  const farmThread = realmFarmThread(values);
  const farmBling = realmFarmBling(values);
  const dblEnabled = doubleEnabled(values);
  const dblTypes = dblEnabled ? doubleTypes(values) : [];
  const dblTypeLabels: Record<string, string> = { Jihua: '超级激化', Bless: '灵感迸发', Weekly: '深度突破' };
  const dblFollowJihuaEmpty =
    dblEnabled
    && dblTypes.includes('Jihua')
    && doubleJihuaMode(values) === 'FollowDeficit'
    && !enabled.includes('renewal')
    && !enabled.includes('crafting');
  const dblFollowCraftingNoCommon =
    dblEnabled
    && dblTypes.includes('Jihua')
    && doubleJihuaMode(values) === 'FollowDeficit'
    && !enabled.includes('renewal')
    && enabled.includes('crafting')
    && !farmThread
    && !farmBling;
  const renewalMats = ['泡泡', farmThread ? '丝线' : null, farmBling ? '噗灵' : null].filter(Boolean) as string[];
  const renewalSkipped = [!farmThread ? '丝线' : null, !farmBling ? '噗灵' : null].filter(Boolean) as string[];
  const renewalDetail = renewalSkipped.length
    ? `按「焕新」计划刷${renewalMats.join('、')}（默认跳过${renewalSkipped.join('与')}）`
    : '按「焕新」计划刷泡泡、丝线与噗灵';
  const glowIndex = enabled.indexOf('glow');
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );
  const save = (next: RealmQueueEntry[]) => setTaskOptionValue(instanceId, taskId, 'RealmQueue', realmQueueOption(next));
  const onDragEnd = ({ active, over }: DragEndEvent) => {
    if (!over || active.id === over.id) return;
    const from = entries.findIndex((entry) => entry.goal === active.id);
    const to = entries.findIndex((entry) => entry.goal === over.id);
    if (from >= 0 && to >= 0) save(arrayMove(entries, from, to));
  };
  const fallback = values.RealmTypes?.type === 'select' ? values.RealmTypes.caseName : 'Bless';

  const { queueSummary, glowComputing } = useRealmForecast({ optionValues: values });
  const { totalFiniteEnergy, totalFiniteDays, hasUnknownEnabled, hasInfiniteOrStalled } = queueSummary;

  const summaryText =
    enabled.length === 0
      ? '未开启队列目标，全部体力直接用于兜底副本'
      : hasUnknownEnabled
        ? '部分已开启目标尚无记录，可点右侧「查看计划」同步后自动计算'
        : hasInfiniteOrStalled
          ? '存在无法继续或预计超过 10 年的目标，请检查仓库分解设置'
          : totalFiniteEnergy === 0
            ? '当前已开启目标均已就绪，剩余体力直接用于兜底副本'
            : `已开启目标合计约 ${compactNumber(totalFiniteEnergy ?? 0)} 体力 · 预计 ${totalFiniteDays ?? 0} 天完成后转入兜底`;

  return (
    <div id={REALM_QUEUE_CARD_ID} className="mb-4 space-y-2 rounded-lg border border-border bg-bg-primary p-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="text-sm font-semibold">刷取顺序</p>
        <p className="text-[11px] text-text-secondary">每批挑战前选排在最前、仍有缺口的目标；拖动调整先后</p>
      </div>
      {dblEnabled && dblTypes.length > 0 && (
        <div className="space-y-1 rounded-lg border border-accent/30 bg-accent/5 px-2.5 py-1.5 text-[11px] text-text-secondary">
          <p className="flex flex-wrap items-center gap-1.5">
            <span className="inline-flex items-center gap-1 font-medium text-accent">
              <Sparkles className="h-3 w-3 shrink-0" />
              限时双倍优先插队
            </span>
            <span>
              已启用：{dblTypes.map((item) => dblTypeLabels[item] ?? item).join('、')}（对应目标完成时跳过，不强行刷取）
            </span>
          </p>
          {dblFollowJihuaEmpty && (
            <p className="flex items-center gap-1 text-warning">
              <AlertTriangle className="h-3 w-3 shrink-0" />
              素材激化当前设为「跟随焕新 / 制衣缺口」，但下方刷取顺序中这两项均未开启，双倍激化将跳过（朝夕心愿仍会自动改刷备用目标）；如需双倍也固定刷取可改为「继承兜底设置」。
            </p>
          )}
          {dblFollowCraftingNoCommon && (
            <p className="flex items-center gap-1 text-text-secondary">
              <AlertTriangle className="h-3 w-3 shrink-0 text-warning" />
              制衣材料默认仅刷魔物晶石（不含丝线与噗灵）；如需趁激化双倍刷取，可在下方分别开启「用体力刷纯真丝线」或「用体力刷噗灵」。
            </p>
          )}
        </div>
      )}
      <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={onDragEnd}>
        <SortableContext items={entries.map((entry) => entry.goal)} strategy={verticalListSortingStrategy}>
          <div className="space-y-1.5">
            {entries.map((entry) => {
              const index = enabled.indexOf(entry.goal);
              return (
                <QueueRow
                  key={entry.goal}
                  entry={entry}
                  rank={index < 0 ? null : index + 1}
                  disabled={disabled}
                  estimate={queueSummary.goals[entry.goal]}
                  computing={glowComputing}
                  detailOverride={
                    entry.goal === 'renewal' ? renewalDetail : undefined
                  }
                  warn={entry.goal === 'glow' && glowIndex >= 0 && glowIndex < enabled.length - 1}
                  onToggle={(on) => save(entries.map((item) => (item.goal === entry.goal ? { ...item, enabled: on } : item)))}
                >
                  {entry.goal === 'weekly' && entry.enabled && (
                    <WeeklyTargetPicker
                      selected={weeklyTargets(values)}
                      disabled={disabled}
                      onChange={(next) => setTaskOptionValue(instanceId, taskId, 'WeeklyTargets', { type: 'checkbox', caseNames: next })}
                    />
                  )}
                </QueueRow>
              );
            })}
          </div>
        </SortableContext>
      </DndContext>

      <div className="flex items-center gap-2 rounded-lg border border-dashed border-border px-2.5 py-2 text-xs">
        <Shield className="ml-6 h-4 w-4 shrink-0 text-text-muted" />
        <div className="min-w-0 flex-1">
          <p className="font-semibold text-text-primary">兜底 · {FALLBACK_LABELS[fallback] ?? fallback}</p>
          <p className="text-[11px] text-text-secondary">
            {totalFiniteDays !== null && totalFiniteDays > 0 && enabled.length > 0
              ? `以上目标完成前暂不刷兜底；约第 ${totalFiniteDays} 天清完后每日投入 ${dailyEnergy} 体力`
              : '上面的目标都完成或未开启时，剩余活跃能量按下方「兜底副本」设置使用'}
          </p>
        </div>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border bg-bg-secondary/60 px-2.5 py-1.5 text-[11px]">
        <span className="min-w-0 flex-1 text-text-secondary tabular-nums">{summaryText}</span>
        <div className="flex shrink-0 items-center gap-2">
          <label className="inline-flex items-center gap-1 text-text-secondary" title="与「收集进度」各页签的预估天数共用同一每日活跃能量基准">
            <span>每日能量</span>
            <input
              type="number"
              min={10}
              step={10}
              aria-label="每日活跃能量基准"
              value={dailyEnergy}
              onChange={(event) => setDailyEnergy(Math.max(10, Number(event.target.value) || 10))}
              className="w-14 rounded border border-border bg-bg-primary px-1.5 py-0.5 text-center text-[11px] tabular-nums outline-none focus:border-accent"
            />
          </label>
          {enabled.length > 0 && (
            <button
              type="button"
              onClick={() => setShowTrend((value) => !value)}
              aria-expanded={showTrend}
              className="inline-flex items-center gap-1 rounded-md border border-border bg-bg-primary px-2 py-0.5 text-[11px] text-text-secondary hover:border-accent hover:text-accent"
            >
              队列趋势
              <ChevronDown className={clsx('h-3 w-3 transition-transform', showTrend && 'rotate-180')} />
            </button>
          )}
        </div>
      </div>

      {showTrend && enabled.length > 0 && <QueueCombinedTrendPanel summary={queueSummary} />}
    </div>
  );
}

/** Execution mode and status belong only to the queued five-star collection goal. */
export function RealmCollectionWorkflow({
  instanceId,
  taskId,
  values,
  disabled,
}: {
  instanceId: string;
  taskId: string;
  values: Record<string, OptionValue>;
  disabled: boolean;
}) {
  const setTaskOptionValue = useAppStore((state) => state.setTaskOptionValue);
  const account = useAccountStateStore((state) => state.data);
  const progress = useMemo(() => getBlessingProgress(account), [account]);
  const workflow = realmCollectionWorkflow(values);
  const reserveBling = realmReserveBling(values);
  const decompose = values.CollectionDecompose?.type === 'switch' && values.CollectionDecompose.value;

  return (
    <div className="mb-3 space-y-2 border-b border-border pb-3 text-xs">
      <p className="font-medium text-text-primary">五星闪光执行方式</p>
      <div role="radiogroup" aria-label="五星闪光执行方式" className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        {WORKFLOW_MODES.map(({ id, title, detail }) => {
          const active = workflow === id;
          return (
            <button
              key={id}
              type="button"
              role="radio"
              aria-checked={active}
              disabled={disabled}
              onClick={() => setTaskOptionValue(instanceId, taskId, 'CollectionWorkflow', { type: 'select', caseName: id })}
              className={clsx(
                'rounded-lg border p-2.5 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-60',
                active ? 'border-accent bg-accent/10' : 'border-border bg-bg-primary hover:bg-bg-hover',
              )}
            >
              <span className="flex items-center gap-1.5 text-xs font-semibold text-text-primary">
                {title}
                {active && <Check className="ml-auto h-3.5 w-3.5 text-accent" />}
              </span>
              <span className="mt-1 block text-[11px] leading-4 text-text-secondary">{detail}</span>
            </button>
          );
        })}
      </div>
      <p className="text-text-muted">
        {progress.known
          ? `五星闪光已收集 ${progress.owned.size} / ${progress.target} 个颜色。`
          : '尚无闪光记录。第一次运行前请清除祝福闪光仓库的筛选，会先读取或整理全部闪光。'}
      </p>
      {workflow === 'Managed' && reserveBling > 0 && (
        <p className="text-text-secondary">已设置保留噗灵：{reserveBling.toLocaleString()}（升级后噗灵不会低于该数值）。</p>
      )}
      {workflow === 'Managed' && decompose && (
        <p className="text-warning">已开启爆仓分解：仓库满时会一键分解未锁定的闪光，且不可恢复。请勿手动解锁已收藏的祝福闪光。</p>
      )}
    </div>
  );
}
