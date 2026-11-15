import { gameDay, periodKey, type AccountView, type MaterialHistoryPoint } from '@/stores/accountStateStore';
import { getBlessingProgress } from '@/utils/blessingProgress';
import {
  realmCollectionWorkflow,
  realmReserveBling,
  weeklyTargets,
  type RealmGoal,
  type RealmQueueEntry,
} from '@/utils/nikkiOptions';
import type { OptionValue } from '@/types/interface';
import type {
  ForecastInput,
  ForecastPart,
  ForecastResult,
  ForecastSchedule,
} from './collectionForecast';
import {
  addCosts,
  buildRenewalGroups,
  craftingRequirement,
  materialGroup,
  MAX_LEVEL,
  renewalCost,
  RENEWED_LEVEL,
  renewalSource,
  summarizeMaterials,
  type MaterialRow,
  type MaterialTotals,
  type RenewalGroup,
} from './scoring/materialPlanning';
import { buildOwnedProfile } from './scoring/profile';
import type { MaterialsState, ScoringPayload, ScoringSettings } from './scoring/types';

export const WEEKLY_ENERGY_PER_RUN = 60;
const BLING_ID = '992617511';
const SHINY_BUBBLE_ID = '50361309';
const THREAD_ID = '3448636403';
const MAX_FORECAST_DAYS = 3650;

export interface RainbowPreset {
  id: string;
  label: string;
  rate: number;
  note: string;
}

export interface HistoryPoint {
  day: string;
  value: number;
}

export interface WeeklyEstimate {
  kind: 'weekly';
  selectedTargets: string[];
  selectedCount: number;
  perWeekEnergy: number;
  perWeekRuns: number;
  thisWeekKnown: boolean;
  finishedThisWeek: number;
  totalWeeklyCap: number;
  remainingRunsThisWeek: number;
  /** Energy needed for the remainder of the current week. */
  energy: number;
  days: number;
  status: 'done_this_week' | 'pending';
}

export interface TrendMilestone {
  key: string;
  label: string;
  day: number | null;
  energy: number | null;
}

export interface MaterialTrendSeries {
  days: number[];
  progressPct: number[];
  cumulativeEnergy: number[];
  remainingEnergy: number[];
  missingUnits: number[];
  /** For crafting: cumulative energy if jihua bling/thread are also farmed after crystals. For renewal: remaining bling deficit. */
  secondaryMetric: number[];
  milestones: TrendMilestone[];
  totalRequiredEnergy: number;
  exceededHorizon: boolean;
}

export interface MaterialGoalForecast {
  kind: 'renewal' | 'crafting';
  status: 'needs_wardrobe' | 'done' | 'ready';
  mode: 'all' | 'selected';
  scope?: 'all' | 'suits';
  source?: 'plan' | 'targets';
  priorityPendingCount?: number;
  priorityEnergy?: number;
  totals: MaterialTotals;
  /** Materials actually farmed by 幻境挑战 for this goal (for crafting, only trial crystals; for renewal, all farmable materials). */
  realmTotals: MaterialTotals;
  /** Direct sum of missing realm material energy without passive bling offset. */
  rawEnergy: number;
  rawDays: number;
  /** Expected active energy consumed by 幻境挑战 for this goal. */
  energy: number;
  days: number;
  runs: number;
  blingSavedEnergy: number;
  allTrackedEnergy: number;
  allTrackedDays: number;
  trend: MaterialTrendSeries;
  history: { day: string; energy: number; progressPct: number }[];
}

export interface BlessingContext {
  status: 'needs_blessing' | 'empty_selection' | 'done' | 'ready';
  progress: ReturnType<typeof getBlessingProgress>;
  planMode: 'all' | 'selected';
  isFarmOnly: boolean;
  autoBling: boolean;
  reserveBling: number;
  decomposeEnabled: boolean;
  scopeOwned: number;
  scopeTarget: number;
  scopeSetsCount: number;
  totalSetsCount: number;
  completeSets: number;
  completeParts: number;
  rainbowOwned: number;
  rainbowTotal: number;
  scopeComplete: number;
  scopeNext: [string, number] | undefined;
  missingBySource: Map<string, number>;
  presets: RainbowPreset[];
  history: { day: string; owned: number }[];
  baseInput: Omit<ForecastInput, 'rainbow' | 'runsPerDay' | 'blingPerDay'> | null;
  needBling: boolean;
  spendableBling: number | null;
  affordableUpgrades: number;
  particleTotal: number;
}

export interface QueueGoalEstimate {
  goal: RealmGoal;
  enabled: boolean;
  rank: number | null;
  status: 'ready' | 'done' | 'needs_data' | 'computing' | 'stalled';
  /** Energy for this goal alone. */
  ownEnergy: number | null;
  ownEnergyLow?: number | null;
  ownEnergyHigh?: number | null;
  /** Standalone days using 100% of dailyEnergy. */
  ownDays: number | null;
  ownDaysLow?: number | null;
  ownDaysHigh?: number | null;
  /** Calendar day (1-based) when this goal completes under the current queue order (null if disabled, waiting on unknown, or >10y). */
  queuedFinishDay: number | null;
  /** Day when this goal starts getting energy in the queue. */
  queuedStartDay: number | null;
  /** Weekly recurring energy per week (for weekly). */
  weeklyPerWeek?: number;
  /** Extra context hint for the row. */
  note?: string;
}

export interface QueueCombinedTrend {
  days: number[];
  cumulativeEnergy: number[];
  remainingEnergy: number[];
  milestones: TrendMilestone[];
}

export interface QueueScheduleSummary {
  goals: Record<RealmGoal, QueueGoalEstimate>;
  glowSchedule: ForecastSchedule;
  /** Total finite energy of currently enabled goals (remaining this week + renewal + crafting + glow). */
  totalFiniteEnergy: number | null;
  /** Total days to finish all enabled finite goals (excluding recurring future weeks once all downstream goals finish). */
  totalFiniteDays: number | null;
  hasUnknownEnabled: boolean;
  hasInfiniteOrStalled: boolean;
  combinedTrend: QueueCombinedTrend;
}

export function currentGameWeekday(now = Date.now()): number {
  return (gameDay(now).getUTCDay() + 6) % 7;
}

export function currentWeekKey(now = Date.now()): string {
  return periodKey('weekly_free_gifts', now);
}

/** Deterministic estimate for 每周幻境 (60 energy per selected boss). */
export function computeWeeklyEstimate(
  optionValues: Record<string, OptionValue> | undefined,
  account: AccountView | null,
  dailyEnergy: number,
): WeeklyEstimate {
  const selectedTargets = weeklyTargets(optionValues);
  const selectedCount = selectedTargets.length;
  const perWeekEnergy = selectedCount * WEEKLY_ENERGY_PER_RUN;
  const obs = account?.observations?.weekly_realm;
  const weekKey = currentWeekKey();
  const isCurrentWeek = Boolean(obs && obs.period === weekKey && typeof obs.total === 'number');
  const totalWeeklyCap = isCurrentWeek ? Math.max(1, Number(obs?.total ?? 2)) : 2;
  const finishedThisWeek = isCurrentWeek ? Math.max(0, Number(obs?.finished ?? 0)) : 0;

  let remainingRunsThisWeek: number;
  if (isCurrentWeek) {
    const globalRemaining = Math.max(0, totalWeeklyCap - finishedThisWeek);
    if (Array.isArray(obs?.completed_targets) && obs.completed_targets.length > 0) {
      const doneSet = new Set(obs.completed_targets);
      const pendingTargets = selectedTargets.filter((id) => !doneSet.has(id)).length;
      remainingRunsThisWeek = Math.min(pendingTargets, globalRemaining);
    } else {
      remainingRunsThisWeek = Math.min(selectedCount, globalRemaining);
    }
  } else {
    remainingRunsThisWeek = Math.min(selectedCount, totalWeeklyCap);
  }

  const energy = remainingRunsThisWeek * WEEKLY_ENERGY_PER_RUN;
  const safeDaily = Math.max(10, dailyEnergy);
  const days = energy > 0 ? Math.ceil(energy / safeDaily) : 0;

  return {
    kind: 'weekly',
    selectedTargets,
    selectedCount,
    perWeekEnergy,
    perWeekRuns: Math.min(selectedCount, totalWeeklyCap),
    thisWeekKnown: isCurrentWeek,
    finishedThisWeek,
    totalWeeklyCap,
    remainingRunsThisWeek,
    energy,
    days,
    status: energy === 0 ? 'done_this_week' : 'pending',
  };
}

