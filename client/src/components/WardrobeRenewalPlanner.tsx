import { useEffect, useMemo, useState } from 'react';
import { ChevronDown, Loader2, Search, Sparkles, X } from 'lucide-react';
import clsx from 'clsx';
import { useAccountStateStore } from '@/stores/accountStateStore';
import { buildOwnedProfile } from '@/services/scoring/profile';
import { buildRenewalGroups, MAX_LEVEL, RENEWED_LEVEL, renewalSource, type RenewalGroup } from '@/services/scoring/materialPlanning';
import { useWardrobePlanStore } from '@/services/scoring/wardrobePlanStore';
import type { PlanMode, RenewalPlanState } from '@/services/scoring/types';
import { useRealmForecast } from '@/services/useRealmForecast';
import { REALM_GOAL_LABELS } from '@/utils/nikkiOptions';
import { GlowPrepConfigPanel } from './GlowPrepConfigPanel';
import { ManualReadButton, ManualReadLogStrip } from './ManualReadControls';
import { secondaryButton } from './ui/FeaturePage';
import { PlanEmpty, PlanLayout, PlanListPanel, PlanPanel, bulkButton, planCheckbox, planField } from './CollectionPlan';
import { MaterialForecastPanel } from './RealmForecastPanel';
import { WardrobeMaterialSummary } from './WardrobeMaterialSummary';

const smallField = 'rounded-md border border-border bg-bg-primary px-1 py-0.5 text-xs tabular-nums outline-none focus:border-accent';
const PAGE_SIZE = 40;
const num = (value: number) => value.toLocaleString('zh-CN');
const formatTime = (value: string) => new Date(value).toLocaleString('zh-CN', { hour12: false });
const effective = (group: RenewalGroup) => group.level ?? 0;
const topLevel = (group: RenewalGroup) => (group.renewable ? RENEWED_LEVEL : MAX_LEVEL);

