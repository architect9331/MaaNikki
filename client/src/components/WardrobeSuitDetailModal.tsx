import { useRef } from 'react';
import { Calendar, Check, Shirt, Sparkles, X } from 'lucide-react';
import clsx from 'clsx';
import { tagLabel } from '@/services/scoring/diagnostics';
import { ATTRIBUTES, type ScoringCatalog } from '@/services/scoring/types';
import type { SuitBrowserEntry } from '@/services/scoring/wardrobeBrowserData';
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
  catalog,
  levels,
  onLevelChange,
  onShare,
  onClose,
}: {
  suit: SuitBrowserEntry | null;
  thumbUrl?: string;
  catalog: ScoringCatalog;
  levels: Record<string, number>;
  onLevelChange: (groupId: string, level: number | null) => void;
  onShare: (suit: SuitBrowserEntry) => void;
  onClose: () => void;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  useModalFocusTrap({ active: Boolean(suit), containerRef: panelRef, onEscape: onClose });

  if (!suit) return null;

  const progressPct = suit.totalCount ? (suit.obtainedCount / suit.totalCount) * 100 : 0;
  const releaseText = [suit.releaseSeason, suit.releaseDate].filter(Boolean).join(' · ');

  return (
    <ModalPortal>
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4 backdrop-blur-xs" onClick={onClose}>
        <div
          ref={panelRef}
          role="dialog"
          aria-modal="true"
          aria-label={`${suit.name} 套装详情`}
          className="flex max-h-[86vh] w-full max-w-3xl flex-col overflow-hidden rounded-2xl border border-border bg-bg-secondary text-text-primary shadow-xl"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="flex items-center justify-between gap-3 border-b border-border px-5 py-3.5">
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              <span className="rounded-md bg-accent/10 px-2 py-0.5 text-xs font-medium text-accent">
                {suit.quality}★ · {ATTRIBUTES[suit.major] ?? '综合'}
              </span>
              <h3 className="truncate text-base font-semibold">{suit.name}</h3>
              <span className={clsx('rounded-md px-2 py-0.5 text-[11px] font-medium', suit.complete ? 'bg-success/15 text-success' : suit.obtainedCount > 0 ? 'bg-accent/10 text-accent' : 'bg-bg-primary text-text-muted')}>
                {suit.complete ? '已集齐' : suit.obtainedCount > 0 ? `差 ${suit.missingCount} 件` : '暂未获得'}
              </span>
            </div>
            <button type="button" onClick={onClose} aria-label="关闭" className="rounded-lg p-1.5 text-text-muted hover:bg-bg-hover hover:text-text-primary">
              <X className="h-4 w-4" />
            </button>
          </div>

          <div className="grid min-h-0 flex-1 grid-cols-1 gap-5 overflow-y-auto p-5 md:grid-cols-[232px_minmax(0,1fr)]">
            <div className="flex flex-col gap-3">
              <div className={clsx('relative aspect-[5/7] overflow-hidden rounded-xl border border-border', suitBackdropClass(suit.quality))}>
                {thumbUrl ? (
                  <img src={thumbUrl} alt={suit.name} className="h-full w-full object-contain object-bottom px-1 pt-2" />
                ) : (
                  <div className="flex h-full w-full flex-col items-center justify-center gap-2 text-text-muted">
                    <Shirt className="h-10 w-10 opacity-35" />
                    <span className="text-[11px]">暂无立绘预览</span>
                  </div>
                )}
              </div>

              <div className="space-y-2 rounded-xl border border-border bg-bg-primary/60 p-3 text-xs">
                <div className="flex items-center justify-between">
                  <span className="text-text-secondary">收集进度</span>
                  <strong className={clsx('tabular-nums', suit.complete ? 'text-success' : 'text-text-primary')}>
                    {suit.obtainedCount} / {suit.totalCount}
                  </strong>
                </div>
                <div className="h-1.5 overflow-hidden rounded-full bg-border">
                  <div className={clsx('h-full rounded-full transition-all', suit.complete ? 'bg-success' : 'bg-accent')} style={{ width: `${progressPct}%` }} />
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

              <button type="button" onClick={() => onShare(suit)} className={clsx(secondaryButton, 'w-full justify-center')}>
                <ImageIcon className="h-3.5 w-3.5" />生成套装分享卡
              </button>
            </div>

            <div className="flex min-h-0 flex-col rounded-xl border border-border bg-bg-primary/40">
              <div className="flex items-center justify-between border-b border-border px-4 py-2.5 text-xs">
                <span className="font-medium text-text-primary">套装部件清单（{suit.totalCount} 件）</span>
                <span className="text-[11px] text-text-muted">已拥有部件可修改养成等级</span>
              </div>
              <div className="divide-y divide-border overflow-y-auto px-4 py-1 text-xs">
                {suit.parts.map((part) => (
                  <div key={part.id} className="flex items-center justify-between gap-2.5 py-2">
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
