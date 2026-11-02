"""Assemble a verified candidate, update the lock, and promote only on request."""
from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import shutil
import subprocess
import sys

from .common import LOCK, MODEL_PATHS, REPOSITORY, WARDROBE, link_or_copy, log, read_json, sha256, tracked_baseline, write_json


SOURCES = {"resource/model/blessing_identity": "MaaNikki blessing identity model and label metadata",
           "resource/model/blessing_badge": "MaaNikki blessing badge model and label metadata",
           "resource/model/blessing_level": "MaaNikki blessing level model and label metadata",
           WARDROBE: "MaaNikki wardrobe ConvNeXt-Tiny identity model"}


def changed_files(run):
    """Relative paths in the assembled tree whose bytes differ from the baseline."""
    assembled = run.dir / "assembled"
    changed = []
    for path in sorted(p for p in assembled.rglob("*") if p.is_file()):
        relative = path.relative_to(assembled).as_posix()
        baseline = run.root / relative
        if not baseline.is_file() or sha256(baseline) != sha256(path):
            changed.append(relative)
    return changed


def update_lock(run, changed):
    assembled = run.dir / "assembled"
    lock = read_json(run.root / LOCK)
    entries = {row["path"]: row for row in lock["files"]}
    vanished = [path for path in entries if path.startswith(WARDROBE + "/") and not (assembled / path).is_file()]
    lock["files"] = [row for row in lock["files"] if row["path"] not in vanished]
    for relative in changed:
        if not relative.startswith("resource/model/"):
            continue
        path = assembled / relative
        source = next((text for prefix, text in SOURCES.items() if relative.startswith(prefix)), "MaaNikki model")
        row = entries.get(relative)
        if row is None:
            row = {"path": relative}
            lock["files"].append(row)
        row.update(size=path.stat().st_size, sha256=sha256(path), source=f"{source}, {date.today().isoformat()}, update {run.id}")
    target = run.dir / "assembled" / LOCK
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    target.write_text(json.dumps(lock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return lock


def verify_lock(root: Path):
    errors = []
    for row in read_json(root / LOCK)["files"]:
        path = root / row["path"]
        if not path.is_file():
            errors.append(f"missing {row['path']}")
        elif path.stat().st_size != row["size"] or sha256(path) != row["sha256"]:
            errors.append(f"changed {row['path']}")
    tracked = {row["path"] for row in read_json(root / LOCK)["files"]}
    for path in (root / WARDROBE).iterdir():
        if path.is_file() and path.relative_to(root).as_posix() not in tracked:
            errors.append(f"unlocked {path.relative_to(root).as_posix()}")
    return errors


def assemble(run):
    """Every deployable file, linked from the baseline except the candidate overlay."""
    assembled = run.dir / "assembled"
    root = run.root
    for relative in MODEL_PATHS:
        source = run.resolved(relative)
        if source.is_file():
            link_or_copy(source, assembled / relative)
    for path in (root / "resource/model/ocr").iterdir():
        link_or_copy(path, assembled / "resource/model/ocr" / path.name)
    badge = root / "resource/model/blessing_badge.npz"
    if badge.is_file():
        link_or_copy(badge, assembled / "resource/model/blessing_badge.npz")
    for path in (root / "resource/image/wardrobe").iterdir():
        if path.is_file():
            link_or_copy(path, assembled / "resource/image/wardrobe" / path.name)


def probe(run):
    runtime = run.path(run.config.get("runtime_python", "python/python.exe"))
    done = subprocess.run([str(runtime), "-I", str(Path(__file__).with_name("runtime_probe.py")),
                           str(REPOSITORY / "agent"), str(run.dir / "assembled"), str(run.evaluation)],
                          capture_output=True, text=True, encoding="utf-8", cwd=str(run.dir))
    if done.returncode:
        raise SystemExit(f"Packaged runtime refused the candidate:\n{done.stderr[-2000:]}")
    return json.loads(done.stdout.strip().splitlines()[-1])


def gate_results(run):
    return {path.stem.removesuffix("-gate"): read_json(path) for path in sorted(run.evaluation.glob("*-gate.json"))}


def package(run):
    gates = gate_results(run)
    if not gates:
        raise SystemExit("Run evaluate first")
    assemble(run)
    if (run.dir / "assembled" / WARDROBE).is_dir():
        contract = run.dir / "assembled" / WARDROBE / "model-contract.json"
        if not contract.is_file():
            raise SystemExit("Wardrobe candidate lacks a contract; run evaluate")
    else:
        for path in (run.root / WARDROBE).iterdir():
            link_or_copy(path, run.dir / "assembled" / WARDROBE / path.name)
    changed = changed_files(run)
    lock = update_lock(run, changed)
    errors = verify_lock(run.dir / "assembled")
    runtime = probe(run)
    errors += [f"{kind}: packaged runtime disagrees on {len(runtime[kind]['wrong'])} cards"
               for kind in ("wardrobe", "blessing") if runtime[kind]["wrong"]]
    blocked = [name for name, result in gates.items() if result["status"] != "pass"]
    manifest = {"run": run.id, "changed_files": {relative: {"bytes": (run.dir / "assembled" / relative).stat().st_size,
                                                             "sha256": sha256(run.dir / "assembled" / relative)}
                                                 for relative in changed},
                "lock_files": len(lock["files"]), "gates": {name: result["status"] for name, result in gates.items()},
                "blocked": blocked, "verification_errors": errors, "runtime_probe": runtime,
                "ready": not blocked and not errors}
    write_json(run.dir / "package.json", manifest)
    run.mark("package", ready=manifest["ready"])
    log("package", ready=manifest["ready"], changed=len(changed), blocked=blocked, errors=errors,
        runtime={kind: {key: runtime[kind][key] for key in ("cases", "correct")} for kind in ("wardrobe", "blessing")})
    if not manifest["ready"]:
        raise SystemExit(f"Candidate is not replaceable. Blocked gates: {blocked}; errors: {errors}")
    return manifest


def promote(run):
    """Copy a passing candidate into the project; the release check decides the outcome."""
    run.require("package")
    manifest = read_json(run.dir / "package.json")
    if not manifest["ready"]:
        raise SystemExit("The packaged candidate did not pass its gates")
    current = tracked_baseline(run.root)
    if current != run.state["baseline"]:
        drift = sorted(key for key in set(current) | set(run.state["baseline"])
                       if current.get(key) != run.state["baseline"].get(key))
        raise SystemExit(f"The project's models changed after this run began: {drift}")
    backup = run.dir / "promotion-backup"
    if backup.exists():
        raise SystemExit(f"This run was already promoted; restore from {backup} first")
    relatives = list(manifest["changed_files"]) + [LOCK]
    written = []
    try:
        for relative in relatives:
            target = run.root / relative
            if target.is_file():
                (backup / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup / relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            # Remove first: a hard-linked target must never receive the new bytes in place.
            target.unlink(missing_ok=True)
            shutil.copy2(run.dir / "assembled" / relative, target)
            written.append(relative)
        for relative in sorted(run.state["baseline"]):
            if relative.startswith(WARDROBE + "/") and not (run.dir / "assembled" / relative).exists():
                raise RuntimeError(f"Candidate removed {relative}; review it by hand")
        check = subprocess.run([sys.executable, str(run.root / "scripts/release.py"), "check"], cwd=str(run.root),
                               capture_output=True, text=True, encoding="utf-8")
        if check.returncode:
            raise RuntimeError(check.stderr[-2000:] or check.stdout[-2000:])
    except Exception:
        for relative in written:
            target = run.root / relative
            target.unlink(missing_ok=True)
            if (backup / relative).is_file():
                shutil.copy2(backup / relative, target)
        raise
    run.mark("promote", files=len(written))
    log("promote", files=written, release_check=check.stdout.strip().splitlines()[-1:],
        pending="commit, local client sync and in-game verification are separate manual steps")
