import { useMemo } from 'react';
import { useAccountStateStore } from '@/stores/accountStateStore';
import { buildOwnedProfile } from '@/services/scoring/profile';
import { buildRenewalGroups, MAX_LEVEL, RENEWED_LEVEL } from '@/services/scoring/materialPlanning';
import { buildSuitEntries } from '@/services/scoring/wardrobeBrowserData';
import { useWardrobePlanStore } from '@/services/scoring/wardrobePlanStore';
import bagCatalogRaw from '@/data/bagCatalog.json';
import { getBlessingProgress } from '@/utils/blessingProgress';

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

  const profile = useMemo(() => payload?.snapshot ? buildOwnedProfile(payload, settings) : null, [payload, settings]);
  const closet = useMemo(() => {
    if (!payload || !profile) return null;
    const suits = buildSuitEntries(payload, profile);
    return { done: suits.filter((suit) => suit.complete).length, total: suits.length };
  }, [payload, profile]);
  const crafting = useMemo(() => {
    const sketches = payload?.crafting_catalog?.sketches;
    if (!sketches || !profile) return null;
    const owned = new Set(profile.items.map((item) => item.id));
    const scoped = sketches.filter((sketch) => scope === 'all' || sketch.category === 'suit');
    return { done: scoped.filter((sketch) => owned.has(sketch.id)).length, total: scoped.length };
  }, [payload, profile, scope]);
  const renewal = useMemo(() => {
    if (!payload || !profile) return null;
    const groups = buildRenewalGroups(payload.catalog, profile);
    return {
      done: groups.filter((group) => (group.level ?? 0) >= (group.renewable ? RENEWED_LEVEL : MAX_LEVEL)).length,
      total: groups.length,
    };
  }, [payload, profile]);
  const blessing = useMemo(() => {
    const progress = getBlessingProgress(account);
    return progress.known ? { done: progress.owned.size, total: progress.target } : null;
  }, [account]);
  const backpack = useMemo(() => {
    const inv = materials.inventory ?? {};
    const keys = Object.keys(inv);
    if (!keys.length && !materials.backpack_summary) return null;
    const stocked = Object.values(inv).filter((v) => Number(v) > 0).length;
    return { done: stocked, total: Math.max(BAG_CATALOG_TOTAL, stocked) };
  }, [materials.inventory, materials.backpack_summary]);

  return { closet, crafting, renewal, blessing, backpack };
}
