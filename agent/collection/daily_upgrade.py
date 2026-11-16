"""One confirmed upgrade for a daily wish, with optional ordered inventory reuse."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from uuid import uuid4

from daily.settings import ROOT
from .blessing import SLOTS
from .inventory_workflow import FiveStarInventory
from .policy import capabilities
from .scanner import IncompleteObservation, RescanSlot
from .statistics import is_enabled as stats_enabled, upload
from .upgrade import UpgradeUI
from .recovery import RecheckSlot


class DailyUpgrade(FiveStarInventory):
    def checkpoint(self):
        if self.prepared:
            super().checkpoint()
        else:
            self.save()

    def ensure_known_materials(self):
        if self.prepared:
            super().ensure_known_materials()

    def protect_selected(self, facts, generation):
        # Uninitialized daily wishes do not establish ownership of all colours.
        if self.prepared:
            return super().protect_selected(facts, generation)
        return False

    def confirm_upgrade(self, operation, old, new):
        if new["rarity"] == 5:
            return super().confirm_upgrade(operation, old, new)
        self.active.update(level=new["level"], color=new["color"], maximum=self.is_maximum(new))
        self.report["daily_upgrade"] = {**new, "previous_level": old["level"]}
        # Keep the five-star collection's latest_upgrade independent of lower rarities.
        self.confirm_change(operation, result=new)
        self.verified.clear()

    def _candidates(self, *, slot=None):
        items = []
        slots_to_check = [slot] if slot is not None else list(SLOTS)
        unfinished = self.organization.get("unfinished")
        for s in slots_to_check:
            for row in self.organization["slots"].get(s, []):
                if not row["maximum"] and row["mutable"] and not row.get("catalog_unknown"):
                    items.append(row)
        items.sort(key=lambda row: (-row["rarity"], row["id"] != unfinished, row["priority"]))
        return items

    def _locate_slot_candidates(self, slot, *, min_rarity=None):
        for target in self._candidates(slot=slot):
            if min_rarity is not None and target["rarity"] < min_rarity:
                continue
            rows = self.organization["slots"][target["slot"]]
            try:
                found = self.view.locate(target, rows, exact_lock=target["locked"], quantities=True)
            except RecheckSlot:
                if target["slot"] in self.report.get("recovered_slots", []):
                    raise
                self.rebuild_slot(target["slot"])
                return self._locate_slot_candidates(slot, min_rarity=min_rarity)
            if found is not None:
                if found[2]["copies_before"] > 1 and target["id"] != self.organization["unfinished"]:
                    active = deepcopy(target)
                    active["id"] = uuid4().hex
                    rows.insert(rows.index(target), active)
                    self.split_original = target
                else:
                    active = target
                return active, found
            if target["id"] == self.organization["unfinished"]:
                if target["slot"] in self.report.get("recovered_slots", []):
                    raise RecheckSlot(target["slot"], "上次未完成对象仍待核对，保留进度")
                self.rebuild_slot(target["slot"])
                return self._locate_slot_candidates(slot, min_rarity=min_rarity)
            rows.remove(target)
            self.checkpoint()
        return None

    def ordered_candidate(self):
        # Fast path: use cached 5-star candidates in place without scanning all 3 slot prefixes upfront.
        self.ensure_known_materials()
        cached = self._candidates()
        if cached and cached[0]["rarity"] == 5:
            seen_slots = []
            for item in cached:
                if item["rarity"] == 5 and item["slot"] not in seen_slots:
                    seen_slots.append(item["slot"])
            for slot in seen_slots:
                res = self._locate_slot_candidates(slot, min_rarity=5)
                if res is not None:
                    return res

        # Otherwise scan prefixes lazily one slot at a time and stop at the first 5-star candidate.
        scanned_slots = set()
        for slot in SLOTS:
            self.discover_slot_prefix(slot)
            self.ensure_known_materials()
            scanned_slots.add(slot)
            res = self._locate_slot_candidates(slot, min_rarity=5)
            if res is not None:
                return res

        # Fall back to lower rarities only when no 5-star embryo exists in any slot.
        for slot in SLOTS:
            if slot not in scanned_slots:
                self.discover_slot_prefix(slot)
                self.ensure_known_materials()
            res = self._locate_slot_candidates(slot)
            if res is not None:
                return res
        return None

    def live_candidate(self):
        fallback = None
        for slot in SLOTS:
            self.view.enter(slot)
            seen = set()
            for frame, cells, _ in self.view.pages(2000):
                for cell in cells:
                    ordinal = self.view.ordinal(cell[0])
                    if ordinal in seen:
                        continue
                    seen.add(ordinal)
                    _, level, match = cell
                    if level is not None and level >= 10:
                        continue
                    candidate = match.get("candidate") or {}
                    if match.get("identity_accepted"):
                        rarity = candidate["rarity"]
                        maximum = self.catalog["upgrade_costs"][str(rarity)]["max_level"]
                        if level is not None and level >= maximum:
                            continue
                        if fallback is not None and rarity < fallback["rarity"]:
                            continue
                    row, facts, evidence = self.view.observe(frame, cell)
                    flags = capabilities(self.catalog, facts["name"], observed=facts)
                    if row["maximum"] or not flags["lock_mutable"] or facts.get("catalog_unknown"):
                        continue
                    if facts["rarity"] == 5:
                        self.view.select_target(cell[0], facts, row["locked"], evidence)
                        return row, (cell[0], facts, evidence, self.view.generation)
                    if fallback is None or facts["rarity"] > fallback["rarity"]:
                        fallback = row
        if fallback is None:
            return None
        # No mutation occurred while reading. Revisit only the saved cell's area.
        self.view.enter(fallback["slot"])
        self.view.seek(fallback["ordinal"])
        for frame, cells, _ in self.view.pages(self.view.LOCAL_PAGES):
            for cell in cells:
                if self.view.ordinal(cell[0]) != fallback["ordinal"]:
                    continue
                row, facts, evidence = self.view.observe(frame, cell)
                if any(row[key] != fallback[key] for key in ("name", "slot", "rarity", "level", "locked")):
                    raise RuntimeError("朝夕升级候选与首次观察不一致")
                self.view.select_target(cell[0], facts, row["locked"], evidence)
                return row, (cell[0], facts, evidence, self.view.generation)
        raise RuntimeError("未能在记录位置附近找到朝夕升级候选")

    def run(self):
        self.split_original = None
        self.share_statistics = stats_enabled(ROOT)
        self.report.update(mode="daily_upgrade", workflow="daily_upgrade", phase="find_candidate")
        try:
            try:
                self.store.load_organization()
            except (OSError, ValueError, KeyError, TypeError):
                self.report["startup"] = {"mode": "live_inventory"}
            else:
                self.prepare("daily_upgrade")
            if not self.open():
                raise IncompleteObservation("未能打开祝福闪光列表")
            selected = self.ordered_candidate() if self.prepared else self.live_candidate()
            if selected is None:
                self.report.update(complete=True, phase="complete", reason="没有未满级的普通祝福闪光")
                self.rt.log("朝夕心愿：没有可升级的祝福闪光。")
                return {"status": "no_candidate"}
            self.active, found = selected
            self.active["anchor_eligible"] = False
            if self.prepared and self.active["rarity"] == 5 and not self.active["locked"]:
                self.organization["unfinished"] = self.active["id"]
            self.checkpoint()
            self.rt.log(f"朝夕心愿：升级{self.active['rarity']}星 {self.active['name']}·{self.active['slot']}。")
            result = UpgradeUI(self).run(*found, once=True)
            if self.prepared:
                if result["status"] != "upgraded" and self.split_original is not None:
                    # A rejected fill has not split the physical stack.
                    self.organization["slots"][self.active["slot"]].remove(self.active)
                    self.active = self.split_original
                    if self.active["rarity"] == 5 and not self.active["locked"]:
                        self.organization["unfinished"] = self.active["id"]
                if self.active["rarity"] == 5 and self.active["maximum"]:
                    if not self.active["locked"]:
                        self.organization["slots"][self.active["slot"]].remove(self.active)
                    self.organization["unfinished"] = None
                elif self.active["rarity"] != 5:
                    # Lower rarities never enter the five-star unfinished queue.
                    self.active["anchor_eligible"] = False
            self.report.update(complete=True, phase="complete", batch_result=result)
            if result["status"] == "upgraded":
                self.rt.log("朝夕心愿：祝福闪光升级完成，核对积分。")
            else:
                reason = "噗灵不足" if result["status"] == "bling_shortage" else "升级材料不足"
                self.rt.log(f"朝夕心愿：{reason}，本次升级结束。")
            return result
        except BaseException as error:
            self.record_interruption(error)
            raise
        finally:
            try:
                if self.share_statistics:
                    upload(self.store.account, ROOT, catalog_version=self.catalog["version"])
                self.checkpoint()
            except (OSError, ValueError):
                pass

    def begin_change(self, kind, facts, evidence, frame, **details):
        if not self.prepared:
            # A live daily wish cannot publish a complete ordered baseline.
            self.store.invalidate("朝夕升级已提交，库存等级及材料数量待复核", source="daily_upgrade")
        return super().begin_change(kind, facts, evidence, frame, **details)


def upgrade_once(runtime):
    import json

    from data_paths import data_path
    folder = ROOT / "logs/daily-upgrade" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    folder.mkdir(parents=True, exist_ok=True)
    catalog = json.loads(data_path("resource/data/blessing_catalog.json").read_text(encoding="utf-8"))
    try:
        return DailyUpgrade(runtime, catalog, folder).run()
    except RescanSlot as error:
        raise RuntimeError("焦点变化使本次升级定位失效，请重新运行") from error
