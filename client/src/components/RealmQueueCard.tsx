import { useMemo, type ReactNode } from 'react';
import { AlertTriangle, CalendarClock, Check, ChevronRight, GripVertical, Hammer, Shield, Sparkles, Shirt } from 'lucide-react';
import clsx from 'clsx';
import { toast } from 'sonner';
import { DndContext, KeyboardSensor, PointerSensor, closestCenter, useSensor, useSensors, type DragEndEvent } from '@dnd-kit/core';
import { SortableContext, arrayMove, sortableKeyboardCoordinates, useSortable, verticalListSortingStrategy } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { useAppStore } from '@/stores/appStore';
import { useAccountStateStore } from '@/stores/accountStateStore';
import { openCollectionHub, type CollectionTab } from '@/stores/collectionHubStore';
import { getBlessingProgress } from '@/utils/blessingProgress';
import {
  REALM_GOAL_LABELS, WEEKLY_TARGETS, realmCollectionWorkflow, realmQueue, realmQueueOption, realmReserveBling, weeklyTargets,
  type RealmGoal, type RealmQueueEntry,
} from '@/utils/nikkiOptions';
import type { OptionValue } from '@/types/interface';
import { SwitchTrack } from './ui/SwitchTrack';

export const REALM_QUEUE_CARD_ID = 'realm-queue-card';
const GOAL_INFO: Record<RealmGoal, { icon: typeof Shirt; detail: string; tab?: CollectionTab }> = {
  weekly: { icon: CalendarClock, detail: '本周仍有挑战次数时，依次挑战所选关卡各一次' },
  renewal: { icon: Shirt, detail: '按「换新」计划刷泡泡、丝线与噗灵', tab: 'renewal' },
  crafting: { icon: Hammer, detail: '按「制衣」计划刷魔物试炼晶石', tab: 'crafting' },
  glow: { icon: Sparkles, detail: '按「祝福闪光」计划选本追新色，所选范围收齐后跳过', tab: 'blessing' },
};
const FALLBACK_LABELS: Record<string, string> = { Bless: '祝福闪光幻境', Jihua: '素材激化幻境', Monster: '魔物试炼幻境' };

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
    window.setTimeout(() => document.getElementById(REALM_QUEUE_CARD_ID)?.scrollIntoView({ behavior: 'smooth', block: 'center' }), 300);
  };
  return { hasTask: !!task, taskEnabled: !!task?.enabled, entries, enabledGoals, rank, save, openOrderCard };
}

function QueueRow({ entry, rank, disabled, warn, onToggle, children }: {
  entry: RealmQueueEntry; rank: number | null; disabled: boolean; warn: boolean; onToggle: (enabled: boolean) => void; children?: ReactNode;
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: entry.goal, disabled });
  const info = GOAL_INFO[entry.goal];
  const Icon = info.icon;
  return <div ref={setNodeRef} style={{ transform: CSS.Transform.toString(transform), transition }}
    className={clsx('flex items-center gap-2 rounded-lg border px-2.5 py-2 text-xs',
      entry.enabled ? 'border-accent/40 bg-accent/5' : 'border-border bg-bg-secondary', isDragging && 'z-10 shadow-lg')}>
    <button type="button" aria-label={`拖动调整${REALM_GOAL_LABELS[entry.goal]}的顺序`} disabled={disabled}
      className="cursor-grab touch-none rounded p-0.5 text-text-muted hover:bg-bg-hover disabled:cursor-not-allowed" {...attributes} {...listeners}>
      <GripVertical className="h-4 w-4" />
    </button>
    <span className={clsx('flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold tabular-nums',
      rank ? 'bg-accent text-on-accent' : 'bg-bg-tertiary text-text-muted')}>{rank ?? '–'}</span>
    <Icon className={clsx('h-4 w-4 shrink-0', entry.enabled ? 'text-accent' : 'text-text-muted')} />
    <div className="min-w-0 flex-1">
      <p className="font-semibold text-text-primary">{REALM_GOAL_LABELS[entry.goal]}</p>
      <p className="truncate text-[11px] text-text-secondary">{info.detail}</p>
      {children}
      {warn && <p className="mt-0.5 flex items-center gap-1 text-[11px] text-warning"><AlertTriangle className="h-3 w-3 shrink-0" />所选范围收齐前会一直刷闪光，排在后面的目标要等收齐后才会轮到</p>}
    </div>
    {info.tab && <button type="button" onClick={() => openCollectionHub(info.tab!)} className="inline-flex shrink-0 items-center text-[11px] text-text-secondary hover:text-accent">
      查看计划<ChevronRight className="h-3 w-3" />
    </button>}
    <button type="button" role="switch" aria-checked={entry.enabled} aria-label={`开启${REALM_GOAL_LABELS[entry.goal]}`} disabled={disabled}
      onClick={() => onToggle(!entry.enabled)}
      className="inline-flex shrink-0 items-center rounded-full focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50">
      <SwitchTrack checked={entry.enabled} />
    </button>
  </div>;
}

