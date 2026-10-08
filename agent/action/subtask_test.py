"""Developer-only daily subtask entry points."""
from maa.custom_action import CustomAction
from registration import agent_action
from daily.runtime import parameters
from daily.subtask_test import SubtaskTest


@agent_action("nikki.subtask_test")
class SubtaskTestAction(CustomAction):
    def run(self, context, argv):
        settings = parameters(context, argv.node_name)
        return SubtaskTest(context, settings.get("mode", "")).run_test(settings.get("task", ""))
