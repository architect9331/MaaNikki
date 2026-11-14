import { useEffect, useMemo, useState } from 'react';
import { ChevronDown, Package, Play, Search, Square } from 'lucide-react';
import clsx from 'clsx';
import bagCatalogRaw from '@/data/bagCatalog.json';
import { useAccountStateStore } from '@/stores/accountStateStore';
import { useWardrobePlanStore } from '@/services/scoring/wardrobePlanStore';
import { startBackpackRead, stopWardrobeRead, useWardrobeStore, WARDROBE_INSTANCE_ID } from '@/services/wardrobeService';
import { isTauri } from '@/utils/paths';
import { LogsPanel } from './LogsPanel';
import { primaryButton, secondaryButton, Segmented, StatusBar, Tile } from './ui/FeaturePage';

type CatalogTuple = [string, number, string, number];
const BAG_CATALOG = bagCatalogRaw as unknown as Record<string, CatalogTuple>;
const PAGE_SIZE = 80;
const num = (value: number) => value.toLocaleString('zh-CN');
const formatTime = (value: string) => new Date(value).toLocaleString('zh-CN', { hour12: false });
const field = 'rounded-lg border border-border bg-bg-primary px-2.5 py-1.5 text-xs outline-none focus:border-accent';
const stockInput = 'w-20 rounded-md border border-border bg-bg-primary px-1.5 py-0.5 text-right text-xs tabular-nums outline-none focus:border-accent';

interface BagEntry {
  id: string;
  name: string;
  tab: number;
  displayName: string;
  quality: number;
  count: number;
  tracked: boolean;
}

