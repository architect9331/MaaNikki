import { useEffect, useMemo, useState } from 'react';
import { ChevronDown, Loader2, Search, Sparkles } from 'lucide-react';
import clsx from 'clsx';
import { useAccountStateStore } from '@/stores/accountStateStore';
import { buildOwnedProfile } from '@/services/scoring/profile';
import { addCosts, buildRenewalGroups, MAX_LEVEL, renewalCost, RENEWED_LEVEL, renewalSource, summarizeMaterials, type RenewalGroup } from '@/services/scoring/materialPlanning';
import { useWardrobePlanStore } from '@/services/scoring/wardrobePlanStore';
import type { PlanMode, RenewalPlanState } from '@/services/scoring/types';
import { GlowPrepConfigPanel } from './GlowPrepConfigPanel';
import { secondaryButton } from './ui/FeaturePage';
import { PlanEmpty, PlanLayout, PlanListPanel, PlanPanel, bulkButton, planCheckbox, planField } from './CollectionPlan';
import { WardrobeMaterialSummary } from './WardrobeMaterialSummary';

const smallField = 'rounded-md border border-border bg-bg-primary px-1 py-0.5 text-xs tabular-nums outline-none focus:border-accent';
const PAGE_SIZE = 40;
const num = (value: number) => value.toLocaleString('zh-CN');
const formatTime = (value: string) => new Date(value).toLocaleString('zh-CN', { hour12: false });
const effective = (group: RenewalGroup) => group.level ?? 0;
const topLevel = (group: RenewalGroup) => group.renewable ? RENEWED_LEVEL : MAX_LEVEL;

