import { bonusCount, evaluateStars, fits, isAccessory, isFlash, outfitContext, scorePiece, suitRequirement, type ScoreContext } from './math';
import type { CalculationRequest, CalculationResult, FieldBonus, Match, OutfitResult, OwnedItem, PieceScore, ScoreItem, StarCondition } from './types';

interface Requirement { min: number; max: number; accepts: (item: ScoreItem, level: number) => boolean }
interface Candidate { owned: OwnedItem; level: number; piece: PieceScore }
interface SearchState { score: number; accessories: number; counts: number[]; pieces: Candidate[] }
class SearchLimit extends Error {}
// A user-invoked search runs off the UI thread. Stop expanding after this
// responsiveness budget and report the best feasible solution as approximate.
const SEARCH_TIME_MS = 6000;

function isBetter(left: OutfitResult, right: OutfitResult | null, goal: 'score' | 'stars') {
  if (!right) return true;
  if (goal === 'stars') {
    const quality = (value: OutfitResult) => value.targetMet === true ? 2 : value.targetMet === null ? 1 : 0;
    if (quality(left) !== quality(right)) return quality(left) > quality(right);
    const passed = (value: OutfitResult) => value.stars.filter((star) => star === true).length;
    if (passed(left) !== passed(right)) return passed(left) > passed(right);
  }
  return left.score > right.score;
}

/** Branch on conflicting pieces, recomputing the per-slot optimum for each
 * exclusion. This preserves weaker candidates that may avoid a conflict. */
function conflictPair(request: CalculationRequest, pieces: Candidate[]) {
  for (let index = 0; index < pieces.length; index++) {
    const item = request.catalog.items[pieces[index].owned.id];
    for (const other of pieces.slice(index + 1)) {
      const record = request.catalog.items[other.owned.id];
      if (item.excludes.includes(record.id) || record.excludes.includes(item.id)
        || item.excludesTypes.includes(record.type) || record.excludesTypes.includes(item.type)) {
        return [String(item.id), String(record.id)];
      }
    }
  }
  return null;
}

function solveSlots(request: CalculationRequest, candidates: Candidate[], requirements: Requirement[], blocked: Set<string>, check: () => void) {
  const byType = new Map<number, Candidate[]>();
  for (const candidate of candidates) {
    if (blocked.has(candidate.owned.id)) continue;
    const type = candidate.piece.type;
    byType.set(type, [...byType.get(type) ?? [], candidate]);
  }
  // Dominance is safe only within identical requirement contributions. If a
  // discarded choice avoids a conflict, the exclusion branch reinstates it.
  for (const [type, choices] of byType) {
    const best = new Map<string, Candidate>();
    for (const choice of choices) {
      const item = request.catalog.items[choice.owned.id];
      const key = requirements.map((rule) => Number(rule.accepts(item, choice.level))).join(',');
      const previous = best.get(key);
      if (!previous || choice.piece.score > previous.piece.score) best.set(key, choice);
    }
    byType.set(type, [...best.values()]);
  }
  let best: SearchState | null = null;
  let expanded = 0;
  for (const required of [[2], [4, 5]]) {
    if (required.some((type) => !byType.has(type))) continue;
    const types = [...byType.keys()].filter((type) => ![2, 4, 5].includes(type) || required.includes(type)).sort((a, b) => a - b);
    let states: SearchState[] = [{ score: 0, accessories: 0, counts: requirements.map(() => 0), pieces: [] }];
    for (const type of types) {
      const next = new Map<string, SearchState>();
      const choices: (Candidate | null)[] = [...required.includes(type) ? [] : [null], ...byType.get(type)!];
      for (const state of states) for (const choice of choices) {
        if (++expanded % 256 === 0) check();
        const item = choice ? request.catalog.items[choice.owned.id] : null;
        const accessory = item !== null && isAccessory(item);
        if (accessory && state.accessories === 5) continue;
        const counts = state.counts.map((count, index) => count + Number(item !== null && requirements[index].accepts(item, choice!.level)));
        if (counts.some((count, index) => count > requirements[index].max)) continue;
        // Only upper-bound constraints need their full count retained.
        const bounded = counts.map((count, index) => Number.isFinite(requirements[index].max) ? count : Math.min(count, requirements[index].min));
        const accessories = state.accessories + Number(accessory);
        const score = state.score + (choice?.piece.score ?? 0);
        const key = `${accessories}:${bounded.join(',')}`;
        if ((next.get(key)?.score ?? -Infinity) >= score) continue;
        next.set(key, { score, accessories, counts: bounded, pieces: choice ? [...state.pieces, choice] : state.pieces });
      }
      states = [...next.values()];
      check();
    }
    for (const state of states) {
      if (requirements.every((rule, index) => state.counts[index] >= rule.min) && (!best || state.score > best.score)) best = state;
    }
  }
  return best;
}

