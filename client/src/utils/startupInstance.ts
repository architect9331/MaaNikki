/**
 * 启动实例选择（开机自启动 / 手动启动自动执行）
 *
 * 安全要求：命令行 `-i/--instance <实例名>` 指定了实例时，实例名必须精确匹配且唯一命中。
 * 找不到或重名时一律不启动——绝不能回退到「启动后自动执行」里配置的默认实例，
 * 否则在多账号托管场景下可能把任务派发到错误的账号上。
 */

export interface StartupInstanceLike {
  id: string;
  name: string;
}

export type StartupInstanceSelection =
  /** 命令行 --instance 唯一命中 */
  | { kind: 'cli'; instanceId: string }
  /** 未指定命令行参数，使用配置中的自动执行实例 */
  | { kind: 'config'; instanceId: string }
  /** 命令行指定了实例名，但找不到或重名：调用方必须跳过自动执行 */
  | { kind: 'invalid'; reason: 'not-found' | 'ambiguous'; name: string }
  /** 既没有命令行参数，也没有配置自动执行实例 */
  | { kind: 'none' };

export function selectStartupInstance(
  instances: ReadonlyArray<StartupInstanceLike>,
  cliInstanceName: string | null | undefined,
  configuredInstanceId: string | undefined,
): StartupInstanceSelection {
  // 命令行参数一旦出现（含空字符串），就以它为准：匹配失败时不回退到配置实例
  if (cliInstanceName !== null && cliInstanceName !== undefined) {
    const matches = instances.filter((instance) => instance.name === cliInstanceName);
    if (matches.length === 0) {
      return { kind: 'invalid', reason: 'not-found', name: cliInstanceName };
    }
    if (matches.length > 1) {
      return { kind: 'invalid', reason: 'ambiguous', name: cliInstanceName };
    }
    return { kind: 'cli', instanceId: matches[0].id };
  }

  if (configuredInstanceId) {
    return { kind: 'config', instanceId: configuredInstanceId };
  }

  return { kind: 'none' };
}
