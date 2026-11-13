"""Maa action for the realm challenge priority queue (每周幻境 / 焕新材料 / 制衣材料 / 五星闪光 → 兜底)."""
from __future__ import annotations

import traceback
import cv2
from maa.custom_action import CustomAction

from registration import agent_action
from task_control import TaskStopped
from daily import run_state
from daily.runtime import Executors, parameters
from collection.material_pursuit import run_material_goals
from collection.realm_goals import GOAL_LABELS, parse_realm_queue
from .blessing_pursuit import glow_shortfall

FALLBACK_KINDS = {"MaaNikki_Jihua": "jihua", "MaaNikki_Bless": "bless", "MaaNikki_Monster": "monster"}
HANDOFF_NODES = {"glow": "MaaNikki_BlessingPursuit", "fallback": "MaaNikki_RealmEnergy",
                 "done": "MaaNikki_RealmReview", "weekly": "MaaNikki_RealmWeekly"}


def fallback_kind(context) -> str | None:
    energy = context.get_node_data("MaaNikki_RealmEnergy") or {}
    for item in energy.get("next", []):
        name = item if isinstance(item, str) else item.get("name") if isinstance(item, dict) else None
        if name in FALLBACK_KINDS:
            return FALLBACK_KINDS[name]
    return None


@agent_action("nikki.realm_queue")
class RealmQueueAction(CustomAction):
    """Before every batch, farm the first enabled goal that is still short; then fall through."""

    def run(self, context, argv):
        rt = Executors(context)
        node = argv.node_name
        failed = False
        last_kind = None
        param = parameters(context, node)
        try:
            order = param.get("order")
            goals = parse_realm_queue(order)
            organize = parameters(context, "MaaNikki_Pursuit_OrganizeOption").get("mode", "auto")
            resumed = param.get("after_goal")
            if resumed in goals:
                goals = goals[goals.index(resumed) + 1:]
            weekly_next = False
            if "weekly" in goals:
                index = goals.index("weekly")
                # Goals ranked above the weekly realm get the energy first.
                goals, weekly_next = goals[:index], True
            if not resumed:
                labels = [GOAL_LABELS[goal] for goal in parse_realm_queue(order)]
                rt.log("幻境挑战顺序：" + " → ".join(labels + ["兜底副本"]) + "。")
            if goals:
                handoff, last_kind = run_material_goals(rt, goals, lambda role: glow_shortfall(rt, role, organize))
            else:
                handoff = "fallback"
            if weekly_next:
                if handoff == "fallback":
                    handoff = "weekly"
                elif handoff == "glow":
                    rt.log("幻境挑战顺序：五星闪光排在每周幻境前，收齐所选范围后才会继续每周幻境。")
        except TaskStopped:
            raise
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, cv2.error) as error:
            if not rt.stopped:
                rt.diagnostic("realm_queue", str(error), details={"traceback": traceback.format_exc()[-3000:]})
            rt.log(f"幻境挑战顺序中断：{error}。剩余活跃能量改用兜底副本。")
            handoff, failed = "fallback", True
        if rt.stopped:
            return False
        if handoff == "weekly":
            # The weekly node returns here; the resumed queue continues after it.
            return context.override_pipeline({node: {
                "next": [HANDOFF_NODES["weekly"]],
                "custom_action_param": {**param, "after_goal": "weekly"}}})
        kind = "bless" if handoff == "glow" else fallback_kind(context) if handoff == "fallback" else last_kind
        run_state.update_realm(rt, kind=kind, collection=handoff == "glow")
        overrides = {node: {"next": [HANDOFF_NODES[handoff]]}}
        if failed:
            # The fallback still spends the energy, but settlement reports the failure.
            for name, fallback in FALLBACK_KINDS.items():
                overrides[name] = {"next": ["MaaNikki_RealmReviewFailed"], "on_error": ["MaaNikki_RealmReviewFailed"],
                                   "custom_action_param": {"kind": fallback, "return_page": "calendar"}}
        return context.override_pipeline(overrides)
