import { useEffect, useMemo, useRef, useState } from 'react';
import { Calculator, Check, Plus, RotateCcw, Sparkles, X } from 'lucide-react';
import clsx from 'clsx';
import { toast } from 'sonner';
import { useAppStore } from '@/stores/appStore';
import { useAccountStateStore } from '@/stores/accountStateStore';
import { loadScoring, saveMaterialPlan } from '@/services/scoring/api';
import type { MaterialsState } from '@/services/scoring/types';

export interface GlowMaterialDef {
  id: string;
  name: string;
  shortName: string;
  group: 'basic' | 'vortex' | 'chroma';
  stage: string;
  drop: number;
  energy: number;
  quickSteps: [number, string][];
}

export const GLOW_MATERIAL_DEFS: GlowMaterialDef[] = [
  { id: '50361309', name: '闪亮泡泡', shortName: '闪亮泡泡', group: 'basic', stage: '素材激化 · 闪亮泡泡', drop: 80, energy: 10, quickSteps: [[800, '+800'], [4000, '+4000']] },
  { id: '3448636403', name: '纯真丝线', shortName: '纯真丝线', group: 'basic', stage: '素材激化 · 丝线', drop: 150, energy: 10, quickSteps: [[1500, '+1500'], [6000, '+6000']] },
  { id: '992617511', name: '噗灵', shortName: '噗灵', group: 'basic', stage: '素材激化 · 噗灵', drop: 20000, energy: 10, quickSteps: [[160000, '+16万'], [640000, '+64万']] },
  { id: '2431685856', name: '涡影泡泡・典雅', shortName: '典雅 · 攀越', group: 'vortex', stage: '魔物试炼 · 攀越 (1.0)', drop: 50, energy: 40, quickSteps: [[100, '+100'], [300, '+300']] },
  { id: '2299563415', name: '涡影泡泡・清新', shortName: '清新 · 翻转', group: 'vortex', stage: '魔物试炼 · 翻转 (1.0)', drop: 50, energy: 40, quickSteps: [[100, '+100'], [300, '+300']] },
  { id: '201359675', name: '涡影泡泡・甜美', shortName: '甜美 · 风舞', group: 'vortex', stage: '魔物试炼 · 风舞 (1.0)', drop: 50, energy: 40, quickSteps: [[100, '+100'], [300, '+300']] },
  { id: '2201002702', name: '涡影泡泡・性感', shortName: '性感 · 飞旋', group: 'vortex', stage: '魔物试炼 · 飞旋 (1.0)', drop: 50, energy: 40, quickSteps: [[100, '+100'], [300, '+300']] },
  { id: '102807154', name: '涡影泡泡・帅气', shortName: '帅气 · 震荡', group: 'vortex', stage: '魔物试炼 · 震荡 (1.0)', drop: 50, energy: 40, quickSteps: [[100, '+100'], [300, '+300']] },
  { id: '530602518', name: '幻彩泡泡・典雅', shortName: '典雅 · 迷踪', group: 'chroma', stage: '魔物试炼 · 迷踪 (2.0)', drop: 50, energy: 40, quickSteps: [[100, '+100'], [300, '+300']] },
  { id: '2628798194', name: '幻彩泡泡・清新', shortName: '清新 · 强袭', group: 'chroma', stage: '魔物试炼 · 强袭 (2.0)', drop: 50, energy: 40, quickSteps: [[100, '+100'], [300, '+300']] },
  { id: '432042829', name: '幻彩泡泡・甜美', shortName: '甜美 · 失重', group: 'chroma', stage: '魔物试炼 · 失重 (2.0)', drop: 50, energy: 40, quickSteps: [[100, '+100'], [300, '+300']] },
  { id: '2530246569', name: '幻彩泡泡・性感', shortName: '性感 · 驱逐', group: 'chroma', stage: '魔物试炼 · 驱逐 (2.0)', drop: 50, energy: 40, quickSteps: [[100, '+100'], [300, '+300']] },
  { id: '333473796', name: '幻彩泡泡・帅气', shortName: '帅气 · 暴行', group: 'chroma', stage: '魔物试炼 · 暴行 (2.0)', drop: 50, energy: 40, quickSteps: [[100, '+100'], [300, '+300']] },
];

const field = 'rounded-lg border border-border bg-bg-secondary px-2 py-1 text-xs outline-none focus:border-accent';
const num = (v: number) => v.toLocaleString('zh-CN');
const hasPositiveTargets = (m?: MaterialsState | null) =>
  Object.values(m?.glow_plan?.targets ?? {}).some((v) => Number(v) > 0);

