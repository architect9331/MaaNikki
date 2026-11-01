import { useEffect, useMemo, useRef, useState } from 'react';
import { AlertCircle, Calculator, Check, Loader2, Save, Search, Sparkles, Square, Star } from 'lucide-react';
import clsx from 'clsx';
import { openUrl } from '@tauri-apps/plugin-opener';
import { useAccountStateStore } from '@/stores/accountStateStore';
import { isTauri } from '@/utils/paths';
import { loadScoring, saveScoring } from '@/services/scoring/api';
import { buildOwnedProfile } from '@/services/scoring/profile';
import { ATTRIBUTES, HEART_GROUPS, emptySettings } from '@/services/scoring/types';
import type { CalculationResult, FieldBonus, Match, OutfitResult, ScoringPayload, ScoringSettings } from '@/services/scoring/types';

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

function LevelSelect({ value, onChange, inferred }: { value: number | null; onChange: (level: number | null) => void; inferred?: string | null }) {
  return <select aria-label="共享养成等级" disabled={!!inferred} className={clsx(field, 'max-w-36 !py-1.5 !text-xs disabled:text-text-muted')} value={inferred ? '' : value ?? ''}
    onChange={(event) => onChange(event.target.value === '' ? null : Number(event.target.value))}>
    <option value="">{inferred ? '自动：已焕新' : '等级未知'}</option>
    {Array.from({ length: 12 }, (_, level) => <option key={level} value={level}>{levelLabel(level)}</option>)}
  </select>;
}

function NumberField({ label, value, onChange }: { label: string; value: number; onChange: (value: number) => void }) {
  return <label className="space-y-1.5 text-xs text-text-secondary"><span>{label}</span>
    <input className={field} type="number" min="0" step="any" value={value} onChange={(event) => {
      const next = Number(event.target.value);
      if (Number.isFinite(next) && next >= 0) onChange(next);
    }} />
  </label>;
}

