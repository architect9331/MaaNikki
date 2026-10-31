import { create } from 'zustand';
import { toast } from 'sonner';
import { useAppStore } from '@/stores/appStore';
import { EXPLORATION_INSTANCE_ID as instanceId, explorationFeatures } from '@/types/exploration';
import { normalizeAgentConfigs, type SelectedTask } from '@/types/interface';
import { buildDesktopWindowControllerConfig, getDesktopWindowFilters } from '@/utils/controller';
import { computeResourcePaths } from '@/utils/resourcePath';
import { generateTaskPipelineOverride } from '@/utils/pipelineOverride';
import { buildPiEnvVars } from '@/utils/piEnv';
import { getInterfaceLangKey } from '@/i18n';
import { isTauri } from '@/utils/paths';
import { loggers } from '@/utils/logger';
import { startGlobalCallbackListener, waitForCtrlResult, waitForResResult } from '@/components/connection/callbackCache';
import { maaService } from './maaService';
import { stopInstanceTasks } from './taskStopService';

type Status = 'off' | 'starting' | 'running' | 'paused' | 'stopping' | 'error';
export const useExplorationStore = create<{
  enabled: boolean;
  status: Status;
  error: string;
}>(() => ({ enabled: false, status: 'off', error: '' }));

export const explorationStatusLabels: Record<Status, string> = {
  off: '已关闭', starting: '启动中', running: '运行中', paused: '日常期间暂停', stopping: '停止中', error: '启动或运行失败',
};

let queue: Promise<unknown> = Promise.resolve();
let dailyReservations = 0;
let created = false;
let active = false;
let needsStop = false;
let appliedSettings = '';
let resourceKey = '';
let revision = 0;

function serialize<T>(work: () => Promise<T>): Promise<T> {
  const result = queue.then(work);
  queue = result.catch(() => {});
  return result;
}

function dailyBusy() {
  return dailyReservations > 0 || useAppStore.getState().instances.some((instance) => instance.isRunning);
}

async function stop(destroy = false) {
  if (!created) return;
  // Await the Python action's finally blocks (input release and overlay close)
  // before allowing a daily controller to acquire the window.
  if (needsStop && !await stopInstanceTasks(instanceId)) throw new Error('辅助功能停止超时，请稍后重试。');
  active = false;
  needsStop = false;
  appliedSettings = '';
  if (destroy) {
    await maaService.destroyInstance(instanceId);
    created = false;
    resourceKey = '';
  }
}

class Superseded extends Error {}

