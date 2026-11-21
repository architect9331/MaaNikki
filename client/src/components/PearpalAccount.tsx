import { useRef } from 'react';
import {
  ArrowRight,
  BookOpen,
  Compass,
  Gem,
  Loader2,
  LogIn,
  LogOut,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  X,
} from 'lucide-react';
import clsx from 'clsx';
import {
  getPearpalLoginState,
  logoutPearpal,
  openPearpalLogin,
  pearpalStatusLabel,
  refreshPearpalStatus,
  usePearpalStore,
  type PearpalStatus,
} from '@/services/pearpalService';
import { useResonanceViewStore } from '@/services/resonanceService';
import { useAppStore } from '@/stores/appStore';
import { openCollectionHub } from '@/stores/collectionHubStore';
import { isTauri } from '@/utils/paths';
import { FeatureTile } from './FeatureTile';
import { ModalPortal, useModalFocusTrap } from './ui/Modal';
import { Card, primaryButton, secondaryButton } from './ui/FeaturePage';
import { Tooltip } from './ui/Tooltip';

export function PearPalIcon({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      <path d="M12 6.5c.3-1.4 1-2.5 2.1-3.3" />
      <path d="M12 5.2c-1.1-1.3-2.6-1.7-3.9-1.1.6 1.3 2 2 3.4 1.6" />
      <path d="M12 6.5c2.2 0 3.3 1.5 3.9 3.6.5 1.7 2.1 3.3 2.1 6.1a6 6 0 0 1-12 0c0-2.8 1.6-4.4 2.1-6.1C8.7 8 9.8 6.5 12 6.5Z" />
      <rect x="9.2" y="13.5" width="5.6" height="4.2" rx="1.1" />
      <path d="M11.6 15.6h.8" />
    </svg>
  );
}

function collectedSummary(status: PearpalStatus): string | null {
  if (
    status.star_collected == null &&
    status.dewdrop_collected == null &&
    status.box_collected == null
  ) {
    return null;
  }
  return `奇想星 ${status.star_collected ?? '—'} · 灵感露珠 ${status.dewdrop_collected ?? '—'} · 宝箱 ${status.box_collected ?? '—'}`;
}

export function PearpalStatusPill({ className }: { className?: string }) {
  const status = usePearpalStore((s) => s.status);
  const busy = usePearpalStore((s) => s.busy);
  const loginError = usePearpalStore((s) => s.loginError);
  const openModal = usePearpalStore((s) => s.openModal);

  if (!isTauri()) return null;

  const state = getPearpalLoginState(status);
  const progress = collectedSummary(status);
  const errorText = loginError || (status.has_login ? status.error : '') || '';

  const tooltip = (
    <div className="space-y-1">
      <p className="font-medium">
        美鸭梨账号 · {pearpalStatusLabel(status)}
        {status.uid ? `（UID ${status.uid}）` : ''}
      </p>
      {state === 'logged_in' && progress && (
        <p className="text-text-secondary tabular-nums">{progress}</p>
      )}
      {state === 'expired' && errorText && (
        <p className="text-warning">{errorText}</p>
      )}
      <p className="text-text-muted">
        {state === 'logged_in'
          ? '点击查看账号详情、切换账号或退出登录'
          : state === 'expired'
            ? '点击立即重新登录美鸭梨'
            : '点击打开官方美鸭梨网页登录，登录后全应用同步生效'}
      </p>
    </div>
  );

  const handleClick = () => {
    if (state === 'logged_in') openModal();
    else void openPearpalLogin({ switchAccount: state === 'expired' });
  };

  return (
    <Tooltip side="bottom" content={tooltip}>
      <button
        type="button"
        disabled={busy}
        onClick={handleClick}
        className={clsx(
          'inline-flex shrink-0 items-center gap-1.5 rounded-lg border px-2.5 py-1 text-xs font-medium transition-colors disabled:opacity-50',
          state === 'logged_in' &&
            'border-border bg-bg-primary text-text-secondary hover:bg-bg-hover hover:text-text-primary',
          state === 'expired' &&
            'border-warning/40 bg-warning/10 text-warning hover:bg-warning/15',
          state === 'logged_out' &&
            'border-accent/40 bg-accent/10 text-accent hover:bg-accent/15',
          className,
        )}
      >
        {busy ? (
          <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" aria-hidden="true" />
        ) : state === 'logged_out' ? (
          <PearPalIcon className="h-3.5 w-3.5 shrink-0" />
        ) : (
          <span
            aria-hidden="true"
            className={clsx(
              'h-2 w-2 shrink-0 rounded-full',
              state === 'logged_in' ? 'bg-success' : 'bg-warning',
            )}
          />
        )}
        <span>
          {busy
            ? '登录中…'
            : state === 'logged_in'
              ? '美鸭梨已登录'
              : state === 'expired'
                ? '美鸭梨已过期'
                : '登录美鸭梨'}
        </span>
      </button>
    </Tooltip>
  );
}

