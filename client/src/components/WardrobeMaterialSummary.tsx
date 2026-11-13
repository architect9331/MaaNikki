import type { ReactNode } from 'react';
import { Check, Flame, MapPin } from 'lucide-react';
import clsx from 'clsx';
import { toast } from 'sonner';
import { useAppStore } from '@/stores/appStore';
import type { RealmMaterialTarget } from '@/services/scoring/heartPresets';
import { MATERIAL_GROUP_LABELS, type MaterialGroup, type MaterialRow, type MaterialTotals } from '@/services/scoring/materialPlanning';

const num = (value: number) => value.toLocaleString('zh-CN');
const stockField = 'w-20 rounded-md border border-border bg-bg-primary px-1.5 py-0.5 text-right text-xs tabular-nums outline-none focus:border-accent';

/** Reads and points the current profile's realm challenge at one material. */
export function useRealmChallenge() {
  const instances = useAppStore((state) => state.instances);
  const activeInstanceId = useAppStore((state) => state.activeInstanceId);
  const setTaskOptionValue = useAppStore((state) => state.setTaskOptionValue);
  const instance = instances.find((item) => item.id === activeInstanceId) ?? instances[0];
  const task = instance?.selectedTasks.find((item) => item.taskName === 'realm_challenge');
  const values = task?.optionValues ?? {};
  const collection = values.CollectionMode?.type === 'switch' && values.CollectionMode.value;
  const crafting = !collection && values.CraftingCollectionMode?.type === 'switch' && values.CraftingCollectionMode.value;
  const realmType = collection || crafting ? '' : values.RealmTypes?.type === 'select' ? values.RealmTypes.caseName : 'Bless';

  const isCurrent = (target: RealmMaterialTarget) => {
    const option = values[target.optionKey];
    return realmType === target.realmType && option?.type === 'select' && option.caseName === target.caseName;
  };
  const apply = (target: RealmMaterialTarget) => {
    if (!instance || !task) {
      toast.error('当前配置中还没有「幻境挑战」任务，请先在主界面添加。');
      return;
    }
    setTaskOptionValue(instance.id, task.id, 'CollectionMode', { type: 'switch', value: false });
    setTaskOptionValue(instance.id, task.id, 'CraftingCollectionMode', { type: 'switch', value: false });
    setTaskOptionValue(instance.id, task.id, 'RealmTypes', { type: 'select', caseName: target.realmType });
    setTaskOptionValue(instance.id, task.id, target.optionKey, { type: 'select', caseName: target.caseName });
    toast.success(`已将幻境挑战设为「${target.label}」`);
  };
  const setCraftingCollection = (enabled: boolean) => {
    if (!instance || !task) {
      toast.error('当前配置中还没有「幻境挑战」任务，请先在主界面添加。');
      return;
    }
    if (enabled) setTaskOptionValue(instance.id, task.id, 'CollectionMode', { type: 'switch', value: false });
    setTaskOptionValue(instance.id, task.id, 'CraftingCollectionMode', { type: 'switch', value: enabled });
    toast.success(enabled ? '幻境挑战已改为「制衣材料全收集」' : '幻境挑战已关闭制衣材料全收集');
  };
  return { hasTask: !!task, crafting, isCurrent, apply, setCraftingCollection };
}

function SourceButton({ target }: { target: RealmMaterialTarget }) {
  const realm = useRealmChallenge();
  const current = realm.isCurrent(target);
  return <button type="button" onClick={() => realm.apply(target)} title={`将幻境挑战设为：${target.label}`}
    className={clsx('inline-flex shrink-0 items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] transition-colors',
      current ? 'bg-accent/15 font-medium text-accent' : 'border border-border text-text-secondary hover:border-accent hover:text-accent')}>
    {current ? <Check className="h-3 w-3" /> : <Flame className="h-3 w-3" />}{current ? '正在刷' : '去刷'}
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
  const sections = groups
    .map((group) => ({ group, rows: totals.rows.filter((row) => row.group === group) }))
    .filter((section) => section.rows.length);
  return <section className="overflow-hidden rounded-xl border border-border bg-bg-secondary">
    <div className="border-b border-border px-4 py-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-sm font-semibold">{title}</h3>
        <span className="text-xs text-text-secondary">{scope}</span>
      </div>
      {totals.rows.length > 0 && <div className="mt-3 grid grid-cols-3 gap-2 text-center">
        <div className="rounded-lg bg-bg-primary px-2 py-2">
          <p className="text-[11px] text-text-secondary">还缺材料</p>
          <p className={clsx('mt-0.5 text-lg font-semibold tabular-nums', totals.missingKinds ? 'text-accent' : 'text-success')}>
            {totals.missingKinds}<span className="ml-0.5 text-xs font-normal text-text-muted">/ {totals.trackedKinds} 种</span>
          </p>
        </div>
        <div className="rounded-lg bg-bg-primary px-2 py-2">
          <p className="text-[11px] text-text-secondary">补齐约需能量</p>
          <p className="mt-0.5 text-lg font-semibold tabular-nums">{num(totals.energy)}</p>
        </div>
        <div className="rounded-lg bg-bg-primary px-2 py-2">
          <p className="text-[11px] text-text-secondary">按每日 320 能量</p>
          <p className="mt-0.5 text-lg font-semibold tabular-nums">{totals.energy ? <>{totals.days}<span className="ml-0.5 text-xs font-normal text-text-muted">天</span></> : '已备齐'}</p>
        </div>
      </div>}
      {actions && totals.rows.length > 0 && <div className="mt-3 flex flex-wrap items-center gap-2">{actions}</div>}
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
