"""Detailed task messages persisted independently of the UI focus channel."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
import json
import os
from pathlib import Path
from threading import Lock


_ACTION = ContextVar("maanikki_message_action", default=None)
_LOCK = Lock()
# The client exports and clears .log files recursively under debug/.
_DIRECTORY = Path(__file__).resolve().parents[1] / "debug/messages"


@contextmanager
def message_scope(action):
    token = _ACTION.set(action)
    try:
        yield
    finally:
        _ACTION.reset(token)


def write_message(text, display):
    """Append before UI delivery, including file-only and stopped-task messages.

    Return False on an IO failure so the caller can keep detail visible rather
    than silently discarding the only human-readable copy.
    """
    now = datetime.now().astimezone()
    record = {"timestamp": now.isoformat(timespec="milliseconds"), "pid": os.getpid(),
              "action": _ACTION.get(), "message": text, "display": display}
    try:
        with _LOCK:
            _DIRECTORY.mkdir(parents=True, exist_ok=True)
            path = _DIRECTORY / f"{now:%Y%m%d}-{os.getpid()}.log"
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False
