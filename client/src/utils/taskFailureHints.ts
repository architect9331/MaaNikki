// What to check after a task fails. Task-level messages above it name the failed
// step; these lines list the preconditions users can act on, from docs/manual.md.
const HINTS: Record<string, string> = {
  __MXU_PRETASK__start_game: '可在任务选项中填写官方启动器 launcher.exe 的路径，并确认启动器能手动打开游戏。',
  dig: '请确认美鸭梨挖掘已解锁。',
  zhaoxi: '请确认心愿涉及的地图和能力已解锁；小游戏和制作服装类心愿需要手动完成。',
  realm_challenge: '请确认关卡、困难难度和快速挑战已解锁，并清除祝福闪光仓库的筛选。',
  starlight_crystals: '请确认相关地图和传送点已解锁。',
  xinghai: '请确认星海相关的传送点和能力已解锁；缺分时可查看任务选项中的轮换表。',
  home: '请确认家园已解锁，并在任务选项中设置可用的采星能力方案。',
  monthly_pass: '请确认奇迹之旅入口可用。',
  mailbox: '请确认邮箱页面能正常打开。',
  weekly_free_gifts: '请确认商城页面能正常打开。',
  monthly_crystal_exchange: '请确认静谧水滴足够兑换。',
  crown: '请确认奇迹之冠已解锁且本期赛事正在开放。',
  fishing: '请先站在钓点面向水面，并确认已配置钓鱼能力方案。',
  blessing_lock: '请清除祝福闪光仓库的筛选后重试。',
};

export function taskFailureHint(taskName: string | undefined): string {
  const specific = taskName ? HINTS[taskName] : undefined;
  const followUp = '仍未解决可使用「导出日志」发到 QQ 群或 Issue。';
  return specific ? `${specific}${followUp}` : `请查看上方的提示。${followUp}`;
}
