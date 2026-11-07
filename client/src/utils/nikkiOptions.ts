import type { OptionValue } from '@/types/interface';

const REALM_MODE_TARGETS: Record<string, string> = {
  JihuaTarget: 'Jihua', JihuaMaterialMode: 'Jihua', JihuaMaterials: 'Jihua', MonsterTarget: 'Monster',
};
const COLLECTION_MANAGED_OPTIONS = new Set(['CollectionReserveBling', 'CollectionBling', 'CollectionDecompose']);

export const realmCollectionEnabled = (values: Record<string, OptionValue>): boolean =>
  values.CollectionMode?.type === 'switch' && values.CollectionMode.value;

export const realmCollectionWorkflow = (values: Record<string, OptionValue>): 'Managed' | 'FarmOnly' =>
  values.CollectionWorkflow?.type === 'select' && values.CollectionWorkflow.caseName === 'FarmOnly'
    ? 'FarmOnly'
    : 'Managed';

export const realmFarmOnlyEnabled = (values: Record<string, OptionValue>): boolean =>
  realmCollectionEnabled(values) && realmCollectionWorkflow(values) === 'FarmOnly';

export const realmCraftingEnabled = (values: Record<string, OptionValue>): boolean =>
  !realmCollectionEnabled(values) && values.CraftingCollectionMode?.type === 'switch' && values.CraftingCollectionMode.value;

/** In collection mode the blessing target is only the fallback for a failed organization. */
export function realmOptionLabel(key: string, values: Record<string, OptionValue>): string | undefined {
  return key === 'BlessTarget' && realmCollectionEnabled(values) ? '备选副本' : undefined;
}

// Target choices remain saved while a different consumption mode is active.
export function realmOptionActive(key: string, values: Record<string, OptionValue>): boolean {
  const collection = realmCollectionEnabled(values);
  const farmOnly = realmFarmOnlyEnabled(values);
  const crafting = realmCraftingEnabled(values);
  if (key === 'CollectionMode') return collection;
  if (key === 'CraftingCollectionMode') return crafting;
  if (key === 'WeeklyEnabled') return true;
  if (key === 'WeeklyTargets') {
    const weekly = values.WeeklyEnabled;
    return weekly?.type !== 'switch' || weekly.value;
  }
  if (key === 'CollectionWorkflow' || key === 'CollectionOrganize') return collection;
  if (COLLECTION_MANAGED_OPTIONS.has(key)) return collection && !farmOnly;
  // In collection mode the blessing target is the failed-organization fallback.
  if (key === 'BlessTarget' && collection) return true;
  if (key === 'RealmTypes') return !collection && !crafting;
  if (crafting) return false;
  // Collection mode's bling runs spend the configured materials.
  if (collection && (key === 'JihuaMaterialMode' || key === 'JihuaMaterials')) {
    if (farmOnly) return false;
    const bling = values.CollectionBling;
    return bling?.type !== 'switch' || bling.value;
  }
  const selected = values.RealmTypes;
  const current = collection ? '' : selected?.type === 'select' ? selected.caseName : 'Bless';
  if (key === 'BlessTarget') return current === 'Bless';
  return !REALM_MODE_TARGETS[key] || REALM_MODE_TARGETS[key] === current;
}

// Options rendered as backups only matter for the daily wish energy top-up.
export function realmOptionBackup(key: string, values: Record<string, OptionValue>): boolean {
  return (key in REALM_MODE_TARGETS || key === 'BlessTarget') && !realmOptionActive(key, values);
}

/** The former realm "five-star collection" case is now its own mode switch. */
export function migrateNikkiOptionValues(values: Record<string, OptionValue>): Record<string, OptionValue> {
  const realm = values.RealmTypes;
  const legacyCollection = realm?.type === 'select' && realm.caseName === 'FiveStarCollection';
  if (!legacyCollection && !('CollectionPriority' in values)) return values;
  const next = { ...values };
  delete next.CollectionPriority;
  if (legacyCollection) {
    next.RealmTypes = { type: 'select', caseName: 'Bless' };
    next.CollectionMode = { type: 'switch', value: true };
  }
  return next;
}
