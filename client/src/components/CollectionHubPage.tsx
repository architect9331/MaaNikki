import { Check, ChevronDown, Clock, Gem, Hammer, RefreshCw, Play, Sparkles, Square, Shirt } from 'lucide-react';
import clsx from 'clsx';
import { useEffect, useRef, useState } from 'react';
import { useAccountStateStore } from '@/stores/accountStateStore';
import { useWardrobeStore, startWardrobeRead, stopWardrobeRead, wardrobeLabels, WARDROBE_INSTANCE_ID } from '@/services/wardrobeService';
import { isTauri } from '@/utils/paths';
import { LogsPanel } from './LogsPanel';
import { FeaturePage, primaryButton, secondaryButton, StatusBar, Tabs } from './ui/FeaturePage';
import { useWardrobePlanStore } from '@/services/scoring/wardrobePlanStore';
import { useCollectionOverview, type Progress } from './useCollectionOverview';
import { WardrobeBrowser } from './WardrobeBrowser';
import { BlessingProgressPanel } from './BlessingProgressPage';
import { useCollectionHubStore, type CollectionTab } from '@/stores/collectionHubStore';
import { WardrobeCraftingPlanner } from './WardrobeCraftingPlanner';
import { WardrobeRenewalPlanner } from './WardrobeRenewalPlanner';

type Tab = CollectionTab;
// Each tab carries its own progress, so the page reads as an overview before any tab is opened.
const TABS = [
  ['closet', Shirt, '我的衣柜', '套'],
  ['crafting', Hammer, '制衣', '张'],
  ['renewal', Gem, '换新', '组'],
  ['blessing', Sparkles, '祝福闪光', '色'],
] as const;
const figure = (progress: Progress | null, unit: string) => progress
  ? `${progress.done.toLocaleString('zh-CN')} / ${progress.total.toLocaleString('zh-CN')} ${unit}` : '尚无记录';
const formatTime = (value: string) => new Date(value).toLocaleString('zh-CN', { hour12: false });

