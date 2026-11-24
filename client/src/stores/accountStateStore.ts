import { create } from 'zustand';
import { invoke } from '@tauri-apps/api/core';
import { isTauri } from '@/utils/paths';
import { apiGet } from '@/utils/backendApi';
import type { OptionValue } from '@/types/interface';

export interface CollectionKey { name: string; slot: string; color: string }
export interface WardrobeSnapshot {
  complete: boolean;
  updated_at: string;
  scanned_count: number;
  completed_parts: number;
  total_parts?: number;
  ambiguous_count: number;
  unconfirmed_count?: number;
  reason?: string | null;
  parts: Record<string, { name: string; scanned_count: number; ambiguous_count: number; complete: boolean }>;
}
export interface BlessingSnapshot {
  catalog_version: string | number;
  record?: { updated_at?: string; source?: string };
  scan?: { identity_complete?: boolean; reason?: string; workflow?: string };
  items?: (CollectionKey & { level: number; rarity: number; count: number })[];
  locks?: { complete?: boolean; groups?: (CollectionKey & { level: number; locked_count: number })[] };
  pursuit?: {
    mode?: string;
    existing_colors_checked?: boolean;
    protection_valid?: boolean;
    pending_operation?: unknown;
    parts?: { name: string; slot: string; locked_colors: string[]; missing_colors: string[]; embryos?: number }[];
    resources?: {
      bling?: number | null; particles?: number | null; capacity?: [number, number] | null;
      duplicates?: number; embryos?: number; spare_embryos?: number; bling_per_run?: number | null;
      reserve_bling?: number | null;
    } | null;
  };
  part_embryos?: Record<string, number>;
  unlocked_max_colors?: CollectionKey[];
  protection?: { complete?: boolean; keepers?: CollectionKey[] };
}
export interface PearpalNotebookObservation {
  schema_version?: number;
  uid?: string;
  updated_at?: string;
  five_star_colors?: CollectionKey[];
  four_star_colors?: CollectionKey[];
  dispatch?: { reward_id?: number; text?: string; spend_time?: number; start_time?: number }[];
  summary?: {
    login_days?: number;
    total_play_time?: number;
    cloth_num?: number;
    achievement_num?: number;
    box_num?: number;
    designdrawing_num?: number;
    permanent_tower?: number;
    periodic_tower?: number;
    energy?: number;
    daily_task?: number;
  };
}
export interface WeeklyRealmObservation {
  period?: string;
  finished?: number;
  total?: number;
  completed_targets?: string[];
  observed_at?: string;
}
export interface MaterialHistoryPoint {
  renewal_energy?: number;
  crafting_energy?: number;
  renewal_missing?: number;
  crafting_missing?: number;
}
export interface AccountView {
  wardrobe?: {
    inventory?: WardrobeSnapshot;
    last_scan?: WardrobeSnapshot;
    pearpal_cloth_num?: number;
    renewal_scan?: {
      updated_at?: string;
      complete?: boolean;
      tall_mode?: boolean;
      scanned_cards?: number;
      matched_groups?: number;
      unmatched_count?: number;
    };
  };
  account_key?: string | null;
  uid?: string | null;
  updated_at?: string | null;
  plan_revision?: string | null;
  observations?: {
    eureka_rolls?: { total: number; colors: Record<string, number> };
    anonymous_statistics?: { reporter?: string };
    collection_history?: Record<string, { owned: number }>;
    material_history?: Record<string, MaterialHistoryPoint>;
    weekly_realm?: WeeklyRealmObservation;
    pearpal_notebook?: PearpalNotebookObservation;
    giant_meteor?: {
      game_day: string;
      outcome: 'found' | 'not_found' | 'unconfirmed';
      checked_at: string;
    };
  };
  tasks: Record<string, {
    period: string;
    items: Record<string, { outcome: string; confirmed_at: string }>;
    stars?: number; total?: number; observed_at?: string;
    awards?: { claimed_stars: number; confirmed_at: string };
  }>;
  blessing: BlessingSnapshot | null;
  catalog: {
    version: string | number;
    sets: { name: string; rarity: number; source: string; colors: string[]; collection_target?: boolean }[];
    probability?: { iridescent: number; samples: number; successes: number; scenarios: number[] };
  };
  rotation: {
    anchor: string; reset_hour: number; target_score: number;
    tasks: Record<string, { label: string; score?: number; adapted: boolean }>;
    cycles: { label: string; tasks: string[] }[];
  } | null;
}

