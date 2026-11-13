import { upgradeCost } from './math';
import type { RealmMaterialTarget } from './heartPresets';
import type { CraftingCatalog, CraftingMaterialMeta, CraftingSketch, MaterialsState, OwnedProfile, ScoringCatalog } from './types';

/** Daily active energy used to turn realm energy into a rough number of days. */
export const DAILY_ENERGY = 320;
export const RENEWED_LEVEL = 11;
export const MAX_LEVEL = 10;

export type MaterialGroup = 'crystal' | 'jihua' | 'vortex' | 'chroma' | 'world';
export const MATERIAL_GROUP_LABELS: Record<MaterialGroup, string> = {
  crystal: '魔物试炼 · 制衣晶石',
  jihua: '素材激化 · 泡泡 / 丝线 / 噗灵',
  vortex: '魔物试炼 · 涡影泡泡（1.0）',
  chroma: '魔物试炼 · 幻彩泡泡（2.0）',
  world: '大世界采集物',
};

export interface MaterialRow {
  id: string;
  name: string;
  group: MaterialGroup;
  need: number;
  /** null when the backpack scan does not track this material. */
  owned: number | null;
  missing: number;
  runs: number;
  energy: number;
  source?: RealmMaterialTarget;
}

export interface MaterialTotals {
  rows: MaterialRow[];
  missingKinds: number;
  trackedKinds: number;
  energy: number;
  days: number;
}

export function materialGroup(meta?: CraftingMaterialMeta): MaterialGroup {
  if (!meta) return 'world';
  if (meta.kind === 'monster_craft') return 'crystal';
  if (meta.kind === 'jihua') return 'jihua';
  if (meta.kind === 'monster_glow') return meta.name.includes('幻彩') ? 'chroma' : 'vortex';
  return 'world';
}

/** Every realm-farmable material maps to one realm challenge option. */
export function realmSourceFor(meta?: CraftingMaterialMeta): RealmMaterialTarget | undefined {
  if (!meta?.realm || !meta.option_case) return undefined;
  const jihua = meta.realm === 'jihua';
  return {
    realmType: jihua ? 'Jihua' : 'Monster',
    optionKey: jihua ? 'JihuaTarget' : 'MonsterTarget',
    caseName: meta.option_case,
    label: `${jihua ? '素材激化' : '魔物试炼'} · ${meta.level ?? meta.name}`,
  };
}

/** Compare a requirement with the recorded backpack stock. */
export function summarizeMaterials(required: Record<string, number>, crafting: CraftingCatalog | null | undefined,
  inventory: Record<string, number>, names: Record<string, string> = {}): MaterialTotals {
  const rows: MaterialRow[] = [];
  for (const [id, raw] of Object.entries(required)) {
    const need = Math.max(0, Math.round(raw));
    if (!need) continue;
    const meta = crafting?.materials[id];
    const group = materialGroup(meta);
    const tracked = group !== 'world';
    const owned = tracked ? Math.max(0, Number(inventory[id] ?? 0)) : null;
    const missing = owned === null ? 0 : Math.max(0, need - owned);
    const runs = missing && meta?.drop ? Math.ceil(missing / meta.drop) : 0;
    rows.push({ id, name: meta?.name ?? names[id] ?? id, group, need, owned, missing, runs,
      energy: runs * (meta?.energy ?? 0), source: realmSourceFor(meta) });
  }
  rows.sort((a, b) => Number(b.missing > 0) - Number(a.missing > 0) || b.missing - a.missing || b.need - a.need);
  const tracked = rows.filter((row) => row.owned !== null);
  const energy = tracked.reduce((sum, row) => sum + row.energy, 0);
  return { rows, missingKinds: tracked.filter((row) => row.missing > 0).length, trackedKinds: tracked.length,
    energy, days: Math.ceil(energy / DAILY_ENERGY) };
}

export function addCosts(target: Record<string, number>, cost: Record<string, number>) {
  for (const [id, count] of Object.entries(cost)) target[id] = (target[id] ?? 0) + count;
  return target;
}

export function craftingRequirement(sketches: CraftingSketch[]) {
  const required: Record<string, number> = {};
  for (const sketch of sketches) addCosts(required, sketch.costs);
  return required;
}

export interface RenewalGroup {
  id: string;
  scoreId: string;
  name: string;
  part: string;
  quality: number;
  /** Saved or inferred level; null means unknown and is planned from level 0. */
  level: number | null;
  inferred: boolean;
  /** Whether level 11 costs extra materials beyond full level. */
  renewable: boolean;
  suitName?: string;
}

/** Owned clothing groups that share one growth level. */
export function buildRenewalGroups(catalog: ScoringCatalog, profile: OwnedProfile): RenewalGroup[] {
  const scoreIdByGroup = new Map<string, string>();
  for (const item of profile.items) if (item.group && !scoreIdByGroup.has(item.group)) scoreIdByGroup.set(item.group, item.id);
  const suitByItem = new Map<number, string>();
  for (const suit of catalog.suits) for (const id of suit.items) suitByItem.set(id, suit.name);
  const rows: RenewalGroup[] = [];
  for (const group of profile.groups) {
    const scoreId = scoreIdByGroup.get(group.id);
    const item = scoreId ? catalog.items[scoreId] : undefined;
    if (!scoreId || !item || item.type >= 200) continue;
    const curve = catalog.curves[item.curve];
    const renewable = !!curve && curve.ratios.length > RENEWED_LEVEL
      && Object.keys(upgradeCost(catalog, item, MAX_LEVEL, RENEWED_LEVEL)).length > 0;
    rows.push({ id: group.id, scoreId, name: group.name, part: (group.part ?? '').split(' / ').pop() ?? '',
      quality: item.quality, level: group.level, inferred: !!group.inferred, renewable,
      suitName: suitByItem.get(Number(scoreId)) });
  }
  return rows.sort((a, b) => b.quality - a.quality || (a.suitName ?? '~').localeCompare(b.suitName ?? '~', 'zh-CN')
    || a.name.localeCompare(b.name, 'zh-CN'));
}

export function renewalCost(catalog: ScoringCatalog, group: RenewalGroup, target: number) {
  const item = catalog.items[group.scoreId];
  return item ? upgradeCost(catalog, item, group.level, target) : {};
}

/** Mirrors the agent: which data drives the 焕新材料 goal. */
export function renewalSource(materials: MaterialsState): 'plan' | 'targets' {
  const plan = materials.renewal_plan;
  if (plan?.source) return plan.source;
  if (plan?.enabled) return 'plan';
  const glow = materials.glow_plan;
  if (glow && glow.enabled !== false && !glow.completed && Object.values(glow.targets ?? {}).some((value) => Number(value) > 0)) return 'targets';
  return 'plan';
}
