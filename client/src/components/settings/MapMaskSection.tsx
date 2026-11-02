import { useCallback, useEffect, useState } from 'react';
import { Card, primaryButton, secondaryButton } from '@/components/ui/FeaturePage';
import { invoke } from '@tauri-apps/api/core';
import { isTauri } from '@/utils/windowUtils';

type Status = {
  has_login?: boolean;
  authenticated?: boolean;
  star_collected?: number;
  dewdrop_collected?: number;
  error?: string;
};

export function MapMaskSection() {
  const [status, setStatus] = useState<Status>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const refresh = useCallback(async () => {
    try {
      setStatus(await invoke<Status>('pearpal_status'));
    } catch (reason) {
      setError(String(reason));
    }
  }, []);

  useEffect(() => {
    if (!isTauri()) return;
    let disposed = false;
    let unlisten: (() => void) | undefined;
    void import('@tauri-apps/api/event').then(async ({ listen }) => {
      const off = await listen<string>('pearpal-login-error', ({ payload }) => setError(payload));
      if (disposed) off();
      else unlisten = off;
    }).catch(() => {});
    void refresh();
    const timer = window.setInterval(() => void refresh(), 2000);
    return () => {
      disposed = true;
      unlisten?.();
      window.clearInterval(timer);
    };
  }, [refresh]);

  if (!isTauri()) return null;

  async function run(command: string) {
    setBusy(true);
    setError('');
    try {
      await invoke(command);
      await refresh();
    } catch (reason) {
      setError(String(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="美鸭梨账号" aside={<span className="text-xs text-text-secondary">{status.has_login
      ? status.authenticated ? '已登录' : '已保存登录信息' : '尚未登录'}</span>}>
      <div className="space-y-3 px-4 py-3">
        <p className="text-xs leading-5 text-text-secondary">
          登录正在游玩的国服账号后可使用地图标记：打开大地图自动校准，返回场景后跟随小地图。支持奇迹大陆、万相境和 16:9 游戏窗口，粉色星形是奇想星，蓝色水滴是灵感露珠。
        </p>
        {status.has_login && status.authenticated && (
          <p className="text-xs tabular-nums text-text-secondary">已收集奇想星 {status.star_collected ?? '—'}，灵感露珠 {status.dewdrop_collected ?? '—'}</p>
        )}
        {(error || (status.has_login && status.error)) && (
          <p role="status" className="text-xs text-error">{error || status.error}</p>
        )}
        <div className="flex gap-2">
          <button type="button" className={primaryButton} disabled={busy} onClick={() => void run('pearpal_open_login')}>
            {status.has_login ? '重新登录' : '登录美鸭梨'}
          </button>
          {status.has_login && (
            <button type="button" className={secondaryButton} disabled={busy} onClick={() => void run('pearpal_logout')}>退出登录</button>
          )}
        </div>
      </div>
    </Card>
  );
}
