"""Read the game's own key bindings from 设置 → 控制 → 键位设置.

The page lists two columns of "label | key" rows. Rows are matched by their
exact in-game label, so similarly named rows (奇想自行车跳跃 vs 跳跃,
家园能力衍生功能1 vs 能力衍生功能1) cannot be confused. Keys shown as text are
converted to our key names; icon keys are classified only by glyph shape
(the wide space-bar icon), and unknown icons are reported and left unchanged.
"""
from __future__ import annotations

from datetime import datetime
import re
import time as wall_time

import cv2

from game_keys import DEFAULTS, FIXED, parse_key


# Page geometry on the 1280x720 baseline. The list scrolls between the page
# title and the 恢复默认 button; both columns share one scroll view.
LIST_ROI = [140, 108, 1000, 504]
# A row is used only when its centre lies clear of the faded list edges.
ROW_TOP, ROW_BOTTOM = 128, 596
# (label start, key button start, key button end) for the left/right columns.
COLUMNS = ((150, 410, 625), (655, 918, 1135))
# Same-row tolerance between a label and its key text (rows are ~50 px apart).
ROW_TOLERANCE = 15
# Scroll with the pointer in the gap between the columns: hovering a key
# button highlights it, and nothing here may click a binding.
SCROLL_ROI = [140, 112, 508, 296]
# One wheel notch. The distance a notch moves the list is measured from rows
# seen before and after each scroll, then the next scroll sends as many
# notches as keep about 60% of the visible rows on screen; that overlap lets
# every row be read in at least two positions without skipping any.
NOTCH = 120
KEEP_RATIO = .6
MAX_NOTCHES = 8
MAX_SCROLLS = 40
# Key buttons are read one by one with recognition-only OCR on the fixed
# button box; whole-list detection drops or merges single-letter captions.
KEY_HALF_HEIGHT = 16

# In-game row label -> our key name. Only keys our features use and the game
# lets players rebind; greyed rows are fixed internally and not read.
GAME_ROWS = {
    "向前移动": "forward", "向后移动": "backward", "向左移动": "left", "向右移动": "right",
    "跳跃": "jump", "急坠": "falling", "摇铃": "bell",
    "能力衍生功能1": "ability_derive_1", "能力衍生功能2": "ability_derive_2",
    "家园能力衍生功能1": "sub_ability",
    "地图": "map", "背包": "backpack", "拍照": "photo", "换装": "dress",
    "奇想日历": "calendar", "奇迹之旅/乐园构想": "monthly", "使用消耗品": "item",
    "商城": "shop", "聊天": "chat",
    **{f"切换能力{n}": f"ability_{i}" for i, n in enumerate("一二三四五六七八", 1)},
    "交互": "interact", "退出关卡": "exit", "钓鱼收线": "fishing_reel",
}
assert not set(GAME_ROWS.values()) & FIXED

CANONICAL = {
    "ESC": "Esc", "ESCAPE": "Esc", "SPACE": "Space", "空格": "Space", "ENTER": "Enter",
    "RETURN": "Enter", "回车": "Enter", "BACKSPACE": "Backspace", "TAB": "Tab",
    "SHIFT": "Shift", "CTRL": "Ctrl", "CONTROL": "Ctrl", "ALT": "Alt",
    "CAPSLOCK": "CapsLock", "CAPS": "CapsLock",
    "UP": "Up", "DOWN": "Down", "LEFT": "Left", "RIGHT": "Right",
    "↑": "Up", "↓": "Down", "←": "Left", "→": "Right",
    "INSERT": "Insert", "INS": "Insert", "DELETE": "Delete", "DEL": "Delete",
    "HOME": "Home", "END": "End", "PAGEUP": "PageUp", "PGUP": "PageUp",
    "PAGEDOWN": "PageDown", "PGDN": "PageDown",
    "LSHIFT": "LShift", "RSHIFT": "RShift", "LCTRL": "LCtrl", "RCTRL": "RCtrl",
    "LALT": "LAlt", "RALT": "RAlt", "WIN": "Win", "LWIN": "LWin", "RWIN": "RWin",
    "PAUSE": "Pause", "PRINTSCREEN": "PrintScreen", "NUMLOCK": "NumLock",
    "SCROLLLOCK": "ScrollLock",
    "`": "Backquote", "~": "Backquote", "-": "Minus", "=": "Equal", "[": "BracketLeft",
    "]": "BracketRight", "\\": "Backslash", ";": "Semicolon", "'": "Quote",
    ",": "Comma", ".": "Period", "/": "Slash",
    "鼠标左键": "MouseLeft", "左键": "MouseLeft", "鼠标右键": "MouseRight", "右键": "MouseRight",
    "鼠标中键": "MouseMiddle", "中键": "MouseMiddle",
    "鼠标侧键1": "Thumb1", "侧键1": "Thumb1", "鼠标侧键2": "Thumb2", "侧键2": "Thumb2",
}


