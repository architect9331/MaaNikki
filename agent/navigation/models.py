from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path


class NavigationError(ValueError):
    pass


class NavigationStuckError(NavigationError):
    """Movement made no progress for the route's stuck timeout."""


MINIMAP_POSITION_SCORE = .55
# Coordinates use pixel edges, shared by native sampling and reference crops.
# Paired player-centered maps place the minimap arrow pivot at (181, 124) in 1080p.
MINIMAP_CENTER = (181/1.5, 124/1.5)
MINIMAP_RADIUS = 100/1.5


@dataclass(frozen=True)
class MapSpec:
    id: str
    title: str
    image: Path
    scale: float
    width: int
    height: int
    bigmap_scale: float = 0.465
    center: tuple[float, float] = MINIMAP_CENTER
    radius: float = MINIMAP_RADIUS
    position_score_floor: float | None = MINIMAP_POSITION_SCORE


def maps(resource: Path) -> dict[str, MapSpec]:
    catalog = resource / "navigation" / "maps.json"
    try:
        items = json.loads(catalog.read_text(encoding="utf-8"))["maps"]
        return {
            item["id"]: MapSpec(
                item["id"], item["title"], catalog.parent / item["image"],
                float(item["scale_720"]), int(item["width"]), int(item["height"]),
                float(item.get("bigmap_scale_720", 0.465)),
                position_score_floor=(float(item.get("position_score_floor", MINIMAP_POSITION_SCORE))
                                      if item.get("position_score_floor", MINIMAP_POSITION_SCORE) is not None else None),
            ) for item in items
        }
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise NavigationError("地图配置无法读取。") from error


@dataclass(frozen=True)
class Point:
    x: float
    y: float
    radius: float = 3
    jump: bool = False
    action: str = ""
    params: dict | None = None


@dataclass(frozen=True)
class Route:
    id: str
    map_id: str
    points: tuple[Point, ...]
    start_radius: float
    timeout: float
    capacity: int | None
    document: dict


def parse_route(value: dict, name: str | None = None) -> Route:
    if value.get("schema_version") not in (2, 3):
        raise NavigationError("路线格式不支持。")
    points = tuple(
        Point(float(point["x"]), float(point["y"]), float(point.get("radius", 3)),
              bool(point.get("jump", False)), point.get("action", ""), point.get("params", {}))
        for point in value["points"]
    )
    if not points:
        raise NavigationError("路线没有途经点。")
    return Route(value["id"], value["map"], points,
                 float(value.get("start_radius", 6)), float(value.get("timeout", 120)),
                 value.get("capacity"), value)


def load_route(resource: Path, name: str) -> Route | None:
    path = resource / "routes" / "daily" / f"{name}.json"
    try:
        return parse_route(json.loads(path.read_text(encoding="utf-8")), name)
    except (OSError, KeyError, TypeError, ValueError):
        return None
