import { useMemo } from 'react';
import { Check, Flame, Hammer, Sparkles } from 'lucide-react';
import clsx from 'clsx';
import { useAppStore } from '@/stores/appStore';
import { useAccountStateStore } from '@/stores/accountStateStore';
import { getBlessingProgress } from '@/utils/blessingProgress';
import { realmCollectionEnabled, realmCraftingEnabled } from '@/utils/nikkiOptions';
import type { OptionValue } from '@/types/interface';

const MODES = [
  { id: 'daily', icon: Flame, title: '日常消耗', detail: '按选定方式清理剩余活跃能量' },
  { id: 'blessing', icon: Sparkles, title: '五星闪光全收集', detail: '按预期新色选本，刷取、升级并锁定新颜色' },
  { id: 'crafting', icon: Hammer, title: '制衣材料全收集', detail: '按未制作设计图缺口自动刷取魔物试炼晶石' },
] as const;

/** The realm modes share the weekly step but keep separate settings. */
export function RealmModeSwitch({ instanceId, taskId, values, disabled }: {
  instanceId: string; taskId: string; values: Record<string, OptionValue>; disabled: boolean;
}) {
  const setTaskOptionValue = useAppStore((state) => state.setTaskOptionValue);
  const setCurrentPage = useAppStore((state) => state.setCurrentPage);
  const account = useAccountStateStore((state) => state.data);
  const progress = useMemo(() => getBlessingProgress(account), [account]);
  const collection = realmCollectionEnabled(values);
  const crafting = realmCraftingEnabled(values);
  const activeMode = collection ? 'blessing' : crafting ? 'crafting' : 'daily';
  const decompose = values.CollectionDecompose?.type === 'switch' && values.CollectionDecompose.value;
  const autoBling = values.CollectionBling?.type !== 'switch' || values.CollectionBling.value;
  const steps = [...progress.known ? [] : ['首次整理全部闪光'], '优先升高概率部位', autoBling ? '升不起时刷噗灵，否则刷胚子' : '按预期新色刷取',
    '升级新胚子'];

  function selectMode(mode: 'daily' | 'blessing' | 'crafting') {
    setTaskOptionValue(instanceId, taskId, 'CollectionMode', { type: 'switch', value: mode === 'blessing' });
    setTaskOptionValue(instanceId, taskId, 'CraftingCollectionMode', { type: 'switch', value: mode === 'crafting' });
  }

  return <div className="mb-4 space-y-2">
    <div role="radiogroup" aria-label="幻境挑战模式" className="grid grid-cols-1 gap-2 sm:grid-cols-3">
      {MODES.map(({ id, icon: Icon, title, detail }) => {
        const active = activeMode === id;
        return <button key={id} type="button" role="radio" aria-checked={active} disabled={disabled}
          onClick={() => selectMode(id)}
          className={clsx('relative rounded-lg border p-3 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-60',
            active ? 'border-accent bg-accent/10' : 'border-border bg-bg-primary hover:bg-bg-hover')}>
          <span className="flex items-center gap-2 text-sm font-semibold">
            <Icon className={clsx('h-4 w-4 shrink-0', active ? 'text-accent' : 'text-text-muted')} />
            <span className="truncate">{title}</span>
            {active && <Check className="ml-auto h-4 w-4 shrink-0 text-accent" />}
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
      {decompose && <p className="mt-1.5 text-warning">已开启爆仓分解：仓库满时会一键分解未锁定的闪光，且不可恢复。请勿手动解锁已收藏的祝福闪光。</p>}
    </div>}
    {crafting && <div className="rounded-lg border border-border bg-bg-primary px-3 py-2.5 text-xs text-text-secondary">
      <p>根据已读衣柜自动计算全部未制作设计图所需的 10 种魔物试炼晶石（5 种磐岩晶石 + 5 种大地晶珀），按缺口从大到小精准计算次数挑战，刷完一种后同轮剩余能量自动接力下一种。</p>
      <button type="button" onClick={() => setCurrentPage('wardrobe')} className="mt-1.5 font-medium text-accent hover:underline">
        前往「衣柜与搭配 → 制衣与材料」查看各晶石缺口与库存 →
      </button>
    </div>}
  </div>;
}
