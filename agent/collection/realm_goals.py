"""The realm challenge priority queue saved in the task option (no game dependencies)."""
from __future__ import annotations

REALM_GOALS = ("renewal", "crafting", "glow")
GOAL_LABELS = {"renewal": "换新材料", "crafting": "制衣材料", "glow": "五星闪光"}


def parse_realm_queue(raw) -> list[str]:
    """Enabled goals in priority order from the saved order, e.g. "renewal,-crafting,glow".

    A leading "-" keeps a goal's position while it is switched off; unknown or
    repeated names are ignored so a damaged value only drops goals, never adds them.
    """
    enabled, seen = [], set()
    for token in str(raw or "").split(","):
        token = token.strip()
        name = token.lstrip("-")
        if name not in REALM_GOALS or name in seen:
            continue
        seen.add(name)
        if not token.startswith("-"):
            enabled.append(name)
    return enabled
