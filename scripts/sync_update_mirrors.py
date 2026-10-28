"""Mirror an immutable GitHub release, publishing its index only after all assets."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "build/update-mirrors.json").read_text(encoding="utf-8"))


def run(*args: str, capture: bool = False) -> str:
    command = [shutil.which(args[0]) or args[0], *args[1:]]
    return subprocess.run(command, check=True, text=True, encoding="utf-8",
                          stdout=subprocess.PIPE if capture else None).stdout or ""


def checksum(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prepare(tag: str) -> tuple[Path, list[str]]:
    if not re.fullmatch(r"v\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", tag):
        raise ValueError("Invalid release tag")
    repo = CONFIG["repository"]
    release = json.loads(run("gh", "api", f"repos/{repo}/releases/tags/{tag}", capture=True))
    if release["draft"] or release["tag_name"] != tag:
        raise ValueError("Only published releases may be mirrored")
    folder = ROOT / ".build" / "update-mirrors" / tag
    folder.mkdir(parents=True, exist_ok=True)
    # Source archives and rolling manifests remain on GitHub; clients need the
    # full runtime, patches and checksum file. This keeps mirror storage small.
    assets = [asset for asset in release["assets"] if
              asset["name"].endswith(("-win-x86_64.zip", ".patch.zip", "-SHA256SUMS.txt"))]
    if not any(asset["name"] == f"MaaNikki-{tag}-win-x86_64.zip" for asset in assets):
        raise ValueError("Release has no Windows runtime")
    for asset in assets:
        name = asset["name"]
        if not re.fullmatch(r"MaaNikki-[A-Za-z0-9_.-]+", name):
            raise ValueError("Invalid release filename")
        expected = asset.get("digest", "")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected):
            raise ValueError(f"Missing GitHub SHA-256 for {name}")
        path = folder / name
        if not path.is_file() or path.stat().st_size != asset["size"] or checksum(path) != expected[7:]:
            run("gh", "release", "download", tag, "--repo", repo,
                "--pattern", name, "--dir", str(folder), "--clobber")
        if path.stat().st_size != asset["size"] or checksum(path) != expected[7:]:
            raise ValueError(f"Release asset validation failed: {name}")
    index = [{
        "tag_name": tag, "name": release["name"], "body": release["body"],
        "prerelease": release["prerelease"], "draft": False,
        "assets": [{key: asset[key] for key in ("name", "size", "browser_download_url", "digest")}
                   for asset in assets],
    }]
    (folder / "releases.json").write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    return folder, [asset["name"] for asset in assets]


def sync_r2(folder: Path, names: list[str], tag: str):
    config = CONFIG["r2"]
    bucket = config["bucket"]
    if config.get("require_workers_free") is not True:
        raise ValueError("R2 requires the Workers Free gateway")
    # The gateway's deploy command checks Workers Free with the maintainer's
    # login. CI only has R2 permissions and cannot change the Workers plan.
    managed = json.loads(run("cf", "r2", "buckets", "domains", "managed", "list",
                             "--bucket-name", bucket, capture=True))
    custom = json.loads(run("cf", "r2", "buckets", "domains", "custom", "list",
                            "--bucket-name", bucket, capture=True))
    if managed.get("enabled") or custom.get("domains"):
        raise ValueError("R2 must stay private; public access bypasses the free gateway")
    objects = json.loads(run("cf", "r2", "objects", "list", "--bucket-name", bucket,
                             "--per-page", "1000", capture=True))
    if not isinstance(objects, list) or len(objects) >= 1000:
        raise ValueError("R2 inventory is too large; clean old release folders before uploading")
    existing = {item["key"]: int(item["size"]) for item in objects}
    etags = {item["key"]: item.get("etag", "").strip('"') for item in objects}
    if "releases.json" in existing:
        previous = json.loads(run("cf", "r2", "objects", "get", "releases.json",
                                  "--bucket-name", bucket, "--text", capture=True))
        version = lambda value: tuple(map(int, re.match(r"v(\d+)\.(\d+)\.(\d+)", value).groups()))
        if previous and version(previous[0]["tag_name"]) > version(tag):
            raise ValueError("A newer R2 release is already published; refusing to replace its index")
    sizes = dict(existing)
    for name in [*names, "releases.json"]:
        key = name if name == "releases.json" else f"{tag}/{name}"
        # Reserve space for the old and new index until its replacement completes.
        sizes[key] = max(existing.get(key, 0), (folder / name).stat().st_size)
    limit = min(int(config["storage_limit_bytes"]), 2 * 1024 ** 3)
    if sum(sizes.values()) + 1024 * 1024 > limit:
        raise ValueError("R2 storage limit reached (2 GiB); clean old release folders before uploading")
    if len(names) > 20:
        raise ValueError("Too many uploads for one release")
    for name in [*names, "releases.json"]:
        path = folder / name
        if path.stat().st_size > 300_000_000:
            raise ValueError(f"{name} exceeds the cf upload API's 300 MB limit")
        key = name if name == "releases.json" else f"{tag}/{name}"
        if key in existing:
            with path.open("rb") as stream:
                md5 = hashlib.file_digest(stream, "md5").hexdigest()
            if existing[key] == path.stat().st_size and etags[key] == md5:
                print(f"r2: already uploaded {key}", flush=True)
                continue
            if name != "releases.json":
                raise ValueError(f"R2 release asset already exists with different content: {key}")
        print(f"r2: uploading {key} ({path.stat().st_size} bytes)", flush=True)
        run("cf", "r2", "objects", "put", key, "--bucket-name", bucket,
            "--file", str(path), "--content-type",
            "application/json" if name.endswith(".json") else
            "application/zip" if name.endswith(".zip") else "text/plain")


def sync_sourceforge(folder: Path, names: list[str], tag: str):
    project = CONFIG["sourceforge_project"]
    username = os.environ["SOURCEFORGE_USERNAME"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", project) or not re.fullmatch(r"[A-Za-z0-9_-]+", username):
        raise ValueError("Invalid SourceForge project or username")
    target = f"{username}@frs.sourceforge.net:/home/frs/project/{project}"
    # The SSH key and pinned known_hosts file are supplied by the workflow.
    ssh = "ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=15"
    run("rsync", "-av", "--timeout=90", "-e", ssh,
        *(str(folder / name) for name in names), f"{target}/{tag}/")
    run("rsync", "-av", "--timeout=90", "--delay-updates", "-e", ssh,
        str(folder / "releases.json"), f"{target}/")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=["r2", "sourceforge"])
    parser.add_argument("--tag")
    args = parser.parse_args()
    configured = CONFIG["r2" if args.provider == "r2" else "sourceforge_project"]
    if not configured:
        print(f"{args.provider}: not configured; skipped", flush=True)
        return
    tag = args.tag or "v" + json.loads((ROOT / "interface.json").read_text(encoding="utf-8-sig"))["version"]
    folder, names = prepare(tag)
    if args.provider == "r2":
        sync_r2(folder, names, tag)
    else:
        sync_sourceforge(folder, names, tag)
    print(f"{args.provider}: {tag} mirrored ({len(names)} assets + release index)", flush=True)


if __name__ == "__main__":
    main()
