import { Check, ChevronLeft, ChevronRight } from 'lucide-react';
import clsx from 'clsx';
import {
  ALL_REGIONS,
  REGION_META,
  normalizeRegionConfig,
  type CollectionRegion,
  type RegionFilterConfig,
  type RegionPriorityMode,
} from '@/services/collectionRegions';
import type { PlanMode } from '@/services/scoring/types';
import { Segmented } from './ui/FeaturePage';

const PRIORITY_OPTIONS = [
  ['global', '全局最优'],
  ['ordered', '按地区优先'],
] as const;

export function CollectionRegionFilter({
  value,
  mode,
  counts,
  onChange,
}: {
  value?: Partial<RegionFilterConfig> | null;
  mode: PlanMode;
  counts?: Partial<Record<CollectionRegion, string>>;
  onChange: (next: RegionFilterConfig) => void;
}) {
  const cfg = normalizeRegionConfig(value);
  const enabledSet = new Set(cfg.enabled);
  const activeOrder = mode === 'selected' ? cfg.order : cfg.order.filter((reg) => enabledSet.has(reg));

  const toggleRegion = (reg: CollectionRegion) => {
    const on = enabledSet.has(reg);
    if (on && cfg.enabled.length <= 1) return;
    const nextEnabled = on
      ? cfg.enabled.filter((item) => item !== reg)
      : ALL_REGIONS.filter((item) => item === reg || enabledSet.has(item));
    onChange({ ...cfg, enabled: nextEnabled });
  };

  const setPriority = (nextPriority: RegionPriorityMode) => {
    onChange({ ...cfg, priority: nextPriority });
  };

  const moveInOrder = (reg: CollectionRegion, delta: -1 | 1) => {
    const visible = activeOrder;
    const pos = visible.indexOf(reg);
    const targetPos = pos + delta;
    if (pos < 0 || targetPos < 0 || targetPos >= visible.length) return;
    const swapWith = visible[targetPos];
    const nextOrder = [...cfg.order];
    const idxA = nextOrder.indexOf(reg);
    const idxB = nextOrder.indexOf(swapWith);
    if (idxA < 0 || idxB < 0) return;
    [nextOrder[idxA], nextOrder[idxB]] = [nextOrder[idxB], nextOrder[idxA]];
    onChange({ ...cfg, order: nextOrder });
  };

  return (
    <div className="flex w-full flex-col gap-2">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        {mode !== 'selected' ? (
          <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="包含地区">
            {ALL_REGIONS.map((reg) => {
              const meta = REGION_META[reg];
              const on = enabledSet.has(reg);
              const locked = on && cfg.enabled.length <= 1;
              const detail = counts?.[reg];
              return (
                <button
                  key={reg}
                  type="button"
                  aria-pressed={on}
                  disabled={locked}
                  title={locked ? '至少保留一个地区' : `${on ? '关闭' : '开启'}${meta.label}（${meta.version}）`}
                  onClick={() => toggleRegion(reg)}
                  className={clsx(
                    'inline-flex items-center gap-1 rounded-lg border px-2 py-1 text-xs transition-colors disabled:cursor-not-allowed',
                    on
                      ? 'border-accent/50 bg-accent/10 font-medium text-accent'
                      : 'border-border bg-bg-primary text-text-secondary hover:border-accent hover:text-accent',
                  )}
                >
                  {on && <Check className="h-3 w-3 shrink-0" />}
                  <span>{meta.label}</span>
                  <span className="text-[10px] opacity-70">{meta.version}</span>
                  {detail && <span className="text-[10px] tabular-nums opacity-75">· {detail}</span>}
                </button>
              );
            })}
          </div>
        ) : (
          <span className="text-xs text-text-muted">自选模式按已勾选条目计算，可在此指定跨地区刷取顺序</span>
        )}

        {(mode === 'selected' || cfg.enabled.length >= 2) && (
          <div className="flex flex-wrap items-center gap-2">
            <Segmented
              label="地区刷取顺序"
              value={cfg.priority}
              options={PRIORITY_OPTIONS}
              onChange={(next) => setPriority(next as RegionPriorityMode)}
            />
          </div>
        )}
      </div>

      {cfg.priority === 'ordered' && activeOrder.length >= 2 && (
        <div className="flex flex-wrap items-center gap-1.5 text-xs">
          <span className="text-[11px] text-text-muted">刷取先后：</span>
          {activeOrder.map((reg, idx) => {
            const meta = REGION_META[reg];
            return (
              <span
                key={reg}
                className="inline-flex items-center gap-1 rounded-md border border-accent/30 bg-bg-primary px-1.5 py-0.5 text-[11px] text-text-primary"
              >
                <span className="flex h-4 w-4 items-center justify-center rounded-full bg-accent/15 text-[10px] font-semibold tabular-nums text-accent">
                  {idx + 1}
                </span>
                <span className="font-medium">{meta.label}</span>
                <span className="inline-flex items-center gap-0.5 pl-0.5">
                  <button
                    type="button"
                    aria-label={`提前${meta.label}顺序`}
                    disabled={idx === 0}
                    onClick={() => moveInOrder(reg, -1)}
                    className="rounded p-0.5 text-text-muted hover:bg-bg-hover hover:text-accent disabled:cursor-not-allowed disabled:opacity-30"
                  >
                    <ChevronLeft className="h-3 w-3" />
                  </button>
                  <button
                    type="button"
                    aria-label={`延后${meta.label}顺序`}
                    disabled={idx === activeOrder.length - 1}
                    onClick={() => moveInOrder(reg, 1)}
                    className="rounded p-0.5 text-text-muted hover:bg-bg-hover hover:text-accent disabled:cursor-not-allowed disabled:opacity-30"
                  >
                    <ChevronRight className="h-3 w-3" />
                  </button>
                </span>
              </span>
            );
          })}
          <span className="text-[11px] text-text-muted">前序地区缺口备齐后自动刷下一地区</span>
        </div>
      )}
    </div>
  );
}
