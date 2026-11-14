import { useEffect, useMemo, useState } from 'react';
import { Image as ImageIcon, RefreshCw, Search } from 'lucide-react';
import { invoke } from '@tauri-apps/api/core';
import clsx from 'clsx';
import { toast } from 'sonner';
import { collectionShareImage } from '@/utils/collectionShareImage';
import { useAccountStateStore } from '@/stores/accountStateStore';
import { collectionSlots as slots, collectionKey as keyOf } from '@/utils/blessingProgress';
import { REALM_GOAL_LABELS } from '@/utils/nikkiOptions';
import { useWardrobePlanStore } from '@/services/scoring/wardrobePlanStore';
import type { PlanMode } from '@/services/scoring/types';
import { useRealmForecast } from '@/services/useRealmForecast';
import { isTauri } from '@/utils/paths';
import { BlessingForecastPanel } from './RealmForecastPanel';
import { Card, Segmented, Tile, Notices, secondaryButton, type Notice } from './ui/FeaturePage';
import { PlanLayout, PlanListPanel, PlanPanel, bulkButton, planCheckbox, planField } from './CollectionPlan';

const COLOR_FILL: Record<string, string> = {
  蓝: '#4f8fe0',
  绿: '#4cae6b',
  紫: '#9a6fdc',
  粉: '#ec7fae',
  黄: '#e6b53c',
  红: '#e0625c',
  白: '#d9dde3',
  彩: 'conic-gradient(#e0625c, #e6b53c, #4cae6b, #4f8fe0, #9a6fdc, #e0625c)',
};
const number = (value: number) => value.toLocaleString('zh-CN');
const wan = (value: number) =>
  value >= 10000 ? `${(value / 10000).toLocaleString('zh-CN', { maximumFractionDigits: 1 })} 万` : number(value);

function ColorDot({ color, collected, label }: { color: string; collected: boolean; label: string }) {
  return (
    <span
      role="img"
      aria-label={label}
      title={label}
      className={clsx(
        'inline-block h-3.5 w-3.5 shrink-0 rounded-full',
        collected ? 'ring-1 ring-black/10' : 'border border-dashed border-border-strong',
      )}
      style={collected ? { background: COLOR_FILL[color] ?? 'var(--color-accent)' } : undefined}
    />
  );
}

