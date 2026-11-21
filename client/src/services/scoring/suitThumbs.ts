import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { apiGet, apiPost, getApiBase } from '@/utils/backendApi';
import { isTauri } from '@/utils/paths';

export interface SuitThumbsStatus {
  state: string;
  available: boolean;
  count: number;
  version: string | null;
  error: string | null;
}

const CHUNK_SIZE = 48;
const thumbCache = new Map<string, string>();
let cachedVersion: string | null = null;

function ensureCacheVersion(version: string | null | undefined) {
  const normalized = version ?? null;
  if (normalized && cachedVersion && normalized !== cachedVersion) {
    thumbCache.clear();
  }
  if (normalized) cachedVersion = normalized;
}

export function useSuitThumbsStatus() {
  const [status, setStatus] = useState<SuitThumbsStatus | null>(null);
  const [retrying, setRetrying] = useState(false);
  const autoSyncedRef = useRef(false);

  const runSync = useCallback(async () => {
    setRetrying(true);
    try {
      const next = isTauri()
        ? await invoke<SuitThumbsStatus>('sync_suit_thumbs')
        : await apiPost<SuitThumbsStatus>('/suit-thumbs/sync');
      ensureCacheVersion(next.version);
      setStatus(next);
    } catch {
      // Errors are surfaced via status or ignored when remote bundle is absent.
    } finally {
      setRetrying(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    let unlisten: (() => void) | null = null;

    const load = async () => {
      try {
        const current = isTauri()
          ? await invoke<SuitThumbsStatus>('get_suit_thumbs_status')
          : await apiGet<SuitThumbsStatus>('/suit-thumbs/status');
        if (cancelled) return;
        ensureCacheVersion(current.version);
        setStatus(current);
        if (!current.available && current.state === 'idle' && !autoSyncedRef.current) {
          autoSyncedRef.current = true;
          void runSync();
        }
      } catch {
        // Leave unavailable state graceful.
      }
    };

    void load();
    if (isTauri()) {
      void listen<SuitThumbsStatus>('suit-thumbs-status', (event) => {
        if (cancelled) return;
        ensureCacheVersion(event.payload.version);
        setStatus(event.payload);
      }).then((fn) => {
        if (cancelled) fn();
        else unlisten = fn;
      }).catch(() => {});
    }

    return () => {
      cancelled = true;
      unlisten?.();
    };
  }, [runSync]);

  return { status, retrying, retrySync: runSync };
}

export function useSuitThumbMap(visibleKeys: string[], status: SuitThumbsStatus | null): Record<string, string> {
  const [revision, setRevision] = useState(0);
  const available = Boolean(status?.available);
  const version = status?.version ?? '1';
  const keysSignature = visibleKeys.join(',');

  useEffect(() => {
    ensureCacheVersion(status?.version);
    if (!available || !isTauri() || !keysSignature) return;
    const requested = keysSignature.split(',').filter(Boolean);
    const missing = requested.filter((key) => !thumbCache.has(key));
    if (!missing.length) return;

    let cancelled = false;
    const loadMissing = async () => {
      for (let i = 0; i < missing.length; i += CHUNK_SIZE) {
        if (cancelled) return;
        const batch = missing.slice(i, i + CHUNK_SIZE);
        try {
          const result = await invoke<Record<string, string>>('get_suit_thumbs_batch', { keys: batch });
          if (cancelled) return;
          let added = false;
          for (const [key, dataUrl] of Object.entries(result)) {
            if (dataUrl && thumbCache.get(key) !== dataUrl) {
              thumbCache.set(key, dataUrl);
              added = true;
            }
          }
          if (added) setRevision((prev) => prev + 1);
        } catch {
          break;
        }
      }
    };
    void loadMissing();

    return () => {
      cancelled = true;
    };
  }, [available, keysSignature, status?.version]);

  return useMemo(() => {
    if (!available || !keysSignature) return {};
    const keys = keysSignature.split(',').filter(Boolean);
    const map: Record<string, string> = {};
    if (!isTauri()) {
      const base = getApiBase();
      const ver = encodeURIComponent(version);
      for (const key of keys) {
        map[key] = `${base}/suit-thumbs/${encodeURIComponent(key)}?v=${ver}`;
      }
      return map;
    }
    for (const key of keys) {
      const url = thumbCache.get(key);
      if (url) map[key] = url;
    }
    return map;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [available, keysSignature, version, revision]);
}
