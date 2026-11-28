import { upgradeCost } from './math';
import type { RealmMaterialTarget } from './heartPresets';
import { curveRegion, type CollectionRegion } from '../collectionRegions';
import type { CraftingCatalog, CraftingMaterialMeta, CraftingSketch, MaterialsState, OwnedProfile, ScoringCatalog } from './types';

/** Daily active energy used as fallback when no shared assumption is supplied. */
export const DAILY_ENERGY = 280;
export const RENEWED_LEVEL = 11;
export const MAX_LEVEL = 10;

export type MaterialGroup = 'crystal' | 'jihua' | 'vortex' | 'chroma' | 'misi' | 'world';
export const MATERIAL_GROUP_LABELS: Record<MaterialGroup, string> = {
  crystal: '魔物试炼 · 制衣晶石',
  jihua: '素材激化 · 泡泡 / 丝线 / 噗灵',
  chroma: '魔物试炼 · 幻彩泡泡（1.0）',
  vortex: '魔物试炼 · 涡影泡泡（2.0）',
  misi: '魔物试炼 · 弥思泡泡（3.0）',
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
  if (meta.kind === 'monster_glow') {
    if (meta.name.includes('弥思')) return 'misi';
    return meta.name.includes('幻彩') ? 'chroma' : 'vortex';
  }
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
  inventory: Record<string, number>, names: Record<string, string> = {}, dailyEnergy = DAILY_ENERGY): MaterialTotals {
  const safeDaily = Math.max(10, dailyEnergy || DAILY_ENERGY);
  const rows: MaterialRow[] = [];
  for (const [id, raw] of Object.entries(required)) {
    const need = Math.max(0, Math.round(raw));
    if (!need) continue;
    const meta = crafting?.materials[id];
    const group = materialGroup(meta);
    const hasRecordedWorldStock = group === 'world' && Object.prototype.hasOwnProperty.call(inventory, id);
    const tracked = group !== 'world' || hasRecordedWorldStock;
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
    energy, days: Math.ceil(energy / safeDaily) };
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

export type CraftabilityStatus = 'craftable' | 'need_world' | 'missing';

export interface CraftabilityMissingItem {
  id: string;
  name: string;
  group: MaterialGroup;
  need: number;
  owned: number | null;
  missing: number;
}

export interface CraftabilityResult {
  status: CraftabilityStatus;
  missingItems: CraftabilityMissingItem[];
  untrackedCount: number;
}

/**
 * Evaluate whether a sketch (or summed suit costs) can be crafted right now
 * given the recorded backpack inventory.
 */
export function evaluateCostsCraftability(
  costs: Record<string, number>,
  crafting: CraftingCatalog | null | undefined,
  inventory: Record<string, number>,
  names: Record<string, string> = {},
): CraftabilityResult {
  const missingItems: CraftabilityMissingItem[] = [];
  let missingRealm = false;
  let missingWorld = false;
  let untrackedCount = 0;

  for (const [id, rawNeed] of Object.entries(costs ?? {})) {
    const need = Math.max(0, Math.round(Number(rawNeed) || 0));
    if (!need) continue;
    const meta = crafting?.materials[id];
    const group = materialGroup(meta);
    const hasStock = Object.prototype.hasOwnProperty.call(inventory, id);
    const tracked = group !== 'world' || hasStock;
    const owned = tracked ? Math.max(0, Number(inventory[id] ?? 0)) : null;
    if (owned === null) {
      untrackedCount++;
      missingWorld = true;
      missingItems.push({ id, name: meta?.name ?? names[id] ?? id, group, need, owned: null, missing: need });
      continue;
    }
    const missing = Math.max(0, need - owned);
    if (missing > 0) {
      if (group === 'world') missingWorld = true;
      else missingRealm = true;
      missingItems.push({ id, name: meta?.name ?? names[id] ?? id, group, need, owned, missing });
    }
  }

  const status: CraftabilityStatus = !missingRealm && !missingWorld
    ? 'craftable'
    : !missingRealm
      ? 'need_world'
      : 'missing';

  missingItems.sort((a, b) => Number(a.group === 'world') - Number(b.group === 'world') || b.missing - a.missing);
  return { status, missingItems, untrackedCount };
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
  /** Game region inferred from the item's monster_glow bubble curve. */
  region: CollectionRegion | null;
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
      suitName: suitByItem.get(Number(scoreId)),
      region: curveRegion(catalog, item.curve) });
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

