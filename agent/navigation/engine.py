"""Camera-aligned forward movement with timed movement and jump state."""
from __future__ import annotations
import math
from dataclasses import replace
from statistics import median
from task_control import task_time as time
from .models import NavigationError, NavigationStuckError, Point, Route
from .geometry import project_segment, segment_distance
from .vision import Locator, Pose
from .heading import HeadingContinuity


class SceneUnavailable(NavigationError):
    """A temporary scene/map interruption, recoverable by the route loop."""


class HeadingUncertain(NavigationError):
    """Position is usable; confirm camera direction locally without opening a map."""


def distance(a, b):
    return math.hypot(a.x-b.x, a.y-b.y)


def angle_delta(target, current):
    return (target-current+180) % 360-180


def _pause_generation(inputs):
    return getattr(inputs, "pause_generation", getattr(inputs, "generation", 0))


def crossed_waypoint(target, start, end, radius, pass_radius):
    """Use a wider pass margin only after crossing the target's closest point."""
    if start is None:
        return False
    closest = segment_distance(target, start, end)
    if closest <= radius:
        return True
    if closest > pass_radius:
        return False
    dx, dy = end.x-start.x, end.y-start.y
    # Both endpoints must straddle the perpendicular through the target. This
    # excludes standing still or merely approaching inside the wider margin.
    return ((target.x-start.x)*dx+(target.y-start.y)*dy > 0
            and (target.x-end.x)*dx+(target.y-end.y)*dy <= 0)


class Motion:
    """Cooperative timers: only the navigation thread may call Maa APIs.

    Agent reverse calls share a receive channel. A timer thread must not query
    stopping or submit inputs while another thread is receiving a screenshot.
    """
    JUMP_HOLD = .3
    JUMP_GAP = .1
    FLOAT_PRESS = .08
    JUMP_PENDING = {"first", "second_wait", "second_hold"}

    def __init__(self, inputs, event=lambda value: None):
        self.inputs = inputs
        self.event = event
        self.until = 0
        self.steer_keys = set()
        self.base_keys = ()
        self.lead_keys = None
        self.lead_until = 0.0
        self.last_turn_at = 0.0
        self.jump_state = "idle"
        self.jump_at = 0
        self.float_entered_at = 0.0
        self.float_canceled_at = 0.0
        self.forward_started = None
        self.forward_seconds = 0
        self.last_stop = None
        self.generation = _pause_generation(inputs)

    def _apply_keys(self, wanted_keys, now=None):
        now = time.monotonic() if now is None else now
        wanted = set(wanted_keys)
        # Release discontinued keys before pressing newly added keys so opposing
        # directions (e.g. A -> D or W -> S) are never held at the same time.
        for key in list(self.steer_keys - wanted):
            self.steer_keys.discard(key)
            if key in {"left", "right", "backward"}:
                self.last_turn_at = now
            self.inputs.key_up(key)
        for key in ("forward", "backward", "left", "right"):
            if key in wanted and key not in self.steer_keys:
                self.steer_keys.add(key)
                if key in {"left", "right", "backward"}:
                    self.last_turn_at = now
                self.inputs.key_down(key)
        if self.steer_keys & {"left", "right", "backward"}:
            self.last_turn_at = now

    def _finish_lead(self):
        if self.lead_keys is None:
            return
        self.lead_keys = None
        self.lead_until = 0.0
        if self.until:
            self._apply_keys(self.base_keys)

    def steer(self, keys, seconds, *, lead_keys=None, lead_seconds=0.0,
              trim_key=None, trim_seconds=0.0):
        if not math.isfinite(seconds) or seconds <= 0:
            raise NavigationError("移动时长无效。")
        generation = self.generation
        self.tick()
        if self.generation != generation:
            return
        now = time.monotonic()
        base = tuple(keys)
        if lead_keys is None and trim_key and trim_seconds > 0 and trim_key not in base:
            lead_keys = base + (trim_key,)
            lead_seconds = trim_seconds
        self.base_keys = base
        if lead_keys and lead_seconds > 0 and set(lead_keys) != set(base):
            self.lead_keys = tuple(lead_keys)
            self.lead_until = now + min(seconds, lead_seconds)
            active = self.lead_keys
        else:
            self.lead_keys = None
            self.lead_until = 0.0
            active = base
        if active and self.forward_started is None:
            self.forward_started = now
        self.until = now + seconds
        self._apply_keys(active, now)
        self.tick()

    def forward(self, seconds):
        if not math.isfinite(seconds) or seconds <= 0:
            raise NavigationError("前进时长无效。")
        self.steer(("forward",), seconds)

    def stop(self):
        deadline = self.until
        had_steer = bool(self.steer_keys) or self.forward_started is not None
        self.until = 0
        self.base_keys = ()
        self.lead_keys = None
        self.lead_until = 0.0
        started = time.monotonic()
        try:
            if self.steer_keys & {"left", "right", "backward"}:
                self.last_turn_at = started
            for key in list(self.steer_keys) or ["forward"]:
                self.steer_keys.discard(key)
                self.inputs.key_up(key)
        finally:
            self.steer_keys.clear()
            completed = time.monotonic()
            if had_steer and self.forward_started is not None:
                self.forward_seconds += completed-self.forward_started
                self.forward_started = None
                elapsed = completed-started
                self.last_stop = {"release_seconds": elapsed,
                                  "deadline_lateness": max(0, started-deadline) if deadline else 0}
                self.event({"type": "forward_stop", **self.last_stop})

    def forward_elapsed(self):
        return self.forward_seconds+(time.monotonic()-self.forward_started
                                     if self.forward_started is not None else 0)

    def jump_transition(self, state, reason, **details):
        now = time.monotonic()
        self.event({"type": "jump_transition", "before": self.jump_state, "after": state,
                    "phase_seconds": now-self.jump_at if self.jump_state != "idle" else None,
                    "reason": reason, **details})
        self.jump_state, self.jump_at = state, now
        if state == "air":
            self.float_entered_at = now
        elif state in {"first", "second_hold"}:
            self.float_canceled_at = 0.0

    def jump(self, wanted, walking, *, reason="route", walking_reliable=True, allow_air_jump=True):
        if not wanted:
            if self.jump_state == "idle":
                return False
            prev_state = self.jump_state
            # Cancel a queued second jump before tick can submit it.
            self.inputs.key_up("jump")
            now = time.monotonic()
            airborne = (not walking) if walking_reliable else (
                self.float_entered_at > 0 and now - self.float_entered_at < 2.15
            )
            cancel_float = prev_state == "air" and airborne
            float_age = now - self.float_entered_at if self.float_entered_at > 0 else 99.0
            self.jump_transition("idle", reason, walking=walking,
                                 walking_reliable=walking_reliable, cancel_float=cancel_float)
            if cancel_float:
                # Avoid tapping jump inside the initial ~0.30s float-entry montage window
                # where Witch Float's broom-mount notify could treat jump as air-dash.
                if 0.0 <= float_age < 0.30:
                    self.wait(min(0.18, 0.30 - float_age))
                self.inputs.key_down("jump")
                pressed_at = time.monotonic()
                try:
                    self.wait(self.FLOAT_PRESS)
                finally:
                    self.inputs.key_up("jump")
                    self.event({"type": "jump_cancel_input", "reason": reason,
                                "held_seconds": time.monotonic()-pressed_at})
                self.float_canceled_at = time.monotonic()
            elif prev_state in {"second_hold", "air"} or (
                prev_state in {"first", "second_wait"} and walking_reliable and not walking
            ):
                self.float_canceled_at = now
            return False
        self.tick()
        now = time.monotonic()
        ground_ready = walking or not walking_reliable or not allow_air_jump
        rejump_ready = (
            (walking and now - self.jump_at > .2)
            if walking_reliable
            else (self.float_entered_at > 0 and now - self.float_entered_at >= 2.7)
        )
        if (self.jump_state == "idle" and ground_ready) or (self.jump_state == "air" and rejump_ready):
            self.inputs.key_down("jump")
            self.jump_transition("first", "ground_jump_down", walking=walking,
                                 walking_reliable=walking_reliable)
            return True
        if self.jump_state == "idle" and walking_reliable and not walking:
            self.inputs.key_down("jump")
            self.jump_transition("second_hold", "air_jump_down", walking=walking,
                                 walking_reliable=walking_reliable)
            return True
        return False

    def tick(self):
        self.inputs.check()
        generation = _pause_generation(self.inputs)
        if generation != self.generation:
            self.generation = generation
            # A key-down RPC may itself have waited for focus restoration.
            # Release that input too; the caller must obtain a fresh pose.
            self.close()
            return
        now = time.monotonic()
        if self.steer_keys & {"left", "right", "backward"}:
            self.last_turn_at = now
        if self.until and now >= self.until:
            self.stop()
        elif self.lead_keys is not None and now >= self.lead_until:
            self._finish_lead()
        if self.jump_state == "first" and now-self.jump_at >= self.JUMP_HOLD:
            self.inputs.key_up("jump")
            self.jump_transition("second_wait", "first_jump_up")
        elif self.jump_state == "second_wait" and now-self.jump_at >= self.JUMP_GAP:
            self.inputs.key_down("jump")
            self.jump_transition("second_hold", "second_jump_down")
        elif self.jump_state == "second_hold" and now-self.jump_at >= self.FLOAT_PRESS:
            self.inputs.key_up("jump")
            self.jump_transition("air", "second_jump_up")

    def guard(self, blocking_seconds):
        """Keep cruising unless the next blocking read could miss release.

        Near the deadline, service the timers until W is released before
        starting that read. Jump timing remains independent of forward input.
        """
        self.tick()
        # Finish the short jump sequence before a blocking screenshot/OCR.
        # Each tick still services W's independent release deadline.
        while self.jump_state in self.JUMP_PENDING:
            self.wait(.01)
        if self.lead_keys is not None and self.lead_until - time.monotonic() <= blocking_seconds:
            while self.lead_keys is not None and self.until:
                self.wait(min(.015, max(0, self.lead_until - time.monotonic())))
        if self.until and self.until-time.monotonic() <= blocking_seconds:
            while self.until:
                self.wait(min(.02, max(0, self.until-time.monotonic())))

    def wait(self, seconds):
        deadline = time.monotonic()+seconds
        while True:
            self.tick()
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(.02, remaining))

    def close(self):
        self.until = 0
        self.base_keys = ()
        self.lead_keys = None
        self.lead_until = 0.0
        self.steer_keys.clear()
        if self.jump_state != "idle":
            prev_state = self.jump_state
            self.jump_transition("idle", "motion_close")
            if prev_state in {"second_hold", "air"}:
                self.float_canceled_at = time.monotonic()
        if self.forward_started is not None:
            self.forward_seconds += time.monotonic()-self.forward_started
        self.forward_started = None
        self.inputs.release()


