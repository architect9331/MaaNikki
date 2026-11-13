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

from types import SimpleNamespace

import cv2
import numpy as np

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


def observe(runtime, centre, seconds, *, want=None):
    """Return a state only after CONFIRM_FRAMES consecutive frames agree.

    With ``want`` set, keep watching until that state is confirmed or time runs out;
    otherwise report the state the latest frames agree on (or "unknown")."""
    from task_control import task_time as time
    deadline = time.monotonic() + seconds
    last, streak, badges = None, 0, []
    while not runtime.stopped:
        state, badges = companion_state(runtime.capture(), centre)
        streak = streak + 1 if state == last else 1
        last = state
        if state != "unknown" and streak >= CONFIRM_FRAMES and (want is None or state == want):
            break
        if time.monotonic() >= deadline or not runtime.pause(.25):
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
        runtime.log("小姜饼人已在身边，直接开始采集。", detail=True)
        return True
    if state != "in":
        raise NavigationError("右下角未能确认小姜饼人的召唤按钮，请确认能力按钮未被遮挡后重试。")
    for attempt in (1, 2):
        if runtime.stopped:
            raise NavigationError("已停止，尚未召唤小姜饼人。")
        if not runtime.key(SUMMON_KEY, .3):
            raise NavigationError("未能按下「能力衍生功能1」，请在按键设置中确认该按键。")
        state, badges = observe(runtime, centre, 4, want="out")
        event({"type": "gingerbread_companion", "phase": "summon", "attempt": attempt,
               "state": state, "badges": [[round(x, 1), round(y, 1)] for x, y in badges]})
        if state == "out":
            runtime.log("已召唤小姜饼人随行。", detail=True)
            return True
        if state != "in":
            # Only press again when the "not out" layout is still confirmed;
            # an uncertain frame may mean the first press already worked.
            break
    raise NavigationError("已按下召唤键，但未能确认小姜饼人出现，请手动召唤后重试。")
