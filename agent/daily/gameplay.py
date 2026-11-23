"""MVP page workflows shared by standalone and daily-task execution."""
from __future__ import annotations

import re
import json
import cv2
import numpy as np
from task_control import task_time as time

from .ui import catalog, expected_text, fields
from .settings import ROOT
from adventure_report import lookup_monster_drop, normalize_item_name, try_record_adventure_entry


class Gameplay:
    # Minimum energy for one challenge, not the selected batch's total cost.
    MIN_ENERGY = {"jihua": 10, "bless": 40, "monster": 40, "weekly": 60}
    PARADISE_CLAIM_LIMIT = 5
    REALM_LEVEL_ROI = [145, 90, 410, 600]
    # Region accordion headers ("心愿原野" / "伊赞之土") have no left icon and sit at x=90..255.
    REALM_HEADER_ROI = [65, 85, 440, 605]
    # The level list ends before the recognition ROI's right/bottom margins.
    REALM_SCROLL_ROI = [165, 105, 335, 545]

    def __init__(self, runtime):
        self.rt, self.ui = runtime, runtime.ui
        self.energy_exhausted = False
        self.jihua_exhausted = False
        self.jihua_material_capped = False
        self.quick_unavailable = False
        self._last_jihua_quote = None

    def read_energy(self):
        """Read only on the confirmed calendar; an OCR miss is not zero."""
        if not self.rt.hit("MaaNikki_CalendarReady"):
            return None
        # Returning from a realm challenge plays a ~2s Mira Level EXP gain
        # banner ("经验 +...") across the top-center Vital Energy counter.
        for attempt in range(16):
            if attempt > 0 and not self.rt.hit("MaaNikki_CalendarReady"):
                return None
            result = self.rt.recognize("MaaNikki_Energy_Remaining")
            raw = getattr(result.best_result, "text", "") if result and result.hit else ""
            match = re.fullmatch(r"\s*(\d+)\s*[/／]\s*(\d+)\s*", raw)
            if match:
                current, total = map(int, match.groups())
                if 0 <= current <= 1000 and 0 < total <= 1000:
                    return current
            if not self.rt.pause(.25):
                break
        return None

    def weekly_state(self, image=None):
        """Read the selected boss's quota and the realm's own energy counter."""
        image = self.rt.capture() if image is None else image
        if image is None:
            return None, None
        ready = self.rt.recognize("MaaNikki_Weekly_PageReady", image=image)
        if not ready or not ready.hit:
            return None, None
        # The calendar x/2 is a total; each boss has its own remaining quota.
        # Both counters stay visible on the boss page and through its dialog.
        quota = self.rt.text([890, 580, 340, 65], image=image)
        match = re.search(r"(\d+)\s*[/／]\s*(\d+)", quota)
        remaining = None
        if match:
            current, total = map(int, match.groups())
            if 0 <= current <= total and total > 0:
                remaining = current
        raw_energy = self.rt.text([1100, 15, 150, 50], image=image)
        match = re.search(r"(\d+)\s*[/／]\s*(\d+)", raw_energy)
        energy = None
        if match:
            current, total = map(int, match.groups())
            if 0 <= current <= 9999 and 0 < total <= 9999:
                energy = current
        self.rt.evidence.observe("weekly_state", remaining=remaining, energy=energy)
        return remaining, energy

    def weekly_quick_challenge_locked(self, image=None):
        """Return True when the selected weekly boss has never been cleared (quick challenge locked)."""
        image = self.rt.capture() if image is None else image
        if image is None:
            return False
        ready = self.rt.recognize("MaaNikki_Weekly_PageReady", image=image)
        if not ready or not ready.hit:
            return False
        record = re.sub(r"\s+", "", self.rt.text([740, 310, 360, 60], image=image) or "")
        return bool(re.search(r"最佳[时時][间間][记記][录錄][：:]?暂无", record))

    def exhausted(self, allow_exhausted, energy=None):
        if self.rt.stopped:
            return False
        self.energy_exhausted = True
        remaining = f"剩余活跃能量 {energy} 点，" if energy is not None else ""
        self.rt.log(remaining + ("不足一次所选挑战，本次正常结束。" if allow_exhausted else
                                "不足一次所选挑战，本项未执行，不计入心愿完成。"))
        return allow_exhausted

    def confirm_unavailable(self, kind, allow_exhausted, cancel=None):
        """A missing/disabled button alone never proves energy exhaustion."""
        if self.rt.stopped:
            return False
        frame = self.rt.capture()
        shortage = bool(frame is not None and self.rt.text(
            [380, 200, 520, 340], r"(?:活跃)?能量(?:不足|不够)|体力(?:不足|不够)", image=frame))
        remaining, energy = self.weekly_state(frame) if kind == "weekly" else (None, None)
        quota_used = remaining == 0
        shortage = shortage or (energy is not None and energy < self.MIN_ENERGY[kind])
        if not shortage and not quota_used:
            self.rt.diagnostic("realm_confirmation", "未能确认挑战提交", image=frame,
                                details={"kind": kind, "cancel": cancel})
        if cancel and self.ui.dismiss_realm_confirm(cancel) is False:
            self.rt.log("未能关闭挑战确认窗口，本项停止。")
            return False
        if quota_used:
            self.rt.log("当前每周幻境的奖励次数已用完，跳过。")
            return True
        if shortage:
            return self.exhausted(allow_exhausted, energy)
        # Re-read after returning safely, including when preflight OCR missed.
        if kind != "jihua" and self.ui.calendar():
            energy = self.read_energy()
            if energy is not None and energy < self.MIN_ENERGY[kind]:
                return self.exhausted(allow_exhausted, energy)
        self.rt.log("未能确认挑战，且未确认活跃能量不足，请检查游戏画面。")
        return False

    def read_dig_queue_count(self):
        frame = self.rt.frame()
        shape = getattr(frame, "shape", None)
        if not isinstance(shape, tuple) or len(shape) < 2 or shape[0] < 150 or shape[1] < 1275:
            return None
        crop = frame[112:144, 1155:1270]
        if getattr(crop, "size", 0) == 0:
            return None
        zoomed = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
        raw = self.rt.text([0, 0, zoomed.shape[1], zoomed.shape[0]], image=zoomed)
        if not isinstance(raw, str):
            return None
        return re.search(r"(\d+)\s*[/／]", raw)

    def _dig_choice_modal_open(self):
        for node in ("MaaNikki_Dig_Again", "MaaNikki_Dig_GatherConfirm"):
            res = self.rt.recognize(node)
            if getattr(res, "hit", None) is not True:
                return False
        return True

    def dig(self):
        def steps():
            if not self.ui.find_menu_entry(node="MaaNikki_Dig_Open"):
                self.rt.log("未找到美鸭梨挖掘入口。")
                return False
            if not self.rt.hit("MaaNikki_Dig_Gather"):
                raw = self.ui.text(self.ui.roi("AreaDigingNumText"))
                count = re.search(r"(\d+)\s*[/／]", raw)
                if count is None:
                    count = self.read_dig_queue_count()
                active = bool(count and int(count.group(1)) > 0)
                if not active and not self.rt.stopped:
                    self.rt.diagnostic(
                        "dig",
                        "empty_dig_queue" if count else "dig_status_unreadable",
                        details={"raw": raw},
                    )
                self.rt.log("仍在挖掘，本次没有可收获的奖励。" if active else
                            "没有正在挖掘的目标，请在游戏中设置。" if count else "无法读取当前挖掘状态。")
                return active
            repeat = (self.rt.context.get_node_data("MaaNikki_Dig_Again") or {}).get("enabled", True)
            # These buttons are mutually exclusive choices in the same dialog.
            choice = "MaaNikki_Dig_Again" if repeat else "MaaNikki_Dig_GatherConfirm"
            label = "再次挖掘" if repeat else "确认收获"
            if (
                not self.ui.enter_page(
                    lambda: self.rt.click_template("MaaNikki_Dig_Gather", attempts=1),
                    choice,
                    source="MaaNikki_Dig_Gather",
                    attempts=3,
                    seconds=4,
                )
                or not self.ui.wait_page(choice, stable=True, seconds=2)
            ):
                if not self.rt.stopped:
                    self.rt.diagnostic("dig", "choice_button_unconfirmed", details={"choice": choice})
                self.rt.log(f"未确认挖掘收取界面的“{label}”按钮，本项停止。")
                return False
            dig_targets = self._read_dig_target_names()
            self.observe_dig_targets(found=dig_targets)
            self.rt.log(f"挖掘收取方式：{label}。", detail=True)
            # Submit choice after modal reveal stabilizes. If '再次挖掘' was
            # ignored while the modal remained open, retry once on that modal.
            if not self.rt.click_template(choice, attempts=1):
                if not self.rt.stopped:
                    self.rt.diagnostic("dig", "choice_click_failed", details={"choice": choice})
                return False
            if not repeat and not self.ui.skip_reward(node="MaaNikki_Dig_SkipReward"):
                if not self.rt.stopped:
                    self.rt.diagnostic("dig", "reward_popup_unconfirmed")
                self.rt.log("未确认挖掘奖励弹窗，本项未完成。")
                return False
            returned_dig = self.ui.wait_page("MaaNikki_Dig_PageReady")
            if not returned_dig and repeat and not self.rt.stopped and self._dig_choice_modal_open():
                if self.rt.pause(0.3) and self.rt.click_template(choice, attempts=1):
                    returned_dig = self.ui.wait_page("MaaNikki_Dig_PageReady")
            if not returned_dig:
                if not self.rt.stopped:
                    self.rt.diagnostic("dig", "dig_page_return_unconfirmed", details={"repeat": repeat})
                self.rt.log("挖掘操作后未确认返回挖掘页面，本项未完成。")
                return False
            self.rt.log("已收取奖励并再次挖掘。" if repeat else "已收取挖掘奖励。")
            try_record_adventure_entry(
                ROOT,
                self.rt,
                category="dig",
                task="dig",
                title="美鸭梨挖掘收获",
                detail=("目标：" + "、".join(dig_targets)) if dig_targets else "已收取本轮挖掘奖励",
                runs=1,
            )
            return True

        returned = False
        try:
            ok = steps()
        finally:
            if not self.rt.stopped:
                returned = self.rt.main()
                if not returned:
                    self.rt.log("挖掘处理后未能返回主界面，本项未正常结束。")
        return bool(ok and not self.rt.stopped and returned)

    def _stats_account(self):
        """Resolve an already-bound account UID without leaving modal dialogs."""
        try:
            from account_state import AccountState
            from task_control import active_session
            session = active_session()
            uid = (
                getattr(self.rt, "_account_uid", None)
                or (getattr(session, "account_uid", None) if session else None)
                or self.rt.read_account_uid(self.rt.frame())
            )
            if not uid:
                index_path = ROOT / "config/accounts/index.json"
                if index_path.is_file():
                    index = json.loads(index_path.read_text(encoding="utf-8-sig"))
                    active = index.get("active_game_account") if isinstance(index, dict) else None
                    if isinstance(active, str) and active.startswith("CN:"):
                        candidate = active[3:]
                        if re.fullmatch(r"[0-9]{1,32}", candidate):
                            uid = candidate
            if isinstance(uid, str) and re.fullmatch(r"[0-9]{1,32}", uid):
                return AccountState(ROOT, uid)
        except Exception:
            pass
        return None

    def _read_dig_target_names(self):
        try:
            raw_text = self.rt.text([180, 110, 920, 460]) or ""
            materials = catalog(self.ui.resource).get("materials", {})
            dig_names = sorted(
                (name for name, meta in materials.items() if isinstance(meta, dict) and meta.get("dig")),
                key=len,
                reverse=True,
            )
            found = []
            remaining = raw_text
            for name in dig_names:
                if name in remaining:
                    found.append(name)
                    remaining = remaining.replace(name, " ")
            return found
        except Exception:
            return []

    def observe_dig_targets(self, found=None):
        try:
            from collection import statistics
            if not statistics.is_task_stats_enabled(ROOT):
                return
            account = self._stats_account()
            if account is None:
                return
            if found is None:
                found = self._read_dig_target_names()
            statistics.sync_task_stats(account, ROOT, extra_dig_targets=found)
            statistics.upload(account, ROOT)
        except Exception:
            pass

    def lookbook(self):
        if not self.ui.find_menu_entry(node="MaaNikki_Xinghai_OpenLookbook"):
            return False
        if not self.rt.click_template("MaaNikki_Xinghai_SelectPhoto"):
            self.rt.log("未能打开图册照片详情，本项停止。")
            return False
        # The dress/scene keeps animating. Wait only for the like control.
        node = "MaaNikki_Xinghai_Like"
        roi = fields(self.rt.context, node)["roi"]
        if not self.rt.wait_hit(node, 10) or not self.ui.stable(.98, roi=roi):
            self.rt.log("照片详情中的点赞按钮未能稳定识别，本项停止。")
            return False
        if not self.rt.click_template(node):
            return False
        # Like toggles: submit once, then leave without clicking again.
        self.rt.log("已点击照片点赞按钮。", detail=True)
        return self.rt.main()

    def realm(self, kind, maximize=True, entered=False, weekly_node=None, allow_exhausted=False,
              return_page="main", fixed_count=None, *, allow_unavailable=False, before_submit=None,
              record_bling_stats=True, record_report=True, report_prefix=None, report_task=None):
        if kind == "bless":
            self.rt.account_uid()
        if fixed_count is not None and (type(fixed_count) is not int or not 1 <= fixed_count <= 999):
            raise ValueError("固定挑战数量必须是 1～999 的整数")
        self.energy_exhausted = False
        self.jihua_exhausted = False
        self.jihua_material_capped = False
        self.quick_unavailable = False
        self._last_jihua_quote = None
        self.level_missing = False
        self.weekly_quota_used = False
        self.weekly_quick_locked = False
        prefix = {"jihua": "Jihua", "bless": "Bless", "monster": "Monster", "weekly": "Weekly"}[kind]
        cancel = "MaaNikki_Weekly_Cancel" if kind == "weekly" else f"MaaNikki_{prefix}_Cancel"
        energy_before = None

        def steps():
            nonlocal energy_before
            if not entered and not self.rt.hit(f"MaaNikki_{prefix}_PageReady"):
                if not self.ui.calendar():
                    return False
                energy = self.read_energy()
                energy_before = energy
                if energy is not None and energy < self.MIN_ENERGY[kind]:
                    return self.exhausted(allow_exhausted, energy)
            if not entered and not self.ui.realm(kind):
                return False
            if kind == "jihua":
                ok = self.jihua(maximize, allow_exhausted, fixed_count=fixed_count,
                                record_bling_stats=record_bling_stats)
                if ok and record_report and not self.energy_exhausted and not self.jihua_exhausted:
                    self._record_realm_report(
                        kind, None, maximize=maximize, fixed_count=fixed_count,
                        energy_before=energy_before, report_prefix=report_prefix, report_task=report_task,
                    )
                return ok
            node = weekly_node if kind == "weekly" else f"MaaNikki_{prefix}_Level"
            if not self.select_realm_level(kind, node):
                return False
            if kind == "weekly":
                # Selecting a different boss changes this panel asynchronously.
                # Its quota must belong to the requested boss, not the one
                # whose reward was just claimed.
                if not self.ui.wait_page(text=fields(self.rt.context, node)["expected"],
                                         roi=[600, 90, 600, 150]):
                    self.realm_level_diagnostic(kind, node)
                    return False
                remaining, energy = self.weekly_state()
                if energy is not None:
                    energy_before = energy
                if remaining == 0:
                    self.weekly_quota_used = True
                    self.rt.log("当前每周幻境的奖励次数已用完，跳过。")
                    return True
                if energy is not None and energy < self.MIN_ENERGY[kind]:
                    return self.exhausted(allow_exhausted, energy)
                if self.weekly_quick_challenge_locked():
                    self.weekly_quick_locked = True
                    return False
            if kind == "bless" and not self.rt.click_template("MaaNikki_Bless_Difficulty"):
                return False
            quick = "MaaNikki_Weekly_Quick_Qiggeda" if kind == "weekly" else f"MaaNikki_{prefix}_Quick"
            confirm = "MaaNikki_Weekly_Confirm_Qiggeda" if kind == "weekly" else f"MaaNikki_{prefix}_Confirm"
            def recover_level():
                return (self.ui.realm(kind) and self.select_realm_level(kind, node)
                        and (kind != "bless" or self.rt.click_template("MaaNikki_Bless_Difficulty")))
            can_skip = allow_unavailable and kind == "bless"
            if not self.ui.enter_page(lambda: self.rt.click_template(quick, attempts=1, wait_seconds=0),
                    confirm, source=quick, recover=None if can_skip else recover_level,
                    attempts=1 if can_skip else 3):
                # Only a failure before opening the submission dialog can
                # authorize another realm. An unknown page remains an error.
                if (can_skip and not self.rt.stopped
                        and self.ui.wait_page("MaaNikki_Bless_PageReady", seconds=2)
                        and not self.rt.hit(confirm) and not self.rt.hit(cancel)):
                    self.quick_unavailable = True
                    return False
                return self.confirm_unavailable(kind, allow_exhausted,
                                                cancel if self.rt.hit(cancel) else None)
            if fixed_count is not None and not self.rt.set_amount(kind, 0, desired_count=fixed_count):
                return False
            if fixed_count is None and maximize and kind != "weekly" and not self.rt.click_template(f"MaaNikki_{prefix}_Max", wait_seconds=.2):
                return False
            # Daily supplementation uses the game's default minimum quantity.
            # Only the standalone energy-clearing task selects Max.
            if before_submit is not None:
                before_submit()
            if kind == "bless":
                self.invalidate_blessing_inventory("即将提交祝福闪光挑战，奖励数量待复核")
            if not self.rt.click_template(confirm):
                return self.confirm_unavailable(kind, allow_exhausted, cancel)
            result = self.ui.skip_reward()
            if (not result and not self.rt.stopped and self.rt.hit(confirm)
                    and not self.rt.text([380, 200, 520, 340], r"(?:活跃)?能量(?:不足|不够)|体力(?:不足|不够)")
                    and (kind != "weekly" or self.weekly_state()[0] != 0)):
                if self.rt.click_template(confirm):
                    result = self.ui.skip_reward()
            if not result and not self.rt.stopped and (self.rt.hit(cancel) or self.rt.hit(confirm)):
                return self.confirm_unavailable(kind, allow_exhausted,
                                                cancel if self.rt.hit(cancel) else None)
            if result and kind == "bless":
                self.invalidate_blessing_inventory("已确认祝福闪光挑战奖励，新增库存数量待复核")
            if result and record_report and not self.energy_exhausted and not self.weekly_quota_used:
                self._record_realm_report(
                    kind, node, maximize=maximize, fixed_count=fixed_count,
                    energy_before=energy_before, report_prefix=report_prefix, report_task=report_task,
                )
            self.rt.log("挑战奖励已领取。" if result else "挑战奖励领取结果待确认，请检查游戏画面。",
                        detail=bool(result and before_submit is not None))
            return result

        returned = True
        try:
            ok = steps()
        finally:
            if not self.rt.stopped and kind != "jihua":
                self.ui.dismiss_realm_confirm(cancel)
            if not entered and not self.rt.stopped:
                returned = self.ui.calendar() if return_page == "calendar" else self.rt.main()
                if not returned:
                    self.quick_unavailable = False
                    self.rt.log("未能返回后续步骤需要的页面，本项停止。")
        return bool(ok and returned)

    def invalidate_blessing_inventory(self, reason):
        # Applies to ordinary farming and daily supplementation too. Resource
        # acquisition never silently leaves an older full snapshot reusable.
        from collection.storage import InventoryStore
        from data_paths import data_path
        catalog = json.loads(data_path("resource/data/blessing_catalog.json").read_text(encoding="utf-8"))
        InventoryStore(ROOT, catalog, self.rt).invalidate(reason, source="realm_bless")

    @staticmethod
    def _realm_chevron_state(frame, y0, y1):
        """Classify a realm region header chevron at x=442..470 as collapsed ('v') or expanded ('^')."""
        y0 = max(92, int(y0))
        y1 = min(680, int(y1))
        if y1 - y0 < 12:
            return "unknown"
        patch = cv2.cvtColor(frame[y0:y1, 442:470], cv2.COLOR_BGR2GRAY).astype(np.float32)
        bg = float(np.median(np.concatenate([patch[:, :3], patch[:, -3:]], axis=1)))
        diff = np.abs(patch - bg)
        peak = float(diff.max())
        if peak < 18:
            return "unknown"
        mask = diff >= max(15.0, peak * 0.35)
        row_counts = mask.sum(axis=1)
        valid = (row_counts >= 1) & (row_counts <= 20)
        best_block, cur = [], []
        for row_idx, ok in enumerate(valid):
            if ok:
                cur.append(row_idx)
            elif cur:
                if len(cur) > len(best_block):
                    best_block = cur
                cur = []
        if len(cur) > len(best_block):
            best_block = cur
        if len(best_block) < 6:
            return "unknown"
        sub = mask[best_block[0]:best_block[-1] + 1, :]
        third = max(2, sub.shape[0] // 3)

        def _span_stats(part):
            spans, stds = [], []
            for row in part:
                xs = np.where(row)[0]
                if len(xs):
                    spans.append(float(xs[-1] - xs[0]))
                    stds.append(float(xs.std()))
            return (
                float(np.mean(spans)) if spans else 0.0,
                float(np.mean(stds)) if stds else 0.0,
            )

        top_span, top_std = _span_stats(sub[:third])
        bot_span, bot_std = _span_stats(sub[-third:])
        if top_span > bot_span + 1.5 and top_std > bot_std:
            return "collapsed"
        if bot_span > top_span + 1.5 and bot_std > top_std:
            return "expanded"
        return "unknown"

    def _scroll_realm_to_top(self):
        rx, ry, rw, rh = self.REALM_SCROLL_ROI
        frame = self.rt.capture()
        if frame is None:
            return None
        before = frame[ry:ry + rh, rx:rx + rw].copy()
        for _ in range(12):
            if self.rt.stopped or not self.ui.scroll(self.REALM_SCROLL_ROI, 1800):
                return None
            frame = self.rt.capture()
            if frame is None:
                return None
            current = frame[ry:ry + rh, rx:rx + rw]
            if self.ui.scroll_unchanged(before, current):
                return frame
            before = current.copy()
        return frame

    def _realm_header_groups(self, frame):
        result = self.rt.recognize(
            "MaaNikki_Daily_OCR",
            {"roi": self.REALM_HEADER_ROI, "expected": r"产出[：:]|心愿原野|[伊尹]赞之土"},
            image=frame,
        )
        items = list(getattr(result, "filtered_results", None) or []) if result and result.hit else []
        items.sort(key=lambda item: item.box[1])
        groups = []
        for item in items:
            box = list(item.box)
            text = re.sub(r"\s+", "", getattr(item, "text", "") or "")
            if not text:
                continue
            if (groups
                    and len(groups[-1]["items"]) == 1
                    and "产出" not in groups[-1]["items"][0][1]
                    and "产出" in text
                    and 0 <= box[1] - groups[-1]["items"][0][0][1] <= 36):
                groups[-1]["items"].append((box, text))
            else:
                groups.append({"items": [(box, text)]})
        parsed = []
        for group in groups:
            combined = "".join(t for _, t in group["items"]).replace("尹赞之土", "伊赞之土")
            match = re.search(r"产出[：:]?(.+)$", combined)
            region_key = match.group(1) if match else re.sub(r"产出[：:]?", "", combined)
            if not region_key:
                continue
            sub = next((b for b, t in group["items"] if "产出" in t), None)
            title = next((b for b, t in group["items"] if "产出" not in t), None)
            click_box = sub or title
            if title is not None and sub is not None:
                mid = (title[1] + sub[1] + sub[3]) // 2
                y0, y1 = mid - 24, mid + 24
            elif sub is not None:
                y0, y1 = sub[1] - 30, sub[1] + 6
            else:
                y0, y1 = title[1] - 4, title[1] + 36
            parsed.append({
                "key": region_key,
                "click_box": click_box,
                "y0": y0,
                "y1": y1,
            })
        return parsed

    def _expand_collapsed_realm_regions(self):
        frame = self._scroll_realm_to_top()
        if frame is None:
            return False
        rx, ry, rw, rh = self.REALM_SCROLL_ROI
        stalled, expanded_any, expands = set(), False, 0
        for _ in range(24):
            if self.rt.stopped:
                return expanded_any
            headers = self._realm_header_groups(frame)
            clicked_current = False
            for header in headers:
                if header["key"] in stalled:
                    continue
                state = self._realm_chevron_state(frame, header["y0"], header["y1"])
                if state != "collapsed" or expands >= 10:
                    continue
                expands += 1
                if not self.ui.click_box(header["click_box"]) or not self.rt.pause(.45):
                    return expanded_any
                expanded_any = True
                clicked_current = True
                frame = self.rt.capture()
                if frame is None:
                    return expanded_any
                after_headers = self._realm_header_groups(frame)
                still_collapsed = any(
                    item["key"] == header["key"]
                    and abs(item["y0"] - header["y0"]) <= 28
                    and self._realm_chevron_state(frame, item["y0"], item["y1"]) == "collapsed"
                    for item in after_headers
                )
                if still_collapsed:
                    stalled.add(header["key"])
                break
            if clicked_current:
                continue
            before = frame[ry:ry + rh, rx:rx + rw].copy()
            if not self.ui.scroll(self.REALM_SCROLL_ROI, -600):
                return expanded_any
            after = self.rt.capture()
            if after is None:
                return expanded_any
            if self.ui.scroll_unchanged(before, after[ry:ry + rh, rx:rx + rw]):
                break
            frame = after
        return expanded_any

    def find_realm_level(self, kind, node, *, click=True):
        """Find (and optionally click) a realm trial, expanding collapsed regions if needed."""
        found = self.ui.find(roi=self.REALM_LEVEL_ROI, node=node, scroll_roi=self.REALM_SCROLL_ROI, click=click)
        if found or self.rt.stopped or kind not in ("bless", "monster"):
            return found
        if self._expand_collapsed_realm_regions():
            return self.ui.find(roi=self.REALM_LEVEL_ROI, node=node, scroll_roi=self.REALM_SCROLL_ROI, click=click)
        return False

    def select_realm_level(self, kind, node):
        if self.find_realm_level(kind, node, click=True):
            return True
        if not self.rt.stopped:
            if (kind == "weekly" and self.rt.hit("MaaNikki_Weekly_PageReady")
                    and self.rt.text(self.REALM_LEVEL_ROI, "挑战|守护|幻象")):
                self.level_missing = True
                return False
            self.rt.log("未找到所选幻境关卡，请确认已在游戏中解锁该试炼。")
            self.realm_level_diagnostic(kind, node)
        return False

    def realm_level_diagnostic(self, kind, node):
        """Keep the failed list before the caller returns to the calendar/main."""
        definition = fields(self.rt.context, node)
        self.rt.diagnostic("realm_level", "未找到目标幻境关卡", details={
            "kind": kind, "node": node, "expected": definition.get("expected"),
            "roi": definition.get("roi", self.REALM_LEVEL_ROI),
            "scroll_roi": self.REALM_SCROLL_ROI})

    def _record_realm_report(self, kind, node, *, maximize, fixed_count, energy_before,
                             report_prefix=None, report_task=None):
        per_run = self.MIN_ENERGY[kind]
        prefix_text = report_prefix or ""
        task_name = report_task or kind
        items = {}
        if kind == "jihua":
            quote = self._last_jihua_quote
            if quote is not None:
                target_name, energy_spent, gain = quote
                runs = max(1, energy_spent // per_run)
                items = {target_name: gain} if gain > 0 else {}
                level_label = target_name
            else:
                runs = fixed_count if fixed_count is not None else (
                    max(1, energy_before // per_run) if maximize and energy_before is not None else 1
                )
                energy_spent = runs * per_run
                level_label = normalize_item_name(
                    expected_text((fields(self.rt.context, "MaaNikki_Jihua_Target") or {}).get("expected"), "激化目标")
                )
            title = f"{prefix_text}素材激化 · {level_label}"
        else:
            fallback_label = {"bless": "祝福闪光", "monster": "魔物试炼", "weekly": "每周幻境"}[kind]
            realm_title = {"bless": "祝福闪光", "monster": "魔物试炼", "weekly": "心之突破"}[kind]
            level_label = expected_text(
                (fields(self.rt.context, node) or {}).get("expected") if node else None,
                fallback_label,
            )
            if kind == "weekly":
                runs = 1
            elif fixed_count is not None:
                runs = fixed_count
            elif maximize and energy_before is not None and energy_before >= per_run:
                runs = energy_before // per_run
            else:
                runs = 1
            energy_spent = runs * per_run
            if kind == "monster":
                drop_info = lookup_monster_drop(ROOT, level_label)
                if drop_info is not None:
                    _, mat_name, drop_qty = drop_info
                    items = {mat_name: runs * drop_qty}
            title = f"{prefix_text}{realm_title} · {level_label}"
        try_record_adventure_entry(
            ROOT,
            self.rt,
            category="realm",
            task=task_name,
            title=title,
            detail=f"消耗 {energy_spent} 活跃能量 · {runs} 次",
            energy_spent=energy_spent,
            runs=runs,
            items=items,
        )

    def _read_jihua_quote(self):
        """Read (target_name, energy, gain) from the staged Jihua screen before final confirmation."""
        try:
            frame = self.rt.frame()
            if frame is None:
                return None
            target = normalize_item_name(re.sub(r"\s+", "", self.rt.text([920, 170, 165, 45], image=frame) or ""))
            if not target:
                return None
            gain_raw = re.sub(r"[,，\s]+", "", self.rt.text([930, 306, 160, 38], image=frame) or "")
            if not re.fullmatch(r"\d+", gain_raw):
                return None
            gain = int(gain_raw)
            energy_raw = re.sub(r"[,，\s]+", "", self.rt.text([1025, 558, 115, 42], image=frame) or "")
            energy_match = re.search(r"(\d+)", energy_raw)
            if energy_match is None:
                fallback_raw = re.sub(r"[,，\s]+", "", self.rt.text([935, 558, 210, 42], image=frame) or "")
                energy_match = re.search(r"消耗[^\d]*(\d+)", fallback_raw) or re.search(r"(\d+)$", fallback_raw)
            if energy_match is None:
                return None
            energy = int(energy_match.group(1))
            if 10 <= energy <= 10000 and 1 <= gain <= 100_000_000:
                return target, energy, gain
        except Exception:
            pass
        return None

    def _read_jihua_bling_quote(self):
        """Read (energy, gain) from the staged Jihua screen before final confirmation."""
        quote = self._read_jihua_quote()
        if quote is None or quote[0] != "噗灵":
            return None
        return quote[1], quote[2]

    def _record_jihua_bling_stats(self, energy: int, gain: int):
        try:
            from collection import statistics
            if not statistics.is_enabled(ROOT):
                return
            account = self._stats_account()
            if account is None:
                return
            account.update(lambda data: statistics.queue_change(data, bling=(energy, gain)))
            statistics.upload(account, ROOT)
        except Exception:
            pass

    def jihua(self, maximize, allow_exhausted=False, fixed_count=None, *, record_bling_stats=True):
        submitted = False
        self._last_jihua_quote = None
        self.jihua_material_capped = False

        def enter():
            nonlocal submitted
            submitted = self.rt.click_template("MaaNikki_Jihua_Go", attempts=1,
                                               wait_seconds=0, verify=False)
            return submitted

        def steps():
            if not self.ui.enter_page(enter, "MaaNikki_MainDetected",
                    source="MaaNikki_Jihua_PageReady", attempts=1, seconds=30):
                return False
            if not self.jihua_open_table() or not self.jihua_select_target():
                return False
            staged = self.jihua_stage_materials(maximize, fixed_count)
            if staged is None:
                return False
            if not staged:
                if self.jihua_table_idle():
                    self.jihua_exhausted = True
                    self.rt.log("当前无可继续激化的材料或已达今日激化上限，本次正常结束。" if allow_exhausted else
                                "当前无可继续激化的材料或已达今日激化上限，本项未执行，不计入心愿完成。")
                    return bool(allow_exhausted)
                return self.confirm_unavailable("jihua", allow_exhausted)
            quote = self._read_jihua_quote()
            self._last_jihua_quote = quote
            bling_quote = (quote[1], quote[2]) if (record_bling_stats and quote and quote[0] == "噗灵") else None
            if not self.rt.click_template("MaaNikki_Jihua_FinalConfirm"):
                return self.confirm_unavailable("jihua", allow_exhausted)
            rewarded = self.jihua_reward()
            if rewarded and bling_quote is not None:
                self._record_jihua_bling_stats(*bling_quote)
            return rewarded

        left = True
        try:
            ok = steps()
        finally:
            # This realm has a separate world map; closing its menus does not
            # make the calendar's realm entrance usable. Clean up failures too.
            if submitted and not self.rt.stopped:
                left = self.jihua_leave()
        return bool(ok and left)

    def jihua_leave(self):
        self.rt.log("素材激化结束，正在后退穿过传送门离开幻境。", detail=True)
        if (self.rt.main() and self.rt.hold("backward", 3)
                and self.ui.wait_page("MaaNikki_MainDetected", seconds=30)
                and self.ui.calendar()
                and self.rt.click_template("MaaNikki_Jihua_Huanjing", attempts=1, wait_seconds=0)):
            # The ordinary HUD is also present inside this realm. A working
            # realm entrance proves we have actually returned to the world.
            self.rt.log("已离开素材激化幻境，幻境挑战入口已恢复。", detail=True)
            return True
        if self.rt.stopped:
            return False
        self.rt.log("未确认通过传送门离境，正在传送到星海无界枢纽。")
        from .starsea import Starsea
        if Starsea(self.rt, self.ui.resource).hub():
            return True
        self.rt.log("未能离开素材激化幻境，本项停止。")
        return False

    def jihua_reward(self):
        # Animation skip and reward skip are separate layers.
        if not self.rt.wait_hit("MaaNikki_Energy_SkipReward", 4):
            if not self.rt.stopped and self.rt.hit("MaaNikki_Jihua_NumConfirm"):
                self.rt.click_template("MaaNikki_Jihua_NumConfirm", attempts=1, wait_seconds=.3)
            if not self.rt.stopped and self.rt.hit("MaaNikki_Jihua_FinalConfirm"):
                self.rt.click_template("MaaNikki_Jihua_FinalConfirm", attempts=1, wait_seconds=.2)
            if not self.rt.wait_hit("MaaNikki_Energy_SkipReward", 8):
                if not self.rt.stopped:
                    self.rt.diagnostic("jihua_reward", "未识别到素材激化动画跳过提示")
                    self.rt.log("未识别到素材激化动画跳过提示，请检查游戏画面。")
                return False
        if not self.rt.pause(.5) or not self.rt.key("skip", .5):
            return False
        result = self.ui.skip_reward()
        if not result and not self.rt.stopped:
            self.rt.diagnostic("jihua_reward", "素材激化奖励领取结果待确认")
        self.rt.log("挑战奖励已领取。" if result else "挑战奖励领取结果待确认，请检查游戏画面。")
        return result

    def jihua_open_table(self):
        from .runtime import INTERACTION_ROI
        # World interaction labels move with the object/camera. Use the same
        # search area as other world prompts, rather than the old sample crop.
        prompt = self.ui.feature(text=r"素\s*材\s*激\s*化\s*台", roi=INTERACTION_ROI)
        steps = 0
        reason = "未找到素材激化台交互提示"
        for _ in range(6):
            # Check before the first move and allow a delayed prompt to settle
            # while stopped. Cap total movement at 2.5 seconds.
            if self.ui.wait_page(prompt, seconds=1.2):
                opened = self.ui.enter_page(lambda: self.rt.key("interact", 0),
                    "MaaNikki_Jihua_Target", source=prompt, recover=self.rt.main)
                if opened:
                    return True
                reason = "已识别素材激化台提示，但未能确认打开兑换界面"
                break
            if self.rt.stopped:
                return False
            if steps >= 5:
                break
            # HUD can briefly reappear before the actual loading screen starts.
            # Wait without inputs when it disappears, rather than aborting.
            if not self.rt.hit("MaaNikki_MainDetected"):
                self.rt.log("正在等待素材激化幻境加载完成。", detail=True)
                if not self.ui.wait_page("MaaNikki_MainDetected", seconds=30):
                    reason = "等待素材激化幻境主界面恢复超时"
                    break
                # Loading may have ended beside the table; recheck its prompt
                # before moving so arrival does not cause an unnecessary step.
                if self.ui.wait_page(prompt, seconds=1.2):
                    opened = self.ui.enter_page(lambda: self.rt.key("interact", 0),
                        "MaaNikki_Jihua_Target", source=prompt, recover=self.rt.main)
                    if opened:
                        return True
                    reason = "已识别素材激化台提示，但未能确认打开兑换界面"
                    break
            if not self.rt.hold("forward", .5):
                return False
            steps += 1
        if not self.rt.stopped:
            self.rt.diagnostic("jihua_table", reason, details={
                "prompt_roi": INTERACTION_ROI, "approach_steps": steps,
                "planned_move_seconds": round(steps*.5, 2)})
            self.rt.log(reason+"。")
        return False

    def jihua_select_target(self):
        def cost_ready():
            return bool(
                self.rt.text([900, 560, 240, 100], r"激\s*化\s*材\s*料")
                or (not self.rt.text([740, 95, 220, 70], r"选\s*择\s*产\s*物")
                    and self.rt.text([740, 95, 500, 430], r"预计获得|消耗材料"))
            )

        target_roi = self.ui.roi("AreaJihuaTargetSelect")
        if self.ui.enter_page(
                lambda: self.ui.find(roi=target_roi, node="MaaNikki_Jihua_Target"),
                cost_ready, source="MaaNikki_Jihua_Target"):
            return True
        if not self.rt.stopped:
            self.rt.diagnostic("jihua_target", "未能确认选择素材激化目标产物")
            self.rt.log("未能确认选择素材激化目标产物，本项停止。")
        return False

    def jihua_table_idle(self):
        if self.rt.stopped or self.rt.hit("MaaNikki_Jihua_NumConfirm"):
            return False
        return (not self.rt.hit("MaaNikki_Jihua_FinalConfirm")
                and bool(self.rt.text([900, 560, 240, 100], r"激\s*化\s*材\s*料")))

    def jihua_stage_one(self, open_dialog, maximize, target_energy):
        if self.ui.dismiss_tutorial() is False or self.ui.dismiss_reward() is False:
            return "error", 0
        if not self.ui.page_matches("MaaNikki_Jihua_NumConfirm"):
            if not open_dialog():
                return ("error" if self.rt.stopped else "not_opened"), 0
            if not self.ui.wait_page("MaaNikki_Jihua_NumConfirm", seconds=3):
                return ("error" if self.rt.stopped else "not_opened"), 0
        if not self.rt.pause(.3):
            return "error", 0
        staged_energy = 0
        capped = False
        if target_energy is not None:
            if not self.rt.set_amount("jihua", target_energy, allow_partial_cap=True):
                return "error", 0
            staged_energy = int(getattr(self.rt, "_last_jihua_staged_energy", 0) or 0)
            capped = bool(getattr(self.rt, "_last_jihua_capped", False))
        elif maximize and not self.rt.click_template("MaaNikki_Jihua_NumMax", wait_seconds=.2):
            return "error", 0
        if not self.rt.click_template("MaaNikki_Jihua_NumConfirm"):
            return "error", 0
        has_final = self.rt.wait_hit("MaaNikki_Jihua_FinalConfirm", 1.0)
        if target_energy is not None and capped:
            rem = max(0, target_energy - staged_energy)
            if has_final and rem < self.MIN_ENERGY["jihua"]:
                return "ready", staged_energy
            return "partial", staged_energy
        if has_final:
            return "ready", staged_energy
        return "insufficient", staged_energy

    def jihua_stage_materials(self, maximize, fixed_count):
        most = (self.rt.context.get_node_data("MaaNikki_Jihua_MaterialMostOpen") or {}).get("enabled", False)
        rem_energy = fixed_count * self.MIN_ENERGY["jihua"] if fixed_count is not None else None
        attempted_any = False
        if most:
            first_roi = self.ui.roi("AreaJihuaFirstMaterial")
            if (self.rt.action("Click", target=[93, 675]) and self.rt.pause(.2)
                    and self.ui.find(roi=[60, 585, 75, 85], text="数量", scroll=False)
                    and self.ui.click_asset("ButtonJihuaSort")
                    and self.ui.stable()):
                for col in range(5):
                    if rem_energy is not None and rem_energy < self.MIN_ENERGY["jihua"]:
                        break
                    cell_roi = [first_roi[0] + col * 105, first_roi[1], first_roi[2], first_roi[3]]
                    state, used_energy = self.jihua_stage_one(
                        lambda target=cell_roi: self.rt.action("Click", target=target),
                        maximize, rem_energy)
                    if state == "ready":
                        return True
                    if state == "error":
                        return None
                    if state == "not_opened":
                        break
                    attempted_any = True
                    if rem_energy is not None and used_energy > 0:
                        rem_energy = max(0, rem_energy - used_energy)
                        if rem_energy < self.MIN_ENERGY["jihua"] and self.rt.hit("MaaNikki_Jihua_FinalConfirm"):
                            return True
                if self.rt.hit("MaaNikki_Jihua_FinalConfirm"):
                    self.jihua_material_capped = True
                    return True
            self.rt.log("未能按数量投入足够激化材料，尝试已设置的首选和备选材料。" if attempted_any
                        else "未能按数量选择材料，尝试已设置的首选和备选材料。")
        options = fields(self.rt.context, "MaaNikki_Jihua_Material").get("expected", [])
        options = options if isinstance(options, list) else [options]
        options = [str(item).strip() for item in options if str(item or "").strip()]
        if not options:
            options = ["星荧草", "纽扣松果", "小绵菊"]
        metadata = catalog(self.ui.resource)["materials"]
        cost_roi = self.ui.roi("AreaJihuaCostSelect")
        for label in options:
            if rem_energy is not None and rem_energy < self.MIN_ENERGY["jihua"]:
                break
            label = {"小棉菊": "小绵菊"}.get(label, label)
            item = metadata.get(label)
            if not item or not item.get("jihua"):
                self.rt.log(f"{label}不能作为激化材料，尝试备选。")
                continue
            state, used_energy = self.jihua_stage_one(
                lambda asset=item["game_img"]: self.ui.find(
                    roi=cost_roi, asset=asset, scale=.5*2/3, threshold=.7),
                maximize, rem_energy)
            if state == "ready":
                return True
            if state == "error":
                return None
            if state in ("insufficient", "partial"):
                attempted_any = True
                if rem_energy is not None and used_energy > 0:
                    rem_energy = max(0, rem_energy - used_energy)
                    if rem_energy < self.MIN_ENERGY["jihua"] and self.rt.hit("MaaNikki_Jihua_FinalConfirm"):
                        return True
        if self.rt.hit("MaaNikki_Jihua_FinalConfirm"):
            self.jihua_material_capped = True
            return True
        if attempted_any:
            return False
        self.rt.log("未找到可用的激化材料。")
        return False if not self.rt.stopped and self.jihua_table_idle() else None

    def monthly_diagnostic(self, kind, stage, reason=None):
        """Retain the unfamiliar reward layer before ordinary page recovery."""
        if self.rt.stopped:
            return
        self.rt.diagnostic("monthly_"+stage, reason or "奖励页面状态待确认", details={"page": kind})

    def paradise_reward_return(self):
        def page_returned():
            with self.rt.observe():
                return (self.ui.page_matches("MaaNikki_Monthly_ParadiseReady")
                        and not self.rt.hit("MaaNikki_Monthly_ParadiseReward")
                        and not self.rt.hit("MaaNikki_Monthly_ParadiseContinue")
                        and not self.rt.hit("MaaNikki_RewardLayer"))

        deadline, dismissed = time.monotonic()+15, 0
        while not self.rt.stopped and time.monotonic() < deadline:
            with self.rt.observe(fresh=True):
                reward = self.rt.hit("MaaNikki_RewardLayer")
                continuation = self.rt.hit("MaaNikki_Monthly_ParadiseContinue")
                returned = not (reward or continuation) and page_returned()
            if reward or continuation:
                if dismissed >= 15:
                    break
                dismissed += 1
                self.rt.log("乐园构想：已识别奖励继续提示，尝试关闭奖励页面。", detail=True)
                if not self.rt.key("skip", .5):
                    return False
            elif returned and dismissed:
                return self.ui.wait_page(page_returned)
            elif not self.rt.pause(.3):
                return False
        self.monthly_diagnostic("paradise", "return_unconfirmed", "领取后未能确认回到乐园构想")
        self.rt.log("乐园构想领取后未能确认返回，已记录奖励界面，尝试正常退出。")
        return False

    def monthly_tab_selected(self, page):
        """Travel tabs use a gold selected background and dark inactive tabs."""
        with self.rt.observe():
            frame = self.rt.frame()
            result = self.rt.recognize(f"MaaNikki_Monthly_{page}Tab", image=frame)
            if frame is None or not result or not result.hit:
                return False
            box = getattr(result.best_result, "box", None)
            if box is None or len(box) != 4:
                return False
            x, y, width, height = map(int, box)
            patch = frame[max(0, y):min(frame.shape[0], y+height),
                          max(0, x):min(frame.shape[1], x+width)]
            if patch.size == 0:
                return False
            # Bounds come from the existing monthly/task_tab.png selected
            # template; monthly/treasure_tab.png provides the inactive style.
            selected = cv2.inRange(cv2.cvtColor(patch, cv2.COLOR_BGR2HSV),
                                   (10, 40, 100), (35, 200, 255))
            return cv2.countNonZero(selected) / selected.size >= .45

    def monthly_travel_tab(self, page):
        """Retry transient OCR misses; submit a tab click only once."""
        deadline = time.monotonic()+5
        submitted = False
        while not self.rt.stopped and time.monotonic() < deadline:
            with self.rt.observe(fresh=True):
                reward = self.rt.hit("MaaNikki_RewardLayer")
                if not reward and self.monthly_tab_selected(page):
                    return True
                if not reward and not submitted and self.ui.page_matches("MaaNikki_Monthly_TravelReady"):
                    # Read the entrance and its source from the same frame.
                    # Reward transitions can temporarily distort the tab text.
                    result = self.rt.recognize(f"MaaNikki_Monthly_{page}Tab")
                    if result and result.hit:
                        if not self.rt.action("Click", target=list(result.box)):
                            self.monthly_diagnostic("travel", "tab_click_failed", f"{page}页签点击未完成")
                            return False
                        submitted = True
            if reward and not self.rt.key("skip", .25):
                return False
            if not self.rt.pause(.1):
                return False
        self.monthly_diagnostic("travel", "tab_unconfirmed", f"{page}页签切换待确认")
        return False

    def monthly_travel_claim_return(self, page):
        """Finish on the actual receipt or settled task-button update."""
        deadline, dismissed, settled = time.monotonic()+15, 0, 0
        claim = f"MaaNikki_Monthly_Claim{page}"
        while not self.rt.stopped and time.monotonic() < deadline:
            with self.rt.observe(fresh=True):
                reward = self.rt.hit("MaaNikki_RewardLayer")
                returned = (not reward and self.monthly_tab_selected(page)
                            and (dismissed > 0 or page == "Task" and not self.rt.hit(claim)))
            if reward:
                settled = 0
                if dismissed >= 15:
                    break
                if not self.rt.key("skip", .25):
                    return False
                dismissed += 1
            elif returned:
                settled += 1
                if settled >= (2 if dismissed > 0 else 4):
                    return True
            else:
                settled = 0
            if not self.rt.pause(.2):
                return False
        self.monthly_diagnostic("travel", "claim_unconfirmed", f"{page}领取结果待确认")
        self.rt.log("奇迹之旅奖励领取状态待确认，本项停止。")
        return False

    def paradise_task_tab_ready(self):
        with self.rt.observe():
            return (self.ui.page_matches("MaaNikki_Monthly_ParadiseReady")
                    and (self.rt.hit("MaaNikki_Monthly_ParadiseClaim")
                         or self.rt.hit("MaaNikki_Monthly_ParadiseClaimEmpty")
                         or self.rt.hit("MaaNikki_Monthly_ParadiseTaskOngoing")
                         or self.rt.hit("MaaNikki_Monthly_ParadiseTaskCompleted")))

    def paradise_claim_state(self, claim):
        """Read the claim button; progress and the button can update after a reward closes."""
        for attempt in range(3):
            with self.rt.observe(fresh=True):
                if self.rt.hit(claim):
                    return "available"
                # With no claimable rewards, Paradise can show only ongoing-task
                # or completed-task glyphs and omit the bottom claim button.
                if attempt == 2 and (self.ui.page_matches("MaaNikki_Monthly_ParadiseReady")
                                     and (self.rt.hit("MaaNikki_Monthly_ParadiseClaimEmpty")
                                          or self.rt.hit("MaaNikki_Monthly_ParadiseTaskOngoing")
                                          or self.rt.hit("MaaNikki_Monthly_ParadiseTaskCompleted"))
                                     and not self.rt.hit("MaaNikki_RewardLayer")
                                     and not self.rt.hit("MaaNikki_Monthly_ParadiseReward")
                                     and not self.rt.hit("MaaNikki_Monthly_ParadiseContinue")):
                    return "empty"
            if attempt < 2 and not self.rt.pause(1):
                return None
        return "unknown"

    def paradise_claims(self, claim):
        """Claim until no reward remains; return whether any reward was claimed.

        Claimed Paradise rewards can count toward cumulative milestones and
        make another reward claimable, so one claim does not end this page.
        The limit only stops a button that keeps matching without any effect.
        """
        claimed = False
        for _ in range(self.PARADISE_CLAIM_LIMIT):
            state = self.paradise_claim_state(claim)
            if state is None or self.rt.stopped:
                return None
            if state == "empty":
                return claimed
            if state == "unknown":
                self.monthly_diagnostic("paradise", "claim_unconfirmed", "未能确定一键领取按钮状态")
                self.rt.log("乐园构想领取按钮状态未确认，本项未完成。")
                return None
            if not self.rt.click_template(claim, wait_seconds=.5):
                return None
            if not self.paradise_reward_return():
                return None
            claimed = True
        self.monthly_diagnostic("paradise", "claim_repeated", "多次领取后仍显示可领取")
        self.rt.log("乐园构想多次领取后仍显示可领取，本项未完成。")
        return None

    def monthly_rewards(self, kind):
        label = "乐园构想" if kind == "paradise" else "奇迹之旅"
        self.rt.log(f"已识别{label}，开始处理奖励。", detail=True)
        claimed, task_claimed, treasure_available = False, False, False
        # Inspect Treasure first without claiming. Return after Task only
        # for an existing treasure reward or newly claimed task progress.
        pages = ("Task",) if kind == "paradise" else ("Treasure", "Task", "Treasure")
        for index, page in enumerate(pages):
            if index == 2 and not (treasure_available or task_claimed):
                continue
            node = ("MaaNikki_Monthly_ParadiseTaskTab" if kind == "paradise"
                    else f"MaaNikki_Monthly_{page}Tab")
            selection_node = "MaaNikki_Monthly_ParadiseEnabled" if kind == "paradise" else node
            if not (self.rt.context.get_node_data(selection_node) or {}).get("enabled", False):
                continue
            if index == 2:
                self.rt.log("正在检查并领取旅行秘宝奖励。", detail=True)
            if kind == "travel":
                if not self.monthly_travel_tab(page):
                    return False
            else:
                if not self.ui.enter_page(
                        lambda: self.rt.click_template(node, attempts=1, wait_seconds=0),
                        self.paradise_task_tab_ready,
                        source="MaaNikki_Monthly_ParadiseReady"):
                    self.monthly_diagnostic("paradise", "tab_unconfirmed", "乐园纪事页签切换待确认")
                    return False
            claim = ("MaaNikki_Monthly_ParadiseClaim" if kind == "paradise"
                     else f"MaaNikki_Monthly_Claim{page}")
            if kind == "paradise":
                result = self.paradise_claims(claim)
                if result is None or not self.rt.pause(.5):
                    return False
                claimed = claimed or result
                continue
            with self.rt.observe(fresh=True):
                available = self.rt.hit(claim)
            if available:
                if index == 0:
                    treasure_available = True
                    continue
                if not self.rt.click_template(claim, wait_seconds=0):
                    return False
                if not self.monthly_travel_claim_return(page):
                    return False
                claimed = True
                if page == "Task":
                    task_claimed = True
        self.rt.log(f"{label}奖励已领取。" if claimed else f"{label}所选页签没有可领取的奖励。")
        return not self.rt.stopped

    def monthly_paradise_entry(self):
        """Accept the closed-season return only after a verified entrance click."""
        source = "MaaNikki_Monthly_TravelReady"
        destination = "MaaNikki_Monthly_ParadiseReady"
        node = "MaaNikki_Monthly_ToParadise"
        if not self.ui.wait_page(source):
            return "failed"

        def entrance_absent():
            with self.rt.observe():
                return self.ui.page_matches(source) and not self.rt.hit(node)

        # Some seasons remove the switch altogether. Require its absence on
        # separate captures of the known travel page, not an unknown screen.
        if entrance_absent() and self.ui.wait_page(entrance_absent, seconds=2):
            self.ui.monthly_paradise_unavailable = True
            return "unavailable"

        submitted = False

        def click():
            nonlocal submitted
            submitted = self.rt.click_template(node, attempts=1, wait_seconds=0, verify=False)
            return submitted

        def arrived():
            with self.rt.observe():
                return (self.ui.page_matches(destination)
                        or (submitted and (self.ui.page_matches("MaaNikki_MainDetected")
                                           or self.ui.page_matches("MaaNikki_MenuReady"))))

        # An expired Paradise switch or locked Home feature can return to the
        # world/menu or keep the player on Travel after clicking the switch.
        if not self.ui.enter_page(click, arrived, source=source, attempts=1, seconds=8,
                                  diagnostic_context={"page": "paradise", "optional": True}):
            if (submitted and not self.rt.stopped
                    and self.ui.wait_page(source, seconds=1.5)):
                if (self.rt.click_template(node, attempts=1, wait_seconds=0, verify=False)
                        and self.ui.wait_page(arrived, seconds=3)):
                    if self.ui.page_matches(destination):
                        return "ready"
                if not self.rt.stopped and self.ui.wait_page(source, seconds=1.5):
                    self.ui.monthly_paradise_unavailable = True
                    return "unavailable"
            return "failed"
        if self.ui.page_matches(destination):
            return "ready"
        if submitted and (self.ui.page_matches("MaaNikki_MainDetected")
                          or self.ui.page_matches("MaaNikki_MenuReady")):
            self.ui.monthly_paradise_unavailable = True
            return "unavailable"
        return "failed"

    def monthly(self):
        kind, label = "unknown", "奇迹之旅／乐园构想"
        # Availability belongs to this run; a new season can reopen Paradise.
        self.ui.monthly_paradise_unavailable = False
        completed = set()
        selected = {
            "travel": any((self.rt.context.get_node_data(f"MaaNikki_Monthly_{page}Tab") or {}).get("enabled", False)
                          for page in ("Task", "Treasure")),
            "paradise": (self.rt.context.get_node_data("MaaNikki_Monthly_ParadiseEnabled") or {}).get("enabled", False),
        }
        if not any(selected.values()):
            self.rt.log("未选择奇迹之旅或乐园构想奖励，跳过。")
            return True
        def steps():
            nonlocal kind, label
            if not self.ui.monthly_page():
                self.rt.log("未能确认奇迹之旅或乐园构想页面。")
                return False
            for index in range(2):
                if self.ui.page_matches("MaaNikki_Monthly_ParadiseReady"):
                    kind, label = "paradise", "乐园构想"
                elif self.ui.page_matches("MaaNikki_Monthly_TravelReady"):
                    kind, label = "travel", "奇迹之旅"
                else:
                    self.monthly_diagnostic("unknown", "page_unconfirmed", "未能确定奖励页面类型")
                    return False
                if kind in completed:
                    self.monthly_diagnostic(kind, "switch_unconfirmed", "切换后仍停留在已处理页面")
                    return False
                if selected[kind] and not self.monthly_rewards(kind):
                    return False
                completed.add(kind)
                if index == 0:
                    other = "travel" if kind == "paradise" else "paradise"
                    if not selected[other]:
                        break
                    if other == "paradise" and self.ui.monthly_paradise_unavailable:
                        self.rt.log("本轮乐园构想入口不可用，已跳过。")
                        break
                    target = "Travel" if kind == "paradise" else "Paradise"
                    target_label = "奇迹之旅" if kind == "paradise" else "乐园构想"
                    self.rt.log(f"{label}处理完成，点击左下角切换到{target_label}。", detail=True)
                    if other == "paradise":
                        result = self.monthly_paradise_entry()
                        if result == "unavailable":
                            self.rt.log("本轮乐园构想入口不可用，已跳过。")
                            break
                        switched = result == "ready"
                    else:
                        # A new Travel season can land on its intro or Start
                        # screen. Resolve those before requiring the reward tabs.
                        switched = self.rt.click_template(
                            f"MaaNikki_Monthly_To{target}", ready="MaaNikki_Monthly_TravelOpened")
                        if switched and not self.ui.page_matches("MaaNikki_Monthly_TravelReady"):
                            switched = self.ui.monthly_intro_start() and (
                                (self.ui.page_matches("MaaNikki_Monthly_TravelReady")
                                 and self.ui.wait_page("MaaNikki_Monthly_TravelReady"))
                                or self.rt.click_template(
                                    "MaaNikki_Monthly_Start", ready="MaaNikki_Monthly_TravelReady"))
                    if not switched:
                        self.monthly_diagnostic(kind, "switch_unconfirmed", f"未能切换到{target_label}")
                        return False
            self.rt.log("所选奇迹之旅与乐园构想奖励检查完成。", detail=True)
            return True

        returned = False
        try:
            ok = steps()
        finally:
            if not self.rt.stopped:
                returned = self.rt.main()
                if not returned:
                    self.monthly_diagnostic(kind, "exit_unconfirmed", "未能返回主界面")
                    self.rt.log(f"{label}处理后未能返回主界面，本项未正常结束。")
        return bool(ok and not self.rt.stopped and returned)
