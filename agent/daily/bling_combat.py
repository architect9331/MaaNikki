"""Open-world Bling monster combat for Fan Suit (万相渡浮生) standoff auto-absorb."""
from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime
import math
import re
from task_control import task_time as time

import cv2
import numpy as np

from adventure_report import try_record_adventure_entry
from file_io import write_json
from navigation.controller import BackgroundInput, ForegroundInput
from navigation.engine import Navigator, angle_delta
from navigation.models import GameWindowClosed, NavigationError, Route, load_route, maps, parse_route
from navigation.session import create_navigator
from navigation.startup_abilities import ensure_persistent_abilities
from navigation.teleport import Teleporter
from navigation.vision import Locator
from .runtime import RESOURCE, Runtime
from .settings import ROOT, foreground_inputs as inputs_allowed
from .startup import focus_game

M_PER_MAP_PX = 0.90
_CAP_RE = re.compile(r"已达.*上限|今日.*上限|获取上限|达到上限|无法获得更多")
_ROUTE_MAP = {
    "greedy_pouch_guaranteed": ("bling_greedy_pouch_guaranteed",),
    "greedy_pouch_all": ("bling_greedy_pouch_all",),
    "duoduo_bird": ("bling_duoduo_bird",),
    "both_guaranteed": ("bling_greedy_pouch_guaranteed", "bling_duoduo_bird"),
}


def read_goddess_hud_state(frame) -> tuple[bool, bool, bool, int]:
    """Read live E/R readiness and Heart-Oath pips from a 1280x720 main HUD frame.

    Rationale and limits: uses the always-lit [Q] bow icon (1195..1232, 514..552) as a
    HUD gate (0.08 <= white <= 0.45) to reject maps, menus, and full-white frames, then
    requires both opaque white glyph/keycap strokes and cyan cost diamonds for E and R.
    Only 16:9 captures (>=960 px wide) are scaled to 1280x720; other layouts fail closed
    to (False, False, False, 0) so callers fall back safely.
    """
    if frame is None or not hasattr(frame, "shape"):
        return False, False, False, 0
    fh, fw = frame.shape[:2]
    if (fw, fh) != (1280, 720):
        if fw < 960 or fh <= 0 or abs((fw / fh) - (16.0 / 9.0)) > 0.02:
            return False, False, False, 0
        frame = cv2.resize(frame, (1280, 720), interpolation=cv2.INTER_AREA)

    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV).astype(np.float32)
    bgr = frame.astype(np.float32)

    def white_frac(x0: int, y0: int, x1: int, y1: int) -> float:
        v = hsv[y0:y1, x0:x1, 2]
        s = hsv[y0:y1, x0:x1, 1]
        return float(((v >= 210.0) & (s <= 45.0)).mean())

    def cyan_count(x0: int, y0: int, x1: int, y1: int) -> int:
        h, s, v = hsv[y0:y1, x0:x1, 0], hsv[y0:y1, x0:x1, 1], hsv[y0:y1, x0:x1, 2]
        b, g, r = bgr[y0:y1, x0:x1, 0], bgr[y0:y1, x0:x1, 1], bgr[y0:y1, x0:x1, 2]
        mask = (h >= 75.0) & (h <= 115.0) & (s >= 60.0) & (v >= 175.0) & (b > r + 25.0) & (g > r + 25.0)
        return int(mask.sum())

    q_white = white_frac(1195, 514, 1232, 552)
    if not (0.08 <= q_white <= 0.45):
        return False, False, False, 0

    e_ready = (
        white_frac(1171, 467, 1205, 490) >= 0.05
        and white_frac(1179, 491, 1197, 502) >= 0.15
        and cyan_count(1170, 456, 1206, 468) >= 25
    )
    r_ready = (
        white_frac(1222, 467, 1258, 490) >= 0.025
        and white_frac(1231, 491, 1249, 502) >= 0.12
        and cyan_count(1228, 456, 1252, 468) >= 15
    )
    pips = sum(
        1
        for i in range(6)
        if cyan_count(int(round(585 + i * 15.5)) - 6, 661, int(round(585 + i * 15.5)) + 7, 674) >= 35
    )
    return True, e_ready, r_ready, pips


