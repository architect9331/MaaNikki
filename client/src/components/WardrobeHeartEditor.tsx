import { useMemo, useState } from 'react';
import { Check, RotateCcw, Sparkles } from 'lucide-react';
import clsx from 'clsx';
import { ATTRIBUTES, HEART_GROUPS, type HeartSettings } from '@/services/scoring/types';
import {
  ALL_HEART_NODES,
  STARSEA_NODES,
  WISHFIELD_NODES,
  computeHeartFromNodes,
  inferNodesFromHeart,
  type HeartUnlockNode,
} from '@/services/scoring/heartPresets';

const field = 'w-full rounded-lg border border-border bg-bg-primary px-3 py-2 text-sm outline-none focus:border-accent';
const card = 'rounded-xl border border-border bg-bg-secondary p-4';

function NumberField({ label, value, onChange }: { label: string; value: number; onChange: (value: number) => void }) {
  return <label className="space-y-1.5 text-xs text-text-secondary"><span>{label}</span>
    <input className={field} type="number" min="0" step="any" value={value} onChange={(event) => {
      const next = Number(event.target.value);
      if (Number.isFinite(next) && next >= 0) onChange(next);
    }} />
  </label>;
}

export function WardrobeHeartEditor({ heart, onChange }: {
  heart: HeartSettings;
  onChange: (updater: (previous: HeartSettings) => HeartSettings) => void;
}) {
  const [regionTab, setRegionTab] = useState<'wishfield' | 'starsea' | 'manual'>('wishfield');
  const inferred = useMemo(() => inferNodesFromHeart(heart), [heart]);
  const unlockedSet = useMemo(() => new Set(inferred.unlocked), [inferred.unlocked]);
  const heartFilled = heart.flat.some(Boolean) || heart.stat.some(Boolean) || heart.part.some(Boolean) || heart.display > 0;

  function applySelection(unlocked: number[], flatLevels: number[]) {
    const next = computeHeartFromNodes({ unlocked, flatLevels });
    onChange(() => next);
  }

  function toggleNode(node: HeartUnlockNode) {
    const next = new Set(unlockedSet);
    if (next.has(node.id)) next.delete(node.id);
    else next.add(node.id);
    applySelection([...next], inferred.flatLevels);
  }

  function setFlatLevel(attrIndex: number, level: number) {
    const nextLevels = inferred.flatLevels.map((old, i) => i === attrIndex ? level : old);
    applySelection(inferred.unlocked, nextLevels);
  }

  function setRegionAll(nodes: HeartUnlockNode[], checked: boolean, maxFlat = false) {
    const next = new Set(unlockedSet);
    for (const node of nodes) {
      if (checked) next.add(node.id);
      else next.delete(node.id);
    }
    const nextLevels = maxFlat ? Array(5).fill(checked ? 10 : 0) : inferred.flatLevels;
    applySelection([...next], nextLevels);
  }

  const activeNodes = regionTab === 'wishfield' ? WISHFIELD_NODES : STARSEA_NODES;
  const wishfieldCount = WISHFIELD_NODES.filter((n) => unlockedSet.has(n.id)).length;
  const starseaCount = STARSEA_NODES.filter((n) => unlockedSet.has(n.id)).length;

  return <details className={card}>
    <summary className="cursor-pointer text-sm font-medium">
      无限之心加成
      <span className="ml-1.5 text-xs font-normal text-text-muted">
        {heartFilled ? `心愿原野 ${wishfieldCount}/15 · 星海 ${starseaCount}/5 · 展示 +${heart.display}%` : '未填写'}
      </span>
    </summary>
    <div className="mt-3 space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs leading-5 text-text-secondary">可按区域节点直接勾选或一键满级，自动折算为各项加成；也可切换到手动微调。</p>
        <div className="flex flex-wrap gap-1.5">
          <button type="button" onClick={() => applySelection(ALL_HEART_NODES.map((n) => n.id), [10, 10, 10, 10, 10])}
            className="inline-flex items-center gap-1 rounded-lg bg-accent/10 px-2.5 py-1 text-xs font-medium text-accent hover:bg-accent/20">
            <Sparkles className="h-3.5 w-3.5" />全部满级
          </button>
          <button type="button" onClick={() => applySelection(WISHFIELD_NODES.map((n) => n.id), [10, 10, 10, 10, 10])}
            className="rounded-lg border border-border bg-bg-primary px-2.5 py-1 text-xs text-text-secondary hover:bg-bg-hover">
            仅心愿原野满级
          </button>
          <button type="button" onClick={() => applySelection([], [0, 0, 0, 0, 0])}
            className="inline-flex items-center gap-1 rounded-lg border border-border bg-bg-primary px-2.5 py-1 text-xs text-text-secondary hover:bg-bg-hover">
            <RotateCcw className="h-3 w-3" />清空
          </button>
        </div>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border pt-3">
        <div className="flex gap-1 rounded-lg bg-bg-primary p-0.5">
          {([
            ['wishfield', `心愿原野碎片 (${wishfieldCount}/15)`],
            ['starsea', `星海碎片 (${starseaCount}/5)`],
            ['manual', '手动微调数值'],
          ] as const).map(([id, label]) => (
            <button key={id} type="button" onClick={() => setRegionTab(id)}
              className={clsx('rounded-lg px-2.5 py-1 text-xs transition-colors',
                regionTab === id ? 'bg-accent/10 font-medium text-accent' : 'text-text-secondary hover:text-text-primary')}>
              {label}
            </button>
          ))}
        </div>
        {regionTab !== 'manual' && (
          <div className="flex gap-1.5">
            <button type="button" onClick={() => setRegionAll(activeNodes, true, regionTab === 'wishfield')}
              className="rounded-lg border border-border px-2 py-1 text-[11px] text-text-secondary hover:bg-bg-hover">
              本区全满
            </button>
            <button type="button" onClick={() => setRegionAll(activeNodes, false, regionTab === 'wishfield')}
              className="rounded-lg border border-border px-2 py-1 text-[11px] text-text-secondary hover:bg-bg-hover">
              本区清空
            </button>
          </div>
        )}
      </div>

      {regionTab !== 'manual' ? (
        <div className="space-y-3">
          <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-3">
            {activeNodes.map((node) => {
              const checked = unlockedSet.has(node.id);
              return (
                <button key={node.id} type="button" role="checkbox" aria-checked={checked} onClick={() => toggleNode(node)}
                  className={clsx('flex items-center gap-2 rounded-lg border px-2.5 py-2 text-left text-xs transition-colors',
                    checked ? 'border-accent bg-accent/10 text-text-primary' : 'border-border bg-bg-primary text-text-secondary hover:bg-bg-hover')}>
                  <span className={clsx('flex h-4 w-4 shrink-0 items-center justify-center rounded border',
                    checked ? 'border-accent bg-accent text-on-accent' : 'border-border bg-bg-secondary')}>
                    {checked && <Check className="h-3 w-3" />}
                  </span>
                  <span className="truncate"><span className="mr-1 tabular-nums text-text-muted">{node.order}.</span>{node.label}</span>
                </button>
              );
            })}
          </div>
          {regionTab === 'wishfield' && (
            <div className="border-t border-border pt-3">
              <p className="mb-2 text-xs font-medium text-text-secondary">五维造型节点等级（每级 +10 固定属性，上限 +100）</p>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
                {ATTRIBUTES.map((name, index) => (
                  <label key={name} className="space-y-1 text-xs text-text-secondary">
                    <span>{name}</span>
                    <select className={clsx(field, '!py-1.5 !text-xs tabular-nums')} value={inferred.flatLevels[index] ?? 0}
                      onChange={(e) => setFlatLevel(index, Number(e.target.value))}>
                      {Array.from({ length: 11 }, (_, lv) => (
                        <option key={lv} value={lv}>{lv === 0 ? '未解锁 (0)' : `${lv} 级 (+${lv * 10})`}</option>
                      ))}
                    </select>
                  </label>
                ))}
              </div>
            </div>
          )}
        </div>
      ) : (
        <div className="space-y-3">
          <div>
            <p className="mb-2 text-xs font-medium">固定属性</p>
            <div className="grid grid-cols-5 gap-2">{ATTRIBUTES.map((name, index) => <NumberField key={name} label={name} value={heart.flat[index]}
              onChange={(value) => onChange((prev) => ({ ...prev, flat: prev.flat.map((old, i) => i === index ? value : old) }))} />)}</div>
          </div>
          {(['stat', 'part'] as const).map((kind) => <div key={kind}>
            <p className="mb-2 text-xs font-medium">{kind === 'stat' ? '属性加成 %（含通用节点）' : '部位加成 %'}</p>
            <div className="grid grid-cols-3 gap-2 sm:grid-cols-6">{HEART_GROUPS.map((name, index) => <NumberField key={name} label={name} value={heart[kind][index]}
              onChange={(value) => onChange((prev) => ({ ...prev, [kind]: prev[kind].map((old, i) => i === index ? value : old) }))} />)}</div>
          </div>)}
          <div className="max-w-40"><NumberField label="展示加成 %" value={heart.display}
            onChange={(display) => onChange((prev) => ({ ...prev, display }))} /></div>
        </div>
      )}
    </div>
  </details>;
}