function suiteRule(request: CalculationRequest, ids: number[], condition?: Extract<StarCondition, { kind: 'suit' }>): Requirement {
  const members = new Set(ids);
  return { min: suitRequirement(request.catalog, ids), max: Infinity,
    accepts: (item, level) => members.has(item.id) && (!condition || fits(item, condition) && (!condition.refreshed || level === 11)) };
}

function* starBranches(request: CalculationRequest, match: Match, candidates: Candidate[], check: () => void): Generator<Requirement[]> {
  const available = new Set(candidates.map((entry) => entry.owned.id));
  function* expandCondition(rule: StarCondition): Generator<Requirement[]> {
    if (rule.kind === 'rank') { yield []; return; }
    if (rule.kind === 'pieces') { yield [{ min: rule.count, max: Infinity, accepts: (item) => fits(item, rule) }]; return; }
    for (const suit of request.catalog.suits) {
      check();
      if (rule.suit && rule.suit !== suit.id || !suit.items.length) continue;
      const required = suiteRule(request, suit.items, rule);
      const matching = candidates.filter((candidate) => required.accepts(request.catalog.items[candidate.owned.id], candidate.level));
      if (matching.length < required.min || suit.items.some((id) => !request.catalog.items[id]
        || !isAccessory(request.catalog.items[id]) && !available.has(String(id)))) continue;
      yield [required];
    }
  }
  function* expandRules(rules: StarCondition[], index = 0, prefix: Requirement[] = []): Generator<Requirement[]> {
    if (index === rules.length) { yield prefix; return; }
    for (const requirements of expandCondition(rules[index])) yield* expandRules(rules, index + 1, [...prefix, ...requirements]);
  }
  function* expandStars(index: number, prefix: Requirement[]): Generator<Requirement[]> {
    if (index === match.stars.length) { yield prefix; return; }
    for (const group of match.stars[index].groups) {
      for (const requirements of expandRules(group)) yield* expandStars(index + 1, [...prefix, ...requirements]);
    }
    // Include subsets too: if all stars are impossible, search for the highest
    // attainable count rather than treating the score-first outfit as optimal.
    yield* expandStars(index + 1, prefix);
  }
  yield* expandStars(0, []);
}

/** Enumerate only composition counters that an active field bonus reads.
 * Counting constraints bind each assumed context to the actual final outfit. */
function* contexts(request: CalculationRequest, bonuses: FieldBonus[], owned: OwnedItem[], check: () => void): Generator<{ context: ScoreContext; rules: Requirement[] }> {
  const tags = [...new Set(bonuses.flatMap((bonus) => bonus.countTag === undefined ? [] : [bonus.countTag]))];
  const suits = [...new Set(bonuses.flatMap((bonus) => bonus.suits ?? []))];
  function* withSuits(index: number, context: ScoreContext, rules: Requirement[]): Generator<{ context: ScoreContext; rules: Requirement[] }> {
    check();
    if (index === suits.length) { yield { context, rules }; return; }
    const id = suits[index];
    const suit = request.catalog.suits.find((entry) => entry.id === id);
    if (!suit) { yield* withSuits(index + 1, context, rules); return; }
    const rule = suiteRule(request, suit.items);
    yield* withSuits(index + 1, context, [...rules, { ...rule, min: 0, max: rule.min - 1 }]);
    yield* withSuits(index + 1, { counts: context.counts, suits: new Set([...context.suits, id]) }, [...rules, rule]);
  }
  function* withTags(index: number, context: ScoreContext, rules: Requirement[]): Generator<{ context: ScoreContext; rules: Requirement[] }> {
    if (index === tags.length) { yield* withSuits(0, context, rules); return; }
    const tag = tags[index];
    const types = new Set(owned.map((entry) => request.catalog.items[entry.id]).filter((item) => !isFlash(item) && item.tags.includes(tag)).map((item) => item.type));
    const accessories = [...types].filter((type) => type >= 100 && type < 200).length;
    const maximum = types.size - accessories + Math.min(5, accessories);
    for (let count = 0; count <= maximum; count++) {
      check();
      yield* withTags(index + 1, { counts: { ...context.counts, [tag]: count }, suits: context.suits },
        [...rules, { min: count, max: count, accepts: (item) => !isFlash(item) && item.tags.includes(tag) }]);
    }
  }
  yield* withTags(0, { counts: {}, suits: new Set() }, []);
}

