import type { FieldBonus, HeartSettings, Match, OwnedItem, PieceScore, ScoreItem, ScoringCatalog, StarCondition } from './types';

export const isAccessory = (item: ScoreItem) => item.type >= 100 && item.type < 200;
export const isFlash = (item: ScoreItem) => item.type >= 300;
export const heartGroup = (type: number) => type === 1 ? 0 : type === 3 ? 1 : [2, 4, 5].includes(type) ? 2 : type === 6 ? 3 : type === 7 ? 4 : 5;
export interface ScoreContext { counts: Record<string, number>; suits: Set<string> }
export interface Equipped { item: OwnedItem; level: number }

export function fits(item: ScoreItem, condition: { tags: number[]; props: number[] }) {
  return !isFlash(item) && condition.tags.every((tag) => item.visibleTags.includes(tag))
    && (condition.props[0] === undefined || condition.props[0] === item.major)
    && (condition.props[1] === undefined || condition.props[1] === item.minor);
}

export function suitRequirement(catalog: ScoringCatalog, ids: number[]) {
  const items = ids.map((id) => catalog.items[id]).filter(Boolean);
  return items.filter((item) => !isAccessory(item)).length + Math.min(5, items.filter(isAccessory).length);
}

export function hasSuit(catalog: ScoringCatalog, ids: number[], equipped: Map<string, number>, condition?: Extract<StarCondition, { kind: 'suit' }>) {
  if (!ids.length) return false;
  let accessories = 0;
  for (const id of ids) {
    const item = catalog.items[id];
    if (!item) return false;
    const level = equipped.get(String(id));
    const qualifies = level !== undefined && (!condition || fits(item, condition) && (!condition.refreshed || level === 11));
    if (isAccessory(item)) { if (qualifies) accessories++; }
    else if (!qualifies) return false;
  }
  return accessories >= Math.min(5, ids.filter((id) => isAccessory(catalog.items[id])).length);
}

export function outfitContext(catalog: ScoringCatalog, equipped: Equipped[]): ScoreContext {
  const counts: Record<string, number> = {};
  const levels = new Map(equipped.map((row) => [row.item.id, row.level]));
  for (const { item } of equipped) {
    const record = catalog.items[item.id];
    if (!isFlash(record)) for (const tag of record.tags) counts[tag] = (counts[tag] ?? 0) + 1;
  }
  return { counts, suits: new Set(catalog.suits.filter((suit) => hasSuit(catalog, suit.items, levels)).map((suit) => suit.id)) };
}

export function bonusCount(bonus: FieldBonus, context: ScoreContext) {
  if (bonus.countTag !== undefined) return context.counts[bonus.countTag] ?? 0;
  if (bonus.suits) return Number(bonus.suits.some((suit) => context.suits.has(suit)));
  return 1;
}

export function upgradeCost(catalog: ScoringCatalog, item: ScoreItem, from: number | null, to: number) {
  const cost: Record<string, number> = {};
  const base = from ?? 0;
  if (isFlash(item) || base >= to) return cost;
  const curve = catalog.curves[item.curve];
  for (const [id, value] of Object.entries(curve.costs[to] ?? {})) {
    const delta = value - (curve.costs[base]?.[id] ?? 0);
    if (delta > 0) cost[id] = delta;
  }
  return cost;
}

/** Independent implementation of the observed per-piece mathematical rules.
 * Preserve JS double arithmetic and the website's two-stage clothing rounding.
 */
export function scorePiece(catalog: ScoringCatalog, owned: OwnedItem, level: number, match: Match,
  heart: HeartSettings, bonuses: FieldBonus[], context: ScoreContext): PieceScore {
  const item = catalog.items[owned.id];
  const ratio = catalog.curves[item.curve].ratios[level];
  if (!Number.isFinite(ratio)) throw new Error(`衣服等级无效：${owned.name}`);
  const flash = isFlash(item);
  const appliesHeart = match.area === 1 && !flash;
  const group = heartGroup(item.type);
  const flat = appliesHeart ? [...heart.flat] : Array(5).fill(0);
  const stat: number[] = Array(5).fill(appliesHeart ? heart.stat[group] : 0);
  const weights: number[] = Array(5).fill(0);
  let part = appliesHeart ? heart.part[group] : 0;
  let display = heart.display;
  for (const bonus of bonuses) {
    const count = bonusCount(bonus, context);
    if (!count) continue;
    if (bonus.global) { display += bonus.multiplier * count; continue; }
    if (!bonus.types.includes(item.type)
      || bonus.tags.length && !bonus.tags.some((tag) => item.tags.includes(tag))
      || bonus.major.length && !bonus.major.includes(item.major)
      || bonus.refreshed !== undefined && bonus.refreshed !== (!flash && level === 11)) continue;
    for (let index = 0; index < 5; index++) {
      flat[index] += bonus.flat[index] * count;
      stat[index] += bonus.stat[index] * count;
      weights[index] += bonus.weight[index] * count;
    }
    part += bonus.multiplier * count;
  }
  const grow = (base: number) => Math.ceil(flash ? base * ratio : Math.floor(base * ratio * 100) / 100);
  const grownAttributes = item.props.map(grow);
  const attributeScores = grownAttributes.map((value, index) =>
    (value + flat[index]) * (1 + stat[index] / 100) * match.weights[index] * (1 + weights[index] / 100));
  let tagScore = 0;
  if (!flash || level >= 5) {
    const tagWeights = flash ? match.eurekaTags : match.tags;
    const base = grow(match.tagBase);
    for (const tag of item.tags) for (const weight of tagWeights[tag] ?? []) {
      tagScore += base * weight * (item.type === 2 ? match.dressMultiplier : 1);
    }
  }
  const multiplier = 1 + part / 100 + display / 100;
  const score = Math.ceil((attributeScores.reduce((sum, value) => sum + value, 0) + tagScore) * multiplier);
  if (!Number.isFinite(score)) throw new Error('加成数值过大，请检查设置。');
  return { item: owned, type: item.type, from: owned.level, level, score, attributeScores, grownAttributes, tagScore,
    multiplier, cost: upgradeCost(catalog, item, owned.level, level) };
}

export function resolveRankThreshold(match: Match, thresholds: Record<string, number>, rank: number | string) {
  return thresholds[rank] ?? match.thresholds?.[rank];
}

export function evaluateStars(catalog: ScoringCatalog, match: Match, pieces: PieceScore[], total: number, thresholds: Record<string, number>) {
  const levels = new Map(pieces.map((piece) => [piece.item.id, piece.level]));
  const condition = (rule: StarCondition): boolean | null => {
    if (rule.kind === 'rank') {
      const target = resolveRankThreshold(match, thresholds, rule.rank);
      return target === undefined ? null : total >= target;
    }
    if (rule.kind === 'pieces') return pieces.filter((piece) => fits(catalog.items[piece.item.id], rule)).length >= rule.count;
    return catalog.suits.some((suit) => (!rule.suit || rule.suit === suit.id) && hasSuit(catalog, suit.items, levels, rule));
  };
  return match.stars.map((star) => {
    const groups = star.groups.map((group) => group.map(condition));
    if (groups.some((group) => group.every((value) => value === true))) return true;
    if (groups.some((group) => !group.includes(false) && group.includes(null))) return null;
    return false;
  });
}
