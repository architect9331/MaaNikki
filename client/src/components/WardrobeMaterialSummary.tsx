import { useState, type ReactNode } from 'react';
import { MapPin } from 'lucide-react';
import clsx from 'clsx';
import { Tile } from './ui/FeaturePage';
import { MATERIAL_GROUP_LABELS, type MaterialGroup, type MaterialRow, type MaterialTotals } from '@/services/scoring/materialPlanning';
import { useForecastAssumptionsStore } from '@/stores/forecastAssumptionsStore';

const num = (value: number) => value.toLocaleString('zh-CN');
const stockField =
  'w-20 rounded-md border border-border bg-bg-primary px-1.5 py-0.5 text-right text-xs tabular-nums outline-none focus:border-accent';

function MaterialLine({ row, onStockChange }: { row: MaterialRow; onStockChange?: (id: string, value: number) => void }) {
  const done = row.owned !== null && row.missing === 0;
  const pct = row.owned === null ? 0 : Math.min(100, Math.round((row.owned / row.need) * 100));
  return (
    <div className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1 px-3 py-2 text-xs">
      <div className="min-w-0">
        <p className="truncate font-medium text-text-primary">{row.name}</p>
        {row.source && (
          <p className="mt-0.5 flex items-center gap-1 truncate text-[11px] text-text-muted">
            <MapPin className="h-3 w-3 shrink-0" />
            {row.source.label}
          </p>
        )}
      </div>
      <div className="flex items-center gap-2">
        {row.owned !== null && onStockChange ? (
          <input
            type="number"
            min="0"
            aria-label={`${row.name}库存`}
            title="库存（每日背包扫描会自动更新，也可手动改）"
            value={row.owned}
            onChange={(event) => onStockChange(row.id, Number(event.target.value))}
            className={stockField}
          />
        ) : null}
        <span className="w-24 text-right tabular-nums text-text-secondary">
          {row.owned === null ? '需要' : '/'} {num(row.need)}
        </span>
        <span
          className={clsx(
            'w-28 text-right tabular-nums',
            done ? 'text-success' : row.owned === null ? 'text-text-muted' : 'font-semibold text-accent',
          )}
        >
          {row.owned === null ? '库存未记录' : done ? '已够' : `还差 ${num(row.missing)}`}
        </span>
      </div>
      {row.owned !== null && (
        <div className="col-span-2 h-1 overflow-hidden rounded-full bg-border">
          <div className={clsx('h-full rounded-full', done ? 'bg-success' : 'bg-accent')} style={{ width: `${pct}%` }} />
        </div>
      )}
    </div>
  );
}

