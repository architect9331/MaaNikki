import { create } from 'zustand';
import { toast } from 'sonner';
import { useAppStore } from '@/stores/appStore';
import { normalizeAgentConfigs, type InputOption, type SelectedTask } from '@/types/interface';
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
import { readLocalFile } from './routeStudioService';
import { stopInstanceTasks } from './taskStopService';
import { wardrobeBusy } from './wardrobeService';

const INSTANCE_ID = 'maanikki-game-keys';
const ENTRY = 'MaaNikki_GameKeysRead';
const LABEL = '读取游戏键位';
/** Written by the agent action nikki.game_keys_read. */
const RESULT_FILE = 'config/game_keys_read.json';

interface ReadReport {
  created_at: number;
  complete: boolean;
  values: Record<string, string>;
  rows: { name: string; label: string; caption: string; value: string | null }[];
  missing: { name: string; label: string }[];
  warnings: string[];
}

export interface GameKeyChange {
  optionKey: string;
  name: string;
  label: string;
  from: string;
  to: string;
}

export interface GameKeysReadResult {
  complete: boolean;
  changes: GameKeyChange[];
  unchanged: number;
  missing: string[];
  warnings: string[];
}

type Status = 'idle' | 'starting' | 'reading' | 'stopping';
export const useGameKeysReadStore = create<{ status: Status; result: GameKeysReadResult | null }>(
  () => ({ status: 'idle', result: null }),
);

let pending: Promise<void> | null = null;
let cancellation: AbortController | null = null;

export function gameKeysReadBusy() {
  return pending !== null;
}

export function stopGameKeysRead() {
  if (!pending) return;
  useGameKeysReadStore.setState({ status: 'stopping' });
  cancellation?.abort();
}

export function startGameKeysRead(): Promise<void> {
  if (pending) return pending;
  const abort = new AbortController();
  cancellation = abort;
  useGameKeysReadStore.setState({ status: 'starting', result: null });
  pending = run(abort.signal).finally(() => {
    pending = null;
    cancellation = null;
    useGameKeysReadStore.setState({ status: 'idle' });
  });
  return pending;
}

const sameKey = (a: string, b: string) => a.trim().toLowerCase() === b.trim().toLowerCase();

function gameKeyOptions(): [string, InputOption][] {
  const options = useAppStore.getState().projectInterface?.option ?? {};
  return Object.entries(options)
    .filter((entry): entry is [string, InputOption] => entry[0].startsWith('GameKeys') && entry[1].type === 'input');
}

/** Compare the read bindings with the saved (or default) values of each settings entry. */
function compare(report: ReadReport): GameKeysReadResult {
  const saved = useAppStore.getState().globalOptionValues;
  const changes: GameKeyChange[] = [];
  let unchanged = 0;
  for (const [optionKey, option] of gameKeyOptions()) {
    const current = saved[optionKey];
    for (const input of option.inputs) {
      const to = report.values[input.name];
      if (!to) continue;
      const from = (current?.type === 'input' ? current.values[input.name] : undefined) ?? input.default ?? '';
      if (sameKey(from, to)) unchanged += 1;
      else changes.push({ optionKey, name: input.name, label: String(input.label ?? input.name), from, to });
    }
  }
  return {
    complete: report.complete,
    changes,
    unchanged,
    missing: (report.missing ?? []).map((item) => item.label),
    warnings: report.warnings ?? [],
  };
}

/** Save the confirmed changes into the global game key settings. */
export function applyGameKeysRead(changes: GameKeyChange[]) {
  const state = useAppStore.getState();
  const grouped = new Map<string, Record<string, string>>();
  for (const change of changes) {
    grouped.set(change.optionKey, { ...grouped.get(change.optionKey), [change.name]: change.to });
  }
  for (const [optionKey, values] of grouped) {
    const current = useAppStore.getState().globalOptionValues[optionKey];
    const base = current?.type === 'input' ? current : { type: 'input' as const, values: {} };
    state.setGlobalOptionValue(optionKey, { ...base, values: { ...base.values, ...values } });
  }
  useGameKeysReadStore.setState({ result: null });
  toast.success(`已更新 ${changes.length} 个游戏键位，重新运行任务后生效。`);
}

