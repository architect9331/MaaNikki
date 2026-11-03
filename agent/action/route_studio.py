"""Route editor actions: recording a run and executing a user route."""
from maa.custom_action import CustomAction
from registration import agent_action
from daily.route_studio import RouteRecorder, UserRouteRun
from daily.runtime import parameters


@agent_action("nikki.route_record")
class RouteRecordAction(CustomAction):
    def run(self, context, argv):
        return RouteRecorder(context).record(parameters(context, argv.node_name))


@agent_action("nikki.route_run")
class RouteRunAction(CustomAction):
    def run(self, context, argv):
        return UserRouteRun(context).run_test(parameters(context, argv.node_name))
