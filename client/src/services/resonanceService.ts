import { create } from 'zustand';
import { invoke } from '@tauri-apps/api/core';
import { listen, type UnlistenFn } from '@tauri-apps/api/event';
import { toast } from 'sonner';
import { isTauri } from '@/utils/paths';

export interface ResonanceAccount {
  key: string; uid: string; region: string; total: number; notebook_total: number; last_success: number | null;
}
export interface ResonanceStatus {
  enabled: boolean; active_account: string | null; accounts: ResonanceAccount[];
  busy: boolean; phase: string; error: string | null; auth_paused: boolean;
  next_sync: number | null; banner: string | null; page: number; completed: number;
  banner_count: number; last_added: number; last_notebook_added: number; catalog_updated_at: string; interval_minutes: number;
}
export type ResonancePull = [string, string];
export interface NotebookPull {
  card_pool_id: string; pool_cnt: number; result: string; rarity: number;
  times_from_last_five_stars: number; times_from_last_four_stars: number;
}
export interface ResonanceOutfit { name: string; rarity: number; items: string[] }
export interface ResonanceHistory {
  history: {
    schema_version: number; account_key: string; uid: string; region: string;
    last_success: number | null;
    banners: Record<string, { pulls: ResonancePull[]; updated_at: number; possible_gap: boolean }>;
    notebook: { pulls: Record<string, Record<string, NotebookPull>>; updated_at: number | null };
  };
  banners: { id: string; name: string; kind: number; outfits: string[] }[];
  items: Record<string, { name: string; rarity: number; canonical_id?: string; outfit_id?: string }>;
  outfits: Record<string, ResonanceOutfit>;
}
const initial: ResonanceStatus = {
  enabled: false, active_account: null, accounts: [], busy: false, phase: 'idle',
  error: null, auth_paused: false, next_sync: null, banner: null, page: 0,
  completed: 0, banner_count: 0, last_added: 0, last_notebook_added: 0, catalog_updated_at: '', interval_minutes: 30,
};
export const useResonanceStore = create<{
  status: ResonanceStatus; ready: boolean; changing: boolean; connectionError: string | null;
}>(() => ({ status: initial, ready: false, changing: false, connectionError: null }));

export async function refreshResonance() {
  if (!isTauri()) return;
  try {
    const status = await invoke<ResonanceStatus>('resonance_status');
    useResonanceStore.setState({ status, ready: true, connectionError: null });
  } catch (error) { useResonanceStore.setState({ connectionError: String(error) }); }
}

export async function setResonanceEnabled(enabled: boolean) {
  if (useResonanceStore.getState().changing || !isTauri()) return;
  useResonanceStore.setState({ changing: true });
  try {
    const status = await invoke<ResonanceStatus>('resonance_set_enabled', { enabled });
    useResonanceStore.setState({ status, ready: true, connectionError: null });
  } catch (error) { toast.error(String(error)); }
  finally { useResonanceStore.setState({ changing: false }); }
}

export function monitorResonance() {
  if (!isTauri()) return () => {};
  let disposed = false;
  const unlisten: UnlistenFn[] = [];
  void (async () => {
    for (const subscription of [
      listen<ResonanceStatus>('resonance-status', ({ payload }) => {
        if (!disposed) useResonanceStore.setState({ status: payload, ready: true, connectionError: null });
      }),
      listen<string>('resonance-notice', ({ payload }) => {
        if (!disposed) toast(payload, { id: 'resonance-notice' });
      }),
    ]) {
      try {
        const cleanup = await subscription;
        if (disposed) cleanup(); else unlisten.push(cleanup);
      } catch { /* Status refresh still works if event subscription is unavailable. */ }
    }
    if (!disposed) await refreshResonance();
  })();
  const refresh = () => { void refreshResonance(); };
  window.addEventListener('focus', refresh);
  return () => {
    disposed = true;
    unlisten.forEach((cleanup) => cleanup());
    window.removeEventListener('focus', refresh);
  };
}

export function resonanceLabel(status: ResonanceStatus): string {
  if (status.busy) return '正在同步';
  if (status.auth_paused) return '等待登录';
  if (status.error) return '同步异常';
  return status.enabled ? '自动同步已开启' : '自动同步已关闭';
}
