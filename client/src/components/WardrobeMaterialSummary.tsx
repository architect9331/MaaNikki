import { useState, type ReactNode } from 'react';
import { Check, Flame, MapPin } from 'lucide-react';
import clsx from 'clsx';
import { toast } from 'sonner';
import { useAppStore } from '@/stores/appStore';
import type { RealmMaterialTarget } from '@/services/scoring/heartPresets';
import { Tile } from './ui/FeaturePage';
import { MATERIAL_GROUP_LABELS, type MaterialGroup, type MaterialRow, type MaterialTotals } from '@/services/scoring/materialPlanning';

const num = (value: number) => value.toLocaleString('zh-CN');
const stockField = 'w-20 rounded-md border border-border bg-bg-primary px-1.5 py-0.5 text-right text-xs tabular-nums outline-none focus:border-accent';

/** Points the realm challenge's fallback (兜底) stage at one material. */
export function useRealmChallenge() {
  const instances = useAppStore((state) => state.instances);
  const activeInstanceId = useAppStore((state) => state.activeInstanceId);
  const setTaskOptionValue = useAppStore((state) => state.setTaskOptionValue);
  const instance = instances.find((item) => item.id === activeInstanceId) ?? instances[0];
  const task = instance?.selectedTasks.find((item) => item.taskName === 'realm_challenge');
  const values = task?.optionValues ?? {};
  const realmType = values.RealmTypes?.type === 'select' ? values.RealmTypes.caseName : 'Bless';

  const isCurrent = (target: RealmMaterialTarget) => {
    const option = values[target.optionKey];
    return realmType === target.realmType && option?.type === 'select' && option.caseName === target.caseName;
  };
  const apply = (target: RealmMaterialTarget) => {
    if (!instance || !task) {
      toast.error('当前配置中还没有「幻境挑战」任务，请先在主界面添加。');
      return;
    }
    setTaskOptionValue(instance.id, task.id, 'RealmTypes', { type: 'select', caseName: target.realmType });
    setTaskOptionValue(instance.id, task.id, target.optionKey, { type: 'select', caseName: target.caseName });
    toast.success(`已将幻境挑战的兜底副本设为「${target.label}」`);
  };
  return { hasTask: !!task, isCurrent, apply };
}

function SourceButton({ target }: { target: RealmMaterialTarget }) {
  const realm = useRealmChallenge();
  const current = realm.isCurrent(target);
  return <button type="button" onClick={() => realm.apply(target)} title={`把幻境挑战的兜底副本设为：${target.label}`}
    className={clsx('inline-flex shrink-0 items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] transition-colors',
      current ? 'bg-accent/15 font-medium text-accent' : 'border border-border text-text-secondary hover:border-accent hover:text-accent')}>
    {current ? <Check className="h-3 w-3" /> : <Flame className="h-3 w-3" />}{current ? '已设兜底' : '设为兜底'}
  </button>;
}

function MaterialLine({ row, onStockChange }: { row: MaterialRow; onStockChange?: (id: string, value: number) => void }) {
  const done = row.owned !== null && row.missing === 0;
  const pct = row.owned === null ? 0 : Math.min(100, Math.round(row.owned / row.need * 100));
  return <div className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1 px-3 py-2 text-xs">
    <div className="min-w-0">
      <p className="truncate font-medium text-text-primary">{row.name}</p>
      {row.source && <p className="mt-0.5 flex items-center gap-1 truncate text-[11px] text-text-muted"><MapPin className="h-3 w-3 shrink-0" />{row.source.label}</p>}
    </div>
    <div className="flex items-center gap-2">
      {row.owned !== null && onStockChange
        ? <input type="number" min="0" aria-label={`${row.name}库存`} title="库存（每日背包扫描会自动更新，也可手动改）" value={row.owned}
          onChange={(event) => onStockChange(row.id, Number(event.target.value))} className={stockField} />
        : null}
      <span className="w-24 text-right tabular-nums text-text-secondary">{row.owned === null ? '需要' : '/'} {num(row.need)}</span>
      <span className={clsx('w-28 text-right tabular-nums', done ? 'text-success' : row.owned === null ? 'text-text-muted' : 'font-semibold text-accent')}>
        {row.owned === null ? '库存未记录' : done ? '已够' : `还差 ${num(row.missing)}`}
      </span>
      {row.source ? <SourceButton target={row.source} /> : <span className="w-[52px]" />}
    </div>
    {row.owned !== null && <div className="col-span-2 h-1 overflow-hidden rounded-full bg-border">
      <div className={clsx('h-full rounded-full', done ? 'bg-success' : 'bg-accent')} style={{ width: `${pct}%` }} />
    </div>}
  </div>;
}

