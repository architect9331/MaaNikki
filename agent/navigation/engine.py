"""Camera-aligned forward movement with timed movement and jump state."""
from __future__ import annotations
import math
from statistics import median
from task_control import task_time as time
from .models import NavigationError, NavigationStuckError, Point, Route
from .geometry import project_segment, segment_distance
from .vision import Locator, Pose


class SceneUnavailable(NavigationError):
    """A temporary scene/map interruption, recoverable by the route loop."""


def distance(a, b):
    return math.hypot(a.x-b.x, a.y-b.y)


def angle_delta(target, current):
    return (target-current+180) % 360-180


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
        self.jump_state = "idle"
        self.jump_at = 0
        self.forward_started = None
        self.forward_seconds = 0
        self.last_stop = None
        self.generation = getattr(inputs, "generation", 0)

    def forward(self, seconds):
        if not math.isfinite(seconds) or seconds <= 0:
            raise NavigationError("前进时长无效。")
        generation = self.generation
        self.tick()
        if self.generation != generation:
            return
        # Include submission latency in the budget instead of starting the
        # timer after the key-down RPC has already moved the character.
        now = time.monotonic()
        if self.forward_started is None:
            self.forward_started = now
        self.until = now+seconds
        self.inputs.key_down("forward")
        self.tick()

    def stop(self):
        deadline = self.until
        self.until = 0
        started = time.monotonic()
        try:
            self.inputs.key_up("forward")
        finally:
            completed = time.monotonic()
            if self.forward_started is not None:
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

    def jump(self, wanted, walking, *, reason="route"):
        if not wanted:
            if self.jump_state == "idle":
                return
            # Cancel a queued second jump before tick can submit it.
            self.inputs.key_up("jump")
            cancel_float = self.jump_state == "air" and not walking
            self.jump_transition("idle", reason, walking=walking, cancel_float=cancel_float)
            if cancel_float:
                self.inputs.key_down("jump")
                pressed_at = time.monotonic()
                try:
                    self.wait(self.FLOAT_PRESS)
                finally:
                    self.inputs.key_up("jump")
                    self.event({"type": "jump_cancel_input", "reason": reason,
                                "held_seconds": time.monotonic()-pressed_at})
            return
        self.tick()
        now = time.monotonic()
        if walking and (self.jump_state == "idle" or self.jump_state == "air" and now-self.jump_at > .2):
            self.inputs.key_down("jump")
            self.jump_transition("first", "ground_jump_down", walking=walking)
        elif not walking and self.jump_state == "idle":
            self.inputs.key_down("jump")
            self.jump_transition("second_hold", "air_jump_down", walking=walking)

    def tick(self):
        self.inputs.check()
        generation = getattr(self.inputs, "generation", 0)
        if generation != self.generation:
            self.generation = generation
            # A key-down RPC may itself have waited for focus restoration.
            # Release that input too; the caller must obtain a fresh pose.
            self.close()
            return
        now = time.monotonic()
        if self.until and now >= self.until:
            self.stop()
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
        if self.jump_state != "idle":
            self.jump_transition("idle", "motion_close")
        if self.forward_started is not None:
            self.forward_seconds += time.monotonic()-self.forward_started
        self.forward_started = None
        self.inputs.release()


