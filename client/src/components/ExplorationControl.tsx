import { Compass, Loader2 } from 'lucide-react';
import clsx from 'clsx';
import { useAppStore } from '@/stores/appStore';
import { explorationStatusLabels, setExplorationEnabled, useExplorationStore } from '@/services/explorationService';
import { isTauri } from '@/utils/paths';
import { Tooltip } from './ui/Tooltip';

export function ExplorationControl({ showSettings = true }: { showSettings?: boolean }) {
  const { enabled, status, error } = useExplorationStore();
  const busy = status === 'starting' || status === 'stopping';
  const label = explorationStatusLabels[status];
  return <div role="group" aria-label="辅助功能"
    className={clsx('flex shrink-0 items-center rounded-md transition-colors hover:bg-bg-hover',
      status === 'error' || status === 'paused' ? 'text-warning' : enabled ? 'text-accent' : 'text-text-secondary')}>
    {showSettings ? <Tooltip side="bottom" content={<div className="space-y-1">
      <p className="font-medium">辅助功能 · {label}</p>
      <p className="text-text-secondary">{error || '点击图标打开设置，点击开关启停辅助。'}</p>
    </div>}>
      <button type="button" onClick={() => useAppStore.getState().setCurrentPage('exploration')}
        className="relative flex items-center gap-2 rounded-md p-2 text-sm transition-colors hover:bg-bg-hover focus-visible:outline-2 focus-visible:outline-accent"
        aria-label="辅助功能设置">
        {busy ? <Loader2 className="h-5 w-5 animate-spin" aria-hidden="true" /> : <Compass className="h-5 w-5" aria-hidden="true" />}
        <span className="whitespace-nowrap">辅助功能</span>
        {(status === 'paused' || status === 'error') && <span aria-hidden="true" className="absolute right-1 top-1 h-1.5 w-1.5 rounded-full bg-warning ring-2 ring-bg-secondary" />}
      </button>
    </Tooltip> : <span className="pl-2 text-sm">{enabled ? '已开启' : '已关闭'}</span>}
    <Tooltip side="bottom" content={isTauri() ? `${enabled ? '关闭' : '开启'}辅助功能 · ${label}` : '请在桌面客户端操作开关。'}>
      <button type="button" role="switch" aria-checked={enabled} aria-label="启用辅助功能"
        disabled={!isTauri()} onClick={() => setExplorationEnabled(!enabled)}
        className="flex items-center rounded-md px-1.5 py-2 transition-colors hover:bg-bg-hover focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-50">
        <span aria-hidden="true" className={clsx('relative h-4 w-7 shrink-0 rounded-full transition-colors', enabled ? 'bg-accent' : 'bg-text-muted/35')}>
          <span className={clsx('absolute top-0.5 h-3 w-3 rounded-full bg-white transition-transform', enabled ? 'translate-x-3.5' : 'translate-x-0.5')} />
        </span>
      </button>
    </Tooltip>
    <span className="sr-only" role="status">辅助功能{label}{error && `：${error}`}</span>
  </div>;
}
