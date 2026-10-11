"""Native Agent pause regressions: python/python.exe -B scripts/test_agent_pause.py.

Uses two processes and a synthetic controller/window; never touches the game.
The worker is bounded by a subprocess timeout to detect IPC deadlocks safely.
"""
from pathlib import Path
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
os.environ.setdefault("MAAFW_BINARY_PATH", str(ROOT / "maafw"))


def server(identifier, directory):
    from maa.library import Library
    Library.open(ROOT / "maafw", agent_server=True)
    from maa.agent.agent_server import AgentServer
    from maa.custom_action import CustomAction
    from maa.custom_recognition import CustomRecognition
    from maa.controller import Controller
    from daily.runtime import Runtime
    from registration import agent_action, agent_recognition
    from task_control import active_session, TaskStopped

    focused = directory / "focused"
    marker = directory / "stage"
    user32 = Mock()
    user32.IsWindow.return_value = True
    user32.GetWindowTextLengthW.return_value = 4
    def title(hwnd, buffer, length):
        buffer.value = "Infinity Nikki"
        return len(buffer.value)
    # Buffer must fit the actual test title.
    user32.GetWindowTextLengthW.return_value = len("Infinity Nikki")
    user32.GetWindowTextW.side_effect = title
    user32.GetForegroundWindow.side_effect = lambda: 1 if focused.exists() else 0

    @agent_recognition("pause_probe_recognition")
    class ProbeRecognition(CustomRecognition):
        def analyze(self, context, argv):
            rt = Runtime(context)
            assert rt.controller.info["hwnd"] == 1
            return CustomRecognition.AnalyzeResult(box=[1, 1, 2, 2], detail="ok")

    @agent_action("pause_probe_child")
    class ProbeChild(CustomAction):
        def run(self, context, argv):
            rt = Runtime(context)
            assert rt.controller.info["hwnd"] == 1
            rt.check_focus()
            return True

    @agent_action("pause_probe")
    class Probe(CustomAction):
        def run(self, context, argv):
            rt = Runtime(context)
            rt.check_focus()
            # The timer's lazy adapter, nested actions and recognition must
            # all retain the same native proxy as this original Runtime.
            assert rt.pause(.02)
            assert rt.controller.info["hwnd"] == 1
            assert Runtime(context).controller is rt.controller
            assert rt.run("Child")
            assert rt.recognize("Recognition").hit
            assert rt.controller.info["hwnd"] == 1
            mode = json.loads(argv.custom_action_param)["mode"]
            if mode != "normal":
                focused.unlink()
                marker.write_text("waiting", encoding="utf-8")
                try:
                    rt.check_focus()
                except TaskStopped:
                    marker.write_text("stopped", encoding="utf-8")
                    raise
                marker.write_text("resumed", encoding="utf-8")
                assert active_session().generation == 1
                assert rt.capture() is not None
            return True

    native_info = Controller.info
    # Only adapt synthetic window metadata; every info access still makes
    # the real reverse IPC call, including its native controller identifier.
    def window_info(controller):
        return {**native_info.fget(controller), "type": "win32", "hwnd": 1}
    with patch("navigation.controller.ctypes.WinDLL", return_value=user32), \
            patch.object(Controller, "info", property(window_info)):
        assert AgentServer.start_up(identifier)
        AgentServer.join()
        AgentServer.shut_down()


