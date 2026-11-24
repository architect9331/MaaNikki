import { create } from 'zustand';
import { gameDay, useAccountStateStore } from './accountStateStore';

const MUTE_KEY = 'maanikki:giant-meteor-effect-muted-day';

function readMutedDay(): string | null {
  try {
    return window.localStorage.getItem(MUTE_KEY);
  } catch {
    return null;
  }
}

interface GiantMeteorUiStore {
  dialogOpen: boolean;
  previewActive: boolean;
  mutedDay: string | null;
  burstSeq: number;
  openDialog: () => void;
  closeDialog: () => void;
  setPreviewActive: (active: boolean) => void;
  toggleMuteForDay: (day: string) => void;
  triggerBurst: () => void;
}

export const useGiantMeteorStore = create<GiantMeteorUiStore>((set) => ({
  dialogOpen: false,
  previewActive: false,
  mutedDay: readMutedDay(),
  burstSeq: 0,
  openDialog: () => set({ dialogOpen: true }),
  closeDialog: () => set({ dialogOpen: false }),
  setPreviewActive: (active) =>
    set((state) => ({
      previewActive: active,
      burstSeq: active ? state.burstSeq + 1 : state.burstSeq,
    })),
  toggleMuteForDay: (day) =>
    set((state) => {
      const next = state.mutedDay === day ? null : day;
      try {
        if (next) window.localStorage.setItem(MUTE_KEY, next);
        else window.localStorage.removeItem(MUTE_KEY);
      } catch {
        // Ignore storage write errors.
      }
      return {
        mutedDay: next,
        burstSeq: next === null ? state.burstSeq + 1 : state.burstSeq,
      };
    }),
  triggerBurst: () => set((state) => ({ burstSeq: state.burstSeq + 1 })),
}));

export function useGiantMeteorState() {
  const { data, error, loading, refresh } = useAccountStateStore();
  const dialogOpen = useGiantMeteorStore((s) => s.dialogOpen);
  const previewActive = useGiantMeteorStore((s) => s.previewActive);
  const mutedDay = useGiantMeteorStore((s) => s.mutedDay);
  const burstSeq = useGiantMeteorStore((s) => s.burstSeq);
  const openDialog = useGiantMeteorStore((s) => s.openDialog);
  const closeDialog = useGiantMeteorStore((s) => s.closeDialog);
  const setPreviewActive = useGiantMeteorStore((s) => s.setPreviewActive);
  const toggleMuteForDay = useGiantMeteorStore((s) => s.toggleMuteForDay);
  const triggerBurst = useGiantMeteorStore((s) => s.triggerBurst);

  const today = gameDay().toISOString().slice(0, 10);
  const observation = data?.observations?.giant_meteor;
  const record =
    observation?.game_day === today && Number.isFinite(Date.parse(observation.checked_at))
      ? observation
      : null;
  const found = !error && record?.outcome === 'found';
  const absent = !error && record?.outcome === 'not_found';
  const checkedAt = record
    ? new Date(record.checked_at).toLocaleString('zh-CN', {
        timeZone: 'Asia/Shanghai',
        hour12: false,
      })
    : null;
  const mutedToday = mutedDay === today;
  const effectActive = (found && !mutedToday) || previewActive;

  return {
    data,
    error,
    loading,
    refresh,
    today,
    record,
    found,
    absent,
    checkedAt,
    dialogOpen,
    previewActive,
    mutedToday,
    effectActive,
    burstSeq,
    openDialog,
    closeDialog,
    setPreviewActive,
    toggleMuteForDay,
    triggerBurst,
  };
}
