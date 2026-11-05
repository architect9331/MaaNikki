import { ATTRIBUTES } from './types';
import type {
  GrowthGroup,
  Match,
  OutfitResult,
  OwnedProfile,
  PieceScore,
  ScoreItem,
  ScoringCatalog,
  ScoringSettings,
  StarCondition,
} from './types';
import { fits, isAccessory, isFlash, resolveRankThreshold, suitRequirement } from './math';
import { ITEM_SOURCES } from './wardrobeBrowserData';

const RANK_LABELS: Record<number, string> = { 1: '及格', 2: '一般', 4: '优秀', 5: '完美' };

export function tagLabel(catalog: ScoringCatalog, tagId: number | string): string {
  return catalog.tag_names?.[String(tagId)] ?? `标签${tagId}`;
}

export function formatRuleFilter(catalog: ScoringCatalog, rule: { tags: number[]; props: number[] }): string {
  const parts: string[] = [];
  for (const p of rule.props) {
    if (ATTRIBUTES[p]) parts.push(`【${ATTRIBUTES[p]}】`);
  }
  for (const t of rule.tags) {
    parts.push(`【${tagLabel(catalog, t)}】`);
  }
  return parts.join('') || '指定条件';
}

function shortPartName(catalog: ScoringCatalog, type: number): string {
  return (catalog.part_names[type] ?? String(type)).split(' / ').pop() ?? String(type);
}

/**
 * Compute how many non-conflicting outfit slots a set of qualifying ScoreItems can occupy:
 * - Hair (1): max 1
 * - Dress (2) vs Top (4) + Bottom (5): max(hasDress ? 1 : 0, (hasTop ? 1 : 0) + (hasBottom ? 1 : 0))
 * - Coat (3): max 1
 * - Socks (6): max 1
 * - Shoes (7): max 1
 * - Accessories (100..199): up to 5 distinct accessory subtypes
 */
function maxNonConflictingSlots(items: ScoreItem[]): { count: number; coveredTypes: Set<number> } {
  const types = new Set<number>();
  const accTypes = new Set<number>();
  for (const item of items) {
    if (isFlash(item)) continue;
    types.add(item.type);
    if (isAccessory(item)) accTypes.add(item.type);
  }
  const hair = types.has(1) ? 1 : 0;
  const coat = types.has(3) ? 1 : 0;
  const socks = types.has(6) ? 1 : 0;
  const shoes = types.has(7) ? 1 : 0;
  const dressOrTopBottom = Math.max(types.has(2) ? 1 : 0, (types.has(4) ? 1 : 0) + (types.has(5) ? 1 : 0));
  const accessories = Math.min(5, accTypes.size);
  return {
    count: hair + coat + socks + shoes + dressOrTopBottom + accessories,
    coveredTypes: types,
  };
}

/**
 * Identify all growth groups whose items satisfy any `pieces` or `suit` star condition of `match`.
 * Used to prioritize star-critical groups at the top of the "补充衣服等级" list.
 */
export function starRequiredGroupBadges(catalog: ScoringCatalog, match: Match): Map<string, string[]> {
  const result = new Map<string, Set<string>>();
  const addBadge = (scoreId: number | string, label: string) => {
    const gameItem = catalog.game_items[String(scoreId)];
    if (!gameItem?.group) return;
    let set = result.get(gameItem.group);
    if (!set) {
      set = new Set<string>();
      result.set(gameItem.group, set);
    }
    set.add(label);
  };

  for (const star of match.stars) {
    for (const group of star.groups) {
      for (const rule of group) {
        if (rule.kind === 'pieces') {
          const label = `星级条件 · ${formatRuleFilter(catalog, rule).replace(/【|】/g, ' ').trim()}`;
          for (const item of Object.values(catalog.items)) {
            if (fits(item, rule)) addBadge(item.id, label);
          }
        } else if (rule.kind === 'suit') {
          for (const suit of catalog.suits) {
            if (rule.suit && rule.suit !== suit.id) continue;
            const qualifying = suit.items.filter((id) => catalog.items[id] && fits(catalog.items[id], rule));
            if (qualifying.length >= suitRequirement(catalog, suit.items)) {
              const label = `星级套装 · ${suit.name}`;
              for (const id of suit.items) addBadge(id, label);
            }
          }
        }
      }
    }
  }

  return new Map([...result.entries()].map(([k, v]) => [k, [...v]]));
}

export interface UnownedRecommendation {
  id: string;
  name: string;
  partName: string;
  quality: number;
  slotNeeded: boolean;
  source?: string;
}

export interface SuitDiagnosticCandidate {
  suitId: string;
  suitName: string;
  quality: number;
  ownedCount: number;
  totalCount: number;
  complete: boolean;
  unknownGroups: GrowthGroup[];
  unrefreshedGroups: GrowthGroup[];
  missingParts: { id: string; name: string; partName: string; source?: string }[];
}