export function PearpalAccountTile({ compact = false }: { compact?: boolean }) {
  const status = usePearpalStore((s) => s.status);
  const busy = usePearpalStore((s) => s.busy);
  const loginError = usePearpalStore((s) => s.loginError);
  const openModal = usePearpalStore((s) => s.openModal);

  if (!isTauri()) return null;

  const state = getPearpalLoginState(status);
  const progress = collectedSummary(status);
  const errorText = loginError || (status.has_login ? status.error : '') || '';
  const statusTone = state === 'logged_in' ? 'ok' : state === 'expired' ? 'warn' : 'idle';
  const tileStatus = busy
    ? '处理中…'
    : state === 'logged_in'
      ? '已登录'
      : state === 'expired'
        ? '登录已过期'
        : '未登录 · 去登录';

  const tooltip = (
    <div className="space-y-1">
      <p className="font-medium">
        美鸭梨账号 · {pearpalStatusLabel(status)}
        {status.uid ? `（UID ${status.uid}）` : ''}
      </p>
      {state === 'logged_in' && (
        <p className="text-text-secondary tabular-nums">
          {progress || '已登录国服美鸭梨，支持地图标记、共鸣记录与闪光图鉴核对。'}
        </p>
      )}
      {state === 'expired' && (
        <p className="text-warning">{errorText || '登录状态已过期，请重新登录。'}</p>
      )}
      {state === 'logged_out' && (
        <p className="text-text-secondary">
          登录后可用于地图标记、共鸣记录、大地图可挖抽数与祝福闪光图鉴核对。
        </p>
      )}
      <p className="text-text-muted">点击登录或管理美鸭梨账号</p>
    </div>
  );

  return (
    <FeatureTile
      icon={PearPalIcon}
      title="美鸭梨账号"
      compact={compact}
      busy={busy}
      warn={state === 'expired'}
      statusTone={statusTone}
      status={tileStatus}
      className={clsx(!compact && state === 'expired' && 'border-warning/40')}
      tooltip={tooltip}
      onClick={openModal}
    />
  );
}

