import { useCallback, useEffect, useState } from 'react';
import { MapPin } from 'lucide-react';
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
    <div className="bg-bg-secondary rounded-xl p-4 border border-border space-y-3">
      <div className="flex items-start gap-3">
        <MapPin className="w-5 h-5 text-accent shrink-0 mt-0.5" />
        <div className="min-w-0 flex-1 space-y-1">
          <div className="font-medium text-text-primary">开荒辅助 · 地图标记</div>
          <p className="text-xs text-text-muted">
            登录正在游玩的美鸭梨国服账号，在开荒辅助中开启奇想星、灵感露珠标记。打开大地图自动识别常用缩放并校准，返回场景后跟随小地图。
          </p>
          <p className="text-xs text-text-muted">
            首版支持奇迹大陆、万相境及 16:9 游戏窗口。粉色星形表示奇想星，蓝色水滴表示灵感露珠。收集进度在辅助运行时定期同步。
          </p>
          <p className="text-sm text-text-secondary">
            {status.has_login
              ? status.authenticated
                ? '已登录 · 已收集奇想星 ' + (status.star_collected ?? '—') + '，灵感露珠 ' + (status.dewdrop_collected ?? '—')
                : '已保存登录信息'
              : '尚未登录美鸭梨'}
          </p>
          {(error || (status.has_login && status.error)) && (
            <p role="status" className="text-xs text-red-400">{error || status.error}</p>
          )}
        </div>
      </div>
      <div className="flex gap-2 pl-8">
        <button
          type="button"
          className="px-3 py-1.5 rounded-lg text-sm bg-accent text-white disabled:opacity-50"
          disabled={busy}
          onClick={() => void run('pearpal_open_login')}
        >{status.has_login ? '重新登录' : '登录美鸭梨'}</button>
        {status.has_login && (
          <button
            type="button"
            className="px-3 py-1.5 rounded-lg text-sm border border-border text-text-secondary disabled:opacity-50"
            disabled={busy}
            onClick={() => void run('pearpal_logout')}
          >退出登录</button>
        )}
      </div>
    </div>
  );
}