export type RuleDiagnostic =
  | {
      kind: 'rank';
      satisfied: boolean | null;
      rank: number;
      rankLabel: string;
      currentScore: number;
      targetScore?: number;
      maxScore?: number;
      isCustom: boolean;
      deficit: number | null;
      upgradeableEquipped: PieceScore[];
    }
  | {
      kind: 'pieces';
      satisfied: boolean;
      filterLabel: string;
      equippedCount: number;
      requiredCount: number;
      maxEquipableOwnedCount: number;
      unknownGroups: GrowthGroup[];
      upgradeableEquipped: PieceScore[];
      recommendedUnowned: UnownedRecommendation[];
    }
  | {
      kind: 'suit';
      satisfied: boolean;
      filterLabel: string;
      refreshed: boolean;
      candidates: SuitDiagnosticCandidate[];
    };

export interface UnmetStarDiagnostic {
  starIndex: number;
  description: string;
  status: boolean | null;
  rules: RuleDiagnostic[];
}

export function analyzeUnmetStars(
  catalog: ScoringCatalog,
  profile: OwnedProfile,
  settings: ScoringSettings,
  matchId: string,
  match: Match,
  outfit: OutfitResult,
): UnmetStarDiagnostic[] {
  const userThresholds = settings.rank_thresholds[matchId] ?? {};
  const groupById = new Map(profile.groups.map((g) => [g.id, g]));
  const ownedScoreIds = new Set(profile.items.map((item) => String(item.id)));
  const equippedLevels = new Map(outfit.pieces.map((p) => [String(p.item.id), p.level]));

  const evaluateRuleDiagnostic = (rule: StarCondition): RuleDiagnostic => {
    if (rule.kind === 'rank') {
      const targetScore = resolveRankThreshold(match, userThresholds, rule.rank);
      const isCustom = userThresholds[rule.rank] !== undefined;
      const satisfied = targetScore === undefined ? null : outfit.score >= targetScore;
      const deficit = targetScore === undefined ? null : Math.max(0, targetScore - outfit.score);
      const upgradeableEquipped = outfit.pieces.filter(
        (p) => !isFlash(catalog.items[p.item.id]) && p.level < 11,
      );
      return {
        kind: 'rank',
        satisfied,
        rank: rule.rank,
        rankLabel: RANK_LABELS[rule.rank] ?? String(rule.rank),
        currentScore: outfit.score,
        targetScore,
        maxScore: match.max_score,
        isCustom,
        deficit,
        upgradeableEquipped,
      };
    }

    if (rule.kind === 'pieces') {
      const matchingEquipped = outfit.pieces.filter((p) => fits(catalog.items[p.item.id], rule));
      const equippedCount = matchingEquipped.length;
      const satisfied = equippedCount >= rule.count;

      const ownedMatchingItems: ScoreItem[] = [];
      const unknownGroupIds = new Set<string>();
      for (const owned of profile.items) {
        const record = catalog.items[owned.id];
        if (!record || !fits(record, rule)) continue;
        ownedMatchingItems.push(record);
        if (owned.level === null && owned.group) {
          unknownGroupIds.add(owned.group);
        }
      }
      const { count: maxEquipableOwnedCount, coveredTypes } = maxNonConflictingSlots(ownedMatchingItems);
      const unknownGroups = [...unknownGroupIds]
        .map((id) => groupById.get(id))
        .filter((g): g is GrowthGroup => Boolean(g));

      const upgradeableEquipped = matchingEquipped.filter((p) => p.level < 11);

      const recommendedUnowned: UnownedRecommendation[] = [];
      if (maxEquipableOwnedCount < rule.count) {
        const seenGroups = new Set<string>();
        const candidates = Object.values(catalog.items)
          .filter((item) => !ownedScoreIds.has(String(item.id)) && fits(item, rule) && !/^\d+$/.test(item.name))
          .map((item) => {
            const group = catalog.game_items[String(item.id)]?.group ?? String(item.id);
            const source = ITEM_SOURCES[String(item.id)];
            const slotNeeded = !coveredTypes.has(item.type);
            return { item, group, source, slotNeeded };
          })
          .sort((a, b) => {
            if (a.slotNeeded !== b.slotNeeded) return a.slotNeeded ? -1 : 1;
            if (Boolean(a.source) !== Boolean(b.source)) return a.source ? -1 : 1;
            return b.item.quality - a.item.quality;
          });

        for (const c of candidates) {
          if (seenGroups.has(c.group)) continue;
          seenGroups.add(c.group);
          recommendedUnowned.push({
            id: String(c.item.id),
            name: c.item.name,
            partName: shortPartName(catalog, c.item.type),
            quality: c.item.quality,
            slotNeeded: c.slotNeeded,
            source: c.source,
          });
          if (recommendedUnowned.length >= 6) break;
        }
      }

      return {
        kind: 'pieces',
        satisfied,
        filterLabel: formatRuleFilter(catalog, rule),
        equippedCount,
        requiredCount: rule.count,
        maxEquipableOwnedCount,
        unknownGroups,
        upgradeableEquipped,
        recommendedUnowned,
      };
    }

    // rule.kind === 'suit'
    const matchingSuits = catalog.suits.filter((suit) => {
      if (rule.suit && rule.suit !== suit.id) return false;
      const validIds = suit.items.filter((id) => catalog.items[id] && !isFlash(catalog.items[id]));
      if (!validIds.length) return false;
      const qualifying = validIds.filter((id) => fits(catalog.items[id], rule));
      return qualifying.length >= suitRequirement(catalog, validIds);
    });

    let anySatisfied = false;
    const candidates: SuitDiagnosticCandidate[] = matchingSuits.map((suit) => {
      const validIds = suit.items.filter((id) => catalog.items[id] && !isFlash(catalog.items[id]));
      const reqCount = suitRequirement(catalog, validIds);
      let equippedQualifying = 0;
      let ownedCount = 0;
      let maxQuality = 3;
      const unknownGroupIds = new Set<string>();
      const unrefreshedGroupIds = new Set<string>();
      const missingParts: { id: string; name: string; partName: string; source?: string }[] = [];

      for (const id of validIds) {
        const record = catalog.items[id];
        maxQuality = Math.max(maxQuality, record.quality);
        const eqLevel = equippedLevels.get(String(id));
        if (eqLevel !== undefined && fits(record, rule) && (!rule.refreshed || eqLevel === 11)) {
          equippedQualifying++;
        }
        const gameGroup = catalog.game_items[String(id)]?.group;
        if (ownedScoreIds.has(String(id))) {
          ownedCount++;
          const group = gameGroup ? groupById.get(gameGroup) : undefined;
          if (group) {
            if (group.level === null) unknownGroupIds.add(group.id);
            else if (rule.refreshed && group.level < 11) unrefreshedGroupIds.add(group.id);
          }
        } else {
          missingParts.push({
            id: String(id),
            name: record.name,
            partName: shortPartName(catalog, record.type),
            source: ITEM_SOURCES[String(id)],
          });
        }
      }

      if (equippedQualifying >= reqCount) anySatisfied = true;

      return {
        suitId: suit.id,
        suitName: suit.name,
        quality: maxQuality,
        ownedCount,
        totalCount: validIds.length,
        complete: ownedCount >= reqCount,
        unknownGroups: [...unknownGroupIds].map((gid) => groupById.get(gid)!).filter(Boolean),
        unrefreshedGroups: [...unrefreshedGroupIds].map((gid) => groupById.get(gid)!).filter(Boolean),
        missingParts,
      };
    });

    candidates.sort((a, b) => {
      if (a.complete !== b.complete) return a.complete ? -1 : 1;
      const ratioA = a.totalCount ? a.ownedCount / a.totalCount : 0;
      const ratioB = b.totalCount ? b.ownedCount / b.totalCount : 0;
      if (ratioA !== ratioB) return ratioB - ratioA;
      return b.quality - a.quality;
    });

    const suitLabel = rule.suit
      ? `「${catalog.suits.find((s) => s.id === rule.suit)?.name ?? rule.suit}」`
      : formatRuleFilter(catalog, rule);

    return {
      kind: 'suit',
      satisfied: anySatisfied,
      filterLabel: `${suitLabel}${rule.refreshed ? '焕新' : ''}套装`,
      refreshed: rule.refreshed,
      candidates: candidates.slice(0, 4),
    };
  };

  const diagnostics: UnmetStarDiagnostic[] = [];
  match.stars.forEach((star, index) => {
    const status = outfit.stars[index];
    if (status === true) return;
    const primaryGroup = star.groups[0] ?? [];
    const rules = primaryGroup.map(evaluateRuleDiagnostic);
    diagnostics.push({
      starIndex: index + 1,
      description: star.description || primaryGroup.map((r) => {
        if (r.kind === 'rank') return `获得${RANK_LABELS[r.rank] ?? r.rank}评价`;
        if (r.kind === 'pieces') return `穿戴${r.count}件${formatRuleFilter(catalog, r)}部件`;
        return `穿戴完整${formatRuleFilter(catalog, r)}${r.refreshed ? '焕新' : ''}套装`;
      }).join('，'),
      status,
      rules,
    });
  });

  return diagnostics;
}
