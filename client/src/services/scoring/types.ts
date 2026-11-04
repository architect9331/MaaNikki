export const ATTRIBUTES = ['典雅', '清新', '甜美', '性感', '帅气'];
export const HEART_GROUPS = ['发型', '外套', '衣服', '袜子', '鞋子', '饰品'];

export interface ScoreItem {
  id: number;
  type: number;
  curve: number;
  props: number[];
  tags: number[];
  visibleTags: number[];
  major: number;
  minor: number;
  excludes: number[];
  excludesTypes: number[];
  origin: number;
  name: string;
  quality: number;
}
export interface GameItem {
  score_id: string;
  web_id: string;
  group: string;
  name: string;
  part: string;
  quality: number;
  glow_reward: boolean;
}
export interface FieldBonus {
  description: string;
  value?: number;
  types: number[];
  tags: number[];
  major: number[];
  flat: number[];
  stat: number[];
  weight: number[];
  multiplier: number;
  add: number;
  global: boolean;
  countTag?: number;
  suits?: string[];
  refreshed?: boolean;
}
export type StarCondition =
  | { kind: 'rank'; rank: number }
  | { kind: 'pieces'; tags: number[]; props: number[]; count: number }
  | { kind: 'suit'; suit?: string; tags: number[]; props: number[]; refreshed: boolean };
export interface Match {
  name: string;
  group: string;
  area: number;
  weights: number[];
  tags: Record<string, number[]>;
  eurekaTags: Record<string, number[]>;
  tagBase: number;
  dressMultiplier: number;
  bonuses: FieldBonus[];
  choices: FieldBonus[];
  stars: { description: string; groups: StarCondition[][] }[];
}
export interface ScoringCatalog {
  schema_version: number;
  version: string;
  source: { name: string; url: string };
  items: Record<string, ScoreItem>;
  game_items: Record<string, GameItem>;
  matches: Record<string, Match>;
  suits: { id: string; items: number[]; name: string }[];
  curves: { ratios: number[]; costs: Record<string, number>[] }[];
  material_names: Record<string, string>;
  part_names: Record<string, string>;
}
export interface HeartSettings {
  flat: number[];
  stat: number[];
  part: number[];
  display: number;
}
export interface ScoringSettings {
  schema_version: 1;
  heart: HeartSettings;
  levels: Record<string, number>;
  rank_thresholds: Record<string, Record<string, number>>;
  updated_at?: string;
}
export interface WardrobeRecord {
  item_ids: number[];
  names: string[];
  source: string;
}
export interface CraftingMaterialMeta {
  id: string;
  name: string;
  kind: 'monster_craft' | 'monster_glow' | 'jihua' | 'world';
  realm?: 'monster' | 'jihua';
  option_case?: string;
  level?: string;
  energy?: number;
  drop?: number;
}
export interface CraftingSketch {
  id: string;
  name: string;
  category: 'suit' | 'piece';
  suit_id?: string | null;
  suit_name?: string | null;
  quality: number;
  part: string;
  costs: Record<string, number>;
}
export interface CraftingCatalog {
  schema_version: number;
  sketches: CraftingSketch[];
  materials: Record<string, CraftingMaterialMeta>;
}
export interface GlowPlanState {
  enabled: boolean;
  completed?: boolean;
  source_label?: string;
  targets: Record<string, number>;
  updated_at?: string;
}
export interface MaterialsState {
  inventory?: Record<string, number>;
  inventory_updated_at?: string;
  crafting_scope?: 'all' | 'suits';
  glow_plan?: GlowPlanState | null;
  updated_at?: string;
}
export interface ScoringPayload {
  account_key: string;
  uid: string;
  snapshot: {
    complete: boolean;
    updated_at: string;
    scanned_count: number;
    parts: Record<string, { name: string; items: WardrobeRecord[] }>;
  } | null;
  blessing: {
    locks?: { complete?: boolean; groups?: { name: string; slot: string; level: number; locked_count: number }[] };
    items?: { name: string; slot: string; level: number; count: number; identity_known?: boolean }[];
  } | null;
  settings: ScoringSettings | null;
  catalog: ScoringCatalog;
  materials?: MaterialsState | null;
  crafting_catalog?: CraftingCatalog | null;
}
export interface OwnedItem {
  id: string;
  gameId?: string;
  group?: string;
  name: string;
  level: number | null;
  evidence?: string;
  refreshReward?: string;
}
export interface GrowthGroup {
  id: string;
  name: string;
  part: string;
  owned: number;
  inferred: string | null;
  level: number | null;
}
export interface OwnedProfile {
  items: OwnedItem[];
  groups: GrowthGroup[];
  aligned: number;
  skipped: { name: string; part: string; reason: string }[];
  cosmetics: number;
}
export interface PieceScore {
  item: OwnedItem;
  type: number;
  from: number | null;
  level: number;
  score: number;
  attributeScores: number[];
  grownAttributes: number[];
  tagScore: number;
  multiplier: number;
  cost: Record<string, number>;
  /** Score above the current level; null while that level is unknown. */
  gain?: number | null;
}
export interface OutfitResult {
  score: number;
  pieces: PieceScore[];
  globalScore: number;
  stars: (boolean | null)[];
  cost: Record<string, number>;
  unknownCostGroups: string[];
  choice: number | null;
  exact: boolean;
  targetMet: boolean | null;
}
export interface CalculationRequest {
  catalog: ScoringCatalog;
  owned: OwnedItem[];
  settings: ScoringSettings;
  matchId: string;
  matchIds?: string[];
  excludedIds?: string[];
  choice: number | null;
  goal: 'score' | 'stars';
}
export interface BatchUpgradeItem {
  groupId: string;
  item: OwnedItem;
  from: number | null;
  to: number;
  totalGain: number;
  stages: string[];
  cost: Record<string, number>;
}
export interface BatchStageSummary {
  matchId: string;
  matchName: string;
  current: OutfitResult | null;
  potential: OutfitResult | null;
}
export interface BatchCalculationResult {
  stages: BatchStageSummary[];
  cost: Record<string, number>;
  upgrades: BatchUpgradeItem[];
  unknownCostGroups: string[];
  elapsedMs: number;
}
export interface CalculationResult {
  current: OutfitResult | null;
  potential: OutfitResult | null;
  batch?: BatchCalculationResult | null;
  unknownGroups: number;
  elapsedMs: number;
}

export function emptySettings(): ScoringSettings {
  return { schema_version: 1, heart: { flat: Array(5).fill(0), stat: Array(6).fill(0), part: Array(6).fill(0), display: 0 }, levels: {}, rank_thresholds: {} };
}
