#!/usr/bin/env python3
import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OWNER = "ZelbertYQ"
REPO = "MCMI-Tools"


def run(args, check=True, capture=False):
    result = subprocess.run(
        args,
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if check and result.returncode != 0:
        if capture:
            if result.stdout:
                print(result.stdout, end="")
            if result.stderr:
                print(result.stderr, end="", file=sys.stderr)
        raise SystemExit(result.returncode)
    return result


def read_version():
    init_path = ROOT / "__init__.py"
    tree = ast.parse(init_path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "bl_info":
                bl_info = ast.literal_eval(node.value)
                version = bl_info.get("version")
                if (
                    isinstance(version, tuple)
                    and len(version) > 0
                    and all(isinstance(value, int) for value in version)
                ):
                    return version
    raise SystemExit("Could not read bl_info['version'] from __init__.py")


def tag_from_version(version):
    return "v" + ".".join(str(value) for value in version)


def current_branch():
    result = run(["git", "branch", "--show-current"], capture=True)
    return result.stdout.strip()


def has_dirty_worktree():
    result = run(["git", "status", "--porcelain"], capture=True)
    return bool(result.stdout.strip())


def tag_exists(tag):
    result = run(["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}"], check=False, capture=True)
    return result.returncode == 0


def remote_tag_exists(tag):
    result = run(["git", "ls-remote", "--tags", "origin", tag], check=False, capture=True)
    return bool(result.stdout.strip())


def release_exists(tag, token):
    request = urllib.request.Request(
        f"https://api.github.com/repos/{OWNER}/{REPO}/releases/tags/{tag}",
        headers=github_headers(token),
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status == 200
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return False
        raise


def github_headers(token):
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": f"{REPO}-release-script",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def create_release_with_api(tag, title, notes, token, draft=False, prerelease=False):
    if not token:
        raise SystemExit("GITHUB_TOKEN is required when GitHub CLI is not available")
    payload = {
        "tag_name": tag,
        "target_commitish": "main",
        "name": title,
        "body": notes,
        "draft": draft,
        "prerelease": prerelease,
    }
    request = urllib.request.Request(
        f"https://api.github.com/repos/{OWNER}/{REPO}/releases",
        data=json.dumps(payload).encode("utf-8"),
        headers=github_headers(token),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request) as response:
            if response.status not in {200, 201}:
                raise SystemExit(f"Unexpected GitHub response: {response.status}")
    except urllib.error.HTTPError as err:
        print(err.read().decode("utf-8", errors="replace"), file=sys.stderr)
        raise SystemExit(err.code)


def create_release_with_gh(tag, title, notes, draft=False, prerelease=False):
    args = ["gh", "release", "create", tag, "--title", title, "--notes", notes]
    if draft:
        args.append("--draft")
    if prerelease:
        args.append("--prerelease")
    run(args)


def build_notes(tag):
    previous = run(["git", "describe", "--tags", "--abbrev=0", f"{tag}^"], check=False, capture=True)
    if previous.returncode == 0 and previous.stdout.strip():
        previous_tag = previous.stdout.strip()
        log_range = f"{previous_tag}..{tag}"
        header = f"Changes since {previous_tag}:"
    else:
        log_range = tag
        header = "Changes:"
    commits = run(["git", "log", "--pretty=format:- %s", log_range], capture=True).stdout.strip()
    if not commits:
        commits = "- Release update"
    return f"{header}\n\n{commits}"


def main():
    parser = argparse.ArgumentParser(description="Publish the current MCMI Tools version to GitHub Releases.")
    parser.add_argument("--dry-run", action="store_true", help="Print what would happen without pushing or creating a release.")
    parser.add_argument("--allow-dirty", action="store_true", help="Allow publishing with uncommitted changes.")
    parser.add_argument("--notes-file", type=Path, help="Use this markdown file as release notes.")
    parser.add_argument("--draft", action="store_true", help="Create a draft release.")
    parser.add_argument("--prerelease", action="store_true", help="Mark the release as a prerelease.")
    args = parser.parse_args()

    version = read_version()
    tag = tag_from_version(version)
    title = f"MCMI Tools {tag}"

    if current_branch() != "main":
        raise SystemExit("Release must be published from the main branch")
    if has_dirty_worktree() and not args.allow_dirty:
        raise SystemExit("Working tree is dirty. Commit changes first or pass --allow-dirty.")
    if tag_exists(tag):
        raise SystemExit(f"Local tag already exists: {tag}")
    if remote_tag_exists(tag):
        raise SystemExit(f"Remote tag already exists: {tag}")

    notes = args.notes_file.read_text(encoding="utf-8") if args.notes_file else None

    if args.dry_run:
        print(f"Version: {version}")
        print(f"Tag: {tag}")
        print("Would run: git push origin main")
        print(f"Would run: git tag -a {tag} -m {title!r}")
        print(f"Would run: git push origin {tag}")
        print(f"Would create GitHub Release: {title}")
        return

    run(["git", "push", "origin", "main"])
    run(["git", "tag", "-a", tag, "-m", title])
    run(["git", "push", "origin", tag])

    notes = notes if notes is not None else build_notes(tag)
    token = os.environ.get("GITHUB_TOKEN", "")
    if release_exists(tag, token):
        print(f"GitHub Release already exists for {tag}")
        return

    if shutil.which("gh"):
        create_release_with_gh(tag, title, notes, draft=args.draft, prerelease=args.prerelease)
    else:
        create_release_with_api(tag, title, notes, token, draft=args.draft, prerelease=args.prerelease)

    print(f"Published {tag}")


if __name__ == "__main__":
    main()
