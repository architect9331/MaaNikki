import type { OptionValue } from '@/types/interface';

const COLLECTION_MANAGED_OPTIONS = new Set(['CollectionReserveBling', 'CollectionBling', 'CollectionDecompose']);
export const COLLECTION_ADVANCED_OPTIONS = new Set([
  'CollectionReserveBling',
  'CollectionBling',
  'CollectionDecompose',
  'CollectionOrganize',
]);
const DOUBLE_AND_STRATEGY_KEYS = [
  'DoubleEnabled',
  'DoubleTypes',
  'DoubleWeeklyPriority',
  'WeeklyTargets',
  'DoubleJihuaMode',
  'DoubleBlessMode',
  'MonsterTargetMode',
] as const;
const FALLBACK_AND_TARGET_KEYS = [
  'RealmTypes',
  'JihuaTarget',
  'BlessTarget',
  'MonsterTarget',
] as const;
const COLLECTION_OPTION_KEYS = [
  'CollectionReserveBling',
  'CollectionBling',
  'CollectionDecompose',
  'CollectionOrganize',
] as const;
const MATERIAL_OPTION_KEYS = [
  'FarmThread',
  'FarmBling',
  'JihuaMaterialMode',
  'JihuaMaterials',
] as const;
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

export interface RealmOptionRoleHint {
  label: string;
  description: string;
  tone?: 'accent' | 'muted';
}

export interface RealmOptionSection {
  id: 'double' | 'fallback' | 'collection' | 'materials';
  title: string;
  description: string;
  keys: string[];
  subheadings?: Record<string, string>;
}