interface SimItem {
  id: string;
  name: string;
  group: MaterialRow['group'];
  priority: number;
  missing: number;
  drop: number;
  energyPerRun: number;
}

function sampleDayPoints(
  totalDays: number,
  dailySnapshots: {
    day: number;
    progressPct: number;
    cumulativeEnergy: number;
    remainingEnergy: number;
    missingUnits: number;
    secondaryMetric: number;
  }[],
): Omit<MaterialTrendSeries, 'milestones' | 'totalRequiredEnergy' | 'exceededHorizon'> {
  const step = Math.max(1, Math.ceil(totalDays / 240));
  const days: number[] = [];
  const progressPct: number[] = [];
  const cumulativeEnergy: number[] = [];
  const remainingEnergy: number[] = [];
  const missingUnits: number[] = [];
  const secondaryMetric: number[] = [];

  for (let index = 0; index < dailySnapshots.length; index++) {
    const snap = dailySnapshots[index];
    if (index === 0 || index === dailySnapshots.length - 1 || snap.day % step === 0) {
      days.push(snap.day);
      progressPct.push(snap.progressPct);
      cumulativeEnergy.push(snap.cumulativeEnergy);
      remainingEnergy.push(snap.remainingEnergy);
      missingUnits.push(snap.missingUnits);
      secondaryMetric.push(snap.secondaryMetric);
    }
  }
  return { days, progressPct, cumulativeEnergy, remainingEnergy, missingUnits, secondaryMetric };
}

