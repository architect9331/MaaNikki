import { useEffect, useRef, type CSSProperties } from 'react';
import { Check, Eye, EyeOff, RefreshCw, Sparkles, Star, X } from 'lucide-react';
import { toast } from 'sonner';
import clsx from 'clsx';
import { useGiantMeteorState } from '@/stores/giantMeteorStore';
import { ModalPortal, useModalFocusTrap } from './ui/Modal';

export function MeteorIcon({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      <path d="m19 3-6 6m8-2-4 4m0-9-6 6" />
      <path d="m9 9 1.4 3.6L14 14l-3.6 1.4L9 19l-1.4-3.6L4 14l3.6-1.4Z" />
      <path d="m4 20 1-1m11-3 1-1" />
    </svg>
  );
}

function MeteorEmblem({
  discovered = false,
  onClick,
}: {
  discovered?: boolean;
  onClick?: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={discovered ? '点击召唤一波流星雨' : '点击试放一波流星'}
      className={clsx(
        'meteor-emblem cursor-pointer rounded-full transition-transform hover:scale-105 active:scale-95 focus-visible:outline-2 focus-visible:outline-accent',
        discovered && 'meteor-emblem--discovered',
      )}
    >
      <span className="meteor-emblem-orbit" aria-hidden="true" />
      <MeteorIcon className="meteor-emblem-comet" />
      {discovered && (
        <>
          <Star className="meteor-emblem-star meteor-emblem-star--one" aria-hidden="true" />
          <Star className="meteor-emblem-star meteor-emblem-star--two" aria-hidden="true" />
          <Sparkles className="meteor-emblem-sparkles" aria-hidden="true" />
        </>
      )}
    </button>
  );
}

interface AmbientMeteorSpec {
  top: string;
  left: string;
  length: number;
  duration: string;
  delay: string;
  hero?: boolean;
}

const AMBIENT_METEORS: AmbientMeteorSpec[] = [
  { top: '6%', left: '78%', length: 168, duration: '6.8s', delay: '0.4s', hero: true },
  { top: '14%', left: '94%', length: 122, duration: '7.6s', delay: '2.1s' },
  { top: '3%', left: '54%', length: 136, duration: '8.4s', delay: '3.7s' },
  { top: '26%', left: '98%', length: 148, duration: '7.2s', delay: '5.2s' },
  { top: '10%', left: '36%', length: 112, duration: '9.1s', delay: '6.5s' },
  { top: '42%', left: '96%', length: 128, duration: '8.0s', delay: '1.3s' },
];

const BURST_METEORS: AmbientMeteorSpec[] = [
  { top: '5%', left: '74%', length: 225, duration: '1.55s', delay: '0s', hero: true },
  { top: '12%', left: '92%', length: 165, duration: '1.35s', delay: '0.16s' },
  { top: '2%', left: '52%', length: 175, duration: '1.45s', delay: '0.34s' },
  { top: '24%', left: '96%', length: 150, duration: '1.30s', delay: '0.52s' },
  { top: '8%', left: '34%', length: 145, duration: '1.40s', delay: '0.72s' },
];

interface StarMoteSpec {
  top: string;
  left: string;
  size: number;
  delay: string;
  duration: string;
  warm?: boolean;
}

