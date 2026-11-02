"""Anonymous aggregate statistics: level-10 colours and measured bling yield.

Only catalogue names, slot, colour, counts, energy, bling gain and versions leave
this machine, plus a random reporter ID. As in Penguin Statistics, the server issues
that ID on the first report and it is unrelated to the UID or device. It lets analysis
count accounts and exclude abnormal reporters. A failed upload stays queued for the
next run and never affects the task.
"""
import json
import os
from pathlib import Path
import re
import threading
from urllib import error as url_error, request
from uuid import uuid4


TIMEOUT_SECONDS = 30  # Background only; cross-border links can be slow.
MAX_ROWS = 500


def endpoint(root):
    try:
        data = json.loads((Path(root) / "interface.json").read_text(encoding="utf-8-sig"))
        value = data.get("statistics", {}).get("endpoint", "")
    except (OSError, ValueError, AttributeError):
        return ""
    return value if isinstance(value, str) and value.startswith("https://") else ""


def queue_change(data, *, colour=None, bling=None):
    stats = data["observations"].setdefault("anonymous_statistics", {"batch": None, "rolls": {}, "bling": []})
    if colour is not None:
        key = "|".join(colour)
        if key in stats["rolls"] or len(stats["rolls"]) < MAX_ROWS:
            stats["rolls"][key] = stats["rolls"].get(key, 0) + 1
    if bling is not None and len(stats["bling"]) < MAX_ROWS:
        stats["bling"].append(list(bling))
    stats["batch"] = None  # New content needs a new idempotency key.


def upload(account, root, catalog_version):
    """Send the queue once in the background; the server deduplicates by batch."""
    url = endpoint(root)
    if not url:
        return None
    batch = {}

    def prepare(data):
        stats = data["observations"].get("anonymous_statistics")
        if not stats or not stats["rolls"] and not stats["bling"]:
            return
        stats["batch"] = stats["batch"] or uuid4().hex
        batch.update(json.loads(json.dumps(stats)))

    account.update(prepare)
    if not batch:
        return None

    def send():
        headers = {"Content-Type": "application/json"}
        if batch.get("reporter"):
            headers["Authorization"] = "MaaNikkiID " + batch["reporter"]
        body = json.dumps({
            "schema": 1, "batch": batch["batch"], "catalog": str(catalog_version),
            "app": os.environ.get("PI_VERSION", "unknown"),
            "rolls": [[*key.split("|"), count] for key, count in batch["rolls"].items()],
            "bling": batch["bling"]}, ensure_ascii=False).encode("utf-8")
        try:
            call = request.Request(url, body, headers, method="POST")
            with request.urlopen(call, timeout=TIMEOUT_SECONDS) as response:
                if response.status >= 300:
                    return
                issued = response.headers.get("X-MaaNikki-Set-ID", "")
        except (OSError, ValueError, url_error.URLError):
            return

        def clear(data):
            stats = data["observations"].get("anonymous_statistics")
            if not stats:
                return
            if re.fullmatch(r"[0-9a-f]{32}", issued):
                stats["reporter"] = issued
            if stats["batch"] != batch["batch"]:
                return  # New entries arrived: the merged queue resends under a new key.
            stats.update(batch=None, rolls={}, bling=[])

        try:
            account.update(clear)
        except (OSError, ValueError, KeyError, TypeError):
            pass

    thread = threading.Thread(target=send, name="eureka-statistics", daemon=True)
    thread.start()
    return thread
