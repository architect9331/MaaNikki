import { Compass, Fish, Hand, Loader2, MapPin, Play, Square } from 'lucide-react';
import { useAppStore } from '@/stores/appStore';
import { EXPLORATION_INSTANCE_ID, explorationFeatures } from '@/types/exploration';
import { explorationStatusLabels, setExplorationEnabled, useExplorationStore } from '@/services/explorationService';
import { isTauri } from '@/utils/paths';
import { startFishing, stopFishing, useFishingStore } from '@/services/fishingService';
import { OptionEditor } from './OptionEditor';
import { Card, FeaturePage, primaryButton, secondaryButton, StatusBar } from './ui/FeaturePage';
import { MapMaskSection } from './settings/MapMaskSection';
import { LogsPanel } from './LogsPanel';
import { SwitchTrack } from './ui/SwitchTrack';

function FeatureRows({ features }: { features: readonly (typeof explorationFeatures)[number][] }) {
  const settings = useAppStore((state) => state.explorationSettings);
  const setFeature = useAppStore((state) => state.setExplorationFeature);
  return <div className="divide-y divide-border">
    {features.map(({ key, label, description }) => <label key={key} className="flex cursor-pointer items-center gap-5 px-4 py-3 hover:bg-bg-hover/50">
      <div className="flex-1 space-y-0.5"><span className="text-sm font-medium">{label}</span><p className="text-xs leading-5 text-text-secondary">{description}</p></div>
      <input type="checkbox" role="switch" className="peer sr-only" checked={settings[key]} onChange={(event) => setFeature(key, event.target.checked)} />
      <SwitchTrack checked={settings[key]} />
    </label>)}
  </div>;
}

function FishingCard() {
  const status = useFishingStore((state) => state.status);
  const busy = status !== 'idle';
  return <Card icon={Fish} title="当前位置钓鱼" aside={<button type="button" disabled={!isTauri() || status === 'stopping'}
    onClick={() => busy ? stopFishing() : void startFishing()} className={busy ? secondaryButton : primaryButton}>
    {status === 'starting' || status === 'stopping' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : busy ? <Square className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
    {status === 'stopping' ? '正在停止' : busy ? '停止钓鱼' : '开始钓鱼'}
  </button>}>
    <div className="space-y-3 px-4 py-3">
      <p className="text-xs leading-5 text-text-secondary">先站在钓点面向水面，再点开始。软件会切回游戏，准备能力并自动抛竿，鱼钓光或到达次数后结束。</p>
      {['FishingCastCount', 'FishingAbilityPlan', 'FishingRhythms'].map((key) => <OptionEditor key={key} optionKey={key} globalScope disabled={busy} />)}
    </div>
  </Card>;
}

export function ExplorationPage({ onClose }: { onClose: () => void }) {
  const { enabled, status, error } = useExplorationStore();
  return <FeaturePage icon={Compass} title="辅助功能" onBack={onClose}>
    <StatusBar tone={status === 'error' ? 'warn' : status === 'running' ? 'ok' : status === 'off' ? 'idle' : 'busy'} title={explorationStatusLabels[status]}
      detail={error || '日常运行时自动暂停，结束后继续。设置自动保存，软件启动后手动开启辅助。'}>
      <label className="flex cursor-pointer items-center gap-2 text-xs text-text-secondary">
        启用辅助功能
        <input type="checkbox" role="switch" className="peer sr-only" disabled={!isTauri()} checked={enabled} onChange={(event) => setExplorationEnabled(event.target.checked)} />
        <SwitchTrack checked={enabled} />
      </label>
    </StatusBar>
    <div className="grid items-start gap-4 lg:grid-cols-2">
      <div className="space-y-4">
        <Card icon={Hand} title="交互辅助">
          <FeatureRows features={explorationFeatures.slice(0, 4)} />
          <p className="border-t border-border px-4 py-3 text-xs leading-5 text-text-muted">键盘交互支持后台拾取和对白；清洁跳过、鼠标交互及钓鱼在游戏前台运行，游戏最小化后暂停。</p>
        </Card>
      </div>
      <div className="space-y-4">
        <FishingCard />
        <Card icon={MapPin} title="地图标记"><FeatureRows features={explorationFeatures.slice(4)} /></Card>
        <MapMaskSection />
      </div>
    </div>
    <Card><div className="h-52"><LogsPanel instanceId={EXPLORATION_INSTANCE_ID} /></div></Card>
  </FeaturePage>;
}