async function reconcile() {
  const desired = useExplorationStore.getState();
  if (!desired.enabled) {
    await stop(true);
    useExplorationStore.setState({ status: 'off' });
    return;
  }
  if (dailyBusy()) {
    await stop();
    useExplorationStore.setState({ status: 'paused' });
    return;
  }
  const backend = await maaService.getAllStates();
  if (!backend) throw new Error('无法读取任务运行状态。');
  if (Object.entries(backend.instances).some(([id, state]) => id !== instanceId && state.isRunning)) {
    await stop();
    useExplorationStore.setState({ status: 'paused' });
    return;
  }
  const state = useAppStore.getState();
  const signature = JSON.stringify([state.explorationSettings, state.globalOptionValues]);
  if (active && !backend.instances[instanceId]?.isRunning) {
    const result = backend.instances[instanceId]?.taskRunState;
    if (result?.overallStatus === 'Succeeded' || result?.statuses[instanceId] === 'succeeded') {
      revision++;
      useExplorationStore.setState({ enabled: false, status: 'stopping', error: '' });
      await stop(true);
      useExplorationStore.setState({ status: 'off' });
      return;
    }
    throw new Error('辅助功能已结束，请查看日志后重新开启。');
  }
  if (active && signature === appliedSettings) {
    if (useExplorationStore.getState().status !== 'running') useExplorationStore.setState({ status: 'running' });
    return;
  }
  if (active) await stop();
  const currentRevision = revision;
  const check = () => {
    if (!useExplorationStore.getState().enabled || dailyBusy() || revision !== currentRevision) throw new Superseded();
  };
  check();
  if (!Object.values(state.explorationSettings).some(Boolean)) throw new Error('请先在辅助功能设置中开启一项功能。');
  const pi = state.projectInterface;
  const controller = pi?.controller.find((item) => item.type === 'Win32');
  const resource = pi?.resource[0];
  if (!pi || !controller || !resource) throw new Error('辅助功能需要 Windows 游戏窗口和已加载的项目资源。');
  useExplorationStore.setState({ status: 'starting', error: '' });
  if (await maaService.isWorkstationLocked()) throw new Error('请解锁桌面后开启辅助功能。');
  if (!state.maaInitialized) await maaService.init();
  check();
  await startGlobalCallbackListener();
  if (!created) {
    await maaService.createInstance(instanceId);
    created = true;
  }
  check();
  const { classRegex, titleRegex } = getDesktopWindowFilters(controller);
  const windows = await maaService.findWin32Windows(classRegex, titleRegex);
  const window = windows[0];
  if (!window) throw new Error('请打开无限暖暖游戏窗口后再开启辅助功能。');
  const config = buildDesktopWindowControllerConfig(controller, window.handle);
  if (!config) throw new Error('辅助功能的窗口控制器配置无效。');
  const ctrlId = await maaService.connectController(instanceId, config);
  state.registerCtrlIdName(instanceId, ctrlId, window.window_name || window.class_name, 'window');
  if (await waitForCtrlResult(ctrlId) !== 'succeeded') throw new Error('连接游戏窗口失败。');
  check();
  const paths = computeResourcePaths(resource, controller, state.basePath);
  const nextResourceKey = JSON.stringify(paths);
  if (resourceKey !== nextResourceKey) {
    const ids = await maaService.loadResource(instanceId, paths);
    const results = await Promise.all(ids.map((id) => waitForResResult(id, 60000)));
    if (!ids.length || results.some((result) => result !== 'succeeded')) throw new Error('辅助功能资源加载失败。');
    resourceKey = nextResourceKey;
  }
  check();
  const task: SelectedTask = {
    id: instanceId, taskName: 'exploration', enabled: true, expanded: false,
    optionValues: Object.fromEntries(explorationFeatures.map(({ key }) => [key, { type: 'switch', value: state.explorationSettings[key] }])),
  };
  const runtimeInterface = { ...pi, task: [...pi.task, {
    name: 'exploration', label: '辅助功能', entry: 'MaaNikki_Exploration', option: explorationFeatures.map(({ key }) => key),
  }] };
  const envs = buildPiEnvVars({
    projectInterface: pi, controllerName: controller.name, resourceName: resource.name,
    language: state.language, maaVersion: state.maaVersion,
    translations: state.interfaceTranslations[getInterfaceLangKey(state.language)],
  });
  state.registerEntryTaskName('MaaNikki_Exploration', '辅助功能');
  needsStop = true;
  const ids = await maaService.startTasks(instanceId, [{
    entry: 'MaaNikki_Exploration', task_name: 'exploration', selected_task_id: instanceId,
    pipeline_override: generateTaskPipelineOverride(task, runtimeInterface, controller.name, resource.name, state.globalOptionValues),
  }], normalizeAgentConfigs(pi.agent), state.basePath, state.tcpCompatMode, envs, { name: controller.name, type: controller.type });
  ids.forEach((id) => state.registerTaskIdName(id, '辅助功能'));
  active = true;
  appliedSettings = signature;
  // A toggle or daily reservation can arrive while the Agent is starting.
  check();
  useExplorationStore.setState({ status: 'running' });
}

async function reconcileSafely() {
  try {
    await reconcile();
  } catch (error) {
    if (error instanceof Superseded) {
      try {
        await stop(!useExplorationStore.getState().enabled);
        useExplorationStore.setState({ status: useExplorationStore.getState().enabled ? 'paused' : 'off' });
        return;
      } catch (stopError) { error = stopError; }
    }
    useExplorationStore.setState({ enabled: false });
    try { await stop(true); } catch (cleanupError) { loggers.task.error('辅助功能停止失败:', cleanupError); }
    const message = String(error instanceof Error ? error.message : error);
    useExplorationStore.setState({ status: 'error', error: message });
    useAppStore.getState().addLog(instanceId, { type: 'error', message });
    loggers.task.error('辅助功能:', message);
    toast.error(message, { id: 'exploration-error' });
  }
}

export function setExplorationEnabled(enabled: boolean) {
  revision++;
  useExplorationStore.setState({ enabled, error: '', status: enabled ? 'starting' : 'stopping' });
  void serialize(reconcileSafely);
}

export function explorationOwnsWindow() {
  return needsStop;
}

/** Reserve the window before prerequisites, connection changes, or task submission. */
export async function reserveDailyWindow(): Promise<() => void> {
  dailyReservations++;
  revision++;
  try {
    await serialize(async () => {
      await stop();
      if (useExplorationStore.getState().enabled) useExplorationStore.setState({ status: 'paused' });
    });
  } catch (error) {
    dailyReservations--;
    throw error;
  }
  let released = false;
  return () => {
    if (released) return;
    released = true;
    dailyReservations--;
    // The backend owns task completion. The monitor resumes after it is idle.
  };
}

export function monitorExploration(): () => void {
  if (!isTauri()) return () => {};
  let pending = false;
  const timer = window.setInterval(() => {
    if (pending || !useExplorationStore.getState().enabled) return;
    pending = true;
    void serialize(reconcileSafely).finally(() => { pending = false; });
  }, 1000);
  return () => window.clearInterval(timer);
}