export function PearpalAccountModal() {
  const modalOpen = usePearpalStore((s) => s.modalOpen);
  const closeModal = usePearpalStore((s) => s.closeModal);
  const status = usePearpalStore((s) => s.status);
  const busy = usePearpalStore((s) => s.busy);
  const refreshing = usePearpalStore((s) => s.refreshing);
  const loginError = usePearpalStore((s) => s.loginError);
  const panelRef = useRef<HTMLDivElement>(null);

  useModalFocusTrap({ active: modalOpen && isTauri(), containerRef: panelRef, onEscape: closeModal });

  if (!modalOpen || !isTauri()) return null;

  const state = getPearpalLoginState(status);
  const progress = collectedSummary(status);
  const errorText = loginError || (status.has_login ? status.error : '') || '';

  const jumpTo = (action: () => void) => {
    closeModal();
    action();
  };

  return (
    <ModalPortal>
      <div
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4 backdrop-blur-sm"
        onMouseDown={(event) => {
          if (event.target === event.currentTarget) closeModal();
        }}
      >
        <div
          ref={panelRef}
          role="dialog"
          aria-modal="true"
          aria-labelledby="pearpal-account-modal-title"
          className="relative w-full max-w-md overflow-hidden rounded-xl border border-border bg-bg-secondary text-text-primary shadow-xl"
        >
          <div className="flex items-center gap-2.5 border-b border-border px-5 py-3.5">
            <PearPalIcon className="h-5 w-5 text-accent" />
            <h2 id="pearpal-account-modal-title" className="text-base font-semibold">
              美鸭梨账号
            </h2>
            <span
              className={clsx(
                'inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium',
                state === 'logged_in' && 'bg-success/15 text-success',
                state === 'expired' && 'bg-warning/15 text-warning',
                state === 'logged_out' && 'bg-bg-tertiary text-text-secondary',
              )}
            >
              <span
                aria-hidden="true"
                className={clsx(
                  'h-1.5 w-1.5 rounded-full',
                  state === 'logged_in'
                    ? 'bg-success'
                    : state === 'expired'
                      ? 'bg-warning'
                      : 'bg-text-muted',
                )}
              />
              {state === 'logged_in' ? '已登录' : state === 'expired' ? '登录已过期' : '尚未登录'}
            </span>
            <button
              type="button"
              onClick={closeModal}
              aria-label="关闭"
              className="ml-auto rounded-lg p-1.5 text-text-secondary hover:bg-bg-hover"
            >
              <X className="h-4 w-4" />
            </button>
          </div>

          <div className="space-y-3 px-5 py-4">
            <div className="space-y-2 rounded-lg border border-border bg-bg-primary/70 px-4 py-3 text-xs">
              {state === 'logged_in' ? (
                <>
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-text-secondary">登录状态</span>
                    <span className="font-medium text-success">已连接美鸭梨国服</span>
                  </div>
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-text-secondary">绑定角色</span>
                    <span className="tabular-nums text-text-primary">
                      {status.uid ? `UID ${status.uid}` : '同步手账或共鸣后自动识别'}
                    </span>
                  </div>
                  {progress && (
                    <div className="flex items-center justify-between gap-3">
                      <span className="text-text-secondary">大地图收集</span>
                      <span className="tabular-nums text-text-primary">{progress}</span>
                    </div>
                  )}
                </>
              ) : (
                <p className="leading-5 text-text-secondary">
                  登录正在游玩的<strong className="font-medium text-text-primary">美鸭梨国服账号</strong>
                  后，可全局启用大地图/小地图未收集标记、共鸣历史与大地图可挖抽数核算、祝福闪光图鉴核对及大世界探索纪念。
                </p>
              )}
              <p className="flex items-center gap-1.5 pt-0.5 text-[11px] text-text-muted">
                <ShieldCheck className="h-3.5 w-3.5 shrink-0 text-accent-secondary" />
                <span>登录凭据通过 Windows 系统加密仅保存在本地，不上传第三方服务器。</span>
              </p>
            </div>

            {errorText && (
              <p
                role="status"
                className="rounded-lg border border-warning/30 bg-warning/10 px-3 py-2 text-xs leading-5 text-warning"
              >
                {errorText}
              </p>
            )}

            <div className="space-y-1.5">
              <p className="text-[11px] font-medium text-text-muted">关联功能入口</p>
              <div className="grid grid-cols-2 gap-2">
                <button
                  type="button"
                  onClick={() => jumpTo(() => useAppStore.getState().setCurrentPage('exploration'))}
                  className="flex items-center gap-2 rounded-lg border border-border bg-bg-primary/60 px-3 py-2 text-left text-xs text-text-primary transition-colors hover:bg-bg-hover"
                >
                  <Compass className="h-3.5 w-3.5 shrink-0 text-accent-secondary" />
                  <span className="min-w-0 flex-1 truncate">地图标记</span>
                  <ArrowRight className="h-3 w-3 shrink-0 text-text-muted" />
                </button>
                <button
                  type="button"
                  onClick={() =>
                    jumpTo(() => {
                      useResonanceViewStore.getState().setView('outfits');
                      useAppStore.getState().setCurrentPage('resonance');
                    })
                  }
                  className="flex items-center gap-2 rounded-lg border border-border bg-bg-primary/60 px-3 py-2 text-left text-xs text-text-primary transition-colors hover:bg-bg-hover"
                >
                  <BookOpen className="h-3.5 w-3.5 shrink-0 text-accent-secondary" />
                  <span className="min-w-0 flex-1 truncate">共鸣记录</span>
                  <ArrowRight className="h-3 w-3 shrink-0 text-text-muted" />
                </button>
                <button
                  type="button"
                  onClick={() =>
                    jumpTo(() => {
                      useResonanceViewStore.getState().setView('map_resources');
                      useAppStore.getState().setCurrentPage('resonance');
                    })
                  }
                  className="flex items-center gap-2 rounded-lg border border-border bg-bg-primary/60 px-3 py-2 text-left text-xs text-text-primary transition-colors hover:bg-bg-hover"
                >
                  <Gem className="h-3.5 w-3.5 shrink-0 text-accent-secondary" />
                  <span className="min-w-0 flex-1 truncate">大地图资源</span>
                  <ArrowRight className="h-3 w-3 shrink-0 text-text-muted" />
                </button>
                <button
                  type="button"
                  onClick={() => jumpTo(() => openCollectionHub('blessing'))}
                  className="flex items-center gap-2 rounded-lg border border-border bg-bg-primary/60 px-3 py-2 text-left text-xs text-text-primary transition-colors hover:bg-bg-hover"
                >
                  <Sparkles className="h-3.5 w-3.5 shrink-0 text-accent-secondary" />
                  <span className="min-w-0 flex-1 truncate">祝福闪光核对</span>
                  <ArrowRight className="h-3 w-3 shrink-0 text-text-muted" />
                </button>
              </div>
            </div>
          </div>

          <div className="flex items-center gap-2 border-t border-border px-5 py-3.5">
            <button
              type="button"
              onClick={() => void refreshPearpalStatus()}
              disabled={refreshing || busy}
              title="刷新美鸭梨登录状态"
              aria-label="刷新美鸭梨登录状态"
              className="mr-auto rounded-lg p-2 text-text-secondary hover:bg-bg-hover disabled:opacity-50"
            >
              <RefreshCw className={clsx('h-4 w-4', refreshing && 'animate-spin')} />
            </button>
            {status.has_login && (
              <button
                type="button"
                disabled={busy}
                onClick={() => void logoutPearpal()}
                className={secondaryButton}
              >
                <LogOut className="h-3.5 w-3.5" />
                退出登录
              </button>
            )}
            <button
              type="button"
              disabled={busy}
              onClick={() => void openPearpalLogin({ switchAccount: status.has_login })}
              className={primaryButton}
            >
              {busy ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <LogIn className="h-3.5 w-3.5" />
              )}
              {state === 'logged_in'
                ? '切换账号'
                : state === 'expired'
                  ? '重新登录'
                  : '登录美鸭梨'}
            </button>
          </div>
        </div>
      </div>
    </ModalPortal>
  );
}

