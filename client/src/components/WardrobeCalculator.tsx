import { useEffect, useMemo, useRef, useState } from 'react';
import { AlertCircle, ArrowUpRight, Ban, Calculator, Check, ClipboardCopy, Loader2, RefreshCw, RotateCcw, Search, Sparkles, Square, Star, X } from 'lucide-react';
import clsx from 'clsx';
import { openUrl } from '@tauri-apps/plugin-opener';
import { useAccountStateStore } from '@/stores/accountStateStore';
import { isTauri } from '@/utils/paths';
import { loadScoring, saveScoring } from '@/services/scoring/api';
import { scorePiece } from '@/services/scoring/math';
import { buildOwnedProfile } from '@/services/scoring/profile';
import { ATTRIBUTES, emptySettings } from '@/services/scoring/types';
import type { CalculationResult, FieldBonus, Match, OutfitResult, ScoringPayload, ScoringSettings } from '@/services/scoring/types';
import { WardrobeHeartEditor } from './WardrobeHeartEditor';
import { WardrobeMatchPicker } from './WardrobeMatchPicker';
import { WardrobeBatchPlanView, WardrobeMaterialList } from './WardrobeMaterialActions';
import { ImageIcon, WardrobePhotoCardModal } from './WardrobePhotoCardModal';

const field = 'w-full rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm outline-none focus:border-accent';
const card = 'rounded-xl border border-border bg-bg-secondary p-4';
const number = (value: number) => value.toLocaleString('zh-CN', { maximumFractionDigits: 2 });
const levelLabel = (level: number | null) => level === null ? '等级未知' : level === 11 ? '已焕新' : `${level} 级`;
const bonusLabel = (bonus: FieldBonus) => bonus.description.replaceAll('{1}', String(bonus.value ?? bonus.multiplier));
const rankName = (rank: number) => ({ 2: '一般', 4: '优秀', 5: '完美' }[rank] ?? `评价 ${rank}`);
const starLabel = (match: Match, index: number) => match.stars[index].description || match.stars[index].groups.map((group) =>
  group.map((rule) => rule.kind === 'rank' ? `获得${rankName(rule.rank)}评价` : rule.kind === 'suit' ? '满足套装要求' : `穿戴 ${rule.count} 件符合要求的部件`).join('，')).join('；或');
const ranksIn = (match: Match | undefined) => [...new Set(match?.stars.flatMap((star) =>
  star.groups.flatMap((group) => group.flatMap((rule) => rule.kind === 'rank' ? [rule.rank] : []))) ?? [])].sort((a, b) => a - b);
const LEVEL_PAGE = 60;

