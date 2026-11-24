import { create } from 'zustand';
import { invoke } from '@tauri-apps/api/core';
import { isTauri } from '@/utils/paths';
import {
  DEFAULT_MATCH_RULE,
  inferPreset,
  ruleFromPreset,
  type DecodedShareCode,
  type MatchPreset,
  type MatchRule,
  type OutfitCollectionFile,
  type OutfitEntry,
  type OutfitItemDictionary,
} from './types';

const RULE_STORAGE_KEY = 'maanikki_outfit_match_rule_v1';
const WEB_COLLECTION_KEY = 'maanikki_outfit_collection_v1';

function readStoredRule(): MatchRule {
  try {
    const raw = localStorage.getItem(RULE_STORAGE_KEY);
    if (!raw) return DEFAULT_MATCH_RULE;
    const parsed = JSON.parse(raw) as Partial<MatchRule>;
    const ignoreMakeup = typeof parsed.ignoreMakeup === 'boolean' ? parsed.ignoreMakeup : true;
    const ignoreAccessories = typeof parsed.ignoreAccessories === 'boolean' ? parsed.ignoreAccessories : false;
    const ignoreShoesAndSocks = typeof parsed.ignoreShoesAndSocks === 'boolean' ? parsed.ignoreShoesAndSocks : false;
    const requireExactVariant = typeof parsed.requireExactVariant === 'boolean' ? parsed.requireExactVariant : false;
    return {
      preset: inferPreset({ ignoreMakeup, ignoreAccessories, ignoreShoesAndSocks, requireExactVariant }),
      ignoreMakeup,
      ignoreAccessories,
      ignoreShoesAndSocks,
      requireExactVariant,
    };
  } catch {
    return DEFAULT_MATCH_RULE;
  }
}

function writeStoredRule(rule: MatchRule): void {
  try {
    localStorage.setItem(RULE_STORAGE_KEY, JSON.stringify(rule));
  } catch {
    // Ignore storage write errors.
  }
}

async function persistItems(items: OutfitEntry[]): Promise<void> {
  const payload: OutfitCollectionFile = { schema_version: 1, items };
  if (isTauri()) {
    await invoke('save_outfit_collection', { collection: payload });
  } else {
    localStorage.setItem(WEB_COLLECTION_KEY, JSON.stringify(payload));
  }
}

export async function decodeShareCode(code: string): Promise<DecodedShareCode> {
  if (!isTauri()) {
    throw new Error('搭配码联网解析仅在桌面客户端中可用。');
  }
  return invoke<DecodedShareCode>('decode_outfit_share_code', { code });
}

/** Resize an uploaded/pasted/game-album cover image into a crisp compact JPEG data URL (<= 540px). */
export async function compressCoverDataUrl(sourceDataUrl: string, maxSide = 540): Promise<string> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => {
      const scale = Math.min(1, maxSide / Math.max(img.width, img.height, 1));
      const w = Math.max(1, Math.round(img.width * scale));
      const h = Math.max(1, Math.round(img.height * scale));
      const canvas = document.createElement('canvas');
      canvas.width = w;
      canvas.height = h;
      const ctx = canvas.getContext('2d');
      if (!ctx) {
        resolve(sourceDataUrl);
        return;
      }
      ctx.drawImage(img, 0, 0, w, h);
      resolve(canvas.toDataURL('image/jpeg', 0.84));
    };
    img.onerror = () => reject(new Error('无法读取所选封面图片。'));
    img.src = sourceDataUrl;
  });
}

export async function fileToCompressedCover(file: File): Promise<string> {
  const dataUrl = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ''));
    reader.onerror = () => reject(new Error('读取本地图片失败。'));
    reader.readAsDataURL(file);
  });
  return compressCoverDataUrl(dataUrl);
}

