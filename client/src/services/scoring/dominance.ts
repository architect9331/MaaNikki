import type { PlanMode, RenewalScope, ScoreItem, ScoringCatalog } from './types';

export interface DominatedItemInfo {
  dominatorId: string;
  dominatorName: string;
  dominatorQuality: number;
}

export const RENEWAL_SCOPE_OPTIONS: readonly { id: RenewalScope; label: string; hint: string }[] = [
  { id: 'five_star', label: '五星及以上', hint: '先拉满星冕与五星，战力上限最高且全带焕新奖励' },
  { id: 'five_plus_four_suits', label: '5★+ 与 4★套装', hint: '补充四星套装拿共鸣奖励与巅峰赛替补' },
  { id: 'four_plus', label: '四星及以上', hint: '覆盖所有带焕新奖励的四星、五星与星冕部件' },
  { id: 'all', label: '全部星级', hint: '包含二星与三星部件在内的全量养成' },
];

export function normalizeRenewalScope(scope?: string | null): RenewalScope {
  if (scope === 'five_star' || scope === 'five_plus_four_suits' || scope === 'four_plus' || scope === 'all') {
    return scope;
  }
  return 'all';
}

export function matchesRenewalScope(quality: number, inSuit: boolean, scope: RenewalScope): boolean {
  if (scope === 'five_star') return quality >= 5;
  if (scope === 'five_plus_four_suits') return quality >= 5 || (quality === 4 && inSuit);
  if (scope === 'four_plus') return quality >= 4;
  return true;
}

interface CatalogDominanceIndex {
  clothes: Map<string, ScoreItem>;
  byType: Map<number, string[]>;
  suitIds: Set<string>;
  conflicts: Map<string, Set<string>>;
}

const INDEX_CACHE = new WeakMap<ScoringCatalog, CatalogDominanceIndex>();

export function normalizePlanMode(mode?: string | null): PlanMode {
  return mode === 'power' ? 'power' : mode === 'selected' ? 'selected' : 'all';
}

function getCatalogIndex(catalog: ScoringCatalog): CatalogDominanceIndex {
  const cached = INDEX_CACHE.get(catalog);
  if (cached) return cached;

  const clothes = new Map<string, ScoreItem>();
  const byType = new Map<number, string[]>();
  for (const raw of Object.values(catalog.items)) {
    if (raw.type >= 200 || raw.quality < 2 || /^\d+$/.test(raw.name)) continue;
    const sid = String(raw.id);
    clothes.set(sid, raw);
    const list = byType.get(raw.type);
    if (list) list.push(sid);
    else byType.set(raw.type, [sid]);
  }

  const suitIds = new Set<string>();
  for (const suit of catalog.suits) {
    for (const id of suit.items) suitIds.add(String(id));
  }

  const conflicts = new Map<string, Set<string>>();
  const addConflict = (left: string, right: string) => {
    if (left === right) return;
    let set = conflicts.get(left);
    if (!set) {
      set = new Set<string>();
      conflicts.set(left, set);
    }
    set.add(right);
  };

  for (const [sid, raw] of clothes) {
    for (const eid of raw.excludes ?? []) {
      const esid = String(eid);
      if (clothes.has(esid)) {
        addConflict(sid, esid);
        addConflict(esid, sid);
      }
    }
    for (const et of raw.excludesTypes ?? []) {
      for (const esid of byType.get(et) ?? []) {
        addConflict(sid, esid);
        addConflict(esid, sid);
      }
    }
  }

  const index: CatalogDominanceIndex = { clothes, byType, suitIds, conflicts };
  INDEX_CACHE.set(catalog, index);
  return index;
}

function isSubset<T>(subset: Set<T> | undefined, superset: Set<T> | undefined): boolean {
  if (!subset || subset.size === 0) return true;
  if (!superset || subset.size > superset.size) return false;
  for (const value of subset) {
    if (!superset.has(value)) return false;
  }
  return true;
}

function dominates(
  a: ScoreItem,
  b: ScoreItem,
  aId: string,
  bId: string,
  conflicts: Map<string, Set<string>>,
): boolean {
  if (aId === bId || a.type !== b.type || a.major !== b.major) return false;
  for (let i = 0; i < 5; i++) {
    if ((a.props[i] ?? 0) < (b.props[i] ?? 0)) return false;
  }
  for (const tag of b.tags) {
    if (!a.tags.includes(tag)) return false;
  }
  const aConf = conflicts.get(aId);
  const bConf = conflicts.get(bId);
  if (!isSubset(aConf, bConf)) return false;

  const propBetter = a.props.some((value, idx) => value > (b.props[idx] ?? 0));
  const tagBetter = a.tags.length > b.tags.length;
  const confBetter = (aConf?.size ?? 0) < (bConf?.size ?? 0);
  return propBetter || tagBetter || confBetter;
}

/**
 * Identify standalone clothing score IDs in `candidateIds` that are strictly
 * dominated by at least one piece in `availableIds` under all styling battle rules.
 */
export function findDominatedStandaloneItems(
  catalog: ScoringCatalog,
  candidateIds: Iterable<string>,
  availableIds: Set<string>,
): Map<string, DominatedItemInfo> {
  const { clothes, suitIds, conflicts } = getCatalogIndex(catalog);
  const availByType = new Map<number, ScoreItem[]>();
  for (const sid of availableIds) {
    const item = clothes.get(String(sid));
    if (!item) continue;
    const list = availByType.get(item.type);
    if (list) list.push(item);
    else availByType.set(item.type, [item]);
  }
  for (const list of availByType.values()) {
    list.sort(
      (left, right) =>
        right.quality - left.quality ||
        right.props.reduce((s, v) => s + v, 0) - left.props.reduce((s, v) => s + v, 0),
    );
  }

  const dominated = new Map<string, DominatedItemInfo>();
  for (const rawId of candidateIds) {
    const sid = String(rawId);
    if (suitIds.has(sid) || dominated.has(sid)) continue;
    const item = clothes.get(sid);
    if (!item) continue;
    for (const candidate of availByType.get(item.type) ?? []) {
      const aId = String(candidate.id);
      if (dominates(candidate, item, aId, sid, conflicts)) {
        dominated.set(sid, {
          dominatorId: aId,
          dominatorName: candidate.name,
          dominatorQuality: candidate.quality,
        });
        break;
      }
    }
  }
  return dominated;
}
