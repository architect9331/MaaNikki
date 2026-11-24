import { invoke } from '@tauri-apps/api/core';
import { isTauri } from '@/utils/paths';
import { apiGet, apiPut } from '@/utils/backendApi';

export interface InfiniteHeartShardMeta {
  id: number;
  name: string;
  tab_index: number;
  token_name: string;
  token_id: number;
  node_count: number;
  total_tokens: number;
  total_bling: number;
}

export interface InfiniteHeartCostItem {
  item_id: number;
  name: string;
  amount: number;
}

export interface InfiniteHeartNodeMeta {
  id: number;
  version_id: number;
  graph_id: number;
  title: string;
  desc: string;
  category: string;
  category_label: string;
  is_root: boolean;
  predecessor_ids: number[];
  successor_ids: number[];
  tokens_each: number;
  max_level: number;
  costs_each: InfiniteHeartCostItem[];
  conditions: string[];
  observability: 'exact' | 'group_all' | 'max_only' | 'shared_root' | 'shared_ambiguous' | 'unmapped';
  screen_pos: [number, number] | null;
  heart_preset_id?: number;
  flat_attr_index?: number;
}

export interface InfiniteHeartCatalog {
  schema_version: number;
  versions: InfiniteHeartShardMeta[];
  nodes: InfiniteHeartNodeMeta[];
}

export type InfiniteHeartNodeStateKind = 'unlocked' | 'locked' | 'ambiguous';

export interface InfiniteHeartScanNode {
  state: InfiniteHeartNodeStateKind;
  level: number;
  score: number;
  px: number;
}

export interface InfiniteHeartScan {
  schema_version: number;
  updated_at: string;
  complete: boolean;
  scanned_shards: number[];
  currencies?: Record<string, number>;
  bling?: number | null;
  nodes: Record<string, InfiniteHeartScanNode>;
}

export interface InfiniteHeartOverride {
  state: 'unlocked' | 'locked';
  level?: number;
}

export interface InfiniteHeartPayload {
  catalog: InfiniteHeartCatalog;
  account_key: string | null;
  uid: string | null;
  last_scan: InfiniteHeartScan | null;
  overrides: Record<string, InfiniteHeartOverride>;
}

export async function loadInfiniteHeart(): Promise<InfiniteHeartPayload> {
  return isTauri()
    ? invoke<InfiniteHeartPayload>('get_infinite_heart')
    : apiGet<InfiniteHeartPayload>('/infinite-heart');
}

export async function saveInfiniteHeartOverrides(
  accountKey: string,
  overrides: Record<string, InfiniteHeartOverride>,
): Promise<void> {
  if (isTauri()) {
    await invoke('save_infinite_heart_overrides', { accountKey, overrides });
  } else {
    await apiPut('/infinite-heart', { account_key: accountKey, overrides });
  }
}

export function resolveNodeState(
  node: InfiniteHeartNodeMeta,
  scan: InfiniteHeartScan | null,
  overrides: Record<string, InfiniteHeartOverride>,
): { state: InfiniteHeartNodeStateKind; level: number; manual: boolean } {
  const key = String(node.id);
  const manual = overrides[key];
  if (manual) {
    const level =
      manual.state === 'unlocked'
        ? Math.max(1, Math.min(node.max_level, manual.level ?? node.max_level))
        : Math.max(0, Math.min(node.max_level - 1, manual.level ?? 0));
    return {
      state: level >= node.max_level ? 'unlocked' : 'locked',
      level,
      manual: true,
    };
  }
  const scanned = scan?.nodes?.[key];
  if (!scanned) {
    return { state: 'locked', level: 0, manual: false };
  }
  return {
    state: scanned.state,
    level: Math.max(0, Math.min(node.max_level, scanned.level ?? (scanned.state === 'unlocked' ? node.max_level : 0))),
    manual: false,
  };
}
