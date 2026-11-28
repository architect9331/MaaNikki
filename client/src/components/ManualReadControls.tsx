import { Loader2, Play, Square } from 'lucide-react';
import {
  READ_SPECS,
  startBackpackRead,
  startBlessDecompose,
  startBlessOrganize,
  startBlessUpgrade,
  startInfiniteHeartRead,
  startRenewalIncrementalRead,
  startRenewalRead,
  startSketchRead,
  stopWardrobeRead,
  useWardrobeStore,
  type WardrobeReadMode,
  WARDROBE_INSTANCE_ID,
} from '@/services/wardrobeService';
import { isTauri } from '@/utils/paths';
import { LogsPanel } from './LogsPanel';
import { primaryButton, secondaryButton } from './ui/FeaturePage';

const START_BY_MODE: Record<Exclude<WardrobeReadMode, 'wardrobe'>, () => Promise<void>> = {
  renewal: startRenewalRead,
  renewal_inc: startRenewalIncrementalRead,
  backpack: startBackpackRead,
  sketch: startSketchRead,
  infinite_heart: startInfiniteHeartRead,
  bless_organize: startBlessOrganize,
  bless_upgrade: startBlessUpgrade,
  bless_decompose: startBlessDecompose,
};

export function ManualReadButton({
  targetMode,
  idleLabel,
  primary = false,
  stopPropagation = false,
  compact = false,
}: {
  targetMode: Exclude<WardrobeReadMode, 'wardrobe'>;
  idleLabel: string;
  primary?: boolean;
  stopPropagation?: boolean;
  compact?: boolean;
}) {
  const { status, mode } = useWardrobeStore();
  const busy = ['starting', 'reading', 'stopping'].includes(status);
  const busyHere = busy && mode === targetMode;

  return (
    <button
      type="button"
      disabled={!isTauri() || (busy && !busyHere) || status === 'stopping'}
      onClick={(event) => {
        if (stopPropagation) {
          event.preventDefault();
          event.stopPropagation();
        }
        if (busyHere) stopWardrobeRead();
        else void START_BY_MODE[targetMode]();
      }}
      className={
        compact
          ? 'inline-flex items-center gap-1 rounded-lg border border-border px-2 py-1 text-xs hover:bg-bg-hover disabled:opacity-40'
          : busyHere
            ? secondaryButton
            : primary
              ? primaryButton
              : secondaryButton
      }
    >
      {busyHere ? <Square className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
      {busyHere ? (targetMode.startsWith('bless_') ? '停止操作' : '停止读取') : idleLabel}
    </button>
  );
}

export function ManualReadLogStrip({ modes }: { modes: WardrobeReadMode[] }) {
  const { status, mode } = useWardrobeStore();
  const busy = ['starting', 'reading', 'stopping'].includes(status);
  if (!busy || !modes.includes(mode)) return null;
  const label = READ_SPECS[mode]?.busyText ?? '正在处理';
  return (
    <section className="overflow-hidden rounded-xl border border-accent/40 bg-bg-secondary">
      <div className="flex items-center gap-2 border-b border-border px-4 py-2 text-xs text-accent">
        <Loader2 className="h-3.5 w-3.5 animate-spin shrink-0" />
        <span>{status === 'starting' ? '正在连接游戏窗口…' : status === 'stopping' ? '正在停止读取…' : label}</span>
      </div>
      <div className="h-40"><LogsPanel instanceId={WARDROBE_INSTANCE_ID} /></div>
    </section>
  );
}