const STAR_MOTES: StarMoteSpec[] = [
  { top: '4.5%', left: '12%', size: 11, delay: '0s', duration: '3.2s', warm: true },
  { top: '6%', left: '28%', size: 8, delay: '1.1s', duration: '2.8s' },
  { top: '3.5%', left: '47%', size: 12, delay: '0.5s', duration: '3.6s', warm: true },
  { top: '7%', left: '66%', size: 9, delay: '1.7s', duration: '3.1s' },
  { top: '5%', left: '84%', size: 13, delay: '0.3s', duration: '2.9s', warm: true },
  { top: '11%', left: '93%', size: 10, delay: '1.4s', duration: '3.5s' },
  { top: '18%', left: '3.5%', size: 9, delay: '0.9s', duration: '3.4s', warm: true },
  { top: '22%', left: '96.5%', size: 11, delay: '2.0s', duration: '3.0s', warm: true },
  { top: '42%', left: '2.2%', size: 8, delay: '1.6s', duration: '3.8s' },
  { top: '54%', left: '97.2%', size: 9, delay: '0.7s', duration: '3.3s', warm: true },
  { top: '78%', left: '3.2%', size: 10, delay: '1.3s', duration: '3.5s' },
  { top: '86%', left: '94.5%', size: 12, delay: '0.4s', duration: '3.1s', warm: true },
  { top: '92%', left: '18%', size: 9, delay: '2.2s', duration: '3.6s' },
  { top: '93%', left: '76%', size: 8, delay: '1.0s', duration: '2.9s', warm: true },
];

/** Title bar glowing badge shown across every page on Giant Meteor night (or preview). */
export function GiantMeteorTitleBadge() {
  const { found, previewActive, openDialog } = useGiantMeteorState();
  if (!found && !previewActive) return null;

  return (
    <button
      type="button"
      onClick={openDialog}
      title={found ? '今夜家园已出现巨陨星预告，点击查看详情' : '正在预览巨陨星全界面彩蛋，点击查看或退出'}
      className="meteor-title-badge inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-[11px] font-medium transition-transform hover:scale-[1.03] active:scale-95"
    >
      <Sparkles className="h-3 w-3 shrink-0" aria-hidden="true" />
      <span>{found ? '今夜巨陨星降临' : '巨陨星彩蛋预览'}</span>
    </button>
  );
}

/**
 * Whole-window Giant Meteor Easter Egg stage:
 * - Syncs root `.giant-meteor-night` atmosphere class across the entire window
 * - Renders non-blocking starlight canopy, golden frame halo, twinkling stars & falling shooting stars
 * - Announces first discovery via toast + celebratory meteor volley
 * - Hosts the shared Giant Meteor detail modal accessible from any page
 */
