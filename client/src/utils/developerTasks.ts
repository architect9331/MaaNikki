import type { TaskItem } from '@/types/interface';

export const ROUTE_TEST_ENTRY = 'MaaNikki_RouteTest';

export function isDeveloperTask(task: TaskItem | undefined): boolean {
  return task?.developer_only === true || task?.entry === ROUTE_TEST_ENTRY;
}

export function isTaskAvailable(task: TaskItem | undefined, devMode: boolean): boolean {
  return devMode || !isDeveloperTask(task);
}
