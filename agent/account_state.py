"""Per-role journals shared with the desktop client; game identity comes from pixels."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re

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


def game_day(now=None):
    return ((now or datetime.now(timezone.utc)).astimezone(GAME_TIMEZONE)
            - timedelta(hours=4)).date()


def period_key(task, now=None):
    day = game_day(now)
    if task == "weekly_free_gifts":
        return (day - timedelta(days=day.weekday())).isoformat()
    if task == "crown":
        return f"{day:%Y-%m}-{'01' if day.day < 16 else '16'}"
    return f"{day:%Y-%m}"


def read_json(path, default=None):
    if not path.is_file():
        return deepcopy(default)
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("账号状态 JSON 超过 64 MiB")
    return json.loads(path.read_text(encoding="utf-8-sig"))


@contextmanager
def account_lock(root):
    path = Path(root) / "config/accounts/store.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
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


def blank_account(uid):
    return {"schema_version": 3, "scope": "game_account", "region": "CN", "uid": uid,
            "tasks": {}, "blessing": {}, "observations": {}}


class AccountState:
    def __init__(self, root, uid, runtime=None):
        if not isinstance(uid, str) or re.fullmatch(r"[0-9]{1,32}", uid) is None:
            raise ValueError("游戏 UID 无效")
        self.root, self.uid = Path(root), uid
        self.runtime = runtime
        self.path = self.root / "config/accounts/CN" / (uid + ".json")

    @classmethod
    def for_runtime(cls, root, runtime):
        return cls(root, runtime.account_uid(), runtime)

    @classmethod
    def bind(cls, root, uid):
        account = cls(root, uid)
        with account_lock(root):
            index_path = Path(root) / "config/accounts/index.json"
            index = read_json(index_path, {"schema_version": 1})
            if not isinstance(index, dict) or index.get("schema_version") != 1:
                raise ValueError("账号索引格式无效")
            data = account.load()
            if data.get("migration", {}).get("legacy_game_state") and not index.get("legacy_game_account"):
                # A copied role JSON carries its completed migration receipt.
                # Losing the installation index must not reimport old globals.
                index["legacy_game_account"] = "CN:" + uid
            legacy_owner = index.get("legacy_game_account")
            if (legacy_owner in (None, "CN:" + uid)
                    and not data.get("migration", {}).get("legacy_game_state")):
                # Reserve one owner before publishing its file. After a crash,
                # only that same UID can resume this one-time migration.
                index["legacy_game_account"] = "CN:" + uid
                write_json(index_path, index)
                old_path = Path(root) / "config/account_state.json"
                legacy = read_json(old_path, {"schema_version": 2, "tasks": {}, "blessing": {}})
                if (not isinstance(legacy, dict) or legacy.get("schema_version") != 2
                        or not isinstance(legacy.get("tasks"), dict) or not isinstance(legacy.get("blessing"), dict)):
                    raise ValueError("旧账号状态 JSON 格式无效")
                # Existing UID-bound values win; preserve all old snapshots and
                # incomplete-operation journals under their original sections.
                for section in ("tasks", "blessing", "observations"):
                    old = legacy.get(section, {})
                    if not isinstance(old, dict):
                        raise ValueError("旧账号记录格式无效")
                    for key, value in old.items():
                        if value is not None and data[section].get(key) is None:
                            data[section][key] = deepcopy(value)
                for key in ("inventory", "last_scan"):
                    old = read_json(Path(root) / "config/blessing" / (key + ".json"))
                    if old is not None and data["blessing"].get(key) is None:
                        data["blessing"][key] = old
                # Retain the exact original file as migration evidence. It is
                # never used for another UID once this index marker is saved.
                data.setdefault("migration", {})["legacy_game_state"] = str(old_path)
            now = datetime.now(timezone.utc).isoformat()
            data["identity"] = {"source": "game_uid_ocr", "confirmed_at": now}
            data["updated_at"] = now
            write_json(account.path, data)
            index.update(active_game_account="CN:" + uid, updated_at=now)
            write_json(index_path, index)
        return account

    def load(self):
        data = read_json(self.path, blank_account(self.uid))
        if (not isinstance(data, dict) or data.get("schema_version") != 3
                or data.get("uid") != self.uid or data.get("region") != "CN"
                or any(not isinstance(data.get(section), dict) for section in ("tasks", "blessing", "observations"))
                or not isinstance(data.get("migration", {}), dict)):
            raise ValueError("UID 账号存档格式无效")
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

    def update(self, change):
        if self.runtime is not None:
            self.runtime.verify_account_uid()
        with account_lock(self.root):
            data = self.load()
            change(data)
            data["updated_at"] = datetime.now(timezone.utc).isoformat()
            if len(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")) > 64 * 1024 * 1024:
                raise ValueError("账号状态 JSON 超过 64 MiB")
            write_json(self.path, data)

    def save_blessing(self, key, snapshot, *, publish=False):
        records = {key: deepcopy(snapshot)}
        if publish:
            records["inventory"] = deepcopy(snapshot)
        self.update(lambda data: data["blessing"].update(records))

    def record_giant_meteor(self, outcome, *, day, text=""):
        if outcome not in {"found", "not_found", "unconfirmed"} or day != game_day().isoformat():
            raise ValueError("巨陨星检测结果或游戏日期无效")
        def change(data):
            observations = data["observations"]
            previous = observations.get("giant_meteor", {})
            if isinstance(previous, dict) and previous.get("game_day") == day:
                if previous.get("outcome") == "found" or (outcome == "unconfirmed"
                        and previous.get("outcome") == "not_found"):
                    return
            observations["giant_meteor"] = {
                "game_day": day, "outcome": outcome,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "source": "home_map", "text": str(text)[:500],
            }
        self.update(change)

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
