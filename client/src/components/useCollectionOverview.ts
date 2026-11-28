import { useMemo } from 'react';
import { useAccountStateStore } from '@/stores/accountStateStore';
import { buildOwnedProfile } from '@/services/scoring/profile';
import {
  findDominatedStandaloneItems,
  matchesRenewalScope,
  normalizePlanMode,
  normalizeRenewalScope,
} from '@/services/scoring/dominance';
import { buildRenewalGroups, MAX_LEVEL, RENEWED_LEVEL } from '@/services/scoring/materialPlanning';
import { buildSuitEntries } from '@/services/scoring/wardrobeBrowserData';
import { useWardrobePlanStore } from '@/services/scoring/wardrobePlanStore';
import {
  blessingSourceRegion,
  isAllRegionsEnabled,
  normalizeRegionConfig,
  sketchRegion,
} from '@/services/collectionRegions';
import bagCatalogRaw from '@/data/bagCatalog.json';
import { collectionKey, collectionSlots, getBlessingProgress } from '@/utils/blessingProgress';

export interface Progress { done: number; total: number }
const BAG_CATALOG_TOTAL = Object.keys(bagCatalogRaw as Record<string, unknown>).length;

/**
 * Where each collection area stands, for the tabs of 收集进度. The figures
 * match the ones each tab shows; null means there is no record to count yet.
 */
export function useCollectionOverview(): Record<'closet' | 'crafting' | 'renewal' | 'blessing' | 'backpack' | 'heart', Progress | null> {
  const account = useAccountStateStore((state) => state.data);
  const payload = useWardrobePlanStore((state) => state.payload);
  const settings = useWardrobePlanStore((state) => state.settings);
  const materials = useWardrobePlanStore((state) => state.materials);
  const scope = materials.crafting_scope ?? 'all';

  const craftingMode = normalizePlanMode(materials.crafting_plan?.mode);
  const renewalMode = normalizePlanMode(materials.renewal_plan?.mode);
  const renewalScope = normalizeRenewalScope(materials.renewal_scope);
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
    const regionCfg = normalizeRegionConfig(materials.crafting_regions);
    const enabledRegions = new Set(regionCfg.enabled);
    const allRegions = isAllRegionsEnabled(regionCfg);
    const matDefs = payload.crafting_catalog?.materials;
    const scoped = sketches.filter((sketch) => {
      if (scope !== 'all' && sketch.category !== 'suit') return false;
      if (craftingMode !== 'selected' && !allRegions) {
        const reg = sketchRegion(sketch, matDefs);
        if (!reg || !enabledRegions.has(reg)) return false;
      }
      return true;
    });
    if (craftingMode === 'power' && scope !== 'suits') {
      const dominated = findDominatedStandaloneItems(
        payload.catalog,
        scoped.filter((sketch) => !owned.has(sketch.id)).map((sketch) => sketch.id),
        new Set([...owned, ...sketches.map((sketch) => sketch.id)]),
      );
      const active = scoped.filter((sketch) => owned.has(sketch.id) || !dominated.has(sketch.id));
      return { done: active.filter((sketch) => owned.has(sketch.id)).length, total: active.length };
    }
    return { done: scoped.filter((sketch) => owned.has(sketch.id)).length, total: scoped.length };
  }, [payload, profile, scope, craftingMode, materials.crafting_regions]);
  const renewal = useMemo(() => {
    if (!payload || !profile) return null;
    const groups = buildRenewalGroups(payload.catalog, profile);
    const regionCfg = normalizeRegionConfig(materials.renewal_regions);
    const enabledRegions = new Set(regionCfg.enabled);
    const allRegions = isAllRegionsEnabled(regionCfg);
    const ownedScoreIds =
      renewalMode === 'power' ? new Set(groups.map((group) => group.scoreId)) : null;
    const dominated =
      ownedScoreIds
        ? findDominatedStandaloneItems(payload.catalog, ownedScoreIds, ownedScoreIds)
        : null;
    const active =
      renewalMode === 'selected'
        ? groups
        : groups.filter(
            (group) =>
              !(renewalMode === 'power' && dominated?.has(group.scoreId)) &&
              matchesRenewalScope(group.quality, Boolean(group.suitName), renewalScope) &&
              (allRegions || (group.region !== null && enabledRegions.has(group.region))),
          );
    return {
      done: active.filter((group) => (group.level ?? 0) >= (group.renewable ? RENEWED_LEVEL : MAX_LEVEL)).length,
      total: active.length,
    };
  }, [payload, profile, renewalMode, renewalScope, materials.renewal_regions]);
  const blessing = useMemo(() => {
    const progress = getBlessingProgress(account);
    if (!progress.known) return null;
    const regionCfg = normalizeRegionConfig(materials.blessing_regions);
    const enabledRegions = new Set(regionCfg.enabled);
    const allRegions = isAllRegionsEnabled(regionCfg);
    const activeSets =
      blessingMode === 'selected' || allRegions
        ? progress.sets
        : progress.sets.filter((set) => {
            const reg = blessingSourceRegion(set.source);
            return reg !== null && enabledRegions.has(reg);
          });
    if (blessingMode === 'power') {
      const done = activeSets.reduce(
        (sum, set) =>
          sum +
          collectionSlots.filter((slot) =>
            set.colors.some((color) => progress.owned.has(collectionKey({ name: set.name, slot, color }))),
          ).length,
        0,
      );
      return { done, total: activeSets.length * collectionSlots.length };
    }
    const done = activeSets.reduce(
      (sum, set) =>
        sum +
        collectionSlots.reduce(
          (slotSum, slot) =>
            slotSum + set.colors.filter((color) => progress.owned.has(collectionKey({ name: set.name, slot, color }))).length,
          0,
        ),
      0,
    );
    const total = activeSets.reduce((sum, set) => sum + set.colors.length * collectionSlots.length, 0);
    return { done, total };
  }, [account, blessingMode, materials.blessing_regions]);
  const backpack = useMemo(() => {
    const inv = materials.inventory ?? {};
    const keys = Object.keys(inv);
    if (!keys.length && !materials.backpack_summary) return null;
    const stocked = Object.values(inv).filter((v) => Number(v) > 0).length;
    return { done: stocked, total: Math.max(BAG_CATALOG_TOTAL, stocked) };
  }, [materials.inventory, materials.backpack_summary]);

  const heart = useMemo(() => {
    const scan = account?.infinite_heart?.last_scan;
    const overrides = account?.infinite_heart?.overrides ?? {};
    const scannedNodes = scan?.nodes ?? {};
    const ids = new Set([...Object.keys(scannedNodes), ...Object.keys(overrides)]);
    if (!ids.size) return null;
    let done = 0;
    for (const id of ids) {
      const ov = overrides[id];
      if (ov ? ov.state === 'unlocked' : scannedNodes[id]?.state === 'unlocked') done++;
    }
    return { done, total: Math.max(383, ids.size) };
  }, [account?.infinite_heart]);

  return { closet, crafting, renewal, blessing, backpack, heart };
}
