"""Home daily route and its final giant-meteor notice."""
from __future__ import annotations

from types import SimpleNamespace

from account_state import AccountState, game_day
from .settings import ROOT


class Home:
    def __init__(self, runtime):
        self.rt, self.ui = runtime, runtime.ui

    def run(self):
        if not self.rt.main() or self.rt.stopped:
            return False
        card = SimpleNamespace(rule=SimpleNamespace(executor="home"), remaining=None)
        result = self.rt.navigate("home_daily", "home", card)
        if self.rt.stopped:
            return False
        # Upstream checks this notice even when the route reports a failed action.
        self.rt.log("检测是否有巨陨星。")
        day = game_day().isoformat()
        outcome, text = "unconfirmed", ""
        if not self.rt.main() or not self.ui.open_map():
            self.save_meteor(outcome, day, text)
            return False
        # The notice area is empty on ordinary days. Confirm the map on each
        # frame and inspect twice before recording that no notice was found.
        for attempt in range(2):
            frame = self.rt.capture()
            ready = (self.rt.recognize("MaaNikki_Navigation_MapFeature", image=frame)
                     if frame is not None else None)
            if not ready or not ready.hit:
                outcome = "unconfirmed"
                break
            text = self.ui.text(self.ui.roi("AreaBigMapMeteorText"), image=frame)
            if "巨陨星" in "".join(text.split()):
                outcome = "found"
                break
            if attempt == 0 and not self.rt.pause(.4):
                return False
            outcome = "not_found"
        self.save_meteor(outcome, day, text)
        if outcome == "found":
            self.rt.log("今日有巨陨星！")
        elif outcome == "not_found":
            self.rt.log("今日检测未发现巨陨星。")
        else:
            self.rt.log("巨陨星检测未确认，稍后可重新运行家园日常。")
        return result

    def save_meteor(self, outcome, day, text):
        try:
            AccountState(ROOT).record_giant_meteor(outcome, day=day, text=text)
        except (OSError, ValueError) as error:
            self.rt.log(f"巨陨星检测结果未能保存：{error}")
