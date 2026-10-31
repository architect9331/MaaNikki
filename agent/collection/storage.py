"""Local inventory shared by scanning and future collection tasks.

Use load() to read a copy, modify its items after confirmed game operations,
then save() with source="task". Never record a planned reward as owned.
The scan's created timestamp remains unchanged; record.updated_at tracks writes.
"""
from __future__ import annotations

from copy import deepcopy
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path

from .report import validate_snapshot
from account_state import AccountState
from .protection import keeper_keys, pending_reward
from .blessing import SLOTS
from .policy import capabilities, definition_for, protected_inventory, unregistered_inventory


class InventoryStore:
    def __init__(self, root, catalog, runtime):
        self.directory = Path(root) / "config" / "blessing"
        self.path = self.directory / "inventory.json"
        self.last_scan_path = self.directory / "last_scan.json"
        self.catalog = catalog
        self.account = AccountState.for_runtime(root, runtime)

    def exists(self, path):
        return self.account.load()["blessing"].get(path.stem) is not None

    def reference(self, path):
        return f"{self.account.path}#blessing/{path.stem}"

    def read_snapshot(self, path):
        data = self.account.load()["blessing"].get(path.stem)
        if data is None:
            raise ValueError("尚未保存祝福闪光库存 JSON")
        return validate_snapshot(deepcopy(data), self.catalog)

    def load(self):
        """Read validated inventory; missing/corrupt records must stop planning."""
        snapshot = self.read_snapshot(self.path)
        if snapshot.get("catalog_version") != self.catalog["version"]:
            raise ValueError("本地库存图鉴版本已变化，请重新扫描")
        if not snapshot["scan"]["identity_complete"]:
            raise ValueError("本地库存的部件及等级不完整，请重新扫描")
        if self.exists(self.last_scan_path):
            latest = self.read_snapshot(self.last_scan_path)
            if (not latest["scan"]["identity_complete"]
                    and self.updated_at(latest) > self.updated_at(snapshot)):
                raise ValueError("最近一次扫描未完成，旧库存已保留；请重新扫描后再规划")
        return snapshot

    @staticmethod
    def updated_at(snapshot):
        record = snapshot.get("record")
        if not isinstance(record, dict) or not isinstance(record.get("updated_at"), str):
            raise ValueError("本地库存缺少更新时间，请重新扫描")
        value = datetime.fromisoformat(record["updated_at"])
        if value.tzinfo is None:
            raise ValueError("本地库存更新时间缺少时区，请重新扫描")
        return value

    def load_protection(self):
        """Use the newest trustworthy protection record, not stale quantities."""
        candidates = [(self.read_snapshot(path), path)
                      for path in (self.path, self.last_scan_path) if self.exists(path)]
        if not candidates:
            raise ValueError("尚未保存祝福闪光库存 JSON")
        candidates.sort(key=lambda pair: self.updated_at(pair[0]), reverse=True)
        latest, path = candidates[0]
        try:
            protected = keeper_keys(latest, self.catalog)
        except ValueError:
            # A stopped initial observation with zero operations cannot change
            # existing locks. Its journal must be local and explicitly match.
            progress = latest.get("pursuit", {})
            record = latest.get("record", {})
            if (record.get("source") != "pursuit" or not isinstance(progress, dict)
                    or progress.get("existing_colors_checked") is not False
                    or progress.get("pending_operation") is not None
                    or progress.get("latest_upgrade") is not None or len(candidates) < 2):
                raise
            folder = Path(record.get("evidence_dir", "")).resolve()
            logs = (self.directory.parents[1] / "logs/blessing-pursuit").resolve()
            if not folder.is_relative_to(logs):
                raise ValueError("上次中断记录的日志目录无效")
            report_path = folder / "pursuit.json"
            if not report_path.is_file() or report_path.stat().st_size > 20 * 1024 * 1024:
                raise ValueError("无法核对上次中断是否修改过库存")
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if (not isinstance(report, dict) or report.get("operations") != []
                    or report.get("collection") != progress):
                raise ValueError("上次中断存在操作或记录不一致，需重新核对")
            latest, path = candidates[1]
            protected = keeper_keys(latest, self.catalog)
        return protected, {"path": self.reference(path), "updated_at": latest["record"]["updated_at"],
                           "source": latest["record"]["source"]}

    def recover_legacy_lookup(self, data, order):
        """Recover only the old anchor bug with matching confirmed local evidence."""
        reason = "定位区间存在格子识别空缺，未将目标记为已消耗"
        progress = data.get("pursuit")
        record = data.get("record", {})
        if (order.get("complete") is not False or order.get("reason") != reason
                or order.get("anchor_policy_version", 1) != 1
                or order.get("pending_operation") is not None
                or order.get("unfinished") is not None
                or record.get("source") != "pursuit" or not isinstance(progress, dict)
                or progress.get("phase") != "interrupted"
                or progress.get("existing_colors_checked") is not True
                or progress.get("pending_operation") is not None):
            return False
        startup = progress.get("startup")
        if not isinstance(startup, dict) or startup.get("mode") != "ordered_json":
            return False
        folder = Path(record.get("evidence_dir", "")).resolve()
        logs = (self.directory.parents[1] / "logs/blessing-pursuit").resolve()
        if not folder.is_relative_to(logs):
            return False
        evidence = []
        for name in ("pursuit.json", "inventory.json"):
            path = folder / name
            if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
                return False
            evidence.append(json.loads(path.read_text(encoding="utf-8")))
        report, snapshot = evidence
        if not isinstance(report, dict) or not isinstance(snapshot, dict):
            return False
        operations = report.get("operations")
        if (report.get("workflow") != "five_star_pursuit"
                or report.get("phase") != "interrupted" or report.get("reason") != reason
                or report.get("startup") != startup or report.get("collection") != progress
                or snapshot.get("organization") != order or snapshot.get("pursuit") != progress
                or snapshot.get("record") != record
                or not isinstance(operations, list) or not operations
                or any(not isinstance(op, dict) or op.get("status") != "confirmed"
                       for op in operations)):
            return False
        return True

    def recover_reward_submission(self, data, order):
        """Resume acquisition-only uncertainty with the saved journal as evidence."""
        operation = order.get("pending_operation")
        progress = data.get("pursuit")
        record = data.get("record", {})
        if (not pending_reward(operation)
                or not isinstance(progress, dict)
                or progress.get("existing_colors_checked") is not True
                or progress.get("protection_valid") is not True
                or progress.get("pending_operation") != operation):
            return False
        startup = progress.get("startup")
        if not isinstance(startup, dict) or startup.get("mode") != "ordered_json":
            return False
        # New records keep the established baseline complete. Only repair the
        # older blanket invalidation using its matching local journal.
        if order.get("complete") is True:
            return True
        if record.get("source") != "pursuit":
            return False
        folder = Path(record.get("evidence_dir", "")).resolve()
        logs = (self.directory.parents[1] / "logs/blessing-pursuit").resolve()
        if not folder.is_relative_to(logs):
            return False
        evidence = []
        for name in ("pursuit.json", "inventory.json"):
            path = folder / name
            if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
                return False
            evidence.append(json.loads(path.read_text(encoding="utf-8")))
        report, snapshot = evidence
        if not isinstance(report, dict) or not isinstance(snapshot, dict):
            return False
        operations = report.get("operations")
        return (report.get("workflow") == "five_star_pursuit"
                and report.get("collection") == progress
                and snapshot.get("organization") == order
                and snapshot.get("pursuit") == progress
                and snapshot.get("record") == record
                and isinstance(operations, list) and bool(operations)
                and operations[-1] == operation
                and all(isinstance(op, dict) and op.get("status") == "confirmed"
                        for op in operations[:-1]))

    def load_organization(self):
        """Full collection requires the newest complete ordered baseline."""
        candidates = [(self.read_snapshot(path), path)
                      for path in (self.path, self.last_scan_path) if self.exists(path)]
        if not candidates:
            raise ValueError("请先完整运行一次“祝福闪光仓库读取”")
        data, path = max(candidates, key=lambda pair: self.updated_at(pair[0]))
        order = deepcopy(data.get("organization"))
        if (data.get("catalog_version") != self.catalog["version"] or not isinstance(order, dict)
                or order.get("version") != 1
                or order.get("sort") != "acquired_desc"
                or set(order.get("slots", {})) != set(SLOTS)):
            raise ValueError("缺少完整的获得时间整理基准，请先完整运行“祝福闪光仓库读取”（旧锁定记录不能代替）")
        operation = order.get("pending_operation")
        reward_recovery = self.recover_reward_submission(data, order) if pending_reward(operation) else False
        recovered = False
        if order.get("complete") is not True:
            recovered = self.recover_legacy_lookup(data, order)
            if not recovered and not reward_recovery:
                raise ValueError("整理基准需重新核对："
                                 + str(order.get("reason") or "尚未完成完整整理")
                                 + "；请运行“祝福闪光仓库读取”")
        if operation is not None and not reward_recovery:
            raise ValueError("上次库存修改尚未确认，请重新运行祝福闪光仓库读取")
        identifiers = set()
        protected = set()
        for slot, rows in order["slots"].items():
            if not isinstance(rows, list) or len(rows) > 10000:
                raise ValueError("顺序基准格式无效，请重新整理")
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("顺序基准含未知闪光，请重新整理")
                definition = definition_for(self.catalog, row.get("name"), observed=row)
                flags = capabilities(self.catalog, row.get("name"), observed=row)
                maximum = self.catalog["upgrade_costs"][str(definition["rarity"])]["max_level"]
                if (row.get("slot") != slot or row.get("rarity") != definition["rarity"]
                        or type(row.get("level")) is not int or not 0 <= row["level"] <= maximum
                        or row.get("color") is not None and row["color"] not in definition["colors"]
                        or any(type(row.get(field)) is not bool for field in ("locked", "maximum", "mutable", "collectible"))
                        or row["maximum"] != (row["level"] == maximum)
                        or row["collectible"] != flags["collection_target"]
                        or row["mutable"] != flags["lock_mutable"]
                        or row.get("protected_inventory") and not protected_inventory(row)
                        or row.get("catalog_unknown") and not unregistered_inventory(row)
                        or protected_inventory(row) and not row["locked"]
                        or any(type(row.get(field)) is not int or row[field] < 0 for field in ("ordinal", "priority"))
                        or row["priority"] == 0 or not isinstance(row.get("id"), str) or row["id"] in identifiers):
                    raise ValueError("顺序基准条目无效，请重新整理")
                identifiers.add(row["id"])
                if row["locked"] and row["maximum"] and row["rarity"] == 5 and row["collectible"]:
                    if row["color"] is None:
                        raise ValueError("满级收藏颜色不完整，请重新整理")
                    protected.add((row["name"], slot, row["color"]))
            # Older pursuit records cloned each upgraded copy with its queue
            # group's priority, then treated that inferred insertion order as
            # a physical anchor. Keep the collection but reobserve its order.
            if order.get("anchor_policy_version", 1) < 2:
                priorities = Counter(row["priority"] for row in rows)
                for row in rows:
                    if priorities[row["priority"]] > 1:
                        row["anchor_eligible"] = False
        if (type(order.get("next_priority")) is not int
                or order["next_priority"] <= max((row["priority"] for rows in order["slots"].values() for row in rows), default=0)
                or order.get("unfinished") is not None and order["unfinished"] not in identifiers):
            raise ValueError("顺序进度无效，请重新整理")
        origin = {"path": self.reference(path), "updated_at": data["record"]["updated_at"]}
        if recovered or reward_recovery:
            if protected != keeper_keys(data, self.catalog):
                raise ValueError("顺序基准与已确认收藏不一致，请重新整理")
            order.update(complete=True, reason=None)
            origin["recovery"] = "pending_reward" if reward_recovery else "confirmed_legacy_lookup"
        order["anchor_policy_version"] = 2
        return order, protected, origin

    def prepare(self, snapshot, *, source, evidence_dir=None):
        data = validate_snapshot(deepcopy(snapshot), self.catalog)
        if data.get("catalog_version", self.catalog["version"]) != self.catalog["version"]:
            raise ValueError("库存图鉴版本已变化，请重新扫描")
        data["catalog_version"] = self.catalog["version"]
        data["scan"]["quantity_state"] = "verified" if data["scan"]["identity_complete"] else "needs_rescan"
        data["items"] = self.canonical_items(data)
        record = data.setdefault("record", {})
        if not isinstance(record, dict):
            raise ValueError("库存记录元数据必须是对象")
        record.update(updated_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                      source=source)
        if evidence_dir is not None:
            record["evidence_dir"] = Path(evidence_dir).resolve().as_posix()
        return data

    @staticmethod
    def canonical_items(snapshot):
        """One record per variant/level/XP; copies and lock counts are explicit."""
        grouped = {}
        known_locks = {}
        fields = ("name", "slot", "rarity", "color", "level", "xp", "xp_known",
                  "identity_known", "color_known", "collection_target", "lock_mutable", "decomposable",
                  "protected_inventory", "catalog_unknown", "inventory_evidence")
        for item in snapshot["items"]:
            item.setdefault("xp_known", False)
            key = tuple(item.get(field) for field in ("name", "slot", "color", "level", "xp", "xp_known"))
            entry = grouped.setdefault(key, {**{field: item.get(field) for field in fields},
                                             "count": 0, "locked_count": None,
                                             "lock_state_known": False, "evidence": []})
            entry["count"] += item["count"]
            if (not item["lock_mutable"] and item.get("lock_state_known")
                    and item.get("locked_count") is not None):
                entry["locked_count"] = (entry["locked_count"] or 0) + item["locked_count"]
                known_locks[key] = known_locks.get(key, 0) + item["count"]
            if isinstance(item.get("evidence"), list):
                entry["evidence"].extend(item["evidence"])
            else:
                proof = {field: value for field, value in item.items()
                         if field not in (*fields, "count", "locked_count", "lock_state_known")}
                if proof:
                    entry["evidence"].append(proof)
        locks = snapshot["locks"]
        for key, entry in grouped.items():
            if not entry["lock_mutable"]:
                entry["lock_state_known"] = known_locks.get(key, 0) == entry["count"]
                if not entry["lock_state_known"]:
                    entry["locked_count"] = None
                continue
            exact = [group for group in locks["groups"] if all(
                group.get(field) == entry.get(field) for field in ("name", "slot", "color", "level"))]
            base = [group for group in locks["groups"] if all(
                group.get(field) == entry.get(field) for field in ("name", "slot", "level"))]
            siblings = [other for other in grouped.values() if all(
                other.get(field) == entry.get(field) for field in ("name", "slot", "color", "level"))]
            # Unknown colours and split XP groups cannot establish per-row locks.
            if locks["complete"] and entry["color"] is not None and len(siblings) == 1 and not any(
                    group.get("color") is None for group in base):
                entry["locked_count"] = sum(group["locked_count"] for group in exact)
                entry["lock_state_known"] = True
        return sorted(grouped.values(), key=lambda item: (
            item["name"], item["slot"], item["color"] or "", item["level"], item["xp"], item["xp_known"]))

    def save(self, snapshot, *, source="task", evidence_dir=None):
        """Replace known inventory atomically; unknown colours remain explicit.

        Callers must keep count/level/colour and scan.reported_capacity in sync
        after actual additions, upgrades or removals. Update locks.groups too,
        or mark locks.complete=False when the new lock counts are unknown.
        This API assumes serial
        task execution; concurrent writers need coordination by the scheduler.
        """
        data = self.prepare(snapshot, source=source, evidence_dir=evidence_dir)
        if not data["scan"]["identity_complete"]:
            raise ValueError("部件及等级不完整的记录不能覆盖本地库存")
        self.account.save_blessing("inventory", data)
        return data

    def save_scan(self, snapshot, evidence_dir, *, source="scan"):
        """Always retain the latest attempt, publishing only complete identities."""
        if source == "scan" and "organization" not in snapshot:
            # A read-only quantity scan cannot create an ordered baseline, but
            # must not discard an already valid one either.
            try:
                order, _, _ = self.load_organization()
            except (OSError, ValueError, KeyError, TypeError):
                pass
            else:
                snapshot = deepcopy(snapshot)
                snapshot["organization"] = order
        data = self.prepare(snapshot, source=source, evidence_dir=evidence_dir)
        published = data["scan"]["identity_complete"]
        self.account.save_blessing("last_scan", data, publish=published)
        return data, published

    def invalidate(self, reason, *, source):
        """Mark stock stale before a reward submission without scanning the UI.

        Keep the last full inventory on disk and retain previously confirmed
        collection progress. Unknown rewards never become invented item counts.
        An interrupted or unconfirmed submission conservatively stays stale.
        """
        previous = [self.read_snapshot(path)
                    for path in (self.path, self.last_scan_path) if self.exists(path)]
        latest = max(previous, key=self.updated_at) if previous else {}
        partial = {
            "schema_version": 1, "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "items": [], "excluded_items": [], "unresolved": [],
            "scan": {"complete": False, "traversed": False, "slots": {},
                     "reported_capacity": None, "reason": reason,
                     "warnings": ["库存数量待复核；空条目列表不表示仓库为空"],
                     "workflow": "inventory_invalidated"},
        }
        if latest.get("catalog_version") == self.catalog["version"] and isinstance(latest.get("pursuit"), dict):
            partial["pursuit"] = deepcopy(latest["pursuit"])
        if source == "realm_bless" and latest:
            if isinstance(latest.get("organization"), dict):
                partial["organization"] = deepcopy(latest["organization"])
            try:
                protected, _ = self.load_protection()
            except (OSError, ValueError, KeyError, TypeError):
                pass  # Quantity invalidation must not invent a protection proof.
            else:
                partial["protection"] = {"complete": True, "keepers": [
                    {"name": name, "slot": slot, "color": color}
                    for name, slot, color in sorted(protected)]}
        data = self.prepare(partial, source=source)
        self.account.save_blessing("last_scan", data)
        return data
