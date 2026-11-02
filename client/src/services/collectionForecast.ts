/** Monte Carlo of the collection's daily rule, from the account's current stocks.
 *
 * Each day: upgrade held embryos; if bling stops them, the day's energy buys bling;
 * otherwise farm the source with the most expected new colours and upgrade again.
 * An estimate for planning: colour rates are unofficial and game events are ignored.
 */
export interface ForecastPart { source: string; normalMissing: number; normalTotal: number; rainbowMissing: boolean }
export interface ForecastInput {
  parts: ForecastPart[];
  rainbow: number;
  runsPerDay: number;
  blingPerRun: number;
  blingPerDay: number;
  bling: number;
  particles: number;
  duplicates: number;
  embryos: number;
  capacity: number;
  capacityLimit: number;
  decompose: boolean;
}
export interface ForecastMilestone { key: string; label: string; low: number | null; day: number | null; high: number | null }
export interface ForecastSeries { low: number[]; median: number[]; high: number[] }
export type ForecastResource = 'bling' | 'particles' | 'embryos' | 'capacity';
export interface ForecastResult extends ForecastSeries {
  owned: number;
  total: number;
  days: number[];
  /** End-of-day stocks, sampled on the same days as the colour curve. */
  resources: Record<ForecastResource, ForecastSeries>;
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
  parts.forEach((part, index) => sources.set(part.source, [...sources.get(part.source) ?? [], index]));
  const chance = (index: number) => {
    const part = parts[index];
    return part.normalMissing * (1 - input.rainbow) / part.normalTotal + (part.rainbowMissing ? input.rainbow : 0);
  };
  const open = (index: number) => parts[index].normalMissing > 0 || parts[index].rainbowMissing;
  const firstOpen = parts.findIndex((_, index) => open(index));
  const total = parts.reduce((sum, part) => sum + part.normalMissing + Number(part.rainbowMissing), 0);
  const normalTotal = parts.reduce((sum, part) => sum + part.normalMissing, 0);
  // Held embryos have no recorded part here; spread them across the open parts.
  const openParts = parts.map((_, index) => index).filter(open);
  let queue = Array.from({ length: firstOpen < 0 ? 0 : input.embryos }, () => openParts[Math.floor(rand() * openParts.length)]);
  let { bling, particles, duplicates, capacity } = input;
  let done = 0;
  let normalDone = 0;
  let carry = 0;
  let firstBling: number | null = null;
  let firstFull: number | null = null;
  const curve: number[] = [];
  const stocks: Record<ForecastResource, number[]> = { bling: [], particles: [], embryos: [], capacity: [] };
  const reached: (number | null)[] = [null, null, null];
  let normalDay: number | null = normalTotal ? null : 0;

  const upgrade = () => {
    for (;;) {
      queue = queue.filter(open);
      if (!queue.length) return 'done';
      if (bling < COST_BLING) return 'bling';
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
      const roll = rand();
      if (roll < input.rainbow) {
        if (part.rainbowMissing) { part.rainbowMissing = false; done++; } else duplicates++;
      } else if ((roll - input.rainbow) / (1 - input.rainbow) * part.normalTotal < part.normalMissing) {
        part.normalMissing--;
        done++;
        normalDone++;
      } else duplicates++;
    }
  };

