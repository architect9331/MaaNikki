import { useEffect, useMemo, useState } from 'react';
import { ChevronDown, ListPlus, Loader2, Search, Sparkles, X } from 'lucide-react';
import clsx from 'clsx';
import { toast } from 'sonner';
import { useAccountStateStore } from '@/stores/accountStateStore';
import { useAppStore } from '@/stores/appStore';
import { MATERIAL_REALM_TARGETS } from '@/services/scoring/heartPresets';
import { buildOwnedProfile } from '@/services/scoring/profile';
import { addCosts, buildRenewalGroups, MAX_LEVEL, renewalCost, RENEWED_LEVEL, summarizeMaterials, type RenewalGroup } from '@/services/scoring/materialPlanning';
import { useWardrobePlanStore } from '@/services/scoring/wardrobePlanStore';
import type { MaterialsState } from '@/services/scoring/types';
import { GlowPrepConfigPanel } from './GlowPrepConfigPanel';
import { WardrobeMaterialSummary } from './WardrobeMaterialSummary';

const field = 'rounded-lg border border-border bg-bg-primary px-2.5 py-1.5 text-xs outline-none focus:border-accent';
const smallField = 'rounded-md border border-border bg-bg-primary px-1 py-0.5 text-xs tabular-nums outline-none focus:border-accent';
const PAGE_SIZE = 40;
const num = (value: number) => value.toLocaleString('zh-CN');
const selectionKey = (account: string) => `maanikki.wardrobe.renewal-selection.${account}`;
const effective = (group: RenewalGroup) => group.level ?? 0;
const topLevel = (group: RenewalGroup) => group.renewable ? RENEWED_LEVEL : MAX_LEVEL;
const levelText = (level: number) => level === RENEWED_LEVEL ? '焕新' : level === MAX_LEVEL ? '满级' : `${level} 级`;

function readSelection(account: string): Record<string, number> {
  try {
    const raw = JSON.parse(localStorage.getItem(selectionKey(account)) ?? '{}');
    return raw && typeof raw === 'object' && !Array.isArray(raw)
      ? Object.fromEntries(Object.entries(raw).filter(([, level]) => level === MAX_LEVEL || level === RENEWED_LEVEL)) as Record<string, number>
      : {};
  } catch {
    return {};
  }
}

/** Hands a renewal shortfall to the standalone glow-up preparation task. */
function useGlowPrepDispatch() {
  const instances = useAppStore((state) => state.instances);
  const activeInstanceId = useAppStore((state) => state.activeInstanceId);
  const addTaskToInstance = useAppStore((state) => state.addTaskToInstance);
  const reorderTasks = useAppStore((state) => state.reorderTasks);
  const toggleTaskEnabled = useAppStore((state) => state.toggleTaskEnabled);
  return () => {
    const instance = instances.find((item) => item.id === activeInstanceId) ?? instances[0];
    if (!instance) return;
    const existing = instance.selectedTasks.find((task) => task.taskName === 'glow_material_prep');
    if (existing) {
      if (!existing.enabled) toggleTaskEnabled(instance.id, existing.id);
      return;
    }
    addTaskToInstance(instance.id, { name: 'glow_material_prep', option: [] });
    const updated = useAppStore.getState().instances.find((item) => item.id === instance.id);
    const from = (updated?.selectedTasks.length ?? 1) - 1;
    const realm = updated?.selectedTasks.findIndex((task) => task.taskName === 'realm_challenge') ?? -1;
    if (realm >= 0 && from > realm) reorderTasks(instance.id, from, realm);
  };
}