/** Deterministic daily simulator for crafting (monster_craft + optional jihua) and renewal (bubbles/thread first, bling last). */
function simulateMaterialTrend(options: {
  kind: 'crafting' | 'renewal';
  rows: MaterialRow[];
  crafting: ScoringPayload['crafting_catalog'];
  dailyEnergy: number;
  dailyBlingIncome: number;
  extraEnergy?: number;
  includeJihuaInPrimary: boolean;
  totalRequiredEnergy: number;
}): {
  energy: number;
  days: number;
  runs: number;
  blingSavedEnergy: number;
  trend: MaterialTrendSeries;
} {
  const {
    kind,
    rows,
    crafting,
    dailyEnergy,
    dailyBlingIncome,
    extraEnergy = 0,
    includeJihuaInPrimary,
    totalRequiredEnergy,
  } = options;
  const safeDaily = Math.max(10, dailyEnergy);
  const mats = crafting?.materials ?? {};

  const toSimItem = (row: MaterialRow, priority: number): SimItem | null => {
    const meta = mats[row.id];
    const drop = meta?.drop ?? 0;
    const energyPerRun = meta?.energy ?? 0;
    if (!meta?.realm || drop <= 0 || energyPerRun <= 0 || row.missing <= 0) return null;
    return { id: row.id, name: row.name, group: row.group, priority, missing: row.missing, drop, energyPerRun };
  };

  const primaryItems: SimItem[] = [];
  const secondaryJihua: SimItem[] = [];
  for (const row of rows) {
    if (row.missing <= 0) continue;
    if (kind === 'crafting') {
      if (row.group === 'crystal') {
        const item = toSimItem(row, 0);
        if (item) primaryItems.push(item);
      } else if (row.group === 'jihua') {
        const item = toSimItem(row, 1);
        if (item) secondaryJihua.push(item);
      }
    } else {
      // Farm bubbles and pure thread first (whichever has the largest energy gap); farm bling last.
      const priority = row.id === BLING_ID ? 1 : 0;
      const item = toSimItem(row, priority);
      if (item) primaryItems.push(item);
    }
  }

  const rawPrimaryEnergy = primaryItems.reduce(
    (sum, item) => sum + Math.ceil(item.missing / item.drop) * item.energyPerRun,
    0,
  );

  const itemEnergyGap = (item: SimItem) =>
    item.missing > 0 ? Math.ceil(item.missing / item.drop) * item.energyPerRun : 0;
  const sortQueue = (items: SimItem[]) =>
    items.sort(
      (a, b) =>
        a.priority - b.priority
        || itemEnergyGap(b) - itemEnergyGap(a)
        || b.missing - a.missing
        || a.id.localeCompare(b.id),
    );

  const remainingPrimaryEnergy = (items: SimItem[]) =>
    items.reduce((sum, item) => sum + (item.missing > 0 ? Math.ceil(item.missing / item.drop) * item.energyPerRun : 0), 0);
  const remainingPrimaryUnits = (items: SimItem[]) =>
    items.reduce((sum, item) => sum + (item.id === BLING_ID ? 0 : item.missing), 0);
  const remainingBling = (items: SimItem[]) => items.find((item) => item.id === BLING_ID)?.missing ?? 0;

  const progressFromRemaining = (remEnergy: number) => {
    if (totalRequiredEnergy <= 0) return 100;
    return Math.max(0, Math.min(100, ((totalRequiredEnergy - remEnergy) / totalRequiredEnergy) * 100));
  };

  // Track group completion milestones.
  const hasGroup = (groups: MaterialRow['group'][]) => primaryItems.some((item) => groups.includes(item.group));
  const groupDone = (items: SimItem[], groups: MaterialRow['group'][]) =>
    items.every((item) => !groups.includes(item.group) || item.missing <= 0);

  let energySpent = 0;
  let totalRuns = 0;
  let secondarySpent = 0;
  let blingSurplus = 0;
  let energyCarry = Math.max(0, extraEnergy);

  const milestones: Record<string, TrendMilestone> = {};
  const recordMilestone = (key: string, label: string, day: number | null, energy: number | null) => {
    if (!(key in milestones)) milestones[key] = { key, label, day, energy };
  };

  const applyPassiveBling = (items: SimItem[], amount: number) => {
    if (amount <= 0) return;
    const blingItem = items.find((item) => item.id === BLING_ID);
    if (!blingItem || blingItem.missing <= 0) {
      blingSurplus += amount;
      return;
    }
    const totalAvail = amount + blingSurplus;
    const used = Math.min(blingItem.missing, totalAvail);
    blingItem.missing -= used;
    blingSurplus = totalAvail - used;
  };

  const spendEnergyOnItems = (items: SimItem[], budget: number): { used: number; runsDone: number } => {
    let pool = budget;
    let used = 0;
    let runsDone = 0;
    while (pool > 0) {
      sortQueue(items);
      const next = items.find((item) => item.missing > 0 && pool >= item.energyPerRun);
      if (!next) break;
      const runsNeeded = Math.ceil(next.missing / next.drop);
      const maxRuns = Math.floor(pool / next.energyPerRun);
      const runs = Math.min(runsNeeded, maxRuns);
      if (runs <= 0) break;
      const cost = runs * next.energyPerRun;
      next.missing = Math.max(0, next.missing - runs * next.drop);
      pool -= cost;
      used += cost;
      runsDone += runs;
    }
    return { used, runsDone };
  };

  // Apply day-0 extraEnergy if any.
  if (energyCarry > 0 && primaryItems.some((item) => item.missing > 0)) {
    const res = spendEnergyOnItems(primaryItems, energyCarry);
    energySpent += res.used;
    totalRuns += res.runsDone;
    energyCarry -= res.used;
  }

  const initialRemEnergy = remainingPrimaryEnergy(primaryItems);
  const snapshots: {
    day: number;
    progressPct: number;
    cumulativeEnergy: number;
    remainingEnergy: number;
    missingUnits: number;
    secondaryMetric: number;
  }[] = [
    {
      day: 0,
      progressPct: progressFromRemaining(initialRemEnergy),
      cumulativeEnergy: energySpent,
      remainingEnergy: initialRemEnergy,
      missingUnits: remainingPrimaryUnits(primaryItems),
      secondaryMetric:
        kind === 'crafting'
          ? secondaryJihua.reduce((sum, item) => sum + Math.ceil(item.missing / item.drop) * item.energyPerRun, 0)
          : remainingBling(primaryItems),
    },
  ];

  const hadVortex = hasGroup(['vortex']);
  const hadChroma = hasGroup(['chroma']);
  const hadJihuaNonBling = primaryItems.some((item) => item.id === SHINY_BUBBLE_ID || item.id === THREAD_ID);
  const halfTarget = initialRemEnergy / 2;

  let day = 0;
  let exceededHorizon = false;
  const primaryActive = () => primaryItems.some((item) => item.missing > 0);
  const secondaryActive = () => !includeJihuaInPrimary && secondaryJihua.some((item) => item.missing > 0);

  let finishDay = primaryActive() ? 0 : 0;

  while ((primaryActive() || secondaryActive()) && day < MAX_FORECAST_DAYS) {
    day++;
    if (dailyBlingIncome > 0) {
      applyPassiveBling(includeJihuaInPrimary ? primaryItems : [...primaryItems, ...secondaryJihua], dailyBlingIncome);
    }

    let budget = safeDaily + energyCarry;
    energyCarry = 0;

    if (primaryActive()) {
      const res = spendEnergyOnItems(primaryItems, budget);
      energySpent += res.used;
      totalRuns += res.runsDone;
      budget -= res.used;
      if (!primaryActive()) {
        finishDay = day;
      }
    }

    if (!primaryActive() && secondaryActive()) {
      const res = spendEnergyOnItems(secondaryJihua, budget);
      secondarySpent += res.used;
      budget -= res.used;
    }

    // Carry leftover energy smaller than the next run cost.
    const nextNeed = (primaryActive() ? primaryItems : secondaryJihua).find((item) => item.missing > 0)?.energyPerRun ?? 40;
    if (budget > 0 && budget < nextNeed) {
      energyCarry = budget;
    }

    const remEnergy = remainingPrimaryEnergy(primaryItems);
    const remBling = remainingBling(primaryItems);
    const remSecondary = secondaryJihua.reduce((sum, item) => sum + (item.missing > 0 ? Math.ceil(item.missing / item.drop) * item.energyPerRun : 0), 0);

    snapshots.push({
      day,
      progressPct: progressFromRemaining(remEnergy),
      cumulativeEnergy: energySpent,
      remainingEnergy: remEnergy,
      missingUnits: remainingPrimaryUnits(primaryItems),
      secondaryMetric: kind === 'crafting' ? energySpent + secondarySpent + remSecondary : remBling,
    });

    if (hadVortex && groupDone(primaryItems, ['vortex'])) {
      recordMilestone('vortex', '1.0 涡影泡泡备齐', day, energySpent);
    }
    if (hadChroma && groupDone(primaryItems, ['chroma'])) {
      recordMilestone('chroma', '2.0 幻彩泡泡备齐', day, energySpent);
    }
    if (
      hadJihuaNonBling &&
      primaryItems.every((item) => (item.id !== SHINY_BUBBLE_ID && item.id !== THREAD_ID) || item.missing <= 0)
    ) {
      recordMilestone('jihua_mat', '闪亮泡泡与丝线备齐', day, energySpent);
    }
    if (initialRemEnergy > 0 && remEnergy <= halfTarget) {
      recordMilestone('half', '材料缺口完成一半', day, energySpent);
    }
    if (!primaryActive()) {
      recordMilestone('all', kind === 'crafting' ? '试炼晶石全部备齐' : '全部焕新材料备齐', day, energySpent);
    }
    if (kind === 'crafting' && !primaryActive() && !secondaryActive()) {
      recordMilestone('all_with_jihua', '含噗灵与丝线全部备齐', day, energySpent + secondarySpent);
    }
  }

  if (primaryActive()) {
    exceededHorizon = true;
  }

  const sampled = sampleDayPoints(Math.max(1, day), snapshots);
  const blingSavedEnergy = Math.max(0, rawPrimaryEnergy - energySpent);

  return {
    energy: energySpent,
    days: primaryActive() ? MAX_FORECAST_DAYS : finishDay,
    runs: totalRuns,
    blingSavedEnergy,
    trend: {
      ...sampled,
      milestones: Object.values(milestones),
      totalRequiredEnergy,
      exceededHorizon,
    },
  };
}

