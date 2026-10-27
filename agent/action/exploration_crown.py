"""Maa callbacks for exploration assistance and summit challenges."""
from __future__ import annotations

import cv2
from maa.custom_action import CustomAction
from registration import agent_action
from daily.exploration import Exploration
from daily.crown import Crown
from daily.settings import ROOT
from account_state import AccountState, period_key


@agent_action("nikki.feature_settings")
class FeatureSettings(CustomAction):
    def run(self, context, argv):
        return True


@agent_action("nikki.exploration")
class ExplorationAction(CustomAction):
    def run(self, context, argv):
        rt = Exploration(context)
        try:
            return rt.run_assist()
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, cv2.error) as error:
            rt.log(f"辅助功能已停止：{error}。")
            return False


@agent_action("nikki.crown")
class CrownAction(CustomAction):
    def run(self, context, argv):
        rt = Crown(context)
        try:
            account = AccountState(ROOT)
            period = period_key("crown")
            if account.complete("crown"):
                rt.log("巅峰赛：本期已完成，跳过。")
                return True
            completed = rt.run_crown()
            if not completed and not rt.stopped:
                rt.diagnostic("crown", "巅峰赛流程未完成")
            if completed and not rt.stopped:
                account.confirm("crown", "challenge", "completed", period=period)
            return completed
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, cv2.error) as error:
            rt.log(f"巅峰赛未完成：{error}。")
            if not rt.stopped:
                rt.diagnostic("crown", str(error))
            return False
        finally:
            if not rt.stopped and not rt.finish("MaaNikki_Crown")[1]:
                rt.log("巅峰赛处理后未能恢复任务衔接页面。")
                return False
