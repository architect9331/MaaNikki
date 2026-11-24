import { buildOwnedProfile } from '../scoring/profile';
import { ITEM_SOURCES } from '../scoring/wardrobeBrowserData';
import type { ScoringPayload, ScoringSettings } from '../scoring/types';
import type {
  MatchRule,
  OutfitEntry,
  OutfitEvaluation,
  OutfitItemDictionary,
  OutfitMatchStatus,
  PieceCategory,
  PieceOwnershipStatus,
  ResolvedOutfitPiece,
} from './types';

const CORE_PARTS = new Set(['210', '290', '220', '230', '242', '250', '260']);
const SHOES_AND_SOCKS_PARTS = new Set(['250', '260']);
const MAKEUP_PARTS = new Set(['281', '282', '283', '284', '285', '286']);

const PART_SORT_ORDER: Record<string, number> = {
  '210': 1,
  '290': 2,
  '220': 3,
  '230': 4,
  '242': 5,
  '250': 6,
  '260': 7,
  '271': 11,
  '272': 12,
  '273': 13,
  '274': 14,
  '275': 15,
  '276': 16,
  '277': 17,
  '292': 18,
  '293': 19,
  '294': 20,
  '295': 21,
  '296': 22,
  '270': 23,
  '278': 24,
  '413': 25,
  '279': 26,
  '281': 31,
  '282': 32,
  '283': 33,
  '284': 34,
  '285': 35,
  '286': 36,
};

const FALLBACK_PART_NAMES: Record<string, string> = {
  '210': '发型',
  '220': '外套',
  '230': '上衣',
  '242': '下装',
  '250': '袜子',
  '260': '鞋子',
  '270': '臂饰',
  '271': '发饰',
  '272': '帽子',
  '273': '耳饰',
  '274': '颈饰',
  '275': '腕饰',
  '276': '项圈',
  '277': '手套',
  '278': '手持物',
  '279': '肤绘',
  '281': '底妆',
  '282': '眉妆',
  '283': '睫毛',
  '284': '美瞳',
  '285': '唇妆',
  '286': '肤色',
  '290': '连衣裙',
  '292': '面饰',
  '293': '胸饰',
  '294': '挂饰',
  '295': '背饰',
  '296': '戒指',
  '413': '能力手持物',
};

/**
 * Extract unique share codes from arbitrary pasted text (Bilibili, Xiaohongshu, NGA, etc.).
 * Official share codes start with a region prefix [1-9A-CK], followed by Base-62 chars and `#`.
 */
