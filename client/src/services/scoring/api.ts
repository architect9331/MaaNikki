import { invoke } from '@tauri-apps/api/core';
import { isTauri } from '@/utils/paths';
import { apiGet, apiPut } from '@/utils/backendApi';
import type { MaterialsState, ScoringPayload, ScoringSettings } from './types';
import { applyHotItemSources } from './wardrobeBrowserData';

export async function loadScoring(): Promise<ScoringPayload> {
  const payload = isTauri()
    ? await invoke<ScoringPayload>('get_wardrobe_scoring')
    : await apiGet<ScoringPayload>('/wardrobe-scoring');
  applyHotItemSources(payload.item_sources);
  return payload;
}
export async function saveScoring(accountKey: string, settings: ScoringSettings) {
  if (isTauri()) await invoke('save_wardrobe_scoring', { accountKey, settings });
  else await apiPut('/wardrobe-scoring', { account_key: accountKey, settings });
}
export async function saveMaterialPlan(accountKey: string, materials: MaterialsState) {
  if (isTauri()) await invoke('save_material_plan', { accountKey, materials });
}