/** Dynamic role badge explaining when each unified default target or shared rule applies. */
export function realmOptionUsageHint(
  key: string,
  values: Record<string, OptionValue>,
): RealmOptionRoleHint | undefined {
  const current = fallbackRealm(values);
  const dbl = doubleEnabled(values);
  const dblTypes = dbl ? doubleTypes(values) : [];
  const blessFallback = doubleBlessMode(values) === 'InheritFallback';
  const jihuaFallback = doubleJihuaMode(values) === 'InheritFallback';
  const monsterFallback = monsterTargetMode(values) === 'InheritFallback';
  const dblBless = dblTypes.includes('Bless');
  const dblJihua = dblTypes.includes('Jihua');
  const dblBlessFallback = dblBless && blessFallback;
  const dblBlessGlow = dblBless && !blessFallback;
  const dblJihuaFallback = dblJihua && jihuaFallback;
  const queueGlow = realmCollectionEnabled(values);
  const collection = queueGlow || dblBlessGlow;

  if (key === 'JihuaTarget') {
    if (current === 'Jihua') {
      return {
        label: dblJihuaFallback ? '当前兜底 · 双倍与补分' : '当前兜底',
        tone: 'accent',
        description: dblJihuaFallback
          ? '已设为剩余活跃能量兜底，且激化策略为「继承兜底设置」：队列完成后、超级激化双倍与朝夕补分都兑换此处奖励。'
          : jihuaFallback
            ? '已设为剩余活跃能量兜底，队列完成后直接兑换此处奖励，并同时覆盖朝夕心愿素材激化补分。'
            : '已设为剩余活跃能量兜底，队列完成后直接兑换此处奖励；跟随缺口已备齐但朝夕心愿仍需补分时也使用此处。',
      };
    }
    if (dblJihuaFallback) {
      return {
        label: '双倍与补分',
        tone: 'accent',
        description: '素材激化策略当前设为「继承兜底设置」，超级激化双倍与朝夕心愿激化补分均直接兑换此处奖励。',
      };
    }
    if (jihuaFallback) {
      return {
        label: '朝夕补分直用',
        tone: 'accent',
        description: '素材激化策略当前设为「继承兜底设置」，朝夕心愿需要挑战素材激化补分时直接兑换此处奖励。',
      };
    }
    return {
      label: '无缺口备用',
      tone: 'muted',
      description: '当前策略为「跟随焕新 / 制衣缺口」；若所选材料已备齐（或队列未开启材料目标）但朝夕心愿仍需激化补分，改刷此处奖励。',
    };
  }

  if (key === 'BlessTarget') {
    if (current === 'Bless') {
      return {
        label: collection ? '当前兜底 · 整理备用' : dblBlessFallback ? '当前兜底 · 双倍与补分' : '当前兜底',
        tone: 'accent',
        description: collection
          ? '已设为剩余活跃能量兜底，队列完成后挑战此关卡；同时作为五星闪光仓库整理未完成及朝夕无缺补分的备用关卡。'
          : blessFallback
            ? '已设为剩余活跃能量兜底，队列完成后挑战此关卡；同时用于灵感迸发双倍与朝夕心愿祝福闪光补分。'
            : '已设为剩余活跃能量兜底，队列完成后挑战此关卡；已收齐或未整理仍需朝夕补分时也使用此处。',
      };
    }
    if (collection && dblBlessFallback) {
      return {
        label: '整理备用 · 双倍与补分',
        tone: 'accent',
        description: '五星闪光仓库整理未完成时退回此处；且灵感迸发策略为「继承兜底设置」，双倍与朝夕补分均直接挑战此处关卡。',
      };
    }
    if (collection) {
      return {
        label: '整理失败 / 无缺备用',
        tone: 'muted',
        description: '五星闪光追全仓库整理未完成时退回此处；所选五星范围已收齐但朝夕心愿仍需补分时也改刷此处。',
      };
    }
    if (dblBlessFallback) {
      return {
        label: '双倍与补分',
        tone: 'accent',
        description: '祝福闪光策略当前设为「继承兜底设置」，灵感迸发双倍与朝夕心愿均直接挑战此关卡。',
      };
    }
    if (blessFallback) {
      return {
        label: '朝夕补分直用',
        tone: 'accent',
        description: '祝福闪光策略当前设为「继承兜底设置」，朝夕心愿需要祝福闪光补分时直接挑战此关卡。',
      };
    }
    return {
      label: '无缺口备用',
      tone: 'muted',
      description: '已收齐所选五星范围或尚无整理记录但朝夕心愿仍需补分时，改刷此处关卡。',
    };
  }

  if (key === 'MonsterTarget') {
    if (current === 'Monster') {
      return {
        label: '当前兜底',
        tone: 'accent',
        description: monsterFallback
          ? '已设为剩余活跃能量兜底，队列完成后直接挑战此关卡；朝夕心愿魔物试炼补分也直接挑战此处。'
          : '已设为剩余活跃能量兜底，队列完成后直接挑战此关卡；晶石或泡泡已备齐仍需朝夕补分时也使用此处。',
      };
    }
    if (monsterFallback) {
      return {
        label: '朝夕补分直用',
        tone: 'accent',
        description: '魔物试炼策略当前设为「继承兜底设置」，朝夕心愿需要魔物试炼补分时直接挑战此关卡。',
      };
    }
    return {
      label: '无缺口备用',
      tone: 'muted',
      description: '按「跟随缺口」已无试炼晶石或魔物泡泡缺口（或队列未开启材料目标），但朝夕心愿仍需补分时改刷此处。',
    };
  }

  if (key === 'MonsterTargetMode') {
    return {
      label: '仅朝夕补分',
      tone: 'muted',
      description: '魔物试炼当前无限时双倍活动，此策略仅在朝夕心愿需要挑战魔物试炼补分时生效。',
    };
  }

  if (key === 'JihuaMaterialMode') {
    const hasRenewal = realmGoalRank(values, 'renewal') !== null;
    const hasCrafting = realmGoalRank(values, 'crafting') !== null;
    const farmOnly = collection && realmCollectionWorkflow(values) === 'FarmOnly';
    const blingSwitch = values.CollectionBling?.type !== 'switch' || values.CollectionBling.value;
    const directJihua =
      hasRenewal
      || current === 'Jihua'
      || (dblJihua && (jihuaFallback || hasCrafting))
      || (collection && !farmOnly && blingSwitch);
    if (!directJihua) {
      return {
        label: '朝夕激化用',
        tone: 'muted',
        description: '当前刷取顺序、双倍与兜底不会主动挑战素材激化；仅在朝夕心愿遇素材激化补分时按此选择兑换材料。',
      };
    }
  }
  return undefined;
}

/** Keep collapsed task header preview pills concise and high-signal. */
export function realmOptionPreview(key: string, values: Record<string, OptionValue>): boolean {
  return (key === 'DoubleEnabled' || key === 'RealmTypes') && realmOptionActive(key, values);
}

