import type { OptionValue } from '@/types/interface';

const REALM_MODE_TARGETS: Record<string, string> = {
  JihuaTarget: 'Jihua', JihuaMaterialMode: 'Jihua', JihuaMaterials: 'Jihua', MonsterTarget: 'Monster',
};
const COLLECTION_MANAGED_OPTIONS = new Set(['CollectionReserveBling', 'CollectionBling', 'CollectionDecompose']);
export const COLLECTION_ADVANCED_OPTIONS = new Set([
  'CollectionReserveBling',
  'CollectionBling',
  'CollectionDecompose',
  'CollectionOrganize',
]);
const DOUBLE_OPTION_KEYS = new Set([
  'DoubleEnabled', 'DoubleTypes', 'DoubleWeeklyPriority',
]);
const STRATEGY_OPTION_KEYS = new Set([
  'DoubleJihuaMode', 'DoubleBlessMode', 'MonsterTargetMode',
]);
export const DOUBLE_REALM_TYPES = ['Jihua', 'Bless', 'Weekly'] as const;

export type RealmGoal = 'weekly' | 'renewal' | 'crafting' | 'glow';
/** Goals that farm toward a collection plan; the weekly realm has no plan. */
export type PlanGoal = Exclude<RealmGoal, 'weekly'>;
export interface RealmQueueEntry { goal: RealmGoal; enabled: boolean }
export const REALM_GOALS: RealmGoal[] = ['weekly', 'renewal', 'crafting', 'glow'];
export const REALM_GOAL_LABELS: Record<RealmGoal, string> = { weekly: '每周幻境', renewal: '焕新材料', crafting: '制衣材料', glow: '五星闪光' };
export const WEEKLY_TARGETS = [{ id: 'Qiggeda', label: '奇格格达' }, { id: 'Juan', label: '卷卷' }] as const;

const hasWeeklyToken = (raw: string | undefined) =>
  String(raw ?? '').split(',').some((token) => token.trim().replace(/^-/, '') === 'weekly');

/**
 * "weekly,renewal,-crafting,glow": order is priority, a leading "-" keeps a switched-off goal's place.
 * Orders saved before the weekly realm joined the queue ran it first, so it leads them switched on.
 */
