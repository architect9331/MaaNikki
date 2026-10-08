"""Prepare and verify a pinned Windows x64 runtime; package source and binaries.

Requires Python 3.11+ on the build host. Uses only the standard library.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.request
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".build" / "downloads"
SOURCE_DIRS = ("agent", "client", "resource", "tasks", "options", "build", "scripts", "licenses", ".github")
SOURCE_FILES = ("interface.json", ".gitignore", ".gitattributes", ".node-version",
                "rust-toolchain.toml", "requirements.txt", "requirements.lock",
                "README.md", "AGENTS.md", "BUILDING.md", "THIRD_PARTY_NOTICES.md", "LICENSE", "LICENSE.md")
RUNTIME_DIRS = ("agent", "resource", "tasks", "options")
GENERATED_DIRS = {"node_modules", "target", "dist", "__pycache__", ".git", "schemas"}
GENERATED_SUFFIXES = {".pyc", ".pyo", ".pdb"}


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid JSON in {path}: {error}") from error


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_files():
    for name in SOURCE_FILES:
        path = ROOT / name
        if path.is_file():
            yield path
    for name in SOURCE_DIRS:
        directory = ROOT / name
        if not directory.is_dir():
            continue
        # Prune generated directories before descending (Cargo targets can be huge).
        for current, dirs, files in os.walk(directory):
            dirs[:] = sorted(d for d in dirs if d not in GENERATED_DIRS)
            for filename in sorted(files):
                path = Path(current) / filename
                if path.suffix not in GENERATED_SUFFIXES and not filename.startswith(".env"):
                    yield path


def verify_source():
    lock = read_json(ROOT / "build/runtime.lock.json")
    requirements = [line.strip() for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.startswith("#")]
    expected = [f"{wheel['package']}=={wheel['version']}" for wheel in lock["wheels"]]
    if sorted(requirements) != sorted(expected):
        raise ValueError("requirements.txt and runtime.lock.json differ")
    expected_hashes = [f"{wheel['package']}=={wheel['version']} --hash=sha256:{wheel['sha256']}"
                       for wheel in lock["wheels"]]
    locked_lines = [line.strip() for line in (ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.startswith("#")]
    if sorted(locked_lines) != sorted(expected_hashes):
        raise ValueError("requirements.lock and runtime.lock.json differ")
    versions = [read_json(ROOT / "client/package.json")["version"],
                read_json(ROOT / "client/src-tauri/tauri.conf.json")["version"],
                tomllib.loads((ROOT / "client/src-tauri/Cargo.toml").read_text(encoding="utf-8"))["package"]["version"]]
    if len(set(versions)) != 1:
        raise ValueError(f"Client versions differ: {versions}")
    for model in read_json(ROOT / "build/resource-models.lock.json")["files"]:
        path = ROOT / model["path"]
        if path.stat().st_size != model["size"] or sha256(path) != model["sha256"]:
            raise ValueError(f"OCR model changed: {model['path']}")
    interface = read_json(ROOT / "interface.json")
    for imported in interface.get("import", []):
        if not (ROOT / imported).is_file():
            raise ValueError(f"Missing interface import: {imported}")
    files = list(source_files())
    for path in files:
        # TypeScript config files use JSONC and are checked by the frontend build.
        if path.suffix == ".json" and not path.name.startswith("tsconfig"):
            read_json(path)
        elif path.suffix == ".py":
            compile(path.read_text(encoding="utf-8-sig"), str(path), "exec")
        if path.stat().st_size >= 100 * 1024 * 1024:
            raise ValueError(f"Source file exceeds GitHub's 100 MiB limit: {path}")
    size = sum(path.stat().st_size for path in files)
    print(f"Source verified: {len(files)} files, {size / 1024**2:.1f} MiB. OCR hashes verified.", flush=True)
    return files


def download(artifact: dict) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / artifact["name"]
    if path.is_file() and sha256(path) == artifact["sha256"]:
        return path
    temporary = path.with_name(path.name + ".part")
    for attempt in range(3):
        try:
            print(f"Downloading {artifact['name']}", flush=True)
            request = urllib.request.Request(artifact["url"], headers={"User-Agent": "MaaNikki-build"})
            with urllib.request.urlopen(request, timeout=180) as response, temporary.open("wb") as out:
                shutil.copyfileobj(response, out)
            if sha256(temporary) != artifact["sha256"]:
                raise ValueError(f"SHA-256 mismatch: {artifact['name']}")
            temporary.replace(path)
            return path
        except (OSError, ValueError):
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("Unreachable")


def extract_member(archive: zipfile.ZipFile, member: zipfile.ZipInfo, destination: Path, relative: str):
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or "\\" in relative or ":" in relative:
        raise ValueError(f"Unsafe archive path: {relative}")
    target = destination.joinpath(*path.parts).resolve()
    if not target.is_relative_to(destination.resolve()):
        raise ValueError(f"Unsafe archive path: {relative}")
    if member.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with archive.open(member) as source, target.open("wb") as out:
        shutil.copyfileobj(source, out)


def build_output(path: Path) -> Path:
    path = path.resolve()
    # Generated files must stay separate from the installed runtime and source.
    if not any(path.is_relative_to(ROOT / area) and path != ROOT / area for area in (".build", "artifacts")):
        raise ValueError("Output must be a subdirectory of .build/ or artifacts/")
    if path.exists() and any(path.iterdir()):
        raise ValueError(f"Output is not empty; choose a new output directory: {path}")
    path.mkdir(parents=True, exist_ok=True)
    return path


def prepare(output: Path) -> Path:
    if platform.system() != "Windows" or platform.machine().lower() not in ("amd64", "x86_64"):
        raise RuntimeError("This runtime targets Windows x64 only")
    verify_source()
    output = build_output(output)
    lock = read_json(ROOT / "build/runtime.lock.json")
    with zipfile.ZipFile(download(lock["python"])) as archive:
        for member in archive.infolist():
            extract_member(archive, member, output / "python", member.filename)
    (output / "python/python312._pth").write_text(
        "python312.zip\n.\nLib/site-packages\nimport site\n", encoding="utf-8")
    with zipfile.ZipFile(download(lock["maafw"])) as archive:
        for member in archive.infolist():
            if member.filename.startswith("bin/") and member.filename.endswith(".dll"):
                extract_member(archive, member, output / "maafw", member.filename[4:])
            elif member.filename.startswith("share/MaaAgentBinary/") and not member.is_dir():
                extract_member(archive, member, output / "maafw/MaaAgentBinary",
                               member.filename[len("share/MaaAgentBinary/"):])
            elif member.filename == "LICENSE.md":
                extract_member(archive, member, output / "licenses", "MaaFramework-LGPL-3.0.md")
    for wheel in lock["wheels"]:
        with zipfile.ZipFile(download(wheel)) as archive:
            for member in archive.infolist():
                # The SDK supplies identical native DLLs in maafw/. Avoid a duplicate set.
                if member.filename.startswith("maa/bin/"):
                    continue
                parts = PurePosixPath(member.filename).parts
                if not parts:
                    continue
                if parts[0].endswith(".data"):
                    if len(parts) < 3 or parts[1] not in ("purelib", "platlib"):
                        continue  # Console entry points are not used by the embedded runtime.
                    relative = "/".join(parts[2:])
                else:
                    relative = member.filename
                extract_member(archive, member, output / "python/Lib/site-packages", relative)
    for directory in RUNTIME_DIRS:
        shutil.copytree(ROOT / directory, output / directory, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(ROOT / "interface.json", output / "interface.json")
    if (ROOT / "licenses").is_dir():
        shutil.copytree(ROOT / "licenses", output / "licenses", dirs_exist_ok=True)
    for name in ("README.md", "THIRD_PARTY_NOTICES.md", "LICENSE", "LICENSE.md"):
        if (ROOT / name).is_file():
            shutil.copy2(ROOT / name, output / name)
    shutil.copy2(ROOT / "build/runtime.lock.json", output / "runtime-manifest.json")
    verify_runtime(output)
    print(f"Runtime prepared: {output}", flush=True)
    return output


def verify_runtime(output: Path):
    output = output.resolve()
    lock = read_json(ROOT / "build/runtime.lock.json")
    # Importing main registers actions/recognitions; it does not start the socket server or control the game.
    check = """