/** Type 0-10, or 11 for refreshed; clearing the field marks the level unknown. */
function LevelInput({ value, onChange, inferred }: { value: number | null; onChange: (level: number | null) => void; inferred?: string | null }) {
  if (inferred) return <span className="shrink-0 rounded-lg bg-accent/10 px-2 py-1 text-xs text-accent">已焕新</span>;
  return <input aria-label="共享养成等级，0 到 10，11 表示已焕新" type="number" min="0" max="11" placeholder="未知" value={value ?? ''}
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

function MatchBrief({ match }: { match: Match }) {
  const strongest = Math.max(...match.weights);
  return <section className={card}>
    <p className="text-xs text-text-muted">{match.group}</p>
    <h3 className="mt-1 text-base font-semibold">{match.name}</h3>
    <div className="mt-3 grid grid-cols-5 gap-2">{ATTRIBUTES.map((name, index) => <div key={name}>
      <p className={clsx('text-xs', match.weights[index] === strongest ? 'font-semibold text-accent' : 'text-text-muted')}>{name}</p>
      <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-border"><div className={clsx('h-full rounded-full', match.weights[index] === strongest ? 'bg-accent' : 'bg-text-muted/40')}
        style={{ width: `${match.weights[index] / strongest * 100}%` }} /></div>
      <p className="mt-1 text-[11px] tabular-nums text-text-muted">×{number(match.weights[index])}</p>
    </div>)}</div>
    {!!match.bonuses.length && <div className="mt-3 flex flex-wrap gap-1.5">{match.bonuses.map((bonus, index) =>
      <span key={index} className="rounded-lg bg-accent/10 px-2 py-0.5 text-xs text-accent">{bonusLabel(bonus)}</span>)}</div>}
    {!!match.stars.length && <ul className="mt-3 space-y-1 text-xs text-text-secondary">{match.stars.map((_, index) =>
      <li key={index} className="flex gap-1.5"><Star className="mt-0.5 h-3 w-3 shrink-0 text-text-muted" />{starLabel(match, index)}</li>)}</ul>}
  </section>;
}

function OutfitView({ outfit, payload, settings, match, potential, setLevel, excludedIds, onToggleExclude }: {
  outfit: OutfitResult | null; payload: ScoringPayload; settings: ScoringSettings; match: Match; potential: boolean;
  setLevel: (group: string, level: number | null) => void;
  excludedIds: string[];
  onToggleExclude: (id: string) => void;
}) {
  if (!outfit) return <div className={clsx(card, 'py-9 text-center text-sm text-text-secondary')}>
    {potential ? '暂未找到可用的完整搭配，请检查衣柜对齐情况或恢复已排除部件。' : '已知等级的衣服暂未组成完整搭配，可在下方补充等级或查看焕新潜力。'}
  </div>;
  const { catalog } = payload;
  const top = Math.max(...outfit.pieces.map((piece) => piece.score), 1);
  const upgrades = potential ? outfit.pieces.filter((piece) => piece.gain).sort((a, b) => (b.gain ?? 0) - (a.gain ?? 0)) : [];
  return <div className="space-y-3">
    <section className="overflow-hidden rounded-xl border border-border bg-bg-secondary">
      {!outfit.exact && <p className="border-b border-border px-4 py-2 text-xs text-text-secondary">搜索时间已到，展示当前找到的可行方案。</p>}
      {outfit.choice !== null && <p className="border-b border-border px-4 py-2 text-xs text-text-secondary">场地加成：{bonusLabel(match.choices[outfit.choice])}</p>}
      {outfit.pieces.map((piece) => {
        const isExcluded = excludedIds.includes(piece.item.id);
        return <details key={`${piece.item.id}:${piece.item.group ?? ''}`} className="group border-b border-border last:border-0">
          <summary className="flex cursor-pointer list-none items-center gap-3 px-4 py-2.5 hover:bg-bg-hover">
            <span className="w-16 shrink-0 truncate text-xs text-text-muted">{(catalog.part_names[piece.type] ?? String(piece.type)).split(' / ').pop()}</span>
            <div className="min-w-0 flex-1"><p className={clsx('truncate text-sm', isExcluded && 'line-through opacity-60')}>{piece.item.name}</p>
              <div className="mt-1 flex items-center gap-2"><div className="h-1 flex-1 overflow-hidden rounded-full bg-border"><div className="h-full rounded-full bg-accent/60" style={{ width: `${piece.score / top * 100}%` }} /></div>
                <span className="shrink-0 text-[11px] text-text-muted">{potential && piece.from !== piece.level ? `${levelLabel(piece.from)} → ${levelLabel(piece.level)}` : levelLabel(piece.level)}</span></div></div>
            <strong className="w-16 text-right text-sm font-semibold tabular-nums">{number(piece.score)}</strong>
          </summary>
          <div className="space-y-3 border-t border-border bg-bg-primary/50 px-4 py-3 text-xs">
            <div className="flex flex-wrap items-center justify-between gap-2">
              {piece.item.group ? (
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-text-secondary">当前共享等级{piece.item.evidence && ` · ${piece.item.evidence}`}</span>
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
        : <p className="mt-2 text-xs text-text-secondary">已知等级的部件都已到位。</p>}
      <h3 className="mt-4 text-sm font-medium">养成材料差额</h3>
      <WardrobeMaterialList
        cost={outfit.cost}
        catalog={catalog}
        accountKey={payload.account_key}
        materials={payload.materials}
        sourceLabel={`${match.group} · ${match.name}`}
      />
      {!!outfit.unknownCostGroups.length && <p className="mt-3 text-xs text-text-secondary">另有 {outfit.unknownCostGroups.length} 个部件等级未知，补充后可计入提升值和材料差额。</p>}
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
  const [mode, setMode] = useState<'current' | 'potential'>('potential');
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
  const matching = useMemo(() => (profile?.groups ?? []).filter((group) =>
    `${group.name} ${group.part}`.includes(query)
    && (levelFilter === 'all' || levelFilter === 'unknown' && group.level === null || levelFilter === 'used' && used.has(group.id)))
    .sort((a, b) => (groupValue.get(b.id) ?? 0) - (groupValue.get(a.id) ?? 0)), [profile, query, levelFilter, used, groupValue]);
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
    const lines = [`${resultMatch.group} · ${resultMatch.name}`, `${mode === 'current' ? '已知等级' : '焕新潜力'}估分 ${number(outfit.score)}`,
      ...outfit.pieces.map((piece) => `${(payload.catalog.part_names[piece.type] ?? '').split(' / ').pop()}：${piece.item.name}（${levelLabel(piece.level)}）`)];
    try {
      await navigator.clipboard.writeText(lines.join('\n'));
      setCopied(true); setTimeout(() => setCopied(false), 1500);
    } catch (cause) { setError(String(cause)); }
  }

  if (loading) return <div className="flex items-center justify-center gap-2 py-16 text-sm text-text-secondary"><Loader2 className="h-4 w-4 animate-spin" />正在读取账号衣柜与计分资料</div>;
  if (!payload || !profile) return <p className={clsx(card, 'text-sm text-warning')} role="alert">{error || '请先读取衣柜。'}</p>;

  return <div className="space-y-4">
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
        {!!ranksIn(match).length && <div className="space-y-1.5"><p className="text-xs text-text-secondary">评价分数门槛 <span className="text-text-muted">（照游戏填写，留空时星级待定）</span></p>
          <div className="grid grid-cols-3 gap-2">{ranksIn(match).map((rank) => <label key={rank} className="space-y-1 text-[11px] text-text-muted"><span>{rankName(rank)}</span>
            <input type="number" min="0" className={clsx(field, '!px-2 !py-1.5 !text-xs')} placeholder="待填" value={settings.rank_thresholds[matchId]?.[rank] ?? ''} onChange={(event) => {
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
        <div className="flex justify-between"><span>可用衣服</span><strong className="text-text-primary tabular-nums">{profile.aligned} 件</strong></div>
        <div className="flex justify-between"><span>等级未知</span><strong className={clsx('tabular-nums', unknownCount ? 'text-warning' : 'text-text-primary')}>{unknownCount} 组</strong></div>
        <div className="flex justify-between"><span>已读祝福闪光</span><strong className="text-text-primary tabular-nums">{profile.items.filter((item) => !item.group).length} 种</strong></div>
        {(!payload.snapshot || !payload.snapshot.complete) && <button onClick={onOpenRead} className="text-accent hover:underline">{payload.snapshot ? '衣柜未读完，继续读取' : '还没有衣柜记录，去读取'}</button>}
        {!!profile.skipped.length && <details><summary className="cursor-pointer">{profile.skipped.length} 件待确认身份</summary>
          <div className="mt-2 max-h-32 space-y-1 overflow-auto">{profile.skipped.map((item, index) => <p key={index}>{item.part} · {item.name} · {item.reason}</p>)}</div></details>}
        <p className="border-t border-border pt-2 text-[11px] text-text-muted">{saving ? '正在保存…' : dirty ? '设置待保存' : '设置已自动保存'} · 资料 {payload.catalog.version} · <a className="hover:underline" href={payload.catalog.source.url} target="_blank" rel="noreferrer" onClick={(event) => {
          if (!isTauri()) return;
          event.preventDefault();
          void openUrl(payload.catalog.source.url).catch((cause: unknown) => setError(String(cause)));
        }}>{payload.catalog.source.name}</a></p>
      </section>
    </aside>

    <div className="min-w-0 space-y-3">
      {match && <MatchBrief match={match} />}
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
            <span className="flex items-center gap-1.5 text-xs text-text-secondary">{key === 'potential' && <Sparkles className="h-3.5 w-3.5 text-accent" />}{key === 'current' ? '已知等级' : '全部焕新后'}</span>
            <span className="mt-1.5 flex items-center gap-3"><strong className={clsx('text-2xl font-semibold tabular-nums', mode === key && 'text-accent')}>{outfit ? number(outfit.score) : '—'}</strong>{outfit && <Stars outfit={outfit} match={resultMatch} />}</span>
            {key === 'potential' && outfit && result.current && <span className="mt-1 block text-xs text-text-muted">比已知等级高 {number(outfit.score - result.current.score)}</span>}
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
            modeLabel={mode === 'current' ? '已知等级' : '全部焕新后'}
          />
        )}
        <div className={clsx(stale && 'opacity-60')}><OutfitView outfit={result[mode]} payload={payload} settings={settings} match={resultMatch} potential={mode === 'potential'} setLevel={setLevel} excludedIds={excludedIds} onToggleExclude={toggleExclude} /></div>
      </> : <div className={clsx(card, 'py-10 text-center text-sm text-text-secondary')}>{profile.items.length ? '选好场次后开始计算，这里会显示推荐搭配。' : '读取衣柜后才能计算搭配。'}</div>}

      <details className={card} open={unknownCount > 0 && !result}><summary className="cursor-pointer text-sm font-medium">补充衣服等级 <span className="ml-1 text-xs font-normal text-text-muted">{profile.groups.length} 组，{unknownCount} 组未知</span></summary>
        <p className="mt-3 text-xs leading-5 text-text-secondary">同一养成组的各颜色共享等级。按当前关卡的焕新分数从高到低排列，从上往下填最划算；填 0～10，11 表示已焕新，用 Tab 跳到下一项。</p>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <div className="flex gap-1 rounded-lg bg-bg-primary p-0.5">{([['unknown', '未知'], ['used', '当前搭配'], ['all', '全部']] as const).map(([id, label]) =>
            <button key={id} onClick={() => setLevelFilter(id)} className={clsx('rounded-lg px-2.5 py-1 text-xs', levelFilter === id ? 'bg-accent/10 font-medium text-accent' : 'text-text-secondary hover:text-text-primary')}>{label}</button>)}</div>
          <label className="flex min-w-40 flex-1 items-center gap-2"><Search className="h-4 w-4 shrink-0 text-text-muted" /><input className={clsx(field, '!py-1.5 !text-xs')} placeholder="搜索衣服名称或部位" aria-label="搜索养成组" value={query} onChange={(event) => setQuery(event.target.value)} /></label>
        </div>
        <div className="mt-3 divide-y divide-border">{matching.slice(0, levelLimit).map((group) => <div key={group.id} className="flex items-center gap-3 py-2">
          <div className="min-w-0 flex-1"><p className="truncate text-sm">{group.name}</p><p className="mt-0.5 truncate text-xs text-text-muted">{group.part} · 已有 {group.owned} 色{group.inferred ? ` · 焕新赠送款：${group.inferred}` : ''}</p></div>
          <span className="shrink-0 text-xs tabular-nums text-text-muted">{number(groupValue.get(group.id) ?? 0)}</span>
          <LevelInput value={settings.levels[group.id] ?? null} inferred={group.inferred} onChange={(level) => setLevel(group.id, level)} />
        </div>)}
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
