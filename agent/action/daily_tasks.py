from __future__ import annotations

from datetime import datetime
import json
import re
import time

import cv2

from registration import agent_action
from maa.custom_action import CustomAction

from daily.planner import Card, bell_notice, classify, select_energy, select_free
from daily.runtime import Executors, parameters
from daily.settings import ROOT, realm_settings
from daily import run_state


class DailyRun(Executors):
    def __init__(self, context, mode, task_id=None):
        super().__init__(context)
        self.mode = mode
        self.title = "朝夕心愿" if mode == "zhaoxi" else "星海拾光"
        self.prefix = "Zhaoxi" if mode == "zhaoxi" else "Xinghai"
        self.events = []
        self.attempted = set()
        self.successful = set()
        self.scans = []
        self.task_id = task_id
        self.deferred = False
        self.phase = "daily"
        self.rewards_ok = None
        self.return_main_ok = None
        self.handoff_page = None
        self.handoff_ok = None
        self.score_reads = []

    def open_page(self):
        ready = f"MaaNikki_{self.prefix}_DailyReady"
        if self.hit(ready) and self.ui.wait_page(ready):
            return True
        entrance = f"MaaNikki_{self.prefix}_Entrance"
        return self.ui.calendar() and self.click_template(entrance)

    def score(self):
        from daily.score import score_image
        from daily.ui import fields
        node = f"MaaNikki_{self.prefix}_Score"
        x, y, w, h = fields(self.context, node)["roi"]
        previous = None
        crop = None
        for attempt in range(3):
            image = self.capture()
            if image is None:
                return None
            crop = image[y:y+h, x:x+w]
            value = None
            for method, frame in (("glyphs", score_image(crop)), ("original", crop)):
                if frame is None:
                    continue
                result = self.recognize(node, {"roi": [0, 0, frame.shape[1], frame.shape[0]],
                                              "only_rec": True, "threshold": .8}, image=frame)
                text = getattr(getattr(result, "best_result", None), "text", "").strip()
                self.score_reads.append({"attempt": attempt+1, "method": method, "text": text,
                                         "hit": bool(result and result.hit),
                                         "raw": [getattr(item, "text", "") for item in
                                                 (getattr(result, "all_results", None) or [])]})
                if result and result.hit and re.fullmatch(r"0|[1-5]00", text):
                    value = int(text)
                    break
            if value is not None and previous == value:
                return value
            previous = value
            if attempt < 2 and not self.pause(.4):
                break
        if crop is not None:
            try:
                directory = ROOT / "logs/daily"
                directory.mkdir(parents=True, exist_ok=True)
                name = f"{self.mode}-score-{datetime.now():%Y%m%d-%H%M%S-%f}.png"
                if cv2.imwrite(str(directory / name), crop):
                    self.score_reads.append({"failure_image": name, "roi": [x, y, w, h]})
            except (OSError, cv2.error):
                pass
        return None

    def read_card(self, slot, center, roi):
        deadline = time.monotonic()+25
        previous = None
        record = {"slot": slot, "center": center, "raw": "", "stable": False,
                  "finished": False, "classification": None, "notice_frames": 0}
        while not self.stopped and time.monotonic() < deadline:
            image = self.capture()
            if image is None:
                break
            text = self.text(roi, image=image)
            record["raw"] = text
            if bell_notice(text):
                if not record["notice_frames"]:
                    self.log("摇铃提示遮挡了任务文字，等待提示消失后继续识别。")
                record["notice_frames"] += 1
                previous = None
            elif text.strip():
                card = classify(self.mode, text, slot)
                finished = self.recognize(f"MaaNikki_{self.prefix}_DailyFinished", image=image)
                marker = bool(finished and finished.hit)
                # Require two clean readings of identity, progress and marker.
                # Unknown tasks must also have repeatable text before skipping.
                signature = (card.rule.key if card.rule else re.sub(r"\s+", "", text),
                             card.remaining, marker)
                if signature == previous:
                    complete = card.remaining == 0 or marker and card.remaining is None
                    record.update(stable=True, finished=complete, finished_marker=marker,
                                  classification=card.rule.key if card.rule else None,
                                  remaining=card.remaining)
                    self.scans.append(record)
                    return card, complete
                previous = signature
            else:
                previous = None
            if not self.pause(.5):
                break
        record["reason"] = "stopped" if self.stopped else "unreadable_or_obscured"
        self.scans.append(record)
        self.log(f"第 {slot+1} 项任务文字一直被遮挡或未能稳定读取，本次停止，请稍后重试。")
        return None

    def scan(self):
        fields = parameters(self.context, f"MaaNikki_{self.prefix}_DailyLayout")
        cards = []
        centers = fields["card_centers"]
        for slot, center in enumerate(centers):
            if not self.action("Click", target=center) or not self.pause(0.3):
                return None
            result = self.read_card(slot, center, fields["detail_roi"])
            if result is None:
                return None
            card, finished = result
            if finished:
                continue
            cards.append(card)
            if not card.rule:
                self.log(f"第 {slot+1} 项暂无法自动处理：{card.text}。")
        return cards

    def claim(self):
        name = f"MaaNikki_Claim{self.prefix}Rewards"
        if not (self.context.get_node_data(name) or {}).get("enabled", True):
            self.rewards_ok = True
            return True
        self.rewards_ok = self.run(name, {name: {"next": []}})
        return self.rewards_ok

    def check_group(self):
        if not self.open_page():
            return None
        score = self.score()
        if score is not None:
            self.log(f"{self.title}本批执行后实际积分：{score}/500。")
        return score

    def free_batch(self, available, score):
        predicted = score
        while not self.stopped and predicted < 500:
            remaining = [c for c in available if c.slot not in self.attempted]
            chosen = select_free(remaining, predicted)
            if not chosen:
                break
            card = chosen[0]
            self.attempted.add(card.slot)
            rule = card.rule
            self.log(f"正在完成{self.title}：{rule.label}（{rule.score} 分，不消耗活跃能量）。")
            linked = rule.key == "animal_one" and "animal_three" in self.successful
            ok = linked or self.execute(self.mode, card)
            self.events.append({"slot": card.slot, "task": rule.key, "success": ok, "linked": linked})
            if ok:
                predicted += rule.score
                self.successful.add(rule.key)
            else:
                self.log(f"{rule.label}未完成，尝试其他可执行任务。")
            if not self.main():
                break
        return predicted

    def save_report(self, score):
        directory = ROOT / "logs" / "daily"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now()
        operation_failed = score is None or self.rewards_ok is False or self.handoff_ok is False
        report = json.dumps({
            "time": datetime.now().isoformat(timespec="seconds"), "mode": self.mode,
            "actual_score": score, "score_complete": score == 500,
            "complete": score == 500 and not self.stopped and not operation_failed,
            "rewards_ok": self.rewards_ok, "returned_main": self.return_main_ok,
            "handoff_page": self.handoff_page, "handoff_ok": self.handoff_ok,
            "phase": self.phase, "pending_energy_review": self.deferred and not self.stopped,
            "stopped": bool(self.stopped),
            "status": "stopped" if self.stopped else "failed" if operation_failed else "complete" if score == 500 else
                      "awaiting_energy_review" if self.deferred else "incomplete",
            "events": self.events, "scans": self.scans, "score_reads": self.score_reads,
        }, ensure_ascii=False, indent=2)
        (directory / f"{self.mode}-latest.json").write_text(report, encoding="utf-8")
        (directory / f"{self.mode}-{stamp.strftime('%Y%m%d-%H%M%S-%f')}.json").write_text(report, encoding="utf-8")

    def run_daily(self):
        score = None
        try:
            if self.mode == "zhaoxi":
                run_state.clear(self)
            if not self.open_page():
                self.log(f"未能打开{self.title}页面。")
                return False
            score = self.score()
            if score is None:
                self.log(f"无法可靠读取{self.title}积分，停止自动执行，不消耗活跃能量。")
                return False
            if score == 500:
                self.log(f"{self.title}已达 500 分，领取可领取奖励。")
                return self.claim()
            if not self.setting(self.mode).get("auto_complete", True):
                self.log(f"{self.title}当前 {score}/500，仅领取已经解锁的奖励。")
                return self.claim()
            # Scan before any world action, including crystal collection. The
            # scan excludes completed cards; only remaining tasks may execute.
            cards = self.scan()
            if cards is None:
                return False
            available = []
            for card in cards:
                if self.available(self.mode, card):
                    available.append(card)
                elif card.rule:
                    reason = "缺少已校准的路线或当前交互位置不匹配" if card.rule.route else "暂未适配或已关闭"
                    self.log(f"{card.rule.label}：{reason}，本轮跳过。")
            free = [c for c in available if not c.rule.energy]
            expected_free = sum(c.rule.score for c in free)
            self.log(f"当前 {score}/500；可执行的不耗体力任务预计贡献 {expected_free} 分。"
                     + ("优先仅使用这些任务凑分。" if score+expected_free >= 500 else
                        "先做这些任务，不足部分再考虑当天幻境。" if self.mode == "zhaoxi" else
                        "先做可执行任务；其余任务不会使用幻境体力补分。"))
            while score < 500 and any(c.slot not in self.attempted for c in free) and not self.stopped:
                if not self.main():
                    return False
                self.free_batch(free, score)
                score = self.check_group()
                if score is None:
                    self.log("本批积分识别失败，不继续消耗体力。")
                    return False
                # Every batch consumes at least one unattempted card. Recheck
                # only between batches, but exhaust free alternatives before
                # any energy supplement when predicted points were optimistic.
            if score < 500 and self.mode == "zhaoxi":
                energy = [c for c in available if c.rule.energy and c.rule.key != "energy"]
                if energy:
                    preferred, overrides = realm_settings()
                    predicted = score
                    pending = list(energy)
                    while predicted < 500 and pending and not self.stopped:
                        card = select_energy(pending, predicted, preferred)
                        pending.remove(card)
                        self.log(f"不耗体力任务不足，使用{card.rule.label}补分；保留其余体力。")
                        ok = self.energy(card, preferred, overrides)
                        self.events.append({"slot": card.slot, "task": card.rule.key, "success": ok})
                        if ok:
                            predicted += card.rule.score
                            self.successful.add(card.rule.key)
                    score = self.check_group()
                    if score is None:
                        return False
            elif not free:
                # Still on the task page if there were no actions at all.
                if not self.open_page():
                    return False
            if not self.open_page():
                return False
            claimed = self.claim()
            if score == 500:
                self.log(f"{self.title}已完成 500 分，奖励处理结束。" if claimed else
                         f"{self.title}已达 500 分，但奖励未能确认领取。")
                return claimed
            energy_card = next((card for card in cards if card.rule and card.rule.key == "energy"), None)
            if claimed and self.mode == "zhaoxi" and energy_card and self.task_id is not None:
                self.deferred = run_state.defer(self, self.task_id, score, energy_card)
                if self.deferred:
                    self.log(f"朝夕心愿当前 {score}/500，本阶段已结束；将在本轮幻境挑战后仅复核积分和领奖。")
                    return claimed
            self.log(f"{self.title}当前实际 {score}/500；剩余任务需要手动完成，本轮未达标。")
            return False
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, cv2.error) as error:
            self.log(f"{self.title}停止：{error}。")
            return False
        finally:
            self.handoff_page, self.handoff_ok = self.finish(f"MaaNikki_{self.prefix}") if not self.stopped else (None, False)
            self.return_main_ok = self.handoff_ok and self.handoff_page == "main"
            if not self.handoff_ok:
                run_state.clear(self)
                self.deferred = False
            try:
                self.save_report(score)
            except OSError:
                pass
            if not self.handoff_ok:
                return False

    def review_after_energy(self, pending):
        """Final settlement only; never runs the daily task planner again."""
        self.phase = "after_energy"
        self.events, self.scans = pending.events, pending.scans
        score = None
        try:
            self.log("本轮幻境已处理，复核待补的朝夕心愿积分和奖励。")
            if not self.open_page():
                return False
            score = self.score()
            if score is None:
                self.log("无法可靠读取朝夕心愿积分，本次未继续执行任务。")
                return False
            self.events.append({"type": "after_energy_review", "source_task_id": pending.source_task_id,
                                "before_score": pending.score, "actual_score": score,
                                "energy_remaining_before": pending.energy_remaining})
            claimed = self.claim()
            if score == 500:
                self.log("朝夕心愿已完成 500 分，奖励处理结束。" if claimed else
                         "朝夕心愿已达 500 分，但奖励未能确认领取。")
                return claimed
            self.log(f"清体力后朝夕心愿实际 {score}/500；不再追加挑战，剩余任务需要手动完成。")
            return False
        finally:
            self.handoff_page, self.handoff_ok = self.finish("MaaNikki_RealmChallenge") if not self.stopped else (None, False)
            self.return_main_ok = self.handoff_ok and self.handoff_page == "main"
            try:
                self.save_report(score)
            except OSError:
                pass
            if not self.handoff_ok:
                return False


