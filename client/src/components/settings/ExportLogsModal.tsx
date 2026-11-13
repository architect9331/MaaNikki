import { useState, useCallback, useRef } from 'react';
import { useModalFocusTrap } from '@/components/ui/Modal';
import { useTranslation } from 'react-i18next';
import { Loader2, CheckCircle, XCircle, Archive, FolderOpen, Copy, Check } from 'lucide-react';

type ExportStatus = 'exporting' | 'success' | 'error';

interface ExportLogsModalProps {
  show: boolean;
  status: ExportStatus;
  zipPath?: string;
  error?: string;
  onClose: () => void;
  onOpen?: () => void;
}

export function ExportLogsModal({
  show,
  status,
  zipPath,
  error,
  onClose,
  onOpen,
}: ExportLogsModalProps) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);

  const handleCopy = useCallback(async () => {
    if (!zipPath) return;
    try {
      await navigator.clipboard.writeText(zipPath);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // 复制失败，静默处理
    }
  }, [zipPath]);

  const canClose = status !== 'exporting';
  const panelRef = useRef<HTMLDivElement>(null);
  // 导出进行中不响应 Esc，与点击遮罩的规则一致
  useModalFocusTrap({
    active: show,
    containerRef: panelRef,
    onEscape: () => {
      if (canClose) onClose();
    },
  });

  if (!show) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm animate-in fade-in duration-200"
      onClick={canClose ? onClose : undefined}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={t('debug.exportLogs')}
        className="mxu-overlay-surface w-full max-w-sm mx-4 bg-bg-secondary rounded-xl shadow-2xl border border-border overflow-hidden animate-in zoom-in-95 duration-200"
        onClick={(e) => e.stopPropagation()}
      >
        {/* 标题栏 */}
        <div className="flex items-center justify-between px-4 py-3 bg-bg-tertiary border-b border-border">
          <div className="flex items-center gap-2">
            <Archive className="w-5 h-5 text-accent" />
            <span className="text-sm font-medium text-text-primary">{t('debug.exportLogs')}</span>
          </div>
        </div>

        {/* 内容区 */}
        <div className="p-4 space-y-4">
          {status === 'exporting' && (
            <div className="flex items-center gap-3 text-sm text-text-primary">
              <Loader2 className="w-5 h-5 animate-spin text-accent" />
              <span>{t('debug.exportingLogs')}</span>
            </div>
          )}

          {status === 'success' && (
            <div className="space-y-3">
              <div className="flex items-center gap-2 p-3 bg-success/10 text-success rounded-lg">
                <CheckCircle className="w-5 h-5 shrink-0" />
                <span className="text-sm">{t('debug.logsExported')}</span>
              </div>
              {zipPath && (
                <div className="flex items-center gap-2">
                  <p className="flex-1 text-xs text-text-muted break-all">{zipPath}</p>
                  <button
                    onClick={handleCopy}
                    className="shrink-0 p-1 rounded-lg hover:bg-bg-hover transition-colors"
                    title={t('logs.copyAll')}
                  >
                    {copied ? (
                      <Check className="w-3.5 h-3.5 text-success" />
                    ) : (
                      <Copy className="w-3.5 h-3.5 text-text-muted" />
                    )}
                  </button>
                </div>
              )}
            </div>
          )}

          {status === 'error' && (
            <div className="space-y-3">
              <div className="flex items-center gap-2 p-3 bg-error/10 text-error rounded-lg">
                <XCircle className="w-5 h-5 shrink-0" />
                <span className="text-sm">{t('debug.exportLogsFailed')}</span>
              </div>
              {error && <p className="text-xs text-text-muted break-all">{error}</p>}
            </div>
          )}
        </div>

        {/* 底部按钮 */}
        {canClose && (
          <div className="flex items-center justify-end gap-2 px-4 py-3 bg-bg-tertiary border-t border-border">
            {status === 'success' && onOpen && (
              <button
                onClick={onOpen}
                className="flex items-center gap-1.5 px-4 py-2 text-sm bg-accent text-on-accent hover:bg-accent-hover rounded-lg transition-colors"
              >
                <FolderOpen className="w-4 h-4" />
                {t('common.open')}
              </button>
            )}
            <button
              onClick={onClose}
              className="px-4 py-2 text-sm bg-bg-tertiary hover:bg-bg-hover text-text-primary border border-border rounded-lg transition-colors"
            >
              {t('common.close')}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
