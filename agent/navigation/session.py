"""Runtime wiring shared by daily routes, route trials and local approaches."""
from __future__ import annotations

from pathlib import Path

from client_config import mxu_config, node_action_params
from overlay_style import should_exclude_from_capture
from task_control import active_session
from .engine import Navigator
from .overlay import RouteMinimapOverlay, resolve_route_overlay_config

_ROOT = Path(__file__).resolve().parents[2]


def resolve_navigation_steering_mode(context=None) -> str:
    """Resolve user-configured navigation steering ('wasd' | 'camera'), defaulting to 'wasd'."""
    if context is not None:
        try:
            mode = str(node_action_params(context, "MaaNikki_NavigationSteering").get("mode") or "").strip().lower()
            if mode in {"wasd", "camera"}:
                return mode
        except Exception:
            pass
    try:
        cfg = mxu_config(_ROOT)
        opt = ((cfg or {}).get("globalOptionValues") or {}).get("NavigationSteeringMode")
        if isinstance(opt, dict) and opt.get("caseName") == "Camera":
            return "camera"
    except Exception:
        pass
    return "wasd"


def create_navigator(runtime, teleporter, *, actions=None, event=lambda value: None, wasd_mode: bool | None = None):
    """Bind page checks, walking state and independent recovery in one place."""
    hwnd = getattr(teleporter.inputs, "hwnd", 0) or runtime.controller.info.get("hwnd", 0)
    overlay_mode, task_override = resolve_route_overlay_config(getattr(runtime, "context", None))
    session = active_session()
    hud = session.ensure_hud(hwnd) if (session is not None and hwnd) else None
    allow_bg = bool(
        (session is not None and (session._background_mode() or session._control_mode() == "background"))
        or getattr(teleporter.inputs, "window_only", False)
    )
    if wasd_mode is not None:
        resolved_wasd = bool(wasd_mode)
    elif getattr(teleporter.inputs, "window_only", False):
        resolved_wasd = True
    else:
        resolved_wasd = resolve_navigation_steering_mode(getattr(runtime, "context", None)) == "wasd"
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
        scene_recovery=lambda: runtime.ui.recover_world_scene(event),
        page_detector=lambda frame: bool(
            (result := runtime.recognize("MaaNikki_MainDetected", image=frame)) and result.hit),
        overlay=overlay,
        wasd_mode=resolved_wasd)
