"""Publish verified build artifacts as a GitHub prerelease using only stdlib and gh."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.request

from incremental import HISTORY_LIMIT, archive_files, build_updates, validate_snapshot
from release import verify_source

ROOT = Path(__file__).resolve().parents[1]
BEIJING_TZ = timezone(timedelta(hours=8))
VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?")
SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
SECTION_RE = re.compile(
    r"^## \[([^\]]+)\](?:[ \t]*-[ \t]*([^\n]*))?[ \t]*\n(.*?)(?=^## \[|\Z)",
    re.MULTILINE | re.DOTALL,
)
UNRELEASED_HEADERS = {"待发布", "unreleased"}
RELEASE_FILES = (
    "CHANGELOG.md",
    "interface.json",
    "client/package.json",
    "client/src-tauri/tauri.conf.json",
    "client/src-tauri/Cargo.toml",
    "client/src-tauri/Cargo.lock",
)


def api(path: str, method: str = "GET", data=None, *, missing_ok: bool = False):
    request = urllib.request.Request(
        f"{os.environ['GITHUB_API_URL']}/repos/{os.environ['GITHUB_REPOSITORY']}/{path}",
        data=None if data is None else json.dumps(data).encode("utf-8"),
        method=method,
        headers={
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if missing_ok and error.code == 404:
            return None
        raise RuntimeError(f"GitHub {method} {path} failed: HTTP {error.code}") from error


def find_release(tag: str):
    # List authenticated releases so interrupted drafts are included as well.
    page = 1
    while True:
        releases = api(f"releases?per_page=100&page={page}")
        for release in releases:
            if release["tag_name"] == tag:
                return release
        if len(releases) < 100:
            return None
        page += 1


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def previous_baselines(version: str) -> list[dict]:
    # Bootstrap once from the last full archive. Subsequent releases carry the
    # rolling file manifests, so CI need not download ten historical runtimes.
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        return []
    versions = []
    page = 1
    current = tuple(map(int, version.split(".")))
    while True:
        releases = api(f"releases?per_page=100&page={page}")
        for release in releases:
            tag = release["tag_name"].removeprefix("v")
            if not release["draft"] and re.fullmatch(r"\d+\.\d+\.\d+", tag):
                if tuple(map(int, tag.split("."))) < current:
                    versions.append((tuple(map(int, tag.split("."))), release))
        if len(releases) < 100:
            break
        page += 1
    if not versions:
        return []
    previous = max(versions, key=lambda item: item[0])[1]
    tag = previous["tag_name"]
    prefix = f"MaaNikki-{tag}"
    assets = {asset["name"]: asset for asset in previous["assets"]}
    manifest_name = f"{prefix}-update.json"
    name = manifest_name if manifest_name in assets else f"{prefix}-win-x86_64.zip"
    if name not in assets:
        report(f"No runtime baseline for {tag}; publishing a full package and initial file manifest.")
        return []
    folder = ROOT / ".build" / "incremental" / tag
    folder.mkdir(parents=True, exist_ok=True)
    subprocess.run(["gh", "release", "download", tag, "--repo", os.environ["GITHUB_REPOSITORY"],
                    "--pattern", name, "--dir", str(folder), "--clobber"], check=True)
    downloaded = folder / name
    if assets[name].get("digest") != f"sha256:{digest(downloaded)}":
        raise ValueError(f"Baseline asset checksum mismatch: {name}")
    if name == manifest_name:
        manifest = json.loads(downloaded.read_text(encoding="utf-8"))
        if manifest.get("schema_version") != 1 or manifest.get("version") != tag.removeprefix("v"):
            raise ValueError("Baseline manifest does not match its release")
        snapshots = [validate_snapshot(manifest),
                     *(validate_snapshot(item) for item in manifest.get("baselines", []))]
        unique = {item["version"]: item for item in snapshots
                  if tuple(map(int, item["version"].split("."))) < current}
        return sorted(unique.values(), key=lambda item: tuple(map(int, item["version"].split("."))),
                      reverse=True)[:HISTORY_LIMIT]
    return [{"version": tag.removeprefix("v"), "files": archive_files(downloaded)}]


def add_incremental_packages(version: str, packages: dict[str, str]):
    runtime = ROOT / "artifacts" / f"MaaNikki-v{version}-win-x86_64.zip"
    extra = build_updates(runtime, version, previous_baselines(version), ROOT / "artifacts")
    sums = ROOT / "artifacts" / f"MaaNikki-v{version}-SHA256SUMS.txt"
    for path in extra:
        packages[path.name] = digest(path)
    sums.write_text("".join(f"{checksum}  {name}\n" for name, checksum in sorted(packages.items())
                            if name != sums.name), encoding="utf-8")
    packages[sums.name] = digest(sums)


def verify_packages(version: str) -> dict[str, str]:
    prefix = f"MaaNikki-v{version}"
    names = [f"{prefix}-win-x86_64.zip", f"{prefix}-source.zip"]
    sums = ROOT / "artifacts" / f"{prefix}-SHA256SUMS.txt"
    entries = {}
    for line in sums.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if match is None or match[2] in entries:
            raise ValueError("Invalid or duplicate SHA256SUMS entry")
        entries[match[2]] = match[1]
    if set(entries) != set(names):
        raise ValueError("SHA256SUMS must contain exactly the runtime and source archives")
    for name in names:
        if digest(ROOT / "artifacts" / name) != entries[name]:
            raise ValueError(f"Package checksum mismatch: {name}")
    entries[sums.name] = digest(sums)
    return entries


def ensure_tag(tag: str, sha: str):
    reference = api(f"git/ref/tags/{tag}", missing_ok=True)
    if reference is None:
        api("git/refs", "POST", {"ref": f"refs/tags/{tag}", "sha": sha})
        return
    target = reference["object"]
    while target["type"] == "tag":
        target = api(f"git/tags/{target['sha']}")["object"]
    if target["type"] != "commit" or target["sha"] != sha:
        raise ValueError(f"Existing tag {tag} does not point to this build; refusing to move it")


def report(message: str):
    print(message, flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(message + "\n")


def release_notes(version: str, tag: str, sha: str) -> str:
    changelog = ROOT / "CHANGELOG.md"
    if not changelog.is_file():
        return api("releases/generate-notes", "POST", {
            "tag_name": tag, "target_commitish": sha,
        })["body"]
    match = re.search(
        rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## |\Z)",
        changelog.read_text(encoding="utf-8-sig"), re.MULTILINE | re.DOTALL,
    )
    if match is None or not match[1].strip():
        raise ValueError(f"CHANGELOG.md has no release notes for {version}")
    # Relative links in repository documentation need a commit URL on Releases.
    body = match[1].strip()
    base = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ['GITHUB_REPOSITORY']}/blob/{sha}/"
    return re.sub(r"(\]\()(docs/[^)]+)(\))", lambda m: m[1] + base + m[2] + m[3], body)


def write_outputs(**values: str):
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as stream:
            for key, value in values.items():
                stream.write(f"{key}={value}\n")


def check_actions_branch() -> str:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("This publisher must run in GitHub Actions")
    if os.environ.get("GITHUB_EVENT_NAME") not in {"push", "workflow_dispatch", "schedule"}:
        raise ValueError("Only branch pushes, scheduled runs, and manual builds may publish releases")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    default_branch = event.get("repository", {}).get("default_branch") or "main"
    if os.environ.get("GITHUB_REF") != f"refs/heads/{default_branch}":
        raise ValueError("Only the default branch may publish releases")
    return default_branch


def published_releases() -> dict[str, dict]:
    published = {}
    page = 1
    while True:
        releases = api(f"releases?per_page=100&page={page}")
        for release in releases:
            tag = release["tag_name"]
            if not release["draft"] and tag.startswith("v"):
                published[tag.removeprefix("v")] = release
        if len(releases) < 100:
            return published
        page += 1


def latest_published_semver(published: dict[str, dict]) -> str | None:
    candidates = [
        (tuple(map(int, version.split("."))), version)
        for version in published
        if SEMVER_RE.fullmatch(version)
    ]
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def bump_semver(base: str, rule: str) -> str:
    match = SEMVER_RE.fullmatch(base)
    if match is None:
        raise ValueError(f"Cannot auto-increment non-standard version: {base}")
    major, minor, patch = map(int, match.groups())
    if rule == "major":
        return f"{major + 1}.0.0"
    if rule == "minor":
        return f"{major}.{minor + 1}.0"
    if rule == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"Unsupported version bump rule: {rule}")


def has_notes(body: str) -> bool:
    return any(
        line.strip() and not line.lstrip().startswith("#")
        for line in body.splitlines()
    )


def replace_once(path: Path, old: str, new: str):
    raw = path.read_bytes()
    newline = "\r\n" if b"\r\n" in raw else "\n"
    text = raw.decode("utf-8").replace("\r\n", "\n")
    if text.count(old) != 1:
        raise ValueError(f"Expected exactly one occurrence of {old!r} in {path}")
    updated = text.replace(old, new, 1).replace("\n", newline)
    path.write_bytes(updated.encode("utf-8"))


def maybe_bump_client_version(last_published: str | None):
    if not last_published:
        return
    last_tag = f"v{last_published}"
    verify_tag = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{last_tag}"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if verify_tag.returncode != 0:
        return
    diff = subprocess.run(
        ["git", "diff", "--quiet", f"refs/tags/{last_tag}..HEAD", "--", "client"],
        cwd=ROOT,
    )
    if diff.returncode == 0:
        return
    tag_pkg_raw = subprocess.check_output(
        ["git", "show", f"refs/tags/{last_tag}:client/package.json"],
        cwd=ROOT, text=True, encoding="utf-8",
    )
    tag_client_version = json.loads(tag_pkg_raw)["version"]
    current_client_version = json.loads((ROOT / "client/package.json").read_text(encoding="utf-8"))["version"]
    if current_client_version != tag_client_version:
        return
    match = re.fullmatch(r"(.*?\.)(\d+)", current_client_version)
    if match is None:
        raise ValueError(f"Cannot auto-increment client version: {current_client_version}")
    new_client_version = f"{match.group(1)}{int(match.group(2)) + 1}"
    replace_once(
        ROOT / "client/package.json",
        f'  "version": "{current_client_version}",\n',
        f'  "version": "{new_client_version}",\n',
    )
    replace_once(
        ROOT / "client/src-tauri/tauri.conf.json",
        f'  "version": "{current_client_version}",\n',
        f'  "version": "{new_client_version}",\n',
    )
    replace_once(
        ROOT / "client/src-tauri/Cargo.toml",
        f'name = "mxu"\nversion = "{current_client_version}"\n',
        f'name = "mxu"\nversion = "{new_client_version}"\n',
    )
    replace_once(
        ROOT / "client/src-tauri/Cargo.lock",
        f'[[package]]\nname = "mxu"\nversion = "{current_client_version}"\n',
        f'[[package]]\nname = "mxu"\nversion = "{new_client_version}"\n',
    )
    report(f"Incremented client version: {current_client_version} -> {new_client_version}")


def prepare_release():
    default_branch = check_actions_branch()
    head_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    input_version = os.environ.get("INPUT_VERSION", "").strip()
    if input_version and input_version not in {"patch", "minor", "major"} and not VERSION_RE.fullmatch(input_version):
        raise ValueError(f"Invalid target version or bump rule: {input_version}")

    changelog_path = ROOT / "CHANGELOG.md"
    changelog_raw = changelog_path.read_bytes()
    changelog_newline = "\r\n" if b"\r\n" in changelog_raw else "\n"
    changelog_text = changelog_raw.decode("utf-8-sig").replace("\r\n", "\n")
    sections = list(SECTION_RE.finditer(changelog_text))
    if not sections:
        raise ValueError("CHANGELOG.md has no version sections")

    published = published_releases()
    last_published = latest_published_semver(published)
    interface_version = json.loads((ROOT / "interface.json").read_text(encoding="utf-8-sig"))["version"]
    if not isinstance(interface_version, str) or not VERSION_RE.fullmatch(interface_version):
        raise ValueError("Invalid project version in interface.json")

    first = sections[0]
    header = first.group(1).strip()
    date_part = (first.group(2) or "").strip()
    body = first.group(3)
    is_unreleased = header.lower() in UNRELEASED_HEADERS

    if is_unreleased:
        if not has_notes(body):
            if input_version:
                raise ValueError("CHANGELOG.md [待发布] section has no release notes")
            write_outputs(should_build="false", sha=head_sha, version="")
            report("CHANGELOG.md [待发布] section is empty; skipping build.")
            return
        base_version = last_published or interface_version
        if input_version in {"patch", "minor", "major"}:
            target_version = bump_semver(base_version, input_version)
        elif input_version:
            target_version = input_version
        elif interface_version not in published:
            target_version = interface_version
        else:
            target_version = bump_semver(base_version, "patch")
    else:
        if not VERSION_RE.fullmatch(header):
            raise ValueError(f"Invalid top version heading in CHANGELOG.md: [{header}]")
        if header in published:
            if input_version:
                raise ValueError(
                    f"v{header} is already published; add release notes under ## [待发布] in CHANGELOG.md first"
                )
            write_outputs(should_build="false", sha=head_sha, version="")
            report(f"Latest changelog version v{header} is already published and there is no [待发布] section; skipping build.")
            return
        if not has_notes(body):
            raise ValueError(f"CHANGELOG.md has no release notes for {header}")
        base_version = last_published or interface_version
        if input_version in {"patch", "minor", "major"}:
            target_version = bump_semver(base_version, input_version)
        elif input_version:
            target_version = input_version
        else:
            target_version = header

    if target_version in published:
        raise ValueError(f"Target version v{target_version} is already published")

    bjt_today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
    effective_date = date_part if (not is_unreleased and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_part)) else bjt_today
    new_heading = f"## [{target_version}] - {effective_date}"
    line_end = changelog_text.find("\n", first.start())
    old_heading = changelog_text[first.start():line_end]
    if old_heading != new_heading:
        updated_changelog = (
            changelog_text[:first.start()]
            + new_heading
            + changelog_text[line_end:]
        ).replace("\n", changelog_newline)
        changelog_path.write_bytes(updated_changelog.encode("utf-8"))

    if interface_version != target_version:
        replace_once(
            ROOT / "interface.json",
            f'  "version": "{interface_version}",\n',
            f'  "version": "{target_version}",\n',
        )

    maybe_bump_client_version(last_published)
    verify_source()
    release_notes(target_version, f"v{target_version}", head_sha)

    status = subprocess.check_output(
        ["git", "status", "--porcelain", "--", *RELEASE_FILES], cwd=ROOT, text=True,
    ).strip()
    if status:
        subprocess.run(["git", "add", "--", *RELEASE_FILES], cwd=ROOT, check=True)
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], cwd=ROOT, check=True)
        subprocess.run(
            ["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"],
            cwd=ROOT, check=True,
        )
        subprocess.run(["git", "commit", "-m", f"chore(release): v{target_version}"], cwd=ROOT, check=True)
        subprocess.run(["git", "push", "origin", f"HEAD:refs/heads/{default_branch}"], cwd=ROOT, check=True)
        head_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        report(f"Finalized and pushed release commit for v{target_version} ({head_sha[:7]}).")
    else:
        report(f"Using existing release commit for v{target_version} ({head_sha[:7]}).")

    write_outputs(should_build="true", sha=head_sha, version=target_version)


def publish_release():
    check_actions_branch()
    sha = os.environ.get("RELEASE_SHA") or os.environ["GITHUB_SHA"]
    checked_out = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
    ).strip()
    if checked_out != sha:
        raise ValueError("Checkout does not match the build commit")
    version = json.loads((ROOT / "interface.json").read_text(encoding="utf-8-sig"))["version"]
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise ValueError("Invalid project version")
    tag = f"v{version}"
    release = find_release(tag)
    if release is not None and not release["draft"]:
        report(f"{tag} is already published; leaving its tag and assets unchanged.")
        return

    packages = verify_packages(version)
    add_incremental_packages(version, packages)
    if release is not None and release["target_commitish"] != sha:
        raise ValueError(f"Draft {tag} belongs to another commit; rerun its original build")
    body = release_notes(version, tag, sha)
    ensure_tag(tag, sha)
    if release is None:
        release = api("releases", "POST", {
            "tag_name": tag,
            "target_commitish": sha,
            "name": f"MaaNikki {tag}",
            "body": body,
            "draft": True,
            "prerelease": True,
        })

    # Retry uploads only while this version remains a draft. Published assets are immutable.
    subprocess.run([
        "gh", "release", "upload", tag, "--repo", os.environ["GITHUB_REPOSITORY"],
        "--clobber", *(str(ROOT / "artifacts" / name) for name in packages),
    ], cwd=ROOT, check=True)
    uploaded = api(f"releases/{release['id']}/assets?per_page=100")
    assets = {asset["name"]: asset for asset in uploaded}
    if len(uploaded) != len(packages) or set(assets) != set(packages):
        raise ValueError("Release attachments do not match the verified packages and update manifest")
    for name, checksum in packages.items():
        asset = assets[name]
        if asset["state"] != "uploaded" or asset.get("digest") != f"sha256:{checksum}":
            raise ValueError(f"Uploaded asset checksum mismatch: {name}")
    published = api(f"releases/{release['id']}", "PATCH", {
        "draft": False, "prerelease": True, "make_latest": "false", "body": body,
    })
    write_outputs(published="true")
    report(f"Published prerelease [{tag}]({published['html_url']}) with verified attachments.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=("prepare", "publish"), default="publish")
    args = parser.parse_args()
    if args.action == "prepare":
        prepare_release()
    else:
        publish_release()


if __name__ == "__main__":
    main()
