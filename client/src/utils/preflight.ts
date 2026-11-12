import { invoke } from '@tauri-apps/api/core';
import { useAccountStateStore } from '@/stores/accountStateStore';
import { useAppStore } from '@/stores/appStore';
import type { Instance, OptionValue } from '@/types/interface';
import { getBlessingProgress } from '@/utils/blessingProgress';
import { realmCollectionEnabled, realmFarmOnlyEnabled } from '@/utils/nikkiOptions';
import { isTauri } from '@/utils/paths';

export interface PreflightIssue {
  id: string;
  title: string;
  detail: string;
  /** Once confirmed, this reminder does not return. */
  once?: boolean;
}

const enabledTask = (instance: Instance, name: string) =>
  instance.selectedTasks.find((task) => task.taskName === name && task.enabled);

const switchOn = (values: Record<string, OptionValue>, key: string, fallback: boolean) => {
  const option = values[key];
  return option?.type === 'switch' ? option.value : fallback;
};

/**
 * Only the four movement keys must differ. Other actions share keys by default,
 * such as F for both skipping and picking up.
 */
function duplicateMoveKeys(): string[] {
  const state = useAppStore.getState();
  const definition = state.projectInterface?.option?.GameKeysMove;
  if (definition?.type !== 'input') return [];
  const saved = state.globalOptionValues?.GameKeysMove;
  const owners = new Map<string, string[]>();
  for (const input of definition.inputs) {
    const raw = (saved?.type === 'input' ? saved.values[input.name] : undefined) ?? input.default ?? '';
    const name = raw.trim().toLowerCase();
    if (name) owners.set(name, [...owners.get(name) ?? [], String(input.label ?? input.name)]);
  }
  return [...owners.entries()].filter(([, labels]) => labels.length > 1)
    .map(([key, labels]) => `${key.toUpperCase()}（${labels.join('、')}）`);
}

/** Checks everything knowable before touching the game; the rest stays with the task. */
export async function preflightIssues(instance: Instance): Promise<PreflightIssue[]> {
  const issues: PreflightIssue[] = [];
  const state = useAppStore.getState();

  const realmIdx = instance.selectedTasks.findIndex((task) => task.taskName === 'realm_challenge' && task.enabled);
  const zhaoxiIdx = instance.selectedTasks.findIndex((task) => task.taskName === 'zhaoxi' && task.enabled);
  if (realmIdx >= 0 && zhaoxiIdx > realmIdx) {
    issues.push({ id: 'realm-before-zhaoxi', once: true, title: '「幻境挑战」排在「朝夕心愿」之前',
      detail: '幻境挑战会先消耗完当天的活跃能量；若当天朝夕心愿刷出其他指定幻境且不耗体力心愿不足 500 分，将没有剩余体力自动补分。建议将「朝夕心愿」拖动到「幻境挑战」上方。' });
  }

  const realm = enabledTask(instance, 'realm_challenge');
  if (realm && realmCollectionEnabled(realm.optionValues)) {
    const farmOnly = realmFarmOnlyEnabled(realm.optionValues);
    if (!getBlessingProgress(useAccountStateStore.getState().data).localKnown) {
      issues.push({ id: 'collection-filter', title: '第一次全收集前，请清除祝福闪光仓库的筛选',
        detail: farmOnly
          ? '第一次运行会只读扫描全部祝福闪光与胚子数量，筛选会让读取不完整。'
          : '第一次运行会读取并锁定全部祝福闪光，筛选会让读取不完整。' });
    }
    if (!farmOnly && switchOn(realm.optionValues, 'CollectionDecompose', true)) {
      issues.push({ id: 'collection-decompose', once: true, title: '已开启「爆仓时分解重复祝福闪光」',
        detail: '仓库满时会一键分解未锁定的闪光，每批最多 100 件，且不可恢复。请提前锁定想保留的闪光，并且不要手动解锁已收藏的闪光。不想分解可以在幻境挑战中关闭。' });
    }
  }

  const duplicates = duplicateMoveKeys();
  if (duplicates.length) {
    issues.push({ id: 'duplicate-keys', title: '移动方向的按键重复',
      detail: `${duplicates.join('；')}。四个移动方向需要不同的键，请在「设置 → 任务设置 → 游戏键位」中修改，并和游戏内保持一致。` });
  }

  const start = enabledTask(instance, '__MXU_PRETASK__start_game');
  const launcher = start?.optionValues.StartGameProgram;
  const path = launcher?.type === 'input' ? launcher.values.path?.trim() : '';
  if (path && isTauri()) {
    const exists = await invoke<boolean>('local_file_exists', { filename: path }).catch(() => true);
    if (!exists) {
      issues.push({ id: 'launcher-missing', title: '找不到填写的官方启动器',
        detail: `「${path}」不存在。可以重新选择 launcher.exe，或清空让软件自动查找。` });
    }
  }

  return issues.filter((issue) => !issue.once || !state.preflightAcks.includes(issue.id));
}
