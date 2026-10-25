"""Realm resource acquisition using the scheduler's ranked source candidates."""
import re

from daily.gameplay import Gameplay
from daily.runtime import Executors


class CollectionSupply:
    def __init__(self, runtime, journal):
        self.rt, self.journal = runtime, journal

    def run(self, kind, source=None, *, alternatives=()):
        if kind != "bless" or not source:
            raise ValueError("全收集只获取指定副本的祝福闪光，不自动兑换噗灵")
        if not self.rt.ui.calendar():
            raise RuntimeError("未能打开幻境挑战入口")
        game = Gameplay(self.rt)
        energy = game.read_energy()
        if energy is None:
            raise RuntimeError("无法读取当前活跃能量，未提交幻境")
        if energy < game.MIN_ENERGY[kind]:
            return {"status": "energy_exhausted"}
        clone = self.rt.context.clone()
        child = Executors(clone)
        game = Gameplay(child)
        try:
            candidates = list(dict.fromkeys((source, *alternatives)))
            availability = self.journal.report["realm_availability"] = []
            for candidate in candidates:
                if not child.ui.realm(kind):
                    raise RuntimeError("未能打开祝福闪光幻境列表，未提交挑战")
                self.check_list(child)
                if not clone.override_pipeline({"MaaNikki_Bless_Level": {"expected": re.escape(candidate)}}):
                    raise RuntimeError("追全幻境设置未能应用")
                found = child.ui.find(roi=game.REALM_LEVEL_ROI, node="MaaNikki_Bless_Level",
                                      scroll_roi=game.REALM_SCROLL_ROI, click=False)
                self.check_list(child)
                entry = {"source": candidate, "status": "found" if found else "not_found_in_list"}
                availability.append(entry)
                self.journal.save()
                if not found:
                    self.rt.log(f"幻境列表未找到 {candidate}，可能尚未解锁，本次跳过并检查下一顺位。",
                                ui_text=f"跳过 {candidate}：列表中未找到。")
                    continue
                self.rt.log(f"五星追全检查 {candidate} 的快速挑战入口。", detail=True)
                operation = None

                def before_submit():
                    nonlocal operation
                    # List/quick-entry failures cannot modify inventory. Only
                    # create a pending reward immediately before submission.
                    operation = self.journal.begin_change(
                        "realm", None, {"source": "challenge_confirmation"}, child.capture(),
                        realm=kind, source=candidate, energy_before=energy)
                    self.rt.log(f"五星追全实际选择 {candidate}，使用剩余活跃能量快速挑战一批。",
                                ui_text=f"开始刷取：{candidate}。")

                ok = game.realm(kind, maximize=True, allow_exhausted=True, return_page="calendar",
                                allow_unavailable=True, before_submit=before_submit)
                if game.quick_unavailable and operation is None:
                    # Returning from realm() has restored the calendar. Verify
                    # no energy was consumed before allowing a different realm.
                    remaining = game.read_energy()
                    if remaining is None:
                        raise RuntimeError("快速挑战不可用后无法核对活跃能量，停止换本")
                    if remaining < game.MIN_ENERGY[kind]:
                        return {"status": "energy_exhausted"}
                    if remaining < energy:
                        raise RuntimeError("快速挑战检查期间活跃能量减少，停止换本并保留记录")
                    entry.update(status="quick_unavailable", energy_before=energy, energy_after=remaining)
                    # Natural regeneration during list checks is harmless.
                    energy = remaining
                    self.journal.save()
                    self.rt.log(f"{candidate} 快速挑战不可用，已核对未消耗活跃能量，本次跳过并检查下一顺位。",
                                ui_text=f"跳过 {candidate}：快速挑战不可用。")
                    continue
                if not ok:
                    raise RuntimeError("追全幻境未完成，停止并保存当前进度，不重复提交挑战")
                if game.energy_exhausted:
                    if operation is not None:
                        operation["status"] = "not_executed_energy_exhausted"
                        self.journal.pending = None
                        self.journal.checkpoint()
                    return {"status": "energy_exhausted"}
                if operation is None:
                    raise RuntimeError("缺少幻境提交记录，停止重复挑战")
                remaining = game.read_energy()
                if remaining is None or remaining >= energy:
                    raise RuntimeError("未确认幻境活跃能量实际消耗，停止重复提交")
                self.journal.confirm_change(operation, energy_after=remaining)
                entry["status"] = "reward_confirmed"
                self.journal.save()
                return {"status": "reward_confirmed", "kind": kind, "source": candidate,
                        "energy_before": energy, "energy_after": remaining}
            if not child.ui.calendar():
                raise RuntimeError("检查幻境列表后未能返回日程页")
            self.rt.log("所有缺色副本均未找到或无法快速挑战，本次未提交挑战，继续处理已有胚子。",
                        ui_text="本轮缺色副本均不可用，继续处理已有胚子。")
            return {"status": "no_available_realm", "candidates": candidates}
        finally:
            self.rt.navigation_halted |= child.navigation_halted

    @staticmethod
    def check_list(runtime):
        if runtime.stopped or runtime.navigation_halted or not runtime.hit("MaaNikki_Bless_PageReady"):
            raise RuntimeError("幻境列表检查中断或页面状态改变，停止选择，未提交挑战")
