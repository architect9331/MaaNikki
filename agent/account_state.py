"""Per-role journals shared with the desktop client; game identity comes from pixels."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import re

from file_io import write_json

GAME_TIMEZONE = timezone(timedelta(hours=8))
PERIOD_TASKS = {
    "weekly_free_gifts": ("虹迹赠礼", "芳馨赠礼", "星实置业之礼"),
    "weekly_home_shop": ("能量结晶", "闪亮泡泡"),
    "monthly_crystal_exchange": ("启示水晶", "共鸣水晶"),
    "crown": ("challenge",),
}
OUTCOMES = {
    "weekly_free_gifts": {"claimed", "sold_out"},
    "weekly_home_shop": {"purchased", "sold_out"},
    "monthly_crystal_exchange": {"exchanged", "already_exchanged", "sold_out"},
    "crown": {"completed"},
}
REDEEM_OUTCOMES = {"redeemed", "already_used", "expired"}
LOCKED_TELEPORT_TTL_DAYS = 7
LOCKED_TELEPORT_PRUNE_DAYS = 30


def _locked_region_key(map_id: str, region: str) -> str:
    return f"{map_id}::{region}"


def _locked_spire_key(map_id: str, region: str, name: str) -> str:
    return f"{map_id}::{region}::{name}"


def game_day(now=None):
    return ((now or datetime.now(timezone.utc)).astimezone(GAME_TIMEZONE)
            - timedelta(hours=4)).date()


def period_key(task, now=None):
    day = game_day(now)
    if task in ("weekly_free_gifts", "weekly_home_shop"):
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
            if task == "redeem_codes":
                if not isinstance(record, dict) or not isinstance(record.get("items"), dict):
                    raise ValueError("账号兑换码记录格式无效")
                for code, receipt in record["items"].items():
                    if (not isinstance(code, str) or not code or len(code) > 40
                            or not isinstance(receipt, dict)
                            or receipt.get("outcome") not in REDEEM_OUTCOMES
                            or not isinstance(receipt.get("confirmed_at"), str)
                            or datetime.fromisoformat(receipt["confirmed_at"]).tzinfo is None):
                        raise ValueError("账号兑换码缺少有效的处理凭据")
                continue
            if task not in PERIOD_TASKS:
                continue
            if (not isinstance(record, dict) or not isinstance(record.get("period"), str)
                    or not isinstance(record.get("items"), dict)):
                raise ValueError("账号周期任务记录格式无效")
            if task == "crown" and "stars" in record:
                if (type(record["stars"]) is not int or type(record.get("total")) is not int
                        or not 0 <= record["stars"] <= record["total"] == 24
                        or not isinstance(record.get("observed_at"), str)
                        or datetime.fromisoformat(record["observed_at"]).tzinfo is None):
                    raise ValueError("巅峰赛星数记录无效")
            if task == "crown" and "awards" in record:
                awards = record["awards"]
                if (not isinstance(awards, dict) or type(awards.get("claimed_stars")) is not int
                        or not 0 <= awards["claimed_stars"] <= 24
                        or not isinstance(awards.get("confirmed_at"), str)
                        or datetime.fromisoformat(awards["confirmed_at"]).tzinfo is None):
                    raise ValueError("巅峰赛领奖记录无效")
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

    def crown_progress(self):
        record = self.load()["tasks"].get("crown", {})
        # Old completion receipts carry no star count. Keep them readable until
        # the next calendar observation replaces them, but never infer stars.
        if record.get("period") == period_key("crown") and "stars" in record:
            return record["stars"], record["total"]
        return None

    def record_crown_progress(self, stars, total, *, period, source="calendar"):
        if (type(stars) is not int or type(total) is not int
                or not 0 <= stars <= total == 24 or source not in {"calendar", "awards"}):
            raise ValueError("巅峰赛星数无效")
        def change(data):
            previous = data["tasks"].get("crown", {})
            record = {
                "period": period, "items": {}, "stars": stars, "total": total,
                "observed_at": datetime.now(timezone.utc).isoformat(), "source": source,
            }
            if previous.get("period") == period and "awards" in previous:
                record["awards"] = previous["awards"]
            data["tasks"]["crown"] = record
        self.update(change)

    def record_crown_awards(self, stars, *, period):
        if type(stars) is not int or not 0 <= stars <= 24:
            raise ValueError("巅峰赛领奖星数无效")
        def change(data):
            record = data["tasks"].get("crown", {})
            if record.get("period") != period or record.get("stars") != stars:
                raise ValueError("巅峰赛领奖记录与当前星数不一致")
            record["awards"] = {
                "claimed_stars": stars, "confirmed_at": datetime.now(timezone.utc).isoformat(),
                "source": "awards_list",
            }
        self.update(change)

    def complete(self, task, *, target_stars=12):
        if task == "crown":
            record = self.load()["tasks"].get("crown", {})
            stars = record.get("stars")
            return (record.get("period") == period_key("crown") and stars is not None
                    and stars >= target_stars
                    and record.get("awards", {}).get("claimed_stars", -1) >= stars)
        return set(PERIOD_TASKS[task]).issubset(self.completed_items(task))

    def confirm(self, task, item, outcome, *, period):
        if task == "crown":
            raise ValueError("巅峰赛应记录实际星数")
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

    def redeemed_codes(self):
        record = self.load()["tasks"].get("redeem_codes", {})
        items = record.get("items", {})
        return {code: receipt.get("outcome") for code, receipt in items.items()}

    def record_redeem_code(self, code, outcome, *, detail=""):
        clean = str(code or "").strip()
        if not clean or len(clean) > 40 or any(ch.isspace() for ch in clean):
            raise ValueError("兑换码格式无效")
        if outcome not in REDEEM_OUTCOMES:
            raise ValueError("兑换码处理结果无效")

        def change(data):
            record = data["tasks"].get("redeem_codes")
            if not isinstance(record, dict) or not isinstance(record.get("items"), dict):
                record = {"items": {}}
            entry = {
                "outcome": outcome,
                "confirmed_at": datetime.now(timezone.utc).isoformat(),
            }
            if detail:
                entry["detail"] = str(detail)[:120]
            record["items"][clean] = entry
            data["tasks"]["redeem_codes"] = record

        self.update(change)

    def locked_teleports(self, *, ttl_days=LOCKED_TELEPORT_TTL_DAYS, today=None):
        """Return active (regions, spires) remembered as locked on this UID."""
        if ttl_days <= 0:
            return set(), set()
        current_day = today or game_day()
        raw = self.load()["observations"].get("locked_teleports")
        if not isinstance(raw, dict):
            return set(), set()

        def is_active(entry):
            if not isinstance(entry, dict):
                return False
            day_str = entry.get("observed_day")
            if not isinstance(day_str, str):
                return False
            try:
                observed = date.fromisoformat(day_str)
            except ValueError:
                return False
            age = (current_day - observed).days
            return 0 <= age < ttl_days

        regions: set[tuple[str, str]] = set()
        raw_regions = raw.get("regions")
        if isinstance(raw_regions, dict):
            for entry in raw_regions.values():
                if not is_active(entry):
                    continue
                map_id = str(entry.get("map") or "").strip()
                region = str(entry.get("region") or "").strip()
                if map_id and region:
                    regions.add((map_id, region))

        spires: set[tuple[str, str, str]] = set()
        raw_spires = raw.get("spires")
        if isinstance(raw_spires, dict):
            for entry in raw_spires.values():
                if not is_active(entry):
                    continue
                map_id = str(entry.get("map") or "").strip()
                region = str(entry.get("region") or "").strip()
                name = str(entry.get("name") or "").strip()
                if map_id and region and name:
                    spires.add((map_id, region, name))
        return regions, spires

    def record_locked_teleport(self, *, kind, map_id, region, name="", today=None):
        """Remember one locked map region or teleport spire for this UID."""
        clean_map = str(map_id or "").strip()
        clean_region = str(region or "").strip()
        clean_name = str(name or "").strip()
        if kind not in {"region", "spire"} or not clean_map or not clean_region:
            raise ValueError("未解锁传送点记录参数无效")
        if kind == "spire" and not clean_name:
            raise ValueError("未解锁流转之柱缺少名称")
        current_day = today or game_day()
        day_iso = current_day.isoformat()
        now_iso = datetime.now(timezone.utc).isoformat()

        def _prune(bucket):
            stale = []
            for key, entry in bucket.items():
                day_str = entry.get("observed_day") if isinstance(entry, dict) else None
                if not isinstance(day_str, str):
                    stale.append(key)
                    continue
                try:
                    age = (current_day - date.fromisoformat(day_str)).days
                except ValueError:
                    stale.append(key)
                    continue
                if age < 0 or age >= LOCKED_TELEPORT_PRUNE_DAYS:
                    stale.append(key)
            for key in stale:
                bucket.pop(key, None)

        def change(data):
            store = data["observations"].get("locked_teleports")
            if not isinstance(store, dict):
                store = {"schema_version": 1, "regions": {}, "spires": {}}
            regions = store.get("regions") if isinstance(store.get("regions"), dict) else {}
            spires = store.get("spires") if isinstance(store.get("spires"), dict) else {}
            _prune(regions)
            _prune(spires)
            if kind == "region":
                regions[_locked_region_key(clean_map, clean_region)] = {
                    "map": clean_map,
                    "region": clean_region,
                    "observed_day": day_iso,
                    "observed_at": now_iso,
                }
            else:
                spires[_locked_spire_key(clean_map, clean_region, clean_name)] = {
                    "map": clean_map,
                    "region": clean_region,
                    "name": clean_name,
                    "observed_day": day_iso,
                    "observed_at": now_iso,
                }
            data["observations"]["locked_teleports"] = {
                "schema_version": 1,
                "regions": regions,
                "spires": spires,
            }

        self.update(change)

    def clear_locked_teleport(self, *, regions=(), spires=()):
        """Remove confirmed-unlocked regions or teleport spires from UID memory."""
        region_keys = {
            _locked_region_key(str(m).strip(), str(r).strip())
            for m, r in regions
            if str(m).strip() and str(r).strip()
        }
        spire_keys = set()
        for m, r, n in spires:
            cm, cr, cn = str(m).strip(), str(r).strip(), str(n).strip()
            if cm and cr and cn:
                spire_keys.add(_locked_spire_key(cm, cr, cn))
                region_keys.add(_locked_region_key(cm, cr))
        if not region_keys and not spire_keys:
            return False
        existing = self.load()["observations"].get("locked_teleports")
        if not isinstance(existing, dict):
            return False
        existing_regions = existing.get("regions") if isinstance(existing.get("regions"), dict) else {}
        existing_spires = existing.get("spires") if isinstance(existing.get("spires"), dict) else {}
        if not (region_keys.intersection(existing_regions) or spire_keys.intersection(existing_spires)):
            return False

        cleared = False

        def change(data):
            nonlocal cleared
            store = data["observations"].get("locked_teleports")
            if not isinstance(store, dict):
                return
            reg_bucket = store.get("regions")
            if isinstance(reg_bucket, dict):
                for key in region_keys:
                    if reg_bucket.pop(key, None) is not None:
                        cleared = True
            sp_bucket = store.get("spires")
            if isinstance(sp_bucket, dict):
                for key in spire_keys:
                    if sp_bucket.pop(key, None) is not None:
                        cleared = True

        self.update(change)
        return cleared
