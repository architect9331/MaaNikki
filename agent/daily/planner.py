from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Rule:
    key: str
    label: str
    words: tuple[str, ...]
    score: int
    priority: int
    executor: str = ""
    energy: bool = False
    route: str = ""


# These are observed task descriptions, not a prediction of the game's task pool.
ZHAOXI = (
    Rule("energy", "消耗活跃能量", ("活跃能量", "消耗"), 200, 5, "energy", True),
    Rule("jihua", "素材激化", ("素材激化幻境",), 200, 5, "jihua", True),
    Rule("bless", "获取祝福闪光", ("幻境", "祝福闪光"), 200, 5, "bless", True),
    Rule("monster", "魔物试炼", ("魔物试炼",), 200, 5, "monster", True),
    Rule("plant", "采集植物", ("植物",), 200, 4, "plant", route="zhaoxi_plant"),
    Rule("insect", "捕虫", ("昆虫",), 200, 3, "insect", route="zhaoxi_insect"),
    Rule("fight", "打怪", ("魔气怪",), 200, 2, "fight", route="zhaoxi_fight"),
    Rule("minigame", "小游戏", ("小游戏",), 200, 0),
    Rule("photo", "拍照", ("照片",), 100, 5, "photo"),
    Rule("dig", "美鸭梨挖掘", ("挖掘",), 100, 0),
    Rule("upgrade", "升级祝福闪光", ("升级", "祝福闪光"), 100, 0),
    Rule("clothes", "制作服装", ("制作",), 100, 0),
)

XINGHAI = (
    Rule("photo", "拍照", ("拍下", "你的存在"), 200, 5, "photo"),
    Rule("place", "放置摆饰", ("摆饰",), 200, 5, "place", route="xinghai_place"),
    Rule("music", "更改音乐", ("留声机",), 100, 5, "music", route="xinghai_music"),
    Rule("chat", "聚会聊天", ("聚会频道",), 100, 5, "chat", route="xinghai_chat"),
    Rule("like", "星绘图册点赞", ("星绘图册", "点赞"), 200, 5, "like"),
    Rule("coco", "椰果采摘", ("椰果采摘",), 300, 0),
    Rule("coco_photo", "椰果拍照", ("举起椰", "照片"), 200, 0),
    Rule("fragment", "星愿碎片投递", ("星愿碎片", "投递"), 300, 0),
    Rule("fragment_photo", "星愿碎片拍照", ("星愿碎片", "照片"), 200, 0),
    Rule("bottle", "漂流瓶查看", ("漂流瓶", "查看"), 200, 4, "bottle", route="xinghai_bottle"),
    Rule("delivery", "投递信笺", ("投递", "信笺"), 200, 3, "delivery", route="xinghai_delivery"),
    Rule("bottle_photo", "星愿瓶合影", ("星愿瓶", "合影"), 200, 4, "photo", route="xinghai_bottle_photo"),
    Rule("crystal", "星光结晶", ("10个星光结晶",), 100, 0, "crystal"),
    Rule("meteor", "召唤流星", ("1次流星",), 200, 0),
    # User-confirmed daily: teleport to the hub, then hold the configured bell key.
    Rule("bell", "召唤摇铃", ("召唤摇铃",), 100, 6, "bell"),
    Rule("wing_photo", "星芒之翼合影", ("星芒之翼", "合影"), 200, 4, "photo", route="xinghai_wing_photo"),
    Rule("bubble", "制造泡泡", ("制造", "泡泡"), 200, 3, "bubble", route="xinghai_bubble"),
    Rule("bubble_photo", "泡泡合影", ("泡泡", "合影"), 200, 0),
    Rule("animal_three", "动物互动3次", ("动物互动", "3次"), 300, 5, "animal", route="xinghai_animal_three"),
    Rule("animal_one", "不同动物互动", ("不同的动物",), 100, 5, "animal", route="xinghai_animal_one"),
)


@dataclass(frozen=True)
class Card:
    slot: int
    text: str
    rule: Rule | None
    remaining: int | None = None


def bell_notice(text: str) -> bool:
    """Transient dialogue, not the daily description about ringing the bell."""
    text = re.sub(r"\s+", "", text)
    return ("闪烁" in text or "试试吧" in text
            or "星之铃" in text and ("似乎" in text or "试试" in text))


def classify(mode: str, text: str, slot: int) -> Card:
    normalized = re.sub(r"\s+", "", text).replace("／", "/")
    rule = next((r for r in (ZHAOXI if mode == "zhaoxi" else XINGHAI)
                 if all(word in normalized for word in r.words)), None)
    progress = re.search(r"[（(]?(\d+)\s*/\s*(\d+)[）)]?", normalized)
    remaining = None
    if progress:
        used, total = map(int, progress.groups())
        if 0 <= used <= total <= 10000:
            remaining = total - used
    if rule and rule.key == "energy" and remaining is None:
        required = re.search(r"(?:消耗|累计消耗)(\d+)点?活跃能量", normalized)
        if required:
            remaining = int(required.group(1))
    return Card(slot, text, rule, remaining)


def group_score(cards: list[Card]) -> int:
    return sum(card.rule.score for card in cards if card.rule)


def select_free(cards: list[Card], score: int) -> list[Card]:
    free = [c for c in cards if c.rule and not c.rule.energy]
    ordered = sorted(free, key=lambda c: (c.rule.priority, c.rule.score), reverse=True)
    if score >= 500:
        return []
    return ordered


def select_energy(cards: list[Card], score: int, preferred: str) -> Card | None:
    candidates = [c for c in cards if c.rule and c.rule.energy and c.rule.key != "energy"]
    if score >= 500 or not candidates:
        return None
    # Specific realm cards use the default minimum; cumulative-energy credit
    # is verified after the separately selected energy-clearing workflow.
    return min(candidates, key=lambda c: (
        c.rule.key == "energy",
        c.rule.key != preferred,
        -c.rule.score,
    ))