/** The right-hand material panel shared by crafting and renewal planning. */
export function WardrobeMaterialSummary({ title, scope, totals, groups, onStockChange, actions, note, empty }: {
  title: string;
  scope: ReactNode;
  totals: MaterialTotals;
  groups: MaterialGroup[];
  onStockChange?: (id: string, value: number) => void;
  actions?: ReactNode;
  note?: ReactNode;
  empty: ReactNode;
}) {
  // With a long list, what is still short matters more than what is already enough.
  const [onlyMissing, setOnlyMissing] = useState(false);
  const enoughCount = totals.rows.filter((row) => row.group !== 'world' && row.owned !== null && row.missing === 0).length;
  const sections = groups
    .map((group) => ({ group, rows: totals.rows.filter((row) => row.group === group
      && (!onlyMissing || group === 'world' || row.owned === null || row.missing > 0)) }))
    .filter((section) => section.rows.length);
  return <section className="overflow-hidden rounded-xl border border-border bg-bg-secondary">
    <div className="border-b border-border px-4 py-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold">{title}</h2>
        <span className="text-xs text-text-secondary">{scope}</span>
      </div>
      {totals.rows.length > 0 && <div className="mt-3 grid grid-cols-3 gap-2">
        <Tile label="还缺材料" value={<><span className={totals.missingKinds ? 'text-accent' : 'text-success'}>{totals.missingKinds}</span><span className="ml-1 text-xs font-normal text-text-muted">/ {totals.trackedKinds} 种</span></>} />
        <Tile label="补齐约需能量" value={num(totals.energy)} />
        <Tile label="按每日 320 能量" value={totals.energy ? <>{totals.days}<span className="ml-1 text-xs font-normal text-text-muted">天</span></> : '已备齐'} />
      </div>}
      {totals.rows.length > 0 && (actions || enoughCount > 0) && <div className="mt-3 flex flex-wrap items-center gap-2">
        {enoughCount > 0 && <label className="mr-auto inline-flex cursor-pointer items-center gap-1.5 text-xs text-text-secondary">
          <input type="checkbox" checked={onlyMissing} onChange={(event) => setOnlyMissing(event.target.checked)} className="h-3.5 w-3.5 accent-accent" />
          只看还缺的（已够 {enoughCount} 种）
        </label>}
        {actions}
      </div>}
    </div>
    {totals.rows.length === 0
      ? <div className="px-4 py-10 text-center text-xs leading-5 text-text-secondary">{empty}</div>
      : sections.map(({ group, rows }) => group === 'world'
        ? <details key={group} className="border-b border-border last:border-b-0">
          <summary className="cursor-pointer bg-bg-primary/60 px-4 py-2 text-xs font-semibold text-text-secondary">
            {MATERIAL_GROUP_LABELS[group]}<span className="ml-1.5 font-normal text-text-muted">{rows.length} 种 · 库存不在背包扫描范围内，仅列出需求</span>
          </summary>
          <div className="grid grid-cols-2 gap-1.5 p-3 sm:grid-cols-3">
            {rows.map((row) => <div key={row.id} className="flex items-center justify-between rounded-md bg-bg-primary px-2.5 py-1.5 text-xs">
              <span className="truncate text-text-secondary">{row.name}</span><strong className="ml-2 tabular-nums">{num(row.need)}</strong>
            </div>)}
          </div>
        </details>
        : <div key={group} className="border-b border-border last:border-b-0">
          <p className="bg-bg-primary/60 px-4 py-2 text-xs font-semibold text-text-secondary">{MATERIAL_GROUP_LABELS[group]}</p>
          <div className="divide-y divide-border/60">{rows.map((row) => <MaterialLine key={row.id} row={row} onStockChange={onStockChange} />)}</div>
        </div>)}
    {note && <p className="border-t border-border px-4 py-2.5 text-[11px] leading-5 text-text-muted">{note}</p>}
  </section>;
}
