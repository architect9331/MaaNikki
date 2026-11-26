"""Weekly Home Shop (园宝小铺) purchases using 万象星实."""
from __future__ import annotations

import re
from task_control import task_time as time

from account_state import AccountState, period_key
from adventure_report import try_record_adventure_entry
from .settings import ROOT


HOME_TELEPORT = {
    "name": "家园码头",
    "province": "家园",
    "region": "家园",
    "x": 1047.0,
    "y": 721.85,
}

# Verified 1280x720 ROIs calibrated directly on live Home Shop captures.
LIST_ROI = [75, 165, 1140, 445]
SCROLL_INPUT_ROI = [360, 220, 560, 320]
STAR_SEED_BALANCE_ROI = [1140, 10, 130, 38]
SHOP_ENTRY_ROI = [330, 540, 190, 75]
STRICT_TAB_ROI = [430, 8, 145, 46]
ABILITY_SECTION_ROI = [115, 125, 145, 42]
HUD_HOME_MANAGE_ROI = [760, 0, 520, 90]

MODAL_ITEM_NAME_ROI = [340, 322, 230, 44]
MODAL_LIMIT_ROI = [710, 364, 185, 36]
MODAL_QTY_ROI = [750, 266, 95, 36]
MODAL_TOTAL_ROI = [760, 444, 150, 40]
MODAL_MAX_BTN_BOX = [886, 271, 28, 28]
MODAL_CONFIRM_ROI = [520, 505, 240, 62]

CONFIRM2_TITLE_ROI = [565, 208, 150, 46]
CONFIRM2_BODY_ROI = [415, 312, 450, 58]
CONFIRM2_OK_ROI = [660, 428, 140, 48]

ITEM_SPECS = {
    "能量结晶": {
        "option_key": "energy_crystal",
        "ocr_expected": r"^能量结晶$",
        "unit_price": 50000,
        "pack_size": 1,
        "unit_label": "个",
    },
    "闪亮泡泡": {
        "option_key": "shiny_bubbles",
        "ocr_expected": r"^闪亮泡泡",
        "unit_price": 8000,
        "pack_size": 80,
        "unit_label": "个",
    },
}


def _parse_int(text: str) -> int | None:
    match = re.search(r"\d+", (text or "").replace(",", "").replace("，", ""))
    return int(match.group(0)) if match else None


