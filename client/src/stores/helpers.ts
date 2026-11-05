import type {
  Instance,
  OptionValue,
  OptionDefinition,
  PresetOptionValue,
  SelectedTask,
  TaskItem,
} from '@/types/interface';
import { findSwitchCase } from '@/utils/optionHelpers';
import type { AppState } from './types';

/** 生成唯一 ID */
export const generateId = () => Math.random().toString(36).substring(2, 9);

/** 只替换 id 匹配的元素，其余元素保持原引用 */
export const mapById = <T extends { id: string }>(
  items: T[],
  id: string,
  update: (item: T) => T,
): T[] => items.map((item) => (item.id === id ? update(item) : item));

/** 更新指定实例（其余实例保持原引用） */
export const mapInstance = (
  instances: Instance[],
  instanceId: string,
  update: (instance: Instance) => Instance,
): Instance[] => mapById(instances, instanceId, update);

/**
 * 更新指定实例的任务列表。
 * update 返回原任务列表（同一引用）时保持该实例不变，便于表达“条件不满足则不修改”。
 */
export const mapInstanceTasks = (
  instances: Instance[],
  instanceId: string,
  update: (tasks: SelectedTask[]) => SelectedTask[],
): Instance[] =>
  mapInstance(instances, instanceId, (instance) => {
    const tasks = update(instance.selectedTasks);
    return tasks === instance.selectedTasks ? instance : { ...instance, selectedTasks: tasks };
  });

/** 更新指定实例中的指定任务 */
export const mapTask = (
  instances: Instance[],
  instanceId: string,
  taskId: string,
  update: (task: SelectedTask) => SelectedTask,
): Instance[] =>
  mapInstanceTasks(instances, instanceId, (tasks) => mapById(tasks, taskId, update));

/**
 * 将数组中 from 位置的元素移动到 to 位置，返回新数组。
 * 下标越界或位置未变化时返回原数组（同一引用）。
 */
export const moveItem = <T>(items: T[], from: number, to: number): T[] => {
  const len = items.length;
  if (from < 0 || from >= len || to < 0 || to >= len || from === to) return items;
  const next = [...items];
  const [moved] = next.splice(from, 1);
  next.splice(to, 0, moved);
  return next;
};

/** 创建默认选项值 */
export const createDefaultOptionValue = (optionDef: OptionDefinition): OptionValue => {
  if (optionDef.type === 'input') {
    const values: Record<string, string> = {};
    optionDef.inputs.forEach((input) => {
      values[input.name] = input.default || '';
    });
    return { type: 'input', values };
  }

  if (optionDef.type === 'hotkey') {
    const values: Record<string, string> = {};
    optionDef.hotkeys.forEach((input) => {
      values[input.name] = input.default || '';
    });
    return { type: 'hotkey', values };
  }

  if (optionDef.type === 'switch') {
    const defaultCase = optionDef.default_case || optionDef.cases[0]?.name || 'Yes';
    const isYes = ['Yes', 'yes', 'Y', 'y'].includes(defaultCase);
    return { type: 'switch', value: isYes };
  }

  if (optionDef.type === 'checkbox') {
    const defaultCases = optionDef.default_case || [];
    return { type: 'checkbox', caseNames: [...defaultCases] };
  }

  // select type (default)
  const defaultCase = optionDef.default_case || optionDef.cases[0]?.name || '';
  return { type: 'select', caseName: defaultCase };
};

export type OptionValueSanitizeLogger = (message: string) => void;

/**
 * 校验保存的选项值是否仍符合当前 Project Interface 定义。
 * 返回 null 表示应丢弃保存值并回退到当前默认值。
 */
