from functools import wraps
from copy import copy
import traceback
from performance import action_profile
from task_control import TaskStopped, task_session

from maa.agent.agent_server import AgentServer


def agent_action(name):
    def register(cls):
        run = cls.run

        @wraps(run)
        def guarded(self, context, argv):
            task_id = getattr(getattr(argv, "task_detail", None), "task_id", 0)
            with action_profile(argv.node_name) as profile:
                try:
                    with task_session(context, task_id):
                        result = run(self, context, argv)
                    if profile is not None:
                        profile.succeeded = bool(result)
                    return result
                except TaskStopped:
                    return False
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
    def register(cls):
        analyze = cls.analyze

        @wraps(analyze)
        def guarded(self, context, argv):
            task_id = getattr(getattr(argv, "task_detail", None), "task_id", 0)
            try:
                with task_session(context, task_id) as session:
                    while True:
                        generation = session.generation if session else 0
                        if session:
                            session.wait()
                        if session and generation != session.generation:
                            captured = session.controller.post_screencap().wait()
                            if not captured.succeeded:
                                return None
                            argv = copy(argv)
                            argv.image = captured.get()
                        generation = session.generation if session else 0
                        result = analyze(self, context, argv)
                        if session:
                            session.wait()
                        if not session or generation == session.generation:
                            return result
                        captured = session.controller.post_screencap().wait()
                        if not captured.succeeded:
                            return None
                        argv = copy(argv)
                        argv.image = captured.get()
            except TaskStopped:
                return None
            except Exception:
                # Recognition callbacks must also return a defined result if
                # the window/Agent channel closes during a foreground wait.
                traceback.print_exc()
                return None

        cls.analyze = guarded
        return AgentServer.custom_recognition(name)(cls)
    return register
