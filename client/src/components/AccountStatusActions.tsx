import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Check, RefreshCw, Sparkles, Star, X } from 'lucide-react';
import { toast } from 'sonner';
import clsx from 'clsx';
import { gameDay, useAccountStateStore } from '@/stores/accountStateStore';
import { getBlessingProgress } from '@/utils/blessingProgress';
import { ModalPortal, useModalFocusTrap } from './ui/Modal';
import { Tooltip } from './ui/Tooltip';
import { ExplorationControl } from './ExplorationControl';

function MeteorIcon({ className }: { className?: string }) {
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7"
    strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden="true">
    <path d="m19 3-6 6m8-2-4 4m0-9-6 6" />
    <path d="m9 9 1.4 3.6L14 14l-3.6 1.4L9 19l-1.4-3.6L4 14l3.6-1.4Z" />
    <path d="m4 20 1-1m11-3 1-1" />
  </svg>;
}

function MeteorEmblem({ discovered = false }: { discovered?: boolean }) {
  return <span className={clsx('meteor-emblem', discovered && 'meteor-emblem--discovered')} aria-hidden="true">
    <span className="meteor-emblem-orbit" />
    <MeteorIcon className="meteor-emblem-comet" />
    {discovered && <>
      <Star className="meteor-emblem-star meteor-emblem-star--one" />
      <Star className="meteor-emblem-star meteor-emblem-star--two" />
      <Sparkles className="meteor-emblem-sparkles" />
    </>}
  </span>;
}

