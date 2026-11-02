import { useEffect } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { maaService } from '@/services/maaService';
import { useAppStore } from '@/stores/appStore';
import { loggers } from '@/utils/logger';
import { isTauri } from '@/utils/paths';

const log = loggers.app;

interface RunResult { succeeded: string[]; failed: string[] }

export function runResultUrl(template: string, title: string, content: string): string {
  return template.replace(/\{title\}/g, encodeURIComponent(title)).replace(/\{content\}/g, encodeURIComponent(content));
}

/** Push one summary when a run ends. A stopped run was the user's own decision, so it stays silent. */
export function useRunResultNotifier(): void {
  useEffect(() => {
    if (!isTauri()) return;
    const results = new Map<string, RunResult>();
    let cancelled = false;
    let unlisten: (() => void) | undefined;

    void maaService.onCallback((message, details) => {
      if (message !== 'Tasker.Task.Succeeded' && message !== 'Tasker.Task.Failed') return;
      if (details.task_id === undefined || details.entry === 'MaaTaskerPostStop') return;
      const state = useAppStore.getState();
      for (const [instanceId, mapping] of Object.entries(state.maaTaskIdMapping)) {
        const selectedId = mapping[details.task_id];
        if (!selectedId) continue;
        const task = state.instances.find((item) => item.id === instanceId)?.selectedTasks.find((item) => item.id === selectedId);
        if (!task) continue;
        const label = state.taskIdToName[details.task_id] ?? task.customName ?? task.taskName;
        const result = results.get(instanceId) ?? { succeeded: [], failed: [] };
        (message === 'Tasker.Task.Succeeded' ? result.succeeded : result.failed).push(label);
        results.set(instanceId, result);
      }
    }).then((stop) => {
      if (cancelled) stop();
      else unlisten = stop;
    });

    const unsubscribe = useAppStore.subscribe((state) => state.instances, (instances, previous) => {
      for (const instance of previous) {
        if (!instance.isRunning || instances.find((item) => item.id === instance.id)?.isRunning) continue;
        const result = results.get(instance.id);
        results.delete(instance.id);
        const { runResultWebhook, instanceTaskStatus } = useAppStore.getState();
        const url = runResultWebhook.trim();
        if (!url || !result || instanceTaskStatus[instance.id] === null) continue;
        const title = result.failed.length ? `MaaNikki：${result.failed.length} 个任务未完成` : 'MaaNikki：任务已完成';
        const content = [`配置：${instance.name}`, `完成 ${result.succeeded.length} 个`,
          ...result.failed.length ? [`未完成：${result.failed.join('、')}`] : []].join('\n');
        void invoke('send_run_webhook', { url: runResultUrl(url, title, content) })
          .catch((error) => log.warn('运行结果推送失败:', error));
      }
    });

    return () => {
      cancelled = true;
      unlisten?.();
      unsubscribe();
    };
  }, []);
}
