import { useEffect, useMemo, useRef, useState } from 'react';
import { useAccountStateStore } from '@/stores/accountStateStore';
import { useAppStore } from '@/stores/appStore';
import { useForecastAssumptionsStore } from '@/stores/forecastAssumptionsStore';
import { realmQueue } from '@/utils/nikkiOptions';
import type { OptionValue } from '@/types/interface';
import type { ForecastInput, ForecastResult } from './collectionForecast';
import {
  buildBlessingContext,
  computeCraftingForecast,
  computeRenewalForecast,
  computeWeeklyEstimate,
  simulateQueueSchedule,
} from './realmForecast';
import { useWardrobePlanStore } from './scoring/wardrobePlanStore';

const SUMMARY_URL = 'https://stats.maanikki.cn/v1/summary';
let cachedCommunity: { rate: number; samples: number } | null = null;
let communityRequested = false;
const communityListeners = new Set<(val: { rate: number; samples: number }) => void>();

function subscribeCommunity(cb: (val: { rate: number; samples: number }) => void) {
  if (cachedCommunity) {
    cb(cachedCommunity);
    return () => {};
  }
  communityListeners.add(cb);
  if (!communityRequested) {
    communityRequested = true;
    void fetch(SUMMARY_URL)
      .then((response) => (response.ok ? response.json() : null))
      .then((summary: { colors?: { color: string; count: number }[] } | null) => {
        const samples = summary?.colors?.reduce((sum, row) => sum + row.count, 0) ?? 0;
        const rainbow = summary?.colors?.find((row) => row.color === '彩')?.count ?? 0;
        if (samples) {
          cachedCommunity = { rate: rainbow / samples, samples };
          for (const listener of communityListeners) listener(cachedCommunity);
        }
      })
      .catch(() => {
        /* Offline fallback: preset simply stays hidden. */
      });
  }
  return () => {
    communityListeners.delete(cb);
  };
}

