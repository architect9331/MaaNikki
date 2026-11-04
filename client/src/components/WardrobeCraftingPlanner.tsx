import { useEffect, useMemo, useState } from 'react';
import { Check, Hammer, Loader2 } from 'lucide-react';
import clsx from 'clsx';
import { toast } from 'sonner';
import { useAccountStateStore } from '@/stores/accountStateStore';
import { loadScoring, saveMaterialPlan } from '@/services/scoring/api';
import { buildOwnedProfile } from '@/services/scoring/profile';
import { emptySettings, type MaterialsState, type ScoringPayload } from '@/services/scoring/types';
import { GlowPrepConfigPanel } from './GlowPrepConfigPanel';
import { Segmented, Stat } from './ui/FeaturePage';

const field = 'rounded-lg border border-border bg-bg-primary px-2.5 py-1 text-xs outline-none focus:border-accent';
const card = 'rounded-xl border border-border bg-bg-secondary p-4';
const num = (v: number) => v.toLocaleString('zh-CN');

export function WardrobeCraftingPlanner({ refreshToken }: { refreshToken: number }) {
  const activeUid = useAccountStateStore((state) => state.data?.uid);
  const [payload, setPayload] = useState<ScoringPayload | null>(null);
  const [materials, setMaterials] = useState<MaterialsState>({});
  const [loading, setLoading] = useState(true);
  const [showGlowStock, setShowGlowStock] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    void loadScoring().then((next) => {
      if (cancelled) return;
      setPayload(next);
      setMaterials(next.materials ?? {});
    }).catch(() => {}).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [activeUid, refreshToken]);

  const profile = useMemo(() => payload ? buildOwnedProfile(payload, payload.settings ?? emptySettings()) : null, [payload]);
  const ownedIds = useMemo(() => new Set(profile?.items.map((i) => i.id) ?? []), [profile]);
  const scope = materials.crafting_scope ?? 'all';
  const inventory = materials.inventory ?? {};
  const craftingCatalog = payload?.crafting_catalog;

  function persist(next: MaterialsState) {
    setMaterials(next);
    if (payload) void saveMaterialPlan(payload.account_key, next).catch((err) => toast.error(String(err)));
  }

  function setStock(mid: string, val: number) {
    const nextInv = { ...inventory, [mid]: Math.max(0, Math.round(val)) };
    persist({ ...materials, inventory: nextInv, inventory_updated_at: new Date().toISOString() });
  }

  const summary = useMemo(() => {
    if (!craftingCatalog) return null;
    const activeSketches = craftingCatalog.sketches.filter((s) => scope === 'all' || s.category === 'suit');
    const uncrafted = activeSketches.filter((s) => !ownedIds.has(s.id));
    const required: Record<string, number> = {};
    for (const sketch of uncrafted) {
      for (const [mid, cnt] of Object.entries(sketch.costs)) {
        required[mid] = (required[mid] ?? 0) + cnt;
      }
    }
    const crystals = Object.values(craftingCatalog.materials)
      .filter((m) => m.kind === 'monster_craft')
      .map((meta) => {
        const req = required[meta.id] ?? 0;
        const cur = Number(inventory[meta.id] ?? 0);
        const deficit = Math.max(0, req - cur);
        const runs = Math.ceil(deficit / (meta.drop ?? 5));
        return { meta, req, cur, deficit, runs, energy: runs * (meta.energy ?? 40) };
      })
      .sort((a, b) => b.deficit - a.deficit || b.req - a.req);

    const worldMats = Object.entries(required)
      .filter(([mid]) => craftingCatalog.materials[mid]?.kind === 'world')
      .map(([mid, req]) => ({ id: mid, name: craftingCatalog.materials[mid]?.name ?? mid, req }))
      .sort((a, b) => b.req - a.req);

    const totalDeficit = crystals.reduce((s, c) => s + c.deficit, 0);
    const totalEnergy = crystals.reduce((s, c) => s + c.energy, 0);
    return {
      totalSketches: activeSketches.length,
      craftedCount: activeSketches.length - uncrafted.length,
      uncraftedCount: uncrafted.length,
      crystals,
      worldMats,
      totalDeficit,
      totalEnergy,
      days: Math.ceil(totalEnergy / 320),
    };
  }, [craftingCatalog, scope, ownedIds, inventory]);

  if (loading) return <div className="flex items-center justify-center gap-2 py-16 text-sm text-text-secondary"><Loader2 className="h-4 w-4 animate-spin" />正在加载制衣与材料规划</div>;
  if (!payload || !craftingCatalog || !summary) return <p className={clsx(card, 'text-sm text-text-secondary')}>请先在「衣柜读取」中读取当前账号的衣柜。</p>;

  const glowMats = Object.values(craftingCatalog.materials).filter((m) => m.kind === 'monster_glow' || m.kind === 'jihua');

  return <div className="space-y-4">
    <div className="grid grid-cols-3 gap-3">
      <Stat label="已制作设计图" value={<>{summary.craftedCount}<span className="ml-1 text-sm font-normal text-text-muted">/ {summary.totalSketches}</span></>} />
      <Stat label="试炼晶石总缺口" value={<>{num(summary.totalDeficit)}<span className="ml-1 text-xs font-normal text-text-muted">个</span></>} />
      <Stat label="预计刷齐时间" value={summary.totalEnergy ? <>{summary.days}<span className="ml-1 text-xs font-normal text-text-muted">天（{num(summary.totalEnergy)} 能量）</span></> : '已集齐'} />
    </div>

    <GlowPrepConfigPanel
      externalMaterials={materials}
      accountKey={payload.account_key}
      onChanged={setMaterials}
    />

    <section className={clsx(card, 'space-y-3')}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Hammer className="h-4 w-4 text-accent" />
          <h3 className="text-sm font-semibold">魔物试炼制衣晶石追全（共 10 种）</h3>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Segmented label="设计图范围" value={scope} options={[['all', '全部设计图 (844)'], ['suits', '仅成套设计图 (538)']] as const}
            onChange={(next) => persist({ ...materials, crafting_scope: next })} />
          <button type="button" onClick={() => setShowGlowStock((v) => !v)}
            className="rounded-lg border border-border bg-bg-primary px-2.5 py-1.5 text-xs text-text-secondary hover:bg-bg-hover">
            {showGlowStock ? '收起泡泡/噗灵库存' : '管理泡泡/噗灵库存'}
          </button>
        </div>
      </div>
      <p className="text-xs text-text-secondary">
        根据已读衣柜自动剔除已制作服装。在「幻境挑战」中选择「制衣材料全收集」后，将按缺口从大到小精准计算挑战次数并自动累加库存。
      </p>

      <div className="grid grid-cols-1 gap-2.5 md:grid-cols-2">
        {summary.crystals.map(({ meta, req, cur, deficit, runs }) => {
          const pct = req > 0 ? Math.min(100, Math.round(cur / req * 100)) : 100;
          return (
            <div key={meta.id} className="rounded-xl border border-border bg-bg-primary p-3 text-xs">
              <div className="flex items-center justify-between gap-2">
                <div>
                  <strong className="text-sm text-text-primary">{meta.name}</strong>
                  <span className="ml-2 text-text-muted">试炼 · {meta.level}</span>
                </div>
                <label className="flex items-center gap-1.5 text-text-secondary">
                  <span>库存</span>
                  <input type="number" min="0" value={cur} onChange={(e) => setStock(meta.id, Number(e.target.value))}
                    className={clsx(field, '!w-20 text-center tabular-nums')} />
                </label>
              </div>
              <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-border">
                <div className={clsx('h-full rounded-full', deficit === 0 ? 'bg-success' : 'bg-accent')} style={{ width: `${pct}%` }} />
              </div>
              <div className="mt-2 flex items-center justify-between text-[11px] text-text-muted">
                <span>未制作总需 {num(req)} 个</span>
                {deficit > 0 ? (
                  <span className="font-medium text-accent">还差 {num(deficit)} 个（需刷 {runs} 次 / {runs * 40} 能量）</span>
                ) : (
                  <span className="inline-flex items-center gap-1 text-success"><Check className="h-3 w-3" />已集齐</span>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {showGlowStock && (
        <div className="border-t border-border pt-3">
          <p className="mb-2 text-xs font-medium text-text-secondary">焕新泡泡、丝线与噗灵当前库存</p>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            {glowMats.map((m) => (
              <label key={m.id} className="space-y-1 rounded-lg border border-border bg-bg-primary p-2 text-xs">
                <span className="block truncate text-text-secondary">{m.name}</span>
                <input type="number" min="0" value={Number(inventory[m.id] ?? 0)}
                  onChange={(e) => setStock(m.id, Number(e.target.value))}
                  className={clsx(field, 'w-full tabular-nums')} />
              </label>
            ))}
          </div>
        </div>
      )}
    </section>

    <details className={card}>
      <summary className="cursor-pointer text-sm font-medium">
        未制作设计图的大世界采集物汇总 <span className="ml-1 text-xs font-normal text-text-muted">共 {summary.worldMats.length} 种材料</span>
      </summary>
      <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4 text-xs">
        {summary.worldMats.slice(0, 60).map((m) => (
          <div key={m.id} className="flex items-center justify-between rounded-lg bg-bg-primary px-3 py-2">
            <span className="truncate text-text-secondary">{m.name}</span>
            <strong className="ml-2 tabular-nums text-text-primary">{num(m.req)}</strong>
          </div>
        ))}
      </div>
    </details>
  </div>;
}
