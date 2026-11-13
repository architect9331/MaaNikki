import { create } from 'zustand';
import { toast } from 'sonner';
import { loadScoring, saveMaterialPlan, saveScoring } from './api';
import { emptySettings, type MaterialsState, type ScoringPayload, type ScoringSettings } from './types';

/**
 * One shared copy of the wardrobe record, clothing levels and material stock for
 * the closet, crafting and renewal views. Keeping a single copy means a level or
 * stock edit in one view is not overwritten by a stale save from another view.
 */
interface WardrobePlanState {
  token: string;
  payload: ScoringPayload | null;
  settings: ScoringSettings;
  materials: MaterialsState;
  loading: boolean;
  error: string;
  load: (token: string) => Promise<void>;
  setLevel: (groupId: string, level: number | null) => void;
  setLevels: (changes: Record<string, number | null>) => void;
  updateMaterials: (update: (previous: MaterialsState) => MaterialsState) => void;
  /** Adopt materials that another component has already saved. */
  adoptMaterials: (next: MaterialsState) => void;
}

let inflight: { token: string; promise: Promise<void> } | null = null;

export const useWardrobePlanStore = create<WardrobePlanState>((set, get) => ({
  token: '',
  payload: null,
  settings: emptySettings(),
  materials: {},
  loading: false,
  error: '',
  load: (token) => {
    if (get().token === token && get().payload) return Promise.resolve();
    if (inflight?.token === token) return inflight.promise;
    set({ loading: true });
    const promise = loadScoring().then((payload) => {
      if (inflight?.token !== token) return;
      set({
        token,
        payload,
        settings: payload.settings ?? emptySettings(),
        materials: payload.materials ?? {},
        loading: false,
        error: '',
      });
    }).catch((error) => {
      if (inflight?.token !== token) return;
      set({ token, payload: null, loading: false, error: String(error instanceof Error ? error.message : error) });
    }).finally(() => {
      if (inflight?.token === token) inflight = null;
    });
    inflight = { token, promise };
    return promise;
  },
  setLevel: (groupId, level) => get().setLevels({ [groupId]: level }),
  setLevels: (changes) => {
    const { payload, settings } = get();
    if (!payload) return;
    const levels = { ...settings.levels };
    for (const [groupId, level] of Object.entries(changes)) {
      if (level === null) delete levels[groupId];
      else levels[groupId] = level;
    }
    const next = { ...settings, levels };
    set({ settings: next });
    void saveScoring(payload.account_key, next).catch((error) => toast.error(String(error)));
  },
  updateMaterials: (update) => {
    const { payload, materials } = get();
    const next = update(materials);
    set({ materials: next });
    if (payload) void saveMaterialPlan(payload.account_key, next).catch((error) => toast.error(String(error)));
  },
  adoptMaterials: (next) => set({ materials: next }),
}));
