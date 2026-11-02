import { useRef } from 'react';
import { AlertTriangle } from 'lucide-react';
import { ModalPortal, useModalFocusTrap } from '@/components/ui/Modal';
import type { PreflightIssue } from '@/utils/preflight';

export function PreflightDialog({ issues, onCancel, onContinue }: {
  issues: PreflightIssue[]; onCancel: () => void; onContinue: () => void;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  useModalFocusTrap({ containerRef: panelRef, onEscape: onCancel });
  return <ModalPortal>
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4 backdrop-blur-sm">
      <div ref={panelRef} role="dialog" aria-modal="true" aria-labelledby="preflight-title"
        className="flex max-h-[80vh] w-full max-w-md flex-col rounded-xl border border-border bg-bg-secondary shadow-xl">
        <h2 id="preflight-title" className="flex items-center gap-2 border-b border-border px-5 py-3.5 text-base font-semibold text-text-primary">
          <AlertTriangle className="h-5 w-5 text-warning" />开始前请确认
        </h2>
        <ul className="flex-1 space-y-3 overflow-y-auto px-5 py-4">
          {issues.map((issue) => <li key={issue.id}>
            <p className="text-sm font-medium text-text-primary">{issue.title}</p>
            <p className="mt-0.5 text-xs leading-5 text-text-secondary">{issue.detail}</p>
            {issue.once && <p className="mt-1 text-xs text-text-muted">这项只提醒一次，点「继续运行」后不再出现。</p>}
          </li>)}
        </ul>
        <div className="flex gap-2 border-t border-border px-5 py-3.5">
          <button type="button" onClick={onCancel}
            className="flex-1 rounded-lg bg-bg-tertiary px-4 py-2 text-sm font-medium text-text-secondary hover:bg-bg-hover">先去处理</button>
          <button type="button" onClick={onContinue}
            className="flex-1 rounded-lg bg-accent px-4 py-2 text-sm font-medium text-on-accent hover:bg-accent-hover">继续运行</button>
        </div>
      </div>
    </div>
  </ModalPortal>;
}