export interface FarmablePriorityItem {
  id: string;
  name: string;
  realm: 'monster' | 'jihua';
  levelLabel: string;
  tier: 'priority' | 'base';
  isBling: boolean;
  missing: number;
  totalMissing: number;
  runs: number;
  energy: number;
  sourceLabel: string;
}

const BLING_ID = '992617511';
const SHINY_BUBBLE_ID = '50361309';
const THREAD_ID = '3448636403';
const JIHUA_TIEBREAK: Record<string, number> = {
  [SHINY_BUBBLE_ID]: 0,
  [THREAD_ID]: 1,
  [BLING_ID]: 2,
};

/**
 * Rank farmable deficits in the exact order executed by agent material_pursuit:
 * Tier 0 (搭配计算优先) before region/base tiers, non-bling before bling, larger energy deficit first.
 */
export function rankFarmableDeficits(
  required: Record<string, number>,
  priorityRequired: Record<string, number>,
  crafting: CraftingCatalog | null | undefined,
  inventory: Record<string, number>,
  realmFilter?: 'monster' | 'jihua',
  orderedTiers?: Record<string, number>[],
): FarmablePriorityItem[] {
  const matDefs = crafting?.materials ?? {};
  const list: { sortKey: [number, number, number, number]; item: FarmablePriorityItem }[] = [];
  const baseTiers = orderedTiers && orderedTiers.length > 0 ? orderedTiers : [required];

  for (const [id, rawNeed] of Object.entries(required)) {
    const need = Math.max(0, Math.round(Number(rawNeed) || 0));
    if (!need) continue;
    const meta = matDefs[id];
    if (!meta?.realm || !meta?.level) continue;
    if (realmFilter && meta.realm !== realmFilter) continue;
    const owned = Math.max(0, Number(inventory[id] ?? 0));
    const totalMissing = need - owned;
    if (totalMissing <= 0) continue;
    const prioNeed = Math.max(0, Math.round(Number(priorityRequired[id] ?? 0) || 0));
    const prioMissing = Math.max(0, prioNeed - owned);
    let tierNum = 0;
    let effDeficit = prioMissing;
    if (prioMissing <= 0) {
      tierNum = baseTiers.length + 1;
      effDeficit = totalMissing;
      for (let idx = 0; idx < baseTiers.length; idx++) {
        const tierNeed = Math.max(0, Math.round(Number(baseTiers[idx][id] ?? 0) || 0));
        const tierMissing = tierNeed - owned;
        if (tierMissing > 0) {
          tierNum = idx + 1;
          effDeficit = tierMissing;
          break;
        }
      }
    }
    const isBling = id === BLING_ID;
    const drop = Math.max(1, meta.drop ?? 1);
    const perEnergy = Math.max(1, meta.energy ?? 40);
    const runs = Math.ceil(effDeficit / drop);
    const energy = runs * perEnergy;
    const tiebreak = JIHUA_TIEBREAK[id] ?? 9;
    const source = realmSourceFor(meta);
    list.push({
      sortKey: [tierNum * 2 + (isBling ? 1 : 0), -energy, tiebreak, -effDeficit],
      item: {
        id,
        name: meta.name,
        realm: meta.realm,
        levelLabel: meta.level,
        tier: prioMissing > 0 ? 'priority' : 'base',
        isBling,
        missing: effDeficit,
        totalMissing,
        runs,
        energy,
        sourceLabel: source?.label ?? meta.level,
      },
    });
  }

  list.sort((a, b) => {
    for (let i = 0; i < 4; i++) {
      if (a.sortKey[i] !== b.sortKey[i]) return a.sortKey[i] - b.sortKey[i];
    }
    return a.item.name.localeCompare(b.item.name, 'zh-CN');
  });
  return list.map((entry) => entry.item);
}
