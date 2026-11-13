import { useEffect, useMemo, useRef, useState } from 'react';
import { AlertCircle, ArrowUpRight, Ban, Calculator, Check, ClipboardCopy, Loader2, RefreshCw, RotateCcw, Search, Sparkles, Square, Star, X } from 'lucide-react';
import clsx from 'clsx';
import { openUrl } from '@tauri-apps/plugin-opener';
import { useAccountStateStore } from '@/stores/accountStateStore';
import { isTauri } from '@/utils/paths';
import { loadScoring, saveScoring } from '@/services/scoring/api';
import { scorePiece } from '@/services/scoring/math';
import { analyzeUnmetStars, buildMinimumStarUpgradePlan, starRequiredGroupBadges, tagLabel } from '@/services/scoring/diagnostics';
import { buildOwnedProfile } from '@/services/scoring/profile';
import { ATTRIBUTES, emptySettings } from '@/services/scoring/types';
import type { CalculationResult, FieldBonus, Match, OutfitResult, OwnedProfile, ScoringCatalog, ScoringPayload, ScoringSettings } from '@/services/scoring/types';
import { secondaryButton, StatusBar } from './ui/FeaturePage';
import { WardrobeHeartEditor } from './WardrobeHeartEditor';
import { WardrobeMatchPicker } from './WardrobeMatchPicker';
import { WardrobeBatchPlanView, WardrobeMaterialList } from './WardrobeMaterialActions';
import { ImageIcon, WardrobePhotoCardModal } from './WardrobePhotoCardModal';

const field = 'w-full rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm outline-none focus:border-accent';
const card = 'rounded-xl border border-border bg-bg-secondary p-4';
const number = (value: number) => value.toLocaleString('zh-CN', { maximumFractionDigits: 2 });
const levelLabel = (level: number | null) => level === null ? '0 级' : level === 11 ? '已焕新' : `${level} 级`;
const bonusLabel = (bonus: FieldBonus) => bonus.description.replaceAll('{1}', String(bonus.value ?? bonus.multiplier));
const rankName = (rank: number) => ({ 2: '一般', 4: '优秀', 5: '完美' }[rank] ?? `评价 ${rank}`);
const starLabel = (match: Match, index: number) => match.stars[index].description || match.stars[index].groups.map((group) =>
  group.map((rule) => rule.kind === 'rank' ? `获得${rankName(rule.rank)}评价` : rule.kind === 'suit' ? '满足套装要求' : `穿戴 ${rule.count} 件符合要求的部件`).join('，')).join('；或');
const ranksIn = (match: Match | undefined) => [...new Set(match?.stars.flatMap((star) =>
  star.groups.flatMap((group) => group.flatMap((rule) => rule.kind === 'rank' ? [rule.rank] : []))) ?? [])].sort((a, b) => a - b);
const LEVEL_PAGE = 60;

/** Type 0-10, or 11 for refreshed; clearing the field falls back to minimum level 0. */
function LevelInput({ value, onChange, inferred }: { value: number | null; onChange: (level: number | null) => void; inferred?: string | null }) {
  if (inferred) return <span className="shrink-0 rounded-lg bg-accent/10 px-2 py-1 text-xs text-accent">已焕新</span>;
  return <input aria-label="共享养成等级，0 到 10，11 表示已焕新，留空默认按 0 级计算" type="number" min="0" max="11" placeholder="0" value={value ?? ''}
    className={clsx(field, '!w-16 shrink-0 !px-2 !py-1 text-center !text-xs tabular-nums')}
    onChange={(event) => {
      const raw = event.target.value;
      const level = Number(raw);
      if (raw === '') onChange(null);
      else if (Number.isInteger(level) && level >= 0 && level <= 11) onChange(level);
    }} />;
}

function Stars({ outfit, match }: { outfit: OutfitResult; match: Match }) {
  if (!outfit.stars.length) return null;
  return <div className="flex gap-1.5" aria-label="星级条件结果">{outfit.stars.map((passed, index) =>
    <span key={index} title={`${starLabel(match, index)}：${passed === null ? '待填写分数门槛' : passed ? '满足' : '未满足'}`}>
      <Star className={clsx('h-4 w-4', passed === true ? 'fill-accent text-accent' : passed === null ? 'text-text-muted' : 'text-text-muted/40')} />
    </span>)}</div>;
}

function MatchBrief({ match, catalog }: { match: Match; catalog: ScoringCatalog }) {
  const strongest = Math.max(...match.weights);
  const tagEntries = Object.entries(match.tags);
  return <section className={card}>
    <div className="flex flex-wrap items-center justify-between gap-2">
      <div>
        <p className="text-xs text-text-muted">{match.group}</p>
        <h3 className="mt-1 text-base font-semibold">{match.name}</h3>
      </div>
      {match.thresholds?.['5'] && (
        <div className="text-right text-[11px] text-text-muted">
          <span className="rounded-md bg-accent/10 px-2 py-0.5 font-medium text-accent">
            参考完美线 {number(match.thresholds['5'])}
          </span>
          {match.max_score && <span className="ml-1.5">满槽 {number(match.max_score)}</span>}
        </div>
      )}
    </div>
    <div className="mt-3 grid grid-cols-5 gap-2">{ATTRIBUTES.map((name, index) => <div key={name}>
      <p className={clsx('text-xs', match.weights[index] === strongest ? 'font-semibold text-accent' : 'text-text-muted')}>{name}</p>
      <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-border"><div className={clsx('h-full rounded-full', match.weights[index] === strongest ? 'bg-accent' : 'bg-text-muted/40')}
        style={{ width: `${match.weights[index] / strongest * 100}%` }} /></div>
      <p className="mt-1 text-[11px] tabular-nums text-text-muted">×{number(match.weights[index])}</p>
    </div>)}</div>
    {(!!tagEntries.length || !!match.bonuses.length) && (
      <div className="mt-3 flex flex-wrap gap-1.5">
        {tagEntries.map(([tagId, weights]) => (
          <span key={`tag-${tagId}`} className="rounded-lg border border-accent/30 bg-accent/5 px-2 py-0.5 text-xs text-accent">
            标签 · {tagLabel(catalog, tagId)} ×{weights.join('/')}
          </span>
        ))}
        {match.bonuses.map((bonus, index) => (
          <span key={index} className="rounded-lg bg-accent/10 px-2 py-0.5 text-xs text-accent">{bonusLabel(bonus)}</span>
        ))}
      </div>
    )}
    {!!match.stars.length && <ul className="mt-3 space-y-1 text-xs text-text-secondary">{match.stars.map((_, index) =>
      <li key={index} className="flex gap-1.5"><Star className="mt-0.5 h-3 w-3 shrink-0 text-text-muted" />{starLabel(match, index)}</li>)}</ul>}
  </section>;
}

