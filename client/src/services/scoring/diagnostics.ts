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
import {
  bonusCount,
  evaluateStars,
  fits,
  hasSuit,
  isAccessory,
  isFlash,
  outfitContext,
  resolveRankThreshold,
  scorePiece,
  suitRequirement,
  upgradeCost,
} from './math';
import { ITEM_SOURCES } from './wardrobeBrowserData';

const RANK_LABELS: Record<number, string> = { 1: '及格', 2: '一般', 4: '优秀', 5: '完美' };

export function buildScoreIdToGroupMap(catalog: ScoringCatalog): Map<string, string> {
  const map = new Map<string, string>();
  for (const entry of Object.values(catalog.game_items)) {
    if (entry.score_id && entry.group && !map.has(String(entry.score_id))) {
      map.set(String(entry.score_id), entry.group);
    }
  }
  return map;
}

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
  const scoreIdToGroup = buildScoreIdToGroupMap(catalog);
  const addBadge = (scoreId: number | string, label: string) => {
    const group = scoreIdToGroup.get(String(scoreId));
    if (!group) return;
    let set = result.get(group);
    if (!set) {
      set = new Set<string>();
      result.set(group, set);
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
  requiredCount: number;
  readyCount: number;
  complete: boolean;
  unknownGroups: GrowthGroup[];
  unrefreshedGroups: GrowthGroup[];
  missingParts: { id: string; name: string; partName: string; source?: string }[];
}

export interface MinimumUpgradeItem {
  groupId: string;
  itemId: string;
  name: string;
  partName: string;
  suitName?: string;
  fromLevel: number | null;
  toLevel: number;
  scoreAtFrom: number;
  scoreAtTo: number;
  gain: number;
  mandatorySuitRefresh: boolean;
  reason: string;
  cost: Record<string, number>;
}

export interface MinimumStarUpgradePlan {
  targetStars: number;
  currentStars: number;
  achievedScore: number;
  targetScore?: number;
  mandatorySuitName?: string;
  mandatorySuitRequiredCount?: number;
  upgradeItems: MinimumUpgradeItem[];
  zeroLevelWearItems: {
    groupId: string;
    itemId: string;
    name: string;
    partName: string;
    suitName?: string;
    reason: string;
  }[];
  totalCost: Record<string, number>;
}

export function buildMinimumStarUpgradePlan(
  catalog: ScoringCatalog,
  settings: ScoringSettings,
  matchId: string,
  match: Match,
  outfit: OutfitResult,
  potentialOutfit: OutfitResult | null,
): MinimumStarUpgradePlan | null {
  if (!potentialOutfit) return null;
  const currentStars = outfit.stars.filter((s) => s === true).length;
  const targetStars = potentialOutfit.stars.filter((s) => s === true).length;
  if (targetStars <= currentStars) return null;

  const userThresholds = settings.rank_thresholds[matchId] ?? {};
  const potentialLevels = new Map(potentialOutfit.pieces.map((p) => [String(p.item.id), p.level]));
  const pieceSuitName = new Map<string, string>();
  const mandatoryRefreshIds = new Set<string>();
  const pieceRuleLabels = new Map<string, string>();
  let mandatorySuitName: string | undefined;
  let mandatorySuitRequiredCount: number | undefined;
  let targetScore: number | undefined;

  for (const star of match.stars) {
    for (const group of star.groups) {
      for (const rule of group) {
        if (rule.kind === 'rank') {
          const threshold = resolveRankThreshold(match, userThresholds, rule.rank);
          if (threshold !== undefined && potentialOutfit.score >= threshold) {
            targetScore = Math.max(targetScore ?? 0, threshold);
          }
        } else if (rule.kind === 'pieces') {
          const label = formatRuleFilter(catalog, rule);
          for (const p of potentialOutfit.pieces) {
            const rec = catalog.items[p.item.id];
            if (rec && fits(rec, rule)) {
              pieceRuleLabels.set(String(p.item.id), label);
            }
          }
        } else if (rule.kind === 'suit') {
          for (const suit of catalog.suits) {
            if (rule.suit && rule.suit !== suit.id) continue;
            if (hasSuit(catalog, suit.items, potentialLevels, rule)) {
              const suitSet = new Set(suit.items.map(String));
              for (const p of potentialOutfit.pieces) {
                const pid = String(p.item.id);
                if (suitSet.has(pid)) {
                  pieceSuitName.set(pid, suit.name);
                  if (rule.refreshed) {
                    mandatoryRefreshIds.add(pid);
                  }
                }
              }
              if (rule.refreshed) {
                mandatorySuitName = suit.name;
                mandatorySuitRequiredCount = suitRequirement(catalog, suit.items);
              }
              break;
            }
          }
        }
      }
    }
  }

  const bonuses = [
    ...match.bonuses,
    ...(potentialOutfit.choice === null ? [] : [match.choices[potentialOutfit.choice]]),
  ];

  const evaluateAtLevels = (levelsMap: Map<string, number>) => {
    const equipped = potentialOutfit.pieces.map((p) => ({
      item: p.item,
      level: levelsMap.get(String(p.item.id)) ?? 0,
    }));
    const ctx = outfitContext(catalog, equipped);
    const pieceScores = new Map<string, number>();
    let total = bonuses.reduce((sum, b) => sum + (b.global ? b.add * bonusCount(b, ctx) : 0), 0);
    const evaluatedPieces: PieceScore[] = [];
    for (const p of potentialOutfit.pieces) {
      const lvl = levelsMap.get(String(p.item.id)) ?? 0;
      const ps = scorePiece(catalog, p.item, lvl, match, settings.heart, bonuses, ctx);
      pieceScores.set(String(p.item.id), ps.score);
      total += ps.score;
      evaluatedPieces.push(ps);
    }
    const stars = evaluateStars(catalog, match, evaluatedPieces, total, userThresholds);
    return {
      score: total,
      pieceScores,
      stars,
      passedCount: stars.filter((s) => s === true).length,
    };
  };

  const zeroBaselineLevels = new Map<string, number>();
  const workingLevels = new Map<string, number>();
  for (const p of potentialOutfit.pieces) {
    const pid = String(p.item.id);
    const rec = catalog.items[p.item.id];
    if (isFlash(rec)) {
      zeroBaselineLevels.set(pid, p.level);
      workingLevels.set(pid, p.level);
    } else {
      const baseLvl = p.from ?? 0;
      zeroBaselineLevels.set(pid, baseLvl);
      workingLevels.set(pid, mandatoryRefreshIds.has(pid) ? 11 : baseLvl);
    }
  }

  const zeroEval = evaluateAtLevels(zeroBaselineLevels);
  let currentEval = evaluateAtLevels(workingLevels);

  while (
    currentEval.passedCount < targetStars ||
    (targetScore !== undefined && currentEval.score < targetScore)
  ) {
    const candidates = potentialOutfit.pieces.filter((p) => {
      const rec = catalog.items[p.item.id];
      return !isFlash(rec) && (workingLevels.get(String(p.item.id)) ?? 0) < 11;
    });
    if (!candidates.length) break;

    let bestPiece: PieceScore | null = null;
    let bestGain = -1;
    for (const cand of candidates) {
      const pid = String(cand.item.id);
      const trial = new Map(workingLevels);
      trial.set(pid, 11);
      const gain = evaluateAtLevels(trial).score - currentEval.score;
      if (gain > bestGain) {
        bestGain = gain;
        bestPiece = cand;
      }
    }
    if (!bestPiece || bestGain <= 0) break;

    const bestPid = String(bestPiece.item.id);
    const startLvl = workingLevels.get(bestPid) ?? 0;
    const trial11 = new Map(workingLevels);
    trial11.set(bestPid, 11);
    const eval11 = evaluateAtLevels(trial11);

    if (
      eval11.passedCount >= targetStars &&
      (targetScore === undefined || eval11.score >= targetScore)
    ) {
      let chosenLvl = 11;
      for (let lvl = startLvl + 1; lvl <= 11; lvl++) {
        const trial = new Map(workingLevels);
        trial.set(bestPid, lvl);
        const ev = evaluateAtLevels(trial);
        if (
          ev.passedCount >= targetStars &&
          (targetScore === undefined || ev.score >= targetScore)
        ) {
          chosenLvl = lvl;
          currentEval = ev;
          break;
        }
      }
      workingLevels.set(bestPid, chosenLvl);
      break;
    } else {
      workingLevels.set(bestPid, 11);
      currentEval = eval11;
    }
  }

  const upgradeItems: MinimumUpgradeItem[] = [];
  const zeroLevelWearItems: MinimumStarUpgradePlan['zeroLevelWearItems'] = [];
  const totalCost: Record<string, number> = {};

  for (const p of potentialOutfit.pieces) {
    const rec = catalog.items[p.item.id];
    if (isFlash(rec) || !p.item.group) continue;
    const pid = String(p.item.id);
    const baseLvl = p.from ?? 0;
    const targetLvl = workingLevels.get(pid) ?? baseLvl;
    const suitName = pieceSuitName.get(pid);
    const mandatorySuitRefresh = mandatoryRefreshIds.has(pid);

    if (targetLvl > baseLvl) {
      const scoreAtFrom = zeroEval.pieceScores.get(pid) ?? 0;
      const scoreAtTo = currentEval.pieceScores.get(pid) ?? p.score;
      const itemCost = upgradeCost(catalog, rec, baseLvl, targetLvl);
      for (const [mId, cnt] of Object.entries(itemCost)) {
        totalCost[mId] = (totalCost[mId] ?? 0) + cnt;
      }
      const reason = mandatorySuitRefresh
        ? `「${suitName}」整套焕新必升`
        : suitName
          ? `「${suitName}」套装提分主力`
          : pieceRuleLabels.has(pid)
            ? `${pieceRuleLabels.get(pid)}部件提分主力`
            : '凑满完美线高分主力';
      upgradeItems.push({
        groupId: p.item.group,
        itemId: pid,
        name: p.item.name,
        partName: shortPartName(catalog, p.type),
        suitName,
        fromLevel: p.from,
        toLevel: targetLvl,
        scoreAtFrom,
        scoreAtTo,
        gain: Math.max(0, scoreAtTo - scoreAtFrom),
        mandatorySuitRefresh,
        reason,
        cost: itemCost,
      });
    } else if (p.from === null && targetLvl === 0 && (suitName || pieceRuleLabels.has(pid))) {
      zeroLevelWearItems.push({
        groupId: p.item.group,
        itemId: pid,
        name: p.item.name,
        partName: shortPartName(catalog, p.type),
        suitName,
        reason: suitName
          ? `「${suitName}」套装部件（0级穿戴即可）`
          : `满足${pieceRuleLabels.get(pid)}件数（0级穿戴即可）`,
      });
    }
  }

  upgradeItems.sort((a, b) => {
    if (a.mandatorySuitRefresh !== b.mandatorySuitRefresh) {
      return a.mandatorySuitRefresh ? -1 : 1;
    }
    return b.gain - a.gain;
  });

  return {
    targetStars,
    currentStars,
    achievedScore: currentEval.score,
    targetScore,
    mandatorySuitName,
    mandatorySuitRequiredCount,
    upgradeItems,
    zeroLevelWearItems,
    totalCost,
  };
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
  const scoreIdToGroup = buildScoreIdToGroupMap(catalog);

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
            const group = scoreIdToGroup.get(String(item.id)) ?? String(item.id);
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
      let readyCount = 0;
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
        const gameGroup = scoreIdToGroup.get(String(id));
        if (ownedScoreIds.has(String(id))) {
          ownedCount++;
          const group = gameGroup ? groupById.get(gameGroup) : undefined;
          if (group) {
            if (group.level === null) unknownGroupIds.add(group.id);
            else if (rule.refreshed && group.level < 11) unrefreshedGroupIds.add(group.id);
            else readyCount++;
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
        requiredCount: reqCount,
        readyCount,
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
