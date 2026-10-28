"""Build file-level Windows update packages from verified release archives."""
from __future__ import annotations

from copy import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import zipfile

HISTORY_LIMIT = 10
PROTECTED = {"config", "cache", "logs", "debug", ".git", ".build", "artifacts"}


def checked_path(name: str) -> str:
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or any(part in {".", "..", ""} for part in name.split("/"))
            or "\\" in name or ":" in name or name.rstrip(". ") != name
            or path.parts[0].lower() in PROTECTED):
        raise ValueError(f"Invalid runtime file path: {name}")
    return name


def archive_files(path: Path) -> dict[str, str]:
    files = {}
    seen = set()
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            name = checked_path(member.filename)
            if name.lower() in seen or name == "changes.json":
                raise ValueError(f"Duplicate or reserved runtime file: {name}")
            seen.add(name.lower())
            with archive.open(member) as stream:
                files[name] = hashlib.file_digest(stream, "sha256").hexdigest()
    if "interface.json" not in files or "MaaNikki.exe" not in files:
        raise ValueError("Archive is not a MaaNikki Windows runtime")
    return dict(sorted(files.items()))


def validate_snapshot(snapshot):
    if not isinstance(snapshot, dict) or not re.fullmatch(r"\d+\.\d+\.\d+", snapshot.get("version", "")):
        raise ValueError("Invalid baseline version")
    files = snapshot.get("files")
    if not isinstance(files, dict) or not {"interface.json", "MaaNikki.exe"} <= files.keys():
        raise ValueError("Invalid baseline files")
    seen = set()
    for name, checksum in files.items():
        checked_path(name)
        if name.lower() in seen or not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise ValueError("Invalid baseline file hash")
        seen.add(name.lower())
    return {"version": snapshot["version"], "files": files}


def build_updates(runtime: Path, version: str, baselines: list[dict], output: Path) -> list[Path]:
    current = {"version": version, "files": archive_files(runtime)}
    baselines = [validate_snapshot(item) for item in baselines[:HISTORY_LIMIT]]
    patches = []
    with zipfile.ZipFile(runtime) as archive:
        for baseline in baselines:
            before, after = baseline["files"], current["files"]
            added = sorted(after.keys() - before.keys())
            deleted = sorted(before.keys() - after.keys())
            modified = sorted(name for name in before.keys() & after.keys() if before[name] != after[name])
            changes = {
                "maanikki_patch": 1, "from_version": baseline["version"], "to_version": version,
                "added": added, "modified": modified, "deleted": deleted,
                "base_files": before, "target_files": after,
            }
            path = output / f"MaaNikki-v{version}-from-v{baseline['version']}.patch.zip"
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as patch:
                patch.writestr("changes.json", json.dumps(changes, ensure_ascii=False, separators=(",", ":")))
                for name in sorted(added + modified):
                    # ZipFile updates header offsets on the supplied ZipInfo.
                    # Keep the source archive intact for other baseline patches.
                    patch.writestr(copy(archive.getinfo(name)), archive.read(name))
            if path.stat().st_size >= runtime.stat().st_size:
                path.unlink()
                continue
            print(f"Incremental {baseline['version']} -> {version}: {path.stat().st_size / 1024**2:.2f} MiB "
                  f"({len(added)} added, {len(modified)} modified, {len(deleted)} deleted)", flush=True)
            patches.append(path)
    manifest = output / f"MaaNikki-v{version}-update.json"
    manifest.write_text(json.dumps({"schema_version": 1, **current, "baselines": baselines},
                                   ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    return [manifest, *patches]
