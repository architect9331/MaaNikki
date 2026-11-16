import { useEffect, useMemo, useState } from 'react';
import { ChevronRight, Hammer, Loader2, RotateCcw, Search } from 'lucide-react';
import clsx from 'clsx';
import { useAccountStateStore } from '@/stores/accountStateStore';
import { buildOwnedProfile } from '@/services/scoring/profile';
import { findDominatedStandaloneItems, normalizePlanMode } from '@/services/scoring/dominance';
import { craftingRequirement, rankFarmableDeficits, summarizeMaterials } from '@/services/scoring/materialPlanning';
import { useWardrobePlanStore } from '@/services/scoring/wardrobePlanStore';
import type { CraftingSketch, PlanMode } from '@/services/scoring/types';
import { useRealmForecast } from '@/services/useRealmForecast';
import { REALM_GOAL_LABELS } from '@/utils/nikkiOptions';
import { ManualReadButton, ManualReadLogStrip } from './ManualReadControls';
import { secondaryButton } from './ui/FeaturePage';
import { PlanEmpty, PlanLayout, PlanListPanel, PlanPanel, bulkButton, planCheckbox, planField } from './CollectionPlan';
import { MaterialForecastPanel } from './RealmForecastPanel';
import { WardrobeMaterialSummary } from './WardrobeMaterialSummary';

const PAGE_SIZE = 30;
const num = (value: number) => value.toLocaleString('zh-CN');
const formatTime = (value: string) => new Date(value).toLocaleString('zh-CN', { hour12: false });

interface SketchGroup {
  key: string;
  title: string;
  quality: number;
  suit: boolean;
  total: number;
  missing: CraftingSketch[];
}

function groupSketches(sketches: CraftingSketch[], ownedIds: Set<string>) {
  const map = new Map<string, SketchGroup>();
  for (const sketch of sketches) {
    const suit = sketch.category === 'suit' && !!sketch.suit_name;
    const key = suit ? `suit:${sketch.suit_id ?? sketch.suit_name}` : `piece:${sketch.id}`;
    let group = map.get(key);
    if (!group) {
      group = { key, title: suit ? sketch.suit_name! : sketch.name, quality: sketch.quality, suit, total: 0, missing: [] };
      map.set(key, group);
    }
    group.total++;
    group.quality = Math.max(group.quality, sketch.quality);
    if (!ownedIds.has(sketch.id)) group.missing.push(sketch);
  }
  return [...map.values()]
    .filter((group) => group.missing.length)
    .sort((a, b) => b.quality - a.quality || Number(b.suit) - Number(a.suit) || a.missing.length - b.missing.length);
}