  for (let day = 1; day <= MAX_DAYS && done < total; day++) {
    bling += input.blingPerDay;
    carry += input.runsPerDay;
    const runs = Math.floor(carry);
    carry -= runs;
    const status = upgrade();
    if (status === 'bling') {
      firstBling ??= day;
      bling += runs * input.blingPerRun;
    } else {
      let source = '';
      let value = -1;
      for (const [name, indices] of sources) {
        const expected = indices.reduce((sum, index) => sum + chance(index), 0);
        if (expected > value) { source = name; value = expected; }
      }
      if (capacity >= input.capacityLimit) {
        firstFull ??= day;
        if (input.decompose && !queue.length) {
          const cleared = Math.min(DECOMPOSE_BATCH, duplicates);
          duplicates -= cleared;
          particles += cleared * DUPLICATE_PARTICLES;
          capacity -= Math.min(DECOMPOSE_BATCH, capacity);
        }
      }
      if (capacity < input.capacityLimit) {
        const indices = sources.get(source)!;
        for (let run = 0; run < runs; run++) {
          const index = indices[Math.floor(rand() * indices.length)];
          particles += LOW_RARITY_PARTICLES;
          capacity++;
          if (open(index)) queue.push(index);
          else { particles += EMBRYO_PARTICLES; capacity--; }
        }
      }
    }
    upgrade();
    curve.push(done);
    stocks.bling.push(bling);
    // Spare duplicates become particles on demand, so they count as available.
    stocks.particles.push(particles + duplicates * DUPLICATE_PARTICLES);
    stocks.embryos.push(queue.length);
    stocks.capacity.push(capacity);
    [0.25, 0.5, 0.75].forEach((share, index) => { if (reached[index] === null && total && done >= total * share) reached[index] = day; });
    if (normalDay === null && normalDone >= normalTotal) normalDay = day;
  }
  return { curve, stocks, total, reached, normalDay, allDay: done >= total ? curve.length : null, firstBling, firstFull };
}

const percentile = (sorted: number[], share: number) => sorted[Math.min(sorted.length - 1, Math.floor(share * sorted.length))];

export function forecast(input: ForecastInput): ForecastResult {
  const trials = Array.from({ length: TRIALS }, (_, index) => trial(input, index + 1));
  const owned = input.parts.reduce((sum, part) => sum
    + part.normalTotal - part.normalMissing + Number(!part.rainbowMissing), 0);
  const total = trials[0]?.total ?? 0;
  const horizon = Math.max(1, ...trials.map((run) => run.curve.length));
  const step = Math.max(1, Math.ceil(horizon / 240));
  const days: number[] = [0];
  const low = [owned];
  const median = [owned];
  const high = [owned];
  const starts: Record<ForecastResource, number> = { bling: input.bling, embryos: input.embryos, capacity: input.capacity,
    particles: input.particles + input.duplicates * DUPLICATE_PARTICLES };
  const keys = Object.keys(starts) as ForecastResource[];
  const resources = Object.fromEntries(keys.map((key) => [key,
    { low: [starts[key]], median: [starts[key]], high: [starts[key]] }])) as Record<ForecastResource, ForecastSeries>;
  for (let day = step; day <= horizon; day += step) {
    const values = trials.map((run) => owned + (run.curve[Math.min(day, run.curve.length) - 1] ?? 0)).sort((a, b) => a - b);
    days.push(day);
    low.push(percentile(values, 0.1));
    median.push(percentile(values, 0.5));
    high.push(percentile(values, 0.9));
    for (const key of keys) {
      // A finished trial keeps its final stocks rather than dropping out of the band.
      const stocks = trials.map((run) => run.stocks[key][Math.min(day, run.stocks[key].length) - 1] ?? starts[key]).sort((a, b) => a - b);
      resources[key].low.push(percentile(stocks, 0.1));
      resources[key].median.push(percentile(stocks, 0.5));
      resources[key].high.push(percentile(stocks, 0.9));
    }
  }
  const milestone = (key: string, label: string, pick: (run: ReturnType<typeof trial>) => number | null): ForecastMilestone => {
    const values = trials.map(pick);
    const known = values.filter((value): value is number => value !== null).sort((a, b) => a - b);
    // Most trials must reach it, or the median date would describe an unlikely run.
    if (known.length < TRIALS / 2) return { key, label, low: null, day: null, high: null };
    return { key, label, low: percentile(known, 0.1), day: percentile(known, 0.5), high: percentile(known, 0.9) };
  };
  return { owned, total: owned + total, days, low, median, high, resources, milestones: [
    milestone('bling', '噗灵开始不够，要穿插刷噗灵', (run) => run.firstBling),
    milestone('half', '缺的颜色收齐一半', (run) => run.reached[1]),
    milestone('full', '仓库第一次满', (run) => run.firstFull),
    milestone('normal', '普通色全部收齐', (run) => run.normalDay),
    milestone('all', '全收集完成', (run) => run.allDay),
  ] };
}