// Target choices remain saved while they are not used.
export function realmOptionActive(key: string, values: Record<string, OptionValue>): boolean {
  if (
    key === 'RealmQueue'
    || key === 'RealmTypes'
    || key === 'DoubleEnabled'
    || key === 'DoubleJihuaMode'
    || key === 'DoubleBlessMode'
    || key === 'MonsterTargetMode'
    || key === 'JihuaTarget'
    || key === 'BlessTarget'
    || key === 'MonsterTarget'
    || key === 'JihuaMaterialMode'
    || key === 'JihuaMaterials'
  ) {
    return true;
  }

  const dbl = doubleEnabled(values);
  const dblTypes = dbl ? doubleTypes(values) : [];
  const dblBless = dblTypes.includes('Bless');
  const dblWeekly = dblTypes.includes('Weekly');

  if (key === 'DoubleTypes') return dbl;
  if (key === 'DoubleWeeklyPriority') return dblWeekly;

  const queueWeekly = realmGoalRank(values, 'weekly') !== null;
  if (key === 'WeeklyTargets') return queueWeekly || dblWeekly;

  const queueGlow = realmCollectionEnabled(values);
  const dblBlessGlow = dblBless && doubleBlessMode(values) === 'GlowPursuit';
  const collection = queueGlow || dblBlessGlow;
  const farmOnly = collection && realmCollectionWorkflow(values) === 'FarmOnly';
  if (key === 'CollectionWorkflow' || key === 'CollectionOrganize') return collection;
  if (COLLECTION_MANAGED_OPTIONS.has(key)) return collection && !farmOnly;

  const jihuaFollow = doubleJihuaMode(values) === 'FollowDeficit';
  const hasRenewal = realmGoalRank(values, 'renewal') !== null;
  const hasCrafting = realmGoalRank(values, 'crafting') !== null;
  if (key === 'FarmThread' || key === 'FarmBling') {
    return hasRenewal || (jihuaFollow && hasCrafting);
  }
  return true;
}

/** Unified 4-card layout: double+strategies, fallback+all 3 default targets, collection, and jihua consumables. */
export function realmOptionSections(keys: string[], values: Record<string, OptionValue>): RealmOptionSection[] {
  const available = new Set(keys.filter((key) => realmOptionActive(key, values)));
  const dbl = doubleEnabled(values);
  const dblTypes = dbl ? doubleTypes(values) : [];
  const dblBlessGlow = dblTypes.includes('Bless') && doubleBlessMode(values) === 'GlowPursuit';
  const queueWeekly = realmGoalRank(values, 'weekly') !== null;
  const collectionActive = realmCollectionEnabled(values) || dblBlessGlow;
  const hasCommonToggle = available.has('FarmThread') || available.has('FarmBling');

  const doubleKeys = DOUBLE_AND_STRATEGY_KEYS.filter((key) => {
    if (!available.has(key)) return false;
    if (key === 'WeeklyTargets') return !queueWeekly && dblTypes.includes('Weekly');
    return true;
  });
  const fallbackKeys = FALLBACK_AND_TARGET_KEYS.filter((key) => available.has(key));
  const collectionKeys = collectionActive ? COLLECTION_OPTION_KEYS.filter((key) => available.has(key)) : [];
  const materialKeys = MATERIAL_OPTION_KEYS.filter((key) => available.has(key));

  const sections: RealmOptionSection[] = [
    {
      id: 'double',
      title: '限时双倍与朝夕补分',
      description: dbl
        ? '开启优先插队后，检测到超级激化、灵感迸发、深度突破或回归双倍且有可挑战目标时先于「刷取顺序」插队；下方选关策略同时用于双倍插队与朝夕心愿活跃能量补分。'
        : '已关闭限时双倍插队，直接按「刷取顺序」使用活跃能量；下方选关策略仍继续服务朝夕心愿活跃能量补分。',
      keys: doubleKeys,
      subheadings: { DoubleJihuaMode: '选关策略 · 双倍插队与朝夕补分共用' },
    },
    {
      id: 'fallback',
      title: '兜底副本与默认目标',
      description: '「刷取顺序」中的目标都完成或未开启后，剩余活跃能量投入所选兜底副本；三个幻境的默认与备用目标统一在此设置，方便对照切换。',
      keys: fallbackKeys,
      subheadings: { JihuaTarget: '各幻境默认 / 备用目标' },
    },
    {
      id: 'collection',
      title: '五星闪光 · 收集设置',
      description: realmFarmOnlyEnabled(values)
        ? '用于「五星闪光」追全或灵感迸发双倍追全。只刷胚子时不升级、不分解、不修改锁定。'
        : '用于「五星闪光」追全或灵感迸发双倍追全：整理仓库、升级与收集新颜色。',
      keys: collectionKeys,
    },
    {
      id: 'materials',
      title: '素材激化 · 规划与消耗',
      description: hasCommonToggle
        ? '控制焕新与激化双倍是否动用体力刷取纯真丝线／噗灵，并设定素材激化界面兑换奖励时消耗的收集材料。'
        : '设定素材激化界面兑换奖励时消耗的收集材料；焕新备料、双倍激化、兜底激化、闪光补噗灵与朝夕心愿激化共用此选择。',
      keys: materialKeys,
      ...(hasCommonToggle ? { subheadings: { JihuaMaterialMode: '激化兑换所消耗的收集材料（共用）' } } : {}),
    },
  ];
  return sections.filter((section) => section.keys.length > 0);
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
