"""Five-star protection, upgrades and persistent collection progress."""
from __future__ import annotations

from datetime import datetime
from copy import deepcopy
from uuid import uuid4

from daily.runtime import parameters
from .blessing import SLOTS
from .locking import BlessingLocker
from .report import write_json
from .storage import InventoryStore
from .upgrade import UpgradeUI
from .ordered import OrderedView, merge_prefix, pending


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
        self.store = InventoryStore(folder.parents[2], catalog)
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
                     "started": datetime.now().isoformat(timespec="seconds"),
                     "facts": {key: facts[key] for key in ("name", "slot", "rarity", "level", "color")}
                     if facts else None, "evidence": evidence, **details,
                     "before": self.image(f"before_{len(self.report['operations']):04d}.png", frame)}
        self.report["operations"].append(operation)
        self.pending = operation
        self.checkpoint()
        return operation

    def confirm_change(self, operation, **details):
        operation.update(status="confirmed", confirmed=datetime.now().isoformat(timespec="seconds"), **details)
        self.pending = None
        self.checkpoint()

    def confirm_upgrade(self, operation, old, new):
        if self.active is None:
            raise RuntimeError("升级缺少持久化处理对象")
        self.active.update(level=new["level"], color=new["color"], maximum=new["level"] == 10)
        self.report["latest_upgrade"] = {
            **{key: new[key] for key in ("name", "slot", "color", "level")},
            "previous_level": old["level"], "experience": operation.get("experience_after"),
            "confirmed_at": datetime.now().isoformat(timespec="seconds")}
        self.confirm_change(operation, result={key: new[key] for key in ("name", "slot", "color", "level")})
        self.verified.clear()
        self.rt.log(f"升级完成：{new['name']}·{new['slot']}，{old['level']} → {new['level']} 级。"
                    + (f"满级颜色：{new['color']}色。" if new["level"] == 10 else "继续放入素材升级。"), detail=True)

    def protect_selected(self, facts, generation):
        key = self.key(facts)
        if key in self.protected:
            self.rt.log(f"{facts['name']}·{facts['slot']}·{facts['color']}色已有满级收藏，保留当前重复项未锁定。",
                        ui_text=f"重复颜色：{facts['name']}·{facts['slot']}·{facts['color']}色，留作素材。")
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
            prefix, cut = self.view.scan(slot, old=old)
            rows, next_priority = merge_prefix(old, prefix, cut, self.organization["next_priority"])
            # Saved locked keepers must survive the observed replacement.
            old_keys = {self.key(row) for row in old[:cut] if row["locked"] and row["maximum"]}
            new_keys = {self.key(row) for row in prefix if row["locked"] and row["maximum"]}
            if not old_keys <= new_keys:
                raise RuntimeError("新增区核对发现旧收藏改变，请重新运行祝福闪光整理")
            unfinished = self.organization["unfinished"]
            if unfinished and any(row["id"] == unfinished for row in old) and not any(
                    row["id"] == unfinished for row in rows):
                raise RuntimeError("上次未完成对象无法与新顺序衔接，请重新整理")
            self.organization["slots"][slot] = rows
            self.organization["next_priority"] = next_priority
            self.checkpoint()

    def next_target(self):
        rows = [row for slot in SLOTS for row in self.organization["slots"][slot]]
        unfinished = self.organization["unfinished"]
        if unfinished:
            return next(row for row in rows if row["id"] == unfinished)
        candidates = [row for row in rows if pending(row) and not self.part_complete(row["name"], row["slot"])]
        return min(candidates, key=lambda row: (row["priority"], -row["ordinal"])) if candidates else None

    def process_inventory(self):
        if not self.prepared:
            raise RuntimeError("需先完成祝福闪光整理基准")
        if self.progress()["complete"] and not self.organization["unfinished"]:
            return self.batch_result("collection_complete")
        self.report["phase"] = "upgrade_queue"
        if not self.open():
            raise RuntimeError("未能返回祝福闪光列表")
        self.inventory_read = True
        try:
            self.discover_prefixes()
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
                found = self.view.locate(target, rows, quantities=True)
                if found is None:
                    if target["id"] == self.organization["unfinished"]:
                        raise RuntimeError("上次未完成对象在定位区间内消失，请重新整理")
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
        self.organization["pending_operation"] = None
        self.prepared = True
        self.report["startup"] = {"mode": "ordered_json", "keepers": len(protected), **origin}
        if origin.get("recovery") == "confirmed_legacy_lookup":
            self.rt.log(f"已核对上次操作记录，恢复 {len(protected)} 个已收藏颜色及待处理进度。")
        self.rt.log(f"已读取完整获得时间基准及 {len(protected)} 个五星收藏；仅核对新增区域和待处理位置附近。", detail=True)
        self.log_progress("开始处理")
        self.save()

    def finish(self, reason):
        if not self.prepared or self.pending is not None or not self.organization.get("complete"):
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
                                    "path": str(self.store.last_scan_path)}
        self.rt.log("已保存收藏颜色、锁定状态和待处理队列；未锁定材料数量不统计，本轮直接结束。", detail=True)
        self.save()
        return True

    def run(self):
        """Standalone processing: shortages are normal exits, not farm requests."""
        self.prepare("inventory_only")
        outcome = self.process_inventory()
        reasons = {
            "collection_complete": "五星全部部位的满级颜色已收齐并锁定",
            "bling_shortage": "升级噗灵不足，已保存进度；补充后可继续",
            "materials_shortage": "升级材料不足，已保存进度；补充后可继续",
            "no_embryos": "当前库存没有可升级的缺色五星，仅升级整理模式结束",
        }
        return self.finish(reasons[outcome["status"]])
