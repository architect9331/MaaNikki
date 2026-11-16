"""Package, validate, diff, and publish MaaNikki hot-update game data bundles.

Semi-automatic publisher workflow (uses only Python stdlib and `gh` CLI):
  python scripts/publish_data.py status
  python scripts/publish_data.py package [--version 0.1.1]
  python scripts/publish_data.py publish [--version 0.1.1] [--yes]
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DATA_REPO = "architect9331/MaaNikki-data"
ASSET_PREFIX = "MaaNikki-data"
BEIJING_TZ = timezone(timedelta(hours=8))
SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)

# (workspace_source_path, runtime_overlay_target_path)
CATALOG_FILES: tuple[tuple[str, str], ...] = (
    ("resource/data/wardrobe_scoring.json", "resource/data/wardrobe_scoring.json"),
    ("resource/data/blessing_catalog.json", "resource/data/blessing_catalog.json"),
    ("resource/data/crafting_catalog.json", "resource/data/crafting_catalog.json"),
    ("resource/data/xinghai_rotation.json", "resource/data/xinghai_rotation.json"),
    ("client/src-tauri/src/data/resonance_catalog.json", "resource/data/resonance_catalog.json"),
    ("client/src/services/scoring/itemSources.json", "resource/data/item_sources.json"),
    ("resource/navigation/materials_catalog_v1.json", "resource/navigation/materials_catalog_v1.json"),
    ("resource/model/wardrobe/labels.json", "resource/model/wardrobe/labels.json"),
    ("resource/model/wardrobe/parts.json", "resource/model/wardrobe/parts.json"),
    ("resource/model/wardrobe/coverage.json", "resource/model/wardrobe/coverage.json"),
    ("resource/model/wardrobe/model-contract.json", "resource/model/wardrobe/model-contract.json"),
    ("resource/model/backpack/catalog.json", "resource/model/backpack/catalog.json"),
    ("resource/model/rejuvenate/categories.json", "resource/model/rejuvenate/categories.json"),
    ("resource/model/rejuvenate/icons.npz", "resource/model/rejuvenate/icons.npz"),
    ("resource/model/blessing_identity.json", "resource/model/blessing_identity.json"),
    ("resource/model/blessing_badge.json", "resource/model/blessing_badge.json"),
    ("resource/model/blessing_level.json", "resource/model/blessing_level.json"),
)

MODEL_FILES: tuple[tuple[str, str], ...] = (
    ("resource/model/blessing_identity.onnx", "resource/model/blessing_identity.onnx"),
    ("resource/model/blessing_level.onnx", "resource/model/blessing_level.onnx"),
    ("resource/model/blessing_badge.onnx", "resource/model/blessing_badge.onnx"),
    ("resource/model/blessing_badge.npz", "resource/model/blessing_badge.npz"),
    ("resource/model/backpack/icons.npz", "resource/model/backpack/icons.npz"),
)

STAT_LABELS: tuple[tuple[str, str], ...] = (
    ("clothes_groups", "服装评分归并组"),
    ("game_items", "游戏服装单品条目"),
    ("suits", "服装套装"),
    ("pinnacle_matches", "奇迹之冠关卡"),
    ("resonance_banners", "共鸣卡池"),
    ("resonance_outfits", "共鸣套装"),
    ("resonance_items", "共鸣可抽取部件"),
    ("blessing_sets", "祝福闪光套装"),
    ("crafting_sketches", "设计图配方"),
    ("item_sources", "服装获取途径条目"),
)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run_gh(*args: str, input_bytes: bytes | None = None, allow_not_found: bool = False) -> bytes | None:
    exe = shutil.which("gh") or "gh"
    last_err = ""
    for attempt in range(3):
        proc = subprocess.run([exe, *args], input=input_bytes, capture_output=True)
        if proc.returncode == 0:
            return proc.stdout
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        if allow_not_found and ("404" in stderr or "Not Found" in stderr):
            return None
        last_err = stderr or f"exit code {proc.returncode}"
    raise RuntimeError(f"gh {' '.join(args[:4])} failed: {last_err}")


def verify_local_data() -> dict:
    """Validate all local game catalogs, model locks, and cross-references before packaging."""
    for src, _ in (*CATALOG_FILES, *MODEL_FILES):
        path = ROOT / src
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"Missing or empty data source file: {src}")

    lock = read_json(ROOT / "build/resource-models.lock.json")
    for entry in lock.get("files", []):
        path = ROOT / entry["path"]
        if not path.is_file():
            raise ValueError(f"Missing locked model asset: {entry['path']}")
        if path.stat().st_size != entry["size"] or sha256(path) != entry["sha256"]:
            raise ValueError(f"Model lock mismatch (run model/lock sync first): {entry['path']}")

    ws = read_json(ROOT / "resource/data/wardrobe_scoring.json")
    bc = read_json(ROOT / "resource/data/blessing_catalog.json")
    cc = read_json(ROOT / "resource/data/crafting_catalog.json")
    xr = read_json(ROOT / "resource/data/xinghai_rotation.json")
    rc = read_json(ROOT / "client/src-tauri/src/data/resonance_catalog.json")
    isrc = read_json(ROOT / "client/src/services/scoring/itemSources.json")
    mc = read_json(ROOT / "resource/navigation/materials_catalog_v1.json")

    if not ws.get("items") or not ws.get("game_items") or not ws.get("suits") or not ws.get("matches"):
        raise ValueError("wardrobe_scoring.json is missing required non-empty tables")
    if not bc.get("sets") or not cc.get("sketches") or not xr.get("tasks") or not mc:
        raise ValueError("One or more auxiliary game catalogs are empty")
    if not rc.get("banners") or not rc.get("outfits") or not rc.get("items"):
        raise ValueError("resonance_catalog.json is missing banners, outfits, or items")
    if not isrc.get("sources"):
        raise ValueError("itemSources.json has no item source entries")

    outfits = rc["outfits"]
    rc_items = rc["items"]
    for banner in rc["banners"]:
        for oid in banner.get("outfits", []):
            if str(oid) not in outfits and oid not in outfits:
                raise ValueError(f"Resonance banner {banner.get('id')} references missing outfit {oid}")
    for oid, outfit in outfits.items():
        pieces = outfit.get("pieces") if isinstance(outfit, dict) else None
        if pieces:
            for pid in pieces:
                if str(pid) not in rc_items:
                    raise ValueError(f"Resonance outfit {oid} references missing piece {pid}")

    sys.path.insert(0, str(ROOT / "agent"))
    try:
        from wardrobe.model import WardrobeCatalog
        wb = WardrobeCatalog(ROOT / "resource/model/wardrobe", ROOT / "resource/data/wardrobe_scoring.json")
        if not wb.by_item_id:
            raise ValueError("WardrobeCatalog failed to index any item IDs")
    finally:
        sys.path.pop(0)

    return {
        "wardrobe_version": str(ws.get("version", "")),
        "blessing_version": str(bc.get("version", "")),
        "resonance_updated_at": str(rc.get("updated_at", "")),
        "clothes_groups": len(ws["items"]),
        "game_items": len(ws["game_items"]),
        "suits": len(ws["suits"]),
        "pinnacle_matches": len(ws["matches"]),
        "resonance_banners": len(rc["banners"]),
        "resonance_outfits": len(rc["outfits"]),
        "resonance_items": len(rc["items"]),
        "blessing_sets": len(bc["sets"]),
        "crafting_sketches": len(cc["sketches"]),
        "item_sources": len(isrc["sources"]),
    }


def collect_bundle_files(pairs: tuple[tuple[str, str], ...]) -> dict[str, dict]:
    inventory: dict[str, dict] = {}
    for src, target in sorted(pairs, key=lambda item: item[1]):
        path = ROOT / src
        inventory[target] = {"size": path.stat().st_size, "sha256": sha256(path)}
    return inventory


def write_deterministic_zip(out_path: Path, pairs: tuple[tuple[str, str], ...]) -> None:
    partial = out_path.with_name(out_path.name + ".partial")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for src, target in sorted(pairs, key=lambda item: item[1]):
            info = zipfile.ZipInfo(filename=target, date_time=FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, (ROOT / src).read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    partial.replace(out_path)


def fetch_latest_remote_manifest(repo: str = DATA_REPO) -> tuple[dict | None, str | None]:
    raw = run_gh("api", f"repos/{repo}/contents/manifest.json", allow_not_found=True)
    if not raw:
        return None, None
    doc = json.loads(raw.decode("utf-8"))
    blob_sha = doc.get("sha")
    content = base64.b64decode(doc["content"]).decode("utf-8")
    return json.loads(content), blob_sha


def next_patch_version(previous_version: str | None) -> str:
    if not previous_version:
        return "0.1.0"
    clean = previous_version.removeprefix("v")
    match = SEMVER_RE.fullmatch(clean)
    if not match:
        raise ValueError(f"Previous version is not X.Y.Z semver: {previous_version}")
    major, minor, patch = map(int, match.groups())
    return f"{major}.{minor}.{patch + 1}"


def diff_files(current: dict[str, dict], previous: dict[str, dict]) -> list[str]:
    changed = [path for path, meta in current.items() if previous.get(path) != meta]
    removed = [path for path in previous if path not in current]
    return sorted(changed + removed)


def inspect_changes(
    stats: dict,
    catalog_files: dict[str, dict],
    model_files: dict[str, dict],
    previous: dict | None,
    *,
    allow_shrink: bool = False,
) -> dict:
    if previous is None:
        lines = [
            f"- 初始游戏数据基线（衣柜数据版本 `{stats['wardrobe_version']}`，共鸣数据更新至 `{stats['resonance_updated_at']}`）",
            f"- 收录服装单品 {stats['game_items']} 件（{stats['suits']} 套）、共鸣卡池 {stats['resonance_banners']} 期、奇迹之冠 {stats['pinnacle_matches']} 关、祝福闪光 {stats['blessing_sets']} 套、设计图 {stats['crafting_sketches']} 张",
        ]
        return {
            "is_initial": True,
            "catalog_changed": sorted(catalog_files.keys()),
            "models_changed": sorted(model_files.keys()),
            "stat_deltas": {},
            "notes": "\n".join(lines),
        }

    prev_bundles = previous.get("bundles", {})
    prev_catalog = prev_bundles.get("catalog", {}).get("files", {})
    prev_models = prev_bundles.get("models", {}).get("files", {})
    cat_diff = diff_files(catalog_files, prev_catalog)
    mod_diff = diff_files(model_files, prev_models)

    prev_stats = previous.get("stats", {})
    shrunk: list[str] = []
    deltas: dict[str, tuple[int, int]] = {}
    bullet_parts: list[str] = []
    for key, label in STAT_LABELS:
        old_val = int(prev_stats.get(key, 0))
        new_val = int(stats.get(key, 0))
        if new_val < old_val:
            shrunk.append(f"{label} ({old_val} -> {new_val})")
        if new_val != old_val:
            deltas[key] = (old_val, new_val)
            sign = "+" if new_val > old_val else ""
            bullet_parts.append(f"{label} {old_val} → {new_val} ({sign}{new_val - old_val})")

    if shrunk and not allow_shrink:
        raise ValueError("Refusing to publish shrinking catalog counts without --allow-shrink: " + ", ".join(shrunk))

    lines: list[str] = []
    if stats["wardrobe_version"] != prev_stats.get("wardrobe_version"):
        lines.append(f"- 衣柜与巅峰赛数据周期更新：`{prev_stats.get('wardrobe_version')}` → `{stats['wardrobe_version']}`")
    if stats["resonance_updated_at"] != prev_stats.get("resonance_updated_at"):
        lines.append(f"- 共鸣卡池目录更新至：`{stats['resonance_updated_at']}`")
    if bullet_parts:
        lines.append("- 条目变更：" + "；".join(bullet_parts))
    if cat_diff:
        lines.append(f"- 目录数据包变更文件（{len(cat_diff)} 项）：" + "、".join(f"`{p}`" for p in cat_diff))
    if mod_diff:
        lines.append(f"- 识别模型包同步更新（{len(mod_diff)} 项）：" + "、".join(f"`{p}`" for p in mod_diff))
    else:
        lines.append(f"- 识别模型与图标库无变动（继续复用 `{prev_bundles.get('models', {}).get('tag', '')}`）")

    return {
        "is_initial": False,
        "catalog_changed": cat_diff,
        "models_changed": mod_diff,
        "stat_deltas": deltas,
        "notes": "\n".join(lines) if lines else "- 例行数据校验与元数据更新",
    }


def build_candidate(
    version: str | None = None,
    *,
    repo: str = DATA_REPO,
    allow_shrink: bool = False,
    full_models: bool = False,
    custom_note: str | None = None,
) -> dict:
    stats = verify_local_data()
    previous, remote_blob_sha = fetch_latest_remote_manifest(repo)
    resolved_version = (version or next_patch_version(previous["version"] if previous else None)).removeprefix("v")
    if not SEMVER_RE.fullmatch(resolved_version):
        raise ValueError(f"Version must be X.Y.Z semver: {resolved_version}")
    if previous and resolved_version == previous.get("version"):
        raise ValueError(f"Version v{resolved_version} is already the latest remote manifest version")

    catalog_files = collect_bundle_files(CATALOG_FILES)
    model_files = collect_bundle_files(MODEL_FILES)
    diff = inspect_changes(stats, catalog_files, model_files, previous, allow_shrink=allow_shrink)

    tag = f"v{resolved_version}"
    stage = ROOT / ".build" / "data-release" / tag
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True, exist_ok=True)

    catalog_name = f"{ASSET_PREFIX}-{tag}-catalog.zip"
    catalog_zip = stage / catalog_name
    write_deterministic_zip(catalog_zip, CATALOG_FILES)
    catalog_bundle = {
        "tag": tag,
        "asset": catalog_name,
        "size": catalog_zip.stat().st_size,
        "sha256": sha256(catalog_zip),
        "files": catalog_files,
    }

    upload_paths = [catalog_zip]
    need_models_upload = diff["is_initial"] or bool(diff["models_changed"]) or full_models
    if need_models_upload:
        models_name = f"{ASSET_PREFIX}-{tag}-models.zip"
        models_zip = stage / models_name
        write_deterministic_zip(models_zip, MODEL_FILES)
        models_bundle = {
            "tag": tag,
            "asset": models_name,
            "size": models_zip.stat().st_size,
            "sha256": sha256(models_zip),
            "files": model_files,
        }
        upload_paths.append(models_zip)
    else:
        prev_models_bundle = previous["bundles"]["models"]
        models_bundle = {
            "tag": prev_models_bundle["tag"],
            "asset": prev_models_bundle["asset"],
            "size": prev_models_bundle["size"],
            "sha256": prev_models_bundle["sha256"],
            "files": model_files,
        }

    published_at = datetime.now(BEIJING_TZ).isoformat(timespec="seconds")
    manifest = {
        "schema_version": 1,
        "version": resolved_version,
        "tag": tag,
        "published_at": published_at,
        "repository": repo,
        "stats": stats,
        "bundles": {
            "catalog": catalog_bundle,
            "models": models_bundle,
        },
    }
    manifest_name = f"{ASSET_PREFIX}-{tag}-manifest.json"
    manifest_path = stage / manifest_name
    manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    manifest_path.write_text(manifest_text, encoding="utf-8")
    upload_paths.insert(0, manifest_path)

    checksums_name = f"{ASSET_PREFIX}-{tag}-SHA256SUMS.txt"
    checksums_path = stage / checksums_name
    checksums_path.write_text(
        "".join(f"{sha256(p)}  {p.name}\n" for p in upload_paths),
        encoding="utf-8",
    )
    upload_paths.append(checksums_path)

    notes_body = custom_note.strip() if custom_note and custom_note.strip() else diff["notes"]
    notes_path = stage / "RELEASE_NOTES.md"
    notes_path.write_text(notes_body + "\n", encoding="utf-8")

    return {
        "tag": tag,
        "version": resolved_version,
        "stage": stage,
        "manifest": manifest,
        "manifest_text": manifest_text,
        "remote_blob_sha": remote_blob_sha,
        "previous": previous,
        "diff": diff,
        "notes": notes_body,
        "notes_path": notes_path,
        "upload_paths": upload_paths,
    }


def print_status(repo: str = DATA_REPO, *, allow_shrink: bool = False) -> None:
    stats = verify_local_data()
    previous, _ = fetch_latest_remote_manifest(repo)
    catalog_files = collect_bundle_files(CATALOG_FILES)
    model_files = collect_bundle_files(MODEL_FILES)
    diff = inspect_changes(stats, catalog_files, model_files, previous, allow_shrink=allow_shrink)

    if previous is None:
        print(f"[远端状态] {repo} 尚未发布任何数据包（将创建首个基线 v0.1.0）")
    else:
        print(f"[远端状态] 最新已发布版本：{previous.get('tag')}（发布于 {previous.get('published_at')}）")
        if not diff["catalog_changed"] and not diff["models_changed"]:
            print("[本地对比] 本地所有游戏目录与识别模型文件与线上完全一致，无需重复发布。")
            return
    print("[变更摘要]")
    print(diff["notes"])


def publish_candidate(candidate: dict, *, repo: str = DATA_REPO, assume_yes: bool = False, force: bool = False) -> None:
    diff = candidate["diff"]
    if not diff["is_initial"] and not diff["catalog_changed"] and not diff["models_changed"] and not force:
        print("本地游戏数据与线上最新版本完全一致，已跳过发布（如需强制重发可传 --force）。")
        return

    tag = candidate["tag"]
    existing = run_gh("api", f"repos/{repo}/releases/tags/{tag}", allow_not_found=True)
    if existing is not None:
        raise RuntimeError(f"Release {tag} already exists on {repo}")

    print(f"准备发布数据包：{repo} {tag}")
    print("发布说明：")
    print(candidate["notes"])
    print("待上传资产：")
    for path in candidate["upload_paths"]:
        print(f"  - {path.name} ({path.stat().st_size / 1024:.1f} KB, sha256={sha256(path)[:16]}...)")

    if not assume_yes:
        if not sys.stdin.isatty():
            raise SystemExit("Non-interactive session requires --yes to confirm publishing.")
        answer = input(f"\n确认发布 {tag} 到 {repo}？[y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            print("已取消发布。")
            return

    date_label = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
    title = f"MaaNikki Data {tag} ({date_label})"
    run_gh(
        "release", "create", tag,
        *(str(p) for p in candidate["upload_paths"]),
        "--repo", repo,
        "--target", "main",
        "--title", title,
        "--notes-file", str(candidate["notes_path"]),
    )

    rel_raw = run_gh("api", f"repos/{repo}/releases/tags/{tag}")
    rel = json.loads(rel_raw.decode("utf-8"))
    remote_assets = {item["name"]: item for item in rel.get("assets", [])}
    for local_path in candidate["upload_paths"]:
        remote = remote_assets.get(local_path.name)
        if not remote:
            raise RuntimeError(f"Uploaded asset missing from GitHub release: {local_path.name}")
        expected_digest = f"sha256:{sha256(local_path)}"
        if remote.get("size") != local_path.stat().st_size or remote.get("digest") != expected_digest:
            raise RuntimeError(f"Remote asset digest mismatch for {local_path.name}: {remote.get('digest')}")

    # Update main/manifest.json only after all release assets are live and verified.
    put_body: dict[str, str] = {
        "message": f"release(data): {tag}",
        "content": base64.b64encode(candidate["manifest_text"].encode("utf-8")).decode("ascii"),
    }
    if candidate["remote_blob_sha"]:
        put_body["sha"] = candidate["remote_blob_sha"]
    run_gh(
        "api", f"repos/{repo}/contents/manifest.json",
        "-X", "PUT", "--input", "-",
        input_bytes=json.dumps(put_body).encode("utf-8"),
    )
    print(f"发布完成：{rel.get('html_url')}（全部 {len(candidate['upload_paths'])} 个资产 SHA-256 校验通过，索引已更新）")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=DATA_REPO, help="Target GitHub data repository")
    sub = parser.add_subparsers(dest="command", required=True)

    st = sub.add_parser("status", help="Validate local data and show diff against latest published release")
    st.add_argument("--allow-shrink", action="store_true")

    for name, help_text in (("package", "Validate and build candidate release bundles locally"),
                            ("publish", "Validate, package, confirm, and publish to GitHub Releases")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--version", help="Explicit X.Y.Z version (defaults to auto-incrementing patch)")
        p.add_argument("--note", help="Custom Markdown release notes body")
        p.add_argument("--allow-shrink", action="store_true", help="Allow shrinking item counts when intentional")
        p.add_argument("--full-models", action="store_true", help="Upload models.zip even when unchanged")
        if name == "publish":
            p.add_argument("--yes", action="store_true", help="Skip interactive [y/N] confirmation")
            p.add_argument("--force", action="store_true", help="Publish even if no files changed")

    args = parser.parse_args()
    if args.command == "status":
        print_status(args.repo, allow_shrink=args.allow_shrink)
        return
    candidate = build_candidate(
        args.version, repo=args.repo, allow_shrink=args.allow_shrink,
        full_models=args.full_models, custom_note=args.note,
    )
    if args.command == "package":
        print(f"候选数据包已构建：{candidate['stage']}")
        for path in candidate["upload_paths"]:
            print(f"  - {path.name}: {path.stat().st_size / 1024:.1f} KB")
        return
    publish_candidate(candidate, repo=args.repo, assume_yes=args.yes, force=args.force)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Data release failed: {exc}", file=sys.stderr)
        sys.exit(1)
