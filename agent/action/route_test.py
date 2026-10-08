"""Developer movement tests using the connected client's controller and bindings."""
from maa.custom_action import CustomAction
from registration import agent_action
from daily.route_test import RouteTest
from daily.runtime import parameters


@agent_action("nikki.route_test")
class RouteTestAction(CustomAction):
    def run(self, context, argv):
        return RouteTest(context).run_test(parameters(context, argv.node_name))
