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
import { fishingBusy } from './fishingService';

const INSTANCE_ID = 'maanikki-gungunji';
type Status = 'idle' | 'starting' | 'running' | 'stopping';
export const useGungunjiStore = create<{ status: Status }>(() => ({ status: 'idle' }));

let pending: Promise<void> | null = null;
let cancellation: AbortController | null = null;

export function gungunjiBusy() {
  return pending !== null;
}

export function stopGungunji() {
  if (!pending) return;
  useGungunjiStore.setState({ status: 'stopping' });
  cancellation?.abort();
}

/**
 * One on-demand Surprise-O-Matic (惊喜滚滚机) auto-pull session.
 * Runs in foreground like fishing and pauses/resumes exploration assists automatically.
 */
export function startGungunji(): Promise<void> {
  if (pending) return pending;
  const abort = new AbortController();
  cancellation = abort;
  useGungunjiStore.setState({ status: 'starting' });
  pending = run(abort.signal).finally(() => {
    pending = null;
    cancellation = null;
    useGungunjiStore.setState({ status: 'idle' });
  });
  return pending;
}

async function run(signal: AbortSignal) {
  let release: (() => void) | undefined;
  let created = false;
  let submitted = false;
  const check = () => { if (signal.aborted) throw new Error('滚滚机抽取已停止'); };
  try {
    if (!isTauri()) throw new Error('请在桌面客户端使用自动抽取滚滚机。');
    const state = useAppStore.getState();
    const busy = '请等待当前任务结束后再抽取滚滚机。';
    if (wardrobeBusy() || fishingBusy() || dailyWindowReserved() || state.instances.some((instance) => instance.isRunning)) {
      throw new Error(busy);
    }
    const pi = state.projectInterface;
    const controller = pi?.controller.find((item) => item.type === 'Win32');
    const resource = pi?.resource[0];
    if (!pi || !controller || !resource) throw new Error('请先加载项目资源。');
    if (await maaService.isWorkstationLocked()) throw new Error('请解锁桌面后再抽取滚滚机。');
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
    if (!resources.length || loaded.some((result) => result !== 'succeeded')) throw new Error('滚滚机资源加载失败。');
    check();
    const task: SelectedTask = { id: INSTANCE_ID, taskName: 'gungunji_pull', enabled: true, expanded: false, optionValues: {} };
    const definition = { ...pi, task: [...pi.task, { name: 'gungunji_pull', label: '自动抽取滚滚机', entry: 'MaaNikki_Gungunji' }] };
    const envs = buildPiEnvVars({
      projectInterface: pi,
      controllerName: controller.name,
      resourceName: resource.name,
      language: state.language,
      maaVersion: state.maaVersion,
      translations: state.interfaceTranslations[getInterfaceLangKey(state.language)],
    });
    state.registerEntryTaskName('MaaNikki_Gungunji', '自动抽取滚滚机');
    submitted = true;
    const ids = await maaService.startTasks(
      INSTANCE_ID,
      [{
        entry: 'MaaNikki_Gungunji',
        task_name: 'gungunji_pull',
        selected_task_id: INSTANCE_ID,
        pipeline_override: generateTaskPipelineOverride(task, definition, controller.name, resource.name, state.globalOptionValues),
      }],
      normalizeAgentConfigs(pi.agent),
      state.basePath,
      state.tcpCompatMode,
      envs,
      { name: controller.name, type: controller.type },
    );
    ids.forEach((value) => state.registerTaskIdName(value, '自动抽取滚滚机'));
    if (ids.length !== 1) throw new Error('滚滚机任务提交失败。');
    useGungunjiStore.setState({ status: 'running' });
    const outcome = await waitForTaskResult(ids[0], { signal });
    check();
    if (outcome !== 'succeeded') throw new Error('滚滚机抽取未完成，请查看日志。');
    toast.success('滚滚机抽取结束');
  } catch (error) {
    if (!signal.aborted) toast.error(String(error instanceof Error ? error.message : error));
  } finally {
    try {
      if (submitted && !await stopInstanceTasks(INSTANCE_ID)) toast.error('滚滚机抽取尚未停止，请稍后再试。');
      if (created) await maaService.destroyInstance(INSTANCE_ID);
    } catch (error) {
      toast.error(String(error instanceof Error ? error.message : error));
    }
    release?.();
    if (submitted) await focusWindow().catch(() => {});
  }
}