export function extractShareCodes(rawText: string): string[] {
  const found: string[] = [];
  const seen = new Set<string>();
  const add = (candidate: string) => {
    const core = candidate.trim().replace(/#+$/, '');
    if (core.length < 6 || core.length > 28) return;
    if (!/^[1-9A-CK][0-9A-Za-z]+$/.test(core)) return;
    const canonical = `${core}#`;
    if (!seen.has(canonical)) {
      seen.add(canonical);
      found.push(canonical);
    }
  };

  // First pass: explicit `#`-terminated share codes anywhere in free-form copy-pasted text.
  const hashRegex = /(?<![0-9A-Za-z])([1-9A-CK][0-9A-Za-z]{5,26})#/g;
  for (const match of rawText.matchAll(hashRegex)) {
    add(match[1]);
  }
  if (found.length > 0) return found;

  // Fallback: user pasted bare codes without trailing `#` (one per line or whitespace-separated).
  for (const token of rawText.split(/[\s,，;；|]+/)) {
    const clean = token.trim();
    if (/^[1-9A-CK][0-9A-Za-z]{7,20}$/.test(clean)) {
      add(clean);
    }
  }
  return found;
}

/** Convert UE5 linear RGB (0..1+) to sRGB `#RRGGBB`. */
export function linearToSrgbHex(r: number, g: number, b: number): string {
  const toByte = (v: number) => {
    const clamped = Math.max(0, Math.min(1, Number.isFinite(v) ? v : 0));
    const srgb = clamped <= 0.0031308
      ? clamped * 12.92
      : 1.055 * Math.pow(clamped, 1 / 2.4) - 0.055;
    return Math.round(Math.max(0, Math.min(1, srgb)) * 255)
      .toString(16)
      .padStart(2, '0');
  };
  return `#${toByte(r)}${toByte(g)}${toByte(b)}`;
}

/** Infer part code from a 10-digit Infinity Nikki item ID (e.g. 1023900438 -> '290', 1020850438 -> '285'). */
function inferPartFromClothId(clothId: string): string {
  if (/^\d{10}$/.test(clothId)) {
    const mid = clothId.slice(4, 6);
    if (mid === '42') return '242';
    const prefixed = `2${mid}`;
    if (FALLBACK_PART_NAMES[prefixed]) return prefixed;
  }
  return '';
}

function classifyCategory(partCode: string): PieceCategory {
  if (MAKEUP_PARTS.has(partCode)) return 'makeup';
  if (CORE_PARTS.has(partCode)) return 'core';
  return 'accessory';
}

export interface WardrobeOwnershipIndex {
  hasCloset: boolean;
  hasMakeupScan: boolean;
  ownedExactIds: Set<string>;
  ownedGroupIds: Set<string>;
  suitNameByScoreId: Map<string, string>;
  suitNameByGameId: Map<string, string>;
  variantLabelByGameId: Map<string, string>;
}

export function buildWardrobeOwnershipIndex(
  payload: ScoringPayload | null,
  settings: ScoringSettings,
): WardrobeOwnershipIndex {
  const ownedExactIds = new Set<string>();
  const ownedGroupIds = new Set<string>();
  const suitNameByScoreId = new Map<string, string>();
  const suitNameByGameId = new Map<string, string>();
  const variantLabelByGameId = new Map<string, string>();

  if (!payload) {
    return {
      hasCloset: false,
      hasMakeupScan: false,
      ownedExactIds,
      ownedGroupIds,
      suitNameByScoreId,
      suitNameByGameId,
      variantLabelByGameId,
    };
  }

  const profile = buildOwnedProfile(payload, settings);
  for (const gid of profile.ownedGameIds) ownedExactIds.add(gid);
  for (const grp of profile.groups) ownedGroupIds.add(grp.id);

  // Also include raw snapshot item_ids directly (crucial for makeup 281..286 and ability handhelds 413,
  // which buildOwnedProfile excludes from score optimization).
  let hasMakeupScan = false;
  const parts = payload.snapshot?.parts ?? {};
  for (const [partCode, state] of Object.entries(parts)) {
    const rows = state?.items ?? [];
    if (MAKEUP_PARTS.has(partCode) && rows.length > 0) {
      hasMakeupScan = true;
    }
    for (const row of rows) {
      if (row.source === 'selected_card_unconfirmed') continue;
      for (const id of row.item_ids ?? []) {
        const sid = String(id);
        ownedExactIds.add(sid);
        const gi = payload.catalog.game_items?.[sid];
        if (gi?.group) ownedGroupIds.add(gi.group);
      }
    }
  }

  // Index parent suit names and evolution / glow-up variant labels.
  for (const suit of payload.catalog.suits ?? []) {
    for (const scoreId of suit.items ?? []) {
      const sk = String(scoreId);
      if (!suitNameByScoreId.has(sk)) suitNameByScoreId.set(sk, suit.name);
    }
    for (const variant of suit.variants ?? []) {
      const label = variant.kind === 'glow'
        ? '焕新色'
        : variant.stage > 0
          ? `进化${variant.stage}`
          : '进化色';
      for (const gid of variant.game_items ?? []) {
        const gk = String(gid);
        suitNameByGameId.set(gk, suit.name);
        variantLabelByGameId.set(gk, label);
      }
    }
  }

  const hasCloset = ownedExactIds.size > 0 || ownedGroupIds.size > 0;
  return {
    hasCloset,
    hasMakeupScan,
    ownedExactIds,
    ownedGroupIds,
    suitNameByScoreId,
    suitNameByGameId,
    variantLabelByGameId,
  };
}

export function evaluateOutfit(
  outfit: OutfitEntry,
  ownership: WardrobeOwnershipIndex,
  dict: OutfitItemDictionary | null,
  payload: ScoringPayload | null,
  rule: MatchRule,
): OutfitEvaluation {
  const gameItems = payload?.catalog.game_items ?? {};
  const dyeMap = new Map<string, string[]>();
  const paletteSeen = new Set<string>();
  const paletteHex: string[] = [];

  for (const summary of outfit.decoded.dye_summaries ?? []) {
    const cid = String(summary.cloth_id);
    const localSeen = new Set<string>();
    const hexes: string[] = [];
    for (const c of summary.colors ?? []) {
      const hex = linearToSrgbHex(c.r, c.g, c.b);
      if (!localSeen.has(hex)) {
        localSeen.add(hex);
        hexes.push(hex);
      }
      if (!paletteSeen.has(hex) && paletteHex.length < 6) {
        paletteSeen.add(hex);
        paletteHex.push(hex);
      }
    }
    if (hexes.length > 0) dyeMap.set(cid, hexes);
  }

  const patternSet = new Set((outfit.decoded.pattern_cloth_ids ?? []).map(String));
  const hiddenFxSet = new Set((outfit.decoded.hidden_fx_cloth_ids ?? []).map(String));

  const pieces: ResolvedOutfitPiece[] = [];
  const suitCount = new Map<string, number>();

  for (const rawId of outfit.decoded.wearing_clothes ?? []) {
    const clothId = String(rawId);
    const gi = gameItems[clothId];
    const dItem = dict?.items?.[clothId];

    const partCode = gi?.part || dItem?.part || inferPartFromClothId(clothId);
    const rawPartName = dict?.part_names?.[partCode] ?? FALLBACK_PART_NAMES[partCode] ?? (partCode || '其他');
    const partName = rawPartName.includes('·') ? rawPartName.split('·').pop()! : rawPartName;
    const category = classifyCategory(partCode);
    const name = gi?.name || dItem?.name || `未知部件 #${clothId}`;
    const quality = gi?.quality ?? dItem?.quality ?? 3;
    const groupId = gi?.group ?? null;

    const suitName = ownership.suitNameByGameId.get(clothId)
      ?? (gi?.score_id ? ownership.suitNameByScoreId.get(gi.score_id) : undefined);
    if (suitName) {
      suitCount.set(suitName, (suitCount.get(suitName) ?? 0) + 1);
    }

    let variantLabel = ownership.variantLabelByGameId.get(clothId);
    if (!variantLabel && gi?.glow_reward) {
      variantLabel = '焕新色';
    } else if (!variantLabel && groupId && groupId !== clothId) {
      variantLabel = '进化/变体色';
    }

    const source = gi?.score_id ? ITEM_SOURCES[gi.score_id] : undefined;

    let pieceStatus: PieceOwnershipStatus = 'missing';
    if (ownership.ownedExactIds.has(clothId)) {
      pieceStatus = 'owned_exact';
    } else if (groupId && ownership.ownedGroupIds.has(groupId)) {
      pieceStatus = 'owned_group';
    }

    const ignored = (
      (category === 'makeup' && rule.ignoreMakeup)
      || (category === 'accessory' && rule.ignoreAccessories)
      || (SHOES_AND_SOCKS_PARTS.has(partCode) && rule.ignoreShoesAndSocks)
    );

    const satisfied = ignored
      || pieceStatus === 'owned_exact'
      || (!rule.requireExactVariant && pieceStatus === 'owned_group');

    pieces.push({
      clothId,
      groupId,
      name,
      partCode,
      partName,
      category,
      quality,
      suitName,
      variantLabel,
      source,
      ownership: pieceStatus,
      ignored,
      satisfied,
      dyedHexColors: dyeMap.get(clothId) ?? [],
      hasPattern: patternSet.has(clothId),
      fxHidden: hiddenFxSet.has(clothId),
    });
  }

  pieces.sort((a, b) => (PART_SORT_ORDER[a.partCode] ?? 99) - (PART_SORT_ORDER[b.partCode] ?? 99));

  const required = pieces.filter((p) => !p.ignored);
  const exactCount = required.filter((p) => p.ownership === 'owned_exact').length;
  const sameModelPieces = required.filter((p) => p.ownership === 'owned_group');
  const missingPieces = required.filter((p) => p.ownership === 'missing');
  const matchedCount = required.filter((p) => p.satisfied).length;
  const requiredCount = required.length;

  let status: OutfitMatchStatus;
  if (missingPieces.length === 0) {
    if (rule.requireExactVariant && sameModelPieces.length > 0) {
      status = 'same_model';
    } else {
      status = 'wearable';
    }
  } else if (missingPieces.length <= 2) {
    status = 'near';
  } else {
    status = 'missing';
  }

  const suitNames = [...suitCount.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, 3)
    .map(([s]) => s);

  return {
    status,
    requiredCount,
    matchedCount,
    exactCount,
    sameModelCount: sameModelPieces.length,
    missingCount: missingPieces.length,
    totalPieceCount: pieces.length,
    dyedPieceCount: pieces.filter((p) => p.dyedHexColors.length > 0).length,
    pieces,
    missingPieces,
    sameModelPieces,
    paletteHex,
    suitNames,
    usesMakeup: pieces.some((p) => p.category === 'makeup'),
  };
}
