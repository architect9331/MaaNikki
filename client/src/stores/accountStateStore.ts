import { create } from 'zustand';
import { invoke } from '@tauri-apps/api/core';
import { isTauri } from '@/utils/paths';
import { apiGet } from '@/utils/backendApi';

export interface CollectionKey { name: string; slot: string; color: string }
export interface BlessingSnapshot {
  catalog_version: string | number;
  record?: { updated_at?: string; source?: string };
  scan?: { identity_complete?: boolean; reason?: string; workflow?: string };
  items?: (CollectionKey & { level: number; rarity: number; count: number })[];
  locks?: { complete?: boolean; groups?: (CollectionKey & { level: number; locked_count: number })[] };
  pursuit?: {
    existing_colors_checked?: boolean;
    protection_valid?: boolean;
    pending_operation?: unknown;
    parts?: { name: string; slot: string; locked_colors: string[]; missing_colors: string[] }[];
  };
  protection?: { complete?: boolean; keepers?: CollectionKey[] };
}
export interface AccountView {
  account_key?: string | null;
  uid?: string | null;
  observations?: {
    giant_meteor?: {
      game_day: string;
      outcome: 'found' | 'not_found' | 'unconfirmed';
      checked_at: string;
    };
  };
  tasks: Record<string, { period: string; items: Record<string, { outcome: string; confirmed_at: string }> }>;
  blessing: BlessingSnapshot | null;
  catalog: { version: string | number; sets: { name: string; rarity: number; source: string; colors: string[]; collection_target?: boolean }[] };
  rotation: {
    anchor: string; reset_hour: number; target_score: number;
    tasks: Record<string, { label: string; score?: number; adapted: boolean }>;
    cycles: { label: string; tasks: string[] }[];
  } | null;
}

const requiredItems: Record<string, string[]> = {
  weekly_free_gifts: ['虹迹赠礼', '芳馨赠礼', '星实置业之礼'],
  monthly_crystal_exchange: ['启示水晶', '共鸣水晶'],
  crown: ['challenge'],
};
export function gameDay(now = Date.now()): Date {
  return new Date(now + (8 - 4) * 3600000);
}
export function periodKey(task: string, now = Date.now()): string {
  const day = gameDay(now);
  if (task === 'weekly_free_gifts') {
    day.setUTCDate(day.getUTCDate() - (day.getUTCDay() + 6) % 7);
    return day.toISOString().slice(0, 10);
  }
  const month = day.toISOString().slice(0, 7);
  return task === 'crown' ? `${month}-${day.getUTCDate() < 16 ? '01' : '16'}` : month;
}
export function periodCompleted(data: AccountView | null, task: string): boolean {
  const required = requiredItems[task];
  const record = data?.tasks?.[task];
  const outcomes: Record<string, string[]> = {
    weekly_free_gifts: ["claimed", "sold_out"],
    monthly_crystal_exchange: ["exchanged", "already_exchanged", "sold_out"],
    crown: ["completed"],
  };
  return Boolean(required && record?.period === periodKey(task) && required.every((key) =>
    record.items?.[key] && outcomes[task].includes(record.items[key].outcome) &&
    Number.isFinite(Date.parse(record.items[key].confirmed_at))));
}
export function periodTaskLabel(task: string, completed: boolean, selected: boolean): string | null {
  if (!requiredItems[task]) return null;
  const period = task === 'crown' ? '本期' : task === 'weekly_free_gifts' ? '本周' : '本月';
  return completed ? `${period}已完成` : selected ? `${period}将运行` : `${period}未勾选`;
}

let pending: Promise<AccountView> | null = null;
export const useAccountStateStore = create<{
  data: AccountView | null; error: string | null; loading: boolean;
  refresh: () => Promise<AccountView>;
}>((set) => ({
  data: null, error: null, loading: false,
  refresh: () => {
    if (pending) return pending;
    set({ loading: true });
    pending = (isTauri() ? invoke<AccountView>('get_account_state') : apiGet<AccountView>('/account-state'))
      .then((data) => { set({ data, error: null }); return data; })
      .catch((error: unknown) => { set({ error: String(error), data: null }); throw error; })
      .finally(() => { pending = null; set({ loading: false }); });
    return pending;
  },
}));
