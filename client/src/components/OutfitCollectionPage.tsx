import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Check,
  Copy,
  Download,
  Palette,
  Plus,
  RefreshCw,
  Search,
  Shirt,
  Sparkles,
  Upload,
} from 'lucide-react';
import { toast } from 'sonner';
import clsx from 'clsx';
import { useAccountStateStore } from '@/stores/accountStateStore';
import { openCollectionHub } from '@/stores/collectionHubStore';
import { useWardrobePlanStore } from '@/services/scoring/wardrobePlanStore';
import { buildWardrobeOwnershipIndex, evaluateOutfit } from '@/services/outfits/matcher';
import { useOutfitCollectionStore } from '@/services/outfits/store';
import type {
  MatchPreset,
  OutfitEntry,
  OutfitEvaluation,
  OutfitMatchStatus,
} from '@/services/outfits/types';
import { FeaturePage, primaryButton, secondaryButton, Segmented, StatusBar, Tile } from './ui/FeaturePage';
import { OutfitImportModal } from './OutfitImportModal';
import { OutfitDetailModal } from './OutfitDetailModal';

type StatusFilter = 'all' | 'wearable' | 'near' | 'missing';
type SortMode = 'match' | 'recent' | 'missing_asc';

const STATUS_PRIORITY: Record<OutfitMatchStatus, number> = {
  wearable: 0,
  same_model: 1,
  near: 2,
  missing: 3,
};

function statusBadge(ev: OutfitEvaluation): { text: string; cls: string } {
  if (ev.status === 'wearable') {
    return { text: '可直接穿', cls: 'bg-success/90 text-white' };
  }
  if (ev.status === 'same_model') {
    return { text: `同模 · 缺 ${ev.sameModelCount} 变体`, cls: 'bg-warning/90 text-white' };
  }
  if (ev.status === 'near') {
    return { text: `仅缺 ${ev.missingCount} 件`, cls: 'bg-warning/90 text-white' };
  }
  return { text: `缺 ${ev.missingCount} 件`, cls: 'bg-black/65 text-white' };
}