function useWorkerForecast(input: ForecastInput | null) {
  const [result, setResult] = useState<ForecastResult | null>(null);
  const [busy, setBusy] = useState(false);
  const reqIdRef = useRef(0);
  const key = input ? JSON.stringify(input) : '';

  useEffect(() => {
    if (!input) {
      setResult(null);
      setBusy(false);
      return;
    }
    const reqId = ++reqIdRef.current;
    setBusy(true);
    const worker = new Worker(new URL('./collectionForecast.worker.ts', import.meta.url), { type: 'module' });
    const timer = window.setTimeout(() => {
      worker.postMessage({ id: reqId, input });
    }, 140);
    worker.onmessage = (event: MessageEvent<{ id?: number; result?: ForecastResult }>) => {
      if (event.data.id === reqId && event.data.result) {
        setResult(event.data.result);
      }
      setBusy(false);
      worker.terminate();
    };
    worker.onerror = () => {
      setBusy(false);
      worker.terminate();
    };
    return () => {
      window.clearTimeout(timer);
      worker.terminate();
    };
    // The serialized key captures every simulation parameter.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  return { result, busy };
}

/**
 * Unified hook feeding RealmQueueCard and CollectionHub planners.
 * Ensures WardrobePlanStore stays fresh whenever AccountState updates on disk.
 */
export function useRealmForecast(options?: {
  optionValues?: Record<string, OptionValue>;
  extraBling?: number;
  extraParticles?: number;
  extraMaterialEnergy?: number;
  refreshToken?: number;
  skipGlowWorker?: boolean;
  /** Skip the stochastic glow worker unless glow is enabled and ranked ahead of this goal. */
  neededOnlyIfGlowBefore?: 'renewal' | 'crafting';
}) {
  const account = useAccountStateStore((state) => state.data);
  const activeOptionValues = useAppStore((state) => {
    if (options?.optionValues) return options.optionValues;
    const inst = state.instances.find((i) => i.id === state.activeInstanceId) ?? state.instances[0];
    return inst?.selectedTasks.find((t) => t.taskName === 'realm_challenge')?.optionValues;
  });
  const { payload, settings, materials, loading: planLoading, load } = useWardrobePlanStore();
  const { dailyEnergy, dailyBlingIncome, rainbowPreset } = useForecastAssumptionsStore();
  const [community, setCommunity] = useState<{ rate: number; samples: number } | null>(cachedCommunity);

  useEffect(() => subscribeCommunity(setCommunity), []);

  const wardrobeUpdatedAt = account?.wardrobe?.inventory?.updated_at ?? account?.wardrobe?.last_scan?.updated_at ?? '';
  const refreshToken = options?.refreshToken ?? 0;
  useEffect(() => {
    if (account?.uid) {
      void load(`${account.uid}:${account.updated_at ?? wardrobeUpdatedAt}:${refreshToken}`);
    }
  }, [account?.uid, account?.updated_at, wardrobeUpdatedAt, refreshToken, load]);

  const entries = useMemo(() => realmQueue(activeOptionValues), [activeOptionValues]);
  const weekly = useMemo(
    () => computeWeeklyEstimate(activeOptionValues, account, dailyEnergy),
    [activeOptionValues, account, dailyEnergy],
  );
  const crafting = useMemo(
    () =>
      computeCraftingForecast(
        payload,
        settings,
        materials,
        account,
        dailyEnergy,
        dailyBlingIncome,
        options?.extraMaterialEnergy ?? 0,
      ),
    [payload, settings, materials, account, dailyEnergy, dailyBlingIncome, options?.extraMaterialEnergy],
  );
  const renewal = useMemo(
    () =>
      computeRenewalForecast(
        payload,
        settings,
        materials,
        account,
        dailyEnergy,
        dailyBlingIncome,
        options?.extraMaterialEnergy ?? 0,
      ),
    [payload, settings, materials, account, dailyEnergy, dailyBlingIncome, options?.extraMaterialEnergy],
  );
  const blessing = useMemo(
    () => buildBlessingContext(account, materials, activeOptionValues, community),
    [account, materials, activeOptionValues, community],
  );

  const chosenPreset = useMemo(
    () =>
      blessing.presets.find((item) => item.id === rainbowPreset) ??
      blessing.presets.find((item) => item.id === 'catalog') ??
      blessing.presets[0],
    [blessing.presets, rainbowPreset],
  );
  const rainbowRate = chosenPreset?.rate ?? 0.05;

  const glowNeededForCaller = useMemo(() => {
    if (options?.skipGlowWorker) return false;
    if (!options?.neededOnlyIfGlowBefore) return true;
    const enabled = entries.filter((entry) => entry.enabled).map((entry) => entry.goal);
    const gIdx = enabled.indexOf('glow');
    const targetIdx = enabled.indexOf(options.neededOnlyIfGlowBefore);
    return gIdx >= 0 && targetIdx >= 0 && gIdx < targetIdx;
  }, [options?.skipGlowWorker, options?.neededOnlyIfGlowBefore, entries]);

  const glowStandaloneInput = useMemo<ForecastInput | null>(() => {
    if (!glowNeededForCaller || blessing.status !== 'ready' || !blessing.baseInput) return null;
    return {
      ...blessing.baseInput,
      rainbow: rainbowRate,
      runsPerDay: dailyEnergy / 40,
      blingPerDay: dailyBlingIncome,
      bling: blessing.baseInput.bling + (options?.extraBling ?? 0),
      particles: blessing.baseInput.particles + (options?.extraParticles ?? 0),
    };
  }, [
    glowNeededForCaller,
    options?.extraBling,
    options?.extraParticles,
    blessing.status,
    blessing.baseInput,
    rainbowRate,
    dailyEnergy,
    dailyBlingIncome,
  ]);

  const preGlowSchedule = useMemo(
    () =>
      simulateQueueSchedule({
        entries,
        weekly,
        renewal,
        crafting,
        blessing,
        glowStandalone: null,
        glowQueued: null,
        glowComputing: false,
        dailyEnergy,
      }).glowSchedule,
    [entries, weekly, renewal, crafting, blessing, dailyEnergy],
  );

  const hasPrecedingForGlow = Boolean(
    preGlowSchedule.priorEnergy || (preGlowSchedule.weeklyAhead && preGlowSchedule.weeklyAhead.perWeekEnergy > 0),
  );

  const glowQueuedInput = useMemo<ForecastInput | null>(() => {
    if (!glowStandaloneInput || !hasPrecedingForGlow) return null;
    return { ...glowStandaloneInput, schedule: preGlowSchedule };
  }, [glowStandaloneInput, hasPrecedingForGlow, preGlowSchedule]);

  const standaloneWorker = useWorkerForecast(glowStandaloneInput);
  const queuedWorker = useWorkerForecast(glowQueuedInput);

  const glowStandalone = standaloneWorker.result;
  const glowQueued = hasPrecedingForGlow ? queuedWorker.result : glowStandalone;
  const glowComputing = standaloneWorker.busy || (hasPrecedingForGlow && queuedWorker.busy);

  const queueSummary = useMemo(
    () =>
      simulateQueueSchedule({
        entries,
        weekly,
        renewal,
        crafting,
        blessing,
        glowStandalone,
        glowQueued,
        glowComputing,
        dailyEnergy,
      }),
    [entries, weekly, renewal, crafting, blessing, glowStandalone, glowQueued, glowComputing, dailyEnergy],
  );

  return {
    account,
    payload,
    settings,
    materials,
    planLoading,
    entries,
    weekly,
    crafting,
    renewal,
    blessing,
    chosenPreset,
    glowStandalone,
    glowQueued,
    glowComputing,
    queueSummary,
    dailyEnergy,
    dailyBlingIncome,
    rainbowPreset,
  };
}
