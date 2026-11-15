import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { CalendarClock, Loader2 } from 'lucide-react';
import clsx from 'clsx';
import type { ForecastResult, ForecastSeries } from '@/services/collectionForecast';
import type {
  MaterialGoalForecast,
  QueueScheduleSummary,
  RainbowPreset,
} from '@/services/realmForecast';
import { useForecastAssumptionsStore } from '@/stores/forecastAssumptionsStore';
import { Segmented } from './ui/FeaturePage';

const DAY_MS = 86400000;
const CONTROL =
  'w-full rounded-lg border border-border bg-bg-primary px-2 py-1.5 text-sm tabular-nums outline-none focus:border-accent';

export const dayAfter = (days: number) => new Date(Date.now() + days * DAY_MS);
export const dateLabel = (date: Date) => `${date.getFullYear()} 年 ${date.getMonth() + 1} 月`;
export const shortOrFullDateLabel = (days: number) => {
  const d = dayAfter(days);
  if (days <= 90) return `${d.getMonth() + 1} 月 ${d.getDate()} 日`;
  return dateLabel(d);
};
const fullDate = (date: Date) => `${date.getFullYear()} 年 ${date.getMonth() + 1} 月 ${date.getDate()} 日`;

export const compactNumber = (value: number) =>
  value >= 10000
    ? `${(value / 10000).toLocaleString('zh-CN', { maximumFractionDigits: value >= 1000000 ? 0 : 1 })} 万`
    : Math.round(value).toLocaleString('zh-CN');

export interface ChartSeriesDef {
  id: string;
  label: string;
  unit: string;
  median: number[];
  low?: number[];
  high?: number[];
  top?: number;
  format?: (value: number) => string;
  past?: { day: string; value: number }[];
}