export function WardrobeCraftingPlanner({ refreshToken, onOpenCloset }: { refreshToken: number; onOpenCloset: () => void }) {
  const account = useAccountStateStore((state) => state.data);
  const activeUid = account?.uid;
  const wardrobeUpdatedAt = account?.wardrobe?.inventory?.updated_at ?? account?.wardrobe?.last_scan?.updated_at ?? '';
  const { payload, settings, materials, loading, load, updateMaterials } = useWardrobePlanStore();
  const [search, setSearch] = useState('');
  const [quality, setQuality] = useState('all');
  const [kind, setKind] = useState<'all' | 'suit' | 'piece' | 'dominated'>('all');
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [limit, setLimit] = useState(PAGE_SIZE);
  const [extraEnergy, setExtraEnergy] = useState(0);

  useEffect(() => {
    if (activeUid) void load(`${activeUid}:${account?.updated_at ?? wardrobeUpdatedAt}:${refreshToken}`);
  }, [activeUid, account?.updated_at, wardrobeUpdatedAt, refreshToken, load]);
  useEffect(() => {
    setLimit(PAGE_SIZE);
  }, [search, quality, kind]);

  const { entries, crafting: craftingForecast, queueSummary, dailyEnergy } = useRealmForecast({
    extraMaterialEnergy: extraEnergy,
    refreshToken,
    neededOnlyIfGlowBefore: 'crafting',
  });

  const crafting = payload?.crafting_catalog;
  const scope = materials.crafting_scope ?? 'all';
  const mode: PlanMode = normalizePlanMode(materials.crafting_plan?.mode);
  const selected = useMemo(() => new Set(materials.crafting_plan?.sketch_ids ?? []), [materials.crafting_plan]);
  const profile = useMemo(() => (payload ? buildOwnedProfile(payload, settings) : null), [payload, settings]);
  const ownedIds = useMemo(() => new Set(profile?.items.map((item) => item.id) ?? []), [profile]);

  // 全收集与战力共用阶段范围（全部设计图 / 仅成套）；战力额外排除纯下位散件；自选可从全部未制设计图勾选。
  const allSketches = crafting?.sketches ?? [];
  const dominatedMap = useMemo(() => {
    if (!payload) return new Map();
    const available = new Set([...ownedIds, ...allSketches.map((sketch) => sketch.id)]);
    return findDominatedStandaloneItems(
      payload.catalog,
      allSketches.filter((sketch) => !ownedIds.has(sketch.id)).map((sketch) => sketch.id),
      available,
    );
  }, [payload, ownedIds, allSketches]);
  const scoped = useMemo(() => allSketches.filter((sketch) => scope === 'all' || sketch.category === 'suit'), [allSketches, scope]);
  const powerSketches = useMemo(() => scoped.filter((sketch) => !dominatedMap.has(sketch.id)), [scoped, dominatedMap]);
  const dominatedSketches = useMemo(
    () => (scope === 'suits' ? [] : allSketches.filter((sketch) => !ownedIds.has(sketch.id) && dominatedMap.has(sketch.id))),
    [scope, allSketches, ownedIds, dominatedMap],
  );
  const effectiveKind = (mode !== 'power' || scope === 'suits') && kind === 'dominated' ? 'all' : kind;
  const listed = mode === 'all' ? scoped : mode === 'power' ? (effectiveKind === 'dominated' ? dominatedSketches : powerSketches) : allSketches;
  const groups = useMemo(() => groupSketches(listed, ownedIds), [listed, ownedIds]);
  const uncraftedScoped = useMemo(() => scoped.filter((sketch) => !ownedIds.has(sketch.id)), [scoped, ownedIds]);
  const uncraftedPower = useMemo(() => powerSketches.filter((sketch) => !ownedIds.has(sketch.id)), [powerSketches, ownedIds]);
  const picked = useMemo(
    () => allSketches.filter((sketch) => selected.has(sketch.id) && !ownedIds.has(sketch.id)),
    [allSketches, selected, ownedIds],
  );
  const planned = mode === 'all' ? uncraftedScoped : mode === 'power' ? uncraftedPower : picked;

  const { monsterQueue, jihuaQueue, scopePreviews } = useMemo(() => {
    const inv = materials.inventory ?? {};
    const snapshotKey = payload?.snapshot
      ? `${payload.snapshot.updated_at ?? ''}:${payload.snapshot.scanned_count ?? 0}`
      : '';
    const baselineMatch = mode !== 'selected' && Boolean(snapshotKey) && materials.wardrobe_baseline_key === snapshotKey;
    const applyConsumed = (req: Record<string, number>) => {
      if (!baselineMatch) return req;
      const next = { ...req };
      for (const [id, used] of Object.entries(materials.crafted_consumed ?? {})) {
        if (next[id]) next[id] = Math.max(0, next[id] - Math.max(0, Number(used)));
      }
      return next;
    };
    const plannedReq = applyConsumed(craftingRequirement(planned));
    const monster = rankFarmableDeficits(plannedReq, {}, crafting, inv, 'monster');
    const jihua = rankFarmableDeficits(plannedReq, {}, crafting, inv, 'jihua');

    const safeDaily = Math.max(10, dailyEnergy);
    const previewFor = (targetScope: 'all' | 'suits') => {
      const items = allSketches.filter(
        (sketch) =>
          !ownedIds.has(sketch.id) &&
          (targetScope === 'all' || sketch.category === 'suit') &&
          (mode !== 'power' || targetScope === 'suits' || !dominatedMap.has(sketch.id)),
      );
      const req = applyConsumed(craftingRequirement(items));
      const sum = summarizeMaterials(req, crafting, inv, payload?.catalog.material_names, safeDaily);
      const crystalEnergy = sum.rows.filter((r) => r.group === 'crystal').reduce((acc, r) => acc + r.energy, 0);
      const crystalDays = crystalEnergy > 0 ? Math.max(1, Math.ceil(crystalEnergy / safeDaily)) : 0;
      return { count: items.length, crystalEnergy, crystalDays };
    };
    return {
      monsterQueue: monster,
      jihuaQueue: jihua,
      scopePreviews: {
        all: previewFor('all'),
        suits: previewFor('suits'),
      },
    };
  }, [planned, materials, payload, mode, crafting, allSketches, ownedIds, dominatedMap, dailyEnergy]);

  const { totals, realmTotals } = craftingForecast;
  const consumedTotal = Object.values(materials.crafted_consumed ?? {}).reduce((sum, value) => sum + Math.max(0, Number(value)), 0);
  const crystalShort = realmTotals.missingKinds;

  const enabledGoals = entries.filter((entry) => entry.enabled).map((entry) => entry.goal);
  const craftingIdx = enabledGoals.indexOf('crafting');
  const queueRank = craftingIdx < 0 ? null : craftingIdx + 1;
  const precedingNames = craftingIdx > 0 ? enabledGoals.slice(0, craftingIdx).map((g) => REALM_GOAL_LABELS[g]) : [];

  function setPlan(nextMode: PlanMode, ids: Set<string> = selected) {
    updateMaterials((previous) => ({ ...previous, crafting_plan: { mode: nextMode, sketch_ids: [...ids] } }));
  }
  function toggle(ids: string[], on: boolean) {
    const next = new Set(selected);
    for (const id of ids) {
      if (on) next.add(id);
      else next.delete(id);
    }
    setPlan('selected', next);
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
        正在加载制衣规划
      </div>
    );
  }
  if (!payload?.snapshot || !crafting) {
    return (
      <PlanEmpty
        icon={Hammer}
        action={
          <button type="button" onClick={onOpenCloset} className={secondaryButton}>
            去「我的衣柜」读取
          </button>
        }
      >
        制衣计划需要先知道你已经做过哪些衣服。读取一次衣柜后，这里会列出还没做的设计图和要备的材料。
      </PlanEmpty>
    );
  }

  const q = search.trim();
  const shown = groups.filter(
    (group) =>
      (quality === 'all' || String(group.quality) === quality) &&
      (effectiveKind === 'all' || effectiveKind === 'dominated' || (effectiveKind === 'suit') === group.suit) &&
      (!q || group.title.includes(q) || group.missing.some((sketch) => sketch.name.includes(q))),
  );
  const activeTotal = mode === 'power' ? powerSketches.length : scoped.length;
  const madeCount = activeTotal - planned.length;
  const scopeLabel = scope === 'suits' ? '仅成套' : '全部设计图';
  const planLabel =
    mode === 'all'
      ? `全收集（${scopeLabel}） · ${num(planned.length)} 张未制作设计图`
      : mode === 'power'
        ? `战力（${scopeLabel}） · ${num(planned.length)} 张未制作设计图`
        : `自选 · ${num(planned.length)} 张设计图`;

  const priorityOrder = monsterQueue.map((item) => item.id);
  const craftingAdvice = monsterQueue.length > 0
    ? {
        label: '刷取顺位',
        title: `下轮刷「${monsterQueue[0].name}」（${monsterQueue[0].sourceLabel} · 还差 ${num(monsterQueue[0].energy)} 体力）`,
        detail: [
          monsterQueue.slice(1, 3).length
            ? `随后：${monsterQueue.slice(1, 3).map((x, i) => `#${i + 2} ${x.name}（${num(x.energy)} 体力）`).join(' → ')}`
            : '其余晶石均已备齐',
          jihuaQueue.length ? `双倍激化将补 ${jihuaQueue.map((x) => x.name).join(' → ')}` : null,
        ].filter(Boolean).join(' · '),
      }
    : jihuaQueue.length > 0
      ? {
          label: '刷取顺位',
          title: '魔物试炼晶石已齐，主队列不再占用体力',
          detail: `若开启超级激化双倍（智能选择），将依次补 ${jihuaQueue.map((x) => `${x.name}（还差 ${num(x.missing)}）`).join(' → ')}`,
        }
      : {
          label: '刷取顺位',
          title: '当前范围所需副本材料已全部备齐',
          detail: planned.length > 0 ? '仅剩大世界采集物可在图鉴路线采集' : '当前范围内没有未制作的设计图',
        };

  return (
    <div className="space-y-4">
      <ManualReadLogStrip modes={['backpack']} />
      <PlanPanel
        goal="crafting"
        tone={payload.snapshot.complete ? 'ok' : 'warn'}
        basis={
          <>
            按 {formatTime(payload.snapshot.updated_at)} 的衣柜记录计算 · 已制作{' '}
            <strong className="tabular-nums text-text-primary">{num(mode === 'selected' ? allSketches.length - allSketches.filter((s) => !ownedIds.has(s.id)).length : madeCount)}</strong> / {num(mode === 'selected' ? allSketches.length : activeTotal)} 张设计图
            {!payload.snapshot.complete && <span className="text-warning"> · 衣柜尚未读完，未读到的衣服会被当作没做</span>}
          </>
        }
        actions={
          <button type="button" onClick={onOpenCloset} className={secondaryButton}>
            更新衣柜
          </button>
        }
        mode={mode}
        onModeChange={(next) => {
          if (next !== 'power' && kind === 'dominated') setKind('all');
          setPlan(next);
        }}
        modeDetail={
          mode === 'all' ? (
            <>全部未制作的设计图，共 {num(uncraftedScoped.length)} 张；做好的衣服下次读衣柜后自动移出。</>
          ) : mode === 'power' ? (
            scope === 'suits' ? (
              <>只做套装图纸以拿齐套装加成与共鸣效果，共 {num(uncraftedPower.length)} 张（套装部件不做上位排除）。</>
            ) : (
              <>
                保留套装与无可替代部件，自动取消已有服装或可制图纸中存在严格上位替代的纯散件（已排除 {num(dominatedSketches.length)} 张），共 {num(uncraftedPower.length)} 张；保留全部图纸以便随时切回全收集。
              </>
            )
          ) : (
            <>只为勾选的套装或部件备料，已选 {num(picked.length)} 张。</>
          )
        }
        scopeRow={
          mode !== 'selected'
            ? {
                label: '阶段范围',
                content: (
                  <div className="flex flex-wrap items-center gap-2">
                    <div className="grid grid-cols-2 gap-1.5" role="radiogroup" aria-label="制衣阶段范围">
                      {(
                        [
                          ['suits', '仅成套', '先做套装拿齐共鸣'] as const,
                          ['all', '全部设计图', '包含未制作散件'] as const,
                        ]
                      ).map(([id, title, hint]) => {
                        const preview = scopePreviews[id];
                        const active = scope === id;
                        return (
                          <button
                            key={id}
                            type="button"
                            role="radio"
                            aria-checked={active}
                            title={hint}
                            onClick={() => updateMaterials((previous) => ({ ...previous, crafting_scope: id }))}
                            className={clsx(
                              'rounded-lg border px-2.5 py-1.5 text-left transition-colors',
                              active
                                ? 'border-accent bg-accent/10 text-text-primary'
                                : 'border-border bg-bg-primary text-text-secondary hover:bg-bg-hover',
                            )}
                          >
                            <div className={clsx('text-xs font-medium', active && 'text-accent')}>{title}</div>
                            <div className="text-[11px] tabular-nums text-text-muted">
                              {num(preview.count)} 张 · {preview.crystalEnergy > 0 ? `晶石约 ${preview.crystalDays} 天` : '晶石已齐'}
                            </div>
                          </button>
                        );
                      })}
                    </div>
                    <span className="text-xs text-text-muted">先做套装，再按需补散件；随时切换不丢进度</span>
                  </div>
                ),
              }
            : undefined
        }
        queueStatus={crystalShort ? `还差 ${crystalShort} 种试炼晶石（约 ${num(realmTotals.energy)} 体力）` : '试炼晶石已备齐，轮到时直接交给下一项'}
        queueHint="平时按未制作设计图缺口刷取魔物试炼晶石；超级激化双倍（智能选择）也会顺带为制衣按缺口补纯真丝线与噗灵（先丝线、后噗灵）。"
        advice={craftingAdvice}
      />
      <PlanLayout
        list={
          <PlanListPanel
            title={mode === 'all' ? '全收集包含的设计图' : mode === 'power' ? (effectiveKind === 'dominated' ? '战力模式已排除的纯下位散件' : '战力模式包含的设计图') : '想做哪些衣服'}
            mode={mode}
            includedCount={effectiveKind === 'dominated' ? dominatedSketches.length : planned.length}
            selectedCount={picked.length}
            onClear={() => setPlan('selected', new Set())}
            filters={
              <>
                <label className="flex min-w-36 flex-1 items-center gap-1.5">
                  <Search className="h-3.5 w-3.5 text-text-muted" />
                  <input
                    className={clsx(planField, 'w-full')}
                    placeholder="搜索套装或部件"
                    value={search}
                    onChange={(event) => setSearch(event.target.value)}
                  />
                </label>
                <select aria-label="套装或散件" className={planField} value={effectiveKind} onChange={(event) => setKind(event.target.value as typeof kind)}>
                  <option value="all">套装与散件</option>
                  <option value="suit">只看套装</option>
                  <option value="piece">只看散件</option>
                  {mode === 'power' && dominatedSketches.length > 0 && (
                    <option value="dominated">已排除下位散件（{dominatedSketches.length}）</option>
                  )}
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
                <button type="button" onClick={() => toggle(shown.flatMap((group) => group.missing.map((sketch) => sketch.id)), true)} className={bulkButton}>
                  勾选
                </button>
                <button type="button" onClick={() => toggle(shown.flatMap((group) => group.missing.map((sketch) => sketch.id)), false)} className={bulkButton}>
                  取消勾选
                </button>
              </>
            }
            empty={shown.length ? undefined : groups.length ? '没有符合筛选的未制作设计图' : '设计图已经全部做完啦'}
            more={{ shown: limit, total: shown.length, onMore: () => setLimit((previous) => previous + PAGE_SIZE) }}
          >
            {shown.slice(0, limit).map((group) => {
              const ids = group.missing.map((sketch) => sketch.id);
              const count = ids.filter((id) => selected.has(id)).length;
              const open = expanded.has(group.key);
              return (
                <div key={group.key} className={clsx(mode === 'selected' && count > 0 && 'bg-accent/5')}>
                  <div className="flex items-center gap-2 px-4 py-2 text-xs">
                    {mode === 'selected' && (
                      <input
                        type="checkbox"
                        aria-label={`选择${group.title}`}
                        checked={count === ids.length}
                        className={planCheckbox}
                        ref={(element) => {
                          if (element) element.indeterminate = count > 0 && count < ids.length;
                        }}
                        onChange={(event) => toggle(ids, event.target.checked)}
                      />
                    )}
                    <button
                      type="button"
                      disabled={!group.suit}
                      onClick={() =>
                        setExpanded((previous) => {
                          const next = new Set(previous);
                          if (next.has(group.key)) next.delete(group.key);
                          else next.add(group.key);
                          return next;
                        })
                      }
                      className="flex min-w-0 flex-1 items-center gap-1.5 text-left"
                    >
                      {group.suit && <ChevronRight className={clsx('h-3.5 w-3.5 shrink-0 text-text-muted transition-transform', open && 'rotate-90')} />}
                      <span className="shrink-0 rounded bg-accent/10 px-1.5 py-0.5 text-[11px] font-medium text-accent">{group.quality}★</span>
                      <span className="truncate font-medium text-text-primary">{group.title}</span>
                      {!group.suit && <span className="shrink-0 text-text-muted">· {group.missing[0].part}</span>}
                    </button>
                    {!group.suit && dominatedMap.has(group.missing[0].id) && (
                      <span className="shrink-0 rounded bg-bg-tertiary px-1.5 py-0.5 text-[11px] text-text-muted">
                        已被「{dominatedMap.get(group.missing[0].id)!.dominatorName}」替代
                      </span>
                    )}
                    {group.suit && (
                      <span className="shrink-0 tabular-nums text-text-secondary">
                        还差 {group.missing.length} / {group.total} 件
                      </span>
                    )}
                  </div>
                  {group.suit && open && (
                    <div className="space-y-1 pb-2 pl-11 pr-4">
                      {group.missing.map((sketch) => (
                        <label key={sketch.id} className="flex items-center gap-2 text-xs text-text-secondary">
                          {mode === 'selected' && (
                            <input
                              type="checkbox"
                              checked={selected.has(sketch.id)}
                              onChange={(event) => toggle([sketch.id], event.target.checked)}
                              className={planCheckbox}
                            />
                          )}
                          <span className="w-12 shrink-0 text-text-muted">{sketch.part}</span>
                          <span className="truncate">{sketch.name}</span>
                        </label>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </PlanListPanel>
        }
        summary={
          <WardrobeMaterialSummary
            title="制衣材料清单"
            scope={planLabel}
            totals={totals}
            realmTotals={realmTotals}
            priorityOrder={priorityOrder}
            groups={['crystal', 'jihua', 'world']}
            onStockChange={setStock}
            headerAction={<ManualReadButton targetMode="backpack" idleLabel="更新背包" compact />}
            empty={mode === 'selected' && !picked.length ? '在左侧勾选想做的设计图，这里会算出需要、库存与还差多少。' : '不需要额外的制衣材料'}
            actions={
              mode !== 'selected' && consumedTotal > 0 ? (
                <button
                  type="button"
                  onClick={() => updateMaterials((previous) => ({ ...previous, crafted_consumed: {} }))}
                  title="清空两次衣柜读取之间自动核销的制衣消耗"
                  className="inline-flex items-center gap-1 rounded-lg border border-border px-2.5 py-1.5 text-xs text-text-secondary hover:bg-bg-hover"
                >
                  <RotateCcw className="h-3.5 w-3.5" />
                  重置制衣核销（{num(consumedTotal)}）
                </button>
              ) : undefined
            }
            note={
              mode !== 'selected'
                ? '已读衣柜里有的衣服会自动排除；两次读取之间自己做了衣服时，每日背包扫描发现晶石减少会自动核销，避免重复刷。'
                : '做好勾选的衣服后重新读取衣柜，它们会自动移出计划。噗灵、丝线与焕新共用库存，两边同时规划时请留足余量。'
            }
          />
        }
      />
      <MaterialForecastPanel
        forecast={craftingForecast}
        extraEnergy={extraEnergy}
        onExtraEnergyChange={setExtraEnergy}
        queueRank={queueRank}
        queuedFinishDay={queueSummary.goals.crafting.queuedFinishDay}
        precedingNames={precedingNames}
      />
    </div>
  );
}
