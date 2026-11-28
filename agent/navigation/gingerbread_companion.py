"""Ensure the 幸福雪未融 Gingerbread companion is summoned before open-world collection.

HUD evidence (bottom-right, 1280x720 frames, measured from 1080p player captures
scaled by 2/3, relative to the centre of the main ability button ROI):

- Companion not out: one secondary button sits directly above the main button,
  its key badge ("能力衍生功能1", default G) about 39 px above the main centre.
- Companion out: that button moves ~26 px left (its icon becomes a swap arrow)
  and an extra button appears ~26 px right with its own key badge
  ("能力衍生功能2", default T) on the same row.

Recognition rule and rationale:
The icons themselves are white strokes on a translucent disc, so on snow, sunlit
water or sky they disappear into the background. A key badge, however, is an
opaque near-white rounded box that always contains a darker glyph (the bound key
letter or a mouse symbol). We therefore look for *enclosed darker holes inside a
near-white plateau* at the badge row: on dark/grass/night backgrounds the badge is
an isolated plateau with a letter hole; on bright snow the plateau merges with the
background but the letter still forms an enclosed hole. Texture alone rarely makes a
letter-sized hole surrounded by a badge-sized near-white box exactly on the badge row.

The state is decided from the *pattern* of the three badge positions, not from any
single detection:
- out:   left and right badges present, centre absent;
- in:    centre badge present, both sides absent (also requires the main ability icon
         to be 幸福雪未融, checked separately);
- anything else is unknown and is never acted on.
A spurious hole can therefore only turn a decision into "unknown", and a key is only
pressed after several consecutive frames agree on "in".

Limits: the letter itself is not read (players may rebind keys), so another ability
with two secondary buttons at the same positions would look "out"; this is excluded
by first confirming the main ability icon. Positions assume the default HUD layout and
scale; a moved or scaled HUD reports "unknown" and stops instead of pressing keys.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
from pathlib import Path
from types import SimpleNamespace
import time as wall_time

import cv2
import numpy as np

from daily.settings import ROOT
from file_io import write_json
from .ability import Ability
from .models import NavigationError

LABEL = "幸福雪未融"
ICON = "IconAbilityGingerbread"
NODE = "MaaNikki_Navigation_GingerbreadAbility"
SUMMON_KEY = "ability_derive_1"

# 720p offsets from the main ability ROI centre; see module docstring.
BADGE_DY = -39
SIDE_DX = 26
POSITION_TOLERANCE = (6, 5)
# Near-white badge plateau. Badges are opaque (V ~250, S ~0); keep a margin for
# 720p downscaling while rejecting saturated foliage, water and warm UI glow.
BADGE_V, BADGE_S = 225, 45
# Glyph holes after downscaling: one stroked letter or mouse symbol.
HOLE_AREA = (2, 60)
HOLE_MAX_W, HOLE_H = 12, (3, 11)
BADGE_BOX = (13, 12)
BADGE_FILL = .6
# Sampled badges: plateau mean S ~8, glyph ~50-65 V darker than the plateau.
# Rejected light-menu texture: plateau S 27-45 and holes only ~10-25 V darker.
PLATEAU_MAX_S = 20
GLYPH_CONTRAST = 30
CONFIRM_FRAMES = 3


def badge_centres(frame, centre):
    """Return 720p centres of key badges found on the secondary-button row."""
    cx, cy = centre
    row = cy + BADGE_DY
    x0, y0 = max(0, cx - SIDE_DX - 24), max(0, row - 16)
    x1, y1 = min(frame.shape[1], cx + SIDE_DX + 24), min(frame.shape[0], row + 16)
    patch = frame[y0:y1, x0:x1]
    if patch.size == 0:
        return []
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    bright = ((hsv[..., 2] >= BADGE_V) & (hsv[..., 1] <= BADGE_S)).astype(np.uint8)
    # Holes are 4-connected darker regions that do not touch the search border,
    # i.e. fully enclosed by the (8-connected) near-white plateau.
    count, labels, stats, _ = cv2.connectedComponentsWithStats(1 - bright, connectivity=4)
    height, width = bright.shape
    found = []
    for index in range(1, count):
        x, y, w, h, area = stats[index]
        if x == 0 or y == 0 or x + w >= width or y + h >= height:
            continue
        if not (HOLE_AREA[0] <= area <= HOLE_AREA[1] and w <= HOLE_MAX_W
                and HOLE_H[0] <= h <= HOLE_H[1]):
            continue
        hx, hy = x + w / 2, y + h / 2
        bw, bh = max(BADGE_BOX[0], w + 6), max(BADGE_BOX[1], h + 5)
        bx0, by0 = max(0, round(hx - bw / 2)), max(0, round(hy - bh / 2))
        box = bright[by0:by0 + bh, bx0:bx0 + bw]
        holes = labels[by0:by0 + bh, bx0:bx0 + bw] == index
        rest = box.size - int(holes.sum())
        if rest <= 0 or float(box[~holes].sum()) / rest < BADGE_FILL:
            continue
        # A badge is neutral white with a clearly darker glyph. Light tinted
        # surfaces (sky, lit snow, UI glow) only cross the saturation limit
        # in patches and leave "holes" that are barely darker than the rest.
        plateau = box.astype(bool) & ~holes
        values = hsv[by0:by0 + bh, bx0:bx0 + bw]
        plateau_v = float(values[..., 2][plateau].mean())
        if (float(values[..., 1][plateau].mean()) > PLATEAU_MAX_S
                or float(hsv[..., 2][labels == index].mean()) > plateau_v - GLYPH_CONTRAST):
            continue
        found.append((x0 + hx, y0 + hy))
    merged = []
    for point in found:
        # Multi-part glyphs (e.g. letters with separate strokes) are one badge.
        for item in merged:
            if abs(item[0] - point[0]) <= 6 and abs(item[1] - point[1]) <= 5:
                break
        else:
            merged.append(point)
    return merged


def companion_state(frame, centre):
    """Classify the secondary-button row as "out", "in" or "unknown"."""
    if frame is None:
        return "unknown", []
    cx, cy = centre
    row = cy + BADGE_DY
    badges = badge_centres(frame, centre)
    tx, ty = POSITION_TOLERANCE
    def present(dx):
        return any(abs(x - (cx + dx)) <= tx and abs(y - row) <= ty for x, y in badges)
    left, middle, right = present(-SIDE_DX), present(0), present(SIDE_DX)
    if left and right and not middle:
        return "out", badges
    if middle and not left and not right:
        return "in", badges
    return "unknown", badges


class GingerbreadAbility(Ability):
    """幸福雪未融 in the ability wheel; the plan comes from the collection task option."""

    def __init__(self, actions, plan):
        super().__init__(actions, LABEL, ICON, NODE, "gingerbread", "gingerbread")
        self.plan = plan

    def ability_plan(self):
        return self.plan


def main_centre(runtime):
    x, y, w, h = runtime.ui.roi("AreaAbilityButton")
    return round(x + w / 2), round(y + h / 2)


def observe(runtime, centre, seconds, *, want=None, abort_on_steady=None, steady_seconds=5.8):
    """Return a state only after CONFIRM_FRAMES consecutive frames agree.

    With ``want`` set, keep watching until that state is confirmed or time runs out;
    if ``abort_on_steady`` is set and the state never leaves ``abort_on_steady`` after
    ``steady_seconds`` (exceeding the ~3.8-4.5s summon bell animation so a post-switch
    swallowed keypress can be retried safely without interrupting a live summon), return early."""
    from task_control import task_time as time
    started = time.monotonic()
    deadline = started + seconds
    grace_extended = False
    saw_change = False
    last, streak, badges = None, 0, []
    while not runtime.stopped:
        state, badges = companion_state(runtime.capture(), centre)
        streak = streak + 1 if state == last else 1
        last = state
        if abort_on_steady is not None and state != abort_on_steady:
            saw_change = True
        if state != "unknown" and streak >= CONFIRM_FRAMES and (want is None or state == want):
            break
        now = time.monotonic()
        if (abort_on_steady is not None
                and not saw_change
                and state == abort_on_steady
                and streak >= CONFIRM_FRAMES
                and now - started >= steady_seconds):
            break
        if now >= deadline:
            if not grace_extended and want is not None and state == want and streak < CONFIRM_FRAMES:
                grace_extended = True
                deadline = now + 1.0
            else:
                break
        if not runtime.pause(.25):
            break
    confirmed = last if last and last != "unknown" and streak >= CONFIRM_FRAMES else "unknown"
    return confirmed, badges


def ensure_companion(runtime, inputs, event, plan, actions=None):
    """Select 幸福雪未融 and make sure the Gingerbread companion is out.

    Presses 能力衍生功能1 only after the "not out" layout is confirmed; never presses
    it while the companion is out, because the same key then switches it instead."""
    if type(plan) is not int or plan not in (1, 2, 3):
        raise NavigationError("小姜饼人能力方案设置无效，请选择方案一、二或三。")
    if actions is None:
        from .ability import ability_matches
        actions = SimpleNamespace(rt=runtime, inputs=inputs, event=event,
                                  ability=lambda node: ability_matches(runtime, node))
    if not runtime.main():
        raise NavigationError("未能回到游戏主界面，尚未召唤小姜饼人。")
    if not GingerbreadAbility(actions, plan).select():
        raise NavigationError(f"未能切换到{LABEL}能力，尚未召唤小姜饼人。")
    centre = main_centre(runtime)
    state, badges = observe(runtime, centre, 3)
    event({"type": "gingerbread_companion", "phase": "check", "state": state,
           "badges": [[round(x, 1), round(y, 1)] for x, y in badges]})
    if state == "out":
        mark_gingerbread_active(runtime)
        runtime.log("小姜饼人已在身边，直接开始采集。", detail=True)
        return True
    if state != "in":
        raise NavigationError("右下角未能确认小姜饼人的召唤按钮，请确认能力按钮未被遮挡后重试。")
    clear_gingerbread_active(runtime)
    for attempt in (1, 2, 3):
        if runtime.stopped:
            raise NavigationError("已停止，尚未召唤小姜饼人。")
        if not runtime.key(SUMMON_KEY, .3):
            raise NavigationError("未能按下「能力衍生功能1」，请在按键设置中确认该按键。")
        # The summoning bell animation keeps the HUD badge "in" for ~3.8-4.5s before
        # splitting to left/right badges (+ ~0.75s to confirm). Allow 8s total while
        # only aborting after steady_seconds (5.8s) if cooldown swallowed the keypress.
        state, badges = observe(runtime, centre, 8.0, want="out", abort_on_steady="in")
        event({"type": "gingerbread_companion", "phase": "summon", "attempt": attempt,
               "state": state, "badges": [[round(x, 1), round(y, 1)] for x, y in badges]})
        if state == "out":
            mark_gingerbread_active(runtime)
            runtime.log("已召唤小姜饼人随行。", detail=True)
            return True
        if state != "in":
            # Only press again when the "not out" layout is still confirmed;
            # an uncertain frame may mean the first press already worked.
            break
    raise NavigationError("已按下召唤键，但未能确认小姜饼人出现，请手动召唤后重试。")


_ACTIVE_SESSIONS: dict[str, dict] = {}
_STATE_FILE = ROOT / "logs" / "navigation" / ".gingerbread_active.json"
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _window_process_identity(hwnd: int) -> tuple[int, int] | None:
    """Return (pid, creation_filetime) for a live Win32 window handle."""
    if not hwnd:
        return None
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(wintypes.HWND(int(hwnd)), ctypes.byref(pid))
        if not pid.value:
            return None
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not handle:
            return int(pid.value), 0
        try:
            creation = wintypes.FILETIME()
            exit_t = wintypes.FILETIME()
            kernel_t = wintypes.FILETIME()
            user_t = wintypes.FILETIME()
            if kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_t),
                ctypes.byref(kernel_t),
                ctypes.byref(user_t),
            ):
                created = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
                return int(pid.value), created
            return int(pid.value), 0
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, ValueError, TypeError):
        return None


def _session_identity(runtime) -> tuple[str, int, int, str]:
    """Identify the connected live game process (falls back to controller UUID)."""
    hwnd = None
    if hasattr(runtime, "_pointer_guard_context"):
        try:
            hwnd, _ = runtime._pointer_guard_context(getattr(runtime, "navigation_inputs", None))
        except Exception:
            hwnd = None
    proc = _window_process_identity(hwnd) if hwnd else None
    uid = str(getattr(runtime, "_account_uid", None) or "")
    if proc is not None:
        pid, created = proc
        return f"pid:{pid}:{created}", pid, created, uid
    uuid = str(getattr(getattr(runtime, "controller", None), "uuid", None) or "default")
    return f"ctrl:{uuid}", 0, 0, uid


def mark_gingerbread_active(runtime) -> None:
    """Record that the Gingerbread companion is confirmed summoned for this live game process."""
    key, pid, created, uid = _session_identity(runtime)
    record = {
        "key": key,
        "pid": pid,
        "created": created,
        "uid": uid,
        "confirmed_at": wall_time.time(),
    }
    _ACTIVE_SESSIONS[key] = record
    if pid:
        try:
            _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            write_json(_STATE_FILE, record)
        except OSError:
            pass


def clear_gingerbread_active(runtime) -> None:
    """Clear remembered Gingerbread active state when confirmed dismissed."""
    key, pid, created, _ = _session_identity(runtime)
    _ACTIVE_SESSIONS.pop(key, None)
    if pid and _STATE_FILE.is_file():
        try:
            stored = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(stored, dict) and stored.get("pid") == pid and stored.get("created") == created:
                _STATE_FILE.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass


def _remembered_gingerbread_active(runtime) -> bool:
    key, pid, created, uid = _session_identity(runtime)
    record = _ACTIVE_SESSIONS.get(key)
    if record is None and pid and _STATE_FILE.is_file():
        try:
            stored = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
            if (isinstance(stored, dict) and stored.get("pid") == pid
                    and stored.get("created") == created):
                record = stored
                _ACTIVE_SESSIONS[key] = stored
        except (OSError, ValueError):
            record = None
    if not isinstance(record, dict):
        return False
    saved_uid = str(record.get("uid") or "")
    if uid and saved_uid and uid != saved_uid:
        return False
    if uid and not saved_uid:
        record["uid"] = uid
    return True


def is_gingerbread_active(runtime, *, frame=None) -> bool:
    """Return True when Gingerbread companion is confirmed active on this live game session.

    Checks the current overworld HUD directly (zero-input read-only) when 幸福雪未融 is
    currently equipped so manual summoning or dismissal is respected immediately; otherwise
    falls back to the session state recorded when 幸福雪未融 was confirmed out for this
    same live game process.
    """
    try:
        current = runtime.capture() if frame is None else frame
        if current is not None and runtime.hit("MaaNikki_MainDetected", image=current):
            from .startup_abilities import GROUPS, hud_ability_matches
            if hud_ability_matches(runtime, GROUPS["gingerbread"], current):
                state, _ = companion_state(current, main_centre(runtime))
                if state == "out":
                    mark_gingerbread_active(runtime)
                    return True
                if state == "in":
                    clear_gingerbread_active(runtime)
                    return False
    except Exception:
        pass
    return _remembered_gingerbread_active(runtime)