function OutfitView({ outfit, payload, settings, match, potential, setLevel }: {
  outfit: OutfitResult | null; payload: ScoringPayload; settings: ScoringSettings; match: Match; potential: boolean;
  setLevel: (group: string, level: number | null) => void;
}) {
  if (!outfit) return <div className={clsx(card, 'py-9 text-center text-sm text-text-secondary')}>
    {potential ? '暂未找到可用的完整搭配，请检查衣柜对齐情况。' : '已知等级的衣服暂未组成完整搭配，可补充等级或查看焕新潜力。'}
  </div>;
  const { catalog } = payload;
  return <div className="space-y-3">
    <section className={clsx(card, 'flex flex-wrap items-center gap-5')}>
      <div className="mr-auto"><p className="text-xs text-text-secondary">{potential ? '现有衣服全部焕新后的估分' : '已知等级衣服的估分'}</p>
        <p className="mt-2 text-3xl font-semibold tabular-nums text-accent">{number(outfit.score)}</p></div>
      {!!outfit.stars.length && <div className="flex gap-2" aria-label="星级条件结果">{outfit.stars.map((passed, index) =>
        <span key={index} title={`${starLabel(match, index)}：${passed === null ? '待填写分数门槛' : passed ? '满足' : '未满足'}`} className="text-center">
          <Star className={clsx('h-5 w-5', passed === true ? 'fill-accent text-accent' : 'text-text-muted')} />
          {passed === null && <span className="text-xs text-text-muted">待定</span>}
        </span>)}</div>}
      {!outfit.exact && <p className="w-full text-xs text-text-secondary">搜索时间已到，展示当前找到的可行方案。</p>}
      {outfit.choice !== null && <p className="w-full text-xs text-text-secondary">场地加成：{bonusLabel(match.choices[outfit.choice])}</p>}
    </section>
    <section className="overflow-hidden rounded-xl border border-border bg-bg-secondary">
      {outfit.pieces.map((piece) => <details key={`${piece.item.id}:${piece.item.group ?? ''}`} className="group border-b border-border last:border-0">
        <summary className="flex cursor-pointer list-none items-center gap-3 px-4 py-3 hover:bg-bg-hover">
          <span className="w-12 shrink-0 text-xs text-text-muted">{catalog.part_names[piece.type] ?? piece.type}</span>
          <div className="min-w-0 flex-1"><p className="truncate text-sm">{piece.item.name}</p>
            <p className="mt-1 text-xs text-text-muted">{potential && piece.from !== piece.level ? `${levelLabel(piece.from)} → ${levelLabel(piece.level)}` : levelLabel(piece.level)}</p></div>
          <strong className="text-sm font-semibold tabular-nums">{number(piece.score)}</strong>
        </summary>
        <div className="space-y-3 border-t border-border bg-bg-primary/50 px-4 py-3 text-xs">
          {piece.item.group && <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-text-secondary">当前共享等级{piece.item.evidence && ` · ${piece.item.evidence}`}</span>
            <LevelSelect value={settings.levels[piece.item.group] ?? null} inferred={piece.item.refreshReward}
              onChange={(level) => setLevel(piece.item.group!, level)} /></div>}
          <div className="grid grid-cols-5 gap-2">{ATTRIBUTES.map((name, index) => <div key={name}>
            <p className="text-text-muted">{name}</p><p className="mt-1 tabular-nums">{number(piece.attributeScores[index])}</p>
            <p className="mt-1 text-text-muted">属性 {number(piece.grownAttributes[index])}</p>
          </div>)}</div>
          <p className="leading-5 text-text-secondary">标签分 {number(piece.tagScore)} · 最终倍率 ×{number(piece.multiplier)} · 单件向上取整</p>
        </div>
      </details>)}
      {outfit.globalScore !== 0 && <p className="px-4 py-3 text-xs text-text-secondary">全局固定加分 {number(outfit.globalScore)}</p>}
    </section>
    {potential && <section className={card}><h3 className="text-sm font-medium">养成材料差额</h3>
      <div className="mt-3 flex flex-wrap gap-x-5 gap-y-2 text-xs">{Object.entries(outfit.cost).map(([id, count]) =>
        <span key={id}>{catalog.material_names[id] ?? id} <strong className="ml-1 tabular-nums">{number(count)}</strong></span>)}
        {!Object.keys(outfit.cost).length && <span className="text-text-secondary">已知等级的部件暂无材料差额</span>}</div>
      {!!outfit.unknownCostGroups.length && <p className="mt-3 text-xs text-text-secondary">另有 {outfit.unknownCostGroups.length} 个部件等级未知，补充等级后可计入材料差额。</p>}
    </section>}
  </div>;
}

