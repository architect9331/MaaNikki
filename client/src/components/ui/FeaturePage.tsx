import type { ReactNode } from 'react';
import { ArrowLeft } from 'lucide-react';
import clsx from 'clsx';

type IconType = React.ComponentType<{ className?: string }>;

/** Every feature page has the same title bar and content width. */
export function FeaturePage({ icon: Icon, title, onBack, actions, children }: {
  icon: IconType;
  title: string;
  onBack: () => void;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return <div className="flex min-h-0 flex-1 flex-col bg-bg-primary text-text-primary">
    <div className="flex items-center gap-3 border-b border-border bg-bg-secondary px-5 py-2.5">
      <button onClick={onBack} title="返回任务列表" aria-label="返回任务列表" className="rounded-lg p-2 hover:bg-bg-hover"><ArrowLeft className="h-4 w-4" /></button>
      <Icon className="h-5 w-5 text-accent-secondary" />
      <h1 className="mr-auto text-lg font-semibold">{title}</h1>
      {actions}
    </div>
    <div className="min-h-0 flex-1 overflow-auto p-5">
      <div className="mx-auto max-w-5xl space-y-4">{children}</div>
    </div>
  </div>;
}

const TONE_DOT = { ok: 'bg-success', warn: 'bg-warning', idle: 'bg-text-muted', busy: 'bg-accent animate-pulse' };

/** The strip that opens a page: where things stand, and the one thing to do next. */
export function StatusBar({ tone = 'idle', title, detail, children }: {
  tone?: keyof typeof TONE_DOT;
  title: ReactNode;
  detail?: ReactNode;
  children?: ReactNode;
}) {
  return <section className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-xl border border-border bg-bg-secondary px-4 py-3">
    <span aria-hidden="true" className={clsx('h-2 w-2 shrink-0 rounded-full', TONE_DOT[tone])} />
    <div className="min-w-48 flex-1">
      <p className="text-sm font-medium" role="status">{title}</p>
      {detail && <p className="mt-0.5 text-xs leading-5 text-text-secondary">{detail}</p>}
    </div>
    {children && <div className="flex shrink-0 items-center gap-2">{children}</div>}
  </section>;
}

export function Card({ icon: Icon, title, aside, className, children }: {
  icon?: IconType;
  title?: ReactNode;
  aside?: ReactNode;
  className?: string;
  children: ReactNode;
}) {
  return <section className={clsx('overflow-hidden rounded-xl border border-border bg-bg-secondary', className)}>
    {title && <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-3">
      {Icon && <Icon className="h-4 w-4 text-accent-secondary" />}
      <h2 className="mr-auto text-sm font-semibold">{title}</h2>
      {aside}
    </div>}
    {children}
  </section>;
}

export function Stat({ label, value, detail, warn }: { label: string; value: ReactNode; detail?: ReactNode; warn?: boolean }) {
  return <div className="rounded-xl border border-border bg-bg-secondary px-4 py-3">
    <p className="text-xs text-text-secondary">{label}</p>
    <p className={clsx('mt-1 text-xl font-semibold tabular-nums', warn && 'text-warning')}>{value}</p>
    {detail && <p className="mt-0.5 text-xs leading-5 text-text-muted">{detail}</p>}
  </div>;
}

export function Segmented<T extends string>({ value, options, onChange, label }: {
  value: T;
  options: readonly (readonly [T, string])[];
  onChange: (value: T) => void;
  label: string;
}) {
  return <div role="group" aria-label={label} className="inline-flex shrink-0 rounded-lg border border-border bg-bg-secondary p-0.5">
    {options.map(([id, text]) => <button key={id} type="button" onClick={() => onChange(id)} aria-pressed={value === id}
      className={clsx('rounded-md px-3 py-1 text-xs transition-colors', value === id ? 'bg-accent/10 font-medium text-accent' : 'text-text-secondary hover:bg-bg-hover')}>{text}</button>)}
  </div>;
}

export const primaryButton = 'inline-flex items-center gap-1.5 rounded-lg bg-accent px-3 py-1.5 text-xs font-medium text-on-accent hover:bg-accent-hover disabled:opacity-40';
export const secondaryButton = 'inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-xs hover:bg-bg-hover disabled:opacity-40';
