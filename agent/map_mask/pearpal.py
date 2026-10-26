"""Read-only official map data and account collection state.

Endpoint/coordinate conventions and raw Snappy decoding adapted from Whimbox
2a75df8 (GPL-3.0); see THIRD_PARTY_NOTICES.md.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import threading
import time
import urllib.parse
import urllib.request

from file_io import write_json
from message_log import write_message


API = "https://myl-api.nuanpaper.com"
PAGE = "https://myl.nuanpaper.com/tools/map"
STAR = {"11", "132", "145", "167", "244"}
DEW = {"12", "133", "146", "168", "245"}
WORLDS = {"1": "miraland", "4020034": "wanxiang"}
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


def request(url, payload=None):
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json", "User-Agent": "MaaNikki-Map/1.0",
        "Accept": "application/json",
    })
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


@dataclass(frozen=True)
class Point:
    id: str
    kind: str
    map_id: str
    x: float
    y: float
    entrance: bool = False


class PearPal:
    """One network worker, immutable spatial index, no network work on capture."""
    def __init__(self, root: Path):
        self.directory = root / "config/map-mask"
        self.login = self.directory / "login.dat"
        self.cache = root / "cache/map-mask/public.json"
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.points = {}
        self.ready = False
        self.message = "正在读取美鸭梨收集进度…"
        self.thread = threading.Thread(target=self._run, name="pearpal-map-data", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()

    def visible(self, map_id, bounds, kinds):
        with self.lock:
            data, ready, message = self.points.get(map_id), self.ready, self.message
        if not self.login.is_file():
            return (), "请在设置中登录美鸭梨"
        if not ready or data is None:
            return (), message
        points, xs = data
        left, top, right, bottom = bounds
        return tuple(p for p in points[bisect_left(xs, left):bisect_right(xs, right)]
                     if top <= p.y <= bottom and p.kind in kinds), message

    def _public(self):
        if self.cache.is_file() and time.time() - self.cache.stat().st_mtime < 86400:
            try:
                cached = json.loads(self.cache.read_text(encoding="utf-8"))
                if cached.get("schema_version") == 1:
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
                    expanded.extend({**parent, **child, "x": parent.get("x"), "y": parent.get("y"),
                                     "entrance": True} for child in children if isinstance(child, dict))
            for item in expanded:
                catalog = (item.get("catalog_id") or item.get("catalogId") or item.get("catalog")
                           or item.get("category"))
                if catalog is None and len(item.get("catalog_ids", [])) == 1:
                    catalog = item["catalog_ids"][0]
                if isinstance(catalog, list):
                    catalog = catalog[0] if catalog else None
                if isinstance(catalog, dict):
                    catalog = catalog.get("id")
                kind = "star" if str(catalog) in STAR else "dewdrop" if str(catalog) in DEW else None
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
                    point = Point(str(item["id"]), kind, map_id, x, y, bool(item.get("entrance")))
                    points[(world_id, point.id)] = point
        if not points:
            raise DataError("美鸭梨地图没有返回收集点。")
        from dataclasses import asdict
        if not self.stop.is_set():
            write_json(self.cache, {"schema_version": 1, "points": [asdict(p) for p in points.values()]})
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
                                          {"client_id": 1106, **account})).get("data")
                if not isinstance(response, dict):
                    raise DataError("美鸭梨收集进度无效，请重新登录。")
                awarded = {kind: id_set(response.get(key)) for kind, key in
                           (("star", "star"), ("dewdrop", "dewdrop"))}
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
