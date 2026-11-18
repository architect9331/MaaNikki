import { useCallback, useEffect, useMemo, useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import clsx from 'clsx';
import {
  CalendarDays,
  ChevronLeft,
  ChevronRight,
  Coins,
  Filter,
  Flame,
  Package,
  RefreshCw,
  Sparkles,
} from 'lucide-react';
import { apiGet } from '@/utils/backendApi';
import { isTauri } from '@/utils/paths';
import { Card, Segmented, Stat } from './ui/FeaturePage';

export interface AdventureReportEntry {
  id: string;
  at: string;
  category: string;
  task: string;
  title: string;
  detail?: string;
  energy_spent?: number;
  runs?: number;
  status?: string;
  items?: Record<string, number>;
  meta?: Record<string, unknown>;
}

export interface AdventureReportDaySummary {
  energy_spent?: number;
  realm_runs?: number;
  bling_gained?: number;
  particles_gained?: number;
  realm_materials_gained?: number;
  starlight_crystals_gained?: number;
  open_world_pickups?: number;
  zhaoxi_score?: number | null;
  xinghai_score?: number | null;
  entry_count?: number;
}

export interface AdventureReportDay {
  summary?: AdventureReportDaySummary;
  items?: Record<string, number>;
  entries?: AdventureReportEntry[];
}

interface AdventureReportAccount {
  key: string;
  uid?: string | null;
  label: string;
  days_count: number;
  updated_at?: string | null;
}

interface AdventureReportResponse {
  active_account_key?: string | null;
  selected_key: string;
  accounts: AdventureReportAccount[];
  report: {
    schema_version: number;
    account_key: string;
    uid?: string | null;
    updated_at?: string | null;
    days: Record<string, AdventureReportDay>;
  };
}

type ChartMetric = 'energy' | 'bling' | 'runs' | 'yield';
type CategoryFilter = 'all' | 'realm' | 'blessing' | 'starsea' | 'open_world' | 'daily' | 'minigame' | 'dig' | 'crown';

const CATEGORY_META: Record<string, { label: string; tone: string }> = {
  realm: { label: '幻境挑战', tone: 'bg-accent/15 text-accent' },
  blessing: { label: '闪光分解', tone: 'bg-purple-500/15 text-purple-400' },
  starsea: { label: '星海收集', tone: 'bg-sky-500/15 text-sky-400' },
  open_world: { label: '大世界采集', tone: 'bg-emerald-500/15 text-emerald-400' },
  daily: { label: '每日日常', tone: 'bg-amber-500/15 text-amber-400' },
  minigame: { label: '小游戏', tone: 'bg-orange-500/15 text-orange-400' },
  dig: { label: '美鸭梨挖掘', tone: 'bg-teal-500/15 text-teal-400' },
  crown: { label: '巅峰赛', tone: 'bg-rose-500/15 text-rose-400' },
};

const WEEKDAYS = ['一', '二', '三', '四', '五', '六', '日'];

function currentGameDayKey(): string {
  return new Date(Date.now() + (8 - 4) * 3600_000).toISOString().slice(0, 10);
}

function monthDaysList(monthKey: string): string[] {
  const [y, m] = monthKey.split('-').map(Number);
  if (!y || !m) return [];
  const total = new Date(Date.UTC(y, m, 0)).getUTCDate();
  return Array.from({ length: total }, (_, idx) => `${monthKey}-${String(idx + 1).padStart(2, '0')}`);
}

function shiftMonth(monthKey: string, delta: number): string {
  const [y, m] = monthKey.split('-').map(Number);
  const d = new Date(Date.UTC(y, (m || 1) - 1 + delta, 1));
  return d.toISOString().slice(0, 7);
}

function formatMonthLabel(monthKey: string): string {
  const [y, m] = monthKey.split('-').map(Number);
  return `${y} 年 ${m} 月`;
}

function formatTimeOnly(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso.slice(11, 19) || iso;
  return date.toLocaleTimeString('zh-CN', { hour12: false });
}

function formatCompactCount(val: number): string {
  if (val >= 100000) return `${(val / 10000).toFixed(1)}w`;
  if (val >= 10000) return `${(val / 10000).toFixed(2).replace(/\.00$/, '')}w`;
  return val.toLocaleString('zh-CN');
}

function dayYieldTotal(day: AdventureReportDay | undefined, materialFilter: string): number {
  if (!day) return 0;
  if (materialFilter !== 'all') {
    return day.items?.[materialFilter] ?? 0;
  }
  const s = day.summary;
  if (!s) return 0;
  return (s.realm_materials_gained ?? 0) + (s.starlight_crystals_gained ?? 0) + (s.open_world_pickups ?? 0);
}

function metricValue(day: AdventureReportDay | undefined, metric: ChartMetric, materialFilter: string): number {
  if (!day) return 0;
  if (materialFilter !== 'all' && metric === 'yield') {
    return day.items?.[materialFilter] ?? 0;
  }
  const s = day.summary;
  if (!s) return 0;
  if (metric === 'energy') return s.energy_spent ?? 0;
  if (metric === 'bling') return s.bling_gained ?? 0;
  if (metric === 'runs') return s.realm_runs ?? 0;
  return dayYieldTotal(day, materialFilter);
}

export function AdventureReportPanel() {
  const todayKey = currentGameDayKey();
  const [accountKey, setAccountKey] = useState<string>('');
  const [data, setData] = useState<AdventureReportResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [monthKey, setMonthKey] = useState<string>(() => todayKey.slice(0, 7));
  const [selectedDay, setSelectedDay] = useState<string>(todayKey);
  const [metric, setMetric] = useState<ChartMetric>('energy');
  const [materialFilter, setMaterialFilter] = useState<string>('all');
  const [categoryFilter, setCategoryFilter] = useState<CategoryFilter>('all');
  const [hoverDay, setHoverDay] = useState<string | null>(null);

  const loadReport = useCallback(async (targetKey?: string) => {
    setLoading(true);
    try {
      const chosen = targetKey ?? accountKey;
      const res = isTauri()
        ? await invoke<AdventureReportResponse>('get_adventure_report', {
            accountKey: chosen || null,
          })
        : await apiGet<AdventureReportResponse>(
            chosen ? `/adventure-report?accountKey=${encodeURIComponent(chosen)}` : '/adventure-report',
          );
      setData(res);
      setError('');
      if (!chosen && res.selected_key) {
        setAccountKey(res.selected_key);
      }
      const days = Object.keys(res.report?.days ?? {}).sort();
      if (days.length > 0 && !res.report?.days?.[todayKey]) {
        const latest = days[days.length - 1];
        setSelectedDay((prev) => (res.report?.days?.[prev] ? prev : latest));
        setMonthKey((prev) => (prev === todayKey.slice(0, 7) ? latest.slice(0, 7) : prev));
      }
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  }, [accountKey, todayKey]);

  useEffect(() => {
    void loadReport();
    if (!isTauri()) return;
    let dispose: (() => void) | undefined;
    void listen('adventure-report-changed', () => {
      void loadReport();
    }).then((fn) => {
      dispose = fn;
    });
    return () => dispose?.();
  }, [loadReport]);

  const daysMap = data?.report?.days ?? {};
  const monthDays = useMemo(() => monthDaysList(monthKey), [monthKey]);
  const leadingEmptyCells = useMemo(() => {
    if (!monthDays.length) return 0;
    const [y, m] = monthKey.split('-').map(Number);
    const weekday = new Date(Date.UTC(y, m - 1, 1)).getUTCDay();
    return (weekday + 6) % 7;
  }, [monthDays.length, monthKey]);

  const availableMaterials = useMemo(() => {
    const totals = new Map<string, number>();
    for (const dayDoc of Object.values(daysMap)) {
      for (const [name, qty] of Object.entries(dayDoc.items ?? {})) {
        if (qty > 0) totals.set(name, (totals.get(name) ?? 0) + qty);
      }
    }
    return [...totals.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], 'zh-CN'));
  }, [daysMap]);

  const monthTotals = useMemo(() => {
    let energy = 0;
    let runs = 0;
    let bling = 0;
    let particles = 0;
    let crystals = 0;
    let pickups = 0;
    let realmMaterials = 0;
    let filteredMaterialTotal = 0;
    let activeDays = 0;
    for (const day of monthDays) {
      const doc = daysMap[day];
      if (!doc) continue;
      const s = doc.summary ?? {};
      if ((s.entry_count ?? doc.entries?.length ?? 0) > 0) activeDays += 1;
      energy += s.energy_spent ?? 0;
      runs += s.realm_runs ?? 0;
      bling += s.bling_gained ?? 0;
      particles += s.particles_gained ?? 0;
      crystals += s.starlight_crystals_gained ?? 0;
      pickups += s.open_world_pickups ?? 0;
      realmMaterials += s.realm_materials_gained ?? 0;
      if (materialFilter !== 'all') {
        filteredMaterialTotal += doc.items?.[materialFilter] ?? 0;
      }
    }
    return {
      energy,
      runs,
      bling,
      particles,
      crystals,
      pickups,
      realmMaterials,
      filteredMaterialTotal,
      activeDays,
    };
  }, [daysMap, materialFilter, monthDays]);
  const selectedDoc = daysMap[selectedDay];
  const selectedSummary = selectedDoc?.summary;

  const chartPeak = useMemo(() => {
    let maxVal = 1;
    for (const day of monthDays) {
      const v = metricValue(daysMap[day], metric, materialFilter);
      if (v > maxVal) maxVal = v;
    }
    return maxVal;
  }, [daysMap, materialFilter, metric, monthDays]);

  const calendarPeak = useMemo(() => {
    let maxVal = 1;
    for (const day of monthDays) {
      const doc = daysMap[day];
      const v = materialFilter !== 'all'
        ? (doc?.items?.[materialFilter] ?? 0)
        : (doc?.summary?.energy_spent ?? 0) + (doc?.summary?.open_world_pickups ?? 0) * 5 + (doc?.summary?.starlight_crystals_gained ?? 0) * 20;
      if (v > maxVal) maxVal = v;
    }
    return maxVal;
  }, [daysMap, materialFilter, monthDays]);

  const selectedDayItems = useMemo(() => {
    const entries = Object.entries(selectedDoc?.items ?? {}).filter(([, qty]) => qty > 0);
    return entries.sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], 'zh-CN'));
  }, [selectedDoc?.items]);

  const filteredEntries = useMemo(() => {
    const raw = [...(selectedDoc?.entries ?? [])].reverse();
    return raw.filter((entry) => {
      if (categoryFilter !== 'all' && entry.category !== categoryFilter) return false;
      if (materialFilter !== 'all' && !(entry.items && (entry.items[materialFilter] ?? 0) > 0)) return false;
      return true;
    });
  }, [categoryFilter, materialFilter, selectedDoc?.entries]);

  const activeHoverDay = hoverDay ?? selectedDay;
  const activeHoverVal = metricValue(daysMap[activeHoverDay], metric, materialFilter);

  return (
    <div className="space-y-4">
      {/* 顶部控制条：账号切换 + 月份导航 + 刷新 */}
      <div className="flex flex-wrap items-center gap-2 rounded-xl border border-border bg-bg-secondary px-4 py-3">
        <div className="flex items-center gap-2">
          <CalendarDays className="h-4 w-4 text-accent" />
          <span className="text-sm font-semibold text-text-primary">冒险日报</span>
          <span className="hidden text-xs text-text-muted sm:inline">按游戏日（04:00 刷新）按天汇总消耗与产出</span>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          {!!data?.accounts?.length && (
            <select
              aria-label="选择游戏账号"
              value={accountKey || data.selected_key}
              onChange={(event) => {
                const next = event.target.value;
                setAccountKey(next);
                void loadReport(next);
              }}
              className="rounded-lg border border-border bg-bg-primary px-2.5 py-1.5 text-xs text-text-primary outline-none focus:border-accent"
            >
              {data.accounts.map((acc) => (
                <option key={acc.key} value={acc.key}>
                  {acc.label}{acc.days_count > 0 ? `（${acc.days_count} 天）` : ''}
                </option>
              ))}
            </select>
          )}
          <div className="inline-flex items-center rounded-lg border border-border bg-bg-primary">
            <button
              type="button"
              onClick={() => setMonthKey((prev) => shiftMonth(prev, -1))}
              aria-label="上个月"
              className="rounded-l-lg p-1.5 text-text-secondary hover:bg-bg-hover"
            >
              <ChevronLeft className="h-3.5 w-3.5" />
            </button>
            <span className="min-w-24 px-2 text-center text-xs font-medium tabular-nums text-text-primary">
              {formatMonthLabel(monthKey)}
            </span>
            <button
              type="button"
              onClick={() => setMonthKey((prev) => shiftMonth(prev, 1))}
              aria-label="下个月"
              className="rounded-r-lg p-1.5 text-text-secondary hover:bg-bg-hover"
            >
              <ChevronRight className="h-3.5 w-3.5" />
            </button>
          </div>
          <button
            type="button"
            onClick={() => {
              setMonthKey(todayKey.slice(0, 7));
              setSelectedDay(todayKey);
            }}
            className="rounded-lg border border-border bg-bg-primary px-2.5 py-1.5 text-xs text-text-secondary hover:bg-bg-hover"
          >
            今天
          </button>
          <button
            type="button"
            onClick={() => void loadReport()}
            disabled={loading}
            className="inline-flex items-center gap-1 rounded-lg border border-border bg-bg-primary px-2.5 py-1.5 text-xs text-text-secondary hover:bg-bg-hover disabled:opacity-50"
          >
            <RefreshCw className={clsx('h-3.5 w-3.5', loading && 'animate-spin')} />
            <span>刷新</span>
          </button>
        </div>
      </div>

      {error && (
        <div className="rounded-xl border border-warning/30 bg-warning/10 px-4 py-3 text-xs text-warning">
          冒险日报读取失败：{error}
        </div>
      )}

      {/* 本月核心指标卡片 */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat
          label="本月活跃能量消耗"
          value={<>{monthTotals.energy.toLocaleString('zh-CN')} <span className="text-xs font-normal text-text-secondary">点</span></>}
          detail={`幻境挑战 ${monthTotals.runs} 次 · 记录活跃 ${monthTotals.activeDays} 天`}
        />
        <Stat
          label="本月噗灵与闪光粒子"
          value={<>{formatCompactCount(monthTotals.bling)} <span className="text-xs font-normal text-text-secondary">噗灵</span></>}
          detail={`分解获得闪光粒子 ${monthTotals.particles.toLocaleString('zh-CN')} 个`}
        />
        <Stat
          label={materialFilter === 'all' ? '本月材料与星光结晶' : `本月「${materialFilter}」`}
          value={
            materialFilter === 'all'
              ? <>{(monthTotals.realmMaterials + monthTotals.crystals).toLocaleString('zh-CN')} <span className="text-xs font-normal text-text-secondary">份</span></>
              : <>{monthTotals.filteredMaterialTotal.toLocaleString('zh-CN')} <span className="text-xs font-normal text-text-secondary">个</span></>
          }
          detail={`星光结晶 ${monthTotals.crystals} 个 · 幻境材料 ${monthTotals.realmMaterials.toLocaleString('zh-CN')} 个`}
        />
        <Stat
          label="大世界拾取与选中日心愿"
          value={<>{monthTotals.pickups.toLocaleString('zh-CN')} <span className="text-xs font-normal text-text-secondary">个材料</span></>}
          detail={
            selectedSummary?.zhaoxi_score != null || selectedSummary?.xinghai_score != null
              ? `选中日 朝夕 ${selectedSummary?.zhaoxi_score ?? '—'}/500 · 星海 ${selectedSummary?.xinghai_score ?? '—'}/500`
              : `${selectedDay} 暂无心愿积分记录`
          }
        />
      </div>

      {/* 日历与每日柱状图 */}
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1.05fr)_minmax(0,1.15fr)]">
        <Card
          icon={CalendarDays}
          title={`${formatMonthLabel(monthKey)} 每日汇总`}
          aside={<span className="text-xs text-text-muted">点击日期查看当日流水</span>}
        >
          <div className="p-3">
            <div className="mb-1.5 grid grid-cols-7 gap-1 text-center text-[11px] font-medium text-text-muted">
              {WEEKDAYS.map((wd) => <div key={wd} className="py-1">{wd}</div>)}
            </div>
            <div className="grid grid-cols-7 gap-1">
              {Array.from({ length: leadingEmptyCells }).map((_, idx) => (
                <div key={`pad-${idx}`} className="h-14 rounded-lg bg-bg-primary/30" />
              ))}
              {monthDays.map((day) => {
                const dayNum = Number(day.slice(8, 10));
                const doc = daysMap[day];
                const s = doc?.summary;
                const filteredQty = materialFilter !== 'all' ? (doc?.items?.[materialFilter] ?? 0) : 0;
                const intensityRaw = materialFilter !== 'all'
                  ? filteredQty
                  : (s?.energy_spent ?? 0) + (s?.open_world_pickups ?? 0) * 5 + (s?.starlight_crystals_gained ?? 0) * 20;
                const hasActivity = materialFilter !== 'all'
                  ? filteredQty > 0
                  : (s?.entry_count ?? doc?.entries?.length ?? 0) > 0;
                const ratio = intensityRaw > 0 ? Math.min(1, intensityRaw / calendarPeak) : 0;
                const isSelected = day === selectedDay;
                const isToday = day === todayKey;
                return (
                  <button
                    key={day}
                    type="button"
                    onClick={() => setSelectedDay(day)}
                    className={clsx(
                      'relative flex h-14 flex-col justify-between rounded-lg border p-1.5 text-left transition-all',
                      isSelected
                        ? 'border-accent bg-accent/15 ring-1 ring-accent'
                        : hasActivity
                          ? 'border-border/80 bg-bg-primary hover:border-accent/50'
                          : 'border-border/40 bg-bg-primary/40 text-text-muted hover:bg-bg-hover/40',
                    )}
                    style={!isSelected && ratio > 0 ? { backgroundColor: `color-mix(in srgb, var(--accent) ${Math.round(8 + ratio * 18)}%, transparent)` } : undefined}
                  >
                    <div className="flex items-center justify-between gap-1">
                      <span className={clsx('text-[11px] font-semibold tabular-nums', isToday ? 'text-accent' : 'text-text-primary')}>
                        {dayNum}
                      </span>
                      {s?.zhaoxi_score === 500 && (
                        <span className="h-1.5 w-1.5 rounded-full bg-success" title="朝夕心愿已完成 500/500" />
                      )}
                    </div>
                    <div className="truncate text-[10px] font-medium tabular-nums text-text-secondary">
                      {materialFilter !== 'all'
                        ? (filteredQty > 0 ? `+${formatCompactCount(filteredQty)}` : '')
                        : (s?.energy_spent ?? 0) > 0
                          ? `${s!.energy_spent}⚡`
                          : (s?.open_world_pickups ?? 0) > 0
                            ? `${s!.open_world_pickups}采`
                            : (s?.starlight_crystals_gained ?? 0) > 0
                              ? `✦${s!.starlight_crystals_gained}`
                              : hasActivity
                                ? '✓'
                                : ''}
                    </div>
                  </button>
                );
              })}
            </div>
          </div>
        </Card>

        <Card
          icon={Flame}
          title={materialFilter !== 'all' && metric === 'yield' ? `每日「${materialFilter}」产出` : '每日消耗与收益趋势'}
          aside={
            <Segmented
              label="图表指标"
              value={metric}
              onChange={setMetric}
              options={[
                ['energy', '能量'],
                ['bling', '噗灵'],
                ['runs', '场次'],
                ['yield', '材料'],
              ] as const}
            />
          }
        >
          <div className="flex h-full flex-col justify-between p-4">
            <div className="mb-2 flex items-center justify-between text-xs text-text-secondary">
              <span className="tabular-nums">{activeHoverDay}</span>
              <span className="font-medium tabular-nums text-text-primary">
                {metric === 'energy' && `${activeHoverVal.toLocaleString('zh-CN')} 活跃能量`}
                {metric === 'bling' && `${activeHoverVal.toLocaleString('zh-CN')} 噗灵`}
                {metric === 'runs' && `${activeHoverVal.toLocaleString('zh-CN')} 次幻境`}
                {metric === 'yield' && `${activeHoverVal.toLocaleString('zh-CN')} 个`}
              </span>
            </div>
            <div
              className="flex h-44 items-end gap-1"
              onMouseLeave={() => setHoverDay(null)}
            >
              {monthDays.map((day) => {
                const val = metricValue(daysMap[day], metric, materialFilter);
                const heightPct = val > 0 ? Math.max(4, Math.round((val / chartPeak) * 100)) : 0;
                const isSelected = day === selectedDay;
                const isHovered = day === hoverDay;
                return (
                  <button
                    key={day}
                    type="button"
                    onMouseEnter={() => setHoverDay(day)}
                    onClick={() => setSelectedDay(day)}
                    title={`${day}：${val.toLocaleString('zh-CN')}`}
                    className="group flex h-full min-w-0 flex-1 flex-col justify-end"
                  >
                    <div
                      className={clsx(
                        'w-full rounded-t transition-all',
                        isSelected
                          ? 'bg-accent'
                          : isHovered
                            ? 'bg-accent/80'
                            : val > 0
                              ? 'bg-accent/45 group-hover:bg-accent/70'
                              : 'bg-border/35',
                      )}
                      style={{ height: val > 0 ? `${heightPct}%` : '2px' }}
                    />
                  </button>
                );
              })}
            </div>
            <div className="mt-1.5 flex gap-1 border-t border-border pt-1.5 text-[10px] tabular-nums text-text-muted">
              {monthDays.map((day, idx) => {
                const dayNum = idx + 1;
                const show = dayNum === 1 || dayNum % 5 === 0 || dayNum === monthDays.length;
                return (
                  <span key={day} className="min-w-0 flex-1 text-center">
                    {show ? dayNum : ''}
                  </span>
                );
              })}
            </div>
          </div>
        </Card>
      </div>

      {/* 筛选栏 + 当日材料产出标签 + 单次流水明细 */}
      <Card
        icon={Package}
        title={`${selectedDay} 冒险明细`}
        aside={
          <div className="flex flex-wrap items-center gap-2">
            <div className="inline-flex items-center gap-1.5 text-xs text-text-secondary">
              <Filter className="h-3.5 w-3.5 text-text-muted" />
              <select
                aria-label="按材料筛选"
                value={materialFilter}
                onChange={(event) => {
                  const val = event.target.value;
                  setMaterialFilter(val);
                  if (val !== 'all') setMetric('yield');
                }}
                className="max-w-44 rounded-lg border border-border bg-bg-primary px-2.5 py-1 text-xs text-text-primary outline-none focus:border-accent"
              >
                <option value="all">全部材料 / 产出</option>
                {availableMaterials.map(([name, total]) => (
                  <option key={name} value={name}>
                    {name}（累计 {formatCompactCount(total)}）
                  </option>
                ))}
              </select>
            </div>
            {materialFilter !== 'all' && (
              <button
                type="button"
                onClick={() => setMaterialFilter('all')}
                className="rounded-lg border border-border bg-bg-primary px-2 py-1 text-xs text-text-secondary hover:bg-bg-hover"
              >
                清除材料筛选
              </button>
            )}
          </div>
        }
      >
        <div className="space-y-4 p-4">
          {/* 任务分类筛选胶囊 */}
          <div className="flex flex-wrap items-center gap-1.5">
            {([
              ['all', '全部'],
              ['realm', '幻境挑战'],
              ['blessing', '闪光分解'],
              ['starsea', '星海收集'],
              ['open_world', '大世界采集'],
              ['minigame', '小游戏'],
              ['dig', '美鸭梨挖掘'],
              ['daily', '每日日常'],
              ['crown', '巅峰赛'],
            ] as const).map(([key, label]) => (
              <button
                key={key}
                type="button"
                onClick={() => setCategoryFilter(key)}
                className={clsx(
                  'rounded-full px-2.5 py-1 text-xs transition-colors',
                  categoryFilter === key
                    ? 'bg-accent font-medium text-white'
                    : 'bg-bg-primary text-text-secondary hover:bg-bg-hover',
                )}
              >
                {label}
              </button>
            ))}
          </div>

          {/* 当日材料总产出标签 */}
          {selectedDayItems.length > 0 && (
            <div className="rounded-lg border border-border/80 bg-bg-primary/60 p-3">
              <div className="mb-2 flex items-center gap-1.5 text-xs font-medium text-text-secondary">
                <Coins className="h-3.5 w-3.5 text-accent" />
                <span>当日已统计材料 / 资源汇总（点击可筛选）</span>
              </div>
              <div className="flex flex-wrap gap-1.5">
                {selectedDayItems.map(([name, qty]) => {
                  const active = materialFilter === name;
                  return (
                    <button
                      key={name}
                      type="button"
                      onClick={() => {
                        const next = active ? 'all' : name;
                        setMaterialFilter(next);
                        if (next !== 'all') setMetric('yield');
                      }}
                      className={clsx(
                        'inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs tabular-nums transition-colors',
                        active
                          ? 'border-accent bg-accent/15 font-medium text-accent'
                          : 'border-border bg-bg-secondary text-text-primary hover:border-accent/50',
                      )}
                    >
                      <span>{name}</span>
                      <span className="font-semibold text-accent">×{qty.toLocaleString('zh-CN')}</span>
                    </button>
                  );
                })}
              </div>
            </div>
          )}

          {/* 当日单次流水明细 */}
          {loading && !data ? (
            <div className="py-10 text-center text-xs text-text-muted">正在加载冒险日报…</div>
          ) : filteredEntries.length === 0 ? (
            <div className="rounded-lg border border-dashed border-border bg-bg-primary/40 px-4 py-8 text-center text-xs text-text-muted">
              <Sparkles className="mx-auto mb-2 h-6 w-6 text-text-muted/60" />
              <p>
                {selectedDoc?.entries?.length
                  ? '当前筛选条件下没有匹配的执行记录，请切换材料或分类筛选。'
                  : `${selectedDay} 暂无冒险记录。运行幻境挑战、朝夕心愿、星海收集、大世界采集或日常任务后将自动记入此处。`}
              </p>
            </div>
          ) : (
            <div className="divide-y divide-border rounded-lg border border-border bg-bg-primary">
              {filteredEntries.map((entry) => {
                const catInfo = CATEGORY_META[entry.category] ?? {
                  label: entry.category,
                  tone: 'bg-bg-tertiary text-text-secondary',
                };
                const entryItems = Object.entries(entry.items ?? {}).filter(([, qty]) => qty > 0);
                return (
                  <div key={entry.id} className="flex flex-col gap-2 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
                    <div className="min-w-0 space-y-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-[11px] tabular-nums text-text-muted">
                          {formatTimeOnly(entry.at)}
                        </span>
                        <span className={clsx('rounded px-1.5 py-0.5 text-[11px] font-medium', catInfo.tone)}>
                          {catInfo.label}
                        </span>
                        <span className="text-sm font-medium text-text-primary">{entry.title}</span>
                        {entry.status === 'partial' && (
                          <span className="rounded bg-warning/15 px-1.5 py-0.5 text-[10px] text-warning">
                            部分完成
                          </span>
                        )}
                      </div>
                      {entry.detail && (
                        <p className="text-xs text-text-secondary">{entry.detail}</p>
                      )}
                    </div>
                    <div className="flex flex-wrap items-center gap-1.5 sm:justify-end">
                      {(entry.energy_spent ?? 0) > 0 && (
                        <span className="rounded-md bg-bg-secondary px-2 py-0.5 text-xs tabular-nums text-text-secondary">
                          -{entry.energy_spent} 活跃能量
                        </span>
                      )}
                      {(entry.runs ?? 0) > 0 && entry.category === 'realm' && (
                        <span className="rounded-md bg-bg-secondary px-2 py-0.5 text-xs tabular-nums text-text-secondary">
                          {entry.runs} 次
                        </span>
                      )}
                      {entryItems.map(([name, qty]) => (
                        <span
                          key={name}
                          className="rounded-md border border-accent/25 bg-accent/10 px-2 py-0.5 text-xs font-medium tabular-nums text-accent"
                        >
                          {name} ×{qty.toLocaleString('zh-CN')}
                        </span>
                      ))}
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          <p className="text-[11px] leading-5 text-text-muted">
            口径说明：按《无限暖暖》每日 04:00 游戏日切分并按 UID 隔离存储。第一阶段自动记录幻境能量消耗与确定性产出、星光结晶前后差值、闪光分解粒子、大世界采集拾取与日常进度；含随机掉率结算弹窗的精细逐件识别将在后续阶段补充。
          </p>
        </div>
      </Card>
    </div>
  );
}
