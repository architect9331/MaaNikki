"""Anonymous aggregate statistics: level-10 colours, bling yield, digging targets and task preferences.

Only catalogue names, slot, colour, counts, energy, bling gain, Pear-Pal digging target
names, non-personal task/option preference tags and versions leave this machine, plus a
random reporter ID. As in Penguin Statistics, the server issues that ID on the first
report and it is unrelated to the UID or device. A failed upload stays queued for the
next run and never affects the task.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import threading
from urllib import error as url_error, request
from uuid import uuid4

from client_config import active_instance, mxu_config


TIMEOUT_SECONDS = 30  # Background only; cross-border links can be slow.
MAX_ROWS = 500


def is_enabled(root, pipeline_enabled=True):
    if not pipeline_enabled:
        return False
    env = os.environ.get("PI_MAANIKKI_BLESSING_STATS")
    if env is not None:
        return env == "1"
    try:
        config = mxu_config(Path(root)) or {}
        value = config.get("settings", {}).get("helpImproveBlessingStats")
        return True if value is None else bool(value)
    except (OSError, ValueError, TypeError, AttributeError):
        return True


def is_task_stats_enabled(root):
    env = os.environ.get("PI_MAANIKKI_TASK_STATS")
    if env is not None:
        return env == "1"
    try:
        config = mxu_config(Path(root)) or {}
        value = config.get("settings", {}).get("helpImproveTaskStats")
        return True if value is None else bool(value)
    except (OSError, ValueError, TypeError, AttributeError):
        return True


def endpoint(root):
    try:
        data = json.loads((Path(root) / "interface.json").read_text(encoding="utf-8-sig"))
        value = data.get("statistics", {}).get("endpoint", "")
    except (OSError, ValueError, AttributeError):
        return ""
    return value if isinstance(value, str) and value.startswith("https://") else ""


def _reserve_bling_bucket(raw_value) -> str:
    try:
        amount = int(str(raw_value or "0").strip())
    except ValueError:
        return "0"
    if amount <= 0:
        return "0"
    if amount <= 100_000:
        return "1-10w"
    if amount <= 500_000:
        return "10w-50w"
    if amount <= 1_000_000:
        return "50w-100w"
    return "100w+"
def collect_task_preferences(root) -> list[list[str]]:
    """Extract anonymous task & option preference pairs from the active instance."""
    root_path = Path(root)
    saved = active_instance(root_path)
    config = mxu_config(root_path) or {}
    if not saved:
        return []
    instance = saved[1] or {}
    pairs: list[list[str]] = []
    seen: set[tuple[str, str]] = set()

    def add(category: str, value: str):
        cat = str(category).strip()[:32]
        val = str(value).strip()[:64]
        if cat and val and (cat, val) not in seen and len(pairs) < MAX_ROWS:
            seen.add((cat, val))
            pairs.append([cat, val])

    for task in instance.get("tasks", []):
        if not isinstance(task, dict):
            continue
        name = str(task.get("taskName") or "").strip()
        if not name:
            continue
        enabled = bool(task.get("enabled"))
        add("task_enabled" if enabled else "task_disabled", name)
        opts = task.get("optionValues") or {}
        if not isinstance(opts, dict):
            continue
        if name == "realm_challenge":
            from .realm_goals import parse_realm_queue
            goals = parse_realm_queue(((opts.get("RealmQueue") or {}).get("values") or {}).get("order", ""))
            coll = "glow" in goals
            wf = opts.get("CollectionWorkflow", {}).get("caseName") or "Managed"
            rtype = opts.get("RealmTypes", {}).get("caseName") or "Bless"
            first = next((goal for goal in goals if goal != "weekly"), None)
            mode = (
                ("collection:farm_only" if wf == "FarmOnly" else "collection:managed") if first == "glow"
                else first if first
                else f"daily:{rtype}"
            )
            add("realm_mode", mode)
            add("realm_queue", ",".join(goals) or "none")
            add("realm_fallback", rtype)
            if coll:
                reserve_raw = (opts.get("CollectionReserveBling", {}).get("values") or {}).get("amount", "0")
                add("collection_reserve_bling", _reserve_bling_bucket(reserve_raw))
        for opt_key, opt_val in opts.items():
            if not isinstance(opt_val, dict):
                continue
            kind = opt_val.get("type")
            if kind == "switch":
                add(f"opt:{name}:{opt_key}", "Yes" if opt_val.get("value") else "No")
            elif kind == "select":
                case_name = opt_val.get("caseName")
                if isinstance(case_name, str) and case_name:
                    add(f"opt:{name}:{opt_key}", case_name)
            elif kind == "checkbox":
                for case_name in opt_val.get("caseNames") or []:
                    if isinstance(case_name, str) and case_name:
                        add(f"opt:{name}:{opt_key}", case_name)

    settings = config.get("settings") or {}
    if isinstance(settings, dict):
        add("setting:exploration_helper", "Yes" if settings.get("explorationEnabled") else "No")
        add("setting:auto_start", "Yes" if settings.get("autoStartInstanceId") else "No")
    pairs.sort()
    return pairs


def _ensure_stats(data: dict) -> dict:
    stats = data["observations"].setdefault(
        "anonymous_statistics",
        {"batch": None, "rolls": {}, "bling": [], "dig": {}, "prefs": []},
    )
    stats.setdefault("rolls", {})
    stats.setdefault("bling", [])
    stats.setdefault("dig", {})
    stats.setdefault("prefs", [])
    return stats


def queue_change(data, *, colour=None, bling=None, dig_targets=None, preferences=None):
    stats = _ensure_stats(data)
    changed = False
    if colour is not None:
        key = "|".join(colour)
        if key in stats["rolls"] or len(stats["rolls"]) < MAX_ROWS:
            stats["rolls"][key] = stats["rolls"].get(key, 0) + 1
            changed = True
    if bling is not None and len(stats["bling"]) < MAX_ROWS:
        stats["bling"].append(list(bling))
        changed = True
    if dig_targets:
        for item in dig_targets:
            label = str(item or "").strip()
            if label and (label in stats["dig"] or len(stats["dig"]) < MAX_ROWS):
                stats["dig"][label] = stats["dig"].get(label, 0) + 1
                changed = True
    if preferences is not None:
        digest = hashlib.sha256(json.dumps(preferences, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
        if digest != stats.get("prefs_hash"):
            stats["prefs"] = preferences[:MAX_ROWS]
            stats["prefs_hash"] = digest
            changed = True
    if changed:
        stats["batch"] = None  # New content needs a new idempotency key.
def sync_task_stats(account, root, *, extra_dig_targets=None):
    """Queue any new Pear-Pal dispatch targets, in-game dig targets, and task preferences."""
    if not is_task_stats_enabled(root):
        return
    prefs = collect_task_preferences(root)

    def apply(data):
        stats = _ensure_stats(data)
        targets = list(extra_dig_targets or [])
        notebook = data.get("observations", {}).get("pearpal_notebook")
        if isinstance(notebook, dict) and isinstance(notebook.get("dispatch"), list):
            dispatch = notebook["dispatch"]
            sig_items = sorted(
                f"{item.get('text', '')}:{item.get('start_time', 0)}"
                for item in dispatch
                if isinstance(item, dict) and item.get("text")
            )
            sig = "|".join(sig_items)
            if sig and sig != stats.get("last_dispatch_sig"):
                stats["last_dispatch_sig"] = sig
                for item in dispatch:
                    if isinstance(item, dict) and item.get("text"):
                        targets.append(str(item["text"]))
        queue_change(data, dig_targets=targets or None, preferences=prefs or None)

    try:
        account.update(apply)
    except (OSError, ValueError, KeyError, TypeError):
        pass


def upload(account, root, catalog_version=1):
    """Send the queue once in the background; the server deduplicates by batch."""
    url = endpoint(root)
    if not url:
        return None
    sync_task_stats(account, root)
    blessing_ok = is_enabled(root)
    task_ok = is_task_stats_enabled(root)
    if not blessing_ok and not task_ok:
        return None
    batch = {}

    def prepare(data):
        stats = data["observations"].get("anonymous_statistics")
        if not stats:
            return
        has_blessing = blessing_ok and (stats.get("rolls") or stats.get("bling"))
        has_task = task_ok and (stats.get("dig") or stats.get("prefs"))
        if not has_blessing and not has_task:
            return
        stats["batch"] = stats.get("batch") or uuid4().hex
        batch.update(json.loads(json.dumps(stats)))

    account.update(prepare)
    if not batch:
        return None

    def send():
        # Cloudflare rejects urllib's default User-Agent with error 1010.
        headers = {"Content-Type": "application/json", "User-Agent": "MaaNikki"}
        if batch.get("reporter"):
            headers["Authorization"] = "MaaNikkiID " + batch["reporter"]
        rolls_payload = (
            [[*key.split("|"), count] for key, count in batch.get("rolls", {}).items()]
            if blessing_ok else []
        )
        bling_payload = batch.get("bling", []) if blessing_ok else []
        dig_payload = (
            [[name, count] for name, count in batch.get("dig", {}).items()]
            if task_ok else []
        )
        prefs_payload = batch.get("prefs", []) if task_ok else []
        body = json.dumps({
            "schema": 1,
            "batch": batch["batch"],
            "catalog": str(catalog_version),
            "app": os.environ.get("PI_VERSION", "unknown"),
            "rolls": rolls_payload,
            "bling": bling_payload,
            "dig": dig_payload,
            "prefs": prefs_payload,
        }, ensure_ascii=False).encode("utf-8")
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
            if stats.get("batch") != batch["batch"]:
                return  # New entries arrived: the merged queue resends under a new key.
            stats["batch"] = None
            if blessing_ok:
                stats["rolls"] = {}
                stats["bling"] = []
            if task_ok:
                stats["dig"] = {}
                stats["prefs"] = []

        try:
            from account_state import AccountState
            AccountState(account.root, account.uid).update(clear)
        except (OSError, ValueError, KeyError, TypeError):
            pass

    thread = threading.Thread(target=send, name="eureka-statistics", daemon=True)
    thread.start()
    return thread