def _split_route_by_teleport(route: Route) -> list[dict]:
    """Split a multi-spire route JSON into independent single-spire leg documents."""
    doc = route.document
    pts = list(doc.get("points") or [])
    if not pts:
        return [deepcopy(doc)]
    legs: list[dict] = []
    cur_tp = doc.get("teleport")
    cur_pts: list[dict] = []
    for pt in pts:
        if pt.get("action") == "teleport":
            if cur_pts and any(p.get("action") == "bling_combat" for p in cur_pts):
                legs.append({
                    **doc,
                    "id": f"{doc['id']}_leg_{len(legs) + 1}",
                    "teleport": deepcopy(cur_tp),
                    "points": cur_pts,
                })
            cur_tp = (pt.get("params") or {}).get("checkpoint") or cur_tp
            cur_pts = [{"x": pt["x"], "y": pt["y"], "radius": 8.5, "jump": False, "action": "", "params": {}}]
        else:
            cur_pts.append(deepcopy(pt))
    if cur_pts and any(p.get("action") == "bling_combat" for p in cur_pts):
        legs.append({
            **doc,
            "id": f"{doc['id']}_leg_{len(legs) + 1}",
            "teleport": deepcopy(cur_tp),
            "points": cur_pts,
        })
    return legs or [deepcopy(doc)]


