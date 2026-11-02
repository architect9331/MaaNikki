import type { NotebookPull, ResonanceHistory } from './resonanceService';

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

/** Pulls spent on this copy; the counters reset at the previous copy of that rarity. */
const pityOf = (row: NotebookPull) => (row.rarity === 5 ? row.times_from_last_five_stars : row.times_from_last_four_stars) + 1;
const average = (values: number[]) => values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : 0;
const today = () => new Date().toISOString().slice(0, 10);

export function collectionOutfits(data: ResonanceHistory, bannerFilter: string) {
  const pulls = new Map<string, number[]>();
  const relevant = new Set<string>();
  const poolIds = Object.keys(data.history.notebook.pulls);
  for (const id of poolIds) {
    if (bannerFilter !== 'all' && bannerFilter !== id) continue;
    const notebook = Object.values(data.history.notebook.pulls[id] || {});
    if (!notebook.length) continue;
    data.banners.find((banner) => banner.id === id)?.outfits.forEach((outfit) => relevant.add(outfit));
    // Outfit statistics use notebook ordinals only; timestamp history is a
    // separate source and never changes these counts.
    for (const row of notebook) {
      const canonical = data.items[row.result]?.canonical_id || row.result;
      pulls.set(canonical, [...pulls.get(canonical) ?? [], pityOf(row)]);
      const outfit = data.items[row.result]?.outfit_id;
      if (outfit) relevant.add(outfit);
    }
  }
  return [...relevant].flatMap((id) => {
    const outfit = data.outfits[id];
    if (!outfit?.items.length) return [];
    const parts = outfit.items.map((item) => {
      const spent = pulls.get(item) ?? [];
      return { item, name: data.items[item]?.name || '物品 ' + item, count: spent.length, pity: spent };
    });
    const obtained = parts.filter((part) => part.count > 0).length;
    const copies = Math.min(...parts.map((part) => part.count));
    const spent = parts.flatMap((part) => part.pity);
    const banner = data.banners.find((entry) => entry.outfits.includes(id));
    return [{ id, ...outfit, parts, obtained, copies, banner: banner?.name ?? '', spent: spent.reduce((sum, value) => sum + value, 0), average: average(spent) }];
  }).sort((a, b) => {
    // Unfinished outfits lead, closest to completion first; finished ones follow.
    const done = (outfit: { obtained: number; items: string[] }) => Number(outfit.obtained === outfit.items.length);
    return done(a) - done(b) || b.rarity - a.rarity || b.obtained / b.items.length - a.obtained / a.items.length || a.name.localeCompare(b.name, 'zh-CN');
  });
}

/** Account totals. Pulls after a pool's last 4★+ record are invisible, so totals are lower bounds. */
export function resonanceSummary(data: ResonanceHistory) {
  const kinds = new Map(data.banners.map((banner) => [banner.id, banner.kind]));
  const groups = { limitedFive: [] as number[], limitedFour: [] as number[], standardFive: [] as number[], standardFour: [] as number[] };
  const distribution = Array.from({ length: 20 }, (_, index) => ({ pulls: index + 1, count: 0 }));
  let total = 0;
  for (const [id, pool] of Object.entries(data.history.notebook.pulls)) {
    const rows = Object.values(pool);
    if (!rows.length) continue;
    total += Math.max(...rows.map((row) => row.pool_cnt)) + 1;
    const standard = kinds.get(id) === 1;
    for (const row of rows) {
      const pity = pityOf(row);
      if (row.rarity === 5) {
        (standard ? groups.standardFive : groups.limitedFive).push(pity);
        if (distribution[pity - 1]) distribution[pity - 1].count++;
      } else (standard ? groups.standardFour : groups.limitedFour).push(pity);
    }
  }
  const fives = [...groups.limitedFive, ...groups.standardFive];
  return {
    total, distribution,
    limitedFive: { count: groups.limitedFive.length, average: average(groups.limitedFive) },
    limitedFour: { count: groups.limitedFour.length, average: average(groups.limitedFour) },
    standardFive: { count: groups.standardFive.length, average: average(groups.standardFive) },
    standardFour: groups.standardFour.length,
    fiveAverage: average(fives),
    fourAverage: average([...groups.limitedFour, ...groups.standardFour]),
    earlyShare: fives.length ? fives.filter((value) => value <= 10).length / fives.length : 0,
    hardShare: fives.length ? fives.filter((value) => value >= 18).length / fives.length : 0,
  };
}

/** Banners open today, with what is left to collect at the account's own pace. */
export function activeBanners(data: ResonanceHistory, outfits: ReturnType<typeof collectionOutfits>) {
  const now = today();
  const summary = resonanceSummary(data);
  return data.banners.flatMap((banner) => {
    const run = banner.runs?.find((entry) => entry.start <= now && now <= entry.end);
    if (!run) return [];
    const rows = Object.values(data.history.notebook.pulls[banner.id] ?? {}).sort((a, b) => a.pool_cnt - b.pool_cnt);
    const last = rows[rows.length - 1];
    const lastFive = [...rows].reverse().find((row) => row.rarity === 5);
    const since = last ? last.pool_cnt - (lastFive?.pool_cnt ?? -1) : 0;
    const owned = outfits.filter((outfit) => banner.outfits.includes(outfit.id));
    const remaining = owned.flatMap((outfit) => outfit.parts.filter((part) => !part.count).map(() => outfit.rarity));
    const need = remaining.reduce((sum, rarity) => sum + (rarity === 5 ? summary.fiveAverage || 18 : summary.fourAverage || 8), 0);
    const left = Math.max(0, Math.ceil((Date.parse(run.end + 'T23:59:59') - Date.now()) / 86400000));
    return [{ id: banner.id, name: banner.name, left, since, missing: remaining.length, need: Math.round(need),
      outfits: owned.map((outfit) => outfit.name) }];
  });
}
