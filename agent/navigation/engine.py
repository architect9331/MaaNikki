"""Camera-aligned forward movement with timed movement and jump state."""
from __future__ import annotations
import math
import time
from .models import NavigationError, Route
from .vision import Locator, Pose


class SceneUnavailable(NavigationError):
    """A temporary scene/map interruption, recoverable by the route loop."""


def distance(a, b):
    return math.hypot(a.x-b.x, a.y-b.y)


def angle_delta(target, current):
    return (target-current+180) % 360-180


def segment_distance(p, a, b):
    dx, dy = b.x-a.x, b.y-a.y
    length = dx*dx+dy*dy
    fraction = max(0, min(1, ((p.x-a.x)*dx+(p.y-a.y)*dy)/length)) if length else 0
    return math.hypot(p.x-a.x-fraction*dx, p.y-a.y-fraction*dy)


class Motion:
    """Cooperative timers: only the navigation thread may call Maa APIs.

    Agent reverse calls share a receive channel. A timer thread must not query
    stopping or submit inputs while another thread is receiving a screenshot.
    """
    def __init__(self, inputs):
        self.inputs = inputs
        self.until = 0
        self.jump_state = "idle"
        self.jump_at = 0

    def forward(self, seconds):
        self.tick()
        self.inputs.key_down("forward")
        self.until = time.monotonic()+seconds

    def stop(self):
        self.until = 0
        self.inputs.key_up("forward")

    def jump(self, wanted, walking):
        self.tick()
        now = time.monotonic()
        if wanted and walking and (self.jump_state == "idle" or self.jump_state == "air" and now-self.jump_at > .2):
            self.inputs.key_down("jump")
            self.jump_state, self.jump_at = "first", now
        elif wanted and not walking and self.jump_state == "idle":
            self.inputs.key_down("jump")
            self.inputs.key_up("jump")
            self.jump_state, self.jump_at = "air", now
        elif not wanted and self.jump_state != "idle":
            self.inputs.key_up("jump")
            if self.jump_state == "air" and not walking:
                self.inputs.key_down("jump")
                self.inputs.key_up("jump")
            self.jump_state = "idle"

    def tick(self):
        self.inputs.check()
        now = time.monotonic()
        if self.until and now >= self.until:
            self.inputs.key_up("forward")
            self.until = 0
        if self.jump_state == "first" and now-self.jump_at >= .3:
            self.inputs.key_up("jump")
            self.jump_state, self.jump_at = "second_wait", now
        elif self.jump_state == "second_wait" and now-self.jump_at >= .1:
            self.inputs.key_down("jump")
            self.inputs.key_up("jump")
            self.jump_state, self.jump_at = "air", now

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
        self.jump_state = "idle"
        self.inputs.release()