const WORKFLOW_MODES = [
  { id: 'Managed', title: '全流程托管', detail: '自动整理、按预期新色刷本、分解、升级并锁定满级新颜色' },
  { id: 'FarmOnly', title: '只刷胚子', detail: '按已收集颜色与仓库胚子数算期望选本刷取，升级与管理交给自己' },
] as const;

/** The weekly realm levels, picked inside its queue row; at least one stays selected. */
function WeeklyTargetPicker({ selected, disabled, onChange }: { selected: string[]; disabled: boolean; onChange: (next: string[]) => void }) {
  return <div className="mt-1 flex flex-wrap items-center gap-1.5" role="group" aria-label="每周幻境目标">
    {WEEKLY_TARGETS.map(({ id, label }) => {
      const on = selected.includes(id);
      const locked = on && selected.length <= 1;
      return <button key={id} type="button" aria-pressed={on} disabled={disabled || locked}
        title={locked ? '至少保留一个目标' : undefined}
        onClick={() => onChange(on ? selected.filter((item) => item !== id) : WEEKLY_TARGETS.map((t) => t.id).filter((item) => item === id || selected.includes(item)))}
        className={clsx('inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[11px] transition-colors disabled:cursor-not-allowed',
          on ? 'border-accent/50 bg-accent/10 font-medium text-accent' : 'border-border text-text-secondary hover:border-accent hover:text-accent',
          disabled && 'opacity-60')}>
        {on && <Check className="h-3 w-3" />}{label}
      </button>;
    })}
  </div>;
}