/** Unified crafting forecast matching WardrobeCraftingPlanner and agent material_pursuit. */
export function computeCraftingForecast(
  payload: ScoringPayload | null,
  settings: ScoringSettings,
  materials: MaterialsState,
  account: AccountView | null,
  dailyEnergy: number,
  dailyBlingIncome: number,
  extraEnergy = 0,
): MaterialGoalForecast {
  const crafting = payload?.crafting_catalog ?? null;
  const scope = materials.crafting_scope ?? 'all';
  const mode = materials.crafting_plan?.mode === 'selected' ? 'selected' : 'all';

  if (!payload?.snapshot || !crafting) {
    return {
      kind: 'crafting',
      status: 'needs_wardrobe',
      mode,
      scope,
      totals: { rows: [], missingKinds: 0, trackedKinds: 0, energy: 0, days: 0 },
      realmTotals: { rows: [], missingKinds: 0, trackedKinds: 0, energy: 0, days: 0 },
      rawEnergy: 0,
      rawDays: 0,
      energy: 0,
      days: 0,
      runs: 0,
      blingSavedEnergy: 0,
      allTrackedEnergy: 0,
      allTrackedDays: 0,
      trend: {
        days: [0],
        progressPct: [100],
        cumulativeEnergy: [0],
        remainingEnergy: [0],
        missingUnits: [0],
        secondaryMetric: [0],
        milestones: [],
        totalRequiredEnergy: 0,
        exceededHorizon: false,
      },
      history: [],
    };
  }

  const profile = buildOwnedProfile(payload, settings);
  const ownedIds = new Set(profile.items.map((item) => item.id));
  const selected = new Set(materials.crafting_plan?.sketch_ids ?? []);
  const allSketches = crafting.sketches ?? [];
  const scoped = allSketches.filter((sketch) => scope === 'all' || sketch.category === 'suit');
  const planned =
    mode === 'all'
      ? scoped.filter((sketch) => !ownedIds.has(sketch.id))
      : allSketches.filter((sketch) => selected.has(sketch.id) && !ownedIds.has(sketch.id));

  const required = craftingRequirement(planned);
  const snapshotKey = `${payload.snapshot.updated_at ?? ''}:${payload.snapshot.scanned_count ?? 0}`;
  if (mode === 'all' && materials.wardrobe_baseline_key === snapshotKey) {
    for (const [id, used] of Object.entries(materials.crafted_consumed ?? {})) {
      if (required[id]) required[id] = Math.max(0, required[id] - Math.max(0, Number(used)));
    }
  }

  const inventory = materials.inventory ?? {};
  const totals = summarizeMaterials(required, crafting, inventory, payload.catalog.material_names, dailyEnergy);
  const crystalRows = totals.rows.filter((row) => row.group === 'crystal');
  const crystalEnergy = crystalRows.reduce((sum, row) => sum + row.energy, 0);
  const safeDaily = Math.max(10, dailyEnergy);
  const realmTotals: MaterialTotals = {
    rows: crystalRows,
    missingKinds: crystalRows.filter((row) => row.missing > 0).length,
    trackedKinds: crystalRows.length,
    energy: crystalEnergy,
    days: Math.ceil(crystalEnergy / safeDaily),
  };

  // Total energy of crystals required by planned uncrafted sketches (starting from zero inventory) for true % progress.
  const fullCrystalTotals = summarizeMaterials(
    Object.fromEntries(
      Object.entries(required).filter(([id]) => crafting.materials[id]?.kind === 'monster_craft'),
    ),
    crafting,
    {},
    payload.catalog.material_names,
    dailyEnergy,
  );
  const totalRequiredEnergy = fullCrystalTotals.energy;

  const sim = simulateMaterialTrend({
    kind: 'crafting',
    rows: totals.rows,
    crafting,
    dailyEnergy: safeDaily,
    dailyBlingIncome,
    extraEnergy,
    includeJihuaInPrimary: false,
    totalRequiredEnergy,
  });

  const history = Object.entries(account?.observations?.material_history ?? {})
    .filter(([, point]: [string, MaterialHistoryPoint]) => typeof point.crafting_energy === 'number')
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([day, point]) => {
      const energy = Math.max(0, Number(point.crafting_energy ?? 0));
      const progressPct = totalRequiredEnergy > 0
        ? Math.max(0, Math.min(100, ((totalRequiredEnergy - energy) / totalRequiredEnergy) * 100))
        : 100;
      return { day, energy, progressPct };
    });

  return {
    kind: 'crafting',
    status: sim.energy === 0 ? 'done' : 'ready',
    mode,
    scope,
    totals,
    realmTotals,
    rawEnergy: crystalEnergy,
    rawDays: realmTotals.days,
    energy: sim.energy,
    days: sim.days,
    runs: sim.runs,
    blingSavedEnergy: 0,
    allTrackedEnergy: totals.energy,
    allTrackedDays: totals.days,
    trend: sim.trend,
    history,
  };
}

/** Unified renewal forecast matching WardrobeRenewalPlanner and agent material_pursuit. */
export function computeRenewalForecast(
  payload: ScoringPayload | null,
  settings: ScoringSettings,
  materials: MaterialsState,
  account: AccountView | null,
  dailyEnergy: number,
  dailyBlingIncome: number,
  extraEnergy = 0,
): MaterialGoalForecast {
  const source = renewalSource(materials);
  const plan = materials.renewal_plan;
  const mode = plan?.mode === 'selected' ? 'selected' : 'all';
  const crafting = payload?.crafting_catalog ?? null;

  if (source === 'plan' && !payload?.snapshot) {
    return {
      kind: 'renewal',
      status: 'needs_wardrobe',
      mode,
      source,
      totals: { rows: [], missingKinds: 0, trackedKinds: 0, energy: 0, days: 0 },
      realmTotals: { rows: [], missingKinds: 0, trackedKinds: 0, energy: 0, days: 0 },
      rawEnergy: 0,
      rawDays: 0,
      energy: 0,
      days: 0,
      runs: 0,
      blingSavedEnergy: 0,
      allTrackedEnergy: 0,
      allTrackedDays: 0,
      trend: {
        days: [0],
        progressPct: [100],
        cumulativeEnergy: [0],
        remainingEnergy: [0],
        missingUnits: [0],
        secondaryMetric: [0],
        milestones: [],
        totalRequiredEnergy: 0,
        exceededHorizon: false,
      },
      history: [],
    };
  }

  const required: Record<string, number> = {};
  const priorityRequired: Record<string, number> = {};
  let priorityPendingCount = 0;
  if (source === 'targets') {
    const glow = materials.glow_plan;
    if (glow && glow.enabled !== false && !glow.completed) {
      for (const [id, val] of Object.entries(glow.targets ?? {})) {
        const count = Math.round(Number(val) || 0);
        if (count > 0) required[id] = count;
      }
    }
  } else if (payload?.snapshot) {
    const profile = buildOwnedProfile(payload, settings);
    const groups: RenewalGroup[] = buildRenewalGroups(payload.catalog, profile);
    const targets = plan?.targets ?? {};
    const priorityTargets = plan?.priority_targets ?? {};
    const topLevel = (group: RenewalGroup) => (group.renewable ? RENEWED_LEVEL : MAX_LEVEL);
    for (const group of groups) {
      const wanted = mode === 'all' ? topLevel(group) : targets[group.id];
      if (wanted === undefined) continue;
      const target = Math.min(wanted, topLevel(group));
      if ((group.level ?? 0) < target) {
        addCosts(required, renewalCost(payload.catalog, group, target));
      }
      const prioWanted = priorityTargets[group.id];
      if (typeof prioWanted === 'number' && prioWanted > 0) {
        const pTarget = Math.min(prioWanted, target);
        if ((group.level ?? 0) < pTarget) {
          priorityPendingCount++;
          addCosts(priorityRequired, renewalCost(payload.catalog, group, pTarget));
        }
      }
    }
    const snapshotKey = `${payload.snapshot.updated_at ?? ''}:${payload.snapshot.scanned_count ?? 0}`;
    if (materials.renewal_baseline_key === `${snapshotKey}|${settings.updated_at ?? ''}`) {
      for (const [id, used] of Object.entries(materials.renewal_consumed ?? {})) {
        if (required[id]) required[id] = Math.max(0, required[id] - Math.max(0, Number(used)));
      }
    }
    for (const id of Object.keys(priorityRequired)) {
      priorityRequired[id] = Math.min(priorityRequired[id], required[id] ?? 0);
      if (priorityRequired[id] <= 0) delete priorityRequired[id];
    }
  }

  const inventory = materials.inventory ?? {};
  const totals = summarizeMaterials(required, crafting, inventory, payload?.catalog.material_names, dailyEnergy);
  const farmableRows = totals.rows.filter((row) => row.group !== 'world');
  const rawEnergy = farmableRows.reduce((sum, row) => sum + row.energy, 0);
  const safeDaily = Math.max(10, dailyEnergy);
  const realmTotals: MaterialTotals = {
    rows: farmableRows,
    missingKinds: farmableRows.filter((row) => row.missing > 0).length,
    trackedKinds: farmableRows.length,
    energy: rawEnergy,
    days: Math.ceil(rawEnergy / safeDaily),
  };

  const fullFarmableReq = Object.fromEntries(
    Object.entries(required).filter(([id]) => materialGroup(crafting?.materials[id]) !== 'world'),
  );
  const fullTotals = summarizeMaterials(fullFarmableReq, crafting, {}, payload?.catalog.material_names, dailyEnergy);
  const totalRequiredEnergy = fullTotals.energy;

  const prioTotals = summarizeMaterials(
    Object.fromEntries(
      Object.entries(priorityRequired).filter(([id]) => materialGroup(crafting?.materials[id]) !== 'world'),
    ),
    crafting,
    inventory,
    payload?.catalog.material_names,
    dailyEnergy,
  );

  const sim = simulateMaterialTrend({
    kind: 'renewal',
    rows: farmableRows,
    crafting,
    dailyEnergy: safeDaily,
    dailyBlingIncome,
    extraEnergy,
    includeJihuaInPrimary: true,
    totalRequiredEnergy,
  });

  const history = Object.entries(account?.observations?.material_history ?? {})
    .filter(([, point]: [string, MaterialHistoryPoint]) => typeof point.renewal_energy === 'number')
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([day, point]) => {
      const energy = Math.max(0, Number(point.renewal_energy ?? 0));
      const progressPct = totalRequiredEnergy > 0
        ? Math.max(0, Math.min(100, ((totalRequiredEnergy - energy) / totalRequiredEnergy) * 100))
        : 100;
      return { day, energy, progressPct };
    });

  return {
    kind: 'renewal',
    status: sim.energy === 0 ? 'done' : 'ready',
    mode,
    source,
    priorityPendingCount,
    priorityEnergy: prioTotals.energy,
    totals,
    realmTotals,
    rawEnergy,
    rawDays: realmTotals.days,
    energy: sim.energy,
    days: sim.days,
    runs: sim.runs,
    blingSavedEnergy: sim.blingSavedEnergy,
    allTrackedEnergy: totals.energy,
    allTrackedDays: totals.days,
    trend: sim.trend,
    history,
  };
}

