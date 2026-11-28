import { AlertCircle, Compass, Dices, Fish, Hand, Loader2, MapPin, Play, Square } from 'lucide-react';
import clsx from 'clsx';
import { useAppStore } from '@/stores/appStore';
import { EXPLORATION_INSTANCE_ID, explorationFeatures } from '@/types/exploration';
import { explorationStatusLabels, setExplorationEnabled, useExplorationStore } from '@/services/explorationService';
import { getPearpalLoginState, openPearpalLogin, usePearpalStore } from '@/services/pearpalService';
import { isTauri } from '@/utils/paths';
import { startFishing, stopFishing, useFishingStore } from '@/services/fishingService';
import { startGungunji, stopGungunji, useGungunjiStore } from '@/services/gungunjiService';
import { OptionEditor } from './OptionEditor';
import { Card, FeaturePage, primaryButton, secondaryButton, StatusBar } from './ui/FeaturePage';
import { MapMaskSection } from './settings/MapMaskSection';
import { PearpalStatusPill } from './PearpalAccount';
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

function GungunjiCard() {
  const status = useGungunjiStore((state) => state.status);
  const busy = status !== 'idle';
  return <Card icon={Dices} title="自动抽取滚滚机" aside={<button type="button" disabled={!isTauri() || status === 'stopping'}
    onClick={() => busy ? stopGungunji() : void startGungunji()} className={busy ? secondaryButton : primaryButton}>
    {status === 'starting' || status === 'stopping' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : busy ? <Square className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
    {status === 'stopping' ? '正在停止' : busy ? '停止抽取' : '开始抽取'}
  </button>}>
    <div className="space-y-3 px-4 py-3">
      <p className="text-xs leading-5 text-text-secondary">
        先站在任意一台惊喜滚滚机前（1.0 / 2.0 / 3.0 均适用）或打开滚滚机界面，再点开始。达到轮数上限、低于保留噗灵下限或奖池抽空时自动停止。
      </p>
      {['GungunjiPullMode', 'GungunjiMaxPulls', 'GungunjiReserveBling'].map((key) => <OptionEditor key={key} optionKey={key} globalScope disabled={busy} />)}
    </div>
  </Card>;
}

export function ExplorationPage({ onClose }: { onClose: () => void }) {
  const { enabled, status, error } = useExplorationStore();
  const explorationSettings = useAppStore((state) => state.explorationSettings);
  const pearpalStatus = usePearpalStore((state) => state.status);
  const pearpalBusy = usePearpalStore((state) => state.busy);
  const pearpalState = getPearpalLoginState(pearpalStatus);
  const mapMarkerEnabled =
    explorationSettings.AssistMapStar ||
    explorationSettings.AssistMapDewdrop ||
    explorationSettings.AssistMapChest ||
    explorationSettings.AssistMapBox;

  return <FeaturePage icon={Compass} title="辅助功能" onBack={onClose} actions={<PearpalStatusPill />}>
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
          <p className="border-t border-border px-4 py-3 text-xs leading-5 text-text-muted">键盘交互支持后台拾取、对白和跳过；鼠标交互、钓鱼及滚滚机在游戏前台运行，游戏最小化后暂停。</p>
        </Card>
        <GungunjiCard />
      </div>
      <div className="space-y-4">
        <FishingCard />
        <Card icon={MapPin} title="地图标记">
          {isTauri() && pearpalState !== 'logged_in' && (
            <div
              className={clsx(
                'flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-2.5 text-xs',
                pearpalState === 'expired' || mapMarkerEnabled
                  ? 'bg-warning/10 text-warning'
                  : 'bg-bg-primary/70 text-text-secondary',
              )}
            >
              <span className="flex min-w-0 flex-1 items-center gap-1.5">
                <AlertCircle className="h-3.5 w-3.5 shrink-0" />
                <span>
                  {pearpalState === 'expired'
                    ? '美鸭梨登录已过期，重新登录后地图标记才能同步最新进度'
                    : mapMarkerEnabled
                      ? '已开启地图标记，登录美鸭梨账号后即可在游戏中显示标点'
                      : '开启地图标记前需要先登录美鸭梨国服账号'}
                </span>
              </span>
              <button
                type="button"
                disabled={pearpalBusy}
                onClick={() => void openPearpalLogin({ switchAccount: pearpalState === 'expired' })}
                className={primaryButton}
              >
                {pearpalState === 'expired' ? '重新登录' : '登录美鸭梨'}
              </button>
            </div>
          )}
          <FeatureRows features={explorationFeatures.slice(4)} />
        </Card>
        <MapMaskSection />
      </div>
    </div>
    <Card><div className="h-52"><LogsPanel instanceId={EXPLORATION_INSTANCE_ID} /></div></Card>
  </FeaturePage>;
}
