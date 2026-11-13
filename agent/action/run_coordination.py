"""Batch boundaries, daily settlement and an explicitly deferred single upgrade."""
from __future__ import annotations

import cv2
from client_config import parse_custom_param
from registration import agent_action
from task_control import TaskStopped
from maa.custom_action import CustomAction

from daily import run_state
from daily.runtime import Executors, parameters
from .daily_tasks import DailyRun
from daily.settings import ROOT
from collection.realm_goals import parse_realm_queue


@agent_action("nikki.run_coordination")
class RunCoordinationAction(CustomAction):
    def run(self, context, argv):
        rt = Executors(context)
        try:
            options = parse_custom_param(argv.custom_action_param)
            task_id = argv.task_detail.task_id
            if options.get("stage") == "begin_realm":
                uid = rt.account_uid()
                try:
                    from account_state import AccountState
                    from collection import statistics
                    statistics.upload(AccountState(ROOT, uid), ROOT)
                except Exception:
                    pass
                energy = context.get_node_data("MaaNikki_RealmEnergy") or {}
                next_names = {item if isinstance(item, str) else item.get("name")
                              for item in energy.get("next", []) if isinstance(item, (str, dict))}
                # The queue picks the actual realm later and updates this record;
                # until then a queued blessing goal counts as collection mode.
                queue = parse_realm_queue(parameters(context, "MaaNikki_RealmQueue").get("order"))
                collection = "glow" in queue
                kind = "bless" if collection or "MaaNikki_Bless" in next_names else (
                    "jihua" if "MaaNikki_Jihua" in next_names else
                    "monster" if "MaaNikki_Monster" in next_names else None)
                run_state.begin_realm(rt, task_id, kind=kind, collection=collection)
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
                overrides["MaaNikki_BlessingPursuit"] = {
                    "next": ["MaaNikki_RealmReview"],
                    "on_error": ["MaaNikki_RealmReviewFailed"],
                    "custom_action_param": {
                        **parameters(context, "MaaNikki_BlessingPursuit"),
                        "resume_queue": True,
                        "return_page": "calendar"}}
                overrides["MaaNikki_RealmQueue"] = {
                    "next": ["MaaNikki_RealmEnergy"],
                    "custom_action_param": {
                        **parameters(context, "MaaNikki_RealmQueue"), "after_goal": ""},
                    "on_error": ["MaaNikki_RealmReviewFailed"]}
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
            # Only a specifically deferred upgrade can run here. All other
            # daily actions and realm challenges remain in their own stages.
            reviewed = DailyRun(clone, "zhaoxi").review_after_energy(pending, realm_ok=realm_ok)
            if not reviewed and not rt.stopped:
                # The original daily stage can return successfully to defer its
                # score check. Its failed entrances predate this task's Sentry
                # attachment window, so retain them with their original times.
                rt.evidence.republish(pending.failure_evidence)
            return realm_ok and reviewed
        except TaskStopped:
            raise
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, cv2.error) as error:
            rt.log(f"本轮任务衔接未完成：{error}。")
            return False
