import { useAccountStateStore, gameDay } from '@/stores/accountStateStore';
import type { OptionValue } from '@/types/interface';
import { AlertCircle } from 'lucide-react';
import clsx from 'clsx';

export function XinghaiScoreHint({ values, compact = false }: {
  values: Record<string, OptionValue>;
  compact?: boolean;
}) {
  const rotation = useAccountStateStore((s) => s.data?.rotation);
  const loading = useAccountStateStore((s) => s.loading);
  const error = useAccountStateStore((s) => s.error);
  if (!rotation) return compact ? null : (
    <p className="text-xs text-text-muted" role="status">
      {error ? '任务轮换读取失败，暂无法计算 500 分；请稍后重试。' : loading
        ? '正在读取任务轮换…' : '未读取到任务轮换，暂无法计算 500 分。'}
    </p>
  );
  const selected = (key: string) => {
    const value = values[key === "crystal" ? "XinghaiForceCrystal" : `XinghaiTask_${key}`];
    return value?.type === 'switch' ? value.value : true;
  };
  const today = gameDay();
  const index = ((Math.floor((Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate()) -
    Date.parse(rotation.anchor + 'T00:00:00Z')) / 86400000) % 8) + 8) % 8;
  const cycles = rotation.cycles.map((cycle, i) => {
    const adapted = cycle.tasks.filter((key) => rotation.tasks[key]?.adapted);
    const maximum = adapted.filter(selected).reduce((n, key) => n + (rotation.tasks[key].score ?? 0), 0);
    const disabled = adapted.filter((key) => !selected(key)).map((key) => rotation.tasks[key].label);
    const unsupported = cycle.tasks.filter((key) => !rotation.tasks[key]?.adapted)
      .map((key) => rotation.tasks[key]?.label ?? key);
    return { ...cycle, i, maximum, disabled, unsupported, insufficient: maximum < rotation.target_score };
  });
  const warnings = cycles.filter((cycle) => cycle.insufficient);
  if (compact) {
    if (!warnings.length) return null;
    const current = warnings.find((warning) => warning.i === index);
    return (
      <p className="flex items-start gap-1.5 text-xs text-warning" role="status">
        <AlertCircle className="w-3.5 h-3.5 shrink-0" />
        <span>所选任务有 {warnings.length} 组轮换不足 {rotation.target_score} 分{current ? `，今日最多 ${current.maximum} 分` : ''}。展开查看原因。</span>
      </p>
    );
  }
  return (
    <div className="rounded-md border border-border bg-bg-secondary overflow-hidden text-xs text-text-secondary">
      <div className="flex items-center justify-between gap-3 border-b border-border px-3 py-2">
        <span className="font-medium text-text-primary">8 天任务轮换</span>
        <span className="text-text-muted">今日：第 {index + 1} 天</span>
      </div>
      <div className="divide-y divide-border" aria-live="polite">
        {cycles.map((cycle) => (
          <div key={cycle.i} className={clsx('px-3 py-2', cycle.insufficient && 'bg-warning/5')}>
            <div className="flex items-start gap-3">
              <div className="w-14 shrink-0 pt-0.5">
                <span className={clsx('font-medium', cycle.insufficient ? 'text-warning' : 'text-text-primary')}>第 {cycle.i + 1} 天</span>
                {cycle.i === index && <span className="mt-1 block w-fit rounded bg-accent/10 px-1 text-[10px] text-accent">今日</span>}
              </div>
              <div className="min-w-0 flex-1 space-y-1.5">
                <div className="flex flex-wrap gap-x-2 gap-y-1 leading-5">
                  {cycle.tasks.map((key) => {
                    const task = rotation.tasks[key];
                    const enabled = task?.adapted && selected(key);
                    return <span key={key} className={clsx(enabled ? 'text-text-secondary' : 'text-text-muted', task?.adapted && !enabled && 'line-through')}
                      title={!task?.adapted ? '尚未适配，不计入可达积分' : enabled ? `${task.score ?? 0} 分` : '已关闭，不计入可达积分'}>
                      {task?.label ?? key}{!task?.adapted && <span className="text-[10px]">（未适配）</span>}
                    </span>;
                  })}
                </div>
                {cycle.insufficient && <p className="flex items-start gap-1 text-warning leading-5">
                  <AlertCircle className="mt-0.5 w-3.5 h-3.5 shrink-0" />
                  <span>无法达到 {rotation.target_score} 分。{[
                    cycle.disabled.length ? `已关闭：${cycle.disabled.join('、')}` : '',
                    cycle.unsupported.length ? `未适配：${cycle.unsupported.join('、')}` : '',
                  ].filter(Boolean).join('；')}。</span>
                </p>}
              </div>
              <span className={clsx('shrink-0 pt-0.5 tabular-nums', cycle.insufficient ? 'text-warning font-medium' : 'text-text-secondary')}
                title={`所选已适配任务合计 ${cycle.maximum} 分，目标 ${rotation.target_score} 分`}>
                {Math.min(cycle.maximum, rotation.target_score)} / {rotation.target_score}
              </span>
            </div>
          </div>
        ))}
      </div>
      <p className="border-t border-border px-3 py-2 text-text-muted">按已勾选且已适配的子任务计算可达积分，实际得分以游戏内完成结果为准。</p>
    </div>
  );
}
