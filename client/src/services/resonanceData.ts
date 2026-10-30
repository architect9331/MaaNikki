import type { ResonanceHistory } from './resonanceService';

export interface HistoryRow {
  key: string; bannerId: string; item: string; name: string; rarity: number;
  time: string;
}

export function historyRows(data: ResonanceHistory): HistoryRow[] {
  const date = (value: string) => /^[0-9]+$/.test(value)
    ? Number(value) * (value.length > 10 ? 1 : 1000) : Date.parse(value.replace(' ', 'T'));
  return Object.entries(data.history.banners).flatMap(([bannerId, entry]) =>
    entry.pulls.map(([time, item], index) => ({
      key: bannerId + ':' + index, bannerId, item,
      name: data.items[item]?.name || '物品 ' + item, rarity: data.items[item]?.rarity || 0,
      time,
    }))).sort((a, b) => date(b.time) - date(a.time));
}

export function collectionOutfits(data: ResonanceHistory, bannerFilter: string) {
  const counts = new Map<string, number>();
  const relevant = new Set<string>();
  const poolIds = Object.keys(data.history.notebook.pulls);
  for (const id of poolIds) {
    if (bannerFilter !== 'all' && bannerFilter !== id) continue;
    const notebook = Object.values(data.history.notebook.pulls[id] || {});
    if (!notebook.length) continue;
    data.banners.find((banner) => banner.id === id)?.outfits.forEach((outfit) => relevant.add(outfit));
    const count = (items: string[]) => {
      const result = new Map<string, number>();
      for (const item of items) {
        const canonical = data.items[item]?.canonical_id || item;
        result.set(canonical, (result.get(canonical) || 0) + 1);
        const outfit = data.items[item]?.outfit_id;
        if (outfit) relevant.add(outfit);
      }
      return result;
    };
    // Outfit statistics use notebook ordinals only; timestamp history is a
    // separate source and never changes these counts.
    for (const [item, countInPool] of count(notebook.map((row) => row.result))) {
      counts.set(item, (counts.get(item) || 0) + countInPool);
    }
  }
  return [...relevant].flatMap((id) => {
    const outfit = data.outfits[id];
    if (!outfit?.items.length) return [];
    const parts = outfit.items.map((item) => ({ item, name: data.items[item]?.name || '物品 ' + item, count: counts.get(item) || 0 }));
    const obtained = parts.filter((part) => part.count > 0).length;
    const copies = Math.min(...parts.map((part) => part.count));
    return [{ id, ...outfit, parts, obtained, copies }];
  }).sort((a, b) => b.rarity - a.rarity || b.obtained / b.items.length - a.obtained / a.items.length || a.name.localeCompare(b.name, 'zh-CN'));
}
