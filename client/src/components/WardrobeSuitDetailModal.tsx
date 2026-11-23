import { useEffect, useMemo, useRef, useState } from 'react';
import { Calendar, Check, Shirt, Sparkles, X } from 'lucide-react';
import clsx from 'clsx';
import { tagLabel } from '@/services/scoring/diagnostics';
import { useSuitThumbMap, type SuitThumbsStatus } from '@/services/scoring/suitThumbs';
import { ATTRIBUTES, type ScoringCatalog } from '@/services/scoring/types';
import type { SuitBrowserEntry, SuitVariantEntry } from '@/services/scoring/wardrobeBrowserData';
import { ModalPortal, useModalFocusTrap } from './ui/Modal';
import { secondaryButton } from './ui/FeaturePage';
import { ImageIcon } from './WardrobePhotoCardModal';

const field = 'rounded-lg border border-border bg-bg-primary px-3 py-1.5 text-xs outline-none focus:border-accent';

export function suitBackdropClass(quality: number): string {
  if (quality >= 5) return 'bg-gradient-to-b from-amber-500/14 via-rose-500/8 to-bg-primary/60';
  if (quality === 4) return 'bg-gradient-to-b from-violet-500/14 via-accent/8 to-bg-primary/60';
  return 'bg-gradient-to-b from-sky-500/10 via-bg-primary/30 to-bg-primary/60';
}

export function LevelBadgeInput({ value, inferred, onChange }: {
  value: number | null;
  inferred?: string | null;
  onChange: (level: number | null) => void;
}) {
  if (inferred) return <span className="shrink-0 rounded-lg bg-accent/10 px-2 py-0.5 text-[11px] text-accent">已焕新</span>;
  return (
    <input
      aria-label="共享等级 0-11，留空按 0 级计"
      type="number"
      min="0"
      max="11"
      placeholder="0"
      value={value ?? ''}
      className={clsx(field, '!w-14 shrink-0 !px-1.5 !py-0.5 text-center tabular-nums')}
      onChange={(e) => {
        const raw = e.target.value;
        const lv = Number(raw);
        if (raw === '') onChange(null);
        else if (Number.isInteger(lv) && lv >= 0 && lv <= 11) onChange(lv);
      }}
    />
  );
}

