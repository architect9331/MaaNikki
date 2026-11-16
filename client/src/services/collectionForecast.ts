/** Monte Carlo of the collection's daily rule, from the account's current stocks.
 *
 * Each day: upgrade held embryos; if bling stops them (and autoBling is on),
 * remaining energy buys bling; otherwise farm the source with the most expected
 * new colours and upgrade again.
 * An estimate for planning: colour rates are unofficial and game events are ignored.
 */
export interface ForecastPart {
  source: string;
  normalMissing: number;
  normalTotal: number;
  rainbowMissing: boolean;
  embryos?: number;
  kindOnly?: boolean;
}

export interface ForecastSchedule {
  /** One-off energy consumed by finite non-weekly goals ranked before glow (e.g. renewal + crafting). */
  priorEnergy?: number;
  /** Weekly realm schedule when enabled and ranked ahead of glow. */
  weeklyAhead?: {
    /** Current game-day weekday: 0=Monday .. 6=Sunday. */
    startWeekday: number;
    /** Remaining weekly realm energy this week. */
    thisWeekRemaining: number;
    /** Weekly realm energy reserved every Monday starting next week. */
    perWeekEnergy: number;
    /** True when weekly sits before priorEnergy in the queue. */
    beforePrior?: boolean;
  };
}

export interface ForecastInput {
  parts: ForecastPart[];
  rainbow: number;
  runsPerDay: number;
  blingPerRun: number;
  blingPerDay: number;
  bling: number;
  reserveBling?: number;
  autoBling?: boolean;
  usePartEmbryos?: boolean;
  particles: number;
  duplicates: number;
  embryos: number;
  capacity: number;
  capacityLimit: number;
  decompose: boolean;
  schedule?: ForecastSchedule;
}

export interface ForecastMilestone {
  key: string;
  label: string;
  low: number | null;
  day: number | null;
  high: number | null;
  energy?: number | null;
}

export interface ForecastSeries {
  low: number[];
  median: number[];
  high: number[];
}

export type ForecastResource = 'bling' | 'particles' | 'embryos' | 'capacity' | 'energy';

export interface ForecastResult extends ForecastSeries {
  owned: number;
  total: number;
  days: number[];
  /** End-of-day stocks and cumulative energy, sampled on the same days as the colour curve. */
  resources: Record<ForecastResource, ForecastSeries>;
  totalEnergy: { low: number | null; median: number | null; high: number | null };
  blockedByCapacity: boolean;
  milestones: ForecastMilestone[];
}

const TRIALS = 150;
const MAX_DAYS = 3650;
const COST_BLING = 72000;
const COST_PARTICLES = 360;
const DUPLICATE_PARTICLES = 300;
const EMBRYO_PARTICLES = 30;
const LOW_RARITY_PARTICLES = 65;
const DECOMPOSE_BATCH = 100;
const ENERGY_PER_RUN = 40;

function random(seed: number) {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let value = state;
    value = Math.imul(value ^ (value >>> 15), value | 1);
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
  };
}

