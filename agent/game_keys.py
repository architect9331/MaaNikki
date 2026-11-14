"""Semantic game inputs shared by tasks and routes."""
from __future__ import annotations

from dataclasses import dataclass

from client_config import node_action_params
import re


# Labels follow the rows of the game's 键位设置 page so players can copy them.
# Keep photo shutter and UI shortcuts separate from movement bindings.
DEFAULTS = {
    # 基础操作
    "forward": ("W", "向前移动"), "backward": ("S", "向后移动"),
    "left": ("A", "向左移动"), "right": ("D", "向右移动"),
    "jump": ("Space", "跳跃"), "falling": ("Q", "急坠"), "bell": ("X", "摇铃"),
    "ability_derive_1": ("G", "能力衍生功能1"), "ability_derive_2": ("T", "能力衍生功能2"),
    "sub_ability": ("R", "家园能力衍生功能1"),
    # 系统功能
    "map": ("M", "地图"), "backpack": ("B", "背包"), "photo": ("P", "拍照"),
    "dress": ("C", "换装"), "calendar": ("L", "奇想日历"),
    "monthly": ("J", "奇迹之旅/乐园构想"), "item": ("Z", "使用消耗品"),
    "shop": ("H", "商城"), "chat": ("Enter", "聊天"),
    **{f"ability_{i}": (str(i), f"切换能力：{n}") for i, n in enumerate("一二三四五六七八", 1)},
    # 通用 / 关卡 / 玩法
    "interact": ("F", "交互"), "skip": ("F", "跳过动画／奖励"),
    "exit": ("Backspace", "退出关卡"), "fishing_reel": ("MouseRight", "钓鱼：收线"),
    # Not rebindable in the game (greyed rows or absent from the page).
    "menu": ("Esc", "美鸭梨"), "attack": ("MouseLeft", "净化"),
    "ability_use": ("MouseRight", "主能力按键"), "capture": ("MouseRight", "主能力按键（捕虫）"),
    "place": ("MouseLeft", "放置摆饰"), "recover": ("MouseRight", "收回摆饰"),
    "shutter": ("Space", "拍照快门"), "ability_config": ("E", "换装页能力配置切换"),
}
# The game greys these out (or never lists them), so they cannot differ from
# the defaults and are not offered as settings.
FIXED = frozenset({"menu", "attack", "ability_use", "capture", "place", "recover",
                   "shutter", "ability_config"})
MOVEMENT = {"forward", "left", "backward", "right"}
MACRO_KEYS = MOVEMENT | {"jump", "falling", "attack"}
SPECIAL = {"ESC": 27, "ESCAPE": 27, "SPACE": 32, "ENTER": 13, "RETURN": 13,
           "BACKSPACE": 8, "TAB": 9, "SHIFT": 16, "CTRL": 17, "CONTROL": 17,
           "ALT": 18, "CAPSLOCK": 20, "UP": 38, "DOWN": 40, "LEFT": 37, "RIGHT": 39,
           "INSERT": 45, "DELETE": 46, "HOME": 36, "END": 35, "PAGEUP": 33, "PAGEDOWN": 34,
           "LSHIFT": 160, "RSHIFT": 161, "LCTRL": 162, "RCTRL": 163, "LALT": 164, "RALT": 165,
           "WIN": 91, "META": 91, "LWIN": 91, "RWIN": 92, "APPS": 93,
           "PAUSE": 19, "PRINTSCREEN": 44, "NUMLOCK": 144, "SCROLLLOCK": 145,
           "BACKQUOTE": 192, "MINUS": 189, "EQUAL": 187, "BRACKETLEFT": 219,
           "BRACKETRIGHT": 221, "BACKSLASH": 220, "SEMICOLON": 186, "QUOTE": 222,
           "COMMA": 188, "PERIOD": 190, "SLASH": 191,
           "NUMPADADD": 107, "NUMPADSUBTRACT": 109, "NUMPADMULTIPLY": 106,
           "NUMPADDIVIDE": 111, "NUMPADDECIMAL": 110}
MOUSE = {"MOUSELEFT": 0, "MOUSERIGHT": 1, "MOUSEMIDDLE": 2,
         "MOUSEX1": 3, "MOUSEX2": 4, "THUMB1": 3, "THUMB2": 4,
         "左键": 0, "右键": 1, "中键": 2, "侧键1": 3, "侧键2": 4}


@dataclass(frozen=True)
class Binding:
    kind: str
    code: int


def parse_key(value: str) -> Binding:
    if not isinstance(value, str):
        raise ValueError("请输入单个键名。")
    name = value.strip().upper()
    if name in MOUSE:
        return Binding("mouse", MOUSE[name])
    if re.fullmatch(r"[A-Z0-9]", name):
        return Binding("key", ord(name))
    if name in SPECIAL:
        return Binding("key", SPECIAL[name])
    if re.fullmatch(r"F([1-9]|1[0-9]|2[0-4])", name):
        return Binding("key", 111 + int(name[1:]))
    if re.fullmatch(r"NUMPAD[0-9]", name):
        return Binding("key", 96 + int(name[-1]))
    raise ValueError("键名无效；支持字母、数字、F1–F24、Space、Esc、Enter、Backspace、Shift 和鼠标键，不支持组合键。")


def node_name(name):
    return "MaaNikki_Key_" + name


class GameKeys:
    def __init__(self, context=None, values=None):
        self.context = context
        self.values = values or {}
        self.cache = {}

    def get(self, name):
        if name not in DEFAULTS:
            raise ValueError("未登记的游戏动作。")
        if name == "skip" and "skip" not in self.values:
            # The game has no separate skip row; skip and reward-continue prompts follow interact.
            return self.get("interact")
        if name not in self.cache:
            value = self.values.get(name, DEFAULTS[name][0])
            if name in FIXED:
                # Not configurable in the game; a stale saved value cannot apply.
                value = DEFAULTS[name][0]
            elif self.context is not None:
                value = node_action_params(self.context, node_name(name)).get("value", value)
            try:
                self.cache[name] = parse_key(value)
            except ValueError as error:
                raise ValueError(f"游戏键位“{DEFAULTS[name][1]}”：{error}") from error
        return self.cache[name]

    def movement(self):
        keys = [self.get(name) for name in ("forward", "left", "backward", "right")]
        if any(key.kind != "key" for key in keys) or len(set(keys)) != 4:
            raise ValueError("四个移动方向必须设置为不同的键盘按键。")
        return {key.code for key in keys}

