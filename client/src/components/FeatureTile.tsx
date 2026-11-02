import type { ComponentType, ReactNode } from 'react';
import { Loader2 } from 'lucide-react';
import clsx from 'clsx';
import { Tooltip } from './ui/Tooltip';
import { SwitchTrack } from './ui/SwitchTrack';

export interface FeatureTileToggle {
  checked: boolean;
  disabled?: boolean;
  label: string;
  onChange: (checked: boolean) => void;
}

/**
 * One feature entry: the same icon, name and status everywhere. Compact tiles are
 * the icon-only form shown in the top bar while the side panel is collapsed, so
 * their switch moves to the feature's own page.
 */
export function FeatureTile({ icon: Icon, title, status, tooltip, onClick, warn, busy, toggle, compact, className }: {
  icon: ComponentType<{ className?: string }>;
  title: string;
  status: string;
  tooltip?: ReactNode;
  onClick: () => void;
  warn?: boolean;
  busy?: boolean;
  toggle?: FeatureTileToggle;
  compact?: boolean;
  className?: string;
}) {
  const icon = busy
    ? <Loader2 className="h-5 w-5 shrink-0 animate-spin" aria-hidden="true" />
    : <Icon className="h-5 w-5 shrink-0" aria-hidden="true" />;
  const hint = tooltip ?? <div className="space-y-1"><p className="font-medium">{title}</p><p className="text-text-secondary">{status}</p></div>;

  if (compact) {
    return <Tooltip side="bottom" content={hint}>
      <button type="button" onClick={onClick} aria-label={`${title}：${status}`}
        className={clsx('relative flex shrink-0 items-center rounded-lg p-2 text-accent-secondary transition-colors hover:bg-bg-hover focus-visible:outline-2 focus-visible:outline-accent', className)}>
        {icon}
        {warn && <span aria-hidden="true" className="absolute right-1 top-1 h-1.5 w-1.5 rounded-full bg-warning ring-2 ring-bg-secondary" />}
      </button>
    </Tooltip>;
  }

  return <div className={clsx('relative min-w-0 rounded-xl border border-border bg-bg-secondary transition-colors hover:bg-bg-hover', className)}>
    <Tooltip side="bottom" content={hint}>
      <button type="button" onClick={onClick} aria-label={`${title}：${status}`}
        className={clsx('flex w-full min-w-0 items-center gap-2 rounded-xl px-2.5 py-2.5 text-left focus-visible:outline-2 focus-visible:outline-accent', toggle && 'pr-11')}>
        <span className="text-accent-secondary">{icon}</span>
        <span className="min-w-0">
          <span className="block truncate text-sm font-medium text-text-primary">{title}</span>
          <span className={clsx('block truncate text-xs tabular-nums', warn ? 'text-warning' : 'text-text-muted')}>{status}</span>
        </span>
      </button>
    </Tooltip>
    {toggle && <button type="button" role="switch" aria-checked={toggle.checked} aria-label={toggle.label} disabled={toggle.disabled}
      onClick={() => toggle.onChange(!toggle.checked)}
      className="absolute right-2 top-1/2 flex -translate-y-1/2 items-center rounded-lg p-1 focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-50">
      <SwitchTrack checked={toggle.checked} compact />
    </button>}
  </div>;
}
