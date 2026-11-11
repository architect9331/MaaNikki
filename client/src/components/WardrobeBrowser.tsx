import { useEffect, useMemo, useState } from 'react';
import { Check, Loader2, Search } from 'lucide-react';
import clsx from 'clsx';
import { useAccountStateStore } from '@/stores/accountStateStore';
import { loadScoring, saveScoring } from '@/services/scoring/api';
import { tagLabel } from '@/services/scoring/diagnostics';
import { buildOwnedProfile } from '@/services/scoring/profile';
import { ATTRIBUTES, emptySettings, type Match, type OutfitResult, type ScoringPayload, type ScoringSettings } from '@/services/scoring/types';
import { buildPieceEntries, buildSuitEntries, type SuitBrowserEntry } from '@/services/scoring/wardrobeBrowserData';
import { Segmented, Stat } from './ui/FeaturePage';
import { ImageIcon, WardrobePhotoCardModal } from './WardrobePhotoCardModal';

const field = 'rounded-lg border border-border bg-bg-primary px-3 py-1.5 text-xs outline-none focus:border-accent';
const card = 'rounded-xl border border-border bg-bg-secondary p-4';
const PAGE_SIZE = 36;

function LevelBadgeInput({ value, inferred, onChange }: { value: number | null; inferred?: string | null; onChange: (level: number | null) => void }) {
  if (inferred) return <span className="shrink-0 rounded-lg bg-accent/10 px-2 py-0.5 text-[11px] text-accent">已焕新</span>;
  return <input aria-label="共享等级 0-11，留空按 0 级计" type="number" min="0" max="11" placeholder="0" value={value ?? ''}
    className={clsx(field, '!w-14 shrink-0 !px-1.5 !py-0.5 text-center tabular-nums')}
    onChange={(e) => {
      const raw = e.target.value;
      const lv = Number(raw);
      if (raw === '') onChange(null);
      else if (Number.isInteger(lv) && lv >= 0 && lv <= 11) onChange(lv);
    }} />;
}

