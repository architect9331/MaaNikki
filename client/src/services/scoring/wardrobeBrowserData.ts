import itemSourcesData from './itemSources.json';
import type { GameItem, OwnedProfile, ScoreItem, ScoringPayload } from './types';

export type SuitSortBy = 'quality' | 'release';
export type SuitSortOrder = 'desc' | 'asc';

export let ITEM_SOURCES: Record<string, string> = itemSourcesData.sources;
export let SKETCH_CHESTS = itemSourcesData.chests;

export function applyHotItemSources(
  overlay: { sources?: Record<string, string>; chests?: unknown } | null | undefined,
): void {
  if (overlay && overlay.sources && typeof overlay.sources === 'object') {
    ITEM_SOURCES = overlay.sources;
    if (overlay.chests && typeof overlay.chests === 'object') {
      SKETCH_CHESTS = overlay.chests as typeof itemSourcesData.chests;
    }
  } else {
    ITEM_SOURCES = itemSourcesData.sources;
    SKETCH_CHESTS = itemSourcesData.chests;
  }
}

export interface SuitPartEntry {
  id: string;
  gameId?: string;
  name: string;
  partName: string;
  quality: number;
  major: number;
  tags: number[];
  owned: boolean;
  groupId?: string;
  inferred?: string | null;
  source?: string;
}

export interface SuitVariantEntry {
  id: string;
  kind: 'base' | 'evo' | 'glow';
  stage: number;
  label: string;
  name: string;
  thumbKey: string;
  obtainedCount: number;
  totalCount: number;
  missingCount: number;
  complete: boolean;
  parts: SuitPartEntry[];
}

export interface SuitBrowserEntry {
  id: string;
  name: string;
  suitId?: number;
  releaseOrder: number;
  releaseSeason?: string;
  releaseDate?: string;
  quality: number;
  major: number;
  tags: number[];
  obtainedCount: number;
  totalCount: number;
  missingCount: number;
  complete: boolean;
  parts: SuitPartEntry[];
  variants: SuitVariantEntry[];
  evoTotal: number;
  evoUnlocked: number;
  evoComplete: boolean;
  hasGlow: boolean;
  glowUnlocked: boolean;
  glowObtainedCount: number;
  glowTotalCount: number;
}

export interface PieceBrowserEntry {
  id: string;
  name: string;
  partCode: number;
  partName: string;
  quality: number;
  major: number;
  tags: number[];
  props: number[];
  owned: boolean;
  groupId?: string;
  colorCount: number;
  inferred?: string | null;
  suitName?: string;
  source?: string;
}

export function sortSuitEntries(
  suits: SuitBrowserEntry[],
  sortBy: SuitSortBy,
  releaseOrderDir: SuitSortOrder = 'desc',
): SuitBrowserEntry[] {
  return [...suits].sort((a, b) => {
    const ratioDiff = b.obtainedCount / b.totalCount - a.obtainedCount / a.totalCount;
    if (sortBy === 'release') {
      if (releaseOrderDir === 'asc') {
        const aKnown = a.releaseOrder > 0;
        const bKnown = b.releaseOrder > 0;
        if (aKnown !== bKnown) return aKnown ? -1 : 1;
        return (
          a.releaseOrder - b.releaseOrder
          || b.quality - a.quality
          || ratioDiff
          || a.name.localeCompare(b.name, 'zh-CN')
        );
      }
      return (
        b.releaseOrder - a.releaseOrder
        || b.quality - a.quality
        || ratioDiff
        || a.name.localeCompare(b.name, 'zh-CN')
      );
    }
    return (
      b.quality - a.quality
      || ratioDiff
      || b.releaseOrder - a.releaseOrder
      || a.name.localeCompare(b.name, 'zh-CN')
    );
  });
}

