import { useEffect, useMemo } from 'react';
import { BookOpen, Calculator, ChevronRight, Compass, LibraryBig, Palette } from 'lucide-react';
import clsx from 'clsx';
import { useAccountStateStore } from '@/stores/accountStateStore';
import { useGiantMeteorState } from '@/stores/giantMeteorStore';
import { explorationStatusLabels, setExplorationEnabled, useExplorationStore } from '@/services/explorationService';
import { resonanceLabel, setResonanceEnabled, useResonanceStore } from '@/services/resonanceService';
import { useWardrobeStore, wardrobeLabels } from '@/services/wardrobeService';
import { useWardrobePlanStore } from '@/services/scoring/wardrobePlanStore';
import { buildWardrobeOwnershipIndex, evaluateOutfit } from '@/services/outfits/matcher';
import { useOutfitCollectionStore } from '@/services/outfits/store';
import { useCollectionOverview } from './useCollectionOverview';
import { isTauri } from '@/utils/paths';
import { useAppStore } from '@/stores/appStore';
import { FeatureTile } from './FeatureTile';
import { PearpalAccountTile } from './PearpalAccount';
import { MeteorIcon } from './GiantMeteorEasterEgg';
import { Tooltip } from './ui/Tooltip';

function GiantMeteorStatus({ compact, pill = false }: { compact?: boolean; pill?: boolean }) {
  const { error, record, found, absent, checkedAt, previewActive, openDialog } = useGiantMeteorState();

  const label = error ? '巨陨星状态读取失败' : found ? '巨陨星降临！'
    : absent ? '今日未发现巨陨星' : record ? '巨陨星检测未确认' : '今日尚未检测巨陨星';
  const description = error ? '账号状态读取失败，可刷新后重试。'
    : found ? '家园已出现巨陨星预告，记得夜间前往查看。'
    : absent ? '今日家园检测未发现巨陨星。'
    : record ? '上次检测未能确认地图状态，可重新运行家园日常。'
    : '运行家园日常后，这里会展示今日的检测结果。';

  const meteorTooltip = <div className="space-y-1">
    <p className="font-medium">{label}</p>
    <p className="text-text-secondary">{checkedAt ? `检测于 ${checkedAt}` : description}</p>
    <p className="text-text-muted">点击查看 · 每日 04:00 刷新</p>
  </div>;
  const meteorWarn = Boolean(error) || record?.outcome === 'unconfirmed';
  const highlighted = found || previewActive;
  const meteorShortStatus = error
    ? '读取失败'
    : found
      ? '今夜降临 ✦'
      : previewActive
        ? '预览中 ✦'
        : absent
          ? '未发现'
          : record
            ? '未确认'
            : '未检测';

  return <>
    {pill && !compact ? (
      <Tooltip side="bottom" content={meteorTooltip}>
        <button
          type="button"
          onClick={openDialog}
          aria-label={`巨陨星：${label}`}
          className={clsx(
            'flex w-full min-w-0 items-center gap-1.5 rounded-lg border bg-bg-secondary px-2.5 py-1.5 text-left text-xs transition-colors hover:bg-bg-hover focus-visible:outline-2 focus-visible:outline-accent',
            meteorWarn ? 'border-warning/45' : 'border-border',
            highlighted && 'meteor-status--found',
          )}
        >
          <MeteorIcon
            className={clsx(
              'h-3.5 w-3.5 shrink-0',
              highlighted ? 'text-[var(--meteor-gold)]' : meteorWarn ? 'text-warning' : 'text-accent-secondary',
            )}
          />
          <span className={clsx('truncate font-medium', highlighted ? 'text-[var(--meteor-title)]' : 'text-text-primary')}>巨陨星</span>
          <span
            className={clsx(
              'ml-auto shrink-0 text-[11px]',
              highlighted
                ? 'font-semibold text-[var(--meteor-gold)]'
                : meteorWarn
                  ? 'text-warning'
                  : 'text-text-muted',
            )}
          >
            {meteorShortStatus}
          </span>
        </button>
      </Tooltip>
    ) : (
      <FeatureTile icon={MeteorIcon} title="巨陨星" compact={compact} warn={meteorWarn}
        status={error ? '状态读取失败' : found ? '今夜降临 ✦' : previewActive ? '预览中 ✦' : absent ? '今日未发现' : record ? '检测未确认' : '今日尚未检测'}
        className={clsx(highlighted && 'meteor-status--found')}
        tooltip={meteorTooltip}
        onClick={openDialog} />
    )}
    <span className="sr-only" role="status">{label}</span>
  </>;
}

