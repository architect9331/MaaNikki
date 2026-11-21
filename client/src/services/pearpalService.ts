import { create } from 'zustand';
import { invoke } from '@tauri-apps/api/core';
import { listen, type UnlistenFn } from '@tauri-apps/api/event';
import { toast } from 'sonner';
import { isTauri } from '@/utils/paths';
import { refreshResonance } from './resonanceService';
import { useAccountStateStore } from '@/stores/accountStateStore';

export interface PearpalStatus {
  has_login: boolean;
  authenticated?: boolean;
  uid?: string;
  star_collected?: number;
  dewdrop_collected?: number;
  box_collected?: number;
  remaining?: number;
  updated_at?: number;
  error?: string;
}

export type PearpalLoginState = 'logged_in' | 'expired' | 'logged_out';

export function getPearpalLoginState(status: PearpalStatus): PearpalLoginState {
  if (!status.has_login) return 'logged_out';
  if (status.authenticated === false || Boolean(status.error)) return 'expired';
  return 'logged_in';
}

export function pearpalStatusLabel(status: PearpalStatus): string {
  const state = getPearpalLoginState(status);
  if (state === 'logged_in') return '已登录';
  if (state === 'expired') return '登录已过期';
  return '未登录';
}

export const usePearpalStore = create<{
  status: PearpalStatus;
  ready: boolean;
  busy: boolean;
  refreshing: boolean;
  loginError: string;
  modalOpen: boolean;
  openModal: () => void;
  closeModal: () => void;
  clearError: () => void;
}>((set) => ({
  status: { has_login: false },
  ready: false,
  busy: false,
  refreshing: false,
  loginError: '',
  modalOpen: false,
  openModal: () => set({ modalOpen: true }),
  closeModal: () => set({ modalOpen: false }),
  clearError: () => set({ loginError: '' }),
}));

let pendingRefresh: Promise<PearpalStatus | null> | null = null;

export async function refreshPearpalStatus(
  options?: { silent?: boolean },
): Promise<PearpalStatus | null> {
  if (!isTauri()) return null;
  if (pendingRefresh) return pendingRefresh;
  const silent = options?.silent === true;
  if (!silent && !usePearpalStore.getState().refreshing) {
    usePearpalStore.setState({ refreshing: true });
  }
  pendingRefresh = invoke<PearpalStatus>('pearpal_status')
    .then((raw) => {
      const next: PearpalStatus = {
        ...raw,
        has_login: Boolean(raw?.has_login),
      };
      const state = getPearpalLoginState(next);
      usePearpalStore.setState((prev) => ({
        status: next,
        ready: true,
        loginError: state === 'logged_in' ? '' : prev.loginError,
      }));
      return next;
    })
    .catch((error: unknown) => {
      usePearpalStore.setState((prev) => ({
        ready: true,
        loginError: silent ? prev.loginError : String(error),
      }));
      return null;
    })
    .finally(() => {
      pendingRefresh = null;
      if (usePearpalStore.getState().refreshing) {
        usePearpalStore.setState({ refreshing: false });
      }
    });
  return pendingRefresh;
}

export async function openPearpalLogin(options?: { switchAccount?: boolean }): Promise<void> {
  if (!isTauri() || usePearpalStore.getState().busy) return;
  usePearpalStore.setState({ busy: true, loginError: '' });
  try {
    await invoke('pearpal_open_login', {
      switchAccount: Boolean(options?.switchAccount),
    });
  } catch (error) {
    const message = String(error);
    usePearpalStore.setState({ loginError: message });
    toast.error(message);
  } finally {
    await refreshPearpalStatus({ silent: true });
    usePearpalStore.setState({ busy: false });
  }
}

export async function logoutPearpal(): Promise<void> {
  if (!isTauri() || usePearpalStore.getState().busy) return;
  usePearpalStore.setState({ busy: true, loginError: '' });
  try {
    await invoke('pearpal_logout');
    await Promise.all([
      refreshPearpalStatus({ silent: true }),
      refreshResonance(),
      useAccountStateStore.getState().refresh({ silent: true }).catch(() => {}),
    ]);
    toast.success('已退出美鸭梨账号', { id: 'pearpal-logout' });
  } catch (error) {
    const message = String(error);
    usePearpalStore.setState({ loginError: message });
    toast.error(message);
  } finally {
    usePearpalStore.setState({ busy: false });
  }
}

export function monitorPearpal(): () => void {
  if (!isTauri()) return () => {};
  let disposed = false;
  const unlisten: UnlistenFn[] = [];

  const syncAfterChange = (loggedIn: boolean) => {
    if (disposed) return;
    if (loggedIn) {
      usePearpalStore.setState({ loginError: '' });
      toast.success('美鸭梨账号登录成功', { id: 'pearpal-login-changed' });
    }
    void refreshPearpalStatus({ silent: true });
    void refreshResonance();
    void useAccountStateStore.getState().refresh({ silent: true }).catch(() => {});
  };

  void (async () => {
    for (const subscription of [
      listen<boolean>('pearpal-login-changed', ({ payload }) => {
        syncAfterChange(Boolean(payload));
      }),
      listen<string>('pearpal-login-error', ({ payload }) => {
        if (!disposed) {
          usePearpalStore.setState({ loginError: payload });
          void refreshPearpalStatus({ silent: true });
        }
      }),
    ]) {
      try {
        const cleanup = await subscription;
        if (disposed) cleanup();
        else unlisten.push(cleanup);
      } catch {
        /* Status polling still works if event subscription is unavailable. */
      }
    }
    if (!disposed) {
      await refreshPearpalStatus({ silent: true });
    }
  })();

  const refresh = () => {
    if (!disposed) void refreshPearpalStatus({ silent: true });
  };
  const onVisibility = () => {
    if (document.visibilityState === 'visible') refresh();
  };
  const timer = window.setInterval(() => {
    if (document.visibilityState === 'visible') refresh();
  }, 15000);

  window.addEventListener('focus', refresh);
  document.addEventListener('visibilitychange', onVisibility);

  return () => {
    disposed = true;
    unlisten.forEach((cleanup) => cleanup());
    window.clearInterval(timer);
    window.removeEventListener('focus', refresh);
    document.removeEventListener('visibilitychange', onVisibility);
  };
}
