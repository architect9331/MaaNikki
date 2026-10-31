"""Batch boundaries, daily settlement and an explicitly deferred single upgrade."""
from __future__ import annotations

import json
import cv2
from registration import agent_action
from maa.custom_action import CustomAction

from daily import run_state
from daily.runtime import Executors, parameters
from .daily_tasks import DailyRun
from daily.settings import ROOT
from collection.storage import InventoryStore


@agent_action("nikki.run_coordination")
class RunCoordinationAction(CustomAction):
    def run(self, context, argv):
        rt = Executors(context)
        try:
            options = argv.custom_action_param
            options = json.loads(options or "{}") if isinstance(options, str) else options
            task_id = argv.task_detail.task_id
            if options.get("stage") == "begin_realm":
                rt.account_uid()
                energy = context.get_node_data("MaaNikki_RealmEnergy") or {}
                next_names = {item if isinstance(item, str) else item.get("name")
                              for item in energy.get("next", []) if isinstance(item, (str, dict))}
                collection = options.get("five_star_collection") is True or "MaaNikki_BlessingPursuit" in next_names
                kind = "bless" if collection or "MaaNikki_Bless" in next_names else (
                    "jihua" if "MaaNikki_Jihua" in next_names else
                    "monster" if "MaaNikki_Monster" in next_names else None)
                if collection:
                    catalog = json.loads((ROOT / "resource/data/blessing_catalog.json").read_text(encoding="utf-8"))
                    _, protected, _ = InventoryStore(ROOT, catalog, rt).load_organization()
                    total = sum(len(item["colors"]) * 3 for item in catalog["sets"]
                                if item["rarity"] == 5 and item.get("collection_target", True))
                    if len(protected) == total:
                        rt.log("五星祝福闪光颜色已收齐；本次不消耗活跃能量，请选择其他幻境。")
                        run_state.begin_realm(rt, task_id, kind=kind, collection=collection)
                        return context.override_pipeline({"MaaNikki_RealmChallenge": {"next": ["MaaNikki_RealmReview"]}})
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
                        "mode": "full_collection", "return_page": "calendar"}}
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
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, cv2.error) as error:
            rt.log(f"本轮任务衔接未完成：{error}。")
            return False
