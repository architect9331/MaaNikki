import type { ReactNode } from 'react';
import { CircleHelp } from 'lucide-react';
import clsx from 'clsx';
import { Tooltip } from './Tooltip';

export function HelpTip({ content, label = '说明', inverted = false }: {
  content: ReactNode;
  label?: string;
  inverted?: boolean;
}) {
  if (!content) return null;
  return (
    <Tooltip content={<div className="max-h-80 overflow-y-auto text-xs leading-relaxed whitespace-pre-line [&_img]:max-w-full">{content}</div>}
      side="top" align="start" maxWidth="max-w-sm">
      <button type="button" aria-label={label}
        onClick={(event) => { event.preventDefault(); event.stopPropagation(); }}
        className={clsx('inline-flex shrink-0 items-center justify-center rounded p-0.5 cursor-help transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50',
          inverted ? 'text-white/80 hover:text-white' : 'text-text-muted hover:text-accent')}>
        <CircleHelp className="w-3.5 h-3.5" />
      </button>
    </Tooltip>
  );
}
