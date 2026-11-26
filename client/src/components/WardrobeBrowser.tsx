import { useEffect, useMemo, useState } from 'react';
import {
  ArrowDownWideNarrow,
  ArrowUpNarrowWide,
  Check,
  Loader2,
  RefreshCw,
  Search,
  Shirt,
} from 'lucide-react';
import clsx from 'clsx';
import { useAccountStateStore } from '@/stores/accountStateStore';
import { tagLabel } from '@/services/scoring/diagnostics';
import { buildOwnedProfile } from '@/services/scoring/profile';
import { useSuitThumbMap, useSuitThumbsStatus } from '@/services/scoring/suitThumbs';
import { ATTRIBUTES, type Match, type OutfitResult } from '@/services/scoring/types';
import { useWardrobePlanStore } from '@/services/scoring/wardrobePlanStore';
import {
  buildPieceEntries,
  buildSuitEntries,
  sortSuitEntries,
  type SuitBrowserEntry,
  type SuitSortBy,
  type SuitSortOrder,
  type SuitVariantEntry,
} from '@/services/scoring/wardrobeBrowserData';
import { Segmented, Tile } from './ui/FeaturePage';
import { WardrobePhotoCardModal } from './WardrobePhotoCardModal';
import { LevelBadgeInput, qualityLabel, suitBackdropClass, WardrobeSuitDetailModal } from './WardrobeSuitDetailModal';

const field = 'rounded-lg border border-border bg-bg-primary px-3 py-1.5 text-xs outline-none focus:border-accent';
const card = 'rounded-2xl border border-border bg-bg-secondary p-4 shadow-xs';
const SUIT_PAGE_SIZE = 24;
const PIECE_PAGE_SIZE = 36;
const SORT_STORAGE_KEY = 'maanikki_wardrobe_suit_sort';

function readStoredSort(): { by: SuitSortBy; dir: SuitSortOrder } {
  try {
    const raw = localStorage.getItem(SORT_STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as { by?: unknown; dir?: unknown };
      const by: SuitSortBy = parsed.by === 'release' ? 'release' : 'quality';
      const dir: SuitSortOrder = parsed.dir === 'asc' ? 'asc' : 'desc';
      return { by, dir };
    }
  } catch {
    // Ignore storage read failures.
  }
  return { by: 'quality', dir: 'desc' };
}

function suitSubtitle(suit: SuitBrowserEntry, catalog: Parameters<typeof tagLabel>[0]): string {
  if (suit.releaseSeason) return suit.releaseSeason;
  if (suit.releaseDate) return suit.releaseDate.slice(0, 7);
  if (suit.tags.length > 0) return tagLabel(catalog, suit.tags[0]);
  return '';
}