export function WardrobeCalculator({ refreshToken }: { refreshToken: number }) {
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
  const [query, setQuery] = useState('');
  const [completed, setCompleted] = useState<{ value: CalculationResult; key: object } | null>(null);
  const [mode, setMode] = useState<'current' | 'potential'>('potential');
  const [stage, setStage] = useState('');
  const worker = useRef<Worker | null>(null);
  const draftRef = useRef({ settings, saved });
  const payloadRef = useRef(payload);
  const contextKey = useMemo(() => ({}), [payload, settings, matchId, choice, goal]);
  const result = completed?.key === contextKey ? completed.value : null;
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
      if (previousKey !== next.account_key) setChoice(null);
    }).catch((cause: unknown) => { if (!cancelled) setError(String(cause)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [activeUid, refreshToken]);

  useEffect(() => {
    worker.current?.terminate(); worker.current = null;
    setStage(''); setCompleted(null);
  }, [settings, payload, matchId, choice, goal]);
  useEffect(() => () => { worker.current?.terminate(); }, []);

  const profile = useMemo(() => payload ? buildOwnedProfile(payload, settings) : null, [payload, settings]);
  const groups = useMemo(() => profile?.groups.filter((group) => `${group.name} ${group.part}`.includes(query)).slice(0, 60) ?? [], [profile, query]);
  const matchGroups = useMemo(() => {
    const grouped = new Map<string, [string, Match][]>();
    for (const entry of Object.entries(payload?.catalog.matches ?? {})) {
      grouped.set(entry[1].group, [...grouped.get(entry[1].group) ?? [], entry]);
    }
    return [...grouped];
  }, [payload]);
  const match = payload?.catalog.matches[matchId];
  const dirty = JSON.stringify(settings) !== saved;

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
  async function save() {
    if (!payload) return;
    const key = payload.account_key;
    const draft = settings;
    setSaving(true); setError('');
    try {
      await saveScoring(key, draft);
      if (payloadRef.current?.account_key === key) setSaved(JSON.stringify(draft));
    } catch (cause) { if (payloadRef.current?.account_key === key) setError(String(cause)); }
    finally { setSaving(false); }
  }
  function run() {
    if (!payload || !profile || !match) return;
    worker.current?.terminate();
    const task = new Worker(new URL('../services/scoring/calculator.worker.ts', import.meta.url), { type: 'module' });
    worker.current = task; setError(''); setCompleted(null); setStage('正在准备搭配');
    task.onmessage = (event: MessageEvent<{ stage?: string; result?: CalculationResult; error?: string }>) => {
      if (worker.current !== task) return;
      if (event.data.stage) setStage(event.data.stage);
      if (event.data.result) setCompleted({ value: event.data.result, key: contextKey });
      if (event.data.error) setError(event.data.error);
      if (event.data.result || event.data.error) { setStage(''); task.terminate(); worker.current = null; }
    };
    task.onerror = (event) => {
      if (worker.current !== task) return;
      setError(event.message || '计算进程启动失败，请重试。'); setStage(''); task.terminate(); worker.current = null;
    };
    task.postMessage({ catalog: payload.catalog, owned: profile.items, settings, matchId, choice, goal });
  }

  if (loading) return <div className="flex items-center justify-center gap-2 py-16 text-sm text-text-secondary"><Loader2 className="h-4 w-4 animate-spin" />正在读取账号衣柜与计分资料</div>;
  if (!payload || !profile) return <p className={clsx(card, 'text-sm text-warning')} role="alert">{error || '请先读取衣柜。'}</p>;

  return <div className="space-y-4">
    <section className={card}>
      <div className="flex flex-wrap items-center gap-3"><div className="mr-auto"><h2 className="text-sm font-semibold">搭配计算</h2>
        <p className="mt-1 text-xs text-text-secondary">UID {payload.uid} · 资料 {payload.catalog.version} · <a className="text-accent hover:underline" href={payload.catalog.source.url} target="_blank" rel="noreferrer" onClick={(event) => {
          if (!isTauri()) return;
          event.preventDefault();
          void openUrl(payload.catalog.source.url).catch((cause: unknown) => setError(String(cause)));
        }}>{payload.catalog.source.name}</a></p></div>
        <button onClick={() => void save()} disabled={saving || !dirty} className="flex items-center gap-1.5 rounded-lg border border-border px-3 py-2 text-xs hover:bg-bg-hover disabled:opacity-40">
          {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : dirty ? <Save className="h-3.5 w-3.5" /> : <Check className="h-3.5 w-3.5" />}{dirty ? '保存账号设置' : '设置已保存'}</button>
      </div>
      <div className="mt-4 grid grid-cols-3 gap-3 text-xs text-text-secondary">
        <p>已对齐 <strong className="text-text-primary">{profile.aligned}</strong> 件</p>
        <p>等级未知 <strong className="text-text-primary">{profile.groups.filter((group) => group.level === null).length}</strong> 组</p>
        <p>已读锁定闪光 <strong className="text-text-primary">{profile.items.filter((item) => !item.group).length}</strong> 种</p>
      </div>
      {payload.snapshot && !payload.snapshot.complete && <p className="mt-3 text-xs text-text-secondary">当前使用已读取的部分衣柜，可继续读取后刷新资料。</p>}
      {!!profile.skipped.length && <details className="mt-3 text-xs text-text-secondary"><summary className="cursor-pointer">{profile.skipped.length} 件待对齐或确认身份</summary>
        <div className="mt-2 max-h-40 space-y-1.5 overflow-auto">{profile.skipped.map((item, index) => <p key={index}>{item.part} · {item.name} · {item.reason}</p>)}</div>
      </details>}
    </section>

    <section className={clsx(card, 'space-y-4')}>
      <label className="block space-y-2 text-xs text-text-secondary"><span>计分关卡</span>
        <select className={field} value={matchId} onChange={(event) => { setMatchId(event.target.value); setChoice(null); }}>
          {matchGroups.map(([name, matches]) => <optgroup label={name} key={name}>{matches.map(([id, entry]) => <option value={id} key={id}>{entry.name}</option>)}</optgroup>)}
        </select>
      </label>
      <div className="grid grid-cols-2 gap-3"><label className="space-y-2 text-xs text-text-secondary"><span>计算目标</span>
        <select className={field} value={goal} onChange={(event) => setGoal(event.target.value as typeof goal)}><option value="stars">优先满足星级条件</option><option value="score">最高评分</option></select></label>
        <label className="space-y-2 text-xs text-text-secondary"><span>场地加成</span>
          <select className={field} value={choice ?? ''} onChange={(event) => setChoice(event.target.value === '' ? null : Number(event.target.value))}>
            <option value="">{match?.choices.length ? '自动选择' : '无可选加成'}</option>
            {match?.choices.map((bonus, index) => <option value={index} key={index}>{bonusLabel(bonus)}</option>)}
          </select></label>
      </div>
      {!!match?.bonuses.length && <p className="text-xs text-text-secondary">固定加成：{match.bonuses.map(bonusLabel).join('；')}</p>}
      {!!match?.stars.length && <div className="space-y-2 text-xs text-text-secondary">{match.stars.map((_, index) => <p key={index}>★ {starLabel(match, index)}</p>)}</div>}
      {!!ranksIn(match).length && <div><p className="mb-2 text-xs text-text-secondary">填写游戏显示的评价门槛，用于核验分数条件；留空时该条件标为待定。</p>
        <div className="grid grid-cols-3 gap-3">{ranksIn(match).map((rank) => <label key={rank} className="space-y-1.5 text-xs text-text-secondary"><span>{rankName(rank)}所需分数</span>
          <input type="number" min="0" className={field} placeholder="待填写" value={settings.rank_thresholds[matchId]?.[rank] ?? ''} onChange={(event) => {
            const raw = event.target.value;
            const value = Number(raw);
            if (raw !== '' && (!Number.isFinite(value) || value < 0)) return;
            changeSettings((previous) => {
              const thresholds = { ...previous.rank_thresholds[matchId] };
              if (raw === '') delete thresholds[rank]; else thresholds[rank] = value;
              return { ...previous, rank_thresholds: { ...previous.rank_thresholds, [matchId]: thresholds } };
            });
          }} />
        </label>)}</div>
      </div>}
    </section>

    <details className={card}><summary className="cursor-pointer text-sm font-medium">无限之心加成</summary>
      <p className="mt-3 text-xs leading-5 text-text-secondary">填入养成后的合计加成，百分比填写数值。属性与部位加成按关卡区域生效，展示加成单独计算。</p>
      <p className="mb-2 mt-4 text-xs font-medium">固定属性</p>
      <div className="grid grid-cols-5 gap-2">{ATTRIBUTES.map((name, index) => <NumberField key={name} label={name} value={settings.heart.flat[index]}
        onChange={(value) => changeSettings((previous) => ({ ...previous, heart: { ...previous.heart, flat: previous.heart.flat.map((old, i) => i === index ? value : old) } }))} />)}</div>
      {(['stat', 'part'] as const).map((kind) => <div key={kind}><p className="mb-2 mt-4 text-xs font-medium">{kind === 'stat' ? '属性加成 %（含通用节点）' : '部位加成 %'}</p>
        <div className="grid grid-cols-3 gap-2 sm:grid-cols-6">{HEART_GROUPS.map((name, index) => <NumberField key={name} label={name} value={settings.heart[kind][index]}
          onChange={(value) => changeSettings((previous) => ({ ...previous, heart: { ...previous.heart, [kind]: previous.heart[kind].map((old, i) => i === index ? value : old) } }))} />)}</div>
      </div>)}
      <div className="mt-4 max-w-40"><NumberField label="展示加成 %" value={settings.heart.display}
        onChange={(display) => changeSettings((previous) => ({ ...previous, heart: { ...previous.heart, display } }))} /></div>
    </details>

    <details className={card}><summary className="cursor-pointer text-sm font-medium">衣服养成 · {profile.groups.length} 组</summary>
      <p className="mt-3 text-xs leading-5 text-text-secondary">同一养成组的各颜色共享等级。已持有焕新赠送款的组会自动记为已焕新，其余可手动补充。</p>
      <label className="mt-3 flex items-center gap-2"><Search className="h-4 w-4 text-text-muted" /><input className={field} placeholder="搜索衣服名称或部位" aria-label="搜索养成组" value={query} onChange={(event) => setQuery(event.target.value)} /></label>
      <div className="mt-3 max-h-80 divide-y divide-border overflow-auto">{groups.map((group) => <div key={group.id} className="flex items-center gap-3 py-2.5">
        <div className="min-w-0 flex-1"><p className="truncate text-sm">{group.name}</p><p className="mt-1 text-xs text-text-muted">{group.part} · 已有 {group.owned} 色 · {group.inferred ? `焕新赠送款：${group.inferred}` : levelLabel(group.level)}</p></div>
        <LevelSelect value={settings.levels[group.id] ?? null} inferred={group.inferred} onChange={(level) => setLevel(group.id, level)} />
      </div>)}</div>
      {groups.length === 60 && <p className="mt-2 text-xs text-text-muted">展示前 60 组，可输入名称缩小范围。</p>}
    </details>

    {error && <p className="flex items-start gap-2 text-xs text-warning" role="alert"><AlertCircle className="h-4 w-4 shrink-0" />{error}</p>}
    <div className="flex items-center gap-3"><button onClick={stage ? () => { worker.current?.terminate(); worker.current = null; setStage(''); } : run}
      disabled={!profile.items.length} className="flex items-center gap-2 rounded-lg bg-accent px-5 py-2.5 text-sm font-medium text-white hover:bg-accent-hover disabled:opacity-40">
      {stage ? <Square className="h-4 w-4" /> : <Calculator className="h-4 w-4" />}{stage ? '停止计算' : '计算搭配'}</button>
      <p className="text-xs text-text-secondary">{stage || (result ? `计算完成 · ${(result.elapsedMs / 1000).toFixed(1)} 秒` : '按已有衣服计算，祝福闪光采用已读等级。')}</p>
    </div>
    {result && match && <><div className="flex items-center gap-1 rounded-lg bg-bg-secondary p-1">{(['current', 'potential'] as const).map((key) => <button key={key}
      onClick={() => setMode(key)} className={clsx('flex flex-1 items-center justify-center gap-2 rounded-md px-3 py-2 text-sm', mode === key ? 'bg-bg-primary text-accent shadow-sm' : 'text-text-secondary hover:text-text-primary')}>
      {key === 'potential' && <Sparkles className="h-4 w-4" />}{key === 'current' ? '已知等级' : '焕新潜力'}</button>)}</div>
      <OutfitView outfit={result[mode]} payload={payload} settings={settings} match={match} potential={mode === 'potential'} setLevel={setLevel} />
    </>}
  </div>;
}
