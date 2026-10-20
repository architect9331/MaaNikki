import type { OptionValue } from '@/types/interface';

// Target choices remain saved while a different consumption mode is active.
export function realmOptionActive(key: string, values: Record<string, OptionValue>): boolean {
  if (key === 'WeeklyTargets') {
    const weekly = values.WeeklyEnabled;
    return weekly?.type !== 'switch' || weekly.value;
  }
  const modes: Record<string, string> = {
    JihuaTarget: 'Jihua', JihuaMaterialMode: 'Jihua', JihuaMaterials: 'Jihua',
    BlessTarget: 'Bless', MonsterTarget: 'Monster',
  };
  const selected = values.RealmTypes;
  return !modes[key] || modes[key] === (selected?.type === 'select' ? selected.caseName : 'Bless');
}