interface OutfitCollectionState {
  loaded: boolean;
  loading: boolean;
  error: string;
  items: OutfitEntry[];
  dict: OutfitItemDictionary | null;
  rule: MatchRule;
  load: () => Promise<void>;
  setPreset: (preset: MatchPreset) => void;
  patchRule: (patch: Partial<Omit<MatchRule, 'preset'>>) => void;
  addOutfit: (entry: OutfitEntry) => Promise<void>;
  addOutfits: (entries: OutfitEntry[]) => Promise<void>;
  updateOutfit: (id: string, patch: Partial<Pick<OutfitEntry, 'title' | 'note' | 'tags' | 'author' | 'coverDataUrl' | 'decoded'>>) => Promise<void>;
  deleteOutfit: (id: string) => Promise<void>;
  mergeImportedItems: (entries: OutfitEntry[]) => Promise<number>;
}

let loadInflight: Promise<void> | null = null;

export const useOutfitCollectionStore = create<OutfitCollectionState>((set, get) => ({
  loaded: false,
  loading: false,
  error: '',
  items: [],
  dict: null,
  rule: readStoredRule(),

  load: async () => {
    if (get().loaded && get().dict) return;
    if (loadInflight) return loadInflight;
    set({ loading: true, error: '' });
    loadInflight = (async () => {
      try {
        let items: OutfitEntry[] = [];
        let dict: OutfitItemDictionary | null = get().dict;
        if (isTauri()) {
          const [col, loadedDict] = await Promise.all([
            invoke<OutfitCollectionFile>('get_outfit_collection'),
            dict ? Promise.resolve(dict) : invoke<OutfitItemDictionary>('get_outfit_item_dictionary').catch(() => null),
          ]);
          items = Array.isArray(col?.items) ? col.items : [];
          dict = loadedDict;
        } else {
          const raw = localStorage.getItem(WEB_COLLECTION_KEY);
          if (raw) {
            const parsed = JSON.parse(raw) as Partial<OutfitCollectionFile>;
            if (Array.isArray(parsed.items)) items = parsed.items;
          }
        }
        set({ items, dict, loaded: true, loading: false, error: '' });
      } catch (err) {
        set({
          loading: false,
          loaded: true,
          error: err instanceof Error ? err.message : String(err),
        });
      } finally {
        loadInflight = null;
      }
    })();
    return loadInflight;
  },

  setPreset: (preset) => {
    const next = ruleFromPreset(preset, get().rule);
    writeStoredRule(next);
    set({ rule: next });
  },

  patchRule: (patch) => {
    const merged = { ...get().rule, ...patch };
    const next: MatchRule = {
      ...merged,
      preset: inferPreset(merged),
    };
    writeStoredRule(next);
    set({ rule: next });
  },

  addOutfit: async (entry) => {
    const prev = get().items;
    const next = [entry, ...prev.filter((item) => item.code !== entry.code)];
    await persistItems(next);
    set({ items: next });
  },

  addOutfits: async (entries) => {
    if (entries.length === 0) return;
    const existingCodes = new Set(entries.map((e) => e.code));
    const next = [...entries, ...get().items.filter((item) => !existingCodes.has(item.code))];
    await persistItems(next);
    set({ items: next });
  },

  updateOutfit: async (id, patch) => {
    const now = new Date().toISOString();
    const next = get().items.map((item) =>
      item.id === id ? { ...item, ...patch, updatedAt: now } : item,
    );
    await persistItems(next);
    set({ items: next });
  },

  deleteOutfit: async (id) => {
    const next = get().items.filter((item) => item.id !== id);
    await persistItems(next);
    set({ items: next });
  },

  mergeImportedItems: async (entries) => {
    const prev = get().items;
    const byCode = new Map(prev.map((item) => [item.code, item]));
    let added = 0;
    const additions: OutfitEntry[] = [];
    for (const item of entries) {
      if (!item || typeof item.code !== 'string' || !item.decoded || !Array.isArray(item.decoded.wearing_clothes)) {
        continue;
      }
      if (!byCode.has(item.code)) {
        additions.push(item);
        byCode.set(item.code, item);
        added += 1;
      }
    }
    if (additions.length === 0) return 0;
    const next = [...additions, ...prev];
    await persistItems(next);
    set({ items: next });
    return added;
  },
}));
