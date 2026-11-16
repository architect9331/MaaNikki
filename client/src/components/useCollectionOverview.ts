import { useMemo } from 'react';
import { useAccountStateStore } from '@/stores/accountStateStore';
import { buildOwnedProfile } from '@/services/scoring/profile';
import { findDominatedStandaloneItems, normalizePlanMode } from '@/services/scoring/dominance';
import { buildRenewalGroups, MAX_LEVEL, RENEWED_LEVEL } from '@/services/scoring/materialPlanning';
import { buildSuitEntries } from '@/services/scoring/wardrobeBrowserData';
import { useWardrobePlanStore } from '@/services/scoring/wardrobePlanStore';
import bagCatalogRaw from '@/data/bagCatalog.json';
import { collectionKey, collectionSlots, getBlessingProgress } from '@/utils/blessingProgress';

export interface Progress { done: number; total: number }
const BAG_CATALOG_TOTAL = Object.keys(bagCatalogRaw as Record<string, unknown>).length;

/**
 * Where each collection area stands, for the tabs of 收集进度. The figures
 * match the ones each tab shows; null means there is no record to count yet.
 */
export function useCollectionOverview(): Record<'closet' | 'crafting' | 'renewal' | 'blessing' | 'backpack', Progress | null> {
  const account = useAccountStateStore((state) => state.data);
  const payload = useWardrobePlanStore((state) => state.payload);
  const settings = useWardrobePlanStore((state) => state.settings);
  const materials = useWardrobePlanStore((state) => state.materials);
  const scope = materials.crafting_scope ?? 'all';

  const craftingMode = normalizePlanMode(materials.crafting_plan?.mode);
  const renewalMode = normalizePlanMode(materials.renewal_plan?.mode);
  const blessingMode = normalizePlanMode(materials.blessing_plan?.mode);

  const profile = useMemo(() => payload?.snapshot ? buildOwnedProfile(payload, settings) : null, [payload, settings]);
  const closet = useMemo(() => {
    if (!payload || !profile) return null;
    const suits = buildSuitEntries(payload, profile);
    return { done: suits.filter((suit) => suit.complete).length, total: suits.length };
  }, [payload, profile]);
  const crafting = useMemo(() => {
    const sketches = payload?.crafting_catalog?.sketches;
    if (!sketches || !profile || !payload) return null;
    const owned = new Set(profile.items.map((item) => item.id));
    if (craftingMode === 'power') {
      const dominated = findDominatedStandaloneItems(
        payload.catalog,
        sketches.filter((sketch) => !owned.has(sketch.id)).map((sketch) => sketch.id),
        new Set([...owned, ...sketches.map((sketch) => sketch.id)]),
      );
      const scoped = sketches.filter((sketch) => owned.has(sketch.id) || !dominated.has(sketch.id));
      return { done: scoped.filter((sketch) => owned.has(sketch.id)).length, total: scoped.length };
    }
    const scoped = sketches.filter((sketch) => scope === 'all' || sketch.category === 'suit');
    return { done: scoped.filter((sketch) => owned.has(sketch.id)).length, total: scoped.length };
  }, [payload, profile, scope, craftingMode]);
  const renewal = useMemo(() => {
    if (!payload || !profile) return null;
    const groups = buildRenewalGroups(payload.catalog, profile);
    const active =
      renewalMode === 'power'
        ? (() => {
            const ownedScoreIds = new Set(groups.map((group) => group.scoreId));
            const dominated = findDominatedStandaloneItems(payload.catalog, ownedScoreIds, ownedScoreIds);
            return groups.filter((group) => !dominated.has(group.scoreId));
          })()
        : groups;
    return {
      done: active.filter((group) => (group.level ?? 0) >= (group.renewable ? RENEWED_LEVEL : MAX_LEVEL)).length,
      total: active.length,
    };
  }, [payload, profile, renewalMode]);
  const blessing = useMemo(() => {
    const progress = getBlessingProgress(account);
    if (!progress.known) return null;
    if (blessingMode === 'power') {
      const done = progress.sets.reduce(
        (sum, set) =>
          sum +
          collectionSlots.filter((slot) =>
            set.colors.some((color) => progress.owned.has(collectionKey({ name: set.name, slot, color }))),
          ).length,
        0,
      );
      return { done, total: progress.sets.length * collectionSlots.length };
    }
    return { done: progress.owned.size, total: progress.target };
  }, [account, blessingMode]);
  const backpack = useMemo(() => {
    const inv = materials.inventory ?? {};
    const keys = Object.keys(inv);
    if (!keys.length && !materials.backpack_summary) return null;
    const stocked = Object.values(inv).filter((v) => Number(v) > 0).length;
    return { done: stocked, total: Math.max(BAG_CATALOG_TOTAL, stocked) };
  }, [materials.inventory, materials.backpack_summary]);

  return { closet, crafting, renewal, blessing, backpack };
}
