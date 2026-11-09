"""Runtime wiring shared by daily routes, route trials and local approaches."""
from __future__ import annotations

from .engine import Navigator
from .overlay import RouteMinimapOverlay, resolve_route_overlay_mode


def create_navigator(runtime, teleporter, *, actions=None, event=lambda value: None):
    """Bind page checks, walking state and independent recovery in one place."""
    hwnd = getattr(teleporter.inputs, "hwnd", 0) or runtime.controller.info.get("hwnd", 0)
    overlay_mode = resolve_route_overlay_mode(getattr(runtime, "context", None))
    overlay = RouteMinimapOverlay(hwnd, teleporter.locator.spec, initial_mode=overlay_mode) if hwnd else None
    return Navigator(teleporter.locator, teleporter.inputs, runtime.capture,
        lambda: runtime.stopped, runtime.pause, event,
        point_action=actions,
        point_prepare=actions.prepare_point if actions else None,
        route_branch=actions.branch_route if actions and actions.executor == "home" else None,
        walking=runtime.ui.walking, relocalize=teleporter.player_location,
        scene_recovery=lambda: runtime.ui.revive(event),
        page_detector=lambda frame: bool(
            (result := runtime.recognize("MaaNikki_MainDetected", image=frame)) and result.hit),
        overlay=overlay)
