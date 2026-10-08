"""Maa callbacks for exploration assistance and summit challenges."""
from __future__ import annotations

import cv2
from maa.custom_action import CustomAction
from registration import agent_action
from daily.exploration import Exploration
from daily.crown import Crown


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
            rt.log(f"开荒辅助已停止：{error}。")
            return False


@agent_action("nikki.crown")
class CrownAction(CustomAction):
    def run(self, context, argv):
        rt = Crown(context)
        try:
            return rt.run_crown()
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, cv2.error) as error:
            rt.log(f"巅峰赛未完成：{error}。")
            return False
        finally:
            if not rt.stopped and not rt.finish("MaaNikki_Crown")[1]:
                rt.log("巅峰赛处理后未能恢复任务衔接页面。")
                return False
