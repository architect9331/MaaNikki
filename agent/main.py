import os
import sys
from pathlib import Path

# Embedded Python uses an isolated path list instead of the script directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["MAAFW_BINARY_PATH"] = str(Path(__file__).resolve().parents[1] / "maafw")

from runtime_threads import (
    cap_loaded_maafw_opencv,
    configure_worker_threads,
    start_parent_exit_watchdog,
)

configure_worker_threads(2)

from maa.agent.agent_server import AgentServer
from maa.tasker import Tasker

from action import claim_daily_rewards as _claim_daily_rewards
from action import weekly_challenge as _weekly_challenge
from action import daily_tasks as _daily_tasks
from action import game_key as _game_key
from action import gameplay as _gameplay
from action import wardrobe as _wardrobe
from action import blessing_pursuit as _blessing_pursuit
from action import realm_queue as _realm_queue
from action import run_coordination as _run_coordination
from action import exploration_crown as _exploration_crown
from action import route_test as _route_test
from action import route_studio as _route_studio
from action import subtask_test as _subtask_test
from action import start_game as _start_game
from action import close_game as _close_game
from recognition import ui_template as _ui_template
from recognition import crown as _crown
from tall_window import adopt_session_16_9_handoff, restore_session_16_9_windows
from game_hud import close_all_huds


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    Tasker.set_log_dir(str(project_root / "debug"))

    if len(sys.argv) < 2:
        print("Usage: python main.py <socket_id>")
        print("socket_id is provided by the MaaFramework client.")
        raise SystemExit(1)

    socket_id = sys.argv[-1]

    adopt_session_16_9_handoff()

    def _cleanup_on_parent_exit() -> None:
        try:
            close_all_huds()
        except Exception:
            pass
        try:
            restore_session_16_9_windows()
        except Exception:
            pass
        try:
            AgentServer.shut_down()
        except Exception:
            pass

    start_parent_exit_watchdog(_cleanup_on_parent_exit)
    try:
        AgentServer.start_up(socket_id)
        cap_loaded_maafw_opencv(2)
        try:
            AgentServer.join()
        finally:
            AgentServer.shut_down()
    finally:
        close_all_huds()
        restore_session_16_9_windows()


if __name__ == "__main__":
    main()
