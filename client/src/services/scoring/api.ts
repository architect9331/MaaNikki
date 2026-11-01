import { invoke } from '@tauri-apps/api/core';
import { isTauri } from '@/utils/paths';
import { apiGet, apiPut } from '@/utils/backendApi';
import type { ScoringPayload, ScoringSettings } from './types';

export function loadScoring(): Promise<ScoringPayload> {
  return isTauri() ? invoke<ScoringPayload>('get_wardrobe_scoring') : apiGet<ScoringPayload>('/wardrobe-scoring');
}
export async function saveScoring(accountKey: string, settings: ScoringSettings) {
  if (isTauri()) await invoke('save_wardrobe_scoring', { accountKey, settings });
  else await apiPut('/wardrobe-scoring', { account_key: accountKey, settings });
}
