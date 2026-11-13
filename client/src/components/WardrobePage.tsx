import { Calculator, RefreshCw } from 'lucide-react';
import clsx from 'clsx';
import { useEffect, useState } from 'react';
import { useAccountStateStore } from '@/stores/accountStateStore';
import { openCollectionHub } from '@/stores/collectionHubStore';
import { FeaturePage } from './ui/FeaturePage';
import { WardrobeCalculator } from './WardrobeCalculator';

/** 搭配计算: the outfit calculator; reading the closet lives in 收集进度 → 我的衣柜. */
export function WardrobePage({ onClose }: { onClose: () => void }) {
  const { data, refresh, loading } = useAccountStateStore();
  const [refreshToken, setRefreshToken] = useState(0);
  useEffect(() => { void refresh().catch(() => {}); }, [refresh]);
  return <FeaturePage icon={Calculator} title="搭配计算" onBack={onClose} actions={<>
    {data?.uid && <span className="text-xs tabular-nums text-text-muted">UID {data.uid}</span>}
    <button onClick={() => void refresh().then(() => setRefreshToken((previous) => previous + 1)).catch(() => {})} disabled={loading} title="刷新衣柜记录" aria-label="刷新衣柜记录" className="rounded-lg p-2 hover:bg-bg-hover disabled:opacity-40"><RefreshCw className={clsx('h-4 w-4', loading && 'animate-spin')} /></button>
  </>}>
    <WardrobeCalculator refreshToken={refreshToken} onOpenRead={() => openCollectionHub('closet')} />
  </FeaturePage>;
}