export function UnifiedForecastChart({
  days,
  seriesList,
  metric,
  height = 240,
}: {
  days: number[];
  seriesList: ChartSeriesDef[];
  metric: string;
  height?: number;
}) {
  const box = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(720);
  const [hover, setHover] = useState<number | null>(null);

  useEffect(() => {
    const element = box.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.max(300, entry.contentRect.width)));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const current = seriesList.find((item) => item.id === metric) ?? seriesList[0];
  const past = useMemo(
    () =>
      (current?.past ?? []).map((point) => ({
        x: Math.round((new Date(point.day).getTime() - Date.now()) / DAY_MS),
        y: point.value,
      })),
    [current?.past],
  );

  const margin = { left: 54, right: 16, top: 12, bottom: 26 };
  const start = Math.min(0, ...past.map((point) => point.x));
  const end = Math.max(1, days[days.length - 1] ?? 1);
  const maxVal = Math.max(
    1,
    ...(current?.high ?? current?.median ?? [1]),
    ...past.map((p) => p.y),
  );
  const top = current?.top ?? maxVal * 1.05;

  const scaleX = (day: number) => margin.left + ((day - start) / (end - start)) * (width - margin.left - margin.right);
  const scaleY = (value: number) => margin.top + (1 - Math.min(top, Math.max(0, value)) / top) * (height - margin.top - margin.bottom);

  const line = (values: number[]) =>
    values.map((value, index) => `${index ? 'L' : 'M'}${scaleX(days[index] ?? 0).toFixed(1)} ${scaleY(value).toFixed(1)}`).join('');

  const hasBand = Boolean(current?.low && current?.high && current.low.length === days.length && current.high.length === days.length);
  const band = hasBand
    ? `${line(current!.high!)}L${[...current!.low!]
        .reverse()
        .map((value, index) => `${scaleX(days[days.length - 1 - index] ?? 0).toFixed(1)} ${scaleY(value).toFixed(1)}`)
        .join('L')}Z`
    : '';

  const yTicks = [0, 0.25, 0.5, 0.75, 1].map((share) => top * share);
  const xTicks = useMemo(() => {
    const ticks: { day: number; label: string }[] = [];
    if (end <= 45) {
      const stepDays = end <= 7 ? 1 : end <= 16 ? 3 : 7;
      for (let d = stepDays; d <= end && ticks.length < 8; d += stepDays) {
        const dt = dayAfter(d);
        ticks.push({ day: d, label: `${dt.getMonth() + 1}.${String(dt.getDate()).padStart(2, '0')}` });
      }
      if (!ticks.length || ticks[ticks.length - 1].day < end) {
        const dt = dayAfter(end);
        ticks.push({ day: end, label: `${dt.getMonth() + 1}.${String(dt.getDate()).padStart(2, '0')}` });
      }
      return ticks;
    }
    const months = end <= 150 ? 1 : end <= 400 ? 3 : end <= 900 ? 6 : 12;
    const cursor = new Date();
    cursor.setDate(1);
    cursor.setHours(0, 0, 0, 0);
    cursor.setMonth(cursor.getMonth() + months - (cursor.getMonth() % months));
    while (ticks.length < 12) {
      const day = (cursor.getTime() - Date.now()) / DAY_MS;
      if (day > end) break;
      ticks.push({
        day,
        label: `${String(cursor.getFullYear()).slice(2)}.${String(cursor.getMonth() + 1).padStart(2, '0')}`,
      });
      cursor.setMonth(cursor.getMonth() + months);
    }
    return ticks;
  }, [end]);

  const hovered = hover === null ? null : days.findIndex((day) => day >= hover);
  const pointer = hovered !== null && hovered >= 0 ? hovered : null;
  const formatVal = current?.format ?? compactNumber;

  if (!current) return null;

  return (
    <div ref={box} className="relative">
      <svg
        width={width}
        height={height}
        role="img"
        aria-label={`${current.label}预测：当前 ${formatVal(current.median[0] ?? 0)}`}
        onMouseMove={(event) => {
          const bounds = event.currentTarget.getBoundingClientRect();
          const day =
            start + ((event.clientX - bounds.left - margin.left) / (width - margin.left - margin.right)) * (end - start);
          setHover(day >= 0 && day <= end ? day : null);
        }}
        onMouseLeave={() => setHover(null)}
      >
        {yTicks.map((tick) => (
          <g key={tick}>
            <line
              x1={margin.left}
              x2={width - margin.right}
              y1={scaleY(tick)}
              y2={scaleY(tick)}
              stroke="var(--color-border)"
              strokeWidth={1}
            />
            <text x={margin.left - 6} y={scaleY(tick) + 4} textAnchor="end" fontSize={11} fill="var(--color-text-muted)">
              {formatVal(tick)}
            </text>
          </g>
        ))}
        {xTicks.map((tick) => (
          <text key={`${tick.day}-${tick.label}`} x={scaleX(tick.day)} y={height - 8} textAnchor="middle" fontSize={11} fill="var(--color-text-muted)">
            {tick.label}
          </text>
        ))}
        {band && <path d={band} fill="var(--color-accent)" opacity={0.14} />}
        <path d={line(current.median)} fill="none" stroke="var(--color-accent)" strokeWidth={2} strokeLinejoin="round" />
        {past.length > 1 && (
          <path
            d={past.map((point, index) => `${index ? 'L' : 'M'}${scaleX(point.x).toFixed(1)} ${scaleY(point.y).toFixed(1)}`).join('')}
            fill="none"
            stroke="var(--color-text-primary)"
            strokeWidth={2}
            strokeLinejoin="round"
          />
        )}
        <circle
          cx={scaleX(0)}
          cy={scaleY(current.median[0] ?? 0)}
          r={4}
          fill="var(--color-text-primary)"
          stroke="var(--color-bg-secondary)"
          strokeWidth={2}
        />
        {pointer !== null && (
          <g>
            <line
              x1={scaleX(days[pointer])}
              x2={scaleX(days[pointer])}
              y1={margin.top}
              y2={height - margin.bottom}
              stroke="var(--color-text-muted)"
              strokeWidth={1}
              strokeDasharray="3 3"
            />
            <circle
              cx={scaleX(days[pointer])}
              cy={scaleY(current.median[pointer] ?? 0)}
              r={4}
              fill="var(--color-accent)"
              stroke="var(--color-bg-secondary)"
              strokeWidth={2}
            />
          </g>
        )}
      </svg>
      {pointer !== null && (
        <div
          className="pointer-events-none absolute top-2 z-10 min-w-44 rounded-lg border border-border bg-bg-primary px-2.5 py-1.5 text-xs shadow-md"
          style={
            scaleX(days[pointer]) > width / 2
              ? { right: width - scaleX(days[pointer]) + 10 }
              : { left: scaleX(days[pointer]) + 10 }
          }
        >
          <p className="font-medium tabular-nums">
            {fullDate(dayAfter(days[pointer]))}
            <span className="ml-1.5 text-[11px] font-normal text-text-muted">第 {days[pointer]} 天</span>
          </p>
          <dl className="mt-1 space-y-0.5 tabular-nums">
            {seriesList.map((item) => {
              const fmt = item.format ?? compactNumber;
              return (
                <div
                  key={item.id}
                  className={clsx(
                    'flex justify-between gap-4',
                    item.id === current.id ? 'font-medium text-text-primary' : 'text-text-secondary',
                  )}
                >
                  <dt>{item.label}</dt>
                  <dd>
                    约 {fmt(item.median[pointer] ?? 0)}
                    {item.unit}
                  </dd>
                </div>
              );
            })}
          </dl>
          {hasBand && (
            <p className="mt-1 tabular-nums text-text-muted">
              {current.label}八成区间 {formatVal(current.low![pointer] ?? 0)}～{formatVal(current.high![pointer] ?? 0)}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

/** Shared assumption controls synced across 制衣 / 焕新 / 祝福闪光 / 幻境挑战. */
function SharedAssumptionsBar({
  showIncome = true,
  extraControls,
  note,
}: {
  showIncome?: boolean;
  extraControls?: ReactNode;
  note: ReactNode;
}) {
  const { dailyEnergy, dailyBlingIncome, setDailyEnergy, setDailyBlingIncome } = useForecastAssumptionsStore();
  return (
    <details className="mt-3 rounded-lg border border-border px-3 py-2">
      <summary className="cursor-pointer text-sm">调整假设，试试“如果……”（与幻境挑战列表实时联动）</summary>
      <div className="mt-3 space-y-3">
        {extraControls}
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <label className="space-y-1 text-xs text-text-secondary">
            <span>每天活跃能量</span>
            <input
              className={CONTROL}
              type="number"
              min={10}
              step={10}
              value={dailyEnergy}
              onChange={(event) => setDailyEnergy(Math.max(10, Number(event.target.value) || 10))}
            />
          </label>
          {showIncome && (
            <label className="space-y-1 text-xs text-text-secondary">
              <span>每天日常噗灵收入</span>
              <input
                className={CONTROL}
                type="number"
                min={0}
                step={10000}
                value={dailyBlingIncome}
                onChange={(event) => setDailyBlingIncome(Math.max(0, Number(event.target.value) || 0))}
              />
            </label>
          )}
        </div>
        <p className="text-xs leading-5 text-text-muted">{note}</p>
      </div>
    </details>
  );
}

const formatPct = (v: number) => `${Math.round(v)}%`;

/** Unified future-trend & energy forecast panel for 焕新 and 制衣. */
export function MaterialForecastPanel({
  forecast,
  extraEnergy,
  onExtraEnergyChange,
  queueRank,
  queuedFinishDay,
  precedingNames,
}: {
  forecast: MaterialGoalForecast;
  extraEnergy: number;
  onExtraEnergyChange: (value: number) => void;
  queueRank: number | null;
  queuedFinishDay: number | null;
  precedingNames: string[];
}) {
  const { dailyEnergy, dailyBlingIncome } = useForecastAssumptionsStore();
  const isCrafting = forecast.kind === 'crafting';
  const [metric, setMetric] = useState<'progress' | 'energy' | 'remaining' | 'units' | 'secondary'>('progress');

  const seriesList = useMemo<ChartSeriesDef[]>(() => {
    const t = forecast.trend;
    const progressPast = forecast.history.map((pt) => ({ day: pt.day, value: pt.progressPct }));
    const energyPast = forecast.history.map((pt) => ({ day: pt.day, value: pt.energy }));
    return [
      {
        id: 'progress',
        label: isCrafting ? '晶石进度' : '备齐进度',
        unit: '',
        median: t.progressPct,
        top: 100,
        format: formatPct,
        past: progressPast,
      },
      {
        id: 'energy',
        label: '累计消耗体力',
        unit: ' 体力',
        median: t.cumulativeEnergy,
      },
      {
        id: 'remaining',
        label: '剩余所需体力',
        unit: ' 体力',
        median: t.remainingEnergy,
        past: energyPast,
      },
      {
        id: 'units',
        label: isCrafting ? '剩余晶石缺口' : '剩余泡泡缺口',
        unit: ' 个',
        median: t.missingUnits,
      },
      {
        id: 'secondary',
        label: isCrafting ? '含噗灵/丝线折算' : '剩余噗灵缺口',
        unit: isCrafting ? ' 体力' : '',
        median: t.secondaryMetric,
      },
    ];
  }, [forecast.trend, forecast.history, isCrafting]);

  if (forecast.status === 'needs_wardrobe') return null;

  const done = forecast.energy === 0;
  const hasPast = seriesList.some((item) => item.id === metric && (item.past?.length ?? 0) > 1);
  const queueDelta =
    queueRank !== null && queuedFinishDay !== null && queuedFinishDay > forecast.days ? queuedFinishDay : null;

  return (
    <section
      className="rounded-xl border border-border bg-bg-secondary p-4"
      aria-label={isCrafting ? '制衣材料时间与体力预估' : '焕新材料时间与体力预估'}
    >
      <div className="flex flex-wrap items-end gap-x-8 gap-y-3">
        <div>
          <p className="text-xs text-text-secondary">{isCrafting ? '预计试炼晶石备齐' : '预计焕新材料备齐'}</p>
          <p className="mt-1 text-2xl font-semibold tabular-nums">
            {done
              ? '已备齐'
              : forecast.trend.exceededHorizon
                ? '10 年以上'
                : shortOrFullDateLabel(forecast.days)}
          </p>
          {!done && !forecast.trend.exceededHorizon && (
            <p className="mt-0.5 text-xs tabular-nums text-text-secondary">
              自身约 {forecast.days} 天（按每日 {dailyEnergy} 能量）
              {queueDelta !== null && (
                <span className="ml-1.5 text-accent">
                  · 按幻境队列顺位约第 {queueDelta} 天完成（{shortOrFullDateLabel(queueDelta)}）
                </span>
              )}
            </p>
          )}
        </div>

        <div>
          <p className="text-xs text-text-secondary">预计消耗活跃能量</p>
          <p className="mt-1 text-2xl font-semibold tabular-nums text-accent">
            {done ? '0 体力' : `约 ${compactNumber(forecast.energy)} 体力`}
          </p>
          <p className="mt-0.5 text-xs tabular-nums text-text-secondary">
            {isCrafting
              ? forecast.allTrackedEnergy > forecast.energy
                ? `幻境刷晶石 ${forecast.runs} 次 · 若含噗灵与丝线折算共约 ${compactNumber(forecast.allTrackedEnergy)} 体力`
                : `共 ${forecast.runs} 次魔物试炼（不含需自备的大世界采集物）`
              : forecast.blingSavedEnergy > 0
                ? `共 ${forecast.runs} 次挑战 · 日常噗灵收入抵扣约 ${compactNumber(forecast.blingSavedEnergy)} 体力（纯直刷 ${compactNumber(forecast.rawEnergy)} 体力）`
                : `共 ${forecast.runs} 次幻境挑战`}
          </p>
        </div>

        <div className="ml-auto flex items-center gap-4 pb-1 text-xs text-text-secondary">
          {hasPast && (
            <span className="flex items-center gap-1.5">
              <span className="h-0.5 w-4 rounded-lg bg-text-primary" />
              实际
            </span>
          )}
          <span className="flex items-center gap-1.5">
            <span className="h-0.5 w-4 rounded-lg bg-accent" />
            预计走势
          </span>
        </div>
      </div>

      {queueRank !== null && precedingNames.length > 0 && !done && (
        <p className="mt-2 flex items-center gap-1.5 text-xs text-text-secondary">
          <CalendarClock className="h-3.5 w-3.5 shrink-0 text-accent" />
          当前在幻境挑战中排第 {queueRank} 位，需先处理「{precedingNames.join('」「')}」；上方时间曲线按自身满体力投入绘制，排队完成日同步显示在幻境挑战列表。
        </p>
      )}

      <div className="mt-3">
        <Segmented
          label="预估走势"
          value={metric}
          onChange={setMetric}
          options={seriesList.map((item) => [item.id as typeof metric, item.label] as const)}
        />
      </div>

      <div className="mt-2">
        <UnifiedForecastChart days={forecast.trend.days} seriesList={seriesList} metric={metric} />
      </div>

      {forecast.trend.milestones.length > 0 && (
        <ol className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
          {forecast.trend.milestones
            .slice()
            .sort((a, b) => (a.day ?? Infinity) - (b.day ?? Infinity))
            .slice(0, 5)
            .map((item) => (
              <li key={item.key} className="rounded-lg bg-bg-primary px-3 py-2">
                <p className="text-sm font-medium tabular-nums">
                  {item.day === null ? '—' : item.day === 0 ? '今天' : shortOrFullDateLabel(item.day)}
                </p>
                <p className="mt-0.5 text-xs leading-5 text-text-secondary">{item.label}</p>
                {item.day !== null && item.energy !== null && (
                  <p className="text-[11px] tabular-nums text-text-muted">
                    第 {item.day} 天 · 约 {compactNumber(item.energy)} 体力
                  </p>
                )}
              </li>
            ))}
        </ol>
      )}

      <SharedAssumptionsBar
        extraControls={
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <label className="space-y-1 text-xs text-text-secondary">
              <span>如果额外投入体力</span>
              <input
                className={CONTROL}
                type="number"
                min={0}
                step={40}
                value={extraEnergy}
                onChange={(event) => onExtraEnergyChange(Math.max(0, Number(event.target.value) || 0))}
              />
            </label>
          </div>
        }
        note={
          isCrafting
            ? '按当前已读衣柜与背包库存模拟每日优先刷取缺口最大的魔物试炼晶石（40 体力 / 5 个）。大世界采集物与活动赠送未计入。'
            : `按“优先按体力缺口刷取各类泡泡与纯真丝线，最后以素材激化补噗灵（每日日常噗灵收入 ${compactNumber(dailyBlingIncome)} 自动折抵）”模拟。`
        }
      />
    </section>
  );
}

/** Unified Blessing forecast panel (supports standalone & queue-aware schedule + total energy KPI). */
export function BlessingForecastPanel({
  standalone,
  queued,
  busy,
  presets,
  chosenPresetId,
  history,
  reserveBling,
  extraBling,
  extraParticles,
  onExtraBlingChange,
  onExtraParticlesChange,
  queueRank,
  precedingNames,
  done,
  blockedByCapacity,
  isFarmOnly,
}: {
  standalone: ForecastResult | null;
  queued: ForecastResult | null;
  busy: boolean;
  presets: RainbowPreset[];
  chosenPresetId: string;
  history: { day: string; owned: number }[];
  reserveBling: number;
  extraBling: number;
  extraParticles: number;
  onExtraBlingChange: (value: number) => void;
  onExtraParticlesChange: (value: number) => void;
  queueRank: number | null;
  precedingNames: string[];
  done: boolean;
  blockedByCapacity: boolean;
  isFarmOnly: boolean;
}) {
  const { dailyEnergy, rainbowPreset, useQueueOrder, setRainbowPreset, setUseQueueOrder } =
    useForecastAssumptionsStore();
  const [metric, setMetric] = useState<'colors' | 'energy' | 'bling' | 'particles' | 'embryos' | 'capacity'>('colors');

  const hasPreceding = queueRank !== null && precedingNames.length > 0;
  const activeResult = hasPreceding && useQueueOrder ? (queued ?? standalone) : standalone;
  const chosen = presets.find((item) => item.id === rainbowPreset) ?? presets.find((item) => item.id === chosenPresetId) ?? presets[0];

  const seriesList = useMemo<ChartSeriesDef[]>(() => {
    if (!activeResult) return [];
    const pick = (s: ForecastSeries) => ({ low: s.low, median: s.median, high: s.high });
    return [
      {
        id: 'colors',
        label: '颜色',
        unit: ' 色',
        low: activeResult.low,
        median: activeResult.median,
        high: activeResult.high,
        top: activeResult.total || 1,
        past: history.map((point) => ({ day: point.day, value: point.owned })),
      },
      {
        id: 'energy',
        label: '累计体力',
        unit: ' 体力',
        ...pick(activeResult.resources.energy),
      },
      {
        id: 'bling',
        label: '噗灵',
        unit: '',
        ...pick(activeResult.resources.bling),
      },
      {
        id: 'particles',
        label: '可用粒子',
        unit: '',
        ...pick(activeResult.resources.particles),
      },
      {
        id: 'embryos',
        label: '待升胚子',
        unit: ' 个',
        ...pick(activeResult.resources.embryos),
      },
      {
        id: 'capacity',
        label: '仓库',
        unit: ' 格',
        ...pick(activeResult.resources.capacity),
      },
    ];
  }, [activeResult, history]);

  const all = activeResult?.milestones.find((item) => item.key === 'all');
  const energyStat = activeResult?.totalEnergy;

  if (done) {
    return (
      <section className="rounded-xl border border-border bg-bg-secondary p-4" aria-label="全收集时间与体力预估">
        <div className="flex flex-wrap items-center gap-6">
          <div>
            <p className="text-xs text-text-secondary">预计收集完成</p>
            <p className="mt-1 text-2xl font-semibold text-success">所选范围已收齐</p>
          </div>
          <div>
            <p className="text-xs text-text-secondary">预计消耗活跃能量</p>
            <p className="mt-1 text-2xl font-semibold tabular-nums">0 体力</p>
          </div>
        </div>
      </section>
    );
  }

  return (
    <section className="rounded-xl border border-border bg-bg-secondary p-4" aria-label="全收集时间与体力预估">
      <div className="flex flex-wrap items-end gap-x-8 gap-y-3">
        <div>
          <p className="text-xs text-text-secondary">
            {hasPreceding && useQueueOrder ? '按当前幻境顺序预计完成' : '预计收集完成'}
          </p>
          <p className="mt-1 flex items-center gap-2 text-2xl font-semibold tabular-nums">
            {blockedByCapacity
              ? '仓库已满，需先开启分解或清理'
              : all?.day != null
                ? shortOrFullDateLabel(all.day)
                : activeResult
                  ? '10 年以上'
                  : '计算中'}
            {busy && <Loader2 className="h-4 w-4 animate-spin text-text-muted" />}
          </p>
          {all?.day != null && (
            <p className="mt-0.5 text-xs tabular-nums text-text-secondary">
              约 {all.day} 天，八成概率在 {all.low}～{all.high} 天之间（每日 {dailyEnergy} 能量）
            </p>
          )}
        </div>

        <div>
          <p className="text-xs text-text-secondary">预计消耗活跃能量</p>
          <p className="mt-1 flex items-center gap-2 text-2xl font-semibold tabular-nums text-accent">
            {blockedByCapacity
              ? '—'
              : energyStat?.median != null
                ? `约 ${compactNumber(energyStat.median)} 体力`
                : activeResult
                  ? '待定'
                  : '计算中'}
          </p>
          {energyStat?.low != null && energyStat?.high != null && (
            <p className="mt-0.5 text-xs tabular-nums text-text-secondary">
              八成概率在 {compactNumber(energyStat.low)}～{compactNumber(energyStat.high)} 体力之间
              {isFarmOnly ? ' · 只刷胚子模式按自主升满估算' : ''}
            </p>
          )}
        </div>

        <div className="ml-auto flex items-center gap-4 pb-1 text-xs text-text-secondary">
          <span className="flex items-center gap-1.5">
            <span className="h-0.5 w-4 rounded-lg bg-text-primary" />
            实际
          </span>
          <span className="flex items-center gap-1.5">
            <span className="h-0.5 w-4 rounded-lg bg-accent" />
            预估中位
          </span>
          <span className="flex items-center gap-1.5">
            <span className="h-2.5 w-4 rounded-sm bg-accent/15" />
            八成区间
          </span>
        </div>
      </div>

      {hasPreceding && (
        <div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-xs text-text-secondary">
          <span className="flex items-center gap-1.5">
            <CalendarClock className="h-3.5 w-3.5 shrink-0 text-accent" />
            幻境挑战中「{precedingNames.join('」「')}」排在五星闪光前面
          </span>
          <label className="inline-flex cursor-pointer items-center gap-1.5">
            <input
              type="checkbox"
              checked={useQueueOrder}
              onChange={(event) => setUseQueueOrder(event.target.checked)}
              className="h-3.5 w-3.5 accent-accent"
            />
            计入前面目标的体力占用
          </label>
        </div>
      )}

      <div className="mt-3">
        <Segmented
          label="预估走势"
          value={metric}
          onChange={setMetric}
          options={seriesList.map((item) => [item.id as typeof metric, item.label] as const)}
        />
      </div>

      <div className="mt-2">
        {activeResult && seriesList.length > 0 ? (
          <UnifiedForecastChart days={activeResult.days} seriesList={seriesList} metric={metric} height={260} />
        ) : (
          <div className="h-[260px]" />
        )}
      </div>

      {activeResult && (
        <ol className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
          {[...activeResult.milestones]
            .sort((a, b) => (a.day ?? Infinity) - (b.day ?? Infinity))
            .map((item) => (
              <li key={item.key} className="rounded-lg bg-bg-primary px-3 py-2">
                <p className="text-sm font-medium tabular-nums">
                  {item.day === null ? '—' : shortOrFullDateLabel(item.day)}
                </p>
                <p className="mt-0.5 text-xs leading-5 text-text-secondary">{item.label}</p>
                {item.day !== null && item.energy != null && (
                  <p className="text-[11px] tabular-nums text-text-muted">
                    约 {compactNumber(item.energy)} 体力
                  </p>
                )}
              </li>
            ))}
        </ol>
      )}

      <SharedAssumptionsBar
        extraControls={
          <>
            <div>
              <p className="mb-1.5 text-xs text-text-secondary">彩色概率（官方未公布）</p>
              <div className="flex flex-wrap gap-1.5">
                {presets.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() => setRainbowPreset(item.id)}
                    aria-pressed={item.id === chosen?.id}
                    title={item.note}
                    className={clsx(
                      'rounded-lg border px-2.5 py-1 text-xs tabular-nums',
                      item.id === chosen?.id ? 'border-accent bg-accent/10 text-accent' : 'border-border hover:bg-bg-hover',
                    )}
                  >
                    {item.label} {(item.rate * 100).toFixed(1)}%
                  </button>
                ))}
              </div>
              {chosen && <p className="mt-1.5 text-xs text-text-muted">{chosen.note}</p>}
            </div>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <label className="space-y-1 text-xs text-text-secondary">
                <span>如果额外获得噗灵</span>
                <input
                  className={CONTROL}
                  type="number"
                  min={0}
                  step={1000000}
                  value={extraBling}
                  onChange={(event) => onExtraBlingChange(Math.max(0, Number(event.target.value) || 0))}
                />
              </label>
              <label className="space-y-1 text-xs text-text-secondary">
                <span>如果额外获得粒子</span>
                <input
                  className={CONTROL}
                  type="number"
                  min={0}
                  step={1000}
                  value={extraParticles}
                  onChange={(event) => onExtraParticlesChange(Math.max(0, Number(event.target.value) || 0))}
                />
              </label>
            </div>
          </>
        }
        note={`模拟 150 次“每天先升级，升不起就刷噗灵，否则刷本”的流程${reserveBling ? `（已按保留 ${compactNumber(reserveBling)} 噗灵计算）` : ''}。未计入活动加成、礼包和版本新增套组，结果只作参考。`}
      />
    </section>
  );
}

/** Compact queue-level future trend & milestone view embedded inside RealmQueueCard. */
export function QueueCombinedTrendPanel({ summary }: { summary: QueueScheduleSummary }) {
  const [metric, setMetric] = useState<'cumulative' | 'remaining'>('cumulative');
  const seriesList = useMemo<ChartSeriesDef[]>(
    () => [
      {
        id: 'cumulative',
        label: '队列累计消耗体力',
        unit: ' 体力',
        median: summary.combinedTrend.cumulativeEnergy,
      },
      {
        id: 'remaining',
        label: '队列剩余待刷体力',
        unit: ' 体力',
        median: summary.combinedTrend.remainingEnergy,
      },
    ],
    [summary.combinedTrend],
  );

  return (
    <div className="space-y-2.5 rounded-lg border border-border bg-bg-secondary/60 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-xs font-semibold text-text-primary">当前队列未来趋势预估</span>
        <Segmented
          label="队列趋势指标"
          value={metric}
          onChange={setMetric}
          options={[
            ['cumulative', '累计体力'],
            ['remaining', '剩余体力'],
          ]}
        />
      </div>
      <UnifiedForecastChart days={summary.combinedTrend.days} seriesList={seriesList} metric={metric} height={175} />
      {summary.combinedTrend.milestones.length > 0 && (
        <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-4">
          {summary.combinedTrend.milestones.map((m) => (
            <div key={m.key} className="rounded-md bg-bg-primary px-2.5 py-1.5 text-[11px]">
              <p className="font-medium text-text-primary tabular-nums">
                {m.day === null ? '—' : m.day === 0 ? '已完成' : `第 ${m.day} 天 · ${shortOrFullDateLabel(m.day)}`}
              </p>
              <p className="truncate text-text-secondary">{m.label}</p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
