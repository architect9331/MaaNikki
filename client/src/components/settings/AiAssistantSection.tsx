import { useState } from 'react';
import { Bot, Check, Copy } from 'lucide-react';
import { toast } from 'sonner';
import { useAppStore } from '@/stores/appStore';
import { SwitchButton } from '@/components/FormControls';
import { isTauri } from '@/utils/paths';

function CopyBlock({ title, text }: { title: string; text: string }) {
  const [copied, setCopied] = useState(false);
  const copy = () => void navigator.clipboard.writeText(text).then(() => {
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  }).catch(() => toast.error('复制失败，请手动选中'));
  return <div>
    <div className="mb-1 flex items-center justify-between gap-2">
      <span className="text-xs font-medium text-text-secondary">{title}</span>
      <button type="button" onClick={copy} aria-label={`复制${title}`}
        className="flex items-center gap-1 rounded px-2 py-0.5 text-xs text-text-secondary hover:bg-bg-hover">
        {copied ? <Check className="h-3.5 w-3.5 text-success" /> : <Copy className="h-3.5 w-3.5" />}{copied ? '已复制' : '复制'}
      </button>
    </div>
    <pre className="overflow-x-auto whitespace-pre rounded-lg bg-bg-primary p-2.5 text-xs leading-5 text-text-primary">{text}</pre>
  </div>;
}

/** Connection details for the user's own AI tool; this app never calls a model. */
export function AiAssistantSection() {
  const basePath = useAppStore((state) => state.basePath);
  const [allowWrite, setAllowWrite] = useState(false);
  if (!isTauri()) return null;

  const root = basePath.replace(/[\\/]+$/, '').replace(/\//g, '\\');
  const python = `${root}\\python\\python.exe`;
  const server = `${root}\\ai\\mcp_server.py`;
  const flag = allowWrite ? ' --allow-write' : '';
  const toml = (value: string) => value.replace(/\\/g, '\\\\');
  const codex = [`[mcp_servers.maanikki]`, `command = "${toml(python)}"`,
    `args = ["${toml(server)}"${allowWrite ? ', "--allow-write"' : ''}]`].join('\n');

  return <section id="section-ai-assistant" className="space-y-4 scroll-mt-4">
    <h2 className="flex items-center gap-2 text-lg font-semibold text-text-primary"><Bot className="h-5 w-5 text-accent" />AI 助手</h2>
    <div className="space-y-3 rounded-xl border border-border bg-bg-secondary p-4">
      <p className="text-xs leading-5 text-text-muted">
        让你自己的 Codex 或 Claude Code 帮你完成配置。它们通过 MCP 读取任务、选项和手册；软件不会调用任何模型，也不会上传内容。
      </p>
      <div className="flex items-center justify-between gap-3">
        <div>
          <span className="text-sm font-medium text-text-primary">允许 AI 修改配置</span>
          <p className="mt-0.5 text-xs leading-5 text-text-muted">
            开启后生成的命令可以写入任务勾选和选项。写入前自动备份，关闭 MaaNikki 后才能写入；AI 无法启动任务或操作游戏。
          </p>
        </div>
        <SwitchButton value={allowWrite} onChange={setAllowWrite} />
      </div>
      <CopyBlock title="Claude Code" text={`claude mcp add maanikki -- "${python}" "${server}"${flag}`} />
      <CopyBlock title="Codex（~/.codex/config.toml）" text={codex} />
      <CopyBlock title="Skill：复制到 ~/.claude/skills 或 ~/.codex/skills" text={`${root}\\ai\\skills\\maanikki-setup`} />
      <CopyBlock title="然后可以这样说" text="请用 maanikki 帮我完成 MaaNikki 的首次配置，我想每天跑完日常并追全祝福闪光。" />
    </div>
  </section>;
}