function summarize(request: CalculationRequest, match: Match, entries: Candidate[], choice: number | null): OutfitResult {
  const context = outfitContext(request.catalog, entries.map((entry) => ({ item: entry.owned, level: entry.level })));
  const bonuses = [...match.bonuses, ...choice === null ? [] : [match.choices[choice]]];
  const pieces = entries.map((entry) => scorePiece(request.catalog, entry.owned, entry.level, match, request.settings.heart, bonuses, context));
  const globalScore = bonuses.reduce((sum, bonus) => sum + (bonus.global ? bonus.add * bonusCount(bonus, context) : 0), 0);
  const score = pieces.reduce((sum, piece) => sum + piece.score, globalScore);
  const stars = evaluateStars(request.catalog, match, pieces, score, request.settings.rank_thresholds[request.matchId] ?? {});
  const cost: Record<string, number> = {};
  for (const piece of pieces) for (const [id, count] of Object.entries(piece.cost)) cost[id] = (cost[id] ?? 0) + count;
  return { score, pieces: pieces.sort((a, b) => a.type - b.type), globalScore, stars, cost, choice, exact: true,
    unknownCostGroups: [...new Set(pieces.filter((piece) => piece.from === null).map((piece) => piece.item.group).filter((id): id is string => !!id))],
    targetMet: !stars.length ? null : stars.includes(false) ? false : stars.includes(null) ? null : true };
}

function optimize(request: CalculationRequest, potential: boolean): OutfitResult | null {
  const match = request.catalog.matches[request.matchId];
  if (!match) throw new Error('请选择计分关卡。');
  const deadline = performance.now() + SEARCH_TIME_MS;
  const check = () => { if (performance.now() > deadline) throw new SearchLimit(); };
  const owned = request.owned.filter((entry) => entry.level !== null || potential && !isFlash(request.catalog.items[entry.id]));
  const choices = request.choice === null ? match.choices.length ? match.choices.map((_, index) => index) : [null] : [request.choice];
  const bestResult: { value: OutfitResult | null } = { value: null };
  try {
    for (const choice of choices) {
      if (choice !== null && !match.choices[choice]) throw new Error('场地加成选项无效。');
      const bonuses = [...match.bonuses, ...choice === null ? [] : [match.choices[choice]]];
      for (const { context, rules } of contexts(request, bonuses, owned, check)) {
        const candidates = owned.map((entry): Candidate => {
          const item = request.catalog.items[entry.id];
          const level = potential && !isFlash(item) ? request.catalog.curves[item.curve].ratios.length - 1 : entry.level!;
          return { owned: entry, level, piece: scorePiece(request.catalog, entry, level, match, request.settings.heart, bonuses, context) };
        });
        const search = (requirements: Requirement[]) => {
          const pending = [new Set<string>()];
          const visited = new Set<string>();
          while (pending.length) {
            check();
            const blocked = pending.pop()!;
            const key = [...blocked].sort().join(',');
            if (visited.has(key)) continue;
            visited.add(key);
            const best = solveSlots(request, candidates, [...rules, ...requirements], blocked, check);
            if (!best) continue;
            const conflict = conflictPair(request, best.pieces);
            if (conflict) {
              for (const id of conflict) pending.push(new Set([...blocked, id]));
              continue;
            }
            const current = summarize(request, match, best.pieces, choice);
            if (isBetter(current, bestResult.value, request.goal)) bestResult.value = current;
          }
        };
        // Establish a valid score-first result before searching star constraints.
        search([]);
        if (request.goal === 'stars' && match.stars.length) {
          for (const branch of starBranches(request, match, candidates, check)) search(branch);
        }
      }
    }
  } catch (error) {
    if (!(error instanceof SearchLimit)) throw error;
    if (bestResult.value) bestResult.value.exact = false;
  }
  return bestResult.value;
}

export function calculate(request: CalculationRequest, progress?: (stage: string) => void): CalculationResult {
  const started = performance.now();
  progress?.('正在计算已知等级搭配');
  const current = optimize(request, false);
  progress?.('正在计算焕新潜力搭配');
  const potential = optimize(request, true);
  return { current, potential, unknownGroups: new Set(request.owned.filter((item) => item.level === null).map((item) => item.group)).size,
    elapsedMs: Math.round(performance.now() - started) };
}
