"""Shared screenshot-driven page, search and reward operations."""
from __future__ import annotations

from functools import lru_cache
from datetime import datetime
import json
from pathlib import Path
import re
import time

import cv2
import numpy as np
from performance import measure
from vision import prepared_template


def fields(context, node):
    data = context.get_node_data(node) or {}
    recognition = data.get("recognition", {})
    value = recognition.get("param", {}) if isinstance(recognition, dict) else data
    custom = value.get("custom_recognition_param")
    if isinstance(custom, str):
        custom = json.loads(custom or "{}")
    return {**value, **custom} if isinstance(custom, dict) else value


@lru_cache(maxsize=4)
def catalog(resource):
    root = Path(resource)
    value = {"assets": {}, "materials": {}}
    available = False
    # Owned assets take precedence and can run without the optional local set.
    for path in (root / "image/game/catalog.json",
                 root / "image/nikki/alignment/catalog.json"):
        if not path.is_file():
            continue
        available = True
        replacement = json.loads(path.read_text(encoding="utf-8"))
        for name in ("assets", "materials"):
            entries = replacement.get(name, {})
            if not isinstance(entries, dict):
                raise ValueError("图像资源索引格式无效，请重新保存截图资源。")
            value[name].update(entries)
    if not available:
        raise ValueError("图像资源索引缺失，请先补全并保存所需截图资源。")
    return value


