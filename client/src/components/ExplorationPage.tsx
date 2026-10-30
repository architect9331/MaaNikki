import { ArrowLeft, Compass, MapPin, Hand } from 'lucide-react';
import clsx from 'clsx';
import { useAppStore } from '@/stores/appStore';
import { EXPLORATION_INSTANCE_ID, explorationFeatures } from '@/types/exploration';
import { explorationStatusLabels, useExplorationStore } from '@/services/explorationService';
import { ExplorationControl } from './ExplorationControl';
import { MapMaskSection } from './settings/MapMaskSection';
import { LogsPanel } from './LogsPanel';
import { SwitchTrack } from './ui/SwitchTrack';

export function ExplorationPage({ onClose }: { onClose: () => void }) {
  const settings = useAppStore((state) => state.explorationSettings);
  const setFeature = useAppStore((state) => state.setExplorationFeature);
  const { status, error } = useExplorationStore();
  return <div className="flex min-h-0 flex-1 flex-col bg-bg-primary text-text-primary">
    <div className="flex items-center gap-3 border-b border-border bg-bg-secondary px-5 py-3">
      <button onClick={onClose} className="rounded-md p-2 hover:bg-bg-hover" aria-label="返回任务列表"><ArrowLeft className="h-4 w-4" /></button>
      <Compass className="h-5 w-5 text-accent" />
      <h1 className="flex-1 text-lg font-semibold">辅助功能</h1>
      <ExplorationControl showSettings={false} />
    </div>
    <div className="min-h-0 flex-1 overflow-auto p-4">
      <div className="mx-auto max-w-3xl space-y-4">
        <div className="flex items-center gap-3 rounded-lg border border-border bg-bg-secondary px-4 py-3">
          <span className={clsx('h-2 w-2 shrink-0 rounded-full', status === 'running' ? 'bg-success' : status === 'error' ? 'bg-warning' : 'bg-text-muted')} />
          <div className="space-y-1">
            <p className="text-sm font-medium" role="status">{explorationStatusLabels[status]}</p>
            <p className="text-xs leading-5 text-text-secondary">日常运行时自动暂停，结束后继续。设置自动保存，软件启动后手动开启辅助。</p>
          </div>
        </div>
        {error && <p role="alert" className="rounded-lg border border-error/30 bg-error/5 p-3 text-sm text-error">{error}</p>}
        {[{ title: '交互辅助', icon: Hand, features: explorationFeatures.slice(0, 4) },
          { title: '地图标记', icon: MapPin, features: explorationFeatures.slice(4) }].map(({ title, icon: Icon, features }) =>
          <section key={title} className="overflow-hidden rounded-xl border border-border bg-bg-secondary">
            <h2 className="flex items-center gap-2 border-b border-border px-4 py-3 text-sm font-semibold"><Icon className="h-4 w-4 text-accent" />{title}</h2>
            <div className="divide-y divide-border">
              {features.map(({ key, label, description }) => <label key={key} className="flex cursor-pointer items-center gap-5 px-4 py-3.5 hover:bg-bg-hover/50">
                <div className="flex-1 space-y-1"><span className="text-sm font-medium">{label}</span><p className="text-xs leading-5 text-text-secondary">{description}</p></div>
                <input type="checkbox" role="switch" className="peer sr-only" checked={settings[key]} onChange={(event) => setFeature(key, event.target.checked)} />
                <SwitchTrack checked={settings[key]} />
              </label>)}
            </div>
            {title === '交互辅助' && <p className="border-t border-border px-4 py-3 text-xs leading-5 text-text-muted">键盘交互支持后台拾取、对白和跳过；鼠标交互及钓鱼在游戏前台运行，游戏最小化后暂停。</p>}
          </section>)}
        <MapMaskSection />
        <details className="overflow-hidden rounded-xl border border-border bg-bg-secondary">
          <summary className="cursor-pointer px-4 py-3 text-sm font-medium">运行日志</summary>
          <div className="h-64"><LogsPanel instanceId={EXPLORATION_INSTANCE_ID} /></div>
        </details>
      </div>
    </div>
  </div>;
}
