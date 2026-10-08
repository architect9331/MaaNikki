"""Run one selected daily executor, independently of daily score and scheduling."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import json
import os

import cv2

from action.daily_tasks import DailyRun
from .developer import focus_game
from .gameplay import Gameplay
from .planner import Card, XINGHAI, ZHAOXI
from .route_test import TestInputs, inputs_allowed
from .settings import ROOT
from navigation.models import NavigationError


RULES = {"zhaoxi": ZHAOXI, "xinghai": XINGHAI}


def test_rule(mode, key):
    rule = next((rule for rule in RULES.get(mode, ()) if rule.key == key), None)
    if rule is None:
        raise NavigationError("子任务测试项目无效，请重新添加任务。")
    # The dig workflow exists as a standalone task, though daily scheduling
    # doesn't use it for score credit. Test that same workflow explicitly.
    if mode == "zhaoxi" and key == "dig":
        rule = replace(rule, executor="dig")
    return rule


def unsupported_reason(rule):
    if rule.key == "energy":
        return "累计消耗活跃能量是进度条件，没有单独执行动作；请测试具体幻境。"
    return "此子任务尚未适配执行流程。" if not rule.executor else None


class SubtaskTest(DailyRun):
    def __init__(self, context, mode):
        super().__init__(context, mode)
        self.test_directory = None
        self.last_frame = None
        self.test_stage = "preflight"

    def capture(self):
        frame = super().capture()
        if frame is not None:
            self.last_frame = frame
        return frame

    def setting(self, mode):
        # Choosing a developer subtask explicitly requests its executor, even
        # when the normal daily automatic-completion switch is disabled.
        return {**super().setting(mode), "auto_complete": True}

    def real_meteor_card(self):
        if not self.open_page():
            raise NavigationError("未能打开星海拾光，无法找到流星的立即前往入口。")
        cards = self.scan()
        if cards is None:
            raise NavigationError("星海任务扫描失败，未执行召唤流星。")
        card = next((card for card in cards if card.rule and card.rule.key == "meteor"), None)
        if card is None:
            raise NavigationError("当天没有未完成的召唤流星任务，无法使用立即前往入口。")
        return card

    def run_test(self, key):
        inputs, previous = None, self.navigation_inputs
        result = {"mode": self.mode, "task": key, "success": False,
                  "execution_ok": False, "status": "failed"}
        try:
            if os.environ.get("PI_MAANIKKI_DEV_MODE") != "1":
                raise NavigationError("请开启客户端设置中的开发模式后再测试子任务。")
            rule = test_rule(self.mode, key)
            result["label"] = rule.label
            self.test_directory = ROOT / "logs/daily-tests" / (
                datetime.now().strftime("%Y%m%d-%H%M%S-%f")+f"-{self.mode}-{key}")
            self.test_directory.mkdir(parents=True)
            reason = unsupported_reason(rule)
            if reason:
                result.update(status="unsupported", reason=reason)
                self.log(f"{rule.label}：{reason}")
            else:
                if self.stopped:
                    raise NavigationError("子任务测试已停止。")
                if not inputs_allowed():
                    raise NavigationError("子任务测试需要当前连接使用前台鼠标和键盘（Seize）。")
                inputs = TestInputs(self.controller, lambda: self.stopped, bindings=self.game_keys)
                self.test_stage = "focus_game"
                focus_game(self, inputs)
                self.navigation_inputs = inputs
                result["window"] = inputs.focus_details()
                self.test_stage = "prepare"
                if rule.executor == "meteor":
                    card = self.real_meteor_card()
                    result["card_source"] = "daily_scan"
                else:
                    # Test the executor even if the task isn't offered today or
                    # has already finished. Preserve normal default quotas.
                    card = Card(-1, "开发模式单项试跑", rule)
                    result["card_source"] = "developer_test"
                result["card"] = {"slot": card.slot, "text": card.text, "remaining": card.remaining}
                result["settings"] = self.setting(self.mode)
                if not self.available(self.mode, card):
                    result.update(status="unavailable", reason="必要选项未开启、路线未适配，或当前交互位置不匹配。")
                    self.log(f"{rule.label}无法开始：{result['reason']}")
                else:
                    self.test_stage = "execute"
                    self.log(f"开始单项测试：{self.title} · {rule.label}。")
                    if rule.energy:
                        # Uses the options on THIS test task; never the saved
                        # realm task's Max count or weekly challenge loop.
                        ok = Gameplay(self).realm(rule.key, maximize=False, return_page="calendar")
                    else:
                        ok = self.execute(self.mode, card)
                    result.update(execution_ok=bool(ok), status="executed" if ok else "failed")
        except (NavigationError, OSError, KeyError, TypeError, ValueError, RuntimeError, cv2.error) as error:
            result.update(reason=str(error), error_type=type(error).__name__)
            self.log("子任务测试停止："+str(error))
        finally:
            failure_frame = self.last_frame
            result["stage"] = self.test_stage
            if inputs:
                try:
                    if self.navigation_inputs is inputs and inputs.foreground() and not self.stopped:
                        self.test_stage = "return"
                        result["returned_main"] = self.main()
                    elif self.navigation_inputs is inputs and not inputs.foreground():
                        self.navigation_halted = True
                except (OSError, ValueError, RuntimeError, cv2.error) as error:
                    result.update(returned_main=False, return_error=str(error))
                try:
                    inputs.release()
                except (OSError, ValueError, RuntimeError) as error:
                    result.update(release_error=str(error))
                    self.navigation_halted = True
            self.navigation_inputs = previous
            result["stopped"] = bool(self.stopped)
            result["success"] = bool(result["execution_ok"] and result.get("returned_main")
                                     and not self.stopped and not result.get("release_error"))
            if result["success"]:
                result["status"] = "executed"
            elif self.stopped:
                result["status"] = "stopped"
            elif result["status"] == "executed":
                result["status"] = "failed"
            self.save_test_result(result, failure_frame if not result["execution_ok"] else self.last_frame)
        return result["success"]

    def save_test_result(self, result, frame):
        if self.test_directory is None:
            return
        try:
            if not result["success"] and frame is not None:
                # Preserve the last recognition frame, before recovery can
                # replace it, and exclude the account corner.
                if frame.shape[:2] == (720, 1280):
                    frame = frame[:680]
                cv2.imencode(".png", frame)[1].tofile(str(self.test_directory / "failure.png"))
            report = {**result, "scans": self.scans, "events": self.events,
                      "time": datetime.now().isoformat(timespec="seconds")}
            content = json.dumps(report, ensure_ascii=False, indent=2)
            (self.test_directory / "result.json").write_text(content, encoding="utf-8")
            (self.test_directory.parent / "latest.json").write_text(content, encoding="utf-8")
            self.log("子任务测试"+("执行结束" if result["success"] else "未完成")+"，日志："+str(self.test_directory))
        except (OSError, ValueError, cv2.error):
            self.log("子任务测试日志保存失败。")
