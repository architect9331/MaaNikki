import { useEffect, useMemo, useRef, useState } from 'react';
import { ChevronDown, ChevronLeft, ChevronRight, ChevronUp, Search } from 'lucide-react';
import clsx from 'clsx';
import type { Match } from '@/services/scoring/types';

interface Series { id: string; stages: [string, Match][] }

/** Series keep catalog order; dated contests sort newest first within a region. */
function buildTree(matches: Record<string, Match>) {
  const regions = new Map<string, Map<string, Series>>();
  for (const entry of Object.entries(matches)) {
    const [region, series = region] = entry[1].group.split(' / ');
    const group = regions.get(region) ?? new Map<string, Series>();
    regions.set(region, group);
    const row = group.get(series) ?? { id: entry[1].group, stages: [] };
    row.stages.push(entry);
    group.set(series, row);
  }
  return [...regions].map(([region, series]) => ({ region, series: [...series].sort((a, b) => {
    const dated = (name: string) => /\d{4}-\d{2}-\d{2}/.exec(name)?.[0] ?? '';
    return dated(b[0]).localeCompare(dated(a[0]));
  }) }));
}

export function WardrobeMatchPicker({ matches, value, onChange }: {
  matches: Record<string, Match>; value: string; onChange: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const root = useRef<HTMLDivElement>(null);
  const tree = useMemo(() => buildTree(matches), [matches]);
  const current = matches[value];
  const siblings = useMemo(() => Object.keys(matches).filter((id) => matches[id].group === current?.group), [matches, current?.group]);
  const position = siblings.indexOf(value);
  const needle = query.trim().toLowerCase();

  useEffect(() => {
    if (!open) return;
    setExpanded(new Set(current ? [current.group] : []));
    const close = (event: MouseEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false); };
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, [open, current]);

  const choose = (id: string) => { onChange(id); setOpen(false); setQuery(''); };
  return <div ref={root} className="relative">
    <div className="flex items-stretch gap-1.5">
      <button type="button" aria-expanded={open} onClick={() => setOpen((previous) => !previous)}
        className="min-w-0 flex-1 rounded-lg border border-border bg-bg-primary px-3 py-2 text-left hover:border-accent/60">
        <span className="flex items-center gap-2"><span className="min-w-0 flex-1">
          <span className="block truncate text-[11px] text-text-muted">{current?.group ?? '选择关卡'}</span>
          <span className="mt-0.5 block truncate text-sm font-medium">{current?.name ?? '未选择'}</span></span>
        {open ? <ChevronUp className="h-4 w-4 shrink-0 text-text-muted" /> : <ChevronDown className="h-4 w-4 shrink-0 text-text-muted" />}</span>
      </button>
      {([[-1, ChevronLeft, '上一场'], [1, ChevronRight, '下一场']] as const).map(([step, Icon, label]) =>
        <button key={label} type="button" aria-label={label} title={label} disabled={!siblings[position + step]}
          onClick={() => onChange(siblings[position + step])}
          className="rounded-lg border border-border px-1.5 hover:bg-bg-hover disabled:opacity-30"><Icon className="h-4 w-4" /></button>)}
    </div>
    {open && <div className="absolute left-0 right-0 top-full z-20 mt-1 overflow-hidden rounded-lg border border-border bg-bg-primary shadow-lg">
      <label className="flex items-center gap-2 border-b border-border px-3 py-2"><Search className="h-3.5 w-3.5 text-text-muted" />
        <input autoFocus aria-label="搜索关卡" placeholder="搜索关卡或系列" value={query} onChange={(event) => setQuery(event.target.value)}
          className="w-full bg-transparent text-sm outline-none" /></label>
      <div className="max-h-96 overflow-auto py-1 text-sm">
        {tree.map(({ region, series }) => {
          const rows = series.map(([name, row]) => ({ name, row, stages: needle && !`${region} ${name}`.toLowerCase().includes(needle)
            ? row.stages.filter(([, stage]) => stage.name.toLowerCase().includes(needle)) : row.stages }))
            .filter((row) => row.stages.length);
          if (!rows.length) return null;
          return <div key={region}><p className="px-3 pb-1 pt-2 text-[11px] font-medium text-text-muted">{region}</p>
            {rows.map(({ name, row, stages }) => {
              const show = !!needle || expanded.has(row.id);
              return <div key={row.id}>
                <button type="button" aria-expanded={show} onClick={() => setExpanded((previous) => {
                  const next = new Set(previous);
                  if (next.has(row.id)) next.delete(row.id); else next.add(row.id);
                  return next;
                })} className="flex w-full items-center gap-1.5 px-3 py-1.5 text-left hover:bg-bg-hover">
                  <ChevronRight className={clsx('h-3.5 w-3.5 shrink-0 text-text-muted transition-transform', show && 'rotate-90')} />
                  <span className="min-w-0 flex-1 truncate">{name}</span><span className="text-[11px] text-text-muted">{stages.length} 场</span>
                </button>
                {show && stages.map(([id, stage]) => <button key={id} type="button" onClick={() => choose(id)}
                  className={clsx('block w-full truncate py-1.5 pl-9 pr-3 text-left text-xs hover:bg-bg-hover', id === value && 'bg-accent/10 font-medium text-accent')}>{stage.name}</button>)}
              </div>;
            })}
          </div>;
        })}
      </div>
    </div>}
  </div>;
}
