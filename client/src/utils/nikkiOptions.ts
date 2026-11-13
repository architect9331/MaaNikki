import type { OptionValue } from '@/types/interface';

const REALM_MODE_TARGETS: Record<string, string> = {
  JihuaTarget: 'Jihua', JihuaMaterialMode: 'Jihua', JihuaMaterials: 'Jihua', MonsterTarget: 'Monster',
};
const COLLECTION_MANAGED_OPTIONS = new Set(['CollectionReserveBling', 'CollectionBling', 'CollectionDecompose']);

export type RealmGoal = 'renewal' | 'crafting' | 'glow';
export interface RealmQueueEntry { goal: RealmGoal; enabled: boolean }
export const REALM_GOALS: RealmGoal[] = ['renewal', 'crafting', 'glow'];
export const REALM_GOAL_LABELS: Record<RealmGoal, string> = { renewal: '换新材料', crafting: '制衣材料', glow: '五星闪光' };

/** "renewal,-crafting,glow": order is priority, a leading "-" keeps a switched-off goal's place. */
export function parseRealmQueue(raw: string | undefined): RealmQueueEntry[] {
  const entries: RealmQueueEntry[] = [];
  for (const token of String(raw ?? '').split(',')) {
    const name = token.trim().replace(/^-/, '') as RealmGoal;
    if (REALM_GOALS.includes(name) && !entries.some((entry) => entry.goal === name)) {
      entries.push({ goal: name, enabled: !token.trim().startsWith('-') });
    }
  }
  for (const goal of REALM_GOALS) if (!entries.some((entry) => entry.goal === goal)) entries.push({ goal, enabled: false });
  return entries;
}

export const serializeRealmQueue = (entries: RealmQueueEntry[]) =>
  entries.map((entry) => `${entry.enabled ? '' : '-'}${entry.goal}`).join(',');

export const realmQueueOption = (entries: RealmQueueEntry[]): OptionValue =>
  ({ type: 'input', values: { order: serializeRealmQueue(entries) } });

export function realmQueue(values?: Record<string, OptionValue>): RealmQueueEntry[] {
  const option = values?.RealmQueue;
  return parseRealmQueue(option?.type === 'input' ? option.values.order : undefined);
}

/** 1-based position among the enabled goals, or null when the goal is off. */
export function realmGoalRank(values: Record<string, OptionValue> | undefined, goal: RealmGoal): number | null {
  const enabled = realmQueue(values).filter((entry) => entry.enabled);
  const index = enabled.findIndex((entry) => entry.goal === goal);
  return index < 0 ? null : index + 1;
}

export const realmCollectionEnabled = (values: Record<string, OptionValue>): boolean =>
  realmGoalRank(values, 'glow') !== null;

export const realmCollectionWorkflow = (values: Record<string, OptionValue>): 'Managed' | 'FarmOnly' =>
  values.CollectionWorkflow?.type === 'select' && values.CollectionWorkflow.caseName === 'FarmOnly'
    ? 'FarmOnly'
    : 'Managed';

export const realmFarmOnlyEnabled = (values: Record<string, OptionValue>): boolean =>
  realmCollectionEnabled(values) && realmCollectionWorkflow(values) === 'FarmOnly';

