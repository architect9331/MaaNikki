import { useEffect, useMemo, useState } from 'react';
import { ChevronRight, Hammer, Loader2, RotateCcw, Search, Sparkles, X } from 'lucide-react';
import clsx from 'clsx';
import { useAccountStateStore } from '@/stores/accountStateStore';
import { buildOwnedProfile } from '@/services/scoring/profile';
import { craftingRequirement, summarizeMaterials } from '@/services/scoring/materialPlanning';
import { useWardrobePlanStore } from '@/services/scoring/wardrobePlanStore';
import type { CraftingSketch } from '@/services/scoring/types';
import { Segmented } from './ui/FeaturePage';
import { useRealmChallenge, WardrobeMaterialSummary } from './WardrobeMaterialSummary';

const field = 'rounded-lg border border-border bg-bg-primary px-2.5 py-1.5 text-xs outline-none focus:border-accent';
const PAGE_SIZE = 30;
const num = (value: number) => value.toLocaleString('zh-CN');
const selectionKey = (account: string) => `maanikki.wardrobe.crafting-selection.${account}`;

interface SketchGroup { key: string; title: string; quality: number; suit: boolean; total: number; missing: CraftingSketch[] }

function readSelection(account: string): Set<string> {
  try {
    const raw = JSON.parse(localStorage.getItem(selectionKey(account)) ?? '[]');
    return new Set(Array.isArray(raw) ? raw.map(String) : []);
  } catch {
    return new Set();
  }
}