export function GlowPrepConfigPanel({ compact = false, externalMaterials, accountKey: propKey, onChanged }: {
  compact?: boolean;
  externalMaterials?: MaterialsState | null;
  accountKey?: string;
  onChanged?: (next: MaterialsState) => void;
}) {
  const activeUid = useAccountStateStore((state) => state.data?.uid);
  const setCurrentPage = useAppStore((state) => state.setCurrentPage);
  const [accountKey, setAccountKey] = useState(propKey ?? '');
  const [materials, setMaterials] = useState<MaterialsState>(externalMaterials ?? {});
  const [tab, setTab] = useState<'active' | 'vortex' | 'chroma' | 'basic'>(() =>
    hasPositiveTargets(externalMaterials) ? 'active' : 'vortex',
  );
  const initializedAccountRef = useRef<string | null>(
    propKey && externalMaterials !== undefined ? propKey : null,
  );

  useEffect(() => {
    if (propKey && externalMaterials !== undefined) {
      setAccountKey(propKey);
      setMaterials(externalMaterials ?? {});
      if (initializedAccountRef.current !== propKey) {
        initializedAccountRef.current = propKey;
        setTab(hasPositiveTargets(externalMaterials) ? 'active' : 'vortex');
      }
      return;
    }
    let cancelled = false;
    void loadScoring().then((res) => {
      if (cancelled) return;
      setAccountKey(res.account_key);
      const nextMats = res.materials ?? {};
      setMaterials(nextMats);
      if (initializedAccountRef.current !== res.account_key) {
        initializedAccountRef.current = res.account_key;
        setTab(hasPositiveTargets(nextMats) ? 'active' : 'vortex');
      }
    }).catch(() => {});
    return () => { cancelled = true; };
  }, [activeUid, propKey, externalMaterials]);

  const inventory = materials.inventory ?? {};
  const targets = materials.glow_plan?.targets ?? {};
  const activeDefs = useMemo(() => GLOW_MATERIAL_DEFS.filter((d) => (targets[d.id] ?? 0) > 0), [targets]);

  function saveTargets(nextTargets: Record<string, number>, label?: string) {
    if (!accountKey) {
      toast.error('请先连接游戏或读取一次衣柜以确认当前账号。');
      return;
    }
    const cleaned = Object.fromEntries(Object.entries(nextTargets).filter(([, v]) => Number.isFinite(v) && v > 0));
    const hasRemaining = Object.entries(cleaned).some(([id, t]) => t > Number(inventory[id] ?? 0));
    const next: MaterialsState = {
      ...materials,
      glow_plan: Object.keys(cleaned).length ? {
        enabled: true,
        completed: !hasRemaining,
        source_label: label ?? materials.glow_plan?.source_label ?? '手动指定目标',
        targets: cleaned,
        updated_at: new Date().toISOString(),
      } : null,
    };
    setMaterials(next);
    onChanged?.(next);
    void saveMaterialPlan(accountKey, next).catch((err) => toast.error(String(err)));
  }

  function setTarget(id: string, value: number) {
    const next = { ...targets };
    if (!value || value <= 0) delete next[id];
    else next[id] = Math.round(value);
    saveTargets(next, '手动指定目标');
  }

  function addTargetDelta(id: string, delta: number) {
    const curStock = Number(inventory[id] ?? 0);
    const curTarget = Number(targets[id] ?? 0);
    const base = Math.max(curStock, curTarget);
    setTarget(id, base + delta);
  }

  const totalEnergy = useMemo(() => activeDefs.reduce((sum, d) => {
    const deficit = Math.max(0, (targets[d.id] ?? 0) - Number(inventory[d.id] ?? 0));
    return sum + Math.ceil(deficit / d.drop) * d.energy;
  }, 0), [activeDefs, targets, inventory]);

  const shownDefs = tab === 'active'
    ? activeDefs
    : GLOW_MATERIAL_DEFS.filter((d) => d.group === tab);

  return (
    <div className={clsx('space-y-3', !compact && 'rounded-xl border border-border bg-bg-secondary p-4')}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <span className="inline-flex items-center gap-1.5 text-xs font-semibold text-text-primary">
            <Sparkles className="h-3.5 w-3.5 text-accent" />
            焕新材料筹备目标
            {activeDefs.length > 0 && (
              <span className="rounded bg-accent/10 px-1.5 py-0.5 text-[11px] text-accent">
                已设 {activeDefs.length} 项 · {totalEnergy > 0 ? `还需约 ${num(totalEnergy)} 能量` : '已全部达标'}
              </span>
            )}
          </span>
          <p className="mt-0.5 text-[11px] text-text-secondary">
            可直接填写目标库存或点「+数量」在现有库存上追加，也可从搭配计算导入；刷满后自动跳过并把剩余能量交给下方幻境挑战。
          </p>
        </div>
        <div className="flex items-center gap-1.5">
          {compact && (
            <button type="button" onClick={() => setCurrentPage('wardrobe')}
              className="inline-flex items-center gap-1 rounded-lg border border-border bg-bg-secondary px-2 py-1 text-[11px] text-text-secondary hover:bg-bg-hover">
              <Calculator className="h-3 w-3" />搭配计算导入
            </button>
          )}
          {activeDefs.length > 0 && (
            <button type="button" onClick={() => saveTargets({}, '')}
              className="inline-flex items-center gap-1 rounded-lg border border-border bg-bg-secondary px-2 py-1 text-[11px] text-text-secondary hover:bg-bg-hover">
              <RotateCcw className="h-3 w-3" />清空目标
            </button>
          )}
        </div>
      </div>

      <div className="flex flex-wrap gap-1 rounded-lg bg-bg-secondary p-0.5 border border-border">
        {([
          ['active', `已设目标 (${activeDefs.length})`],
          ['vortex', '1.0 涡影泡泡 (5)'],
          ['chroma', '2.0 幻彩泡泡 (5)'],
          ['basic', '闪亮泡泡 / 丝线 / 噗灵'],
        ] as const).map(([id, label]) => (
          <button key={id} type="button" onClick={() => setTab(id)}
            className={clsx('rounded-md px-2.5 py-1 text-xs transition-colors',
              tab === id ? 'bg-accent/15 font-medium text-accent' : 'text-text-secondary hover:text-text-primary')}>
            {label}
          </button>
        ))}
      </div>

      {shownDefs.length === 0 ? (
        <div className="rounded-lg border border-dashed border-border bg-bg-primary px-4 py-5 text-center text-xs text-text-secondary">
          当前尚未设置任何焕新材料筹备目标。请切换到右侧的分类标签页手动输入目标库存或点「+数量」追加，也可从搭配计算结果一键导入。
        </div>
      ) : (
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        {shownDefs.map((def) => {
          const cur = Number(inventory[def.id] ?? 0);
          const target = Number(targets[def.id] ?? 0);
          const deficit = Math.max(0, target - cur);
          const runs = Math.ceil(deficit / def.drop);
          return (
            <div key={def.id} className={clsx('rounded-lg border p-2.5 text-xs transition-colors',
              target > 0 ? 'border-accent/40 bg-accent/5' : 'border-border bg-bg-primary')}>
              <div className="flex items-center justify-between gap-2">
                <div className="min-w-0">
                  <strong className="text-text-primary">{def.name}</strong>
                  <span className="ml-1.5 text-[11px] text-text-muted">库存 {num(cur)}</span>
                </div>
                {target > 0 && (
                  <button type="button" onClick={() => setTarget(def.id, 0)} title="取消此项目标"
                    className="rounded p-0.5 text-text-muted hover:bg-bg-hover hover:text-text-primary">
                    <X className="h-3.5 w-3.5" />
                  </button>
                )}
              </div>
              <div className="mt-2 flex flex-wrap items-center justify-between gap-1.5">
                <label className="flex items-center gap-1 text-[11px] text-text-secondary">
                  <span>目标</span>
                  <input aria-label={`${def.name}目标库存`} type="number" min="0" placeholder="未设"
                    value={target || ''}
                    onChange={(e) => setTarget(def.id, e.target.value === '' ? 0 : Number(e.target.value))}
                    className={clsx(field, '!w-20 text-center tabular-nums')} />
                </label>
                <div className="flex items-center gap-1">
                  {def.quickSteps.map(([delta, label]) => (
                    <button key={delta} type="button" onClick={() => addTargetDelta(def.id, delta)}
                      className="inline-flex items-center rounded border border-border bg-bg-secondary px-1.5 py-0.5 text-[11px] text-text-secondary hover:border-accent hover:text-accent"
                      title={`在当前库存或目标基础上增加 ${label}`}>
                      <Plus className="h-2.5 w-2.5" />{label.slice(1)}
                    </button>
                  ))}
                </div>
              </div>
              {target > 0 && (
                <p className="mt-1.5 text-[11px]">
                  {deficit > 0 ? (
                    <span className="text-accent">还差 {num(deficit)}（约刷 {runs} 次 · {runs * def.energy} 能量）</span>
                  ) : (
                    <span className="inline-flex items-center gap-1 text-success"><Check className="h-3 w-3" />已达到目标库存（将跳过）</span>
                  )}
                </p>
              )}
            </div>
          );
        })}
      </div>
      )}
    </div>
  );
}
