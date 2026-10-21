"""Shared atomic JSON snapshots; task owners decide how write failures surface."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile


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
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
