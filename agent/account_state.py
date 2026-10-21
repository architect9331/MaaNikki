"""Local account journal. Only confirmed observations may mark a period complete.

One installation tracks one game account, just like the original blessing store.
Changing MXU tabs does not change the game account. Legacy snapshots are imported
whole, including interrupted scans and ordered-operation journals.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from file_io import write_json

GAME_TIMEZONE = timezone(timedelta(hours=8))
PERIOD_TASKS = {
    "weekly_free_gifts": ("虹迹赠礼", "芳馨赠礼", "星实置业之礼"),
    "monthly_crystal_exchange": ("启示水晶", "共鸣水晶"),
    "crown": ("challenge",),
}
OUTCOMES = {
    "weekly_free_gifts": {"claimed", "sold_out"},
    "monthly_crystal_exchange": {"exchanged", "already_exchanged", "sold_out"},
    "crown": {"completed"},
}


def period_key(task, now=None):
    # Game days turn over at 04:00 China time; weeks start on Monday.
    day = ((now or datetime.now(timezone.utc)).astimezone(GAME_TIMEZONE)
           - timedelta(hours=4)).date()
    if task == "weekly_free_gifts":
        return (day - timedelta(days=day.weekday())).isoformat()
    if task == "crown":
        return f"{day:%Y-%m}-{'01' if day.day < 16 else '16'}"
    return f"{day:%Y-%m}"


class AccountState:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / "config/account_state.json"

    def load(self):
        if self.path.is_file():
            if self.path.stat().st_size > 64 * 1024 * 1024:
                raise ValueError("账号状态 JSON 超过 64 MiB")
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if (not isinstance(data, dict) or data.get("schema_version") != 2
                    or not isinstance(data.get("tasks"), dict)
                    or not isinstance(data.get("blessing"), dict)):
                raise ValueError("账号状态 JSON 格式无效")
            for task, record in data["tasks"].items():
                if task not in PERIOD_TASKS:
                    continue
                if (not isinstance(record, dict) or not isinstance(record.get("period"), str)
                        or not isinstance(record.get("items"), dict)):
                    raise ValueError("账号周期任务记录格式无效")
                for item, receipt in record["items"].items():
                    if (item not in PERIOD_TASKS[task] or not isinstance(receipt, dict)
                            or receipt.get("outcome") not in OUTCOMES[task]
                            or not isinstance(receipt.get("confirmed_at"), str)
                            or datetime.fromisoformat(receipt["confirmed_at"]).tzinfo is None):
                        raise ValueError("账号周期任务缺少有效的完成凭据")
            return data
        blessing = {}
        for key in ("inventory", "last_scan"):
            path = self.root / "config/blessing" / (key + ".json")
            if path.is_file():
                if path.stat().st_size > 20 * 1024 * 1024:
                    raise ValueError("旧版祝福闪光快照超过 20 MiB")
                blessing[key] = json.loads(path.read_text(encoding="utf-8-sig"))
        return {"schema_version": 2, "scope": "local_account", "tasks": {},
                "blessing": blessing}

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Separate lock survives atomic replacement of the journal itself.
        with self.path.with_suffix(".lock").open("a+b") as handle:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            import os
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def update(self, change):
        with self.locked():
            data = self.load()
            change(data)
            data["updated_at"] = datetime.now(timezone.utc).isoformat()
            write_json(self.path, data)

    def save_blessing(self, key, snapshot, *, publish=False):
        records = {key: deepcopy(snapshot)}
        if publish:
            records["inventory"] = deepcopy(snapshot)
        self.update(lambda data: data["blessing"].update(records))

    def completed_items(self, task, period=None):
        record = self.load()["tasks"].get(task, {})
        return dict(record.get("items", {})) if record.get("period") == (period or period_key(task)) else {}

    def complete(self, task):
        return set(PERIOD_TASKS[task]).issubset(self.completed_items(task))

    def confirm(self, task, item, outcome, *, period):
        if item not in PERIOD_TASKS[task] or outcome not in OUTCOMES[task]:
            raise ValueError("未知的周期任务项目")
        def change(data):
            record = data["tasks"].get(task, {})
            if record.get("period") != period:
                record = {"period": period, "items": {}}
            record["items"][item] = {"outcome": outcome,
                                      "confirmed_at": datetime.now(timezone.utc).isoformat()}
            data["tasks"][task] = record
        self.update(change)
