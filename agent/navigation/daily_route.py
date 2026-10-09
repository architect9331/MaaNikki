"""Task-aware route actions. Movement never grants daily-task credit by itself."""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import time

import cv2
import numpy as np

from .models import NavigationError


def material_amount(text, material, interaction=False):
    """Count exact obtained-item lines, never essence, insight or seed byproducts."""
    amount = 0
    for line in text.splitlines():
        line = re.sub(r"\s+", "", line)
        if any(word in line for word in ("精粹", "心得", "种子")):
            continue
        match = re.fullmatch(r"(?:获得[:：]?)?" + re.escape(material) + r"(?:[xX×+](\d+(?:\.\d+)?))?", line)
        if match:
            value = 1 if interaction or not match.group(1) else float(match.group(1))
            if value == int(value) and 1 <= value <= 20:
                amount += int(value)
    return amount


def load_macro(resource: Path, name):
    try:
        path = resource / "routes" / "macros" / f"{name}.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("schema_version") not in (1, 2) or not value.get("steps"):
            raise ValueError("Invalid macro format")
        return value
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise NavigationError("宏资源无法读取。") from error


def preflight(resource, route, executor):
    actions = {p.action for p in route.points if p.action}
    required = {"plant": "collect", "insect": "insect", "fight": "macro", "bubble": "macro", "photo": "photo",
                "place": "place", "music": "music", "bottle": "bottle", "delivery": "delivery", "chat": "chat"}.get(executor)
    if executor == "home" and "fishing_star" not in actions:
        raise NavigationError("家园日常路线缺少钓星动作。")
    if required and required not in actions:
        raise NavigationError("路线没有包含此任务所需的动作。")
    if executor in {"plant", "insect"} and not route.document.get("material"):
        raise NavigationError("采集路线未指定可确认的素材名称。")
    extra_materials = route.document.get("count_materials", [])
    if (not isinstance(extra_materials, list)
            or any(not isinstance(name, str) or not name.strip() for name in extra_materials)):
        raise NavigationError("采集路线的计数素材列表无效。")
    for point in route.points:
        if point.action == "teleport" and not point.params.get("checkpoint"):
            raise NavigationError("路线内的传送动作未指定传送点。")
        if point.action == "minigame":
            raise NavigationError("小游戏入场与结束页面尚未适配。")
        if point.action == "macro":
            load_macro(resource, point.params["id"])


