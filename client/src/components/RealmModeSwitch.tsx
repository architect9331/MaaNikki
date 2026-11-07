import { useMemo } from 'react';
import { Check, Flame, Hammer, Sparkles } from 'lucide-react';
import clsx from 'clsx';
import { useAppStore } from '@/stores/appStore';
import { useAccountStateStore } from '@/stores/accountStateStore';
import { getBlessingProgress } from '@/utils/blessingProgress';
import { realmCollectionEnabled, realmCollectionWorkflow, realmCraftingEnabled } from '@/utils/nikkiOptions';
import type { OptionValue } from '@/types/interface';

const MODES = [
  { id: 'daily', icon: Flame, title: '日常消耗', detail: '按选定方式清理剩余活跃能量' },
  { id: 'blessing', icon: Sparkles, title: '五星闪光全收集', detail: '按预期新色选本，支持全流程托管或只刷胚子' },
  { id: 'crafting', icon: Hammer, title: '制衣材料全收集', detail: '按未制作设计图缺口自动刷取魔物试炼晶石' },
] as const;

const WORKFLOW_MODES = [
  {
    id: 'Managed',
    title: '全流程托管',
    detail: '自动整理、按预期新色刷本、分解、升级并锁定满级新颜色',
  },
  {
    id: 'FarmOnly',
    title: '只刷胚子',
    detail: '按当前已收集颜色与仓库胚子数算期望选本刷取，升级与管理交给自己',
  },
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
  const workflow = realmCollectionWorkflow(values);
  const farmOnly = workflow === 'FarmOnly';
  const crafting = realmCraftingEnabled(values);
  const activeMode = collection ? 'blessing' : crafting ? 'crafting' : 'daily';
  const decompose = values.CollectionDecompose?.type === 'switch' && values.CollectionDecompose.value;
  const autoBling = values.CollectionBling?.type !== 'switch' || values.CollectionBling.value;
  const reserveBlingRaw = values.CollectionReserveBling?.type === 'input'
    ? Number.parseInt(values.CollectionReserveBling.values.amount ?? '0', 10)
    : 0;
  const reserveBling = Number.isFinite(reserveBlingRaw) && reserveBlingRaw > 0 ? reserveBlingRaw : 0;
  const steps = farmOnly
    ? [...progress.localKnown ? [] : ['首次扫描已收集颜色与胚子'], '按已有颜色与仓库胚子算期望选本', '刷取胚子（升级与锁定交由手动管理）']
    : [
        ...progress.localKnown ? [] : ['首次整理全部闪光'],
        '按出新色概率选本与升级',
        autoBling ? '升不起时刷噗灵，否则刷胚子' : '按预期新色刷取',
        '升级新胚子并锁定新颜色',
      ];

  function selectMode(mode: 'daily' | 'blessing' | 'crafting') {
    setTaskOptionValue(instanceId, taskId, 'CollectionMode', { type: 'switch', value: mode === 'blessing' });
    setTaskOptionValue(instanceId, taskId, 'CraftingCollectionMode', { type: 'switch', value: mode === 'crafting' });
  }

  function selectWorkflow(nextWorkflow: 'Managed' | 'FarmOnly') {
    setTaskOptionValue(instanceId, taskId, 'CollectionWorkflow', { type: 'select', caseName: nextWorkflow });
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
    {collection && <div className="space-y-2 rounded-lg border border-border bg-bg-primary px-3 py-2.5 text-xs">
      <div role="radiogroup" aria-label="全收集执行方式" className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        {WORKFLOW_MODES.map(({ id, title, detail }) => {
          const active = workflow === id;
          return <button key={id} type="button" role="radio" aria-checked={active} disabled={disabled}
            onClick={() => selectWorkflow(id)}
            className={clsx('rounded-lg border p-2.5 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-60',
              active ? 'border-accent bg-accent/10' : 'border-border bg-bg-secondary/60 hover:bg-bg-hover')}>
            <span className="flex items-center gap-1.5 text-xs font-semibold text-text-primary">
              <span>{title}</span>
              {active && <Check className="ml-auto h-3.5 w-3.5 shrink-0 text-accent" />}
            </span>
            <span className="mt-1 block text-[11px] leading-4 text-text-secondary">{detail}</span>
          </button>;
        })}
      </div>
      <div className="flex flex-wrap items-center gap-x-1.5 gap-y-1 text-text-secondary">{steps.map((step, index) =>
        <span key={step} className="flex items-center gap-1.5">{index > 0 && <span className="text-text-muted">→</span>}{step}</span>)}</div>
      <p className="text-text-muted">{progress.known
        ? `已收集 ${progress.owned.size} / ${progress.target} 个颜色${farmOnly ? '，将结合仓库现有胚子数规划期望最高的副本。' : '，继续按记录处理。'}`
        : farmOnly
          ? '尚无记录。第一次运行前请清除祝福闪光仓库的筛选，会只读扫描各部位颜色与胚子数，不会修改锁定、分解或升级。'
          : '尚无记录。第一次运行前请清除祝福闪光仓库的筛选，整理时会锁定每款每色的满级收藏。'}</p>
      {!farmOnly && reserveBling > 0 && (
        <p className="text-text-secondary">已设置保留噗灵：{reserveBling.toLocaleString()}（升级后噗灵不会低于该数值）。</p>
      )}
      {!farmOnly && decompose && <p className="text-warning">已开启爆仓分解：仓库满时会一键分解未锁定的闪光，且不可恢复。请勿手动解锁已收藏的祝福闪光。</p>}
    </div>}
    {crafting && <div className="rounded-lg border border-border bg-bg-primary px-3 py-2.5 text-xs text-text-secondary">
      <p>根据已读衣柜自动计算全部未制作设计图所需的 10 种魔物试炼晶石（5 种磐岩晶石 + 5 种大地晶珀），按缺口从大到小精准计算次数挑战，刷完一种后同轮剩余能量自动接力下一种。</p>
      <button type="button" onClick={() => setCurrentPage('wardrobe')} className="mt-1.5 font-medium text-accent hover:underline">
        前往「衣柜与搭配 → 制衣与材料」查看各晶石缺口与库存 →
      </button>
    </div>}
  </div>;
}
