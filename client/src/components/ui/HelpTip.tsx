import type { ReactNode } from 'react';
import { CircleHelp } from 'lucide-react';
import clsx from 'clsx';
import { Tooltip } from './Tooltip';

/**
 * Details stay out of the way: the icon appears when the option row is hovered,
 * and with `children` the label itself also opens the details.
 */
export function HelpTip({ content, label = '说明', inverted = false, children }: {
  content: ReactNode;
  label?: string;
  inverted?: boolean;
  children?: ReactNode;
}) {
  if (!content) return <>{children}</>;
  const icon = <CircleHelp className="w-3.5 h-3.5 shrink-0" />;
  const body = <div className="max-h-80 overflow-y-auto text-xs leading-relaxed whitespace-pre-line [&_img]:max-w-full">{content}</div>;
  const reveal = 'opacity-0 transition-opacity group-hover/option:opacity-100 focus-visible:opacity-100 [@media(hover:none)]:opacity-100';
  if (children) {
    return (
      <Tooltip content={body} side="top" align="start" maxWidth="max-w-sm">
        <span tabIndex={0} aria-label={label}
          onClick={(event) => event.preventDefault()}
          className="inline-flex min-w-0 items-center gap-1 cursor-help rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50">
          {children}
          <span className={clsx('text-text-muted', reveal, 'group-focus-within/option:opacity-100')}>{icon}</span>
        </span>
      </Tooltip>
    );
  }
  return (
    <Tooltip content={body} side="top" align="start" maxWidth="max-w-sm">
      <button type="button" aria-label={label}
        onClick={(event) => { event.preventDefault(); event.stopPropagation(); }}
        className={clsx('inline-flex shrink-0 items-center justify-center rounded p-0.5 cursor-help focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50',
          reveal, inverted ? 'text-white/80 hover:text-white' : 'text-text-muted hover:text-accent')}>
        {icon}
      </button>
    </Tooltip>
  );
}
