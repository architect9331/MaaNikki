import { useMemo } from 'react';
import { Check, Flame, Sparkles } from 'lucide-react';
import clsx from 'clsx';
import { useAppStore } from '@/stores/appStore';
import { useAccountStateStore } from '@/stores/accountStateStore';
import { getBlessingProgress } from '@/utils/blessingProgress';
import { realmCollectionEnabled } from '@/utils/nikkiOptions';
import type { OptionValue } from '@/types/interface';

const MODES = [
  { collection: false, icon: Flame, title: '日常消耗', detail: '按选定方式清理剩余活跃能量' },
  { collection: true, icon: Sparkles, title: '五星全收集', detail: '按预期新色选本，刷取、升级并锁定新颜色' },
] as const;

/** The two realm modes share the weekly step but keep separate settings. */
export function RealmModeSwitch({ instanceId, taskId, values, disabled }: {
  instanceId: string; taskId: string; values: Record<string, OptionValue>; disabled: boolean;
}) {
  const setTaskOptionValue = useAppStore((state) => state.setTaskOptionValue);
  const account = useAccountStateStore((state) => state.data);
  const progress = useMemo(() => getBlessingProgress(account), [account]);
  const collection = realmCollectionEnabled(values);
  const decompose = values.CollectionDecompose?.type === 'switch' && values.CollectionDecompose.value;
  const autoBling = values.CollectionBling?.type !== 'switch' || values.CollectionBling.value;
  const steps = [...progress.known ? [] : ['首次整理全部闪光'], autoBling ? '刷胚子或补噗灵' : '按预期新色刷取',
    '优先升高概率部位', ...decompose ? ['分解重复五星'] : []];
  return <div className="mb-4 space-y-2">
    <div role="radiogroup" aria-label="幻境挑战模式" className="grid grid-cols-2 gap-2">
      {MODES.map(({ collection: target, icon: Icon, title, detail }) => {
        const active = collection === target;
        return <button key={title} type="button" role="radio" aria-checked={active} disabled={disabled}
          onClick={() => setTaskOptionValue(instanceId, taskId, 'CollectionMode', { type: 'switch', value: target })}
          className={clsx('relative rounded-lg border p-3 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-60',
            active ? 'border-accent bg-accent/10' : 'border-border bg-bg-primary hover:bg-bg-hover')}>
          <span className="flex items-center gap-2 text-sm font-semibold">
            <Icon className={clsx('h-4 w-4', active ? 'text-accent' : 'text-text-muted')} />{title}
            {active && <Check className="ml-auto h-4 w-4 text-accent" />}
          </span>
          <span className="mt-1.5 block text-xs leading-5 text-text-secondary">{detail}</span>
        </button>;
      })}
    </div>
    {collection && <div className="rounded-lg border border-border bg-bg-primary px-3 py-2.5 text-xs">
      <div className="flex flex-wrap items-center gap-x-1.5 gap-y-1 text-text-secondary">{steps.map((step, index) =>
        <span key={step} className="flex items-center gap-1.5">{index > 0 && <span className="text-text-muted">→</span>}{step}</span>)}</div>
      <p className="mt-1.5 text-text-muted">{progress.known
        ? `已收集 ${progress.owned.size} / ${progress.target} 个颜色，继续按记录处理。`
        : '尚无记录。第一次运行前请清除祝福闪光仓库的筛选，整理时会锁定每款每色的满级收藏。'}</p>
    </div>}
  </div>;
}
