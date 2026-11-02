"""Schedule independent inventory processing and resource acquisition."""
from collections import Counter

import cv2

from task_control import TaskStopped
from .scanner import RescanSlot
from .supply import CollectionSupply
from .decompose import DuplicateDecomposer, LowRarityDecomposer


class FiveStarPursuit:
    def __init__(self, inventory, *, decompose_duplicates=False, auto_bling=True, chance_priority=True,
                 share_statistics=False):
        self.inventory = inventory
        self.rt = inventory.rt
        self.decompose_duplicates = decompose_duplicates
        self.auto_bling = auto_bling
        self.chance_priority = chance_priority
        inventory.chance_priority = chance_priority
        inventory.share_statistics = share_statistics
        self.supply = CollectionSupply(self.rt, inventory)

    def make_room(self):
        """A full warehouse blocks farming, though the last batch may overflow it.

        Returns whether farming can go ahead. A decomposition failure before
        submission only skips today's farming; a submitted batch stays pending.
        """
        stock = self.inventory
        if not stock.warehouse_full():
            return True
        # New drops must be read and queued before auto-fill could reach them.
        stock.process_inventory()
        if self.decompose_duplicates:
            try:
                DuplicateDecomposer(stock).run()
            except TaskStopped:
                raise
            except (RuntimeError, ValueError, KeyError, TypeError, RescanSlot, cv2.error) as error:
                if stock.pending is not None:
                    raise
                stock.report["duplicate_decomposition"] = {"complete": False, "reason": str(error)}
                self.rt.diagnostic("blessing_duplicates", str(error))
                self.rt.log(f"五星分解未完成：{error}。", ui_text=f"五星分解未完成：{error}。")
        if not stock.warehouse_full():
            return True
        message = ("祝福闪光仓库已满且本次未能腾出空间，今天不刷本。" if self.decompose_duplicates else
                   "祝福闪光仓库已满，今天不刷本。请开启「分解五星祝福闪光」或手动分解。")
        stock.report["warehouse_full"] = True
        self.rt.log(message, ui_text=message)
        return False

    def acquire(self, kind, source=None, *, alternatives=()):
        self.inventory.report["phase"] = "realm_supply"
        outcome = self.supply.run(kind, source, alternatives=alternatives)
        self.inventory.report["supply_result"] = outcome
        self.inventory.verified.clear()
        if outcome["status"] == "reward_confirmed":
            self.inventory.organization["decomposition_pending"] = True
        self.inventory.checkpoint()
        if outcome["status"] == "reward_confirmed":
            spent = outcome["energy_before"] - outcome["energy_after"]
            self.rt.log(f"本轮刷取完成：{outcome['source']}，消耗 {spent} 点活跃能量；新增胚子待升级后计入满级颜色收藏。",
                        ui_text=f"刷取完成：{outcome['source']}，消耗 {spent} 点活跃能量。")
        elif outcome["status"] == "energy_exhausted":
            self.rt.log("活跃能量不足，本次只处理已有闪光。")
        return outcome["status"] == "reward_confirmed"

    def spend_on_bling(self):
        """A bling shortage leaves embryos waiting: more embryos cannot help today."""
        stock = self.inventory
        self.rt.log("噗灵不够升级已有胚子，今天的活跃能量改刷噗灵。", ui_text="噗灵不足，今天改刷噗灵。")
        stock.report["phase"] = "realm_bling"
        outcome = self.supply.bling()
        stock.report["bling_supply"] = outcome
        if outcome["status"] == "bling_confirmed":
            stock.organization["bling_refill"] = {
                "before": stock.organization.get("bling"),
                "energy": outcome["energy_before"] - outcome["energy_after"]}
        stock.checkpoint()
        return outcome["status"] != "not_spent"

    def spend_energy(self):
        stock = self.inventory
        value = Counter()
        for part in stock.progress()["parts"]:
            value[part["source"]] += (stock.new_colour_chance(part["name"], part["slot"])
                                      if self.chance_priority else len(part["missing_colors"]))
        # Stable sorting keeps catalog order for equal values.
        sources = sorted((source for source in value if value[source]), key=lambda source: -value[source])
        if not sources:
            return
        stock.report["realm_candidates"] = [
            {"rank": index + 1, "source": source, "value": round(value[source], 4)}
            for index, source in enumerate(sources)]
        stock.save()
        unit = "预期新色" if self.chance_priority else "缺色数"
        self.rt.log(f"五星追全幻境顺位（{unit}）：" + " → ".join(
            f"{source}（{value[source]:.2f}）" for source in sources)
            + "。按顺位选择能快速挑战的副本，用剩余活跃能量挑战一批，然后升级。",
            detail=True)
        self.acquire("bless", sources[0], alternatives=sources[1:])

    def run(self):
        stock = self.inventory
        stock.prepare("full_collection")
        if stock.progress()["complete"]:
            stock.report.update(complete=True, phase="complete",
                                reason="本地图鉴内五星颜色已收齐，请选择其他幻境；本次不消耗活跃能量",
                                inventory={"reused": True})
            self.rt.log(stock.report["reason"])
            stock.save()
            return True
        # One decision per day. Existing embryos go first; only what they leave decides.
        outcome = stock.process_inventory() if stock.queued_rolls() else None
        short = (outcome or {}).get("status") == "bling_shortage" and self.auto_bling
        if short and not self.spend_on_bling():
            self.rt.log("素材激化未消耗活跃能量（可能缺少材料），今天继续刷胚子。",
                        ui_text="素材激化未执行，改刷胚子。")
            short = False
        if not short and "recovered_reward" not in stock.report and self.make_room():
            self.spend_energy()
        if stock.organization.get("decomposition_pending"):
            # Clear low-rarity drops before reading acquisition order. The
            # decomposer updates old position hints; process_inventory then
            # discovers the surviving prefix once, before five-star auto-fill.
            LowRarityDecomposer(stock).run()
        # Even with no energy, process the saved unfinished item and inventory.
        outcome = stock.process_inventory()
        reasons = {
            "collection_complete": "本地图鉴内五星颜色已收齐，可选择其他幻境",
            "bling_shortage": "升级噗灵不足，已保存进度；可切换普通幻境挑战刷噗灵，补充后继续",
            "materials_shortage": "升级材料不足，已保存进度；下次刷取后优先继续当前闪光",
            "no_embryos": "本轮可升级的缺色五星已处理，保存进度",
        }
        return stock.finish(reasons[outcome["status"]])
