import { ArrowUpRight, Check, Flame, Star } from 'lucide-react';
import clsx from 'clsx';
import { toast } from 'sonner';
import { useAppStore } from '@/stores/appStore';
import { MATERIAL_REALM_TARGETS } from '@/services/scoring/heartPresets';
import type { BatchCalculationResult, ScoringCatalog } from '@/services/scoring/types';

const card = 'rounded-xl border border-border bg-bg-secondary p-4';
const number = (value: number) => value.toLocaleString('zh-CN', { maximumFractionDigits: 2 });
const levelLabel = (level: number | null) => level === null ? '等级未知' : level === 11 ? '已焕新' : `${level} 级`;

export function WardrobeMaterialList({ cost, catalog }: {
  cost: Record<string, number>;
  catalog: ScoringCatalog;
}) {
  const instances = useAppStore((state) => state.instances);
  const activeInstanceId = useAppStore((state) => state.activeInstanceId);
  const setTaskOptionValue = useAppStore((state) => state.setTaskOptionValue);
  const entries = Object.entries(cost).filter(([, count]) => count > 0);

  const activeInstance = instances.find((inst) => inst.id === activeInstanceId) ?? instances[0];
  const realmTask = activeInstance?.selectedTasks.find((task) => task.taskName === 'realm_challenge');
  const realmValues = realmTask?.optionValues ?? {};
  const isCollectionMode = realmValues.CollectionMode?.type === 'switch' && realmValues.CollectionMode.value;
  const activeRealmType = isCollectionMode ? '' : realmValues.RealmTypes?.type === 'select' ? realmValues.RealmTypes.caseName : 'Bless';

  function applyRealmTarget(materialId: string) {
    const target = MATERIAL_REALM_TARGETS[materialId];
    if (!target) return;
    if (!activeInstance || !realmTask) {
      toast.error('当前配置页签中尚未添加「幻境挑战」任务，请先在主界面添加。');
      return;
    }
    setTaskOptionValue(activeInstance.id, realmTask.id, 'CollectionMode', { type: 'switch', value: false });
    setTaskOptionValue(activeInstance.id, realmTask.id, 'RealmTypes', { type: 'select', caseName: target.realmType });
    setTaskOptionValue(activeInstance.id, realmTask.id, target.optionKey, { type: 'select', caseName: target.caseName });
    toast.success(`已将当前配置的幻境挑战设为「${target.label}」`);
  }

  if (!entries.length) {
    return <p className="mt-2 text-xs text-text-secondary">已知等级的部件暂无材料差额</p>;
  }

  return (
    <div className="mt-2.5 grid grid-cols-1 gap-2 sm:grid-cols-2">
      {entries.map(([id, count]) => {
        const name = catalog.material_names[id] ?? id;
        const target = MATERIAL_REALM_TARGETS[id];
        const currentCase = target && realmValues[target.optionKey]?.type === 'select'
          ? (realmValues[target.optionKey] as { type: 'select'; caseName: string }).caseName
          : undefined;
        const isCurrentTarget = !!target && activeRealmType === target.realmType && currentCase === target.caseName;

        return (
          <div key={id} className="flex items-center justify-between gap-2 rounded-lg border border-border bg-bg-primary px-3 py-2 text-xs">
            <div className="min-w-0">
              <span className="font-medium text-text-primary">{name}</span>
              <strong className="ml-1.5 tabular-nums text-accent">{number(count)}</strong>
              {target && <p className="mt-0.5 truncate text-[11px] text-text-muted">{target.label}</p>}
            </div>
            {target && realmTask && (
              <button type="button" onClick={() => applyRealmTarget(id)}
                className={clsx(' inline-flex shrink-0 items-center gap-1 rounded-lg px-2 py-1 text-[11px] transition-colors',
                  isCurrentTarget
                    ? 'bg-accent/15 font-medium text-accent'
                    : 'border border-border bg-bg-secondary text-text-secondary hover:border-accent hover:text-accent')}
                title={`将幻境挑战设为：${target.label}`}>
                {isCurrentTarget ? <Check className="h-3 w-3" /> : <Flame className="h-3 w-3" />}
                {isCurrentTarget ? '当前目标' : '设为幻境目标'}
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}

export function WardrobeBatchPlanView({ batch, catalog, activeMatchId, onSelectStage }: {
  batch: BatchCalculationResult;
  catalog: ScoringCatalog;
  activeMatchId: string;
  onSelectStage: (matchId: string) => void;
}) {
  return (
    <section className={clsx(card, 'space-y-4 border-accent/40')}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold">整期多场次合并规划（共 {batch.stages.length} 场）</h3>
          <p className="mt-0.5 text-xs text-text-secondary">跨场次复用的衣服已按所需最高等级自动去重，点击下方场次可切换查看单场搭配细节。</p>
        </div>
        <span className="text-xs tabular-nums text-text-muted">合并用时 {(batch.elapsedMs / 1000).toFixed(1)} 秒</span>
      </div>

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {batch.stages.map((stage) => {
          const outfit = stage.potential ?? stage.current;
          const active = stage.matchId === activeMatchId;
          return (
            <button key={stage.matchId} type="button" onClick={() => onSelectStage(stage.matchId)}
              className={clsx('rounded-lg border p-2.5 text-left transition-colors',
                active ? 'border-accent bg-accent/10' : 'border-border bg-bg-primary hover:bg-bg-hover')}>
              <p className="truncate text-xs font-medium text-text-primary">{stage.matchName}</p>
              <p className="mt-1 text-sm font-semibold tabular-nums text-accent">{outfit ? number(outfit.score) : '—'}</p>
              {outfit && !!outfit.stars.length && (
                <div className="mt-1 flex gap-1">
                  {outfit.stars.map((passed, i) => (
                    <Star key={i} className={clsx('h-3 w-3', passed === true ? 'fill-accent text-accent' : 'text-text-muted/40')} />
                  ))}
                </div>
              )}
            </button>
          );
        })}
      </div>

      <div>
        <h4 className="text-xs font-semibold text-text-secondary">跨场次优先升哪件（按登场场次数与累计提分排序）</h4>
        {batch.upgrades.length ? (
          <ol className="mt-2 space-y-1.5 text-xs">
            {batch.upgrades.slice(0, 10).map((up) => (
              <li key={up.groupId} className="flex flex-wrap items-center gap-2 rounded-lg bg-bg-primary px-3 py-2">
                <ArrowUpRight className="h-3.5 w-3.5 shrink-0 text-accent" />
                <span className="min-w-0 flex-1 truncate font-medium">
                  {up.item.name}
                  <span className="ml-2 font-normal text-text-muted">{levelLabel(up.from)} → {levelLabel(up.to)}</span>
                </span>
                <span className="rounded bg-accent/10 px-1.5 py-0.5 text-[11px] text-accent">
                  用于 {up.stages.length} 场（{up.stages.join('、')}）
                </span>
                <strong className="w-20 text-right tabular-nums text-accent">+{number(up.totalGain)}</strong>
              </li>
            ))}
          </ol>
        ) : (
          <p className="mt-2 text-xs text-text-secondary">整期所有场次用到的已知等级部件均已满级。</p>
        )}
      </div>

      <div>
        <h4 className="text-xs font-semibold text-text-secondary">整期去重总材料差额</h4>
        <WardrobeMaterialList cost={batch.cost} catalog={catalog} />
        {!!batch.unknownCostGroups.length && (
          <p className="mt-2 text-xs text-text-secondary">另有 {batch.unknownCostGroups.length} 组登场部件等级未知，在下方补充等级后即可计入整期总材料差额。</p>
        )}
      </div>
    </section>
  );
}