/** 幻境挑战「刷取顺序」: ordered goals with switches, then the fixed fallback realm. */
export function RealmQueueCard({ instanceId, taskId, values, disabled }: {
  instanceId: string; taskId: string; values: Record<string, OptionValue>; disabled: boolean;
}) {
  const setTaskOptionValue = useAppStore((state) => state.setTaskOptionValue);
  const account = useAccountStateStore((state) => state.data);
  const progress = useMemo(() => getBlessingProgress(account), [account]);
  const entries = realmQueue(values);
  const enabled = entries.filter((entry) => entry.enabled).map((entry) => entry.goal);
  const glowIndex = enabled.indexOf('glow');
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }));
  const save = (next: RealmQueueEntry[]) => setTaskOptionValue(instanceId, taskId, 'RealmQueue', realmQueueOption(next));
  const onDragEnd = ({ active, over }: DragEndEvent) => {
    if (!over || active.id === over.id) return;
    const from = entries.findIndex((entry) => entry.goal === active.id);
    const to = entries.findIndex((entry) => entry.goal === over.id);
    if (from >= 0 && to >= 0) save(arrayMove(entries, from, to));
  };
  const fallback = values.RealmTypes?.type === 'select' ? values.RealmTypes.caseName : 'Bless';
  const workflow = realmCollectionWorkflow(values);
  const reserveBling = realmReserveBling(values);
  const decompose = values.CollectionDecompose?.type === 'switch' && values.CollectionDecompose.value;

  return <div id={REALM_QUEUE_CARD_ID} className="mb-4 space-y-2 rounded-lg border border-border bg-bg-primary p-3">
    <div className="flex flex-wrap items-baseline justify-between gap-2">
      <p className="text-sm font-semibold">刷取顺序</p>
      <p className="text-[11px] text-text-secondary">每批挑战前选排在最前、仍有缺口的目标；拖动调整先后</p>
    </div>
    <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={onDragEnd}>
      <SortableContext items={entries.map((entry) => entry.goal)} strategy={verticalListSortingStrategy}>
        <div className="space-y-1.5">
          {entries.map((entry) => {
            const index = enabled.indexOf(entry.goal);
            return <QueueRow key={entry.goal} entry={entry} rank={index < 0 ? null : index + 1} disabled={disabled}
              warn={entry.goal === 'glow' && glowIndex >= 0 && glowIndex < enabled.length - 1}
              onToggle={(on) => save(entries.map((item) => item.goal === entry.goal ? { ...item, enabled: on } : item))}>
              {entry.goal === 'weekly' && entry.enabled && <WeeklyTargetPicker selected={weeklyTargets(values)} disabled={disabled}
                onChange={(next) => setTaskOptionValue(instanceId, taskId, 'WeeklyTargets', { type: 'checkbox', caseNames: next })} />}
            </QueueRow>;
          })}
        </div>
      </SortableContext>
    </DndContext>
    <div className="flex items-center gap-2 rounded-lg border border-dashed border-border px-2.5 py-2 text-xs">
      <Shield className="ml-6 h-4 w-4 shrink-0 text-text-muted" />
      <div className="min-w-0 flex-1">
        <p className="font-semibold text-text-primary">兜底 · {FALLBACK_LABELS[fallback] ?? fallback}</p>
        <p className="text-[11px] text-text-secondary">上面的目标都完成或未开启时，剩余活跃能量按下方「兜底副本」设置使用</p>
      </div>
    </div>
    {glowIndex >= 0 && <div className="space-y-2 rounded-lg border border-border bg-bg-secondary/60 px-3 py-2.5 text-xs">
      <div role="radiogroup" aria-label="五星闪光执行方式" className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        {WORKFLOW_MODES.map(({ id, title, detail }) => {
          const active = workflow === id;
          return <button key={id} type="button" role="radio" aria-checked={active} disabled={disabled}
            onClick={() => setTaskOptionValue(instanceId, taskId, 'CollectionWorkflow', { type: 'select', caseName: id })}
            className={clsx('rounded-lg border p-2.5 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-60',
              active ? 'border-accent bg-accent/10' : 'border-border bg-bg-primary hover:bg-bg-hover')}>
            <span className="flex items-center gap-1.5 text-xs font-semibold text-text-primary">{title}{active && <Check className="ml-auto h-3.5 w-3.5 text-accent" />}</span>
            <span className="mt-1 block text-[11px] leading-4 text-text-secondary">{detail}</span>
          </button>;
        })}
      </div>
      <p className="text-text-muted">{progress.known
        ? `五星闪光已收集 ${progress.owned.size} / ${progress.target} 个颜色。`
        : '尚无闪光记录。第一次运行前请清除祝福闪光仓库的筛选，会先读取或整理全部闪光。'}</p>
      {workflow === 'Managed' && reserveBling > 0 && <p className="text-text-secondary">已设置保留噗灵：{reserveBling.toLocaleString()}（升级后噗灵不会低于该数值）。</p>}
      {workflow === 'Managed' && decompose && <p className="text-warning">已开启爆仓分解：仓库满时会一键分解未锁定的闪光，且不可恢复。请勿手动解锁已收藏的祝福闪光。</p>}
    </div>}
  </div>;
}
