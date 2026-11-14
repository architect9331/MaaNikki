import { create } from 'zustand';
import { toast } from 'sonner';
import { useAppStore } from '@/stores/appStore';
import { useAccountStateStore } from '@/stores/accountStateStore';
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
import { reserveDailyWindow, dailyWindowReserved } from './explorationService';
import { stopInstanceTasks } from './taskStopService';

export const WARDROBE_INSTANCE_ID = 'maanikki-wardrobe';
export type WardrobeReadMode = 'wardrobe' | 'renewal' | 'backpack';
type Status = 'idle' | 'starting' | 'reading' | 'stopping' | 'completed' | 'stopped' | 'error';

const READ_SPECS: Record<WardrobeReadMode, { taskName: string; label: string; entry: string; busyText: string }> = {
  wardrobe: { taskName: 'wardrobe', label: '衣柜读取', entry: 'MaaNikki_WardrobeRead', busyText: '正在读取衣柜' },
  renewal: { taskName: 'renewal_read', label: '焕新读取', entry: 'MaaNikki_RenewalRead', busyText: '正在读取服装焕新' },
  backpack: { taskName: 'backpack_read', label: '背包读取', entry: 'MaaNikki_BackpackRead', busyText: '正在读取背包库存' },
};

export const useWardrobeStore = create<{ status: Status; mode: WardrobeReadMode; error: string }>(() => ({
  status: 'idle', mode: 'wardrobe', error: '',
}));
export const wardrobeLabels: Record<Status, string> = {
  idle: '准备读取', starting: '连接游戏中', reading: '正在读取衣柜', stopping: '正在停止',
  completed: '读取完成', stopped: '已停止', error: '读取中断',
};
let pending: Promise<void> | null = null;
let cancellation: AbortController | null = null;
let retainedCleanup: (() => Promise<void>) | null = null;

export function wardrobeBusy() {
  return pending !== null || retainedCleanup !== null;
}

export function stopWardrobeRead() {
  if (!pending) return;
  useWardrobeStore.setState({ status: 'stopping' });
  cancellation?.abort();
}

export function startRead(mode: WardrobeReadMode): Promise<void> {
  if (pending) return pending;
  const abort = new AbortController();
  cancellation = abort;
  useWardrobeStore.setState({ status: 'starting', mode, error: '' });
  pending = run(abort.signal, mode).finally(() => { pending = null; cancellation = null; });
  return pending;
}

export function startWardrobeRead(): Promise<void> {
  return startRead('wardrobe');
}

export function startRenewalRead(): Promise<void> {
  return startRead('renewal');
}

export function startBackpackRead(): Promise<void> {
  return startRead('backpack');
}