function UnmetStarsPanel({
  outfit,
  potentialOutfit,
  payload,
  profile,
  settings,
  matchId,
  match,
  potential,
  setLevel,
  setLevelsBatch,
  onMaterialsChange,
}: {
  outfit: OutfitResult;
  potentialOutfit: OutfitResult | null;
  payload: ScoringPayload;
  profile: OwnedProfile;
  settings: ScoringSettings;
  matchId: string;
  match: Match;
  potential: boolean;
  setLevel: (group: string, level: number | null) => void;
  setLevelsBatch: (updates: Record<string, number | null>, autoRun?: boolean) => void;
  onMaterialsChange: (next: ScoringPayload['materials']) => void;
}) {
  const diagnostics = useMemo(
    () => analyzeUnmetStars(payload.catalog, profile, settings, matchId, match, outfit),
    [payload.catalog, profile, settings, matchId, match, outfit],
  );
  const minPlan = useMemo(
    () =>
      !potential
        ? buildMinimumStarUpgradePlan(
            payload.catalog,
            settings,
            matchId,
            match,
            outfit,
            potentialOutfit,
          )
        : null,
    [payload.catalog, settings, matchId, match, outfit, potentialOutfit, potential],
  );
  if (!diagnostics.length) return null;

  return (
    <section className="rounded-xl border border-warning/40 bg-warning/5 p-4 space-y-3">
      <div className="flex items-center justify-between gap-2">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold text-warning">
          <AlertCircle className="h-4 w-4 shrink-0" />
          未满星原因与补齐建议（{potential ? '全部焕新后' : '当前等级'}）
        </h3>
        <span className="text-xs text-text-muted">共 {diagnostics.length} 颗星待达成</span>
      </div>

      {minPlan && (
        <div className="rounded-xl border border-accent/40 bg-accent/5 p-3.5 space-y-3 text-xs">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div className="space-y-1">
              <div className="flex flex-wrap items-center gap-2">
                <span className="inline-flex items-center gap-1 rounded-md bg-accent px-2 py-0.5 text-xs font-semibold text-on-accent">
                  <Sparkles className="h-3 w-3" />
                  满星最少养成方案
                </span>
                <strong className="text-sm text-text-primary">
                  {`最少需升级 ${minPlan.upgradeItems.length} 件衣服${
                    minPlan.mandatorySuitName &&
                    minPlan.upgradeItems.every((i) => i.mandatorySuitRefresh)
                      ? `（将「${minPlan.mandatorySuitName}」升至已焕新）`
                      : ''
                  }`}
                </strong>
              </div>
              <p className="leading-5 text-text-secondary">
                {minPlan.mandatorySuitName &&
                minPlan.upgradeItems.every((i) => i.mandatorySuitRefresh) ? (
                  <>
                    本关星级要求穿戴<strong>整套焕新套装</strong>，你已集齐{' '}
                    <strong className="text-text-primary">「{minPlan.mandatorySuitName}」</strong>，
                    按规则必须将以下{' '}
                    <strong className="text-accent">{minPlan.upgradeItems.length} 件</strong>{' '}
                    套装部件全部升至<strong>已焕新 (11级)</strong> 才能触发套装条件（少升 1 件均无法达成）；其余散件保持当前或默认 0 级即可。
                  </>
                ) : (
                  <>
                    无需将全身衣服全部升满！按单件提分性价比从高到低计算，
                    <strong>最少只需升级以下 {minPlan.upgradeItems.length} 件衣服</strong>
                    （其余部件保持当前或默认 0 级即可），即可同时满足星级条件并跨过分数线。
                  </>
                )}{' '}
                达标后预计总分{' '}
                <strong className="tabular-nums text-accent">{number(minPlan.achievedScore)}</strong> 分
                {minPlan.targetScore !== undefined && (
                  <>（完美线 {number(minPlan.targetScore)} 分）</>
                )}
                ，可直接达成 <strong>{minPlan.targetStars} 星</strong>。
              </p>
            </div>

            <div className="flex flex-wrap items-center gap-1.5">
              <button
                type="button"
                onClick={() => {
                  const updates: Record<string, number | null> = {};
                  for (const item of minPlan.upgradeItems) {
                    updates[item.groupId] = item.toLevel;
                  }
                  setLevelsBatch(updates, true);
                }}
                className="rounded-lg bg-accent px-2.5 py-1 text-xs font-medium text-on-accent hover:bg-accent-hover"
              >
                {`一键标记这 ${minPlan.upgradeItems.length} 件已达标并重算`}
              </button>
            </div>
          </div>

          <div className="rounded-lg border border-border bg-bg-secondary divide-y divide-border">
            {minPlan.upgradeItems.map((item, idx) => (
              <div key={item.groupId} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-accent/15 text-[10px] font-semibold text-accent">
                      {idx + 1}
                    </span>
                    <span className="text-text-muted">{item.partName}</span>
                    <strong className="text-text-primary">{item.name}</strong>
                    <span
                      className={clsx(
                        'rounded px-1.5 py-0.2 text-[10px] font-medium',
                        item.mandatorySuitRefresh
                          ? 'bg-warning/15 text-warning'
                          : 'bg-accent/10 text-accent',
                      )}
                    >
                      {item.reason}
                    </span>
                  </div>
                  <p className="mt-0.5 text-[11px] text-text-muted">
                    当前：{item.levelKnown ? levelLabel(item.fromLevel) : '0 级（默认）'}{' '}
                    → 需升至 <strong className="text-accent">{levelLabel(item.toLevel)}</strong>
                    {' · '}单件得分 {number(item.scoreAtFrom)} → {number(item.scoreAtTo)}{' '}
                    <span className="font-medium text-accent">(+{number(item.gain)})</span>
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-[11px] text-text-muted">当前实际等级</span>
                  <LevelInput
                    value={settings.levels[item.groupId] ?? null}
                    onChange={(lvl) => setLevel(item.groupId, lvl)}
                  />
                </div>
              </div>
            ))}
          </div>

          {Object.keys(minPlan.totalCost).length > 0 && (
            <div className="rounded-lg border border-border bg-bg-secondary p-3 space-y-2">
              <p className="font-medium text-text-primary">
                升级以上 {minPlan.upgradeItems.length} 件衣服预计所需材料（未填等级部件按 0 级计算）：
              </p>
              <WardrobeMaterialList
                cost={minPlan.totalCost}
                catalog={payload.catalog}
                accountKey={payload.account_key}
                materials={payload.materials}
                sourceLabel={`${match.group} · ${match.name}（满星最少升级）`}
                onMaterialsChange={onMaterialsChange}
              />
            </div>
          )}
        </div>
      )}

      <div className="space-y-3">
        {diagnostics.map((star) => (
          <div key={star.starIndex} className="rounded-lg border border-border bg-bg-secondary p-3 space-y-2.5 text-xs">
            <div className="flex items-center justify-between gap-2">
              <span className="font-medium text-text-primary">
                第 {star.starIndex} 星：{star.description}
              </span>
              <span className="shrink-0 rounded bg-warning/15 px-1.5 py-0.5 text-[11px] text-warning">
                {star.status === null ? '待填门槛' : '未达成'}
              </span>
            </div>

            {star.rules.map((rule, rIdx) => {
              if (rule.kind === 'rank') {
                if (rule.satisfied === true) {
                  return (
                    <p key={rIdx} className="flex items-center gap-1.5 text-success">
                      <Check className="h-3.5 w-3.5 shrink-0" />
                      {rule.rankLabel}评价分数已达标（当前 {number(rule.currentScore)} / 目标 {number(rule.targetScore ?? 0)}）
                    </p>
                  );
                }
                const potentialBeats =
                  !potential &&
                  potentialOutfit &&
                  rule.targetScore !== undefined &&
                  potentialOutfit.score >= rule.targetScore;
                return (
                  <div key={rIdx} className="space-y-1.5 text-text-secondary">
                    <p>
                      <span className="font-medium text-text-primary">分数差距：</span>
                      {rule.targetScore !== undefined ? (
                        <>
                          当前 <strong className="tabular-nums text-text-primary">{number(rule.currentScore)}</strong> 分，
                          距离{rule.isCustom ? '自定义' : '参考'}{rule.rankLabel}线{' '}
                          <strong className="tabular-nums text-text-primary">{number(rule.targetScore)}</strong> 分还差{' '}
                          <strong className="tabular-nums text-warning">{number(rule.deficit ?? 0)}</strong> 分
                          {rule.maxScore ? `（客户端满槽 ${number(rule.maxScore)}）` : ''}。
                        </>
                      ) : (
                        '尚未设置该评价的分数门槛，可在左侧填写。'
                      )}
                    </p>
                    {potentialBeats && (
                      <p className="text-accent">
                        全部焕新后可达 <strong>{number(potentialOutfit!.score)}</strong> 分（已越过{rule.rankLabel}线），升级/焕新部件（或补充已强化衣服的实际等级）即可过线。
                      </p>
                    )}
                  </div>
                );
              }

              if (rule.kind === 'pieces') {
                return (
                  <div key={rIdx} className="space-y-2 border-t border-border/60 pt-2 first:border-0 first:pt-0">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span className="text-text-secondary">
                        部件条件：要求穿戴 <strong>{rule.requiredCount}</strong> 件{rule.filterLabel}部件
                      </span>
                      <span className={clsx('font-medium', rule.satisfied ? 'text-success' : 'text-warning')}>
                        当前已穿戴 {rule.equippedCount} / {rule.requiredCount} 件
                        {rule.satisfied ? '（部件数已达标）' : ''}
                      </span>
                    </div>

                    {!rule.satisfied && rule.maxEquipableOwnedCount >= rule.requiredCount && (
                      <p className="text-text-secondary">
                        衣柜已有足够符合条件的部件（最多可同时穿戴 <strong>{rule.maxEquipableOwnedCount}</strong> 件），将左侧计算目标设为「优先满足星级条件」或恢复已排除部件即可凑齐。
                      </p>
                    )}

                    {rule.maxEquipableOwnedCount < rule.requiredCount && (
                      <div className="rounded-lg border border-warning/30 bg-bg-primary/60 p-2.5 space-y-1.5">
                        <p className="text-warning">
                          衣柜库存不足：你当前拥有的{rule.filterLabel}部件在部位不冲突的前提下最多只能同时穿戴{' '}
                          <strong>{rule.maxEquipableOwnedCount}</strong> 件（需要 {rule.requiredCount} 件）。
                        </p>
                        {!!rule.recommendedUnowned.length && (
                          <>
                            <p className="text-text-secondary">建议优先获取以下缺失部位的符合条件部件：</p>
                            <ul className="space-y-1 text-text-secondary">
                              {rule.recommendedUnowned.map((rec) => (
                                <li key={rec.id} className="flex flex-wrap items-center justify-between gap-2 rounded bg-bg-secondary px-2 py-1">
                                  <span>
                                    <strong className="text-text-primary">{rec.name}</strong>
                                    <span className="ml-1.5 text-text-muted">{rec.partName} · {rec.quality}★</span>
                                    {rec.slotNeeded && (
                                      <span className="ml-1.5 rounded bg-accent/10 px-1.5 py-0.2 text-[10px] text-accent">空缺部位</span>
                                    )}
                                  </span>
                                  <span className="text-[11px] text-text-muted">{rec.source ?? '常驻/活动获取'}</span>
                                </li>
                              ))}
                            </ul>
                          </>
                        )}
                      </div>
                    )}

                    {rule.satisfied && !!rule.upgradeableEquipped.length && (
                      <p className="text-text-secondary">
                        为满足{rule.filterLabel}件数穿戴了未满级部件（
                        {rule.upgradeableEquipped.map((p) => `${p.item.name} ${levelLabel(p.level)}`).join('、')}
                        ），优先强化它们可在保持满星条件的同时提高总分。
                      </p>
                    )}
                  </div>
                );
              }

              // rule.kind === 'suit'
              return (
                <div key={rIdx} className="space-y-2 border-t border-border/60 pt-2 first:border-0 first:pt-0">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="text-text-secondary">
                      套装条件：要求穿戴完整 <strong>{rule.filterLabel}</strong>
                    </span>
                    <span className={clsx('font-medium', rule.satisfied ? 'text-success' : 'text-warning')}>
                      {rule.satisfied ? '套装条件已满足' : '套装条件未满足'}
                    </span>
                  </div>

                  {rule.candidates.length ? (
                    <div className="space-y-2">
                      {rule.candidates.map((cand) => {
                        const pendingGroups = cand.unrefreshedGroups;
                        return (
                          <div key={cand.suitId} className="rounded-lg border border-border bg-bg-primary/60 p-2.5 space-y-2">
                            <div className="flex flex-wrap items-center justify-between gap-2">
                              <span className="font-medium text-text-primary">
                                {cand.suitName} <span className="text-text-muted">({cand.quality}★)</span>
                              </span>
                              <div className="flex flex-wrap items-center gap-2">
                                {cand.complete && (
                                  <span className="text-[11px] text-text-secondary">
                                    已达标 <strong className="text-text-primary">{cand.readyCount}</strong> / {cand.requiredCount} 件
                                    {pendingGroups.length > 0 && (
                                      <span className="ml-1 text-warning">（还需焕新 {pendingGroups.length} 件）</span>
                                    )}
                                  </span>
                                )}
                                <span className={clsx('text-[11px] font-medium', cand.complete ? 'text-success' : 'text-warning')}>
                                  已收集 {cand.ownedCount} / {cand.totalCount} 件
                                </span>
                              </div>
                            </div>

                            {cand.complete && !!pendingGroups.length && (
                              <div className="space-y-1.5">
                                <div className="flex flex-wrap items-center justify-between gap-2">
                                  <p className="text-text-secondary">
                                    已集齐该套装，本关要求<strong>整套 {cand.requiredCount} 件全部焕新 (11级)</strong>，以下{' '}
                                    <strong className="text-warning">{pendingGroups.length}</strong> 件尚未焕新（未填等级按 0 级计）：
                                  </p>
                                  <div className="flex items-center gap-1.5">
                                    <button
                                      type="button"
                                      onClick={() => {
                                        const updates: Record<string, number | null> = {};
                                        for (const g of pendingGroups) updates[g.id] = 11;
                                        setLevelsBatch(updates, true);
                                      }}
                                      className="rounded bg-accent/15 px-2 py-0.5 text-[11px] font-medium text-accent hover:bg-accent/25"
                                    >
                                      整套标记已焕新(11级)
                                    </button>
                                  </div>
                                </div>
                                <div className="divide-y divide-border/60">
                                  {pendingGroups.map((g) => (
                                    <div key={g.id} className="flex items-center justify-between gap-2 py-1">
                                      <span>
                                        <strong className="text-text-primary">{g.name}</strong>{' '}
                                        <span className="text-text-muted">
                                          · {g.part}（当前：{g.level === null ? '0 级（默认）' : levelLabel(g.level)} → 需升至 已焕新）
                                        </span>
                                      </span>
                                      <LevelInput
                                        value={settings.levels[g.id] ?? null}
                                        inferred={g.inferred}
                                        onChange={(lvl) => setLevel(g.id, lvl)}
                                      />
                                    </div>
                                  ))}
                                </div>
                              </div>
                            )}

                            {cand.complete && !pendingGroups.length && !rule.satisfied && (
                              <p className="text-text-secondary">
                                已集齐该套装且满足穿戴条件，将左侧计算目标设为「优先满足星级条件」或恢复已排除部件即可整套穿戴。
                              </p>
                            )}

                            {!cand.complete && !!cand.missingParts.length && (
                              <div className="space-y-1">
                                <p className="text-text-secondary">缺失部件（共 {cand.missingParts.length} 件）：</p>
                                <ul className="space-y-1">
                                  {cand.missingParts.map((mp) => (
                                    <li key={mp.id} className="flex flex-wrap items-center justify-between gap-2 text-text-secondary">
                                      <span><strong className="text-text-primary">{mp.name}</strong> · {mp.partName}</span>
                                      <span className="text-[11px] text-text-muted">{mp.source ?? '套装获取'}</span>
                                    </li>
                                  ))}
                                </ul>
                              </div>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  ) : (
                    <p className="text-text-muted">图鉴中暂未找到匹配的套装记录。</p>
                  )}
                </div>
              );
            })}
          </div>
        ))}
      </div>
    </section>
  );
}

function OutfitView({ outfit, potentialOutfit, payload, profile, settings, matchId, match, potential, setLevel, setLevelsBatch, excludedIds, onToggleExclude, onMaterialsChange }: {
  outfit: OutfitResult | null; potentialOutfit: OutfitResult | null; payload: ScoringPayload; profile: OwnedProfile; settings: ScoringSettings; matchId: string; match: Match; potential: boolean;
  setLevel: (group: string, level: number | null) => void;
  setLevelsBatch: (updates: Record<string, number | null>, autoRun?: boolean) => void;
  excludedIds: string[];
  onToggleExclude: (id: string) => void;
  onMaterialsChange: (next: ScoringPayload['materials']) => void;
}) {
  if (!outfit) return <div className={clsx(card, 'py-9 text-center text-sm text-text-secondary')}>
    暂未找到可用的完整搭配，请检查衣柜读取情况或恢复已排除部件。
  </div>;
  const { catalog } = payload;
  const top = Math.max(...outfit.pieces.map((piece) => piece.score), 1);
  const upgrades = potential ? outfit.pieces.filter((piece) => piece.gain).sort((a, b) => (b.gain ?? 0) - (a.gain ?? 0)) : [];
  return <div className="space-y-3">
    <UnmetStarsPanel
      outfit={outfit}
      potentialOutfit={potentialOutfit}
      payload={payload}
      profile={profile}
      settings={settings}
      matchId={matchId}
      match={match}
      potential={potential}
      setLevel={setLevel}
      setLevelsBatch={setLevelsBatch}
      onMaterialsChange={onMaterialsChange}
    />
    <section className="overflow-hidden rounded-xl border border-border bg-bg-secondary">
      {!outfit.exact && <p className="border-b border-border px-4 py-2 text-xs text-text-secondary">搜索时间已到，展示当前找到的可行方案。</p>}
      {outfit.choice !== null && <p className="border-b border-border px-4 py-2 text-xs text-text-secondary">场地加成：{bonusLabel(match.choices[outfit.choice])}</p>}
      {outfit.pieces.map((piece) => {
        const isExcluded = excludedIds.includes(piece.item.id);
        const itemTags = catalog.items[piece.item.id]?.visibleTags ?? [];
        return <details key={`${piece.item.id}:${piece.item.group ?? ''}`} className="group border-b border-border last:border-0">
          <summary className="flex cursor-pointer list-none items-center gap-3 px-4 py-2.5 hover:bg-bg-hover">
            <span className="w-16 shrink-0 truncate text-xs text-text-muted">{(catalog.part_names[piece.type] ?? String(piece.type)).split(' / ').pop()}</span>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-1.5">
                <p className={clsx('truncate text-sm', isExcluded && 'line-through opacity-60')}>{piece.item.name}</p>
                {itemTags.map((t) => (
                  <span key={t} className="shrink-0 rounded bg-bg-primary px-1.5 py-0.2 text-[10px] text-text-muted">
                    {tagLabel(catalog, t)}
                  </span>
                ))}
              </div>
              <div className="mt-1 flex items-center gap-2"><div className="h-1 flex-1 overflow-hidden rounded-full bg-border"><div className="h-full rounded-full bg-accent/60" style={{ width: `${piece.score / top * 100}%` }} /></div>
                <span className="shrink-0 text-[11px] text-text-muted">{potential && piece.from !== piece.level ? `${levelLabel(piece.from)} → ${levelLabel(piece.level)}` : levelLabel(piece.level)}</span></div></div>
            <strong className="w-16 text-right text-sm font-semibold tabular-nums">{number(piece.score)}</strong>
          </summary>
          <div className="space-y-3 border-t border-border bg-bg-primary/50 px-4 py-3 text-xs">
            <div className="flex flex-wrap items-center justify-between gap-2">
              {piece.item.group ? (
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-text-secondary">当前共享等级 · {piece.item.evidence ?? '默认按 0 级计'}</span>
                  <LevelInput value={settings.levels[piece.item.group] ?? null} inferred={piece.item.refreshReward}
                    onChange={(level) => setLevel(piece.item.group!, level)} />
                </div>
              ) : <span className="text-text-secondary">{piece.item.evidence || '祝福闪光'}</span>}
              <button type="button" onClick={() => onToggleExclude(piece.item.id)}
                className={clsx('inline-flex items-center gap-1 rounded-lg border px-2.5 py-1 text-xs transition-colors',
                  isExcluded
                    ? 'border-accent bg-accent/10 text-accent'
                    : 'border-border bg-bg-secondary text-text-secondary hover:border-warning hover:text-warning')}>
                <Ban className="h-3 w-3" />{isExcluded ? '已排除（点击恢复）' : '排除此件并重算'}
              </button>
            </div>
            <div className="grid grid-cols-5 gap-2">{ATTRIBUTES.map((name, index) => <div key={name}>
              <p className="text-text-muted">{name}</p><p className="mt-1 tabular-nums">{number(piece.attributeScores[index])}</p>
              <p className="mt-1 text-text-muted">属性 {number(piece.grownAttributes[index])}</p>
            </div>)}</div>
            <p className="leading-5 text-text-secondary">标签分 {number(piece.tagScore)} · 最终倍率 ×{number(piece.multiplier)} · 单件向上取整</p>
          </div>
        </details>;
      })}
      {outfit.globalScore !== 0 && <p className="px-4 py-3 text-xs text-text-secondary">全局固定加分 {number(outfit.globalScore)}</p>}
    </section>
    {potential && <section className={card}><h3 className="text-sm font-medium">先升哪件</h3>
      {upgrades.length ? <ol className="mt-3 space-y-2 text-xs">{upgrades.slice(0, 8).map((piece) =>
        <li key={`${piece.item.id}:${piece.item.group ?? ''}`} className="flex items-center gap-2"><ArrowUpRight className="h-3.5 w-3.5 shrink-0 text-accent" />
          <span className="min-w-0 flex-1 truncate">{piece.item.name}<span className="ml-2 text-text-muted">{levelLabel(piece.from)} → {levelLabel(piece.level)}</span></span>
          <strong className="tabular-nums text-accent">+{number(piece.gain!)}</strong></li>)}</ol>
        : <p className="mt-2 text-xs text-text-secondary">当前搭配中的服装均已满级焕新。</p>}
      <h3 className="mt-4 text-sm font-medium">养成材料差额</h3>
      <WardrobeMaterialList
        cost={outfit.cost}
        catalog={catalog}
        accountKey={payload.account_key}
        materials={payload.materials}
        sourceLabel={`${match.group} · ${match.name}`}
        onMaterialsChange={onMaterialsChange}
      />
      {!!outfit.unknownCostGroups.length && <p className="mt-3 text-xs text-text-secondary">其中 {outfit.unknownCostGroups.length} 个未填等级部件已按最低等级（0 级）计算提升值与材料差额；若游戏内已强化，展开部件填入实际等级即可扣减对应消耗。</p>}
    </section>}
  </div>;
}

export function WardrobeCalculator({ refreshToken, onOpenRead }: { refreshToken: number; onOpenRead: () => void }) {
  const activeUid = useAccountStateStore((state) => state.data?.uid);
  const [payload, setPayload] = useState<ScoringPayload | null>(null);
  const [settings, setSettings] = useState<ScoringSettings>(emptySettings);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState('');
  const [saving, setSaving] = useState(false);
  const [matchId, setMatchId] = useState('');
  const [choice, setChoice] = useState<number | null>(null);
  const [goal, setGoal] = useState<'score' | 'stars'>('stars');
  const [planScope, setPlanScope] = useState<'single' | 'series'>('single');
  const [excludedIds, setExcludedIds] = useState<string[]>([]);
  const [query, setQuery] = useState('');
  const [levelFilter, setLevelFilter] = useState<'all' | 'unknown' | 'used'>('unknown');
  const [levelLimit, setLevelLimit] = useState(LEVEL_PAGE);
  const [completed, setCompleted] = useState<{ value: CalculationResult; key: object; matchId: string } | null>(null);
  const [mode, setMode] = useState<'current' | 'potential'>('current');
  const [stage, setStage] = useState('');
  const [copied, setCopied] = useState(false);
  const [shareOpen, setShareOpen] = useState(false);
  const worker = useRef<Worker | null>(null);
  const draftRef = useRef({ settings, saved });
  const payloadRef = useRef(payload);
  const contextKey = useMemo(() => ({}), [payload, settings, matchId, choice, goal, planScope, excludedIds]);
  const result = completed?.value ?? null;
  const stale = !!completed && completed.key !== contextKey;
  draftRef.current = { settings, saved };
  payloadRef.current = payload;

  useEffect(() => {
    let cancelled = false;
    const previousKey = payloadRef.current?.account_key;
    const previousDraft = draftRef.current;
    setLoading(true); setPayload(null); setError(''); setSaved('');
    void loadScoring().then((next) => {
      if (cancelled) return;
      setPayload(next);
      const preserveDraft = previousKey === next.account_key && JSON.stringify(previousDraft.settings) !== previousDraft.saved;
      setSettings(preserveDraft ? previousDraft.settings : next.settings ?? emptySettings());
      setSaved(preserveDraft ? previousDraft.saved : JSON.stringify(next.settings ?? emptySettings()));
      const matches = Object.entries(next.catalog.matches);
      // Prefer the newest dated peak contest available in the bundled data.
      const latest = matches.filter(([, match]) => match.group.includes('巅峰赛')).sort((a, b) => b[1].group.localeCompare(a[1].group));
      setMatchId((previous) => previousKey === next.account_key && next.catalog.matches[previous] ? previous : ([...latest, ...matches][0] ?? [''])[0]);
      if (previousKey !== next.account_key) { setChoice(null); setCompleted(null); }
    }).catch((cause: unknown) => { if (!cancelled) setError(String(cause)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [activeUid, refreshToken]);

  useEffect(() => {
    worker.current?.terminate(); worker.current = null;
    setStage('');
  }, [settings, payload, matchId, choice, goal, planScope, excludedIds]);
  useEffect(() => () => { worker.current?.terminate(); }, []);
  useEffect(() => { setLevelLimit(LEVEL_PAGE); }, [query, levelFilter, matchId]);

  const dirty = !!payload && JSON.stringify(settings) !== saved;
  // Levels and bonuses save themselves; the indicator reports the outcome.
  useEffect(() => {
    if (!dirty || !payload) return;
    const key = payload.account_key;
    const draft = settings;
    const timer = setTimeout(() => {
      setSaving(true);
      void saveScoring(key, draft).then(() => {
        if (payloadRef.current?.account_key === key) setSaved(JSON.stringify(draft));
      }).catch((cause: unknown) => {
        if (payloadRef.current?.account_key === key) setError(String(cause));
      }).finally(() => setSaving(false));
    }, 800);
    return () => clearTimeout(timer);
  }, [dirty, payload, settings]);

  const profile = useMemo(() => payload ? buildOwnedProfile(payload, settings) : null, [payload, settings]);
  const match = payload?.catalog.matches[matchId];
  const seriesMatchIds = useMemo(() => {
    if (!payload || !match) return [];
    const stageNumber = (name: string) => Number(/第(\d+)场/.exec(name)?.[1] ?? Infinity);
    return Object.entries(payload.catalog.matches)
      .filter(([, m]) => m.group === match.group)
      .sort((a, b) => stageNumber(a[1].name) - stageNumber(b[1].name))
      .map(([id]) => id);
  }, [payload, match]);
  const resultMatch = completed ? payload?.catalog.matches[completed.matchId] : undefined;
  const used = useMemo(() => new Set([
    ...(result?.[mode]?.pieces.flatMap((piece) => piece.item.group ? [piece.item.group] : []) ?? []),
    ...(result?.batch?.upgrades.map((up) => up.groupId) ?? []),
  ]), [result, mode]);
  // Best potential score for this match: the most useful levels come first.
  const groupValue = useMemo(() => {
    const values = new Map<string, number>();
    if (!payload || !profile || !match) return values;
    const context = { counts: {}, suits: new Set<string>() };
    for (const item of profile.items) {
      if (!item.group) continue;
      const record = payload.catalog.items[item.id];
      const score = scorePiece(payload.catalog, item, payload.catalog.curves[record.curve].ratios.length - 1, match,
        settings.heart, match.bonuses, context).score;
      values.set(item.group, Math.max(values.get(item.group) ?? 0, score));
    }
    return values;
  }, [payload, profile, match, settings.heart]);
  const starBadges = useMemo(
    () => (payload && match ? starRequiredGroupBadges(payload.catalog, match) : new Map<string, string[]>()),
    [payload, match],
  );
  const matching = useMemo(() => (profile?.groups ?? []).filter((group) =>
    `${group.name} ${group.part}`.includes(query)
    && (levelFilter === 'all' || levelFilter === 'unknown' && group.level === null || levelFilter === 'used' && used.has(group.id)))
    .sort((a, b) => {
      const aStar = starBadges.has(a.id) ? 1 : 0;
      const bStar = starBadges.has(b.id) ? 1 : 0;
      if (aStar !== bStar) return bStar - aStar;
      return (groupValue.get(b.id) ?? 0) - (groupValue.get(a.id) ?? 0);
    }), [profile, query, levelFilter, used, groupValue, starBadges]);
  const unknownCount = profile?.groups.filter((group) => group.level === null).length ?? 0;

  function changeSettings(update: (previous: ScoringSettings) => ScoringSettings) {
    setSettings(update); setError('');
  }
  function setLevel(group: string, level: number | null) {
    changeSettings((previous) => {
      const levels = { ...previous.levels };
      if (level === null) delete levels[group]; else levels[group] = level;
      return { ...previous, levels };
    });
  }
  const pendingAutoRunRef = useRef(false);
  function setLevelsBatch(updates: Record<string, number | null>, autoRun = false) {
    if (autoRun) pendingAutoRunRef.current = true;
    changeSettings((previous) => {
      const levels = { ...previous.levels };
      for (const [group, level] of Object.entries(updates)) {
        if (level === null) delete levels[group]; else levels[group] = level;
      }
      return { ...previous, levels };
    });
  }
  useEffect(() => {
    if (!pendingAutoRunRef.current) return;
    pendingAutoRunRef.current = false;
    run();
  }, [settings]);
  function run(overrideExcluded?: string[]) {
    if (!payload || !profile || !match) return;
    const activeExcluded = overrideExcluded ?? excludedIds;
    worker.current?.terminate();
    const task = new Worker(new URL('../services/scoring/calculator.worker.ts', import.meta.url), { type: 'module' });
    worker.current = task; setError(''); setStage('正在准备搭配');
    const key = contextKey;
    const target = matchId;
    task.onmessage = (event: MessageEvent<{ stage?: string; result?: CalculationResult; error?: string }>) => {
      if (worker.current !== task) return;
      if (event.data.stage) setStage(event.data.stage);
      if (event.data.result) setCompleted({ value: event.data.result, key, matchId: target });
      if (event.data.error) setError(event.data.error);
      if (event.data.result || event.data.error) { setStage(''); task.terminate(); worker.current = null; }
    };
    task.onerror = (event) => {
      if (worker.current !== task) return;
      setError(event.message || '计算进程启动失败，请重试。'); setStage(''); task.terminate(); worker.current = null;
    };
    task.postMessage({
      catalog: payload.catalog,
      owned: profile.items,
      settings,
      matchId,
      matchIds: planScope === 'series' && seriesMatchIds.length > 1 ? seriesMatchIds : undefined,
      excludedIds: activeExcluded,
      choice,
      goal,
    });
  }
  function toggleExclude(id: string) {
    setExcludedIds((prev) => {
      const next = prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id];
      setTimeout(() => run(next), 0);
      return next;
    });
  }
  async function copyOutfit() {
    const outfit = result?.[mode];
    if (!outfit || !resultMatch || !payload) return;
    const lines = [`${resultMatch.group} · ${resultMatch.name}`, `${mode === 'current' ? '当前等级' : '焕新潜力'}估分 ${number(outfit.score)}`,
      ...outfit.pieces.map((piece) => `${(payload.catalog.part_names[piece.type] ?? '').split(' / ').pop()}：${piece.item.name}（${levelLabel(piece.level)}）`)];
    try {
      await navigator.clipboard.writeText(lines.join('\n'));
      setCopied(true); setTimeout(() => setCopied(false), 1500);
    } catch (cause) { setError(String(cause)); }
  }

  if (loading) return <div className="flex items-center justify-center gap-2 py-16 text-sm text-text-secondary"><Loader2 className="h-4 w-4 animate-spin" />正在读取账号衣柜与计分资料</div>;
  if (!payload || !profile) return <p className={clsx(card, 'text-sm text-warning')} role="alert">{error || '请先读取衣柜。'}</p>;

  const glowKinds = profile.items.filter((item) => !item.group).length;
  return <div className="space-y-4">
    {/* The same opening strip as the other pages: what the result is based on, and the one thing to fix first. */}
    <StatusBar tone={!payload.snapshot ? 'idle' : payload.snapshot.complete ? 'ok' : 'warn'}
      title={!payload.snapshot ? '还没有衣柜记录' : payload.snapshot.complete
        ? `按 ${new Date(payload.snapshot.updated_at).toLocaleString('zh-CN', { hour12: false })} 的衣柜记录计算` : '衣柜尚未读完，只按已读到的衣服计算'}
      detail={payload.snapshot
        ? <>可用衣服 <strong className="tabular-nums text-text-primary">{profile.aligned}</strong> 件 · 已读祝福闪光 {glowKinds} 种
          {unknownCount > 0 && <> · {unknownCount} 组没填等级，按 0 级计算，可在下方「补充衣服等级」填写</>}</>
        : '读取一次衣柜后，才能按你拥有的衣服算出最高分搭配。'}>
      <button type="button" onClick={onOpenRead} className={secondaryButton}>{!payload.snapshot ? '去读取衣柜' : payload.snapshot.complete ? '更新衣柜' : '继续读取'}</button>
    </StatusBar>
    <WardrobeMatchPicker matches={payload.catalog.matches} value={matchId} onChange={(id) => { setMatchId(id); setChoice(null); }} />
    <div className="grid items-start gap-4 lg:grid-cols-[18rem_minmax(0,1fr)]">
    <aside className="space-y-3 lg:sticky lg:top-0">
      <section className={clsx(card, 'space-y-3')}>
        <label className="block space-y-1.5 text-xs text-text-secondary"><span>计算目标</span>
          <select className={field} value={goal} onChange={(event) => setGoal(event.target.value as typeof goal)}><option value="stars">优先满足星级条件</option><option value="score">最高评分</option></select></label>
        {seriesMatchIds.length > 1 && (
          <label className="block space-y-1.5 text-xs text-text-secondary"><span>规划范围</span>
            <select className={field} value={planScope} onChange={(event) => setPlanScope(event.target.value as typeof planScope)}>
              <option value="single">仅当前选中场次</option>
              <option value="series">同系列整期合并规划（共 {seriesMatchIds.length} 场）</option>
            </select>
          </label>
        )}
        {!!match?.choices.length && <label className="block space-y-1.5 text-xs text-text-secondary"><span>场地加成</span>
          <select className={field} value={choice ?? ''} onChange={(event) => setChoice(event.target.value === '' ? null : Number(event.target.value))}>
            <option value="">自动选择</option>
            {match.choices.map((bonus, index) => <option value={index} key={index}>{bonusLabel(bonus)}</option>)}
          </select></label>}
        {!!ranksIn(match).length && <div className="space-y-1.5">
          <div className="flex items-center justify-between text-xs">
            <span className="text-text-secondary">评价分数门槛 <span className="text-text-muted">（已内置参考线，可覆盖）</span></span>
            {!!Object.keys(settings.rank_thresholds[matchId] ?? {}).length && (
              <button type="button" className="text-[11px] text-accent hover:underline" onClick={() => changeSettings((previous) => {
                const next = { ...previous.rank_thresholds };
                delete next[matchId];
                return { ...previous, rank_thresholds: next };
              })}>恢复默认</button>
            )}
          </div>
          <div className="grid grid-cols-3 gap-2">{ranksIn(match).map((rank) => <label key={rank} className="space-y-1 text-[11px] text-text-muted"><span>{rankName(rank)}</span>
            <input type="number" min="0" className={clsx(field, '!px-2 !py-1.5 !text-xs')} placeholder={String(match?.thresholds?.[rank] ?? '待填')} value={settings.rank_thresholds[matchId]?.[rank] ?? ''} onChange={(event) => {
              const raw = event.target.value;
              const value = Number(raw);
              if (raw !== '' && (!Number.isFinite(value) || value < 0)) return;
              changeSettings((previous) => {
                const thresholds = { ...previous.rank_thresholds[matchId] };
                if (raw === '') delete thresholds[rank]; else thresholds[rank] = value;
                return { ...previous, rank_thresholds: { ...previous.rank_thresholds, [matchId]: thresholds } };
              });
            }} />
          </label>)}</div></div>}
        <button onClick={stage ? () => { worker.current?.terminate(); worker.current = null; setStage(''); } : () => run()} disabled={!profile.items.length || !match}
          className="flex w-full items-center justify-center gap-2 rounded-lg bg-accent px-5 py-2.5 text-sm font-medium text-on-accent hover:bg-accent-hover disabled:opacity-40">
          {stage ? <Square className="h-4 w-4" /> : stale ? <RefreshCw className="h-4 w-4" /> : <Calculator className="h-4 w-4" />}{stage ? '停止计算' : stale ? '重新计算' : planScope === 'series' && seriesMatchIds.length > 1 ? `计算当前及整期 ${seriesMatchIds.length} 场` : '开始计算'}</button>
        {stage && <p className="flex items-center gap-1.5 text-xs text-text-secondary"><Loader2 className="h-3.5 w-3.5 animate-spin" />{stage}</p>}
        {error && <p className="flex items-start gap-1.5 text-xs text-warning" role="alert"><AlertCircle className="h-4 w-4 shrink-0" />{error}</p>}
      </section>
      <section className={clsx(card, 'space-y-2 text-xs text-text-secondary')}>
        {!!profile.skipped.length && <details><summary className="cursor-pointer">{profile.skipped.length} 件待确认身份</summary>
          <div className="mt-2 max-h-32 space-y-1 overflow-auto">{profile.skipped.map((item, index) => <p key={index}>{item.part} · {item.name} · {item.reason}</p>)}</div></details>}
        <p className={clsx('text-[11px] text-text-muted', !!profile.skipped.length && 'border-t border-border pt-2')}>{saving ? '正在保存…' : dirty ? '设置待保存' : '设置已自动保存'} · 资料 {payload.catalog.version} · <a className="hover:underline" href={payload.catalog.source.url} target="_blank" rel="noreferrer" onClick={(event) => {
          if (!isTauri()) return;
          event.preventDefault();
          void openUrl(payload.catalog.source.url).catch((cause: unknown) => setError(String(cause)));
        }}>{payload.catalog.source.name}</a></p>
      </section>
    </aside>

    <div className="min-w-0 space-y-3">
      {match && <MatchBrief match={match} catalog={payload.catalog} />}
      {!!excludedIds.length && (
        <section className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-warning/30 bg-warning/5 px-3.5 py-2.5 text-xs">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="font-medium text-warning">已排除 {excludedIds.length} 件：</span>
            {excludedIds.map((id) => {
              const name = payload.catalog.items[id]?.name ?? id;
              return (
                <button key={id} type="button" onClick={() => toggleExclude(id)}
                  className="inline-flex items-center gap-1 rounded-md border border-border bg-bg-secondary px-2 py-0.5 text-text-primary hover:border-accent"
                  title="点击恢复此部件">
                  {name}<X className="h-3 w-3 text-text-muted" />
                </button>
              );
            })}
          </div>
          <button type="button" onClick={() => { setExcludedIds([]); setTimeout(() => run([]), 0); }}
            className="inline-flex items-center gap-1 text-text-secondary hover:text-text-primary">
            <RotateCcw className="h-3 w-3" />恢复全部
          </button>
        </section>
      )}
      {result && resultMatch ? <>
        {stale && <p className="flex items-center gap-1.5 rounded-lg bg-warning/10 px-3 py-2 text-xs text-warning"><AlertCircle className="h-3.5 w-3.5" />设置已更改，以下是上次的结果{completed!.matchId !== matchId ? `（${resultMatch.name}）` : ''}，重新计算后更新。</p>}
        {result.batch && (
          <WardrobeBatchPlanView
            batch={result.batch}
            catalog={payload.catalog}
            activeMatchId={matchId}
            onSelectStage={(id) => { setMatchId(id); setChoice(null); }}
            accountKey={payload.account_key}
            materials={payload.materials}
            sourceLabel={`${resultMatch.group}（整期 ${result.batch.stages.length} 场）`}
            onMaterialsChange={(next) => setPayload((prev) => prev ? { ...prev, materials: next } : prev)}
          />
        )}
        <div className={clsx('grid grid-cols-2 gap-2', stale && 'opacity-60')} role="tablist">{(['current', 'potential'] as const).map((key) => {
          const outfit = result[key];
          return <button key={key} role="tab" aria-selected={mode === key} onClick={() => setMode(key)}
            className={clsx('rounded-xl border p-3 text-left', mode === key ? 'border-accent bg-accent/10' : 'border-border bg-bg-secondary hover:bg-bg-hover')}>
            <span className="flex items-center gap-1.5 text-xs text-text-secondary">{key === 'potential' && <Sparkles className="h-3.5 w-3.5 text-accent" />}{key === 'current' ? '当前等级' : '全部焕新后'}</span>
            <span className="mt-1.5 flex items-center gap-3"><strong className={clsx('text-2xl font-semibold tabular-nums', mode === key && 'text-accent')}>{outfit ? number(outfit.score) : '—'}</strong>{outfit && <Stars outfit={outfit} match={resultMatch} />}</span>
            {key === 'current' && unknownCount > 0 && <span className="mt-1 block text-xs text-text-muted">未填等级按 0 级计</span>}
            {key === 'potential' && outfit && result.current && <span className="mt-1 block text-xs text-text-muted">比当前等级高 {number(outfit.score - result.current.score)}</span>}
          </button>;
        })}</div>
        <div className="flex items-center justify-between text-xs text-text-muted"><span>计算用时 {result.elapsedMs < 100 ? '不足 0.1' : (result.elapsedMs / 1000).toFixed(1)} 秒 · 点击部件行可展开查看属性或临时排除</span>
          <div className="flex items-center gap-3">
            <button type="button" onClick={() => setShareOpen(true)} disabled={!result[mode]} className="flex items-center gap-1 hover:text-text-primary disabled:opacity-40"><ImageIcon className="h-3.5 w-3.5" />生成分享图</button>
            <button onClick={() => void copyOutfit()} disabled={!result[mode]} className="flex items-center gap-1 hover:text-text-primary disabled:opacity-40">{copied ? <Check className="h-3.5 w-3.5" /> : <ClipboardCopy className="h-3.5 w-3.5" />}{copied ? '已复制' : '复制搭配'}</button>
          </div>
        </div>
        {result[mode] && (
          <WardrobePhotoCardModal
            open={shareOpen}
            onClose={() => setShareOpen(false)}
            outfit={result[mode]!}
            match={resultMatch}
            catalog={payload.catalog}
            modeLabel={mode === 'current' ? '当前等级' : '全部焕新后'}
          />
        )}
        <div className={clsx(stale && 'opacity-60')}><OutfitView outfit={result[mode]} potentialOutfit={result.potential} payload={payload} profile={profile} settings={settings} matchId={completed?.matchId ?? matchId} match={resultMatch} potential={mode === 'potential'} setLevel={setLevel} setLevelsBatch={setLevelsBatch} excludedIds={excludedIds} onToggleExclude={toggleExclude} onMaterialsChange={(next) => setPayload((prev) => prev ? { ...prev, materials: next } : prev)} /></div>
      </> : <div className={clsx(card, 'py-10 text-center text-sm text-text-secondary')}>{profile.items.length ? '选好场次后开始计算，这里会显示推荐搭配。' : '读取衣柜后才能计算搭配。'}</div>}

      <details className={card}><summary className="cursor-pointer text-sm font-medium">补充衣服等级 <span className="ml-1 text-xs font-normal text-text-muted">{profile.groups.length} 组，未填 {unknownCount} 组默认按 0 级计</span></summary>
        <p className="mt-3 text-xs leading-5 text-text-secondary">未填写等级的衣服默认按最低等级（0 级）参与计算；若游戏内已强化过部分衣服，可在此补充实际等级（填 0～10，11 表示已焕新，用 Tab 跳到下一项）。同一养成组的各颜色共享等级，当前关卡星级条件涉及的衣服已优先置顶。</p>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <div className="flex gap-1 rounded-lg bg-bg-primary p-0.5">{([['unknown', '未填'], ['used', '当前搭配'], ['all', '全部']] as const).map(([id, label]) =>
            <button key={id} onClick={() => setLevelFilter(id)} className={clsx('rounded-lg px-2.5 py-1 text-xs', levelFilter === id ? 'bg-accent/10 font-medium text-accent' : 'text-text-secondary hover:text-text-primary')}>{label}</button>)}</div>
          <label className="flex min-w-40 flex-1 items-center gap-2"><Search className="h-4 w-4 shrink-0 text-text-muted" /><input className={clsx(field, '!py-1.5 !text-xs')} placeholder="搜索衣服名称或部位" aria-label="搜索养成组" value={query} onChange={(event) => setQuery(event.target.value)} /></label>
        </div>
        <div className="mt-3 divide-y divide-border">{matching.slice(0, levelLimit).map((group) => {
          const badges = starBadges.get(group.id);
          return <div key={group.id} className="flex items-center gap-3 py-2">
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-1.5">
                <p className="truncate text-sm">{group.name}</p>
                {badges?.map((b) => (
                  <span key={b} className="rounded bg-warning/15 px-1.5 py-0.2 text-[10px] font-medium text-warning">
                    {b}
                  </span>
                ))}
              </div>
              <p className="mt-0.5 truncate text-xs text-text-muted">{group.part} · 已有 {group.owned} 色{group.inferred ? ` · 焕新赠送款：${group.inferred}` : ''}</p>
            </div>
            <span className="shrink-0 text-xs tabular-nums text-text-muted">{number(groupValue.get(group.id) ?? 0)}</span>
            <LevelInput value={settings.levels[group.id] ?? null} inferred={group.inferred} onChange={(level) => setLevel(group.id, level)} />
          </div>;
        })}
        {!matching.length && <p className="py-4 text-center text-xs text-text-muted">{levelFilter === 'used' && !result ? '计算后可只看当前搭配用到的衣服。' : '没有符合条件的衣服。'}</p>}</div>
        {matching.length > levelLimit && <button onClick={() => setLevelLimit((previous) => previous + LEVEL_PAGE)} className="mt-2 w-full rounded-lg border border-border py-1.5 text-xs hover:bg-bg-hover">再显示 {Math.min(LEVEL_PAGE, matching.length - levelLimit)} 组（共 {matching.length} 组）</button>}
      </details>

      <WardrobeHeartEditor
        heart={settings.heart}
        onChange={(updater) => changeSettings((previous) => ({ ...previous, heart: updater(previous.heart) }))}
      />
    </div>
    </div>
  </div>;
}