class GameUI:
    # Confirmed parent relationships allow Esc without returning to the world.
    PAGE_PARENTS = {
        "MaaNikki_Zhaoxi_DailyReady": "MaaNikki_CalendarReady",
        "MaaNikki_Xinghai_DailyReady": "MaaNikki_CalendarReady",
        "MaaNikki_RealmPageReady": "MaaNikki_CalendarReady",
        **{f"MaaNikki_{kind}_PageReady": "MaaNikki_RealmPageReady"
           for kind in ("Jihua", "Bless", "Monster", "Weekly")},
    }
    def __init__(self, runtime, resource):
        self.rt, self.resource = runtime, resource

    def roi(self, name):
        definition = fields(self.rt.context, "MaaNikki_Asset_"+name.lower())
        value = definition.get("roi", catalog(self.resource)["assets"][name]["roi"])
        if value is None:
            raise ValueError("此图像没有登记页面区域。")
        return value

    @staticmethod
    def similarity(a, b):
        if a is None or b is None or a.shape != b.shape:
            return 0.0
        return float(cv2.matchTemplate(a, b, cv2.TM_CCORR_NORMED)[0, 0])

    def stable(self, threshold=.9995, seconds=5, roi=None):
        end, previous = time.monotonic()+seconds, None
        unchanged_since, confirmations = time.monotonic(), 0
        while not self.rt.stopped and time.monotonic() < end:
            frame = self.rt.capture()
            if frame is None:
                return False
            if roi:
                x, y, w, h = roi
                frame = frame[y:y+h, x:x+w]
            if self.similarity(previous, frame) <= threshold:
                unchanged_since, confirmations = time.monotonic(), 0
            confirmations += 1
            if confirmations > 3 and time.monotonic()-unchanged_since > .25:
                return True
            previous = frame
            if not self.rt.pause(.1):
                return False
        return False

    def unhover(self):
        """Leave UI controls without clicking or turning the game camera."""
        self.rt.invalidate_frame()
        return (not self.rt.stopped
                and self.rt.controller.post_touch_move(0, 0).wait().succeeded)

    def text(self, roi, image=None, color=None):
        frame = self.rt.frame() if image is None else image
        if frame is None:
            return ""
        if color:
            x, y, w, h = roi
            mask = cv2.inRange(cv2.cvtColor(frame[y:y+h, x:x+w], cv2.COLOR_BGR2HSV),
                               np.array(color[0], dtype=np.uint8), np.array(color[1], dtype=np.uint8))
            frame = frame.copy()
            frame[y:y+h, x:x+w] = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)
        return self.rt.text(roi, image=frame)

    @measure("recognition.asset")
    def asset_boxes(self, name, image=None, roi=None, scale=1, threshold=.75, color=None, count=1, gray=None,
                    gray_limit=None,
                    method=None):
        data = catalog(self.resource)["assets"][name]
        definition = fields(self.rt.context, "MaaNikki_Asset_"+name.lower())
        if method is None:
            # UI glyphs and raw game/material artwork use different metrics.
            method = definition.get("method", cv2.TM_CCORR_NORMED if name.startswith(("Icon", "Button"))
                                    else cv2.TM_CCOEFF_NORMED)
        gray = gray or gray_limit or definition.get("gray_limit")
        template_path = definition.get("template", data["template"])
        if isinstance(template_path, list):
            template_path = template_path[0]
        if template_path.startswith("nikki/"):
            scale = 1  # These crops already use the 720p baseline.
        frame = self.rt.frame() if image is None else image
        if frame is None:
            return []
        image_root = (self.resource / "image").resolve()
        path = (image_root / template_path).resolve()
        if not path.is_relative_to(image_root):
            raise ValueError("任务图像资源路径无效。")
        prepared = prepared_template(path, scale=scale, gray=gray, color=color)
        template, mask = prepared.image, prepared.mask
        if not template.size:
            return []
        roi = roi or definition.get("roi") or data["roi"] or [0, 0, 1280, 720]
        x, y, w, h = roi
        target = frame[y:y+h, x:x+w]
        if color:
            target = cv2.inRange(cv2.cvtColor(target, cv2.COLOR_BGR2HSV), np.array(color[0]), np.array(color[1]))
        elif gray:
            target = cv2.threshold(cv2.cvtColor(target, cv2.COLOR_BGR2GRAY), gray[0], gray[1], cv2.THRESH_BINARY)[1]
        if target.shape[0] < template.shape[0] or target.shape[1] < template.shape[1]:
            return []
        with measure("recognition.match."+name):
            rates = cv2.matchTemplate(target, template, method, mask=mask)
        rates = np.nan_to_num(rates, copy=False, nan=-1, posinf=-1, neginf=-1)
        boxes = []
        th, tw = template.shape[:2]
        for _ in range(count):
            _, score, _, (px, py) = cv2.minMaxLoc(rates)
            if score < threshold:
                break
            boxes.append([x+px, y+py, tw, th])
            if len(boxes) == count:
                break  # No further match needs the overlap-suppression arrays.
            # Suppress only boxes whose overlapping area exceeds 50%, matching
            # the resource-image search rather than a rectangular half-width.
            top, left = max(0, py-th), max(0, px-tw)
            bottom, right = min(rates.shape[0], py+th+1), min(rates.shape[1], px+tw+1)
            yy, xx = np.indices((bottom-top, right-left))
            overlap = np.maximum(0, tw-np.abs(xx+left-px))*np.maximum(0, th-np.abs(yy+top-py))
            view = rates[top:bottom, left:right]
            view[overlap > tw*th*.5] = -1
        return boxes

    def asset(self, name, **kwargs):
        boxes = self.asset_boxes(name, **kwargs)
        return boxes[0] if boxes else None

    def walking(self):
        return bool(self.asset("IconMovementWalk", threshold=.9, color=([0, 0, 210], [180, 50, 255])))

    def click_asset(self, name, attempts=3, **kwargs):
        for index in range(attempts):
            box = self.asset(name, **kwargs)
            if box:
                return self.click_box(box) and self.rt.pause(.3)
            if index+1 < attempts and not self.rt.pause(1):
                break
        return False

    def click_box(self, box):
        """All UI clicks share Runtime's centering, hover and release timing."""
        return self.rt.action("Click", target=box)

    def wait_text(self, text, roi, attempts=3):
        return self.wait_page(text=text, roi=roi, seconds=attempts)

    def wait_pickup(self, seconds=2):
        """Match the white pickup lettering independently of scene background."""
        definition = fields(self.rt.context, "MaaNikki_Navigation_Pickup")
        name = definition.get("template", "game/navigation/pickup.png")
        if isinstance(name, list):
            name = name[0]
        root = (self.resource / "image").resolve()
        path = (root / name).resolve()
        if not path.is_relative_to(root):
            raise ValueError("拾取图像资源路径无效。")
        template = prepared_template(path, grayscale=True, gray=(210, 255)).image
        if not template.size:
            raise ValueError("拾取图像没有可识别的亮色文字。")
        roi = definition.get("roi", [265, 97, 897, 534])
        limit = definition.get("threshold", .75)
        limit = limit[0] if isinstance(limit, list) else limit
        deadline = time.monotonic()+seconds
        frame = None
        self.last_pickup_frame = None
        self.last_pickup_diagnostics = {"roi": list(roi), "best_score": 0.0, "method": "none"}
        while not self.rt.stopped and time.monotonic() < deadline:
            frame = self.rt.capture()
            if frame is None:
                return False
            self.last_pickup_frame = frame
            x, y, w, h = roi
            crop = cv2.cvtColor(frame[y:y+h, x:x+w], cv2.COLOR_BGR2GRAY)
            crop = cv2.threshold(crop, 210, 255, cv2.THRESH_BINARY)[1]
            if crop.shape[0] >= template.shape[0] and crop.shape[1] >= template.shape[1]:
                rates = cv2.matchTemplate(crop, template, cv2.TM_CCORR_NORMED)
                score = float(np.nan_to_num(rates, copy=False, nan=-1, posinf=-1, neginf=-1).max())
                self.last_pickup_diagnostics["best_score"] = max(score, self.last_pickup_diagnostics["best_score"])
                if score >= limit:
                    self.last_pickup_diagnostics["method"] = "white_template"
                    return True
            if not self.rt.pause(.2):
                return False
        # Only the interaction verb counts, not a floating object/name label.
        found = bool(frame is not None and not self.rt.stopped and self.rt.text(roi, "拾取", image=frame))
        if found:
            self.last_pickup_diagnostics["method"] = "pickup_text"
        return found

    def scroll(self, roi, amount):
        x, y, w, h = roi
        # post_scroll(dx, dy) uses a default target of (0, 0) and moves
        # there even after post_touch_move. Supply the target atomically.
        return (self.rt.action("Scroll", target=[x+w-8, y+h-8], dx=0, dy=amount)
                and self.rt.pause(.2))

    def menu(self):
        if self.page_matches("MaaNikki_MenuReady") and self.wait_page("MaaNikki_MenuReady"):
            return True
        if not self.main():
            return False
        return self.enter_page(lambda: self.rt.key("menu", 0), "MaaNikki_MenuReady",
                               source="MaaNikki_MainDetected", recover=self.main)

    def find_menu_entry(self, *, node):
        """All current Esc entrances are on page one; never scroll this menu."""
        from .runtime import parameters
        ready = parameters(self.rt.context, node).get("ready")
        if ready and self.page_matches(ready) and self.wait_page(ready):
            return True
        return self.menu() and self.rt.click_template(node)

    def find(self, *, roi, text=None, node=None, asset=None, scale=1, threshold=.75,
             color=None, exact=False, scroll=True):
        """Search current view, reset to top if absent, then scan to bottom."""
        if node:
            definition = fields(self.rt.context, node)
            roi = definition.get("roi", roi)
            text = definition.get("expected", text)
        reset = False
        next_image = None
        for _ in range(80 if scroll else 1):
            if self.rt.stopped:
                return False
            image = self.rt.capture() if next_image is None else next_image
            next_image = None
            if image is None:
                return False
            raw_image = image
            if asset:
                box = self.asset(asset, image=image, roi=roi, scale=scale, threshold=threshold, color=color)
            else:
                if node:
                    result = self.rt.recognize(node, {"roi": roi}, image=image)
                else:
                    pattern = "^"+re.escape(text)+"$" if exact else re.escape(text)
                    if color:
                        x, y, w, h = roi
                        filtered = cv2.inRange(cv2.cvtColor(image[y:y+h, x:x+w], cv2.COLOR_BGR2HSV),
                                               np.array(color[0]), np.array(color[1]))
                        image = image.copy()
                        image[y:y+h, x:x+w] = cv2.cvtColor(filtered, cv2.COLOR_GRAY2BGR)
                    result = self.rt.recognize("MaaNikki_Daily_OCR", {"roi": roi, "expected": pattern}, image=image)
                box = list(result.box) if result and result.hit else None
            if box:
                if node:
                    from .runtime import parameters
                    if parameters(self.rt.context, node).get("ready"):
                        return self.rt.click_template(node)
                return self.click_box(box)
            if not scroll:
                return False
            x, y, w, h = roi
            before = raw_image[y:y+h, x:x+w].copy()
            if not reset:
                for _ in range(12):
                    if not self.scroll(roi, 1800):
                        return False
                    frame = self.rt.capture()
                    if frame is None:
                        return False
                    current = frame[y:y+h, x:x+w]
                    if self.similarity(before, current) > .99:
                        break
                    before = current.copy()
                next_image = frame
                reset = True
                continue
            if not self.scroll(roi, -600):
                return False
            frame = self.rt.capture()
            if frame is None or self.similarity(before, frame[y:y+h, x:x+w]) > .99:
                return False
            next_image = frame
        return False

    def skip_reward(self, *, node="MaaNikki_RewardLayer"):
        deadline = time.monotonic()+10
        while not self.rt.stopped and time.monotonic() < deadline:
            if self.rt.hit(node):
                return self.rt.pause(1) and self.rt.key("skip", .5)
            if not self.rt.pause(.3):
                break
        return False

    def feature(self, *, asset=None, text=None, roi=None, **options):
        """Adapt existing local features to the shared page checks."""
        if (asset is None) == (text is None):
            raise ValueError("页面特征必须指定图像或文字。")
        if text is not None and roi is None:
            raise ValueError("文字页面特征必须指定识别区域。")
        def check():
            return (self.asset(asset, roi=roi, **options) if asset else self.rt.text(roi, text))
        check.__name__ = asset or text
        return check

    def page_matches(self, page):
        with self.rt.observe():
            return bool(page() if callable(page) else self.rt.hit(page))

    @measure("wait.page")
    def wait_page(self, node=None, *, asset=None, text=None, threshold=.99, seconds=5,
                  roi=None, stable=False):
        """Confirm a page twice; image stability is an optional local requirement."""
        page = node
        if asset or text:
            page = self.feature(asset=asset, text=text, roi=roi, threshold=threshold,
                                method=cv2.TM_CCORR_NORMED)
        if page is None:
            raise ValueError("缺少目标页面特征。")
        if stable and roi is None:
            roi = self.roi(asset) if asset else fields(self.rt.context, node).get("roi")
        if stable and roi is None:
            raise ValueError("页面稳定检查必须指定局部区域。")
        deadline = time.monotonic()+seconds
        confirmations = 0
        while not self.rt.stopped and time.monotonic() < deadline:
            # Two confirmations must come from separate captures, even inside
            # a caller's observation scope.
            with self.rt.observe(fresh=True):
                matched = self.page_matches(page)
            if matched:
                if confirmations == 0 and not self.unhover():
                    return False
                confirmations += 1
                if confirmations >= 2:
                    if stable:
                        remaining = deadline-time.monotonic()
                        if remaining <= 0 or not self.stable(.98, seconds=remaining, roi=roi):
                            return False
                        return not self.rt.stopped and self.page_matches(page)
                    return True
            else:
                confirmations = 0
            if not self.rt.pause(.25):
                return False
        return False

    def enter_page(self, click, destination, *, source=None, recover=None, attempts=3, seconds=5):
        """Recheck the source or recover it before every bounded entrance retry."""
        events, failed = [], False
        # Without a known source, an input can only be submitted once.
        attempts = attempts if source else 1
        try:
            for attempt in range(1, attempts+1):
                if self.rt.stopped:
                    return False
                with self.rt.observe():
                    destination_visible = self.page_matches(destination)
                    source_visible = bool(source and not destination_visible and self.page_matches(source))
                if destination_visible and self.wait_page(destination, seconds=seconds):
                    events.append({"type": "arrived", "attempt": attempt, "already_open": True})
                    return True
                # If a visible destination failed confirmation, reclassify the
                # source on a new frame before considering recovery.
                if destination_visible and source:
                    source_visible = self.page_matches(source)
                if source and not source_visible:
                    # Give a late transition time to finish before any recovery input.
                    if self.wait_page(destination, seconds=seconds):
                        events.append({"type": "arrived", "attempt": attempt, "late": True})
                        return True
                    failed = True
                    events.append({"type": "source_unavailable", "attempt": attempt,
                                   **self.entry_failure_frame(attempt)})
                    if not recover:
                        self.rt.log("未确认目标页面或入口所在页面，本项停止。")
                        return False
                    if self.rt.stopped:
                        return False
                    self.rt.log(f"未确认当前页面，正在恢复入口页面（{attempt}/{attempts}）。")
                    recovered = bool(recover())
                    events.append({"type": "recovery", "attempt": attempt, "success": recovered})
                    if self.rt.stopped:
                        return False
                    if self.page_matches(destination) and self.wait_page(destination, seconds=seconds):
                        events.append({"type": "arrived", "attempt": attempt, "after_recovery": True})
                        return True
                    if not recovered or not self.page_matches(source):
                        continue
                if self.rt.stopped:
                    return False
                submitted = bool(click())
                events.append({"type": "input", "attempt": attempt, "success": submitted})
                if self.wait_page(destination, seconds=seconds):
                    events.append({"type": "arrived", "attempt": attempt})
                    return True
                failed = True
                events.append({"type": "not_arrived", "attempt": attempt,
                               "source_visible": bool(source and self.page_matches(source)),
                               **self.entry_failure_frame(attempt)})
                if attempt < attempts:
                    self.rt.log(f"未确认进入目标页面，将重新检查并重试（{attempt+1}/{attempts}）。")
            self.rt.log("多次尝试后仍未进入目标页面，本项停止。")
            return False
        finally:
            if failed:
                self.report_entry(source, destination, events)

    def entry_failure_frame(self, attempt):
        try:
            from .settings import ROOT
            frame = self.rt.capture()
            if frame is None:
                return {"image_status": "capture_unavailable"}
            directory = ROOT / "logs/navigation"
            directory.mkdir(parents=True, exist_ok=True)
            name = datetime.now().strftime("%Y%m%d-%H%M%S-%f")+f"-page-attempt{attempt}.png"
            # Keep interface features while excluding the account identifier.
            ok, png = cv2.imencode(".png", frame[:680])
            if ok:
                (directory / name).write_bytes(png.tobytes())
                return {"image": name}
        except (OSError, cv2.error):
            pass
        return {"image_status": "save_failed"}

    def report_entry(self, source, destination, events):
        def name(page):
            return getattr(page, "__name__", str(page)) if page else None
        try:
            from .settings import ROOT
            directory = ROOT / "logs/navigation"
            directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now()
            record = {"time": stamp.isoformat(timespec="seconds"), "operation": "page_entry",
                      "source": name(source), "destination": name(destination),
                      "stopped": bool(self.rt.stopped),
                      "status": "stopped" if self.rt.stopped else
                                "recovered" if any(e["type"] == "arrived" for e in events) else "failed",
                      "events": events}
            path = directory / (stamp.strftime("%Y%m%d-%H%M%S-%f")+"-page.json")
            path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            self.rt.log("页面进入记录未能保存。")

    def recover_page(self, page):
        """Rebuild known entrance paths through ordinary main recovery."""
        if self.page_matches(page) and self.wait_page(page):
            return True
        if page == "MaaNikki_MainDetected":
            return self.main()
        if page == "MaaNikki_MenuReady":
            return self.menu()
        if page == "MaaNikki_CalendarReady":
            return self.calendar()
        if page == "MaaNikki_RealmPageReady":
            return self.realm_overview()
        for kind in ("Jihua", "Bless", "Monster", "Weekly"):
            if page == f"MaaNikki_{kind}_PageReady":
                return self.realm(kind.lower())
        if page == "MaaNikki_Xinghai_LookbookReady":
            return self.find_menu_entry(node="MaaNikki_Xinghai_OpenLookbook")
        if page == "MaaNikki_Crown_PageReady":
            return self.find_menu_entry(node="MaaNikki_Crown_Open")
        if page == "MaaNikki_Monthly_PageReady":
            return self.monthly_page()
        if page == "MaaNikki_Monthly_Start":
            return self.monthly_start()
        return False

    def main(self):
        if self.rt.hit("MaaNikki_MainDetected"):
            if self.wait_page("MaaNikki_MainDetected"):
                return True
        deadline, escapes = time.monotonic()+15, 0
        while not self.rt.stopped and time.monotonic() < deadline and escapes < 8:
            # Reclassify before every input; animated backgrounds need not stop.
            with self.rt.observe():
                if self.rt.hit("MaaNikki_MainDetected"):
                    page = "main"
                elif self.asset("IconDungeonFeature", threshold=.9, gray=(230, 255), method=cv2.TM_CCORR_NORMED):
                    page = "dungeon"
                elif self.rt.hit("MaaNikki_Energy_SkipReward"):
                    page = "reward"
                elif self.asset("IconUILoading", threshold=.99, method=cv2.TM_CCORR_NORMED):
                    page = "loading"
                else:
                    page = "unknown"
            if page == "main":
                if self.wait_page("MaaNikki_MainDetected", seconds=2):
                    return True
                continue
            elif page == "dungeon":
                if not (self.rt.key("exit", 0)
                        and self.wait_page(asset="ButtonDungeonQuitOK", threshold=.75)
                        and self.click_asset("ButtonDungeonQuitOK", attempts=1)):
                    return False
            elif page == "reward":
                if not self.rt.key("skip", .5):
                    return False
            elif page == "loading":
                if not self.rt.pause(.5):
                    return False
            else:
                if self.rt.hit("MaaNikki_MainDetected"):
                    continue
                escapes += 1
                if not self.rt.key("menu", .35):
                    return False
            if self.wait_page("MaaNikki_MainDetected", seconds=min(1.5, max(0, deadline-time.monotonic()))):
                return True
        return False

    def back_to(self, destination):
        """Follow verified parents; no page cache or blind Esc sequence."""
        with self.rt.observe():
            already_open = self.rt.hit(destination)
            frame = None if already_open else self.rt.frame()
        if already_open:
            return self.wait_page(destination)
        if frame is None:
            return False
        for node in self.PAGE_PARENTS:
            route, current = [], node
            while current in self.PAGE_PARENTS:
                current = self.PAGE_PARENTS[current]
                route.append(current)
                if current == destination:
                    break
            if not route or route[-1] != destination:
                continue
            result = self.rt.recognize(node, image=frame)
            if not result or not result.hit:
                continue
            for parent in route:
                if not self.enter_page(lambda: self.rt.key("menu", 0), parent,
                                       source=node, attempts=1):
                    return False
                node = parent
            return True
        return False

    def calendar(self):
        if self.back_to("MaaNikki_CalendarReady"):
            return True
        if self.main() and self.enter_page(lambda: self.rt.key("calendar", 0),
                "MaaNikki_CalendarReady", source="MaaNikki_MainDetected", recover=self.main):
            return True
        self.rt.log("未能进入奇想日历，请检查当前游戏页面。")
        return False

    def ability_page(self):
        wardrobe = self.feature(asset="IconWardrobeFeature", threshold=.99,
                                method=cv2.TM_CCORR_NORMED)
        ability = self.feature(asset="IconAbilityFeature", threshold=.99,
                               method=cv2.TM_CCORR_NORMED)
        if self.page_matches(ability) and self.wait_page(ability):
            return True
        def open_wardrobe():
            return self.main() and self.enter_page(lambda: self.rt.key("dress", 0), wardrobe,
                source="MaaNikki_MainDetected", recover=self.main)
        if open_wardrobe() and self.enter_page(lambda: self.rt.key("ability_config", 0), ability,
                                               source=wardrobe, recover=open_wardrobe):
            return True
        self.rt.log("未能进入能力配置，请检查换装页面；若仍是旧版界面，请先接取“变强吧大喵”任务。")
        return False

    def open_map(self, event=lambda value: None):
        """Retry only from a confirmed main page; never toggle an unknown page."""
        node = "MaaNikki_Navigation_MapFeature"
        if self.rt.hit(node) and self.wait_page(node):
            return True
        if not self.main():
            self.map_failure(0, "main_unavailable", event)
            return False
        attempt = 0
        def open_once():
            nonlocal attempt
            attempt += 1
            event({"type": "map_open_attempt", "attempt": attempt})
            return self.rt.key("map", 0)
        if self.enter_page(open_once, node, source="MaaNikki_MainDetected", recover=self.main):
            event({"type": "map_opened", "attempt": attempt})
            return True
        self.map_failure(attempt, "not_detected_after_retries", event)
        return False

    def map_failure(self, attempt, reason, event, stage="open"):
        frame = self.rt.capture()
        record = {"type": f"map_{stage}_failure", "attempt": attempt, "reason": reason}
        if frame is not None:
            main = self.rt.recognize("MaaNikki_MainDetected", image=frame)
            record["main_detected"] = bool(main and main.hit)
        try:
            if frame is not None and frame.shape[:2] == (720, 1280):
                from .settings import ROOT
                folder = ROOT / "logs/navigation"
                folder.mkdir(parents=True, exist_ok=True)
                filename = datetime.now().strftime("%Y%m%d-%H%M%S-%f")+f"-map-{stage}.png"
                # Keep page features; exclude the account identifier at bottom.
                ok, png = cv2.imencode(".png", frame[:680])
                if ok:
                    (folder / filename).write_bytes(png.tobytes())
                    record["file"] = filename
                    record["roi"] = [0, 0, 1280, 680]
        except (OSError, ValueError, cv2.error):
            self.rt.log("开地图失败时的截图未能保存。")
        event(record)

    def realm(self, kind):
        prefix = {"jihua": "Jihua", "bless": "Bless", "monster": "Monster", "weekly": "Weekly"}[kind]
        destination = f"MaaNikki_{prefix}_PageReady"
        if self.rt.hit(destination) and self.wait_page(destination):
            return True
        if (self.realm_overview() and self.rt.click_template(f"MaaNikki_{prefix}_Entrance")):
            return True
        self.rt.log("未能进入所选幻境页面，本次没有注入活跃能量。")
        return False

    def realm_overview(self):
        if self.page_matches("MaaNikki_RealmPageReady") and self.wait_page("MaaNikki_RealmPageReady"):
            return True
        return self.calendar() and self.rt.click_template("MaaNikki_Jihua_Huanjing")

    def monthly_page(self):
        destination = "MaaNikki_Monthly_PageReady"
        if self.page_matches(destination) and self.wait_page(destination):
            return True
        if not self.monthly_start():
            return self.wait_page(destination)
        return self.rt.click_template("MaaNikki_Monthly_Start")

    def monthly_start(self):
        """Recover the introduction/start page without submitting Start again."""
        destination = "MaaNikki_Monthly_PageReady"
        if self.page_matches("MaaNikki_Monthly_Start") and self.wait_page("MaaNikki_Monthly_Start"):
            return True
        def travel_opened():
            return any(self.rt.hit(node) for node in (
                destination, "MaaNikki_Monthly_IntroSkip", "MaaNikki_Monthly_Start"))
        if not (self.main() and self.enter_page(lambda: self.rt.key("monthly", 0), travel_opened,
                source="MaaNikki_MainDetected", recover=self.main, seconds=10)):
            return False
        if self.page_matches(destination):
            return False
        intro = (self.rt.context.get_node_data("MaaNikki_Monthly_IntroSkip") or {}).get("enabled", True)
        if self.rt.hit("MaaNikki_Monthly_IntroSkip"):
            if not intro or not self.rt.hold("skip", 3):
                return False
        return self.wait_page("MaaNikki_Monthly_Start")