async function run(signal: AbortSignal, mode: WardrobeReadMode) {
  const id = WARDROBE_INSTANCE_ID;
  const spec = READ_SPECS[mode];
  let release: (() => void) | undefined;
  let created = false;
  let submitted = false;
  let poll: ReturnType<typeof setInterval> | undefined;
  const check = () => { if (signal.aborted) throw new Error('读取已停止'); };
  try {
    if (retainedCleanup) await retainedCleanup();
    check();
    if (!isTauri()) throw new Error(`请在桌面客户端执行${spec.label}。`);
    const state = useAppStore.getState();
    if (dailyWindowReserved() || state.instances.some((instance) => instance.isRunning)) {
      throw new Error(`请等待当前任务结束后执行${spec.label}。`);
    }
    const pi = state.projectInterface;
    const controller = pi?.controller.find((item) => item.type === 'Win32');
    const resource = pi?.resource[0];
    if (!pi || !controller || !resource) throw new Error('请先加载项目资源。');
    if (await maaService.isWorkstationLocked()) throw new Error(`请解锁桌面后执行${spec.label}。`);
    check();
    if (dailyWindowReserved()) throw new Error(`请等待当前任务结束后执行${spec.label}。`);
    release = await reserveDailyWindow();
    check();
    const backend = await maaService.getAllStates();
    if (!backend) throw new Error('无法读取任务状态。');
    if (Object.values(backend.instances).some((item) => item.isRunning)) {
      throw new Error(`请等待当前任务结束后执行${spec.label}。`);
    }
    if (!state.maaInitialized) await maaService.init();
    check();
    await startGlobalCallbackListener();
    check();
    await maaService.createInstance(id);
    created = true;
    check();
    const { classRegex, titleRegex } = getDesktopWindowFilters(controller);
    const windows = await maaService.findWin32Windows(classRegex, titleRegex);
    check();
    if (!windows[0]) throw new Error('请先打开游戏。');
    const config = buildDesktopWindowControllerConfig(controller, windows[0].handle);
    if (!config) throw new Error('游戏窗口控制器配置无效。');
    const ctrlId = await maaService.connectController(id, config);
    state.registerCtrlIdName(id, ctrlId, windows[0].window_name || windows[0].class_name, 'window');
    if (await waitForCtrlResult(ctrlId) !== 'succeeded') throw new Error('连接游戏窗口失败。');
    check();
    const paths = computeResourcePaths(resource, controller, state.basePath);
    const resources = await maaService.loadResource(id, paths);
    const results = await Promise.all(resources.map((value) => waitForResResult(value, 60000)));
    if (!resources.length || results.some((result) => result !== 'succeeded')) throw new Error(`${spec.label}资源加载失败。`);
    check();
    const task: SelectedTask = { id, taskName: spec.taskName, enabled: true, expanded: false, optionValues: {} };
    const definition = { ...pi, task: [...pi.task, { name: spec.taskName, label: spec.label, entry: spec.entry }] };
    const envs = buildPiEnvVars({ projectInterface: pi, controllerName: controller.name, resourceName: resource.name,
      language: state.language, maaVersion: state.maaVersion,
      translations: state.interfaceTranslations[getInterfaceLangKey(state.language)] });
    state.registerEntryTaskName(spec.entry, spec.label);
    submitted = true;
    const ids = await maaService.startTasks(id, [{ entry: spec.entry, task_name: spec.taskName, selected_task_id: id,
      pipeline_override: generateTaskPipelineOverride(task, definition, controller.name, resource.name, state.globalOptionValues) }],
    normalizeAgentConfigs(pi.agent), state.basePath, state.tcpCompatMode, envs, { name: controller.name, type: controller.type });
    ids.forEach((value) => state.registerTaskIdName(value, spec.label));
    check();
    if (ids.length !== 1) throw new Error(`${spec.label}任务提交失败。`);
    useWardrobeStore.setState({ status: 'reading' });
    poll = setInterval(() => { void useAccountStateStore.getState().refresh().catch(() => {}); }, 2000);
    const outcome = await waitForTaskResult(ids[0], { signal });
    check();
    if (outcome !== 'succeeded') throw new Error(`本次${spec.label}未完成，已保存读取进度，请查看日志。`);
    useWardrobeStore.setState({ status: 'completed' });
    toast.success(`${spec.label}完成`);
  } catch (error) {
    const message = String(error instanceof Error ? error.message : error);
    useWardrobeStore.setState({ status: signal.aborted ? 'stopped' : 'error', error: signal.aborted ? '' : message });
    if (!signal.aborted) toast.error(message);
  } finally {
    if (poll) clearInterval(poll);
    const returnFocus = submitted;
    if (created || submitted || release) {
      const cleanup = async () => {
        if (submitted) {
          if (!await stopInstanceTasks(id)) throw new Error(`${spec.label}尚未停止，请稍后再试。`);
          submitted = false;
        }
        if (created) {
          await maaService.destroyInstance(id);
          created = false;
        }
        release?.();
        retainedCleanup = null;
      };
      try {
        await cleanup();
      } catch (error) {
        // Keep the window reservation while the backend may still own input.
        const message = String(error instanceof Error ? error.message : error);
        useWardrobeStore.setState({ status: 'error', error: message });
        retainedCleanup = cleanup;
        toast.error(message);
      }
    }
    await useAccountStateStore.getState().refresh().catch(() => {});
    if (returnFocus && !retainedCleanup) await focusWindow().catch(() => {});
  }
}
