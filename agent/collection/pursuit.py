"""Schedule independent inventory processing and resource acquisition."""
from collections import Counter

from .supply import CollectionSupply
from .decompose import LowRarityDecomposer


class FiveStarPursuit:
    def __init__(self, inventory):
        self.inventory = inventory
        self.rt = inventory.rt
        self.supply = CollectionSupply(self.rt, inventory)

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
            self.rt.log(f"本轮刷取完成：{outcome['source']}，消耗 {spent} 点体力；新增胚子待升级后计入满级颜色收藏。",
                        ui_text=f"刷取完成：{outcome['source']}，消耗 {spent} 点体力。")
        elif outcome["status"] == "energy_exhausted":
            self.rt.log("体力不足，本次只处理已有闪光。")
        return outcome["status"] == "reward_confirmed"

    def run(self):
        stock = self.inventory
        stock.prepare("full_collection")
        if stock.progress()["complete"]:
            stock.report.update(complete=True, phase="complete",
                                reason="五星颜色已收齐，请选择其他幻境；本次不消耗体力",
                                inventory={"reused": True})
            self.rt.log(stock.report["reason"])
            stock.save()
            return True
        missing = Counter()
        for part in stock.progress()["parts"]:
            missing[part["source"]] += len(part["missing_colors"])
        if any(missing.values()):
            # Stable sorting keeps catalog order for equal missing-color counts.
            sources = sorted((source for source in missing if missing[source]),
                             key=lambda source: -missing[source])
            stock.report["realm_candidates"] = [
                {"rank": index + 1, "source": source, "missing_colors": missing[source]}
                for index, source in enumerate(sources)]
            stock.save()
            self.rt.log("五星追全幻境顺位（缺少满级颜色数）：" + " → ".join(
                f"{source}（{missing[source]}）" for source in sources)
                + "。按顺位选择能快速挑战的副本，用剩余体力挑战一批，然后升级。",
                detail=True)
            self.acquire("bless", sources[0], alternatives=sources[1:])
        if stock.organization.get("decomposition_pending"):
            LowRarityDecomposer(stock).run()
        # Even with no energy, process the saved unfinished item and inventory.
        outcome = stock.process_inventory()
        reasons = {
            "collection_complete": "五星颜色已收齐，可选择其他幻境",
            "bling_shortage": "升级噗灵不足，已保存进度；可切换普通幻境挑战刷噗灵，补充后继续",
            "materials_shortage": "升级材料不足，已保存进度；下次刷取后优先继续当前闪光",
            "no_embryos": "本轮可升级的缺色五星已处理，保存进度",
        }
        return stock.finish(reasons[outcome["status"]])