/** Build Blessing progress + ForecastInput scoped accurately to 全收集 or 自选. */
export function buildBlessingContext(
  account: AccountView | null,
  materials: MaterialsState,
  optionValues: Record<string, OptionValue> | undefined,
  community: { rate: number; samples: number } | null = null,
): BlessingContext {
  const progress = getBlessingProgress(account);
  const { sets, partEmbryos } = progress;
  const snapshot = account?.blessing;
  const resources = snapshot?.pursuit?.resources;
  const probability = account?.catalog?.probability;
  const rolls = account?.observations?.eureka_rolls;
  const slots = ['头部', '手部', '脚部'];

  const planMode = materials.blessing_plan?.mode === 'selected' ? 'selected' : 'all';
  const chosen = new Set(materials.blessing_plan?.sets ?? []);
  const inScope = (name: string) => planMode === 'all' || chosen.has(name);
  const scopeSets = sets.filter((s) => inScope(s.name));

  const isFarmOnly = optionValues?.CollectionWorkflow
    ? realmCollectionWorkflow(optionValues) === 'FarmOnly'
    : snapshot?.pursuit?.mode === 'farm_only';
  const configuredReserveBling = optionValues?.CollectionReserveBling
    ? realmReserveBling(optionValues)
    : Math.max(0, Number(resources?.reserve_bling ?? 0) || 0);
  const reserveBling = isFarmOnly ? 0 : configuredReserveBling;
  const decomposeEnabled =
    !isFarmOnly && (optionValues?.CollectionDecompose?.type === 'switch' ? optionValues.CollectionDecompose.value : true);
  const autoBling =
    !isFarmOnly && (optionValues?.CollectionBling?.type === 'switch' ? optionValues.CollectionBling.value : true);

  const has = (name: string, slot: string, color: string) => progress.owned.has(`${name}/${slot}/${color}`);
  const count = (name: string, colors: string[], slot?: string) =>
    (slot ? [slot] : slots).reduce((n, part) => n + colors.filter((color) => has(name, part, color)).length, 0);

  const completeSets = sets.filter((s) => count(s.name, s.colors) === s.colors.length * 3).length;
  const completeParts = sets.reduce(
    (n, s) => n + slots.filter((slot) => count(s.name, s.colors, slot) === s.colors.length).length,
    0,
  );
  const rainbowOwned = sets.reduce((n, s) => n + slots.filter((slot) => has(s.name, slot, '彩')).length, 0);
  const rainbowTotal = sets.filter((s) => s.colors.includes('彩')).length * 3;

  const scopeOwned = scopeSets.reduce((n, s) => n + count(s.name, s.colors), 0);
  const scopeTarget = scopeSets.reduce((n, s) => n + s.colors.length * 3, 0);
  const scopeComplete = scopeSets.filter((s) => count(s.name, s.colors) === s.colors.length * 3).length;

  const buildParts = (targetSets: typeof sets): ForecastPart[] =>
    targetSets.flatMap((s) =>
      slots.map((slot) => {
        const normal = s.colors.filter((color) => color !== '彩');
        return {
          source: s.source ?? s.name,
          normalTotal: normal.length,
          normalMissing: normal.filter((color) => !has(s.name, slot, color)).length,
          rainbowMissing: s.colors.includes('彩') && !has(s.name, slot, '彩'),
          embryos: partEmbryos[`${s.name}/${slot}`] ?? 0,
        };
      }),
    );

  const calcMissingBySource = (partList: ForecastPart[]) => {
    const values = new Map<string, number>();
    const rainbow = probability?.iridescent ?? 0.05;
    for (const part of partList) {
      const k = Math.max(0, part.embryos ?? 0);
      const q = part.normalTotal > 0 ? (1 - rainbow) / part.normalTotal : 0;
      const marginal =
        part.normalMissing * q * Math.pow(1 - q, k) + (part.rainbowMissing ? rainbow * Math.pow(1 - rainbow, k) : 0);
      values.set(part.source, (values.get(part.source) ?? 0) + marginal);
    }
    return values;
  };

  const allParts = buildParts(sets);
  const scopeParts = planMode === 'all' ? allParts : buildParts(scopeSets);
  const missingBySource = calcMissingBySource(allParts);
  const scopeMissingMap = planMode === 'all' ? missingBySource : calcMissingBySource(scopeParts);
  const scopeNext = [...scopeMissingMap].sort((a, b) => b[1] - a[1])[0];

  const presets: RainbowPreset[] = [];
  const catalogRate = probability?.iridescent ?? 0.05;
  presets.push({ id: 'catalog', label: '基准', rate: catalogRate, note: '综合 MaaNikki 用户上报与社区统计的 5% 基准估算值。' });
  if (community) {
    presets.push({
      id: 'community',
      label: '用户统计',
      rate: community.rate,
      note: `MaaNikki 用户匿名上报，共 ${community.samples.toLocaleString('zh-CN')} 次升满。`,
    });
  }
  presets.push({ id: 'low', label: '偏低', rate: probability?.scenarios?.[0] ?? 0.0367, note: '统计数据置信区间的下沿。' });
  presets.push({ id: 'high', label: '偏高', rate: probability?.scenarios?.[2] ?? 0.0623, note: '统计数据置信区间的上沿。' });
  if (rolls?.total) {
    presets.push({
      id: 'own',
      label: '本账号',
      rate: Math.max((rolls.colors['彩'] ?? 0) / rolls.total, 0.005),
      note: `本账号 ${rolls.total} 次升满，彩色 ${rolls.colors['彩'] ?? 0} 次。样本少时波动很大。`,
    });
  }

  const history = Object.entries(account?.observations?.collection_history ?? {})
    .map(([day, row]) => ({ day, owned: row.owned }))
    .sort((a, b) => a.day.localeCompare(b.day));

  const particleTotal = (resources?.particles ?? 0) + (resources?.duplicates ?? 0) * 300;
  const spendableBling = resources?.bling == null ? null : Math.max(0, resources.bling - reserveBling);
  const affordableUpgrades = spendableBling == null ? 0 : Math.floor(spendableBling / 72000);
  const embryosToUpgrade = Math.min(
    resources?.embryos ?? 0,
    resources?.particles == null ? resources?.embryos ?? 0 : Math.max(1, Math.floor(particleTotal / 360)),
  );
  const needBling = !isFarmOnly && !!resources?.embryos && spendableBling != null && affordableUpgrades < embryosToUpgrade;

  const status: BlessingContext['status'] = !progress.known
    ? 'needs_blessing'
    : planMode === 'selected' && scopeSets.length === 0
      ? 'empty_selection'
      : scopeTarget - scopeOwned <= 0
        ? 'done'
        : 'ready';

  const hasPerPartEmbryos = Object.keys(partEmbryos).length > 0;
  const baseInput: BlessingContext['baseInput'] =
    progress.known && scopeParts.length > 0
      ? {
          parts: scopeParts,
          bling: resources?.bling ?? 0,
          reserveBling,
          autoBling,
          usePartEmbryos: hasPerPartEmbryos,
          particles: resources?.particles ?? 0,
          duplicates: resources?.duplicates ?? 0,
          embryos: resources?.embryos ?? 0,
          capacity: resources?.capacity?.[0] ?? progress.owned.size,
          capacityLimit: resources?.capacity?.[1] ?? 1000,
          decompose: decomposeEnabled,
          blingPerRun: resources?.bling_per_run ?? 80000,
        }
      : null;

  return {
    status,
    progress,
    planMode,
    isFarmOnly,
    autoBling,
    reserveBling,
    decomposeEnabled,
    scopeOwned,
    scopeTarget,
    scopeSetsCount: scopeSets.length,
    totalSetsCount: sets.length,
    completeSets,
    completeParts,
    rainbowOwned,
    rainbowTotal,
    scopeComplete,
    scopeNext,
    missingBySource,
    presets,
    history,
    baseInput,
    needBling,
    spendableBling,
    affordableUpgrades,
    particleTotal,
  };
}

