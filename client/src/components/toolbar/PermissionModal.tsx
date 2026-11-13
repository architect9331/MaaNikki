import { useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { ShieldAlert, Loader2, AlertCircle } from 'lucide-react';
import clsx from 'clsx';
import { ModalPortal, useModalFocusTrap } from '@/components/ui/Modal';

interface PermissionModalProps {
  isOpen: boolean;
  isRestarting: boolean;
  /** 重启没有成功时为 true，弹窗内说明下一步怎么做 */
  failed?: boolean;
  onCancel: () => void;
  onRestart: () => void;
}

export function PermissionModal({
  isOpen,
  isRestarting,
  failed,
  onCancel,
  onRestart,
}: PermissionModalProps) {
  const { t } = useTranslation();
  const panelRef = useRef<HTMLDivElement>(null);
  const restartRef = useRef<HTMLButtonElement>(null);

  useModalFocusTrap({
    active: isOpen,
    containerRef: panelRef,
    initialFocusRef: restartRef,
    // 重启过程中不允许用 Esc 关掉，避免以为已经取消
    onEscape: () => {
      if (!isRestarting) onCancel();
    },
  });

  if (!isOpen) return null;

  return (
    <ModalPortal>
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4 backdrop-blur-sm">
        <div
          ref={panelRef}
          role="dialog"
          aria-modal="true"
          aria-labelledby="permission-title"
          className="mxu-overlay-surface bg-bg-primary rounded-xl shadow-xl max-w-md w-full overflow-hidden"
        >
          {/* 标题栏 */}
          <div className="flex items-center gap-3 px-5 py-4 border-b border-border bg-bg-secondary">
            <ShieldAlert className="w-5 h-5 text-warning" />
            <h3 id="permission-title" className="font-medium text-text-primary">
              {t('permission.title')}
            </h3>
          </div>

          {/* 内容 */}
          <div className="px-5 py-4">
            <p className="text-text-secondary text-sm leading-relaxed">{t('permission.message')}</p>
            <p className="text-text-muted text-xs mt-3">{t('permission.hint')}</p>
            {failed && (
              <p
                role="alert"
                className="mt-3 flex items-start gap-1.5 rounded-lg bg-error/10 px-3 py-2 text-xs leading-5 text-error"
              >
                <AlertCircle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
                {t('permission.restartFailed')}
              </p>
            )}
          </div>

          {/* 按钮 */}
          <div className="flex justify-end gap-2 px-5 py-4 border-t border-border bg-bg-secondary">
            <button
              type="button"
              onClick={onCancel}
              disabled={isRestarting}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-bg-tertiary hover:bg-bg-hover text-text-secondary transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {t('common.cancel')}
            </button>
            <button
              ref={restartRef}
              type="button"
              onClick={onRestart}
              disabled={isRestarting}
              className={clsx(
                'flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-colors',
                isRestarting
                  ? 'bg-accent/70 text-white cursor-wait'
                  : 'bg-accent hover:bg-accent-hover text-on-accent',
              )}
            >
              {isRestarting ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin" />
                  <span>{t('permission.restarting')}</span>
                </>
              ) : (
                <>
                  <ShieldAlert className="w-4 h-4" />
                  <span>{t('permission.restart')}</span>
                </>
              )}
            </button>
          </div>
        </div>
      </div>
    </ModalPortal>
  );
}
