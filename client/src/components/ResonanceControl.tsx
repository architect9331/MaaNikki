import { BookOpen, Loader2 } from 'lucide-react';
import clsx from 'clsx';
import { useAppStore } from '@/stores/appStore';
import { resonanceLabel, setResonanceEnabled, useResonanceStore } from '@/services/resonanceService';
import { isTauri } from '@/utils/paths';
import { Tooltip } from './ui/Tooltip';
import { SwitchTrack } from './ui/SwitchTrack';

export function ResonanceControl({ showSettings = true }: { showSettings?: boolean }) {
  const { status, ready, changing, connectionError } = useResonanceStore();
  const label = resonanceLabel(status);
  const error = connectionError || status.error;
  return <div role="group" aria-label="共鸣记录"
    className={clsx('flex shrink-0 items-center rounded-md transition-colors hover:bg-bg-hover',
      error ? 'text-warning' : status.enabled ? 'text-accent' : 'text-text-secondary')}>
    {showSettings ? <Tooltip side="bottom" content={<div className="space-y-1">
      <p className="font-medium">共鸣记录 · {label}</p>
      <p className="text-text-secondary">{error || '查看历史记录、同步状态与导出。'}</p>
    </div>}>
      <button type="button" onClick={() => useAppStore.getState().setCurrentPage('resonance')}
        className="relative flex items-center gap-2 rounded-md p-2 text-sm transition-colors hover:bg-bg-hover focus-visible:outline-2 focus-visible:outline-accent"
        aria-label="共鸣记录与设置">
        {status.busy ? <Loader2 className="h-5 w-5 animate-spin" aria-hidden="true" /> : <BookOpen className="h-5 w-5" aria-hidden="true" />}
        <span className="whitespace-nowrap">共鸣记录</span>
        {error && <span aria-hidden="true" className="absolute right-1 top-1 h-1.5 w-1.5 rounded-full bg-warning ring-2 ring-bg-secondary" />}
      </button>
    </Tooltip> : <span className="pl-2 text-sm">{status.enabled ? '已开启' : '已关闭'}</span>}
    <Tooltip side="bottom" content={isTauri() ? `${status.enabled ? '关闭' : '开启'}共鸣记录自动同步` : '请在桌面客户端同步共鸣记录。'}>
      <button type="button" role="switch" aria-checked={status.enabled} aria-label="自动同步共鸣记录"
        disabled={!isTauri() || !ready || changing} onClick={() => void setResonanceEnabled(!status.enabled)}
        className="flex items-center rounded-md px-1.5 py-2 transition-colors hover:bg-bg-hover focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-50">
        <SwitchTrack checked={status.enabled} compact />
      </button>
    </Tooltip>
    <span className="sr-only" role="status">共鸣记录{label}</span>
  </div>;
}
