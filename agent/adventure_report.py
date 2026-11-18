"""UID-scoped local adventure daily report ledger (4:00 AM UTC+8 game day)."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from account_state import game_day, read_json
from data_paths import data_path
from file_io import write_json

MAX_DAYS = 400
MAX_ENTRIES_PER_DAY = 200
_ITEM_NAME_ALIASES = {
    "丝线": "纯真丝线",
}
_MONSTER_CATALOG_CACHE: dict[str, tuple[float, dict[str, tuple[str, str, int] | None]]] = {}


def is_enabled() -> bool:
    return os.environ.get("PI_MAANIKKI_ADVENTURE_REPORT") != "0"


def normalize_item_name(name: str) -> str:
    cleaned = str(name or "").strip()
    return _ITEM_NAME_ALIASES.get(cleaned, cleaned)


def _valid_uid(uid: object) -> str | None:
    if isinstance(uid, str) and re.fullmatch(r"[0-9]{1,32}", uid):
        return uid
    return None


def resolve_report_owner(root: Path, rt=None, uid: str | None = None) -> tuple[str, str | None]:
    """Resolve the active CN UID for report isolation without triggering game captures."""
    resolved = _valid_uid(uid)
    if resolved is None and rt is not None:
        resolved = _valid_uid(getattr(rt, "_account_uid", None))
    if resolved is None:
        try:
            from task_control import active_session

            session = active_session()
            if session is not None:
                resolved = _valid_uid(getattr(session, "account_uid", None))
        except Exception:
            pass
    if resolved is None:
        try:
            index = read_json(Path(root) / "config/accounts/index.json")
            active = index.get("active_game_account") if isinstance(index, dict) else None
            if isinstance(active, str) and active.startswith("CN:"):
                resolved = _valid_uid(active[3:])
        except Exception:
            pass
    if resolved is not None:
        return f"CN:{resolved}", resolved
    return "default", None


def report_path(root: Path, account_key: str) -> Path:
    base = Path(root) / "config/adventure_reports"
    if account_key.startswith("CN:"):
        return base / "CN" / f"{account_key[3:]}.json"
    return base / "default.json"


@contextmanager
def _report_lock(root: Path):
    path = Path(root) / "config/adventure_reports/store.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _blank_report(account_key: str, uid: str | None) -> dict:
    return {
        "schema_version": 1,
        "scope": "adventure_report",
        "account_key": account_key,
        "uid": uid,
        "updated_at": None,
        "days": {},
    }


def _sanitize_items(items: dict[str, int] | None) -> dict[str, int]:
    cleaned: dict[str, int] = {}
    if not isinstance(items, dict):
        return cleaned
    for raw_name, raw_qty in items.items():
        name = normalize_item_name(str(raw_name))
        if not name:
            continue
        try:
            qty = int(raw_qty)
        except (TypeError, ValueError):
            continue
        if qty > 0:
            cleaned[name] = cleaned.get(name, 0) + qty
    return cleaned


def _recompute_day(day_doc: dict) -> dict:
    entries = day_doc.get("entries")
    if not isinstance(entries, list):
        entries = []
    if len(entries) > MAX_ENTRIES_PER_DAY:
        entries = entries[-MAX_ENTRIES_PER_DAY:]

    merged_items: dict[str, int] = {}
    energy_spent = 0
    realm_runs = 0
    realm_materials_gained = 0
    open_world_pickups = 0
    zhaoxi_score: int | None = None
    xinghai_score: int | None = None

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        cat = str(entry.get("category") or "")
        task = str(entry.get("task") or "")
        e_spent = max(0, int(entry.get("energy_spent") or 0))
        e_runs = max(0, int(entry.get("runs") or 0))
        energy_spent += e_spent
        if cat == "realm":
            realm_runs += e_runs

        e_items = entry.get("items")
        if isinstance(e_items, dict):
            for name, qty in e_items.items():
                try:
                    val = int(qty)
                except (TypeError, ValueError):
                    continue
                if val <= 0:
                    continue
                merged_items[name] = merged_items.get(name, 0) + val
                if cat == "open_world":
                    open_world_pickups += val
                elif cat == "realm" and name != "噗灵":
                    realm_materials_gained += val

        meta = entry.get("meta")
        if isinstance(meta, dict):
            score = meta.get("score")
            if isinstance(score, int) and not isinstance(score, bool):
                if task == "zhaoxi":
                    zhaoxi_score = score if zhaoxi_score is None else max(zhaoxi_score, score)
                elif task == "xinghai":
                    xinghai_score = score if xinghai_score is None else max(xinghai_score, score)

    summary = {
        "energy_spent": energy_spent,
        "realm_runs": realm_runs,
        "bling_gained": merged_items.get("噗灵", 0),
        "particles_gained": merged_items.get("闪光粒子", 0),
        "realm_materials_gained": realm_materials_gained,
        "starlight_crystals_gained": merged_items.get("星光结晶", 0),
        "open_world_pickups": open_world_pickups,
        "zhaoxi_score": zhaoxi_score,
        "xinghai_score": xinghai_score,
        "entry_count": len(entries),
    }
    return {
        "summary": summary,
        "items": merged_items,
        "entries": entries,
    }


def lookup_monster_drop(root: Path, level_name: str) -> tuple[str, str, int] | None:
    """Resolve a unique monster trial material drop from the crafting catalog."""
    level = str(level_name or "").strip()
    if not level:
        return None
    try:
        catalog_path = data_path("resource/data/crafting_catalog.json", Path(root))
        if not catalog_path.is_file():
            return None
        mtime = catalog_path.stat().st_mtime
        cache_key = str(catalog_path)
        cached = _MONSTER_CATALOG_CACHE.get(cache_key)
        if cached is None or cached[0] != mtime:
            raw = json.loads(catalog_path.read_text(encoding="utf-8-sig"))
            mats = raw.get("materials") if isinstance(raw, dict) else {}
            by_level: dict[str, list[tuple[str, str, int]]] = {}
            for mid, meta in (mats or {}).items():
                if not isinstance(meta, dict) or meta.get("realm") != "monster":
                    continue
                lvl = str(meta.get("level") or "").strip()
                name = str(meta.get("name") or "").strip()
                drop = meta.get("drop")
                if lvl and name and isinstance(drop, int) and not isinstance(drop, bool) and drop > 0:
                    by_level.setdefault(lvl, []).append((str(mid), name, int(drop)))
            resolved_map = {
                lvl: items[0] if len(items) == 1 else None
                for lvl, items in by_level.items()
            }
            _MONSTER_CATALOG_CACHE[cache_key] = (mtime, resolved_map)
            cached = (mtime, resolved_map)
        return cached[1].get(level)
    except Exception:
        return None


def record_adventure_entry(
    root: Path,
    rt=None,
    *,
    uid: str | None = None,
    category: str,
    task: str,
    title: str,
    detail: str = "",
    energy_spent: int = 0,
    runs: int = 0,
    items: dict[str, int] | None = None,
    meta: dict | None = None,
    status: str = "completed",
    upsert_key: str | None = None,
    day: str | None = None,
) -> bool:
    """Append or upsert one adventure daily report entry and recompute that day's summary."""
    if not is_enabled():
        return False
    root_path = Path(root)
    account_key, resolved_uid = resolve_report_owner(root_path, rt=rt, uid=uid)
    path = report_path(root_path, account_key)
    day_key = day or game_day().isoformat()
    now_local = datetime.now().astimezone().isoformat(timespec="seconds")

    cleaned_items = _sanitize_items(items)
    entry: dict = {
        "id": f"{day_key}-{uuid4().hex[:10]}",
        "at": now_local,
        "category": str(category),
        "task": str(task),
        "title": str(title),
        "detail": str(detail or ""),
        "energy_spent": max(0, int(energy_spent or 0)),
        "runs": max(0, int(runs or 0)),
        "status": str(status or "completed"),
        "items": cleaned_items,
    }
    if isinstance(meta, dict) and meta:
        entry["meta"] = dict(meta)
    if upsert_key:
        entry["upsert_key"] = str(upsert_key)

    with _report_lock(root_path):
        report = read_json(path)
        if not isinstance(report, dict) or report.get("schema_version") != 1:
            report = _blank_report(account_key, resolved_uid)
        if not isinstance(report.get("days"), dict):
            report["days"] = {}
        day_doc = report["days"].get(day_key)
        if not isinstance(day_doc, dict):
            day_doc = {"summary": {}, "items": {}, "entries": []}
        entries = list(day_doc.get("entries") or [])
        if upsert_key:
            replaced = False
            for index, existing in enumerate(entries):
                if isinstance(existing, dict) and existing.get("upsert_key") == upsert_key:
                    entry["id"] = existing.get("id") or entry["id"]
                    entries[index] = entry
                    replaced = True
                    break
            if not replaced:
                entries.append(entry)
        else:
            entries.append(entry)
        day_doc["entries"] = entries
        report["days"][day_key] = _recompute_day(day_doc)
        if len(report["days"]) > MAX_DAYS:
            for old_day in sorted(report["days"])[:-MAX_DAYS]:
                del report["days"][old_day]
        report["account_key"] = account_key
        report["uid"] = resolved_uid
        report["updated_at"] = now_local
        write_json(path, report)
    return True


def try_record_adventure_entry(root: Path, rt=None, **kwargs) -> bool:
    """Best-effort report recording that never breaks the caller's task flow."""
    try:
        return record_adventure_entry(root, rt=rt, **kwargs)
    except Exception:
        return False