export function OutfitCollectionPage({ onClose }: { onClose: () => void }) {
  const { data: accountData, refresh: refreshAccount, loading: accountLoading } = useAccountStateStore();
  const { payload, settings, load: loadWardrobe } = useWardrobePlanStore();
  const {
    items,
    dict,
    rule,
    loaded,
    loading,
    load,
    setPreset,
    patchRule,
    mergeImportedItems,
  } = useOutfitCollectionStore();

  const [refreshToken, setRefreshToken] = useState(0);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');
  const [selectedTag, setSelectedTag] = useState<string>('all');
  const [search, setSearch] = useState('');
  const [sortMode, setSortMode] = useState<SortMode>('match');
  const [showImport, setShowImport] = useState(false);
  const [activeOutfitId, setActiveOutfitId] = useState<string | null>(null);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const backupInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    void load();
    void refreshAccount().catch(() => {});
  }, [load, refreshAccount]);

  const planRevision = accountData?.plan_revision
    ?? accountData?.wardrobe?.last_scan?.updated_at
    ?? accountData?.wardrobe?.inventory?.updated_at
    ?? '';

  useEffect(() => {
    if (accountData?.uid) {
      void loadWardrobe(`${accountData.uid}:${planRevision}:${refreshToken}`);
    }
  }, [accountData?.uid, planRevision, refreshToken, loadWardrobe]);

  const ownership = useMemo(
    () => buildWardrobeOwnershipIndex(payload, settings),
    [payload, settings],
  );

  const evaluated = useMemo(() => {
    return items.map((outfit) => ({
      outfit,
      ev: evaluateOutfit(outfit, ownership, dict, payload, rule),
    }));
  }, [items, ownership, dict, payload, rule]);

  const counts = useMemo(() => {
    let wearable = 0;
    let near = 0;
    let missing = 0;
    let anyUsesMakeup = false;
    for (const { ev } of evaluated) {
      if (ev.usesMakeup) anyUsesMakeup = true;
      if (ev.status === 'wearable' || ev.status === 'same_model') wearable += 1;
      else if (ev.status === 'near') near += 1;
      else missing += 1;
    }
    return { total: evaluated.length, wearable, near, missing, anyUsesMakeup };
  }, [evaluated]);

  const allTags = useMemo(() => {
    const map = new Map<string, number>();
    for (const item of items) {
      for (const tag of item.tags) {
        map.set(tag, (map.get(tag) ?? 0) + 1);
      }
    }
    return [...map.entries()].sort((a, b) => b[1] - a[1]);
  }, [items]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    const list = evaluated.filter(({ outfit, ev }) => {
      if (statusFilter === 'wearable' && ev.status !== 'wearable' && ev.status !== 'same_model') return false;
      if (statusFilter === 'near' && ev.status !== 'near') return false;
      if (statusFilter === 'missing' && ev.status !== 'missing') return false;
      if (selectedTag !== 'all' && !outfit.tags.includes(selectedTag)) return false;
      if (!q) return true;
      return (
        outfit.title.toLowerCase().includes(q)
        || outfit.code.toLowerCase().includes(q)
        || outfit.note.toLowerCase().includes(q)
        || outfit.tags.some((t) => t.toLowerCase().includes(q))
        || ev.pieces.some((p) => p.name.toLowerCase().includes(q) || (p.suitName ?? '').toLowerCase().includes(q))
      );
    });
    return [...list].sort((a, b) => {
      if (sortMode === 'recent') {
        return b.outfit.createdAt.localeCompare(a.outfit.createdAt);
      }
      if (sortMode === 'missing_asc') {
        return a.ev.missingCount - b.ev.missingCount
          || STATUS_PRIORITY[a.ev.status] - STATUS_PRIORITY[b.ev.status]
          || b.outfit.createdAt.localeCompare(a.outfit.createdAt);
      }
      const ratioA = a.ev.requiredCount > 0 ? a.ev.matchedCount / a.ev.requiredCount : 1;
      const ratioB = b.ev.requiredCount > 0 ? b.ev.matchedCount / b.ev.requiredCount : 1;
      return STATUS_PRIORITY[a.ev.status] - STATUS_PRIORITY[b.ev.status]
        || ratioB - ratioA
        || a.ev.missingCount - b.ev.missingCount
        || b.outfit.createdAt.localeCompare(a.outfit.createdAt);
    });
  }, [evaluated, statusFilter, selectedTag, search, sortMode]);

  const activePair = useMemo(
    () => (activeOutfitId ? evaluated.find((x) => x.outfit.id === activeOutfitId) ?? null : null),
    [evaluated, activeOutfitId],
  );

  const handleCopy = async (e: React.MouseEvent, outfit: OutfitEntry) => {
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(outfit.code);
      setCopiedId(outfit.id);
      toast.success(`已复制搭配码 ${outfit.code}`);
      setTimeout(() => setCopiedId((prev) => (prev === outfit.id ? null : prev)), 1500);
    } catch {
      toast.error('复制失败');
    }
  };

  const handleExportBackup = () => {
    const blob = new Blob(
      [JSON.stringify({ schema_version: 1, exported_at: new Date().toISOString(), items }, null, 2)],
      { type: 'application/json;charset=utf-8' },
    );
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `maanikki-outfits-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    URL.revokeObjectURL(url);
    toast.success(`已导出 ${items.length} 套搭配收藏`);
  };

  const handleImportBackup = async (file: File) => {
    try {
      const text = await file.text();
      const parsed = JSON.parse(text) as { items?: OutfitEntry[] } | OutfitEntry[];
      const list = Array.isArray(parsed) ? parsed : Array.isArray(parsed?.items) ? parsed.items : [];
      const added = await mergeImportedItems(list);
      toast.success(added > 0 ? `已合入 ${added} 套新搭配` : '备份中的搭配已全部存在');
    } catch (err) {
      toast.error(err instanceof Error ? err.message : '导入备份失败');
    }
  };

  const needMakeupHint = !rule.ignoreMakeup && ownership.hasCloset && !ownership.hasMakeupScan && counts.anyUsesMakeup;

  return (
    <FeaturePage
      icon={Palette}
      title="搭配收藏"
      onBack={onClose}
      actions={
        <>
          {accountData?.uid && (
            <span className="text-xs tabular-nums text-text-muted">UID {accountData.uid}</span>
          )}
          <button
            type="button"
            onClick={() => void refreshAccount().then(() => setRefreshToken((v) => v + 1)).catch(() => {})}
            disabled={accountLoading}
            title="刷新当前账号衣柜"
            aria-label="刷新当前账号衣柜"
            className="rounded-lg p-2 hover:bg-bg-hover disabled:opacity-40"
          >
            <RefreshCw className={clsx('h-4 w-4', accountLoading && 'animate-spin')} />
          </button>
          <input
            ref={backupInputRef}
            type="file"
            accept="application/json,.json"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              e.target.value = '';
              if (f) void handleImportBackup(f);
            }}
          />
          <button
            type="button"
            onClick={() => backupInputRef.current?.click()}
            className={secondaryButton}
            title="从 JSON 备份导入搭配收藏"
          >
            <Upload className="h-3.5 w-3.5" />导入备份
          </button>
          {items.length > 0 && (
            <button
              type="button"
              onClick={handleExportBackup}
              className={secondaryButton}
              title="将当前收藏导出为 JSON 文件备份或分享"
            >
              <Download className="h-3.5 w-3.5" />导出备份
            </button>
          )}
          <button type="button" onClick={() => setShowImport(true)} className={primaryButton}>
            <Plus className="h-3.5 w-3.5" />导入搭配码
          </button>
        </>
      }
    >
      {!ownership.hasCloset && (
        <StatusBar
          tone="warn"
          title="尚未读取当前账号衣柜"
          detail="可以先导入并解析搭配码；读取一次衣柜后，这里会自动核对每套搭配是否能穿并标出缺失部件。"
        >
          <button
            type="button"
            onClick={() => openCollectionHub('closet')}
            className={primaryButton}
          >
            <Shirt className="h-3.5 w-3.5" />去读取衣柜
          </button>
        </StatusBar>
      )}

      {needMakeupHint && (
        <StatusBar
          tone="idle"
          title="当前衣柜尚未读取妆容部件"
          detail="「服装焕新」不含妆容记录。若只想核对衣服是否齐全，可切换为「忽略妆容」，或前往衣柜运行一次「博物图鉴校准」。"
        >
          <button type="button" onClick={() => setPreset('no_makeup')} className={primaryButton}>
            切换为忽略妆容
          </button>
          <button type="button" onClick={() => openCollectionHub('closet')} className={secondaryButton}>
            前往衣柜校准
          </button>
        </StatusBar>
      )}

      {/* Match rule controls */}
      <section className="rounded-2xl border border-border bg-bg-secondary p-4 shadow-xs space-y-3">
        <div className="flex flex-wrap items-center gap-3">
          <span className="text-xs font-medium text-text-secondary">匹配规则</span>
          <Segmented<MatchPreset>
            label="搭配匹配规则"
            value={rule.preset}
            onChange={(value) => setPreset(value)}
            options={[
              ['all', '全部匹配'],
              ['no_makeup', '忽略妆容'],
              ['no_accessories', '忽略饰品'],
              ['core_only', '仅看主体'],
            ]}
          />
          <div className="flex flex-wrap items-center gap-3 text-xs text-text-secondary sm:ml-auto">
            <label className="inline-flex cursor-pointer items-center gap-1.5 select-none">
              <input
                type="checkbox"
                checked={rule.ignoreShoesAndSocks}
                onChange={(e) => patchRule({ ignoreShoesAndSocks: e.target.checked })}
                className="accent-accent"
              />
              忽略鞋袜
            </label>
            <label
              className="inline-flex cursor-pointer items-center gap-1.5 select-none"
              title="开启后，必须拥有搭配码指定的具体进化色或焕新色才算完全匹配；关闭时只要拥有同款原色或任一变体即视为可穿"
            >
              <input
                type="checkbox"
                checked={rule.requireExactVariant}
                onChange={(e) => patchRule({ requireExactVariant: e.target.checked })}
                className="accent-accent"
              />
              要求精确进化/焕新色
            </label>
          </div>
        </div>

        {/* Clickable summary tiles */}
        <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-4">
          <button
            type="button"
            onClick={() => setStatusFilter('all')}
            className={clsx(
              'rounded-xl text-left transition-all',
              statusFilter === 'all' && 'ring-2 ring-accent/60',
            )}
          >
            <Tile label="全部收藏" value={`${counts.total} 套`} detail="跨账号共享收藏夹" />
          </button>
          <button
            type="button"
            onClick={() => setStatusFilter('wearable')}
            className={clsx(
              'rounded-xl text-left transition-all',
              statusFilter === 'wearable' && 'ring-2 ring-success/70',
            )}
          >
            <Tile
              label="当前规则可穿"
              value={`${counts.wearable} 套`}
              detail={counts.total > 0 ? `占比 ${Math.round((counts.wearable / counts.total) * 100)}%` : '按当前账号衣柜实时核算'}
            />
          </button>
          <button
            type="button"
            onClick={() => setStatusFilter('near')}
            className={clsx(
              'rounded-xl text-left transition-all',
              statusFilter === 'near' && 'ring-2 ring-warning/70',
            )}
          >
            <Tile label="仅缺 1–2 件" value={`${counts.near} 套`} detail="微调散件即可穿" />
          </button>
          <button
            type="button"
            onClick={() => setStatusFilter('missing')}
            className={clsx(
              'rounded-xl text-left transition-all',
              statusFilter === 'missing' && 'ring-2 ring-error/60',
            )}
          >
            <Tile label="缺少 3 件以上" value={`${counts.missing} 套`} detail="可留作复刻种草清单" />
          </button>
        </div>

        {/* Search, tag pills, sort */}
        {items.length > 0 && (
          <div className="flex flex-wrap items-center gap-2 pt-1">
            <div className="relative min-w-[200px] flex-1">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-text-muted" />
              <input
                type="text"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="搜标题、搭配码、备注，或某件衣服 / 套装名…"
                className="w-full rounded-lg border border-border bg-bg-primary py-1.5 pl-8 pr-3 text-xs outline-none focus:border-accent"
              />
            </div>
            <select
              value={sortMode}
              onChange={(e) => setSortMode(e.target.value as SortMode)}
              aria-label="排序方式"
              className="rounded-lg border border-border bg-bg-primary px-2.5 py-1.5 text-xs outline-none focus:border-accent"
            >
              <option value="match">匹配度优先</option>
              <option value="missing_asc">缺件最少优先</option>
              <option value="recent">最近收藏优先</option>
            </select>
            {allTags.length > 0 && (
              <div className="flex w-full flex-wrap items-center gap-1.5 pt-1">
                <button
                  type="button"
                  onClick={() => setSelectedTag('all')}
                  className={clsx(
                    'rounded-full border px-2.5 py-0.5 text-[11px] transition-colors',
                    selectedTag === 'all'
                      ? 'border-accent bg-accent/15 font-medium text-accent'
                      : 'border-border bg-bg-primary text-text-secondary hover:bg-bg-hover',
                  )}
                >
                  全部标签
                </button>
                {allTags.map(([tag, count]) => (
                  <button
                    key={tag}
                    type="button"
                    onClick={() => setSelectedTag(selectedTag === tag ? 'all' : tag)}
                    className={clsx(
                      'rounded-full border px-2.5 py-0.5 text-[11px] transition-colors',
                      selectedTag === tag
                        ? 'border-accent bg-accent/15 font-medium text-accent'
                        : 'border-border bg-bg-primary text-text-secondary hover:bg-bg-hover',
                    )}
                  >
                    #{tag} <span className="opacity-70">{count}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
      </section>

      {/* Main grid or empty state */}
      {!loaded && loading ? (
        <div className="py-16 text-center text-sm text-text-secondary">正在加载搭配收藏…</div>
      ) : items.length === 0 ? (
        <section className="rounded-2xl border border-dashed border-border bg-bg-secondary px-6 py-12 text-center">
          <Palette className="mx-auto h-10 w-10 text-accent opacity-65" />
          <h3 className="mt-3 text-sm font-semibold">还没有收藏任何搭配码</h3>
          <p className="mx-auto mt-1.5 max-w-md text-xs leading-5 text-text-secondary">
            看到喜欢的 B站、小红书或社区搭配码，直接整段粘贴进来即可自动解析全部衣服、饰品、妆容与染色方案，瞬间筛选出当前账号能直接穿的搭配，再也不用进游戏一个个试。
          </p>
          <div className="mt-5 flex justify-center gap-2">
            <button type="button" onClick={() => setShowImport(true)} className={primaryButton}>
              <Plus className="h-3.5 w-3.5" />导入第一个搭配码
            </button>
          </div>
        </section>
      ) : filtered.length === 0 ? (
        <section className="rounded-2xl border border-border bg-bg-secondary px-6 py-10 text-center text-xs text-text-secondary">
          当前筛选条件下没有匹配的搭配，试试切换状态或清空搜索关键词。
        </section>
      ) : (
        <div className="grid grid-cols-1 gap-3.5 sm:grid-cols-2 lg:grid-cols-3">
          {filtered.map(({ outfit, ev }) => {
            const badge = statusBadge(ev);
            const gradientColors = ev.paletteHex.length >= 2
              ? ev.paletteHex.slice(0, 3)
              : ev.paletteHex.length === 1
                ? [ev.paletteHex[0], '#6366f1']
                : ['#6366f1', '#ec4899'];
            const pct = ev.requiredCount > 0 ? Math.round((ev.matchedCount / ev.requiredCount) * 100) : 100;

            return (
              <article
                key={outfit.id}
                onClick={() => setActiveOutfitId(outfit.id)}
                className="group flex cursor-pointer flex-col overflow-hidden rounded-2xl border border-border bg-bg-secondary shadow-xs transition-all hover:border-accent/50 hover:shadow-md"
              >
                {/* Cover banner */}
                <div className="relative h-40 overflow-hidden bg-bg-primary">
                  {outfit.coverDataUrl ? (
                    <img
                      src={outfit.coverDataUrl}
                      alt={outfit.title}
                      className="h-full w-full object-cover transition-transform duration-300 group-hover:scale-[1.03]"
                    />
                  ) : (
                    <div
                      className="flex h-full flex-col justify-end p-3.5 text-white"
                      style={{
                        background: `linear-gradient(135deg, ${gradientColors.join(', ')})`,
                      }}
                    >
                      <div className="flex flex-wrap gap-1 opacity-95">
                        {ev.suitNames.map((suit) => (
                          <span
                            key={suit}
                            className="rounded-full bg-black/28 px-2 py-0.5 text-[10px] backdrop-blur-xs"
                          >
                            {suit}
                          </span>
                        ))}
                      </div>
                      <p className="mt-1.5 flex items-center gap-1 text-[11px] opacity-80">
                        <Sparkles className="h-3 w-3" />
                        {ev.totalPieceCount} 件部件 · {ev.dyedPieceCount > 0 ? `${ev.dyedPieceCount} 件已染色` : '原色搭配'}
                      </p>
                    </div>
                  )}

                  {/* Top status & ratio badges */}
                  <div className="absolute left-2.5 top-2.5 flex items-center gap-1.5">
                    <span className={clsx('rounded-full px-2.5 py-0.5 text-[11px] font-medium shadow-xs', badge.cls)}>
                      {badge.text}
                    </span>
                  </div>
                  <span className="absolute right-2.5 top-2.5 rounded-full bg-black/55 px-2 py-0.5 text-[11px] font-medium tabular-nums text-white backdrop-blur-xs">
                    {ev.matchedCount}/{ev.requiredCount}
                  </span>
                </div>

                {/* Card body */}
                <div className="flex flex-1 flex-col gap-2.5 p-3.5">
                  <div className="flex items-start justify-between gap-2">
                    <h3 className="min-w-0 truncate text-sm font-semibold text-text-primary">
                      {outfit.title}
                    </h3>
                    <span className="shrink-0 rounded border border-border bg-bg-primary px-1.5 py-0.5 text-[10px] text-text-muted">
                      {outfit.region}
                    </span>
                  </div>

                  {/* Progress bar */}
                  <div className="h-1.5 overflow-hidden rounded-full bg-border/70">
                    <div
                      className={clsx(
                        'h-full rounded-full transition-all',
                        ev.status === 'wearable'
                          ? 'bg-success'
                          : ev.status === 'same_model' || ev.status === 'near'
                            ? 'bg-warning'
                            : 'bg-accent/70',
                      )}
                      style={{ width: `${pct}%` }}
                    />
                  </div>

                  {/* Missing pieces preview or wearable confirmation */}
                  {ev.missingPieces.length > 0 ? (
                    <div className="flex flex-wrap gap-1">
                      <span className="text-[11px] text-text-muted">缺：</span>
                      {ev.missingPieces.slice(0, 3).map((p) => (
                        <span
                          key={p.clothId}
                          className="rounded-md border border-error/25 bg-error/8 px-1.5 py-0.5 text-[11px] text-error"
                        >
                          {p.partName}·{p.name}
                        </span>
                      ))}
                      {ev.missingPieces.length > 3 && (
                        <span className="text-[11px] text-text-muted">+{ev.missingPieces.length - 3}</span>
                      )}
                    </div>
                  ) : ev.sameModelPieces.length > 0 && rule.requireExactVariant ? (
                    <div className="flex flex-wrap gap-1">
                      <span className="text-[11px] text-text-muted">缺进化/焕新：</span>
                      {ev.sameModelPieces.slice(0, 2).map((p) => (
                        <span
                          key={p.clothId}
                          className="rounded-md border border-warning/30 bg-warning/10 px-1.5 py-0.5 text-[11px] text-warning"
                        >
                          {p.name}
                        </span>
                      ))}
                      {ev.sameModelPieces.length > 2 && (
                        <span className="text-[11px] text-text-muted">+{ev.sameModelPieces.length - 2}</span>
                      )}
                    </div>
                  ) : (
                    <p className="text-[11px] text-success">
                      当前规则下所需 {ev.requiredCount} 件部件已全部满足
                    </p>
                  )}

                  {/* Tags + dye color dots */}
                  {(outfit.tags.length > 0 || ev.paletteHex.length > 0) && (
                    <div className="flex flex-wrap items-center gap-1.5">
                      {outfit.tags.slice(0, 3).map((tag) => (
                        <span
                          key={tag}
                          className="rounded-full bg-accent/10 px-2 py-0.5 text-[10px] text-accent"
                        >
                          #{tag}
                        </span>
                      ))}
                      {ev.paletteHex.length > 0 && (
                        <span className="ml-auto inline-flex items-center gap-1">
                          {ev.paletteHex.slice(0, 5).map((hex) => (
                            <span
                              key={hex}
                              className="h-3 w-3 rounded-full border border-black/15"
                              style={{ backgroundColor: hex }}
                              title={hex}
                            />
                          ))}
                        </span>
                      )}
                    </div>
                  )}

                  {/* Footer: share code + copy */}
                  <div className="mt-auto flex items-center justify-between gap-2 border-t border-border/70 pt-2.5">
                    <code className="truncate font-mono text-[11px] text-text-secondary">
                      {outfit.code}
                    </code>
                    <button
                      type="button"
                      onClick={(e) => void handleCopy(e, outfit)}
                      className="inline-flex shrink-0 items-center gap-1 rounded-lg border border-border bg-bg-primary px-2 py-1 text-[11px] font-medium text-text-primary transition-colors hover:border-accent hover:text-accent"
                    >
                      {copiedId === outfit.id ? <Check className="h-3 w-3 text-success" /> : <Copy className="h-3 w-3" />}
                      {copiedId === outfit.id ? '已复制' : '复制码'}
                    </button>
                  </div>
                </div>
              </article>
            );
          })}
        </div>
      )}

      <OutfitImportModal open={showImport} onClose={() => setShowImport(false)} payload={payload} />
      <OutfitDetailModal
        outfit={activePair?.outfit ?? null}
        evaluation={activePair?.ev ?? null}
        onClose={() => setActiveOutfitId(null)}
      />
    </FeaturePage>
  );
}