function trial(input: ForecastInput, seed: number) {
  const rand = random(seed);
  const parts = input.parts.map((part) => ({ ...part }));
  const sources = new Map<string, number[]>();
  parts.forEach((part, index) => sources.set(part.source, [...(sources.get(part.source) ?? []), index]));
  const chance = (index: number) => {
    const part = parts[index];
    if (part.kindOnly) return part.normalMissing > 0 ? 1 : 0;
    return part.normalMissing * (1 - input.rainbow) / part.normalTotal + (part.rainbowMissing ? input.rainbow : 0);
  };
  const open = (index: number) => parts[index].normalMissing > 0 || parts[index].rainbowMissing;
  const firstOpen = parts.findIndex((_, index) => open(index));
  const total = parts.reduce((sum, part) => sum + part.normalMissing + Number(part.rainbowMissing), 0);
  const normalTotal = parts.reduce((sum, part) => sum + part.normalMissing, 0);
  const openParts = parts.map((_, index) => index).filter(open);
  const isKindOnly = parts.some((part) => part.kindOnly);

  const partEmbryoCount = openParts.reduce((sum, index) => sum + Math.max(0, parts[index].embryos ?? 0), 0);
  let queue: number[];
  if (input.usePartEmbryos && partEmbryoCount > 0) {
    queue = [];
    for (const index of openParts) {
      const rawCount = Math.max(0, parts[index].embryos ?? 0);
      const count = parts[index].kindOnly ? Math.min(1, rawCount) : rawCount;
      for (let i = 0; i < count; i++) queue.push(index);
    }
  } else if (isKindOnly) {
    queue = openParts.slice(0, Math.min(openParts.length, Math.max(0, input.embryos)));
  } else {
    // Held embryos have no recorded part here; spread them across the open parts.
    queue = Array.from({ length: firstOpen < 0 ? 0 : input.embryos }, () => openParts[Math.floor(rand() * openParts.length)]);
  }

  let { bling, particles, duplicates, capacity } = input;
  const reserveBling = Math.max(0, input.reserveBling ?? 0);
  const autoBling = input.autoBling !== false;
  const dailyEnergy = Math.max(0, input.runsPerDay * ENERGY_PER_RUN);
  let pendingPrior = Math.max(0, input.schedule?.priorEnergy ?? 0);
  const weeklyAhead = input.schedule?.weeklyAhead;
  let pendingWeekly = weeklyAhead ? Math.max(0, weeklyAhead.thisWeekRemaining) : 0;

  let done = 0;
  let normalDone = 0;
  let carry = 0;
  let energySpent = 0;
  let firstBling: number | null = null;
  let firstFull: number | null = null;
  let stalledByCapacity = false;
  const curve: number[] = [];
  const stocks: Record<ForecastResource, number[]> = { bling: [], particles: [], embryos: [], capacity: [], energy: [] };
  const reached: (number | null)[] = [null, null, null];
  const reachedEnergy: (number | null)[] = [null, null, null];
  let normalDay: number | null = normalTotal ? null : 0;
  let firstBlingEnergy: number | null = null;
  let firstFullEnergy: number | null = null;
  let normalEnergy: number | null = normalTotal ? null : 0;

  const upgrade = () => {
    for (;;) {
      queue = queue.filter(open);
      if (!queue.length) return 'done';
      if (bling < COST_BLING + reserveBling) return 'bling';
      let best = 0;
      for (let index = 1; index < queue.length; index++) if (chance(queue[index]) > chance(queue[best])) best = index;
      const target = queue[best];
      let shortfall = COST_PARTICLES - particles;
      let usedDuplicates = 0;
      let usedEmbryos = 0;
      if (shortfall > 0) {
        usedDuplicates = Math.min(duplicates, Math.ceil(shortfall / DUPLICATE_PARTICLES));
        shortfall -= usedDuplicates * DUPLICATE_PARTICLES;
        if (shortfall > 0) {
          usedEmbryos = Math.ceil(shortfall / EMBRYO_PARTICLES);
          if (usedEmbryos > queue.length - 1) return 'materials';
        }
      }
      particles += usedDuplicates * DUPLICATE_PARTICLES + usedEmbryos * EMBRYO_PARTICLES - COST_PARTICLES;
      duplicates -= usedDuplicates;
      capacity -= usedDuplicates + usedEmbryos;
      queue.splice(best, 1);
      if (usedEmbryos) {
        queue.sort((left, right) => chance(left) - chance(right));
        queue = queue.slice(usedEmbryos);
      }
      bling -= COST_BLING;
      const part = parts[target];
      if (part.kindOnly) {
        if (part.normalMissing > 0) {
          part.normalMissing = 0;
          done++;
          normalDone++;
        } else {
          duplicates++;
        }
      } else {
        const roll = rand();
        if (roll < input.rainbow) {
          if (part.rainbowMissing) {
            part.rainbowMissing = false;
            done++;
          } else duplicates++;
        } else if ((roll - input.rainbow) / (1 - input.rainbow) * part.normalTotal < part.normalMissing) {
          part.normalMissing--;
          done++;
          normalDone++;
        } else duplicates++;
      }
    }
  };

  for (let day = 1; day <= MAX_DAYS && done < total; day++) {
    bling += input.blingPerDay;
    if (weeklyAhead && day > 1 && (weeklyAhead.startWeekday + day - 1) % 7 === 0) {
      pendingWeekly += weeklyAhead.perWeekEnergy;
    }

    let availableToday = dailyEnergy;
    if (weeklyAhead?.beforePrior !== false) {
      const usedWeekly = Math.min(availableToday, pendingWeekly);
      pendingWeekly -= usedWeekly;
      availableToday -= usedWeekly;
      const usedPrior = Math.min(availableToday, pendingPrior);
      pendingPrior -= usedPrior;
      availableToday -= usedPrior;
    } else {
      const usedPrior = Math.min(availableToday, pendingPrior);
      pendingPrior -= usedPrior;
      availableToday -= usedPrior;
      const usedWeekly = Math.min(availableToday, pendingWeekly);
      pendingWeekly -= usedWeekly;
      availableToday -= usedWeekly;
    }

    carry += availableToday / ENERGY_PER_RUN;
    const runs = Math.floor(carry);
    carry -= runs;

    const status = upgrade();
    if (done < total && runs > 0) {
      if (status === 'bling' && autoBling) {
        firstBling ??= day;
        firstBlingEnergy ??= energySpent;
        bling += runs * input.blingPerRun;
        energySpent += runs * ENERGY_PER_RUN;
      } else {
        let source = '';
        let value = -1;
        const effectiveChance = (index: number) =>
          parts[index].kindOnly && (!open(index) || queue.includes(index)) ? 0 : chance(index);
        for (const [name, indices] of sources) {
          const expected = indices.reduce((sum, index) => sum + effectiveChance(index), 0);
          if (expected > value) {
            source = name;
            value = expected;
          }
        }
        if (capacity >= input.capacityLimit) {
          firstFull ??= day;
          firstFullEnergy ??= energySpent;
          if (input.decompose && !queue.length) {
            const cleared = Math.min(DECOMPOSE_BATCH, duplicates);
            duplicates -= cleared;
            particles += cleared * DUPLICATE_PARTICLES;
            capacity -= Math.min(DECOMPOSE_BATCH, capacity);
          }
        }
        if (capacity < input.capacityLimit && source) {
          const indices = sources.get(source)!;
          for (let run = 0; run < runs; run++) {
            const index = indices[Math.floor(rand() * indices.length)];
            energySpent += ENERGY_PER_RUN;
            particles += LOW_RARITY_PARTICLES;
            capacity++;
            if (open(index) && !(parts[index].kindOnly && queue.includes(index))) queue.push(index);
            else {
              particles += EMBRYO_PARTICLES;
              capacity--;
            }
          }
        } else if (capacity >= input.capacityLimit && !input.decompose && !queue.length) {
          stalledByCapacity = true;
        }
      }
      upgrade();
    }

    curve.push(done);
    stocks.bling.push(bling);
    // Spare duplicates become particles on demand, so they count as available.
    stocks.particles.push(particles + duplicates * DUPLICATE_PARTICLES);
    stocks.embryos.push(queue.length);
    stocks.capacity.push(capacity);
    stocks.energy.push(energySpent);
    [0.25, 0.5, 0.75].forEach((share, index) => {
      if (reached[index] === null && total && done >= total * share) {
        reached[index] = day;
        reachedEnergy[index] = energySpent;
      }
    });
    if (normalDay === null && normalDone >= normalTotal) {
      normalDay = day;
      normalEnergy = energySpent;
    }
  }

  return {
    curve,
    stocks,
    total,
    reached,
    reachedEnergy,
    normalDay,
    normalEnergy,
    allDay: done >= total ? curve.length : null,
    totalEnergy: done >= total ? energySpent : null,
    firstBling,
    firstBlingEnergy,
    firstFull,
    firstFullEnergy,
    stalledByCapacity,
  };
}

