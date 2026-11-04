import itemSourcesData from './itemSources.json';
import type { OwnedProfile, ScoreItem, ScoringPayload } from './types';

const ITEM_SOURCES: Record<string, string> = itemSourcesData.sources;
export const SKETCH_CHESTS = itemSourcesData.chests;

export interface SuitPartEntry {
  id: string;
  name: string;
  partName: string;
  quality: number;
  major: number;
  owned: boolean;
  groupId?: string;
  inferred?: string | null;
  source?: string;
}

export interface SuitBrowserEntry {
  id: string;
  name: string;
  quality: number;
  major: number;
  obtainedCount: number;
  totalCount: number;
  missingCount: number;
  complete: boolean;
  parts: SuitPartEntry[];
}

export interface PieceBrowserEntry {
  id: string;
  name: string;
  partCode: number;
  partName: string;
  quality: number;
  major: number;
  props: number[];
  owned: boolean;
  groupId?: string;
  colorCount: number;
  inferred?: string | null;
  suitName?: string;
  source?: string;
}

export function buildSuitEntries(payload: ScoringPayload, profile: OwnedProfile): SuitBrowserEntry[] {
  const { catalog } = payload;
  const ownedByScoreId = new Map<string, { group?: string; refreshReward?: string }>();
  for (const item of profile.items) {
    if (!ownedByScoreId.has(item.id)) {
      ownedByScoreId.set(item.id, { group: item.group, refreshReward: item.refreshReward });
    }
  }

  return catalog.suits.map((suit) => {
    const validIds = suit.items.filter((id) => catalog.items[id] && catalog.items[id].type < 200);
    const parts: SuitPartEntry[] = validIds.map((id) => {
      const record = catalog.items[id];
      const owned = ownedByScoreId.get(String(id));
      return {
        id: String(id),
        name: record.name,
        partName: (catalog.part_names[record.type] ?? String(record.type)).split(' / ').pop() ?? '',
        quality: record.quality,
        major: record.major,
        owned: !!owned,
        groupId: owned?.group,
        inferred: owned?.refreshReward,
        source: ITEM_SOURCES[String(id)],
      };
    });
    const obtainedCount = parts.filter((p) => p.owned).length;
    const quality = parts.reduce((max, p) => Math.max(max, p.quality), 3);
    const majorCounts = [0, 0, 0, 0, 0];
    for (const p of parts) {
      if (p.major >= 0 && p.major < 5) majorCounts[p.major]++;
    }
    const major = majorCounts.indexOf(Math.max(...majorCounts));
    return {
      id: suit.id,
      name: suit.name,
      quality,
      major,
      obtainedCount,
      totalCount: parts.length,
      missingCount: parts.length - obtainedCount,
      complete: parts.length > 0 && obtainedCount === parts.length,
      parts,
    };
  }).filter((s) => s.totalCount > 0).sort((a, b) => b.quality - a.quality || b.obtainedCount / b.totalCount - a.obtainedCount / a.totalCount);
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