class RouteActions:
    def __init__(self, runtime, resource, route, mode, card, inputs, event):
        self.rt, self.resource, self.route = runtime, resource, route
        self.mode, self.card, self.inputs, self.event = mode, card, inputs, event
        self.count, self.successes, self.attempts = 0, 0, 0
        self.executor = card.rule.executor
        self.quota = card.remaining if card.remaining is not None else {"plant": 5, "insect": 3, "bottle": 1, "crystal": 10}.get(self.executor)
        if self.quota is not None and (type(self.quota) is not int or not 1 <= self.quota <= 100):
            raise NavigationError("任务剩余数量无法可靠读取。")
        self.material = route.document.get("material", "") or ("星光结晶" if self.executor == "crystal" else "")
        self.count_materials = tuple(dict.fromkeys(
            [self.material] + route.document.get("count_materials", []))) if self.material else ()
        self.last_notice = ""
        self.motion = None
        self.navigator = None
        self.action_failed = False
        self.fishing = None
        if self.executor == "home":
            from .fishing import StarFishing
            self.fishing = StarFishing(self)
        from .insect import Insect
        self.insect = Insect(self)

    def notification(self):
        result = self.rt.recognize("MaaNikki_Daily_OCR", {"roi": self.rt.ui.roi("AreaMaterialGetText")})
        if not result or not result.hit:
            return ""
        rows = getattr(result, "filtered_results", []) or [result.best_result]
        return "\n".join(getattr(row, "text", "") for row in rows)

    def obtained(self, baseline, seconds=3, interaction=False, single=False):
        deadline = time.monotonic()+seconds
        while not self.rt.stopped and time.monotonic() < deadline:
            self.inputs.check()
            if self.motion:
                self.motion.tick()
            text = self.notification()
            if self.motion:
                self.motion.tick()
            compact = re.sub(r"\s+", "", text)
            amounts = {name: amount for name in self.count_materials
                       if (amount := material_amount(text, name, interaction))}
            if (amounts
                    and (interaction or compact != re.sub(r"\s+", "", baseline) and compact != self.last_notice)):
                self.last_notice = compact
                for name, amount in amounts.items():
                    self.count += amount
                    self.event({"type": "obtained", "material": name, "amount": amount, "total": self.count, "text": text})
                    self.rt.log(f"已识别获得{name}，本次累计 {self.count}。")
                return True
            if not any(name in compact for name in self.count_materials):
                self.last_notice = ""
            if single:
                break
            if self.motion:
                self.motion.wait(.2)
            else:
                self.inputs.wait(.2)
        return False

    def ability(self, node):
        from daily.ui import fields
        definition = fields(self.rt.context, node)
        template = definition.get("template", "")
        if isinstance(template, list):
            template = template[0] if template else ""
        if not template:
            return False
        root = (self.resource / "image").resolve()
        path = (root / template).resolve()
        if not path.is_relative_to(root):
            return False
        image = cv2.imread(str(path))
        frame = self.rt.capture()
        if image is None or frame is None:
            return False
        area = definition.get("roi", self.rt.ui.roi("AreaAbilityButton"))
        x, y, w, h = area
        def white(value):
            return cv2.inRange(cv2.cvtColor(value, cv2.COLOR_BGR2HSV),
                               np.array([0, 0, 230]), np.array([180, 60, 255]))
        scale = .73*2/3 if template.startswith("game/") else 1
        image = cv2.resize(white(image), None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)
        patch = white(frame[y:y+h, x:x+w])
        if patch.shape[0] < image.shape[0] or patch.shape[1] < image.shape[1]:
            return False
        x, y, w, h = cv2.boundingRect(image)
        if not w or not h:
            return False
        image = image[y:y+h, x:x+w]
        return float(cv2.matchTemplate(patch, image, cv2.TM_CCORR_NORMED).max()) > .8

    def select_insect(self):
        return self.insect.select()

    def prepare_point(self, point, pose):
        """Resolve route teleports before attempting to walk to their coordinates."""
        if point.action != "teleport":
            return False
        self.motion.stop()
        self.motion.jump(False, self.rt.ui.walking())
        checkpoint = point.params["checkpoint"]
        moved = math.hypot(pose.x-point.x, pose.y-point.y) >= 15
        if moved:
            self.teleporter.transport(checkpoint)
        else:
            self.event({"type": "teleport_skipped_nearby", "checkpoint": checkpoint})
        # Upstream recalibrates at every TELEPORT point, even if transport is skipped.
        self.navigator.turn_ratio = None
        return True

    def collect(self):
        initial = self.count
        # One location may expose several pickup prompts. Continue until the
        # prompt disappears, rather than leaving after the first interaction.
        for index in range(40):
            if self.rt.stopped:
                return False
            if not self.rt.ui.wait_pickup(2 if index == 0 else 1):
                break
            baseline = self.notification()
            if not self.rt.key("interact", .5):
                return False
            self.obtained(baseline, seconds=.5, interaction=True)
            if self.quota is not None and self.count >= self.quota:
                break
        return self.count > initial

    def macro(self, name):
        value = load_macro(self.resource, name)
        if not self.rt.hit("MaaNikki_MainDetected"):
            raise NavigationError("宏动作要求处于可移动主界面。")
        self.event({"type": "macro", "id": name})
        semantic = {"jump", "falling"} if name == "starsea_fall" else {"attack"}
        # Resolve the complete input set before the first down event.
        if len({self.rt.game_keys.get(key) for key in semantic}) != len(semantic):
            raise NavigationError("宏动作的跳跃与急坠按键不能相同。")
        try:
            for step in value["steps"]:
                self.inputs.check()
                kind = step["type"]
                if kind == "wait":
                    self.inputs.wait(step["seconds"])
                else:
                    key = step["key"] if kind.startswith("game_") else ({32: "jump", 81: "falling"}[step["key"]] if kind.startswith("key_") else "attack")
                    method = self.inputs.key_down if kind.endswith("_down") else self.inputs.key_up
                    method(key)
        finally:
            self.inputs.release()
        return True

    def __call__(self, point):
        self.inputs.check()
        kind = point.action
        self.attempts += 1
        ok = False
        if kind == "wait":
            self.inputs.wait(point.params["seconds"])
            ok = True
        elif kind == "teleport":
            # Navigation's preparation hook already handled this point.
            ok = True
        elif kind == "fishing_star":
            try:
                ok = self.fishing.run()
            except NavigationError as error:
                # Input interruption still stops the route; ordinary task
                # failures retain upstream's continue-to-next-point behavior.
                self.inputs.check()
                self.event({"type": "fishing_failed", "reason": str(error)})
                self.rt.log(f"此钓星点未完成：{error}")
                ok = False
            if not ok:
                # Record a failed action and continue the remaining points.
                self.action_failed = True
        elif kind in {"collect", "insect"}:
            if kind == "collect":
                ok = self.collect()
            else:
                if not self.ability("MaaNikki_Navigation_InsectAbility"):
                    raise NavigationError("捕虫能力未装备或图标识别失败，已停止。")
                count = point.params.get("count", 1)
                if self.quota is not None:
                    count = min(count, max(1, self.quota-self.count))
                ok = self.insect.catch(count)
            if not ok:
                self.event({"type": "resource_unconfirmed", "action": kind, "x": point.x, "y": point.y})
        elif kind == "macro":
            ok = self.macro(point.params["id"])
        elif kind == "photo":
            ok = self.rt.photo(self.mode)
        elif kind == "place":
            ok = self.rt.place()
        elif kind == "music":
            ok = self.rt.interaction("music")
        elif kind == "chat":
            ok = self.rt.chat(self.mode)
        elif kind == "bottle":
            found = self.rt.ui.wait_pickup(2)
            interacted = found and self.rt.key("interact", .5)
            ok = (interacted and self.rt.ui.wait_page("MaaNikki_Daily_BottleClose")
                  and self.rt.click_template("MaaNikki_Daily_BottleClose"))
            if ok:
                self.count += 1
            else:
                # These are candidate spawn locations, not mandatory pickups.
                # The common main-page check below still guards further movement.
                evidence = None
                frame = getattr(self.rt.ui, "last_pickup_frame", None)
                diagnostics = getattr(self.rt.ui, "last_pickup_diagnostics", {})
                if frame is not None:
                    from datetime import datetime
                    from daily.settings import ROOT
                    try:
                        folder = ROOT / "logs/navigation"
                        folder.mkdir(parents=True, exist_ok=True)
                        file = folder / (datetime.now().strftime("%Y%m%d-%H%M%S-%f")+"-pickup.png")
                        x, y, w, h = diagnostics["roi"]
                        if cv2.imwrite(str(file), frame[y:y+h, x:x+w]):
                            evidence = file.name
                    except (OSError, cv2.error):
                        pass
                self.event({"type": "bottle_unconfirmed", "x": point.x, "y": point.y,
                            "pickup_detected": bool(found), "interacted": bool(interacted),
                            "recognition": diagnostics, "frame": evidence})
                self.rt.log("此点未确认拾取漂流瓶，继续沿路线查找。")
        elif kind == "delivery":
            ok = self.rt.interaction("delivery")
        elif kind == "animal":
            ok = self.rt.animal(self.card, times=point.params.get("times", 1))
        else:
            raise NavigationError("路线动作尚未适配。")
        self.event({"type": "action_result", "action": kind, "success": bool(ok), "count": self.count})
        if ok and kind != "wait":
            self.successes += 1
        if kind not in {"wait", "collect", "insect", "bottle", "fishing_star"} and not ok:
            raise NavigationError("点位动作未确认成功，已停止本项。")
        self.inputs.check()
        if not self.rt.main():
            raise NavigationError("动作后未能回到可移动主界面。")
        if self.executor != "crystal" and self.quota is not None and self.count >= self.quota:
            return "done"
        return "continue"

    def complete(self):
        if self.executor == "home":
            return not self.action_failed
        if self.executor in {"crystal", "animal"}:
            # Passive collection/animal entry is completed by traversal itself.
            # This is only an execution estimate until the daily-score read.
            return True
        if self.executor in {"plant", "insect", "bottle"}:
            return self.quota is not None and self.count >= self.quota
        # Macro completion is only an execution estimate, never confirmed kills/points.
        return self.successes > 0

    def waypoint(self, point):
        if self.executor != "crystal":
            return None
        self.inputs.check()
        # Passing a transit point must not add a polling delay while W is held.
        self.obtained(self.last_notice, seconds=.25, single=True)
        # Crystal runs traverse the chosen island route in full. Toasts may
        # persist or change order; they are diagnostics, not an early-stop quota.
