from functools import wraps
import traceback
from performance import action_profile

from maa.agent.agent_server import AgentServer


def agent_action(name):
    def register(cls):
        run = cls.run

        @wraps(run)
        def guarded(self, context, argv):
            with action_profile(argv.node_name) as profile:
                try:
                    result = run(self, context, argv)
                    if profile is not None:
                        profile.succeeded = bool(result)
                    return result
                except Exception as error:
                    # Never let an exception escape a ctypes boolean callback:
                    # its undefined return value can otherwise look successful.
                    if profile is not None:
                        profile.succeeded = False
                    traceback.print_exc()
                    try:
                        context.run_task("MaaNikki_Daily_Message", {
                            "MaaNikki_Daily_Message": {"focus": {
                                "Node.Action.Starting": f"{argv.node_name} 未完成：{error}。"}}})
                    except Exception:
                        pass
                    return False

        cls.run = guarded
        return AgentServer.custom_action(name)(cls)
    return register


def agent_recognition(name):
    return AgentServer.custom_recognition(name)
