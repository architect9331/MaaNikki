import { useEffect, useMemo, useRef, useState } from 'react';
import { Loader2 } from 'lucide-react';
import clsx from 'clsx';
import type { ForecastInput, ForecastResult } from '@/services/collectionForecast';

export interface RainbowPreset { id: string; label: string; rate: number; note: string }
export interface HistoryPoint { day: string; owned: number }
export const dayAfter = (days: number) => new Date(Date.now() + days * 86400000);
export const dateLabel = (date: Date) => `${date.getFullYear()} 年 ${date.getMonth() + 1} 月`;
const DAY_MS = 86400000;
const SAVED = 'maanikki.collectionForecast.v1';

function loadSaved() {
  try { return JSON.parse(localStorage.getItem(SAVED) ?? '{}') as Record<string, number | string>; } catch { return {}; }
}

function ForecastChart({ result, history }: { result: ForecastResult; history: HistoryPoint[] }) {
  const box = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(720);
  const [hover, setHover] = useState<number | null>(null);
  useEffect(() => {
    const element = box.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.max(320, entry.contentRect.width)));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const height = 260;
  const margin = { left: 38, right: 14, top: 12, bottom: 26 };
  const past = history.map((point) => ({ x: Math.round((new Date(point.day).getTime() - Date.now()) / DAY_MS), y: point.owned }));
  const start = Math.min(0, ...past.map((point) => point.x));
  const end = Math.max(1, result.days[result.days.length - 1]);
  const scaleX = (day: number) => margin.left + (day - start) / (end - start) * (width - margin.left - margin.right);
  const scaleY = (value: number) => margin.top + (1 - value / (result.total || 1)) * (height - margin.top - margin.bottom);
  const line = (values: number[]) => values.map((value, index) => `${index ? 'L' : 'M'}${scaleX(result.days[index]).toFixed(1)} ${scaleY(value).toFixed(1)}`).join('');
  const band = `${line(result.high)}L${[...result.low].reverse().map((value, index) => `${scaleX(result.days[result.days.length - 1 - index]).toFixed(1)} ${scaleY(value).toFixed(1)}`).join('L')}Z`;
  const yTicks = [0, 0.25, 0.5, 0.75, 1].map((share) => Math.round(result.total * share));
  const xTicks = useMemo(() => {
    const months = end <= 400 ? 3 : end <= 900 ? 6 : 12;
    const ticks: { day: number; label: string }[] = [];
    const cursor = new Date(); cursor.setDate(1); cursor.setHours(0, 0, 0, 0);
    cursor.setMonth(cursor.getMonth() + months - cursor.getMonth() % months);
    while (ticks.length < 12) {
      const day = (cursor.getTime() - Date.now()) / DAY_MS;
      if (day > end) break;
      ticks.push({ day, label: `${String(cursor.getFullYear()).slice(2)}.${String(cursor.getMonth() + 1).padStart(2, '0')}` });
      cursor.setMonth(cursor.getMonth() + months);
    }
    return ticks;
  }, [end]);
  const hovered = hover === null ? null : result.days.findIndex((day) => day >= hover);
  const pointer = hovered !== null && hovered >= 0 ? hovered : null;
  return <div ref={box} className="relative">
    <svg width={width} height={height} role="img" aria-label={`收集进度预测：当前 ${result.owned} 色，目标 ${result.total} 色`}
      onMouseMove={(event) => {
        const bounds = event.currentTarget.getBoundingClientRect();
        const day = start + (event.clientX - bounds.left - margin.left) / (width - margin.left - margin.right) * (end - start);
        setHover(day >= 0 && day <= end ? day : null);
      }} onMouseLeave={() => setHover(null)}>
      {yTicks.map((tick) => <g key={tick}>
        <line x1={margin.left} x2={width - margin.right} y1={scaleY(tick)} y2={scaleY(tick)} stroke="var(--color-border)" strokeWidth={1} />
        <text x={margin.left - 6} y={scaleY(tick) + 4} textAnchor="end" fontSize={11} fill="var(--color-text-muted)">{tick}</text>
      </g>)}
      {xTicks.map((tick) => <text key={tick.label} x={scaleX(tick.day)} y={height - 8} textAnchor="middle" fontSize={11} fill="var(--color-text-muted)">{tick.label}</text>)}
      <path d={band} fill="var(--color-accent)" opacity={0.14} />
      <path d={line(result.median)} fill="none" stroke="var(--color-accent)" strokeWidth={2} strokeLinejoin="round" />
      {past.length > 1 && <path d={past.map((point, index) => `${index ? 'L' : 'M'}${scaleX(point.x).toFixed(1)} ${scaleY(point.y).toFixed(1)}`).join('')} fill="none" stroke="var(--color-text-primary)" strokeWidth={2} strokeLinejoin="round" />}
      <circle cx={scaleX(0)} cy={scaleY(result.owned)} r={4} fill="var(--color-text-primary)" stroke="var(--color-bg-secondary)" strokeWidth={2} />
      {pointer !== null && <g>
        <line x1={scaleX(result.days[pointer])} x2={scaleX(result.days[pointer])} y1={margin.top} y2={height - margin.bottom} stroke="var(--color-text-muted)" strokeWidth={1} />
        <circle cx={scaleX(result.days[pointer])} cy={scaleY(result.median[pointer])} r={4} fill="var(--color-accent)" stroke="var(--color-bg-secondary)" strokeWidth={2} />
      </g>}
    </svg>
    {pointer !== null && <div className="pointer-events-none absolute top-2 rounded-md border border-border bg-bg-primary px-2.5 py-1.5 text-xs shadow-md"
      style={scaleX(result.days[pointer]) > width / 2 ? { right: width - scaleX(result.days[pointer]) + 10 } : { left: scaleX(result.days[pointer]) + 10 }}>
      <p className="font-medium">{dateLabel(dayAfter(result.days[pointer]))}</p>
      <p className="mt-0.5 tabular-nums text-text-secondary">约 {result.median[pointer]} 色（{result.low[pointer]}～{result.high[pointer]}）</p>
    </div>}
  </div>;
}

