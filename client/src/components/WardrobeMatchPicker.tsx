import { useEffect, useMemo, useState } from 'react';
import { Trophy } from 'lucide-react';
import clsx from 'clsx';
import type { Match } from '@/services/scoring/types';
import { Segmented } from './ui/FeaturePage';

type Category = 'current' | 'past' | 'contest' | 'other';
interface Series { group: string; label: string; region: string; date: string; stages: [string, Match][] }

const CROWN = '奇迹之冠';
const PEAK = '巅峰赛';
const CATEGORIES = [['current', '当期巅峰赛'], ['past', '往期巅峰赛'], ['contest', '搭配赛'], ['other', '其他搭配']] as const;
/** Contests change on the 1st and 16th, so a period lasts at most 16 days. */
const PERIOD_DAYS = 16;

function buildSeries(matches: Record<string, Match>): Series[] {
  const series = new Map<string, Series>();
  for (const entry of Object.entries(matches)) {
    const { group } = entry[1];
    const [region, label = region] = group.split(' / ');
    const row = series.get(group) ?? { group, label, region, date: /\d{4}-\d{2}-\d{2}/.exec(group)?.[0] ?? '', stages: [] };
    row.stages.push(entry);
    series.set(group, row);
  }
  return [...series.values()];
}

const isPeak = (row: Series) => row.region === CROWN && row.label.includes(PEAK);
const categoryOf = (row: Series, latest: Series | undefined): Category =>
  isPeak(row) ? (row === latest ? 'current' : 'past') : row.region === CROWN ? 'contest' : 'other';

/** The peak contest is the entry point; past periods and other contests sit beside it. */
export function WardrobeMatchPicker({ matches, value, onChange }: {
  matches: Record<string, Match>; value: string; onChange: (id: string) => void;
}) {
  const all = useMemo(() => buildSeries(matches), [matches]);
  const peaks = useMemo(() => all.filter(isPeak).sort((a, b) => b.date.localeCompare(a.date)), [all]);
  const latest = peaks[0];
  const selected = all.find((row) => row.stages.some(([id]) => id === value));
  const [category, setCategory] = useState<Category>('current');
  useEffect(() => { if (selected) setCategory(categoryOf(selected, latest)); }, [selected, latest]);

  const inCategory = useMemo(() => all.filter((row) => categoryOf(row, latest) === category)
    .sort((a, b) => b.date.localeCompare(a.date)), [all, latest, category]);
  const series = selected && inCategory.includes(selected) ? selected : inCategory[0];
  const regions = useMemo(() => [...new Set(inCategory.map((row) => row.region))], [inCategory]);
  const stale = !!latest?.date && (Date.now() - Date.parse(latest.date)) / 86400000 >= PERIOD_DAYS;

  function chooseCategory(next: Category) {
    setCategory(next);
    const first = all.filter((row) => categoryOf(row, latest) === next)
      .sort((a, b) => b.date.localeCompare(a.date))[0];
    if (first) onChange(first.stages[0][0]);
  }

  return <section className="rounded-xl border border-border bg-bg-secondary p-4">
    <div className="flex flex-wrap items-center gap-3">
      <Segmented label="比赛类型" value={category} options={CATEGORIES} onChange={chooseCategory} />
      {category === 'current' && latest && <p className="flex min-w-0 items-center gap-1.5 text-sm font-medium">
        <Trophy className="h-4 w-4 shrink-0 text-accent-secondary" /><span className="truncate">{latest.label.replace(/\s*\(.*\)/, '')}</span>
        <span className="shrink-0 text-xs font-normal tabular-nums text-text-muted">{latest.date} 开始</span></p>}
      {category !== 'current' && <select aria-label="选择比赛" value={series?.group ?? ''} onChange={(event) => {
        const row = inCategory.find((item) => item.group === event.target.value);
        if (row) onChange(row.stages[0][0]);
      }} className="min-w-48 rounded-lg border border-border bg-bg-primary px-3 py-1.5 text-sm outline-none focus:border-accent">
        {regions.length > 1
          ? regions.map((region) => <optgroup key={region} label={region}>{inCategory.filter((row) => row.region === region)
            .map((row) => <option key={row.group} value={row.group}>{row.label}</option>)}</optgroup>)
          : inCategory.map((row) => <option key={row.group} value={row.group}>{row.label}</option>)}
      </select>}
    </div>
    {category === 'current' && stale && <p className="mt-2 text-xs text-warning">资料还没有更新到最新一期，这里显示的是已收录的最近一期。</p>}
    <div className="mt-3 grid grid-cols-[repeat(auto-fill,minmax(10.5rem,1fr))] gap-1.5" role="group" aria-label="选择场次">
      {series?.stages.map(([id, stage]) => <button key={id} type="button" onClick={() => onChange(id)} aria-pressed={id === value} title={stage.name}
        className={clsx('min-w-0 truncate rounded-lg border px-2.5 py-2 text-left text-xs transition-colors',
          id === value ? 'border-accent bg-accent/10 font-medium text-text-primary' : 'border-border bg-bg-primary text-text-secondary hover:bg-bg-hover')}>
        {stage.name}
      </button>)}
    </div>
  </section>;
}