/** The right-hand material panel shared by crafting and renewal planning. */
export function WardrobeMaterialSummary({
  title,
  scope,
  totals,
  realmTotals,
  realmEnergyLabel,
  groups,
  onStockChange,
  headerAction,
  actions,
  note,
  empty,
}: {
  title: string;
  scope: ReactNode;
  totals: MaterialTotals;
  /** Optional realm-farmable subset (e.g. trial crystals only for crafting). */
  realmTotals?: MaterialTotals;
  realmEnergyLabel?: string;
  groups: MaterialGroup[];
  onStockChange?: (id: string, value: number) => void;
  headerAction?: ReactNode;
  actions?: ReactNode;
  note?: ReactNode;
  empty: ReactNode;
}) {
  const dailyEnergy = useForecastAssumptionsStore((state) => state.dailyEnergy);
  // With a long list, what is still short matters more than what is already enough.
  const [onlyMissing, setOnlyMissing] = useState(false);
  const enoughCount = totals.rows.filter((row) => row.owned !== null && row.missing === 0).length;
  const sections = groups
    .map((group) => ({
      group,
      rows: totals.rows.filter(
        (row) => row.group === group && (!onlyMissing || row.owned === null || row.missing > 0),
      ),
    }))
    .filter((section) => section.rows.length);

  const primaryTotals = realmTotals ?? totals;
  const hasSeparateRealm = Boolean(realmTotals && realmTotals.energy !== totals.energy);

  return (
    <section className="overflow-hidden rounded-xl border border-border bg-bg-secondary">
      <div className="border-b border-border px-4 py-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-sm font-semibold">{title}</h2>
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs text-text-secondary">{scope}</span>
            {headerAction}
          </div>
        </div>
        {totals.rows.length > 0 && (
          <div className="mt-3 grid grid-cols-3 gap-2">
            <Tile
              label="还缺材料"
              value={
                <>
                  <span className={totals.missingKinds ? 'text-accent' : 'text-success'}>{totals.missingKinds}</span>
                  <span className="ml-1 text-xs font-normal text-text-muted">/ {totals.trackedKinds} 种</span>
                </>
              }
            />
            <Tile
              label={hasSeparateRealm ? (realmEnergyLabel ?? '幻境晶石所需能量') : '补齐约需能量'}
              value={num(primaryTotals.energy)}
              detail={hasSeparateRealm ? `含噗灵/丝线折算共 ${num(totals.energy)}` : undefined}
            />
            <Tile
              label={`按每日 ${dailyEnergy} 能量`}
              value={
                primaryTotals.energy ? (
                  <>
                    {primaryTotals.days}
                    <span className="ml-1 text-xs font-normal text-text-muted">天</span>
                  </>
                ) : (
                  '已备齐'
                )
              }
              detail={hasSeparateRealm && totals.energy > 0 ? `全套折算约 ${totals.days} 天` : undefined}
            />
          </div>
        )}
        {totals.rows.length > 0 && (actions || enoughCount > 0) && (
          <div className="mt-3 flex flex-wrap items-center gap-2">
            {enoughCount > 0 && (
              <label className="mr-auto inline-flex cursor-pointer items-center gap-1.5 text-xs text-text-secondary">
                <input
                  type="checkbox"
                  checked={onlyMissing}
                  onChange={(event) => setOnlyMissing(event.target.checked)}
                  className="h-3.5 w-3.5 accent-accent"
                />
                只看还缺的（已够 {enoughCount} 种）
              </label>
            )}
            {actions}
          </div>
        )}
      </div>
      {totals.rows.length === 0 ? (
        <div className="px-4 py-10 text-center text-xs leading-5 text-text-secondary">{empty}</div>
      ) : (
        sections.map(({ group, rows }) =>
          group === 'world' ? (
            <details key={group} className="border-b border-border last:border-b-0">
              <summary className="cursor-pointer bg-bg-primary/60 px-4 py-2 text-xs font-semibold text-text-secondary">
                {MATERIAL_GROUP_LABELS[group]}
                <span className="ml-1.5 font-normal text-text-muted">
                  {rows.length} 种
                  {rows.every((row) => row.owned === null)
                    ? ' · 库存不在当前扫描范围内，仅列出需求'
                    : ' · 已同步扫描记录'}
                </span>
              </summary>
              {rows.some((row) => row.owned !== null) ? (
                <div className="divide-y divide-border/60">
                  {rows.map((row) => (
                    <MaterialLine key={row.id} row={row} onStockChange={onStockChange} />
                  ))}
                </div>
              ) : (
                <div className="grid grid-cols-2 gap-1.5 p-3 sm:grid-cols-3">
                  {rows.map((row) => (
                    <div key={row.id} className="flex items-center justify-between rounded-md bg-bg-primary px-2.5 py-1.5 text-xs">
                      <span className="truncate text-text-secondary">{row.name}</span>
                      <strong className="ml-2 tabular-nums">{num(row.need)}</strong>
                    </div>
                  ))}
                </div>
              )}
            </details>
          ) : (
            <div key={group} className="border-b border-border last:border-b-0">
              <p className="bg-bg-primary/60 px-4 py-2 text-xs font-semibold text-text-secondary">
                {MATERIAL_GROUP_LABELS[group]}
              </p>
              <div className="divide-y divide-border/60">
                {rows.map((row) => (
                  <MaterialLine key={row.id} row={row} onStockChange={onStockChange} />
                ))}
              </div>
            </div>
          ),
        )
      )}
      {note && <p className="border-t border-border px-4 py-2.5 text-[11px] leading-5 text-text-muted">{note}</p>}
    </section>
  );
}