export function WardrobeBrowser({ refreshToken }: { refreshToken: number }) {
  const activeUid = useAccountStateStore((state) => state.data?.uid);
  const { payload, settings, loading, load, setLevel } = useWardrobePlanStore();
  const [view, setView] = useState<'suits' | 'pieces'>('suits');
  const [statusFilter, setStatusFilter] = useState('all');
  const [qualityFilter, setQualityFilter] = useState('all');
  const [majorFilter, setMajorFilter] = useState('all');
  const [tagFilter, setTagFilter] = useState('all');
  const [partFilter, setPartFilter] = useState('all');
  const [search, setSearch] = useState('');
  const [initialSort] = useState(readStoredSort);
  const [sortBy, setSortBy] = useState<SuitSortBy>(initialSort.by);
  const [releaseDir, setReleaseDir] = useState<SuitSortOrder>(initialSort.dir);
  const [limit, setLimit] = useState(SUIT_PAGE_SIZE);
  const [selectedSuitId, setSelectedSuitId] = useState<string | null>(null);
  const [sharingTarget, setSharingTarget] = useState<{ suit: SuitBrowserEntry; variant?: SuitVariantEntry } | null>(null);
  const { status: thumbStatus, retrying: retryingThumbs, retrySync } = useSuitThumbsStatus();

  useEffect(() => { void load(`${activeUid ?? ''}:${refreshToken}`); }, [activeUid, refreshToken, load]);

  useEffect(() => {
    try {
      localStorage.setItem(SORT_STORAGE_KEY, JSON.stringify({ by: sortBy, dir: releaseDir }));
    } catch {
      // Ignore storage write failures.
    }
  }, [sortBy, releaseDir]);

  useEffect(() => {
    setLimit(view === 'suits' ? SUIT_PAGE_SIZE : PIECE_PAGE_SIZE);
  }, [view, statusFilter, qualityFilter, majorFilter, tagFilter, partFilter, search, sortBy, releaseDir]);

  const profile = useMemo(() => (payload ? buildOwnedProfile(payload, settings) : null), [payload, settings]);
  const suits = useMemo(() => (payload && profile ? buildSuitEntries(payload, profile) : []), [payload, profile]);
  const pieces = useMemo(() => (payload && profile ? buildPieceEntries(payload, profile) : []), [payload, profile]);
  const sortedSuits = useMemo(() => sortSuitEntries(suits, sortBy, releaseDir), [suits, sortBy, releaseDir]);

  const q = search.trim();
  const filteredSuits = useMemo(() => sortedSuits.filter((s) =>
    (qualityFilter === 'all' || String(s.quality) === qualityFilter)
    && (majorFilter === 'all' || String(s.major) === majorFilter)
    && (tagFilter === 'all' || s.tags.includes(Number(tagFilter)))
    && (statusFilter === 'all'
      || statusFilter === 'complete' && s.complete
      || statusFilter === 'evolved' && s.evoUnlocked > 0
      || statusFilter === 'evo_complete' && s.evoComplete
      || statusFilter === 'glow_unlocked' && s.glowUnlocked
      || statusFilter === 'incomplete' && !s.complete && s.obtainedCount > 0
      || statusFilter === 'near' && (s.missingCount === 1 || s.missingCount === 2)
      || statusFilter === 'unowned' && s.obtainedCount === 0)
    && (!q
      || s.name.includes(q)
      || (s.releaseSeason ?? '').includes(q)
      || s.parts.some((p) => p.name.includes(q))
      || s.variants.some((v) => v.name.includes(q) || v.parts.some((p) => p.name.includes(q))))),
  [sortedSuits, qualityFilter, majorFilter, tagFilter, statusFilter, q]);

  const visibleSuits = useMemo(
    () => (view === 'suits' ? filteredSuits.slice(0, limit) : []),
    [view, filteredSuits, limit],
  );
  const visibleSuitKeys = useMemo(() => visibleSuits.map((s) => s.id), [visibleSuits]);
  const thumbMap = useSuitThumbMap(visibleSuitKeys, thumbStatus);
  const selectedSuit = useMemo(
    () => (selectedSuitId ? suits.find((s) => s.id === selectedSuitId) ?? null : null),
    [suits, selectedSuitId],
  );

  if (loading && !payload) {
    return (
      <div className="flex items-center justify-center gap-2 py-16 text-sm text-text-secondary">
        <Loader2 className="h-4 w-4 animate-spin" />正在加载衣柜图鉴
      </div>
    );
  }
  if (!payload || !profile) {
    return <p className={clsx(card, 'text-sm text-text-secondary')}>暂时无法加载衣柜记录，读取衣柜后点右上角刷新。</p>;
  }

  const tagEntries = Object.entries(payload.catalog.tag_names ?? {}).sort((a, b) => Number(a[0]) - Number(b[0]));
  const filteredPieces = pieces.filter((p) => {
    const lv = p.inferred ? 11 : p.groupId ? (settings.levels[p.groupId] ?? null) : null;
    return (qualityFilter === 'all' || String(p.quality) === qualityFilter)
      && (majorFilter === 'all' || String(p.major) === majorFilter)
      && (tagFilter === 'all' || p.tags.includes(Number(tagFilter)))
      && (partFilter === 'all' || String(p.partCode) === partFilter)
      && (statusFilter === 'all'
        || statusFilter === 'owned' && p.owned
        || statusFilter === 'unowned' && !p.owned
        || statusFilter === 'refreshed' && p.owned && lv === 11
        || statusFilter === 'unknown_lv' && p.owned && lv === null
        || statusFilter === 'chest_missing' && !p.owned && (p.source ?? '').includes('宝箱'))
      && (!q || p.name.includes(q) || (p.suitName ?? '').includes(q) || (p.source ?? '').includes(q));
  });

  const completeSuitsCount = suits.filter((s) => s.complete).length;
  const unlockedEvoCount = suits.reduce((sum, s) => sum + s.evoUnlocked, 0);
  const totalEvoCount = suits.reduce((sum, s) => sum + s.evoTotal, 0);
  const ownedPiecesCount = pieces.filter((p) => p.owned).length;
  const refreshedGroupsCount = profile.groups.filter((g) => g.level === 11).length;
  const pageStep = view === 'suits' ? SUIT_PAGE_SIZE : PIECE_PAGE_SIZE;
  const totalCount = view === 'suits' ? filteredSuits.length : filteredPieces.length;

  return <div className="space-y-4">
    <section className={clsx(card, 'space-y-3')}>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Tile label="已集齐套装" value={<>{completeSuitsCount}<span className="ml-1 text-xs font-normal text-text-muted">/ {suits.length}</span></>} />
        <Tile label="已解锁进化色" value={<>{unlockedEvoCount}<span className="ml-1 text-xs font-normal text-text-muted">/ {totalEvoCount}</span></>} />
        <Tile label="已收录散件" value={<>{ownedPiecesCount}<span className="ml-1 text-xs font-normal text-text-muted">/ {pieces.length}</span></>} />
        <Tile label="已焕新养成组" value={<>{refreshedGroupsCount}<span className="ml-1 text-xs font-normal text-text-muted">/ {profile.groups.length}</span></>} />
      </div>
      <p className="text-[11px] leading-5 text-text-secondary">顶部套装收集按本地资料中的 {suits.length} 套原色计分款统计，点击套装可切换查看进化色与焕新色立绘及部件进度；解锁新进化色后点上方「博物图鉴校准」即可同步更新。</p>
      <div className="flex flex-wrap items-center gap-2">
        <Segmented label="浏览视图" value={view} options={[['suits', '按套装查看'], ['pieces', '按散件查看']] as const} onChange={(v) => { setView(v); setStatusFilter('all'); }} />
        <select aria-label="状态筛选" className={field} value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
          {view === 'suits' ? <>
            <option value="all">全部状态</option><option value="complete">已集齐</option><option value="evolved">已解锁进化</option><option value="evo_complete">已满进</option><option value="glow_unlocked">已集齐焕新色</option><option value="near">差 1～2 件集齐</option><option value="incomplete">收集中</option><option value="unowned">暂未获得</option>
          </> : <>
            <option value="all">全部拥有状态</option><option value="owned">已拥有</option><option value="unowned">未拥有</option><option value="chest_missing">未收集的大世界宝箱散件</option><option value="refreshed">已焕新 (11级)</option><option value="unknown_lv">已拥有但未填等级</option>
          </>}
        </select>
        <select aria-label="星级筛选" className={field} value={qualityFilter} onChange={(e) => setQualityFilter(e.target.value)}>
          <option value="all">全部星级</option><option value="8">星冕</option><option value="5">5 星</option><option value="4">4 星</option><option value="3">3 星</option>
        </select>
        <select aria-label="主属性筛选" className={field} value={majorFilter} onChange={(e) => setMajorFilter(e.target.value)}>
          <option value="all">全部主属性</option>{ATTRIBUTES.map((name, idx) => <option key={name} value={idx}>{name}</option>)}
        </select>
        {!!tagEntries.length && (
          <select aria-label="标签筛选" className={field} value={tagFilter} onChange={(e) => setTagFilter(e.target.value)}>
            <option value="all">全部标签</option>
            {tagEntries.map(([id, name]) => <option key={id} value={id}>{name}</option>)}
          </select>
        )}
        {view === 'pieces' && (
          <select aria-label="部位筛选" className={field} value={partFilter} onChange={(e) => setPartFilter(e.target.value)}>
            <option value="all">全部部位</option>
            {Object.entries(payload.catalog.part_names).filter(([k]) => Number(k) < 200).map(([k, name]) => <option key={k} value={k}>{name}</option>)}
          </select>
        )}
        <label className="flex min-w-44 flex-1 items-center gap-1.5"><Search className="h-3.5 w-3.5 text-text-muted" />
          <input className={clsx(field, 'w-full')} placeholder={view === 'suits' ? '搜索套装、版本或部件名称' : '搜索套装或部件名称'} value={search} onChange={(e) => setSearch(e.target.value)} />
        </label>
      </div>
      {view === 'suits' && (
        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border/60 pt-2.5">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs text-text-secondary">排序</span>
            <Segmented
              label="排序方式"
              value={sortBy}
              options={[['quality', '星级优先'], ['release', '发布时间']] as const}
              onChange={setSortBy}
            />
            {sortBy === 'release' && (
              <button
                type="button"
                onClick={() => setReleaseDir((prev) => (prev === 'desc' ? 'asc' : 'desc'))}
                className="inline-flex items-center gap-1 rounded-lg border border-border bg-bg-primary px-2.5 py-1 text-xs text-text-secondary hover:bg-bg-hover hover:text-text-primary"
                title="切换发布时间先后顺序"
              >
                {releaseDir === 'desc'
                  ? <><ArrowDownWideNarrow className="h-3.5 w-3.5 text-accent" />最新在前</>
                  : <><ArrowUpNarrowWide className="h-3.5 w-3.5 text-accent" />最早在前</>}
              </button>
            )}
          </div>
          <div className="flex items-center gap-2 text-[11px] text-text-muted">
            {(thumbStatus?.state === 'downloading' || retryingThumbs) && (
              <span className="inline-flex items-center gap-1 text-accent">
                <Loader2 className="h-3 w-3 animate-spin" />正在缓存套装预览图…
              </span>
            )}
            {thumbStatus?.state === 'error' && !thumbStatus.available && !retryingThumbs && (
              <button
                type="button"
                onClick={() => void retrySync()}
                className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-0.5 text-warning hover:bg-bg-hover"
                title={thumbStatus.error ?? '重试拉取套装预览图'}
              >
                <RefreshCw className="h-3 w-3" />预览图未缓存 · 点击重试
              </button>
            )}
            <span>显示 {Math.min(limit, totalCount)} / {totalCount} 套</span>
          </div>
        </div>
      )}
    </section>

    {view === 'suits' ? (
      visibleSuits.length > 0 ? (
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
          {visibleSuits.map((suit) => {
            const thumbUrl = thumbMap[suit.id];
            const unowned = suit.obtainedCount === 0;
            const subtitle = suitSubtitle(suit, payload.catalog);
            return (
              <button
                key={suit.id}
                type="button"
                onClick={() => setSelectedSuitId(suit.id)}
                className="group flex flex-col overflow-hidden rounded-2xl border border-border bg-bg-secondary text-left shadow-xs transition-all duration-200 hover:-translate-y-0.5 hover:border-accent/55 hover:shadow-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
              >
                <div className={clsx('relative aspect-[5/7] w-full overflow-hidden', suitBackdropClass(suit.quality))}>
                  {thumbUrl ? (
                    <img
                      src={thumbUrl}
                      alt={suit.name}
                      loading="lazy"
                      decoding="async"
                      className={clsx(
                        'h-full w-full object-contain object-bottom px-1.5 pt-2 drop-shadow-xs transition-all duration-300 group-hover:scale-[1.04]',
                        unowned && 'grayscale-[65%] opacity-60 group-hover:grayscale-[25%] group-hover:opacity-85',
                      )}
                    />
                  ) : (
                    <div className={clsx('flex h-full w-full items-center justify-center text-text-muted', unowned && 'opacity-50')}>
                      <Shirt className="h-10 w-10 opacity-30" />
                    </div>
                  )}
                  <span className="absolute left-2 top-2 rounded-md border border-border/60 bg-bg-secondary/85 px-1.5 py-0.5 text-[10px] font-medium text-accent backdrop-blur-xs">
                    {qualityLabel(suit.quality)} · {ATTRIBUTES[suit.major] ?? '综合'}
                  </span>
                  <span className={clsx(
                    'absolute right-2 top-2 rounded-md border px-1.5 py-0.5 text-[10px] font-medium tabular-nums backdrop-blur-xs',
                    suit.complete
                      ? 'border-success/30 bg-success/15 text-success'
                      : suit.obtainedCount > 0
                        ? 'border-border/60 bg-bg-secondary/85 text-text-primary'
                        : 'border-border/60 bg-bg-secondary/80 text-text-muted',
                  )}>
                    {suit.complete ? '已集齐' : unowned ? '未获得' : `${suit.obtainedCount}/${suit.totalCount}`}
                  </span>
                  {(suit.evoTotal > 0 || suit.glowObtainedCount > 0) && (
                    <div className="pointer-events-none absolute inset-x-2 bottom-2 flex items-center justify-between gap-1.5">
                      {suit.evoTotal > 0 ? (
                        <span
                          className={clsx(
                            'inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[10px] font-medium tabular-nums backdrop-blur-xs',
                            suit.evoComplete
                              ? 'border-amber-500/40 bg-amber-500/15 text-amber-600 dark:text-amber-400'
                              : suit.evoUnlocked > 0
                                ? 'border-amber-500/30 bg-bg-secondary/90 text-amber-600 dark:text-amber-400'
                                : 'border-border/60 bg-bg-secondary/80 text-text-muted',
                          )}
                        >
                          <span className="inline-flex items-center gap-0.5" aria-hidden="true">
                            {Array.from({ length: suit.evoTotal }, (_, idx) => (
                              <span
                                key={idx}
                                className={clsx(
                                  'h-1.5 w-1.5 rounded-full',
                                  idx < suit.evoUnlocked ? 'bg-amber-500' : 'bg-text-muted/35',
                                )}
                              />
                            ))}
                          </span>
                          <span>
                            {suit.evoComplete
                              ? (suit.evoTotal > 1 ? '满进' : '已进化')
                              : suit.evoUnlocked > 0
                                ? `${suit.evoUnlocked}进`
                                : `0/${suit.evoTotal}进`}
                          </span>
                        </span>
                      ) : <span />}
                      {suit.glowObtainedCount > 0 && (
                        <span
                          className={clsx(
                            'rounded-md border px-1.5 py-0.5 text-[10px] font-medium tabular-nums backdrop-blur-xs',
                            suit.glowUnlocked
                              ? 'border-accent/35 bg-accent/15 text-accent'
                              : 'border-border/60 bg-bg-secondary/85 text-text-secondary',
                          )}
                        >
                          {suit.glowUnlocked ? '焕新' : `焕 ${suit.glowObtainedCount}/${suit.glowTotalCount}`}
                        </span>
                      )}
                    </div>
                  )}
                </div>
                <div className="space-y-1.5 p-3">
                  <strong className="block truncate text-sm font-semibold text-text-primary">{suit.name}</strong>
                  <div className="flex items-center justify-between gap-2 text-[11px] text-text-muted">
                    <span className="truncate">{subtitle}</span>
                    <span className={clsx('shrink-0 tabular-nums', suit.complete && 'font-medium text-success')}>
                      {suit.obtainedCount} / {suit.totalCount}
                    </span>
                  </div>
                  <div className="h-1.5 overflow-hidden rounded-full bg-border">
                    <div
                      className={clsx('h-full rounded-full transition-all', suit.complete ? 'bg-success' : 'bg-accent')}
                      style={{ width: `${(suit.obtainedCount / suit.totalCount) * 100}%` }}
                    />
                  </div>
                </div>
              </button>
            );
          })}
        </div>
      ) : (
        <p className={clsx(card, 'py-12 text-center text-xs text-text-secondary')}>没有符合筛选条件的套装。</p>
      )
    ) : (
      <div className="overflow-hidden rounded-xl border border-border bg-bg-secondary divide-y divide-border">
        {filteredPieces.slice(0, limit).map((piece) => (
          <div key={piece.id} className="flex flex-wrap items-center gap-3 px-4 py-2.5 text-xs">
            <span className="rounded bg-accent/10 px-1.5 py-0.5 text-[11px] font-medium text-accent">{qualityLabel(piece.quality)} {ATTRIBUTES[piece.major] ?? ''}</span>
            <span className="w-24 shrink-0 truncate text-text-muted">{piece.partName}</span>
            <div className="min-w-0 flex-1">
              <span className={clsx('font-medium', piece.owned ? 'text-text-primary' : 'text-text-muted')}>{piece.name}</span>
              {piece.tags.map((t) => (
                <span key={t} className="ml-1.5 rounded bg-bg-primary px-1.5 py-0.2 text-[10px] text-text-muted">
                  {tagLabel(payload.catalog, t)}
                </span>
              ))}
              {piece.suitName && <span className="ml-2 text-text-muted">[{piece.suitName}]</span>}
              {piece.source && <span className="ml-2 text-text-muted">· {piece.source}</span>}
              {piece.colorCount > 1 && <span className="ml-2 text-text-muted">· 已有 {piece.colorCount} 色</span>}
            </div>
            {piece.owned && piece.groupId && (
              <LevelBadgeInput value={settings.levels[piece.groupId] ?? null} inferred={piece.inferred} onChange={(lv) => setLevel(piece.groupId!, lv)} />
            )}
            <span className={clsx('inline-flex w-14 shrink-0 items-center justify-end gap-1', piece.owned ? 'text-success' : 'text-text-muted')}>
              {piece.owned && <Check className="h-3.5 w-3.5" />}{piece.owned ? '已拥有' : '未获得'}
            </span>
          </div>
        ))}
      </div>
    )}

    {totalCount > limit && (
      <button type="button" onClick={() => setLimit((prev) => prev + pageStep)}
        className="w-full rounded-xl border border-border bg-bg-secondary py-2 text-xs text-text-secondary hover:bg-bg-hover">
        加载更多（已显示 {limit} / {totalCount}）
      </button>
    )}

    <WardrobeSuitDetailModal
      suit={selectedSuit}
      thumbUrl={selectedSuit ? thumbMap[selectedSuit.id] : undefined}
      thumbStatus={thumbStatus}
      catalog={payload.catalog}
      levels={settings.levels}
      onLevelChange={setLevel}
      onShare={(suit, variant) => {
        setSelectedSuitId(null);
        setSharingTarget({ suit, variant });
      }}
      onClose={() => setSelectedSuitId(null)}
    />

    {sharingTarget && (() => {
      const { suit, variant } = sharingTarget;
      const useVariant = variant && variant.kind !== 'base';
      const shareName = useVariant ? variant.name : suit.name;
      const shareParts = useVariant ? variant.parts : suit.parts;
      const shareObtained = useVariant ? variant.obtainedCount : suit.obtainedCount;
      const shareTotal = useVariant ? variant.totalCount : suit.totalCount;
      const shareComplete = useVariant ? variant.complete : suit.complete;
      return (
        <WardrobePhotoCardModal
          open={true}
          onClose={() => setSharingTarget(null)}
          outfit={{
            score: shareObtained,
            globalScore: 0,
            stars: [],
            cost: {},
            unknownCostGroups: [],
            choice: null,
            exact: true,
            targetMet: shareComplete,
            pieces: shareParts.map((p) => {
              const effectiveLv = p.inferred ? 11 : p.groupId ? (settings.levels[p.groupId] ?? 0) : 0;
              return {
                item: { id: p.id, name: p.name, group: p.groupId, level: p.inferred ? 11 : p.groupId ? (settings.levels[p.groupId] ?? null) : null },
                type: payload.catalog.items[p.id]?.type ?? 1,
                from: effectiveLv,
                level: effectiveLv,
                score: p.owned ? 1 : 0,
                attributeScores: [0, 0, 0, 0, 0],
                grownAttributes: [0, 0, 0, 0, 0],
                tagScore: 0,
                multiplier: 1,
                cost: {},
              };
            }),
          } satisfies OutfitResult}
          match={{
            name: shareName,
            group: useVariant ? `${qualityLabel(suit.quality)} 套装收藏 · ${variant.label}` : `${qualityLabel(suit.quality)} 套装收藏`,
            area: 1,
            weights: [0, 0, 0, 0, 0],
            tags: {},
            eurekaTags: {},
            tagBase: 0,
            dressMultiplier: 1,
            bonuses: [],
            choices: [],
            stars: [],
          } satisfies Match}
          catalog={payload.catalog}
          modeLabel={ATTRIBUTES[suit.major] ?? '综合'}
          customHeadline={`已收集 ${shareObtained} / ${shareTotal}`}
        />
      );
    })()}
  </div>;
}