export function buildSuitEntries(payload: ScoringPayload, profile: OwnedProfile): SuitBrowserEntry[] {
  const { catalog } = payload;
  const ownedByScoreId = new Map<string, { group?: string; refreshReward?: string }>();
  for (const item of profile.items) {
    if (!ownedByScoreId.has(item.id)) {
      ownedByScoreId.set(item.id, { group: item.group, refreshReward: item.refreshReward });
    }
  }
  const fallbackGameItem = new Map<string, GameItem>();
  for (const gameItem of Object.values(catalog.game_items ?? {})) {
    const key = String(gameItem.score_id);
    const prev = fallbackGameItem.get(key);
    if (!prev || gameItem.web_id === gameItem.group) {
      fallbackGameItem.set(key, gameItem);
    }
  }

  const entries = catalog.suits.map((suit) => {
    const validIds = suit.items.filter((id) => catalog.items[id] && catalog.items[id].type < 200);
    const parts: SuitPartEntry[] = validIds.map((id) => {
      const record = catalog.items[id];
      const owned = ownedByScoreId.get(String(id));
      const needsFallback = record.quality <= 0 || /^\d+$/.test(record.name);
      const fallback = needsFallback ? fallbackGameItem.get(String(id)) : undefined;
      return {
        id: String(id),
        name: fallback?.name ?? record.name,
        partName: (catalog.part_names[record.type] ?? String(record.type)).split(' / ').pop() ?? '',
        quality: fallback?.quality ?? record.quality,
        major: record.major,
        tags: record.visibleTags ?? record.tags ?? [],
        owned: !!owned,
        groupId: owned?.group,
        inferred: owned?.refreshReward,
        source: ITEM_SOURCES[String(id)],
      };
    });
    const obtainedCount = parts.filter((p) => p.owned).length;
    const quality = parts.reduce((max, p) => Math.max(max, p.quality), 3);
    const majorCounts = [0, 0, 0, 0, 0];
    const tagSet = new Set<number>();
    for (const p of parts) {
      if (p.major >= 0 && p.major < 5) majorCounts[p.major]++;
      for (const t of p.tags) tagSet.add(t);
    }
    const major = majorCounts.indexOf(Math.max(...majorCounts));
    const baseComplete = parts.length > 0 && obtainedCount === parts.length;

    const baseOrder = new Map<string, number>();
    validIds.forEach((id, idx) => baseOrder.set(String(id), idx));

    const catalogVariants = suit.variants ?? [];
    const evoCatalogCount = catalogVariants.filter((v) => v.kind === 'evo').length;
    const builtExtraVariants: SuitVariantEntry[] = [];
    for (const v of catalogVariants) {
      const vParts: SuitPartEntry[] = [];
      for (const rawGid of v.game_items ?? []) {
        const gid = String(rawGid);
        const gameItem = catalog.game_items?.[gid];
        if (!gameItem) continue;
        const record = catalog.items[gameItem.score_id];
        if (!record || record.type >= 200) continue;
        const owned = profile.ownedGameIds.has(gid);
        const ownedMeta = ownedByScoreId.get(String(gameItem.score_id));
        vParts.push({
          id: String(gameItem.score_id),
          gameId: gid,
          name: gameItem.name || record.name,
          partName: (catalog.part_names[record.type] ?? String(record.type)).split(' / ').pop() ?? '',
          quality: gameItem.quality || record.quality,
          major: record.major,
          tags: record.visibleTags ?? record.tags ?? [],
          owned,
          groupId: owned ? (ownedMeta?.group ?? gameItem.group) : undefined,
          inferred: owned ? ownedMeta?.refreshReward : undefined,
          source: ITEM_SOURCES[String(gameItem.score_id)],
        });
      }
      if (!vParts.length) continue;
      vParts.sort((a, b) => {
        const ia = baseOrder.get(a.id) ?? (1000 + (catalog.items[a.id]?.type ?? 999));
        const ib = baseOrder.get(b.id) ?? (1000 + (catalog.items[b.id]?.type ?? 999));
        return ia - ib;
      });
      const vObtained = vParts.filter((p) => p.owned).length;
      const label = v.kind === 'glow'
        ? '焕新'
        : evoCatalogCount <= 1
          ? '进化'
          : `${v.stage}进`;
      builtExtraVariants.push({
        id: v.id,
        kind: v.kind,
        stage: v.stage,
        label,
        name: v.name,
        thumbKey: v.id,
        obtainedCount: vObtained,
        totalCount: vParts.length,
        missingCount: vParts.length - vObtained,
        complete: vObtained === vParts.length,
        parts: vParts,
      });
    }

    const variants: SuitVariantEntry[] = builtExtraVariants.length > 0
      ? [
        {
          id: `${suit.id}_base`,
          kind: 'base',
          stage: 0,
          label: '原色',
          name: suit.name,
          thumbKey: suit.id,
          obtainedCount,
          totalCount: parts.length,
          missingCount: parts.length - obtainedCount,
          complete: baseComplete,
          parts,
        },
        ...builtExtraVariants,
      ]
      : [];

    const evoVariants = builtExtraVariants.filter((v) => v.kind === 'evo');
    const glowVariant = builtExtraVariants.find((v) => v.kind === 'glow');
    const evoTotal = evoVariants.length;
    const evoUnlocked = evoVariants.filter((v) => v.complete).length;

    return {
      id: suit.id,
      name: suit.name,
      suitId: suit.suit_id,
      releaseOrder: suit.release_order ?? 0,
      releaseSeason: suit.release_season,
      releaseDate: suit.release_date,
      quality,
      major,
      tags: [...tagSet],
      obtainedCount,
      totalCount: parts.length,
      missingCount: parts.length - obtainedCount,
      complete: baseComplete,
      parts,
      variants,
      evoTotal,
      evoUnlocked,
      evoComplete: evoTotal > 0 && evoUnlocked === evoTotal,
      hasGlow: Boolean(glowVariant),
      glowUnlocked: Boolean(glowVariant?.complete),
      glowObtainedCount: glowVariant?.obtainedCount ?? 0,
      glowTotalCount: glowVariant?.totalCount ?? 0,
    };
  }).filter((s) => s.totalCount > 0);

  return sortSuitEntries(entries, 'quality', 'desc');
}

