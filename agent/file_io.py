"""Shared atomic JSON snapshots; task owners decide how write failures surface."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time


def write_json(path, data):
    """Publish a complete UTF-8 snapshot, leaving the old file intact on failure."""
    path = Path(path)
    content = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)+"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(prefix=path.name+".", suffix=".tmp", dir=path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        # Windows readers/scanners may briefly deny deletion of either file.
        # Retry only the atomic publish, keeping the fsynced snapshot and old
        # destination intact. Persistent access failures still reach callers
        # after at most 0.75 seconds of backoff; successful writes never wait.
        for attempt in range(5):
            try:
                temporary.replace(path)
                break
            except PermissionError as error:
                if (os.name != "nt" or getattr(error, "winerror", None) not in {5, 32, 33}
                        or attempt == 4):
                    raise
                time.sleep(.05 * (2 ** attempt))
    finally:
        temporary.unlink(missing_ok=True)