class Navigator:
    CAMERA_SETTLE_SECONDS = .15
    CAMERA_PROBE_PIXELS = 180
    # World-map units per second (1 map unit = 0.90m). Unpacked ground Jog/Run
    # is 5.00..6.67 u/s; Floating suits span 4.33 u/s (Bubble/Fairy/Phoenix) up
    # to 5.67..6.56 u/s (Paper Crane Base / LV4). Separate floors prevent wall
    # contact from inflating hold durations while letting float speeds adapt.
    MIN_PLANNING_SPEED = 6.0
    MIN_TRAVEL_SPEED = 5.0
    DEFAULT_JUMP_SPEED = 5.0
    MIN_JUMP_SPEED = 4.2
    _WASD_SECTORS = (
        (0.0, ("forward",)),
        (45.0, ("forward", "right")),
        (90.0, ("right",)),
        (135.0, ("backward", "right")),
        (180.0, ("backward",)),
        (-135.0, ("backward", "left")),
        (-90.0, ("left",)),
        (-45.0, ("forward", "left")),
    )

    def __init__(self, locator: Locator, inputs, capture, stopped, pause, event=lambda value: None,
                 point_action=None, waypoint=None, walking=None, page_detector=None, point_prepare=None,
                 route_branch=None, relocalize=None, scene_recovery=None, overlay=None,
                 wasd_mode: bool | None = None):
        self.locator, self.inputs, self.capture, self.stopped, self._pause, self.event = locator, inputs, capture, stopped, pause, event
        self.point_action, self.waypoint = point_action, waypoint
        self.point_prepare = point_prepare
        self.route_branch = route_branch
        self.walking_detector = walking
        self.page_detector = page_detector
        self.overlay = overlay
        self.wasd_mode = bool(getattr(inputs, "window_only", False)) if wasd_mode is None else bool(wasd_mode)
        self.turn_ratio = None
        self.camera_probe_pixels = self.CAMERA_PROBE_PIXELS
        self.motion = None
        self.walking_reliable = False
        self._segment_from_air = False
        self.next_skip_check = 0
        self.next_toast_check = 0
        self.relocalize = relocalize
        self.scene_recovery = scene_recovery
        self.map_recoveries = 0
        self.relocalized = False
        self.read_costs = {}
        self.pose_at = time.monotonic()
        self.pose_input = 0.0
        self.heading_state = HeadingContinuity()
        self.heading_turn_at = -math.inf
        self.heading_turn_input = 0.0
        self.heading_ref_at = -math.inf
        self.heading_last_frame = None
        self.heading_evidence_saved = False
        self.heading_recheck_count = 0
        self.current_index = 0
        self.active_route = None
        self._wasd_sector_idx: int | None = None
        self._wasd_regime = None
        self._last_move_bearing: float | None = None
        self._last_move_at = -math.inf
        self._middle_cursor_pos = False
        self._middle_consecutive_failures = 0
        self._middle_disabled_until = -math.inf
        self._last_wasd_snap_at = -math.inf
        self._cursor_released = False

    @property
    def use_wasd_steering(self):
        """Use WASD steering only while no foreground mouse/camera phase is active."""
        if not self.wasd_mode:
            return False
        from task_control import active_session
        session = active_session()
        return not (session is not None and session._foreground_required_depth > 0)

    def pause(self, seconds):
        return self.motion.wait(seconds) if self.motion else self._pause(seconds)

    def read_state(self, name, callback):
        """Budget blocking recognition while retaining continuous travel."""
        if self.motion:
            self.motion.guard(self.read_costs.get(name, .2)*1.25+.03)
        begin = time.monotonic()
        try:
            return callback()
        finally:
            self.read_costs[name] = max(self.read_costs.get(name, 0)*.8, time.monotonic()-begin)
            if self.motion:
                self.motion.tick()

    def observe(self, hint, *, commit=True, reset_continuity=False, verify_heading=True):
        self.inputs.check()
        if self.heading_state.pending:
            # Camera input reaches the renderer after the RPC returns. A frame
            # captured during the existing camera-ease interval can still show
            # the old cone; it cannot validate or reject the commanded turn.
            remaining = self.heading_turn_at+self.CAMERA_SETTLE_SECONDS-time.monotonic()
            if remaining > 0 and self.pause(remaining) is False:
                raise NavigationError("已停止导航。")
        if self.motion:
            self.motion.guard(self.read_costs.get("observe", .3)*1.25+.03)
        begin = time.monotonic()
        input_begin = self.motion.forward_elapsed() if self.motion else 0
        frame = self.capture()
        if (frame is not None and self.overlay is not None
                and getattr(self.overlay, "is_presenting", lambda: False)()
                and int(frame.max()) == 0):
            # Verify whether the WDA_EXCLUDEFROMCAPTURE window caused the black
            # capture rather than permanently killing the overlay on teleport fades.
            self.overlay.hide()
            self.pause(0.08)
            begin = time.monotonic()
            retry = self.capture()
            if retry is not None and int(retry.max()) > 0:
                self.overlay.disable_for_capture()
                frame = retry
            elif retry is not None:
                frame = retry
        # A screenshot describes capture time, not the later OCR completion.
        # Midpoint is an estimate because Maa does not expose a frame timestamp.
        self.pose_at = (begin+time.monotonic())/2
        self.pose_input = (input_begin+self.motion.forward_elapsed())/2 if self.motion else 0
        if self.motion:
            self.motion.tick()
        if self.motion and self.point_action and time.monotonic() >= self.next_skip_check:
            self.next_skip_check = time.monotonic()+.2
            result = self.point_action.rt.recognize("MaaNikki_Energy_SkipReward", image=frame) if frame is not None else None
            self.motion.tick()
            if result and result.hit:
                # Handle collection overlays on the same Maa thread, before
                # any more movement; the route loop then verifies recovery.
                self.motion.close()
                self.inputs.check()
                if not self.point_action.rt.key("skip", .5):
                    raise NavigationError("未能跳过获取提示。")
                self.event({"type": "route_reward_skipped"})
                self.locator.heading_diagnostics = {"method": "reward_overlay"}
                anchor = hint if hint is not None else ((self.locator.previous.x, self.locator.previous.y)
                                                       if self.locator.previous else (0.0, 0.0))
                return Pose(anchor[0], anchor[1], 0.0, 0.0, None, 0.0, False)
        # Validate the page separately from map correlation. Loading/menu frames
        # must not update the position history, regardless of their match score.
        page_ready = self.page_detector(frame) if self.page_detector else self.inputs.ready()
        if not page_ready:
            if self.motion:
                self.motion.close()
            self.locator.last_frame = frame
            self.locator.heading_diagnostics = {"method": "page_unavailable"}
            previous = self.locator.previous
            anchor = (previous.x, previous.y) if previous else (hint or (0.0, 0.0))
            pose = Pose(anchor[0], anchor[1], 0.0, 0.0, None, 0.0, False)
        else:
            if self.locator.previous is None or reset_continuity:
                self.heading_state.reset()
                self.heading_last_frame = None
                self.heading_turn_at = self.heading_ref_at = self.pose_at
                self.heading_turn_input = input_begin
            pose = self.locator.locate(frame, hint, search_radius=26, commit=commit,
                                       reset_continuity=reset_continuity,
                                       progress=self.motion.tick if self.motion else None)
            if pose.heading is not None and verify_heading:
                evidence = self.heading_state.evidence(pose.heading)
                arrow = self.locator.heading_diagnostics.get("player_arrow", {}).get("heading")
                wasd_steering = self.use_wasd_steering
                straight_motion = (not wasd_steering) or (
                    self.motion is not None
                    and self.motion.steer_keys == {"forward"}
                    and self.pose_at - self.motion.last_turn_at >= .35
                )
                # The character follows W after the camera turns. Only use its
                # arrow once forward has been held beyond the turn transition;
                # standing arrows are stale camera evidence by definition.
                arrow_ready = bool(straight_motion and self.motion and self.motion.forward_started is not None
                                   and self.pose_at-self.motion.forward_started >= .25
                                   and self.pose_at-self.heading_turn_at >= .25
                                   and input_begin-self.heading_turn_input >= .25)
                corroborated = (arrow_ready and arrow is not None
                                and abs(angle_delta(arrow, pose.heading)) <= 8)
                wasd_camera_active = bool(
                    wasd_steering
                    and self.motion is not None
                    and (bool(self.motion.steer_keys & {"left", "right", "backward"})
                         or self.pose_at - self.motion.last_turn_at <= .85)
                )
                wasd_ref_at = self.heading_ref_at if self.heading_ref_at > -math.inf else self.heading_turn_at
                wasd_tol = (
                    max(18.0, min(68.0, 200.0 * max(.05, self.pose_at - wasd_ref_at) + 12.0))
                    if wasd_camera_active else None
                )
                wasd_agrees = bool(
                    wasd_tol is not None
                    and self.heading_state.reference is not None
                    and abs(angle_delta(pose.heading, self.heading_state.reference)) <= wasd_tol
                )
                self.locator.heading_diagnostics["continuity"] = {
                    **evidence, "arrow_ready": arrow_ready, "arrow_corroborated": corroborated,
                    **({"wasd_tol": round(wasd_tol, 1), "wasd_agrees": wasd_agrees}
                       if wasd_tol is not None else {})}
                if not evidence["agrees"] and not corroborated and not wasd_agrees:
                    self.locator.heading_diagnostics.update(method="heading_discontinuity",
                                                           raw_heading=pose.heading)
                    pose = replace(pose, heading=None)
                else:
                    self.heading_state.accept(pose.heading)
                    self.heading_ref_at = self.pose_at
                    self.heading_last_frame = frame
            if pose.heading is None:
                if self.motion:
                    self.motion.close()
                if pose.position_reliable:
                    self.save_heading_evidence(frame)
        if self.motion:
            self.motion.tick()
        if (page_ready and frame is not None and self.point_action is not None
                and hasattr(self.point_action, "poll_gingerbread_transit")
                and time.monotonic() >= self.next_toast_check):
            self.next_toast_check = time.monotonic() + 0.45
            self.point_action.poll_gingerbread_transit(frame, pose)
            if self.motion:
                self.motion.tick()
        if self.overlay:
            if page_ready and pose.position_reliable and self.active_route is not None:
                self.overlay.publish_route(self.active_route, self.current_index, pose)
            else:
                self.overlay.hide()
        self.event({"type": "pose", **pose.dict(), "heading_diagnostics": dict(self.locator.heading_diagnostics),
                    "capture_age_seconds": time.monotonic()-self.pose_at,
                    "input_since_capture": max(0, self.motion.forward_elapsed()-self.pose_input) if self.motion else 0})
        self.read_costs["observe"] = max(self.read_costs.get("observe", 0)*.8, time.monotonic()-begin)
        return pose

    def save_heading_evidence(self, frame):
        if self.heading_evidence_saved or not self.point_action:
            return
        self.heading_evidence_saved = True
        rt = self.point_action.rt
        if self.heading_last_frame is not None:
            rt.diagnostic("navigation_heading_reference", "方向复核前的可信画面",
                          image=self.heading_last_frame)
        rt.diagnostic("navigation_heading", "小地图方向需要复核", image=frame,
                      details=dict(self.locator.heading_diagnostics))

    @staticmethod
    def require_heading(pose):
        if pose.heading is None:
            if pose.position_reliable:
                raise HeadingUncertain("小地图方向需要重新确认。")
            raise SceneUnavailable("小地图位置或主界面需要重新确认。")

    def _ensure_cursor_released(self):
        if self._cursor_released or getattr(self.inputs, "window_only", False):
            return
        if not self.inputs.controller.post_click_key(18).wait().succeeded:
            raise NavigationError("未能解除游戏光标模式，尚未开始移动。")
        self._cursor_released = True

    def camera(self, pixels):
        from .controller import MAX_CAMERA_PIXELS, relative_camera
        # Send the calibrated displacement, then verify the actual heading.
        # Keep an input bound for unusual sensitivity/calibration results.
        part = max(-MAX_CAMERA_PIXELS, min(MAX_CAMERA_PIXELS, round(pixels)))
        generation = getattr(self.inputs, "generation", 0)
        self.inputs.check()
        if getattr(self.inputs, "generation", 0) != generation:
            raise SceneUnavailable("游戏已恢复前台，重新确认位置和方向。")
        if part:
            self._ensure_cursor_released()
            if not relative_camera(self.inputs.controller, part, 0):
                raise NavigationError("转动镜头失败。")
        if part:
            self.heading_state.command(part, self.turn_ratio)
            self.heading_turn_at = time.monotonic()
            self.heading_turn_input = self.motion.forward_elapsed() if self.motion else 0
            if self.overlay and self.turn_ratio:
                self.overlay.notify_camera_turn(part / self.turn_ratio)
        return part

    def stable_heading(self, pose, *, deadline=None):
        gen_fn = ((lambda: _pause_generation(self.inputs))
                  if self.use_wasd_steering else (lambda: getattr(self.inputs, "generation", 0)))
        generation = gen_fn()
        deadline = min(deadline, time.monotonic()+2) if deadline is not None else time.monotonic()+2
        stable_since = None
        origin = None
        lower = upper = 0.0
        while time.monotonic() < deadline:
            self.inputs.check()
            if self.stopped() or self.pause(.04) is False:
                raise NavigationError("已停止导航。")
            pose = self.observe((pose.x, pose.y), verify_heading=False)
            if gen_fn() != generation:
                raise SceneUnavailable("游戏已恢复前台，重新确认位置和方向。")
            if not pose.position_reliable:
                raise SceneUnavailable("小地图位置或主界面需要重新确认。")
            if pose.heading is None:
                stable_since = origin = None
                continue
            if time.monotonic() >= deadline:
                break
            now = self.pose_at
            offset = angle_delta(pose.heading, origin) if origin is not None else 0
            lower, upper = min(lower, offset), max(upper, offset)
            if stable_since is None or upper-lower >= 5:
                stable_since, origin, lower, upper = now, pose.heading, 0.0, 0.0
            # Confirm a settled camera interval, rather than counting/voting
            # frames. The interval covers the existing .15-second camera ease;
            # any unsupported read or angular excursion starts it over.
            if now-stable_since >= .2:
                evidence = self.heading_state.accept(pose.heading)
                self.event({"type": "heading_confirmed", **evidence,
                            "heading": pose.heading, "stable_seconds": now-stable_since})
                self.heading_last_frame = self.locator.last_frame
                self.heading_evidence_saved = False
                return pose
        self.event({"type": "heading_unstable", "diagnostics": dict(self.locator.heading_diagnostics)})
        raise HeadingUncertain("小地图方向未能稳定，已停止移动。")

    def recheck_heading(self, pose, deadline):
        """Keep the current position; reacquire a possibly user-changed camera."""
        if self.motion:
            self.motion.close()
        else:
            self.inputs.release()
        self.heading_recheck_count += 1
        if self.heading_recheck_count > 3:
            raise NavigationError("多次复核方向后仍无法继续前进，已停止路线。")
        self.event({"type": "heading_recheck_started", "attempt": self.heading_recheck_count})
        try:
            return self.stable_heading(pose, deadline=deadline)
        except HeadingUncertain:
            diagnostics = self.locator.heading_diagnostics
            method = diagnostics.get("method")
            probe_eligible = (
                (method == "cone_boundaries" and diagnostics.get("edges_supported") is False)
                or method == "heading_uncertain"
            )
            if (time.monotonic() >= deadline
                    or not diagnostics.get("position_reliable")
                    or not probe_eligible):
                raise
            if not self.inputs.ready():
                raise SceneUnavailable("方向复核时主界面已变化。")
            # At a fixed position, a cone ray can remain unreadable over bright
            # terrain. Re-reading the same angle cannot restore its missing
            # boundary. Reuse the calibration probe while stationary to expose
            # another angle; both rays must still pass the normal checks.
            probe_scale = 2 if self.heading_recheck_count >= 3 else 1
            if self.use_wasd_steering:
                direction = "left" if self.heading_recheck_count % 2 == 0 else "right"
                if self._can_middle_align():
                    hold_seconds = .15 if probe_scale == 1 else .22
                    self.event({"type": "heading_visibility_probe",
                                "attempt": self.heading_recheck_count,
                                "key": direction, "seconds": hold_seconds,
                                "middle_align": True,
                                "diagnostics": dict(diagnostics)})
                    self._hold_wasd_chord((direction,), hold_seconds)
                    clicked = self._press_middle_align(with_cursor_pos=bool(self._middle_cursor_pos))
                    if not clicked and not self._middle_cursor_pos:
                        clicked = self._press_middle_align(with_cursor_pos=True)
                        if clicked:
                            self._middle_cursor_pos = True
                    self.heading_state.pending = False
                    self.heading_turn_at = time.monotonic()
                    self.heading_turn_input = self.motion.forward_elapsed() if self.motion else 0.0
                    if self.pause(.18) is False:
                        raise NavigationError("已停止导航。")
                    confirmed = self.stable_heading(pose, deadline=deadline)
                    self.heading_ref_at = self.pose_at
                    self.heading_turn_at = self.pose_at
                    return confirmed
                hold_seconds = .22 * probe_scale
                self.event({"type": "heading_visibility_probe",
                            "attempt": self.heading_recheck_count,
                            "key": direction, "seconds": hold_seconds,
                            "diagnostics": dict(diagnostics)})
                self.inputs.hold(direction, hold_seconds, restore=False)
                self.heading_turn_at = time.monotonic()
                self.heading_turn_input = self.motion.forward_elapsed() if self.motion else 0.0
                return self.stable_heading(pose, deadline=deadline)
            probe = self.camera_probe_pixels * probe_scale * (-1 if self.heading_recheck_count % 2 == 0 else 1)
            self.event({"type": "heading_visibility_probe",
                        "attempt": self.heading_recheck_count,
                        "pixels": probe,
                        "diagnostics": dict(diagnostics)})
            self.camera(probe)
            return self.stable_heading(pose, deadline=deadline)

    def calibrate(self, pose):
        ratios = []
        probe_pixels = self.CAMERA_PROBE_PIXELS
        for _ in range(6):
            before = pose.heading
            pixels = self.camera(probe_pixels)
            self.pause(.3)
            pose = self.stable_heading(pose)
            delta = angle_delta(pose.heading, before)
            self.event({"type": "camera_probe", "pixels": pixels, "rotation": delta})
            if not 8 <= abs(delta) <= 90:
                ratios.clear()
                probe_pixels = (min(1440, probe_pixels*2) if abs(delta) < 8
                                else max(22, probe_pixels//2))
                continue
            ratio = pixels/delta
            if ratios and (ratio*median(ratios) <= 0 or not .75 <= abs(ratio/median(ratios)) <= 1.33):
                ratios.clear()
            ratios.append(ratio)
            if len(ratios) >= 3 or (len(ratios) == 2 and
                                    max(map(abs, ratios))/min(map(abs, ratios)) <= 1.15):
                self.turn_ratio = median(ratios)
                self.camera_probe_pixels = probe_pixels
                self.event({"type": "camera_calibrated", "pixels_per_degree": self.turn_ratio})
                return pose
        raise NavigationError("镜头校准结果不稳定，请检查游戏前台和小地图显示。")

    def travel_tolerance(self, remaining, radius):
        """Account for small position changes in the near-point bearing."""
        spec = self.locator.spec
        if remaining > radius+2:
            return spec.heading_tolerance_far
        # Cap lateral uncertainty at a quarter of the actual arrival radius.
        # Arrival itself still uses the unchanged radius and settled-pose check.
        slack = min(spec.heading_position_slack, radius*.25)
        uncertainty = math.degrees(math.atan2(slack, max(remaining, .01)))
        return max(spec.heading_tolerance_near, min(5.0, uncertainty))

    def _can_middle_align(self):
        """Return whether middle-click camera alignment is supported and not in cooldown."""
        if time.monotonic() < self._middle_disabled_until:
            return False
        if hasattr(self.inputs, "click_window_middle"):
            return True
        fg = getattr(self.inputs, "foreground", None)
        return bool(hasattr(self.inputs, "click_mouse") and callable(fg) and fg())

    def _press_middle_align(self, *, with_cursor_pos: bool) -> bool:
        """Click the middle mouse button without stealing OS focus."""
        if hasattr(self.inputs, "click_window_middle"):
            return bool(self.inputs.click_window_middle(self.pause, with_cursor_pos=with_cursor_pos))
        fg = getattr(self.inputs, "foreground", None)
        if hasattr(self.inputs, "click_mouse") and callable(fg) and fg():
            return bool(self.inputs.click_mouse(2))
        return False

    @classmethod
    def _nearest_wasd_sector(cls, delta: float):
        """Pick the closest 45-degree WASD chord for relative bearing `delta`."""
        return min(cls._WASD_SECTORS, key=lambda item: abs(angle_delta(delta, item[0])))

    def _hold_wasd_chord(self, chord, seconds: float):
        """Hold a 1- or 2-key WASD direction chord cleanly and release all movement keys afterward."""
        if not math.isfinite(seconds) or seconds <= 0:
            return
        if self.motion is not None:
            self.motion.steer(chord, seconds + 0.05)
            try:
                self.motion.wait(seconds)
            finally:
                self.motion.stop()
            return
        self.inputs.release()
        if hasattr(self.inputs, "_down") and hasattr(self.inputs, "bindings"):
            try:
                for key in chord:
                    self.inputs._down(self.inputs.bindings.get(key), restore=False)
                self.pause(seconds)
            finally:
                self.inputs.release()
            return
        self.inputs.hold(chord[0], seconds, restore=False)

    def _wasd_snap_camera(self, target_heading, pose, *, sector_deg=None, chord=None,
                          pulse=None, stationary=False, deadline=None):
        """Orient the character with WASD, then middle-click to align the camera to character facing."""
        self.require_heading(pose)
        before_heading = pose.heading
        delta = angle_delta(target_heading, before_heading)
        if sector_deg is None or chord is None:
            sector_deg, chord = self._nearest_wasd_sector(delta)
        if pulse is None:
            pulse = 0.10 + 0.00083 * abs(sector_deg)
        generation = _pause_generation(self.inputs)
        self._hold_wasd_chord(chord, pulse)
        if _pause_generation(self.inputs) != generation:
            raise SceneUnavailable("游戏窗口状态已变化，重新确认位置和方向。")
        fg = getattr(self.inputs, "foreground", None)
        before_fg = bool(callable(fg) and fg())
        major_snap = abs(sector_deg) >= 35.0
        after = pose
        snapped = False
        for attempt in range(2):
            use_cursor = bool(self._middle_cursor_pos)
            clicked = self._press_middle_align(with_cursor_pos=use_cursor)
            if not clicked:
                if attempt == 0 and not before_fg and not use_cursor and major_snap:
                    self._middle_cursor_pos = True
                    continue
                if major_snap:
                    self._middle_consecutive_failures += 1
                    if self._middle_consecutive_failures >= 2:
                        self._middle_disabled_until = time.monotonic() + 20.0
                return after, False
            self.heading_state.pending = False
            self.heading_turn_at = time.monotonic()
            self.heading_turn_input = self.motion.forward_elapsed() if self.motion else 0.0
            if self.pause(0.18) is False:
                raise NavigationError("已停止导航。")
            after = self.stable_heading(pose, deadline=deadline)
            if _pause_generation(self.inputs) != generation:
                raise SceneUnavailable("游戏窗口状态已变化，重新确认位置和方向。")
            actual_rot = angle_delta(after.heading, before_heading)
            err_before = abs(delta)
            err_after = abs(angle_delta(target_heading, after.heading))
            if major_snap:
                rotated_enough = (
                    err_after <= err_before - 15.0
                    or (abs(actual_rot) >= 18.0 and (actual_rot * sector_deg > 0 or abs(sector_deg) >= 150.0))
                )
            else:
                rotated_enough = abs(actual_rot) >= 3.0 or err_after <= err_before - 2.0
            if rotated_enough:
                snapped = True
                break
            if major_snap and attempt == 0 and not before_fg and not use_cursor:
                # UnrealWindow may ignore middle-click when the physical cursor lies outside the client rect.
                self._middle_cursor_pos = True
                self._hold_wasd_chord(chord, min(pulse, 0.12))
                if _pause_generation(self.inputs) != generation:
                    raise SceneUnavailable("游戏窗口状态已变化，重新确认位置和方向。")
                continue
            if major_snap:
                self._middle_consecutive_failures += 1
                if self._middle_consecutive_failures >= 2:
                    self._middle_disabled_until = time.monotonic() + 20.0
            return after, False
        if snapped:
            if major_snap:
                self._middle_consecutive_failures = 0
            self.heading_ref_at = self.pose_at
            self.heading_turn_at = self.pose_at
            self.heading_turn_input = self.motion.forward_elapsed() if self.motion else 0.0
            if not stationary:
                self._last_wasd_snap_at = time.monotonic()
            actual_rot = angle_delta(after.heading, before_heading)
            if self.overlay:
                self.overlay.notify_camera_turn(actual_rot)
            self.event({
                "type": "wasd_middle_snap",
                "index": self.current_index,
                "target": target_heading,
                "before": before_heading,
                "after": after.heading,
                "sector": sector_deg,
                "chord": list(chord),
                "pulse": round(pulse, 3),
                "rotation": round(actual_rot, 1),
                "remaining_error": round(abs(angle_delta(target_heading, after.heading)), 1),
                "cursor_pos": bool(self._middle_cursor_pos),
                "stationary": stationary,
            })
        return after, snapped

    def _wasd_turn_stationary(self, target_heading, pose, *, tolerance=5):
        if self.motion:
            self.motion.stop()
        else:
            self.inputs.release()
        pose = self.stable_heading(pose)
        anchor = (pose.x, pose.y)
        turn_sign = None
        deadline = time.monotonic() + 15
        arrow = self.locator.heading_diagnostics.get("player_arrow", {}).get("heading")
        body_aligned = arrow is not None and abs(angle_delta(arrow, pose.heading)) <= 18.0
        micro_gain = 1.0
        prev_micro_delta = None
        sign_flips = 0
        micro_attempts = 0
        no_assist_pulses = 0
        try:
            for _ in range(16):
                self.inputs.check()
                delta = angle_delta(target_heading, pose.heading)
                if abs(delta) <= tolerance:
                    return pose
                if time.monotonic() >= deadline:
                    break
                if self._can_middle_align():
                    if abs(delta) > 22.5:
                        sector_deg, chord = self._nearest_wasd_sector(delta)
                        after, ok = self._wasd_snap_camera(
                            target_heading,
                            pose,
                            sector_deg=sector_deg,
                            chord=chord,
                            stationary=True,
                            deadline=deadline,
                        )
                        pose = after
                        if math.hypot(pose.x - anchor[0], pose.y - anchor[1]) > 2.2:
                            raise SceneUnavailable("原地按键转向时位置偏移过大，重新确认路线。")
                        if ok:
                            body_aligned = True
                            prev_micro_delta = None
                            if abs(angle_delta(target_heading, pose.heading)) <= tolerance:
                                return pose
                            continue
                    else:
                        if abs(delta) <= max(tolerance, 12.0) and (sign_flips >= 2 or micro_attempts >= 3):
                            return pose
                        if not body_aligned:
                            self._hold_wasd_chord(("forward",), 0.12)
                            body_aligned = True
                        if (
                            prev_micro_delta is not None
                            and delta * prev_micro_delta < 0
                            and abs(delta) + abs(prev_micro_delta) < 180
                        ):
                            sign_flips += 1
                            micro_gain = max(0.35, micro_gain * 0.6)
                        prev_micro_delta = delta
                        micro_attempts += 1
                        side = "right" if delta > 0 else "left"
                        pulse = max(0.028, min(0.08, abs(delta) / 360.0 * micro_gain))
                        after, ok = self._wasd_snap_camera(
                            target_heading,
                            pose,
                            sector_deg=delta,
                            chord=("forward", side),
                            pulse=pulse,
                            stationary=True,
                            deadline=deadline,
                        )
                        pose = after
                        if math.hypot(pose.x - anchor[0], pose.y - anchor[1]) > 2.2:
                            raise SceneUnavailable("原地按键转向时位置偏移过大，重新确认路线。")
                        if abs(angle_delta(target_heading, pose.heading)) <= tolerance:
                            return pose
                        if ok:
                            continue
                        if abs(angle_delta(target_heading, pose.heading)) <= max(tolerance, 12.0):
                            return pose
                        if micro_gain < 1.5:
                            micro_gain *= 1.35
                            continue
                tol = max(tolerance, 15.0)
                if abs(delta) <= tol:
                    return pose
                if abs(delta) >= 145.0:
                    if turn_sign is None:
                        turn_sign = 1 if delta >= 0 else -1
                elif abs(delta) < 120.0:
                    turn_sign = None
                effective_sign = turn_sign if turn_sign is not None else (1 if delta >= 0 else -1)
                side = "right" if effective_sign > 0 else "left"
                pulse = max(.06, min(.26, abs(delta) / 140.0))
                before = pose.heading
                self.inputs.hold(side, pulse, restore=False)
                self.heading_turn_at = time.monotonic()
                self.heading_turn_input = self.motion.forward_elapsed() if self.motion else 0.0
                self.pause(.16)
                pose = self.stable_heading(pose, deadline=deadline)
                actual_rot = angle_delta(pose.heading, before)
                if math.hypot(pose.x - anchor[0], pose.y - anchor[1]) > 2.2:
                    raise SceneUnavailable("原地按键转向时位置偏移过大，重新确认路线。")
                self.event({"type": "wasd_turn", "index": self.current_index,
                            "target": target_heading, "before": before, "after": pose.heading,
                            "rotation": actual_rot,
                            "key": side, "seconds": pulse, "stationary": True,
                            "remaining_error": abs(angle_delta(target_heading, pose.heading)),
                            "tolerance": tol})
                if abs(angle_delta(target_heading, pose.heading)) <= tol:
                    return pose
                if abs(actual_rot) < 1.5:
                    no_assist_pulses += 1
                    if no_assist_pulses >= 2:
                        if (
                            not getattr(self.inputs, "window_only", False)
                            and callable(getattr(self.inputs, "foreground", None))
                            and self.inputs.foreground()
                        ):
                            return self.turn(target_heading, pose, stationary=True, tolerance=tolerance, _allow_wasd=False)
                        raise NavigationError("后台对齐动作朝向需要鼠标中键重置视角，当前镜头水平辅助已关闭或未响应。")
                else:
                    no_assist_pulses = 0
            if abs(angle_delta(target_heading, pose.heading)) <= max(tolerance, 15.0):
                return pose
            raise NavigationError("WASD 未能调整至目标朝向，已停止移动。")
        except BaseException:
            self.inputs.release()
            raise

    def turn(self, target_heading, pose, *, stationary=False, tolerance=5, _allow_wasd=True):
        if _allow_wasd and self.use_wasd_steering:
            return self._wasd_turn_stationary(target_heading, pose, tolerance=tolerance)
        generation = getattr(self.inputs, "generation", 0)
        previous_delta = None
        correction_gain = 1.0
        self.require_heading(pose)
        if abs(angle_delta(target_heading, pose.heading)) <= tolerance:
            return pose
        if (not stationary and self.turn_ratio is not None
                and self.motion and self.motion.until
                and abs(angle_delta(target_heading, pose.heading)) < 45):
            # Small corrections keep forward held while there is sufficient
            # travel left to complete the camera update and its observation.
            self.motion.guard(self.read_costs.get("observe", .3)*1.25+.15)
        if (not stationary and self.turn_ratio is not None
                and self.motion and self.motion.until
                and abs(angle_delta(target_heading, pose.heading)) < 45):
            if getattr(self.inputs, "generation", 0) != generation:
                raise SceneUnavailable("游戏已恢复前台，重新确认位置和方向。")
            delta = angle_delta(target_heading, pose.heading)
            previous_delta = delta
            pixels = self.camera(delta*self.turn_ratio)
            expected_rotation = pixels/self.turn_ratio
            after = self.observe((pose.x, pose.y))
            if getattr(self.inputs, "generation", 0) != generation:
                raise SceneUnavailable("游戏已恢复前台，重新确认位置和方向。")
            if (after.heading is None and after.position_reliable and self.heading_state.pending
                    and self.locator.heading_diagnostics.get("method") == "heading_discontinuity"):
                # Finish observing this commanded turn while stationary. A
                # delayed renderer update is part of the turn, not a new
                # uncommanded direction failure consuming route recoveries.
                # stable_heading also rejects unsupported/continuously moving
                # cones and invalidates calibration feedback for external turns.
                after = self.stable_heading(after)
            self.require_heading(after)
            self.event({"type": "camera_turn", "index": self.current_index,
                        "target": target_heading, "before": pose.heading,
                        "after": after.heading, "pixels": pixels,
                        "rotation": angle_delta(after.heading, pose.heading), "gain": correction_gain,
                        "expected_rotation": expected_rotation,
                        "feedback_valid": self.heading_state.feedback_valid,
                        "stationary": False, "tolerance": tolerance})
            if abs(angle_delta(target_heading, after.heading)) <= tolerance:
                return after
            pose = after
        if self.motion:
            self.motion.stop()
        else:
            self.inputs.key_up("forward")
        try:
            pose = self.stable_heading(pose)
            if self.turn_ratio is None:
                pose = self.calibrate(pose)
            deadline = time.monotonic()+25
            unresponsive = 0
            for _ in range(24):
                self.inputs.check()
                delta = angle_delta(target_heading, pose.heading)
                if abs(delta) <= tolerance:
                    return pose
                if time.monotonic() >= deadline:
                    break
                # Reduce corrections when feedback crosses the target (0 deg),
                # ignoring sign flips across the +/-180-degree antipodal seam.
                if previous_delta is not None and delta*previous_delta < 0 and abs(delta)+abs(previous_delta) < 180:
                    correction_gain = max(.125, correction_gain*.5)
                elif previous_delta is not None and delta*previous_delta > 0 and abs(delta) < abs(previous_delta):
                    correction_gain = min(1.0, correction_gain*1.5)
                previous_delta = delta
                before = pose.heading
                step_deg = max(-90.0, min(90.0, delta*correction_gain))
                pixels = self.camera(step_deg*self.turn_ratio)
                expected_rotation = pixels/self.turn_ratio
                pose = self.stable_heading(pose)
                actual = angle_delta(pose.heading, before)
                self.event({"type": "camera_turn", "index": self.current_index,
                            "target": target_heading, "before": before,
                            "after": pose.heading, "pixels": pixels, "rotation": actual,
                            "gain": correction_gain, "stationary": True,
                            "expected_rotation": expected_rotation,
                            "feedback_valid": self.heading_state.feedback_valid,
                            "remaining_error": abs(angle_delta(target_heading, pose.heading)),
                            "tolerance": tolerance})
                # A small final turn can reach tolerance on the same read that
                # would otherwise trip the accumulated no-response counter.
                if abs(angle_delta(target_heading, pose.heading)) <= tolerance:
                    return pose
                # Cone headings have roughly 0.88-degree bins. A one-bin move
                # is real feedback, even though it is smaller than one degree.
                if abs(actual) < .5:
                    unresponsive += 1
                    if unresponsive >= 3:
                        raise NavigationError("镜头连续没有响应转向，已停止移动。")
                else:
                    unresponsive = 0
                    measured = pixels/actual
                    if (self.heading_state.feedback_valid and measured*self.turn_ratio > 0 and 8 <= abs(actual) <= 90
                            and .75 <= abs(measured/self.turn_ratio) <= 1.33):
                        self.turn_ratio = .8*self.turn_ratio+.2*measured
            raise NavigationError("镜头未能稳定朝向路线，已停止移动。")
        except BaseException:
            self.inputs.release()
            raise

    def face_heading(self, heading, *, tolerance=5):
        """Turn the camera, then align the stationary character for an action."""
        if type(heading) not in (int, float) or not math.isfinite(heading) or not 0 <= heading < 360:
            raise NavigationError("动作朝向须为 0–360 度范围内的有效角度（不含 360）。")
        self.inputs.check()
        gen_fn = ((lambda: _pause_generation(self.inputs))
                  if self.use_wasd_steering else (lambda: getattr(self.inputs, "generation", 0)))
        generation = gen_fn()
        if self.motion:
            self.motion.stop()
            self.motion.jump(False, self.walking(), reason="point_facing",
                             walking_reliable=self.walking_reliable)
            if self.overlay:
                self.overlay.notify_motion(0.0, 0.0, False, cancel_jump=True)
        else:
            self.inputs.release()
        previous = self.locator.previous
        if previous is None:
            raise NavigationError("缺少已确认的位置，未调整动作朝向。")
        pose = self.observe((previous.x, previous.y))
        pose = self.turn(heading, pose, stationary=True, tolerance=tolerance)
        if gen_fn() != generation:
            raise SceneUnavailable("游戏已恢复前台，重新确认位置和方向。")
        timing = self.inputs.face_camera()
        after = self.observe((pose.x, pose.y))
        error = abs(angle_delta(heading, after.heading)) if after.heading is not None else None
        effective_tol = (
            max(tolerance, 12.0 if self._can_middle_align() else 15.0)
            if self.use_wasd_steering else tolerance
        )
        confirmed = (gen_fn() == generation
                     and error is not None and error <= effective_tol)
        self.event({"type": "character_facing", "source": "route_heading", "target": heading,
                    "camera_heading": after.heading, "camera_error": error,
                    "before": [pose.x, pose.y], "after": [after.x, after.y],
                    "displacement": distance(pose, after), "camera_confirmed": confirmed, **timing})
        if not confirmed:
            raise SceneUnavailable("动作前的镜头朝向未能保持稳定，未开始交互。")
        return after

    def walking(self):
        if self.walking_detector is not None:
            val = bool(self.read_state("walking", self.walking_detector))
        else:
            val = bool(not self.point_action or self.read_state("walking", self.point_action.rt.ui.walking))
        if val and (self.motion is None or self.motion.jump_state == "idle"):
            self.walking_reliable = True
        return val

    def reacquire_position(self, anchor, deadline, *, force_map=False):
        self.motion.close()
        self.relocalized = False
        reason = ("page_unavailable" if not self.inputs.ready() else
                  self.locator.heading_diagnostics.get("method", "camera_unavailable"))
        self.event({"type": "position_recheck_started", "reason": reason})
        end = min(deadline, time.monotonic()+15)
        previous = None
        steady_since = None
        page_ready = False
        failures = 0
        while time.monotonic() < end:
            self.inputs.check()
            if self.stopped():
                raise NavigationError("已停止导航。")
            self.pause(.25)
            if time.monotonic() >= end:
                break
            page_ready = self.inputs.ready()
            if not page_ready:
                if self.point_action and self.point_action.rt.ui.dismiss_reward() is False:
                    raise NavigationError("未能跳过获取提示。")
                if self.scene_recovery:
                    revived = self.scene_recovery()
                    if revived is not None:
                        if revived:
                            # Respawn may change height or location. Let the
                            # route owner restart from its teleport checkpoint.
                            raise NavigationStuckError("角色倒下后已复活，需要从起点重新运行路线。")
                        raise NavigationError("角色倒下后未能完成复活，本条路线停止。")
                if self.point_action and self.point_action.rt.hit("MaaNikki_Energy_SkipReward"):
                    if not self.point_action.rt.key("skip", .5):
                        raise NavigationError("未能跳过获取提示。")
                previous = None
                steady_since = None
                continue
            # Waiting on unchanged terrain cannot repair a lost local search.
            # Use a freshly opened, player-centered map as an independent anchor.
            if self.relocalize and not self.relocalized and (force_map or failures >= 2):
                if self.map_recoveries >= 3:
                    raise NavigationError("本路线已多次打开地图恢复位置，仍未能稳定前进。")
                self.map_recoveries += 1
                x, y = self.relocalize()
                anchor = Pose(x, y, 0.0, 0.0, None, 0.0)
                self.relocalized = True
                previous = None
                steady_since = None
                end = min(deadline, time.monotonic()+15)
                self.event({"type": "map_position_anchor", "x": x, "y": y,
                            "attempt": self.map_recoveries})
                if self.stopped() or time.monotonic() >= deadline:
                    raise NavigationError("导航已停止或超时。")
            pose = self.observe((anchor.x, anchor.y), commit=False,
                                reset_continuity=self.relocalized, verify_heading=False)
            if pose.heading is not None:
                now = time.monotonic()
                if (previous is None or distance(pose, previous) >= 2
                        or abs(angle_delta(pose.heading, previous.heading)) >= 5):
                    steady_since = now
                if steady_since is not None and now-steady_since >= .2:
                    anchor_distance = distance(pose, anchor)
                    if not self.relocalized and anchor_distance > 4:
                        # A repeated candidate is stable, not independently
                        # verified. Once movement has stopped, waiting must not
                        # turn a rejected position jump into a trusted anchor.
                        self.event({"type": "scene_anchor_conflict",
                                    "anchor": [anchor.x, anchor.y],
                                    "candidate": [pose.x, pose.y],
                                    "distance": anchor_distance,
                                    "verification": "bigmap" if self.relocalize else "unavailable"})
                        if not self.relocalize:
                            raise NavigationError("小地图恢复位置与中断前不一致，缺少大地图复核入口。")
                        force_map = True
                        previous = None
                        steady_since = None
                        continue
                    if self.relocalized and anchor_distance > 6:
                        raise NavigationError("大地图角色位置与小地图定位不一致，未继续移动。")
                    # Tentative reads never replace the last trusted position.
                    self.locator.accept(pose)
                    self.heading_state.accept(pose.heading)
                    self.heading_last_frame = self.locator.last_frame
                    self.heading_evidence_saved = False
                    self.event({"type": "position_confirmed", **pose.dict(),
                                "verification": "bigmap" if self.relocalized else "local_continuity",
                                "anchor_distance": anchor_distance})
                    return pose
                previous = pose
            else:
                previous = None
                steady_since = None
                failures += int(not pose.position_reliable)
        if not page_ready:
            raise NavigationError("等待返回可移动的主界面超时。")
        diagnostics = self.locator.heading_diagnostics
        if diagnostics.get("method") == "position_unavailable":
            matches = diagnostics.get("position_matches", {})
            cone = matches.get("outside_camera_cone", {})
            cone_score = cone.get("score")
            cone_message = (f"，光锥外 {cone_score:.4f}" if cone_score is not None else "")
            raise NavigationError(
                "主界面已恢复，但小地图位置匹配未通过校验"
                f"（灰度相似度 {matches.get('luma', {}).get('score', 0):.4f}，"
                f"地形边缘 {matches.get('known_terrain_edges', {}).get('score', 0):.4f}{cone_message}）。")
        if diagnostics.get("method") == "heading_uncertain":
            raise NavigationError("主界面已恢复，但小地图视角方向识别置信度不足。")
        raise NavigationError("主界面已恢复，但小地图位置或视角方向未能恢复稳定。")

    def rejoin_point(self, route, index, pose):
        """Return to the current segment without crediting or skipping a task."""
        a, b = route.points[max(0, index-1)], route.points[index]
        # Teleport endpoints do not describe a walkable segment from the
        # preceding point; once prepared, return directly to that endpoint.
        if b.action == "teleport":
            a = b
        x, y = project_segment(pose, a, b)
        point = Point(x, y, min(b.radius, 2), b.jump)
        self.event({"type": "route_rejoin", "index": index,
                    "from": [pose.x, pose.y], "target": [point.x, point.y],
                    "distance": distance(pose, point)})
        return point

    def _initial_speeds(self):
        return {"travel": [self.MIN_PLANNING_SPEED], "jump": [self.DEFAULT_JUMP_SPEED]}

    def _planning_speed(self, mode: str, samples):
        floor = self.MIN_JUMP_SPEED if mode == "jump" else self.MIN_TRAVEL_SPEED
        return max(floor, median(samples))

    def _air_brake_lead(self, jumping: bool, velocity: float, *, need_stop: bool = False) -> float:
        """Reserve extra release lead for fast floating suits (e.g. Paper Crane 5.67..6.56 u/s)."""
        if not jumping or not need_stop:
            return .06
        return .06 + max(0.0, min(0.16, (velocity - 4.4) * 0.07))

    def _pre_jump_align_seconds(self, bearing: float, walking: bool, remaining: float, radius: float) -> float:
        """Align body facing on the ground (540..720 deg/s) before takeoff so slow-turning
        floating suits (Paper Crane / Witch at 120 deg/s) do not bank wide in mid-air."""
        if self.motion is None or self.motion.jump_state != "idle":
            return 0.0
        if self.walking_reliable and not walking:
            return 0.0
        if remaining <= radius + 0.35:
            return 0.0
        now = time.monotonic()
        if self._last_move_bearing is not None and now - self._last_move_at <= 0.45:
            turn_deg = abs(angle_delta(bearing, self._last_move_bearing))
        else:
            arrow = self.locator.heading_diagnostics.get("player_arrow", {}).get("heading")
            turn_deg = abs(angle_delta(bearing, arrow)) if arrow is not None else 60.0
        if turn_deg < 25.0:
            return 0.0
        return min(0.15, max(0.07, turn_deg / 540.0 + 0.03))

    def _float_landed(self, timeout=.80):
        motion = self.motion
        if motion is None or motion.float_canceled_at <= 0:
            return True
        if not self.walking_reliable:
            return time.monotonic() - motion.float_canceled_at >= min(.35, timeout)
        if time.monotonic() - motion.float_canceled_at >= timeout:
            motion.float_canceled_at = 0.0
            return True
        if self.walking():
            motion.float_canceled_at = 0.0
            return True
        return False

    def _wait_for_landing(self, timeout=.80, *, generation=None, pause_generation=None, deadline=None):
        motion = self.motion
        if motion is None or motion.float_canceled_at <= 0:
            return True
        for _ in range(12):
            if self._float_landed(timeout):
                motion.float_canceled_at = 0.0
                return True
            if self.stopped() or (deadline is not None and time.monotonic() >= deadline):
                raise NavigationError("导航已停止或超时。")
            if generation is not None and getattr(self.inputs, "generation", 0) != generation:
                return False
            if pause_generation is not None and _pause_generation(self.inputs) != pause_generation:
                return False
            self.pause(.08)
        motion.float_canceled_at = 0.0
        return True

    def _finish_step_motion(self, recovery, duration, generation, *, max_recovery_wait=1.15):
        motion = self.motion
        if recovery:
            self.pause(min(max_recovery_wait, duration + .20))
            motion.stop()
            motion.jump(False, self.walking(), reason="unstuck_complete",
                        walking_reliable=self.walking_reliable)
            if self.overlay:
                self.overlay.notify_motion(0.0, 0.0, False, cancel_jump=True)
            if motion.float_canceled_at > 0:
                self._wait_for_landing(.70, generation=generation)
            self._segment_from_air = False
        elif motion.jump_state in motion.JUMP_PENDING:
            # Preserve the original full-height jump and second jump timing.
            # The independent forward deadline can release W during this wait.
            while motion.jump_state in motion.JUMP_PENDING:
                self.pause(.01)
        else:
            self.pause(.01)

    def advance(self, pose, target, radius, speeds, *, recovery=False, detour_deg=0.0, need_stop=False):
        """Update continuous forward travel using capture-aligned feedback."""
        motion = self.motion
        pause_generation = _pause_generation(self.inputs)
        generation = getattr(self.inputs, "generation", 0)
        remaining = distance(pose, target)
        before_at, before_input = self.pose_at, self.pose_input
        walking = self.walking()
        jumping = target.jump or recovery
        # The movement-button icon is not a reliable airborne detector (it
        # stayed false throughout both grounded routes in the latest logs).
        # Keep its existing jump-control role; estimate by planned motion.
        mode = "jump" if jumping else "travel"
        samples = speeds[mode]
        velocity = self._planning_speed(mode, samples)
        self.inputs.check()
        if _pause_generation(self.inputs) != pause_generation:
            motion.close()
            return pose, False
        # Recognition may have completed well after this pose was captured.
        # Deduct only the forward input since capture, not stationary OCR time.
        advanced_seconds = max(0, motion.forward_elapsed()-before_input)
        bearing = (math.degrees(math.atan2(target.x-pose.x, pose.y-target.y)) + detour_deg) % 360
        brake_lead = self._air_brake_lead(jumping, velocity, need_stop=need_stop)
        # Aim toward the point centre, reserving only the input-release delay.
        # Subtracting half the radius too made 50 ms restart attempts near
        # the circle edge, shorter than the character's movement startup.
        duration = remaining/velocity-advanced_seconds-brake_lead
        if duration <= 0 and advanced_seconds > 0:
            motion.stop()
            return self.observe((pose.x, pose.y)), False
        duration = max(.04, duration)
        if recovery:
            min_recovery = .50 if detour_deg == 0.0 else .85
            duration = max(min_recovery, min(1.05, duration))
        align_lead = self._pre_jump_align_seconds(bearing, walking, remaining, radius) if jumping else 0.0
        if align_lead > 0 and duration < align_lead + .06:
            duration = align_lead + .06
        motion.forward(duration)
        self._last_move_bearing = bearing
        self._last_move_at = time.monotonic()
        if align_lead > 0:
            self.pause(align_lead)
        if _pause_generation(self.inputs) != pause_generation:
            motion.close()
            return pose, False
        started_jump = motion.jump(
            jumping, walking, reason="unstuck" if recovery else "segment",
            walking_reliable=self.walking_reliable,
            allow_air_jump=(not recovery and self._segment_from_air),
        )
        if self.overlay:
            self.overlay.notify_motion(
                velocity, duration, jumping,
                move_heading=bearing, new_jump=started_jump, cancel_jump=not jumping,
            )
        self._finish_step_motion(recovery, duration, generation, max_recovery_wait=1.15)
        observed_at = time.monotonic()
        after = self.observe((pose.x, pose.y))
        observation_seconds = time.monotonic()-observed_at
        input_seconds = max(0, self.pose_input-before_input)
        displacement = distance(pose, after)
        measured = displacement/input_seconds if input_seconds > 0 else 0
        trusted = (generation == getattr(self.inputs, "generation", 0)
                   and after.heading is not None and input_seconds >= .08)
        # Ignore blocked steps instead of interpreting a wall as a low speed.
        learned = trusted and displacement >= .3 and .5 <= measured <= 50
        if learned:
            speeds[mode] = (samples+[measured])[-5:]
        self.event({"type": "forward", "index": self.current_index,
                    "duration": duration, "movement_mode": mode,
                    "bearing": bearing,
                    "jump": jumping,
                    "walking": walking, "jump_state": motion.jump_state,
                    "recovery": recovery, "planning_speed": velocity,
                    "input_seconds": input_seconds, "observation_seconds": observation_seconds,
                    "capture_interval": self.pose_at-before_at, "compensated_input_seconds": advanced_seconds,
                    "before": [pose.x, pose.y], "after": [after.x, after.y],
                    "remaining_before": remaining, "remaining_after": distance(after, target),
                    "measured_speed": measured, "feedback_valid": learned})
        return after, recovery

    def _wasd_reset_state(self, *, reset_snap_cooldown=True):
        self._wasd_sector_idx = None
        self._wasd_regime = None
        self._last_move_bearing = None
        self._last_move_at = -math.inf
        if reset_snap_cooldown:
            self._last_wasd_snap_at = -math.inf

    def _wasd_steer_plan(self, bearing, pose, remaining, tolerance, *, velocity,
                         advanced_seconds, brake_lead=.06, extra_cycle=0.0):
        """Synthesize any 360-degree relative bearing from adjacent 45-degree WASD chords."""
        phi = angle_delta(bearing, pose.heading)
        best_idx = min(
            range(len(self._WASD_SECTORS)),
            key=lambda idx: abs(angle_delta(phi, self._WASD_SECTORS[idx][0])),
        )
        if self._wasd_sector_idx is not None and self._wasd_sector_idx != best_idx:
            prev_err = abs(angle_delta(phi, self._WASD_SECTORS[self._wasd_sector_idx][0]))
            if prev_err <= 24.5:
                best_idx = self._wasd_sector_idx
        self._wasd_sector_idx = best_idx
        sector_deg, base_keys = self._WASD_SECTORS[best_idx]
        delta = angle_delta(phi, sector_deg)
        deadband = max(3.5, min(7.0, float(tolerance)))
        base_duration = remaining / velocity - advanced_seconds - brake_lead
        lead_keys = None
        lead_seconds = 0.0
        if abs(delta) <= deadband:
            self._wasd_regime = "sector"
        else:
            adj_idx = (best_idx + (1 if delta > 0 else -1)) % len(self._WASD_SECTORS)
            _, adj_keys = self._WASD_SECTORS[adj_idx]
            tan_d = math.tan(math.radians(min(24.5, abs(delta))))
            c45 = math.sqrt(0.5)
            weight = min(0.54, tan_d / (c45 + (1.0 - c45) * tan_d))
            obs_cost = max(0.12, min(0.32, self.read_costs.get("observe", 0.20)))
            if extra_cycle > 0.0:
                # Takeoff blocks during align_lead + JUMP_PENDING (~0.48s) before observe();
                # size lead_seconds to the full cycle so the jump trajectory stays centered on bearing.
                cycle = obs_cost + extra_cycle
                lead_seconds = max(0.03, min(0.45, weight * cycle))
                if base_duration > 0 and base_duration < cycle:
                    lead_seconds = min(lead_seconds, base_duration * weight)
            else:
                ideal_lead = (weight / max(0.10, 1.0 - weight)) * obs_cost
                lead_seconds = max(0.03, min(0.28, ideal_lead))
                if base_duration > 0 and base_duration < lead_seconds + obs_cost:
                    lead_seconds = min(lead_seconds, base_duration * weight)
            if lead_seconds >= 0.025 and (base_duration <= 0 or base_duration > 0.06):
                lead_keys = adj_keys
                self._wasd_regime = "blend"
            else:
                lead_seconds = 0.0
                self._wasd_regime = "sector"
        return phi, base_keys, lead_keys, lead_seconds, base_duration

    def wasd_advance(self, pose, target, radius, speeds, *, tolerance=5.0,
                     recovery=False, detour_deg=0.0, need_stop=False):
        """Drive unified 360-degree closed-loop WASD steering with live minimap cone feedback."""
        motion = self.motion
        pause_generation = _pause_generation(self.inputs)
        generation = getattr(self.inputs, "generation", 0)
        remaining = distance(pose, target)
        before_at, before_input = self.pose_at, self.pose_input
        walking = self.walking()
        jumping = target.jump or recovery
        mode = "jump" if jumping else "travel"
        samples = speeds[mode]
        velocity = self._planning_speed(mode, samples)
        self.inputs.check()
        if _pause_generation(self.inputs) != pause_generation:
            motion.close()
            return pose, False, (), 0.0
        advanced_seconds = max(0, motion.forward_elapsed() - before_input)
        base_bearing = math.degrees(math.atan2(target.x - pose.x, pose.y - target.y)) % 360
        bearing = (base_bearing + detour_deg) % 360
        brake_lead = self._air_brake_lead(jumping, velocity, need_stop=need_stop)
        align_lead = self._pre_jump_align_seconds(bearing, walking, remaining, radius) if jumping else 0.0
        starting_ground_jump = (
            jumping
            and motion.jump_state == "idle"
            and (walking or not self.walking_reliable or recovery or not self._segment_from_air)
        )
        jump_cycle = (
            align_lead + motion.JUMP_HOLD + motion.JUMP_GAP + motion.FLOAT_PRESS
            if starting_ground_jump else 0.0
        )
        phi, base_keys, lead_keys, lead_seconds, duration = self._wasd_steer_plan(
            bearing, pose, remaining, tolerance,
            velocity=velocity, advanced_seconds=advanced_seconds,
            brake_lead=brake_lead, extra_cycle=jump_cycle,
        )
        if duration <= 0 and advanced_seconds > 0:
            motion.stop()
            return self.observe((pose.x, pose.y)), False, base_keys, phi
        duration = max(.04, duration)
        if recovery:
            min_recovery = .48 if detour_deg == 0.0 else .82
            duration = max(min_recovery, min(.95, duration))
        if align_lead > 0 and duration < align_lead + .06:
            duration = align_lead + .06
        motion.steer(base_keys, duration, lead_keys=lead_keys, lead_seconds=lead_seconds)
        self._last_move_bearing = bearing
        self._last_move_at = time.monotonic()
        active_set = set(base_keys) | set(lead_keys or ())
        if active_set & {"left", "right", "backward"}:
            self.heading_turn_at = time.monotonic()
            self.heading_turn_input = motion.forward_elapsed()
        if align_lead > 0:
            self.pause(align_lead)
        if _pause_generation(self.inputs) != pause_generation:
            motion.close()
            return pose, False, base_keys, phi
        started_jump = False
        if jumping:
            started_jump = motion.jump(
                True, walking, reason="unstuck" if recovery else "segment",
                walking_reliable=self.walking_reliable,
                allow_air_jump=(not recovery and self._segment_from_air),
            )
        elif motion.jump_state != "idle":
            motion.jump(False, walking, reason="segment",
                        walking_reliable=self.walking_reliable)
        if self.overlay:
            self.overlay.notify_motion(
                velocity, duration, jumping,
                move_heading=bearing, new_jump=started_jump, cancel_jump=not jumping,
            )
        self._finish_step_motion(recovery, duration, generation, max_recovery_wait=1.05)
        observed_at = time.monotonic()
        after = self.observe((pose.x, pose.y))
        observation_seconds = time.monotonic() - observed_at
        input_seconds = max(0, self.pose_input - before_input)
        displacement = distance(pose, after)
        measured = displacement / input_seconds if input_seconds > 0 else 0
        cam_rot = (
            round(angle_delta(after.heading, pose.heading), 1)
            if after.heading is not None and pose.heading is not None else None
        )
        trusted = (
            generation == getattr(self.inputs, "generation", 0)
            and after.heading is not None
            and input_seconds >= .08
        )
        learned = (
            trusted
            and lead_keys is None
            and abs(cam_rot or 0.0) <= 12.0
            and displacement >= .3
            and .5 <= measured <= 50
        )
        if learned:
            speeds[mode] = (samples + [measured])[-5:]
        self.event({"type": "forward", "index": self.current_index, "control": "wasd",
                    "regime": self._wasd_regime, "keys": list(base_keys),
                    "lead_keys": list(lead_keys) if lead_keys else None,
                    "lead_seconds": round(lead_seconds, 3) if lead_keys else 0.0,
                    "align_lead_seconds": round(align_lead, 3) if align_lead > 0 else 0.0,
                    "phi": round(phi, 1), "camera_rotation": cam_rot,
                    "duration": duration, "movement_mode": mode, "bearing": bearing,
                    "jump": jumping, "walking": walking, "jump_state": motion.jump_state,
                    "recovery": recovery, "planning_speed": velocity,
                    "input_seconds": input_seconds, "observation_seconds": observation_seconds,
                    "capture_interval": self.pose_at - before_at, "compensated_input_seconds": advanced_seconds,
                    "before": [pose.x, pose.y], "after": [after.x, after.y],
                    "remaining_before": remaining, "remaining_after": distance(after, target),
                    "measured_speed": measured, "feedback_valid": learned})
        return after, recovery, base_keys, phi

    def follow(self, route: Route):
        if route.map_id != self.locator.spec.id:
            raise NavigationError("路线使用的地图不匹配。")
        if any(p.action for p in route.points) and self.point_action is None:
            raise NavigationError("本路线包含任务动作，请从对应日常任务执行。")
        movement = self.inputs.bindings.movement()
        jump = self.inputs.bindings.get("jump")
        if any(p.jump for p in route.points) and jump.kind == "key" and jump.code in movement:
            raise NavigationError("跳跃按键不能与移动方向相同，请检查游戏键位设置。")
        deadline = time.monotonic()+route.timeout
        origin = route.points[0]
        spec = self.locator.spec
        self.event({"type": "navigation_profile", "map": route.map_id,
                    "steering": "wasd" if self.use_wasd_steering else "camera",
                    "transit_radius_scale": spec.transit_radius_scale,
                    "transit_pass_radius_scale": spec.transit_pass_radius_scale,
                    "heading_tolerance_far": spec.heading_tolerance_far,
                    "heading_tolerance_near": spec.heading_tolerance_near,
                    "heading_position_slack": spec.heading_position_slack})
        self._wasd_reset_state()
        self._cursor_released = False
        pose = self.observe((origin.x, origin.y))
        motion = Motion(self.inputs, self.event)
        self.motion = motion
        if self.point_action:
            self.point_action.motion = motion
            self.point_action.navigator = self
        speeds = self._initial_speeds()
        last = None
        stuck_anchor, stuck_since, detour_hold_until = pose, time.monotonic(), 0.0
        blocked_steps, unstuck_attempts = 0, 0
        best_distance = distance(pose, origin)
        index = 0
        self.current_index = 0
        self.active_route = route
        if self.overlay and pose.position_reliable:
            self.overlay.publish_route(route, index, pose)
        prepared_index = None
        last_good = pose
        scene_interrupted, scene_retries = False, 0
        self.heading_recheck_count = 0
        self._segment_from_air = False
        heading_rechecks = 0
        rejoin = None
        map_checked = False
        turnback_since = None
        generation = _pause_generation(self.inputs)
        try:
            self.inputs.check()
            if not self.use_wasd_steering:
                self._ensure_cursor_released()
            while index < len(route.points):
                self.current_index = index
                if self.stopped() or time.monotonic() >= deadline:
                    raise NavigationError("导航已停止或超时。")
                motion.tick()
                current_generation = _pause_generation(self.inputs)
                if current_generation != generation:
                    generation = current_generation
                    self._wasd_reset_state()
                    self._cursor_released = False
                    self.locator.reset()
                    pose = self.observe((last_good.x, last_good.y))
                    last_good = stuck_anchor = pose
                    stuck_since, detour_hold_until = time.monotonic(), 0.0
                    best_distance = distance(pose, rejoin or route.points[index])
                    map_checked = False
                    last = None
                    speeds = self._initial_speeds()
                    self.heading_recheck_count = 0
                    self._segment_from_air = False
                    scene_interrupted = False
                    heading_rechecks = 0
                    continue
                target = rejoin or route.points[index]
                if (not scene_interrupted and pose.heading is None and pose.position_reliable
                        and self.read_state("ready", self.inputs.ready)):
                    began = time.monotonic()
                    try:
                        pose = self.recheck_heading(pose, deadline)
                    except HeadingUncertain:
                        pose = replace(pose, heading=None)
                    except SceneUnavailable:
                        scene_interrupted = True
                    stuck_since += time.monotonic()-began
                    last = None
                    continue
                if scene_interrupted or pose.heading is None or not self.read_state("ready", self.inputs.ready):
                    scene_retries += 1
                    if scene_retries > 3:
                        raise NavigationError("路线多次中断，已停止，请检查当前位置。")
                    pose = self.reacquire_position(last_good, deadline)
                    if self.relocalized:
                        pending_transport = route.points[index].action == "teleport" and prepared_index != index
                        rejoin = None if pending_transport else self.rejoin_point(route, index, pose)
                        map_checked = True
                    # Local recovery already verified continuity. Larger
                    # corrections require the independent map anchor above and
                    # rejoin the current segment, preserving completed actions.
                    # Distance to waypoints cannot judge a long segment's middle.
                    last_good = stuck_anchor = pose
                    stuck_since, detour_hold_until = time.monotonic(), 0.0
                    self._wasd_reset_state()
                    best_distance = distance(pose, rejoin or route.points[index])
                    scene_interrupted, last = False, None
                    self._segment_from_air = False
                    heading_rechecks = 0
                    speeds = self._initial_speeds()
                    continue
                # A guarded page read may have reached the release deadline.
                # Refresh the old moving frame before arrival/stuck decisions.
                if _pause_generation(self.inputs) != generation:
                    continue
                if not motion.until and motion.forward_elapsed()-self.pose_input > .02:
                    last, pose = pose, self.observe((pose.x, pose.y))
                    continue
                last_good = pose
                if rejoin is None and self.point_prepare and prepared_index != index:
                    if self.point_prepare(target, pose):
                        self.locator.reset()
                        pose = self.observe((target.x, target.y))
                        last_good = pose
                        stuck_anchor, stuck_since, detour_hold_until = pose, time.monotonic(), 0.0
                        self._wasd_reset_state()
                        self._cursor_released = False
                        self._segment_from_air = False
                        motion.float_canceled_at = 0.0
                        best_distance = distance(pose, target)
                        map_checked = False
                        last = None
                        heading_rechecks = 0
                        speeds = self._initial_speeds()
                    prepared_index = index
                    if pose.heading is None or _pause_generation(self.inputs) != generation:
                        continue
                if rejoin is None and index == 0 and distance(pose, origin) > route.start_radius:
                    raise NavigationError("当前位置不在路线起点附近，未开始移动，请先传送或手动到达起点。")
                # Pickup locations require closer arrival than transit points.
                radius = min(target.radius, 1) if target.action == "bottle" else target.radius
                pass_radius = radius
                precise = bool(target.action) or rejoin is not None or index == len(route.points)-1
                # Tighten ordinary transit points. Preserve action/recovery
                # arrivals and explicitly broad endpoints such as meteor routes.
                if not precise and radius <= 1:
                    pass_radius *= max(spec.transit_radius_scale, spec.transit_pass_radius_scale)
                    radius *= spec.transit_radius_scale
                crossed = (not precise and last is not None
                           and crossed_waypoint(target, last, pose, radius, pass_radius))
                if distance(pose, target) <= radius or crossed:
                    heading_rechecks = 0
                    if precise:
                        arrival_pose = pose
                        is_wait = (
                            rejoin is None
                            and target.action == "wait"
                            and not (target.params or {}).get("compare_map_positions")
                        )
                        broad_wait = is_wait and radius >= 2.8
                        accept_radius = (
                            radius + 1.1 if broad_wait
                            else (radius + 0.25 if is_wait else radius)
                        )
                        motion.stop()
                        motion.jump(False, self.walking(), reason="waypoint_arrival",
                                    walking_reliable=self.walking_reliable)
                        if self.overlay:
                            self.overlay.notify_motion(0.0, 0.0, False, cancel_jump=True)
                        settle_tol = (
                            1.0 if broad_wait
                            else (0.8 if is_wait else max(.15, min(.4, radius*.25)))
                        )
                        # Wait up to ~0.36s while still inside accept_radius (or up to ~0.84s
                        # after canceling Float) so landing completes before exit or dwell.
                        prev_pose = pose
                        settle_steps = 7 if motion.float_canceled_at > 0 else 3
                        landed = True
                        for _ in range(settle_steps):
                            self.pause(.12)
                            confirmed = self.observe((prev_pose.x, prev_pose.y))
                            step_drift = distance(prev_pose, confirmed)
                            settled = step_drift <= settle_tol
                            landed = self._float_landed(.80)
                            if (confirmed.heading is None or distance(confirmed, target) > accept_radius
                                    or (settled and landed) or _pause_generation(self.inputs) != generation):
                                break
                            prev_pose = confirmed
                        settled = settled and landed
                        if settled:
                            motion.float_canceled_at = 0.0
                            self._segment_from_air = False
                        self.event({"type": "arrival_check", "index": index,
                                    "target": [target.x, target.y], "radius": radius,
                                    "accept_radius": accept_radius,
                                    "before_stop": [arrival_pose.x, arrival_pose.y],
                                    "after_stop": [confirmed.x, confirmed.y],
                                    "drift": distance(arrival_pose, confirmed),
                                    "step_drift": step_drift, "settled": settled, "landed": landed,
                                    "remaining": distance(confirmed, target), "stop": motion.last_stop})
                        pose = confirmed
                        last = None
                        if (pose.heading is None or distance(pose, target) > accept_radius or not settled
                                or _pause_generation(self.inputs) != generation):
                            if _pause_generation(self.inputs) == generation and (
                                    distance(pose, target) > accept_radius or not settled):
                                if turnback_since is None:
                                    turnback_since = time.monotonic()
                                self.event({
                                    "type": "turnback", "index": index, "reason": "braking_overshoot",
                                    "closest_dist": round(distance(arrival_pose, target), 3),
                                    "remaining": round(distance(pose, target), 3),
                                    "configured_radius": round(target.radius, 3),
                                    "radius": round(radius, 3), "pass_radius": round(pass_radius, 3),
                                    "precise": True,
                                    "pose": [round(pose.x, 3), round(pose.y, 3)],
                                })
                            continue
                    if rejoin is not None:
                        motion.close()
                        rejoin = None
                        last = None
                        turnback_since = None
                        stuck_anchor, stuck_since, detour_hold_until = pose, time.monotonic(), 0.0
                        self._wasd_reset_state()
                        self._segment_from_air = False
                        blocked_steps, unstuck_attempts = 0, 0
                        best_distance = distance(pose, route.points[index])
                        self.event({"type": "route_rejoined", "index": index, **pose.dict()})
                        continue
                    scene_retries = 0
                    self.heading_recheck_count = 0
                    turnback_elapsed = (round(time.monotonic()-turnback_since, 3)
                                        if turnback_since is not None else 0.0)
                    turnback_since = None
                    self.event({"type": "waypoint", "index": index, "total": len(route.points)-1,
                                "pose": pose.dict(), "target": [target.x, target.y],
                                "configured_radius": target.radius,
                                "radius": radius, "pass_radius": pass_radius,
                                "precise": precise,
                                "remaining": distance(pose, target),
                                "crossed": crossed,
                                "turnback_seconds": turnback_elapsed})
                    if self.waypoint and self.waypoint(target) == "done":
                        break
                    if target.action:
                        motion.stop()
                        motion.jump(False, self.walking(), reason="point_action",
                                    walking_reliable=self.walking_reliable)
                        if self.overlay:
                            self.overlay.notify_motion(0.0, 0.0, False, cancel_jump=True)
                        outcome = self.point_action(target)
                        if outcome == "done":
                            break
                        # Page actions/collection change both timers and pose.
                        self.locator.reset()
                        pose = self.observe((target.x, target.y))
                        stuck_anchor, stuck_since, detour_hold_until = pose, time.monotonic(), 0.0
                        self._wasd_reset_state()
                        self._cursor_released = False
                        self._segment_from_air = False
                        motion.float_canceled_at = 0.0
                        if self.route_branch:
                            replacement = self.route_branch(route, index)
                            if replacement is not None:
                                self.event({"type": "route_branch", "source": route.id,
                                            "destination": replacement.id, "index": index})
                                route = replacement
                                self.active_route = route
                    elif index+1 < len(route.points):
                        next_point = route.points[index+1]
                        next_bearing = math.degrees(math.atan2(next_point.x-pose.x, pose.y-next_point.y)) % 360
                        had_motion = bool(motion.until or motion.steer_keys)
                        if target.jump != next_point.jump:
                            motion.stop()
                            if not next_point.jump:
                                motion.jump(False, self.walking(), reason="segment_end",
                                            walking_reliable=self.walking_reliable)
                                if self.overlay:
                                    self.overlay.notify_motion(0.0, 0.0, False, cancel_jump=True)
                                waited = motion.float_canceled_at > 0
                                self._wait_for_landing(.80, pause_generation=generation, deadline=deadline)
                                self._segment_from_air = False
                                if had_motion and not waited:
                                    self.pause(.2)
                            else:
                                self._segment_from_air = False
                                if had_motion:
                                    self.pause(.22)
                        else:
                            if had_motion and not self.use_wasd_steering and pose.heading is not None:
                                if abs(angle_delta(next_bearing, pose.heading)) >= 45:
                                    motion.stop()
                            elif (had_motion and self.use_wasd_steering and target.jump and next_point.jump
                                  and self._last_move_bearing is not None
                                  and abs(angle_delta(next_bearing, self._last_move_bearing)) >= 55):
                                # Slow-banking floating suits (e.g. Paper Crane / Witch at 120 deg/s)
                                # shed sideways momentum briefly before sharp mid-air corners.
                                motion.stop()
                            self._segment_from_air = bool(
                                target.jump
                                and motion.float_canceled_at <= 0
                                and (motion.jump_state != "idle" or (crossed and self.walking_reliable and not self.walking()))
                            )
                    index += 1
                    self.current_index = index
                    if self.overlay and pose.position_reliable:
                        self.overlay.publish_route(route, index, pose)
                    if index < len(route.points):
                        stuck_anchor, stuck_since, detour_hold_until = pose, time.monotonic(), 0.0
                        self._wasd_reset_state(reset_snap_cooldown=False)
                        blocked_steps, unstuck_attempts = 0, 0
                        best_distance = distance(pose, route.points[index])
                        map_checked = False
                    last = None
                    continue
                if target.jump and motion.jump_state == "idle" and motion.float_canceled_at > 0 and not self._float_landed(.80):
                    if not self._wait_for_landing(.80, pause_generation=generation, deadline=deadline):
                        continue
                    self._segment_from_air = False
                    pose = self.observe((pose.x, pose.y))
                    last = None
                    continue
                remaining_distance = distance(pose, target)
                progress = max(.15, min(.75, radius*.5))
                if distance(pose, stuck_anchor) > progress and remaining_distance < best_distance-progress:
                    # Jitter or drifting sideways along a wall is not progress
                    # toward this waypoint and must not reset recovery timers.
                    scene_retries = 0
                    heading_rechecks = 0
                    stuck_anchor, stuck_since, detour_hold_until = pose, time.monotonic(), 0.0
                    blocked_steps, unstuck_attempts = 0, 0
                    best_distance = remaining_distance
                    map_checked = False
                now = time.monotonic()
                stuck_time = now - stuck_since
                if stuck_time > 30:
                    raise NavigationStuckError("原地卡住超过 30 秒，停止路线。")
                recovery = (blocked_steps >= 2) or (stuck_time > 2.2 and now >= detour_hold_until)
                detour_deg = 0.0
                if recovery:
                    detours = (42.0, -42.0, 68.0, -68.0, 95.0, -95.0)
                    detour_deg = 0.0 if unstuck_attempts == 0 else detours[(unstuck_attempts - 1) % len(detours)]
                    self.event({"type": "unstuck", "seconds": round(stuck_time, 3), "index": index,
                                "attempt": unstuck_attempts + 1, "blocked_steps": blocked_steps,
                                "detour_deg": detour_deg,
                                "remaining": remaining_distance, "best_remaining": best_distance})
                    if (self.relocalize and not map_checked and self.map_recoveries < 3
                            and unstuck_attempts >= 4 and stuck_time > 8.0):
                        pose = self.reacquire_position(last_good, deadline, force_map=True)
                        rejoin = self.rejoin_point(route, index, pose)
                        map_checked = True
                        last_good = stuck_anchor = pose
                        stuck_since, detour_hold_until = time.monotonic(), 0.0
                        self._wasd_reset_state()
                        blocked_steps, unstuck_attempts = 0, 0
                        best_distance = distance(pose, rejoin)
                        last = None
                        speeds = self._initial_speeds()
                        continue
                turn_begin = time.monotonic()
                turn_input = motion.forward_elapsed()
                before_turn = pose
                base_bearing = math.degrees(math.atan2(target.x-pose.x, pose.y-target.y)) % 360
                bearing = (base_bearing + detour_deg) % 360
                tolerance = self.travel_tolerance(remaining_distance, radius)
                need_stop = precise
                if not need_stop and rejoin is None and index + 1 < len(route.points):
                    nxt = route.points[index + 1]
                    if target.jump != nxt.jump:
                        need_stop = True
                    elif target.jump:
                        nxt_bearing = math.degrees(math.atan2(nxt.x - target.x, target.y - nxt.y)) % 360
                        need_stop = abs(angle_delta(nxt_bearing, base_bearing)) >= 45.0
                if last is not None and rejoin is None:
                    step_dx, step_dy = pose.x-last.x, pose.y-last.y
                    straddled = ((target.x-last.x)*step_dx+(target.y-last.y)*step_dy > 0
                                 and (target.x-pose.x)*step_dx+(target.y-pose.y)*step_dy <= 0)
                    closest_pass = segment_distance(target, last, pose)
                    if straddled and closest_pass <= max(pass_radius*2.5, radius+2.0):
                        if turnback_since is None:
                            turnback_since = time.monotonic()
                        self.event({
                            "type": "turnback", "index": index, "reason": "pass_miss",
                            "closest_dist": round(closest_pass, 3),
                            "remaining": round(remaining_distance, 3),
                            "configured_radius": round(target.radius, 3),
                            "radius": round(radius, 3), "pass_radius": round(pass_radius, 3),
                            "precise": bool(precise),
                            "turn_angle": (round(abs(angle_delta(bearing, pose.heading)), 1)
                                           if pose.heading is not None else None),
                            "tolerance": round(tolerance, 2),
                            "pose": [round(pose.x, 3), round(pose.y, 3)],
                        })
                if self.use_wasd_steering:
                    heading_rechecks = 0
                    before_move = pose
                    forward_before = motion.forward_elapsed()
                    step_began = time.monotonic()
                    try:
                        pose, recovery_done, _, _ = self.wasd_advance(
                            pose, target, radius, speeds, tolerance=tolerance,
                            recovery=recovery, detour_deg=detour_deg,
                            need_stop=need_stop,
                        )
                    except HeadingUncertain:
                        try:
                            pose = self.recheck_heading(pose, deadline)
                        except HeadingUncertain:
                            pose = replace(pose, heading=None)
                        except SceneUnavailable:
                            scene_interrupted = True
                        stuck_since += max(0.0, time.monotonic() - step_began
                                           - max(0.0, motion.forward_elapsed() - forward_before))
                        last = None
                        continue
                    except SceneUnavailable:
                        scene_interrupted = True
                        continue
                    move_input = max(0.0, motion.forward_elapsed() - forward_before)
                    move_disp = distance(before_move, pose)
                    move_speed = move_disp / move_input if move_input > 0 else 0.0
                    if pose.heading is not None and pose.position_reliable and move_input >= .08:
                        self.heading_recheck_count = 0
                    last = None if (recovery and detour_deg != 0.0) else before_move
                    if recovery_done:
                        unstuck_attempts += 1
                        blocked_steps = 1 if move_disp < .8 else 0
                        detour_hold_until = time.monotonic() + (1.5 if move_disp >= .8 else 0.0)
                    elif move_input >= .14 and move_speed < .95 and distance(pose, target) > radius + .35:
                        blocked_steps += 1
                    elif move_speed >= 2.2:
                        blocked_steps = 0
                    continue
                try:
                    pose = self.turn(bearing, pose, tolerance=tolerance)
                except HeadingUncertain:
                    try:
                        pose = self.recheck_heading(pose, deadline)
                    except HeadingUncertain:
                        pose = replace(pose, heading=None)
                    except SceneUnavailable:
                        scene_interrupted = True
                    stuck_since += max(0, time.monotonic()-turn_begin
                                       -max(0, motion.forward_elapsed()-turn_input))
                    last = None
                    continue
                except SceneUnavailable:
                    scene_interrupted = True
                    continue
                # Small moving corrections can fall back to a stopped turn too.
                # Only time actually holding forward counts toward being stuck.
                turn_forward = max(0, motion.forward_elapsed()-turn_input)
                turn_stop_seconds = max(0.0, time.monotonic()-turn_begin-turn_forward)
                stuck_since += turn_stop_seconds
                if turn_stop_seconds >= .02:
                    self.event({"type": "turn_stop", "index": index,
                                "stop_seconds": round(turn_stop_seconds, 3),
                                "remaining": round(remaining_distance, 3),
                                "tolerance": round(tolerance, 2)})
                if self.stopped() or time.monotonic() >= deadline:
                    raise NavigationError("导航已停止或超时。")
                if _pause_generation(self.inputs) != generation:
                    continue
                if (distance(pose, target) <= radius or not precise
                        and crossed_waypoint(target, before_turn, pose, radius, pass_radius)):
                    last = before_turn
                    continue
                # Coasting during a turn can change the target bearing. When
                # forward was not held and the pose only shifted within
                # sub-pixel minimap correlation jitter, keep the completed
                # stationary alignment so a 0.05-unit grid step near the
                # waypoint cannot loop without advancing.
                bearing = (math.degrees(math.atan2(target.x-pose.x, pose.y-target.y)) + detour_deg) % 360
                tolerance = self.travel_tolerance(distance(pose, target), radius)
                coasted = turn_forward > .02 or distance(before_turn, pose) > .15
                if coasted and abs(angle_delta(bearing, pose.heading)) > tolerance:
                    heading_rechecks = 0 if turn_forward > .02 else heading_rechecks+1
                    # Turning time is excluded from the movement-stuck clock.
                    # Bound this separate loop so noisy bearings cannot keep
                    # correcting indefinitely without another forward attempt.
                    action = (
                        ("advance" if abs(angle_delta(bearing, pose.heading)) <= 45 else "relocalize")
                        if heading_rechecks >= 3 else None
                    )
                    self.event({"type": "heading_recheck", "index": index,
                                "attempt": heading_rechecks, "target": [target.x, target.y],
                                "bearing": bearing, "heading": pose.heading,
                                "tolerance": tolerance, "displacement": distance(before_turn, pose),
                                "forward_seconds": turn_forward,
                                "recovery": action is not None,
                                **({"action": action} if action is not None else {})})
                    last = None
                    if action is not None:
                        heading_rechecks = 0
                    if action == "relocalize":
                        motion.stop()
                        scene_interrupted = True
                    if action != "advance":
                        continue
                heading_rechecks = 0
                before_move = pose
                forward_before = motion.forward_elapsed()
                pose, recovery_done = self.advance(
                    pose, target, radius, speeds, recovery=recovery,
                    detour_deg=detour_deg, need_stop=need_stop,
                )
                move_input = max(0.0, motion.forward_elapsed()-forward_before)
                move_disp = distance(before_move, pose)
                move_speed = move_disp/move_input if move_input > 0 else 0.0
                if (pose.heading is not None and pose.position_reliable
                        and move_input >= .08):
                    self.heading_recheck_count = 0
                last = None if (recovery and detour_deg != 0.0) else before_move
                if recovery_done:
                    unstuck_attempts += 1
                    blocked_steps = 1 if move_disp < .8 else 0
                    # Allow a brief straight test after displacing sideways so a
                    # cleared detour can resume heading toward the target instead
                    # of immediately reversing direction or locking out timed recovery.
                    detour_hold_until = time.monotonic() + (1.5 if move_disp >= .8 else 0.0)
                elif move_input >= .14 and move_speed < .95 and distance(pose, target) > radius+.35:
                    blocked_steps += 1
                elif move_speed >= 2.2:
                    blocked_steps = 0
            self.event({"type": "arrived", **pose.dict()})
            return pose
        finally:
            if self.overlay:
                self.overlay.close()
            try:
                motion.close()
            finally:
                self.motion = None
                if self.point_action:
                    self.point_action.motion = None
                    self.point_action.navigator = None
