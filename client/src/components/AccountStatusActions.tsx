import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { BookOpen, Calculator, Check, Compass, RefreshCw, Route, Sparkles, Star, X } from 'lucide-react';
import { toast } from 'sonner';
import clsx from 'clsx';
import { gameDay, useAccountStateStore } from '@/stores/accountStateStore';
import { explorationStatusLabels, setExplorationEnabled, useExplorationStore } from '@/services/explorationService';
import { resonanceLabel, setResonanceEnabled, useResonanceStore } from '@/services/resonanceService';
import { useWardrobeStore, wardrobeLabels } from '@/services/wardrobeService';
import { getBlessingProgress } from '@/utils/blessingProgress';
import { isTauri } from '@/utils/paths';
import { useAppStore } from '@/stores/appStore';
import { ModalPortal, useModalFocusTrap } from './ui/Modal';
import { FeatureTile } from './FeatureTile';

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

function GiantMeteorStatus({ compact }: { compact?: boolean }) {
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
    const account = data?.account_key ?? 'legacy';
    const key = `maanikki:giant-meteor:${account}:${today}`;
    if (!found || announced.current === key) return;
    announced.current = key;
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
  }, [found, today, data?.account_key]);

  return <>
    <FeatureTile icon={MeteorIcon} title="巨陨星" compact={compact} warn={Boolean(error) || record?.outcome === 'unconfirmed'}
      status={error ? '状态读取失败' : found ? '今日已发现' : absent ? '今日未发现' : record ? '检测未确认' : '今日尚未检测'}
      className={clsx(found && 'meteor-status--found')}
      tooltip={<div className="space-y-1">
        <p className="font-medium">{label}</p>
        <p className="text-text-secondary">{checkedAt ? `检测于 ${checkedAt}` : description}</p>
        <p className="text-text-muted">点击查看 · 每日 04:00 刷新</p>
      </div>}
      onClick={() => setOpen(true)} />
    <span className="sr-only" role="status">{label}</span>
    {open && <ModalPortal>
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4 backdrop-blur-sm"
        onMouseDown={(event) => { if (event.target === event.currentTarget) close(); }}>
        <div ref={panelRef} role="dialog" aria-modal="true" aria-labelledby="giant-meteor-title"
          aria-describedby="giant-meteor-description"
          className={clsx('meteor-dialog relative w-full max-w-sm overflow-hidden rounded-xl border border-border bg-bg-secondary text-text-primary shadow-xl', found && 'meteor-dialog--found')}>
          <button onClick={close} aria-label="关闭" className="absolute right-3 top-3 z-10 rounded-lg p-1.5 text-text-secondary hover:bg-bg-hover"><X className="w-4 h-4" /></button>
          <div className="meteor-dialog-hero flex flex-col items-center px-7 pb-5 pt-7 text-center">
            <MeteorEmblem discovered={found} />
            <h2 id="giant-meteor-title" className="mt-3 text-xl font-semibold tracking-wide">{found ? '巨陨星降临' : '巨陨星状态'}</h2>
            {found && <span className="meteor-rarity mt-2 inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-medium"><Star className="w-3 h-3 fill-current" />稀有发现<Star className="w-3 h-3 fill-current" /></span>}
            <p id="giant-meteor-description" className="mt-3 text-sm leading-6 text-text-secondary">{description}</p>
          </div>
          <div className="mx-5 rounded-lg border border-border bg-bg-primary/70 px-4 py-3 text-xs">
            {data?.uid && <div className="mb-2 flex justify-between gap-3"><span className="text-text-secondary">游戏 UID</span><span className="tabular-nums">{data.uid}</span></div>}
            <div className="flex justify-between gap-3"><span className="text-text-secondary">游戏日期</span><span className="tabular-nums">{today}</span></div>
            <div className="mt-2 flex justify-between gap-3"><span className="text-text-secondary">检测时间</span><span className="tabular-nums">{checkedAt ?? '尚未检测'}</span></div>
          </div>
          <div className="flex items-center gap-2 px-5 py-4">
            <span className="mr-auto text-xs text-text-muted">每日 04:00 更新日期</span>
            <button onClick={() => void refresh().catch(() => {})} disabled={loading} title="刷新本地记录" aria-label="刷新本地记录"
              className="rounded-lg p-2 text-text-secondary hover:bg-bg-hover disabled:opacity-50"><RefreshCw className={clsx('w-4 h-4', loading && 'animate-spin')} /></button>
            <button onClick={close} className="inline-flex items-center gap-1.5 rounded-lg border border-border px-4 py-2 text-xs font-medium hover:bg-bg-hover"><Check className="w-3.5 h-3.5" />知道了</button>
          </div>
        </div>
      </div>
    </ModalPortal>}
  </>;
}