function GiantMeteorStatus() {
  const { data, error, loading, refresh } = useAccountStateStore();
  const [open, setOpen] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);
  const announced = useRef('');
  const close = useCallback(() => setOpen(false), []);
  useModalFocusTrap({ active: open, containerRef: panelRef, onEscape: close });

  const today = gameDay().toISOString().slice(0, 10);
  const observation = data?.observations?.giant_meteor;
  const record = observation?.game_day === today && Number.isFinite(Date.parse(observation.checked_at))
    ? observation : null;
  const found = !error && record?.outcome === 'found';
  const absent = !error && record?.outcome === 'not_found';
  const label = error ? '巨陨星状态读取失败' : found ? '巨陨星降临！'
    : absent ? '今日未发现巨陨星' : record ? '巨陨星检测未确认' : '今日尚未检测巨陨星';
  const description = error ? '账号状态读取失败，可刷新后重试。'
    : found ? '家园已出现巨陨星预告，记得夜间前往查看。'
    : absent ? '今日家园检测未发现巨陨星。'
    : record ? '上次检测未能确认地图状态，可重新运行家园日常。'
    : '运行家园日常后，这里会展示今日的检测结果。';
  const checkedAt = record ? new Date(record.checked_at).toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai', hour12: false,
  }) : null;

  useEffect(() => {
    if (!found || announced.current === today) return;
    announced.current = today;
    const key = `maanikki:giant-meteor:${today}`;
    try {
      if (window.sessionStorage.getItem(key)) return;
      window.sessionStorage.setItem(key, 'announced');
    } catch { /* The mounted component also deduplicates without browser storage. */ }
    toast('今夜有巨陨星！', {
      id: key,
      description: '家园已出现巨陨星预告，记得夜间前往查看。',
      icon: <MeteorIcon className="w-5 h-5" />,
      className: 'meteor-discovery-toast',
      duration: 8000,
    });
  }, [found, today]);

  return <>
    <Tooltip side="bottom" content={<div className="space-y-1">
      <p className="font-medium">{label}</p>
      <p className="text-text-secondary">{checkedAt ? `检测于 ${checkedAt}` : description}</p>
      <p className="text-text-muted">点击查看 · 每日 04:00 刷新</p>
    </div>}>
      <button onClick={() => setOpen(true)} aria-label={label} aria-haspopup="dialog"
        className={clsx('meteor-status relative flex shrink-0 items-center gap-1.5 rounded-md p-2 transition-colors focus-visible:outline-2 focus-visible:outline-accent',
          found ? 'meteor-status--found' : 'text-text-secondary hover:bg-bg-hover')}>
        <MeteorIcon className="relative z-10 w-5 h-5 shrink-0" />
        {found ? <>
          <span className="relative z-10 whitespace-nowrap text-sm font-semibold">巨陨星降临！</span>
          <Sparkles className="relative z-10 w-3 h-3" aria-hidden="true" />
        </> : <span aria-hidden="true" className={clsx('absolute right-1.5 top-1.5 h-1.5 w-1.5 rounded-full ring-2 ring-bg-secondary',
          error || record?.outcome === 'unconfirmed' ? 'bg-warning' : absent ? 'bg-success' : 'bg-text-muted')} />}
      </button>
    </Tooltip>
    <span className="sr-only" role="status">{label}</span>
    {open && <ModalPortal>
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4 backdrop-blur-sm"
        onMouseDown={(event) => { if (event.target === event.currentTarget) close(); }}>
        <div ref={panelRef} role="dialog" aria-modal="true" aria-labelledby="giant-meteor-title"
          aria-describedby="giant-meteor-description"
          className={clsx('meteor-dialog relative w-full max-w-sm overflow-hidden rounded-xl border border-border bg-bg-secondary text-text-primary shadow-xl', found && 'meteor-dialog--found')}>
          <button onClick={close} aria-label="关闭" className="absolute right-3 top-3 z-10 rounded-md p-1.5 text-text-secondary hover:bg-bg-hover"><X className="w-4 h-4" /></button>
          <div className="meteor-dialog-hero flex flex-col items-center px-7 pb-5 pt-7 text-center">
            <MeteorEmblem discovered={found} />
            <h2 id="giant-meteor-title" className="mt-3 text-xl font-semibold tracking-wide">{found ? '巨陨星降临' : '巨陨星状态'}</h2>
            {found && <span className="meteor-rarity mt-2 inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-medium"><Star className="w-3 h-3 fill-current" />稀有发现<Star className="w-3 h-3 fill-current" /></span>}
            <p id="giant-meteor-description" className="mt-3 text-sm leading-6 text-text-secondary">{description}</p>
          </div>
          <div className="mx-5 rounded-lg border border-border bg-bg-primary/70 px-4 py-3 text-xs">
            <div className="flex justify-between gap-3"><span className="text-text-secondary">游戏日期</span><span className="tabular-nums">{today}</span></div>
            <div className="mt-2 flex justify-between gap-3"><span className="text-text-secondary">检测时间</span><span className="tabular-nums">{checkedAt ?? '尚未检测'}</span></div>
          </div>
          <div className="flex items-center gap-2 px-5 py-4">
            <span className="mr-auto text-xs text-text-muted">每日 04:00 更新日期</span>
            <button onClick={() => void refresh().catch(() => {})} disabled={loading} title="刷新本地记录" aria-label="刷新本地记录"
              className="rounded-md p-2 text-text-secondary hover:bg-bg-hover disabled:opacity-50"><RefreshCw className={clsx('w-4 h-4', loading && 'animate-spin')} /></button>
            <button onClick={close} className="inline-flex items-center gap-1.5 rounded-md bg-accent px-4 py-2 text-xs font-medium text-white hover:bg-accent-hover"><Check className="w-3.5 h-3.5" />知道了</button>
          </div>
        </div>
      </div>
    </ModalPortal>}
  </>;
}

export function AccountStatusActions({ onShowProgress }: { onShowProgress: () => void }) {
  const { data, error } = useAccountStateStore();
  const progress = useMemo(() => getBlessingProgress(data), [data]);
  const percentage = progress.percentage === null ? '—' : `${progress.percentage}%`;
  const detail = error ? '账号状态读取失败' : progress.known
    ? `已收集 ${progress.owned.size} / ${progress.target} 个颜色${progress.uncertain ? ' · 上次确认记录，待复核' : ''}`
    : '尚无完整记录，请先运行祝福闪光仓库读取';
  return <>
    <GiantMeteorStatus />
    <Tooltip side="bottom" content={<div className="space-y-1"><p className="font-medium">祝福闪光收集进度 {percentage}</p><p className="text-text-secondary">{detail}</p></div>}>
      <button onClick={onShowProgress}
        className="flex shrink-0 items-center gap-1.5 rounded-md p-2 text-text-secondary transition-colors hover:bg-bg-hover focus-visible:outline-2 focus-visible:outline-accent"
        aria-label={`祝福闪光收集进度 ${percentage}`}>
        <Sparkles className="h-5 w-5 shrink-0" aria-hidden="true" />
        <span className="whitespace-nowrap text-sm">收集进度</span>
        <span className="text-[15px] font-semibold tabular-nums text-accent">{percentage}</span>
      </button>
    </Tooltip>
    <ExplorationControl />
  </>;
}
