import type { GameItem, OwnedItem, OwnedProfile, ScoringPayload, ScoringSettings } from './types';

const normalize = (name: string) => name.replaceAll('・', '·');

/** Only explicit ID links and equal candidate meanings may establish ownership. */
export function buildOwnedProfile(payload: ScoringPayload, settings: ScoringSettings): OwnedProfile {
  const result: OwnedProfile = { items: [], groups: [], aligned: 0, skipped: [], cosmetics: 0 };
  const { catalog } = payload;
  const groups = new Map<string, { entry: GameItem; members: Map<string, GameItem>; reward: string | null }>();
  for (const [part, state] of Object.entries(payload.snapshot?.parts ?? {})) {
    for (const row of state.items ?? []) {
      if (['281', '282', '283', '284', '285', '286'].includes(part)) { result.cosmetics++; continue; }
      let reason = '';
      const entries = row.item_ids.map((id) => catalog.game_items[String(id)]);
      const meanings = new Set(entries.filter(Boolean).map((entry) => `${entry.score_id}:${entry.group}:${entry.glow_reward}`));
      if (row.source === 'selected_card_unconfirmed') reason = '选中格名称待确认';
      else if (!entries.length || entries.some((entry) => !entry)) reason = '计分资料待补充';
      else if (meanings.size !== 1) reason = '同图身份待确认';
      if (reason) {
        result.skipped.push({ name: row.names[0] || row.item_ids.join(' / '), part: state.name, reason });
        continue;
      }
      result.aligned++;
      const entry = entries[0];
      let group = groups.get(entry.group);
      if (!group) {
        group = { entry: catalog.game_items[entry.group] ?? entry, members: new Map(), reward: null };
        groups.set(entry.group, group);
      }
      group.members.set(String(row.item_ids[0]), entry);
      if (entry.glow_reward) group.reward = entry.name;
    }
  }
  // Multiple colours of one score identity are equivalent choices. Keep the
  // actual owned name, and retain a single wardrobe candidate for that group.
  const seen = new Set<string>();
  for (const [id, group] of groups) {
    const manual = settings.levels[id];
    // Obtaining a refresh reward proves the group is already refreshed, even
    // if a manually saved lower level predates that acquisition.
    const level = group.reward ? 11 : Number.isInteger(manual) && manual >= 0 && manual <= 11 ? manual : null;
    result.groups.push({ id, name: group.entry.name, part: catalog.part_names[catalog.items[group.entry.score_id].type],
      owned: group.members.size, inferred: group.reward, level });
    for (const [gameId, entry] of group.members) {
      const key = `${entry.score_id}:${id}`;
      if (seen.has(key)) continue;
      seen.add(key);
      result.items.push({ id: entry.score_id, gameId, group: id, name: entry.name, level,
        refreshReward: group.reward ?? undefined,
        evidence: group.reward ? `焕新赠送款：${group.reward}` : manual !== undefined ? '手动设置' : undefined });
    }
  }
  const flashByName = new Map<string, string[]>();
  for (const item of Object.values(catalog.items)) {
    if (item.type < 300) continue;
    const name = normalize(item.name);
    flashByName.set(name, [...flashByName.get(name) ?? [], String(item.id)]);
  }
  const flashes = new Map<string, OwnedItem>();
  // Unlocked records may already have been consumed by later upgrades. Only
  // confirmed locked pieces establish stable ownership for a saved outfit.
  const observations = (payload.blessing?.locks?.groups ?? []).filter((row) => row.locked_count > 0);
  for (const row of observations) {
    const ids = flashByName.get(normalize(`${row.name}·${row.slot}`));
    if (ids?.length !== 1) continue;
    const id = ids[0];
    const ratios = catalog.curves[catalog.items[id].curve].ratios;
    if (!Number.isInteger(row.level) || row.level < 0 || row.level >= ratios.length) continue;
    if ((flashes.get(id)?.level ?? -1) < row.level) flashes.set(id, { id, name: `${row.name}·${row.slot}`, level: row.level, evidence: '祝福闪光记录' });
  }
  result.items.push(...flashes.values());
  return result;
}
