/**
 * 解析任务启动事件要执行的实例。
 *
 * 选择阶段确定的实例 ID 会随事件一起传递（detail.instanceId），执行阶段必须按该 ID
 * 重新查找；查不到就停止，绝不回退到「当前活动实例」，否则选择与执行之间发生切换时
 * 会操作到错误账号。未携带 ID 的入口（快捷键、托盘、定时）保持原有语义：使用当前活动实例。
 */
export interface StartTaskInstanceLike {
  id: string;
}

export type StartTaskTarget<T extends StartTaskInstanceLike> =
  | { kind: 'requested'; instance: T }
  | { kind: 'active'; instance: T }
  | { kind: 'missing'; instanceId: string }
  | { kind: 'none' };

export function resolveStartTaskTarget<T extends StartTaskInstanceLike>(
  instances: readonly T[],
  requestedInstanceId: string | null | undefined,
  activeInstance: T | null | undefined,
): StartTaskTarget<T> {
  if (requestedInstanceId !== null && requestedInstanceId !== undefined) {
    const instance = instances.find((i) => i.id === requestedInstanceId);
    if (!instance) {
      return { kind: 'missing', instanceId: requestedInstanceId };
    }
    return { kind: 'requested', instance };
  }
  if (activeInstance) {
    return { kind: 'active', instance: activeInstance };
  }
  return { kind: 'none' };
}
