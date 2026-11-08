"""Read-only official map data, account collection state, and point tutorials.

Endpoint/coordinate conventions and raw Snappy decoding adapted from Whimbox
2a75df8 (GPL-3.0); see THIRD_PARTY_NOTICES.md.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import threading
import time
import urllib.parse
import urllib.request

import cv2
import numpy as np

from file_io import write_json
from message_log import write_message


API = "https://myl-api.nuanpaper.com"
PAGE = "https://myl.nuanpaper.com/tools/map"
IMAGE_HOSTS = {
    "material.papegames.com",
    "webstatic.papegames.com",
    "assets.papegames.com",
    "myl.nuanpaper.com",
}
STAR = {"11", "132", "145", "167", "244"}
DEW = {"12", "133", "146", "168", "245"}
CHEST = {"16", "259"}
BOX_HIGH = {"15", "17", "147"}
BOX_MID = {"14", "151"}
BOX_LOW = {"13", "152"}
WORLDS = {
    "1": "miraland",
    "4020034": "wanxiang",
    "10000001": "firework",
    "10000002": "serenity",
    "10000010": "danqing",
}
CATALOG_NAMES = {
    "11": "奇想星", "12": "灵感露珠",
    "13": "初级宝箱", "14": "中级宝箱", "15": "高级宝箱",
    "16": "散件宝箱", "17": "洞窟宝箱",
    "132": "奇想气球", "133": "焰光结晶",
    "145": "奇想气泡", "146": "无忧凝珠",
    "147": "虹染高级宝箱", "151": "虹染中级宝箱", "152": "虹染初级宝箱",
    "167": "奇想玉琚", "168": "丹青玉魄",
    "244": "奇想华灯", "245": "万相金铃", "259": "大喵宝箱",
}
KIND_NAMES = {
    "star": "奇想星", "dewdrop": "灵感露珠", "chest": "设计图宝箱",
    "box_high": "高级宝箱", "box_mid": "中级宝箱", "box_low": "初级宝箱",
}
DIFFICULTY_NAMES = {1: "低", 2: "中", 3: "高"}
CACHE_SCHEMA = 5
LIMIT = 32 * 1024 * 1024


class DataError(ValueError):
    pass


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def credentials(path):
    """DPAPI is tied to the current Windows user; never log the decrypted value."""
    raw = path.read_bytes()
    buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    source, target = Blob(len(raw), buffer), Blob()
    decrypt = ctypes.windll.crypt32.CryptUnprotectData
    decrypt.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    decrypt.restype = wintypes.BOOL
    free = ctypes.windll.kernel32.LocalFree
    free.argtypes, free.restype = [ctypes.c_void_p], ctypes.c_void_p
    if not decrypt(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise DataError("美鸭梨登录信息无法读取，请重新登录。")
    try:
        value = json.loads(ctypes.string_at(target.data, target.size))
        if not all(isinstance(value.get(key), str) and value[key] for key in ("token", "openid")):
            raise DataError("美鸭梨登录信息无效，请重新登录。")
        return value
    finally:
        ctypes.memset(target.data, 0, target.size)
        free(target.data)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DataError("美鸭梨接口地址发生变化，请稍后重试。")


def request(url, payload=None, *, accept="application/json"):
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    headers = {"User-Agent": "MaaNikki-Map/1.0", "Accept": accept}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=15) as response:
            raw = response.read(LIMIT + 1)
    except DataError:
        raise
    except Exception as error:
        # urllib exceptions can include request details; keep credentials out of diagnostics.
        raise DataError(f"美鸭梨连接失败（{type(error).__name__}）。") from None
    if len(raw) > LIMIT:
        raise DataError("美鸭梨地图数据过大。")
    return raw


def decode(raw):
    if raw.lstrip().startswith((b"{", b"[")):
        value = json.loads(raw)
    else:
        index, length, shift = 0, 0, 0
        while True:
            if index >= len(raw) or shift > 28:
                raise DataError("地图压缩数据无效。")
            byte = raw[index]
            index += 1
            length |= (byte & 127) << shift
            if byte < 128:
                break
            shift += 7
        if length > LIMIT:
            raise DataError("地图解压数据过大。")
        output = bytearray()
        while index < len(raw):
            tag = raw[index]
            index += 1
            kind = tag & 3
            count = tag >> 2
            if kind == 0:
                if count >= 60:
                    size = count - 59
                    if index + size > len(raw):
                        raise DataError("地图压缩数据不完整。")
                    count = int.from_bytes(raw[index:index+size], "little")
                    index += size
                count += 1
                if index + count > len(raw) or len(output) + count > length:
                    raise DataError("地图压缩数据不完整。")
                output.extend(raw[index:index+count])
                index += count
            else:
                size = {1: 1, 2: 2, 3: 4}[kind]
                if index + size > len(raw):
                    raise DataError("地图压缩数据不完整。")
                offset = int.from_bytes(raw[index:index+size], "little")
                index += size
                if kind == 1:
                    offset |= (tag & 224) << 3
                    count = 4 + ((tag >> 2) & 7)
                else:
                    count += 1
                if offset <= 0 or offset > len(output) or len(output) + count > length:
                    raise DataError("地图压缩数据无效。")
                block = bytes(output[-offset:])
                output.extend((block * ((count + offset - 1) // offset))[:count])
        if len(output) != length:
            raise DataError("地图解压长度无效。")
        value = json.loads(output)
    if not isinstance(value, dict) or value.get("code") not in (None, 0):
        raise DataError("美鸭梨接口未返回有效数据，请重新登录或稍后重试。")
    return value


def id_set(value):
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list) or any(not isinstance(v, (str, int)) for v in value):
        raise DataError("美鸭梨收集进度格式发生变化。")
    return frozenset(str(v) for v in value)


def localized_text(value):
    for _ in range(2):
        if isinstance(value, str) and value.strip().startswith(("\"", "[", "{")):
            try:
                value = json.loads(value)
            except ValueError:
                break
    if isinstance(value, list):
        for entry in value:
            if isinstance(entry, dict) and str(entry.get("lang", "")).lower() == "zh-cn":
                return str(entry.get("text") or "").strip()
        for entry in value:
            if isinstance(entry, dict) and entry.get("text"):
                return str(entry["text"]).strip()
        return ""
    return str(value).strip() if isinstance(value, str) else ""


def format_timestamp(value):
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})[T\s](\d{2}:\d{2}:\d{2})", str(value or "").strip())
    if not match:
        return ""
    return f"{match[1]}年{match[2]}月{match[3]}日 {match[4]}"


@dataclass(frozen=True)
class Point:
    id: str
    kind: str
    map_id: str
    x: float
    y: float
    entrance: bool = False
    title: str = ""
    parent_id: str = ""
    parent_title: str = ""
    catalog_name: str = ""


@dataclass(frozen=True)
class Tutorial:
    point_id: str
    kind: str
    kind_name: str
    title: str
    difficulty: str = ""
    content: str = ""
    contributor: str = ""
    updated_at: str = ""
    image: object = None
    loading: bool = False
    error: str = ""


class PearPal:
    """Background network workers, immutable spatial index, non-blocking tutorial cache."""
    def __init__(self, root: Path):
        self.directory = root / "config/map-mask"
        self.login = self.directory / "login.dat"
        self.cache = root / "cache/map-mask/public.json"
        self.tutorial_dir = root / "cache/map-mask/tutorials"
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.tutorial_wake = threading.Event()
        self.points = {}
        self.tutorials = {}
        self.tutorial_queue = []
        self.client_id = 1106
        self.ready = False
        self.message = "正在读取美鸭梨收集进度…"
        self.thread = threading.Thread(target=self._run, name="pearpal-map-data", daemon=True)
        self.tutorial_thread = threading.Thread(
            target=self._tutorial_worker, name="pearpal-tutorials", daemon=True)
        self.thread.start()
        self.tutorial_thread.start()

    def close(self):
        self.stop.set()
        self.tutorial_wake.set()

    def visible(self, map_id, bounds, kinds):
        with self.lock:
            data, ready, message = self.points.get(map_id), self.ready, self.message
        if not self.login.is_file():
            return (), "请在设置中登录美鸭梨"
        if not ready or data is None:
            return (), message
        points, xs = data
        left, top, right, bottom = bounds
        active_kinds = set(kinds)
        if "box" in active_kinds:
            active_kinds.update(("box_high", "box_mid", "box_low"))
        return tuple(p for p in points[bisect_left(xs, left):bisect_right(xs, right)]
                     if top <= p.y <= bottom and p.kind in active_kinds), message

    def tutorial(self, point: Point) -> Tutorial:
        with self.lock:
            cached = self.tutorials.get(point.id)
            if cached is not None and (not cached.error or cached.loading):
                return cached
            kind_name = point.catalog_name or KIND_NAMES.get(point.kind, "收集点")
            title = point.title or point.parent_title or f"点位 #{point.id}"
            placeholder = Tutorial(point.id, point.kind, kind_name, title, loading=True)
            self.tutorials[point.id] = placeholder
            if all(p.id != point.id for p in self.tutorial_queue):
                self.tutorial_queue.append(point)
        self.tutorial_wake.set()
        return placeholder

    def _tutorial_worker(self):
        while not self.stop.is_set():
            self.tutorial_wake.wait(0.25)
            self.tutorial_wake.clear()
            while not self.stop.is_set():
                with self.lock:
                    point = self.tutorial_queue.pop(0) if self.tutorial_queue else None
                if point is None:
                    break
                result = self._fetch_tutorial(point)
                with self.lock:
                    self.tutorials[point.id] = result

    def _fetch_tutorial(self, point: Point) -> Tutorial:
        kind_name = point.catalog_name or KIND_NAMES.get(point.kind, "收集点")
        base_title = point.title or point.parent_title or f"点位 #{point.id}"
        if point.entrance and point.parent_title and point.title and point.parent_title != point.title:
            base_title = f"{point.parent_title} · {point.title}"
        try:
            query_id = int(point.id) if point.id.isdigit() else point.id
            raw = decode(request(API + "/v1/strategy/map/spawner/info",
                                 {"client_id": self.client_id, "id": query_id}))
            info = raw.get("data") if isinstance(raw.get("data"), dict) else raw
            post = info.get("creator_post") if isinstance(info.get("creator_post"), dict) else None
            if not post and point.parent_id and point.parent_id != point.id:
                parent_q = int(point.parent_id) if point.parent_id.isdigit() else point.parent_id
                p_raw = decode(request(API + "/v1/strategy/map/spawner/info",
                                       {"client_id": self.client_id, "id": parent_q}))
                p_info = p_raw.get("data") if isinstance(p_raw.get("data"), dict) else p_raw
                if isinstance(p_info.get("creator_post"), dict) or p_info.get("icon"):
                    info = p_info
                    post = p_info.get("creator_post") if isinstance(p_info.get("creator_post"), dict) else None
            desc_text = localized_text(info.get("description"))
            title = point.title or desc_text or point.parent_title or f"点位 #{point.id}"
            if point.entrance and point.parent_title and point.parent_title not in title:
                title = f"{point.parent_title} · {title}"
            difficulty = DIFFICULTY_NAMES.get((post or {}).get("difficulty"), "")
            content = str((post or {}).get("content") or "").strip()
            if not content and desc_text and desc_text != point.title:
                content = desc_text
            if not content:
                content = "洞窟/副本内部收集点位" if point.entrance else "暂无点位文字说明"
            contributor = str((post or {}).get("role_name") or "").strip()
            updated_at = format_timestamp((post or {}).get("submitted_at") or info.get("updated_at") or "")
            image_url = ""
            images = (post or {}).get("images")
            if isinstance(images, list) and images and isinstance(images[0], dict):
                image_url = str(images[0].get("origin") or "").strip()
            if not image_url:
                icon_url = localized_text(info.get("icon"))
                if icon_url.startswith("https://"):
                    image_url = icon_url
            image = self._load_image(image_url) if image_url else None
            return Tutorial(point.id, point.kind, kind_name, title, difficulty=difficulty,
                            content=content, contributor=contributor, updated_at=updated_at,
                            image=image, loading=False)
        except Exception as error:
            msg = str(error) if isinstance(error, DataError) else "教程加载失败，请稍后重试"
            return Tutorial(point.id, point.kind, kind_name, base_title,
                            content=msg, loading=False, error=msg)
    def _load_image(self, url: str):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in IMAGE_HOSTS:
            return None
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
        cache_path = self.tutorial_dir / f"{digest}.img"
        raw = None
        if cache_path.is_file():
            try:
                raw = cache_path.read_bytes()
            except OSError:
                raw = None
        if not raw:
            try:
                raw = request(url, accept="image/*,*/*;q=0.8")
                self.tutorial_dir.mkdir(parents=True, exist_ok=True)
                cache_path.write_bytes(raw)
            except Exception:
                return None
        array = np.frombuffer(raw, dtype=np.uint8)
        decoded = cv2.imdecode(array, cv2.IMREAD_COLOR)
        if decoded is None or decoded.size == 0:
            return None
        h, w = decoded.shape[:2]
        if w > 560:
            scale = 560.0 / w
            decoded = cv2.resize(decoded, (560, max(1, round(h * scale))), interpolation=cv2.INTER_AREA)
        return decoded

    def _public(self):
        if self.cache.is_file() and time.time() - self.cache.stat().st_mtime < 86400:
            try:
                cached = json.loads(self.cache.read_text(encoding="utf-8"))
                if cached.get("schema_version") == CACHE_SCHEMA:
                    return [Point(**v) for v in cached["points"]]
            except (OSError, ValueError, TypeError, KeyError):
                pass
        html = request(PAGE).decode("utf-8")
        sources = re.findall(r'<script[^>]+src=["\']([^"\']+)', html, re.I)
        client_id = None
        for source in sources:
            url = urllib.parse.urljoin(PAGE, source)
            parsed = urllib.parse.urlsplit(url)
            if "/js/custom." not in parsed.path or parsed.scheme != "https":
                continue
            if parsed.hostname not in {"myl.nuanpaper.com", "assets.papegames.com"}:
                continue
            match = re.search(r'nikki5CN:\{clientid:["\'](\d+)["\']',
                              request(url).decode("utf-8"))
            if match:
                client_id = int(match[1])
                break
        if client_id is None:
            raise DataError("美鸭梨地图接口配置读取失败。")
        self.client_id = client_id

        def post(path, **values):
            if self.stop.is_set():
                raise DataError("地图标记已停止。")
            return decode(request(API + "/v1/strategy/map/" + path,
                                  {"client_id": client_id, **values}))

        stages = post("stage/spawner/list").get("stages")
        if not isinstance(stages, dict):
            raise DataError("美鸭梨副本点位格式发生变化。")
        points = {}
        for world_id, map_id in WORLDS.items():
            raw = post("spawner/list", world_id=world_id, catalog_type_id=[]).get("list")
            if not isinstance(raw, list):
                raise DataError("美鸭梨点位格式发生变化。")
            parents = {str(p.get("stage_id") or p.get("stageId")): p for p in raw}
            expanded = list(raw)
            for stage, children in stages.items():
                parent = parents.get(str(stage))
                if parent and isinstance(children, list):
                    p_id = str(parent.get("id") or "")
                    p_title = localized_text(parent.get("description"))
                    expanded.extend({**parent, **child, "x": parent.get("x"), "y": parent.get("y"),
                                     "entrance": True, "parent_id": p_id, "parent_title": p_title}
                                    for child in children if isinstance(child, dict))
            for item in expanded:
                catalog = (item.get("catalog_id") or item.get("catalogId") or item.get("catalog")
                           or item.get("category"))
                if catalog is None and len(item.get("catalog_ids", [])) == 1:
                    catalog = item["catalog_ids"][0]
                if isinstance(catalog, list):
                    catalog = catalog[0] if catalog else None
                if isinstance(catalog, dict):
                    catalog = catalog.get("id")
                cat_str = str(catalog)
                kind = ("star" if cat_str in STAR else
                        "dewdrop" if cat_str in DEW else
                        "chest" if cat_str in CHEST else
                        "box_high" if cat_str in BOX_HIGH else
                        "box_mid" if cat_str in BOX_MID else
                        "box_low" if cat_str in BOX_LOW else None)
                if kind is None or item.get("id") in (None, ""):
                    continue
                item_world = str(item.get("world_id") or item.get("worldId") or world_id)
                if item_world != world_id:
                    continue
                try:
                    # MaaNikki uses the 0.5x Whimbox reference maps: (2/90)/2.
                    x, y = float(item["x"]) / 90, float(item["y"]) / 90
                except (KeyError, ValueError, TypeError):
                    continue
                if math.isfinite(x) and math.isfinite(y):
                    title = localized_text(item.get("description"))
                    point = Point(str(item["id"]), kind, map_id, x, y,
                                  bool(item.get("entrance")), title=title,
                                  parent_id=str(item.get("parent_id") or ""),
                                  parent_title=str(item.get("parent_title") or ""),
                                  catalog_name=CATALOG_NAMES.get(cat_str, KIND_NAMES.get(kind, "")))
                    points[(world_id, point.id)] = point
        if not points:
            raise DataError("美鸭梨地图没有返回收集点。")
        if not self.stop.is_set():
            write_json(self.cache, {"schema_version": CACHE_SCHEMA,
                                    "points": [asdict(p) for p in points.values()]})
        return list(points.values())
    def _run(self):
        public, revision, failures = None, None, 0
        while not self.stop.is_set():
            try:
                if not self.login.is_file():
                    with self.lock:
                        self.points, self.ready, self.message = {}, False, "请在设置中登录美鸭梨"
                    public, revision = None, None
                    self.stop.wait(1)
                    continue
                current = self.login.read_bytes()
                if revision != current:
                    with self.lock:
                        self.points, self.ready = {}, False
                    revision = current
                account = credentials(self.login)
                response = decode(request(API + "/v1/strategy/map/user/info",
                                          {"client_id": self.client_id, **account})).get("data")
                if not isinstance(response, dict):
                    raise DataError("美鸭梨收集进度无效，请重新登录。")
                box_awarded = id_set(response.get("box") or response.get("chest") or [])
                awarded = {
                    "star": id_set(response.get("star")),
                    "dewdrop": id_set(response.get("dewdrop")),
                    "chest": box_awarded,
                    "box_high": box_awarded,
                    "box_mid": box_awarded,
                    "box_low": box_awarded,
                }
                if public is None:
                    public = self._public()
                if self.stop.is_set() or not self.login.is_file() or self.login.read_bytes() != current:
                    continue
                index = {}
                remaining = [p for p in public if p.id not in awarded[p.kind]]
                for map_id in WORLDS.values():
                    ordered = tuple(sorted((p for p in remaining if p.map_id == map_id), key=lambda p: p.x))
                    index[map_id] = ordered, tuple(p.x for p in ordered)
                now = time.time()
                with self.lock:
                    self.points, self.ready, self.message = index, True, ""
                write_json(self.directory / "status.json", {
                    "updated_at": now, "authenticated": True,
                    "star_collected": len(awarded["star"]), "dewdrop_collected": len(awarded["dewdrop"]),
                    "box_collected": len(box_awarded),
                    "remaining": len(remaining), "error": "",
                })
                failures = 0
                delay = 15
            except Exception as error:
                if self.stop.is_set():
                    return
                failures += 1
                message = str(error) if isinstance(error, DataError) else f"地图数据读取失败（{type(error).__name__}）。"
                with self.lock:
                    self.ready, self.message = False, message
                if failures == 1:
                    write_message("地图标记：" + message, None)
                try:
                    write_json(self.directory / "status.json", {
                        "authenticated": False, "error": message, "updated_at": time.time()})
                except OSError:
                    pass
                delay = min(60, 5 * 2 ** min(failures, 4))
            # Observe logout/login quickly even while waiting for the next refresh.
            deadline = time.monotonic() + delay
            while not self.stop.wait(.5) and time.monotonic() < deadline:
                try:
                    if not self.login.is_file() or self.login.read_bytes() != revision:
                        break
                except OSError:
                    break
