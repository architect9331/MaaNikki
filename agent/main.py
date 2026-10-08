import os
import sys
from pathlib import Path

# Embedded Python uses an isolated path list instead of the script directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["MAAFW_BINARY_PATH"] = str(Path(__file__).resolve().parents[1] / "maafw")

from maa.agent.agent_server import AgentServer
from maa.tasker import Tasker

from action import claim_daily_rewards as _claim_daily_rewards
from action import weekly_challenge as _weekly_challenge
from action import daily_tasks as _daily_tasks
from action import game_key as _game_key
from action import gameplay as _gameplay
from action import run_coordination as _run_coordination
from action import exploration_crown as _exploration_crown
from action import route_test as _route_test
from action import subtask_test as _subtask_test
from recognition import ui_template as _ui_template


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    Tasker.set_log_dir(str(project_root / "debug"))

    if len(sys.argv) < 2:
        print("Usage: python main.py <socket_id>")
        print("socket_id is provided by the MaaFramework client.")
        raise SystemExit(1)

    socket_id = sys.argv[-1]

    AgentServer.start_up(socket_id)
    AgentServer.join()
    AgentServer.shut_down()


if __name__ == "__main__":
    main()
