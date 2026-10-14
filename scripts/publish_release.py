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
    if release is not None and release["target_commitish"] != sha:
        raise ValueError(f"Draft {tag} belongs to another commit; rerun its original build")
    ensure_tag(tag, sha)
    if release is None:
        notes = api("releases/generate-notes", "POST", {"tag_name": tag, "target_commitish": sha})
        release = api("releases", "POST", {
            "tag_name": tag,
            "target_commitish": sha,
            "name": f"MaaNikki {tag}",
            "body": notes["body"],
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
        raise ValueError("Release attachments do not match the expected three files")
    for name, checksum in packages.items():
        asset = assets[name]
        if asset["state"] != "uploaded" or asset.get("digest") != f"sha256:{checksum}":
            raise ValueError(f"Uploaded asset checksum mismatch: {name}")
    published = api(f"releases/{release['id']}", "PATCH", {
        "draft": False, "prerelease": True, "make_latest": "false",
    })
    report(f"Published prerelease [{tag}]({published['html_url']}) with verified attachments.")


if __name__ == "__main__":
    main()
