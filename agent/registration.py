from functools import wraps
from copy import copy
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event
import traceback
from performance import action_profile
from message_log import message_scope
from task_control import TaskStopped, task_session

from maa.agent.agent_server import AgentServer


_LOCAL_REGISTRATION = ContextVar("local_action_registration", default=False)
_ACTION_TYPES = {}
_RECOGNITION_TYPES = {}


@contextmanager
def local_registration(resource):
    """Reuse guarded handlers in the startup coordinator's local Maa session."""
    handlers = {}
    token = _LOCAL_REGISTRATION.set(True)
    try:
        yield handlers
        # Imports are cached across game restarts; bind fresh instances each time.
        for name, cls in _ACTION_TYPES.items():
            handler = cls()
            finished = Event()
            finished.set()
            run = handler.run

            def local_run(context, argv, run=run, finished=finished):
                finished.clear()
                try:
                    return run(context, argv)
                finally:
                    finished.set()

            # Native cancellation may finish a job before its Python callback.
            # The startup owner must retain the Tasker until both have ended.
            handler.run = local_run
            handler.callback_finished = finished
            if not resource.register_custom_action(name, handler):
                raise RuntimeError(f"无法注册启动动作：{name}")
            handlers[name] = handler
        for name, cls in _RECOGNITION_TYPES.items():
            if not resource.register_custom_recognition(name, cls()):
                raise RuntimeError(f"无法注册启动识别：{name}")
    finally:
        _LOCAL_REGISTRATION.reset(token)


def agent_action(name, *, stop_on_failure=False):
    def register(cls):
        run = cls.run

        @wraps(run)
        def guarded(self, context, argv):
            task_id = getattr(getattr(argv, "task_detail", None), "task_id", 0)
            succeeded = False
            with action_profile(argv.node_name) as profile, message_scope(argv.node_name):
                try:
                    with task_session(context, task_id) as session:
                        saved = session.evidence.saved if session else 0
                        try:
                            result = run(self, context, argv)
                        except TaskStopped:
                            raise
                        except Exception as error:
                            if session:
                                session.evidence.save("action_exception", str(error), details={
                                    "node": argv.node_name, "action": name,
                                    "traceback": traceback.format_exc()[-3000:]},
                                    generation=session.generation)
                            raise
                        if (session and not result and not session.stopped()
                                and session.evidence.saved == saved
                                and name not in {"nikki.daily_config", "nikki.feature_settings"}):
                            session.evidence.save("action_failed", "动作返回失败", details={
                                "node": argv.node_name, "action": name,
                                "phase": "after_action_return"}, generation=session.generation)
                    succeeded = bool(result)
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
                finally:
                    # Startup is a prerequisite for the entire queue. Ordinary
                    # actions retain their existing skip-on-failure behavior.
                    if stop_on_failure and not succeeded:
                        try:
                            context.tasker.post_stop()
                        except Exception:
                            traceback.print_exc()

        cls.run = guarded
        _ACTION_TYPES[name] = cls
        if _LOCAL_REGISTRATION.get():
            return cls
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
                        if session and argv.image is not None and argv.image.size:
                            session.evidence.remember_frame(argv.image, generation)
                            session.validate_frame(argv.image)
                        try:
                            result = analyze(self, context, argv)
                        except TaskStopped:
                            raise
                        except Exception as error:
                            if session:
                                session.evidence.save("recognition_exception", str(error), details={
                                    "node": argv.node_name, "recognition": name,
                                    "traceback": traceback.format_exc()[-3000:]},
                                    generation=session.generation)
                            raise
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
        _RECOGNITION_TYPES[name] = cls
        if _LOCAL_REGISTRATION.get():
            return cls
        return AgentServer.custom_recognition(name)(cls)
    return register
