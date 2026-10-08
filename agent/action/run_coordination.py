"""Batch boundaries and read-only daily settlement; no cross-task execution."""
from __future__ import annotations

import json
import cv2
from registration import agent_action
from maa.custom_action import CustomAction

from daily import run_state
from daily.runtime import Executors
from .daily_tasks import DailyRun


@agent_action("nikki.run_coordination")
class RunCoordinationAction(CustomAction):
    def run(self, context, argv):
        rt = Executors(context)
        try:
            options = argv.custom_action_param
            options = json.loads(options or "{}") if isinstance(options, str) else options
            task_id = argv.task_detail.task_id
            if options.get("stage") == "begin_realm":
                run_state.begin_realm(rt, task_id)
                # Overrides belong only to this RealmChallenge context. Daily
                # minimum challenges and independent realm entries are unchanged.
                overrides = {node: {
                    "next": ["MaaNikki_RealmReview"],
                    "on_error": ["MaaNikki_RealmReviewFailed"],
                    "custom_action_param": {"kind": kind, "return_page": "calendar"},
                } for node, kind in (("MaaNikki_Jihua", "jihua"), ("MaaNikki_Bless", "bless"),
                                    ("MaaNikki_Monster", "monster"))}
                overrides["MaaNikki_RealmReview"] = {
                    "custom_action_param": {"stage": "settle", "realm_success": True}}
                return context.override_pipeline(overrides)
            if options.get("stage") != "settle":
                return False
            realm_ok = options.get("realm_success", True) is True
            if not realm_ok:
                rt.log("本轮幻境仍有未完成步骤，不会因其他挑战成功而覆盖前面的异常。")
            pending = run_state.take(rt, task_id)
            if pending is None:
                _, handoff_ok = rt.finish("MaaNikki_RealmChallenge")
                return realm_ok and handoff_ok
            if rt.stopped:
                return False
            clone = context.clone()
            if not clone.override_pipeline(pending.claim_overrides):
                rt.log("朝夕领奖设置未能恢复，本次未复核。")
                return False
            # This service only reads the score and claims rewards. It never
            # rescans/reexecutes free tasks or launches another realm challenge.
            reviewed = DailyRun(clone, "zhaoxi").review_after_energy(pending)
            return realm_ok and reviewed
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, cv2.error) as error:
            rt.log(f"本轮任务衔接未完成：{error}。")
            return False
