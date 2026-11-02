import { resonanceLabel, setResonanceEnabled, useResonanceStore } from '@/services/resonanceService';
import { isTauri } from '@/utils/paths';
import { Tooltip } from './ui/Tooltip';
import { SwitchTrack } from './ui/SwitchTrack';

/** The automatic sync switch on the resonance page. */
export function ResonanceControl() {
  const { status, ready, changing } = useResonanceStore();
  return <div role="group" aria-label="共鸣记录" className="flex shrink-0 items-center text-text-secondary">
    <span className="pl-2 text-sm">{status.enabled ? '已开启' : '已关闭'}</span>
    <Tooltip side="bottom" content={isTauri() ? `${status.enabled ? '关闭' : '开启'}共鸣记录自动同步` : '请在桌面客户端同步共鸣记录。'}>
      <button type="button" role="switch" aria-checked={status.enabled} aria-label="自动同步共鸣记录"
        disabled={!isTauri() || !ready || changing} onClick={() => void setResonanceEnabled(!status.enabled)}
        className="flex items-center rounded-lg px-1.5 py-2 transition-colors hover:bg-bg-hover focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-50">
        <SwitchTrack checked={status.enabled} compact />
      </button>
    </Tooltip>
    <span className="sr-only" role="status">共鸣记录{resonanceLabel(status)}</span>
  </div>;
}