export function buildPieceEntries(payload: ScoringPayload, profile: OwnedProfile): PieceBrowserEntry[] {
  const { catalog } = payload;
  const ownedByScoreId = new Map<string, { group?: string; refreshReward?: string }>();
  for (const item of profile.items) {
    if (!ownedByScoreId.has(item.id)) {
      ownedByScoreId.set(item.id, { group: item.group, refreshReward: item.refreshReward });
    }
  }
  const groupColors = new Map(profile.groups.map((g) => [g.id, g.owned]));
  const suitByItemId = new Map<number, string>();
  for (const suit of catalog.suits) {
    for (const itemId of suit.items) suitByItemId.set(itemId, suit.name);
  }

  return Object.values(catalog.items)
    .filter((item: ScoreItem) => item.type < 200 && item.quality >= 2 && !/^\d+$/.test(item.name))
    .map((item) => {
      const owned = ownedByScoreId.get(String(item.id));
      const groupId = owned?.group;
      return {
        id: String(item.id),
        name: item.name,
        partCode: item.type,
        partName: catalog.part_names[item.type] ?? String(item.type),
        quality: item.quality,
        major: item.major,
        tags: item.visibleTags ?? item.tags ?? [],
        props: item.props,
        owned: !!owned,
        groupId,
        colorCount: groupId ? (groupColors.get(groupId) ?? 1) : 0,
        inferred: owned?.refreshReward,
        suitName: suitByItemId.get(item.id),
        source: ITEM_SOURCES[String(item.id)],
      };
    })
    .sort((a, b) => Number(b.owned) - Number(a.owned) || b.quality - a.quality || a.partCode - b.partCode);
}
