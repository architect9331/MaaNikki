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
    <div className="sticky top-0 z-20 flex items-center gap-3 border-b border-border/80 bg-bg-secondary/90 px-5 py-3 backdrop-blur-md">
      <button onClick={onBack} title="返回任务列表" aria-label="返回任务列表" className="rounded-xl border border-transparent p-2 transition-colors hover:border-border hover:bg-bg-hover"><ArrowLeft className="h-4 w-4" /></button>
      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-xl bg-accent/12 text-accent ring-1 ring-accent/20">
        <Icon className="h-4 w-4" />
      </span>
      <h1 className="mr-auto text-base font-semibold tracking-tight sm:text-lg">{title}</h1>
      {actions}
    </div>
    <div className="min-h-0 flex-1 overflow-auto p-5">
      <div className="mx-auto max-w-6xl space-y-4">{children}</div>
    </div>
  </div>;
}

const TONE_DOT = {
  ok: 'bg-success ring-4 ring-success/15',
  warn: 'bg-warning ring-4 ring-warning/15',
  idle: 'bg-text-muted ring-4 ring-text-muted/15',
  busy: 'bg-accent animate-pulse ring-4 ring-accent/20',
};

/** The strip that opens a page: where things stand, and the one thing to do next. */
export function StatusBar({ tone = 'idle', title, detail, children }: {
  tone?: keyof typeof TONE_DOT;
  title: ReactNode;
  detail?: ReactNode;
  children?: ReactNode;
}) {
  return <section className="flex flex-wrap items-center gap-x-3.5 gap-y-2 rounded-2xl border border-border/90 bg-bg-secondary px-4 py-3 shadow-xs">
    <span aria-hidden="true" className={clsx('ml-0.5 h-2 w-2 shrink-0 rounded-full', TONE_DOT[tone])} />
    <div className="min-w-48 flex-1">
      <p className="text-sm font-medium" role="status">{title}</p>
      {detail && <p className="mt-0.5 text-xs leading-5 text-text-secondary">{detail}</p>}
    </div>
    {children && <div className="flex shrink-0 flex-wrap items-center gap-2">{children}</div>}
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
  return <div role="group" aria-label={label} className="inline-flex shrink-0 rounded-xl border border-border bg-bg-primary/80 p-0.5">
    {options.map(([id, text]) => <button key={id} type="button" onClick={() => onChange(id)} aria-pressed={value === id}
      className={clsx('rounded-lg px-3 py-1 text-xs transition-all', value === id ? 'bg-bg-secondary font-medium text-accent shadow-xs ring-1 ring-border/70' : 'text-text-secondary hover:text-text-primary')}>{text}</button>)}
  </div>;
}

export interface PageTab<T extends string> {
  id: T;
  icon: IconType;
  label: string;
  /** Where this tab stands right now, e.g. “134 / 320 套”. */
  value?: ReactNode;
  /** 0–1; draws a thin bar under the tab. */
  progress?: number | null;
}

/** Page-level tabs: each one names an area and shows where it stands. */
export function Tabs<T extends string>({ label, value, tabs, onChange }: {
  label: string;
  value: T;
  tabs: readonly PageTab<T>[];
  onChange: (value: T) => void;
}) {
  const move = (step: number) => {
    const index = tabs.findIndex((tab) => tab.id === value);
    const next = tabs[(index + step + tabs.length) % tabs.length];
    onChange(next.id);
    requestAnimationFrame(() => document.getElementById(`page-tab-${next.id}`)?.focus());
  };
  return <div role="tablist" aria-label={label} className={clsx('grid grid-cols-2 gap-1.5 rounded-2xl border border-border bg-bg-secondary p-1.5 shadow-xs', tabs.length >= 6 ? 'sm:grid-cols-3 lg:grid-cols-6' : tabs.length >= 5 ? 'sm:grid-cols-5' : 'sm:grid-cols-4')}
    onKeyDown={(event) => {
      if (event.key === 'ArrowRight') { event.preventDefault(); move(1); }
      if (event.key === 'ArrowLeft') { event.preventDefault(); move(-1); }
    }}>
    {tabs.map(({ id, icon: Icon, label: text, value: status, progress }) => {
      const active = value === id;
      return <button key={id} id={`page-tab-${id}`} type="button" role="tab" aria-selected={active} tabIndex={active ? 0 : -1}
        onClick={() => onChange(id)}
        className={clsx('min-w-0 rounded-xl px-3 py-2 text-left transition-all', active ? 'bg-accent/12 shadow-xs ring-1 ring-accent/25' : 'hover:bg-bg-hover')}>
        <span className="flex items-center gap-2">
          <Icon className={clsx('h-4 w-4 shrink-0', active ? 'text-accent' : 'text-text-muted')} />
          <span className={clsx('truncate text-sm font-semibold', active ? 'text-accent' : 'text-text-primary')}>{text}</span>
          {status != null && <span className="ml-auto shrink-0 text-xs tabular-nums text-text-secondary">{status}</span>}
        </span>
        <span className="mt-1.5 block h-1 overflow-hidden rounded-full bg-border/70" aria-hidden="true">
          {progress != null && <span className={clsx('block h-full rounded-full', progress >= 1 ? 'bg-success' : active ? 'bg-accent' : 'bg-accent/50')}
            style={{ width: `${Math.max(0, Math.min(1, progress)) * 100}%` }} />}
        </span>
      </button>;
    })}
  </div>;
}

/** A compact figure inside a card; several sit side by side. */
export function Tile({ label, value, detail, warn }: { label: ReactNode; value: ReactNode; detail?: ReactNode; warn?: boolean }) {
  return <div className="min-w-0 rounded-xl border border-border/70 bg-bg-primary/75 px-3.5 py-2.5">
    <p className="truncate text-[11px] text-text-secondary">{label}</p>
    <p className={clsx('mt-0.5 truncate text-lg font-semibold tabular-nums tracking-tight', warn && 'text-warning')}>{value}</p>
    {detail && <p className="mt-0.5 text-[11px] leading-4 text-text-muted">{detail}</p>}
  </div>;
}

export interface Notice { tone: 'info' | 'ok' | 'warn' | 'error'; text: ReactNode }
const NOTICE_DOT = { info: 'bg-accent', ok: 'bg-success', warn: 'bg-warning', error: 'bg-error' };
const NOTICE_TEXT = { info: 'text-text-secondary', ok: 'text-text-secondary', warn: 'text-warning', error: 'text-error' };

/** Everything worth knowing about the data on this page, in one place. */
export function Notices({ items }: { items: Notice[] }) {
  if (!items.length) return null;
  return <ul className="space-y-1.5 rounded-xl border border-border bg-bg-secondary px-4 py-2.5">
    {items.map((item, index) => <li key={index} role={item.tone === 'error' ? 'alert' : undefined}
      className={clsx('flex items-start gap-2 text-xs leading-5', NOTICE_TEXT[item.tone])}>
      <span aria-hidden="true" className={clsx('mt-[7px] h-1.5 w-1.5 shrink-0 rounded-full', NOTICE_DOT[item.tone])} />
      <span className="min-w-0">{item.text}</span>
    </li>)}
  </ul>;
}

export const primaryButton ='inline-flex items-center gap-1.5 rounded-lg bg-accent px-3 py-1.5 text-xs font-medium text-on-accent hover:bg-accent-hover disabled:opacity-40';
export const secondaryButton = 'inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-xs hover:bg-bg-hover disabled:opacity-40';
