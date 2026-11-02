import { useEffect, useMemo } from 'react';
import { ArrowLeft, CalendarCheck, Shirt, Sparkles, Star, type LucideIcon } from 'lucide-react';
import clsx from 'clsx';
import { useAppStore } from '@/stores/appStore';
import {
  crownProgress,
  gameDay,
  periodCompleted,
  periodKey,
  useAccountStateStore,
} from '@/stores/accountStateStore';
import { getBlessingProgress } from '@/utils/blessingProgress';
import type { PageView } from '@/stores/types';

const PERIODIC = [
  { task: 'weekly_free_gifts', label: '每周免费礼包', period: '本周' },
  { task: 'monthly_crystal_exchange', label: '每月五抽兑换', period: '本月' },
  { task: 'crown', label: '奇迹之冠巅峰赛', period: '本期' },
] as const;

function Card({ icon: Icon, title, value, detail, page, warn }: {
  icon: LucideIcon; title: string; value: string; detail: string; page?: PageView; warn?: boolean;
}) {
  const body = <>
    <span className="flex items-center gap-2 text-xs text-text-secondary"><Icon className="h-4 w-4 text-accent" />{title}</span>
    <span className={clsx('mt-1.5 block text-xl font-semibold tabular-nums', warn && 'text-warning')}>{value}</span>
    <span className="mt-0.5 block text-xs leading-5 text-text-muted">{detail}</span>
  </>;
  const shared = 'block w-full rounded-xl border border-border bg-bg-secondary px-4 py-3 text-left';
  return page
    ? <button type="button" onClick={() => useAppStore.getState().setCurrentPage(page)} className={clsx(shared, 'transition-colors hover:bg-bg-hover')}>{body}</button>
    : <div className={shared}>{body}</div>;
}

/** One place for what today's account state means; details stay on their own pages. */
export function OverviewPage({ onClose }: { onClose: () => void }) {
  const { data, error, refresh } = useAccountStateStore();
  const instance = useAppStore((state) => state.instances.find((item) => item.id === state.activeInstanceId));
  useEffect(() => { void refresh().catch(() => {}); }, [refresh]);

  const progress = useMemo(() => getBlessingProgress(data), [data]);
  const today = gameDay().toISOString().slice(0, 10);
  const meteor = data?.observations?.giant_meteor;
  const meteorToday = meteor?.game_day === today ? meteor.outcome : null;
  const crown = crownProgress(data);
  const wardrobe = data?.wardrobe?.inventory ?? data?.wardrobe?.last_scan;
  const resources = data?.blessing?.pursuit?.resources;
  const enabled = instance?.selectedTasks.filter((task) => task.enabled).length ?? 0;
  const total = instance?.selectedTasks.length ?? 0;
  const needBling = !!resources?.embryos && resources.bling != null && resources.bling < 72000;
  const warehouseFull = !!resources?.capacity && resources.capacity[0] >= resources.capacity[1];

  const notes = [
    ...meteorToday === 'found' ? ['今夜有巨陨星，记得前往家园查看。'] : [],
    ...needBling ? ['噗灵不够升级已有胚子，全收集会先刷噗灵。'] : [],
    ...warehouseFull ? ['祝福闪光仓库已满，请确认分解设置，否则当天不会刷本。'] : [],
  ];

  return <div className="flex min-h-0 flex-1 flex-col bg-bg-primary text-text-primary">
    <div className="flex items-center gap-3 border-b border-border bg-bg-secondary px-5 py-3">
      <button onClick={onClose} className="rounded-md p-2 hover:bg-bg-hover" title="返回任务列表" aria-label="返回任务列表"><ArrowLeft className="h-4 w-4" /></button>
      <h1 className="flex-1 text-lg font-semibold">账号总览</h1>
      {data?.uid && <span className="text-xs tabular-nums text-text-muted">UID {data.uid}</span>}
    </div>
    <div className="min-h-0 flex-1 overflow-auto p-4">
      <div className="mx-auto max-w-4xl space-y-4">
        {error && <p role="alert" className="rounded-md border border-error/30 bg-error/5 p-3 text-xs text-error">无法读取账号状态：{error}</p>}
        {!data?.uid && !error && <p className="rounded-md border border-border bg-bg-secondary p-3 text-sm text-text-secondary">运行一次任务后，这里会显示账号的进度。</p>}
        {notes.length > 0 && <ul className="space-y-1.5 rounded-xl border border-accent/30 bg-accent/5 p-3 text-sm">
          {notes.map((note) => <li key={note}>{note}</li>)}
        </ul>}
        <section className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3" aria-label="账号进度">
          <Card icon={Sparkles} title="祝福闪光" page="blessing"
            value={progress.percentage === null ? '尚无记录' : `${progress.percentage}%`}
            detail={progress.known ? `已收集 ${progress.owned.size} / ${progress.target} 个颜色` : '开启五星全收集后会先自动整理'} />
          <Card icon={Shirt} title="衣柜" page="wardrobe"
            value={wardrobe ? `${wardrobe.scanned_count} 件` : '尚未读取'}
            detail={wardrobe ? `读取于 ${new Date(wardrobe.updated_at).toLocaleDateString('zh-CN')}` : '读取后可以使用搭配计算'} />
          <Card icon={Star} title="今日巨陨星"
            value={meteorToday === 'found' ? '已发现' : meteorToday === 'not_found' ? '未发现' : '尚未检测'}
            detail="运行家园日常时检测" />
          <Card icon={CalendarCheck} title="当前配置"
            value={`${enabled} / ${total} 个任务`}
            detail={instance?.name ?? '尚无配置'} />
        </section>
        <section className="rounded-xl border border-border bg-bg-secondary p-4">
          <h2 className="text-sm font-semibold">周期任务</h2>
          <ul className="mt-2 divide-y divide-border/60 text-sm">
            {PERIODIC.map(({ task, label, period }) => {
              const record = data?.tasks?.[task];
              const done = periodCompleted(data, task);
              const started = record?.period === periodKey(task);
              return <li key={task} className="flex items-center justify-between gap-3 py-2">
                <span>{label}</span>
                <span className={clsx('text-xs tabular-nums', done ? 'text-success' : 'text-text-secondary')}>
                  {task === 'crown' && crown ? `${crown.stars} / ${crown.total} 星 · ` : ''}
                  {done ? `${period}已完成` : started ? `${period}进行中` : `${period}未开始`}
                </span>
              </li>;
            })}
          </ul>
        </section>
      </div>
    </div>
  </div>;
}