def normalize_label(text):
    value = re.sub(r"[\s:：*＊·•.。,，'\"“”]", "", str(text or "")).replace("／", "/")
    if value.startswith("切换能力"):
        # OCR reads the Chinese numeral 一 as a dash on this font.
        value = re.sub(r"[—–\-－_]$", "一", value)
    if "衍生功能" in value:
        value = re.sub(r"[lI|｜]$", "1", value)
    return value


def key_name(text):
    """Convert a displayed key caption to our key name, or None when unknown."""
    raw = re.sub(r"\s+", "", str(text or "")).replace("．", ".").replace("。", ".")
    if not raw:
        return None
    upper = raw.upper()
    side = re.fullmatch(r"(左|右|[LR])[.·_\-]?(SHIFT|CTRL|CONTROL|ALT|WIN)", upper)
    if side:
        prefix = "L" if side[1] in ("左", "L") else "R"
        modifier = "CTRL" if side[2] == "CONTROL" else side[2]
        upper = prefix + modifier
    numpad = re.fullmatch(r"(?:NUMPAD|NUM|小键盘)([0-9])", upper)
    if numpad:
        candidate = f"Numpad{numpad[1]}"
    elif re.fullmatch(r"[A-Z0-9]", upper) or re.fullmatch(r"F([1-9]|1[0-9]|2[0-4])", upper):
        candidate = upper
    else:
        candidate = CANONICAL.get(upper) or CANONICAL.get(raw)
    if candidate is None:
        return None
    try:
        parse_key(candidate)
    except ValueError:
        return None
    return candidate


def glyph_icon(gray, column, centre_y):
    """Return 'Space', 'icon' (mouse or other pictogram) or None for a text caption.

    Pictogram keys are drawn on a key-cap plate: the space bar is a flat cap
    clearly wider than tall, and the mouse icon stands on a thin base bar that
    spans its full width below a separate body (seen in the user's 键盘与鼠标
    captures for 净化, 主能力按键 and 钓鱼：收线). Letters, digits and words
    never carry such a detached full-width bar, so a mouse icon can no longer
    be accepted as an OCR'd "0"/"9". Limit: icons other than these two
    shapes fall back to OCR and are rejected only if their text is unknown.
    """
    _, key_x, key_end = column
    y0, y1 = max(0, centre_y-KEY_HALF_HEIGHT), min(gray.shape[0], centre_y+KEY_HALF_HEIGHT)
    crop = gray[y0:y1, key_x+12:key_end-12]
    if crop.size == 0:
        return None
    mask = (crop >= 170).astype("uint8")
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    blobs = [tuple(int(v) for v in stats[i][:5]) for i in range(1, count)
             if stats[i][cv2.CC_STAT_AREA] >= 3]
    if not blobs:
        return None
    left = min(x for x, _, _, _, _ in blobs)
    top = min(y for _, y, _, _, _ in blobs)
    right = max(x+w for x, _, w, _, _ in blobs)
    bottom = max(y+h for _, y, _, h, _ in blobs)
    width, height = right-left, bottom-top
    # Space: one solid plate spanning the glyph. Word captions (Enter, L.Shift)
    # are just as wide but made of separate thin letters.
    plate = max(blobs, key=lambda b: b[2])
    if height and width >= 1.6*height and plate[2] >= .7*width:
        return "Space"
    for blob in blobs:
        x, y, w, h, _ = blob
        others = [b for b in blobs if b is not blob]
        if (h <= 3 and w >= max(8, .7*width) and others
                and y >= max(oy+oh for _, oy, _, oh, _ in others)):
            return "icon"
    return None


