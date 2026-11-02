import { create } from 'zustand';
import { toast } from 'sonner';
import { useAppStore } from '@/stores/appStore';
import { normalizeAgentConfigs, type SelectedTask } from '@/types/interface';
import { buildDesktopWindowControllerConfig, getDesktopWindowFilters } from '@/utils/controller';
import { computeResourcePaths } from '@/utils/resourcePath';
import { generateTaskPipelineOverride } from '@/utils/pipelineOverride';
import { buildPiEnvVars } from '@/utils/piEnv';
import { getInterfaceLangKey } from '@/i18n';
import { isTauri } from '@/utils/paths';
import { focusWindow } from '@/utils/windowUtils';
import { startGlobalCallbackListener, waitForCtrlResult, waitForResResult, waitForTaskResult } from '@/components/connection/callbackCache';
import { maaService } from './maaService';
import { dailyWindowReserved, reserveDailyWindow } from './explorationService';
import { stopInstanceTasks } from './taskStopService';
import { wardrobeBusy } from './wardrobeService';

const INSTANCE_ID = 'maanikki-fishing';
type Status = 'idle' | 'starting' | 'fishing' | 'stopping';
export const useFishingStore = create<{ status: Status }>(() => ({ status: 'idle' }));

let pending: Promise<void> | null = null;
let cancellation: AbortController | null = null;

export function fishingBusy() {
  return pending !== null;
}

export function stopFishing() {
  if (!pending) return;
  useFishingStore.setState({ status: 'stopping' });
  cancellation?.abort();
}

/**
 * One fishing session at the current spot. It takes the window like a daily run,
 * so the assist features pause and resume by themselves afterwards.
 */
export function startFishing(): Promise<void> {
  if (pending) return pending;
  const abort = new AbortController();
  cancellation = abort;
  useFishingStore.setState({ status: 'starting' });
  pending = run(abort.signal).finally(() => {
    pending = null;
    cancellation = null;
    useFishingStore.setState({ status: 'idle' });
  });
  return pending;
}

async function run(signal: AbortSignal) {
  let release: (() => void) | undefined;
  let created = false;
  let submitted = false;
  const check = () => { if (signal.aborted) throw new Error('钓鱼已停止'); };
  try {
    if (!isTauri()) throw new Error('请在桌面客户端钓鱼。');
    const state = useAppStore.getState();
    const busy = '请等待当前任务结束后再钓鱼。';
    if (wardrobeBusy() || dailyWindowReserved() || state.instances.some((instance) => instance.isRunning)) throw new Error(busy);
    const pi = state.projectInterface;
    const controller = pi?.controller.find((item) => item.type === 'Win32');
    const resource = pi?.resource[0];
    if (!pi || !controller || !resource) throw new Error('请先加载项目资源。');
    if (await maaService.isWorkstationLocked()) throw new Error('请解锁桌面后再钓鱼。');
    check();
    release = await reserveDailyWindow();
    check();
    const backend = await maaService.getAllStates();
    if (!backend) throw new Error('无法读取任务状态。');
    if (Object.values(backend.instances).some((item) => item.isRunning)) throw new Error(busy);
    if (!state.maaInitialized) await maaService.init();
    await startGlobalCallbackListener();
    check();
    await maaService.createInstance(INSTANCE_ID);
    created = true;
    const { classRegex, titleRegex } = getDesktopWindowFilters(controller);
    const windows = await maaService.findWin32Windows(classRegex, titleRegex);
    if (!windows[0]) throw new Error('请先打开游戏。');
    const config = buildDesktopWindowControllerConfig(controller, windows[0].handle);
    if (!config) throw new Error('游戏窗口控制器配置无效。');
    const ctrlId = await maaService.connectController(INSTANCE_ID, config);
    state.registerCtrlIdName(INSTANCE_ID, ctrlId, windows[0].window_name || windows[0].class_name, 'window');
    if (await waitForCtrlResult(ctrlId) !== 'succeeded') throw new Error('连接游戏窗口失败。');
    check();
    const resources = await maaService.loadResource(INSTANCE_ID, computeResourcePaths(resource, controller, state.basePath));
    const loaded = await Promise.all(resources.map((value) => waitForResResult(value, 60000)));
    if (!resources.length || loaded.some((result) => result !== 'succeeded')) throw new Error('钓鱼资源加载失败。');
    check();
    const task: SelectedTask = { id: INSTANCE_ID, taskName: 'current_fishing', enabled: true, expanded: false, optionValues: {} };
    const definition = { ...pi, task: [...pi.task, { name: 'current_fishing', label: '当前位置钓鱼', entry: 'MaaNikki_Fishing' }] };
    const envs = buildPiEnvVars({ projectInterface: pi, controllerName: controller.name, resourceName: resource.name,
      language: state.language, maaVersion: state.maaVersion,
      translations: state.interfaceTranslations[getInterfaceLangKey(state.language)] });
    state.registerEntryTaskName('MaaNikki_Fishing', '当前位置钓鱼');
    submitted = true;
    const ids = await maaService.startTasks(INSTANCE_ID, [{ entry: 'MaaNikki_Fishing', task_name: 'current_fishing', selected_task_id: INSTANCE_ID,
      pipeline_override: generateTaskPipelineOverride(task, definition, controller.name, resource.name, state.globalOptionValues) }],
    normalizeAgentConfigs(pi.agent), state.basePath, state.tcpCompatMode, envs, { name: controller.name, type: controller.type });
    ids.forEach((value) => state.registerTaskIdName(value, '当前位置钓鱼'));
    if (ids.length !== 1) throw new Error('钓鱼任务提交失败。');
    useFishingStore.setState({ status: 'fishing' });
    const outcome = await waitForTaskResult(ids[0], { signal });
    check();
    if (outcome !== 'succeeded') throw new Error('钓鱼未完成，请查看日志。');
    toast.success('钓鱼完成');
  } catch (error) {
    if (!signal.aborted) toast.error(String(error instanceof Error ? error.message : error));
  } finally {
    try {
      if (submitted && !await stopInstanceTasks(INSTANCE_ID)) toast.error('钓鱼尚未停止，请稍后再试。');
      if (created) await maaService.destroyInstance(INSTANCE_ID);
    } catch (error) {
      toast.error(String(error instanceof Error ? error.message : error));
    }
    release?.();
    if (submitted) await focusWindow().catch(() => {});
  }
}
