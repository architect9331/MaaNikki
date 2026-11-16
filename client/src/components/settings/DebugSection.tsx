import { useState, useEffect, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Bug,
  RefreshCw,
  FolderOpen,
  ScrollText,
  Network,
  Archive,
  Eraser,
  HardDrive,
  Trash2,
} from 'lucide-react';
import { invoke } from '@tauri-apps/api/core';
import { toast } from 'sonner';

import { useAppStore } from '@/stores/appStore';
import { maaService } from '@/services/maaService';
import { resetCacheIndexState } from '@/services/cacheService';
import { getCurrentLogFileName, loggers } from '@/utils/logger';
import type { StorageCleanResult } from '@/utils/logRetention';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { isTauri, getDebugDir, getConfigDir, openDirectory } from '@/utils/paths';
import { useExportLogs } from '@/utils/useExportLogs';
import { SwitchButton } from '@/components/FormControls';
import { ExportLogsModal } from './ExportLogsModal';

interface StorageCategoryUsage {
  bytes: number;
  files: number;
}

interface StorageUsage {
  logs: StorageCategoryUsage;
  artifacts: StorageCategoryUsage;
  cleanableCache: StorageCategoryUsage;
  configAndRuntime: StorageCategoryUsage;
  totalDataBytes: number;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

export function DebugSection() {
  const { t } = useTranslation();
  const {
    projectInterface,
    dataPath,
    devMode,
    setDevMode,
    saveDraw,
    setSaveDraw,
    tcpCompatMode,
    setTcpCompatMode,
    backendOS,
    backendArch,
    instances,
    logRetentionDays,
    setLogRetentionDays,
  } = useAppStore();
  const [confirmClearLogs, setConfirmClearLogs] = useState(false);
  const [clearingLogs, setClearingLogs] = useState(false);
  const [confirmClearCache, setConfirmClearCache] = useState(false);
  const [clearingCache, setClearingCache] = useState(false);
  const [storageUsage, setStorageUsage] = useState<StorageUsage | null>(null);
  const [loadingStorage, setLoadingStorage] = useState(false);
  const [pendingRetentionDays, setPendingRetentionDays] = useState<number | null>(null);
  const logsInUse = instances.some((instance) => instance.isRunning);

  const loadStorageUsage = useCallback(async () => {
    if (!isTauri()) return;
    setLoadingStorage(true);
    try {
      const usage = await invoke<StorageUsage>('get_storage_usage');
      setStorageUsage(usage);
    } catch (error) {
      loggers.ui.warn('读取存储占用失败:', error);
    } finally {
      setLoadingStorage(false);
    }
  }, []);

  const handleClearLogFiles = async () => {
    if (clearingLogs || logsInUse) return;
    setConfirmClearLogs(false);
    setClearingLogs(true);
    try {
      const result = await invoke<StorageCleanResult>('clear_log_files', {
        excludeFileName: getCurrentLogFileName(),
        confirmed: true,
      });
      toast.success(
        result.freedBytes > 0
          ? t('debug.clearLogFilesDoneFreed', { size: formatBytes(result.freedBytes) })
          : t('debug.clearLogFilesDone'),
      );
      void loadStorageUsage();
    } catch (error) {
      loggers.ui.error('清理日志文件失败:', error);
      toast.error(t('debug.clearLogFilesFailed'));
    } finally {
      setClearingLogs(false);
    }
  };

  const handleClearStorageCache = async () => {
    if (clearingCache) return;
    setConfirmClearCache(false);
    setClearingCache(true);
    try {
      const result = await invoke<StorageCleanResult>('clear_storage_cache', {
        confirmed: true,
      });
      resetCacheIndexState();
      toast.success(
        result.freedBytes > 0
          ? t('debug.clearCacheDoneFreed', { size: formatBytes(result.freedBytes) })
          : t('debug.clearCacheDone'),
      );
      void loadStorageUsage();
    } catch (error) {
      loggers.ui.error('清理临时缓存失败:', error);
      toast.error(t('debug.clearCacheFailed'));
    } finally {
      setClearingCache(false);
    }
  };

  const [mxuVersion, setMxuVersion] = useState<string | null>(null);
  const [maafwVersion, setMaafwVersion] = useState<string | null>(null);
  const [exeDir, setExeDir] = useState<string | null>(null);
  const [cwd, setCwd] = useState<string | null>(null);
  const [webview2Dir, setWebview2Dir] = useState<{ path: string; system: boolean } | null>(null);
  const [systemInfo, setSystemInfo] = useState<{
    os: string;
    osVersion: string;
    arch: string;
    tauriVersion: string;
  } | null>(null);

  const { exportModal, handleExportLogs, closeExportModal, openExportedFile } = useExportLogs();

  const version = projectInterface?.version || '0.1.0';

  // 版本信息（用于调试展示）
  useEffect(() => {
    const loadVersions = async () => {
      // mxu 版本
      if (isTauri()) {
        try {
          const { getVersion } = await import('@tauri-apps/api/app');
          setMxuVersion(await getVersion());
        } catch {
          setMxuVersion(__MXU_VERSION__ || null);
        }
      } else {
        setMxuVersion(__MXU_VERSION__ || null);
      }

      // maafw 版本（Tauri 直接调用，浏览器走 HTTP API）
      try {
        setMaafwVersion(await maaService.getVersion());
      } catch {
        setMaafwVersion(null);
      }

      // 路径信息和系统信息（仅在 Tauri 环境有意义）
      if (isTauri()) {
        try {
          const { invoke } = await import('@tauri-apps/api/core');
          const [exeDirResult, cwdResult, sysInfo, webview2DirResult] = await Promise.all([
            invoke<string>('get_exe_dir'),
            invoke<string>('get_cwd'),
            invoke<{ os: string; os_version: string; arch: string; tauri_version: string }>(
              'get_system_info',
            ),
            invoke<{ path: string; system: boolean }>('get_webview2_dir'),
          ]);
          setExeDir(exeDirResult);
          setCwd(cwdResult);
          setWebview2Dir(webview2DirResult);
          setSystemInfo({
            os: sysInfo.os,
            osVersion: sysInfo.os_version,
            arch: sysInfo.arch,
            tauriVersion: sysInfo.tauri_version,
          });
        } catch {
          setExeDir(null);
          setCwd(null);
          setSystemInfo(null);
        }
      } else {
        // 浏览器环境：从 store 读取后端真实 OS/架构
        if (backendOS) {
          setSystemInfo({
            os: backendOS,
            osVersion: '',
            arch: backendArch || '',
            tauriVersion: '',
          });
        }
      }
    };

    loadVersions();
    void loadStorageUsage();
  }, [loadStorageUsage]);

  useEffect(() => {
    if (exportModal.status === 'success') {
      void loadStorageUsage();
    }
  }, [exportModal.status, loadStorageUsage]);

  // 调试：打开配置目录
  const handleOpenConfigDir = async () => {
    if (!isTauri() || !dataPath) {
      loggers.ui.warn('仅 Tauri 环境支持打开目录, dataPath:', dataPath);
      return;
    }

    try {
      const configPath = await getConfigDir();
      loggers.ui.info('打开配置目录:', configPath);
      await openDirectory(configPath);
    } catch (err) {
      loggers.ui.error('打开配置目录失败:', err);
    }
  };

  // 调试：打开日志目录
  const handleOpenLogDir = async () => {
    if (!isTauri() || !dataPath) {
      loggers.ui.warn('仅 Tauri 环境支持打开目录, dataPath:', dataPath);
      return;
    }

    try {
      const logPath = await getDebugDir();
      loggers.ui.info('打开日志目录:', logPath);
      await openDirectory(logPath);
    } catch (err) {
      loggers.ui.error('打开日志目录失败:', err);
    }
  };

  return (
    <section id="section-debug" className="space-y-4 scroll-mt-4">
      <h2 className="text-sm font-semibold text-text-primary uppercase tracking-wider flex items-center gap-2">
        <Bug className="w-4 h-4" />
        {t('debug.title')}
      </h2>

      <div className="bg-bg-secondary rounded-xl p-4 border border-border space-y-4">
        {/* 版本信息 */}
        <div className="text-sm text-text-secondary space-y-1">
          <p className="font-medium text-text-primary">{t('debug.versions')}</p>
          <p>
            {t('debug.interfaceVersion', { name: projectInterface?.name || 'interface' })}:{' '}
            <span className="font-mono text-text-primary">{version || '-'}</span>
          </p>
          <p>
            {t('debug.maafwVersion')}:{' '}
            <span className="font-mono text-text-primary">
              {maafwVersion || t('maa.notInitialized')}
            </span>
          </p>
          <p>
            {t('debug.mxuVersion')}:{' '}
            <span className="font-mono text-text-primary">{mxuVersion || '-'}</span>
          </p>
        </div>

        {/* 环境信息 */}
        <div className="text-sm text-text-secondary space-y-1">
          <p>
            {t('debug.environment')}:{' '}
            <span className="font-mono text-text-primary">
              {isTauri() ? t('debug.envTauri') : t('debug.envBrowser')}
            </span>
          </p>
        </div>

        {/* 系统信息 */}
        {systemInfo && (
          <div className="text-sm text-text-secondary space-y-1">
            <p className="font-medium text-text-primary">{t('debug.systemInfo')}</p>
            <p>
              {t('debug.operatingSystem')}:{' '}
              <span className="font-mono text-text-primary">{systemInfo.osVersion}</span>
            </p>
            <p>
              {t('debug.architecture')}:{' '}
              <span className="font-mono text-text-primary">{systemInfo.arch}</span>
            </p>
            <p>
              {t('debug.tauriVersion')}:{' '}
              <span className="font-mono text-text-primary">{systemInfo.tauriVersion}</span>
            </p>
          </div>
        )}

        {/* 路径信息（仅 Tauri 环境显示） */}
        {isTauri() && (exeDir || cwd) && (
          <div className="text-sm text-text-secondary space-y-1">
            <p className="font-medium text-text-primary">{t('debug.pathInfo')}</p>
            {cwd && (
              <p className="break-all">
                {t('debug.cwd')}: <span className="font-mono text-text-primary text-xs">{cwd}</span>
              </p>
            )}
            {exeDir && (
              <p className="break-all">
                {t('debug.exeDir')}:{' '}
                <span className="font-mono text-text-primary text-xs">{exeDir}</span>
              </p>
            )}
            <p className="break-all">
              {t('debug.webview2Dir')}:{' '}
              <span className="font-mono text-text-primary text-xs">
                {webview2Dir
                  ? webview2Dir.system
                    ? `(${t('debug.webview2System')})`
                    : webview2Dir.path
                  : '-'}
              </span>
            </p>
          </div>
        )}

        {/* 操作按钮 */}
        <div className="flex flex-wrap gap-2">
          <button
            onClick={handleOpenConfigDir}
            className="flex items-center gap-2 px-3 py-2 text-sm bg-bg-tertiary hover:bg-bg-hover rounded-lg transition-colors"
          >
            <FolderOpen className="w-4 h-4" />
            {t('debug.openConfigDir')}
          </button>
          <button
            onClick={handleOpenLogDir}
            className="flex items-center gap-2 px-3 py-2 text-sm bg-bg-tertiary hover:bg-bg-hover rounded-lg transition-colors"
          >
            <ScrollText className="w-4 h-4" />
            {t('debug.openLogDir')}
          </button>
          <button
            onClick={handleExportLogs}
            disabled={exportModal.show && exportModal.status === 'exporting'}
            className="flex items-center gap-2 px-3 py-2 text-sm bg-bg-tertiary hover:bg-bg-hover rounded-lg transition-colors disabled:opacity-50"
            title={t('debug.exportLogsHint')}
          >
            <Archive className="w-4 h-4" />
            {t('debug.exportLogs')}
          </button>
          {isTauri() && (
            <>
              <button
                onClick={() => setConfirmClearLogs(true)}
                disabled={clearingLogs || logsInUse}
                title={logsInUse ? t('debug.clearLogFilesBusy') : t('debug.clearLogFiles')}
                className="flex items-center gap-2 px-3 py-2 text-sm bg-bg-tertiary hover:bg-bg-hover rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >
                <Eraser className="w-4 h-4" />
                {t('debug.clearLogFiles')}
              </button>
              <button
                onClick={() => setConfirmClearCache(true)}
                disabled={clearingCache}
                className="flex items-center gap-2 px-3 py-2 text-sm bg-bg-tertiary hover:bg-bg-hover rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >
                <Trash2 className="w-4 h-4" />
                {t('debug.clearCache')}
              </button>
            </>
          )}
        </div>

        {isTauri() && (
          <div className="pt-4 border-t border-border space-y-2.5">
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-2 text-sm font-medium text-text-primary">
                <HardDrive className="w-4 h-4 text-accent" />
                <span>{t('debug.storageUsageTitle')}</span>
                {storageUsage && (
                  <span className="text-xs font-normal text-text-muted">
                    ({t('debug.storageTotal', { size: formatBytes(storageUsage.totalDataBytes) })})
                  </span>
                )}
              </div>
              <button
                type="button"
                onClick={() => void loadStorageUsage()}
                disabled={loadingStorage}
                title={t('debug.storageRefresh')}
                className="p-1.5 rounded-lg bg-bg-tertiary hover:bg-bg-hover text-text-secondary transition-colors disabled:opacity-50"
              >
                <RefreshCw className={`w-3.5 h-3.5 ${loadingStorage ? 'animate-spin' : ''}`} />
              </button>
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-xs">
              {[
                {
                  key: 'logs',
                  label: t('debug.storageLogs'),
                  hint: storageUsage
                    ? t('debug.storageFilesCount', { count: storageUsage.logs.files })
                    : '-',
                  bytes: storageUsage?.logs.bytes,
                },
                {
                  key: 'artifacts',
                  label: t('debug.storageArtifacts'),
                  hint: storageUsage
                    ? t('debug.storageFilesCount', { count: storageUsage.artifacts.files })
                    : '-',
                  bytes: storageUsage?.artifacts.bytes,
                },
                {
                  key: 'cleanableCache',
                  label: t('debug.storageCleanableCache'),
                  hint: storageUsage
                    ? t('debug.storageFilesCount', { count: storageUsage.cleanableCache.files })
                    : '-',
                  bytes: storageUsage?.cleanableCache.bytes,
                },
                {
                  key: 'configAndRuntime',
                  label: t('debug.storageConfigRuntime'),
                  hint: t('debug.storageConfigRuntimeHint'),
                  bytes: storageUsage?.configAndRuntime.bytes,
                },
              ].map((item) => (
                <div
                  key={item.key}
                  className="rounded-lg bg-bg-tertiary/70 px-3 py-2 flex items-center justify-between gap-2"
                >
                  <div>
                    <p className="text-text-secondary">{item.label}</p>
                    <p className="text-text-muted mt-0.5">{item.hint}</p>
                  </div>
                  <span className="font-mono text-text-primary font-medium">
                    {item.bytes !== undefined ? formatBytes(item.bytes) : '-'}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}

        {isTauri() && (
          <div className="flex items-center justify-between gap-3 pt-4 border-t border-border">
            <div>
              <span className="font-medium text-text-primary">{t('debug.logRetention')}</span>
              <p className="text-xs text-text-muted mt-0.5">{t('debug.logRetentionHint')}</p>
            </div>
            <select
              value={logRetentionDays}
              onChange={(event) => {
                const days = Number(event.target.value);
                if (days === 0) setLogRetentionDays(0);
                else setPendingRetentionDays(days);
              }}
              className="px-3 py-2 text-sm bg-bg-tertiary rounded-lg border border-border"
            >
              <option value={0}>{t('debug.keepAllLogs')}</option>
              {[7, 30, 90].map((days) => (
                <option key={days} value={days}>{t('debug.keepLogDays', { days })}</option>
              ))}
            </select>
          </div>
        )}

        {/* 开发模式 */}
        <div className="flex items-center justify-between pt-4 border-t border-border">
          <div className="flex items-center gap-3">
            <RefreshCw className="w-5 h-5 text-accent" />
            <div>
              <span className="font-medium text-text-primary">{t('debug.devMode')}</span>
              <p className="text-xs text-text-muted mt-0.5">{t('debug.devModeHint')}</p>
            </div>
          </div>
          <SwitchButton value={devMode} onChange={(v) => setDevMode(v)} />
        </div>

        {/* 保存调试图像 */}
        <div className="flex items-center justify-between pt-4 border-t border-border">
          <div className="flex items-center gap-3">
            <Bug className="w-5 h-5 text-accent" />
            <div>
              <span className="font-medium text-text-primary">{t('debug.saveDraw')}</span>
              <p className="text-xs text-text-muted mt-0.5">{t('debug.saveDrawHint')}</p>
            </div>
          </div>
          <SwitchButton value={saveDraw} onChange={(v) => setSaveDraw(v)} />
        </div>

        {/* 通信兼容模式 */}
        <div className="flex items-center justify-between pt-4 border-t border-border">
          <div className="flex items-center gap-3">
            <Network className="w-5 h-5 text-accent" />
            <div>
              <span className="font-medium text-text-primary">{t('debug.tcpCompatMode')}</span>
              <p className="text-xs text-text-muted mt-0.5">{t('debug.tcpCompatModeHint')}</p>
            </div>
          </div>
          <SwitchButton value={tcpCompatMode} onChange={(v) => setTcpCompatMode(v)} />
        </div>
      </div>

      <ConfirmDialog
        open={pendingRetentionDays !== null}
        title={t('debug.logRetention')}
        message={t('debug.logRetentionConfirm', { days: pendingRetentionDays })}
        confirmText={t('common.confirm')}
        cancelText={t('common.cancel')}
        destructive
        onConfirm={() => {
          if (pendingRetentionDays !== null) setLogRetentionDays(pendingRetentionDays);
          setPendingRetentionDays(null);
        }}
        onCancel={() => setPendingRetentionDays(null)}
      />
      <ConfirmDialog
        open={confirmClearLogs}
        title={t('debug.clearLogFiles')}
        message={t('debug.clearLogFilesConfirm')}
        confirmText={t('common.confirm')}
        cancelText={t('common.cancel')}
        destructive
        confirmDisabled={clearingLogs || logsInUse}
        onConfirm={handleClearLogFiles}
        onCancel={() => setConfirmClearLogs(false)}
      />
      <ConfirmDialog
        open={confirmClearCache}
        title={t('debug.clearCache')}
        message={t('debug.clearCacheConfirm')}
        confirmText={t('common.confirm')}
        cancelText={t('common.cancel')}
        destructive
        confirmDisabled={clearingCache}
        onConfirm={handleClearStorageCache}
        onCancel={() => setConfirmClearCache(false)}
      />
      {/* 导出日志 Modal */}
      <ExportLogsModal
        show={exportModal.show}
        status={exportModal.status === 'idle' ? 'exporting' : exportModal.status}
        zipPath={exportModal.zipPath}
        error={exportModal.error}
        onClose={closeExportModal}
        onOpen={openExportedFile}
      />
    </section>
  );
}