class BlingPointActions:
    """Point executor driving energy-safe Goddess combat and two-stage Fan-suit absorption."""

    MAX_ENERGY = 400.0
    BLACKHOLE_COST = 400.0
    WINDMILL_COST = 200.0

    def __init__(
        self,
        rt: Runtime,
        teleporter: Teleporter,
        inputs,
        *,
        blackhole_enabled: bool = True,
        windmill_enabled: bool = True,
        event=lambda value: None,
    ):
        self.rt = rt
        self.teleporter = teleporter
        self.inputs = inputs
        self.navigator: Navigator | None = None
        self.motion = None
        self.blackhole_enabled = blackhole_enabled
        self.windmill_enabled = windmill_enabled
        self.event = event
        self.executor = "bling_combat"
        self.energy = self.MAX_ENERGY
        self.last_energy_at = time.monotonic()
        self.cap_reached = False
        self._last_pose = None
        self.stats = {
            "attempted": 0,
            "random_quick_taps": 0,
            "blackholes": 0,
            "windmills": 0,
            "light_bursts": 0,
            "cleanup_bursts": 0,
            "rush_strikes": 0,
            "hud_reads": 0,
        }

    def prepare_point(self, point, pose) -> bool:
        if point.action != "teleport":
            return False
        nav = self.navigator
        if nav and nav.motion:
            nav.motion.stop()
            nav.motion.jump(False, self.rt.ui.walking())
        checkpoint = (point.params or {}).get("checkpoint")
        if checkpoint and math.hypot(pose.x - point.x, pose.y - point.y) >= 15.0:
            self.teleporter.transport(checkpoint)
        if nav is not None:
            nav.turn_ratio = None
        return True

    def _refresh_energy(self) -> None:
        now = time.monotonic()
        dt = max(0.0, now - self.last_energy_at)
        self.last_energy_at = now
        self.energy = min(self.MAX_ENERGY, self.energy + dt * 10.0)

    def _align_camera(
        self,
        target_heading: float,
        anchor_xy: tuple[float, float],
        spawn_xy: tuple[float, float] | None = None,
    ):
        nav = self.navigator
        if nav is None:
            return None
        pose = nav.observe(anchor_xy)
        if spawn_xy is not None and pose is not None and pose.position_reliable:
            target_heading = math.degrees(math.atan2(spawn_xy[0] - pose.x, pose.y - spawn_xy[1])) % 360.0
        if pose.heading is None or abs(angle_delta(target_heading, pose.heading)) > 18.0:
            if nav.use_wasd_steering:
                for _ in range(2):
                    if pose.heading is not None and abs(angle_delta(target_heading, pose.heading)) <= 20.0:
                        break
                    try:
                        pose, _ = nav._wasd_snap_camera(target_heading, pose, stationary=True)
                    except NavigationError:
                        pose = nav.observe(anchor_xy)
                        break
            else:
                with self.rt.foreground_task("对齐视角面向目标"):
                    pose = nav.face_heading(target_heading, tolerance=16)
        # Brief 0.06s forward nudge aligns character Root forward with camera for Black Hole offset.
        self.inputs.hold("forward", 0.06)
        try:
            hint = (pose.x, pose.y) if (pose is not None and pose.position_reliable) else anchor_xy
            pose = nav.observe(hint)
        except NavigationError:
            pass
        self._last_pose = pose
        return pose

    def _advance_toward_spawn(
        self,
        spawn_xy: tuple[float, float],
        target_dist_m: float,
        *,
        max_seconds: float,
        initial_pose=None,
    ):
        """Advance smoothly toward spawn_xy in closed loop until target distance or timeout."""
        if self.rt.stopped:
            return initial_pose
        nav = self.navigator
        if nav is None:
            return initial_pose
        pose = initial_pose
        if pose is None or not pose.position_reliable:
            try:
                pose = nav.observe(spawn_xy)
            except NavigationError:
                return None
        cur_m = math.hypot(spawn_xy[0] - pose.x, spawn_xy[1] - pose.y) * M_PER_MAP_PX
        if cur_m <= target_dist_m + 0.35:
            self._last_pose = pose
            return pose

        min_dist_m = cur_m
        last_xy = (pose.x, pose.y)
        started = time.monotonic()
        self.inputs.key_down("forward")
        try:
            while not self.rt.stopped:
                if not self.rt.pause(0.08):
                    break
                try:
                    nxt = nav.observe(last_xy)
                except NavigationError:
                    break
                pose = nxt
                if not pose.position_reliable:
                    break
                last_xy = (pose.x, pose.y)
                dist_m = math.hypot(spawn_xy[0] - pose.x, spawn_xy[1] - pose.y) * M_PER_MAP_PX
                if dist_m < min_dist_m:
                    min_dist_m = dist_m
                if dist_m <= target_dist_m or dist_m > min_dist_m + 1.4:
                    break
                if time.monotonic() - started >= max_seconds:
                    break
        finally:
            try:
                self.inputs.key_up("forward")
            except Exception:
                self.inputs.release()
        self._last_pose = pose
        return pose

    def _light_attack_burst(self, taps: int = 8, *, interval: float = 0.19) -> None:
        """Tap attack briefly without ever holding >0.15s (avoids camera-dependent charged bow)."""
        is_mouse = self.rt.game_keys.get("attack").kind == "mouse"
        with (self.rt.foreground_task("战斗轻击输出") if is_mouse else nullcontext()):
            for idx in range(taps):
                if self.rt.stopped:
                    break
                if idx and not self.rt.pause(interval):
                    break
                self.inputs.hold("attack", 0.06)
        self.stats["light_bursts"] += 1
        self.energy = min(self.MAX_ENERGY, self.energy + taps * 10.0)

    def _cast_combat_sequence(
        self,
        monster_type: str,
        spawn_mode: str,
        spawn_xy: tuple[float, float],
        pose,
        corridor_clear: bool,
        fallback_standoff_m: float = 15.0,
    ) -> bool:
        hud_ok, hud_e, hud_r, hud_pips = read_goddess_hud_state(self.rt.capture())
        if hud_ok:
            self.stats["hud_reads"] += 1
            can_e = self.blackhole_enabled and hud_e
            can_r = self.windmill_enabled and hud_r
            self.energy = float(hud_pips) * 100.0
            self.last_energy_at = time.monotonic()
        else:
            self._refresh_energy()
            can_e = self.blackhole_enabled and self.energy >= self.BLACKHOLE_COST
            can_r = self.windmill_enabled and self.energy >= self.WINDMILL_COST

        if pose is not None and pose.position_reliable:
            cur_dist_m = math.hypot(spawn_xy[0] - pose.x, spawn_xy[1] - pose.y) * M_PER_MAP_PX
        else:
            cur_dist_m = fallback_standoff_m

        if spawn_mode == "random":
            # Never spend 400 energy blindly on optional random points.
            if corridor_clear and cur_dist_m > 17.2:
                pose = self._advance_toward_spawn(
                    spawn_xy,
                    16.6,
                    max_seconds=max(0.6, (cur_dist_m - 16.6) / 3.8 + 0.8),
                    initial_pose=pose,
                )
                self.stats["rush_strikes"] += 1
            self.stats["random_quick_taps"] += 1
            self._light_attack_burst(taps=6)
            self.energy = min(self.MAX_ENERGY, self.energy + 10.0)
            return False

        is_bird = monster_type == "duoduo_bird"
        if can_e:
            if corridor_clear and cur_dist_m > 12.2:
                pose = self._advance_toward_spawn(
                    spawn_xy,
                    11.8,
                    max_seconds=max(0.8, (cur_dist_m - 11.8) / 3.8 + 0.8),
                    initial_pose=pose,
                )
                self.stats["rush_strikes"] += 1
            self.inputs.hold("ability_config", 0.10)
            self.energy = 0.0
            self.stats["blackholes"] += 1
            self.rt.pause(0.40)
            self._light_attack_burst(taps=8)
            self.energy = min(self.MAX_ENERGY, self.energy + 10.0)
            return True

        if can_r:
            if corridor_clear and cur_dist_m > 17.6:
                pose = self._advance_toward_spawn(
                    spawn_xy,
                    17.0,
                    max_seconds=max(0.6, (cur_dist_m - 17.0) / 3.8 + 0.8),
                    initial_pose=pose,
                )
                self.stats["rush_strikes"] += 1
            self.inputs.hold("sub_ability", 0.10)
            self.energy = max(0.0, self.energy - self.WINDMILL_COST)
            self.stats["windmills"] += 1
            self.rt.pause(0.25)
            self._light_attack_burst(taps=9 if is_bird else 8)
            self.energy = min(self.MAX_ENERGY, self.energy + 10.0)
            return False

        if corridor_clear and cur_dist_m > 17.2:
            pose = self._advance_toward_spawn(
                spawn_xy,
                16.6,
                max_seconds=max(0.6, (cur_dist_m - 16.6) / 3.8 + 0.8),
                initial_pose=pose,
            )
            self.stats["rush_strikes"] += 1
        self._light_attack_burst(taps=12 if is_bird else 10)
        self.energy = min(self.MAX_ENERGY, self.energy + 10.0)
        return False

    def _absorb_and_cleanup(
        self,
        monster_type: str,
        spawn_mode: str,
        spawn_xy: tuple[float, float],
        pose,
        corridor_clear: bool,
    ) -> None:
        """Advance into Fan Suit 8m viewport sphere and run extra cleanup light bursts."""
        drop_wait = 1.0 if spawn_mode == "random" else (2.2 if monster_type == "duoduo_bird" else 1.3)
        if not self.rt.pause(drop_wait):
            return
        is_bird = monster_type == "duoduo_bird"
        if corridor_clear:
            pose = self._advance_toward_spawn(spawn_xy, target_dist_m=5.2, max_seconds=3.8, initial_pose=pose)
            if spawn_mode != "random" and not self.rt.stopped:
                self._light_attack_burst(taps=4)
                self.stats["cleanup_bursts"] += 1
            wait1 = 0.9 if spawn_mode == "random" else (1.2 if is_bird else 1.0)
            if not self.rt.pause(wait1):
                return
            if spawn_mode != "random":
                self._advance_toward_spawn(spawn_xy, target_dist_m=2.4, max_seconds=1.8, initial_pose=pose)
                self.rt.pause(1.2 if is_bird else 1.0)
        else:
            if spawn_mode != "random" and not self.rt.stopped:
                self._light_attack_burst(taps=4)
                self.stats["cleanup_bursts"] += 1
            if not self.rt.pause(1.2):
                return
            self.inputs.hold("forward", 0.30)
            self.rt.pause(1.2)

    def _check_daily_cap(self) -> bool:
        try:
            txt = self.rt.text(self.rt.ui.roi("AreaMaterialGetText"), ".+")
            if txt and _CAP_RE.search(re.sub(r"\s+", "", txt)):
                self.cap_reached = True
                return True
        except Exception:
            pass
        return False

    def __call__(self, point) -> str:
        if point.action != "bling_combat":
            return "continue"
        params = point.params or {}
        monster_type = str(params.get("monster_type") or "greedy_pouch")
        spawn_mode = str(params.get("spawn_mode") or "guaranteed")
        heading = float(params.get("facing_heading") or 0.0)
        sx = params.get("spawn_x")
        sy = params.get("spawn_y")
        spawn_xy = (float(sx), float(sy)) if (sx is not None and sy is not None) else (point.x, point.y)
        corridor_clear = bool(params.get("corridor_clear", True))
        fallback_standoff_m = float(params.get("standoff_m") or 15.0)

        self.stats["attempted"] += 1
        pose = self._align_camera(heading, (point.x, point.y), spawn_xy=spawn_xy)
        self._cast_combat_sequence(
            monster_type,
            spawn_mode,
            spawn_xy,
            pose,
            corridor_clear,
            fallback_standoff_m=fallback_standoff_m,
        )
        post_pose = self._last_pose if self._last_pose is not None else pose
        self._absorb_and_cleanup(monster_type, spawn_mode, spawn_xy, post_pose, corridor_clear)

        self.inputs.check()
        if not self.rt.main(self.event):
            raise NavigationError("战斗或拾取后未能返回主界面。")
        if self._check_daily_cap():
            self.rt.log("已检测到今日大世界噗灵获取上限，提前结束任务。")
            return "done"
        return "continue"


