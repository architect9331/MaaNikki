"""Five-star protection, upgrades and persistent collection progress."""
from __future__ import annotations

from datetime import datetime
from copy import deepcopy
from uuid import uuid4

from daily.runtime import parameters
from .blessing import SLOTS
from .locking import BlessingLocker, READY
from file_io import write_json
from .storage import InventoryStore
from .upgrade import UpgradeUI, RESULT
from .ordered import OrderedView, merge_prefix
from .policy import UnregisteredMaterials
from .protection import pending_reward
from .scanner import IncompleteObservation
from .recovery import RecheckSlot, operation_scope


class FiveStarInventory(BlessingLocker):
    def __init__(self, runtime, catalog, folder):
        super().__init__(runtime, catalog, folder)
        self.upgrade_layout = parameters(runtime.context, "MaaNikki_Pursuit_Layout")
        self.definitions = {item["name"]: item for item in catalog["sets"]}
        self.targets = [item for item in catalog["sets"]
                        if item["rarity"] == 5 and item.get("collection_target", True)]
        self.protected = set()
        self.initial_protected = None
        self.report.update(workflow="five_star_pursuit", phase="load_baseline", goal_rarity=5,
                           material_rarity=5, complete=False, stop_reason=None)
        self.journal = self.report
        self.store = InventoryStore(folder.parents[2], catalog, runtime)
        self.working = deepcopy(self.snapshot)
        self.working["scan"]["view"] = "normal_inventory"
        self.prepared = False
        self.organization = None
        self.active = None
        self.view = OrderedView(self)

    def progress(self):
        parts = []
        for definition in self.targets:
            for slot in SLOTS:
                owned = [color for color in definition["colors"]
                         if (definition["name"], slot, color) in self.protected]
                parts.append({"name": definition["name"], "slot": slot,
                              "source": definition["source"], "locked_colors": owned,
                              "missing_colors": [c for c in definition["colors"] if c not in owned]})
        return {"rarity": 5, "material_rarity": 5, "phase": self.report["phase"],
                "mode": self.report.get("mode"), "existing_colors_checked": self.prepared,
                "protection_valid": self.prepared,
                "batch_result": self.report.get("batch_result"),
                "startup": self.report.get("startup"),
                "complete": all(not part["missing_colors"] for part in parts), "parts": parts,
                "stop_reason": self.report.get("stop_reason"),
                "latest_upgrade": self.report.get("latest_upgrade"),
                "pending_operation": self.pending,
                "recovery": deepcopy(self.organization.get("recovery")) if self.organization else None,
                "confirmed_locked_colors": len(self.protected),
                "target_colors": sum(len(item["colors"]) * len(SLOTS) for item in self.targets),
                "changes": self.collection_changes(),
                "operations_file": str(self.folder / "pursuit.json")}

    def collection_changes(self):
        if self.initial_protected is None:
            return None
        total = sum(len(item["colors"]) * len(SLOTS) for item in self.targets)
        added = self.protected - self.initial_protected
        removed = self.initial_protected - self.protected
        return {"before": len(self.initial_protected), "after": len(self.protected),
                "target": total, "missing": total - len(self.protected),
                "added": [{"name": name, "slot": slot, "color": color}
                          for name, slot, color in sorted(added)],
                "removed": [{"name": name, "slot": slot, "color": color}
                            for name, slot, color in sorted(removed)]}

    def log_progress(self, stage):
        changes = self.collection_changes()
        if changes is None:
            return
        owned, total = changes["after"], changes["target"]
        percentage = owned / total * 100 if total else 100
        self.rt.log(f"{stage}：五星满级颜色已收集 {owned}/{total}（{percentage:.1f}%），"
                    f"尚缺 {changes['missing']}；本轮 {changes['before']} → {owned}，"
                    f"新增 {len(changes['added'])} 个。",
                    ui_text=(f"五星收集：{owned}/{total}（{percentage:.1f}%），尚缺 {changes['missing']}。"
                             if stage == "开始处理" else
                             f"{stage}：已收集 {owned}/{total}，"
                             f"新增 {len(changes['added'])} 色，尚缺 {changes['missing']}。"))
        if stage != "开始处理":
            for item in changes["added"]:
                self.rt.log(f"本轮新增收藏：{item['name']}·{item['slot']}·{item['color']}色。", detail=True)
            if self.pending is not None:
                self.rt.log("尚未确认的操作未计入收集变化。", detail=True)

    def record_interruption(self, error, *, reason=None):
        """Invalidate only changes whose effect on existing keepers is unknown."""
        reason = str(error) if reason is None else reason
        self.report.update(complete=False, phase="interrupted", reason=reason)
        if self.organization is None:
            return
        observation = isinstance(error, IncompleteObservation)
        scope = operation_scope(self.pending) if self.pending is not None else []
        resumable = self.organization.get("complete") is True and scope is not None
        if resumable and isinstance(error, RecheckSlot):
            self.request_recheck(error.slot, reason)
        if resumable and self.pending is not None:
            for slot in scope:
                self.request_recheck(slot, reason)
        self.report["interruption"] = {
            "kind": ("operation_pending" if self.pending is not None else
                     "order_recheck" if isinstance(error, RecheckSlot) else
                     "observation_incomplete" if observation else "no_inventory_change"),
            "pending_operation": self.pending is not None,
            "baseline_preserved": resumable,
            "recovery_slots": list(self.organization.get("recovery", {}).get("slots", []))}
        self.organization.update(complete=resumable, reason=reason)

    def request_recheck(self, slot, reason):
        recovery = self.organization.setdefault("recovery", {"slots": [], "reasons": {}})
        if slot not in recovery["slots"]:
            recovery["slots"].append(slot)
        recovery["reasons"][slot] = str(reason)

    def recover_inventory(self):
        """Repair only affected slots before any fresh material consumption."""
        recovery = self.organization.get("recovery") or {}
        if self.pending is None and not recovery.get("slots"):
            return
        if not self.open_recovery():
            raise IncompleteObservation("恢复仓库进度时未能打开祝福闪光列表")
        self.inventory_read = True
        try:
            # Reward submissions only add a prefix. Four-star decomposition
            # is restarted from an empty selection of the remaining materials.
            if self.pending is not None and operation_scope(self.pending) == []:
                operation = self.pending
                if pending_reward(operation) and operation.get("input_attempted") is not False:
                    self.report["recovered_reward"] = deepcopy(operation)
                if (operation.get("input_attempted") is not False
                        and operation.get("kind") in ("realm", "decompose")):
                    self.organization["decomposition_pending"] = True
                self.reconcile_change("not_submitted" if operation.get("input_attempted") is False else
                                      "remaining_inventory")
                if operation.get("kind") == "decompose" and operation.get("input_attempted") is not False:
                    from .decompose import LowRarityDecomposer
                    LowRarityDecomposer(self).run()
            for slot in list(recovery.get("slots", [])):
                self.rebuild_slot(slot)
            self.organization.pop("recovery", None)
            self.organization["reason"] = None
            self.checkpoint()
        finally:
            self.inventory_read = False

    def open_recovery(self):
        """Leave recognised operation pages without submitting more materials."""
        upgrade = UpgradeUI(self)
        if self.rt.hit(RESULT):
            if not self.rt.ui.enter_page(lambda: self.rt.key("skip", 0), upgrade.upgrade_ready,
                                         source=RESULT, attempts=1):
                return False
        if upgrade.upgrade_ready():
            return self.rt.ui.enter_page(lambda: self.rt.key(27, 0), READY,
                                         source=upgrade.upgrade_ready, attempts=1)
        decompose = "MaaNikki_Blessing_DecomposeReady"
        if self.rt.hit(decompose):
            # Esc also safely cancels a still-open confirmation. Recheck the
            # decomposition page before trying the parent entrance again.
            return self.rt.ui.enter_page(lambda: self.rt.key(27, 0), READY,
                                         source=decompose, attempts=2)
        return self.open()

    def reconcile_change(self, outcome):
        """Archive an uncertain operation without inventing its item/XP delta."""
        if self.pending is None:
            return
        operation = deepcopy(self.pending)
        operation.update(status="reconciled", outcome=outcome,
                         reconciled=datetime.now().isoformat(timespec="seconds"))
        self.report.setdefault("recovered_operations", []).append(operation)
        self.pending = None
        self.checkpoint()

    def rebuild_slot(self, slot):
        """Reobserve one changed slot once, retaining the other two unchanged."""
        if slot in self.report.get("recovered_slots", []):
            raise RecheckSlot(slot, "该部位仍待核对，保留已确认进度")
        self.request_recheck(slot, "核对受影响部位的顺序及收藏")
        self.checkpoint()
        self.rt.log(f"正在恢复{slot}进度。")
        old = self.organization["slots"][slot]
        unfinished = next((row for row in old if row["id"] == self.organization["unfinished"]), None)
        observed, _ = self.view.scan(slot)
        # scan() reaches the physical end and checks every intervening cell.
        # No keeper is removed on an incomplete observation.
        rows, priority = merge_prefix(old, observed, len(old), self.organization["next_priority"])
        old_keys = {key for key in self.protected if key[1] == slot}
        present = {self.key(row) for row in rows if row["rarity"] == 5
                   and row["maximum"] and row["collectible"]}
        self.protected.difference_update(old_keys - present)
        self.protected.update(self.key(row) for row in rows if row["rarity"] == 5
                              and row["maximum"] and row["collectible"] and row["locked"])
        # A formerly protected copy found unlocked needs protection again.
        locked = {self.key(row) for row in rows if row["locked"] and row["maximum"]}
        self.protected.difference_update(old_keys - locked)
        self.organization["slots"][slot] = rows
        self.organization["next_priority"] = priority
        if unfinished is not None:
            retained = next((row for row in rows if row["id"] == unfinished["id"]), None)
            candidates = [row for row in rows if row["name"] == unfinished["name"]
                          and not row["locked"] and not row["maximum"] and row["level"] > 0]
            # A split stack can leave the old zero-level signature behind.
            # Prefer the uniquely observed partial upgrade over that remainder;
            # otherwise let the normal queue finish partial items first.
            changed = [row for row in candidates if row["level"] > unfinished["level"]]
            resumed = (changed[0] if len(changed) == 1 else
                       retained if retained is not None and retained["level"] > 0 else
                       candidates[0] if len(candidates) == 1 else
                       retained if not candidates else None)
            self.organization["unfinished"] = resumed["id"] if resumed is not None else None
        if self.pending is not None and slot in (operation_scope(self.pending) or []):
            self.reconcile_change("slot_reobserved")
        self.checkpoint()
        # Full-level results can merge with a more recent stack. Protect every
        # newly observed colour before auto-fill is allowed to consume it.
        for row in rows:
            if (row["rarity"] != 5 or not row["maximum"] or not row["collectible"]
                    or self.key(row) in self.protected):
                continue
            found = self.view.locate(row, rows, exact_lock=row["locked"])
            if found is None:
                raise RecheckSlot(slot, "恢复中的满级颜色尚未重新定位")
            self.active = row
            row["anchor_eligible"] = False
            self.protect_selected(found[1], found[3])
            self.active = None
            self.checkpoint()
        if self.organization["unfinished"] in {row["id"] for row in rows if row["locked"] and row["maximum"]}:
            self.organization["unfinished"] = None
        recovery = self.organization["recovery"]
        recovery["slots"].remove(slot)
        recovery["reasons"].pop(slot, None)
        self.view.slot = None
        self.verified.clear()
        self.report.setdefault("recovered_slots", []).append(slot)
        self.checkpoint()
        removed = old_keys - self.protected
        self.rt.log(f"{slot}进度已恢复。" + (f"核对移除 {len(removed)} 个已不在仓库中的收藏颜色。" if removed else ""))

    def save(self):
        self.report["collection"] = self.progress()
        write_json(self.folder / "pursuit.json", self.report)

    def checkpoint(self):
        # Automatic material selection may consume any unlocked rarity <= 5.
        # Keep only individually confirmed locked keepers in the progress JSON.
        # Unknown material consumption never becomes a fabricated item delta.
        self.working["items"] = [
            {"name": name, "slot": slot, "color": color, "rarity": 5, "level": 10,
             "count": 1, "xp": 0, "xp_known": True, "identity_known": True, "color_known": True}
            for name, slot, color in sorted(self.protected)]
        self.working["scan"].update(complete=False, traversed=False, slots={},
                                    workflow="five_star_pursuit", reason="仅保存已确认锁定的五星收藏；未锁定材料数量不统计，完整数量待单独整理核对")
        self.working["pursuit"] = self.progress()
        if self.organization is not None:
            self.organization["pending_operation"] = deepcopy(self.pending)
            self.working["organization"] = deepcopy(self.organization)
        self.working, _ = self.store.save_scan(self.working, self.folder, source="pursuit")
        write_json(self.folder / "inventory.json", self.working)
        self.save()

    def begin_change(self, kind, facts, evidence, frame, **details):
        if self.pending is not None:
            raise RuntimeError("上一次操作尚未确认，停止继续提交")
        operation = {"kind": kind, "status": "submitted_unconfirmed",
                     "input_attempted": False,
                     "started": datetime.now().isoformat(timespec="seconds"),
                     "facts": {key: facts[key] for key in ("name", "slot", "rarity", "level", "color")}
                     if facts else None, "evidence": evidence, **details,
                     "before": self.image(f"before_{len(self.report['operations']):04d}.png", frame)}
        if self.active is not None:
            operation["target_id"] = self.active["id"]
        self.report["operations"].append(operation)
        self.pending = operation
        self.checkpoint()
        return operation

    def submit_change(self, operation):
        """Persist uncertainty before input, so a process exit remains recoverable."""
        if self.pending is not operation:
            raise RuntimeError("操作记录与当前待提交项不一致")
        operation["input_attempted"] = True
        self.checkpoint()

    def confirm_change(self, operation, **details):
        operation.update(status="confirmed", confirmed=datetime.now().isoformat(timespec="seconds"), **details)
        self.pending = None
        self.checkpoint()

    def confirm_upgrade(self, operation, old, new):
        if self.active is None:
            raise RuntimeError("升级缺少持久化处理对象")
        self.active.update(level=new["level"], color=new["color"], maximum=self.is_maximum(new))
        self.report["latest_upgrade"] = {
            **{key: new[key] for key in ("name", "slot", "color", "level")},
            "previous_level": old["level"], "experience": operation.get("experience_after"),
            "confirmed_at": datetime.now().isoformat(timespec="seconds")}
        self.confirm_change(operation, result={key: new[key] for key in ("name", "slot", "color", "level")})
        self.verified.clear()
        self.rt.log(f"升级完成：{new['name']}·{new['slot']}，{old['level']} → {new['level']} 级。"
                    + (f"满级颜色：{new['color']}色。" if self.is_maximum(new) else
                       "本次心愿升级已完成。" if self.report.get("mode") == "daily_upgrade" else
                       "继续放入素材升级。"), detail=True)

    def protect_selected(self, facts, generation):
        key = self.key(facts)
        if key in self.protected:
            self.rt.log(f"{facts['name']}·{facts['slot']}·{facts['color']}色已有满级收藏，保留当前重复项未锁定。",
                        detail=True)
            return False
        if facts["rarity"] != 5 or facts["level"] != 10 or facts["color"] is None:
            raise RuntimeError("不能将未确认的五星满级颜色记为收藏")
        state = self.lock_state(generation, frame=self.frame(generation))
        changed = state == "unlocked"
        if changed:
            operation = self.begin_change("lock", facts, {"source": "verified_current_selection"}, self._lock_frame)
            # Match the proven inventory locking path: stage the hover, then
            # issue one raw Runtime click without its extra hover/unhover cycle.
            self._hover_read = None
            self._hover_box = None
            if not self.rt.action("TouchMove", target=self.layout["lock_button"]) or not self.rt.pause(.3):
                raise RuntimeError("未能准备新颜色锁定")
            self.check_generation(generation)
            self.submit_change(operation)
            if not self.rt.action("Click", target=self.layout["lock_button"], ui_click=False):
                raise RuntimeError("新颜色锁定提交不确定，已停止且不重试")
            if not self.rt.pause(.4) or self.lock_state(generation, frame=self.frame(generation)) != "locked":
                raise RuntimeError("新颜色锁定结果未确认，已停止且不重试")
            self.protected.add(key)
            if self.active is not None:
                self.active["locked"] = True
            self.confirm_change(operation, after=self.image(
                f"locked_{len(self.report['operations']):04d}.png", self._lock_frame))
            changes = self.collection_changes()
            progress = f"（{changes['after']}/{changes['target']}）" if changes is not None else ""
            self.rt.log(f"已保护五星满级颜色：{facts['name']}·{facts['slot']}·{facts['color']}色。"
                        + (f"五星收集进度 {progress}，本轮新增 {len(changes['added'])}，尚缺 {changes['missing']}。"
                           if changes is not None else ""),
                        ui_text=f"新增收藏：{facts['name']}·{facts['slot']}·{facts['color']}色，已锁定{progress}。")
        else:
            self.protected.add(key)
            if self.active is not None:
                self.active["locked"] = True
        return changed

    def part_complete(self, name, slot):
        return all((name, slot, color) in self.protected for color in self.definitions[name]["colors"])

    def discover_prefixes(self):
        """Connect only the new prefix to the last complete order, once per slot."""
        for slot in SLOTS:
            old = self.organization["slots"][slot]
            try:
                prefix, cut = self.view.scan(slot, old=old)
            except RecheckSlot:
                self.rebuild_slot(slot)
                continue
            rows, next_priority = merge_prefix(old, prefix, cut, self.organization["next_priority"])
            # Saved locked keepers must survive the observed replacement.
            old_keys = {self.key(row) for row in old[:cut] if row["locked"] and row["maximum"]}
            new_keys = {self.key(row) for row in prefix if row["locked"] and row["maximum"]}
            if not old_keys <= new_keys:
                self.rebuild_slot(slot)
                continue
            unfinished = self.organization["unfinished"]
            if unfinished and any(row["id"] == unfinished for row in old) and not any(
                    row["id"] == unfinished for row in rows):
                self.rebuild_slot(slot)
                continue
            self.organization["slots"][slot] = rows
            self.organization["next_priority"] = next_priority
            self.checkpoint()

    def next_target(self):
        rows = [row for slot in SLOTS for row in self.organization["slots"][slot]]
        unprotected = [row for row in rows if row["rarity"] == 5 and row["maximum"]
                       and row["collectible"] and not row["locked"] and self.key(row) not in self.protected]
        if unprotected:
            return min(unprotected, key=lambda row: (row["priority"], -row["ordinal"]))
        unfinished = self.organization["unfinished"]
        if unfinished:
            return next(row for row in rows if row["id"] == unfinished)
        candidates = [row for row in rows if row["rarity"] == 5 and not row["locked"] and row["collectible"]
                      and (row["maximum"] and self.key(row) not in self.protected
                           or not row["maximum"] and not self.part_complete(row["name"], row["slot"]))]
        return min(candidates, key=lambda row: (not row["maximum"], row["level"] == 0,
                                               row["priority"], -row["ordinal"])) if candidates else None

    def ensure_known_materials(self):
        """Game auto-fill ignores our catalogue and could consume new drops."""
        unknown = sorted({row["name"] for rows in self.organization["slots"].values()
                          for row in rows if row["name"] not in self.definitions and not row["locked"]})
        if unknown:
            self.report["unregistered_materials"] = unknown
            raise UnregisteredMaterials(
                "发现未锁定的图鉴外祝福闪光：" + "、".join(unknown)
                + "；为避免一键放入消耗新闪光，已暂停自动升级及分解。请更新图鉴，或手动锁定后重新整理")

    def process_inventory(self):
        if not self.prepared:
            raise RuntimeError("需先完成祝福闪光仓库读取基准")
        if self.progress()["complete"] and not self.organization["unfinished"]:
            return self.batch_result("collection_complete")
        self.report["phase"] = "upgrade_queue"
        if not self.open():
            raise IncompleteObservation("未能返回祝福闪光列表")
        self.inventory_read = True
        try:
            self.discover_prefixes()
            self.ensure_known_materials()
            self.rt.log("开始处理五星胚子。")
            for _ in range(1000):
                target = self.next_target()
                if target is None:
                    return self.batch_result("collection_complete" if self.progress()["complete"] else "no_embryos")
                rows = self.organization["slots"][target["slot"]]
                if target["locked"] and target["maximum"]:
                    self.organization["unfinished"] = None
                    self.checkpoint()
                    continue
                try:
                    found = self.view.locate(target, rows, quantities=True)
                except RecheckSlot:
                    if target["slot"] in self.report.get("recovered_slots", []):
                        raise
                    self.rebuild_slot(target["slot"])
                    continue
                if found is None:
                    if target["id"] == self.organization["unfinished"]:
                        if target["slot"] in self.report.get("recovered_slots", []):
                            raise RecheckSlot(target["slot"], "未完成对象仍待核对，保留进度")
                        self.rebuild_slot(target["slot"])
                        continue
                    # Both immutable boundaries were seen: a fed/merged old
                    # candidate is absent. No warehouse-wide lookup is needed.
                    rows.remove(target)
                    self.checkpoint()
                    continue
                if (target["id"] != self.organization["unfinished"]
                        and found[2]["copies_before"] > 1):
                    # Preserve a remainder only for a freshly observed stack.
                    # A single copy becomes the active item itself.
                    self.active = deepcopy(target)
                    self.active["id"] = uuid4().hex
                    rows.insert(rows.index(target), self.active)
                else:
                    self.active = target
                # Upgrade/lock can split or merge at another acquisition
                # position. A later ordered scan establishes its true order.
                self.active["anchor_eligible"] = False
                self.organization["unfinished"] = self.active["id"]
                self.checkpoint()
                if self.active["maximum"]:
                    self.protect_selected(found[1], found[3])
                    result = {"status": "upgraded", "facts": found[1]}
                else:
                    result = UpgradeUI(self).run(*found)
                if result["status"] != "upgraded":
                    self.report["shortage"] = result
                    return self.batch_result(result["status"], **{k: v for k, v in result.items() if k != "status"})
                if not self.active["locked"]:
                    # A duplicate may have merged elsewhere. It is material,
                    # never a stable position or a protected anchor.
                    rows.remove(self.active)
                self.active = None
                self.organization["unfinished"] = None
                self.checkpoint()
            raise RuntimeError("达到本轮升级数量上限")
        finally:
            self.inventory_read = False

    def batch_result(self, status, **details):
        result = {"status": status, **details}
        self.report["batch_result"] = result
        self.checkpoint()
        return result

    def prepare(self, mode):
        if self.prepared:
            raise RuntimeError("同一整理会话不能重复初始化")
        self.report["mode"] = mode
        order, protected, origin = self.store.load_organization()
        self.organization, self.protected = order, protected
        self.initial_protected = set(protected)
        if (origin.get("recovery") == "pending_reward"
                and order["pending_operation"].get("input_attempted") is not False):
            # Preserve the unknown submission in the new journal, without
            # claiming a reward or resubmitting it. Reconcile the new prefix.
            self.report["recovered_reward"] = deepcopy(order["pending_operation"])
            self.organization["decomposition_pending"] = True
        self.pending = deepcopy(self.organization.get("pending_operation"))
        self.prepared = True
        self.report["startup"] = {"mode": "ordered_json", "keepers": len(protected), **origin}
        if origin.get("recovery") in ("confirmed_legacy_lookup", "unsubmitted_observation"):
            self.rt.log(f"已核对上次操作记录，恢复 {len(protected)} 个已收藏颜色及待处理进度。")
        elif origin.get("recovery") == "pending_reward":
            self.rt.log(f"上次刷取结果待核对，保留 {len(protected)} 个已收藏颜色，本次先处理仓库。")
        elif origin.get("recovery") == "pending_operation":
            self.rt.log(f"保留 {len(protected)} 个已收藏颜色，正在核对上次操作。")
        self.rt.log(f"已读取完整获得时间基准及 {len(protected)} 个五星收藏；仅核对新增区域和待处理位置附近。", detail=True)
        self.log_progress("开始处理")
        self.save()
        self.ensure_known_materials()
        self.recover_inventory()

    def finish(self, reason):
        if (not self.prepared or self.pending is not None or not self.organization.get("complete")
                or self.organization.get("recovery", {}).get("slots")):
            raise RuntimeError("收集进度仍有未确认状态，不能正常结束")
        self.report.update(stop_reason=reason, reason=reason, collection_complete=self.progress()["complete"])
        shortage = self.report.get("batch_result", {}).get("status")
        self.rt.log(reason + "。", detail=shortage not in ("bling_shortage", "materials_shortage", "collection_complete"))
        self.report.update(complete=True, phase="complete")
        # Preserve the queue, including potentially consumed stacks. Local
        # bounded lookups remove absent candidates when they are next needed.
        self.checkpoint()
        self.report["inventory"] = {"published": False, "collection_saved": True,
                                    "quantity_state": "needs_rescan",
                                    "path": self.store.reference(self.store.last_scan_path)}
        self.rt.log("已保存收藏颜色、锁定状态和待处理队列；未锁定材料数量不统计，本轮直接结束。", detail=True)
        self.save()
        return True

    def run(self):
        """Standalone processing: shortages are normal exits, not farm requests."""
        self.prepare("inventory_only")
        outcome = self.process_inventory()
        reasons = {
            "collection_complete": "本地图鉴内五星全部部位的满级颜色已收齐并锁定",
            "bling_shortage": "升级噗灵不足，已保存进度；补充后可继续",
            "materials_shortage": "升级材料不足，已保存进度；补充后可继续",
            "no_embryos": "当前库存没有可升级的缺色五星，仅升级整理模式结束",
        }
        return self.finish(reasons[outcome["status"]])
