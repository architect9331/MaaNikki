"""Publish verified build artifacts as a GitHub prerelease using only stdlib and gh."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.request

from incremental import HISTORY_LIMIT, archive_files, build_updates, validate_snapshot

ROOT = Path(__file__).resolve().parents[1]


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


def main():
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("This publisher must run in GitHub Actions")
    if os.environ.get("GITHUB_EVENT_NAME") not in {"push", "workflow_dispatch"}:
        raise ValueError("Only branch pushes and manual builds may publish releases")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    if os.environ["GITHUB_REF"] != f"refs/heads/{event['repository']['default_branch']}":
        raise ValueError("Only the default branch may publish releases")
    sha = os.environ["GITHUB_SHA"]
    checked_out = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
    ).strip()
    if checked_out != sha:
        raise ValueError("Checkout does not match the build commit")
    version = json.loads((ROOT / "interface.json").read_text(encoding="utf-8-sig"))["version"]
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", version):
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
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as stream:
            stream.write("published=true\n")
    report(f"Published prerelease [{tag}]({published['html_url']}) with verified attachments.")


if __name__ == "__main__":
    main()