/** The feature entries, as a panel of tiles or as icons while the panel is collapsed. */
export function AccountStatusActions({ compact = false }: { compact?: boolean }) {
  const data = useAccountStateStore((state) => state.data);
  const error = useAccountStateStore((state) => state.error);
  const exploration = useExplorationStore();
  const resonance = useResonanceStore();
  const wardrobeStatus = useWardrobeStore((state) => state.status);
  const open = useAppStore((state) => state.setCurrentPage);

  const wardrobe = data?.wardrobe?.inventory ?? data?.wardrobe?.last_scan;
  const loadPlans = useWardrobePlanStore((state) => state.load);
  const planPayload = useWardrobePlanStore((state) => state.payload);
  const planSettings = useWardrobePlanStore((state) => state.settings);
  const planError = useWardrobePlanStore((state) => state.error);
  const plansLoading = useWardrobePlanStore((state) => state.loading);
  const outfitItems = useOutfitCollectionStore((state) => state.items);
  const outfitDict = useOutfitCollectionStore((state) => state.dict);
  const outfitRule = useOutfitCollectionStore((state) => state.rule);
  const loadOutfits = useOutfitCollectionStore((state) => state.load);
  const planRevision = data?.plan_revision ?? wardrobe?.updated_at ?? '';
  useEffect(() => {
    if (data?.uid) void loadPlans(`${data.uid}:${planRevision}:0`);
  }, [data?.uid, planRevision, loadPlans]);
  useEffect(() => { void loadOutfits(); }, [loadOutfits]);
  const outfitStatus = useMemo(() => {
    if (outfitItems.length === 0) return '搭配码与试穿';
    if (!planPayload) return `${outfitItems.length} 套搭配`;
    const ownership = buildWardrobeOwnershipIndex(planPayload, planSettings);
    let wearable = 0;
    for (const item of outfitItems) {
      const ev = evaluateOutfit(item, ownership, outfitDict, planPayload, outfitRule);
      if (ev.status === 'wearable') wearable += 1;
    }
    return `${outfitItems.length} 套 · 可穿 ${wearable}`;
  }, [outfitItems, planPayload, planSettings, outfitDict, outfitRule]);
  const overview = useCollectionOverview();
  const collections = ([
    ['closet', '衣柜套装', '套'], ['crafting', '制衣', '张'],
    ['renewal', '焕新', '组'], ['blessing', '祝福闪光', '色'],
  ] as const).map(([id, label, unit]) => {
    const progress = overview[id];
    const unavailable = id !== 'blessing' && (plansLoading || planError);
    const value = unavailable ? plansLoading ? '加载中' : '读取失败'
      : progress && progress.total > 0 ? `${Math.floor(progress.done / progress.total * 100)}%` : '尚无记录';
    const detail = !unavailable && progress ? `${progress.done} / ${progress.total} ${unit}` : value;
    const percentage = !unavailable && progress && progress.total > 0 ? progress.done / progress.total * 100 : null;
    return { id, label, value, detail, percentage };
  });
  const wardrobeBusy = ['starting', 'reading', 'stopping'].includes(wardrobeStatus);
  const resonanceError = resonance.connectionError || resonance.status.error;
  const explorationBusy = exploration.status === 'starting' || exploration.status === 'stopping';
  const collectionStatus = error ? '账号状态读取失败' : collections.map((item) => `${item.label} ${item.value}`).join('，');
  const collectionHint = <div className="space-y-1"><p className="font-medium">收集进度</p>
    {error ? <p>{error}</p> : collections.map((item) => <p key={item.id} className="flex justify-between gap-4 text-text-secondary"><span>{item.label}</span><span>{item.detail}</span></p>)}
    <p className="text-text-muted">衣柜按本地套装统计；各项独立计算。</p>
  </div>;

  const tiles = <>
    {compact ? <FeatureTile icon={LibraryBig} title="收集进度" compact warn={Boolean(error || planError)}
      status={collectionStatus} tooltip={collectionHint} onClick={() => open('blessing')} />
      : <Tooltip side="bottom" content={collectionHint}>
        <button type="button" onClick={() => open('blessing')} aria-label={`收集进度：${collectionStatus}`}
          className="col-span-full min-w-0 rounded-xl border border-border bg-bg-secondary px-3 py-3 text-left transition-colors hover:bg-bg-hover focus-visible:outline-2 focus-visible:outline-accent">
          <span className="mb-3 flex items-center gap-2 text-sm font-medium text-text-primary">
            <LibraryBig className="h-4 w-4 text-accent-secondary" />收集进度
            <ChevronRight className="ml-auto h-3.5 w-3.5 text-text-muted" />
          </span>
          {error ? <span className="text-xs text-warning">账号状态读取失败</span>
            : <span className="grid grid-cols-4 divide-x divide-border">
              {collections.map((item) => <span key={item.id} className="min-w-0 px-1.5 first:pl-0 last:pr-0">
                <span className="block text-xs font-medium leading-5 text-text-secondary">{item.label}</span>
                <span className={clsx('mt-1 block text-base font-semibold leading-6 tabular-nums', item.percentage === null ? 'text-text-muted' : 'text-text-primary')}>{item.value}</span>
                <span className="mt-2 block h-1 overflow-hidden rounded-full bg-border/70">
                  <span className="block h-full rounded-full bg-accent/70" style={{ width: `${item.percentage ?? 0}%` }} />
                </span>
              </span>)}
            </span>}
        </button>
      </Tooltip>}
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
    <FeatureTile icon={Calculator} title="搭配计算" compact={compact} busy={wardrobeBusy}
      status={wardrobeBusy || wardrobeStatus === 'error' ? wardrobeLabels[wardrobeStatus] : wardrobe ? `${wardrobe.scanned_count} 件` : '尚未读取'}
      warn={wardrobeStatus === 'error'}
      tooltip={<div className="space-y-1"><p className="font-medium">搭配计算</p><p className="text-text-secondary">按已读衣柜计算巅峰赛等比赛的最高分搭配；读取衣柜在「收集进度 → 我的衣柜」。</p></div>}
      onClick={() => open('wardrobe')} />
    <FeatureTile icon={Palette} title="搭配收藏" compact={compact}
      status={outfitStatus}
      tooltip={<div className="space-y-1"><p className="font-medium">搭配收藏</p><p className="text-text-secondary">导入搭配码自动解析全部部件与染色，按全部匹配、忽略妆容或忽略饰品实时筛选当前账号能穿的搭配。</p></div>}
      onClick={() => open('outfits')} />
    {compact ? <>
      <GiantMeteorStatus compact />
      <PearpalAccountTile compact />
    </> : (
      <div className={clsx('col-span-full grid gap-2', isTauri() ? 'grid-cols-2' : 'grid-cols-1')}>
        <PearpalAccountTile pill />
        <GiantMeteorStatus pill />
      </div>
    )}
  </>;
  return compact
    ? <>{tiles}</>
    : <div role="group" aria-label="功能" className="grid shrink-0 grid-cols-2 gap-2">{tiles}</div>;
}
