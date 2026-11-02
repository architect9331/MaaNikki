import { Calculator, Check, Clock, RefreshCw, Play, Square, Shirt } from 'lucide-react';
import clsx from 'clsx';
import { useEffect, useState } from 'react';
import { useAccountStateStore } from '@/stores/accountStateStore';
import { useWardrobeStore, startWardrobeRead, stopWardrobeRead, wardrobeLabels } from '@/services/wardrobeService';
import { isTauri } from '@/utils/paths';
import { LogsPanel } from './LogsPanel';
import { Card, FeaturePage, primaryButton, secondaryButton, Stat, StatusBar } from './ui/FeaturePage';
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
  const tone = error || accountError ? 'warn' : busy ? 'busy' : scan?.complete ? 'ok' : 'idle';
  return <FeaturePage icon={Shirt} title="衣柜与搭配" onBack={onClose} actions={<>
    {data?.uid && <span className="text-xs tabular-nums text-text-muted">UID {data.uid}</span>}
    <button onClick={() => void refresh().then(() => setRefreshToken((previous) => previous + 1)).catch(() => {})} disabled={loading} title="刷新衣柜记录" aria-label="刷新衣柜记录" className="rounded-lg p-2 hover:bg-bg-hover disabled:opacity-40"><RefreshCw className={clsx('h-4 w-4', loading && 'animate-spin')} /></button>
  </>}>
    <div role="tablist" aria-label="衣柜功能" className="grid grid-cols-2 gap-2">
      {([['read', Shirt, '衣柜读取', '读取并记录你拥有的衣服'], ['calculate', Calculator, '搭配计算', '算出最高分搭配和要养成什么']] as const).map(([id, Icon, label, detail]) =>
        <button key={id} type="button" role="tab" aria-selected={tab === id} onClick={() => { setTab(id); if (id === 'calculate') setCalculatorOpened(true); }}
          className={clsx('rounded-xl border p-3 text-left transition-colors', tab === id ? 'border-accent bg-accent/10' : 'border-border bg-bg-secondary hover:bg-bg-hover')}>
          <span className="flex items-center gap-2 text-sm font-semibold"><Icon className={clsx('h-4 w-4', tab === id ? 'text-accent' : 'text-text-muted')} />{label}</span>
          <span className="mt-1 block text-xs text-text-secondary">{detail}</span>
        </button>)}
    </div>
    <div className={clsx('space-y-4', tab !== 'read' && 'hidden')}>
      <StatusBar tone={tone} title={wardrobeLabels[status]} detail={error || accountError || '进入游戏主界面并清空衣柜筛选后开始，读取期间保持游戏在前台。中断后可继续，再次读取会沿用已有记录并识别新增衣服。'}>
        <button disabled={!isTauri() || status === 'stopping'} onClick={() => busy ? stopWardrobeRead() : void startWardrobeRead()} className={busy ? secondaryButton : primaryButton}>
          {busy ? <Square className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}{busy ? '停止读取' : scan ? scan.complete ? '更新读取' : '继续读取' : '开始读取'}
        </button>
      </StatusBar>
      <div className="grid grid-cols-3 gap-3">
        <Stat label="已读取衣服" value={scan?.scanned_count ?? '—'} />
        <Stat label="已读完部位" value={<>{scan?.completed_parts ?? '—'}<span className="ml-1 text-sm font-normal text-text-muted">/ {scan?.total_parts ?? 28}</span></>} />
        <Stat label="同图候选条目" value={scan?.ambiguous_count ?? '—'} />
      </div>
      <Card title="部位统计" aside={<>
        <span className="text-xs text-text-secondary">{scan?.complete ? '已读完全部部位' : scan ? '本次读取进度' : '尚无记录'}</span>
        {scan?.updated_at && <span className="text-xs text-text-muted">{new Date(scan.updated_at).toLocaleString('zh-CN', { hour12: false })}</span>}
      </>}>
        {parts.length ? <div className="grid grid-cols-2 gap-px bg-border/50 sm:grid-cols-4">{parts.map(([id, part]) =>
          <div key={id} className="flex items-center gap-2 bg-bg-secondary px-4 py-3"><span className="min-w-0 flex-1 truncate text-xs">{part.name}</span><strong className="text-sm font-semibold tabular-nums">{part.scanned_count}</strong>{part.complete ? <Check className="h-3.5 w-3.5 text-success" /> : <Clock className="h-3.5 w-3.5 text-text-muted" />}</div>)}</div>
          : <div className="flex flex-col items-center gap-2 px-5 py-10 text-text-muted"><Shirt className="mb-1 h-8 w-8 opacity-50" /><p className="text-sm">读取后在这里查看衣柜统计</p></div>}
        {scan?.reason && <p className="border-t border-border px-4 py-3 text-xs leading-5 text-text-secondary">{scan.reason}</p>}
        {!!scan?.unconfirmed_count && <p className="border-t border-border px-4 py-3 text-xs leading-5 text-text-secondary">{scan.unconfirmed_count} 个选中格待确认名称，候选结果已保存。</p>}
      </Card>
      <Card><div className="h-52"><LogsPanel instanceId={WARDROBE_INSTANCE_ID} /></div></Card>
    </div>
    {calculatorOpened && <div className={clsx(tab !== 'calculate' && 'hidden')}><WardrobeCalculator refreshToken={refreshToken} onOpenRead={() => setTab('read')} /></div>}
  </FeaturePage>;
}
