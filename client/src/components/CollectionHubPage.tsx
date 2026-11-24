import { Check, ChevronDown, Clock, Compass, Gem, Hammer, LibraryBig, Package, RefreshCw, Play, Sparkles, Square, Shirt } from 'lucide-react';
import clsx from 'clsx';
import { useEffect, useRef, useState } from 'react';
import { useAccountStateStore } from '@/stores/accountStateStore';
import {
  READ_SPECS,
  startRenewalIncrementalRead,
  startRenewalRead,
  startWardrobeRead,
  stopWardrobeRead,
  useWardrobeStore,
  wardrobeLabels,
  WARDROBE_INSTANCE_ID,
} from '@/services/wardrobeService';
import { isTauri } from '@/utils/paths';
import { LogsPanel } from './LogsPanel';
import { FeaturePage, primaryButton, secondaryButton, StatusBar, Tabs } from './ui/FeaturePage';
import { useWardrobePlanStore } from '@/services/scoring/wardrobePlanStore';
import { useCollectionOverview, type Progress } from './useCollectionOverview';
import { BackpackViewerPanel } from './BackpackViewerPanel';
import { InfiniteHeartPanel } from './InfiniteHeartPanel';
import { WardrobeBrowser } from './WardrobeBrowser';
import { BlessingProgressPanel } from './BlessingProgressPage';
import { useCollectionHubStore, type CollectionTab } from '@/stores/collectionHubStore';
import { WardrobeCraftingPlanner } from './WardrobeCraftingPlanner';
import { WardrobeRenewalPlanner } from './WardrobeRenewalPlanner';
import { PearpalStatusPill } from './PearpalAccount';

type Tab = CollectionTab;
// Each tab carries its own progress, so the page reads as an overview before any tab is opened.
const TABS = [
  ['closet', Shirt, '我的衣柜', '套装'],
  ['crafting', Hammer, '制衣', '张'],
  ['renewal', Gem, '焕新', '组'],
  ['blessing', Sparkles, '祝福闪光', '色'],
  ['backpack', Package, '背包库存', '种'],
  ['heart', Compass, '无限之心', '点'],
] as const;
const figure = (progress: Progress | null, unit: string) => progress
  ? `${progress.done.toLocaleString('zh-CN')} / ${progress.total.toLocaleString('zh-CN')} ${unit}` : '尚无记录';
const formatTime = (value: string) => new Date(value).toLocaleString('zh-CN', { hour12: false });

