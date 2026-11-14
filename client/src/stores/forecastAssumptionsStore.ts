import { create } from 'zustand';

export const DEFAULT_DAILY_ENERGY = 280;
export const DEFAULT_DAILY_BLING_INCOME = 76000;
export const DEFAULT_RAINBOW_PRESET = 'catalog';
const STORAGE_KEY = 'maanikki.collectionForecast.v1';

export interface ForecastAssumptionsState {
  dailyEnergy: number;
  dailyBlingIncome: number;
  rainbowPreset: string;
  useQueueOrder: boolean;
  setDailyEnergy: (energy: number) => void;
  setDailyBlingIncome: (income: number) => void;
  setRainbowPreset: (preset: string) => void;
  setUseQueueOrder: (useQueueOrder: boolean) => void;
}

interface StoredAssumptions {
  preset?: unknown;
  energy?: unknown;
  income?: unknown;
  useQueueOrder?: unknown;
}

function loadStoredAssumptions(): Pick<
  ForecastAssumptionsState,
  'dailyEnergy' | 'dailyBlingIncome' | 'rainbowPreset' | 'useQueueOrder'
> {
  try {
    const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{}') as StoredAssumptions;
    const rawEnergy = Number(parsed.energy);
    const rawIncome = Number(parsed.income);
    return {
      dailyEnergy: Number.isFinite(rawEnergy) && rawEnergy >= 10 ? Math.round(rawEnergy) : DEFAULT_DAILY_ENERGY,
      dailyBlingIncome: Number.isFinite(rawIncome) && rawIncome >= 0 ? Math.round(rawIncome) : DEFAULT_DAILY_BLING_INCOME,
      rainbowPreset: typeof parsed.preset === 'string' && parsed.preset ? parsed.preset : DEFAULT_RAINBOW_PRESET,
      useQueueOrder: typeof parsed.useQueueOrder === 'boolean' ? parsed.useQueueOrder : true,
    };
  } catch {
    return {
      dailyEnergy: DEFAULT_DAILY_ENERGY,
      dailyBlingIncome: DEFAULT_DAILY_BLING_INCOME,
      rainbowPreset: DEFAULT_RAINBOW_PRESET,
      useQueueOrder: true,
    };
  }
}

function persistAssumptions(next: {
  rainbowPreset: string;
  dailyEnergy: number;
  dailyBlingIncome: number;
  useQueueOrder: boolean;
}) {
  try {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        preset: next.rainbowPreset,
        energy: next.dailyEnergy,
        income: next.dailyBlingIncome,
        useQueueOrder: next.useQueueOrder,
      }),
    );
  } catch {
    /* Convenience persistence only. */
  }
}

const initial = loadStoredAssumptions();

/** Shared daily energy, bling income, and probability assumptions across all collection & realm views. */
export const useForecastAssumptionsStore = create<ForecastAssumptionsState>((set, get) => ({
  ...initial,
  setDailyEnergy: (energy) => {
    const dailyEnergy = Math.max(10, Math.round(Number(energy) || DEFAULT_DAILY_ENERGY));
    set({ dailyEnergy });
    const state = get();
    persistAssumptions({ ...state, dailyEnergy });
  },
  setDailyBlingIncome: (income) => {
    const dailyBlingIncome = Math.max(0, Math.round(Number(income) || 0));
    set({ dailyBlingIncome });
    const state = get();
    persistAssumptions({ ...state, dailyBlingIncome });
  },
  setRainbowPreset: (rainbowPreset) => {
    set({ rainbowPreset });
    const state = get();
    persistAssumptions({ ...state, rainbowPreset });
  },
  setUseQueueOrder: (useQueueOrder) => {
    set({ useQueueOrder });
    const state = get();
    persistAssumptions({ ...state, useQueueOrder });
  },
}));
