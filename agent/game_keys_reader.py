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
SCROLL_STEP = -240
MAX_SCROLLS = 40

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


def glyph_key(gray, column, centre_y):
    """Classify an icon-only key button: 'Space', 'icon' (other glyph) or None."""
    _, key_x, key_end = column
    y0, y1 = max(0, centre_y-15), min(gray.shape[0], centre_y+15)
    crop = gray[y0:y1, key_x+12:key_end-12]
    if crop.size == 0:
        return None
    mask = (crop >= 200).astype("uint8")
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    blobs = [stats[i] for i in range(1, count) if stats[i][cv2.CC_STAT_AREA] >= 25]
    if not blobs:
        return None
    x = min(int(b[cv2.CC_STAT_LEFT]) for b in blobs)
    y = min(int(b[cv2.CC_STAT_TOP]) for b in blobs)
    right = max(int(b[cv2.CC_STAT_LEFT]+b[cv2.CC_STAT_WIDTH]) for b in blobs)
    bottom = max(int(b[cv2.CC_STAT_TOP]+b[cv2.CC_STAT_HEIGHT]) for b in blobs)
    width, height = right-x, bottom-y
    # The space-bar glyph is a flat key cap clearly wider than tall; mouse
    # glyphs are upright. Anything else stays unknown rather than guessed.
    if height and width >= 1.6*height:
        return "Space"
    return "icon"


def parse_rows(rt, frame):
    """Return [(label, name, caption, value, centre_y)] for fully visible known rows."""
    result = rt.recognize("MaaNikki_Daily_OCR", {"roi": LIST_ROI, "expected": ".+"}, image=frame)
    items = list(getattr(result, "all_results", None) or []) if result else []
    labels, keys = [], []
    for item in items:
        text = str(getattr(item, "text", "") or "").strip()
        box = getattr(item, "box", None)
        score = getattr(item, "score", 1)
        if not text or not box or (isinstance(score, (int, float)) and score < .4):
            continue
        x, y, w, h = (int(v) for v in box)
        cx, cy = x+w/2, y+h/2
        for index, (label_x, key_x, key_end) in enumerate(COLUMNS):
            if label_x <= cx < key_x:
                labels.append((index, cx, cy, text))
            elif key_x <= cx <= key_end:
                keys.append((index, cx, cy, text))
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    rows = []
    for index, _, cy, text in labels:
        label = normalize_label(text)
        name = GAME_ROWS.get(label)
        if name is None or not ROW_TOP <= cy <= ROW_BOTTOM:
            continue
        same_row = sorted((kx, ktext) for kindex, kx, ky, ktext in keys
                          if kindex == index and abs(ky-cy) <= ROW_TOLERANCE)
        caption = "".join(ktext for _, ktext in same_row)
        value = key_name(caption) if caption else glyph_key(gray, COLUMNS[index], round(cy))
        rows.append((label, name, caption, value, round(cy)))
    return rows


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

    def scroll(self, amount):
        if not self.rt.ui.scroll(SCROLL_ROI, amount):
            return None
        return self.rt.capture()

    def record(self, rows):
        for label, name, caption, value, _ in rows:
            previous = self.found.get(name)
            known = value not in (None, "icon")
            if previous is None or (known and previous["value"] in (None, "icon")):
                self.found[name] = {"label": label, "caption": caption, "value": value}
            elif known and previous["value"] != value:
                self.warnings.append(f"「{label}」两次读到不同按键（{previous['value']} / {value}），保留第一次结果")

    def scan(self):
        frame = self.rt.capture()
        if frame is None:
            return False
        # The page normally opens at the top; make sure before reading.
        for _ in range(10):
            before = self.crop(frame)
            frame = self.scroll(-SCROLL_STEP)
            if frame is None:
                return False
            if self.rt.ui.scroll_unchanged(before, self.crop(frame)):
                break
        previous_rows = set()
        for step in range(MAX_SCROLLS+1):
            if self.rt.stopped:
                return False
            rows = parse_rows(self.rt, frame)
            labels = {row[0] for row in rows}
            if step and previous_rows and labels and not labels & previous_rows:
                self.warnings.append("列表一次滚动过多，部分键位可能未读取")
            self.record(rows)
            previous_rows = labels or previous_rows
            before = self.crop(frame)
            frame = self.scroll(SCROLL_STEP)
            if frame is None:
                return False
            if self.rt.ui.scroll_unchanged(before, self.crop(frame)):
                self.record(parse_rows(self.rt, frame))
                return True
        self.warnings.append("列表滚动次数达到上限，可能没有读到末尾")
        return True

    def report(self, complete):
        values, rows = {}, []
        for label, name in GAME_ROWS.items():
            entry = self.found.get(name)
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
