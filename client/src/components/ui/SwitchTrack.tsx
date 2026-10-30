import clsx from 'clsx';

export function SwitchTrack({ checked, compact = false }: { checked: boolean; compact?: boolean }) {
  return <span aria-hidden="true" className={clsx(
    'relative inline-block shrink-0 rounded-full transition-colors peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-accent',
    compact ? 'h-4 w-7' : 'h-5 w-9', checked ? 'bg-accent' : 'bg-text-muted/35',
  )}>
    <span className={clsx('absolute left-0.5 top-0.5 rounded-full bg-white shadow-sm transition-transform',
      compact ? 'h-3 w-3' : 'h-4 w-4', checked ? compact ? 'translate-x-3' : 'translate-x-4' : 'translate-x-0')} />
  </span>;
}