const requiredItems: Record<string, string[]> = {
  weekly_free_gifts: ['虹迹赠礼', '芳馨赠礼', '星实置业之礼'],
  weekly_home_shop: ['能量结晶', '闪亮泡泡'],
  monthly_crystal_exchange: ['启示水晶', '共鸣水晶'],
  crown: ['challenge'],
};
export function gameDay(now = Date.now()): Date {
  return new Date(now + (8 - 4) * 3600000);
}
export function periodKey(task: string, now = Date.now()): string {
  const day = gameDay(now);
  if (task === 'weekly_free_gifts' || task === 'weekly_home_shop') {
    day.setUTCDate(day.getUTCDate() - (day.getUTCDay() + 6) % 7);
    return day.toISOString().slice(0, 10);
  }
  const month = day.toISOString().slice(0, 7);
  return task === 'crown' ? `${month}-${day.getUTCDate() < 16 ? '01' : '16'}` : month;
}
export function crownTargetStars(values: Record<string, OptionValue>): number {
  const option = values.CrownTargetStars;
  const raw = option?.type === 'input' ? option.values.stars : undefined;
  return raw && /^(?:[1-9]|1[0-9]|2[0-4])$/.test(raw) ? Number(raw) : 12;
}
export function crownProgress(data: AccountView | null): { stars: number; total: number } | null {
  const record = data?.tasks?.crown;
  if (record?.period !== periodKey('crown') || typeof record.stars !== 'number' ||
      !Number.isInteger(record.stars) || record.total !== 24 ||
      record.stars < 0 || record.stars > record.total ||
      !record.observed_at || !Number.isFinite(Date.parse(record.observed_at))) return null;
  return { stars: record.stars, total: record.total };
}
export function crownAwardsClaimed(data: AccountView | null): boolean {
  const progress = crownProgress(data);
  const awards = data?.tasks?.crown?.awards;
  return Boolean(progress && awards && Number.isInteger(awards.claimed_stars) &&
    awards.claimed_stars >= progress.stars && awards.claimed_stars <= progress.total &&
    Number.isFinite(Date.parse(awards.confirmed_at)));
}
export function periodCompleted(
  data: AccountView | null,
  task: string,
  targetStars = 12,
  optionValues?: Record<string, OptionValue>,
): boolean {
  if (task === 'crown') {
    const progress = crownProgress(data);
    return progress !== null && progress.stars >= targetStars && crownAwardsClaimed(data);
  }
  let required = requiredItems[task];
  if (task === 'weekly_home_shop' && optionValues?.HomeShopItems?.type === 'checkbox') {
    const cases = optionValues.HomeShopItems.caseNames;
    required = [
      ...(cases.includes('EnergyCrystal') ? ['能量结晶'] : []),
      ...(cases.includes('ShinyBubbles') ? ['闪亮泡泡'] : []),
    ];
    if (required.length === 0) return false;
  }
  const record = data?.tasks?.[task];
  const outcomes: Record<string, string[]> = {
    weekly_free_gifts: ['claimed', 'sold_out'],
    weekly_home_shop: ['purchased', 'sold_out'],
    monthly_crystal_exchange: ['exchanged', 'already_exchanged', 'sold_out'],
  };
  return Boolean(required && record?.period === periodKey(task) && required.every((key) =>
    record.items?.[key] && outcomes[task].includes(record.items[key].outcome) &&
    Number.isFinite(Date.parse(record.items[key].confirmed_at))));
}
export function periodTaskLabel(task: string, completed: boolean, selected: boolean): string | null {
  if (!requiredItems[task]) return null;
  const period = task === 'crown'
    ? '本期'
    : (task === 'weekly_free_gifts' || task === 'weekly_home_shop')
      ? '本周'
      : '本月';
  return completed ? `${period}已完成` : selected ? `${period}将运行` : `${period}未勾选`;
}

let pending: Promise<AccountView> | null = null;
let lastSignature: string | null = null;
let cachedData: AccountView | null = null;

interface AccountStateDiffResponse {
  changed: boolean;
  signature: string;
  data?: AccountView;
}

export const useAccountStateStore = create<{
  data: AccountView | null; error: string | null; loading: boolean;
  refresh: (options?: { silent?: boolean }) => Promise<AccountView>;
}>((set, get) => ({
  data: null, error: null, loading: false,
  refresh: (options) => {
    const silent = options?.silent === true;
    if (!silent && !get().loading) set({ loading: true });
    if (pending) return pending;
    const request = isTauri()
      ? invoke<AccountStateDiffResponse>('get_account_state_if_changed', {
        knownSignature: lastSignature,
      }).then((res) => {
        if (!res.changed && cachedData) {
          if (get().error !== null) set({ error: null });
          return cachedData;
        }
        const data = res.data;
        if (!data) {
          throw new Error('账号状态返回为空。');
        }
        lastSignature = res.signature;
        cachedData = data;
        set({ data, error: null });
        return data;
      })
      : apiGet<AccountView>('/account-state').then((data) => {
        cachedData = data;
        set({ data, error: null });
        return data;
      });
    pending = request
      .catch((error: unknown) => {
        lastSignature = null;
        cachedData = null;
        set({ error: String(error), data: null });
        throw error;
      })
      .finally(() => {
        pending = null;
        if (get().loading) set({ loading: false });
      });
    return pending;
  },
}));