export const sanitizeOptionValue = (
  optionKey: string,
  value: OptionValue,
  allOptions: Record<string, OptionDefinition>,
  warn?: OptionValueSanitizeLogger,
): OptionValue | null => {
  const optionDef = allOptions[optionKey];
  if (!optionDef) {
    warn?.(`选项 "${optionKey}" 已不存在，已丢弃保存值`);
    return null;
  }

  const expectedType = optionDef.type || 'select';
  if (value.type !== expectedType) {
    warn?.(
      `选项 "${optionKey}" 的类型已从 "${value.type}" 变更为 "${expectedType}"，已重置为默认值`,
    );
    return null;
  }

  if ((!optionDef.type || optionDef.type === 'select') && value.type === 'select') {
    const caseExists = optionDef.cases.some((caseDef) => caseDef.name === value.caseName);
    if (!caseExists) {
      warn?.(`选项 "${optionKey}" 的 case "${value.caseName}" 已不存在，已重置为默认值`);
      return null;
    }
    return value;
  }

  if (optionDef.type === 'checkbox' && value.type === 'checkbox') {
    const validNames = new Set(optionDef.cases.map((caseDef) => caseDef.name));
    const caseNames = value.caseNames.filter((caseName) => validNames.has(caseName));
    if (caseNames.length !== value.caseNames.length) {
      const removedNames = value.caseNames.filter((caseName) => !validNames.has(caseName));
      warn?.(`选项 "${optionKey}" 包含已不存在的 case "${removedNames.join(', ')}"，已过滤`);
    }
    if (value.caseNames.length > 0 && caseNames.length === 0) {
      warn?.(`选项 "${optionKey}" 保存的 case 已全部失效，已重置为默认值`);
      return null;
    }
    return { type: 'checkbox', caseNames };
  }

  if (optionDef.type === 'input' && value.type === 'input') {
    const validNames = new Set(optionDef.inputs.map((input) => input.name));
    const values: Record<string, string> = {};
    for (const [name, val] of Object.entries(value.values)) {
      if (validNames.has(name)) values[name] = val;
    }
    const encryptedValues: Record<string, string> = {};
    if (value.encryptedValues) {
      for (const [name, val] of Object.entries(value.encryptedValues)) {
        if (validNames.has(name)) encryptedValues[name] = val;
      }
    }
    return {
      type: 'input',
      values,
      ...(Object.keys(encryptedValues).length > 0 ? { encryptedValues } : {}),
    };
  }

  return value;
};

export const sanitizeOptionValues = (
  optionValues: Record<string, OptionValue>,
  allOptions: Record<string, OptionDefinition>,
  warn?: OptionValueSanitizeLogger,
): Record<string, OptionValue> => {
  const cleaned: Record<string, OptionValue> = {};
  for (const [optionKey, value] of Object.entries(optionValues)) {
    const sanitized = sanitizeOptionValue(optionKey, value, allOptions, warn);
    if (sanitized) {
      cleaned[optionKey] = sanitized;
    }
  }
  return cleaned;
};

/**
 * 递归初始化所有选项（包括嵌套选项）的默认值
 * @param optionKeys 顶层选项键列表
 * @param allOptions 所有选项定义
 * @param result 结果对象（用于递归累积）
 */
export const initializeAllOptionValues = (
  optionKeys: string[],
  allOptions: Record<string, OptionDefinition>,
  result: Record<string, OptionValue> = {},
): Record<string, OptionValue> => {
  for (const optKey of optionKeys) {
    const optDef = allOptions[optKey];
    if (!optDef) continue;

    // 如果已经初始化过，跳过（避免循环引用）
    if (result[optKey]) continue;

    // 创建当前选项的默认值
    result[optKey] = createDefaultOptionValue(optDef);

    // 处理嵌套选项：根据当前默认值找到对应的 case，递归初始化其子选项
    if (optDef.type === 'switch' || optDef.type === 'select' || !optDef.type) {
      const currentValue = result[optKey];
      let selectedCase;

      if (optDef.type === 'switch' && 'cases' in optDef) {
        const isChecked = currentValue.type === 'switch' && currentValue.value;
        selectedCase = findSwitchCase(optDef.cases, isChecked);
      } else if ('cases' in optDef) {
        const caseName =
          currentValue.type === 'select' ? currentValue.caseName : optDef.cases?.[0]?.name;
        selectedCase = optDef.cases?.find((c) => c.name === caseName);
      }

      // 递归初始化嵌套选项
      if (selectedCase?.option && selectedCase.option.length > 0) {
        initializeAllOptionValues(selectedCase.option, allOptions, result);
      }
    }
  }

  return result;
};

/**
 * 当选项值改变时，为其激活的 case 初始化尚未存在的嵌套子选项默认值。
 */
export const initializeNestedOptionsForChange = (
  optionKey: string,
  value: OptionValue,
  allOptions: Record<string, OptionDefinition> | undefined,
  targetValues: Record<string, OptionValue>,
): void => {
  if (!allOptions) return;
  const optDef = allOptions[optionKey];
  if (
    !optDef ||
    (optDef.type !== 'switch' && optDef.type !== 'select' && optDef.type !== undefined) ||
    !('cases' in optDef)
  ) {
    return;
  }

  const selectedCase =
    optDef.type === 'switch'
      ? findSwitchCase(optDef.cases, value.type === 'switch' && value.value)
      : optDef.cases?.find(
          (c) => c.name === (value.type === 'select' ? value.caseName : optDef.cases?.[0]?.name),
        );

  if (!selectedCase?.option || selectedCase.option.length === 0) return;

  for (const nestedKey of selectedCase.option) {
    if (!targetValues[nestedKey] && allOptions[nestedKey]) {
      Object.assign(targetValues, initializeAllOptionValues([nestedKey], allOptions));
    }
  }
};

