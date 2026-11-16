import { invoke } from '@tauri-apps/api/core';
import { useAppStore } from '@/stores/appStore';
import { getCurrentLogFileName, loggers } from './logger';

const DAY_MS = 24 * 60 * 60 * 1000;
let cleaning = false;

export interface StorageCleanResult {
  deletedFiles: number;
  freedBytes: number;
}

/** Periodically trim expired files and enforce storage budgets without blocking Exploration Assist. */
export async function cleanupExpiredLogs() {
  const state = useAppStore.getState();
  const now = Date.now();
  const elapsed = now - state.lastLogCleanupAt;
  if (
    cleaning
    || ![0, 7, 30, 90].includes(state.logRetentionDays)
    || (elapsed >= 0 && elapsed < DAY_MS)
    || state.instances.some((instance) => instance.isRunning)
  ) {
    return;
  }
  cleaning = true;
  try {
    const result = await invoke<StorageCleanResult>('cleanup_expired_logs', {
      retentionDays: state.logRetentionDays,
      excludeFileName: getCurrentLogFileName(),
    });
    state.setLastLogCleanupAt(now);
    loggers.app.info(
      `存储定期维护完成: 删除 ${result.deletedFiles} 个文件, 释放 ${result.freedBytes} 字节`,
    );
  } catch (error) {
    loggers.app.warn('存储定期维护失败:', error);
  } finally {
    cleaning = false;
  }
}