class Navigator:
    # World-map units per second. Low displacement (wall contact, animation,
    # noisy localization) must never turn into an arbitrarily long W hold.
    MIN_PLANNING_SPEED = 6.0

    def __init__(self, locator: Locator, inputs, capture, stopped, pause, event=lambda value: None,
                 point_action=None, waypoint=None, walking=None, page_detector=None, point_prepare=None,
                 route_branch=None, relocalize=None, scene_recovery=None):
        self.locator, self.inputs, self.capture, self.stopped, self._pause, self.event = locator, inputs, capture, stopped, pause, event
        self.point_action, self.waypoint = point_action, waypoint
        self.point_prepare = point_prepare
        self.route_branch = route_branch
        self.walking_detector = walking
        self.page_detector = page_detector
        self.turn_ratio = None
        self.motion = None
        self.next_skip_check = 0
        self.relocalize = relocalize
        self.scene_recovery = scene_recovery
        self.map_recoveries = 0
        self.relocalized = False
        self.read_costs = {}
        self.pose_at = time.monotonic()
        self.pose_input = 0.0

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

    def observe(self, hint, *, commit=True, reset_continuity=False):
        self.inputs.check()
        if self.motion:
            self.motion.guard(self.read_costs.get("observe", .3)*1.25+.03)
        begin = time.monotonic()
        input_begin = self.motion.forward_elapsed() if self.motion else 0
        frame = self.capture()
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
            pose = self.locator.locate(frame, hint, search_radius=26, commit=commit,
                                       reset_continuity=reset_continuity,
                                       progress=self.motion.tick if self.motion else None)
            if pose.heading is None:
                if self.motion:
                    self.motion.close()
        if self.motion:
            self.motion.tick()
        self.event({"type": "pose", **pose.dict(), "heading_diagnostics": dict(self.locator.heading_diagnostics),
                    "capture_age_seconds": time.monotonic()-self.pose_at,
                    "input_since_capture": max(0, self.motion.forward_elapsed()-self.pose_input) if self.motion else 0})
        self.read_costs["observe"] = max(self.read_costs.get("observe", 0)*.8, time.monotonic()-begin)
        return pose

    def camera(self, pixels):
        from .controller import MAX_CAMERA_PIXELS, relative_camera
        # Send the calibrated displacement, then verify the actual heading.
        # Keep an input bound for unusual sensitivity/calibration results.
        part = max(-MAX_CAMERA_PIXELS, min(MAX_CAMERA_PIXELS, round(pixels)))
        generation = getattr(self.inputs, "generation", 0)
        self.inputs.check()
        if getattr(self.inputs, "generation", 0) != generation:
            raise SceneUnavailable("游戏已恢复前台，重新确认位置和方向。")
        if part and not relative_camera(self.inputs.controller, part, 0):
            raise NavigationError("转动镜头失败。")
        return part

    def stable_heading(self, pose):
        generation = getattr(self.inputs, "generation", 0)
        previous = None
        stable = 0
        for _ in range(6):
            self.inputs.check()
            if self.stopped() or self.pause(.04) is False:
                raise NavigationError("已停止导航。")
            pose = self.observe((pose.x, pose.y))
            if getattr(self.inputs, "generation", 0) != generation:
                raise SceneUnavailable("游戏已恢复前台，重新确认位置和方向。")
            if pose.heading is None:
                raise SceneUnavailable("小地图暂时不可用，等待画面恢复。")
            if pose.heading is not None and previous is not None and abs(angle_delta(pose.heading, previous)) < 5:
                stable += 1
                if stable >= 2:
                    return pose
            else:
                stable = 0
            previous = pose.heading
        raise NavigationError("小地图方向尚未稳定，已停止移动。")

    def calibrate(self, pose):
        ratios = []
        probe_pixels = 180
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

    def turn(self, target_heading, pose, *, stationary=False, tolerance=5):
        generation = getattr(self.inputs, "generation", 0)
        previous_delta = None
        correction_gain = 1.0
        if pose.heading is None:
            raise SceneUnavailable("小地图暂时不可用，等待画面恢复。")
        if self.turn_ratio is not None and abs(angle_delta(target_heading, pose.heading)) <= tolerance:
            return pose
        if (not stationary and self.turn_ratio is not None
                and abs(angle_delta(target_heading, pose.heading)) < 45):
            # Small corrections keep forward held while there is sufficient
            # travel left to complete the camera update and its observation.
            if self.motion:
                self.motion.guard(self.read_costs.get("observe", .3)*1.25+.15)
            if getattr(self.inputs, "generation", 0) != generation:
                raise SceneUnavailable("游戏已恢复前台，重新确认位置和方向。")
            delta = angle_delta(target_heading, pose.heading)
            previous_delta = delta
            pixels = self.camera(delta*self.turn_ratio)
            self.pause(.04)
            after = self.observe((pose.x, pose.y))
            if getattr(self.inputs, "generation", 0) != generation:
                raise SceneUnavailable("游戏已恢复前台，重新确认位置和方向。")
            if after.heading is None:
                raise SceneUnavailable("小地图暂时不可用，等待画面恢复。")
            self.event({"type": "camera_turn", "target": target_heading, "before": pose.heading,
                        "after": after.heading, "pixels": pixels,
                        "rotation": angle_delta(after.heading, pose.heading), "gain": correction_gain,
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
                # Reduce corrections when feedback crosses the target instead
                # of repeating full-size opposite turns on the same bearing.
                if previous_delta is not None and delta*previous_delta < 0:
                    correction_gain = max(.125, correction_gain*.5)
                previous_delta = delta
                before = pose.heading
                pixels = self.camera(delta*self.turn_ratio*correction_gain)
                self.pause(.15)
                pose = self.stable_heading(pose)
                actual = angle_delta(pose.heading, before)
                self.event({"type": "camera_turn", "target": target_heading, "before": before,
                            "after": pose.heading, "pixels": pixels, "rotation": actual,
                            "gain": correction_gain, "stationary": True,
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
                    if (measured*self.turn_ratio > 0 and 8 <= abs(actual) <= 90
                            and .75 <= abs(measured/self.turn_ratio) <= 1.33):
                        self.turn_ratio = .8*self.turn_ratio+.2*measured
            raise NavigationError("镜头未能稳定朝向路线，已停止移动。")
        except BaseException:
            self.inputs.release()
            raise

    def face_heading(self, heading):
        """Turn the camera, then align the stationary character for an action."""
        if type(heading) not in (int, float) or not math.isfinite(heading) or not 0 <= heading < 360:
            raise NavigationError("动作朝向须为 0–360 度范围内的有效角度（不含 360）。")
        self.inputs.check()
        generation = getattr(self.inputs, "generation", 0)
        if self.motion:
            self.motion.stop()
            self.motion.jump(False, self.walking(), reason="point_facing")
        else:
            self.inputs.release()
        previous = self.locator.previous
        if previous is None:
            raise NavigationError("缺少已确认的位置，未调整动作朝向。")
        pose = self.observe((previous.x, previous.y))
        pose = self.turn(heading, pose, stationary=True)
        if getattr(self.inputs, "generation", 0) != generation:
            raise SceneUnavailable("游戏已恢复前台，重新确认位置和方向。")
        timing = self.inputs.face_camera()
        after = self.observe((pose.x, pose.y))
        error = abs(angle_delta(heading, after.heading)) if after.heading is not None else None
        confirmed = (getattr(self.inputs, "generation", 0) == generation
                     and error is not None and error <= 5)
        self.event({"type": "character_facing", "source": "route_heading", "target": heading,
                    "camera_heading": after.heading, "camera_error": error,
                    "before": [pose.x, pose.y], "after": [after.x, after.y],
                    "displacement": distance(pose, after), "camera_confirmed": confirmed, **timing})
        if not confirmed:
            raise SceneUnavailable("动作前的镜头朝向未能保持稳定，未开始交互。")
        return after

    def walking(self):
        if self.walking_detector is not None:
            return bool(self.read_state("walking", self.walking_detector))
        return not self.point_action or self.read_state("walking", self.point_action.rt.ui.walking)

    def recover_scene(self, anchor, deadline, *, force_map=False):
        self.motion.close()
        self.relocalized = False
        reason = ("page_unavailable" if not self.inputs.ready() else
                  self.locator.heading_diagnostics.get("method", "camera_unavailable"))
        self.event({"type": "scene_wait", "reason": reason})
        end = min(deadline, time.monotonic()+15)
        previous = None
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
                if self.scene_recovery:
                    revived = self.scene_recovery()
                    if revived is not None:
                        if revived:
                            # Respawn may change height or location. Let the
                            # route owner restart from its teleport checkpoint.
                            raise NavigationStuckError("角色落水后已复活，需要从起点重新运行路线。")
                        raise NavigationError("角色落水后未能完成复活，本条路线停止。")
                if self.point_action and self.point_action.rt.hit("MaaNikki_Energy_SkipReward"):
                    if not self.point_action.rt.key("skip", .5):
                        raise NavigationError("未能跳过获取提示。")
                previous = None
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
                end = min(deadline, time.monotonic()+15)
                self.event({"type": "map_position_anchor", "x": x, "y": y,
                            "attempt": self.map_recoveries})
                if self.stopped() or time.monotonic() >= deadline:
                    raise NavigationError("导航已停止或超时。")
            pose = self.observe((anchor.x, anchor.y), commit=False,
                                reset_continuity=self.relocalized)
            if pose.heading is not None:
                if previous is not None and distance(pose, previous) < 2:
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
                        continue
                    if self.relocalized and anchor_distance > 6:
                        raise NavigationError("大地图角色位置与小地图定位不一致，未继续移动。")
                    # Tentative reads never replace the last trusted position.
                    self.locator.accept(pose)
                    self.event({"type": "scene_resumed", **pose.dict(),
                                "verification": "bigmap" if self.relocalized else "local_continuity",
                                "anchor_distance": anchor_distance})
                    return pose
                previous = pose
            else:
                previous = None
                failures += 1
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

    def advance(self, pose, target, radius, speeds, *, recovery=False):
        """Update continuous forward travel using capture-aligned feedback."""
        motion = self.motion
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
        velocity = max(self.MIN_PLANNING_SPEED, median(samples))
        self.inputs.check()
        if getattr(self.inputs, "generation", 0) != generation:
            motion.close()
            return pose, False
        # Recognition may have completed well after this pose was captured.
        # Deduct only the forward input since capture, not stationary OCR time.
        advanced_seconds = max(0, motion.forward_elapsed()-before_input)
        # Aim toward the point centre, reserving only the input-release delay.
        # Subtracting half the radius too made 50 ms restart attempts near
        # the circle edge, shorter than the character's movement startup.
        duration = remaining/velocity-advanced_seconds-.06
        if duration <= 0 and advanced_seconds > 0:
            motion.stop()
            return self.observe((pose.x, pose.y)), False
        duration = max(.04, duration)
        if recovery:
            duration = min(1.0, duration)
        motion.forward(duration)
        if getattr(self.inputs, "generation", 0) != generation:
            motion.close()
            return pose, False
        motion.jump(jumping, walking, reason="unstuck" if recovery else "segment")
        if recovery:
            self.pause(1.5)
            motion.stop()
            motion.jump(False, self.walking(), reason="unstuck_complete")
        elif motion.jump_state in motion.JUMP_PENDING:
            # Preserve the original full-height jump and second jump timing.
            # The independent forward deadline can release W during this wait.
            while motion.jump_state in motion.JUMP_PENDING:
                self.pause(.01)
        else:
            self.pause(.01)
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
        self.event({"type": "forward", "duration": duration, "movement_mode": mode,
                    "bearing": math.degrees(math.atan2(target.x-pose.x, pose.y-target.y)) % 360,
                    "jump": jumping,
                    "walking": walking, "jump_state": motion.jump_state,
                    "recovery": recovery, "planning_speed": velocity,
                    "input_seconds": input_seconds, "observation_seconds": observation_seconds,
                    "capture_interval": self.pose_at-before_at, "compensated_input_seconds": advanced_seconds,
                    "before": [pose.x, pose.y], "after": [after.x, after.y],
                    "remaining_before": remaining, "remaining_after": distance(after, target),
                    "measured_speed": measured, "feedback_valid": learned})
        return after, recovery

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
                    "transit_radius_scale": spec.transit_radius_scale,
                    "transit_pass_radius_scale": spec.transit_pass_radius_scale,
                    "heading_tolerance_far": spec.heading_tolerance_far,
                    "heading_tolerance_near": spec.heading_tolerance_near,
                    "heading_position_slack": spec.heading_position_slack})
        pose = self.observe((origin.x, origin.y))
        motion = Motion(self.inputs, self.event)
        self.motion = motion
        if self.point_action:
            self.point_action.motion = motion
            self.point_action.navigator = self
        speeds = {"travel": [self.MIN_PLANNING_SPEED], "jump": [self.MIN_PLANNING_SPEED]}
        last = None
        stuck_anchor, stuck_since, recovered = pose, time.monotonic(), False
        best_distance = distance(pose, origin)
        index = 0
        prepared_index = None
        last_good = pose
        scene_interrupted, scene_retries = False, 0
        heading_rechecks = 0
        rejoin = None
        map_checked = False
        generation = getattr(self.inputs, "generation", 0)
        try:
            self.inputs.check()
            if not self.inputs.controller.post_click_key(18).wait().succeeded:
                raise NavigationError("未能解除游戏光标模式，尚未开始移动。")
            while index < len(route.points):
                if self.stopped() or time.monotonic() >= deadline:
                    raise NavigationError("导航已停止或超时。")
                motion.tick()
                current_generation = getattr(self.inputs, "generation", 0)
                if current_generation != generation:
                    generation = current_generation
                    self.locator.reset()
                    pose = self.observe((last_good.x, last_good.y))
                    last_good = stuck_anchor = pose
                    stuck_since, recovered = time.monotonic(), False
                    best_distance = distance(pose, rejoin or route.points[index])
                    map_checked = False
                    last = None
                    speeds = {"travel": [self.MIN_PLANNING_SPEED], "jump": [self.MIN_PLANNING_SPEED]}
                    self.turn_ratio = None
                    scene_interrupted = False
                    heading_rechecks = 0
                    continue
                target = rejoin or route.points[index]
                if scene_interrupted or pose.heading is None or not self.read_state("ready", self.inputs.ready):
                    scene_retries += 1
                    if scene_retries > 3:
                        raise NavigationError("路线多次中断，已停止，请检查当前位置。")
                    pose = self.recover_scene(last_good, deadline)
                    if self.relocalized:
                        pending_transport = route.points[index].action == "teleport" and prepared_index != index
                        rejoin = None if pending_transport else self.rejoin_point(route, index, pose)
                        map_checked = True
                    # Local recovery already verified continuity. Larger
                    # corrections require the independent map anchor above and
                    # rejoin the current segment, preserving completed actions.
                    # Distance to waypoints cannot judge a long segment's middle.
                    last_good = stuck_anchor = pose
                    stuck_since, recovered = time.monotonic(), False
                    best_distance = distance(pose, rejoin or route.points[index])
                    scene_interrupted, last = False, None
                    heading_rechecks = 0
                    speeds = {"travel": [self.MIN_PLANNING_SPEED], "jump": [self.MIN_PLANNING_SPEED]}
                    continue
                # A guarded page read may have reached the release deadline.
                # Refresh the old moving frame before arrival/stuck decisions.
                if getattr(self.inputs, "generation", 0) != generation:
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
                        stuck_anchor, stuck_since, recovered = pose, time.monotonic(), False
                        best_distance = distance(pose, target)
                        map_checked = False
                        last = None
                        heading_rechecks = 0
                        speeds = {"travel": [self.MIN_PLANNING_SPEED], "jump": [self.MIN_PLANNING_SPEED]}
                    prepared_index = index
                    if pose.heading is None or getattr(self.inputs, "generation", 0) != generation:
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
                        motion.stop()
                        motion.jump(False, self.walking(), reason="waypoint_arrival")
                        self.pause(.12)
                        confirmed = self.observe((pose.x, pose.y))
                        settled = distance(pose, confirmed) <= max(.15, min(.4, radius*.25))
                        self.event({"type": "arrival_check", "index": index,
                                    "target": [target.x, target.y], "radius": radius,
                                    "before_stop": [arrival_pose.x, arrival_pose.y],
                                    "after_stop": [confirmed.x, confirmed.y],
                                    "drift": distance(arrival_pose, confirmed), "settled": settled,
                                    "remaining": distance(confirmed, target), "stop": motion.last_stop})
                        pose = confirmed
                        last = None
                        if (pose.heading is None or distance(pose, target) > radius or not settled
                                or getattr(self.inputs, "generation", 0) != generation):
                            continue
                    if rejoin is not None:
                        motion.close()
                        rejoin = None
                        last = None
                        stuck_anchor, stuck_since, recovered = pose, time.monotonic(), False
                        best_distance = distance(pose, route.points[index])
                        self.event({"type": "route_rejoined", "index": index, **pose.dict()})
                        continue
                    scene_retries = 0
                    self.event({"type": "waypoint", "index": index, "total": len(route.points)-1,
                                "pose": pose.dict(), "target": [target.x, target.y],
                                "radius": radius, "pass_radius": pass_radius,
                                "remaining": distance(pose, target),
                                "crossed": crossed})
                    if self.waypoint and self.waypoint(target) == "done":
                        break
                    if target.action:
                        motion.stop()
                        motion.jump(False, self.walking(), reason="point_action")
                        outcome = self.point_action(target)
                        if outcome == "done":
                            break
                        # Page actions/collection change both timers and pose.
                        self.locator.reset()
                        pose = self.observe((target.x, target.y))
                        stuck_anchor, stuck_since, recovered = pose, time.monotonic(), False
                        if self.route_branch:
                            replacement = self.route_branch(route, index)
                            if replacement is not None:
                                self.event({"type": "route_branch", "source": route.id,
                                            "destination": replacement.id, "index": index})
                                route = replacement
                    elif index+1 < len(route.points) and target.jump != route.points[index+1].jump and motion.until:
                        motion.stop()
                        self.pause(.5 if route.points[index+1].jump else .2)
                    index += 1
                    if index < len(route.points):
                        stuck_anchor, stuck_since, recovered = pose, time.monotonic(), False
                        best_distance = distance(pose, route.points[index])
                        map_checked = False
                    last = None
                    continue
                remaining_distance = distance(pose, target)
                progress = max(.15, min(.75, radius*.5))
                if distance(pose, stuck_anchor) > progress and remaining_distance < best_distance-progress:
                    # Jitter or drifting sideways along a wall is not progress
                    # toward this waypoint and must not reset recovery timers.
                    scene_retries = 0
                    heading_rechecks = 0
                    stuck_anchor, stuck_since, recovered = pose, time.monotonic(), False
                    best_distance = remaining_distance
                    map_checked = False
                stuck_time = time.monotonic()-stuck_since
                if stuck_time > 30:
                    raise NavigationStuckError("原地卡住超过 30 秒，停止路线。")
                recovery = stuck_time > 5 and not recovered
                if recovery:
                    self.event({"type": "unstuck", "seconds": stuck_time, "index": index,
                                "remaining": remaining_distance, "best_remaining": best_distance})
                    if self.relocalize and not map_checked and self.map_recoveries < 3:
                        pose = self.recover_scene(last_good, deadline, force_map=True)
                        rejoin = self.rejoin_point(route, index, pose)
                        map_checked = True
                        last_good = stuck_anchor = pose
                        stuck_since, recovered = time.monotonic(), False
                        best_distance = distance(pose, rejoin)
                        last = None
                        speeds = {"travel": [self.MIN_PLANNING_SPEED], "jump": [self.MIN_PLANNING_SPEED]}
                        continue
                turn_begin = time.monotonic()
                turn_input = motion.forward_elapsed()
                before_turn = pose
                bearing = math.degrees(math.atan2(target.x-pose.x, pose.y-target.y)) % 360
                tolerance = self.travel_tolerance(remaining_distance, radius)
                try:
                    pose = self.turn(bearing, pose, tolerance=tolerance)
                except SceneUnavailable:
                    scene_interrupted = True
                    continue
                # Small moving corrections can fall back to a stopped turn too.
                # Only time actually holding forward counts toward being stuck.
                turn_forward = max(0, motion.forward_elapsed()-turn_input)
                stuck_since += max(0, time.monotonic()-turn_begin-turn_forward)
                if self.stopped() or time.monotonic() >= deadline:
                    raise NavigationError("导航已停止或超时。")
                if getattr(self.inputs, "generation", 0) != generation:
                    continue
                if (distance(pose, target) <= radius or not precise
                        and crossed_waypoint(target, before_turn, pose, radius, pass_radius)):
                    last = before_turn
                    continue
                # Coasting during a turn can change the target bearing.
                bearing = math.degrees(math.atan2(target.x-pose.x, pose.y-target.y)) % 360
                tolerance = self.travel_tolerance(distance(pose, target), radius)
                if abs(angle_delta(bearing, pose.heading)) > tolerance:
                    heading_rechecks = 0 if turn_forward > .02 else heading_rechecks+1
                    self.event({"type": "heading_recheck", "index": index,
                                "attempt": heading_rechecks, "target": [target.x, target.y],
                                "bearing": bearing, "heading": pose.heading,
                                "tolerance": tolerance, "displacement": distance(before_turn, pose),
                                "forward_seconds": turn_forward,
                                "recovery": heading_rechecks >= 3})
                    # Turning time is excluded from the movement-stuck clock.
                    # Bound this separate loop so noisy bearings cannot keep
                    # correcting indefinitely without another forward attempt.
                    if heading_rechecks >= 3:
                        motion.stop()
                        scene_interrupted = True
                    last = None
                    continue
                heading_rechecks = 0
                before_move = pose
                pose, recovery_done = self.advance(pose, target, radius, speeds, recovery=recovery)
                last = None if recovery else before_move
                if recovery_done:
                    recovered = True
            self.event({"type": "arrived", **pose.dict()})
            return pose
        finally:
            try:
                motion.close()
            finally:
                self.motion = None
                if self.point_action:
                    self.point_action.motion = None
                    self.point_action.navigator = None