/** 祝福闪光 tab of 收集进度: the same 全收集 / 自选 plan, list, progress block and queue action. */
export function BlessingProgressPanel({ refreshToken }: { refreshToken: number }) {
  const { data, error, loading, refresh } = useAccountStateStore();
  const { payload, materials, load, updateMaterials } = useWardrobePlanStore();
  const wardrobeUpdatedAt = data?.wardrobe?.inventory?.updated_at ?? data?.wardrobe?.last_scan?.updated_at ?? '';
  useEffect(() => {
    if (data?.uid) void load(`${data.uid}:${data.updated_at ?? wardrobeUpdatedAt}:${refreshToken}`);
  }, [data?.uid, data?.updated_at, wardrobeUpdatedAt, refreshToken, load]);

  const planMode: PlanMode = materials.blessing_plan?.mode === 'selected' ? 'selected' : 'all';
  const chosen = useMemo(() => new Set(materials.blessing_plan?.sets ?? []), [materials.blessing_plan]);
  const [limit, setLimit] = useState(30);
  const savePlan = (mode: PlanMode, names: Set<string> = chosen) => {
    if (!payload) {
      toast.error('请先在「我的衣柜」读取一次衣柜以确认当前账号，再保存自选计划。');
      return;
    }
    updateMaterials((previous) => ({ ...previous, blessing_plan: { mode, sets: [...names] } }));
  };
  const toggleSets = (names: string[], on: boolean) => {
    const next = new Set(chosen);
    for (const name of names) {
      if (on) next.add(name);
      else next.delete(name);
    }
    savePlan('selected', next);
  };

  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState('missing');
  const [syncingPearpal, setSyncingPearpal] = useState(false);
  const [extraBling, setExtraBling] = useState(0);
  const [extraParticles, setExtraParticles] = useState(0);

  const {
    entries,
    blessing,
    chosenPreset,
    glowStandalone,
    glowQueued,
    glowComputing,
  } = useRealmForecast({ extraBling, extraParticles, refreshToken });

  const snapshot = data?.blessing;
  const { progress } = blessing;
  const { sets, target, uncertain, rewardPending, pearpalCheck } = progress;
  const resources = snapshot?.pursuit?.resources;
  const probability = data?.catalog?.probability;
  const rolls = data?.observations?.eureka_rolls;
  const {
    isFarmOnly,
    reserveBling: effectiveReserveBling,
    scopeOwned,
    scopeTarget,
    scopeSetsCount,
    completeSets,
    completeParts,
    rainbowOwned,
    rainbowTotal,
    scopeComplete,
    scopeNext,
    missingBySource,
    presets,
    history,
    needBling,
    spendableBling,
    affordableUpgrades,
    particleTotal,
  } = blessing;

  const enabledGoals = entries.filter((entry) => entry.enabled).map((entry) => entry.goal);
  const glowIdx = enabledGoals.indexOf('glow');
  const queueRank = glowIdx < 0 ? null : glowIdx + 1;
  const precedingNames = glowIdx > 0 ? enabledGoals.slice(0, glowIdx).map((g) => REALM_GOAL_LABELS[g]) : [];

  const syncPearpal = async () => {
    if (!isTauri() || syncingPearpal) return;
    setSyncingPearpal(true);
    try {
      await invoke('pearpal_sync_notebook');
      await refresh();
      toast.success('已同步美鸭梨手账祝福闪光图鉴并完成核对');
    } catch (reason) {
      toast.error(String(reason));
    } finally {
      setSyncingPearpal(false);
    }
  };

  const has = (name: string, slot: string, color: string) => progress.owned.has(keyOf({ name, slot, color }));
  const count = (name: string, colors: string[], slot?: string) =>
    (slot ? [slot] : slots).reduce((n, part) => n + colors.filter((color) => has(name, part, color)).length, 0);
  const inScope = (name: string) => planMode === 'all' || chosen.has(name);

  const visible = sets.filter((s) => {
    const owned = count(s.name, s.colors);
    const total = s.colors.length * 3;
    const onlyRainbow =
      owned < total && slots.every((slot) => s.colors.every((color) => color === '彩' || has(s.name, slot, color)));
    return (
      `${s.name} ${s.source}`.includes(search.trim()) &&
      (filter === 'all' ||
        (filter === 'complete' && owned === total) ||
        (filter === 'missing' && owned < total) ||
        (filter === 'rainbow' && onlyRainbow))
    );
  });
  const updated = snapshot?.record?.updated_at;
  const copyShareImage = async () => {
    try {
      const blob = await collectionShareImage({
        owned: progress.owned.size,
        target,
        percentage: progress.percentage ?? 0,
        completeSets,
        totalSets: sets.length,
        rainbowOwned,
        rainbowTotal,
      });
      await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })]);
      toast.success('分享图已复制，可以直接粘贴');
    } catch {
      toast.error('复制分享图失败');
    }
  };

  const listed = [...visible].sort(
    (a, b) => (missingBySource.get(b.source ?? '') ?? 0) - (missingBySource.get(a.source ?? '') ?? 0),
  );
  const scopeLabel = planMode === 'all' ? `全收集 · ${number(sets.length)} 个套组` : `自选 · ${number(scopeSetsCount)} 个套组`;

  const time = (value: string) => new Date(value).toLocaleString('zh-CN', { hour12: false });
  const notices: Notice[] = [];
  if (error) notices.push({ tone: 'error', text: `无法读取账号状态：${error}` });
  else if (!progress.known)
    notices.push({
      tone: 'info',
      text: loading
        ? '正在读取本地记录…'
        : '尚无完整的五星收藏记录。把五星闪光加入幻境挑战并运行一次，会先自动整理；也可以点「美鸭梨核对」预览图鉴已解锁的颜色。',
    });
  if (!error && progress.known && !progress.localKnown && pearpalCheck.synced)
    notices.push({
      tone: 'info',
      text: '当前展示美鸭梨手账图鉴已解锁的颜色（不含胚子数量、噗灵与粒子存量）；首次运行全收集时仍需扫描游戏内仓库。',
    });
  if (progress.localKnown && pearpalCheck.synced)
    notices.push(
      pearpalCheck.pearpalOnlyCount > 0
        ? {
            tone: 'warn',
            text: `美鸭梨图鉴核对发现 ${pearpalCheck.pearpalOnlyCount} 个图鉴已解锁但本地记录未包含的颜色（可能为手动升级或曾分解旧收藏），下次运行全收集会自动核对相关部位。`,
          }
        : { tone: 'ok', text: `美鸭梨图鉴核对一致（图鉴已解锁 ${pearpalCheck.pearpalCount} 色，未发现遗漏）。` },
    );
  if (uncertain)
    notices.push({
      tone: 'warn',
      text: '存在未确认操作或收藏保护待复核，以下展示上次已确认的记录，下次全收集会自动核对。',
    });
  if (rewardPending)
    notices.push({ tone: 'warn', text: '上次刷取结果待核对，已收藏颜色保留，下次全收集会先处理仓库。' });
  const remaining = scopeTarget - scopeOwned;
  const advice = !progress.known
    ? undefined
    : needBling
      ? {
          warn: true,
          title: '刷噗灵',
          detail:
            effectiveReserveBling > 0
              ? `扣除保留噗灵（${wan(effectiveReserveBling)}）后不够升级已有胚子，全部体力刷噗灵。`
              : '噗灵不够升级已有胚子，全部体力刷噗灵。',
        }
      : scopeNext?.[1]
        ? {
            title: `刷「${scopeNext[0]}」`,
            detail: isFarmOnly
              ? '已结合仓库里的满级色与未升级胚子折算，刷这个副本期望新色最高。'
              : effectiveReserveBling > 0 && spendableBling != null && spendableBling < 72000
                ? `这里的胚子最可能出新色；噗灵扣除保留（${wan(effectiveReserveBling)}）后不足升级，刷出的胚子会先暂存。`
                : '这里的胚子最可能出新色。',
          }
        : { title: '所选范围已收齐', detail: '可以把体力用在其他目标了。' };

  return (
    <div className="space-y-4">
      <PlanPanel
        goal="glow"
        tone={error || uncertain || rewardPending ? 'warn' : progress.known ? 'ok' : 'idle'}
        basis={
          updated ? (
            <>
              闪光记录更新于 {time(updated)} · 已收集 <strong className="tabular-nums text-text-primary">{number(progress.owned.size)}</strong> /{' '}
              {number(target)} 色
            </>
          ) : pearpalCheck.updatedAt ? (
            `按美鸭梨手账 ${time(pearpalCheck.updatedAt)} 的图鉴显示`
          ) : (
            '尚无收集记录'
          )
        }
        actions={
          <>
            {isTauri() && (
              <button
                type="button"
                onClick={() => void syncPearpal()}
                disabled={syncingPearpal || loading}
                className={secondaryButton}
                title="从美鸭梨奇想手账获取图鉴已解锁颜色，核对本地记录有没有遗漏"
              >
                <RefreshCw className={clsx('h-3.5 w-3.5', syncingPearpal && 'animate-spin')} />
                美鸭梨核对
              </button>
            )}
            {progress.known && (
              <button type="button" onClick={() => void copyShareImage()} className={secondaryButton} title="复制分享图（不含 UID）">
                <ImageIcon className="h-3.5 w-3.5" />
                分享图
              </button>
            )}
          </>
        }
        mode={planMode}
        onModeChange={(next) => savePlan(next)}
        modeDetail={
          planMode === 'all' ? (
            '全部未收集的五星闪光颜色，按出新色概率跨套组挑选副本。'
          ) : (
            <>只追勾选套组的缺色，收齐后交给顺序里的下一项，已选 {number(scopeSetsCount)} 个套组。</>
          )
        }
        queueStatus={
          <>
            {isFarmOnly ? '只刷胚子' : '全流程托管'} ·{' '}
            {progress.known ? (remaining > 0 ? `还差 ${number(remaining)} 色` : '所选范围已收齐') : '首次运行会先整理仓库'}
          </>
        }
        queueHint="顺序、执行方式、保留噗灵与爆仓分解都在「刷取设置」中调整。所选范围收齐前会一直刷闪光，建议把急需的材料排在前面。"
        advice={advice}
      />
      <Notices items={notices} />

      <PlanLayout
        list={
          <PlanListPanel
            title={planMode === 'all' ? '全收集包含的套组' : '想追全哪些套组'}
            mode={planMode}
            includedCount={scopeSetsCount}
            selectedCount={chosen.size}
            onClear={() => savePlan('selected', new Set())}
            filters={
              <>
                <label className="flex min-w-36 flex-1 items-center gap-1.5">
                  <Search className="h-3.5 w-3.5 text-text-muted" />
                  <input
                    className={clsx(planField, 'w-full')}
                    value={search}
                    onChange={(e) => {
                      setSearch(e.target.value);
                      setLimit(30);
                    }}
                    placeholder="搜索套组或副本"
                    aria-label="搜索套组或副本"
                  />
                </label>
                <Segmented
                  label="筛选收集状态"
                  value={filter}
                  onChange={(next) => {
                    setFilter(next);
                    setLimit(30);
                  }}
                  options={[
                    ['missing', '未收齐'],
                    ['rainbow', '只缺彩色'],
                    ['complete', '已收齐'],
                    ['all', '全部'],
                  ]}
                />
              </>
            }
            bulk={
              <>
                <span>把当前列表全部：</span>
                <button type="button" onClick={() => toggleSets(listed.map((s) => s.name), true)} className={bulkButton}>
                  勾选
                </button>
                <button type="button" onClick={() => toggleSets(listed.map((s) => s.name), false)} className={bulkButton}>
                  取消勾选
                </button>
              </>
            }
            empty={data && !listed.length ? '没有符合条件的套组' : undefined}
            more={{ shown: limit, total: listed.length, onMore: () => setLimit((value) => value + 30) }}
          >
            {listed.slice(0, limit).map((set) => {
              const owned = count(set.name, set.colors);
              const total = set.colors.length * 3;
              const picked = planMode === 'selected' && chosen.has(set.name);
              return (
                <div key={set.name} className={clsx('flex items-center gap-2 px-4 py-2 text-xs', picked && 'bg-accent/5')}>
                  {planMode === 'selected' && (
                    <input
                      type="checkbox"
                      aria-label={`选择${set.name}`}
                      checked={picked}
                      className={planCheckbox}
                      onChange={(e) => toggleSets([set.name], e.target.checked)}
                    />
                  )}
                  <div className="w-28 min-w-0 shrink-0">
                    <p className="truncate font-medium text-text-primary" title={set.name}>
                      {set.name}
                    </p>
                    <p className="truncate text-[11px] text-text-muted">
                      {set.source}
                      {set.source === scopeNext?.[0] && !needBling && inScope(set.name) ? ' · 今日推荐' : ''}
                    </p>
                  </div>
                  <div className="flex min-w-0 flex-1 flex-wrap justify-around gap-x-2 gap-y-1">
                    {slots.map((slot) => (
                      <span key={slot} className="flex items-center gap-1" title={slot}>
                        {set.colors.map((color) => {
                          const collected = has(set.name, slot, color);
                          return (
                            <ColorDot
                              key={color}
                              color={color}
                              collected={collected}
                              label={`${set.name} · ${slot} · ${color}色：${collected ? '已收集' : progress.known ? '未收集' : '未记录'}`}
                            />
                          );
                        })}
                      </span>
                    ))}
                  </div>
                  <span className={clsx('w-12 shrink-0 text-right tabular-nums', owned === total ? 'text-success' : 'text-text-secondary')}>
                    {progress.known ? owned : '—'}/{total}
                  </span>
                </div>
              );
            })}
          </PlanListPanel>
        }
        summary={
          <>
            <Card title={planMode === 'all' ? '收集进度' : '所选套组的收集进度'} aside={<span className="text-xs text-text-secondary">{scopeLabel}</span>}>
              <div className="space-y-3 p-4">
                <p className="flex items-baseline gap-2">
                  <strong className="text-3xl font-semibold tabular-nums">{progress.known ? scopeOwned : '—'}</strong>
                  <span className="text-sm text-text-secondary tabular-nums">/ {scopeTarget || '—'} 色</span>
                  <span className="ml-auto text-lg font-semibold tabular-nums text-accent">
                    {progress.known && scopeTarget ? Math.round((scopeOwned / scopeTarget) * 100) : '—'}%
                  </span>
                </p>
                <div className="h-1.5 overflow-hidden rounded-full bg-border">
                  <div
                    className={clsx('h-full rounded-full transition-[width]', scopeTarget && scopeOwned === scopeTarget ? 'bg-success' : 'bg-accent')}
                    style={{ width: `${scopeTarget ? (scopeOwned / scopeTarget) * 100 : 0}%` }}
                  />
                </div>
                <div className="grid grid-cols-3 gap-2">
                  <Tile label="收齐套组" value={`${scopeComplete} / ${scopeSetsCount}`} />
                  <Tile label="收齐部位（全部）" value={`${completeParts} / ${sets.length * 3}`} />
                  <Tile label="彩色（全部）" value={`${rainbowOwned} / ${rainbowTotal}`} />
                </div>
                {planMode === 'selected' && !scopeSetsCount && (
                  <p className="text-xs text-text-secondary">在左侧勾选想追全的套组，这里会显示所选范围的进度与推荐副本。</p>
                )}
              </div>
            </Card>
            <Card title="闪光资源" aside={<span className="text-xs text-text-secondary">运行幻境挑战时自动更新</span>}>
              <div className="grid grid-cols-2 gap-2 p-4 sm:grid-cols-3">
                <Tile
                  label="噗灵"
                  value={resources?.bling == null ? '待读取' : wan(resources.bling)}
                  detail={
                    resources?.bling == null
                      ? '下次升级时读取'
                      : effectiveReserveBling > 0
                        ? `够升级 ${affordableUpgrades} 次（保留 ${wan(effectiveReserveBling)}）`
                        : `够升级 ${affordableUpgrades} 次`
                  }
                  warn={needBling || (!isFarmOnly && spendableBling != null && spendableBling < 72000)}
                />
                <Tile
                  label="可用粒子（含重复折算）"
                  value={resources?.particles == null ? '待读取' : wan(particleTotal)}
                  detail={resources?.particles == null ? '下次分解时读取' : `够升满 ${Math.floor(particleTotal / 360)} 次`}
                />
                <Tile label="待升级胚子" value={resources ? number(resources.embryos ?? 0) : '—'} detail="有胚子时优先升级" />
                <Tile label="满级重复" value={resources ? `约 ${number(resources.duplicates ?? 0)}` : '—'} detail="每件折合 300 粒子" />
                <Tile
                  label="仓库"
                  value={resources?.capacity ? `${number(resources.capacity[0])} / ${number(resources.capacity[1])}` : '待读取'}
                  detail="满了会分解一批再刷本"
                  warn={!!resources?.capacity && resources.capacity[0] >= resources.capacity[1]}
                />
                <Tile
                  label="本账号彩色"
                  value={rolls?.total ? `${rolls.colors['彩'] ?? 0} 次` : '—'}
                  detail={
                    rolls?.total
                      ? `升满 ${number(rolls.total)} 次，按 ${((probability?.iridescent ?? 0.05) * 100).toFixed(1)}% 约应出 ${(rolls.total * (probability?.iridescent ?? 0.05)).toFixed(1)} 次`
                      : '全收集下次升满后开始统计'
                  }
                />
              </div>
            </Card>
          </>
        }
      />

      {progress.known && blessing.status !== 'empty_selection' && (
        <BlessingForecastPanel
          standalone={glowStandalone}
          queued={glowQueued}
          busy={glowComputing}
          presets={presets}
          chosenPresetId={chosenPreset?.id ?? 'catalog'}
          history={history}
          reserveBling={effectiveReserveBling}
          extraBling={extraBling}
          extraParticles={extraParticles}
          onExtraBlingChange={setExtraBling}
          onExtraParticlesChange={setExtraParticles}
          queueRank={queueRank}
          precedingNames={precedingNames}
          done={blessing.status === 'done'}
          blockedByCapacity={Boolean((glowQueued ?? glowStandalone)?.blockedByCapacity)}
          isFarmOnly={isFarmOnly}
        />
      )}
    </div>
  );
}
