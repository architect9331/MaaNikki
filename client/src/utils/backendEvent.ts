import { listen } from '@tauri-apps/api/event';
import { isTauri } from '@/utils/paths';

/**
 * 订阅后端推送事件：Tauri 桌面端走 Tauri 事件，浏览器 WebUI 走 WebSocket。
 *
 * 同步返回取消订阅函数；Tauri 的 listen 是异步注册的，若在注册完成前就取消，
 * 注册完成后会立即注销，避免组件卸载后监听泄漏。
 *
 * @param tauriEvent Tauri 事件名
 * @param onTauriEvent Tauri 事件回调（参数为事件 payload）
 * @param subscribeWebUI WebUI 下的订阅函数，返回取消订阅函数
 * @param onError Tauri 监听注册失败时的回调
 */
export function subscribeBackendEvent<T>(
  tauriEvent: string,
  onTauriEvent: (payload: T) => void,
  subscribeWebUI: () => () => void,
  onError?: (err: unknown) => void,
): () => void {
  if (!isTauri()) {
    return subscribeWebUI();
  }

  let cancelled = false;
  let unlisten: (() => void) | null = null;
  void (async () => {
    try {
      const dispose = await listen<T>(tauriEvent, (event) => onTauriEvent(event.payload));
      if (cancelled) {
        dispose();
        return;
      }
      unlisten = dispose;
    } catch (err) {
      onError?.(err);
    }
  })();

  return () => {
    cancelled = true;
    unlisten?.();
    unlisten = null;
  };
}
