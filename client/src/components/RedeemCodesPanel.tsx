import { useCallback, useEffect, useMemo, useState } from 'react';
import { Check, Copy, Gift, RefreshCw } from 'lucide-react';
import { invoke } from '@tauri-apps/api/core';
import { toast } from 'sonner';
import clsx from 'clsx';
import { isTauri } from '@/utils/paths';
import { useAccountStateStore } from '@/stores/accountStateStore';
import type { OptionValue } from '@/types/interface';

interface SyncedRedeemCode {
  code: string;
  reward: string;
  expires_at?: string | null;
  source: string;
}

interface SyncedRedeemCodesResponse {
  codes: SyncedRedeemCode[];
  synced_at: string;
  online: boolean;
  warnings: string[];
}

const cacheByMode = new Map<boolean, SyncedRedeemCodesResponse>();

function parseManualExtra(raw: string): string[] {
  const tokens = raw.trim().split(/[,，;；\s]+/);
  const result: string[] = [];
  for (const tok of tokens) {
    const cleaned = tok.trim().replace(/^[「」『』【】"'`<>()]+|[「」『』【】"'`<>()]+$/g, '');
    if (
      cleaned.length >= 3 &&
      cleaned.length <= 32 &&
      /^[A-Za-z0-9\u4e00-\u9fff.]+$/.test(cleaned) &&
      !/^[\d.]+$/.test(cleaned)
    ) {
      result.push(cleaned);
    }
  }
  return result;
}

export function RedeemCodesPanel({ values }: { values: Record<string, OptionValue> }) {
  const onlineVal = values.RedeemCodesOnline;
  const online = onlineVal?.type === 'switch' ? onlineVal.value : true;
  const extraVal = values.RedeemCodesExtra;
  const extraRaw = extraVal?.type === 'input' ? String(extraVal.values.Text ?? '') : '';

  const account = useAccountStateStore((s) => s.data);
  const [data, setData] = useState<SyncedRedeemCodesResponse | null>(() => cacheByMode.get(online) ?? null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copiedCode, setCopiedCode] = useState<string | null>(null);

  const loadCodes = useCallback(
    async (force = false) => {
      if (!isTauri()) return;
      if (!force && cacheByMode.has(online)) {
        setData(cacheByMode.get(online)!);
        setError(null);
        return;
      }
      setLoading(true);
      setError(null);
      try {
        const res = await invoke<SyncedRedeemCodesResponse>('get_synced_redeem_codes', { online });
        cacheByMode.set(online, res);
        setData(res);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setLoading(false);
      }
    },
    [online],
  );

  useEffect(() => {
    void loadCodes(false);
  }, [loadCodes]);

  const redeemedMap = useMemo(() => {
    const map = new Map<string, string>();
    const items = account?.tasks?.redeem_codes?.items ?? {};
    for (const [code, receipt] of Object.entries(items)) {
      map.set(code.toLowerCase(), receipt.outcome);
    }
    return map;
  }, [account?.tasks?.redeem_codes?.items]);

  const mergedRows = useMemo(() => {
    const rows: (SyncedRedeemCode & { status: 'pending' | 'claimed' | 'expired'; isManual?: boolean })[] = [];
    const seen = new Set<string>();
    for (const item of data?.codes ?? []) {
      const key = item.code.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      const outcome = redeemedMap.get(key);
      const status =
        outcome === 'redeemed' || outcome === 'already_used'
          ? 'claimed'
          : outcome === 'expired'
            ? 'expired'
            : 'pending';
      rows.push({ ...item, status });
    }
    for (const code of parseManualExtra(extraRaw)) {
      const key = code.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      const outcome = redeemedMap.get(key);
      const status =
        outcome === 'redeemed' || outcome === 'already_used'
          ? 'claimed'
          : 'pending';
      rows.push({
        code,
        reward: '',
        expires_at: null,
        source: '手动',
        status,
        isManual: true,
      });
    }
    return rows;
  }, [data?.codes, extraRaw, redeemedMap]);

  const pendingCount = useMemo(
    () => mergedRows.filter((r) => r.status === 'pending').length,
    [mergedRows],
  );

  const handleCopy = async (code: string) => {
    try {
      await navigator.clipboard.writeText(code);
      setCopiedCode(code);
      toast.success(`已复制兑换码 ${code}`);
      setTimeout(() => setCopiedCode((prev) => (prev === code ? null : prev)), 1500);
    } catch {
      toast.error('复制失败');
    }
  };

  if (!isTauri()) return null;

  return (
    <div className="rounded-lg border border-border bg-bg-secondary overflow-hidden text-xs text-text-secondary">
      <div className="flex items-center justify-between gap-2 border-b border-border px-3 py-2">
        <div className="flex min-w-0 items-center gap-1.5">
          <Gift className="h-3.5 w-3.5 shrink-0 text-accent" />
          <span className="font-medium text-text-primary">当前同步到的兑换码</span>
          {!loading && data && (
            <span className="tabular-nums text-[11px] text-text-muted">
              {mergedRows.length === 0
                ? '暂无有效码'
                : pendingCount > 0
                  ? `共 ${mergedRows.length} 个 · 待领 ${pendingCount}`
                  : `共 ${mergedRows.length} 个 · 已全部领完`}
            </span>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {data?.synced_at && (
            <span className="tabular-nums text-[10px] text-text-muted">{data.synced_at}</span>
          )}
          <button
            type="button"
            onClick={() => void loadCodes(true)}
            disabled={loading}
            title="立即重新同步最新兑换码"
            className="inline-flex items-center gap-1 rounded-md border border-border bg-bg-primary px-2 py-0.5 text-[11px] text-text-secondary transition-colors hover:bg-bg-hover hover:text-text-primary disabled:opacity-50"
          >
            <RefreshCw className={clsx('h-3 w-3', loading && 'animate-spin')} />
            刷新
          </button>
        </div>
      </div>

      {loading && !data ? (
        <div className="px-3 py-3 text-center text-[11px] text-text-muted">正在同步有效兑换码…</div>
      ) : error && mergedRows.length === 0 ? (
        <div className="px-3 py-2.5 text-[11px] text-warning">同步失败：{error}</div>
      ) : mergedRows.length === 0 ? (
        <div className="px-3 py-3 text-center text-[11px] text-text-muted">
          当前暂无未过期的公开兑换码，可在上方「补充兑换码」中手动填写。
        </div>
      ) : (
        <div className="divide-y divide-border/70">
          {mergedRows.map((row) => (
            <div
              key={row.code}
              className={clsx(
                'flex flex-col gap-1 px-3 py-2',
                row.status !== 'pending' && 'opacity-70',
              )}
            >
              <div className="flex flex-wrap items-center gap-1.5">
                <code className="select-all rounded bg-bg-primary px-1.5 py-0.5 font-mono text-[12px] font-medium text-text-primary">
                  {row.code}
                </code>
                <span
                  className={clsx(
                    'rounded-full border px-1.5 py-0.5 text-[10px] font-medium',
                    row.status === 'pending' && 'border-accent/35 bg-accent/10 text-accent',
                    row.status === 'claimed' && 'border-success/30 bg-success/10 text-success',
                    row.status === 'expired' && 'border-warning/30 bg-warning/10 text-warning',
                  )}
                >
                  {row.status === 'pending'
                    ? '待领取'
                    : row.status === 'claimed'
                      ? '已领取'
                      : '已失效'}
                </span>
                <span className="rounded bg-bg-primary px-1.5 py-0.5 text-[10px] text-text-muted">
                  {row.source}
                </span>
                {row.expires_at && (
                  <span className="tabular-nums text-[10px] text-text-muted">
                    截止 {row.expires_at}
                  </span>
                )}
                <button
                  type="button"
                  onClick={() => void handleCopy(row.code)}
                  title="复制该兑换码"
                  className="ml-auto inline-flex items-center gap-0.5 rounded px-1.5 py-0.5 text-[10px] text-text-muted transition-colors hover:bg-bg-hover hover:text-text-primary"
                >
                  {copiedCode === row.code ? (
                    <>
                      <Check className="h-3 w-3 text-success" />
                      已复制
                    </>
                  ) : (
                    <>
                      <Copy className="h-3 w-3" />
                      复制
                    </>
                  )}
                </button>
              </div>
              {row.reward && (
                <p className="truncate text-[11px] text-text-secondary" title={row.reward}>
                  {row.reward}
                </p>
              )}
            </div>
          ))}
        </div>
      )}

      {data && data.warnings.length > 0 && (
        <div className="border-t border-border/70 bg-bg-primary/50 px-3 py-1.5 text-[10px] text-text-muted">
          部分在线源暂不可用，已展示其余来源的兑换码。
        </div>
      )}
    </div>
  );
}
