"""Maa actions for glow-up material preparation and crafting material collection."""
from __future__ import annotations

import traceback
import cv2
from maa.custom_action import CustomAction

from registration import agent_action
from daily.runtime import Executors, parameters
from collection.material_pursuit import run_crafting_pursuit, run_glow_prep


@agent_action("nikki.glow_material_prep")
class GlowMaterialPrepAction(CustomAction):
    def run(self, context, argv):
        rt = Executors(context)
        ok = False
        try:
            ok = run_glow_prep(rt)
            if not ok and not rt.stopped:
                rt.diagnostic("glow_material_prep", "焕新材料筹备未完成")
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, cv2.error) as error:
            if not rt.stopped:
                rt.diagnostic("glow_material_prep", str(error), details={
                    "traceback": traceback.format_exc()[-3000:]})
            rt.log(f"焕新材料筹备停止：{error}。")
        finally:
            if not rt.stopped and not rt.finish(argv.node_name)[1]:
                ok = False
        return ok


@agent_action("nikki.crafting_pursuit")
class CraftingPursuitAction(CustomAction):
    def run(self, context, argv):
        rt = Executors(context)
        ok = False
        options = {}
        try:
            options = parameters(context, argv.node_name)
            ok = run_crafting_pursuit(rt)
            if not ok and not rt.stopped:
                rt.diagnostic("crafting_pursuit", "制衣材料全收集未完成")
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, cv2.error) as error:
            if not rt.stopped:
                rt.diagnostic("crafting_pursuit", str(error), details={
                    "traceback": traceback.format_exc()[-3000:]})
            rt.log(f"制衣材料全收集停止：{error}。")
        finally:
            if not rt.stopped:
                returned = rt.ui.calendar() if options.get("return_page") == "calendar" else rt.finish(argv.node_name)[1]
                if not returned:
                    ok = False
        return ok
