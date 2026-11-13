import { useEffect, useRef } from 'react';
import clsx from 'clsx';

export interface MenuItem { label: string; hint?: string; danger?: boolean; disabled?: boolean; onClick: () => void }
export type MenuEntry = MenuItem | 'separator';

/** Small right-click menu positioned at the cursor; closes on outside click, Esc, wheel or resize. */
export function ContextMenu({ x, y, title, items, onClose }: {
  x: number; y: number; title?: string; items: MenuEntry[]; onClose: () => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const close = (event: Event) => {
      if (event instanceof MouseEvent && box.current?.contains(event.target as Node)) return;
      onClose();
    };
    const key = (event: KeyboardEvent) => { if (event.key === 'Escape') { event.stopPropagation(); onClose(); } };
    window.addEventListener('pointerdown', close, true);
    window.addEventListener('wheel', close, true);
    window.addEventListener('resize', close);
    window.addEventListener('keydown', key, true);
    return () => {
      window.removeEventListener('pointerdown', close, true);
      window.removeEventListener('wheel', close, true);
      window.removeEventListener('resize', close);
      window.removeEventListener('keydown', key, true);
    };
  }, [onClose]);
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    if (rect.right > window.innerWidth) el.style.left = `${Math.max(4, x - rect.width)}px`;
    if (rect.bottom > window.innerHeight) el.style.top = `${Math.max(4, y - rect.height)}px`;
  }, [x, y]);

  return <div ref={box} role="menu" style={{ left: x, top: y }} onContextMenu={(e) => e.preventDefault()}
    className="fixed z-50 min-w-44 rounded-xl border border-border bg-bg-secondary p-1 text-xs shadow-xl">
    {title && <p className="px-2.5 pb-1 pt-1.5 text-[11px] font-medium text-text-muted">{title}</p>}
    {items.map((item, index) => item === 'separator'
      ? <div key={index} className="mx-2 my-1 h-px bg-border" />
      : <button key={index} type="button" role="menuitem" disabled={item.disabled}
        onClick={() => { onClose(); item.onClick(); }}
        className={clsx('flex w-full items-center justify-between gap-4 rounded-lg px-2.5 py-1.5 text-left disabled:opacity-40 disabled:hover:bg-transparent',
          item.danger ? 'text-error hover:bg-error/10' : 'hover:bg-bg-hover')}>
        <span>{item.label}</span>{item.hint && <kbd className="text-[10px] text-text-muted">{item.hint}</kbd>}
      </button>)}
  </div>;
}