export function WardrobeBrowser({ refreshToken }: { refreshToken: number }) {
  const activeUid = useAccountStateStore((state) => state.data?.uid);
  const [payload, setPayload] = useState<ScoringPayload | null>(null);
  const [settings, setSettings] = useState<ScoringSettings>(emptySettings);
  const [loading, setLoading] = useState(true);
  const [view, setView] = useState<'suits' | 'pieces'>('suits');
  const [statusFilter, setStatusFilter] = useState('all');
  const [qualityFilter, setQualityFilter] = useState('all');
  const [majorFilter, setMajorFilter] = useState('all');
  const [tagFilter, setTagFilter] = useState('all');
  const [partFilter, setPartFilter] = useState('all');
  const [search, setSearch] = useState('');
  const [limit, setLimit] = useState(PAGE_SIZE);
  const [sharingSuit, setSharingSuit] = useState<SuitBrowserEntry | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    void loadScoring().then((next) => {
      if (cancelled) return;
      setPayload(next);
      setSettings(next.settings ?? emptySettings());
    }).catch(() => {}).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [activeUid, refreshToken]);

  useEffect(() => { setLimit(PAGE_SIZE); }, [view, statusFilter, qualityFilter, majorFilter, tagFilter, partFilter, search]);

  const profile = useMemo(() => payload ? buildOwnedProfile(payload, settings) : null, [payload, settings]);
  const suits = useMemo(() => payload && profile ? buildSuitEntries(payload, profile) : [], [payload, profile]);
  const pieces = useMemo(() => payload && profile ? buildPieceEntries(payload, profile) : [], [payload, profile]);

  function updateLevel(groupId: string, level: number | null) {
    if (!payload) return;
    const levels = { ...settings.levels };
    if (level === null) delete levels[groupId]; else levels[groupId] = level;
    const next = { ...settings, levels };
    setSettings(next);
    void saveScoring(payload.account_key, next).catch(() => {});
  }

  if (loading) return <div className="flex items-center justify-center gap-2 py-16 text-sm text-text-secondary"><Loader2 className="h-4 w-4 animate-spin" />正在加载衣柜图鉴</div>;
  if (!payload || !profile) return <p className={clsx(card, 'text-sm text-text-secondary')}>请先在「衣柜读取」中读取当前账号的衣柜。</p>;

  const q = search.trim();
  const tagEntries = Object.entries(payload.catalog.tag_names ?? {}).sort((a, b) => Number(a[0]) - Number(b[0]));
  const filteredSuits = suits.filter((s) =>
    (qualityFilter === 'all' || String(s.quality) === qualityFilter)
    && (majorFilter === 'all' || String(s.major) === majorFilter)
    && (tagFilter === 'all' || s.tags.includes(Number(tagFilter)))
    && (statusFilter === 'all'
      || statusFilter === 'complete' && s.complete
      || statusFilter === 'incomplete' && !s.complete && s.obtainedCount > 0
      || statusFilter === 'near' && (s.missingCount === 1 || s.missingCount === 2)
      || statusFilter === 'unowned' && s.obtainedCount === 0)
    && (!q || s.name.includes(q) || s.parts.some((p) => p.name.includes(q))));

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
  const ownedPiecesCount = pieces.filter((p) => p.owned).length;
  const refreshedGroupsCount = profile.groups.filter((g) => g.level === 11).length;

  return <div className="space-y-4">
    <div className="grid grid-cols-3 gap-3">
      <Stat label="已集齐套装" value={<>{completeSuitsCount}<span className="ml-1 text-sm font-normal text-text-muted">/ {suits.length}</span></>} />
      <Stat label="已收录散件" value={<>{ownedPiecesCount}<span className="ml-1 text-sm font-normal text-text-muted">/ {pieces.length}</span></>} />
      <Stat label="已焕新养成组" value={<>{refreshedGroupsCount}<span className="ml-1 text-sm font-normal text-text-muted">/ {profile.groups.length}</span></>} />
    </div>

    <section className={clsx(card, 'space-y-3')}>
      <div className="flex flex-wrap items-center gap-2">
        <Segmented label="浏览视图" value={view} options={[['suits', '按套装查看'], ['pieces', '按散件查看']] as const} onChange={(v) => { setView(v); setStatusFilter('all'); }} />
        <select aria-label="状态筛选" className={field} value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
          {view === 'suits' ? <>
            <option value="all">全部状态</option><option value="complete">已集齐</option><option value="near">差 1～2 件集齐</option><option value="incomplete">收集中</option><option value="unowned">暂未获得</option>
          </> : <>
            <option value="all">全部拥有状态</option><option value="owned">已拥有</option><option value="unowned">未拥有</option><option value="chest_missing">未收集的大世界宝箱散件</option><option value="refreshed">已焕新 (11级)</option><option value="unknown_lv">已拥有但未填等级</option>
          </>}
        </select>
        <select aria-label="星级筛选" className={field} value={qualityFilter} onChange={(e) => setQualityFilter(e.target.value)}>
          <option value="all">全部星级</option><option value="5">5 星</option><option value="4">4 星</option><option value="3">3 星</option>
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
          <input className={clsx(field, 'w-full')} placeholder="搜索套装或部件名称" value={search} onChange={(e) => setSearch(e.target.value)} />
        </label>
      </div>
    </section>

    {view === 'suits' ? (
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        {filteredSuits.slice(0, limit).map((suit) => (
          <details key={suit.id} className={clsx(card, 'group !p-3.5')}>
            <summary className="flex cursor-pointer list-none items-center justify-between gap-2">
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="rounded bg-accent/10 px-1.5 py-0.5 text-[11px] font-medium text-accent">{suit.quality}★ · {ATTRIBUTES[suit.major] ?? '综合'}</span>
                  <strong className="truncate text-sm">{suit.name}</strong>
                  {suit.tags.map((t) => (
                    <span key={t} className="rounded bg-bg-primary px-1.5 py-0.2 text-[10px] text-text-muted">
                      {tagLabel(payload.catalog, t)}
                    </span>
                  ))}
                </div>
                <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-border">
                  <div className={clsx('h-full rounded-full', suit.complete ? 'bg-success' : 'bg-accent')} style={{ width: `${suit.obtainedCount / suit.totalCount * 100}%` }} />
                </div>
              </div>
              <span className={clsx('shrink-0 text-xs font-semibold tabular-nums', suit.complete ? 'text-success' : 'text-text-secondary')}>
                {suit.obtainedCount} / {suit.totalCount}
              </span>
            </summary>
            <div className="mt-3 divide-y divide-border border-t border-border pt-2 text-xs">
              {suit.parts.map((part) => (
                <div key={part.id} className="flex items-center justify-between gap-2 py-1.5">
                  <span className="w-14 shrink-0 truncate text-text-muted">{part.partName}</span>
                  <span className={clsx('min-w-0 flex-1 truncate', part.owned ? 'text-text-primary' : 'text-text-muted')}>
                    {part.name}
                    {part.source && <span className="ml-1.5 text-[11px] text-text-muted">· {part.source}</span>}
                  </span>
                  {part.owned && part.groupId && (
                    <LevelBadgeInput value={settings.levels[part.groupId] ?? null} inferred={part.inferred} onChange={(lv) => updateLevel(part.groupId!, lv)} />
                  )}
                  <span className={clsx('w-12 shrink-0 text-right text-[11px]', part.owned ? 'text-success' : 'text-text-muted')}>
                    {part.owned ? '已拥有' : '未获得'}
                  </span>
                </div>
              ))}
              <div className="flex justify-end pt-2">
                <button type="button" onClick={() => setSharingSuit(suit)}
                  className="inline-flex items-center gap-1 text-xs text-text-secondary hover:text-accent">
                  <ImageIcon className="h-3.5 w-3.5" />生成套装分享卡
                </button>
              </div>
            </div>
          </details>
        ))}
      </div>
    ) : (
      <div className="overflow-hidden rounded-xl border border-border bg-bg-secondary divide-y divide-border">
        {filteredPieces.slice(0, limit).map((piece) => (
          <div key={piece.id} className="flex flex-wrap items-center gap-3 px-4 py-2.5 text-xs">
            <span className="rounded bg-accent/10 px-1.5 py-0.5 text-[11px] font-medium text-accent">{piece.quality}★ {ATTRIBUTES[piece.major] ?? ''}</span>
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
              <LevelBadgeInput value={settings.levels[piece.groupId] ?? null} inferred={piece.inferred} onChange={(lv) => updateLevel(piece.groupId!, lv)} />
            )}
            <span className={clsx('inline-flex w-14 shrink-0 items-center justify-end gap-1', piece.owned ? 'text-success' : 'text-text-muted')}>
              {piece.owned && <Check className="h-3.5 w-3.5" />}{piece.owned ? '已拥有' : '未获得'}
            </span>
          </div>
        ))}
      </div>
    )}

    {(view === 'suits' ? filteredSuits.length : filteredPieces.length) > limit && (
      <button type="button" onClick={() => setLimit((prev) => prev + PAGE_SIZE)}
        className="w-full rounded-xl border border-border bg-bg-secondary py-2 text-xs text-text-secondary hover:bg-bg-hover">
        加载更多（已显示 {limit} / {view === 'suits' ? filteredSuits.length : filteredPieces.length}）
      </button>
    )}
    {sharingSuit && (
      <WardrobePhotoCardModal
        open={true}
        onClose={() => setSharingSuit(null)}
        outfit={{
          score: sharingSuit.obtainedCount,
          globalScore: 0,
          stars: [],
          cost: {},
          unknownCostGroups: [],
          choice: null,
          exact: true,
          targetMet: sharingSuit.complete,
          pieces: sharingSuit.parts.map((p) => {
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
          name: sharingSuit.name,
          group: `${sharingSuit.quality}★ 套装收藏`,
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
        modeLabel={ATTRIBUTES[sharingSuit.major] ?? '综合'}
        customHeadline={`已收集 ${sharingSuit.obtainedCount} / ${sharingSuit.totalCount}`}
      />
    )}
  </div>;
}
