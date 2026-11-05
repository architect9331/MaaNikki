/**
 * 为 Promise 添加超时保护：超时后以给定错误拒绝，且无论成败都会清理计时器，
 * 避免遗留的 setTimeout 在原 Promise 已完成后仍触发未处理的 rejection。
 */
export function withTimeout<T>(
  promise: Promise<T>,
  ms: number,
  message = `Operation timed out after ${ms}ms`,
): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error(message)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => {
    if (timer !== undefined) clearTimeout(timer);
  });
}
