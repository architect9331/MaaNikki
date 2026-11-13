import { ArrowUpRight, ListPlus, Star } from 'lucide-react';
import clsx from 'clsx';
import { toast } from 'sonner';
import { useAppStore } from '@/stores/appStore';
import { saveMaterialPlan } from '@/services/scoring/api';
import { useWardrobePlanStore } from '@/services/scoring/wardrobePlanStore';
import { realmGoalRank } from '@/utils/nikkiOptions';
import { MATERIAL_REALM_TARGETS } from '@/services/scoring/heartPresets';
import type { BatchCalculationResult, MaterialsState, ScoringCatalog } from '@/services/scoring/types';

const card = 'rounded-xl border border-border bg-bg-secondary p-4';
const number = (value: number) => value.toLocaleString('zh-CN', { maximumFractionDigits: 2 });
const levelLabel = (level: number | null) => level === null ? '0 级' : level === 11 ? '已焕新' : `${level} 级`;

export function WardrobeMaterialList({ cost, catalog, accountKey, materials, sourceLabel, onMaterialsChange }: {
  cost: Record<string, number>;
  catalog: ScoringCatalog;
  accountKey?: string;
  materials?: MaterialsState | null;
  sourceLabel?: string;
  onMaterialsChange?: (next: MaterialsState) => void;
}) {
  const instances = useAppStore((state) => state.instances);
  const activeInstanceId = useAppStore((state) => state.activeInstanceId);
  const entries = Object.entries(cost).filter(([, count]) => count > 0);

  // Farming itself (order, switches, fallback realm) is set only in the realm challenge task.
  const activeInstance = instances.find((inst) => inst.id === activeInstanceId) ?? instances[0];
  const realmTask = activeInstance?.selectedTasks.find((task) => task.taskName === 'realm_challenge');

  async function applyGlowPrepPlan(mode: 'replace' | 'add') {
    if (!accountKey || !entries.length) return;
    // Prefer the shared copy so plans edited in the crafting/renewal tabs are kept.
    const shared = useWardrobePlanStore.getState();
    const base = shared.payload?.account_key === accountKey ? shared.materials : materials;
    const inv = base?.inventory ?? {};
    const prevTargets = mode === 'add' ? { ...(base?.glow_plan?.targets ?? {}) } : {};
    const targets: Record<string, number> = { ...prevTargets };
    for (const [id, deficit] of entries) {
      if (MATERIAL_REALM_TARGETS[id]) {
        const base = mode === 'add' ? Math.max(Number(inv[id]) || 0, Number(targets[id]) || 0) : (Number(inv[id]) || 0);
        targets[id] = Math.round(base + deficit);
      }
    }
    const nextMaterials: MaterialsState = {
      ...(base ?? {}),
      // Imported targets replace the wardrobe renewal plan as the 焕新 goal's source.
      renewal_plan: { enabled: false, mode: 'all', targets: {}, ...(base?.renewal_plan ?? {}), source: 'targets' },
      glow_plan: {
        enabled: true,
        completed: false,
        source_label: mode === 'add' ? `${sourceLabel || '搭配计算'}（已累加）` : (sourceLabel || '搭配计算目标'),
        targets,
        updated_at: new Date().toISOString(),
      },
    };
    try {
      await saveMaterialPlan(accountKey, nextMaterials);
      onMaterialsChange?.(nextMaterials);
      if (shared.payload?.account_key === accountKey) shared.adoptMaterials(nextMaterials);
      const saved = mode === 'add' ? '已累加到焕新材料目标' : '已设为焕新材料目标';
      toast.success(realmTask && realmGoalRank(realmTask.optionValues, 'renewal') !== null
        ? saved
        : `${saved}；在幻境挑战的刷取顺序中打开「焕新材料」后会自动刷取`);
    } catch (err) {
      toast.error(String(err));
    }
  }

  if (!entries.length) {
    return <p className="mt-2 text-xs text-text-secondary">当前搭配暂无养成材料差额</p>;
  }

  return (
    <div className="mt-2.5 space-y-2.5">
      {accountKey && (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-accent/30 bg-accent/5 px-3 py-2 text-xs">
          <span className="text-text-secondary">设为「焕新材料」备料目标（可在收集进度 → 焕新中调整）：</span>
          <div className="flex flex-wrap gap-1.5">
            <button type="button" onClick={() => void applyGlowPrepPlan('replace')}
              className="inline-flex shrink-0 items-center gap-1 rounded-lg bg-accent px-2.5 py-1 text-xs font-medium text-on-accent hover:bg-accent-hover">
              <ListPlus className="h-3.5 w-3.5" />设为筹备目标
            </button>
            <button type="button" onClick={() => void applyGlowPrepPlan('add')}
              className="inline-flex shrink-0 items-center gap-1 rounded-lg border border-accent/40 bg-bg-secondary px-2.5 py-1 text-xs font-medium text-accent hover:bg-accent/10">
              + 累加到现有目标
            </button>
          </div>
        </div>
      )}
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
      {entries.map(([id, count]) => {
        const name = catalog.material_names[id] ?? id;
        const target = MATERIAL_REALM_TARGETS[id];

        return (
          <div key={id} className="flex items-center justify-between gap-2 rounded-lg border border-border bg-bg-primary px-3 py-2 text-xs">
            <div className="min-w-0">
              <span className="font-medium text-text-primary">{name}</span>
              <strong className="ml-1.5 tabular-nums text-accent">{number(count)}</strong>
              {target && <p className="mt-0.5 truncate text-[11px] text-text-muted">{target.label}</p>}
            </div>
          </div>
        );
      })}
      </div>
    </div>
  );
}

