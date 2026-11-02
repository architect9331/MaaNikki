import { useRef, useState } from 'react';
import { ArrowRight, Check, ShieldAlert, Sparkles } from 'lucide-react';
import clsx from 'clsx';
import { useAppStore } from '@/stores/appStore';
import { ModalPortal, useModalFocusTrap } from '@/components/ui/Modal';

/** Raise this and the copy in App.tsx when the notice changes materially. */
const SETUP_VERSION = 1;

const TASK_GROUPS = [
  { id: 'daily', title: '每日任务', detail: '挖掘、朝夕心愿、星光结晶、星海拾光、家园和奇迹之旅',
    tasks: ['dig', 'zhaoxi', 'starlight_crystals', 'xinghai', 'home', 'monthly_pass', 'mailbox'] },
  { id: 'realm', title: '幻境挑战', detail: '每周幻境和活跃能量', tasks: ['realm_challenge'] },
  { id: 'shop', title: '商城', detail: '每周免费礼包和每月五抽兑换', tasks: ['weekly_free_gifts', 'monthly_crystal_exchange'] },
  { id: 'crown', title: '奇迹之冠巅峰赛', detail: '用游戏推荐搭配挑战已解锁关卡', tasks: ['crown'] },
] as const;

function Toggle({ checked, onChange, title, detail }: {
  checked: boolean; onChange: (value: boolean) => void; title: string; detail?: string;
}) {
  return <label className={clsx('flex cursor-pointer items-start gap-3 rounded-lg border p-3 transition-colors',
    checked ? 'border-accent bg-accent/10' : 'border-border bg-bg-primary hover:bg-bg-hover')}>
    <input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} className="mt-0.5 h-4 w-4 accent-[var(--color-accent)]" />
    <span className="min-w-0">
      <span className="block text-sm font-medium text-text-primary">{title}</span>
      {detail && <span className="mt-0.5 block text-xs leading-5 text-text-secondary">{detail}</span>}
    </span>
  </label>;
}

/**
 * First launch: notice, goals and game preparation. Users who already had the
 * app only confirm the notice, and their task choices stay untouched.
 */
