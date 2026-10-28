import { invoke } from '@tauri-apps/api/core';
import { useAppStore } from '@/stores/appStore';
import { useExplorationStore } from '@/services/explorationService';
import { getCurrentLogFileName, loggers } from './logger';

const WEEK_MS = 7 * 24 * 60 * 60 * 1000;
let cleaning = false;

/** Periodically trim files under the retention policy confirmed in settings. */
export async function cleanupExpiredLogs() {
  const state = useAppStore.getState();
  const now = Date.now();
  const elapsed = now - state.lastLogCleanupAt;
  if (cleaning || ![7, 30, 90].includes(state.logRetentionDays)
    || (elapsed >= 0 && elapsed < WEEK_MS)
    || state.instances.some((instance) => instance.isRunning)
    || useExplorationStore.getState().enabled) return;
  cleaning = true;
  try {
    const deleted = await invoke<number>('cleanup_expired_logs', {
      retentionDays: state.logRetentionDays, excludeFileName: getCurrentLogFileName(),
    });
    state.setLastLogCleanupAt(now);
    loggers.app.info('过期日志文件清理完成:', deleted);
  } catch (error) {
    loggers.app.warn('过期日志文件清理失败:', error);
  } finally {
    cleaning = false;
  }
}
