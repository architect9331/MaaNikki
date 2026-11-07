import { useCallback, useEffect, useMemo, useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import {
  ArrowLeft,
  Award,
  BarChart3,
  CalendarHeart,
  Clock,
  Compass,
  RefreshCw,
  Shirt,
  Sparkles,
} from 'lucide-react';
import { useAccountStateStore, crownProgress } from '@/stores/accountStateStore';
import { useAppStore } from '@/stores/appStore';
import { getBlessingProgress } from '@/utils/blessingProgress';
import { getInterfaceLangKey } from '@/i18n';
import { isTauri } from '@/utils/paths';

interface TaskStatRecord {
  runs: number;
  completed: number;
  total_duration_secs: number;
  estimated_saved_secs: number;
  last_run_at?: string | null;
}

interface TaskStatisticsData {
  version: number;
  first_recorded_at?: string | null;
  total_runs: number;
  completed_runs: number;
  total_duration_secs: number;
  total_saved_secs: number;
  active_days: string[];
  tasks: Record<string, TaskStatRecord>;
}

const TASK_FALLBACK_LABELS: Record<string, string> = {
  __MXU_PRETASK__start_game: '启动游戏',
  start_game: '启动游戏',
  dig: '美鸭梨挖掘',
  zhaoxi: '朝夕心愿',
  realm_challenge: '幻境挑战',
  starlight_crystals: '星光结晶收集',
  xinghai: '星海拾光',
  home: '家园日常',
  monthly_pass: '奇迹之旅与月卡',
  mailbox: '邮箱领取',
  weekly_free_gifts: '商城免费礼包',
  monthly_crystal_exchange: '每月之约兑换',
  crown: '奇迹之冠巅峰赛',
  glow_material_prep: '焕新材料刷取',
  exploration: '大世界采集',
  fishing: '自动钓鱼',
  custom_route: '自定义路线',
};

function formatDuration(seconds: number): string {
  const safe = Math.max(0, Math.round(seconds));
  if (safe < 60) return `${safe} 秒`;
  const hours = Math.floor(safe / 3600);
  const minutes = Math.max(1, Math.round((safe % 3600) / 60));
  if (hours === 0) return `${minutes} 分钟`;
  return `${hours} 小时 ${minutes} 分钟`;
}

function formatDateTime(iso?: string | null): string {
  if (!iso) return '暂无记录';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString('zh-CN', { hour12: false });
}

function companionDays(firstIso?: string | null, activeDaysCount = 0): number {
  if (!firstIso) return Math.max(1, activeDaysCount);
  const start = new Date(firstIso);
  if (Number.isNaN(start.getTime())) return Math.max(1, activeDaysCount);
  const diffDays = Math.floor((Date.now() - start.getTime()) / (24 * 3600 * 1000)) + 1;
  return Math.max(1, activeDaysCount, diffDays);
}
export function TaskStatsPage({ onClose }: { onClose: () => void }) {
  const account = useAccountStateStore((state) => state.data);
  const projectInterface = useAppStore((state) => state.projectInterface);
  const resolveI18nText = useAppStore((state) => state.resolveI18nText);
  const language = useAppStore((state) => state.language);
  const langKey = getInterfaceLangKey(language);

  const [stats, setStats] = useState<TaskStatisticsData | null>(null);
  const [loading, setLoading] = useState(true);

  const loadStats = useCallback(async () => {
    if (!isTauri()) {
      setLoading(false);
      return;
    }
    try {
      const result = await invoke<TaskStatisticsData>('get_task_statistics');
      setStats(result);
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadStats();
    if (!isTauri()) return;
    let dispose: (() => void) | undefined;
    void listen('task-statistics-changed', () => {
      void loadStats();
    }).then((fn) => {
      dispose = fn;
    });
    return () => dispose?.();
  }, [loadStats]);

  const resolveTaskLabel = useCallback(
    (taskName: string) => {
      const def = projectInterface?.task?.find((item) => item.name === taskName);
      if (def?.label) {
        const resolved = resolveI18nText(def.label, langKey);
        if (resolved) return resolved;
      }
      return TASK_FALLBACK_LABELS[taskName] ?? taskName;
    },
    [projectInterface?.task, resolveI18nText, langKey],
  );

  const blessing = useMemo(() => getBlessingProgress(account), [account]);
  const crown = useMemo(() => crownProgress(account), [account]);
  const pearpalSummary = account?.observations?.pearpal_notebook?.summary;

  const taskRows = useMemo(() => {
    const entries = Object.entries(stats?.tasks ?? {});
    return entries
      .map(([name, record]) => ({
        name,
        label: resolveTaskLabel(name),
        ...record,
      }))
      .sort(
        (a, b) =>
          b.estimated_saved_secs - a.estimated_saved_secs ||
          b.runs - a.runs ||
          a.label.localeCompare(b.label, 'zh-CN'),
      );
  }, [stats?.tasks, resolveTaskLabel]);

  const totalSavedSecs = stats?.total_saved_secs ?? 0;
  const totalDurationSecs = stats?.total_duration_secs ?? 0;
  const totalRuns = stats?.total_runs ?? 0;
  const completedRuns = stats?.completed_runs ?? 0;
  const activeDaysCount = stats?.active_days?.length ?? 0;
  const daysWithUser = companionDays(stats?.first_recorded_at, activeDaysCount);
  const totalEmbryos = useMemo(
    () => Object.values(blessing.partEmbryos ?? {}).reduce((sum, count) => sum + count, 0),
    [blessing.partEmbryos],
  );
  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      <div className="flex h-12 shrink-0 items-center justify-between border-b border-border bg-bg-secondary px-4">
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={onClose}
            aria-label="返回主界面"
            className="rounded-lg p-1.5 hover:bg-bg-hover"
          >
            <ArrowLeft className="h-5 w-5 text-text-secondary" />
          </button>
          <BarChart3 className="h-5 w-5 text-accent" />
          <h1 className="text-base font-semibold text-text-primary">MAA 运行任务统计与纪念</h1>
        </div>
        <button
          type="button"
          onClick={() => void loadStats()}
          className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-bg-primary px-2.5 py-1 text-xs font-medium text-text-secondary transition-colors hover:bg-bg-hover"
        >
          <RefreshCw className="h-3.5 w-3.5" />
          <span>刷新</span>
        </button>
      </div>

      <div className="flex-1 overflow-y-auto p-6">
        <div className="mx-auto max-w-5xl space-y-5">
          {/* 纪念概览卡片 */}
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <div className="rounded-xl border border-border bg-bg-secondary p-4">
              <div className="flex items-center gap-2 text-xs text-text-muted">
                <Clock className="h-4 w-4 text-accent" />
                <span>累计为你节省时间</span>
              </div>
              <div className="mt-2 text-2xl font-bold tabular-nums text-text-primary">
                {formatDuration(totalSavedSecs)}
              </div>
              <div className="mt-1 text-xs text-text-muted">
                后台自动运行 {formatDuration(totalDurationSecs)} · 免去重复机械跑图与菜单操作
              </div>
            </div>

            <div className="rounded-xl border border-border bg-bg-secondary p-4">
              <div className="flex items-center gap-2 text-xs text-text-muted">
                <CalendarHeart className="h-4 w-4 text-accent" />
                <span>相伴与执行里程碑</span>
              </div>
              <div className="mt-2 text-2xl font-bold tabular-nums text-text-primary">
                {totalRuns} <span className="text-sm font-normal text-text-secondary">次任务</span>
              </div>
              <div className="mt-1 text-xs text-text-muted">
                已相伴 {daysWithUser} 天（活跃执行 {activeDaysCount} 天）· 成功完成 {completedRuns} 次
              </div>
            </div>

            <div className="rounded-xl border border-border bg-bg-secondary p-4">
              <div className="flex items-center gap-2 text-xs text-text-muted">
                <Sparkles className="h-4 w-4 text-accent" />
                <span>祝福闪光收集纪念</span>
              </div>
              <div className="mt-2 text-2xl font-bold tabular-nums text-text-primary">
                {blessing.owned.size} <span className="text-sm font-normal text-text-secondary">/ {blessing.target} 色</span>
              </div>
              <div className="mt-1 text-xs text-text-muted">
                {totalEmbryos > 0
                  ? `当前仓库共有未升满胚子 ${totalEmbryos} 件`
                  : blessing.known
                    ? '已完成本地或美鸭梨闪光记录同步'
                    : '运行幻境挑战或美鸭梨核对后自动记录'}
              </div>
            </div>

            <div className="rounded-xl border border-border bg-bg-secondary p-4">
              <div className="flex items-center gap-2 text-xs text-text-muted">
                <Award className="h-4 w-4 text-accent" />
                <span>奇迹大陆收集足迹</span>
              </div>
              <div className="mt-2 text-2xl font-bold tabular-nums text-text-primary">
                {crown ? `${crown.stars}/${crown.total} 星` : pearpalSummary?.cloth_num ? `${pearpalSummary.cloth_num} 件` : '记录中'}
              </div>
              <div className="mt-1 text-xs text-text-muted">
                {pearpalSummary
                  ? `美鸭梨手账：游历 ${pearpalSummary.login_days ?? 0} 天 · 服装 ${pearpalSummary.cloth_num ?? 0} 件`
                  : crown
                    ? '本期奇迹之冠巅峰赛最佳星数'
                    : '随日常与巅峰赛运行自动点亮'}
              </div>
            </div>
          </div>

          {/* 美鸭梨手账纪念横幅（如已同步） */}
          {pearpalSummary && (
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border bg-bg-secondary px-4 py-3 text-xs text-text-secondary">
              <div className="flex items-center gap-2">
                <Compass className="h-4 w-4 text-accent" />
                <span className="font-medium text-text-primary">美鸭梨大世界探索纪念</span>
              </div>
              <div className="flex flex-wrap items-center gap-x-5 gap-y-1 tabular-nums">
                {pearpalSummary.login_days != null && <span>累计登录：{pearpalSummary.login_days} 天</span>}
                {pearpalSummary.cloth_num != null && <span>收集服装：{pearpalSummary.cloth_num} 件</span>}
                {pearpalSummary.designdrawing_num != null && <span>设计图：{pearpalSummary.designdrawing_num} 张</span>}
                {pearpalSummary.box_num != null && <span>开启宝箱：{pearpalSummary.box_num} 个</span>}
                {pearpalSummary.achievement_num != null && <span>达成成就：{pearpalSummary.achievement_num} 项</span>}
              </div>
            </div>
          )}

          {/* 各任务执行次数与节省时间明细 */}
          <div className="rounded-xl border border-border bg-bg-secondary p-5">
            <div className="flex items-center justify-between gap-2">
              <div>
                <h2 className="text-sm font-semibold text-text-primary">各任务执行统计明细</h2>
                <p className="mt-0.5 text-xs text-text-muted">
                  按为你节省的手动操作时间排序，记录每次自动化任务的执行次数与最近执行时刻。
                </p>
              </div>
              {stats?.first_recorded_at && (
                <span className="text-xs text-text-muted">
                  首次记录：{formatDateTime(stats.first_recorded_at)}
                </span>
              )}
            </div>

            {loading ? (
              <div className="py-10 text-center text-sm text-text-muted">正在读取统计数据…</div>
            ) : taskRows.length === 0 ? (
              <div className="mt-4 rounded-lg border border-border bg-bg-primary p-6 text-center text-sm text-text-muted">
                <Shirt className="mx-auto mb-2 h-6 w-6 text-text-muted/60" />
                <p>还没有任务运行记录。开始运行日常任务、幻境挑战或巅峰赛后，这里会自动累计执行次数与节省时间。</p>
              </div>
            ) : (
              <div className="mt-4 divide-y divide-border">
                {taskRows.map((row) => {
                  const share = totalSavedSecs > 0
                    ? Math.min(100, Math.max(3, Math.round((row.estimated_saved_secs / totalSavedSecs) * 100)))
                    : 0;
                  return (
                    <div key={row.name} className="py-3 first:pt-0 last:pb-0">
                      <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
                        <div className="flex items-center gap-2">
                          <span className="font-medium text-text-primary">{row.label}</span>
                          <span className="rounded-md bg-bg-tertiary px-2 py-0.5 text-xs tabular-nums text-text-secondary">
                            执行 {row.runs} 次{row.completed !== row.runs ? `（完成 ${row.completed} 次）` : ''}
                          </span>
                        </div>
                        <div className="flex items-center gap-4 text-xs tabular-nums text-text-secondary">
                          <span>
                            节省约 <strong className="font-semibold text-accent">{formatDuration(row.estimated_saved_secs)}</strong>
                          </span>
                          <span className="text-text-muted">
                            运行耗时 {formatDuration(row.total_duration_secs)}
                          </span>
                        </div>
                      </div>
                      <div className="mt-2 flex items-center gap-3">
                        <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-bg-tertiary">
                          <div
                            className="h-full rounded-full bg-accent transition-all"
                            style={{ width: `${share}%` }}
                          />
                        </div>
                        <span className="shrink-0 text-[11px] tabular-nums text-text-muted">
                          最近：{formatDateTime(row.last_run_at)}
                        </span>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
