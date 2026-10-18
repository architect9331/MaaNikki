"""Local inventory shared by scanning and future collection tasks.

Use load() to read a copy, modify its items after confirmed game operations,
then save() with source="task". Never record a planned reward as owned.
The scan's created timestamp remains unchanged; record.updated_at tracks writes.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path

from .report import load_snapshot, validate_snapshot, write_json
from .protection import keeper_keys
from .blessing import SLOTS


class InventoryStore:
    def __init__(self, root, catalog):
        self.directory = Path(root) / "config" / "blessing"
        self.path = self.directory / "inventory.json"
        self.last_scan_path = self.directory / "last_scan.json"
        self.catalog = catalog

    def load(self):
        """Read validated inventory; missing/corrupt records must stop planning."""
        snapshot = load_snapshot(self.path, self.catalog)
        if snapshot.get("catalog_version") != self.catalog["version"]:
            raise ValueError("本地库存图鉴版本已变化，请重新扫描")
        if not snapshot["scan"]["identity_complete"]:
            raise ValueError("本地库存的部件及等级不完整，请重新扫描")
        if self.last_scan_path.is_file():
            latest = load_snapshot(self.last_scan_path, self.catalog)
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
        candidates = [(load_snapshot(path, self.catalog), path)
                      for path in (self.path, self.last_scan_path) if path.is_file()]
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
        return protected, {"path": str(path), "updated_at": latest["record"]["updated_at"],
                           "source": latest["record"]["source"]}

    def load_organization(self):
        """Full collection requires the newest complete ordered baseline."""
        candidates = [(load_snapshot(path, self.catalog), path)
                      for path in (self.path, self.last_scan_path) if path.is_file()]
        if not candidates:
            raise ValueError("请先完整运行一次“祝福闪光整理”")
        data, path = max(candidates, key=lambda pair: self.updated_at(pair[0]))
        order = deepcopy(data.get("organization"))
        if (data.get("catalog_version") != self.catalog["version"] or not isinstance(order, dict)
                or order.get("version") != 1 or order.get("complete") is not True
                or order.get("sort") != "acquired_desc"
                or set(order.get("slots", {})) != set(SLOTS)):
            raise ValueError("缺少完整的获得时间整理基准，请先完整运行“祝福闪光整理”（旧锁定记录不能代替）")
        operation = order.get("pending_operation")
        if operation is not None and (not isinstance(operation, dict) or operation.get("kind") != "realm"):
            raise ValueError("上次库存修改尚未确认，请重新运行祝福闪光整理")
        definitions = {item["name"]: item for item in self.catalog["sets"]}
        identifiers = set()
        protected = set()
        for slot, rows in order["slots"].items():
            if not isinstance(rows, list) or len(rows) > 10000:
                raise ValueError("顺序基准格式无效，请重新整理")
            for row in rows:
                definition = definitions.get(row.get("name")) if isinstance(row, dict) else None
                if not definition:
                    raise ValueError("顺序基准含未知闪光，请重新整理")
                maximum = self.catalog["upgrade_costs"][str(definition["rarity"])]["max_level"]
                if (row.get("slot") != slot or row.get("rarity") != definition["rarity"]
                        or type(row.get("level")) is not int or not 0 <= row["level"] <= maximum
                        or row.get("color") is not None and row["color"] not in definition["colors"]
                        or any(type(row.get(field)) is not bool for field in ("locked", "maximum", "mutable", "collectible"))
                        or row["maximum"] != (row["level"] == maximum)
                        or row["collectible"] != definition.get("collection_target", True)
                        or row["mutable"] != definition.get("lock_mutable", True)
                        or any(type(row.get(field)) is not int or row[field] < 0 for field in ("ordinal", "priority"))
                        or row["priority"] == 0 or not isinstance(row.get("id"), str) or row["id"] in identifiers):
                    raise ValueError("顺序基准条目无效，请重新整理")
                identifiers.add(row["id"])
                if row["locked"] and row["maximum"] and row["rarity"] == 5 and row["collectible"]:
                    if row["color"] is None:
                        raise ValueError("满级收藏颜色不完整，请重新整理")
                    protected.add((row["name"], slot, row["color"]))
        if (type(order.get("next_priority")) is not int
                or order["next_priority"] <= max((row["priority"] for rows in order["slots"].values() for row in rows), default=0)
                or order.get("unfinished") is not None and order["unfinished"] not in identifiers):
            raise ValueError("顺序进度无效，请重新整理")
        return order, protected, {"path": str(path), "updated_at": data["record"]["updated_at"]}

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
        fields = ("name", "slot", "rarity", "color", "level", "xp", "xp_known",
                  "identity_known", "color_known", "collection_target", "lock_mutable", "decomposable")
        for item in snapshot["items"]:
            item.setdefault("xp_known", False)
            key = tuple(item.get(field) for field in ("name", "slot", "color", "level", "xp", "xp_known"))
            entry = grouped.setdefault(key, {**{field: item.get(field) for field in fields},
                                             "count": 0, "locked_count": None,
                                             "lock_state_known": False, "evidence": []})
            entry["count"] += item["count"]
            if isinstance(item.get("evidence"), list):
                entry["evidence"].extend(item["evidence"])
            else:
                proof = {field: value for field, value in item.items()
                         if field not in (*fields, "count", "locked_count", "lock_state_known")}
                if proof:
                    entry["evidence"].append(proof)
        locks = snapshot["locks"]
        for entry in grouped.values():
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
        write_json(self.path, data)
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
        write_json(self.last_scan_path, data)
        published = data["scan"]["identity_complete"]
        if published:
            write_json(self.path, data)
        return data, published

    def invalidate(self, reason, *, source):
        """Mark stock stale before a reward submission without scanning the UI.

        Keep the last full inventory on disk and retain previously confirmed
        collection progress. Unknown rewards never become invented item counts.
        An interrupted or unconfirmed submission conservatively stays stale.
        """
        previous = [load_snapshot(path, self.catalog)
                    for path in (self.path, self.last_scan_path) if path.is_file()]
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
        write_json(self.last_scan_path, data)
        return data
