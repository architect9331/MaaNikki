"""Run directories, private configuration, hashes and project-tree helpers."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


REPOSITORY = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPOSITORY / ".local/model-update/config.json"
RUNS = REPOSITORY / ".build/model-update"
# Third-party OCR assets follow their own upstream update process.
MODEL_PATHS = ("resource/model/blessing_identity.onnx", "resource/model/blessing_identity.json",
               "resource/model/blessing_badge.onnx", "resource/model/blessing_badge.json",
               "resource/model/blessing_level.onnx", "resource/model/blessing_level.json",
               "resource/data/blessing_catalog.json")
WARDROBE = "resource/model/wardrobe"
LOCK = "build/resource-models.lock.json"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value, compact=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) if compact
            else json.dumps(value, ensure_ascii=False, indent=2))
    temporary = path.with_name(path.name + ".part")
    # Hashes bind exact bytes, and Git normalizes JSON to LF on commit.
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(path)


def sha256(path) -> str:
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def log(stage, **values):
    print(json.dumps({"stage": stage, **values}, ensure_ascii=False), flush=True)


def tracked_baseline(root: Path):
    """Hash every file a candidate may replace, so promotion can detect drift."""
    paths = [root / name for name in MODEL_PATHS]
    paths += sorted((root / WARDROBE).iterdir())
    paths.append(root / LOCK)
    return {path.relative_to(root).as_posix(): sha256(path) for path in paths if path.is_file()}


class Run:
    def __init__(self, run_id, runs=RUNS):
        self.id = run_id
        self.dir = Path(runs) / run_id
        self.state_path = self.dir / "run.json"
        if not self.state_path.is_file():
            raise SystemExit(f"Unknown run {run_id}; start it with: init --run {run_id}")
        self.state = read_json(self.state_path)
        self.root = Path(self.state["root"])
        self.config = read_json(self.state["config"])
        self.candidate = self.dir / "candidate"
        self.evaluation = self.dir / "evaluation"
        for path in self.config.get("python_paths", ()):
            resolved = str(self.path(path))
            if resolved not in sys.path:
                sys.path.insert(0, resolved)

    @staticmethod
    def create(run_id, root=REPOSITORY, config=DEFAULT_CONFIG, runs=RUNS, note=None):
        directory = Path(runs) / run_id
        if (directory / "run.json").exists():
            raise SystemExit(f"Run already exists: {directory}")
        config = Path(config).resolve()
        if not config.is_file():
            raise SystemExit(f"Missing private configuration: {config}. See BUILDING.md")
        root = Path(root).resolve()
        commit = subprocess.run(["git", "-C", str(REPOSITORY), "rev-parse", "HEAD"], capture_output=True, text=True)
        state = {"id": run_id, "root": str(root), "config": str(config), "config_sha256": sha256(config),
                 "created": datetime.now(timezone.utc).isoformat(), "note": note,
                 "git_commit": commit.stdout.strip() or None, "baseline": tracked_baseline(root), "stages": {}}
        write_json(directory / "run.json", state)
        return Run(run_id, runs)

    def path(self, value) -> Path:
        """Resolve private paths; relative ones belong to the checkout."""
        path = Path(value)
        return path if path.is_absolute() else REPOSITORY / path

    def section(self, name):
        return self.config.get(name, {})

    def mark(self, stage, **values):
        self.state["stages"][stage] = {"completed": datetime.now(timezone.utc).isoformat(), **values}
        write_json(self.state_path, self.state)

    def require(self, *stages):
        missing = [stage for stage in stages if stage not in self.state["stages"]]
        if missing:
            raise SystemExit(f"Run the earlier stage first: {', '.join(missing)}")

    def resolved(self, relative: str) -> Path:
        """Prefer the candidate overlay, then the baseline project tree."""
        candidate = self.candidate / relative
        return candidate if candidate.exists() else self.root / relative

    def link_candidate(self, relative: str, source: Path):
        target = self.candidate / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.unlink(missing_ok=True)
        shutil.copy2(source, target)
        normalize_json(target)


def normalize_json(path: Path):
    """Private trainers may write CRLF on Windows; deployed hashes must match Git's LF."""
    if path.suffix == ".json":
        data = path.read_bytes()
        if b"\r\n" in data:
            path.write_bytes(data.replace(b"\r\n", b"\n"))


def link_or_copy(source: Path, target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)