export function realmReserveBling(values?: Record<string, OptionValue>): number {
  const raw = values?.CollectionReserveBling?.type === 'input'
    ? values.CollectionReserveBling.values.amount ?? '0'
    : '0';
  const parsed = Number.parseInt(String(raw).trim().replace(/[,，\s]/g, ''), 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
}

const fallbackRealm = (values: Record<string, OptionValue>) =>
  values.RealmTypes?.type === 'select' ? values.RealmTypes.caseName : 'Bless';

/** With the blessing goal queued, the blessing target also covers a failed organization. */
export function realmOptionLabel(key: string, values: Record<string, OptionValue>): string | undefined {
  return key === 'BlessTarget' && realmCollectionEnabled(values) && fallbackRealm(values) !== 'Bless' ? '备选副本' : undefined;
}

// Target choices remain saved while they are not used.
export function realmOptionActive(key: string, values: Record<string, OptionValue>): boolean {
  const collection = realmCollectionEnabled(values);
  const farmOnly = realmFarmOnlyEnabled(values);
  if (key === 'WeeklyEnabled' || key === 'RealmQueue' || key === 'RealmTypes') return true;
  if (key === 'WeeklyTargets') {
    const weekly = values.WeeklyEnabled;
    return weekly?.type !== 'switch' || weekly.value;
  }
  if (key === 'CollectionWorkflow' || key === 'CollectionOrganize') return collection;
  if (COLLECTION_MANAGED_OPTIONS.has(key)) return collection && !farmOnly;
  const current = fallbackRealm(values);
  if (key === 'BlessTarget') return current === 'Bless' || collection;
  // The blessing goal's bling runs spend the configured materials.
  if ((key === 'JihuaMaterialMode' || key === 'JihuaMaterials') && collection && !farmOnly) {
    const bling = values.CollectionBling;
    if (bling?.type !== 'switch' || bling.value) return true;
  }
  return !REALM_MODE_TARGETS[key] || REALM_MODE_TARGETS[key] === current;
}

// Options rendered as backups only matter for the daily wish energy top-up.
export function realmOptionBackup(key: string, values: Record<string, OptionValue>): boolean {
  return (key in REALM_MODE_TARGETS || key === 'BlessTarget') && !realmOptionActive(key, values);
}

/**
 * The former realm mode switches (五星全收集 / 制衣材料全收集) and the standalone
 * 焕新材料筹备 task become goals of one queue; the realm type stays the fallback.
 */
export function migrateRealmQueueValues(values: Record<string, OptionValue>, renewalFirst = false): Record<string, OptionValue> {
  const legacy = 'CollectionMode' in values || 'CraftingCollectionMode' in values;
  if (!legacy && !renewalFirst) return values;
  const next = { ...values };
  const switchOn = (key: string) => next[key]?.type === 'switch' && (next[key] as { value: boolean }).value;
  let entries: RealmQueueEntry[];
  if (next.RealmQueue?.type === 'input') {
    entries = realmQueue(next);
  } else {
    const glow = switchOn('CollectionMode');
    entries = [
      { goal: 'renewal', enabled: false },
      { goal: 'crafting', enabled: !glow && switchOn('CraftingCollectionMode') },
      { goal: 'glow', enabled: glow },
    ];
  }
  if (renewalFirst) entries = [{ goal: 'renewal', enabled: true }, ...entries.filter((entry) => entry.goal !== 'renewal')];
  delete next.CollectionMode;
  delete next.CraftingCollectionMode;
  next.RealmQueue = realmQueueOption(entries);
  return next;
}

/** Drop the retired 焕新材料筹备 task; when it was enabled, renewal leads the realm queue. */
export function retireGlowPrepTask<T extends { taskName: string; enabled: boolean; optionValues?: Record<string, OptionValue> }>(tasks: T[]): T[] {
  if (!tasks.some((task) => task.taskName === 'glow_material_prep')) return tasks;
  const renewalFirst = tasks.some((task) => task.taskName === 'glow_material_prep' && task.enabled);
  return tasks.filter((task) => task.taskName !== 'glow_material_prep').map((task) => task.taskName === 'realm_challenge'
    ? { ...task, optionValues: migrateRealmQueueValues(task.optionValues ?? {}, renewalFirst) }
    : task);
}

/** Game key inputs regrouped to mirror the game's key settings page: [input, from, to]. */
const MOVED_GAME_KEYS: [string, string, string][] = [
  ['bell', 'GameKeysActions', 'GameKeysMove'],
  ['sub_ability', 'GameKeysActions', 'GameKeysMove'],
  ['chat', 'GameKeysActions', 'GameKeysPages'],
  ['exit', 'GameKeysPages', 'GameKeysActions'],
];

/** Carry saved bindings into their new group; a value already saved there wins. */
function migrateGameKeyGroups(values: Record<string, OptionValue>): Record<string, OptionValue> {
  let next = values;
  for (const [name, from, to] of MOVED_GAME_KEYS) {
    const source = next[from];
    if (source?.type !== 'input' || !(name in source.values)) continue;
    const target = next[to];
    const targetValues = target?.type === 'input' ? target.values : {};
    const { [name]: moved, ...rest } = source.values;
    if (next === values) next = { ...values };
    next[from] = { ...source, values: rest };
    if (!(name in targetValues)) {
      next[to] = { ...(target?.type === 'input' ? target : { type: 'input' as const }), values: { ...targetValues, [name]: moved } };
    }
  }
  return next;
}

/** The former realm "five-star collection" case is now its own mode switch. */
export function migrateNikkiOptionValues(input: Record<string, OptionValue>): Record<string, OptionValue> {
  const values = migrateGameKeyGroups(input);
  const realm = values.RealmTypes;
  const legacyCollection = realm?.type === 'select' && realm.caseName === 'FiveStarCollection';
  if (!legacyCollection && !('CollectionPriority' in values)) return migrateRealmQueueValues(values);
  const next = { ...values };
  delete next.CollectionPriority;
  if (legacyCollection) {
    next.RealmTypes = { type: 'select', caseName: 'Bless' };
    next.CollectionMode = { type: 'switch', value: true };
  }
  return migrateRealmQueueValues(next);
}
