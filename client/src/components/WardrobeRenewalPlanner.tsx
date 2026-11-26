import { useEffect, useMemo, useState } from 'react';
import { ChevronDown, Loader2, Search, Sparkles, X } from 'lucide-react';
import clsx from 'clsx';
import { useAccountStateStore } from '@/stores/accountStateStore';
import { buildOwnedProfile } from '@/services/scoring/profile';
import {
  findDominatedStandaloneItems,
  matchesRenewalScope,
  normalizePlanMode,
  normalizeRenewalScope,
  RENEWAL_SCOPE_OPTIONS,
} from '@/services/scoring/dominance';
import {
  addCosts,
  buildRenewalGroups,
  MAX_LEVEL,
  rankFarmableDeficits,
  renewalCost,
  RENEWED_LEVEL,
  renewalSource,
  summarizeMaterials,
  type RenewalGroup,
} from '@/services/scoring/materialPlanning';
import { useWardrobePlanStore } from '@/services/scoring/wardrobePlanStore';
import type { PlanMode, RenewalPlanState, RenewalScope } from '@/services/scoring/types';
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
  const [status, setStatus] = useState<'todo' | 'below_max' | 'renewable' | 'selected' | 'priority' | 'dominated' | 'all'>('todo');
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

  const { entries, renewal: renewalForecast, queueSummary, dailyEnergy } = useRealmForecast({
    extraMaterialEnergy: extraEnergy,
    refreshToken,
    neededOnlyIfGlowBefore: 'renewal',
  });

  const plan = materials.renewal_plan;
  const mode: PlanMode = normalizePlanMode(plan?.mode);
  const renewalScope: RenewalScope = normalizeRenewalScope(materials.renewal_scope);
  const targets = useMemo(() => plan?.targets ?? {}, [plan]);
  const priorityTargets = useMemo(() => plan?.priority_targets ?? {}, [plan]);
  const hasRenewalBaseline = Boolean(payload?.snapshot?.complete && account?.wardrobe?.renewal_scan?.complete);
  const profile = useMemo(() => (payload ? buildOwnedProfile(payload, settings) : null), [payload, settings]);
  const groups = useMemo(() => (payload && profile ? buildRenewalGroups(payload.catalog, profile) : []), [payload, profile]);
  const dominatedMap = useMemo(() => {
    if (!payload) return new Map();
    const ownedScoreIds = new Set(groups.map((group) => group.scoreId));
    return findDominatedStandaloneItems(payload.catalog, ownedScoreIds, ownedScoreIds);
  }, [payload, groups]);
  const dominatedGroups = useMemo(() => groups.filter((group) => dominatedMap.has(group.scoreId)), [groups, dominatedMap]);

  const baseTargetFor = (group: RenewalGroup, scopeValue: RenewalScope = renewalScope): number | undefined => {
    const top = topLevel(group);
    if (mode === 'selected') {
      const wanted = targets[group.id];
      return wanted !== undefined ? Math.min(wanted, top) : undefined;
    }
    if (mode === 'power' && dominatedMap.has(group.scoreId)) return undefined;
    return matchesRenewalScope(group.quality, Boolean(group.suitName), scopeValue) ? top : undefined;
  };
  const prioTargetFor = (group: RenewalGroup): number | undefined => {
    const wanted = priorityTargets[group.id];
    return typeof wanted === 'number' && wanted > 0 ? Math.min(wanted, topLevel(group)) : undefined;
  };

  // 全收集/战力按所选阶段范围决定基础目标；搭配计算优先目标可穿透阶段与自选限制。
  const planned = useMemo(
    () =>
      groups.flatMap((group) => {
        const baseTarget = baseTargetFor(group);
        const pTarget = prioTargetFor(group);
        const target = Math.max(baseTarget ?? 0, pTarget ?? 0);
        if (target <= 0) return [];
        return effective(group) < target ? [{ group, target, outsideBase: baseTarget === undefined }] : [];
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [groups, mode, renewalScope, targets, priorityTargets, dominatedMap],
  );
  const priorityKeys = Object.keys(priorityTargets);
  const priorityPending = useMemo(
    () =>
      groups.flatMap((group) => {
        const pTarget = prioTargetFor(group);
        if (pTarget === undefined) return [];
        const baseTarget = baseTargetFor(group);
        return effective(group) < pTarget
          ? [{ group, target: pTarget, outsideBase: baseTarget === undefined || pTarget > baseTarget }]
          : [];
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [groups, mode, renewalScope, targets, priorityTargets, dominatedMap],
  );
  const { stagePreviews, priorityOrder, priorityQueue, priorityNeedIds } = useMemo(() => {
    const inv = materials.inventory ?? {};
    const safeDaily = Math.max(10, dailyEnergy);
    const snapshotKey = payload?.snapshot
      ? `${payload.snapshot.updated_at ?? ''}:${payload.snapshot.scanned_count ?? 0}|${settings.updated_at ?? ''}`
      : '';
    const applyConsumed = (req: Record<string, number>) => {
      if (!snapshotKey || materials.renewal_baseline_key !== snapshotKey) return req;
      const next = { ...req };
      for (const [id, used] of Object.entries(materials.renewal_consumed ?? {})) {
        if (next[id]) next[id] = Math.max(0, next[id] - Math.max(0, Number(used)));
      }
      return next;
    };

    const previews: Record<RenewalScope, { count: number; energy: number; days: number }> = {
      five_star: { count: 0, energy: 0, days: 0 },
      five_plus_four_suits: { count: 0, energy: 0, days: 0 },
      four_plus: { count: 0, energy: 0, days: 0 },
      all: { count: 0, energy: 0, days: 0 },
    };

    if (payload?.catalog) {
      for (const opt of RENEWAL_SCOPE_OPTIONS) {
        const req: Record<string, number> = {};
        let count = 0;
        for (const group of groups) {
          if (mode === 'power' && dominatedMap.has(group.scoreId)) continue;
          if (!matchesRenewalScope(group.quality, Boolean(group.suitName), opt.id)) continue;
          const target = topLevel(group);
          if (effective(group) < target) {
            count++;
            addCosts(req, renewalCost(payload.catalog, group, target));
          }
        }
        const adjReq = applyConsumed(req);
        const sum = summarizeMaterials(adjReq, payload.crafting_catalog, inv, payload.catalog.material_names, safeDaily);
        const rawEnergy = sum.rows.filter((r) => r.group !== 'world').reduce((acc, r) => acc + r.energy, 0);
        previews[opt.id] = {
          count,
          energy: rawEnergy,
          days: rawEnergy > 0 ? Math.max(1, Math.ceil(rawEnergy / safeDaily)) : 0,
        };
      }
    }

    const activeReq: Record<string, number> = {};
    const prioReq: Record<string, number> = {};
    if (renewalSource(materials) === 'targets') {
      const glow = materials.glow_plan;
      if (glow && glow.enabled !== false && !glow.completed) {
        for (const [id, val] of Object.entries(glow.targets ?? {})) {
          const count = Math.round(Number(val) || 0);
          if (count > 0) activeReq[id] = count;
        }
      }
    } else if (payload?.catalog) {
      for (const item of planned) {
        addCosts(activeReq, renewalCost(payload.catalog, item.group, item.target));
      }
      for (const item of priorityPending) {
        addCosts(prioReq, renewalCost(payload.catalog, item.group, item.target));
      }
    }
    const adjActiveReq = renewalSource(materials) === 'targets' ? activeReq : applyConsumed(activeReq);
    for (const id of Object.keys(prioReq)) {
      prioReq[id] = Math.min(prioReq[id], adjActiveReq[id] ?? 0);
      if (prioReq[id] <= 0) delete prioReq[id];
    }
    const queue = rankFarmableDeficits(adjActiveReq, prioReq, payload?.crafting_catalog, inv);
    const needIds = new Set(
      Object.entries(prioReq)
        .filter(([id, need]) => need > Math.max(0, Number(inv[id] ?? 0)))
        .map(([id]) => id),
    );
    return {
      stagePreviews: previews,
      priorityOrder: queue.map((item) => item.id),
      priorityQueue: queue,
      priorityNeedIds: needIds,
    };
  }, [materials, dailyEnergy, payload, settings.updated_at, groups, mode, dominatedMap, planned, priorityPending]);
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
        mode: normalizePlanMode(previous.renewal_plan?.mode),
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
  const effectiveStatus = mode !== 'power' && status === 'dominated' ? 'todo' : status;
  const outsidePriorityCount = priorityPending.filter(({ outsideBase }) => outsideBase).length;
  const scopeMeta = RENEWAL_SCOPE_OPTIONS.find((item) => item.id === renewalScope) ?? RENEWAL_SCOPE_OPTIONS[3];
  const shown = groups
    .filter((group) => {
      const level = effective(group);
      const isDominated = dominatedMap.has(group.scoreId);
      const baseTarget = baseTargetFor(group);
      const pTarget = prioTargetFor(group);
      const isPrio = pTarget !== undefined && level < pTarget;
      const effectiveTarget = Math.max(baseTarget ?? 0, pTarget ?? 0);
      if (effectiveStatus === 'dominated') {
        return (
          isDominated &&
          !isPrio &&
          (quality === 'all' || String(group.quality) === quality) &&
          (!q || group.name.includes(q) || (group.suitName ?? '').includes(q) || group.part.includes(q))
        );
      }
      if (mode === 'power' && isDominated && !isPrio && effectiveStatus !== 'all') return false;
      const matchesStatus =
        effectiveStatus === 'all' ||
        (effectiveStatus === 'todo' &&
          (mode === 'selected' ? level < topLevel(group) : effectiveTarget > 0 && level < effectiveTarget)) ||
        (effectiveStatus === 'below_max' && level < MAX_LEVEL && (mode === 'selected' || effectiveTarget > 0)) ||
        (effectiveStatus === 'renewable' &&
          group.renewable &&
          level < RENEWED_LEVEL &&
          (mode === 'selected' || effectiveTarget > 0)) ||
        (effectiveStatus === 'selected' && targets[group.id] !== undefined) ||
        (effectiveStatus === 'priority' && isPrio);
      return (
        (quality === 'all' || String(group.quality) === quality) &&
        matchesStatus &&
        (!q || group.name.includes(q) || (group.suitName ?? '').includes(q) || group.part.includes(q))
      );
    })
    .sort((a, b) => {
      const aPrio = prioTargetFor(a) !== undefined && effective(a) < prioTargetFor(a)! ? 1 : 0;
      const bPrio = prioTargetFor(b) !== undefined && effective(b) < prioTargetFor(b)! ? 1 : 0;
      return bPrio - aPrio;
    });
  const activeGroups = mode === 'selected' ? groups : groups.filter((group) => baseTargetFor(group) !== undefined);
  const doneCount = activeGroups.filter((group) => effective(group) >= topLevel(group)).length;
  const unknownLevels = groups.filter((group) => group.level === null && !group.inferred).length;
  const planLabel = usingTargets
    ? '手动材料目标'
    : mode === 'all'
      ? `全收集（${scopeMeta.label}） · ${num(planned.length)} 件待养成`
      : mode === 'power'
        ? `战力（${scopeMeta.label}） · ${num(planned.length)} 件待养成`
        : `自选 · ${num(planned.length)} 件待养成`;
  const manualTargets = Object.values(materials.glow_plan?.targets ?? {}).filter((value) => Number(value) > 0).length;
  const renewalAdvice = priorityQueue.length > 0
    ? {
        label: '刷取顺位',
        title: `${priorityQueue[0].tier === 'priority' ? '搭配优先 · ' : ''}下轮刷「${priorityQueue[0].name}」（${priorityQueue[0].sourceLabel} · 还差 ${num(priorityQueue[0].energy)} 体力）`,
        detail: [
          priorityQueue.slice(1, 3).length
            ? `随后：${priorityQueue.slice(1, 3).map((x, i) => `#${i + 2} ${x.name}（${num(x.energy)} 体力）`).join(' → ')}`
            : '其余可刷材料均已备齐',
          priorityQueue.some((x) => !x.isBling) && priorityQueue.some((x) => x.isBling)
            ? '噗灵顺位排在泡泡与丝线之后'
            : null,
          outsidePriorityCount > 0 ? `含 ${num(outsidePriorityCount)} 件跨阶段穿透目标` : null,
        ].filter(Boolean).join(' · '),
      }
    : {
        label: '刷取顺位',
        title: '当前阶段副本材料已全部备齐',
        detail: planned.length > 0 ? '现有库存足够完成当前范围内养成目标' : '当前范围内暂无待养成的衣服',
      };

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
            <strong className="tabular-nums text-text-primary">{num(doneCount)}</strong> / {num(activeGroups.length)} 组
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
        onModeChange={(next) => {
          if (next !== 'power' && status === 'dominated') setStatus('todo');
          savePlan({ mode: next });
        }}
        modeDetail={
          mode === 'all' ? (
            <>
              按「{scopeMeta.label}」推进养成（{scopeMeta.hint}），共 {num(planned.length)} 件待养成
              {outsidePriorityCount > 0 ? `（含 ${num(outsidePriorityCount)} 件搭配计算穿透引入）` : ''}；切到更大阶段即可无缝接着养。
            </>
          ) : mode === 'power' ? (
            <>
              按「{scopeMeta.label}」推进并自动取消衣柜中已有严格上位替代的纯散件（共排除 {num(dominatedGroups.length)} 件），共 {num(planned.length)} 件待养成
              {outsidePriorityCount > 0 ? `（含 ${num(outsidePriorityCount)} 件搭配计算穿透引入）` : ''}；新获上位款后自动跳过。
            </>
          ) : (
            <>
              只为勾选的衣服备料，可分别选择升到满级或焕新，已选 {num(pickedCount)} 件
              {outsidePriorityCount > 0 ? `；另有 ${num(outsidePriorityCount)} 件由搭配计算穿透优先筹备` : ''}。
            </>
          )
        }
        scopeRow={
          mode !== 'selected'
            ? {
                label: '阶段范围',
                content: (
                  <div className="grid w-full grid-cols-2 gap-1.5 sm:grid-cols-4" role="radiogroup" aria-label="焕新阶段范围">
                    {RENEWAL_SCOPE_OPTIONS.map((opt) => {
                      const preview = stagePreviews[opt.id];
                      const active = renewalScope === opt.id;
                      return (
                        <button
                          key={opt.id}
                          type="button"
                          role="radio"
                          aria-checked={active}
                          title={opt.hint}
                          onClick={() => updateMaterials((previous) => ({ ...previous, renewal_scope: opt.id }))}
                          className={clsx(
                            'rounded-lg border px-2.5 py-1.5 text-left transition-colors',
                            active
                              ? 'border-accent bg-accent/10 text-text-primary'
                              : 'border-border bg-bg-primary text-text-secondary hover:bg-bg-hover',
                          )}
                        >
                          <div className={clsx('text-xs font-medium', active && 'text-accent')}>{opt.label}</div>
                          <div className="text-[11px] tabular-nums text-text-muted">
                            {preview.count > 0 ? `${num(preview.count)} 件 · 约 ${preview.days} 天` : '本阶段已齐'}
                          </div>
                        </button>
                      );
                    })}
                  </div>
                ),
              }
            : undefined
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
        advice={renewalAdvice}
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
            title={
              mode === 'all'
                ? '全收集包含的衣服'
                : mode === 'power'
                  ? effectiveStatus === 'dominated'
                    ? '战力模式已排除的纯下位散件'
                    : '战力模式包含的衣服'
                  : '想养成哪些衣服'
            }
            mode={mode}
            includedCount={effectiveStatus === 'dominated' ? dominatedGroups.length : planned.length}
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
                <select aria-label="养成状态" className={planField} value={effectiveStatus} onChange={(event) => setStatus(event.target.value as typeof status)}>
                  <option value="todo">还能继续养成</option>
                  <option value="below_max">未满级</option>
                  <option value="renewable">可焕新但未焕新</option>
                  {mode === 'selected' && <option value="selected">只看已选</option>}
                  {priorityKeys.length > 0 && <option value="priority">搭配计算优先</option>}
                  {mode === 'power' && dominatedGroups.length > 0 && (
                    <option value="dominated">已排除下位散件（{dominatedGroups.length}）</option>
                  )}
                  <option value="all">全部已拥有</option>
                </select>
                <select aria-label="星级" className={planField} value={quality} onChange={(event) => setQuality(event.target.value)}>
                  <option value="all">全部星级</option>
                  <option value="8">星冕</option>
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
              const isDominated = dominatedMap.has(group.scoreId);
              const excludedByPower = mode === 'power' && isDominated;
              const baseTarget = baseTargetFor(group);
              const pTarget = prioTargetFor(group);
              const isPriority = pTarget !== undefined && effective(group) < pTarget;
              const target = mode === 'selected' ? targets[group.id] : (isPriority ? Math.max(baseTarget ?? 0, pTarget!) : baseTarget);
              const picked = mode === 'selected' && targets[group.id] !== undefined;
              const done = effective(group) >= topLevel(group);
              const outsideBase = isPriority && (baseTarget === undefined || pTarget! > baseTarget);
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
                  <span className="shrink-0 rounded bg-accent/10 px-1.5 py-0.5 text-[11px] font-medium text-accent">{group.quality >= 8 ? '星冕' : `${group.quality}★`}</span>
                  {isPriority && (
                    <span
                      className="shrink-0 rounded bg-warning/15 px-1.5 py-0.5 text-[10px] font-medium text-warning"
                      title={`优先筹备目标（目标 ${pTarget === RENEWED_LEVEL ? '焕新' : `${pTarget} 级`}）`}
                    >
                      优先
                    </span>
                  )}
                  {outsideBase && (
                    <span
                      className="shrink-0 rounded bg-accent/15 px-1.5 py-0.5 text-[10px] font-medium text-accent"
                      title="搭配计算临时穿透当前范围优先筹备，达到目标等级后自动退回正常阶段"
                    >
                      跨阶段
                    </span>
                  )}
                  <div className="min-w-0 flex-1">
                    <p className="truncate font-medium text-text-primary">{group.name}</p>
                    <p className="truncate text-[11px] text-text-muted">
                      {group.part}
                      {group.suitName ? ` · ${group.suitName}` : ''}
                      {excludedByPower && !isPriority ? ` · 已被「${dominatedMap.get(group.scoreId)!.dominatorName}」替代` : ''}
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
                    <select aria-label={`${group.name}目标`} value={targets[group.id]} onChange={(event) => setTarget(group, Number(event.target.value))} className={clsx(smallField, 'w-16')}>
                      <option value={MAX_LEVEL}>→ 满级</option>
                      {group.renewable && <option value={RENEWED_LEVEL}>→ 焕新</option>}
                    </select>
                  ) : (
                    <span className={clsx('w-16 shrink-0 text-right text-[11px]', excludedByPower && !isPriority ? 'text-text-muted' : done ? 'text-success' : 'text-text-muted')}>
                      {excludedByPower && !isPriority
                        ? '已跳过'
                        : done
                          ? '已到顶'
                          : mode !== 'selected'
                            ? target
                              ? `→ ${target === RENEWED_LEVEL ? '焕新' : '满级'}`
                              : '暂未纳入'
                            : isPriority
                              ? `→ ${pTarget === RENEWED_LEVEL ? '焕新' : '满级'}`
                              : ''}
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
              priorityOrder={priorityOrder}
              priorityNeedIds={priorityNeedIds}
              groups={['jihua', 'vortex', 'chroma', 'misi', 'crystal', 'world']}
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
                    不按衣服计算、直接指定每种材料的目标库存。保存手动目标或从搭配计算导入后，「焕新材料」改按手动目标刷取；在上方切换全收集、战力或自选即可改回衣服计划。
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
