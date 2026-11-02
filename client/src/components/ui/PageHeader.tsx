import type { ReactNode } from 'react';
import { ArrowLeft, type LucideIcon } from 'lucide-react';

/** The shared title bar of every feature page. */
export function PageHeader({ icon: Icon, title, onBack, children }: {
  icon: LucideIcon;
  title: string;
  onBack: () => void;
  children?: ReactNode;
}) {
  return <div className="flex items-center gap-3 border-b border-border bg-bg-secondary px-5 py-3">
    <button onClick={onBack} title="返回任务列表" aria-label="返回任务列表" className="rounded-lg p-2 hover:bg-bg-hover"><ArrowLeft className="h-4 w-4" /></button>
    <Icon className="h-5 w-5 text-accent-secondary" aria-hidden="true" />
    <h1 className="flex-1 text-lg font-semibold">{title}</h1>
    {children}
  </div>;
}
