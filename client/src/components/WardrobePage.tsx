import { ArrowLeft, Check, Clock, RefreshCw, Play, Square, Shirt, AlertCircle } from 'lucide-react';
import clsx from 'clsx';
import { useEffect, useState } from 'react';
import { useAccountStateStore } from '@/stores/accountStateStore';
import { useWardrobeStore, startWardrobeRead, stopWardrobeRead, wardrobeLabels } from '@/services/wardrobeService';
import { isTauri } from '@/utils/paths';
import { LogsPanel } from './LogsPanel';
import { WARDROBE_INSTANCE_ID } from '@/services/wardrobeService';
import { WardrobeCalculator } from './WardrobeCalculator';

export function WardrobePage({ onClose }: { onClose: () => void }) {
  const { data, error: accountError, refresh, loading } = useAccountStateStore();
  const { status, error } = useWardrobeStore();
  const [tab, setTab] = useState<'read' | 'calculate'>('read');
  const [calculatorOpened, setCalculatorOpened] = useState(false);
  const [refreshToken, setRefreshToken] = useState(0);
  const busy = ['starting', 'reading', 'stopping'].includes(status);
  const scan = data?.wardrobe?.last_scan ?? data?.wardrobe?.inventory;
  const parts = Object.entries(scan?.parts ?? {});
  useEffect(() => { void refresh().catch(() => {}); }, [refresh]);
  return <div className="flex min-h-0 flex-1 flex-col bg-bg-primary text-text-primary">
    <div className="flex items-center gap-3 border-b border-border bg-bg-secondary px-5 py-3">
      <button onClick={onClose} className="rounded-md p-2 hover:bg-bg-hover" aria-label="返回任务列表"><ArrowLeft className="h-4 w-4" /></button>
      <Shirt className="h-5 w-5 text-accent" /><h1 className="flex-1 text-lg font-semibold">衣柜</h1>
      {data?.uid && <span className="text-xs tabular-nums text-text-muted">UID {data.uid}</span>}
      <button onClick={() => void refresh().then(() => setRefreshToken((previous) => previous + 1)).catch(() => {})} disabled={loading} aria-label="刷新衣柜记录" className="rounded-md p-2 hover:bg-bg-hover disabled:opacity-40"><RefreshCw className={clsx('h-4 w-4', loading && 'animate-spin')} /></button>
    </div>
    <div className="flex gap-5 border-b border-border bg-bg-secondary px-5">{([['read', '衣柜读取'], ['calculate', '搭配计算']] as const).map(([id, label]) =>
      <button key={id} onClick={() => { setTab(id); if (id === 'calculate') setCalculatorOpened(true); }}
        className={clsx('border-b-2 py-3 text-sm', tab === id ? 'border-accent font-medium text-accent' : 'border-transparent text-text-secondary hover:text-text-primary')}>{label}</button>)}
    </div>
    <div className="min-h-0 flex-1 overflow-auto p-4">
      <div className={clsx('mx-auto max-w-3xl space-y-4', tab !== 'read' && 'hidden')}>
        <section className="rounded-xl border border-border bg-bg-secondary p-5">
          <div className="flex items-center justify-between gap-4">
            <div><h2 className="text-sm font-semibold">{wardrobeLabels[status]}</h2><p className="mt-1.5 text-xs leading-5 text-text-secondary">按获得时间逐部位读取，记录自动保存到当前游戏账号。</p></div>
            <button disabled={!isTauri() || status === 'stopping'} onClick={() => busy ? stopWardrobeRead() : void startWardrobeRead()}
              className={clsx('inline-flex shrink-0 items-center gap-2 rounded-lg px-4 py-2.5 text-sm font-medium disabled:opacity-40', busy ? 'border border-border hover:bg-bg-hover' : 'bg-accent text-white hover:bg-accent-hover')}>
              {busy ? <Square className="h-4 w-4" /> : <Play className="h-4 w-4" />}{busy ? '停止读取' : scan ? scan.complete ? '更新读取' : '继续读取' : '开始读取'}
            </button>
          </div>
          <p className="mt-4 text-xs leading-5 text-text-muted">进入游戏主界面，清空衣柜筛选后开始。读取期间保持游戏在前台；中断后可继续，再次读取会沿用已有记录并识别新增衣服。</p>
          {(error || accountError) && <p className="mt-3 flex items-start gap-1.5 text-xs text-warning" role="alert"><AlertCircle className="h-4 w-4 shrink-0" />{error || accountError}</p>}
        </section>
        <div className="grid grid-cols-3 gap-3">
          {([['已读取衣服', scan?.scanned_count], ['已读完部位', scan?.completed_parts], ['同图候选条目', scan?.ambiguous_count]] as const).map(([label, count]) =>
            <div key={label} className="rounded-lg border border-border bg-bg-secondary px-4 py-3"><p className="text-xs text-text-secondary">{label}</p><p className="mt-2 text-3xl font-semibold tabular-nums">{count ?? '—'}{label === '已读完部位' && <span className="ml-1 text-sm font-normal text-text-muted">/ {scan?.total_parts ?? 28}</span>}</p></div>)}
        </div>
        <section className="overflow-hidden rounded-xl border border-border bg-bg-secondary">
          <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-3"><h2 className="mr-auto text-sm font-semibold">部位统计</h2>
            <span className="text-xs text-text-secondary">{scan?.complete ? '已读完全部部位' : scan ? '本次读取进度' : '尚无记录'}</span>
            {scan?.updated_at && <span className="text-xs text-text-muted">{new Date(scan.updated_at).toLocaleString('zh-CN', { hour12: false })}</span>}
          </div>
          {parts.length ? <div className="grid grid-cols-2 gap-px bg-border/50 sm:grid-cols-3">{parts.map(([id, part]) =>
            <div key={id} className="flex items-center gap-2 bg-bg-secondary px-4 py-3"><span className="min-w-0 flex-1 truncate text-xs">{part.name}</span><strong className="text-sm font-semibold tabular-nums">{part.scanned_count}</strong>{part.complete ? <Check className="h-3.5 w-3.5 text-success" /> : <Clock className="h-3.5 w-3.5 text-text-muted" />}</div>)}</div>
            : <div className="flex flex-col items-center gap-2 px-5 py-10 text-text-muted"><Shirt className="mb-1 h-8 w-8 opacity-50" /><p className="text-sm">读取后在这里查看衣柜统计</p></div>}
          {scan?.reason && <p className="border-t border-border px-4 py-3 text-xs leading-5 text-text-secondary">{scan.reason}</p>}
          {!!scan?.unconfirmed_count && <p className="border-t border-border px-4 py-3 text-xs leading-5 text-text-secondary">{scan.unconfirmed_count} 个选中格待确认名称，候选结果已保存。</p>}
        </section>
        <div className="h-52 overflow-hidden rounded-lg border border-border"><LogsPanel instanceId={WARDROBE_INSTANCE_ID} /></div>
      </div>
      {calculatorOpened && <div className={clsx('mx-auto max-w-3xl', tab !== 'calculate' && 'hidden')}><WardrobeCalculator refreshToken={refreshToken} /></div>}
    </div>
  </div>;
}
