export interface DyeColorEntry {
  group_id: number;
  color_grid_id: number;
  feature_tag: number;
  r: number;
  g: number;
  b: number;
}

export interface ClothDyeSummary {
  cloth_id: number;
  colors: DyeColorEntry[];
}

export interface DecodedShareCode {
  code: string;
  region: string;
  wearing_clothes: number[];
  dye_summaries: ClothDyeSummary[];
  pattern_cloth_ids: number[];
  hidden_fx_cloth_ids: number[];
}

export interface OutfitEntry {
  id: string;
  code: string;
  region: string;
  title: string;
  note: string;
  tags: string[];
  author?: string;
  coverDataUrl?: string;
  createdAt: string;
  updatedAt: string;
  decoded: DecodedShareCode;
}

export interface OutfitCollectionFile {
  schema_version: number;
  items: OutfitEntry[];
}

export interface OutfitItemDictEntry {
  name: string;
  part: string;
  quality: number;
}

export interface OutfitItemDictionary {
  part_names: Record<string, string>;
  items: Record<string, OutfitItemDictEntry>;
}

export type MatchPreset = 'all' | 'no_makeup' | 'no_accessories' | 'core_only' | 'custom';

export interface MatchRule {
  preset: MatchPreset;
  ignoreMakeup: boolean;
  ignoreAccessories: boolean;
  ignoreShoesAndSocks: boolean;
  requireExactVariant: boolean;
}

export type PieceCategory = 'core' | 'accessory' | 'makeup';

export type PieceOwnershipStatus = 'owned_exact' | 'owned_group' | 'missing';

export interface ResolvedOutfitPiece {
  clothId: string;
  groupId: string | null;
  name: string;
  partCode: string;
  partName: string;
  category: PieceCategory;
  quality: number;
  suitName?: string;
  variantLabel?: string;
  source?: string;
  ownership: PieceOwnershipStatus;
  ignored: boolean;
  satisfied: boolean;
  dyedHexColors: string[];
  hasPattern: boolean;
  fxHidden: boolean;
}

export type OutfitMatchStatus = 'wearable' | 'same_model' | 'near' | 'missing';

export interface OutfitEvaluation {
  status: OutfitMatchStatus;
  requiredCount: number;
  matchedCount: number;
  exactCount: number;
  sameModelCount: number;
  missingCount: number;
  totalPieceCount: number;
  dyedPieceCount: number;
  pieces: ResolvedOutfitPiece[];
  missingPieces: ResolvedOutfitPiece[];
  sameModelPieces: ResolvedOutfitPiece[];
  paletteHex: string[];
  suitNames: string[];
  usesMakeup: boolean;
}

export const DEFAULT_MATCH_RULE: MatchRule = {
  preset: 'no_makeup',
  ignoreMakeup: true,
  ignoreAccessories: false,
  ignoreShoesAndSocks: false,
  requireExactVariant: false,
};

export function ruleFromPreset(preset: MatchPreset, current: MatchRule): MatchRule {
  if (preset === 'all') {
    return {
      ...current,
      preset,
      ignoreMakeup: false,
      ignoreAccessories: false,
      ignoreShoesAndSocks: false,
    };
  }
  if (preset === 'no_makeup') {
    return {
      ...current,
      preset,
      ignoreMakeup: true,
      ignoreAccessories: false,
      ignoreShoesAndSocks: false,
    };
  }
  if (preset === 'no_accessories') {
    return {
      ...current,
      preset,
      ignoreMakeup: false,
      ignoreAccessories: true,
      ignoreShoesAndSocks: false,
    };
  }
  if (preset === 'core_only') {
    return {
      ...current,
      preset,
      ignoreMakeup: true,
      ignoreAccessories: true,
      ignoreShoesAndSocks: false,
    };
  }
  return { ...current, preset: 'custom' };
}

export function inferPreset(rule: Omit<MatchRule, 'preset'>): MatchPreset {
  if (rule.ignoreShoesAndSocks) return 'custom';
  if (!rule.ignoreMakeup && !rule.ignoreAccessories) return 'all';
  if (rule.ignoreMakeup && !rule.ignoreAccessories) return 'no_makeup';
  if (!rule.ignoreMakeup && rule.ignoreAccessories) return 'no_accessories';
  if (rule.ignoreMakeup && rule.ignoreAccessories) return 'core_only';
  return 'custom';
}