/** 收集进度: one hub for the closet, crafting, renewal and blessing collection plans. */
export function CollectionHubPage({ onClose }: { onClose: () => void }) {
  const { data, error: accountError, refresh, loading } = useAccountStateStore();
  const { status, error } = useWardrobeStore();
  const tab = useCollectionHubStore((state) => state.tab);
  const setTab = useCollectionHubStore((state) => state.setTab);
  const [opened, setOpened] = useState<Set<Tab>>(() => new Set([tab]));
  // Tabs mount on first visit (also when another page links straight to one).
  useEffect(() => { setOpened((previous) => previous.has(tab) ? previous : new Set(previous).add(tab)); }, [tab]);
  const [showDetails, setShowDetails] = useState(false);
  const [refreshToken, setRefreshToken] = useState(0);
  const busy = ['starting', 'reading', 'stopping'].includes(status);
  const scan = data?.wardrobe?.last_scan ?? data?.wardrobe?.inventory;
  const parts = Object.entries(scan?.parts ?? {});
  const wasBusy = useRef(busy);
  useEffect(() => { void refresh().catch(() => {}); }, [refresh]);
  const loadPlans = useWardrobePlanStore((state) => state.load);
  useEffect(() => { void loadPlans(`${data?.uid ?? ''}:${refreshToken}`); }, [data?.uid, refreshToken, loadPlans]);
  const overview = useCollectionOverview();
  // Reload the closet and planners once a read ends so new clothes show up.
  useEffect(() => {
    if (wasBusy.current && !busy) setRefreshToken((previous) => previous + 1);
    wasBusy.current = busy;
  }, [busy]);
  const tone = error || accountError ? 'warn' : busy ? 'busy' : scan?.complete ? 'ok' : 'idle';
  const open = (next: Tab) => setTab(next);
  const openCloset = () => { open('closet'); setShowDetails(true); };
  const readButton = <button disabled={!isTauri() || status === 'stopping'} onClick={() => busy ? stopWardrobeRead() : void startWardrobeRead()} className={busy ? secondaryButton : primaryButton}>
    {busy ? <Square className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}{busy ? '停止读取' : scan ? scan.complete ? '更新衣柜' : '继续读取' : '开始读取'}
  </button>;
  const summary = scan
    ? <>已读取 <strong className="tabular-nums text-text-primary">{scan.scanned_count}</strong> 件 · 部位 {scan.completed_parts} / {scan.total_parts ?? 28}
      {!!scan.ambiguous_count && <> · 同图候选 {scan.ambiguous_count}</>}{scan.updated_at && <> · 更新于 {formatTime(scan.updated_at)}</>}
      {!scan.complete && !busy && <span className="text-warning"> · 尚未读完，可继续读取</span>}</>
    : '进入游戏主界面并清空衣柜筛选后开始，读取期间保持游戏在前台。';

  return <FeaturePage icon={Sparkles} title="收集进度" onBack={onClose} actions={<>
    {data?.uid && <span className="text-xs tabular-nums text-text-muted">UID {data.uid}</span>}
    <button onClick={() => void refresh().then(() => setRefreshToken((previous) => previous + 1)).catch(() => {})} disabled={loading} title="刷新收集记录" aria-label="刷新收集记录" className="rounded-lg p-2 hover:bg-bg-hover disabled:opacity-40"><RefreshCw className={clsx('h-4 w-4', loading && 'animate-spin')} /></button>
  </>}>
    <Tabs label="收集进度" value={tab} onChange={open} tabs={TABS.map(([id, icon, label, unit]) => ({
      id, icon, label, value: figure(overview[id], unit),
      progress: overview[id]?.total ? overview[id]!.done / overview[id]!.total : null,
    }))} />
    {opened.has('closet') && <div className={clsx('space-y-4', tab !== 'closet' && 'hidden')}>
      <StatusBar tone={tone} title={busy || !scan || error || accountError ? wardrobeLabels[status] : scan.complete ? '衣柜记录完整' : '衣柜记录未读完'}
        detail={error || accountError || summary}>
        {(scan || busy) && <button type="button" onClick={() => setShowDetails((value) => !value)} aria-expanded={showDetails || busy} className={secondaryButton}>
          读取详情<ChevronDown className={clsx('h-3.5 w-3.5 transition-transform', (showDetails || busy) && 'rotate-180')} />
        </button>}
        {readButton}
      </StatusBar>
      {(showDetails || busy) && <section className="overflow-hidden rounded-xl border border-border bg-bg-secondary">
        {parts.length > 0 && <div className="grid grid-cols-2 gap-px bg-border/50 sm:grid-cols-4">{parts.map(([id, part]) =>
          <div key={id} className="flex items-center gap-2 bg-bg-secondary px-4 py-2"><span className="min-w-0 flex-1 truncate text-xs">{part.name}</span><strong className="text-xs font-semibold tabular-nums">{part.scanned_count}</strong>{part.complete ? <Check className="h-3.5 w-3.5 text-success" /> : <Clock className="h-3.5 w-3.5 text-text-muted" />}</div>)}</div>}
        {scan?.reason && <p className="border-t border-border px-4 py-2.5 text-xs leading-5 text-text-secondary">{scan.reason}</p>}
        {!!scan?.unconfirmed_count && <p className="border-t border-border px-4 py-2.5 text-xs leading-5 text-text-secondary">{scan.unconfirmed_count} 个选中格待确认名称，候选结果已保存。</p>}
        <div className="h-48 border-t border-border"><LogsPanel instanceId={WARDROBE_INSTANCE_ID} /></div>
      </section>}
      {scan
        ? <WardrobeBrowser refreshToken={refreshToken} />
        : !busy && <section className="rounded-xl border border-dashed border-border bg-bg-secondary px-6 py-10 text-center">
          <Shirt className="mx-auto h-10 w-10 text-accent opacity-60" />
          <h3 className="mt-3 text-sm font-semibold">先读取一次衣柜</h3>
          <p className="mx-auto mt-1 max-w-md text-xs leading-5 text-text-secondary">读取后可以按套装查看收集进度，制衣、换新和搭配计算也会根据你拥有的衣服自动计算。</p>
          <ol className="mx-auto mt-4 max-w-sm space-y-1 text-left text-xs text-text-secondary">
            <li>1. 打开游戏并停在主界面</li>
            <li>2. 清空衣柜里的筛选条件</li>
            <li>3. 点下方按钮，读取期间保持游戏在前台；中断后可以继续</li>
          </ol>
          <div className="mt-5 flex justify-center">{readButton}</div>
        </section>}
    </div>}
    {opened.has('crafting') && <div className={clsx(tab !== 'crafting' && 'hidden')}><WardrobeCraftingPlanner refreshToken={refreshToken} onOpenCloset={openCloset} /></div>}
    {opened.has('renewal') && <div className={clsx(tab !== 'renewal' && 'hidden')}><WardrobeRenewalPlanner refreshToken={refreshToken} onOpenCloset={openCloset} /></div>}
    {opened.has('blessing') && <div className={clsx(tab !== 'blessing' && 'hidden')}><BlessingProgressPanel refreshToken={refreshToken} /></div>}
  </FeaturePage>;
}
