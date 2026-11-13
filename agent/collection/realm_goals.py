"""The realm challenge priority queue saved in the task option (no game dependencies)."""
from __future__ import annotations

REALM_GOALS = ("weekly", "renewal", "crafting", "glow")
GOAL_LABELS = {"weekly": "每周幻境", "renewal": "换新材料", "crafting": "制衣材料", "glow": "五星闪光"}


def parse_realm_queue(raw) -> list[str]:
    """Enabled goals in priority order from the saved order, e.g. "weekly,renewal,-crafting,glow".

    A leading "-" keeps a goal's position while it is switched off; unknown or
    repeated names are ignored so a damaged value only drops goals, never adds them.
    Orders saved before the weekly realm joined the queue ran it first by default.
    """
    tokens = [token.strip() for token in str(raw or "").split(",") if token.strip()]
    if not any(token.lstrip("-") == "weekly" for token in tokens):
        tokens.insert(0, "weekly")
    enabled, seen = [], set()
    for token in tokens:
        name = token.lstrip("-")
        if name not in REALM_GOALS or name in seen:
            continue
        seen.add(name)
        if not token.startswith("-"):
            enabled.append(name)
    return enabled