export function WardrobeBatchPlanView({ batch, catalog, activeMatchId, onSelectStage, accountKey, materials, sourceLabel, onMaterialsChange }: {
  batch: BatchCalculationResult;
  catalog: ScoringCatalog;
  activeMatchId: string;
  onSelectStage: (matchId: string) => void;
  accountKey?: string;
  materials?: MaterialsState | null;
  sourceLabel?: string;
  onMaterialsChange?: (next: MaterialsState) => void;
}) {
  const forStars = batch.goal === 'stars';
  const unresolved = batch.stages.filter((stage) => stage.starPlan?.status === 'blocked' || stage.starPlan?.status === 'unavailable');
  return (
    <section className={clsx(card, 'space-y-4 border-accent/40')}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold">整期多场次合并规划（共 {batch.stages.length} 场）</h3>
          <p className="mt-0.5 text-xs text-text-secondary">{forStars ? '按各场满星条件规划升级，共用衣服按所需最高等级合并；已满星场次无需额外养成。该方案合并各场达标建议，不保证整期材料消耗最少。' : '跨场次复用的衣服已按所需最高等级自动去重，点击下方场次可切换查看单场搭配细节。'}</p>
        </div>
        <span className="text-xs tabular-nums text-text-muted">合并用时 {(batch.elapsedMs / 1000).toFixed(1)} 秒</span>
      </div>

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {batch.stages.map((stage) => {
          const outfit = stage.potential ?? stage.current;
          const planned = forStars && (stage.starPlan?.status === 'ready' || stage.starPlan?.status === 'upgrade');
          const active = stage.matchId === activeMatchId;
          return (
            <button key={stage.matchId} type="button" onClick={() => onSelectStage(stage.matchId)}
              className={clsx('rounded-lg border p-2.5 text-left transition-colors',
                active ? 'border-accent bg-accent/10' : 'border-border bg-bg-primary hover:bg-bg-hover')}>
              <p className="truncate text-xs font-medium text-text-primary">{stage.matchName}</p>
              <p className="mt-1 text-sm font-semibold tabular-nums text-accent">{planned ? number(stage.starPlan!.score!) : outfit ? number(outfit.score) : '—'}</p>
              {outfit && !!outfit.stars.length && (
                <div className="mt-1 flex gap-1">
                  {outfit.stars.map((passed, i) => (
                    <Star key={i} className={clsx('h-3 w-3', planned || passed === true ? 'fill-accent text-accent' : 'text-text-muted/40')} />
                  ))}
                </div>
              )}
              {forStars && <p className={clsx('mt-1 text-xs', planned ? 'text-text-secondary' : 'text-warning')}>
                {stage.starPlan?.status === 'ready' ? '当前已满星'
                  : stage.starPlan?.status === 'upgrade' ? '按方案升级后满星'
                  : stage.starPlan?.status === 'blocked' ? '焕新后仍未满星' : '暂未确认满星方案'}
              </p>}
            </button>
          );
        })}
      </div>

      <div>
        <h4 className="text-xs font-semibold text-text-secondary">{forStars ? '满星需要升级的衣服（跨场次合并）' : '跨场次优先升哪件（按登场场次数与累计提分排序）'}</h4>
        {batch.upgrades.length ? (
          <ol className="mt-2 space-y-1.5 text-xs">
            {batch.upgrades.map((up) => (
              <li key={up.groupId} className="flex flex-wrap items-center gap-2 rounded-lg bg-bg-primary px-3 py-2">
                <ArrowUpRight className="h-3.5 w-3.5 shrink-0 text-accent" />
                <span className="min-w-0 flex-1 truncate font-medium">
                  {up.item.name}
                  <span className="ml-2 font-normal text-text-muted">{levelLabel(up.from)} → {levelLabel(up.to)}</span>
                </span>
                <span className="rounded bg-accent/10 px-1.5 py-0.5 text-[11px] text-accent">
                  {forStars ? '满星用于' : '用于'} {up.stages.length} 场（{up.stages.join('、')}）
                </span>
                {!forStars && <strong className="w-20 text-right tabular-nums text-accent">+{number(up.totalGain)}</strong>}
              </li>
            ))}
          </ol>
        ) : (
          <p className="mt-2 text-xs text-text-secondary">{forStars ? unresolved.length ? '暂没有可合并的满星升级建议，请先查看下方未达标场次。' : '当前所选场次均已满星，无需额外升级。' : '整期所有场次用到的服装均已满级焕新。'}</p>
        )}
      </div>

      {forStars && unresolved.length > 0 && <div className="space-y-2 rounded-lg border border-warning/40 bg-warning/5 p-3 text-xs">
        <p className="font-medium text-warning">以下场次尚无确认可用的满星升级方案，未计入材料：</p>
        {unresolved.map((stage) => <button key={stage.matchId} type="button" onClick={() => onSelectStage(stage.matchId)}
          className="block w-full text-left leading-5 text-text-secondary hover:text-text-primary">
          <strong>{stage.matchName}</strong>：{stage.starPlan?.status === 'blocked'
            ? '当前找到的搭配即使全部焕新仍未满星，点击查看缺失套装、部件或分数条件。'
            : '缺少分数门槛或尚未找到达标方案，点击查看单场条件。'}
        </button>)}
      </div>}

      <div>
        <h4 className="text-xs font-semibold text-text-secondary">{forStars ? '满星升级方案的去重材料差额' : '整期去重总材料差额'}</h4>
        <WardrobeMaterialList cost={batch.cost} catalog={catalog} accountKey={accountKey} materials={materials} sourceLabel={sourceLabel} onMaterialsChange={onMaterialsChange} />
        {!!batch.unknownCostGroups.length && (
          <p className="mt-2 text-xs text-text-secondary">其中 {batch.unknownCostGroups.length} 组未填等级的登场部件已按最低等级（0 级）计入总材料差额；若游戏内已强化，在下方补充实际等级即可扣减对应消耗。</p>
        )}
      </div>
    </section>
  );
}