import importlib.metadata, json, pathlib, sys
import cv2, numpy, scipy, strenum
from maa.library import Library
from maa.resource import Resource
from maa.tasker import Tasker
root = pathlib.Path.cwd()
expected = json.loads(sys.argv[1])
assert '.'.join(map(str, sys.version_info[:3])) == expected['python_version']
for package in expected['wheels']:
    actual = importlib.metadata.version(package['package']).lstrip('v')
    assert actual == package['version'], (package['package'], actual, package['version'])
assert Library.version().lstrip('v') == expected['maafw_version'], Library.version()
Tasker.set_log_dir(str(root / 'debug'))
resource = Resource()
resource.use_cpu()
job = resource.post_bundle(root / 'resource').wait()
assert job.succeeded and resource.loaded, 'MaaFramework resource loading failed'
print('Runtime verified: pinned dependency versions, OCR and pipeline resource loading.')
"""
    env = os.environ.copy()
    env["MAAFW_BINARY_PATH"] = str(output / "maafw")
    env["PYTHONUTF8"] = "1"
    checks = [(check, [json.dumps(lock)]),
              ("import runpy; runpy.run_path('agent/main.py', run_name='maanikki_import_check'); "
               "print('Agent imports and action/recognition registrations verified.')", [])]
    # Resource/client and agent/server bindings require separate native library contexts.
    for code, arguments in checks:
        result = subprocess.run([str(output / "python/python.exe"), "-B", "-c", code, *arguments],
                                cwd=output, env=env, timeout=180)
        if result.returncode:
            raise RuntimeError(f"Runtime verification failed with exit code {result.returncode}; see output above")


def write_zip(path: Path, files, base: Path):
    temporary = path.with_name(path.name + ".partial")
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6, strict_timestamps=False) as archive:
        for source in sorted(files):
            archive.write(source, source.relative_to(base).as_posix())
    temporary.replace(path)


def collect_dependency_licenses(output: Path):
    """Keep notices for installed JS packages and locally compiled registry crates."""
    index = []

    def collect(directory: Path, category: str, name: str, version: str, declared_license):
        notices = []
        for current, dirs, files in os.walk(directory):
            dirs[:] = [item for item in dirs if item not in {"node_modules", ".git", "target", "tests"}]
            for filename in files:
                upper = filename.upper()
                if not upper.startswith(("LICENSE", "LICENCE", "NOTICE", "COPYRIGHT", "COPYING")):
                    continue
                source = Path(current) / filename
                relative = source.relative_to(directory)
                destination = output / "licenses/dependencies" / category / f"{name.replace('/', '__')}@{version}" / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                notices.append(destination.relative_to(output).as_posix())
        index.append({"ecosystem": category, "package": name, "version": version,
                      "declared_license": declared_license, "notices": notices})

    modules = ROOT / "client/node_modules/.pnpm"
    if not modules.is_dir():
        raise FileNotFoundError("Install the locked frontend dependencies before packaging")
    for package_json in sorted(modules.glob("*/node_modules/*/package.json")) + sorted(modules.glob("*/node_modules/@*/*/package.json")):
        if package_json.is_symlink() or package_json.parent.is_symlink():
            continue
        info = read_json(package_json)
        collect(package_json.parent, "npm", info["name"], info["version"], info.get("license"))

    cargo_home = Path(os.environ.get("CARGO_HOME") or
                      (ROOT / ".build/cargo" if (ROOT / ".build/cargo").is_dir() else Path.home() / ".cargo"))
    registry = cargo_home / "registry/src"
    if not registry.is_dir():
        raise FileNotFoundError("The Cargo source cache from the client build is required for license collection")
    packages = tomllib.loads((ROOT / "client/src-tauri/Cargo.lock").read_text(encoding="utf-8"))["package"]
    for dependency in packages:
        if not dependency.get("source", "").startswith("registry+"):
            continue
        directories = list(registry.glob(f"*/{dependency['name']}-{dependency['version']}"))
        if not directories:
            continue  # Dependencies for other target platforms were not compiled locally.
        directory = directories[0]
        metadata = tomllib.loads((directory / "Cargo.toml").read_text(encoding="utf-8"))["package"]
        collect(directory, "cargo", dependency["name"], dependency["version"], metadata.get("license"))
    path = output / "licenses/dependencies/index.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Preserved license files for {len(index)} installed JS/Rust dependencies.", flush=True)


def package(executable: Path):
    if not executable.is_file():
        raise FileNotFoundError(f"Build the desktop client first: {executable}")
    files = verify_source()
    version = read_json(ROOT / "interface.json")["version"]
    stage = prepare(ROOT / ".build/staging" / uuid.uuid4().hex)
    shutil.copy2(executable, stage / "MaaNikki.exe")
    collect_dependency_licenses(stage)
    # Runtime checks produce logs; only selected release files enter the archive.
    release_files = [path for path in stage.rglob("*") if path.is_file()
                     and path.relative_to(stage).parts[0] not in ("debug", "cache", "config")
                     and "__pycache__" not in path.parts and path.suffix not in GENERATED_SUFFIXES]
    out = ROOT / "artifacts"
    binary_zip = out / f"MaaNikki-v{version}-win-x86_64.zip"
    source_zip = out / f"MaaNikki-v{version}-source.zip"
    write_zip(binary_zip, release_files, stage)
    write_zip(source_zip, files, ROOT)
    checksum_path = out / f"MaaNikki-v{version}-SHA256SUMS.txt"
    checksum_path.write_text("".join(f"{sha256(path)}  {path.name}\n" for path in (binary_zip, source_zip)), encoding="utf-8")
    print(f"Release package: {binary_zip}\nCorresponding source: {source_zip}\nChecksums: {checksum_path}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="Verify source manifests and list the public source scope")
    runtime = sub.add_parser("prepare", help="Download, assemble and verify a clean runtime")
    runtime.add_argument("--output", type=Path, required=True)
    verify = sub.add_parser("verify-runtime", help="Verify an already prepared runtime")
    verify.add_argument("--output", type=Path, required=True)
    release = sub.add_parser("package", help="Package a newly built executable with a clean runtime")
    release.add_argument("--executable", type=Path, default=ROOT / "client/src-tauri/target/release/mxu.exe")
    args = parser.parse_args()
    if args.command == "check":
        files = verify_source()
        manifest = ROOT / ".build/source-files.txt"
        manifest.parent.mkdir(exist_ok=True)
        manifest.write_text("\n".join(sorted(path.relative_to(ROOT).as_posix() for path in files)) + "\n", encoding="utf-8")
        print(f"Public source inventory: {manifest}")
    elif args.command == "prepare":
        prepare(args.output)
    elif args.command == "verify-runtime":
        verify_runtime(args.output)
    else:
        package(args.executable.resolve())


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Build preparation failed: {error}", file=sys.stderr)
        sys.exit(1)
