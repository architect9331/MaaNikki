// SPDX-License-Identifier: AGPL-3.0-only
import { createPortal } from 'react-dom';
import {
  applyGameKeysRead, dismissGameKeysRead, startGameKeysRead, stopGameKeysRead, useGameKeysReadStore,
} from '@/services/gameKeysReadService';

const STATUS_TEXT = { idle: '', starting: '正在连接游戏…', reading: '正在读取游戏键位…', stopping: '正在停止…' } as const;

/** Reads the in-game key settings page and lets the player confirm the differences. */
export function GameKeyAutoRead() {
  const status = useGameKeysReadStore((state) => state.status);
  const result = useGameKeysReadStore((state) => state.result);
  const busy = status !== 'idle';

  return (
    <div className="rounded-lg border border-border/60 bg-bg-primary/60 p-3">
      <div className="flex flex-wrap items-center gap-3">
        <div className="min-w-0 flex-1 basis-[16rem] text-xs text-text-secondary">
          让游戏停在可以移动的主界面，软件会打开「设置 → 控制 → 键位设置」读取当前键位，读完后返回游戏并列出差异供确认。
          {busy && <span className="ml-1 text-accent">{STATUS_TEXT[status]}</span>}
        </div>
        <button
          type="button"
          disabled={status === 'stopping'}
          onClick={() => { if (busy) stopGameKeysRead(); else void startGameKeysRead(); }}
          className="shrink-0 rounded-lg border border-border px-3 py-1.5 text-sm text-accent hover:bg-bg-active disabled:opacity-50"
        >{busy ? '停止读取' : '自动读取键位'}</button>
      </div>
      {result && createPortal(
        <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/45 p-6" role="dialog" aria-modal="true" aria-label="确认读取的游戏键位">
          <div className="flex max-h-full w-full max-w-lg flex-col rounded-xl border border-border bg-bg-secondary p-5 shadow-2xl">
            <h3 className="text-base font-semibold text-text-primary">确认读取的游戏键位</h3>
            <p className="mt-1 text-xs text-text-secondary">
              {result.changes.length
                ? `有 ${result.changes.length} 项与当前设置不同${result.unchanged ? `，${result.unchanged} 项相同` : ''}。`
                : `读取到的 ${result.unchanged} 项与当前设置一致，无需修改。`}
              {!result.complete && ' 本次读取未完整完成，请核对后再应用。'}
            </p>
            <div className="mt-3 min-h-0 flex-1 space-y-3 overflow-y-auto text-sm">
              {result.changes.length > 0 && (
                <table className="w-full text-left">
                  <thead className="text-xs text-text-muted">
                    <tr><th className="py-1 font-normal">游戏中的名称</th><th className="py-1 font-normal">当前</th><th className="py-1 font-normal">读取到</th></tr>
                  </thead>
                  <tbody>
                    {result.changes.map((change) => (
                      <tr key={`${change.optionKey}.${change.name}`} className="border-t border-border/50">
                        <td className="py-1 pr-2 text-text-primary">{change.label}</td>
                        <td className="py-1 pr-2 text-text-tertiary">{change.from || '（空）'}</td>
                        <td className="py-1 font-medium text-accent">{change.to}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              {result.warnings.length > 0 && (
                <ul className="list-disc space-y-0.5 pl-5 text-xs text-warning">
                  {result.warnings.map((warning) => <li key={warning}>{warning}</li>)}
                </ul>
              )}
              {result.missing.length > 0 && (
                <p className="text-xs text-text-muted">未读取到：{result.missing.join('、')}。这些项保持原设置。</p>
              )}
            </div>
            <div className="mt-4 flex justify-end gap-2">
              <button type="button" onClick={dismissGameKeysRead}
                className="rounded-lg border border-border px-4 py-1.5 text-sm text-text-primary hover:bg-bg-active">
                {result.changes.length ? '取消' : '关闭'}
              </button>
              {result.changes.length > 0 && (
                <button type="button" onClick={() => applyGameKeysRead(result.changes)}
                  className="rounded-lg bg-accent px-4 py-1.5 text-sm text-on-accent hover:opacity-90">应用 {result.changes.length} 项</button>
              )}
            </div>
          </div>
        </div>, document.body,
      )}
    </div>
  );
}