/** 收集进度: one hub for the closet, crafting, renewal and blessing collection plans. */
export function CollectionHubPage({ onClose }: { onClose: () => void }) {
  const { data, error: accountError, refresh, loading } = useAccountStateStore();
  const { status, mode, error } = useWardrobeStore();
  const tab = useCollectionHubStore((state) => state.tab);
  const setTab = useCollectionHubStore((state) => state.setTab);
  const [opened, setOpened] = useState<Set<Tab>>(() => new Set([tab]));
  // Tabs mount on first visit (also when another page links straight to one).
  useEffect(() => { setOpened((previous) => previous.has(tab) ? previous : new Set(previous).add(tab)); }, [tab]);
  const [showDetails, setShowDetails] = useState(false);
  const [refreshToken, setRefreshToken] = useState(0);
  const busy = ['starting', 'reading', 'stopping'].includes(status);
  const closetBusy = busy && (mode === 'wardrobe' || mode === 'renewal' || mode === 'renewal_inc');
  const scan = data?.wardrobe?.last_scan ?? data?.wardrobe?.inventory;
  const renewalScan = data?.wardrobe?.renewal_scan;
  const hasClosetBaseline = Boolean(scan?.complete && renewalScan?.complete);
  const pearpalClothNum = data?.observations?.pearpal_notebook?.summary?.cloth_num;
  const recordedClothNum = data?.wardrobe?.pearpal_cloth_num;
  const clothDrift = Boolean(
    scan && typeof pearpalClothNum === 'number' && pearpalClothNum > 0 && recordedClothNum !== pearpalClothNum,
  );
  const parts = Object.entries(scan?.parts ?? {});
  const wasBusy = useRef(busy);
  useEffect(() => { void refresh().catch(() => {}); }, [refresh]);
  const loadPlans = useWardrobePlanStore((state) => state.load);
  const planRevision = data?.plan_revision ?? scan?.updated_at ?? '';
  useEffect(() => {
    if (data?.uid) void loadPlans(`${data.uid}:${planRevision}:${refreshToken}`);
  }, [data?.uid, planRevision, refreshToken, loadPlans]);
  const overview = useCollectionOverview();
  const materials = useWardrobePlanStore((state) => state.materials);
  const blessingUnit = materials.blessing_plan?.mode === 'power' ? '种' : '色';
  // Reload the closet and planners once a read ends so new clothes show up.
  useEffect(() => {
    if (wasBusy.current && !busy) setRefreshToken((previous) => previous + 1);
    wasBusy.current = busy;
  }, [busy]);
  const errorHere = error && (mode === 'wardrobe' || mode === 'renewal' || mode === 'renewal_inc');
  const tone = errorHere || accountError ? 'warn' : closetBusy ? 'busy' : scan?.complete ? 'ok' : 'idle';
  const open = (next: Tab) => setTab(next);
  const openCloset = () => { open('closet'); setShowDetails(true); };
  const primaryClosetMode = hasClosetBaseline ? 'renewal_inc' : 'renewal';
  const primaryBusy = busy && mode === primaryClosetMode;
  const renewalFullBusy = busy && mode === 'renewal';
  const compendiumBusy = busy && mode === 'wardrobe';
  const readButton = (
    <button
      disabled={!isTauri() || (busy && !primaryBusy) || status === 'stopping'}
      onClick={() => (primaryBusy ? stopWardrobeRead() : void (hasClosetBaseline ? startRenewalIncrementalRead() : startRenewalRead()))}
      className={primaryBusy ? secondaryButton : primaryButton}
      title={hasClosetBaseline ? '按获得时间扫描新增服装与近期升级，几秒完成' : '从「服装焕新」一次同步所有可养成服装与等级'}
    >
      {primaryBusy ? <Square className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
      {primaryBusy ? '停止读取' : hasClosetBaseline ? '快速增量同步' : '从服装焕新读取'}
    </button>
  );
  const fullRenewalButton = hasClosetBaseline ? (
    <button
      disabled={!isTauri() || (busy && !renewalFullBusy) || status === 'stopping'}
      onClick={() => (renewalFullBusy ? stopWardrobeRead() : void startRenewalRead())}
      className={secondaryButton}
      title="完整遍历「服装焕新」全部分类，重新核对每件服装等级"
    >
      {renewalFullBusy ? <Square className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
      {renewalFullBusy ? '停止读取' : '完整焕新读取'}
    </button>
  ) : null;
  const compendiumButton = (
    <button
      disabled={!isTauri() || (busy && !compendiumBusy) || status === 'stopping'}
      onClick={() => (compendiumBusy ? stopWardrobeRead() : void startWardrobeRead())}
      className={secondaryButton}
      title="额外读取妆容部件及同款进化染色数量（焕新页不包含妆容与进化数）"
    >
      {compendiumBusy ? <Square className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
      {compendiumBusy ? '停止读取' : '博物图鉴校准'}
    </button>
  );
  const summary = scan
    ? <>已读取 <strong className="tabular-nums text-text-primary">{scan.scanned_count}</strong> 件 · 部位 {scan.completed_parts} / {scan.total_parts ?? 28}
      {renewalScan?.updated_at && <> · 焕新等级于 {formatTime(renewalScan.updated_at)}</>}
      {!renewalScan?.updated_at && scan.updated_at && <> · 更新于 {formatTime(scan.updated_at)}</>}
      {clothDrift && (
        <span className="text-warning">
          {' '}· 美鸭梨手账记录 {pearpalClothNum} 件{recordedClothNum != null ? `（本地上次 ${recordedClothNum} 件）` : ''}，建议增量同步
        </span>
      )}
      {!scan.complete && !busy && <span className="text-warning"> · 尚未读完，可继续读取</span>}</>
    : '推荐先从「服装焕新」读取：可一次同步衣柜拥有情况与全部服装等级，支持按获得时间快速增量更新。';

  return <FeaturePage icon={LibraryBig} title="收集进度" onBack={onClose} actions={<>
    <PearpalStatusPill />
    {data?.uid && <span className="text-xs tabular-nums text-text-muted">UID {data.uid}</span>}
    <button onClick={() => void refresh().then(() => setRefreshToken((previous) => previous + 1)).catch(() => {})} disabled={loading} title="刷新收集记录" aria-label="刷新收集记录" className="rounded-lg p-2 hover:bg-bg-hover disabled:opacity-40"><RefreshCw className={clsx('h-4 w-4', loading && 'animate-spin')} /></button>
  </>}>
    <Tabs label="收集进度" value={tab} onChange={open} tabs={TABS.map(([id, icon, label, unit]) => ({
      id, icon, label, value: figure(overview[id], id === 'blessing' ? blessingUnit : unit),
      progress: overview[id]?.total ? overview[id]!.done / overview[id]!.total : null,
    }))} />
    {opened.has('closet') && <div className={clsx('space-y-4', tab !== 'closet' && 'hidden')}>
      <StatusBar tone={tone} title={closetBusy ? (READ_SPECS[mode]?.busyText ?? wardrobeLabels[status]) : !scan || errorHere || accountError ? wardrobeLabels['idle'] : scan.complete ? '衣柜记录完整' : '衣柜记录未读完'}
        detail={errorHere || accountError || summary}>
        {(scan || closetBusy) && <button type="button" onClick={() => setShowDetails((value) => !value)} aria-expanded={showDetails || closetBusy} className={secondaryButton}>
          读取详情<ChevronDown className={clsx('h-3.5 w-3.5 transition-transform', (showDetails || closetBusy) && 'rotate-180')} />
        </button>}
        {compendiumButton}
        {fullRenewalButton}
        {readButton}
      </StatusBar>
      {(showDetails || closetBusy) && <section className="overflow-hidden rounded-xl border border-border bg-bg-secondary">
        {parts.length > 0 && <div className="grid grid-cols-2 gap-px bg-border/50 sm:grid-cols-4">{parts.map(([id, part]) =>
          <div key={id} className="flex items-center gap-2 bg-bg-secondary px-4 py-2"><span className="min-w-0 flex-1 truncate text-xs">{part.name}</span><strong className="text-xs font-semibold tabular-nums">{part.scanned_count}</strong>{part.complete ? <Check className="h-3.5 w-3.5 text-success" /> : <Clock className="h-3.5 w-3.5 text-text-muted" />}</div>)}</div>}
        {scan?.reason && <p className="border-t border-border px-4 py-2.5 text-xs leading-5 text-text-secondary">{scan.reason}</p>}
        {!!scan?.unconfirmed_count && <p className="border-t border-border px-4 py-2.5 text-xs leading-5 text-text-secondary">{scan.unconfirmed_count} 个选中格待确认名称，候选结果已保存。</p>}
        <div className="h-48 border-t border-border"><LogsPanel instanceId={WARDROBE_INSTANCE_ID} /></div>
      </section>}
      {scan
        ? <WardrobeBrowser refreshToken={refreshToken} />
        : !closetBusy && <section className="rounded-xl border border-dashed border-border bg-bg-secondary px-6 py-10 text-center">
          <Shirt className="mx-auto h-10 w-10 text-accent opacity-60" />
          <h3 className="mt-3 text-sm font-semibold">先读取一次衣柜</h3>
          <p className="mx-auto mt-1 max-w-md text-xs leading-5 text-text-secondary">读取后可以按套装查看收集进度，制衣、焕新和搭配计算也会根据你拥有的衣服自动计算。</p>
          <ol className="mx-auto mt-4 max-w-sm space-y-1 text-left text-xs text-text-secondary">
            <li>1. 打开游戏并停在主界面</li>
            <li>2. 清空衣柜里的筛选条件</li>
            <li>3. 点下方按钮，读取期间保持游戏在前台；中断后可以继续</li>
          </ol>
          <div className="mt-5 flex flex-wrap justify-center gap-2">{readButton}{compendiumButton}</div>
        </section>}
    </div>}
    {opened.has('crafting') && <div className={clsx(tab !== 'crafting' && 'hidden')}><WardrobeCraftingPlanner refreshToken={refreshToken} onOpenCloset={openCloset} /></div>}
    {opened.has('renewal') && <div className={clsx(tab !== 'renewal' && 'hidden')}><WardrobeRenewalPlanner refreshToken={refreshToken} onOpenCloset={openCloset} /></div>}
    {opened.has('blessing') && <div className={clsx(tab !== 'blessing' && 'hidden')}><BlessingProgressPanel refreshToken={refreshToken} /></div>}
    {opened.has('backpack') && <div className={clsx(tab !== 'backpack' && 'hidden')}><BackpackViewerPanel refreshToken={refreshToken} /></div>}
    {opened.has('heart') && <div className={clsx(tab !== 'heart' && 'hidden')}><InfiniteHeartPanel refreshToken={refreshToken} /></div>}
  </FeaturePage>;
}
