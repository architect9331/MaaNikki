from __future__ import annotations

from datetime import datetime, timezone
import re
import cv2
from account_state import AccountState, period_key
from registration import agent_action
from task_control import TaskStopped, active_session
from maa.custom_action import CustomAction
from daily.runtime import Executors
from daily.gameplay import Gameplay
from daily.settings import ROOT

TARGETS = (("MaaNikki_Weekly_Level_Qiggeda", "奇格格达"), ("MaaNikki_Weekly_Level_Juan", "卷卷"))


def _record_weekly_state(rt, finished: int, total: int, completed_targets: list[str] | None = None):
    """Best-effort weekly progress observation using an already-known account UID."""
    session = active_session()
    uid = getattr(rt, "_account_uid", None) or (getattr(session, "account_uid", None) if session else None)
    if not uid:
        return
    try:
        account = AccountState(ROOT, uid, rt)
        period = period_key("weekly_free_gifts")
        now = datetime.now(timezone.utc).isoformat()

        def update(data):
            obs = data.setdefault("observations", {})
            prev = obs.get("weekly_realm")
            prev_targets = prev.get("completed_targets", []) if isinstance(prev, dict) and prev.get("period") == period else []
            merged = list(dict.fromkeys([*(prev_targets if isinstance(prev_targets, list) else []), *(completed_targets or [])]))
            obs["weekly_realm"] = {
                "period": period,
                "finished": max(0, int(finished)),
                "total": max(0, int(total)),
                "completed_targets": merged,
                "observed_at": now,
            }

        account.update(update)
    except Exception:
        pass


def read_count(runtime):
    for _ in range(3):
        result = runtime.recognize("MaaNikki_Weekly_CountOCR")
        raw = getattr(result.best_result, "text", "") if result and result.hit else ""
        raw = re.sub(r"[vV\s]", "", raw)
        match = re.fullmatch(r"(\d+)[/／](\d+)", raw)
        if match:
            finished, total = map(int, match.groups())
            if 0 <= finished <= total <= 10:
                return finished, total
        if not runtime.pause(.3):
            break
    return None


@agent_action("nikki.weekly_challenge")
class WeeklyChallengeAction(CustomAction):
    def run(self, context, argv):
        rt = Executors(context)
        combined = argv.node_name == "MaaNikki_RealmWeekly"
        recovered = False
        targets = [(node, label) for node, label in TARGETS
                   if (context.get_node_data(node) or {}).get("enabled", False)]

        def steps():
            nonlocal recovered
            # Do not reuse a previous run's conditional continuation if the
            # initial weekly-count read is unsafe this time.
            if combined and not context.override_pipeline({argv.node_name: {"on_error": []}}):
                rt.log("本周幻境设置未能应用，本次停止。")
                return False
            if not targets:
                rt.log("未选择每周幻境目标，跳过本周挑战。")
                return True
            if not rt.ui.calendar():
                return False
            count = read_count(rt)
            if count is None:
                image = rt.frame()
                if rt.hit("MaaNikki_CalendarReady", image=image) and rt.text(
                        [860, 175, 290, 60], "尚未解锁|未解锁", image=image):
                    rt.log("心之突破幻境尚未解锁，本次跳过。")
                    return True
                rt.diagnostic("weekly_count", "无法读取本周挑战次数")
                rt.log("无法读取本周挑战次数，本次停止，未继续消耗活跃能量。")
                return False
            finished, total = count
            rt.log(f"本周幻境已完成 {finished}/{total} 次。")
            if finished >= total:
                _record_weekly_state(rt, finished, total)
                rt.log("本周所选目标已无需挑战。", detail=True)
                return True
            game = Gameplay(rt)
            energy = game.read_energy()
            if energy is not None and energy < game.MIN_ENERGY["weekly"]:
                _record_weekly_state(rt, finished, total)
                return game.exhausted(True, energy)
            if not rt.ui.realm("weekly"):
                _record_weekly_state(rt, finished, total)
                return False
            results = []
            skipped_missing = 0
            completed_targets: list[str] = []
            finished_now = finished
            remaining_quota = max(0, total - finished)
            for index, (node, label) in enumerate(targets):
                if rt.stopped:
                    return False
                if remaining_quota <= 0:
                    rt.log("本周幻境次数已满，跳过剩余目标。")
                    break
                if energy is not None and energy < game.MIN_ENERGY["weekly"]:
                    results.append(game.exhausted(True, energy))
                    break
                rt.log(f"正在完成每周幻境：{label}。")
                ok = game.realm("weekly", maximize=False, entered=True, weekly_node=node, allow_exhausted=True)
                if not ok and (game.level_missing or game.weekly_quick_locked):
                    skipped_missing += 1
                    if game.weekly_quick_locked:
                        rt.log(f"{label}尚未首次通关，无法使用快速挑战，本次跳过。")
                    else:
                        rt.log(f"未找到{label}，可能尚未解锁，本次跳过。")
                    continue
                results.append(ok)
                if game.energy_exhausted:
                    break
                if ok:
                    target_id = node.rsplit("_", 1)[-1]
                    completed_targets.append(target_id)
                    if not game.weekly_quota_used:
                        remaining_quota = max(0, remaining_quota - 1)
                        finished_now = min(total, finished_now + 1)
                        if energy is not None:
                            energy = max(0, energy - game.MIN_ENERGY["weekly"])
                    continue
                if game.blessing_overflow:
                    rt.log(f"{label}因祝福闪光数量超过容量上限未能领取，已停止本周幻境。")
                else:
                    rt.log(f"{label}未确认完成。")
                # A failed confirmation/reward may leave a dialog open.
                # Do not search/click another level on that unknown page.
                break
            _record_weekly_state(rt, finished_now, total, completed_targets)
            overall_ok = (bool(results) and (all(results) or remaining_quota == 0)) or (
                not results and skipped_missing == len(targets))
            rt.log("每周幻境处理结束。" if overall_ok else "部分每周幻境未完成，请检查游戏状态。")
            if not overall_ok and combined and not rt.stopped:
                recovered = rt.main()
                if recovered and not context.override_pipeline({
                        argv.node_name: {"on_error": ["MaaNikki_RealmWeeklyIncomplete"]},
                        "MaaNikki_RealmReview": {"custom_action_param": {
                            "stage": "settle", "realm_success": False}}}):
                    rt.log("未能恢复后续幻境流程，本次停止。")
            return overall_ok

        returned = True
        try:
            ok = steps()
        except TaskStopped:
            raise
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, cv2.error) as error:
            if not rt.stopped:
                rt.diagnostic("weekly_challenge", str(error))
            rt.log(f"每周幻境停止：{error}")
            ok = False
        finally:
            if not rt.stopped and not recovered:
                returned = rt.ui.calendar() if combined else rt.main()
                if not returned:
                    rt.log("每周幻境未能返回后续步骤需要的页面，本项停止。")
        return bool(ok and returned)