export function dismissGameKeysRead() {
  useGameKeysReadStore.setState({ result: null });
}

async function readReport(startedAt: number): Promise<ReadReport> {
  let report: ReadReport;
  try {
    report = JSON.parse(await readLocalFile(RESULT_FILE)) as ReadReport;
  } catch {
    throw new Error('没有找到本次读取结果，请查看日志。');
  }
  // An older file from a previous attempt must never be applied.
  if (typeof report.created_at !== 'number' || report.created_at < startedAt - 5000) {
    throw new Error('没有找到本次读取结果，请查看日志。');
  }
  return report;
}

async function run(signal: AbortSignal) {
  let release: (() => void) | undefined;
  let created = false;
  let submitted = false;
  const startedAt = Date.now();
  const check = () => { if (signal.aborted) throw new Error('读取已停止'); };
  try {
    if (!isTauri()) throw new Error('请在桌面客户端读取游戏键位。');
    const state = useAppStore.getState();
    const busy = '请等待当前任务结束后再读取游戏键位。';
    if (wardrobeBusy() || fishingBusy() || dailyWindowReserved() || state.instances.some((instance) => instance.isRunning)) throw new Error(busy);
    const pi = state.projectInterface;
    const controller = pi?.controller.find((item) => item.type === 'Win32');
    const resource = pi?.resource[0];
    if (!pi || !controller || !resource) throw new Error('请先加载项目资源。');
    if (await maaService.isWorkstationLocked()) throw new Error('请解锁桌面后再读取游戏键位。');
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
    if (!resources.length || loaded.some((result) => result !== 'succeeded')) throw new Error('键位读取资源加载失败。');
    check();
    const task: SelectedTask = { id: INSTANCE_ID, taskName: 'game_keys_read', enabled: true, expanded: false, optionValues: {} };
    const definition = { ...pi, task: [...pi.task, { name: 'game_keys_read', label: LABEL, entry: ENTRY }] };
    const envs = buildPiEnvVars({ projectInterface: pi, controllerName: controller.name, resourceName: resource.name,
      language: state.language, maaVersion: state.maaVersion,
      translations: state.interfaceTranslations[getInterfaceLangKey(state.language)] });
    state.registerEntryTaskName(ENTRY, LABEL);
    submitted = true;
    const ids = await maaService.startTasks(INSTANCE_ID, [{ entry: ENTRY, task_name: 'game_keys_read', selected_task_id: INSTANCE_ID,
      pipeline_override: generateTaskPipelineOverride(task, definition, controller.name, resource.name, state.globalOptionValues) }],
    normalizeAgentConfigs(pi.agent), state.basePath, state.tcpCompatMode, envs, { name: controller.name, type: controller.type });
    ids.forEach((value) => state.registerTaskIdName(value, LABEL));
    if (ids.length !== 1) throw new Error('键位读取任务提交失败。');
    useGameKeysReadStore.setState({ status: 'reading' });
    const outcome = await waitForTaskResult(ids[0], { signal });
    check();
    // A partial read still lets the player review what was recognized.
    const report = await readReport(startedAt).catch((error) => {
      if (outcome === 'succeeded') throw error;
      throw new Error('游戏键位读取未完成，请回到游戏主界面后重试，或查看日志。');
    });
    if (!Object.keys(report.values ?? {}).length) throw new Error('没有识别到任何游戏键位，请查看日志。');
    useGameKeysReadStore.setState({ result: compare(report) });
  } catch (error) {
    if (!signal.aborted) toast.error(String(error instanceof Error ? error.message : error));
  } finally {
    try {
      if (submitted && !await stopInstanceTasks(INSTANCE_ID)) toast.error('键位读取尚未停止，请稍后再试。');
      if (created) await maaService.destroyInstance(INSTANCE_ID);
    } catch (error) {
      toast.error(String(error instanceof Error ? error.message : error));
    }
    release?.();
    if (submitted) await focusWindow().catch(() => {});
  }
}
