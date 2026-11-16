import { useMemo, useState, type ReactNode } from 'react';
import {
  AlertTriangle,
  CalendarClock,
  Check,
  ChevronDown,
  ChevronRight,
  GripVertical,
  Hammer,
  Info,
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
import { collectionKey, collectionSlots, getBlessingProgress } from '@/utils/blessingProgress';
import { RENEWAL_SCOPE_OPTIONS } from '@/services/scoring/dominance';
import { useWardrobePlanStore } from '@/services/scoring/wardrobePlanStore';
import { getInterfaceLangKey } from '@/i18n';
import {
  DEFAULT_REALM_OPTION_KEYS,
  REALM_GOAL_LABELS,
  WEEKLY_TARGETS,
  collectionDrawerOptionKeys,
  doubleEnabled,
  doubleJihuaMode,
  doubleTypes,
  monsterTargetMode,
  realmCollectionWorkflow,
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
  weekly: { icon: CalendarClock, detail: '本周有挑战次数时，按顺序各挑战所选关卡一次' },
  renewal: { icon: Shirt, detail: '按「焕新」计划优先补泡泡与纯真丝线，最后补噗灵', tab: 'renewal' },
  crafting: { icon: Hammer, detail: '按「制衣」计划刷魔物试炼晶石', tab: 'crafting' },
  glow: { icon: Sparkles, detail: '按「祝福闪光」缺色选本挑战，所选范围收齐后跳过', tab: 'blessing' },
};
const FALLBACK_LABELS: Record<string, string> = {
  Bless: '祝福闪光',
  Jihua: '素材激化',
  Monster: '魔物试炼',
};
const FALLBACK_TARGET_KEY: Record<string, string> = {
  Bless: 'BlessTarget',
  Jihua: 'JihuaTarget',
  Monster: 'MonsterTarget',
};
const DRAWER_BTN_CLASS =
  'inline-flex shrink-0 items-center gap-0.5 rounded-md border border-border bg-bg-primary px-1.5 py-0.5 text-[11px] text-text-secondary transition-colors hover:border-accent hover:text-accent';

function RightEstimate({ estimate, computing }: { estimate: QueueGoalEstimate; computing: boolean }) {
  if (estimate.status === 'needs_data') {
    return (
      <div className="flex w-[124px] shrink-0 flex-col justify-center text-right tabular-nums sm:w-[144px]">
        <span className="truncate text-[11px] font-medium text-text-muted">{estimate.note ?? '待同步记录'}</span>
        <span className="truncate text-[10px] text-text-muted">
          {estimate.goal === 'weekly' ? '待读取本周次数' : '可点右侧「计划」同步'}
        </span>
      </div>
    );
  }
  if (estimate.status === 'computing') {
    return (
      <div className="flex w-[124px] shrink-0 flex-col justify-center text-right tabular-nums sm:w-[144px]">
        <span className="inline-flex items-center justify-end gap-1 text-[11px] font-medium text-text-muted">
          <Loader2 className="h-3 w-3 animate-spin" />
          正在估算…
        </span>
        <span className="truncate text-[10px] text-text-muted">计算期望体力与天数</span>
      </div>
    );
  }
  if (estimate.status === 'stalled') {
    return (
      <div className="flex w-[124px] shrink-0 flex-col justify-center text-right tabular-nums sm:w-[144px]">
        <span className="truncate text-[11px] font-medium text-warning">{estimate.note ?? '仓库已满需处理'}</span>
        <span className="truncate text-[10px] text-text-muted">请清理或开启分解</span>
      </div>
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
    const mainLabel =
      estimate.goal === 'weekly'
        ? '本周已完成 · 0 体力'
        : '已备齐 · 0 体力 · 0 天';
    const subLabel =
      estimate.goal === 'weekly'
        ? `每周固定 ${estimate.weeklyPerWeek ?? 0} 体力`
        : estimate.note || (estimate.enabled ? '已完成当前目标' : '当前未开启');
    return (
      <div className="flex w-[124px] shrink-0 flex-col justify-center text-right tabular-nums sm:w-[144px]" title={tooltip}>
        <span className={clsx('truncate text-[11px] font-medium', estimate.enabled ? 'text-success' : 'text-text-muted')}>
          {mainLabel}
        </span>
        <span className="truncate text-[10px] text-text-muted">
          {!estimate.enabled && estimate.goal === 'weekly' ? `未开启 · ${subLabel}` : subLabel}
        </span>
      </div>
    );
  }

  const daysText = days === null ? '10 年以上' : `约 ${days} 天`;
  const mainText = `${compactNumber(energy)} 体力 · ${daysText}`;
  const subParts: string[] = [];
  if (estimate.goal === 'weekly' && estimate.weeklyPerWeek != null) {
    if (!estimate.enabled) subParts.push('未开启');
    subParts.push(`每周固定 ${estimate.weeklyPerWeek} 体力`);
  } else {
    if (!estimate.enabled) subParts.push('未开启');
    else if (hasQueuedDelay) subParts.push(`顺位第 ${queuedDay} 天完`);
    else if (estimate.enabled && estimate.goal === 'glow' && queuedDay === null && days !== null) subParts.push('需等前置完');
    if (estimate.note) subParts.push(estimate.note);
  }
  const subText = subParts.join(' · ') || '按当前收集计划估算';

  return (
    <div className="flex w-[124px] shrink-0 flex-col justify-center text-right tabular-nums sm:w-[144px]" title={tooltip}>
      <span
        className={clsx(
          'inline-flex items-center justify-end gap-1 truncate text-[11px] font-medium',
          estimate.enabled ? 'text-accent' : 'text-text-muted',
        )}
      >
        <span className="truncate">{mainText}</span>
        {computing && estimate.goal === 'glow' && <Loader2 className="h-3 w-3 shrink-0 animate-spin opacity-70" />}
      </span>
      <span className="truncate text-[10px] text-text-muted">{subText}</span>
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
  expanded,
  expandLabel,
  onToggleExpand,
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
  expanded?: boolean;
  expandLabel?: string;
  onToggleExpand?: () => void;
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
        'rounded-lg border text-xs',
        entry.enabled ? 'border-accent/40 bg-accent/5' : 'border-border bg-bg-secondary',
        isDragging && 'z-10 shadow-lg',
      )}
    >
      <div className="flex min-h-[54px] items-center gap-2 px-2.5 py-2">
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
          <div className="flex items-center gap-1.5 min-w-0">
            <p className="truncate font-semibold text-text-primary">{REALM_GOAL_LABELS[entry.goal]}</p>
            {warn && (
              <span
                title="所选范围收齐前会一直刷闪光，排在后面的目标要等收齐后才会轮到"
                className="inline-flex shrink-0 items-center gap-0.5 rounded bg-warning/15 px-1.5 py-0.5 text-[10px] font-medium text-warning"
              >
                <AlertTriangle className="h-2.5 w-2.5 shrink-0" />
                延迟后续
              </span>
            )}
          </div>
          <p className="truncate text-[11px] leading-4 text-text-secondary">{detailOverride ?? info.detail}</p>
        </div>
        <RightEstimate estimate={estimate} computing={computing} />
        <div className="flex shrink-0 items-center gap-1.5">
          {info.tab && (
            <button
              type="button"
              onClick={() => openCollectionHub(info.tab!)}
              className="inline-flex shrink-0 items-center text-[11px] text-text-secondary hover:text-accent"
            >
              计划
              <ChevronRight className="h-3 w-3" />
            </button>
          )}
          {expandLabel && onToggleExpand && (
            <button
              type="button"
              aria-expanded={expanded}
              onClick={onToggleExpand}
              className={DRAWER_BTN_CLASS}
            >
              {expandLabel}
              <ChevronDown className={clsx('h-3 w-3 transition-transform', expanded && 'rotate-180')} />
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
      </div>
      {expanded && children && (
        <div className="border-t border-border/70 bg-bg-primary/70 px-3 py-2.5">
          {children}
        </div>
      )}
    </div>
  );
}

const WORKFLOW_MODES = [
  { id: 'Managed', title: '全流程托管', detail: '自动整理、按预期新色刷本、分解、升级并锁定满级新颜色' },
  { id: 'FarmOnly', title: '只刷胚子', detail: '按已收集颜色与仓库胚子数算期望选本刷取，升级与管理交给自己' },
] as const;

/** The weekly realm levels, picked inside its queue row drawer; at least one stays selected. */
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
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="每周幻境目标">
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
                'inline-flex items-center gap-1 rounded-md border px-2 py-1 text-[11px] transition-colors disabled:cursor-not-allowed',
                on
                  ? 'border-accent/50 bg-accent/10 font-medium text-accent'
                  : 'border-border bg-bg-primary text-text-secondary hover:border-accent hover:text-accent',
                disabled && 'opacity-60',
              )}
            >
              {on && <Check className="h-3 w-3" />}
              {label}
            </button>
          );
        })}
      </div>
      <p className="text-[11px] text-text-muted">本周仍有挑战次数时按顺序各挑战一次，至少保留一个目标。</p>
    </div>
  );
}
/** 幻境挑战「刷取顺序」: uniform compact rows with right-aligned estimates and inline expandable drawers. */
export function RealmQueueCard({
  instanceId,
  taskId,
  values,
  disabled,
  renderOptions,
}: {
  instanceId: string;
  taskId: string;
  values: Record<string, OptionValue>;
  disabled: boolean;
  renderOptions?: (optionKeys: string[], subheadings?: Record<string, string>) => ReactNode;
}) {
  const setTaskOptionValue = useAppStore((state) => state.setTaskOptionValue);
  const projectInterface = useAppStore((state) => state.projectInterface);
  const language = useAppStore((state) => state.language);
  const resolveI18nText = useAppStore((state) => state.resolveI18nText);
  const langKey = getInterfaceLangKey(language);
  const { dailyEnergy, setDailyEnergy } = useForecastAssumptionsStore();
  const [showTrend, setShowTrend] = useState(false);
  const [openDrawers, setOpenDrawers] = useState<{ weekly: boolean; glow: boolean; fallback: boolean }>({
    weekly: false,
    glow: false,
    fallback: false,
  });
  const toggleDrawer = (key: keyof typeof openDrawers) =>
    setOpenDrawers((prev) => ({ ...prev, [key]: !prev[key] }));

  const entries = realmQueue(values);
  const enabled = entries.filter((entry) => entry.enabled).map((entry) => entry.goal);
  const selectedWeekly = weeklyTargets(values);
  const weeklyLabels = WEEKLY_TARGETS.filter((item) => selectedWeekly.includes(item.id)).map((item) => item.label);
  const weeklyDetail = weeklyLabels.length
    ? `本周挑战：${weeklyLabels.join('、')}（有次数各挑战一次）`
    : GOAL_INFO.weekly.detail;
  const glowDetail =
    realmCollectionWorkflow(values) === 'FarmOnly'
      ? '只刷胚子 · 按已收集颜色与仓库胚子期望选本，收齐后跳过'
      : '全流程托管 · 自动整理、选本、升级并锁定满级新颜色';

  const dblEnabled = doubleEnabled(values);
  const dblTypes = dblEnabled ? doubleTypes(values) : [];
  const dblTypeLabels: Record<string, string> = {
    Jihua: '超级激化',
    Bless: '灵感迸发',
    Monster: '魔物试炼',
    Weekly: '深度突破',
  };
  const missingPlanKinds = [
    dblTypes.includes('Jihua') && doubleJihuaMode(values) === 'FollowDeficit' ? '超级激化' : null,
    dblTypes.includes('Monster') && monsterTargetMode(values) === 'FollowDeficit' ? '魔物试炼' : null,
  ].filter(Boolean) as string[];
  const dblPlanEmpty =
    dblEnabled
    && missingPlanKinds.length > 0
    && !enabled.includes('renewal')
    && !enabled.includes('crafting');

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
  const targetKey = FALLBACK_TARGET_KEY[fallback];
  const targetOptionDef = targetKey ? projectInterface?.option?.[targetKey] : undefined;
  const targetSelectDef =
    targetOptionDef && (targetOptionDef.type === 'select' || targetOptionDef.type === undefined)
      ? targetOptionDef
      : undefined;
  const targetCaseName = targetKey
    ? (values[targetKey]?.type === 'select' ? values[targetKey].caseName : targetSelectDef?.default_case)
    : undefined;
  const targetCase = targetSelectDef?.cases?.find((c) => c.name === targetCaseName) ?? targetSelectDef?.cases?.[0];
  const targetShort = (targetCase ? resolveI18nText(targetCase.label, langKey) || targetCase.name : '')
    .split('|', 1)[0]
    .trim();
  const fallbackName = FALLBACK_LABELS[fallback] ?? fallback;
  const fallbackTitle = targetShort ? `默认副本 · ${fallbackName}（${targetShort}）` : `默认副本 · ${fallbackName}`;

  const { renewal, crafting, queueSummary, glowComputing } = useRealmForecast({ optionValues: values });
  const modeLabelOf = (m: string) => (m === 'power' ? '战力' : m === 'selected' ? '自选' : '全收集');
  const renewalScopeLabel =
    RENEWAL_SCOPE_OPTIONS.find((item) => item.id === (renewal.renewalScope ?? 'all'))?.label ?? '全部星级';
  const renewalDetail =
    renewal.status === 'needs_wardrobe'
      ? undefined
      : [
          renewal.source === 'targets'
            ? '手动材料目标'
            : renewal.mode === 'selected'
              ? '自选'
              : `${modeLabelOf(renewal.mode)}·${renewalScopeLabel}`,
          (renewal.priorityPendingCount ?? 0) > 0 ? `搭配优先 ${renewal.priorityPendingCount} 件` : null,
          renewal.nextTarget
            ? `下轮：${renewal.nextTarget.name}（${renewal.nextTarget.sourceLabel}）`
            : '所需副本材料已齐',
        ]
          .filter(Boolean)
          .join('｜');
  const craftingDetail =
    crafting.status === 'needs_wardrobe'
      ? undefined
      : [
          crafting.mode === 'selected'
            ? '自选'
            : `${modeLabelOf(crafting.mode)}·${crafting.scope === 'suits' ? '仅成套' : '全部设计图'}`,
          crafting.nextTarget
            ? `下轮：${crafting.nextTarget.name}（${crafting.nextTarget.sourceLabel}）`
            : crafting.energy === 0 && crafting.allTrackedEnergy > 0
              ? '试炼晶石已齐（丝线/噗灵自备）'
              : '试炼晶石已齐',
        ]
          .filter(Boolean)
          .join('｜');
  const { totalFiniteEnergy, totalFiniteDays, hasUnknownEnabled, hasInfiniteOrStalled } = queueSummary;

  const summaryText =
    enabled.length === 0
      ? '未开启队列目标，全部体力直接投入默认副本'
      : hasUnknownEnabled
        ? '部分已开启目标尚无记录，可点右侧「计划」同步后自动计算'
        : hasInfiniteOrStalled
          ? '存在无法继续或预计超过 10 年的目标，请检查仓库分解设置'
          : totalFiniteEnergy === 0
            ? '当前已开启目标均已就绪，剩余体力直接投入默认副本'
            : `已开启目标合计约 ${compactNumber(totalFiniteEnergy ?? 0)} 体力 · 预计 ${totalFiniteDays ?? 0} 天后投入默认副本`;

  const fallbackPrimaryEstimate =
    enabled.length === 0
      ? '当前直接投入'
      : hasUnknownEnabled
        ? '队列完成后接管'
        : totalFiniteDays !== null && totalFiniteDays > 0
          ? `约第 ${totalFiniteDays} 天后接管`
          : totalFiniteEnergy === 0
            ? '当前即可接管'
            : '队列完成后接管';
  return (
    <div id={REALM_QUEUE_CARD_ID} className="mb-3 space-y-2 rounded-lg border border-border bg-bg-primary p-3">
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
              已启用：{dblTypes.map((item) => dblTypeLabels[item] ?? item).join('、')}（智能选择已备齐或已收齐时自动跳过）
            </span>
          </p>
          {dblPlanEmpty && (
            <p className="flex items-center gap-1 text-text-secondary">
              <Info className="h-3 w-3 shrink-0 text-accent" />
              {missingPlanKinds.join('与')}策略当前设为「智能选择（根据收集计划）」，因「焕新材料」与「制衣材料」均未开启，将自动回退为「默认副本」中的对应默认设置。
            </p>
          )}
        </div>
      )}
      <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={onDragEnd}>
        <SortableContext items={entries.map((entry) => entry.goal)} strategy={verticalListSortingStrategy}>
          <div className="space-y-1.5">
            {entries.map((entry) => {
              const index = enabled.indexOf(entry.goal);
              const drawerGoal = entry.goal === 'weekly' || entry.goal === 'glow' ? entry.goal : null;
              return (
                <QueueRow
                  key={entry.goal}
                  entry={entry}
                  rank={index < 0 ? null : index + 1}
                  disabled={disabled}
                  estimate={queueSummary.goals[entry.goal]}
                  computing={glowComputing}
                  detailOverride={
                    entry.goal === 'weekly'
                      ? weeklyDetail
                      : entry.goal === 'glow'
                        ? glowDetail
                        : entry.goal === 'renewal'
                          ? renewalDetail
                          : entry.goal === 'crafting'
                            ? craftingDetail
                            : undefined
                  }
                  warn={entry.goal === 'glow' && glowIndex >= 0 && glowIndex < enabled.length - 1}
                  expanded={drawerGoal ? openDrawers[drawerGoal] : false}
                  expandLabel={entry.goal === 'weekly' ? '关卡' : entry.goal === 'glow' ? '设置' : undefined}
                  onToggleExpand={drawerGoal ? () => toggleDrawer(drawerGoal) : undefined}
                  onToggle={(on) => save(entries.map((item) => (item.goal === entry.goal ? { ...item, enabled: on } : item)))}
                >
                  {entry.goal === 'weekly' && (
                    <WeeklyTargetPicker
                      selected={selectedWeekly}
                      disabled={disabled}
                      onChange={(next) => setTaskOptionValue(instanceId, taskId, 'WeeklyTargets', { type: 'checkbox', caseNames: next })}
                    />
                  )}
                  {entry.goal === 'glow' && (
                    <div className="space-y-3">
                      <RealmCollectionWorkflow instanceId={instanceId} taskId={taskId} values={values} disabled={disabled} />
                      {renderOptions?.(collectionDrawerOptionKeys(values))}
                    </div>
                  )}
                </QueueRow>
              );
            })}
          </div>
        </SortableContext>
      </DndContext>

      <div className="rounded-lg border border-dashed border-border bg-bg-secondary/40 text-xs">
        <div className="flex min-h-[54px] items-center gap-2 px-2.5 py-2">
          <span className="w-5 shrink-0" aria-hidden="true" />
          <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-bg-tertiary text-[10px] font-semibold text-text-muted">
            默
          </span>
          <Shield className="h-4 w-4 shrink-0 text-text-muted" />
          <div className="min-w-0 flex-1">
            <p className="truncate font-semibold text-text-primary">{fallbackTitle}</p>
            <p className="truncate text-[11px] leading-4 text-text-secondary">
              刷取顺序完成或未开启时使用；含各幻境默认关卡与激化耗材设置
            </p>
          </div>
          <div className="flex w-[124px] shrink-0 flex-col justify-center text-right tabular-nums sm:w-[144px]">
            <span className="truncate text-[11px] font-medium text-text-secondary">{fallbackPrimaryEstimate}</span>
            <span className="truncate text-[10px] text-text-muted">每日基准 {dailyEnergy} 体力</span>
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            <button
              type="button"
              aria-expanded={openDrawers.fallback}
              onClick={() => toggleDrawer('fallback')}
              className={DRAWER_BTN_CLASS}
            >
              设置
              <ChevronDown className={clsx('h-3 w-3 transition-transform', openDrawers.fallback && 'rotate-180')} />
            </button>
            <span className="inline-block w-9 shrink-0" aria-hidden="true" />
          </div>
        </div>
        {openDrawers.fallback && (
          <div className="space-y-3 border-t border-border/70 bg-bg-primary/70 px-3 py-2.5">
            <p className="text-[11px] leading-5 text-text-secondary">
              「刷取顺序」中的目标都完成或未开启后，剩余活跃能量投入所选默认副本；各幻境默认目标与素材激化消耗材料均在此统一设置，方便双倍与朝夕心愿共用。
            </p>
            {renderOptions?.([...DEFAULT_REALM_OPTION_KEYS], {
              JihuaTarget: '各幻境默认关卡（默认副本、双倍与朝夕共用）',
              JihuaMaterialMode: '素材激化 · 规划与消耗材料',
            })}
          </div>
        )}
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
  const materials = useWardrobePlanStore((state) => state.materials);
  const progress = useMemo(() => getBlessingProgress(account), [account]);
  const isPowerBlessing = materials.blessing_plan?.mode === 'power';
  const kindDone = useMemo(
    () =>
      progress.sets.reduce(
        (sum, set) =>
          sum +
          collectionSlots.filter((slot) =>
            set.colors.some((color) => progress.owned.has(collectionKey({ name: set.name, slot, color }))),
          ).length,
        0,
      ),
    [progress],
  );
  const kindTotal = progress.sets.length * collectionSlots.length;
  const workflow = realmCollectionWorkflow(values);
  const reserveBling = realmReserveBling(values);
  const decompose = values.CollectionDecompose?.type === 'switch' && values.CollectionDecompose.value;

  return (
    <div className="space-y-2 border-b border-border pb-3 text-xs">
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
          ? isPowerBlessing
            ? `五星闪光已收集 ${kindDone} / ${kindTotal} 个种类（战力模式）。`
            : `五星闪光已收集 ${progress.owned.size} / ${progress.target} 个颜色。`
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