export function parseRealmQueue(raw: string | undefined): RealmQueueEntry[] {
  const entries: RealmQueueEntry[] = hasWeeklyToken(raw) ? [] : [{ goal: 'weekly', enabled: true }];
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

export const doubleEnabled = (values?: Record<string, OptionValue>): boolean =>
  values?.DoubleEnabled?.type === 'switch' ? values.DoubleEnabled.value : true;

export const realmFarmThread = (values?: Record<string, OptionValue>): boolean =>
  values?.FarmThread?.type === 'switch' ? values.FarmThread.value : false;

export const realmFarmBling = (values?: Record<string, OptionValue>): boolean =>
  values?.FarmBling?.type === 'switch' ? values.FarmBling.value : false;

export function doubleTypes(values?: Record<string, OptionValue>): string[] {
  const option = values?.DoubleTypes;
  return option?.type === 'checkbox' ? option.caseNames : [...DOUBLE_REALM_TYPES];
}

export const doubleJihuaMode = (values?: Record<string, OptionValue>): 'FollowDeficit' | 'InheritFallback' =>
  values?.DoubleJihuaMode?.type === 'select' && values.DoubleJihuaMode.caseName === 'InheritFallback'
    ? 'InheritFallback'
    : 'FollowDeficit';

export const doubleBlessMode = (values?: Record<string, OptionValue>): 'GlowPursuit' | 'InheritFallback' =>
  values?.DoubleBlessMode?.type === 'select' && values.DoubleBlessMode.caseName === 'InheritFallback'
    ? 'InheritFallback'
    : 'GlowPursuit';

export const doubleWeeklyPriority = (values?: Record<string, OptionValue>): 'Qiggeda' | 'Juan' =>
  values?.DoubleWeeklyPriority?.type === 'select' && values.DoubleWeeklyPriority.caseName === 'Juan'
    ? 'Juan'
    : 'Qiggeda';

export const monsterTargetMode = (values?: Record<string, OptionValue>): 'FollowDeficit' | 'InheritFallback' =>
  values?.MonsterTargetMode?.type === 'select' && values.MonsterTargetMode.caseName === 'InheritFallback'
    ? 'InheritFallback'
    : 'FollowDeficit';

export const realmCollectionWorkflow = (values: Record<string, OptionValue>): 'Managed' | 'FarmOnly' =>
  values.CollectionWorkflow?.type === 'select' && values.CollectionWorkflow.caseName === 'FarmOnly'
    ? 'FarmOnly'
    : 'Managed';

export const realmFarmOnlyEnabled = (values: Record<string, OptionValue>): boolean =>
  (realmCollectionEnabled(values)
    || (doubleEnabled(values) && doubleTypes(values).includes('Bless') && doubleBlessMode(values) === 'GlowPursuit'))
  && realmCollectionWorkflow(values) === 'FarmOnly';

export function realmReserveBling(values?: Record<string, OptionValue>): number {
  const raw = values?.CollectionReserveBling?.type === 'input'
    ? values.CollectionReserveBling.values.amount ?? '0'
    : '0';
  const parsed = Number.parseInt(String(raw).trim().replace(/[,，\s]/g, ''), 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
}

/** Selected weekly realm levels; an unsaved option means the default of both. */
export function weeklyTargets(values?: Record<string, OptionValue>): string[] {
  const option = values?.WeeklyTargets;
  return option?.type === 'checkbox' ? option.caseNames : WEEKLY_TARGETS.map((target) => target.id);
}

const fallbackRealm = (values: Record<string, OptionValue>) =>
  values.RealmTypes?.type === 'select' ? values.RealmTypes.caseName : 'Bless';

/** With the blessing goal queued, the blessing target also covers a failed organization. */
export function realmOptionLabel(key: string, values: Record<string, OptionValue>): string | undefined {
  const current = fallbackRealm(values);
  const dbl = doubleEnabled(values);
  const dblTypes = dbl ? doubleTypes(values) : [];
  const blessFallback = doubleBlessMode(values) === 'InheritFallback';
  const jihuaFallback = doubleJihuaMode(values) === 'InheritFallback';
  const monsterFallback = monsterTargetMode(values) === 'InheritFallback';
  const dblBlessFallback = dblTypes.includes('Bless') && blessFallback;
  const dblBlessGlow = dblTypes.includes('Bless') && !blessFallback;
  const dblJihuaFallback = dblTypes.includes('Jihua') && jihuaFallback;
  const collection = realmCollectionEnabled(values) || dblBlessGlow;

  if (key === 'BlessTarget') {
    if (current === 'Bless') {
      if (collection) return '兜底 / 闪光整理失败时刷取';
      return blessFallback ? '祝福闪光关卡（兜底 / 补分）' : '兜底 / 朝夕无缺备用关卡';
    }
    if (collection && dblBlessFallback) return '双倍 / 整理失败 / 补分备用';
    if (collection) return '闪光整理失败时刷取';
    if (dblBlessFallback) return '灵感迸发双倍与补分关卡';
    return blessFallback ? '朝夕补分祝福闪光关卡' : '无缺口时朝夕补分备用关卡';
  }
  if (key === 'JihuaTarget') {
    if (current === 'Jihua') {
      return jihuaFallback ? '兜底 / 双倍 / 朝夕激化奖励' : '兜底 / 无缺口时备用奖励';
    }
    if (dblJihuaFallback) return '超级激化双倍与补分奖励';
    return jihuaFallback ? '朝夕补分素材激化奖励' : '无缺口时朝夕补分备用奖励';
  }
  if (key === 'MonsterTarget') {
    if (current === 'Monster') {
      return monsterFallback ? '魔物试炼关卡（兜底 / 朝夕补分）' : '兜底 / 无缺口时备用关卡';
    }
    return monsterFallback ? '朝夕补分魔物试炼关卡' : '无缺口时朝夕补分备用关卡';
  }
  return undefined;
}

// Target choices remain saved while they are not used.
export function realmOptionActive(key: string, values: Record<string, OptionValue>): boolean {
  if (key === 'RealmQueue' || key === 'RealmTypes' || key === 'DoubleEnabled' || STRATEGY_OPTION_KEYS.has(key)) return true;
  const dbl = doubleEnabled(values);
  const dblTypes = dbl ? doubleTypes(values) : [];
  const dblJihua = dblTypes.includes('Jihua');
  const dblBless = dblTypes.includes('Bless');
  const dblWeekly = dblTypes.includes('Weekly');

  if (key === 'DoubleTypes') return dbl;
  if (key === 'DoubleWeeklyPriority') return dblWeekly;

  const queueWeekly = realmGoalRank(values, 'weekly') !== null;
  if (key === 'WeeklyTargets') return queueWeekly || dblWeekly;

  const queueGlow = realmCollectionEnabled(values);
  const dblBlessGlow = dblBless && doubleBlessMode(values) === 'GlowPursuit';
  const dblBlessFallback = dblBless && doubleBlessMode(values) === 'InheritFallback';
  const collection = queueGlow || dblBlessGlow;
  const farmOnly = collection && realmCollectionWorkflow(values) === 'FarmOnly';
  if (key === 'CollectionWorkflow' || key === 'CollectionOrganize') return collection;
  if (COLLECTION_MANAGED_OPTIONS.has(key)) return collection && !farmOnly;

  const current = fallbackRealm(values);
  if (key === 'BlessTarget') return current === 'Bless' || collection || dblBlessFallback;

  const jihuaFollow = doubleJihuaMode(values) === 'FollowDeficit';
  const dblJihuaFallback = dblJihua && !jihuaFollow;
  if (key === 'JihuaTarget') return current === 'Jihua' || dblJihuaFallback;

  const hasRenewal = realmGoalRank(values, 'renewal') !== null;
  const hasCrafting = realmGoalRank(values, 'crafting') !== null;
  if (key === 'FarmThread' || key === 'FarmBling') {
    return hasRenewal || (jihuaFollow && hasCrafting);
  }

  const hasMaterialNeed = hasRenewal
    || dblJihuaFallback
    || (jihuaFollow && hasCrafting);
  // Renewal farming and follow-deficit Jihua also consume the configured offering materials.
  if ((key === 'JihuaMaterialMode' || key === 'JihuaMaterials') && hasMaterialNeed) return true;
  // The blessing goal's bling runs spend the configured materials.
  if ((key === 'JihuaMaterialMode' || key === 'JihuaMaterials') && collection && !farmOnly) {
    const bling = values.CollectionBling;
    if (bling?.type !== 'switch' || bling.value) return true;
  }
  return !REALM_MODE_TARGETS[key] || REALM_MODE_TARGETS[key] === current;
}

// Options rendered as backups only matter for fallback when unified goals have no unmet shortfalls.
export function realmOptionBackup(key: string, values: Record<string, OptionValue>): boolean {
  return (key in REALM_MODE_TARGETS || key === 'BlessTarget') && !realmOptionActive(key, values);
}

/** Each option has one editor, even when several realm stages share its value. */
export function realmOptionSections(keys: string[], values: Record<string, OptionValue>) {
  const current = fallbackRealm(values);
  const dbl = doubleEnabled(values);
  const dblTypes = dbl ? doubleTypes(values) : [];
  const dblJihuaFallback = dblTypes.includes('Jihua') && doubleJihuaMode(values) === 'InheritFallback';
  const dblBlessGlow = dblTypes.includes('Bless') && doubleBlessMode(values) === 'GlowPursuit';
  const dblBlessFallback = dblTypes.includes('Bless') && doubleBlessMode(values) === 'InheritFallback';
  const queueWeekly = realmGoalRank(values, 'weekly') !== null;

  const available = keys.filter((key) => {
    if (key === 'RealmQueue' || key === 'CollectionWorkflow') return false;
    if (key === 'WeeklyTargets') return !queueWeekly && dblTypes.includes('Weekly') && realmOptionActive(key, values);
    return realmOptionActive(key, values);
  });

  const doubleKeys = new Set(DOUBLE_OPTION_KEYS);
  if (!queueWeekly && dblTypes.includes('Weekly')) doubleKeys.add('WeeklyTargets');
  const strategyKeys = new Set(STRATEGY_OPTION_KEYS);
  const collectionKeys = new Set(['CollectionReserveBling', 'CollectionBling', 'CollectionDecompose', 'CollectionOrganize']);
  const collectionActive = realmCollectionEnabled(values) || dblBlessGlow;
  if (current !== 'Bless' && collectionActive) collectionKeys.add('BlessTarget');
  const materialKeys = new Set(['FarmThread', 'FarmBling', 'JihuaMaterialMode', 'JihuaMaterials']);

  return [
    {
      id: 'double', title: '限时双倍 · 插队调度',
      description: dbl
        ? '检测到活动或回归双倍且有可挑战目标时，优先于「刷取顺序」插队处理；按下方策略已备齐或已收齐则自动跳过，不会强行刷取。'
        : '关闭时按上方「刷取顺序」与「兜底副本」使用活跃能量，不自动插队双倍活动；下方策略仍继续服务朝夕心愿补分。',
      keys: available.filter((key) => doubleKeys.has(key)),
    },
    {
      id: 'strategy', title: '双倍与朝夕补分 · 目标策略',
      description: '同时用于限时双倍插队与朝夕心愿活跃能量补分：设成跟随缺口 / 五星追全时优先挑还缺的材料与缺色副本；双倍无缺跳过不强刷，朝夕补分无缺时改用兜底／备用关卡凑满 500 分。',
      keys: available.filter((key) => strategyKeys.has(key)),
    },
    {
      id: 'collection', title: '五星闪光 · 收集设置',
      description: realmFarmOnlyEnabled(values)
        ? '用于「五星闪光」追全或灵感迸发双倍追全。只刷胚子时不升级、不分解、不修改锁定。'
        : '用于「五星闪光」追全或灵感迸发双倍追全：整理仓库、升级与收集新颜色。',
      keys: available.filter((key) => collectionKeys.has(key)),
    },
    {
      id: 'fallback', title: '兜底副本 · 剩余活跃能量',
      description: '刷取顺序中的目标都完成或未开启后，才使用这里的副本与奖励设置。'
        + (current === 'Bless' && collectionActive ? '所选关卡也用于五星闪光整理失败后的备用刷取。' : '')
        + (dblJihuaFallback || (dblBlessFallback && !collectionActive) ? '目标策略设为「继承兜底设置」时同样使用对应关卡目标。' : ''),
      keys: available.filter((key) => !doubleKeys.has(key) && !strategyKeys.has(key) && !collectionKeys.has(key) && !materialKeys.has(key)),
    },
    {
      id: 'materials', title: '素材激化 · 共用消耗材料',
      description: '决定拿哪些材料兑换奖励。焕新备料、双倍激化、五星闪光补噗灵、兜底及朝夕心愿补分都会沿用；兑换哪种奖励由各自目标决定。',
      keys: available.filter((key) => materialKeys.has(key)),
    },
  ].filter((section) => section.keys.length > 0);
}

/**
 * The former realm mode switches (五星全收集 / 制衣材料全收集), the standalone
 * 焕新材料筹备 task and the 优先完成每周幻境 switch become goals of one queue;
 * the realm type stays the fallback.
 */
export function migrateRealmQueueValues(values: Record<string, OptionValue>, renewalFirst = false): Record<string, OptionValue> {
  const legacy = 'CollectionMode' in values || 'CraftingCollectionMode' in values || 'WeeklyEnabled' in values;
  if (!legacy && !renewalFirst) return values;
  const next = { ...values };
  const switchOn = (key: string) => next[key]?.type === 'switch' && (next[key] as { value: boolean }).value;
  const saved = next.RealmQueue?.type === 'input' ? next.RealmQueue.values.order : undefined;
  let entries: RealmQueueEntry[];
  if (saved !== undefined) {
    entries = realmQueue(next);
  } else {
    const glow = switchOn('CollectionMode');
    entries = [
      { goal: 'weekly', enabled: true },
      { goal: 'renewal', enabled: false },
      { goal: 'crafting', enabled: !glow && switchOn('CraftingCollectionMode') },
      { goal: 'glow', enabled: glow },
    ];
  }
  // The weekly realm used to run before everything else while its switch was on.
  if (!hasWeeklyToken(saved) && next.WeeklyEnabled?.type === 'switch') {
    const weekly = next.WeeklyEnabled.value;
    entries = entries.map((entry) => entry.goal === 'weekly' ? { ...entry, enabled: weekly } : entry);
  }
  if (renewalFirst) {
    const weekly = entries.filter((entry) => entry.goal === 'weekly');
    entries = [...weekly, { goal: 'renewal', enabled: true }, ...entries.filter((entry) => entry.goal !== 'renewal' && entry.goal !== 'weekly')];
  }
  delete next.CollectionMode;
  delete next.CraftingCollectionMode;
  delete next.WeeklyEnabled;
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
  const actions = next.GameKeysActions;
  if (actions?.type === 'input' && 'skip' in actions.values) {
    const rest = { ...actions.values };
    delete rest.skip;
    if (next === values) next = { ...values };
    next.GameKeysActions = { ...actions, values: rest };
  }
  return next;
}

/** Migrate retired option shapes and split switches into their current keys. */
export function migrateNikkiOptionValues(input: Record<string, OptionValue>): Record<string, OptionValue> {
  const values = migrateGameKeyGroups(input);
  const realm = values.RealmTypes;
  const legacyCollection = realm?.type === 'select' && realm.caseName === 'FiveStarCollection';
  const legacyCommon = 'FarmCommonMats' in values;
  if (!legacyCollection && !('CollectionPriority' in values) && !legacyCommon) return migrateRealmQueueValues(values);
  const next = { ...values };
  delete next.CollectionPriority;
  if (legacyCommon) {
    const common = next.FarmCommonMats;
    if (common?.type === 'switch') {
      if (!('FarmThread' in next)) next.FarmThread = { type: 'switch', value: common.value };
      if (!('FarmBling' in next)) next.FarmBling = { type: 'switch', value: common.value };
    }
    delete next.FarmCommonMats;
  }
  if (legacyCollection) {
    next.RealmTypes = { type: 'select', caseName: 'Bless' };
    next.CollectionMode = { type: 'switch', value: true };
  }
  return migrateRealmQueueValues(next);
}