def worker(directory):
    from maa.library import Library
    Library.open(ROOT / "maafw")
    from maa.controller import CustomController
    from maa.resource import Resource
    from maa.tasker import Tasker
    from maa.buffer import StringBuffer
    import numpy as np

    focused = directory / "focused"
    marker = directory / "stage"
    class FakeController(CustomController):
        connect = lambda self: True
        request_uuid = lambda self: "agent-pause-test"
        start_app = stop_app = click = swipe = touch_down = touch_move = touch_up = \
            click_key = input_text = key_down = lambda *args: True
        def key_up(self, key):
            focused.touch()
            return True
        def screencap(self):
            return np.zeros((720, 1280, 3), np.uint8)
        def get_custom_info(self):
            return {"type": "win32", "hwnd": 1}

    Tasker.set_log_dir(str(directory / "logs"))
    controller, resource, tasker = FakeController(), Resource(), Tasker()
    assert controller.post_connection().wait().succeeded
    assert controller.info["type"] == "custom"
    assert tasker.bind(resource, controller)
    pipeline = {
        "Probe": {"recognition": "DirectHit", "action": "Custom", "custom_action": "pause_probe"},
        "Child": {"recognition": "DirectHit", "action": "Custom", "custom_action": "pause_probe_child"},
        "Recognition": {"recognition": "Custom", "custom_recognition": "pause_probe_recognition"},
        "MaaNikki_Daily_Message": {"recognition": "DirectHit", "action": "DoNothing"},
    }
    assert resource.override_pipeline(pipeline)
    lib = Library.agent_client()
    signatures = {
        "MaaAgentClientCreateV2": ([ctypes.c_void_p], ctypes.c_void_p),
        "MaaAgentClientBindResource": ([ctypes.c_void_p, ctypes.c_void_p], ctypes.c_uint8),
        "MaaAgentClientConnect": ([ctypes.c_void_p], ctypes.c_uint8),
        "MaaAgentClientSetTimeout": ([ctypes.c_void_p, ctypes.c_int64], ctypes.c_uint8),
        "MaaAgentClientDisconnect": ([ctypes.c_void_p], ctypes.c_uint8),
        "MaaAgentClientDestroy": ([ctypes.c_void_p], None),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(lib, name)
        function.argtypes, function.restype = arguments, result
    identifier = str(uuid4())
    buffer = StringBuffer()
    buffer.set(identifier)
    client = lib.MaaAgentClientCreateV2(buffer._handle)
    assert client
    assert lib.MaaAgentClientBindResource(client, resource._handle)
    assert lib.MaaAgentClientSetTimeout(client, 3000)
    with (directory / "server.log").open("w", encoding="utf-8") as output:
        process = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()),
            "--server", identifier, str(directory)], stdout=output, stderr=output,
            creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            assert lib.MaaAgentClientConnect(client)
            def wait_until(condition):
                deadline = time.monotonic()+5
                while not condition():
                    if time.monotonic() >= deadline:
                        raise AssertionError("Agent did not return within 5 seconds")
                    time.sleep(.02)
            for mode in ("normal", "normal", "resume", "stop", "normal"):
                focused.touch()
                marker.unlink(missing_ok=True)
                job = tasker.post_task("Probe", {"Probe": {"custom_action_param": {"mode": mode}}})
                if mode != "normal":
                    wait_until(lambda: marker.exists())
                    assert marker.read_text(encoding="utf-8") == "waiting"
                    time.sleep(.2)
                    assert not job.status.done
                    if mode == "resume":
                        focused.touch()
                    else:
                        tasker.post_stop()
                wait_until(lambda: job.status.done)
                if mode == "stop":
                    assert marker.read_text(encoding="utf-8") == "stopped"
                else:
                    assert job.status.succeeded, f"{mode} failed: {job.status}"
                if mode == "resume":
                    assert marker.read_text(encoding="utf-8") == "resumed"
                wait_until(lambda: not tasker.running)
            print("Native Agent: repeated startup, nested actions/recognition, resume and paused Stop passed.", flush=True)
        finally:
            lib.MaaAgentClientDisconnect(client)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            lib.MaaAgentClientDestroy(client)


class NativePauseTests(unittest.TestCase):
    def test_native_agent_lifecycle_and_pause(self):
        with tempfile.TemporaryDirectory(prefix="agent-pause-", dir=ROOT / ".build") as folder:
            result = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()),
                "--worker", folder], capture_output=True, text=True, timeout=35)
            server_log = Path(folder) / "server.log"
            server_output = server_log.read_text(encoding="utf-8") if server_log.exists() else ""
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr+server_output)
            self.assertNotIn("controller not found", result.stdout+server_output)


if __name__ == "__main__":
    if "--server" in sys.argv:
        server(sys.argv[-2], Path(sys.argv[-1]))
    elif "--worker" in sys.argv:
        worker(Path(sys.argv[-1]))
    else:
        unittest.main()
