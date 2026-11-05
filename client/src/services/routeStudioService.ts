import { invoke } from '@tauri-apps/api/core';
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
import { fishingBusy } from './fishingService';
import { stopInstanceTasks } from './taskStopService';
import { wardrobeBusy } from './wardrobeService';
import type { Recording, RouteDocument } from './routeDocument';

const INSTANCE_ID = 'maanikki-route';
const RECORDING_FILE = 'config/route_recording.json';

type Status = 'idle' | 'starting' | 'recording' | 'running' | 'stopping';
export const useRouteStudioStore = create<{
  status: Status;
  recording: Recording | null;
  /** A finished recording waiting for the user to keep or discard it. */
  finished: Recording | null;
}>(() => ({ status: 'idle', recording: null, finished: null }));

export interface RouteSummary {
  id: string; title: string; map: string; points: number; builtin: boolean; path: string;
  teleport?: RouteDocument['teleport'];
}

export const listRoutes = () => invoke<{ user: RouteSummary[]; builtin: RouteSummary[] }>('list_routes');
export const saveUserRoute = (route: RouteDocument) => invoke<string>('save_user_route', { id: route.id, route });
export const deleteUserRoute = (id: string) => invoke<void>('delete_user_route', { id });
export const readLocalFile = (filename: string) => invoke<string>('read_local_file', { filename });

export async function readRoute(summary: Pick<RouteSummary, 'id' | 'builtin'>): Promise<RouteDocument> {
  const folder = summary.builtin ? 'resource/routes/daily' : 'config/routes';
  return JSON.parse((await readLocalFile(`${folder}/${summary.id}.json`)).replace(/^﻿/, ''));
}

let pending: Promise<void> | null = null;
let cancellation: AbortController | null = null;

export function stopRouteSession() {
  if (!pending) return;
  useRouteStudioStore.setState({ status: 'stopping' });
  cancellation?.abort();
}

async function readRecording(since: number) {
  try {
    const value = JSON.parse(await readLocalFile(RECORDING_FILE)) as Recording;
    // A file left by an earlier recording must not appear as this one.
    return Date.parse(value.started_at) >= since ? value : null;
  } catch {
    return null;
  }
}

/** Takes the game window like a daily run, so assist features pause and resume by themselves. */
function session(status: Status, label: string, entry: string, params: Record<string, unknown>,
  poll?: () => Promise<void>): Promise<void> {
  if (pending) return pending;
  const abort = new AbortController();
  cancellation = abort;
  useRouteStudioStore.setState({ status: 'starting' });
  pending = run(status, label, entry, params, abort.signal, poll).finally(() => {
    pending = null;
    cancellation = null;
    useRouteStudioStore.setState({ status: 'idle', recording: null });
  });
  return pending;
}

async function run(status: Status, label: string, entry: string, params: Record<string, unknown>,
  signal: AbortSignal, poll?: () => Promise<void>) {
  let release: (() => void) | undefined;
  let created = false;
  let submitted = false;
  let timer: number | undefined;
  const check = () => { if (signal.aborted) throw new Error(`${label}已停止`); };
  try {
    if (!isTauri()) throw new Error(`请在桌面客户端${label}。`);
    const state = useAppStore.getState();
    const busy = `请等待当前任务结束后再${label}。`;
    if (wardrobeBusy() || fishingBusy() || dailyWindowReserved() || state.instances.some((instance) => instance.isRunning)) throw new Error(busy);
    const pi = state.projectInterface;
    const controller = pi?.controller.find((item) => item.type === 'Win32');
    const resource = pi?.resource[0];
    if (!pi || !controller || !resource) throw new Error('请先加载项目资源。');
    if (await maaService.isWorkstationLocked()) throw new Error(`请解锁桌面后再${label}。`);
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
    if (!resources.length || loaded.some((result) => result !== 'succeeded')) throw new Error(`${label}资源加载失败。`);
    check();
    const taskName = `route_${status}`;
    const task: SelectedTask = { id: INSTANCE_ID, taskName, enabled: true, expanded: false, optionValues: {} };
    const definition = { ...pi, task: [...pi.task, { name: taskName, label, entry }] };
    const override = JSON.parse(generateTaskPipelineOverride(task, definition, controller.name, resource.name, state.globalOptionValues) || '[]');
    const envs = buildPiEnvVars({ projectInterface: pi, controllerName: controller.name, resourceName: resource.name,
      language: state.language, maaVersion: state.maaVersion,
      translations: state.interfaceTranslations[getInterfaceLangKey(state.language)] });
    state.registerEntryTaskName(entry, label);
    submitted = true;
    const ids = await maaService.startTasks(INSTANCE_ID, [{ entry, task_name: taskName, selected_task_id: INSTANCE_ID,
      pipeline_override: JSON.stringify([...override, { [entry]: { custom_action_param: params } }]) }],
    normalizeAgentConfigs(pi.agent), state.basePath, state.tcpCompatMode, envs, { name: controller.name, type: controller.type });
    ids.forEach((value) => state.registerTaskIdName(value, label));
    if (ids.length !== 1) throw new Error(`${label}提交失败。`);
    useRouteStudioStore.setState({ status });
    if (poll) timer = window.setInterval(() => void poll(), 400);
    const outcome = await waitForTaskResult(ids[0], { signal });
    check();
    if (outcome !== 'succeeded') throw new Error(`${label}未完成，请查看日志。`);
    if (!poll) toast.success(`${label}完成`);
  } catch (error) {
    if (!signal.aborted) toast.error(String(error instanceof Error ? error.message : error));
  } finally {
    window.clearInterval(timer);
    try {
      if (submitted && !await stopInstanceTasks(INSTANCE_ID)) toast.error(`${label}尚未停止，请稍后再试。`);
      if (created) await maaService.destroyInstance(INSTANCE_ID);
    } catch (error) {
      toast.error(String(error instanceof Error ? error.message : error));
    }
    release?.();
    if (submitted) await focusWindow().catch(() => {});
    await poll?.();
  }
}

/** Record the player's own run. The agent only observes and sends no game input. */
export function startRecording(map: string) {
  const since = Date.now()-5000;
  useRouteStudioStore.setState({ finished: null });
  return session('recording', '录制路线', 'MaaNikki_RouteRecord', { map }, async () => {
    const recording = await readRecording(since);
    if (!recording) return;
    useRouteStudioStore.setState({ recording });
    if (!pending || cancellation?.signal.aborted || recording.state === 'finished' || recording.state === 'failed') {
      useRouteStudioStore.setState({ finished: recording.samples.length ? recording : null });
    }
  });
}

export function runRoute(path: string, mode: 'full' | 'movement', start: number) {
  return session('running', mode === 'full' ? '试跑路线' : '试走路线', 'MaaNikki_RouteRun', { file: path, mode, start });
}

export function discardRecording() {
  useRouteStudioStore.setState({ finished: null });
}
