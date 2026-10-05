"""Validate a manually requested image release against the reviewed main checkout."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

TAG_PATTERN = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")


def git(*arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or f"git {' '.join(arguments)} failed")
    return result.stdout.strip()


def validate_release(tag: str, ref: str, sha: str) -> str:
    if ref != "refs/heads/main":
        raise ValueError("release dispatch must target refs/heads/main")
    if not TAG_PATTERN.fullmatch(tag):
        raise ValueError("version must be a canonical vMAJOR.MINOR.PATCH tag")
    if not COMMIT_PATTERN.fullmatch(sha):
        raise ValueError("reviewed SHA must be a full lowercase Git commit hash")

    checkout_sha = git("rev-parse", "--verify", "HEAD^{commit}")
    if checkout_sha != sha:
        raise ValueError("checked out source does not match the reviewed workflow SHA")

    try:
        tag_sha = git("rev-parse", "--verify", "--end-of-options", f"refs/tags/{tag}^{{commit}}")
    except ValueError as error:
        raise ValueError(f"version tag {tag} does not resolve to a commit") from error
    if tag_sha != sha:
        raise ValueError("version tag must resolve to the reviewed main SHA")
    return checkout_sha


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--sha", required=True)
    arguments = parser.parse_args()
    try:
        print(validate_release(arguments.tag, arguments.ref, arguments.sha))
    except ValueError as error:
        print(f"release validation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