def _parse_ratio(text: str) -> tuple[int, int] | None:
    match = re.search(r"(\d+)\s*[/／]\s*(\d+)", text or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


class WeeklyHomeShop:
    TASK = "weekly_home_shop"
    ENTRY = "MaaNikki_WeeklyHomeShop"
    LABEL = "每周园宝小铺"

    def __init__(self, runtime, options: dict | None = None):
        self.rt, self.ui = runtime, runtime.ui
        self.options = options if isinstance(options, dict) else {}
        self.account = AccountState.for_runtime(ROOT, runtime)
        self.period = period_key(self.TASK)
        self.completed = self.account.completed_items(self.TASK, self.period)
        self.selected = self._selected_items()
        self.purchased_summary: list[str] = []
        self.purchased_items: dict[str, int] = {}

    def _selected_items(self) -> list[str]:
        has_flag = any(
            spec["option_key"] in self.options
            for spec in ITEM_SPECS.values()
        )
        if not has_flag:
            return ["能量结晶", "闪亮泡泡"]
        chosen = [
            name for name, spec in ITEM_SPECS.items()
            if self.options.get(spec["option_key"]) is True
        ]
        return chosen

    def confirm(self, name: str, outcome: str) -> None:
        self.account.confirm(self.TASK, name, outcome, period=self.period)
        self.completed[name] = outcome

    def fail(self, reason: str) -> bool:
        self.rt.log(f"{self.LABEL}：{reason}，本项未完成。")
        if self.rt.stopped:
            return False
        self.rt.diagnostic(
            "home_shop",
            reason,
            details={"entry": self.ENTRY, "selected": self.selected, "completed": self.completed},
        )
        return False

    def _try_open_manage_from_main(self) -> bool:
        if self.ui.page_matches("MaaNikki_HomeShop_ManageReady"):
            return True
        if self.ui.page_matches("MaaNikki_HomeShop_ShopReady"):
            return True
        # Fast path: default hotkey I directly opens 家园经营 when inside Home.
        if self.ui.enter_page(
            lambda: self.rt.action("Key", key=ord("I")),
            "MaaNikki_HomeShop_ManageReady",
            source="MaaNikki_MainDetected",
            attempts=1,
            seconds=2.5,
        ):
            return True
        if self.rt.stopped:
            return False
        # Fallback if I was rebound while already in Home: click top-right 家园经营 icon text.
        frame = self.rt.capture()
        if frame is not None:
            hud_btn = self.rt.recognize(
                "MaaNikki_Daily_OCR",
                {"roi": HUD_HOME_MANAGE_ROI, "expected": r"家园经营"},
                image=frame,
            )
            if hud_btn and hud_btn.hit:
                bx, by, bw, bh = hud_btn.box
                click_box = [bx, max(0, by - 26), bw, bh + 26]
                if self.ui.enter_page(
                    lambda: self.ui.click_box(click_box),
                    "MaaNikki_HomeShop_ManageReady",
                    source="MaaNikki_MainDetected",
                    attempts=1,
                    seconds=3.0,
                ):
                    return True
        return False

    def _teleport_to_home(self) -> bool:
        from navigation.models import NavigationError, maps
        from navigation.teleport import Teleporter
        from navigation.vision import Locator

        self.rt.log(f"{self.LABEL}：当前不在家园，正在传送前往家园…", live=True)
        try:
            with self.rt.input_guard() as inputs:
                locator = Locator(maps(self.rt.ui.resource)["home"])
                teleporter = Teleporter(self.rt, locator, lambda _ev: None, inputs)
                teleporter.transport(HOME_TELEPORT)
        except NavigationError as error:
            self.rt.log(f"{self.LABEL}：传送前往家园失败（{error}）。", detail=True)
            return False
        return bool(self.rt.main())

    def open_shop(self) -> bool:
        if self.ui.page_matches("MaaNikki_HomeShop_ShopReady"):
            return self._ensure_strict_tab()
        if not self.ui.page_matches("MaaNikki_HomeShop_ManageReady"):
            if not self.rt.main():
                return False
            if not self._try_open_manage_from_main():
                if self.rt.stopped:
                    return False
                if not self._teleport_to_home():
                    return False
                if not self._try_open_manage_from_main():
                    return False
        if self.ui.page_matches("MaaNikki_HomeShop_ShopReady"):
            return self._ensure_strict_tab()
        # Click 园宝小铺 on 家园经营 page.
        frame = self.rt.capture()
        if frame is None:
            return False
        entry = self.rt.recognize(
            "MaaNikki_Daily_OCR",
            {"roi": SHOP_ENTRY_ROI, "expected": r"园宝小铺"},
            image=frame,
        )
        target = list(entry.box) if (entry and entry.hit) else [368, 520, 116, 70]
        if not self.ui.enter_page(
            lambda: self.ui.click_box(target),
            "MaaNikki_HomeShop_ShopReady",
            source="MaaNikki_HomeShop_ManageReady",
            attempts=2,
            seconds=6.0,
        ):
            return False
        return self._ensure_strict_tab()

    def _ensure_strict_tab(self) -> bool:
        frame = self.rt.capture()
        if frame is None:
            return False
        if not self.rt.text(ABILITY_SECTION_ROI, r"能力道具", image=frame):
            tab = self.rt.recognize(
                "MaaNikki_Daily_OCR",
                {"roi": STRICT_TAB_ROI, "expected": r"园宝严选"},
                image=frame,
            )
            if tab and tab.hit:
                self.ui.click_box(list(tab.box))
                self.rt.pause(0.4)
        return self.ui.wait_page("MaaNikki_HomeShop_ShopReady", seconds=4.0)

    def _read_balance(self, image=None) -> int | None:
        frame = image if image is not None else self.rt.capture()
        if frame is None:
            return None
        text = self.ui.text(STAR_SEED_BALANCE_ROI, image=frame)
        return _parse_int(text)

    def _find_item_card(self, name: str):
        expected = ITEM_SPECS[name]["ocr_expected"]
        frame = self.rt.capture()
        if frame is None:
            return None, None
        result = self.rt.recognize(
            "MaaNikki_Daily_OCR",
            {"roi": LIST_ROI, "expected": expected},
            image=frame,
        )
        if result and result.hit:
            return list(result.box), frame
        return None, frame

    def _card_is_sold_out(self, box: list[int], image) -> bool:
        bx, by, bw, _ = box
        header_roi = [max(75, bx - 60), max(165, by - 145), min(210, 1215 - max(75, bx - 60)), 52]
        header = self.ui.text(header_roi, image=image)
        if "已售罄" in header or "售罄" in header:
            return True
        ratio = _parse_ratio(header)
        if ratio is not None and ratio[0] == 0:
            return True
        return False

    def _close_modal_to_shop(self) -> None:
        for _ in range(3):
            if self.rt.stopped or self.ui.page_matches("MaaNikki_HomeShop_ShopReady"):
                return
            self.rt.back()
            self.rt.pause(0.35)

    def _dismiss_reward_to_shop(self) -> bool:
        deadline = time.monotonic() + 8.0
        while not self.rt.stopped and time.monotonic() < deadline:
            with self.rt.observe():
                if self.ui.page_matches("MaaNikki_HomeShop_ShopReady"):
                    return True
                reward = (
                    self.rt.hit("MaaNikki_HomeShop_RewardReady")
                    or self.rt.hit("MaaNikki_RewardOverlayReady")
                )
            if reward:
                self.rt.action("Click", target=[640, 620])
                self.rt.pause(0.45)
            else:
                self.rt.pause(0.25)
        return self.ui.page_matches("MaaNikki_HomeShop_ShopReady")

    def _buy_item(self, name: str, box: list[int]) -> bool:
        spec = ITEM_SPECS[name]
        unit_price = spec["unit_price"]
        balance = self._read_balance()
        if balance is not None and balance < unit_price:
            self.rt.log(
                f"{self.LABEL}：万象星实不足（当前 {balance:,}，购买{name}至少需 {unit_price:,}），本次跳过。"
            )
            return True

        if not self.ui.enter_page(
            lambda: self.ui.click_box(box),
            "MaaNikki_HomeShop_BuyModalReady",
            source="MaaNikki_HomeShop_ShopReady",
            attempts=2,
            seconds=4.0,
        ):
            return self.fail(f"未能打开{name}购买弹窗")

        frame = self.rt.capture()
        if frame is None:
            self._close_modal_to_shop()
            return self.fail(f"未能读取{name}购买弹窗")

        detail_name = "".join(self.ui.text(MODAL_ITEM_NAME_ROI, image=frame).split())
        if name not in detail_name:
            self._close_modal_to_shop()
            return self.fail(f"购买弹窗商品名称不匹配（期望 {name}，实际 {detail_name or '空'}）")

        limit_text = self.ui.text(MODAL_LIMIT_ROI, image=frame)
        ratio = _parse_ratio(limit_text)
        remaining = ratio[0] if ratio is not None else 1
        if remaining <= 0:
            self._close_modal_to_shop()
            self.confirm(name, "sold_out")
            self.rt.log(f"{self.LABEL}：{name}本周已售罄，跳过。", detail=True)
            return True

        if remaining > 1:
            if not self.ui.click_box(MODAL_MAX_BTN_BOX):
                self._close_modal_to_shop()
                return self.fail(f"未能点击{name}最大数量按钮")
            self.rt.pause(0.35)
            frame = self.rt.capture()
            if frame is None:
                self._close_modal_to_shop()
                return self.fail(f"调整{name}购买数量后未能读取画面")

        qty = _parse_int(self.ui.text(MODAL_QTY_ROI, image=frame)) or 1
        total_cost = _parse_int(self.ui.text(MODAL_TOTAL_ROI, image=frame))
        balance = self._read_balance(frame)
        if balance is not None and total_cost is not None and total_cost > balance:
            self._close_modal_to_shop()
            self.rt.log(
                f"{self.LABEL}：万象星实不足（当前 {balance:,}，合计需 {total_cost:,}），跳过购买{name}。"
            )
            return True

        confirm_btn = self.rt.recognize(
            "MaaNikki_Daily_OCR",
            {"roi": MODAL_CONFIRM_ROI, "expected": r"^确认$"},
            image=frame,
        )
        if not confirm_btn or not confirm_btn.hit:
            self._close_modal_to_shop()
            return self.fail(f"未识别到{name}购买确认按钮")

        if not self.rt.image_current(frame) or not self.ui.click_box(list(confirm_btn.box)):
            self._close_modal_to_shop()
            return False

        # Wait for either secondary 兑换确认 dialog or direct 恭喜获得 reward overlay.
        deadline = time.monotonic() + 6.0
        settled = False
        while not self.rt.stopped and time.monotonic() < deadline:
            cur = self.rt.capture()
            if cur is None:
                break
            confirm2 = self.rt.recognize(
                "MaaNikki_Daily_OCR",
                {"roi": CONFIRM2_TITLE_ROI, "expected": r"兑换确认"},
                image=cur,
            )
            if confirm2 and confirm2.hit:
                body = "".join(self.ui.text(CONFIRM2_BODY_ROI, image=cur).split())
                if "万象星实" not in body or name not in body:
                    self._close_modal_to_shop()
                    return self.fail(f"二次确认弹窗内容异常（{body or '空'}），已取消操作")
                ok2 = self.rt.recognize(
                    "MaaNikki_Daily_OCR",
                    {"roi": CONFIRM2_OK_ROI, "expected": r"^确认$"},
                    image=cur,
                )
                if not ok2 or not ok2.hit:
                    self._close_modal_to_shop()
                    return self.fail(f"未找到{name}二次兑换确认按钮")
                if not self.rt.image_current(cur) or not self.ui.click_box(list(ok2.box)):
                    self._close_modal_to_shop()
                    return False
                settled = self._dismiss_reward_to_shop()
                break
            reward = (
                self.rt.recognize("MaaNikki_HomeShop_RewardReady", image=cur)
                or self.rt.recognize("MaaNikki_RewardOverlayReady", image=cur)
            )
            if reward and reward.hit:
                settled = self._dismiss_reward_to_shop()
                break
            self.rt.pause(0.25)

        if not settled:
            return self.fail(f"{name}购买后未能确认领取奖励并返回园宝小铺")

        gained_units = qty * spec["pack_size"]
        if qty >= remaining:
            self.confirm(name, "purchased")
        else:
            self.rt.log(
                f"{self.LABEL}：已购买 {name}×{gained_units}（万象星实不足，本周仍剩 {remaining - qty} 份未购）。"
            )
        self.purchased_items[name] = self.purchased_items.get(name, 0) + gained_units
        self.purchased_summary.append(f"{name}×{gained_units}")
        cost_note = f"（消耗 {total_cost:,} 万象星实）" if total_cost else ""
        self.rt.log(f"{self.LABEL}：已购买 {name}×{gained_units}{cost_note}。")
        return True

    def scan(self) -> bool:
        self.purchased_summary = []
        self.purchased_items = {}
        for name in self.selected:
            if self.rt.stopped:
                return False
            if name in self.completed:
                continue
            box, frame = self._find_item_card(name)
            if frame is None:
                return False
            if box is None:
                # Bought-out weekly items move out of the top 园宝严选 row; confirm sold_out.
                self.confirm(name, "sold_out")
                self.rt.log(f"{self.LABEL}：{name}本周已购买，跳过。", detail=True)
                continue
            if self._card_is_sold_out(box, frame):
                self.confirm(name, "sold_out")
                self.rt.log(f"{self.LABEL}：{name}本周已售罄，跳过。", detail=True)
                continue
            if not self.rt.image_current(frame) or not self._buy_item(name, box):
                return False
        if self.purchased_summary:
            try_record_adventure_entry(
                ROOT,
                self.rt,
                category="daily",
                task=self.TASK,
                title=self.LABEL,
                detail=f"已购买：{'、'.join(self.purchased_summary)}",
                runs=len(self.purchased_summary),
                items=self.purchased_items,
            )
        else:
            self.rt.log(f"{self.LABEL}：所选商品本周已购齐或暂跳过。")
        return not self.rt.stopped

    def run(self) -> bool:
        completed = False
        try:
            if not self.selected:
                self.rt.log(f"{self.LABEL}：未勾选任何购买项目，跳过。")
                completed = True
            elif set(self.selected).issubset(self.completed):
                self.rt.log(f"{self.LABEL}：本周所选项目已完成，跳过。")
                completed = True
            else:
                completed = self.scan() if self.open_shop() else self.fail("未能进入园宝小铺页面")
        finally:
            if not self.rt.stopped:
                _, ok = self.rt.finish(self.ENTRY)
                if not ok:
                    self.fail("未能返回后续任务需要的页面")
                    completed = False
        return completed