def parse_rows(rt, frame):
    """Return [(label, name, caption, value, centre_y)] for fully visible known rows."""
    result = rt.recognize("MaaNikki_Daily_OCR", {"roi": LIST_ROI, "expected": ".+"}, image=frame)
    items = list(getattr(result, "all_results", None) or []) if result else []
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    rows = []
    for item in items:
        text = str(getattr(item, "text", "") or "").strip()
        box = getattr(item, "box", None)
        score = getattr(item, "score", 1)
        if not text or not box or (isinstance(score, (int, float)) and score < .4):
            continue
        x, y, w, h = (int(v) for v in box)
        cx, cy = x+w/2, round(y+h/2)
        index = next((i for i, (label_x, key_x, _) in enumerate(COLUMNS)
                      if label_x <= cx < key_x), None)
        label = normalize_label(text)
        name = GAME_ROWS.get(label)
        if index is None or name is None or not ROW_TOP <= cy <= ROW_BOTTOM:
            continue
        icon = glyph_icon(gray, COLUMNS[index], cy)
        if icon:
            rows.append((label, name, "", icon, cy))
            continue
        _, key_x, key_end = COLUMNS[index]
        caption = read_caption(rt, frame, gray, key_x+12, key_end-12, cy)
        rows.append((label, name, caption, key_name(caption) if caption else None, cy))
    return rows


def read_caption(rt, frame, gray, x0, x1, cy):
    """OCR one key caption from a tight, upscaled crop of its bright glyphs."""
    y0, y1 = max(0, cy-KEY_HALF_HEIGHT), min(gray.shape[0], cy+KEY_HALF_HEIGHT)
    ys, xs = (gray[y0:y1, x0:x1] >= 170).nonzero()
    if not len(xs):
        return ""
    pad = 6
    left, right = max(x0, x0+int(xs.min())-pad), min(x1, x0+int(xs.max())+1+pad)
    top, bottom = max(y0, y0+int(ys.min())-pad), min(y1, y0+int(ys.max())+1+pad)
    crop = cv2.resize(frame[top:bottom, left:right], None, fx=3, fy=3,
                      interpolation=cv2.INTER_CUBIC)
    h, w = crop.shape[:2]
    # The crop holds exactly one caption line; recognition-only OCR avoids
    # the detector dropping lone letters or splitting them from the border.
    key = rt.recognize("MaaNikki_Daily_OCR", {"roi": [0, 0, w, h], "expected": ".+",
                                              "only_rec": True}, image=crop)
    text = getattr(getattr(key, "best_result", None), "text", "") if key else ""
    return re.sub(r"\s+", "", str(text or ""))