export function WardrobeRenewalPlanner({ refreshToken, onOpenCloset }: { refreshToken: number; onOpenCloset: () => void }) {
  const activeUid = useAccountStateStore((state) => state.data?.uid);
  const { payload, settings, materials, loading, load, setLevel, updateMaterials, adoptMaterials } = useWardrobePlanStore();
  const ensureGlowTask = useGlowPrepDispatch();
  const [targets, setTargets] = useState<Record<string, number>>({});
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState<'todo' | 'below_max' | 'renewable' | 'selected' | 'all'>('todo');
  const [quality, setQuality] = useState('all');
  const [limit, setLimit] = useState(PAGE_SIZE);
  const [showPrep, setShowPrep] = useState(false);

  useEffect(() => { void load(`${activeUid ?? ''}:${refreshToken}`); }, [activeUid, refreshToken, load]);
  const accountKey = payload?.account_key ?? '';
  useEffect(() => { if (accountKey) setTargets(readSelection(accountKey)); }, [accountKey]);
  useEffect(() => { setLimit(PAGE_SIZE); }, [search, status, quality]);

  const inventory = materials.inventory ?? {};
  const profile = useMemo(() => payload ? buildOwnedProfile(payload, settings) : null, [payload, settings]);
  const groups = useMemo(() => payload && profile ? buildRenewalGroups(payload.catalog, profile) : [], [payload, profile]);
  const chosen = useMemo(() => groups.filter((group) => targets[group.id] !== undefined), [groups, targets]);
  const required = useMemo(() => {
    const total: Record<string, number> = {};
    if (payload) for (const group of chosen) addCosts(total, renewalCost(payload.catalog, group, targets[group.id]));
    return total;
  }, [payload, chosen, targets]);
  const totals = useMemo(() => summarizeMaterials(required, payload?.crafting_catalog, inventory, payload?.catalog.material_names),
    [required, payload, inventory]);
  const unknownChosen = chosen.filter((group) => group.level === null).length;

  function changeTargets(next: Record<string, number>) {
    setTargets(next);
    if (accountKey) localStorage.setItem(selectionKey(accountKey), JSON.stringify(next));
  }
  function setTarget(group: RenewalGroup, level: number | null) {
    const next = { ...targets };
    if (level === null) delete next[group.id]; else next[group.id] = level;
    changeTargets(next);
  }
  function setStock(id: string, value: number) {
    updateMaterials((previous) => ({ ...previous, inventory: { ...previous.inventory, [id]: Math.max(0, Math.round(value) || 0) },
      inventory_updated_at: new Date().toISOString() }));
  }

  function applyGlowPlan(mode: 'replace' | 'add') {
    if (!payload) return;
    const previousTargets = mode === 'add' ? { ...(materials.glow_plan?.targets ?? {}) } : {};
    const nextTargets: Record<string, number> = { ...previousTargets };
    // The task farms until stock reaches the target, so a target is the total amount
    // this plan will spend (plus any earlier target when accumulating).
    for (const [id, need] of Object.entries(required)) {
      if (!MATERIAL_REALM_TARGETS[id] || need <= 0) continue;
      nextTargets[id] = Math.round((mode === 'add' ? Number(nextTargets[id] ?? 0) : 0) + need);
    }
    if (!Object.keys(nextTargets).length) return;
    const remaining = Object.entries(nextTargets).some(([id, target]) => target > Number(inventory[id] ?? 0));
    updateMaterials((previous): MaterialsState => ({
      ...previous,
      glow_plan: {
        enabled: true,
        completed: !remaining,
        source_label: mode === 'add' ? '换新规划（已累加）' : `换新规划 · ${chosen.length} 件`,
        targets: nextTargets,
        updated_at: new Date().toISOString(),
      },
    }));
    ensureGlowTask();
    toast.success(remaining
      ? mode === 'add' ? '已累加到「焕新材料筹备」任务目标' : '已设为「焕新材料筹备」任务目标'
      : '库存已经够用，「焕新材料筹备」会直接跳过');
  }

  if (loading && !payload) return <div className="flex items-center justify-center gap-2 py-16 text-sm text-text-secondary"><Loader2 className="h-4 w-4 animate-spin" />正在加载换新规划</div>;
  if (!payload?.snapshot) return <div className="rounded-xl border border-border bg-bg-secondary px-5 py-10 text-center text-sm text-text-secondary">
    <Sparkles className="mx-auto mb-2 h-8 w-8 opacity-50" />
    换新规划需要先知道你拥有哪些衣服。
    <button type="button" onClick={onOpenCloset} className="ml-1 text-accent hover:underline">去「我的衣柜」读取</button>
  </div>;

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
  const renewedCount = groups.filter((group) => effective(group) >= RENEWED_LEVEL).length;
  const maxedCount = groups.filter((group) => effective(group) >= MAX_LEVEL).length;
  const glowPlan = materials.glow_plan;
  const glowTargetCount = Object.values(glowPlan?.targets ?? {}).filter((value) => Number(value) > 0).length;

  function selectShown(level: number) {
    const next = { ...targets };
    for (const group of shown) {
      const target = level === RENEWED_LEVEL && !group.renewable ? MAX_LEVEL : level;
      if (effective(group) < target) next[group.id] = target;
    }
    changeTargets(next);
  }

  return <div className="space-y-4">
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-xl border border-border bg-bg-secondary px-4 py-3 text-xs text-text-secondary">
      <Sparkles className="h-4 w-4 text-accent" />
      <span>已拥有可养成衣服 <strong className="text-sm tabular-nums text-text-primary">{num(groups.length)}</strong> 组</span>
      <span>满级 <strong className="tabular-nums text-text-primary">{num(maxedCount)}</strong></span>
      <span>已焕新 <strong className="tabular-nums text-text-primary">{num(renewedCount)}</strong></span>
      <span className="ml-auto text-[11px] text-text-muted">等级与「我的衣柜」同步，同款不同色共享等级</span>
    </div>

    <div className="grid grid-cols-1 items-start gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)]">
      <section className="overflow-hidden rounded-xl border border-border bg-bg-secondary">
        <div className="space-y-2 border-b border-border px-4 py-3">
          <div className="flex items-center justify-between gap-2">
            <h3 className="text-sm font-semibold">想养成哪些衣服</h3>
            {chosen.length > 0 && <button type="button" onClick={() => changeTargets({})} className="inline-flex items-center gap-1 text-xs text-text-secondary hover:text-accent"><X className="h-3 w-3" />清空已选 {chosen.length} 件</button>}
          </div>
          <div className="flex flex-wrap gap-2">
            <label className="flex min-w-36 flex-1 items-center gap-1.5"><Search className="h-3.5 w-3.5 text-text-muted" />
              <input className={clsx(field, 'w-full')} placeholder="搜索衣服、套装或部位" value={search} onChange={(event) => setSearch(event.target.value)} />
            </label>
            <select aria-label="养成状态" className={field} value={status} onChange={(event) => setStatus(event.target.value as typeof status)}>
              <option value="todo">还能继续养成</option><option value="below_max">未满级</option><option value="renewable">可焕新但未焕新</option>
              <option value="selected">只看已选</option><option value="all">全部已拥有</option>
            </select>
            <select aria-label="星级" className={field} value={quality} onChange={(event) => setQuality(event.target.value)}>
              <option value="all">全部星级</option><option value="5">5 星</option><option value="4">4 星</option><option value="3">3 星</option>
            </select>
          </div>
          {shown.length > 0 && <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-text-secondary">
            <span>把当前列表全部加入：</span>
            <button type="button" onClick={() => selectShown(MAX_LEVEL)} className="rounded-md border border-border px-2 py-0.5 hover:border-accent hover:text-accent">升到满级</button>
            <button type="button" onClick={() => selectShown(RENEWED_LEVEL)} className="rounded-md border border-border px-2 py-0.5 hover:border-accent hover:text-accent">焕新</button>
          </div>}
        </div>
        {shown.length === 0
          ? <p className="px-4 py-10 text-center text-xs text-text-secondary">{groups.length ? '没有符合筛选的衣服' : '衣柜记录里还没有可养成的衣服'}</p>
          : <div className="max-h-[560px] divide-y divide-border overflow-auto">
            {shown.slice(0, limit).map((group) => {
              const target = targets[group.id];
              const picked = target !== undefined;
              const reached = picked && effective(group) >= target;
              return <div key={group.id} className={clsx('flex items-center gap-2 px-4 py-2 text-xs', picked && 'bg-accent/5')}>
                <input type="checkbox" aria-label={`选择${group.name}`} checked={picked} className="h-3.5 w-3.5 accent-accent"
                  disabled={!picked && effective(group) >= topLevel(group)}
                  onChange={(event) => setTarget(group, event.target.checked ? topLevel(group) : null)} />
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
                  ? <select aria-label={`${group.name}目标`} value={target} onChange={(event) => setTarget(group, Number(event.target.value))}
                    className={clsx(smallField, 'w-16', reached && 'text-success')}>
                    <option value={MAX_LEVEL}>→ 满级</option>
                    {group.renewable && <option value={RENEWED_LEVEL}>→ 焕新</option>}
                  </select>
                  : <span className="w-16 shrink-0 text-right text-[11px] text-text-muted">{effective(group) >= topLevel(group) ? levelText(topLevel(group)) : ''}</span>}
              </div>;
            })}
            {shown.length > limit && <button type="button" onClick={() => setLimit((previous) => previous + PAGE_SIZE)}
              className="w-full py-2 text-xs text-text-secondary hover:bg-bg-hover">加载更多（{limit} / {shown.length}）</button>}
          </div>}
      </section>

      <div className="space-y-4 lg:sticky lg:top-0">
        <WardrobeMaterialSummary
          title="换新材料清单"
          scope={chosen.length ? `已选 ${num(chosen.length)} 件` : '尚未选择'}
          totals={totals}
          groups={['jihua', 'vortex', 'chroma', 'crystal', 'world']}
          onStockChange={setStock}
          empty={chosen.length ? '选中的衣服已经达到目标等级，不需要材料' : '在左侧勾选想升级或焕新的衣服，这里会算出要准备多少泡泡、丝线和噗灵，以及还差多少。'}
          actions={<>
            <button type="button" onClick={() => applyGlowPlan('replace')} className="inline-flex items-center gap-1 rounded-lg bg-accent px-2.5 py-1.5 text-xs font-medium text-on-accent hover:bg-accent-hover">
              <ListPlus className="h-3.5 w-3.5" />设为焕新筹备目标
            </button>
            {glowTargetCount > 0 && <button type="button" onClick={() => applyGlowPlan('add')} className="inline-flex items-center gap-1 rounded-lg border border-accent/40 px-2.5 py-1.5 text-xs font-medium text-accent hover:bg-accent/10">
              + 累加到现有目标
            </button>}
          </>}
          note={<>
            {unknownChosen > 0 && <span className="block text-warning">其中 {unknownChosen} 件没填当前等级，已按 0 级计算；在左侧填上实际等级即可扣减。</span>}
            设为筹备目标后，独立任务「焕新材料筹备」会在幻境挑战前把这些材料刷到需要的数量。噗灵、丝线与制衣共用库存，两边同时规划时请留足余量。
          </>}
        />
        <section className="overflow-hidden rounded-xl border border-border bg-bg-secondary">
          <button type="button" onClick={() => setShowPrep((value) => !value)} className="flex w-full items-center gap-2 px-4 py-3 text-left text-xs">
            <span className="font-semibold text-text-primary">焕新材料筹备任务</span>
            <span className="text-text-secondary">{glowTargetCount ? `${glowPlan?.completed ? '已刷齐' : '进行中'} · ${glowTargetCount} 项目标${glowPlan?.source_label ? ` · 来自${glowPlan.source_label}` : ''}` : '暂无目标'}</span>
            <ChevronDown className={clsx('ml-auto h-4 w-4 text-text-muted transition-transform', showPrep && 'rotate-180')} />
          </button>
          {showPrep && <div className="border-t border-border p-3">
            <GlowPrepConfigPanel externalMaterials={materials} accountKey={payload.account_key} onChanged={adoptMaterials} />
          </div>}
        </section>
      </div>
    </div>
  </div>;
}
