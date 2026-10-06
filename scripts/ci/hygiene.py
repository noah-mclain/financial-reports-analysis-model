"""Check that commits, branch names and added lines follow CLAUDE.md's attribution rules.

    hygiene.py --range BASE..HEAD --owner-login LOGIN [--branch NAME]

Exit 0 when clean, 1 when a rule is broken (every finding is printed to stderr), 2 when the
check cannot be trusted: shallow history, a range base that is not an ancestor of its head,
a missing argument. A check that cannot run is never a pass.

The owner's email is the author of the oldest root commit. Authors must be the owner or the
owner's GitHub noreply address; committers may also be GitHub itself (web merges).
Standard library only, like release.py.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass

# Assistant names, matched ignoring case and not inside a longer run of letters. The one place they are listed.
ASSISTANT_NAMES = ("claude", "anthropic", "chatgpt", "openai", "copilot", "gemini", "codex")
# A name followed by one of these is a reference, not attribution: the project's own CLAUDE.md,
# an "OpenAI-compatible" endpoint.
NAME_ALLOWED_SUFFIXES = (".md", "-compatible")
GENERATED_MARKER = "generated with"
SESSION_LINK_HOST = "claude.ai"
FORBIDDEN_BRANCH_PREFIXES = (
    "claude/",
    "ai/",
    "copilot/",
    "bot/",
    "codex/",
    "cursor/",
    "devin/",
    "dependabot/",
)
# Files that must name assistants to state or enforce the rules.
DIFF_SCAN_EXCLUDED_PATHS = ("CLAUDE.md", "scripts/ci/hygiene.py", "tests/scripts/test_hygiene.py")
GITHUB_WEB_COMMITTER = "noreply@github.com"

# A session id carries a digit: `session_01AbCdEfGhIj`, not `session_management`.
SESSION_ID = r"session_(?=[A-Za-z]*\d)[0-9A-Za-z]{10,}"
# Letter boundaries, not \b: an underscore is a word character, so OPENAI_API_KEY and
# claude_code must still match. CamelCase is split first (see split_camel_case).
NAME_PATTERN = re.compile(
    r"(?<![A-Za-z])(?:" + "|".join(ASSISTANT_NAMES) + r")(?![A-Za-z])"
    r"(?!(?:" + "|".join(map(re.escape, NAME_ALLOWED_SUFFIXES)) + r")\b)",
    re.IGNORECASE,
)
CAMEL_CASE_JOINT = re.compile(r"(?<=[a-z])(?=[A-Z])")
# The tool footer, not the phrase in prose: at the start of a line, after at most a few
# non-word characters (an emoji, "[", "**"), and followed on that line by a link.
GENERATED_PATTERN = re.compile(
    r"^[^\w\n]{0,6}" + re.escape(GENERATED_MARKER) + r"\b[^\n]*(?:\]\(|https?://)",
    re.IGNORECASE | re.MULTILINE,
)
SESSION_LINK_PATTERN = re.compile(
    r"https?://\S*" + re.escape(SESSION_LINK_HOST) + r"\S*|" + SESSION_ID, re.IGNORECASE
)
TRAILER_PATTERN = re.compile(r"^\s*co-authored-by\s*:", re.IGNORECASE | re.MULTILINE)
BRANCH_SESSION_PATTERN = re.compile(SESSION_ID, re.IGNORECASE)
ALL_ZERO_SHA = re.compile(r"0+")
HUNK_HEADER = re.compile(r"^@@ -\S+ \+(\d+)")


class CannotCheck(Exception):
    """The check cannot give a trustworthy answer; the caller exits 2."""


@dataclass(frozen=True)
class Commit:
    sha: str
    author_email: str
    committer_email: str
    message: str


def git(*arguments: str) -> str:
    result = subprocess.run(["git", *arguments], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise CannotCheck(f"git {' '.join(arguments)} failed: {result.stderr.strip()}")
    return result.stdout


def require_full_history() -> None:
    if git("rev-parse", "--is-shallow-repository").strip() == "true":
        raise CannotCheck(
            "the repository is shallow, so the root commit and the range cannot be trusted; "
            "fetch full history (actions/checkout with fetch-depth: 0)"
        )


def resolve_range(range_text: str) -> tuple[str, str]:
    base, separator, head = range_text.partition("..")
    if not separator or not base or not head or head.startswith("."):
        raise CannotCheck(f"--range must look like BASE..HEAD, got {range_text!r}")
    if ALL_ZERO_SHA.fullmatch(base):
        raise CannotCheck(f"range base {base} is all zeros: there is no earlier commit to compare")
    base_sha = git("rev-parse", "--verify", f"{base}^{{commit}}").strip()
    head_sha = git("rev-parse", "--verify", f"{head}^{{commit}}").strip()
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base_sha, head_sha], capture_output=True, check=False
    )
    if ancestor.returncode == 1:
        raise CannotCheck(
            f"range base {base} is not an ancestor of {head}: the history was rewritten or the "
            "base is on another line, so the range would not be the new commits"
        )
    if ancestor.returncode != 0:
        raise CannotCheck(f"git merge-base --is-ancestor failed for {range_text}")
    return base_sha, head_sha


def owner_email(base: str) -> str:
    """The author of the oldest root commit reachable from the base.

    The base side only: a range can bring in its own root commit (an orphan branch, back-dated),
    and that must not be able to name the owner.
    """
    roots = git("log", "--max-parents=0", "--format=%at %ae", base).splitlines()
    if not roots:
        raise CannotCheck("no root commit found")
    return min(roots, key=lambda row: int(row.split(" ", 1)[0])).split(" ", 1)[1]


def read_commits(base: str, head: str) -> list[Commit]:
    output = git("log", "--format=%x1e%H%x1f%ae%x1f%ce%x1f%B", f"{base}..{head}")
    commits = []
    for record in output.split("\x1e")[1:]:
        sha, author, committer, message = record.split("\x1f", 3)
        commits.append(Commit(sha, author, committer, message))
    return commits


def check_identity(commit: Commit, owner: str, login: str) -> list[str]:
    noreply = re.compile(rf"\d+\+{re.escape(login)}@users\.noreply\.github\.com", re.IGNORECASE)

    def is_owner(email: str) -> bool:
        return email.lower() == owner.lower() or noreply.fullmatch(email) is not None

    findings = []
    short = commit.sha[:10]
    if not is_owner(commit.author_email):
        findings.append(f"{short}: author {commit.author_email} is not the owner")
    if not is_owner(commit.committer_email) and commit.committer_email != GITHUB_WEB_COMMITTER:
        findings.append(f"{short}: committer {commit.committer_email} is not the owner")
    return findings


def forbidden_text(text: str) -> list[str]:
    """Which rules the text breaks, by description."""
    broken = []
    if NAME_PATTERN.search(CAMEL_CASE_JOINT.sub(" ", text)):
        broken.append("names an assistant")
    if GENERATED_PATTERN.search(text):
        broken.append(f"says {GENERATED_MARKER!r}")
    if SESSION_LINK_PATTERN.search(text):
        broken.append("carries a session link")
    return broken


def check_message(commit: Commit) -> list[str]:
    short = commit.sha[:10]
    findings = [f"{short}: message {reason}" for reason in forbidden_text(commit.message)]
    if TRAILER_PATTERN.search(commit.message):
        findings.append(f"{short}: message has a Co-Authored-By trailer")
    return findings


def check_branch(branch: str) -> list[str]:
    findings = []
    for prefix in FORBIDDEN_BRANCH_PREFIXES:
        if branch.lower().startswith(prefix):
            findings.append(f"branch {branch!r} starts with the forbidden prefix {prefix!r}")
    if BRANCH_SESSION_PATTERN.search(branch):
        findings.append(f"branch {branch!r} contains a session id")
    return findings


def check_added_lines(base: str, head: str) -> list[str]:
    diff = git(
        "-c", "core.quotepath=off", "diff", "--no-color", "--no-ext-diff", "--no-renames", "-U0",
        base, head,
    )  # fmt: skip
    findings: list[str] = []
    path = ""
    line_number = 0
    in_header = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            in_header = True
            path = ""
        elif in_header and line.startswith("+++ "):
            path = line[4:].removeprefix("b/") if line != "+++ /dev/null" else ""
        elif line.startswith("@@"):
            in_header = False
            hunk = HUNK_HEADER.match(line)
            line_number = int(hunk.group(1)) if hunk else 0
        elif not in_header and line.startswith("+"):
            if path and path not in DIFF_SCAN_EXCLUDED_PATHS:
                findings.extend(
                    f"{path}:{line_number}: added line {reason}"
                    for reason in forbidden_text(line[1:])
                )
            line_number += 1
    return findings


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else None)
    parser.add_argument("--range", required=True, dest="range_text", metavar="BASE..HEAD")
    parser.add_argument("--owner-login", required=True)
    parser.add_argument("--branch")
    try:
        arguments = parser.parse_args(argv)
    except SystemExit as exit_request:
        return 2 if exit_request.code else 0
    try:
        if arguments.branch is not None and not arguments.branch.strip():
            raise CannotCheck("--branch is empty: pass the branch name or leave the option out")
        require_full_history()
        base, head = resolve_range(arguments.range_text)
        owner = owner_email(base)
        commits = read_commits(base, head)
        findings: list[str] = []
        for commit in commits:
            findings += check_identity(commit, owner, arguments.owner_login)
            findings += check_message(commit)
        if arguments.branch is not None:
            findings += check_branch(arguments.branch)
        findings += check_added_lines(base, head)
    except CannotCheck as error:
        print(f"hygiene: cannot check: {error}", file=sys.stderr)
        return 2
    if findings:
        for finding in findings:
            print(f"hygiene: {finding}", file=sys.stderr)
        return 1
    print(f"hygiene: {len(commits)} commit(s) clean (owner {owner})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