export function WardrobeRenewalPlanner({ refreshToken, onOpenCloset }: { refreshToken: number; onOpenCloset: () => void }) {
  const account = useAccountStateStore((state) => state.data);
  const activeUid = account?.uid;
  const wardrobeUpdatedAt = account?.wardrobe?.inventory?.updated_at ?? account?.wardrobe?.last_scan?.updated_at ?? '';
  const { payload, settings, materials, loading, load, setLevel, updateMaterials, adoptMaterials } = useWardrobePlanStore();
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState<'todo' | 'below_max' | 'renewable' | 'selected' | 'priority' | 'all'>('todo');
  const [quality, setQuality] = useState('all');
  const [limit, setLimit] = useState(PAGE_SIZE);
  const [showManual, setShowManual] = useState(false);
  const [extraEnergy, setExtraEnergy] = useState(0);

  useEffect(() => {
    if (activeUid) void load(`${activeUid}:${account?.updated_at ?? wardrobeUpdatedAt}:${refreshToken}`);
  }, [activeUid, account?.updated_at, wardrobeUpdatedAt, refreshToken, load]);
  useEffect(() => {
    setLimit(PAGE_SIZE);
  }, [search, status, quality]);

  const { entries, renewal: renewalForecast, queueSummary } = useRealmForecast({
    extraMaterialEnergy: extraEnergy,
    refreshToken,
    neededOnlyIfGlowBefore: 'renewal',
  });

  const plan = materials.renewal_plan;
  const mode: PlanMode = plan?.mode === 'selected' ? 'selected' : 'all';
  const targets = useMemo(() => plan?.targets ?? {}, [plan]);
  const priorityTargets = useMemo(() => plan?.priority_targets ?? {}, [plan]);
  const hasRenewalBaseline = Boolean(payload?.snapshot?.complete && account?.wardrobe?.renewal_scan?.complete);
  const profile = useMemo(() => (payload ? buildOwnedProfile(payload, settings) : null), [payload, settings]);
  const groups = useMemo(() => (payload && profile ? buildRenewalGroups(payload.catalog, profile) : []), [payload, profile]);
  // 全收集: every owned piece to 焕新 (or full level when it cannot be renewed).
  const planned = useMemo(
    () =>
      groups.flatMap((group) => {
        const wanted = mode === 'all' ? topLevel(group) : targets[group.id];
        if (wanted === undefined) return [];
        const target = Math.min(wanted, topLevel(group));
        return effective(group) < target ? [{ group, target }] : [];
      }),
    [groups, mode, targets],
  );
  const priorityKeys = Object.keys(priorityTargets);
  const priorityPending = useMemo(
    () =>
      groups.flatMap((group) => {
        const wanted = priorityTargets[group.id];
        if (wanted === undefined) return [];
        const cap = mode === 'selected' ? targets[group.id] : topLevel(group);
        if (cap === undefined) return [];
        const target = Math.min(wanted, cap, topLevel(group));
        return effective(group) < target ? [{ group, target }] : [];
      }),
    [groups, priorityTargets, mode, targets],
  );
  const pickedCount = groups.filter((group) => targets[group.id] !== undefined).length;
  const { totals, realmTotals } = renewalForecast;
  const unknownPlanned = planned.filter(({ group }) => group.level === null).length;
  const usingTargets = renewalSource(materials) === 'targets';

  const enabledGoals = entries.filter((entry) => entry.enabled).map((entry) => entry.goal);
  const renewalIdx = enabledGoals.indexOf('renewal');
  const queueRank = renewalIdx < 0 ? null : renewalIdx + 1;
  const precedingNames = renewalIdx > 0 ? enabledGoals.slice(0, renewalIdx).map((g) => REALM_GOAL_LABELS[g]) : [];

  function savePlan(next: Partial<RenewalPlanState>) {
    updateMaterials((previous) => ({
      ...previous,
      renewal_plan: {
        mode: previous.renewal_plan?.mode ?? 'all',
        targets: previous.renewal_plan?.targets ?? {},
        source: 'plan',
        ...next,
        updated_at: new Date().toISOString(),
      },
    }));
  }
  function setTarget(group: RenewalGroup, level: number | null) {
    const next = { ...targets };
    const nextPrio = { ...priorityTargets };
    let prioChanged = false;
    if (level === null) {
      delete next[group.id];
      if (group.id in nextPrio) {
        delete nextPrio[group.id];
        prioChanged = true;
      }
    } else {
      next[group.id] = level;
    }
    savePlan({
      mode: 'selected',
      targets: next,
      ...(prioChanged ? { priority_targets: Object.keys(nextPrio).length ? nextPrio : undefined } : {}),
    });
  }
  function setStock(id: string, value: number) {
    updateMaterials((previous) => ({
      ...previous,
      inventory: { ...previous.inventory, [id]: Math.max(0, Math.round(value) || 0) },
      inventory_updated_at: new Date().toISOString(),
    }));
  }

  if (loading && !payload) {
    return (
      <div className="flex items-center justify-center gap-2 py-16 text-sm text-text-secondary">
        <Loader2 className="h-4 w-4 animate-spin" />
        正在加载焕新规划
      </div>
    );
  }
  if (!payload?.snapshot) {
    return (
      <PlanEmpty
        icon={Sparkles}
        action={
          <button type="button" onClick={onOpenCloset} className={secondaryButton}>
            去「我的衣柜」读取
          </button>
        }
      >
        焕新计划需要先知道你拥有哪些衣服。读取一次衣柜后，这里会列出还能升级或焕新的衣服和要备的材料。
      </PlanEmpty>
    );
  }

  const q = search.trim();
  const shown = groups.filter((group) => {
    const level = effective(group);
    const prioTarget = priorityTargets[group.id];
    const isPrio = prioTarget !== undefined && level < Math.min(prioTarget, topLevel(group));
    return (
      (quality === 'all' || String(group.quality) === quality) &&
      (status === 'all' ||
        (status === 'todo' && level < topLevel(group)) ||
        (status === 'below_max' && level < MAX_LEVEL) ||
        (status === 'renewable' && group.renewable && level < RENEWED_LEVEL) ||
        (status === 'selected' && targets[group.id] !== undefined) ||
        (status === 'priority' && isPrio)) &&
      (!q || group.name.includes(q) || (group.suitName ?? '').includes(q) || group.part.includes(q))
    );
  });
  const doneCount = groups.filter((group) => effective(group) >= topLevel(group)).length;
  const unknownLevels = groups.filter((group) => group.level === null && !group.inferred).length;
  const planLabel = usingTargets
    ? '手动材料目标'
    : mode === 'all'
      ? `全收集 · ${num(planned.length)} 件待养成`
      : `自选 · ${num(planned.length)} 件待养成`;
  const manualTargets = Object.values(materials.glow_plan?.targets ?? {}).filter((value) => Number(value) > 0).length;

  function selectShown(level: number) {
    const next = { ...targets };
    for (const group of shown) {
      const target = Math.min(level, topLevel(group));
      if (effective(group) < target) next[group.id] = target;
    }
    savePlan({ mode: 'selected', targets: next });
  }

  return (
    <div className="space-y-4">
      <ManualReadLogStrip modes={['renewal', 'renewal_inc', 'backpack']} />
      <PlanPanel
        goal="renewal"
        tone={unknownLevels ? 'warn' : 'ok'}
        basis={
          <>
            按 {formatTime(payload.snapshot.updated_at)} 的衣柜记录和已填等级计算 · 已养成到顶{' '}
            <strong className="tabular-nums text-text-primary">{num(doneCount)}</strong> / {num(groups.length)} 组
            {account?.wardrobe?.renewal_scan?.updated_at && (
              <> · 焕新读取于 {formatTime(account.wardrobe.renewal_scan.updated_at)}</>
            )}
            {unknownLevels > 0 && <span className="text-warning"> · {num(unknownLevels)} 组没填等级，按 0 级计算</span>}
          </>
        }
        actions={
          <>
            {hasRenewalBaseline && (
              <ManualReadButton targetMode="renewal_inc" idleLabel="增量同步" primary={unknownLevels > 0} />
            )}
            <ManualReadButton
              targetMode="renewal"
              idleLabel={hasRenewalBaseline ? '完整读取' : '读取焕新'}
              primary={!hasRenewalBaseline && unknownLevels > 0}
            />
            <button type="button" onClick={onOpenCloset} className={secondaryButton}>
              我的衣柜
            </button>
          </>
        }
        mode={mode}
        onModeChange={(next) => savePlan({ mode: next })}
        modeDetail={
          mode === 'all' ? (
            <>全部已拥有的衣服，共 {num(planned.length)} 件待养成；能焕新的按焕新、不能焕新的按满级计算，新衣服同步后自动加入。</>
          ) : (
            <>只为勾选的衣服备料，可分别选择升到满级或焕新，已选 {num(pickedCount)} 件。</>
          )
        }
        queueStatus={
          usingTargets ? (
            <>
              当前按手动材料目标刷取（约 {num(renewalForecast.energy)} 体力）{' '}
              <button type="button" onClick={() => savePlan({})} className="ml-1 text-accent hover:underline">
                改用上面的衣服计划
              </button>
            </>
          ) : realmTotals.missingKinds ? (
            `还差 ${realmTotals.missingKinds} 种待刷材料（预计消耗约 ${num(renewalForecast.energy)} 体力）`
          ) : (
            '材料已备齐，轮到时直接交给下一项'
          )
        }
        queueHint="幻境挑战按当前衣柜与等级自动计算材料缺口：优先补齐各类泡泡与纯真丝线（哪项消耗体力缺口更大先刷哪项），最后再刷噗灵；升级或焕新后随增量同步自动消账。"
      />
      {!usingTargets && priorityKeys.length > 0 && (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-accent/40 bg-accent/5 px-4 py-2.5 text-xs">
          <p className="min-w-0 flex-1 text-text-secondary">
            <strong className="text-accent">优先筹备目标</strong>
            {plan?.priority_label ? `（${plan.priority_label}）` : ''}：
            {priorityPending.length > 0
              ? `还剩 ${priorityPending.length} 件未达目标等级，幻境挑战会优先备齐其材料（约 ${num(renewalForecast.priorityEnergy ?? 0)} 体力），达标后自动继续其余目标。`
              : '所导入部件已全部达到目标等级，将自动按正常计划继续。'}
          </p>
          <button
            type="button"
            onClick={() => {
              savePlan({ priority_targets: undefined, priority_label: undefined });
              if (status === 'priority') setStatus('todo');
            }}
            className="inline-flex shrink-0 items-center gap-1 rounded-lg border border-border bg-bg-secondary px-2.5 py-1 text-xs text-text-secondary hover:bg-bg-hover"
          >
            <X className="h-3.5 w-3.5" />
            清除优先
          </button>
        </div>
      )}
      <PlanLayout
        list={
          <PlanListPanel
            title={mode === 'all' ? '全收集包含的衣服' : '想养成哪些衣服'}
            mode={mode}
            includedCount={planned.length}
            selectedCount={pickedCount}
            onClear={() => savePlan({ mode: 'selected', targets: {} })}
            filters={
              <>
                <label className="flex min-w-36 flex-1 items-center gap-1.5">
                  <Search className="h-3.5 w-3.5 text-text-muted" />
                  <input
                    className={clsx(planField, 'w-full')}
                    placeholder="搜索衣服、套装或部位"
                    value={search}
                    onChange={(event) => setSearch(event.target.value)}
                  />
                </label>
                <select aria-label="养成状态" className={planField} value={status} onChange={(event) => setStatus(event.target.value as typeof status)}>
                  <option value="todo">还能继续养成</option>
                  <option value="below_max">未满级</option>
                  <option value="renewable">可焕新但未焕新</option>
                  {mode === 'selected' && <option value="selected">只看已选</option>}
                  {priorityKeys.length > 0 && <option value="priority">搭配计算优先</option>}
                  <option value="all">全部已拥有</option>
                </select>
                <select aria-label="星级" className={planField} value={quality} onChange={(event) => setQuality(event.target.value)}>
                  <option value="all">全部星级</option>
                  <option value="5">5 星</option>
                  <option value="4">4 星</option>
                  <option value="3">3 星</option>
                </select>
              </>
            }
            bulk={
              <>
                <span>把当前列表全部：</span>
                <button type="button" onClick={() => selectShown(MAX_LEVEL)} className={bulkButton}>
                  升到满级
                </button>
                <button type="button" onClick={() => selectShown(RENEWED_LEVEL)} className={bulkButton}>
                  焕新
                </button>
              </>
            }
            empty={shown.length ? undefined : groups.length ? '没有符合筛选的衣服' : '衣柜记录里还没有可养成的衣服'}
            more={{ shown: limit, total: shown.length, onMore: () => setLimit((previous) => previous + PAGE_SIZE) }}
          >
            {shown.slice(0, limit).map((group) => {
              const target = mode === 'all' ? topLevel(group) : targets[group.id];
              const picked = mode === 'selected' && target !== undefined;
              const done = effective(group) >= topLevel(group);
              const prioTarget = priorityTargets[group.id];
              const isPriority = prioTarget !== undefined && effective(group) < Math.min(prioTarget, topLevel(group));
              return (
                <div key={group.id} className={clsx('flex items-center gap-2 px-4 py-2 text-xs', (picked || isPriority) && 'bg-accent/5')}>
                  {mode === 'selected' && (
                    <input
                      type="checkbox"
                      aria-label={`选择${group.name}`}
                      checked={picked}
                      className={planCheckbox}
                      disabled={!picked && done}
                      onChange={(event) => setTarget(group, event.target.checked ? topLevel(group) : null)}
                    />
                  )}
                  <span className="shrink-0 rounded bg-accent/10 px-1.5 py-0.5 text-[11px] font-medium text-accent">{group.quality}★</span>
                  {isPriority && (
                    <span className="shrink-0 rounded bg-warning/15 px-1.5 py-0.5 text-[10px] font-medium text-warning" title={`优先筹备目标（目标 ${prioTarget === RENEWED_LEVEL ? '焕新' : `${prioTarget} 级`}）`}>
                      优先
                    </span>
                  )}
                  <div className="min-w-0 flex-1">
                    <p className="truncate font-medium text-text-primary">{group.name}</p>
                    <p className="truncate text-[11px] text-text-muted">
                      {group.part}
                      {group.suitName ? ` · ${group.suitName}` : ''}
                    </p>
                  </div>
                  {group.inferred ? (
                    <span className="shrink-0 rounded-md bg-accent/10 px-1.5 py-0.5 text-[11px] text-accent">已焕新</span>
                  ) : (
                    <input
                      type="number"
                      min="0"
                      max="11"
                      placeholder="?"
                      title="当前等级（留空按 0 级计算，11 为焕新）"
                      aria-label={`${group.name}当前等级`}
                      value={group.level ?? ''}
                      className={clsx(smallField, 'w-11 text-center')}
                      onChange={(event) => {
                        const raw = event.target.value;
                        const level = Number(raw);
                        if (raw === '') setLevel(group.id, null);
                        else if (Number.isInteger(level) && level >= 0 && level <= 11) setLevel(group.id, level);
                      }}
                    />
                  )}
                  {picked ? (
                    <select aria-label={`${group.name}目标`} value={target} onChange={(event) => setTarget(group, Number(event.target.value))} className={clsx(smallField, 'w-16')}>
                      <option value={MAX_LEVEL}>→ 满级</option>
                      {group.renewable && <option value={RENEWED_LEVEL}>→ 焕新</option>}
                    </select>
                  ) : (
                    <span className={clsx('w-16 shrink-0 text-right text-[11px]', done ? 'text-success' : 'text-text-muted')}>
                      {done ? '已到顶' : mode === 'all' ? `→ ${target === RENEWED_LEVEL ? '焕新' : '满级'}` : ''}
                    </span>
                  )}
                </div>
              );
            })}
          </PlanListPanel>
        }
        summary={
          <>
            <WardrobeMaterialSummary
              title="焕新材料清单"
              scope={planLabel}
              totals={totals}
              realmTotals={realmTotals}
              realmEnergyLabel="幻境所需能量"
              groups={['jihua', 'vortex', 'chroma', 'crystal', 'world']}
              onStockChange={setStock}
              headerAction={<ManualReadButton targetMode="backpack" idleLabel="更新背包" compact />}
              empty={mode === 'selected' && !pickedCount ? '在左侧勾选想升级或焕新的衣服，这里会算出需要、库存与还差多少。' : '计划里的衣服都已养成到目标，不需要材料'}
              note={
                <>
                  {unknownPlanned > 0 && (
                    <span className="block text-warning">
                      其中 {unknownPlanned} 件没填当前等级，已按 0 级计算；点上方「增量同步 / 完整读取」或手动填写实际等级即可扣减。
                    </span>
                  )}
                  自动规划默认优先刷各类泡泡与纯真丝线（哪项缺口大优先哪项），最后刷噗灵；自己用掉的材料会在每日背包扫描和服装焕新同步时自动核销。
                </>
              }
            />
            <section className="overflow-hidden rounded-xl border border-border bg-bg-secondary">
              <button type="button" onClick={() => setShowManual((value) => !value)} className="flex w-full items-center gap-2 px-4 py-3 text-left text-xs">
                <span className="font-semibold text-text-primary">手动材料目标</span>
                <span className="text-text-secondary">
                  {!usingTargets
                    ? manualTargets
                      ? '已保存，当前未使用'
                      : '未设置'
                    : manualTargets
                      ? `${manualTargets} 项目标${materials.glow_plan?.source_label ? ` · 来自${materials.glow_plan.source_label}` : ''}`
                      : '未设置'}
                </span>
                <ChevronDown className={clsx('ml-auto h-4 w-4 text-text-muted transition-transform', showManual && 'rotate-180')} />
              </button>
              {showManual && (
                <div className="border-t border-border p-3">
                  <p className="mb-2 text-[11px] leading-5 text-text-muted">
                    不按衣服计算、直接指定每种材料的目标库存。保存手动目标或从搭配计算导入后，「焕新材料」改按手动目标刷取；在上方切换全收集或自选即可改回衣服计划。
                  </p>
                  <GlowPrepConfigPanel externalMaterials={materials} accountKey={payload.account_key} onChanged={adoptMaterials} />
                </div>
              )}
            </section>
          </>
        }
      />
      <MaterialForecastPanel
        forecast={renewalForecast}
        extraEnergy={extraEnergy}
        onExtraEnergyChange={setExtraEnergy}
        queueRank={queueRank}
        queuedFinishDay={queueSummary.goals.renewal.queuedFinishDay}
        precedingNames={precedingNames}
      />
    </div>
  );
}