export function WardrobeRenewalPlanner({ refreshToken, onOpenCloset }: { refreshToken: number; onOpenCloset: () => void }) {
  const activeUid = useAccountStateStore((state) => state.data?.uid);
  const { payload, settings, materials, loading, load, setLevel, updateMaterials, adoptMaterials } = useWardrobePlanStore();
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState<'todo' | 'below_max' | 'renewable' | 'selected' | 'all'>('todo');
  const [quality, setQuality] = useState('all');
  const [limit, setLimit] = useState(PAGE_SIZE);
  const [showManual, setShowManual] = useState(false);

  useEffect(() => { void load(`${activeUid ?? ''}:${refreshToken}`); }, [activeUid, refreshToken, load]);
  useEffect(() => { setLimit(PAGE_SIZE); }, [search, status, quality]);

  const plan = materials.renewal_plan;
  const mode: PlanMode = plan?.mode === 'selected' ? 'selected' : 'all';
  const targets = useMemo(() => plan?.targets ?? {}, [plan]);
  const inventory = materials.inventory ?? {};
  const profile = useMemo(() => payload ? buildOwnedProfile(payload, settings) : null, [payload, settings]);
  const groups = useMemo(() => payload && profile ? buildRenewalGroups(payload.catalog, profile) : [], [payload, profile]);
  // 全收集: every owned piece to 焕新 (or full level when it cannot be renewed).
  const planned = useMemo(() => groups.flatMap((group) => {
    const wanted = mode === 'all' ? topLevel(group) : targets[group.id];
    if (wanted === undefined) return [];
    const target = Math.min(wanted, topLevel(group));
    return effective(group) < target ? [{ group, target }] : [];
  }), [groups, mode, targets]);
  const pickedCount = groups.filter((group) => targets[group.id] !== undefined).length;
  const required = useMemo(() => {
    const total: Record<string, number> = {};
    if (payload) for (const { group, target } of planned) addCosts(total, renewalCost(payload.catalog, group, target));
    const snapshotKey = payload?.snapshot ? `${payload.snapshot.updated_at ?? ''}:${payload.snapshot.scanned_count ?? 0}` : '';
    // Bubbles spent on renewal since the last level change are deducted, as the agent does.
    if (snapshotKey && materials.renewal_baseline_key === `${snapshotKey}|${settings.updated_at ?? ''}`) {
      for (const [id, used] of Object.entries(materials.renewal_consumed ?? {})) {
        if (total[id]) total[id] = Math.max(0, total[id] - Math.max(0, Number(used)));
      }
    }
    return total;
  }, [payload, planned, settings.updated_at, materials.renewal_baseline_key, materials.renewal_consumed]);
  const totals = useMemo(() => summarizeMaterials(required, payload?.crafting_catalog, inventory, payload?.catalog.material_names),
    [required, payload, inventory]);
  const unknownPlanned = planned.filter(({ group }) => group.level === null).length;
  const usingTargets = renewalSource(materials) === 'targets';

  function savePlan(next: Partial<RenewalPlanState>) {
    updateMaterials((previous) => ({ ...previous, renewal_plan: {
      mode: previous.renewal_plan?.mode ?? 'all',
      targets: previous.renewal_plan?.targets ?? {},
      source: 'plan',
      ...next,
      updated_at: new Date().toISOString(),
    } }));
  }
  function setTarget(group: RenewalGroup, level: number | null) {
    const next = { ...targets };
    if (level === null) delete next[group.id]; else next[group.id] = level;
    savePlan({ mode: 'selected', targets: next });
  }
  function setStock(id: string, value: number) {
    updateMaterials((previous) => ({ ...previous, inventory: { ...previous.inventory, [id]: Math.max(0, Math.round(value) || 0) },
      inventory_updated_at: new Date().toISOString() }));
  }

  if (loading && !payload) return <div className="flex items-center justify-center gap-2 py-16 text-sm text-text-secondary"><Loader2 className="h-4 w-4 animate-spin" />正在加载焕新规划</div>;
  if (!payload?.snapshot) return <PlanEmpty icon={Sparkles}
    action={<button type="button" onClick={onOpenCloset} className={secondaryButton}>去「我的衣柜」读取</button>}>
    焕新计划需要先知道你拥有哪些衣服。读取一次衣柜后，这里会列出还能升级或焕新的衣服和要备的材料。
  </PlanEmpty>;

  const q = search.trim();
  const shown = groups.filter((group) => {
    const level = effective(group);
    return (quality === 'all' || String(group.quality) === quality)
      && (status === 'all'
        || status === 'todo' && level < topLevel(group)
        || status === 'below_max' && level < MAX_LEVEL
        || status === 'renewable' && group.renewable && level < RENEWED_LEVEL
        || status === 'selected' && targets[group.id] !== undefined)
      && (!q || group.name.includes(q) || (group.suitName ?? '').includes(q) || group.part.includes(q));
  });
  const doneCount = groups.filter((group) => effective(group) >= topLevel(group)).length;
  const unknownLevels = groups.filter((group) => group.level === null && !group.inferred).length;
  const planLabel = mode === 'all' ? `全收集 · ${num(planned.length)} 件待养成` : `自选 · ${num(planned.length)} 件待养成`;
  const manualTargets = Object.values(materials.glow_plan?.targets ?? {}).filter((value) => Number(value) > 0).length;

  function selectShown(level: number) {
    const next = { ...targets };
    for (const group of shown) {
      const target = Math.min(level, topLevel(group));
      if (effective(group) < target) next[group.id] = target;
    }
    savePlan({ mode: 'selected', targets: next });
  }

  return <div className="space-y-4">
    <PlanPanel goal="renewal" tone={unknownLevels ? 'warn' : 'ok'}
      basis={<>按 {formatTime(payload.snapshot.updated_at)} 的衣柜记录和已填等级计算 · 已养成到顶 <strong className="tabular-nums text-text-primary">{num(doneCount)}</strong> / {num(groups.length)} 组
        {unknownLevels > 0 && <span className="text-warning"> · {num(unknownLevels)} 组没填等级，按 0 级计算</span>}</>}
      actions={<button type="button" onClick={onOpenCloset} className={secondaryButton}>更新衣柜</button>}
      mode={mode} onModeChange={(next) => savePlan({ mode: next })}
      modeDetail={mode === 'all'
        ? <>全部已拥有的衣服，共 {num(planned.length)} 件待养成；能焕新的按焕新、不能焕新的按满级计算，新衣服读衣柜后自动加入。</>
        : <>只为勾选的衣服备料，可分别选择升到满级或焕新，已选 {num(pickedCount)} 件。</>}
      queueStatus={usingTargets
        ? <>当前按手动材料目标刷取 <button type="button" onClick={() => savePlan({})} className="ml-1 text-accent hover:underline">改用上面的衣服计划</button></>
        : totals.missingKinds ? `还差 ${totals.missingKinds} 种材料` : '材料已备齐，轮到时直接交给下一项'}
      queueHint="幻境挑战每批前按当前衣柜和已填等级重新计算缺口，刷泡泡、丝线和噗灵。升级或焕新后请在列表里更新等级。" />
    <PlanLayout
      list={<PlanListPanel title={mode === 'all' ? '全收集包含的衣服' : '想养成哪些衣服'} mode={mode}
        includedCount={planned.length} selectedCount={pickedCount} onClear={() => savePlan({ mode: 'selected', targets: {} })}
        filters={<>
          <label className="flex min-w-36 flex-1 items-center gap-1.5"><Search className="h-3.5 w-3.5 text-text-muted" />
            <input className={clsx(planField, 'w-full')} placeholder="搜索衣服、套装或部位" value={search} onChange={(event) => setSearch(event.target.value)} />
          </label>
          <select aria-label="养成状态" className={planField} value={status} onChange={(event) => setStatus(event.target.value as typeof status)}>
            <option value="todo">还能继续养成</option><option value="below_max">未满级</option><option value="renewable">可焕新但未焕新</option>
            {mode === 'selected' && <option value="selected">只看已选</option>}<option value="all">全部已拥有</option>
          </select>
          <select aria-label="星级" className={planField} value={quality} onChange={(event) => setQuality(event.target.value)}>
            <option value="all">全部星级</option><option value="5">5 星</option><option value="4">4 星</option><option value="3">3 星</option>
          </select>
        </>}
        bulk={<><span>把当前列表全部：</span>
          <button type="button" onClick={() => selectShown(MAX_LEVEL)} className={bulkButton}>升到满级</button>
          <button type="button" onClick={() => selectShown(RENEWED_LEVEL)} className={bulkButton}>焕新</button></>}
        empty={shown.length ? undefined : groups.length ? '没有符合筛选的衣服' : '衣柜记录里还没有可养成的衣服'}
        more={{ shown: limit, total: shown.length, onMore: () => setLimit((previous) => previous + PAGE_SIZE) }}>
        {shown.slice(0, limit).map((group) => {
          const target = mode === 'all' ? topLevel(group) : targets[group.id];
          const picked = mode === 'selected' && target !== undefined;
          const done = effective(group) >= topLevel(group);
          return <div key={group.id} className={clsx('flex items-center gap-2 px-4 py-2 text-xs', picked && 'bg-accent/5')}>
            {mode === 'selected' && <input type="checkbox" aria-label={`选择${group.name}`} checked={picked} className={planCheckbox}
              disabled={!picked && done} onChange={(event) => setTarget(group, event.target.checked ? topLevel(group) : null)} />}
            <span className="shrink-0 rounded bg-accent/10 px-1.5 py-0.5 text-[11px] font-medium text-accent">{group.quality}★</span>
            <div className="min-w-0 flex-1">
              <p className="truncate font-medium text-text-primary">{group.name}</p>
              <p className="truncate text-[11px] text-text-muted">{group.part}{group.suitName ? ` · ${group.suitName}` : ''}</p>
            </div>
            {group.inferred
              ? <span className="shrink-0 rounded-md bg-accent/10 px-1.5 py-0.5 text-[11px] text-accent">已焕新</span>
              : <input type="number" min="0" max="11" placeholder="?" title="当前等级（留空按 0 级计算，11 为焕新）" aria-label={`${group.name}当前等级`}
                value={group.level ?? ''} className={clsx(smallField, 'w-11 text-center')}
                onChange={(event) => {
                  const raw = event.target.value;
                  const level = Number(raw);
                  if (raw === '') setLevel(group.id, null);
                  else if (Number.isInteger(level) && level >= 0 && level <= 11) setLevel(group.id, level);
                }} />}
            {picked
              ? <select aria-label={`${group.name}目标`} value={target} onChange={(event) => setTarget(group, Number(event.target.value))} className={clsx(smallField, 'w-16')}>
                <option value={MAX_LEVEL}>→ 满级</option>
                {group.renewable && <option value={RENEWED_LEVEL}>→ 焕新</option>}
              </select>
              : <span className={clsx('w-16 shrink-0 text-right text-[11px]', done ? 'text-success' : 'text-text-muted')}>
                {done ? '已到顶' : mode === 'all' ? `→ ${target === RENEWED_LEVEL ? '焕新' : '满级'}` : ''}
              </span>}
          </div>;
        })}
      </PlanListPanel>}

      summary={<>
        <WardrobeMaterialSummary
          title="焕新材料清单"
          scope={planLabel}
          totals={totals}
          groups={['jihua', 'vortex', 'chroma', 'crystal', 'world']}
          onStockChange={setStock}
          empty={mode === 'selected' && !pickedCount ? '在左侧勾选想升级或焕新的衣服，这里会算出需要、库存与还差多少。' : '计划里的衣服都已养成到目标，不需要材料'}
          note={<>
            {unknownPlanned > 0 && <span className="block text-warning">其中 {unknownPlanned} 件没填当前等级，已按 0 级计算；在左侧填上实际等级即可扣减。</span>}
            自己用掉的泡泡会在每日背包扫描时自动扣减；噗灵、丝线与制衣共用库存，两边同时规划时请留足余量。
          </>}
        />
        <section className="overflow-hidden rounded-xl border border-border bg-bg-secondary">
          <button type="button" onClick={() => setShowManual((value) => !value)} className="flex w-full items-center gap-2 px-4 py-3 text-left text-xs">
            <span className="font-semibold text-text-primary">手动材料目标</span>
            <span className="text-text-secondary">{!usingTargets ? (manualTargets ? '已保存，当前未使用' : '未设置') : manualTargets ? `${manualTargets} 项目标${materials.glow_plan?.source_label ? ` · 来自${materials.glow_plan.source_label}` : ''}` : '未设置'}</span>
            <ChevronDown className={clsx('ml-auto h-4 w-4 text-text-muted transition-transform', showManual && 'rotate-180')} />
          </button>
          {showManual && <div className="border-t border-border p-3">
            <p className="mb-2 text-[11px] leading-5 text-text-muted">不按衣服计算、直接指定每种材料的目标库存。保存手动目标或从搭配计算导入后，「焕新材料」改按手动目标刷取；在上方切换全收集或自选即可改回衣服计划。</p>
            <GlowPrepConfigPanel externalMaterials={materials} accountKey={payload.account_key} onChanged={adoptMaterials} />
          </div>}
        </section>
      </>}
    />
  </div>;
}