/**
 * Simulate sequential execution of the deterministic prefix/suffix goals alongside
 * weekly resets, and produce the ForecastSchedule for the stochastic glow step.
 */
export function simulateQueueSchedule(options: {
  entries: RealmQueueEntry[];
  weekly: WeeklyEstimate;
  renewal: MaterialGoalForecast;
  crafting: MaterialGoalForecast;
  blessing: BlessingContext;
  glowStandalone: ForecastResult | null;
  glowQueued: ForecastResult | null;
  glowComputing: boolean;
  dailyEnergy: number;
  startWeekday?: number;
}): QueueScheduleSummary {
  const {
    entries,
    weekly,
    renewal,
    crafting,
    blessing,
    glowStandalone,
    glowQueued,
    glowComputing,
    dailyEnergy,
    startWeekday = currentGameWeekday(),
  } = options;
  const safeDaily = Math.max(10, dailyEnergy);
  const enabledEntries = entries.filter((entry) => entry.enabled);
  const rankOf = (goal: RealmGoal) => {
    const idx = enabledEntries.findIndex((entry) => entry.goal === goal);
    return idx < 0 ? null : idx + 1;
  };

  const weeklyIdx = enabledEntries.findIndex((entry) => entry.goal === 'weekly');
  const glowIdx = enabledEntries.findIndex((entry) => entry.goal === 'glow');
  const beforeGlow = glowIdx < 0 ? enabledEntries : enabledEntries.slice(0, glowIdx);
  const afterGlow = glowIdx < 0 ? [] : enabledEntries.slice(glowIdx + 1);

  // Build glowSchedule for items strictly before glow.
  const priorNonWeeklyEnergy = beforeGlow
    .filter((entry) => entry.goal === 'renewal' || entry.goal === 'crafting')
    .reduce((sum, entry) => {
      const f = entry.goal === 'renewal' ? renewal : crafting;
      return sum + (f.status === 'ready' ? f.energy : 0);
    }, 0);

  const weeklyBeforeGlow = weeklyIdx >= 0 && (glowIdx < 0 || weeklyIdx < glowIdx);
  const firstNonWeeklyBeforeGlow = beforeGlow.findIndex((entry) => entry.goal === 'renewal' || entry.goal === 'crafting');
  const weeklyBeforePrior =
    weeklyBeforeGlow && (firstNonWeeklyBeforeGlow < 0 || weeklyIdx < firstNonWeeklyBeforeGlow);

  const glowSchedule: ForecastSchedule = {
    priorEnergy: priorNonWeeklyEnergy,
    weeklyAhead: weeklyBeforeGlow
      ? {
          startWeekday,
          thisWeekRemaining: weekly.energy,
          perWeekEnergy: weekly.perWeekEnergy,
          beforePrior: weeklyBeforePrior,
        }
      : undefined,
  };

  // Helper to simulate sequential deterministic goals (weekly, renewal, crafting) starting from a given day offset.
  const simulateSegment = (
    segment: RealmQueueEntry[],
    startDayOffset: number,
    weeklyRemainingInitial: number,
    hasWeeklyReset: boolean,
  ) => {
    const remaining: Partial<Record<'weekly' | 'renewal' | 'crafting', number>> = {};
    for (const entry of segment) {
      if (entry.goal === 'weekly') remaining.weekly = weeklyRemainingInitial;
      else if (entry.goal === 'renewal') remaining.renewal = renewal.status === 'ready' ? renewal.energy : 0;
      else if (entry.goal === 'crafting') remaining.crafting = crafting.status === 'ready' ? crafting.energy : 0;
    }
    const startDays: Partial<Record<RealmGoal, number>> = {};
    const finishDays: Partial<Record<RealmGoal, number>> = {};

    const orderedSegmentGoals = segment.map((entry) => entry.goal);
    const allZero = orderedSegmentGoals.every((g) => (remaining[g as 'weekly' | 'renewal' | 'crafting'] ?? 0) <= 0);
    if (allZero) {
      for (const g of orderedSegmentGoals) {
        startDays[g] = startDayOffset;
        finishDays[g] = startDayOffset;
      }
      return { finishDay: startDayOffset, startDays, finishDays, dayPoints: [] as { day: number; energy: number }[] };
    }

    let day = startDayOffset;
    let totalSpent = 0;
    const dayPoints: { day: number; energy: number }[] = [];
    const maxLoop = startDayOffset + MAX_FORECAST_DAYS;
    const hasPending = () =>
      orderedSegmentGoals.some((g) => (remaining[g as 'weekly' | 'renewal' | 'crafting'] ?? 0) > 0);

    while (hasPending() && day < maxLoop) {
      day++;
      const weekday = (startWeekday + day - 1) % 7;
      if (hasWeeklyReset && day > 1 && weekday === 0) {
        // Weekly attempts reset to the weekly cap each Monday rather than stacking missed weeks.
        if ('weekly' in remaining) remaining.weekly = weekly.perWeekEnergy;
      }
      let budget = safeDaily;
      let spentToday = 0;
      for (const g of orderedSegmentGoals) {
        const key = g as 'weekly' | 'renewal' | 'crafting';
        const rem = remaining[key] ?? 0;
        if (rem <= 0) {
          finishDays[g] ??= day - 1;
          continue;
        }
        if (budget <= 0) break;
        startDays[g] ??= day;
        const used = Math.min(budget, rem);
        remaining[key] = rem - used;
        budget -= used;
        spentToday += used;
        if ((remaining[key] ?? 0) <= 0) {
          finishDays[g] ??= day;
        }
      }
      totalSpent += spentToday;
      dayPoints.push({ day, energy: totalSpent });
    }

    for (const g of orderedSegmentGoals) {
      startDays[g] ??= day;
      finishDays[g] ??= day;
    }
    return { finishDay: day, startDays, finishDays, dayPoints };
  };

  const prefixSim = simulateSegment(beforeGlow, 0, weekly.energy, true);

  // Evaluate glow in standalone and queued contexts.
  const activeGlowResult =
    (beforeGlow.length > 0 ? glowQueued : glowStandalone) ?? glowStandalone;
  const glowAllMilestone = activeGlowResult?.milestones.find((item) => item.key === 'all');
  const glowStandaloneAll = glowStandalone?.milestones.find((item) => item.key === 'all');
  const glowQueuedFinishDay =
    blessing.status === 'done'
      ? prefixSim.finishDay
      : blessing.status === 'ready'
        ? (glowAllMilestone?.day ?? null)
        : null;

  const crossedMondayDuringGlowOrPrefix = (endDay: number) => {
    for (let d = 2; d <= endDay; d++) {
      if ((startWeekday + d - 1) % 7 === 0) return true;
    }
    return false;
  };

  const afterSim =
    glowIdx >= 0 && (blessing.status === 'ready' || blessing.status === 'done') && glowQueuedFinishDay !== null
      ? simulateSegment(
          afterGlow,
          glowQueuedFinishDay,
          crossedMondayDuringGlowOrPrefix(glowQueuedFinishDay) ? weekly.perWeekEnergy : weekly.energy,
          true,
        )
      : null;

  const makeMaterialEstimate = (goal: 'renewal' | 'crafting', f: MaterialGoalForecast): QueueGoalEstimate => {
    const enabled = rankOf(goal) !== null;
    if (f.status === 'needs_wardrobe') {
      return {
        goal,
        enabled,
        rank: rankOf(goal),
        status: 'needs_data',
        ownEnergy: null,
        ownDays: null,
        queuedStartDay: null,
        queuedFinishDay: null,
        note: '需先读取衣柜',
      };
    }
    const startDay = enabled ? (prefixSim.startDays[goal] ?? afterSim?.startDays[goal] ?? null) : null;
    const finishDay = enabled ? (prefixSim.finishDays[goal] ?? afterSim?.finishDays[goal] ?? null) : null;
    let note: string | undefined;
    if (goal === 'crafting' && f.energy === 0 && f.allTrackedEnergy > 0) {
      note = '试炼晶石已齐（噗灵/丝线需自备）';
    } else if (goal === 'renewal' && (f.priorityPendingCount ?? 0) > 0) {
      note = `优先搭配计算 ${f.priorityPendingCount} 件（约 ${(f.priorityEnergy ?? 0).toLocaleString('zh-CN')} 体力）`;
    } else if (f.blingSavedEnergy > 0) {
      note = `日常噗灵抵扣约 ${f.blingSavedEnergy.toLocaleString('zh-CN')} 体力`;
    }
    return {
      goal,
      enabled,
      rank: rankOf(goal),
      status: f.status === 'done' ? 'done' : 'ready',
      ownEnergy: f.energy,
      ownDays: f.days,
      queuedStartDay: startDay,
      queuedFinishDay: f.energy === 0 && enabled ? (startDay ?? prefixSim.finishDay) : finishDay,
      note,
    };
  };

  const weeklyEstimate: QueueGoalEstimate = {
    goal: 'weekly',
    enabled: rankOf('weekly') !== null,
    rank: rankOf('weekly'),
    status: weekly.status === 'done_this_week' ? 'done' : 'ready',
    ownEnergy: weekly.energy,
    ownDays: weekly.days,
    queuedStartDay: rankOf('weekly') !== null ? (prefixSim.startDays.weekly ?? afterSim?.startDays.weekly ?? null) : null,
    queuedFinishDay: rankOf('weekly') !== null ? (prefixSim.finishDays.weekly ?? afterSim?.finishDays.weekly ?? null) : null,
    weeklyPerWeek: weekly.perWeekEnergy,
    note: weekly.thisWeekKnown
      ? weekly.energy === 0
        ? `本周 ${weekly.finishedThisWeek}/${weekly.totalWeeklyCap} 已清完`
        : `本周已确认 ${weekly.finishedThisWeek}/${weekly.totalWeeklyCap}`
      : '未扫过本周次数，按所选全算',
  };

  let glowEstimate: QueueGoalEstimate;
  if (blessing.status === 'needs_blessing') {
    glowEstimate = {
      goal: 'glow',
      enabled: rankOf('glow') !== null,
      rank: rankOf('glow'),
      status: 'needs_data',
      ownEnergy: null,
      ownDays: null,
      queuedStartDay: null,
      queuedFinishDay: null,
      note: '需先整理仓库或美鸭梨核对',
    };
  } else if (blessing.status === 'empty_selection') {
    glowEstimate = {
      goal: 'glow',
      enabled: rankOf('glow') !== null,
      rank: rankOf('glow'),
      status: 'done',
      ownEnergy: 0,
      ownDays: 0,
      queuedStartDay: rankOf('glow') !== null ? prefixSim.finishDay : null,
      queuedFinishDay: rankOf('glow') !== null ? prefixSim.finishDay : null,
      note: '自选尚未勾选套组',
    };
  } else if (blessing.status === 'done') {
    glowEstimate = {
      goal: 'glow',
      enabled: rankOf('glow') !== null,
      rank: rankOf('glow'),
      status: 'done',
      ownEnergy: 0,
      ownDays: 0,
      queuedStartDay: rankOf('glow') !== null ? prefixSim.finishDay : null,
      queuedFinishDay: rankOf('glow') !== null ? prefixSim.finishDay : null,
      note: '所选范围已收齐',
    };
  } else if (glowComputing && !glowStandalone && !activeGlowResult) {
    glowEstimate = {
      goal: 'glow',
      enabled: rankOf('glow') !== null,
      rank: rankOf('glow'),
      status: 'computing',
      ownEnergy: null,
      ownDays: null,
      queuedStartDay: rankOf('glow') !== null ? prefixSim.finishDay : null,
      queuedFinishDay: null,
    };
  } else if (activeGlowResult?.blockedByCapacity || glowStandalone?.blockedByCapacity) {
    glowEstimate = {
      goal: 'glow',
      enabled: rankOf('glow') !== null,
      rank: rankOf('glow'),
      status: 'stalled',
      ownEnergy: null,
      ownDays: null,
      queuedStartDay: rankOf('glow') !== null ? prefixSim.finishDay : null,
      queuedFinishDay: null,
      note: '仓库已满且未开启爆仓分解',
    };
  } else {
    const ownEnergy = glowStandalone?.totalEnergy.median ?? activeGlowResult?.totalEnergy.median ?? null;
    const ownDays = glowStandaloneAll?.day ?? glowAllMilestone?.day ?? null;
    glowEstimate = {
      goal: 'glow',
      enabled: rankOf('glow') !== null,
      rank: rankOf('glow'),
      status: glowComputing && !activeGlowResult ? 'computing' : 'ready',
      ownEnergy,
      ownEnergyLow: glowStandalone?.totalEnergy.low ?? activeGlowResult?.totalEnergy.low ?? null,
      ownEnergyHigh: glowStandalone?.totalEnergy.high ?? activeGlowResult?.totalEnergy.high ?? null,
      ownDays,
      ownDaysLow: glowStandaloneAll?.low ?? glowAllMilestone?.low ?? null,
      ownDaysHigh: glowStandaloneAll?.high ?? glowAllMilestone?.high ?? null,
      queuedStartDay: rankOf('glow') !== null ? prefixSim.finishDay : null,
      queuedFinishDay: rankOf('glow') !== null ? glowQueuedFinishDay : null,
      note: blessing.isFarmOnly ? '只刷胚子模式（按升满估算总消耗）' : undefined,
    };
  }

  const goals: Record<RealmGoal, QueueGoalEstimate> = {
    weekly: weeklyEstimate,
    renewal: makeMaterialEstimate('renewal', renewal),
    crafting: makeMaterialEstimate('crafting', crafting),
    glow: glowEstimate,
  };

  const hasUnknownEnabled = enabledEntries.some((entry) => goals[entry.goal].status === 'needs_data');
  const hasInfiniteOrStalled = enabledEntries.some(
    (entry) =>
      goals[entry.goal].status === 'stalled' ||
      (goals[entry.goal].status === 'ready' && goals[entry.goal].queuedFinishDay === null && entry.goal === 'glow'),
  );

  // Total finite energy = sum of enabled known goals (plus weekly recurrences while running upstream finite goals).
  const glowEnergyUsed =
    glowEstimate.enabled && glowEstimate.status === 'ready'
      ? (activeGlowResult?.totalEnergy.median ?? glowEstimate.ownEnergy ?? 0)
      : 0;
  const knownEnergySum = enabledEntries.reduce((sum, entry) => {
    if (entry.goal === 'glow') return sum + glowEnergyUsed;
    return sum + (goals[entry.goal].ownEnergy ?? 0);
  }, 0);

  // Add weekly runs triggered during multi-week upstream runs.
  const finalDayCandidates = enabledEntries
    .map((entry) => goals[entry.goal].queuedFinishDay)
    .filter((day): day is number => day !== null);
  const totalFiniteDays =
    enabledEntries.length === 0
      ? 0
      : hasUnknownEnabled || hasInfiniteOrStalled
        ? null
        : finalDayCandidates.length
          ? Math.max(0, ...finalDayCandidates)
          : 0;

  // Build combined queue trend curve over totalFiniteDays (or prefix + glow horizon).
  const combinedDays: number[] = [0];
  const combinedCum: number[] = [0];
  const combinedRem: number[] = [];
  const combinedMilestones: TrendMilestone[] = [];

  for (const entry of enabledEntries) {
    const g = goals[entry.goal];
    const labelMap: Record<RealmGoal, string> = {
      weekly: '本周幻境完成',
      renewal: '焕新材料备齐',
      crafting: '制衣晶石备齐',
      glow: '五星闪光收齐',
    };
    if (g.status === 'done') {
      combinedMilestones.push({ key: entry.goal, label: labelMap[entry.goal], day: g.queuedFinishDay ?? 0, energy: 0 });
    } else if (g.status === 'ready' && g.queuedFinishDay !== null) {
      combinedMilestones.push({
        key: entry.goal,
        label: labelMap[entry.goal],
        day: g.queuedFinishDay,
        energy: g.ownEnergy,
      });
    }
  }

  const horizon = Math.max(
    1,
    totalFiniteDays ?? 0,
    prefixSim.finishDay,
    activeGlowResult?.days[activeGlowResult.days.length - 1] ?? 0,
  );
  const step = Math.max(1, Math.ceil(horizon / 160));
  const glowEndDay = glowQueuedFinishDay ?? horizon;
  const countWeeklyMondaysBetween = (fromDayExclusive: number, toDayInclusive: number) => {
    if (!weeklyBeforeGlow || weekly.perWeekEnergy <= 0 || toDayInclusive <= fromDayExclusive) return 0;
    let count = 0;
    for (let d = Math.max(2, fromDayExclusive + 1); d <= toDayInclusive; d++) {
      if ((startWeekday + d - 1) % 7 === 0) count++;
    }
    return count;
  };
  const weeklyDuringGlowTotal =
    glowIdx >= 0 && blessing.status === 'ready'
      ? countWeeklyMondaysBetween(prefixSim.finishDay, glowQueuedFinishDay ?? 0) * weekly.perWeekEnergy
      : 0;

  const prefixMaxEnergy = prefixSim.dayPoints.length ? prefixSim.dayPoints[prefixSim.dayPoints.length - 1].energy : 0;
  const glowMaxEnergy = activeGlowResult?.resources.energy.median[activeGlowResult.resources.energy.median.length - 1] ?? 0;
  const afterMaxEnergy = afterSim?.dayPoints.length ? afterSim.dayPoints[afterSim.dayPoints.length - 1].energy : 0;
  const totalCurveEnergy = prefixMaxEnergy + weeklyDuringGlowTotal + glowMaxEnergy + afterMaxEnergy;

  const evalEnergyAtDay = (day: number) => {
    let e = 0;
    if (prefixSim.dayPoints.length && day > 0) {
      const idx = Math.min(day, prefixSim.dayPoints.length) - 1;
      e += idx >= 0 ? prefixSim.dayPoints[idx].energy : 0;
    }
    if (glowIdx >= 0 && activeGlowResult) {
      e += countWeeklyMondaysBetween(prefixSim.finishDay, Math.min(day, glowEndDay)) * weekly.perWeekEnergy;
      const gDays = activeGlowResult.days;
      const gSeries = activeGlowResult.resources.energy.median;
      let gIdx = 0;
      for (let i = 0; i < gDays.length; i++) {
        if (gDays[i] <= day) gIdx = i;
        else break;
      }
      e += gSeries[gIdx] ?? 0;
    }
    if (afterSim?.dayPoints.length && glowQueuedFinishDay !== null && day > glowQueuedFinishDay) {
      const rel = day - glowQueuedFinishDay;
      const idx = Math.min(rel, afterSim.dayPoints.length) - 1;
      e += idx >= 0 ? afterSim.dayPoints[idx].energy : 0;
    }
    return Math.min(totalCurveEnergy, Math.round(e));
  };

  combinedRem.push(totalCurveEnergy);
  for (let d = step; d <= horizon; d += step) {
    const cum = evalEnergyAtDay(d);
    combinedDays.push(d);
    combinedCum.push(cum);
    combinedRem.push(Math.max(0, totalCurveEnergy - cum));
  }
  if (combinedDays[combinedDays.length - 1] < horizon) {
    const cum = evalEnergyAtDay(horizon);
    combinedDays.push(horizon);
    combinedCum.push(cum);
    combinedRem.push(Math.max(0, totalCurveEnergy - cum));
  }

  return {
    goals,
    glowSchedule,
    totalFiniteEnergy: hasUnknownEnabled || hasInfiniteOrStalled ? null : Math.max(knownEnergySum, totalCurveEnergy),
    totalFiniteDays,
    hasUnknownEnabled,
    hasInfiniteOrStalled,
    combinedTrend: {
      days: combinedDays,
      cumulativeEnergy: combinedCum,
      remainingEnergy: combinedRem,
      milestones: combinedMilestones,
    },
  };
}