class BlingCombatRun(Runtime):
    """Autonomous open-world Bling monster hunter for Fan Suit standoff auto-absorb."""

    def run(self, settings):
        settings = settings or {}
        target_mode = str(settings.get("target_mode") or "greedy_pouch_guaranteed")
        ensure_fan = bool(settings.get("ensure_fan_absorb", True))
        fan_plan = int(settings.get("fan_ability_plan") or 3)
        blackhole_enabled = bool(settings.get("goddess_blackhole_enabled", True))
        windmill_enabled = bool(settings.get("goddess_windmill_enabled", True))
        goddess_slot = int(settings.get("goddess_slot") or 0)
        route_names = _ROUTE_MAP.get(target_mode, _ROUTE_MAP["greedy_pouch_guaranteed"])

        bg = self.background_mode()
        if not bg and not inputs_allowed():
            raise NavigationError("当前连接需要使用前台鼠标与键盘权限，请先开启或切换至后台模式。")

        routes: list[Route] = []
        for name in route_names:
            r = load_route(RESOURCE, name)
            if r is None:
                raise NavigationError(f"无法读取噗灵战斗路线：{name}")
            routes.append(r)

        if ensure_fan and not self.stopped:
            self.log("正在检查并开启常驻能力「化万相·自动拾取噗灵」。", detail=True)
            fan_events: list[dict] = []
            ensure_persistent_abilities(
                self,
                {"abilities": {"mask_absorb": True}, "ability_plan": fan_plan},
                events=fan_events,
            )
            if self.stopped:
                return False

        spec = maps(RESOURCE)["miraland"]
        locator = Locator(spec)
        inputs = (
            BackgroundInput(
                self.controller, lambda: self.stopped,
                lambda: self.hit("MaaNikki_MainDetected"), bindings=self.game_keys,
            )
            if bg else
            ForegroundInput(
                self.controller, lambda: self.stopped,
                lambda: self.hit("MaaNikki_MainDetected"), bindings=self.game_keys,
            )
        )
        self.navigation_inputs = inputs
        summary = {
            "target_mode": target_mode,
            "ensure_fan_absorb": ensure_fan,
            "fan_ability_plan": fan_plan,
            "legs_planned": 0,
            "legs_completed": 0,
            "legs_skipped_locked": 0,
            "cap_reached": False,
        }
        actions: BlingPointActions | None = None
        try:
            if not bg:
                focus_game(self, inputs)
            else:
                inputs.check()
            if goddess_slot in range(1, 9):
                inputs.hold(f"ability_{goddess_slot}", 0.12)
                self.pause(0.35)

            teleporter = Teleporter(self, locator, lambda ev: None, inputs)
            actions = BlingPointActions(
                self, teleporter, inputs,
                blackhole_enabled=blackhole_enabled,
                windmill_enabled=windmill_enabled,
            )
            legs = [leg for r in routes for leg in _split_route_by_teleport(r)]
            summary["legs_planned"] = len(legs)
            locked_regions: set[str] = set()
            self.log(f"开始扇套站桩大世界刷噗灵：共 {len(legs)} 个传送区域。")

            for idx, leg_doc in enumerate(legs, 1):
                if self.stopped or actions.cap_reached:
                    break
                tp = leg_doc.get("teleport") or {}
                region = str(tp.get("region") or "")
                if region and region in locked_regions:
                    summary["legs_skipped_locked"] += 1
                    continue
                leg_model = parse_route(leg_doc)
                try:
                    teleporter.prepare(leg_model)
                    nav = create_navigator(self, teleporter, actions=actions, event=lambda ev: None)
                    nav.follow(leg_model)
                    summary["legs_completed"] += 1
                except NavigationError as error:
                    err_text = str(error)
                    if "尚未解锁" in err_text:
                        if region:
                            locked_regions.add(region)
                        summary["legs_skipped_locked"] += 1
                        self.log(f"第 {idx}/{len(legs)} 区域「{region or tp.get('name', '')}」尚未解锁，已跳过。")
                        continue
                    raise
                if actions._check_daily_cap():
                    self.log("已检测到今日大世界噗灵获取上限，结束刷取。")
                    break

            summary["cap_reached"] = actions.cap_reached
            summary.update(actions.stats)
            ok = not self.stopped and (summary["legs_completed"] > 0 or actions.cap_reached)
            self.log(
                f"大世界刷噗灵完成：经过 {actions.stats['attempted']} 处，"
                f"黑洞 {actions.stats['blackholes']} 次、风车 {actions.stats['windmills']} 次。"
            )
            return ok
        except (NavigationError, GameWindowClosed, OSError, RuntimeError, ValueError, cv2.error) as error:
            self.log(f"大世界刷噗灵停止：{error}")
            summary["error"] = str(error)
            return False
        finally:
            if actions is not None:
                summary["cap_reached"] = actions.cap_reached
                summary.update(actions.stats)
            try:
                inputs.release()
            except Exception:
                pass
            self.navigation_inputs = None
            try:
                folder = ROOT / "logs/bling_combat"
                folder.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                write_json(folder / f"{stamp}.json", summary)
                if summary.get("attempted", 0) > 0:
                    try_record_adventure_entry(
                        ROOT, self,
                        category="open_world",
                        task="bling_combat",
                        title="大世界怪刷噗灵（扇套）",
                        detail=f"经过 {summary['attempted']} 处 · 黑洞 {summary.get('blackholes', 0)} 次",
                        runs=int(summary["attempted"]),
                        status="completed",
                    )
            except Exception:
                pass