const CONTROL = 'w-full rounded-md border border-border bg-bg-primary px-2 py-1.5 text-sm tabular-nums outline-none focus:border-accent';

export function BlessingForecast({ input, presets, defaultPreset, history }: {
  input: Omit<ForecastInput, 'rainbow' | 'runsPerDay' | 'blingPerDay' | 'blingPerRun'> & { blingPerRun: number };
  presets: RainbowPreset[]; defaultPreset: string; history: HistoryPoint[];
}) {
  const saved = useMemo(loadSaved, []);
  const [preset, setPreset] = useState(String(saved.preset ?? ''));
  const [energy, setEnergy] = useState(Number(saved.energy ?? 280));
  const [income, setIncome] = useState(Number(saved.income ?? 76000));
  const [extraBling, setExtraBling] = useState(0);
  const [extraParticles, setExtraParticles] = useState(0);
  const [result, setResult] = useState<ForecastResult | null>(null);
  const [busy, setBusy] = useState(true);
  const chosen = presets.find((item) => item.id === preset) ?? presets.find((item) => item.id === defaultPreset) ?? presets[0];
  const rainbow = chosen?.rate ?? 0.054;
  const key = JSON.stringify({ input, rainbow, energy, income, extraBling, extraParticles });

  useEffect(() => {
    try { localStorage.setItem(SAVED, JSON.stringify({ preset, energy, income })); } catch { /* Convenience only. */ }
  }, [preset, energy, income]);
  useEffect(() => {
    setBusy(true);
    const worker = new Worker(new URL('../services/collectionForecast.worker.ts', import.meta.url), { type: 'module' });
    const timer = setTimeout(() => worker.postMessage({
      ...input, rainbow, runsPerDay: energy / 40, blingPerDay: income,
      bling: input.bling + extraBling, particles: input.particles + extraParticles,
    } satisfies ForecastInput), 150);
    worker.onmessage = (event: MessageEvent<{ result?: ForecastResult }>) => {
      if (event.data.result) setResult(event.data.result);
      setBusy(false);
      worker.terminate();
    };
    worker.onerror = () => { setBusy(false); worker.terminate(); };
    return () => { clearTimeout(timer); worker.terminate(); };
    // The serialized key covers every input to the simulation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const all = result?.milestones.find((item) => item.key === 'all');
  const left = result ? result.total - result.owned : 0;
  return <section className="rounded-xl border border-border bg-bg-secondary p-4" aria-label="全收集时间预估">
    <div className="flex flex-wrap items-end gap-x-6 gap-y-2">
      <div>
        <p className="text-xs text-text-secondary">预计全收集完成</p>
        <p className="mt-1 flex items-center gap-2 text-2xl font-semibold tabular-nums">
          {!left && result ? '已完成' : all?.day != null ? dateLabel(dayAfter(all.day)) : result ? '10 年以上' : '计算中'}
          {busy && <Loader2 className="h-4 w-4 animate-spin text-text-muted" />}
        </p>
      </div>
      {all?.day != null && <p className="pb-1 text-xs text-text-secondary tabular-nums">约 {all.day} 天，八成概率在 {all.low}～{all.high} 天之间</p>}
      <div className="ml-auto flex items-center gap-4 pb-1 text-xs text-text-secondary">
        <span className="flex items-center gap-1.5"><span className="h-0.5 w-4 rounded bg-text-primary" />实际</span>
        <span className="flex items-center gap-1.5"><span className="h-0.5 w-4 rounded bg-accent" />预估中位</span>
        <span className="flex items-center gap-1.5"><span className="h-2.5 w-4 rounded-sm bg-accent/15" />八成区间</span>
      </div>
    </div>
    <div className="mt-3">{result ? <ForecastChart result={result} history={history} /> : <div className="h-[260px]" />}</div>
    {result && <ol className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-5">{[...result.milestones].sort((a, b) => (a.day ?? Infinity) - (b.day ?? Infinity)).map((item) =>
      <li key={item.key} className="rounded-lg bg-bg-primary px-3 py-2">
        <p className="text-sm font-medium tabular-nums">{item.day === null ? '—' : dateLabel(dayAfter(item.day))}</p>
        <p className="mt-0.5 text-xs leading-5 text-text-secondary">{item.label}</p>
      </li>)}</ol>}
    <details className="mt-3 rounded-lg border border-border px-3 py-2">
      <summary className="cursor-pointer text-sm">调整假设，试试“如果……”</summary>
      <div className="mt-3 space-y-3">
        <div><p className="mb-1.5 text-xs text-text-secondary">彩色概率（官方未公布）</p>
          <div className="flex flex-wrap gap-1.5">{presets.map((item) => <button key={item.id} onClick={() => setPreset(item.id)} aria-pressed={item.id === chosen?.id} title={item.note}
            className={clsx('rounded-md border px-2.5 py-1 text-xs tabular-nums', item.id === chosen?.id ? 'border-accent bg-accent/10 text-accent' : 'border-border hover:bg-bg-hover')}>
            {item.label} {(item.rate * 100).toFixed(1)}%</button>)}</div>
          {chosen && <p className="mt-1.5 text-xs text-text-muted">{chosen.note}</p>}
        </div>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <label className="space-y-1 text-xs text-text-secondary"><span>每天活跃能量</span><input className={CONTROL} type="number" min={40} step={40} value={energy} onChange={(event) => setEnergy(Math.max(40, Number(event.target.value) || 40))} /></label>
          <label className="space-y-1 text-xs text-text-secondary"><span>每天日常噗灵收入</span><input className={CONTROL} type="number" min={0} step={10000} value={income} onChange={(event) => setIncome(Math.max(0, Number(event.target.value) || 0))} /></label>
          <label className="space-y-1 text-xs text-text-secondary"><span>如果额外获得噗灵</span><input className={CONTROL} type="number" min={0} step={1000000} value={extraBling} onChange={(event) => setExtraBling(Math.max(0, Number(event.target.value) || 0))} /></label>
          <label className="space-y-1 text-xs text-text-secondary"><span>如果额外获得粒子</span><input className={CONTROL} type="number" min={0} step={1000} value={extraParticles} onChange={(event) => setExtraParticles(Math.max(0, Number(event.target.value) || 0))} /></label>
        </div>
        <p className="text-xs leading-5 text-text-muted">模拟 150 次“每天先升级，升不起就刷噗灵，否则刷本”的流程。未计入活动加成、礼包和版本新增套组，结果只作参考。</p>
      </div>
    </details>
  </section>;
}