class GameKeysReader:
    """Open the key settings page, read every row, and always return to the world."""

    PAGES = ("MaaNikki_GameKeys_SettingsEntrance", "MaaNikki_GameKeys_ControlTab",
             "MaaNikki_GameKeys_KeybindEntrance")

    def __init__(self, rt):
        self.rt = rt
        self.found = {}
        self.warnings = []

    def open(self):
        if not self.rt.ui.menu():
            raise RuntimeError("未能打开美鸭梨菜单，请先回到可移动的主界面")
        messages = ("未在美鸭梨菜单找到「设置」入口", "未能切换到设置的「控制」页",
                    "未能从「键位设置」进入键盘与鼠标页面")
        for node, message in zip(self.PAGES, messages):
            if self.rt.stopped:
                return False
            if not self.rt.click_template(node):
                raise RuntimeError(message)
        return True

    def crop(self, frame):
        x, y, w, h = LIST_ROI
        return frame[y:y+h, x:x+w].copy()

    def settled(self):
        """Capture once the list's smooth scrolling has stopped moving."""
        frame = self.rt.capture()
        for _ in range(8):
            if frame is None or not self.rt.pause(.15):
                return None
            current = self.rt.capture()
            if current is None:
                return None
            if self.rt.ui.scroll_unchanged(self.crop(frame), self.crop(current)):
                return current
            frame = current
        return frame

    def scroll(self, notches):
        """Scroll by whole wheel notches (positive = down) and return a settled frame."""
        for _ in range(abs(notches)):
            if not self.rt.ui.scroll(SCROLL_ROI, -NOTCH if notches > 0 else NOTCH):
                return None
        return self.settled()

    def record(self, rows):
        centre = (ROW_TOP+ROW_BOTTOM)/2
        for label, name, caption, value, cy in rows:
            self.found.setdefault(name, []).append(
                {"label": label, "caption": caption, "value": value, "distance": abs(cy-centre)})

    def entry(self, name):
        """Majority vote over every read of a row; ties go to the read nearest the list centre."""
        reads = self.found.get(name) or []
        known = [read for read in reads if read["value"] not in (None, "icon")]
        pool = known or reads
        if not pool:
            return None
        counts = {}
        for read in pool:
            counts[read["value"]] = counts.get(read["value"], 0)+1
        best = max(pool, key=lambda read: (counts[read["value"]], -read["distance"]))
        if len(counts) > 1 and known:
            summary = " / ".join(f"{value}×{count}" for value, count in counts.items())
            self.warnings.append(f"「{best['label']}」多次读取结果不一致（{summary}），已采用 {best['value']}")
        return best

    def scan(self):
        frame = self.settled()
        if frame is None:
            return False
        # The page normally opens at the top; make sure before reading.
        for _ in range(10):
            before = self.crop(frame)
            frame = self.scroll(-MAX_NOTCHES)
            if frame is None:
                return False
            if self.rt.ui.scroll_unchanged(before, self.crop(frame)):
                break
        span = ROW_BOTTOM-ROW_TOP
        notches, pixels_per_notch = 3, None
        previous = {}
        for step in range(MAX_SCROLLS+1):
            if self.rt.stopped:
                return False
            rows = parse_rows(self.rt, frame)
            positions = {row[0]: row[4] for row in rows}
            shifts = sorted(previous[label]-cy for label, cy in positions.items() if label in previous)
            if step and previous and positions and not shifts:
                self.warnings.append("列表一次滚动过多，部分键位可能未读取")
                notches = max(1, notches//2)
            elif shifts and shifts[len(shifts)//2] > 0:
                pixels_per_notch = shifts[len(shifts)//2]/notches
            if pixels_per_notch:
                notches = max(1, min(MAX_NOTCHES, int((1-KEEP_RATIO)*span/pixels_per_notch)))
            self.record(rows)
            previous = positions or previous
            before = self.crop(frame)
            frame = self.scroll(notches)
            if frame is None:
                return False
            if self.rt.ui.scroll_unchanged(before, self.crop(frame)):
                return True
        self.warnings.append("列表滚动次数达到上限，可能没有读到末尾")
        return True

    def report(self, complete):
        values, rows = {}, []
        for label, name in GAME_ROWS.items():
            entry = self.entry(name)
            if entry is None:
                continue
            value = entry["value"]
            if value == "icon":
                self.warnings.append(f"「{entry['label']}」显示为鼠标等图标，无法识别，保持原设置")
            elif value is None:
                caption = entry["caption"] or "空白"
                self.warnings.append(f"「{entry['label']}」的按键“{caption}”无法识别，保持原设置")
            else:
                values[name] = value
            rows.append({"name": name, "label": label, "caption": entry["caption"],
                         "value": value if value not in (None, "icon") else None})
        missing = [name for name in dict.fromkeys(GAME_ROWS.values()) if name not in self.found]
        return {"created_at": int(wall_time.time()*1000),
                "time": datetime.now().isoformat(timespec="seconds"),
                "complete": bool(complete), "values": values, "rows": rows,
                "missing": [{"name": name, "label": DEFAULTS[name][1]} for name in missing],
                "warnings": list(dict.fromkeys(self.warnings))}

