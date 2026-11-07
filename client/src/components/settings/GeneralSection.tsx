import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  AlertCircle,
  AppWindowMac,
  BarChart3,
  Bell,
  BrushCleaning,
  Check,
  ChevronDown,
  ChevronRight,
  HeartHandshake,
  ListChecks,
  Maximize2,
  Play,
  Power,
  Rocket,
  Settings2,
} from 'lucide-react';

import { useAppStore } from '@/stores/appStore';
import { isTelemetryBlockedByBuild } from '@/services/telemetryService';
import { defaultAddTaskPanelHeight, defaultWindowSize } from '@/types/config';
import { isTauri } from '@/utils/paths';
import { SwitchButton } from '@/components/FormControls';
import { DesktopOnlyWrapper } from '@/components/ui/DesktopOnlyWrapper';

export function GeneralSection() {
  const { t } = useTranslation();
  const {
    showOptionPreview,
    setShowOptionPreview,
    confirmBeforeDelete,
    setConfirmBeforeDelete,
    minimizeToTray,
    setMinimizeToTray,
    setRightPanelWidth,
    setRightPanelCollapsed,
    setAddTaskPanelHeight,
    instances,
    autoStartInstanceId,
    setAutoStartInstanceId,
    autoRunOnLaunch,
    setAutoRunOnLaunch,
    autoStartRemovedInstanceName,
    autoClearLogsOnLaunch,
    setAutoClearLogsOnLaunch,
    helpImproveSoftware,
    helpImproveRouteTelemetry,
    helpImproveBlessingStats,
    helpImproveTaskStats,
    runResultWebhook,
    setRunResultWebhook,
    setHelpImproveSoftware,
    setHelpImproveRouteTelemetry,
    setHelpImproveBlessingStats,
    setHelpImproveTaskStats,
    setCurrentPage,
    projectInterface,
  } = useAppStore();

  // 调试 / 开发版本禁用遥测开关（不可开启）
  const telemetryBlocked = isTelemetryBlockedByBuild(projectInterface);

  // 开机自启动状态（直接从 Tauri 插件查询，不走 store）
  const [autoStartEnabled, setAutoStartEnabled] = useState(false);
  const [autoStartLoading, setAutoStartLoading] = useState(false);
  const isWindowsRef = useRef(false);

  // 自定义下拉框状态
  const [instanceDropdownOpen, setInstanceDropdownOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!isTauri()) return;
    import('@tauri-apps/api/core').then(({ invoke: tauriInvoke }) => {
      tauriInvoke<string>('get_os')
        .then((os) => {
          isWindowsRef.current = os === 'windows';
          if (isWindowsRef.current) {
            tauriInvoke<boolean>('autostart_is_enabled', { suffix: projectInterface?.name })
              .then(setAutoStartEnabled)
              .catch(() => {});
          } else {
            import('@tauri-apps/plugin-autostart').then(({ isEnabled }) => {
              isEnabled()
                .then(setAutoStartEnabled)
                .catch(() => {});
            });
          }
        })
        .catch(() => {});
    });
  }, []);

  // 点击外部关闭下拉框
  useEffect(() => {
    if (!instanceDropdownOpen) return;
    const handleClickOutside = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setInstanceDropdownOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [instanceDropdownOpen]);

  const handleAutoStartToggle = useCallback(async (enabled: boolean) => {
    if (!isTauri()) return;
    setAutoStartLoading(true);
    try {
      if (isWindowsRef.current) {
        const { invoke } = await import('@tauri-apps/api/core');
        await invoke(enabled ? 'autostart_enable' : 'autostart_disable', {
          suffix: projectInterface?.name,
        });
      } else {
        const { enable, disable } = await import('@tauri-apps/plugin-autostart');
        if (enabled) {
          await enable();
        } else {
          await disable();
        }
      }
      setAutoStartEnabled(enabled);
    } catch {
      // 恢复原状
    } finally {
      setAutoStartLoading(false);
    }
  }, []);

  const handleResetWindowLayout = useCallback(async () => {
    if (!isTauri()) return;

    try {
      const { getCurrentWindow } = await import('@tauri-apps/api/window');
      const { LogicalSize } = await import('@tauri-apps/api/dpi');
      const currentWindow = getCurrentWindow();

      await currentWindow.setSize(
        new LogicalSize(defaultWindowSize.width, defaultWindowSize.height),
      );

      await currentWindow.center();
      useAppStore.getState().setWindowPosition(undefined);

      setRightPanelWidth(320);
      setRightPanelCollapsed(false);
      setAddTaskPanelHeight(defaultAddTaskPanelHeight);
    } catch {
      // ignore
    }
  }, [setRightPanelWidth, setRightPanelCollapsed, setAddTaskPanelHeight]);

  // 构建下拉选项列表
  const dropdownOptions = [
    { id: '', name: t('settings.autoStartInstanceNone') },
    ...instances.map((inst) => ({ id: inst.id, name: inst.name })),
  ];
  const selectedOption = dropdownOptions.find((opt) => opt.id === (autoStartInstanceId ?? ''));

  return (
    <section id="section-general" className="space-y-4 scroll-mt-4">
      <h2 className="text-sm font-semibold text-text-primary uppercase tracking-wider flex items-center gap-2">
        <Settings2 className="w-4 h-4" />
        {t('settings.general')}
      </h2>

      {/* 自动化启动设置组 */}
      <DesktopOnlyWrapper>
        <div className="bg-bg-secondary rounded-xl p-4 border border-border space-y-4">
          {/* ① 开机自启动 */}
          {isTauri() && (
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <Power className="w-5 h-5 text-accent" />
                <div>
                  <span className="font-medium text-text-primary">{t('settings.autoStart')}</span>
                  <p className="text-xs text-text-muted mt-0.5">{t('settings.autoStartHint')}</p>
                </div>
              </div>
              <SwitchButton
                value={autoStartEnabled}
                onChange={handleAutoStartToggle}
                disabled={autoStartLoading}
              />
            </div>
          )}

          {/* ② 启动后自动执行 */}
          <div className={isTauri() ? 'pt-4 border-t border-border' : ''}>
            <div className="flex items-center gap-3 mb-3">
              <Play className="w-5 h-5 text-accent" />
              <div>
                <span className="font-medium text-text-primary">
                  {t('settings.autoStartInstance')}
                </span>
                <p className="text-xs text-text-muted mt-0.5">
                  {t('settings.autoStartInstanceHint')}
                </p>
              </div>
            </div>

            {/* 自定义下拉框 */}
            <div className="relative" ref={dropdownRef}>
              <button
                type="button"
                onClick={() => setInstanceDropdownOpen((prev) => !prev)}
                onKeyDown={(e) => {
                  if (e.key === 'Escape') setInstanceDropdownOpen(false);
                }}
                className="w-full px-3 py-2.5 text-sm rounded-lg border flex items-center justify-between gap-2 bg-bg-tertiary border-border text-text-primary hover:bg-bg-hover transition-colors focus:outline-none focus:ring-2 focus:ring-accent/50"
                aria-haspopup="listbox"
                aria-expanded={instanceDropdownOpen}
              >
                <span className="truncate">
                  {selectedOption?.name ?? t('settings.autoStartInstanceNone')}
                </span>
                <ChevronDown
                  className={`w-4 h-4 text-text-muted shrink-0 transition-transform ${instanceDropdownOpen ? 'rotate-180' : ''}`}
                />
              </button>

              {instanceDropdownOpen && (
                <div
                  className="mxu-overlay-surface absolute right-0 z-20 mt-1 w-full min-w-[160px] max-h-60 overflow-y-auto rounded-lg border border-border bg-bg-primary shadow-lg"
                  role="listbox"
                >
                  {dropdownOptions.map((opt) => {
                    const isSelected = opt.id === (autoStartInstanceId ?? '');
                    return (
                      <button
                        key={opt.id}
                        type="button"
                        role="option"
                        aria-selected={isSelected}
                        className={`w-full px-3 py-2 text-sm text-left flex items-center gap-2 transition-colors ${
                          isSelected
                            ? 'bg-accent/10 text-accent font-medium'
                            : 'text-text-primary hover:bg-bg-hover'
                        }`}
                        onClick={() => {
                          setAutoStartInstanceId(opt.id || undefined);
                          setInstanceDropdownOpen(false);
                        }}
                      >
                        <Check
                          className={`w-4 h-4 shrink-0 ${isSelected ? 'opacity-100' : 'opacity-0'}`}
                        />
                        <span className="truncate">{opt.name}</span>
                      </button>
                    );
                  })}
                </div>
              )}
            </div>

            {/* 被删除的自动执行配置提示 */}
            {autoStartRemovedInstanceName && (
              <div className="flex items-center gap-2 mt-2 px-2.5 py-1.5 rounded-lg bg-error/10 text-error text-xs">
                <AlertCircle className="w-3.5 h-3.5 flex-shrink-0" />
                <span>
                  {t('settings.autoStartInstanceRemoved', { name: autoStartRemovedInstanceName })}
                </span>
              </div>
            )}
          </div>

          {/* ③ 手动启动时也自动执行 */}
          <div className="flex items-center justify-between pt-4 border-t border-border">
            <div className="flex items-center gap-3">
              <Rocket className="w-5 h-5 text-accent" />
              <div>
                <span className="font-medium text-text-primary">
                  {t('settings.autoRunOnLaunch')}
                </span>
                <p className="text-xs text-text-muted mt-0.5">
                  {t('settings.autoRunOnLaunchHint')}
                </p>
              </div>
            </div>
            <SwitchButton
              value={autoRunOnLaunch}
              onChange={(v) => setAutoRunOnLaunch(v)}
              disabled={!autoStartInstanceId}
            />
          </div>
        </div>
      </DesktopOnlyWrapper>

      {/* ④ 最小化到托盘 */}
      <DesktopOnlyWrapper>
        <div className="bg-bg-secondary rounded-xl p-4 border border-border">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <AppWindowMac className="w-5 h-5 text-accent" />
              <div>
                <span className="font-medium text-text-primary">
                  {t('settings.minimizeToTray')}
                </span>
                <p className="text-xs text-text-muted mt-0.5">{t('settings.minimizeToTrayHint')}</p>
              </div>
            </div>
            <SwitchButton value={minimizeToTray} onChange={(v) => setMinimizeToTray(v)} />
          </div>
        </div>
      </DesktopOnlyWrapper>

      {/* ⑥ 显示选项预览 */}
      <div className="bg-bg-secondary rounded-xl p-4 border border-border">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <ListChecks className="w-5 h-5 text-accent" />
            <div>
              <span className="font-medium text-text-primary">
                {t('settings.showOptionPreview')}
              </span>
              <p className="text-xs text-text-muted mt-0.5">
                {t('settings.showOptionPreviewHint')}
              </p>
            </div>
          </div>
          <SwitchButton value={showOptionPreview} onChange={(v) => setShowOptionPreview(v)} />
        </div>
      </div>

      {/* ⑥ 帧率选择器 */}

      {/* 自动清理运行日志 */}
      <div className="bg-bg-secondary rounded-xl p-4 border border-border">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <BrushCleaning className="w-5 h-5 text-accent" />
            <div>
              <span className="font-medium text-text-primary">
                {t('settings.autoClearLogsOnLaunch')}
              </span>
              <p className="text-xs text-text-muted mt-0.5">
                {t('settings.autoClearLogsOnLaunchHint')}
              </p>
            </div>
          </div>
          <SwitchButton
            value={autoClearLogsOnLaunch}
            onChange={(v) => setAutoClearLogsOnLaunch(v)}
          />
        </div>
      </div>

      {/* ⑦ 删除确认 */}
      <div className="bg-bg-secondary rounded-xl p-4 border border-border">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <AlertCircle className="w-5 h-5 text-accent" />
            <div>
              <span className="font-medium text-text-primary">
                {t('settings.confirmBeforeDelete')}
              </span>
              <p className="text-xs text-text-muted mt-0.5">
                {t('settings.confirmBeforeDeleteHint')}
              </p>
            </div>
          </div>
          <SwitchButton value={confirmBeforeDelete} onChange={(v) => setConfirmBeforeDelete(v)} />
        </div>
      </div>

      {/* ⑧ 帮助改进软件（三个独立子开关） */}
      <div className="bg-bg-secondary rounded-xl p-4 border border-border space-y-4">
        <div className="flex items-center gap-3">
          <HeartHandshake className="w-5 h-5 text-accent" />
          <div>
            <span className="font-medium text-text-primary">
              {t('settings.helpImproveSoftware')}
            </span>
            {telemetryBlocked && (
              <p className="text-xs text-text-muted mt-0.5">
                {t('settings.helpImproveSoftwareDisabledHint')}
              </p>
            )}
          </div>
        </div>

        {/* 1. 错误与运行诊断 */}
        <div className="flex items-center justify-between pt-3 border-t border-border">
          <div className="pr-4">
            <span className="text-sm font-medium text-text-primary">
              {t('settings.helpImproveDiagnostics')}
            </span>
            <p className="text-xs text-text-muted mt-0.5">
              {t('settings.helpImproveDiagnosticsHint')}
            </p>
          </div>
          <SwitchButton
            value={telemetryBlocked ? false : helpImproveSoftware}
            disabled={telemetryBlocked}
            onChange={(v) => setHelpImproveSoftware(v)}
          />
        </div>

        {/* 2. 路线与坐标调优 */}
        <div className="flex items-center justify-between pt-3 border-t border-border">
          <div className="pr-4">
            <span className="text-sm font-medium text-text-primary">
              {t('settings.helpImproveRouteTelemetry')}
            </span>
            <p className="text-xs text-text-muted mt-0.5">
              {t('settings.helpImproveRouteTelemetryHint')}
            </p>
          </div>
          <SwitchButton
            value={telemetryBlocked ? false : helpImproveRouteTelemetry}
            disabled={telemetryBlocked}
            onChange={(v) => setHelpImproveRouteTelemetry(v)}
          />
        </div>

        {/* 3. 祝福闪光统计 */}
        <div className="flex items-center justify-between pt-3 border-t border-border">
          <div className="pr-4">
            <span className="text-sm font-medium text-text-primary">
              {t('settings.helpImproveBlessingStats')}
            </span>
            <p className="text-xs text-text-muted mt-0.5">
              {t('settings.helpImproveBlessingStatsHint')}
            </p>
          </div>
          <SwitchButton
            value={telemetryBlocked ? false : helpImproveBlessingStats}
            disabled={telemetryBlocked}
            onChange={(v) => setHelpImproveBlessingStats(v)}
          />
        </div>

        {/* 4. 任务偏好与美鸭梨挖掘统计 */}
        <div className="flex items-center justify-between pt-3 border-t border-border">
          <div className="pr-4">
            <span className="text-sm font-medium text-text-primary">
              任务偏好与挖掘统计
            </span>
            <p className="text-xs text-text-muted mt-0.5">
              匿名上报日常任务启用分布、幻境挑战选择与美鸭梨挖掘目标等偏好统计，用于优化默认任务选择与任务推荐（不含账号信息）
            </p>
          </div>
          <SwitchButton
            value={telemetryBlocked ? false : helpImproveTaskStats}
            disabled={telemetryBlocked}
            onChange={(v) => setHelpImproveTaskStats(v)}
          />
        </div>
      </div>

      {/* MAA 运行任务统计与纪念二级入口 */}
      {isTauri() && (
        <div className="bg-bg-secondary rounded-xl p-4 border border-border">
          <div className="flex items-center justify-between gap-3">
            <div className="flex items-center gap-3">
              <BarChart3 className="w-5 h-5 text-accent" />
              <div>
                <span className="font-medium text-text-primary">MAA 运行任务统计与纪念</span>
                <p className="text-xs text-text-muted mt-0.5">
                  查看累计为你节省的时间、各任务执行次数排行以及闪光收集与奇迹大陆探索里程碑
                </p>
              </div>
            </div>
            <button
              type="button"
              onClick={() => setCurrentPage('statistics')}
              className="inline-flex shrink-0 items-center gap-1 px-3 py-2 text-sm font-medium bg-bg-tertiary hover:bg-bg-hover rounded-lg transition-colors text-text-primary"
            >
              <span>查看统计</span>
              <ChevronRight className="w-4 h-4 text-text-muted" />
            </button>
          </div>
        </div>
      )}

      {/* 运行结果推送 */}
      {isTauri() && (
        <div className="bg-bg-secondary rounded-xl p-4 border border-border">
          <label htmlFor="run-result-webhook" className="flex items-center gap-3">
            <Bell className="w-5 h-5 text-accent" />
            <span className="font-medium text-text-primary">运行结果推送</span>
          </label>
          <p className="text-xs text-text-muted mt-1 ml-8 leading-5">
            运行结束后向此地址发送 GET 请求，手动停止时不发送。可用 {'{title}'} 和 {'{content}'} 代表标题和内容，例如 Server酱：
            https://sctapi.ftqq.com/密钥.send?title={'{title}'}&amp;desp={'{content}'}；Bark：https://api.day.app/密钥/{'{title}'}/{'{content}'}。留空则不推送。
          </p>
          <input
            id="run-result-webhook"
            type="url"
            value={runResultWebhook}
            onChange={(event) => setRunResultWebhook(event.target.value)}
            placeholder="https://"
            className="mt-2 ml-8 w-[calc(100%-2rem)] rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm text-text-primary outline-none focus:border-accent"
          />
        </div>
      )}

      {/* ⑨ 重置窗口布局 */}
      {isTauri() && (
        <div className="bg-bg-secondary rounded-xl p-4 border border-border">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <Maximize2 className="w-5 h-5 text-accent" />
              <div>
                <span className="font-medium text-text-primary">
                  {t('settings.resetWindowLayout')}
                </span>
                <p className="text-xs text-text-muted mt-0.5">
                  {t('settings.resetWindowLayoutHint')}
                </p>
              </div>
            </div>
            <button
              onClick={handleResetWindowLayout}
              className="px-4 py-2 text-sm font-medium bg-bg-tertiary hover:bg-bg-hover rounded-lg transition-colors"
            >
              {t('common.reset')}
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
