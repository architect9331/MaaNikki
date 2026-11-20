"""Standalone task action to configure and activate session-persistent abilities."""
from __future__ import annotations

from maa.custom_action import CustomAction

from daily.runtime import Executors, parameters
from navigation.models import NavigationError
from navigation.startup_abilities import ensure_persistent_abilities
from registration import agent_action
from task_control import TaskStopped


@agent_action("nikki.persistent_abilities")
class PersistentAbilitiesAction(CustomAction):
    def run(self, context, argv):
        runtime = Executors(context)
        options = parameters(context, argv.node_name)
        events: list[dict] = []
        try:
            if not ensure_persistent_abilities(runtime, options, events=events):
                return False
            _, finished = runtime.finish(argv.node_name)
            return finished
        except TaskStopped:
            raise
        except NavigationError as error:
            if not runtime.stopped:
                image = runtime.capture()
                runtime.diagnostic(
                    "persistent_abilities",
                    str(error),
                    image=image,
                    details={"options": options, "events": events[-24:]},
                )
                runtime.log(str(error))
            return False
