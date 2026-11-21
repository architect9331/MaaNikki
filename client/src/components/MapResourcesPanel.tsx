import { useCallback, useEffect, useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import clsx from 'clsx';
import {
  ChevronDown,
  ChevronRight,
  Compass,
  Gem,
  Info,
  Loader2,
  PackageOpen,
  RefreshCw,
  Sparkles,
} from 'lucide-react';
import { openPearpalLogin, refreshPearpalStatus } from '@/services/pearpalService';
import { apiGet } from '@/utils/backendApi';
import { isTauri } from '@/utils/paths';
import { Card, primaryButton, secondaryButton, Stat } from './ui/FeaturePage';

interface ChestBreakdown {
  total: number;
  collected: number;
  remaining: number;
  remaining_diamonds: number;
  tiers_remaining: {
    low: number;
    mid: number;
    high: number;
    sketch: number;
    momo: number;
  };
}

interface CollectibleStat {
  label?: string;
  total: number;
  collected: number;
  remaining: number;
  cap_diamonds?: number;
  cap_standard_pulls?: number;
  est_remaining_diamonds?: number;
  est_remaining_standard_pulls?: number;
}

interface SubregionItem {
  id: string;
  label: string;
  boxes: ChestBreakdown;
  stars: CollectibleStat;
  dewdrops: CollectibleStat;
}

interface RegionGroupItem {
  id: string;
  label: string;
  rough_chest_tiers: boolean;
  boxes: ChestBreakdown;
  stars: CollectibleStat;
  dewdrops: CollectibleStat;
  est_total_diamonds: number;
  est_total_standard_pulls: number;
  subregions: SubregionItem[];
}

export interface MapResourcesData {
  authenticated: boolean;
  has_login?: boolean;
  error?: string;
  cache_fetched_at?: string;
  synced_at?: string;
  totals?: {
    box_remaining_diamonds: number;
    milestone_est_diamonds: number;
    milestone_est_standard_pulls: number;
    total_est_diamonds: number;
    approx_diamond_pulls: number;
    stars: CollectibleStat;
    dewdrops: CollectibleStat;
    boxes: { total: number; collected: number; remaining: number };
  };
  groups?: RegionGroupItem[];
  untracked_categories?: { label: string; theory: string }[];
}

function formatTierSummary(tiers: ChestBreakdown['tiers_remaining']): string {
  const parts: string[] = [];
  if (tiers.low > 0) parts.push(`初级×${tiers.low}`);
  if (tiers.mid > 0) parts.push(`中级×${tiers.mid}`);
  if (tiers.high > 0) parts.push(`高级/洞窟×${tiers.high}`);
  if (tiers.sketch > 0) parts.push(`散件×${tiers.sketch}`);
  if (tiers.momo > 0) parts.push(`大喵×${tiers.momo}`);
  return parts.length ? parts.join(' · ') : '已全部开启';
}

function formatSyncTime(iso?: string): string {
  if (!iso) return '—';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString('zh-CN', { hour12: false });
}

function ProgressMini({ collected, total }: { collected: number; total: number }) {
  const pct = total > 0 ? Math.min(100, Math.round((collected / total) * 100)) : 0;
  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between gap-2 text-xs tabular-nums">
        <span className="font-medium text-text-primary">
          {collected.toLocaleString('zh-CN')} / {total.toLocaleString('zh-CN')}
        </span>
        <span className="text-[11px] text-text-muted">{pct}%</span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-border/60">
        <div
          className={clsx('h-full rounded-full transition-all', pct >= 100 ? 'bg-success' : 'bg-accent')}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

export function MapResourcesPanel({ onLogin }: { onLogin?: () => void }) {
  const [data, setData] = useState<MapResourcesData | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState('');
  const [expanded, setExpanded] = useState<Record<string, boolean>>({ wishfield: true, yizan: true });

  const load = useCallback(async (forceRefresh = false) => {
    if (forceRefresh) setRefreshing(true);
    else setLoading(true);
    try {
      const res = isTauri()
        ? await invoke<MapResourcesData>('pearpal_map_resources', { forceRefresh })
        : await apiGet<MapResourcesData>(`/pearpal/map-resources?forceRefresh=${forceRefresh}`);
      setData(res);
      setError('');
      if (isTauri() && res.has_login && !res.authenticated) {
        void refreshPearpalStatus({ silent: true });
      }
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void load(false);
    if (!isTauri()) return;
    let dispose: (() => void) | undefined;
    void listen('pearpal-login-changed', () => {
      void load(false);
    }).then((fn) => {
      dispose = fn;
    });
    return () => dispose?.();
  }, [load]);

  if (loading && !data) {
    return (
      <div className="flex items-center justify-center gap-2 rounded-xl border border-border bg-bg-secondary py-14 text-xs text-text-muted">
        <Loader2 className="h-4 w-4 animate-spin" />
        <span>正在核对美鸭梨大地图收集进度…</span>
      </div>
    );
  }

  if (error) {
    return (
      <Card icon={Compass} title="大地图剩余可挖抽卡资源">
        <div className="space-y-3 p-5 text-xs">
          <p className="text-warning">{error}</p>
          <button type="button" onClick={() => void load(false)} className={secondaryButton}>
            <RefreshCw className="h-3.5 w-3.5" />重试
          </button>
        </div>
      </Card>
    );
  }

  if (!data?.authenticated) {
    const loginHandler =
      onLogin ??
      (isTauri()
        ? () => void openPearpalLogin({ switchAccount: Boolean(data?.has_login) })
        : undefined);
    return (
      <Card icon={Compass} title="大地图剩余可挖抽卡资源">
        <div className="flex flex-col items-center gap-3 px-6 py-12 text-center">
          <Compass className="h-8 w-8 text-accent/70" />
          <div className="space-y-1">
            <p className="text-sm font-medium text-text-primary">
              {data?.has_login ? '美鸭梨登录状态已过期' : '登录美鸭梨后即可核算大地图剩余抽数'}
            </p>
            <p className="max-w-md text-xs leading-5 text-text-secondary">
              {data?.error || '系统将基于美鸭梨大地图的实时收集进度，为你统计各世界未开宝箱钻石与奇想星/灵感露珠里程碑估抽。'}
            </p>
          </div>
          {loginHandler && isTauri() && (
            <button type="button" onClick={loginHandler} className={primaryButton}>
              {data?.has_login ? '重新登录美鸭梨' : '登录美鸭梨'}
            </button>
          )}
        </div>
      </Card>
    );
  }

  const totals = data.totals;
  const groups = data.groups ?? [];
  const untracked = data.untracked_categories ?? [];
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-border bg-bg-secondary px-4 py-3">
        <div className="space-y-0.5">
          <div className="flex items-center gap-2 text-sm font-semibold text-text-primary">
            <Gem className="h-4 w-4 text-accent" />
            <span>大地图剩余可挖资源（来自美鸭梨实时收集进度）</span>
          </div>
          <p className="text-xs text-text-muted">
            进度更新于 {formatSyncTime(data.synced_at)} · 公共点位缓存于 {formatSyncTime(data.cache_fetched_at)}
          </p>
        </div>
        <button
          type="button"
          onClick={() => void load(true)}
          disabled={refreshing}
          className={secondaryButton}
          title="重新拉取最新公共地图点位与个人进度"
        >
          <RefreshCw className={clsx('h-3.5 w-3.5', refreshing && 'animate-spin')} />
          <span>{refreshing ? '同步中…' : '刷新地图资源'}</span>
        </button>
      </div>

      {totals && (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <Stat
            label="宝箱保底可挖钻石"
            value={<>{totals.box_remaining_diamonds.toLocaleString('zh-CN')} <span className="text-xs font-normal text-text-secondary">钻</span></>}
            detail={`剩余 ${totals.boxes.remaining.toLocaleString('zh-CN')} / ${totals.boxes.total.toLocaleString('zh-CN')} 个未开启（约 ${Math.floor(totals.box_remaining_diamonds / 120)} 抽）`}
          />
          <Stat
            label="奇想星与露珠估抽参考"
            value={<>{totals.milestone_est_diamonds.toLocaleString('zh-CN')} <span className="text-xs font-normal text-text-secondary">钻 + {totals.milestone_est_standard_pulls} 常驻</span></>}
            detail={`剩余星 ${totals.stars.remaining} 颗 · 露珠类 ${totals.dewdrops.remaining} 颗`}
          />
          <Stat
            label="地图可挖合计参考"
            value={<>≈ {totals.approx_diamond_pulls} <span className="text-xs font-normal text-text-secondary">抽 + {totals.milestone_est_standard_pulls} 常驻</span></>}
            detail={`合计约 ${totals.total_est_diamonds.toLocaleString('zh-CN')} 钻（按 120 钻 = 1 抽折算）`}
          />
          <Stat
            label="全图三项总收集度"
            value={
              <>
                {(totals.boxes.collected + totals.stars.collected + totals.dewdrops.collected).toLocaleString('zh-CN')}
                <span className="text-xs font-normal text-text-secondary">
                  {' '}/ {(totals.boxes.total + totals.stars.total + totals.dewdrops.total).toLocaleString('zh-CN')}
                </span>
              </>
            }
            detail={`宝箱 ${totals.boxes.collected}/${totals.boxes.total} · 奇想星 ${totals.stars.collected}/${totals.stars.total}`}
          />
        </div>
      )}

      <Card
        icon={PackageOpen}
        title="分区域剩余明细"
        aside={<span className="text-xs text-text-muted">宝箱按档位直算 · 奇想星/露珠按未收集占比估算</span>}
      >
        <div className="divide-y divide-border">
          {groups.map((grp) => {
            const hasSub = grp.subregions.length > 0;
            const isOpen = Boolean(expanded[grp.id]);
            const starHasReward = (grp.stars.cap_diamonds ?? 0) > 0 || (grp.stars.cap_standard_pulls ?? 0) > 0;
            const dewHasReward = (grp.dewdrops.cap_diamonds ?? 0) > 0 || (grp.dewdrops.cap_standard_pulls ?? 0) > 0;
            return (
              <div key={grp.id}>
                <div className="grid gap-3 px-4 py-3.5 lg:grid-cols-[minmax(0,1.15fr)_minmax(0,1.25fr)_minmax(0,1fr)_minmax(0,1fr)_minmax(0,0.9fr)] lg:items-center">
                  <div className="min-w-0 space-y-1">
                    <div className="flex items-center gap-1.5">
                      {hasSub ? (
                        <button
                          type="button"
                          onClick={() => setExpanded((prev) => ({ ...prev, [grp.id]: !isOpen }))}
                          className="inline-flex items-center gap-1 text-sm font-semibold text-text-primary hover:text-accent"
                        >
                          {isOpen ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
                          <span>{grp.label}</span>
                        </button>
                      ) : (
                        <span className="pl-5 text-sm font-semibold text-text-primary">{grp.label}</span>
                      )}
                      {grp.rough_chest_tiers && (
                        <span
                          className="rounded bg-bg-tertiary px-1.5 py-0.5 text-[10px] text-text-muted"
                          title="该区域部分中级宝箱在美鸭梨中归入初级档位，宝箱钻数为保底估算"
                        >
                          保底估算
                        </span>
                      )}
                    </div>
                  </div>

                  <div className="space-y-1">
                    <div className="flex items-baseline justify-between gap-2 text-xs">
                      <span className="text-text-secondary">宝箱</span>
                      <span className="font-medium tabular-nums text-accent">+{grp.boxes.remaining_diamonds.toLocaleString('zh-CN')} 钻</span>
                    </div>
                    <ProgressMini collected={grp.boxes.collected} total={grp.boxes.total} />
                    <p className="truncate text-[11px] text-text-muted" title={formatTierSummary(grp.boxes.tiers_remaining)}>
                      {formatTierSummary(grp.boxes.tiers_remaining)}
                    </p>
                  </div>

                  <div className="space-y-1">
                    <div className="flex items-baseline justify-between gap-2 text-xs">
                      <span className="text-text-secondary">{grp.stars.label || '奇想星'}</span>
                      <span className="tabular-nums text-text-muted">
                        {starHasReward
                          ? `参考 +${grp.stars.est_remaining_diamonds ?? 0}钻${(grp.stars.est_remaining_standard_pulls ?? 0) > 0 ? `+${grp.stars.est_remaining_standard_pulls}常` : ''}`
                          : '无直接抽卡'}
                      </span>
                    </div>
                    <ProgressMini collected={grp.stars.collected} total={grp.stars.total} />
                    {starHasReward && (
                      <p className="text-[11px] text-text-muted">
                        满额共 {grp.stars.cap_diamonds || 0}钻{(grp.stars.cap_standard_pulls || 0) > 0 ? `+${grp.stars.cap_standard_pulls}常驻` : ''}
                      </p>
                    )}
                  </div>

                  <div className="space-y-1">
                    <div className="flex items-baseline justify-between gap-2 text-xs">
                      <span className="text-text-secondary">{grp.dewdrops.label || '灵感露珠'}</span>
                      <span className="tabular-nums text-text-muted">
                        {dewHasReward
                          ? `参考 +${grp.dewdrops.est_remaining_diamonds ?? 0}钻${(grp.dewdrops.est_remaining_standard_pulls ?? 0) > 0 ? `+${grp.dewdrops.est_remaining_standard_pulls}常` : ''}`
                          : '—'}
                      </span>
                    </div>
                    <ProgressMini collected={grp.dewdrops.collected} total={grp.dewdrops.total} />
                    {dewHasReward && (
                      <p className="text-[11px] text-text-muted">
                        满额共 {(grp.dewdrops.cap_diamonds || 0) > 0 ? `${grp.dewdrops.cap_diamonds}钻` : ''}{(grp.dewdrops.cap_diamonds || 0) > 0 && (grp.dewdrops.cap_standard_pulls || 0) > 0 ? '+' : ''}{(grp.dewdrops.cap_standard_pulls || 0) > 0 ? `${grp.dewdrops.cap_standard_pulls}常驻` : ''}
                      </p>
                    )}
                  </div>

                  <div className="text-left lg:text-right">
                    <p className="text-xs text-text-muted">本区参考可挖</p>
                    <p className="text-sm font-semibold tabular-nums text-text-primary">
                      {grp.est_total_diamonds.toLocaleString('zh-CN')} 钻
                      {grp.est_total_standard_pulls > 0 && (
                        <span className="text-xs font-normal text-accent"> +{grp.est_total_standard_pulls} 常驻</span>
                      )}
                    </p>
                  </div>
                </div>

                {hasSub && isOpen && (
                  <div className="border-t border-border/50 bg-bg-primary/50 px-4 py-2">
                    <div className="divide-y divide-border/40">
                      {grp.subregions.map((sub) => (
                        <div key={sub.id} className="grid gap-2 py-2 text-xs lg:grid-cols-[minmax(0,1.15fr)_minmax(0,1.25fr)_minmax(0,1fr)_minmax(0,1fr)_minmax(0,0.9fr)] lg:items-center">
                          <span className="pl-5 text-text-secondary">{sub.label}</span>
                          <div className="flex flex-wrap items-center justify-between gap-2 tabular-nums">
                            <span className="text-text-secondary">宝箱 {sub.boxes.collected}/{sub.boxes.total}</span>
                            <span className="text-accent">+{sub.boxes.remaining_diamonds} 钻</span>
                            <span className="w-full truncate text-[11px] text-text-muted">{formatTierSummary(sub.boxes.tiers_remaining)}</span>
                          </div>
                          <span className="tabular-nums text-text-muted">
                            {grp.stars.label} {sub.stars.collected}/{sub.stars.total}
                          </span>
                          <span className="tabular-nums text-text-muted">
                            {grp.dewdrops.label} {sub.dewdrops.collected}/{sub.dewdrops.total}
                          </span>
                          <span className="text-left tabular-nums text-text-secondary lg:text-right">
                            箱 +{sub.boxes.remaining_diamonds} 钻
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </Card>

      {untracked.length > 0 && (
        <Card icon={Info} title="未计入部分说明（美鸭梨大地图接口暂不返回个人完成状态）">
          <div className="space-y-3 p-4 text-xs">
            <p className="leading-5 text-text-secondary">
              上方仅统计可通过美鸭梨实时核对点位进度的三项（宝箱、奇想星类、灵感露珠类）。以下 6 类全图探索/任务奖励暂无法从接口自动读取，请结合游戏内探索度自行确认：
            </p>
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {untracked.map((item) => (
                <div key={item.label} className="flex items-center justify-between rounded-lg border border-border/70 bg-bg-primary px-3 py-2">
                  <span className="font-medium text-text-primary">{item.label}</span>
                  <span className="tabular-nums text-text-muted">{item.theory}</span>
                </div>
              ))}
            </div>
            <p className="flex items-center gap-1.5 text-[11px] text-text-muted">
              <Sparkles className="h-3.5 w-3.5 shrink-0 text-accent" />
              <span>说明：奇想星/露珠大地图点位总略多于拿满兑换奖励所需数量，若游戏内对应兑换商店已搬空，请以实际拿满为准；星海区域不含宝箱故未单列。</span>
            </p>
          </div>
        </Card>
      )}
    </div>
  );
}
