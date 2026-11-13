import { useEffect, useMemo, useState } from 'react';
import { ChevronRight, Hammer, Loader2, RotateCcw, Search } from 'lucide-react';
import clsx from 'clsx';
import { useAccountStateStore } from '@/stores/accountStateStore';
import { buildOwnedProfile } from '@/services/scoring/profile';
import { craftingRequirement, summarizeMaterials } from '@/services/scoring/materialPlanning';
import { useWardrobePlanStore } from '@/services/scoring/wardrobePlanStore';
import type { CraftingSketch, PlanMode } from '@/services/scoring/types';
import { Segmented, secondaryButton } from './ui/FeaturePage';
import { PlanEmpty, PlanLayout, PlanListPanel, PlanPanel, bulkButton, planCheckbox, planField } from './CollectionPlan';
import { WardrobeMaterialSummary } from './WardrobeMaterialSummary';

const PAGE_SIZE = 30;
const num = (value: number) => value.toLocaleString('zh-CN');
const formatTime = (value: string) => new Date(value).toLocaleString('zh-CN', { hour12: false });

interface SketchGroup { key: string; title: string; quality: number; suit: boolean; total: number; missing: CraftingSketch[] }

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
  return [...map.values()].filter((group) => group.missing.length)
    .sort((a, b) => b.quality - a.quality || Number(b.suit) - Number(a.suit) || a.missing.length - b.missing.length);
}

