"""Runtime wiring shared by daily routes, route trials and local approaches."""
from __future__ import annotations

from overlay_style import should_exclude_from_capture
from task_control import active_session
from .engine import Navigator
from .overlay import RouteMinimapOverlay, resolve_route_overlay_config


def create_navigator(runtime, teleporter, *, actions=None, event=lambda value: None):
    """Bind page checks, walking state and independent recovery in one place."""
    hwnd = getattr(teleporter.inputs, "hwnd", 0) or runtime.controller.info.get("hwnd", 0)
    overlay_mode, task_override = resolve_route_overlay_config(getattr(runtime, "context", None))
    session = active_session()
    hud = session.ensure_hud(hwnd) if (session is not None and hwnd) else None
    allow_bg = bool(
        (session is not None and (session._background_mode() or session._control_mode() == "background"))
        or getattr(teleporter.inputs, "window_only", False)
    )
    exclude_capture = (
        session._exclude_from_capture_setting(runtime.controller)
        if session is not None
        else should_exclude_from_capture(runtime.controller)
    )
    overlay = (
        RouteMinimapOverlay(
            hwnd,
            teleporter.locator.spec,
            exclude_from_capture=True if exclude_capture is None else bool(exclude_capture),
            initial_mode=overlay_mode,
            task_override=task_override,
            hud_sink=hud,
            allow_background=allow_bg,
        )
        if hwnd else None
    )
    return Navigator(teleporter.locator, teleporter.inputs, runtime.capture,
        lambda: runtime.stopped, runtime.pause, event,
        point_action=actions,
        point_prepare=actions.prepare_point if actions else None,
        route_branch=actions.branch_route if actions and actions.executor == "home" else None,
        walking=runtime.ui.walking, relocalize=teleporter.player_location,
        scene_recovery=lambda: runtime.ui.revive(event),
        page_detector=lambda frame: bool(
            (result := runtime.recognize("MaaNikki_MainDetected", image=frame)) and result.hit),
        overlay=overlay,
        wasd_mode=bool(getattr(teleporter.inputs, "window_only", False)))