export function FirstRunSetup() {
  const newUser = useAppStore((state) => !state.onboardingCompleted);
  const setSetupVersion = useAppStore((state) => state.setSetupVersion);
  const acknowledgePreflight = useAppStore((state) => state.acknowledgePreflight);
  const panelRef = useRef<HTMLDivElement>(null);
  const [step, setStep] = useState(0);
  const [accepted, setAccepted] = useState(false);
  const [groups, setGroups] = useState<Record<string, boolean>>({ daily: true, realm: true, shop: true, crown: true });
  const [collection, setCollection] = useState(false);
  const [decompose, setDecompose] = useState(true);
  // The notice must be confirmed, so Escape does not dismiss it.
  useModalFocusTrap({ containerRef: panelRef, onEscape: () => {} });

  const finish = () => {
    const state = useAppStore.getState();
    if (newUser) {
      const instance = state.instances.find((item) => item.id === state.activeInstanceId) ?? state.instances[0];
      for (const task of instance?.selectedTasks ?? []) {
        const group = TASK_GROUPS.find((item) => (item.tasks as readonly string[]).includes(task.taskName));
        if (group && task.enabled !== groups[group.id]) state.toggleTaskEnabled(instance.id, task.id);
        if (task.taskName !== 'realm_challenge') continue;
        state.setTaskOptionValue(instance.id, task.id, 'CollectionMode', { type: 'switch', value: collection });
        if (collection) {
          state.setTaskOptionValue(instance.id, task.id, 'CollectionDecompose', { type: 'switch', value: decompose });
          acknowledgePreflight('collection-decompose');
        }
      }
    }
    setSetupVersion(SETUP_VERSION);
  };

  const notice = <div className="space-y-3 text-sm leading-6 text-text-secondary">
    <div className="flex gap-2.5 rounded-lg border border-warning/30 bg-warning/5 p-3">
      <ShieldAlert className="mt-1 h-4 w-4 shrink-0 text-warning" />
      <p>MaaNikki 是非官方的开源工具，与游戏开发商无关。它只通过截图和模拟键鼠操作游戏，不修改游戏文件，也不读写内存，但第三方工具仍可能不符合游戏规则。<strong className="font-medium text-text-primary">使用风险由你自行承担。</strong></p>
    </div>
    <Toggle checked={accepted} onChange={setAccepted} title="我已了解以上内容，继续使用" />
  </div>;

  const goals = <div className="space-y-2">
    <p className="text-sm text-text-secondary">选择想让 MaaNikki 处理的内容，之后也可以随时调整。</p>
    {TASK_GROUPS.map((group) => <Toggle key={group.id} checked={groups[group.id]} title={group.title} detail={group.detail}
      onChange={(value) => setGroups((current) => ({ ...current, [group.id]: value }))} />)}
    {groups.realm && <div className="space-y-2 rounded-lg border border-border p-3">
      <Toggle checked={collection} onChange={setCollection} title="五星全收集"
        detail="按预期新色选副本，刷取、升级并锁定新颜色。第一次运行会先整理全部祝福闪光。" />
      {collection && <Toggle checked={decompose} onChange={setDecompose} title="仓库满时分解五星祝福闪光"
        detail="一键分解未锁定的闪光，每批最多 100 件，且不可恢复。请提前锁定想保留的闪光。关闭后，仓库满时当天不刷本。" />}
    </div>}
  </div>;

  const preparation = <div className="space-y-3 text-sm leading-6 text-text-secondary">
    <p>开始前请确认：</p>
    <ul className="space-y-2">
      {['游戏画面为 16:9，推荐 1920×1080。',
        '已在「设置 → 任务设置 → 游戏键位」核对按键。',
        '所选任务涉及的地图、传送点、能力和关卡已经解锁。',
        ...(groups.realm && collection ? ['已清除游戏内祝福闪光仓库的筛选。'] : []),
      ].map((item) => <li key={item} className="flex gap-2"><Check className="mt-1 h-4 w-4 shrink-0 text-accent" />{item}</li>)}
    </ul>
    <p className="rounded-lg bg-bg-primary p-3 text-xs leading-5">在用 Codex 或 Claude Code？可以在「设置 → AI 助手」接入，让它们帮你完成配置。</p>
  </div>;

  const steps = newUser
    ? [{ title: '使用须知', body: notice }, { title: '你想用它做什么', body: goals }, { title: '准备游戏', body: preparation }]
    : [{ title: '使用须知', body: notice }];
  const last = step === steps.length - 1;
  const blocked = step === 0 && !accepted;

  return <ModalPortal>
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4 backdrop-blur-sm">
      <div ref={panelRef} role="dialog" aria-modal="true" aria-labelledby="first-run-title"
        className="flex max-h-[85vh] w-full max-w-lg flex-col rounded-2xl bg-bg-secondary shadow-2xl">
        <div className="flex items-center gap-2 border-b border-border px-6 py-4">
          <Sparkles className="h-5 w-5 text-accent" />
          <h2 id="first-run-title" className="flex-1 text-lg font-semibold text-text-primary">{steps[step].title}</h2>
          {steps.length > 1 && <span className="text-xs tabular-nums text-text-muted">{step + 1} / {steps.length}</span>}
        </div>
        <div className="flex-1 overflow-y-auto px-6 py-4">{steps[step].body}</div>
        <div className="flex gap-2 border-t border-border px-6 py-4">
          {step > 0 && <button type="button" onClick={() => setStep(step - 1)}
            className="rounded-lg bg-bg-tertiary px-4 py-2.5 text-sm font-medium text-text-secondary hover:bg-bg-hover">上一步</button>}
          <button type="button" disabled={blocked} onClick={() => last ? finish() : setStep(step + 1)}
            className="flex flex-1 items-center justify-center gap-1.5 rounded-lg bg-accent px-4 py-2.5 text-sm font-medium text-white hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-50">
            {last ? '开始使用' : <>下一步<ArrowRight className="h-4 w-4" /></>}
          </button>
        </div>
      </div>
    </div>
  </ModalPortal>;
}