/**
 * v2.3.0: 将预设的选项值转换为运行时 OptionValue
 */
export const convertPresetOptionValue = (
  optionKey: string,
  presetValue: PresetOptionValue,
  allOptions: Record<string, OptionDefinition>,
): OptionValue | null => {
  const optDef = allOptions[optionKey];
  if (!optDef) return null;

  if (optDef.type === 'switch' && typeof presetValue === 'string') {
    const isYes = ['Yes', 'yes', 'Y', 'y'].includes(presetValue);
    return { type: 'switch', value: isYes };
  }

  if (optDef.type === 'checkbox' && Array.isArray(presetValue)) {
    const validNames = new Set((optDef.cases || []).map((c) => c.name));
    const caseNames = (presetValue as string[]).filter((name) => validNames.has(name));
    return { type: 'checkbox', caseNames };
  }

  if (optDef.type === 'input' && typeof presetValue === 'object' && !Array.isArray(presetValue)) {
    return { type: 'input', values: presetValue as Record<string, string> };
  }

  if (optDef.type === 'hotkey' && typeof presetValue === 'object' && !Array.isArray(presetValue)) {
    return { type: 'hotkey', values: presetValue as Record<string, string> };
  }

  if ((!optDef.type || optDef.type === 'select') && typeof presetValue === 'string') {
    return { type: 'select', caseName: presetValue };
  }

  return null;
};

/** 获取实例当前控制器和资源名称（统一 fallback 顺序） */
export const getCurrentControllerAndResource = (
  state: Pick<
    AppState,
    'projectInterface' | 'instances' | 'selectedController' | 'selectedResource'
  >,
  instanceId: string,
) => {
  const pi = state.projectInterface;
  const instance = state.instances.find((i) => i.id === instanceId);

  const controllerName =
    state.selectedController[instanceId] || instance?.controllerName || pi?.controller[0]?.name;

  const resourceName =
    state.selectedResource[instanceId] || instance?.resourceName || pi?.resource[0]?.name;

  return { controllerName, resourceName };
};

/**
 * 当前控制器/资源名称未设置时如何判定兼容性：
 * - 'allow'：视为兼容。用于界面展示，未选择时不把任务/选项标记为不兼容。
 * - 'deny'：视为不兼容。用于生成运行配置等场景，无法确认兼容时不下发。
 */
export type UnsetScopePolicy = 'allow' | 'deny';

/** 带 controller / resource 限制的定义（任务、选项、资源等） */
interface ScopeRestricted {
  controller?: string[];
  resource?: string[];
}

/** 判断名称限制列表是否允许给定名称；限制列表为空或未配置时始终允许 */
const isNameAllowed = (
  allowed: string[] | undefined,
  name: string | undefined,
  unset: UnsetScopePolicy,
): boolean => {
  if (!allowed || allowed.length === 0) return true;
  if (!name) return unset === 'allow';
  return allowed.includes(name);
};

/** 检查定义是否允许当前控制器 */
export const isControllerAllowed = (
  def: ScopeRestricted | null | undefined,
  controllerName: string | undefined,
  unset: UnsetScopePolicy,
): boolean => isNameAllowed(def?.controller, controllerName, unset);

/** 检查定义是否允许当前资源 */
export const isResourceAllowed = (
  def: ScopeRestricted | null | undefined,
  resourceName: string | undefined,
  unset: UnsetScopePolicy,
): boolean => isNameAllowed(def?.resource, resourceName, unset);

/** 检查定义是否同时允许当前控制器和资源 */
export const isScopeCompatible = (
  def: ScopeRestricted | null | undefined,
  controllerName: string | undefined,
  resourceName: string | undefined,
  unset: UnsetScopePolicy,
): boolean =>
  isControllerAllowed(def, controllerName, unset) && isResourceAllowed(def, resourceName, unset);

/** 检查任务是否与指定控制器/资源兼容（未设置的控制器/资源视为兼容） */
export const isTaskCompatible = (
  taskDef: TaskItem | undefined | null,
  controllerName: string | undefined,
  resourceName: string | undefined,
): boolean => isScopeCompatible(taskDef, controllerName, resourceName, 'allow');