export function PearpalAccountCard({ variant = 'feature' }: { variant?: 'feature' | 'settings' }) {
  const status = usePearpalStore((s) => s.status);
  const busy = usePearpalStore((s) => s.busy);
  const loginError = usePearpalStore((s) => s.loginError);

  if (!isTauri()) return null;

  const state = getPearpalLoginState(status);
  const progress = collectedSummary(status);
  const errorText = loginError || (status.has_login ? status.error : '') || '';

  return (
    <Card
      icon={PearPalIcon}
      title="美鸭梨账号"
      aside={
        <span className="inline-flex items-center gap-1.5 text-xs text-text-secondary">
          <span
            aria-hidden="true"
            className={clsx(
              'h-2 w-2 rounded-full',
              state === 'logged_in'
                ? 'bg-success'
                : state === 'expired'
                  ? 'bg-warning'
                  : 'bg-text-muted',
            )}
          />
          <span
            className={clsx(
              state === 'logged_in' && 'text-success',
              state === 'expired' && 'text-warning',
            )}
          >
            {state === 'logged_in' ? '已登录' : state === 'expired' ? '登录已过期' : '尚未登录'}
          </span>
          {status.uid && state === 'logged_in' && (
            <span className="tabular-nums text-text-muted">· UID {status.uid}</span>
          )}
        </span>
      }
    >
      <div className="space-y-3 px-4 py-3">
        <p className="text-xs leading-5 text-text-secondary">
          {variant === 'settings'
            ? '一次登录即可在全软件共用：支持辅助功能地图标记（奇想星、灵感露珠与宝箱）、共鸣记录与大地图可挖抽数核算、祝福闪光图鉴核对及大世界探索纪念；登录凭据经 Windows 系统加密仅保存在本地。'
            : '登录正在游玩的国服账号后可使用地图标记：打开大地图自动校准并点击标点查看教程，返回场景后跟随小地图（支持奇迹大陆、万相境和 16:9 游戏窗口），同时用于共鸣记录与手账图鉴核对。'}
        </p>
        {state === 'logged_in' && progress && (
          <p className="text-xs tabular-nums text-text-secondary">已收集 {progress}</p>
        )}
        {errorText && (
          <p role="status" className="text-xs text-warning">
            {errorText}
          </p>
        )}
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            className={state === 'logged_in' ? secondaryButton : primaryButton}
            disabled={busy}
            onClick={() => void openPearpalLogin({ switchAccount: status.has_login })}
          >
            {state === 'logged_in' ? '切换账号' : state === 'expired' ? '重新登录' : '登录美鸭梨'}
          </button>
          {state === 'logged_in' && (
            <button
              type="button"
              className={secondaryButton}
              onClick={() => {
                useResonanceViewStore.getState().setView('map_resources');
                useAppStore.getState().setCurrentPage('resonance');
              }}
            >
              查看可挖抽数
            </button>
          )}
          {status.has_login && (
            <button
              type="button"
              className={secondaryButton}
              disabled={busy}
              onClick={() => void logoutPearpal()}
            >
              退出登录
            </button>
          )}
        </div>
      </div>
    </Card>
  );
}