/** The feature entries, as a panel of tiles or as icons while the panel is collapsed. */
export function AccountStatusActions({ compact = false }: { compact?: boolean }) {
  const { data, error } = useAccountStateStore();
  const exploration = useExplorationStore();
  const resonance = useResonanceStore();
  const wardrobeStatus = useWardrobeStore((state) => state.status);
  const open = useAppStore((state) => state.setCurrentPage);

  const progress = useMemo(() => getBlessingProgress(data), [data]);
  const detail = error ? '账号状态读取失败' : progress.known
    ? `五星闪光已收集 ${progress.owned.size} / ${progress.target} 个颜色${progress.uncertain ? ' · 上次确认记录，待复核' : ''}`
    : '尚无闪光记录，把五星闪光加入幻境挑战顺序运行一次时会先自动整理';
  const wardrobe = data?.wardrobe?.inventory ?? data?.wardrobe?.last_scan;
  const wardrobeBusy = ['starting', 'reading', 'stopping'].includes(wardrobeStatus);
  const resonanceError = resonance.connectionError || resonance.status.error;
  const explorationBusy = exploration.status === 'starting' || exploration.status === 'stopping';

  const tiles = <>
    <FeatureTile icon={Sparkles} title="收集进度" compact={compact} warn={Boolean(error)}
      status={error ? '读取失败' : progress.percentage === null ? '尚无记录' : `${progress.percentage}%`}
      tooltip={<div className="space-y-1"><p className="font-medium">收集进度</p><p className="text-text-secondary">我的衣柜、制衣、焕新与祝福闪光的收集计划。</p><p className="text-text-secondary">{detail}</p></div>}
      onClick={() => open('blessing')} />
    <FeatureTile icon={Calculator} title="搭配计算" compact={compact} busy={wardrobeBusy}
      status={wardrobeBusy || wardrobeStatus === 'error' ? wardrobeLabels[wardrobeStatus] : wardrobe ? `${wardrobe.scanned_count} 件` : '尚未读取'}
      warn={wardrobeStatus === 'error'}
      tooltip={<div className="space-y-1"><p className="font-medium">搭配计算</p><p className="text-text-secondary">按已读衣柜计算巅峰赛等比赛的最高分搭配；读取衣柜在「收集进度 → 我的衣柜」。</p></div>}
      onClick={() => open('wardrobe')} />
    <FeatureTile icon={Compass} title="辅助功能" compact={compact} busy={explorationBusy}
      status={explorationStatusLabels[exploration.status]} warn={exploration.status === 'error'}
      tooltip={exploration.error ? <div className="space-y-1"><p className="font-medium">辅助功能</p><p className="text-text-secondary">{exploration.error}</p></div> : undefined}
      toggle={{ checked: exploration.enabled, disabled: !isTauri(), label: '启用辅助功能', onChange: setExplorationEnabled }}
      onClick={() => open('exploration')} />
    <FeatureTile icon={BookOpen} title="共鸣记录" compact={compact} busy={resonance.status.busy}
      status={resonanceLabel(resonance.status).replace('自动同步', '')} warn={Boolean(resonanceError)}
      tooltip={<div className="space-y-1"><p className="font-medium">共鸣记录 · {resonanceLabel(resonance.status)}</p><p className="text-text-secondary">{resonanceError || '查看历史记录、同步状态与导出。'}</p></div>}
      toggle={{ checked: resonance.status.enabled, disabled: !isTauri() || !resonance.ready || resonance.changing,
        label: '自动同步共鸣记录', onChange: (value) => void setResonanceEnabled(value) }}
      onClick={() => open('resonance')} />
    <FeatureTile icon={Route} title="路线编辑器" compact={compact} status="录制和编辑路线"
      tooltip={<div className="space-y-1"><p className="font-medium">路线编辑器</p><p className="text-text-secondary">走一遍就能录下路线，再加上采集、钓鱼等动作。</p></div>}
      onClick={() => open('routes')} />
    <GiantMeteorStatus compact={compact} />
  </>;
  return compact
    ? <>{tiles}</>
    : <div role="group" aria-label="功能" className="grid shrink-0 grid-cols-[repeat(auto-fit,minmax(8.75rem,1fr))] gap-2">{tiles}</div>;
}