export function WardrobeCraftingPlanner({ refreshToken, onOpenCloset }: { refreshToken: number; onOpenCloset: () => void }) {
  const activeUid = useAccountStateStore((state) => state.data?.uid);
  const { payload, settings, materials, loading, load, updateMaterials } = useWardrobePlanStore();
  const [search, setSearch] = useState('');
  const [quality, setQuality] = useState('all');
  const [kind, setKind] = useState<'all' | 'suit' | 'piece'>('all');
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [limit, setLimit] = useState(PAGE_SIZE);

  useEffect(() => { void load(`${activeUid ?? ''}:${refreshToken}`); }, [activeUid, refreshToken, load]);
  useEffect(() => { setLimit(PAGE_SIZE); }, [search, quality, kind]);

  const crafting = payload?.crafting_catalog;
  const scope = materials.crafting_scope ?? 'all';
  const mode: PlanMode = materials.crafting_plan?.mode === 'selected' ? 'selected' : 'all';
  const selected = useMemo(() => new Set(materials.crafting_plan?.sketch_ids ?? []), [materials.crafting_plan]);
  const inventory = materials.inventory ?? {};
  const profile = useMemo(() => payload ? buildOwnedProfile(payload, settings) : null, [payload, settings]);
  const ownedIds = useMemo(() => new Set(profile?.items.map((item) => item.id) ?? []), [profile]);

  // 全收集 follows the design scope; 自选 may pick from every unmade design.
  const allSketches = crafting?.sketches ?? [];
  const scoped = useMemo(() => allSketches.filter((sketch) => scope === 'all' || sketch.category === 'suit'), [allSketches, scope]);
  const listed = mode === 'all' ? scoped : allSketches;
  const groups = useMemo(() => groupSketches(listed, ownedIds), [listed, ownedIds]);
  const uncraftedScoped = useMemo(() => scoped.filter((sketch) => !ownedIds.has(sketch.id)), [scoped, ownedIds]);
  const picked = useMemo(() => allSketches.filter((sketch) => selected.has(sketch.id) && !ownedIds.has(sketch.id)), [allSketches, selected, ownedIds]);
  const planned = mode === 'all' ? uncraftedScoped : picked;

  const totals = useMemo(() => {
    const required = craftingRequirement(planned);
    // Crafting between two wardrobe reads is only known as an account-wide total,
    // so only the 全收集 plan can deduct it (the agent does the same).
    const snapshotKey = payload?.snapshot ? `${payload.snapshot.updated_at ?? ''}:${payload.snapshot.scanned_count ?? 0}` : '';
    if (mode === 'all' && snapshotKey && materials.wardrobe_baseline_key === snapshotKey) {
      for (const [id, used] of Object.entries(materials.crafted_consumed ?? {})) {
        if (required[id]) required[id] = Math.max(0, required[id] - Math.max(0, Number(used)));
      }
    }
    return summarizeMaterials(required, crafting, inventory, payload?.catalog.material_names);
  }, [planned, mode, payload, materials.wardrobe_baseline_key, materials.crafted_consumed, crafting, inventory]);
  const consumedTotal = Object.values(materials.crafted_consumed ?? {}).reduce((sum, value) => sum + Math.max(0, Number(value)), 0);
  const crystalShort = totals.rows.filter((row) => row.group === 'crystal' && row.missing > 0).length;

  function setPlan(nextMode: PlanMode, ids: Set<string> = selected) {
    updateMaterials((previous) => ({ ...previous, crafting_plan: { mode: nextMode, sketch_ids: [...ids] } }));
  }
  function toggle(ids: string[], on: boolean) {
    const next = new Set(selected);
    for (const id of ids) { if (on) next.add(id); else next.delete(id); }
    setPlan('selected', next);
  }
  function setStock(id: string, value: number) {
    updateMaterials((previous) => ({ ...previous, inventory: { ...previous.inventory, [id]: Math.max(0, Math.round(value) || 0) },
      inventory_updated_at: new Date().toISOString() }));
  }

  if (loading && !payload) return <div className="flex items-center justify-center gap-2 py-16 text-sm text-text-secondary"><Loader2 className="h-4 w-4 animate-spin" />正在加载制衣规划</div>;
  if (!payload?.snapshot || !crafting) return <PlanEmpty icon={Hammer}
    action={<button type="button" onClick={onOpenCloset} className={secondaryButton}>去「我的衣柜」读取</button>}>
    制衣计划需要先知道你已经做过哪些衣服。读取一次衣柜后，这里会列出还没做的设计图和要备的材料。
  </PlanEmpty>;

  const q = search.trim();
  const shown = groups.filter((group) => (quality === 'all' || String(group.quality) === quality)
    && (kind === 'all' || (kind === 'suit') === group.suit)
    && (!q || group.title.includes(q) || group.missing.some((sketch) => sketch.name.includes(q))));
  const madeCount = scoped.length - uncraftedScoped.length;
  const planLabel = mode === 'all' ? `全收集 · ${num(planned.length)} 张未制作设计图` : `自选 · ${num(planned.length)} 张设计图`;

  return <div className="space-y-4">
    <PlanPanel goal="crafting" tone={payload.snapshot.complete ? 'ok' : 'warn'}
      basis={<>按 {formatTime(payload.snapshot.updated_at)} 的衣柜记录计算 · 已制作 <strong className="tabular-nums text-text-primary">{num(madeCount)}</strong> / {num(scoped.length)} 张设计图
        {!payload.snapshot.complete && <span className="text-warning"> · 衣柜尚未读完，未读到的衣服会被当作没做</span>}</>}
      actions={<button type="button" onClick={onOpenCloset} className={secondaryButton}>更新衣柜</button>}
      mode={mode} onModeChange={(next) => setPlan(next)}
      modeDetail={mode === 'all'
        ? <>全部未制作的设计图，共 {num(uncraftedScoped.length)} 张；做好的衣服下次读衣柜后自动移出。</>
        : <>只为勾选的套装或部件备料，已选 {num(picked.length)} 张。</>}
      queueStatus={crystalShort ? `还差 ${crystalShort} 种试炼晶石` : '试炼晶石已备齐，轮到时直接交给下一项'}
      queueHint="幻境挑战按缺口刷取魔物试炼晶石；噗灵、丝线和大世界采集物请按清单自行准备。" />
    <PlanLayout
      list={<PlanListPanel title={mode === 'all' ? '全收集包含的设计图' : '想做哪些衣服'} mode={mode}
        includedCount={planned.length} selectedCount={picked.length} onClear={() => setPlan('selected', new Set())}
        filters={<>
          <label className="flex min-w-36 flex-1 items-center gap-1.5"><Search className="h-3.5 w-3.5 text-text-muted" />
            <input className={clsx(planField, 'w-full')} placeholder="搜索套装或部件" value={search} onChange={(event) => setSearch(event.target.value)} />
          </label>
          <select aria-label="套装或散件" className={planField} value={kind} onChange={(event) => setKind(event.target.value as typeof kind)}>
            <option value="all">套装与散件</option><option value="suit">只看套装</option><option value="piece">只看散件</option>
          </select>
          <select aria-label="星级" className={planField} value={quality} onChange={(event) => setQuality(event.target.value)}>
            <option value="all">全部星级</option><option value="5">5 星</option><option value="4">4 星</option><option value="3">3 星</option>
          </select>
          {mode === 'all' && <Segmented label="全收集范围" value={scope} options={[['all', '全部设计图'], ['suits', '仅成套']] as const}
            onChange={(next) => updateMaterials((previous) => ({ ...previous, crafting_scope: next }))} />}
        </>}
        bulk={<><span>把当前列表全部：</span>
          <button type="button" onClick={() => toggle(shown.flatMap((group) => group.missing.map((sketch) => sketch.id)), true)} className={bulkButton}>勾选</button>
          <button type="button" onClick={() => toggle(shown.flatMap((group) => group.missing.map((sketch) => sketch.id)), false)} className={bulkButton}>取消勾选</button></>}
        empty={shown.length ? undefined : groups.length ? '没有符合筛选的未制作设计图' : '设计图已经全部做完啦'}
        more={{ shown: limit, total: shown.length, onMore: () => setLimit((previous) => previous + PAGE_SIZE) }}>
        {shown.slice(0, limit).map((group) => {
          const ids = group.missing.map((sketch) => sketch.id);
          const count = ids.filter((id) => selected.has(id)).length;
          const open = expanded.has(group.key);
          return <div key={group.key} className={clsx(mode === 'selected' && count > 0 && 'bg-accent/5')}>
            <div className="flex items-center gap-2 px-4 py-2 text-xs">
              {mode === 'selected' && <input type="checkbox" aria-label={`选择${group.title}`} checked={count === ids.length} className={planCheckbox}
                ref={(element) => { if (element) element.indeterminate = count > 0 && count < ids.length; }}
                onChange={(event) => toggle(ids, event.target.checked)} />}
              <button type="button" disabled={!group.suit} onClick={() => setExpanded((previous) => {
                const next = new Set(previous);
                if (next.has(group.key)) next.delete(group.key); else next.add(group.key);
                return next;
              })} className="flex min-w-0 flex-1 items-center gap-1.5 text-left">
                {group.suit && <ChevronRight className={clsx('h-3.5 w-3.5 shrink-0 text-text-muted transition-transform', open && 'rotate-90')} />}
                <span className="shrink-0 rounded bg-accent/10 px-1.5 py-0.5 text-[11px] font-medium text-accent">{group.quality}★</span>
                <span className="truncate font-medium text-text-primary">{group.title}</span>
                {!group.suit && <span className="shrink-0 text-text-muted">· {group.missing[0].part}</span>}
              </button>
              {group.suit && <span className="shrink-0 tabular-nums text-text-secondary">还差 {group.missing.length} / {group.total} 件</span>}
            </div>
            {group.suit && open && <div className="space-y-1 pb-2 pl-11 pr-4">
              {group.missing.map((sketch) => <label key={sketch.id} className="flex items-center gap-2 text-xs text-text-secondary">
                {mode === 'selected' && <input type="checkbox" checked={selected.has(sketch.id)} onChange={(event) => toggle([sketch.id], event.target.checked)} className={planCheckbox} />}
                <span className="w-12 shrink-0 text-text-muted">{sketch.part}</span><span className="truncate">{sketch.name}</span>
              </label>)}
            </div>}
          </div>;
        })}
      </PlanListPanel>}

      summary={<WardrobeMaterialSummary
        title="制衣材料清单"
        scope={planLabel}
        totals={totals}
        groups={['crystal', 'jihua', 'world']}
        onStockChange={setStock}
        empty={mode === 'selected' && !picked.length ? '在左侧勾选想做的设计图，这里会算出需要、库存与还差多少。' : '不需要额外的制衣材料'}
        actions={mode === 'all' && consumedTotal > 0 ? <button type="button" onClick={() => updateMaterials((previous) => ({ ...previous, crafted_consumed: {} }))}
          title="清空两次衣柜读取之间自动核销的制衣消耗" className="inline-flex items-center gap-1 rounded-lg border border-border px-2.5 py-1.5 text-xs text-text-secondary hover:bg-bg-hover">
          <RotateCcw className="h-3.5 w-3.5" />重置制衣核销（{num(consumedTotal)}）
        </button> : undefined}
        note={mode === 'all'
          ? '已读衣柜里有的衣服会自动排除；两次读取之间自己做了衣服时，每日背包扫描发现晶石减少会自动核销，避免重复刷。'
          : '做好勾选的衣服后重新读取衣柜，它们会自动移出计划。噗灵、丝线与焕新共用库存，两边同时规划时请留足余量。'}
      />}
    />
  </div>;
}