export function WardrobeSuitDetailModal({
  suit,
  thumbUrl,
  thumbStatus,
  catalog,
  levels,
  onLevelChange,
  onShare,
  onClose,
}: {
  suit: SuitBrowserEntry | null;
  thumbUrl?: string;
  thumbStatus: SuitThumbsStatus | null;
  catalog: ScoringCatalog;
  levels: Record<string, number>;
  onLevelChange: (groupId: string, level: number | null) => void;
  onShare: (suit: SuitBrowserEntry, variant?: SuitVariantEntry) => void;
  onClose: () => void;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const [selectedVariantId, setSelectedVariantId] = useState<string | null>(null);
  useModalFocusTrap({ active: Boolean(suit), containerRef: panelRef, onEscape: onClose });

  useEffect(() => {
    setSelectedVariantId(null);
  }, [suit?.id]);

  const variantThumbKeys = useMemo(
    () => (suit ? suit.variants.map((v) => v.thumbKey) : []),
    [suit],
  );
  const variantThumbMap = useSuitThumbMap(variantThumbKeys, thumbStatus);

  if (!suit) return null;

  const activeVariant = suit.variants.find((v) => v.id === selectedVariantId) ?? suit.variants[0] ?? null;
  const displayName = activeVariant ? activeVariant.name : suit.name;
  const displayParts = activeVariant ? activeVariant.parts : suit.parts;
  const displayObtained = activeVariant ? activeVariant.obtainedCount : suit.obtainedCount;
  const displayTotal = activeVariant ? activeVariant.totalCount : suit.totalCount;
  const displayMissing = activeVariant ? activeVariant.missingCount : suit.missingCount;
  const displayComplete = activeVariant ? activeVariant.complete : suit.complete;
  const isBaseView = !activeVariant || activeVariant.kind === 'base';
  const activeThumbUrl = activeVariant
    ? (variantThumbMap[activeVariant.thumbKey] ?? (isBaseView ? thumbUrl : undefined))
    : thumbUrl;

  const progressPct = displayTotal ? (displayObtained / displayTotal) * 100 : 0;
  const releaseText = [suit.releaseSeason, suit.releaseDate].filter(Boolean).join(' · ');
  const statusBadgeText = isBaseView
    ? (displayComplete ? '已集齐' : displayObtained > 0 ? `差 ${displayMissing} 件` : '暂未获得')
    : activeVariant.kind === 'glow'
      ? (displayComplete ? '已集齐焕新' : displayObtained > 0 ? `焕新中 ${displayObtained}/${displayTotal}` : '未解锁')
      : (displayComplete ? '已解锁' : displayObtained > 0 ? `收集 ${displayObtained}/${displayTotal}` : '未解锁');

  return (
    <ModalPortal>
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4 backdrop-blur-xs" onClick={onClose}>
        <div
          ref={panelRef}
          role="dialog"
          aria-modal="true"
          aria-label={`${displayName} 套装详情`}
          className="flex max-h-[86vh] w-full max-w-3xl flex-col overflow-hidden rounded-2xl border border-border bg-bg-secondary text-text-primary shadow-xl"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="flex items-center justify-between gap-3 border-b border-border px-5 py-3.5">
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              <span className="rounded-md bg-accent/10 px-2 py-0.5 text-xs font-medium text-accent">
                {suit.quality}★ · {ATTRIBUTES[suit.major] ?? '综合'}
              </span>
              <h3 className="truncate text-base font-semibold">{displayName}</h3>
              <span className={clsx('rounded-md px-2 py-0.5 text-[11px] font-medium', displayComplete ? 'bg-success/15 text-success' : displayObtained > 0 ? 'bg-accent/10 text-accent' : 'bg-bg-primary text-text-muted')}>
                {statusBadgeText}
              </span>
            </div>
            <button type="button" onClick={onClose} aria-label="关闭" className="rounded-lg p-1.5 text-text-muted hover:bg-bg-hover hover:text-text-primary">
              <X className="h-4 w-4" />
            </button>
          </div>

          {suit.variants.length > 0 && (
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border bg-bg-primary/40 px-5 py-2.5">
              <div className="flex flex-wrap items-center gap-1.5" role="tablist" aria-label="套装形态切换">
                {suit.variants.map((variant) => {
                  const selected = activeVariant?.id === variant.id;
                  const unlocked = variant.complete;
                  const partial = !unlocked && variant.obtainedCount > 0;
                  return (
                    <button
                      key={variant.id}
                      type="button"
                      role="tab"
                      aria-selected={selected}
                      onClick={() => setSelectedVariantId(variant.id)}
                      className={clsx(
                        'inline-flex items-center gap-1.5 rounded-xl border px-2.5 py-1 text-xs transition-all',
                        selected
                          ? 'border-accent bg-accent/12 font-medium text-accent shadow-xs ring-1 ring-accent/25'
                          : 'border-border bg-bg-secondary text-text-secondary hover:bg-bg-hover hover:text-text-primary',
                      )}
                      title={variant.name}
                    >
                      <span
                        className={clsx(
                          'h-1.5 w-1.5 shrink-0 rounded-full',
                          unlocked
                            ? variant.kind === 'evo'
                              ? 'bg-amber-500'
                              : variant.kind === 'glow'
                                ? 'bg-accent'
                                : 'bg-success'
                            : partial
                              ? 'bg-accent/60'
                              : 'bg-text-muted/35',
                        )}
                      />
                      <span>{variant.label}</span>
                      {unlocked && <Check className="h-3 w-3 shrink-0 text-success" />}
                      {partial && (
                        <span className="text-[10px] tabular-nums opacity-80">
                          {variant.obtainedCount}/{variant.totalCount}
                        </span>
                      )}
                    </button>
                  );
                })}
              </div>
              <div className="flex items-center gap-2 text-[11px] text-text-muted">
                {suit.evoTotal > 0 && (
                  <span className={clsx(suit.evoComplete ? 'font-medium text-amber-500' : suit.evoUnlocked > 0 && 'text-text-primary')}>
                    进化 {suit.evoUnlocked}/{suit.evoTotal}
                  </span>
                )}
                {suit.hasGlow && (
                  <span className={clsx(suit.glowUnlocked && 'font-medium text-accent')}>
                    焕新 {suit.glowUnlocked ? '已集齐' : `${suit.glowObtainedCount}/${suit.glowTotalCount}`}
                  </span>
                )}
              </div>
            </div>
          )}

          <div className="grid min-h-0 flex-1 grid-cols-1 gap-5 overflow-y-auto p-5 md:grid-cols-[232px_minmax(0,1fr)]">
            <div className="flex flex-col gap-3">
              <div className={clsx('relative aspect-[5/7] overflow-hidden rounded-xl border border-border', suitBackdropClass(suit.quality))}>
                {activeThumbUrl ? (
                  <img src={activeThumbUrl} alt={displayName} className="h-full w-full object-contain object-bottom px-1 pt-2 drop-shadow-sm" />
                ) : (
                  <div className="flex h-full w-full flex-col items-center justify-center gap-2 px-3 text-center text-text-muted">
                    <Shirt className="h-10 w-10 opacity-35" />
                    <span className="text-[11px]">{isBaseView ? '暂无立绘预览' : '暂无该形态立绘'}</span>
                  </div>
                )}
              </div>

              <div className="space-y-2 rounded-xl border border-border bg-bg-primary/60 p-3 text-xs">
                <div className="flex items-center justify-between">
                  <span className="text-text-secondary">{isBaseView ? '收集进度' : `${activeVariant.label}进度`}</span>
                  <strong className={clsx('tabular-nums', displayComplete ? 'text-success' : 'text-text-primary')}>
                    {displayObtained} / {displayTotal}
                  </strong>
                </div>
                <div className="h-1.5 overflow-hidden rounded-full bg-border">
                  <div className={clsx('h-full rounded-full transition-all', displayComplete ? 'bg-success' : 'bg-accent')} style={{ width: `${progressPct}%` }} />
                </div>
                {releaseText && (
                  <div className="flex items-center gap-1.5 pt-1 text-[11px] text-text-secondary">
                    <Calendar className="h-3.5 w-3.5 shrink-0 text-text-muted" />
                    <span className="truncate">{releaseText}</span>
                  </div>
                )}
                {!!suit.tags.length && (
                  <div className="flex flex-wrap gap-1 pt-1">
                    {suit.tags.map((t) => (
                      <span key={t} className="inline-flex items-center gap-0.5 rounded bg-bg-secondary px-1.5 py-0.5 text-[10px] text-text-secondary border border-border/60">
                        <Sparkles className="h-2.5 w-2.5 text-accent/70" />
                        {tagLabel(catalog, t)}
                      </span>
                    ))}
                  </div>
                )}
              </div>

              <button type="button" onClick={() => onShare(suit, activeVariant ?? undefined)} className={clsx(secondaryButton, 'w-full justify-center')}>
                <ImageIcon className="h-3.5 w-3.5" />生成套装分享卡
              </button>
            </div>

            <div className="flex min-h-0 flex-col rounded-xl border border-border bg-bg-primary/40">
              <div className="flex items-center justify-between border-b border-border px-4 py-2.5 text-xs">
                <span className="font-medium text-text-primary">
                  {isBaseView ? '套装部件清单' : `${activeVariant.label}部件清单`}（{displayTotal} 件）
                </span>
                <span className="text-[11px] text-text-muted">已拥有部件可修改养成等级</span>
              </div>
              <div className="divide-y divide-border overflow-y-auto px-4 py-1 text-xs">
                {displayParts.map((part) => (
                  <div key={part.gameId ?? part.id} className="flex items-center justify-between gap-2.5 py-2">
                    <span className="w-12 shrink-0 truncate text-text-muted">{part.partName}</span>
                    <div className="min-w-0 flex-1">
                      <span className={clsx('block truncate font-medium', part.owned ? 'text-text-primary' : 'text-text-muted')}>
                        {part.name}
                      </span>
                      {part.source && <span className="block truncate text-[11px] text-text-muted">{part.source}</span>}
                    </div>
                    {part.owned && part.groupId && (
                      <LevelBadgeInput
                        value={levels[part.groupId] ?? null}
                        inferred={part.inferred}
                        onChange={(lv) => onLevelChange(part.groupId!, lv)}
                      />
                    )}
                    <span className={clsx('inline-flex w-14 shrink-0 items-center justify-end gap-1 text-[11px]', part.owned ? 'text-success' : 'text-text-muted')}>
                      {part.owned && <Check className="h-3 w-3" />}
                      {part.owned ? '已拥有' : '未获得'}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      </div>
    </ModalPortal>
  );
}