export function BackpackViewerPanel({ refreshToken }: { refreshToken: number }) {
  const account = useAccountStateStore((state) => state.data);
  const { payload, materials, load, updateMaterials } = useWardrobePlanStore();
  const { status, mode, error } = useWardrobeStore();
  const [search, setSearch] = useState('');
  const [tabFilter, setTabFilter] = useState<'all' | '1' | '2'>('all');
  const [catFilter, setCatFilter] = useState('all');
  const [onlyStocked, setOnlyStocked] = useState(true);
  const [showLogs, setShowLogs] = useState(false);
  const [limit, setLimit] = useState(PAGE_SIZE);

  useEffect(() => {
    if (account?.uid) {
      void load(`${account.uid}:${account.updated_at ?? ''}:${refreshToken}`);
    }
  }, [account?.uid, account?.updated_at, refreshToken, load]);

  useEffect(() => {
    setLimit(PAGE_SIZE);
  }, [search, tabFilter, catFilter, onlyStocked]);

  const busy = ['starting', 'reading', 'stopping'].includes(status);
  const readingBackpack = busy && mode === 'backpack';
  const inventory = materials.inventory ?? {};
  const summary = materials.backpack_summary;

  const entries = useMemo(() => {
    const seen = new Set<string>();
    const rows: BagEntry[] = [];
    const craftMats = payload?.crafting_catalog?.materials ?? {};
    for (const [id, [name, tab, displayName, quality]] of Object.entries(BAG_CATALOG)) {
      seen.add(id);
      const tracked = Object.prototype.hasOwnProperty.call(inventory, id);
      rows.push({
        id,
        name: craftMats[id]?.name ?? name,
        tab,
        displayName,
        quality,
        count: Math.max(0, Number(inventory[id] ?? 0)),
        tracked,
      });
    }
    for (const [id, rawCount] of Object.entries(inventory)) {
      if (seen.has(id)) continue;
      rows.push({
        id,
        name: craftMats[id]?.name ?? id,
        tab: 1,
        displayName: '其他物品',
        quality: 3,
        count: Math.max(0, Number(rawCount ?? 0)),
        tracked: true,
      });
    }
    return rows.sort((a, b) =>
      Number(b.count > 0) - Number(a.count > 0)
      || b.quality - a.quality
      || a.tab - b.tab
      || b.count - a.count
      || a.name.localeCompare(b.name, 'zh-CN'),
    );
  }, [inventory, payload?.crafting_catalog?.materials]);

  const categories = useMemo(() => {
    const set = new Set<string>();
    for (const row of entries) {
      if (tabFilter === 'all' || String(row.tab) === tabFilter) set.add(row.displayName);
    }
    return [...set].sort((a, b) => a.localeCompare(b, 'zh-CN'));
  }, [entries, tabFilter]);

  const q = search.trim();
  const filtered = useMemo(() => entries.filter((row) =>
    (tabFilter === 'all' || String(row.tab) === tabFilter)
    && (catFilter === 'all' || row.displayName === catFilter)
    && (!onlyStocked || row.count > 0)
    && (!q || row.name.includes(q) || row.displayName.includes(q)),
  ), [entries, tabFilter, catFilter, onlyStocked, q]);

  const tab1Count = entries.filter((r) => r.tab === 1 && r.count > 0).length;
  const tab2Count = entries.filter((r) => r.tab === 2 && r.count > 0).length;
  const stockedTotal = entries.filter((r) => r.count > 0).length;
  const bling = inventory['992617511'] ?? summary?.bling;
  const updatedAt = summary?.updated_at ?? materials.inventory_updated_at;

  function setStock(id: string, value: number) {
    updateMaterials((previous) => ({
      ...previous,
      inventory: { ...previous.inventory, [id]: Math.max(0, Math.round(value) || 0) },
      inventory_updated_at: new Date().toISOString(),
    }));
  }

  const readBtn = (
    <button
      type="button"
      disabled={!isTauri() || (busy && !readingBackpack) || status === 'stopping'}
      onClick={() => (readingBackpack ? stopWardrobeRead() : void startBackpackRead())}
      className={readingBackpack ? secondaryButton : primaryButton}
    >
      {readingBackpack ? <Square className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
      {readingBackpack ? '停止读取' : updatedAt ? '更新背包' : '读取背包'}
    </button>
  );

  const tone = readingBackpack ? 'busy' : error && mode === 'backpack' ? 'warn' : summary?.complete ? 'ok' : stockedTotal > 0 ? 'ok' : 'idle';
  const statusTitle = readingBackpack
    ? (status === 'starting' ? '连接游戏中' : status === 'stopping' ? '正在停止读取' : '正在读取背包库存')
    : summary?.complete
      ? '背包库存已读取'
      : updatedAt
        ? '已记录部分材料库存'
        : '尚未读取背包';
  const statusDetail = updatedAt
    ? <>材料页 <strong className="tabular-nums text-text-primary">{summary?.tab1?.items ?? tab1Count}</strong> 项 · 养成材料页{' '}
      <strong className="tabular-nums text-text-primary">{summary?.tab2?.items ?? tab2Count}</strong> 项
      {bling != null && <> · 噗灵 <strong className="tabular-nums text-text-primary">{num(bling)}</strong></>}
      {' · '}更新于 {formatTime(updatedAt)}</>
    : '会自动读取游戏内「背包 · 材料」「背包 · 养成材料」及顶部噗灵数量，同步用于制衣、焕新和库存查询。';

  return (
    <div className="space-y-4">
      <StatusBar tone={tone} title={statusTitle} detail={statusDetail}>
        <button
          type="button"
          onClick={() => setShowLogs((value) => !value)}
          aria-expanded={showLogs || readingBackpack}
          className={secondaryButton}
        >
          读取日志
          <ChevronDown className={clsx('h-3.5 w-3.5 transition-transform', (showLogs || readingBackpack) && 'rotate-180')} />
        </button>
        {readBtn}
      </StatusBar>

      {(showLogs || readingBackpack) && (
        <section className="overflow-hidden rounded-xl border border-border bg-bg-secondary">
          <div className="h-44"><LogsPanel instanceId={WARDROBE_INSTANCE_ID} /></div>
        </section>
      )}

      <section className="rounded-xl border border-border bg-bg-secondary p-4 space-y-3">
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <Tile label="当前噗灵" value={bling != null ? num(bling) : '—'} />
          <Tile label="材料库存" value={<>{tab1Count}<span className="ml-1 text-xs font-normal text-text-muted">种</span></>} />
          <Tile label="养成材料库存" value={<>{tab2Count}<span className="ml-1 text-xs font-normal text-text-muted">种</span></>} />
          <Tile label="有库存总数" value={<>{stockedTotal}<span className="ml-1 text-xs font-normal text-text-muted">/ {entries.length}</span></>} />
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Segmented
            label="背包页签"
            value={tabFilter}
            options={[['all', '全部'], ['1', '材料'], ['2', '养成材料']] as const}
            onChange={(next) => { setTabFilter(next); setCatFilter('all'); }}
          />
          <select aria-label="类别筛选" className={field} value={catFilter} onChange={(e) => setCatFilter(e.target.value)}>
            <option value="all">全部类别</option>
            {categories.map((cat) => <option key={cat} value={cat}>{cat}</option>)}
          </select>
          <label className="flex min-w-36 flex-1 items-center gap-1.5">
            <Search className="h-3.5 w-3.5 shrink-0 text-text-muted" />
            <input
              className={clsx(field, 'w-full')}
              placeholder="搜索物品名称或类别"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </label>
          <label className="inline-flex cursor-pointer items-center gap-1.5 text-xs text-text-secondary">
            <input
              type="checkbox"
              checked={onlyStocked}
              onChange={(e) => setOnlyStocked(e.target.checked)}
              className="h-3.5 w-3.5 accent-accent"
            />
            只看有库存
          </label>
        </div>

        {filtered.length === 0 ? (
          <div className="py-10 text-center text-xs text-text-secondary">
            <Package className="mx-auto mb-2 h-8 w-8 text-text-muted opacity-50" />
            {stockedTotal === 0 ? '还没有背包库存记录，点击右上角「读取背包」后这里会展示完整材料数量。' : '没有符合筛选条件的物品。'}
          </div>
        ) : (
          <>
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {filtered.slice(0, limit).map((row) => (
                <div key={row.id} className="flex items-center justify-between gap-2 rounded-lg border border-border bg-bg-primary px-3 py-2 text-xs">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5">
                      <span className="shrink-0 rounded bg-accent/10 px-1.5 py-0.2 text-[10px] font-medium text-accent">{row.quality}★</span>
                      <p className="truncate font-medium text-text-primary">{row.name}</p>
                    </div>
                    <p className="mt-0.5 truncate text-[11px] text-text-muted">
                      {row.tab === 0 ? '货币' : row.tab === 1 ? '材料' : '养成材料'} · {row.displayName}
                    </p>
                  </div>
                  <input
                    type="number"
                    min="0"
                    aria-label={`${row.name}数量`}
                    title="背包扫描会自动更新，也可手动调整"
                    value={row.count}
                    onChange={(e) => setStock(row.id, Number(e.target.value))}
                    className={stockInput}
                  />
                </div>
              ))}
            </div>
            {filtered.length > limit && (
              <button
                type="button"
                onClick={() => setLimit((prev) => prev + PAGE_SIZE)}
                className="w-full rounded-lg border border-border py-2 text-xs text-text-secondary hover:bg-bg-hover"
              >
                加载更多（{limit} / {filtered.length}）
              </button>
            )}
          </>
        )}
      </section>
    </div>
  );
}
