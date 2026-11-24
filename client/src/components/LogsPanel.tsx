import {
  Fragment,
  useRef,
  useEffect,
  useCallback,
  useState,
  useMemo,
  useLayoutEffect,
} from 'react';
import { useTranslation } from 'react-i18next';
import { Eraser, Copy, ChevronUp, ChevronDown, Archive, ArrowDown } from 'lucide-react';
import { toast } from 'sonner';
import clsx from 'clsx';
import { useAppStore, type LogType } from '@/stores/appStore';
import { useShallow } from 'zustand/react/shallow';
import { ContextMenu, useContextMenu, type MenuItem } from './ContextMenu';
import { isTauri } from '@/utils/paths';
import { useExportLogs } from '@/utils/useExportLogs';
import { ExportLogsModal } from './settings/ExportLogsModal';
import { useIsMobile } from '@/hooks/useIsMobile';
import { getAllLogsFromBackend } from '@/utils/logStdout';
import { loadPersistedRuntimeLogs, mergeRuntimeLogs } from '@/utils/runtimeLogPersistence';
import type { LogEntry } from '@/stores/types';

const DEFAULT_VISIBLE_LOG_LIMIT = 500;
const EXPANDED_LOG_LIMIT = 2000;
const BOTTOM_FOLLOW_THRESHOLD_PX = 24;
const EMPTY_LOGS: LogEntry[] = [];