class Navigator:
    def __init__(self, locator: Locator, inputs, capture, stopped, pause, event=lambda value: None,
                 point_action=None, waypoint=None, walking=None, page_detector=None):
        self.locator, self.inputs, self.capture, self.stopped, self._pause, self.event = locator, inputs, capture, stopped, pause, event
        self.point_action, self.waypoint = point_action, waypoint
        self.walking_detector = walking
        self.page_detector = page_detector
        self.turn_ratio = None
        self.motion = None
        self.next_skip_check = 0

    def pause(self, seconds):
        return self.motion.wait(seconds) if self.motion else self._pause(seconds)

    def observe(self, hint):
        self.inputs.check()
        if self.motion:
            self.motion.tick()
        frame = self.capture()
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
                return Pose(anchor[0], anchor[1], 0.0, 0.0, None, 0.0)
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
            pose = Pose(anchor[0], anchor[1], 0.0, 0.0, None, 0.0)
        else:
            pose = self.locator.locate(frame, hint, search_radius=26)
        if self.motion:
            self.motion.tick()
        self.event({"type": "pose", **pose.dict(), "heading_diagnostics": dict(self.locator.heading_diagnostics)})
        return pose

    def camera(self, pixels):
        from .controller import relative_camera
        # Never issue a large burst without observing its actual effect.
        part = max(-180, min(180, round(pixels)))
        self.inputs.check()
        if part and not relative_camera(self.inputs.controller, part, 0):
            raise NavigationError("转动镜头失败。")
        return part

    def stable_heading(self, pose):
        previous = None
        stable = 0
        for _ in range(6):
            self.inputs.check()
            if self.stopped() or self.pause(.04) is False:
                raise NavigationError("已停止导航。")
            pose = self.observe((pose.x, pose.y))
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
        for _ in range(4):
            before = pose.heading
            pixels = self.camera(90)
            self.pause(.3)
            pose = self.stable_heading(pose)
            delta = angle_delta(pose.heading, before)
            self.event({"type": "camera_probe", "pixels": pixels, "rotation": delta})
            if not 2 <= abs(delta) <= 120:
                ratios.clear()
                continue
            ratio = pixels/delta
            if ratios and ratio*ratios[-1] > 0 and .5 <= abs(ratio/ratios[-1]) <= 2:
                self.turn_ratio = (ratio+ratios[-1])/2
                self.event({"type": "camera_calibrated", "pixels_per_degree": self.turn_ratio})
                return pose
            ratios = [ratio]
        raise NavigationError("镜头校准结果不稳定，请检查游戏前台和小地图显示。")

    def turn(self, target_heading, pose):
        if pose.heading is None:
            raise SceneUnavailable("小地图暂时不可用，等待画面恢复。")
        if self.turn_ratio is not None and pose.heading is not None and abs(angle_delta(target_heading, pose.heading)) <= 5:
            return pose
        if self.turn_ratio is not None and abs(angle_delta(target_heading, pose.heading)) < 45:
            # A bounded small correction preserves continuous walking/jumping.
            # Larger turns and first calibration remain stationary and closed-loop.
            limit = 15 if self.motion and self.motion.jump_state != "idle" else 30
            delta = max(-limit, min(limit, angle_delta(target_heading, pose.heading)))
            self.camera(delta*self.turn_ratio)
            self.pause(.04)
            after = self.observe((pose.x, pose.y))
            if after.heading is None:
                raise SceneUnavailable("小地图暂时不可用，等待画面恢复。")
            return after
        # Calibration and closed-loop steering must not also walk past a point.
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
                if abs(delta) <= 5:
                    return pose
                if time.monotonic() >= deadline:
                    break
                before = pose.heading
                # At most 30 estimated degrees / 180 pixels, then read again.
                pixels = self.camera(max(-30, min(30, delta))*self.turn_ratio)
                self.pause(.15)
                pose = self.stable_heading(pose)
                actual = angle_delta(pose.heading, before)
                self.event({"type": "camera_turn", "target": target_heading, "before": before,
                            "after": pose.heading, "pixels": pixels, "rotation": actual})
                if abs(actual) < 1:
                    unresponsive += 1
                    if unresponsive >= 3:
                        raise NavigationError("镜头连续没有响应转向，已停止移动。")
                else:
                    unresponsive = 0
                    measured = pixels/actual
                    if measured*self.turn_ratio > 0 and abs(actual) <= 90:
                        self.turn_ratio = (self.turn_ratio+measured)/2
            raise NavigationError("镜头未能稳定朝向路线，已停止移动。")
        except BaseException:
            self.inputs.release()
            raise

    def walking(self):
        if self.walking_detector is not None:
            return bool(self.walking_detector())
        return not self.point_action or self.point_action.rt.ui.walking()

    def recover_scene(self, anchor, deadline):
        self.motion.close()
        reason = ("page_unavailable" if not self.inputs.ready() else
                  self.locator.heading_diagnostics.get("method", "camera_unavailable"))
        self.event({"type": "scene_wait", "reason": reason})
        end = min(deadline, time.monotonic()+15)
        previous = None
        page_ready = False
        while time.monotonic() < end:
            self.inputs.check()
            if self.stopped():
                raise NavigationError("已停止导航。")
            self.pause(.25)
            page_ready = self.inputs.ready()
            if not page_ready:
                if self.point_action and self.point_action.rt.hit("MaaNikki_Energy_SkipReward"):
                    if not self.point_action.rt.key("skip", .5):
                        raise NavigationError("未能跳过获取提示。")
                previous = None
                continue
            pose = self.observe((anchor.x, anchor.y))
            if pose.heading is not None:
                if previous is not None and distance(pose, previous) < 2:
                    self.event({"type": "scene_resumed", **pose.dict()})
                    return pose
                previous = pose
            else:
                previous = None
        if not page_ready:
            raise NavigationError("等待返回可移动的主界面超时。")
        raise NavigationError("主界面已恢复，但小地图位置或视角方向未能恢复稳定。")

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
        pose = self.observe((origin.x, origin.y))
        motion = Motion(self.inputs)
        self.motion = motion
        if self.point_action:
            self.point_action.motion = motion
        speeds, periods = [6.0]*5, [.2]*5
        last = None
        stuck_anchor, stuck_since, recovered = pose, time.monotonic(), False
        index = 0
        last_good = pose
        scene_interrupted, scene_retries = False, 0
        try:
            self.inputs.check()
            if not self.inputs.controller.post_click_key(18).wait().succeeded:
                raise NavigationError("未能解除游戏光标模式，尚未开始移动。")
            while index < len(route.points):
                if self.stopped() or time.monotonic() >= deadline:
                    raise NavigationError("导航已停止或超时。")
                motion.tick()
                target = route.points[index]
                if scene_interrupted or pose.heading is None or not self.inputs.ready():
                    scene_retries += 1
                    if scene_retries > 3:
                        raise NavigationError("路线多次中断，已停止，请检查当前位置。")
                    pose = self.recover_scene(last_good, deadline)
                    if distance(pose, last_good) > 4:
                        nearest = min(range(index+1), key=lambda n: distance(pose, route.points[n]))
                        if distance(pose, route.points[nearest]) > 12:
                            raise NavigationError("画面恢复后已偏离当前路线，请重新运行本项。")
                        index = nearest
                        self.event({"type": "route_resume", "index": index})
                    last_good = stuck_anchor = pose
                    stuck_since, recovered = time.monotonic(), False
                    scene_interrupted, last = False, None
                    continue
                last_good = pose
                if index == 0 and distance(pose, origin) > route.start_radius:
                    raise NavigationError("当前位置不在路线起点附近，未开始移动，请先传送或手动到达起点。")
                # Pickup locations require closer arrival than transit points.
                radius = min(target.radius, 1) if target.action == "bottle" else target.radius
                if distance(pose, target) <= radius or last and segment_distance(target, last, pose) <= radius:
                    scene_retries = 0
                    self.event({"type": "waypoint", "index": index, "total": len(route.points)-1})
                    if self.waypoint and self.waypoint(target) == "done":
                        break
                    if target.action:
                        motion.stop()
                        motion.jump(False, self.walking())
                        outcome = self.point_action(target)
                        if outcome == "done":
                            break
                        # Page actions/collection change both timers and pose.
                        self.locator.previous = None
                        pose = self.observe((target.x, target.y))
                        stuck_anchor, stuck_since, recovered = pose, time.monotonic(), False
                    elif index+1 < len(route.points) and target.jump != route.points[index+1].jump and motion.until:
                        motion.stop()
                        self.pause(.5 if route.points[index+1].jump else .2)
                    index += 1
                    last = None
                    continue
                if distance(pose, stuck_anchor) > 1:
                    # Count interruptions without progress, not the lifetime
                    # total of a route that has already resumed walking.
                    scene_retries = 0
                    stuck_anchor, stuck_since, recovered = pose, time.monotonic(), False
                stuck_time = time.monotonic()-stuck_since
                if stuck_time > 15:
                    raise NavigationError("原地卡住超过 15 秒，停止路线。")
                if stuck_time > 5 and not recovered:
                    self.event({"type": "unstuck", "seconds": stuck_time})
                    motion.stop()
                    motion.jump(False, True)
                    self.pause(1)
                    motion.forward(1)
                    motion.jump(True, True)
                    self.pause(1.5)
                    recovered = True
                turn_begin = time.monotonic()
                before_turn = pose
                bearing = math.degrees(math.atan2(target.x-pose.x, pose.y-target.y)) % 360
                delta = abs((bearing-(pose.heading or 0)+180) % 360-180)
                stationary_turn = self.turn_ratio is None or delta >= 45
                if delta >= 45:
                    motion.stop()
                try:
                    pose = self.turn(bearing, pose)
                except SceneUnavailable:
                    scene_interrupted = True
                    continue
                # Only stationary steering is excluded from the stuck timer.
                if stationary_turn:
                    stuck_since += time.monotonic()-turn_begin
                if distance(pose, target) <= radius or segment_distance(target, before_turn, pose) <= radius:
                    last = before_turn
                    continue
                begin = time.monotonic()
                walking = self.walking()
                motion.jump(target.jump, walking)
                velocity = (sum(speeds)-max(speeds)-min(speeds))/3
                period = (sum(periods)-max(periods)-min(periods))/3
                duration = max(.05, distance(pose, target)/max(velocity, .1)-period)
                motion.forward(duration)
                self.event({"type": "forward", "duration": duration, "bearing": bearing, "jump": target.jump,
                            "walking": walking, "jump_state": motion.jump_state})
                if motion.jump_state in {"first", "second_wait"}:
                    # Complete the 0.3s hold + 0.1s second-jump timing before
                    # screenshot/OCR RPCs can delay it. Still one Maa thread.
                    motion.wait(.45)
                self.pause(.01)
                after = self.observe((pose.x, pose.y))
                elapsed = time.monotonic()-begin
                if distance(pose, after) > .5 and elapsed > 0:
                    speeds = speeds[1:]+[distance(pose, after)/elapsed]
                periods = periods[1:]+[elapsed]
                last, pose = pose, after
            self.event({"type": "arrived", **pose.dict()})
            return pose
        finally:
            try:
                motion.close()
            finally:
                self.motion = None
                if self.point_action:
                    self.point_action.motion = None
