"""Home daily route and its final giant-meteor notice."""
from __future__ import annotations

from types import SimpleNamespace


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
        if not self.ui.open_map():
            return False
        text = self.ui.text(self.ui.roi("AreaBigMapMeteorText"))
        if "巨陨星" in text:
            self.rt.log("今日有巨陨星！")
        return result
