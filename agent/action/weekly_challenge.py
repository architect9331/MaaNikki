from __future__ import annotations

import re
import cv2
from registration import agent_action
from maa.custom_action import CustomAction
from daily.runtime import Executors
from daily.gameplay import Gameplay

TARGETS = (("MaaNikki_Weekly_Level_Qiggeda", "奇格格达"), ("MaaNikki_Weekly_Level_Juan", "卷卷"))


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
        try:
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
                rt.log("无法读取本周挑战次数，本次停止，未继续消耗活跃能量。")
                return False
            finished, total = count
            rt.log(f"本周幻境已完成 {finished}/{total} 次。")
            if finished >= total or finished >= len(targets):
                rt.log("本周所选目标已无需挑战。")
                return True
            game = Gameplay(rt)
            energy = game.read_energy()
            if energy is not None and energy < game.MIN_ENERGY["weekly"]:
                return game.exhausted(True, energy)
            if not rt.ui.realm("weekly"):
                return False
            results = []
            for node, label in targets:
                if rt.stopped:
                    return False
                rt.log(f"正在完成每周幻境：{label}。")
                ok = game.realm("weekly", maximize=False, entered=True, weekly_node=node, allow_exhausted=True)
                results.append(ok)
                if game.energy_exhausted:
                    break
                if not ok:
                    rt.log(f"{label}未确认完成。")
                    # A failed confirmation/reward may leave a dialog open.
                    # Do not search/click another level on that unknown page.
                    break
            rt.log("每周幻境处理结束。" if all(results) else "部分每周幻境未完成，请检查游戏状态。")
            if not all(results) and combined and not rt.stopped:
                recovered = rt.main()
                if recovered and not context.override_pipeline({
                        argv.node_name: {"on_error": ["MaaNikki_RealmWeeklyIncomplete"]},
                        "MaaNikki_RealmReview": {"custom_action_param": {
                            "stage": "settle", "realm_success": False}}}):
                    rt.log("未能恢复后续幻境流程，本次停止。")
            return all(results)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, cv2.error) as error:
            rt.log(f"每周幻境停止：{error}")
            return False
        finally:
            if not rt.stopped and not recovered:
                returned = rt.ui.calendar() if combined else rt.main()
                if not returned:
                    rt.log("每周幻境未能返回后续步骤需要的页面，本项停止。")
                    return False
