import type { OptionValue } from './interface';

export const explorationFeatures = [
  { key: 'AssistPickup', label: '自动拾取', description: '出现拾取提示时自动交互。', defaultValue: true },
  { key: 'AssistClear', label: '清洁动画跳过', description: '出现跳过提示时自动跳过动画。', defaultValue: true },
  { key: 'AssistDialogue', label: '自动对话', description: '自动推进对白，对话分支由你选择。', defaultValue: true },
  { key: 'AssistFishing', label: '自动钓鱼', description: '手动抛竿后自动提竿、拉线和收线。普通钓鱼最多 3 竿，家园钓星最多 5 竿。', defaultValue: false },
  { key: 'AssistMapStar', label: '奇想星地图标记', description: '显示美鸭梨账号中尚未收集的奇想星。', defaultValue: false },
  { key: 'AssistMapDewdrop', label: '灵感露珠地图标记', description: '显示美鸭梨账号中尚未收集的灵感露珠。', defaultValue: false },
] as const;

export type ExplorationFeature = typeof explorationFeatures[number]['key'];
export type ExplorationSettings = Record<ExplorationFeature, boolean>;
export const EXPLORATION_INSTANCE_ID = 'maanikki-exploration';

export function normalizeExplorationSettings(
  saved?: Partial<ExplorationSettings>,
  legacy?: Record<string, OptionValue>,
): ExplorationSettings {
  return Object.fromEntries(explorationFeatures.map(({ key, defaultValue }) => {
    const old = legacy?.[key];
    const value = typeof saved?.[key] === 'boolean' ? saved[key]
      : old?.type === 'switch' ? old.value
      : old?.type === 'select' ? old.caseName === 'Yes' : defaultValue;
    return [key, value];
  })) as ExplorationSettings;
}