export function GiantMeteorStage() {
  const {
    data,
    error,
    loading,
    refresh,
    today,
    record,
    found,
    absent,
    checkedAt,
    dialogOpen,
    previewActive,
    mutedToday,
    effectActive,
    burstSeq,
    closeDialog,
    setPreviewActive,
    toggleMuteForDay,
    triggerBurst,
  } = useGiantMeteorState();

  const panelRef = useRef<HTMLDivElement>(null);
  const announced = useRef('');
  useModalFocusTrap({ active: dialogOpen, containerRef: panelRef, onEscape: closeDialog });

  // Sync whole-window root class so title bar, borders, and surfaces share the starfall ambiance.
  useEffect(() => {
    const root = document.documentElement;
    if (effectActive) {
      root.classList.add('giant-meteor-night');
    } else {
      root.classList.remove('giant-meteor-night');
    }
    return () => {
      root.classList.remove('giant-meteor-night');
    };
  }, [effectActive]);

  // First discovery toast + celebratory meteor burst once per account per game day.
  useEffect(() => {
    const account = data?.account_key ?? 'legacy';
    const key = `maanikki:giant-meteor:${account}:${today}`;
    if (!found || announced.current === key) return;
    announced.current = key;
    try {
      if (window.sessionStorage.getItem(key)) return;
      window.sessionStorage.setItem(key, 'announced');
    } catch {
      /* The mounted component also deduplicates without browser storage. */
    }
    triggerBurst();
    toast('今夜有巨陨星！', {
      id: key,
      description: '家园已出现巨陨星预告，记得夜间前往查看。',
      icon: <MeteorIcon className="h-5 w-5" />,
      className: 'meteor-discovery-toast',
      duration: 8000,
    });
  }, [found, today, data?.account_key, triggerBurst]);

  const visualDiscovered = found || previewActive;
  const description = error
    ? '账号状态读取失败，可刷新后重试。'
    : found
      ? '家园已出现巨陨星预告，记得夜间前往查看。点击上方陨星徽章还能召唤一波流星雨。'
      : absent
        ? '今日家园检测未发现巨陨星。'
        : record
          ? '上次检测未能确认地图状态，可重新运行家园日常。'
          : '运行家园日常后，这里会展示今日的检测结果。';

  return (
    <>
      {effectActive && (
        <div className="meteor-night-stage" aria-hidden="true">
          {/* Soft golden-starlight celestial canopy & window frame halo */}
          <div className="meteor-night-aurora" />
          <div className="meteor-night-rim" />

          {/* Twinkling four-point starlight motes around window margins */}
          {STAR_MOTES.map((mote, idx) => (
            <span
              key={idx}
              className={clsx('meteor-star-mote', mote.warm && 'meteor-star-mote--warm')}
              style={
                {
                  top: mote.top,
                  left: mote.left,
                  width: `${mote.size}px`,
                  height: `${mote.size}px`,
                  '--mote-delay': mote.delay,
                  '--mote-duration': mote.duration,
                } as CSSProperties
              }
            />
          ))}

          {/* Continuous staggered shooting stars */}
          {AMBIENT_METEORS.map((m, idx) => (
            <span
              key={idx}
              className={clsx('meteor-streak', m.hero && 'meteor-streak--hero')}
              style={
                {
                  top: m.top,
                  left: m.left,
                  '--streak-length': `${m.length}px`,
                  '--streak-duration': m.duration,
                  '--streak-delay': m.delay,
                } as CSSProperties
              }
            />
          ))}

          {/* Celebratory volley burst on discovery / preview / emblem click */}
          {burstSeq > 0 && (
            <div key={burstSeq} className="meteor-burst-layer">
              {BURST_METEORS.map((m, idx) => (
                <span
                  key={idx}
                  className={clsx(
                    'meteor-streak meteor-streak--burst',
                    m.hero && 'meteor-streak--hero',
                  )}
                  style={
                    {
                      top: m.top,
                      left: m.left,
                      '--streak-length': `${m.length}px`,
                      '--streak-duration': m.duration,
                      '--streak-delay': m.delay,
                    } as CSSProperties
                  }
                />
              ))}
            </div>
          )}
        </div>
      )}

      {/* Subtle floating exit bar only while previewing on a non-meteor day */}
      {previewActive && !found && (
        <div className="fixed bottom-3 right-3 z-40 flex items-center gap-1.5 rounded-full border border-[var(--meteor-border)] bg-bg-secondary/92 px-3 py-1.5 text-xs shadow-lg backdrop-blur-md meteor-status--found">
          <Sparkles className="h-3.5 w-3.5 shrink-0" />
          <span className="font-medium">巨陨星彩蛋预览中</span>
          <button
            type="button"
            onClick={triggerBurst}
            className="ml-1 rounded-full bg-black/8 px-2 py-0.5 text-[11px] font-medium transition-colors hover:bg-black/15 dark:bg-white/10 dark:hover:bg-white/20"
          >
            再来一波
          </button>
          <button
            type="button"
            onClick={() => setPreviewActive(false)}
            className="rounded-full bg-black/8 px-2 py-0.5 text-[11px] font-medium transition-colors hover:bg-black/15 dark:bg-white/10 dark:hover:bg-white/20"
          >
            退出预览
          </button>
        </div>
      )}

      {/* Shared Giant Meteor detail modal */}
      {dialogOpen && (
        <ModalPortal>
          <div
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4 backdrop-blur-sm"
            onMouseDown={(event) => {
              if (event.target === event.currentTarget) closeDialog();
            }}
          >
            <div
              ref={panelRef}
              role="dialog"
              aria-modal="true"
              aria-labelledby="giant-meteor-title"
              aria-describedby="giant-meteor-description"
              className={clsx(
                'meteor-dialog relative w-full max-w-sm overflow-hidden rounded-xl border border-border bg-bg-secondary text-text-primary shadow-xl',
                visualDiscovered && 'meteor-dialog--found',
              )}
            >
              <button
                onClick={closeDialog}
                aria-label="关闭"
                className="absolute right-3 top-3 z-10 rounded-lg p-1.5 text-text-secondary hover:bg-bg-hover"
              >
                <X className="h-4 w-4" />
              </button>
              <div className="meteor-dialog-hero flex flex-col items-center px-7 pb-5 pt-7 text-center">
                <MeteorEmblem
                  discovered={visualDiscovered}
                  onClick={() => {
                    if (!effectActive) {
                      if (found) toggleMuteForDay(today);
                      else setPreviewActive(true);
                    } else {
                      triggerBurst();
                    }
                  }}
                />
                <h2 id="giant-meteor-title" className="mt-3 text-xl font-semibold tracking-wide">
                  {found ? '巨陨星降临' : previewActive ? '巨陨星夜 · 彩蛋预览' : '巨陨星状态'}
                </h2>
                {visualDiscovered && (
                  <span className="meteor-rarity mt-2 inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-medium">
                    <Star className="h-3 w-3 fill-current" />
                    {found ? '稀有发现' : '特效预览'}
                    <Star className="h-3 w-3 fill-current" />
                  </span>
                )}
                <p id="giant-meteor-description" className="mt-3 text-sm leading-6 text-text-secondary">
                  {description}
                </p>
              </div>
              <div className="mx-5 rounded-lg border border-border bg-bg-primary/70 px-4 py-3 text-xs">
                {data?.uid && (
                  <div className="mb-2 flex justify-between gap-3">
                    <span className="text-text-secondary">游戏 UID</span>
                    <span className="tabular-nums">{data.uid}</span>
                  </div>
                )}
                <div className="flex justify-between gap-3">
                  <span className="text-text-secondary">游戏日期</span>
                  <span className="tabular-nums">{today}</span>
                </div>
                <div className="mt-2 flex justify-between gap-3">
                  <span className="text-text-secondary">检测时间</span>
                  <span className="tabular-nums">{checkedAt ?? '尚未检测'}</span>
                </div>
              </div>
              <div className="flex items-center gap-2 px-5 py-4">
                {found ? (
                  <button
                    type="button"
                    onClick={() => toggleMuteForDay(today)}
                    className="mr-auto inline-flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1.5 text-xs text-text-secondary transition-colors hover:bg-bg-hover hover:text-text-primary"
                  >
                    {mutedToday ? <Eye className="h-3.5 w-3.5" /> : <EyeOff className="h-3.5 w-3.5" />}
                    {mutedToday ? '开启星雨' : '暂停星雨'}
                  </button>
                ) : (
                  <button
                    type="button"
                    onClick={() => setPreviewActive(!previewActive)}
                    className={clsx(
                      'mr-auto inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-xs transition-colors',
                      previewActive
                        ? 'border-accent/40 bg-accent/10 text-accent hover:bg-accent/15'
                        : 'border-border text-text-secondary hover:bg-bg-hover hover:text-text-primary',
                    )}
                  >
                    <Sparkles className="h-3.5 w-3.5" />
                    {previewActive ? '结束预览' : '预览星雨'}
                  </button>
                )}
                <button
                  onClick={() => void refresh().catch(() => {})}
                  disabled={loading}
                  title="刷新本地记录"
                  aria-label="刷新本地记录"
                  className="rounded-lg p-2 text-text-secondary hover:bg-bg-hover disabled:opacity-50"
                >
                  <RefreshCw className={clsx('h-4 w-4', loading && 'animate-spin')} />
                </button>
                <button
                  onClick={closeDialog}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-border px-4 py-2 text-xs font-medium hover:bg-bg-hover"
                >
                  <Check className="h-3.5 w-3.5" />
                  知道了
                </button>
              </div>
            </div>
          </div>
        </ModalPortal>
      )}
    </>
  );
}
