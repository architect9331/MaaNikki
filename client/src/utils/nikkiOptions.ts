import type { OptionValue } from '@/types/interface';

const REALM_MODE_TARGETS: Record<string, string> = {
  JihuaTarget: 'Jihua', JihuaMaterialMode: 'Jihua', JihuaMaterials: 'Jihua', MonsterTarget: 'Monster',
};
const COLLECTION_OPTIONS = new Set(['CollectionPriority', 'CollectionBling', 'CollectionDecompose', 'CollectionOrganize']);

export const realmCollectionEnabled = (values: Record<string, OptionValue>): boolean =>
  values.CollectionMode?.type === 'switch' && values.CollectionMode.value;

// Target choices remain saved while a different consumption mode is active.
export function realmOptionActive(key: string, values: Record<string, OptionValue>): boolean {
  if (key === 'WeeklyTargets') {
    const weekly = values.WeeklyEnabled;
    return weekly?.type !== 'switch' || weekly.value;
  }
  const collection = realmCollectionEnabled(values);
  if (COLLECTION_OPTIONS.has(key)) return collection;
  // In collection mode the blessing target is the failed-organization fallback.
  if (key === 'BlessTarget' && collection) return true;
  if (key === 'RealmTypes') return !collection;
  // Collection mode's bling runs spend the configured materials.
  if (collection && (key === 'JihuaMaterialMode' || key === 'JihuaMaterials')) {
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
  if (realm?.type !== 'select' || realm.caseName !== 'FiveStarCollection') return values;
  return { ...values, RealmTypes: { type: 'select', caseName: 'Bless' },
    CollectionMode: { type: 'switch', value: true } };
}