function formatLogTime(date: Date, locale?: string) {
  return date.toLocaleTimeString(locale || undefined, {
    hour12: false,
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

export function LogsPanel({ instanceId }: { instanceId?: string } = {}) {
  const { t, i18n } = useTranslation();
  const isMobile = useIsMobile();
  const logsContainerRef = useRef<HTMLDivElement>(null);
  const logsEndRef = useRef<HTMLDivElement>(null);
  const isFollowingTailRef = useRef(true);
  const [visibleLogLimit, setVisibleLogLimit] = useState(DEFAULT_VISIBLE_LOG_LIMIT);
  const [isAtTop, setIsAtTop] = useState(false);
  // 用户向上翻看旧日志时停止自动跟随，并给出回到最新的入口
  const [isFollowingTail, setIsFollowingTail] = useState(true);
  const [isExpandingLogs, setIsExpandingLogs] = useState(false);

  const {
    sidePanelExpanded,
    toggleSidePanelExpanded,
    selectedInstanceId,
    clearLogs,
    setMaxLogsPerInstance,
    logViewMode,
    setLogViewMode,
  } = useAppStore(
    useShallow((state) => ({
      sidePanelExpanded: state.sidePanelExpanded,
      toggleSidePanelExpanded: state.toggleSidePanelExpanded,
      selectedInstanceId: state.activeInstanceId,
      clearLogs: state.clearLogs,
      setMaxLogsPerInstance: state.setMaxLogsPerInstance,
      logViewMode: state.logViewMode,
      setLogViewMode: state.setLogViewMode,
    })),
  );
  const activeInstanceId = instanceId ?? selectedInstanceId;
  // 只订阅当前实例的日志与实时状态，其他实例写日志时不触发本面板重渲染
  const logs = useAppStore((state) =>
    activeInstanceId ? state.instanceLogs[activeInstanceId] || EMPTY_LOGS : EMPTY_LOGS,
  );
  const liveStatus = useAppStore((state) =>
    activeInstanceId ? state.instanceLiveStatus[activeInstanceId] ?? null : null,
  );
  const { state: menuState, show: showMenu, hide: hideMenu } = useContextMenu();
  const { exportModal, handleExportLogs, closeExportModal, openExportedFile } = useExportLogs();

  // 获取当前实例的日志（精简模式下过滤中间过程 detail 行）
  const filteredLogs = useMemo(
    () => (logViewMode === 'concise' ? logs.filter((entry) => entry.kind !== 'detail') : logs),
    [logs, logViewMode],
  );
  const visibleLogs = useMemo(
    () => filteredLogs.slice(-visibleLogLimit),
    [filteredLogs, visibleLogLimit],
  );
  const canShowMoreLogs =
    visibleLogLimit === DEFAULT_VISIBLE_LOG_LIMIT &&
    visibleLogs.length === DEFAULT_VISIBLE_LOG_LIMIT;

  useEffect(() => {
    setVisibleLogLimit(DEFAULT_VISIBLE_LOG_LIMIT);
    setIsAtTop(false);
    isFollowingTailRef.current = true;
    setIsFollowingTail(true);
  }, [activeInstanceId]);

  useLayoutEffect(() => {
    if (!isFollowingTailRef.current) return;

    // scrollIntoView would also scroll the page holding this panel.
    const el = logsContainerRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [logs.length, visibleLogLimit]);

  const handleLogsScroll = useCallback(() => {
    const el = logsContainerRef.current;
    if (!el) return;

    const top = el.scrollTop <= 4;
    const bottom =
      el.scrollHeight - el.scrollTop - el.clientHeight <= BOTTOM_FOLLOW_THRESHOLD_PX * 2;

    setIsAtTop(top);
    isFollowingTailRef.current = bottom;
    setIsFollowingTail(bottom);
  }, []);

  const handleJumpToLatest = useCallback(() => {
    const el = logsContainerRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    isFollowingTailRef.current = true;
    setIsFollowingTail(true);
  }, []);

  const handleClear = useCallback(() => {
    if (activeInstanceId) {
      clearLogs(activeInstanceId);
      isFollowingTailRef.current = true;
      setIsFollowingTail(true);
    }
  }, [activeInstanceId, clearLogs]);

  const handleCopyAll = useCallback(() => {
    const text = visibleLogs
      .map((log) => `[${log.timestamp.toLocaleTimeString()}] ${log.message}`)
      .join('\n');
    navigator.clipboard.writeText(text).then(
      () => toast.success(t('logs.copied')),
      () => toast.error(t('logs.copyFailed')),
    );
  }, [visibleLogs, t]);

  const handleShowMoreLogs = useCallback(() => {
    if (isExpandingLogs) return;

    const targetLimit = EXPANDED_LOG_LIMIT;
    setIsExpandingLogs(true);

    void (async () => {
      try {
        const backendLogs = await getAllLogsFromBackend();
        const restoredBackendLogs: Record<string, LogEntry[]> = {};
        for (const [instanceId, entries] of Object.entries(backendLogs || {})) {
          restoredBackendLogs[instanceId] = entries.map((entry) => ({
            id: entry.id,
            timestamp: new Date(entry.timestamp),
            type: entry.type as LogType,
            message: entry.message,
            html: entry.html,
          }));
        }

        const store = useAppStore.getState();
        const restoredPersistentLogs = loadPersistedRuntimeLogs(targetLimit);
        const mergedLogs = mergeRuntimeLogs(
          targetLimit,
          store.instanceLogs,
          restoredBackendLogs,
          restoredPersistentLogs,
        );

        useAppStore.setState({ instanceLogs: mergedLogs });
        setMaxLogsPerInstance(targetLimit);
        setVisibleLogLimit(targetLimit);
      } catch {
        // Rehydration failed; keep the 500-line window and allow retry.
      } finally {
        setIsExpandingLogs(false);
      }
    })();
  }, [isExpandingLogs, setMaxLogsPerInstance]);

  const getLogColor = (type: LogType) => {
    switch (type) {
      case 'success':
        return 'text-success';
      case 'warning':
        return 'text-warning';
      case 'error':
        return 'text-error';
      case 'agent':
        return 'text-text-secondary';
      case 'focus':
        return 'text-[var(--color-log-info)]';
      case 'info':
        return 'text-[var(--color-log-info)]';
      default:
        return 'text-text-secondary';
    }
  };

  // 右键菜单处理
  const handleContextMenu = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();

      const menuItems: MenuItem[] = [
        {
          id: 'export-logs',
          label: t('debug.exportLogs'),
          icon: Archive,
          disabled: !isTauri(),
          onClick: handleExportLogs,
        },
        {
          id: 'copy',
          label: t('logs.copyAll'),
          icon: Copy,
          disabled: logs.length === 0,
          onClick: handleCopyAll,
        },
        {
          id: 'clear',
          label: t('logs.clear'),
          icon: Eraser,
          disabled: logs.length === 0,
          danger: true,
          onClick: handleClear,
        },
        { id: 'divider-1', label: '', divider: true },
        {
          id: 'toggle-panel',
          label: sidePanelExpanded ? t('logs.collapse') : t('logs.expand'),
          icon: sidePanelExpanded ? ChevronUp : ChevronDown,
          onClick: toggleSidePanelExpanded,
        },
      ];

      showMenu(e, instanceId ? menuItems.filter((item) => item.id !== 'toggle-panel' && item.id !== 'divider-1') : menuItems);
    },
    [
      t,
      instanceId,
      logs.length,
      sidePanelExpanded,
      handleExportLogs,
      handleCopyAll,
      handleClear,
      toggleSidePanelExpanded,
      showMenu,
    ],
  );

  return (
    <div
      id="logs-panel"
      className={clsx(
        'relative flex flex-col bg-bg-secondary rounded-lg ring-1 ring-inset ring-border overflow-hidden',
        // 3rem = TabBar h-12；若 TabBar 高度变更需同步更新
        instanceId ? 'h-full min-h-0' : isMobile ? 'h-[calc(100dvh-3rem)]' : 'flex-1 min-h-50',
      )}
    >
      {/* 标题栏（桌面端可点击展开/折叠上方面板，移动端仅显示标题） */}
      <div
        role={isMobile || instanceId ? undefined : 'button'}
        tabIndex={isMobile || instanceId ? undefined : 0}
        onClick={isMobile || instanceId ? undefined : toggleSidePanelExpanded}
        onKeyDown={
          isMobile || instanceId
            ? undefined
            : (e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault();
                  toggleSidePanelExpanded();
                }
              }
        }
        className={clsx(
          'flex items-center justify-between px-3 py-2 border-b border-border transition-colors shrink-0 rounded-t-lg',
          !isMobile && !instanceId &&
            'hover:bg-bg-hover cursor-pointer focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/50 outline-none',
        )}
      >
        <span className="text-sm font-medium text-text-primary">{t('logs.title')}</span>
        <div className="flex items-center gap-1.5">
          <div
            role="group"
            aria-label="日志显示模式"
            onClick={(e) => e.stopPropagation()}
            className="inline-flex items-center rounded-md border border-border bg-bg-primary p-0.5 text-[11px]"
          >
            <button
              type="button"
              onClick={() => setLogViewMode('concise')}
              title="精简模式：仅显示任务阶段、收益汇总与异常（导出日志始终包含完整后端记录）"
              className={clsx(
                'rounded px-1.5 py-0.5 transition-colors',
                logViewMode === 'concise'
                  ? 'bg-accent/15 font-medium text-accent'
                  : 'text-text-muted hover:text-text-primary',
              )}
            >
              精简
            </button>
            <button
              type="button"
              onClick={() => setLogViewMode('detailed')}
              title="详细模式：显示每步实时进度与连接/资源加载过程"
              className={clsx(
                'rounded px-1.5 py-0.5 transition-colors',
                logViewMode === 'detailed'
                  ? 'bg-accent/15 font-medium text-accent'
                  : 'text-text-muted hover:text-text-primary',
              )}
            >
              详细
            </button>
          </div>
          <button
            onClick={(e) => {
              e.stopPropagation();
              handleExportLogs();
            }}
            disabled={!isTauri() || (exportModal.show && exportModal.status === 'exporting')}
            className={clsx(
              'p-1 rounded-lg transition-colors',
              !isTauri()
                ? 'text-text-muted cursor-not-allowed'
                : 'text-text-secondary hover:bg-bg-tertiary hover:text-text-primary',
            )}
            title={t('debug.exportLogs')}
            aria-label={t('debug.exportLogs')}
          >
            <Archive className="w-3.5 h-3.5" />
          </button>
          <button
            onClick={(e) => {
              e.stopPropagation();
              handleClear();
            }}
            disabled={logs.length === 0}
            className={clsx(
              'p-1 rounded-lg transition-colors',
              logs.length === 0
                ? 'text-text-muted cursor-not-allowed'
                : 'text-text-secondary hover:bg-bg-tertiary hover:text-text-primary',
            )}
            title={t('logs.clear')}
            aria-label={t('logs.clear')}
          >
            <Eraser className="w-3.5 h-3.5" />
          </button>
          {!isMobile && !instanceId && (
            <span
              className={clsx(
                'p-0.5 rounded-lg transition-colors',
                !sidePanelExpanded ? 'text-accent bg-accent-light' : 'text-text-muted',
              )}
            >
              <ChevronDown
                className={clsx(
                  'w-4 h-4 transition-transform duration-150 ease-out',
                  sidePanelExpanded && 'rotate-180',
                )}
              />
            </span>
          )}
        </div>
      </div>

      {/* 顶部固定实时进度条（高频中间步骤在此原地刷新，不刷屏历史区） */}
      {liveStatus && (
        <div
          className="flex items-center gap-2 border-b border-border/70 bg-accent/[0.06] px-3 py-1.5 text-[11px] text-text-primary shrink-0"
          title={liveStatus}
        >
          <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-accent animate-pulse" aria-hidden="true" />
          <span className="min-w-0 flex-1 truncate">{liveStatus}</span>
        </div>
      )}

      {/* 日志内容 */}
      <div
        ref={logsContainerRef}
        className="flex-1 min-h-0 overflow-y-auto px-2.5 py-2 font-mono text-[12px] leading-5 bg-bg-tertiary dark:bg-bg-secondary"
        onScroll={handleLogsScroll}
        onContextMenu={handleContextMenu}
      >
        {visibleLogs.length === 0 ? (
          <div className="h-full flex items-center justify-center text-text-muted">
            {t('logs.noLogs')}
          </div>
        ) : (
          <>
            {canShowMoreLogs && isAtTop && (
              <div className="flex justify-center py-3">
                <button
                  type="button"
                  onClick={handleShowMoreLogs}
                  disabled={isExpandingLogs}
                  className={clsx(
                    'px-4 py-2 rounded-full border border-border',
                    'bg-bg-secondary text-text-primary shadow-sm',
                    'hover:bg-bg-hover hover:border-accent/50 transition-colors',
                    isExpandingLogs && 'opacity-60 cursor-not-allowed',
                  )}
                >
                  {t('logs.showMoreLogs')}
                </button>
              </div>
            )}
            {visibleLogs.map((log, index) => {
              if (log.kind === 'summary') {
                return (
                  <div
                    key={log.id}
                    className={clsx(
                      'mt-2 mb-1 flex items-center gap-2 rounded-lg border px-2.5 py-1.5 text-[11px] font-sans font-medium',
                      log.type === 'warning'
                        ? 'border-warning/35 bg-warning/10 text-warning'
                        : 'border-success/30 bg-success/10 text-success',
                    )}
                  >
                    <span className="tabular-nums opacity-75">{formatLogTime(log.timestamp, i18n.language)}</span>
                    <span className="min-w-0 flex-1 break-words">{log.message}</span>
                  </div>
                );
              }
              if (log.kind === 'task_start') {
                return (
                  <div
                    key={log.id}
                    className={clsx(
                      'flex items-center gap-2 px-1.5 py-1 text-[12px] font-sans font-semibold text-text-primary',
                      index > 0 && 'mt-1.5 border-t border-border/50 pt-2',
                    )}
                  >
                    <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-accent" aria-hidden="true" />
                    <span className="min-w-0 flex-1 truncate">{log.message}</span>
                    <span className="shrink-0 font-mono text-[10px] font-normal tabular-nums text-text-muted">
                      {formatLogTime(log.timestamp, i18n.language)}
                    </span>
                  </div>
                );
              }
              if (log.kind === 'task_end') {
                return (
                  <div
                    key={log.id}
                    className={clsx(
                      'flex items-start gap-2.5 pl-4 pr-1.5 py-0.5 text-[11px]',
                      log.type === 'error' ? 'text-error font-medium' : 'text-success/90',
                    )}
                  >
                    <span className="w-[56px] shrink-0 tabular-nums text-[10px] text-text-muted">
                      {formatLogTime(log.timestamp, i18n.language)}
                    </span>
                    <span className="min-w-0 flex-1 break-words">{log.message}</span>
                  </div>
                );
              }
              return (
                <Fragment key={log.id}>
                  <div
                    className={clsx(
                      'flex items-start gap-2.5 rounded-md pl-4 pr-1.5 py-0.5',
                      log.kind === 'detail' ? 'text-text-muted text-[11px]' : getLogColor(log.type),
                    )}
                  >
                    <span className="text-text-muted w-[56px] flex-shrink-0 tabular-nums text-[10px] leading-5">
                      {formatLogTime(log.timestamp, i18n.language)}
                    </span>
                    {log.html ? (
                      <span
                        className="min-w-0 flex-1 break-words leading-5 focus-content"
                        dangerouslySetInnerHTML={{ __html: log.html }}
                      />
                    ) : (
                      <span className="min-w-0 flex-1 break-words whitespace-pre-wrap leading-5">
                        {log.message}
                      </span>
                    )}
                  </div>
                </Fragment>
              );
            })}
            <div ref={logsEndRef} aria-hidden="true" />
          </>
        )}
      </div>

      {!isFollowingTail && visibleLogs.length > 0 && (
        <button
          type="button"
          onClick={handleJumpToLatest}
          className="absolute bottom-3 right-4 inline-flex items-center gap-1 rounded-full border border-border bg-bg-secondary px-3 py-1.5 text-xs text-text-primary shadow-md hover:bg-bg-hover hover:border-accent/50 transition-colors"
        >
          <ArrowDown className="w-3.5 h-3.5" />
          {t('logs.jumpToLatest')}
        </button>
      )}

      {/* 右键菜单 */}
      {menuState.isOpen && (
        <ContextMenu items={menuState.items} position={menuState.position} onClose={hideMenu} />
      )}

      {/* 导出日志 Modal */}
      <ExportLogsModal
        show={exportModal.show}
        status={exportModal.status === 'idle' ? 'exporting' : exportModal.status}
        zipPath={exportModal.zipPath}
        error={exportModal.error}
        onClose={closeExportModal}
        onOpen={openExportedFile}
      />
    </div>
  );
}