@agent_action("nikki.daily_tasks")
class DailyTasksAction(CustomAction):
    def run(self, context, argv) -> bool:
        payload = argv.custom_action_param
        options = json.loads(payload or "{}") if isinstance(payload, str) else payload
        mode = options.get("mode")
        if mode not in ("zhaoxi", "xinghai"):
            return False
        run = DailyRun(context, mode, argv.task_detail.task_id)
        result = run.run_daily()
        return bool(result and not run.stopped)


@agent_action("nikki.daily_set_amount")
class DailySetAmountAction(CustomAction):
    def run(self, context, argv) -> bool:
        payload = argv.custom_action_param
        options = json.loads(payload or "{}") if isinstance(payload, str) else payload
        kind = options.get("kind")
        minimum = options.get("minimum_energy", 0)
        if kind not in ("jihua", "bless", "monster") or type(minimum) is not int or not 0 <= minimum <= 1000:
            return False
        return Executors(context).set_amount(kind, minimum)


@agent_action("nikki.daily_fail")
class DailyFailAction(CustomAction):
    def run(self, context, argv) -> bool:
        return False


@agent_action("nikki.daily_config")
class DailyConfigAction(CustomAction):
    """Configuration-only nodes retain their typed custom parameters."""
    def run(self, context, argv) -> bool:
        return True