const percentile = (sorted: number[], share: number) =>
  sorted[Math.min(sorted.length - 1, Math.floor(share * sorted.length))];

export function forecast(input: ForecastInput): ForecastResult {
  const trials = Array.from({ length: TRIALS }, (_, index) => trial(input, index + 1));
  const owned = input.parts.reduce(
    (sum, part) => sum + part.normalTotal - part.normalMissing + Number(!part.rainbowMissing),
    0,
  );
  const total = trials[0]?.total ?? 0;
  const horizon = Math.max(1, ...trials.map((run) => run.curve.length));
  const step = Math.max(1, Math.ceil(horizon / 240));
  const days: number[] = [0];
  const low = [owned];
  const median = [owned];
  const high = [owned];
  const starts: Record<ForecastResource, number> = {
    bling: input.bling,
    embryos: input.embryos,
    capacity: input.capacity,
    particles: input.particles + input.duplicates * DUPLICATE_PARTICLES,
    energy: 0,
  };
  const keys = Object.keys(starts) as ForecastResource[];
  const resources = Object.fromEntries(
    keys.map((key) => [key, { low: [starts[key]], median: [starts[key]], high: [starts[key]] }]),
  ) as Record<ForecastResource, ForecastSeries>;

  for (let day = step; day <= horizon; day += step) {
    const values = trials.map((run) => owned + (run.curve[Math.min(day, run.curve.length) - 1] ?? 0)).sort((a, b) => a - b);
    days.push(day);
    low.push(percentile(values, 0.1));
    median.push(percentile(values, 0.5));
    high.push(percentile(values, 0.9));
    for (const key of keys) {
      // A finished trial keeps its final stocks rather than dropping out of the band.
      const stockValues = trials
        .map((run) => run.stocks[key][Math.min(day, run.stocks[key].length) - 1] ?? starts[key])
        .sort((a, b) => a - b);
      resources[key].low.push(percentile(stockValues, 0.1));
      resources[key].median.push(percentile(stockValues, 0.5));
      resources[key].high.push(percentile(stockValues, 0.9));
    }
  }

  const milestone = (
    key: string,
    label: string,
    pickDay: (run: ReturnType<typeof trial>) => number | null,
    pickEnergy: (run: ReturnType<typeof trial>) => number | null,
  ): ForecastMilestone => {
    const known = trials
      .map((run) => ({ day: pickDay(run), energy: pickEnergy(run) }))
      .filter((entry): entry is { day: number; energy: number | null } => entry.day !== null);
    if (known.length < TRIALS / 2) return { key, label, low: null, day: null, high: null, energy: null };
    const sortedDays = known.map((entry) => entry.day).sort((a, b) => a - b);
    const knownEnergy = known
      .map((entry) => entry.energy)
      .filter((value): value is number => value !== null)
      .sort((a, b) => a - b);
    return {
      key,
      label,
      low: percentile(sortedDays, 0.1),
      day: percentile(sortedDays, 0.5),
      high: percentile(sortedDays, 0.9),
      energy: knownEnergy.length ? percentile(knownEnergy, 0.5) : null,
    };
  };

  const completedEnergies = trials
    .map((run) => run.totalEnergy)
    .filter((value): value is number => value !== null)
    .sort((a, b) => a - b);
  const totalEnergy =
    completedEnergies.length >= TRIALS / 2
      ? {
          low: percentile(completedEnergies, 0.1),
          median: percentile(completedEnergies, 0.5),
          high: percentile(completedEnergies, 0.9),
        }
      : { low: null, median: null, high: null };

  const blockedByCapacity = trials.filter((run) => run.stalledByCapacity).length >= TRIALS / 2;

  const isKindOnly = input.parts.some((part) => part.kindOnly);
  const milestones: ForecastMilestone[] = [
    milestone(
      'bling',
      (input.reserveBling ?? 0) > 0 ? '噗灵触及保留线，要穿插刷噗灵' : '噗灵开始不够，要穿插刷噗灵',
      (run) => run.firstBling,
      (run) => run.firstBlingEnergy,
    ),
    milestone(
      'half',
      isKindOnly ? '缺的种类收齐一半' : '缺的颜色收齐一半',
      (run) => run.reached[1],
      (run) => run.reachedEnergy[1],
    ),
    milestone('full', '仓库第一次满', (run) => run.firstFull, (run) => run.firstFullEnergy),
    ...(isKindOnly
      ? []
      : [milestone('normal', '普通色全部收齐', (run) => run.normalDay, (run) => run.normalEnergy)]),
    milestone('all', isKindOnly ? '战力收齐完成' : '全收集完成', (run) => run.allDay, (run) => run.totalEnergy),
  ];

  return {
    owned,
    total: owned + total,
    days,
    low,
    median,
    high,
    resources,
    totalEnergy,
    blockedByCapacity,
    milestones,
  };
}
