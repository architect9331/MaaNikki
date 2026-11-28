import type { CraftingMaterialMeta, CraftingSketch, PlanMode, ScoringCatalog } from './scoring/types';

export type CollectionRegion = 'wishfield' | 'izhan' | 'xixiya';
export type RegionPriorityMode = 'global' | 'ordered';

export interface RegionFilterConfig {
  /** Enabled regions included in all/power collection scope (non-empty). */
  enabled: CollectionRegion[];
  /** 'global' = cross-region optimal; 'ordered' = farm strictly in `order`. */
  priority: RegionPriorityMode;
  /** Priority order used when `priority === 'ordered'`. */
  order: CollectionRegion[];
}

export const ALL_REGIONS: readonly CollectionRegion[] = ['wishfield', 'izhan', 'xixiya'];
export const DEFAULT_REGION_ORDER: readonly CollectionRegion[] = ['xixiya', 'izhan', 'wishfield'];

export const REGION_META: Record<
  CollectionRegion,
  { id: CollectionRegion; label: string; version: string }
> = {
  wishfield: { id: 'wishfield', label: '心愿原野', version: '1.0' },
  izhan: { id: 'izhan', label: '伊赞之土', version: '2.0' },
  xixiya: { id: 'xixiya', label: '西西亚', version: '3.0' },
};

export const BLESSING_SOURCE_REGION: Record<string, CollectionRegion> = {
  // 1.0 心愿原野
  福鸣之井: 'wishfield',
  愿望梦境仓库: 'wishfield',
  涟漪之窖: 'wishfield',
  幽灵列车: 'wishfield',
  美妆工坊: 'wishfield',
  菲洛米亚女王行宫: 'wishfield',
  古铸剑遗址: 'wishfield',
  巨蛇遗迹: 'wishfield',
  // 2.0 伊赞之土
  母菇梦境: 'izhan',
  伊闪闪宝库: 'izhan',
  巨兽堡垒: 'izhan',
  甜霜梦屿: 'izhan',
  灵魂大潮: 'izhan',
  丰登庆典: 'izhan',
  // 3.0 西西亚
  报时鸟座钟: 'xixiya',
};

const VALID_REGION_SET = new Set<string>(ALL_REGIONS);

export function materialNameRegion(name?: string | null): CollectionRegion | null {
  if (!name) return null;
  if (name.includes('磐岩晶石') || name.includes('幻彩泡泡')) return 'wishfield';
  if (name.includes('大地晶珀') || name.includes('涡影泡泡')) return 'izhan';
  if (name.includes('创想绘片') || name.includes('弥思泡泡')) return 'xixiya';
  return null;
}

export function materialRegion(meta?: CraftingMaterialMeta | null): CollectionRegion | null {
  if (!meta) return null;
  if (meta.kind !== 'monster_craft' && meta.kind !== 'monster_glow') return null;
  return materialNameRegion(meta.name);
}

export function sketchRegion(
  sketch: CraftingSketch,
  materials?: Record<string, CraftingMaterialMeta>,
): CollectionRegion | null {
  if (!materials) return null;
  for (const mid of Object.keys(sketch.costs ?? {})) {
    const reg = materialRegion(materials[mid]);
    if (reg) return reg;
  }
  return null;
}

export function curveRegion(
  catalog: ScoringCatalog,
  curveIndex: number,
): CollectionRegion | null {
  const curve = catalog.curves?.[curveIndex];
  const costs = curve?.costs ?? [];
  const names = catalog.material_names ?? {};
  for (let i = costs.length - 1; i >= 0; i--) {
    const step = costs[i];
    if (!step) continue;
    for (const mid of Object.keys(step)) {
      const reg = materialNameRegion(names[mid]);
      if (reg) return reg;
    }
  }
  return null;
}

export function blessingSourceRegion(source?: string | null): CollectionRegion | null {
  if (!source) return null;
  return BLESSING_SOURCE_REGION[source.trim()] ?? null;
}

export function normalizeRegionConfig(raw?: Partial<RegionFilterConfig> | null): RegionFilterConfig {
  const enabled: CollectionRegion[] = [];
  if (Array.isArray(raw?.enabled)) {
    for (const item of raw.enabled) {
      if (typeof item === 'string' && VALID_REGION_SET.has(item)) {
        const reg = item as CollectionRegion;
        if (!enabled.includes(reg)) enabled.push(reg);
      }
    }
  }
  const finalEnabled = enabled.length > 0 ? enabled : [...ALL_REGIONS];

  const order: CollectionRegion[] = [];
  if (Array.isArray(raw?.order)) {
    for (const item of raw.order) {
      if (typeof item === 'string' && VALID_REGION_SET.has(item)) {
        const reg = item as CollectionRegion;
        if (!order.includes(reg)) order.push(reg);
      }
    }
  }
  for (const reg of DEFAULT_REGION_ORDER) {
    if (!order.includes(reg)) order.push(reg);
  }

  return {
    enabled: finalEnabled,
    priority: raw?.priority === 'ordered' ? 'ordered' : 'global',
    order,
  };
}

export function isAllRegionsEnabled(cfg: RegionFilterConfig): boolean {
  return new Set(cfg.enabled).size >= ALL_REGIONS.length;
}

export function activeRegionOrder(cfg: RegionFilterConfig): CollectionRegion[] | null {
  return cfg.priority === 'ordered' ? cfg.order : null;
}

export function regionRank(
  region: CollectionRegion | null | undefined,
  order: readonly CollectionRegion[] | null | undefined,
): number {
  if (!order || order.length === 0) return 0;
  if (!region) return order.length;
  const idx = order.indexOf(region);
  return idx >= 0 ? idx : order.length;
}

/** Convert per-region requirements into cumulative priority tiers clamped to `totals`. */
export function buildCumulativeRegionTiers(
  totals: Record<string, number>,
  byRegion: Record<string, Record<string, number>>,
  order: readonly CollectionRegion[] | null,
): Record<string, number>[] {
  if (!order || order.length === 0) return [{ ...totals }];
  const tiers: Record<string, number>[] = [];
  const running: Record<string, number> = {};
  for (const reg of order) {
    const regReq = byRegion[reg] ?? {};
    for (const [mid, cnt] of Object.entries(regReq)) {
      running[mid] = (running[mid] ?? 0) + cnt;
    }
    const clamped: Record<string, number> = {};
    for (const [mid, cnt] of Object.entries(running)) {
      const val = Math.min(cnt, totals[mid] ?? 0);
      if (val > 0) clamped[mid] = val;
    }
    tiers.push(clamped);
  }
  tiers.push({ ...totals });
  return tiers;
}

/** Concise badge string for queue summary cards when non-default region filtering or priority is active. */
export function regionSummaryBadge(
  raw: Partial<RegionFilterConfig> | null | undefined,
  mode: PlanMode,
): string | null {
  const cfg = normalizeRegionConfig(raw);
  const allOn = isAllRegionsEnabled(cfg);
  const parts: string[] = [];
  if (mode !== 'selected' && !allOn) {
    const names = ALL_REGIONS.filter((r) => cfg.enabled.includes(r)).map((r) => REGION_META[r].label);
    parts.push(`仅${names.join('+')}`);
  }
  if (cfg.priority === 'ordered') {
    const activeOrder = mode === 'selected' ? cfg.order : cfg.order.filter((r) => cfg.enabled.includes(r));
    if (activeOrder.length >= 2) {
      parts.push(`${REGION_META[activeOrder[0]].label}优先`);
    }
  }
  return parts.length > 0 ? parts.join('·') : null;
}