export function WardrobeCraftingPlanner({ refreshToken, onOpenCloset }: { refreshToken: number; onOpenCloset: () => void }) {
  const activeUid = useAccountStateStore((state) => state.data?.uid);
  const { payload, settings, materials, loading, load, updateMaterials } = useWardrobePlanStore();
  const realm = useRealmChallenge();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState('');
  const [quality, setQuality] = useState('all');
  const [kind, setKind] = useState<'all' | 'suit' | 'piece'>('all');
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [limit, setLimit] = useState(PAGE_SIZE);

  useEffect(() => { void load(`${activeUid ?? ''}:${refreshToken}`); }, [activeUid, refreshToken, load]);
  const accountKey = payload?.account_key ?? '';
  useEffect(() => { if (accountKey) setSelected(readSelection(accountKey)); }, [accountKey]);
  useEffect(() => { setLimit(PAGE_SIZE); }, [search, quality, kind]);

  const crafting = payload?.crafting_catalog;
  const scope = materials.crafting_scope ?? 'all';
  const inventory = materials.inventory ?? {};
  const profile = useMemo(() => payload ? buildOwnedProfile(payload, settings) : null, [payload, settings]);
  const ownedIds = useMemo(() => new Set(profile?.items.map((item) => item.id) ?? []), [profile]);

  const scoped = useMemo(() => (crafting?.sketches ?? []).filter((sketch) => scope === 'all' || sketch.category === 'suit'), [crafting, scope]);
  const uncrafted = useMemo(() => scoped.filter((sketch) => !ownedIds.has(sketch.id)), [scoped, ownedIds]);
  const groups = useMemo(() => {
    const map = new Map<string, SketchGroup>();
    for (const sketch of scoped) {
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
  }, [scoped, ownedIds]);

  // Drop selections that were crafted since they were chosen.
  const activeSelection = useMemo(() => uncrafted.filter((sketch) => selected.has(sketch.id)), [uncrafted, selected]);
  const planned = activeSelection.length ? activeSelection : uncrafted;
  const planAll = !activeSelection.length;

  const totals = useMemo(() => {
    const required = craftingRequirement(planned);
    // Crafting done between two wardrobe reads is only known as an account-wide total,
    // so it can only be deducted from the "everything not yet made" plan.
    const snapshotKey = payload?.snapshot ? `${payload.snapshot.updated_at ?? ''}:${payload.snapshot.scanned_count ?? 0}` : '';
    if (planAll && snapshotKey && materials.wardrobe_baseline_key === snapshotKey) {
      for (const [id, used] of Object.entries(materials.crafted_consumed ?? {})) {
        if (required[id]) required[id] = Math.max(0, required[id] - Math.max(0, Number(used)));
      }
    }
    return summarizeMaterials(required, crafting, inventory, payload?.catalog.material_names);
  }, [planned, planAll, payload, materials.wardrobe_baseline_key, materials.crafted_consumed, crafting, inventory]);
  const consumedTotal = Object.values(materials.crafted_consumed ?? {}).reduce((sum, value) => sum + Math.max(0, Number(value)), 0);

  function changeSelection(next: Set<string>) {
    setSelected(next);
    if (accountKey) localStorage.setItem(selectionKey(accountKey), JSON.stringify([...next]));
  }
  function toggle(ids: string[], on: boolean) {
    const next = new Set(selected);
    for (const id of ids) { if (on) next.add(id); else next.delete(id); }
    changeSelection(next);
  }
  function setStock(id: string, value: number) {
    updateMaterials((previous) => ({ ...previous, inventory: { ...previous.inventory, [id]: Math.max(0, Math.round(value) || 0) },
      inventory_updated_at: new Date().toISOString() }));
  }

  if (loading && !payload) return <div className="flex items-center justify-center gap-2 py-16 text-sm text-text-secondary"><Loader2 className="h-4 w-4 animate-spin" />正在加载制衣规划</div>;
  if (!payload?.snapshot || !crafting) return <div className="rounded-xl border border-border bg-bg-secondary px-5 py-10 text-center text-sm text-text-secondary">
    <Hammer className="mx-auto mb-2 h-8 w-8 opacity-50" />
    制衣规划需要先知道你已经做过哪些衣服。
    <button type="button" onClick={onOpenCloset} className="ml-1 text-accent hover:underline">去「我的衣柜」读取</button>
  </div>;

  const q = search.trim();
  const shown = groups.filter((group) => (quality === 'all' || String(group.quality) === quality)
    && (kind === 'all' || (kind === 'suit') === group.suit)
    && (!q || group.title.includes(q) || group.missing.some((sketch) => sketch.name.includes(q))));
  const madeCount = scoped.length - uncrafted.length;

  return <div className="space-y-4">
    <div className="flex flex-wrap items-center gap-3 rounded-xl border border-border bg-bg-secondary px-4 py-3 text-xs">
      <Hammer className="h-4 w-4 text-accent" />
      <span className="text-text-secondary">已制作设计图 <strong className="text-sm tabular-nums text-text-primary">{num(madeCount)}</strong> / {num(scoped.length)}，还有 <strong className="tabular-nums text-text-primary">{num(uncrafted.length)}</strong> 张未制作</span>
      <span className="ml-auto" />
      <Segmented label="设计图范围" value={scope} options={[['all', '全部设计图'], ['suits', '仅成套设计图']] as const}
        onChange={(next) => updateMaterials((previous) => ({ ...previous, crafting_scope: next }))} />
    </div>

    <div className="grid grid-cols-1 items-start gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)]">
      <section className="overflow-hidden rounded-xl border border-border bg-bg-secondary">
        <div className="space-y-2 border-b border-border px-4 py-3">
          <div className="flex items-center justify-between gap-2">
            <h3 className="text-sm font-semibold">想做哪些衣服</h3>
            {activeSelection.length > 0
              ? <button type="button" onClick={() => changeSelection(new Set())} className="inline-flex items-center gap-1 text-xs text-text-secondary hover:text-accent"><X className="h-3 w-3" />清空已选 {activeSelection.length} 件</button>
              : <span className="text-[11px] text-text-muted">不勾选时按全部未制作计算</span>}
          </div>
          <div className="flex flex-wrap gap-2">
            <label className="flex min-w-36 flex-1 items-center gap-1.5"><Search className="h-3.5 w-3.5 text-text-muted" />
              <input className={clsx(field, 'w-full')} placeholder="搜索套装或部件" value={search} onChange={(event) => setSearch(event.target.value)} />
            </label>
            <select aria-label="套装或散件" className={field} value={kind} onChange={(event) => setKind(event.target.value as typeof kind)}>
              <option value="all">套装与散件</option><option value="suit">只看套装</option><option value="piece">只看散件</option>
            </select>
            <select aria-label="星级" className={field} value={quality} onChange={(event) => setQuality(event.target.value)}>
              <option value="all">全部星级</option><option value="5">5 星</option><option value="4">4 星</option><option value="3">3 星</option>
            </select>
          </div>
        </div>
        {shown.length === 0
          ? <p className="px-4 py-10 text-center text-xs text-text-secondary">{uncrafted.length ? '没有符合筛选的未制作设计图' : '这个范围内的设计图已经全部做完啦'}</p>
          : <div className="max-h-[560px] divide-y divide-border overflow-auto">
            {shown.slice(0, limit).map((group) => {
              const ids = group.missing.map((sketch) => sketch.id);
              const picked = ids.filter((id) => selected.has(id)).length;
              const open = expanded.has(group.key);
              return <div key={group.key} className={clsx(picked > 0 && 'bg-accent/5')}>
                <div className="flex items-center gap-2 px-4 py-2 text-xs">
                  <input type="checkbox" aria-label={`选择${group.title}`} checked={picked === ids.length}
                    ref={(element) => { if (element) element.indeterminate = picked > 0 && picked < ids.length; }}
                    onChange={(event) => toggle(ids, event.target.checked)} className="h-3.5 w-3.5 accent-accent" />
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
                    <input type="checkbox" checked={selected.has(sketch.id)} onChange={(event) => toggle([sketch.id], event.target.checked)} className="h-3.5 w-3.5 accent-accent" />
                    <span className="w-12 shrink-0 text-text-muted">{sketch.part}</span><span className="truncate">{sketch.name}</span>
                  </label>)}
                </div>}
              </div>;
            })}
            {shown.length > limit && <button type="button" onClick={() => setLimit((previous) => previous + PAGE_SIZE)}
              className="w-full py-2 text-xs text-text-secondary hover:bg-bg-hover">加载更多（{limit} / {shown.length}）</button>}
          </div>}
      </section>

      <div className="lg:sticky lg:top-0">
        <WardrobeMaterialSummary
          title="制衣材料清单"
          scope={planAll ? `全部未制作 · ${num(planned.length)} 张设计图` : `已选 ${num(planned.length)} 张设计图`}
          totals={totals}
          groups={['crystal', 'jihua', 'world']}
          onStockChange={setStock}
          empty={uncrafted.length ? '选中的设计图不需要额外材料' : '设计图都做完了，暂时不需要制衣材料'}
          actions={<>
            {planAll && totals.missingKinds > 0 && (realm.crafting
              ? <button type="button" onClick={() => realm.setCraftingCollection(false)} className="inline-flex items-center gap-1 rounded-lg bg-accent/15 px-2.5 py-1.5 text-xs font-medium text-accent">
                <Sparkles className="h-3.5 w-3.5 accent-accent" />幻境挑战正在全收集制衣晶石 · 点此关闭
              </button>
              : <button type="button" onClick={() => realm.setCraftingCollection(true)} className="inline-flex items-center gap-1 rounded-lg bg-accent px-2.5 py-1.5 text-xs font-medium text-on-accent hover:bg-accent-hover">
                <Sparkles className="h-3.5 w-3.5 accent-accent" />交给幻境挑战自动刷齐晶石
              </button>)}
            {planAll && consumedTotal > 0 && <button type="button" onClick={() => updateMaterials((previous) => ({ ...previous, crafted_consumed: {} }))}
              title="清空两次衣柜读取之间自动核销的制衣消耗" className="inline-flex items-center gap-1 rounded-lg border border-border px-2.5 py-1.5 text-xs text-text-secondary hover:bg-bg-hover">
              <RotateCcw className="h-3.5 w-3.5 accent-accent" />重置制衣核销（{num(consumedTotal)}）
            </button>}
          </>}
          note={planAll
            ? '已读衣柜里有的衣服会自动排除；两次读取之间自己做了衣服时，每日背包扫描发现晶石减少会自动核销，避免重复刷。自动全收集始终按全部未制作计算。'
            : '只计算勾选的设计图。点「去刷」可把幻境挑战单独指向这项材料；噗灵、丝线与换新共用库存，两边同时规划时请留足余量。'}
        />
      </div>
    </div>
  </div>;
}
