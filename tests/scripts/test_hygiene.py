from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "ci" / "hygiene.py"
OWNER = ("Owner Person", "owner@example.com")
LOGIN = "owner-login"
NOREPLY_AUTHOR = ("Owner Person", f"123+{LOGIN}@users.noreply.github.com")
WEB_COMMITTER = ("GitHub", "noreply@github.com")
STRANGER = ("Someone Else", "someone@example.org")
ZEROS = "0" * 40


def load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("hygiene_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# Every forbidden word comes from the checker's own constants, so no test spells one.
checker = load_checker()
NAME = checker.ASSISTANT_NAMES[0]
OTHER_NAMES = checker.ASSISTANT_NAMES[1:]
PREFIXES = checker.FORBIDDEN_BRANCH_PREFIXES
EXCLUDED_PATHS = checker.DIFF_SCAN_EXCLUDED_PATHS
GENERATED = checker.GENERATED_MARKER.capitalize()
FOOTER_LINK = "[a tool](https://example.org/tool)"
ALLOWED_SUFFIXES = checker.NAME_ALLOWED_SUFFIXES
SESSION_LINK = f"https://{checker.SESSION_LINK_HOST}/code/session_01AbCdEfGhIjKlMn"


def git(repository: Path, *arguments: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )
    return result.stdout.strip()


Commit = Callable[..., str]


def make_repository(path: Path, root_author: tuple[str, str] = OWNER) -> Path:
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "commit.gpgsign", "false")
    commit_in(path, root_author, root_author, "Initial commit", {"README.txt": "start\n"})
    return path


def commit_in(
    repository: Path,
    author: tuple[str, str],
    committer: tuple[str, str],
    message: str,
    files: dict[str, str] | None = None,
) -> str:
    for name, content in (files or {}).items():
        target = repository / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    git(repository, "add", "-A")
    git(
        repository,
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        message,
        env={
            "GIT_AUTHOR_NAME": author[0],
            "GIT_AUTHOR_EMAIL": author[1],
            "GIT_COMMITTER_NAME": committer[0],
            "GIT_COMMITTER_EMAIL": committer[1],
        },
    )
    return git(repository, "rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    return make_repository(tmp_path / "repo")


@pytest.fixture
def commit(repository: Path) -> Commit:
    def make(
        message: str = "Add a thing",
        files: dict[str, str] | None = None,
        author: tuple[str, str] = OWNER,
        committer: tuple[str, str] | None = None,
    ) -> str:
        return commit_in(repository, author, committer or author, message, files)

    return make


def run_check(
    repository: Path,
    base: str,
    *extra: str,
    head: str = "HEAD",
    login: str | None = LOGIN,
) -> subprocess.CompletedProcess[str]:
    arguments = ["python3", str(SCRIPT), "--range", f"{base}..{head}"]
    if login is not None:
        arguments += ["--owner-login", login]
    return subprocess.run(
        [*arguments, *extra], cwd=repository, capture_output=True, text=True, check=False
    )


def test_owner_commit_passes(repository: Path, commit: Commit) -> None:
    base = git(repository, "rev-parse", "HEAD")
    commit("Add a thing", {"thing.txt": "text\n"})

    result = run_check(repository, base, "--branch", "ci-coverage")

    assert result.returncode == 0, result.stderr


def test_github_web_merge_with_noreply_identities_passes(repository: Path, commit: Commit) -> None:
    base = git(repository, "rev-parse", "HEAD")
    commit(
        "Merge pull request #1 from owner-login/topic",
        author=NOREPLY_AUTHOR,
        committer=WEB_COMMITTER,
    )

    result = run_check(repository, base)

    assert result.returncode == 0, result.stderr


def test_noreply_address_of_another_login_fails(repository: Path, commit: Commit) -> None:
    base = git(repository, "rev-parse", "HEAD")
    commit(author=("Owner Person", "123+another-login@users.noreply.github.com"))

    result = run_check(repository, base)

    assert result.returncode == 1
    assert "author" in result.stderr


def test_foreign_author_fails(repository: Path, commit: Commit) -> None:
    base = git(repository, "rev-parse", "HEAD")
    commit(author=STRANGER, committer=OWNER)

    result = run_check(repository, base)

    assert result.returncode == 1
    assert STRANGER[1] in result.stderr


def test_foreign_committer_fails(repository: Path, commit: Commit) -> None:
    base = git(repository, "rev-parse", "HEAD")
    commit(author=OWNER, committer=STRANGER)

    result = run_check(repository, base)

    assert result.returncode == 1
    assert "committer" in result.stderr


def test_the_web_committer_may_not_be_the_author(repository: Path, commit: Commit) -> None:
    base = git(repository, "rev-parse", "HEAD")
    commit(author=WEB_COMMITTER, committer=WEB_COMMITTER)

    result = run_check(repository, base)

    assert result.returncode == 1


def test_the_owner_is_the_author_of_the_root_commit(tmp_path: Path) -> None:
    repository = make_repository(tmp_path / "other", root_author=STRANGER)
    base = git(repository, "rev-parse", "HEAD")
    commit_in(repository, OWNER, OWNER, "Second commit")

    result = run_check(repository, base)

    assert result.returncode == 1
    assert OWNER[1] in result.stderr


def test_trailer_fails(repository: Path, commit: Commit) -> None:
    base = git(repository, "rev-parse", "HEAD")
    commit("Add a thing\n\nCo-Authored-By: Some Person <person@example.org>")

    result = run_check(repository, base)

    assert result.returncode == 1
    assert "trailer" in result.stderr


@pytest.mark.parametrize("name", checker.ASSISTANT_NAMES)
def test_assistant_name_in_a_message_fails(repository: Path, commit: Commit, name: str) -> None:
    base = git(repository, "rev-parse", "HEAD")
    commit(f"Ask {name.upper()} to tidy this")

    result = run_check(repository, base)

    assert result.returncode == 1
    assert "message" in result.stderr


def test_a_name_inside_a_longer_word_is_not_a_match(repository: Path, commit: Commit) -> None:
    base = git(repository, "rev-parse", "HEAD")
    commit(f"Handle {NAME}ing and un{NAME}")

    result = run_check(repository, base)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("suffix", ALLOWED_SUFFIXES)
def test_a_name_used_as_a_file_or_protocol_reference_is_allowed(
    repository: Path, commit: Commit, suffix: str
) -> None:
    # The project file and the compatible-API wording are references, not attribution.
    base = git(repository, "rev-parse", "HEAD")
    text = f"see {NAME.upper()}{suffix} for the rules"
    commit(text, {"docs/notes.md": text + "\n"})

    result = run_check(repository, base)

    assert result.returncode == 0, result.stderr


def test_an_allowed_suffix_does_not_hide_a_longer_word(repository: Path, commit: Commit) -> None:
    base = git(repository, "rev-parse", "HEAD")
    commit(f"{NAME}{ALLOWED_SUFFIXES[0]}x was here")

    result = run_check(repository, base)

    assert result.returncode == 1


def test_generated_with_line_fails(repository: Path, commit: Commit) -> None:
    base = git(repository, "rev-parse", "HEAD")
    commit(f"Add a thing\n\n{GENERATED} {FOOTER_LINK}")

    result = run_check(repository, base)

    assert result.returncode == 1


def test_session_link_in_a_message_fails(repository: Path, commit: Commit) -> None:
    base = git(repository, "rev-parse", "HEAD")
    commit(f"Add a thing\n\n{SESSION_LINK}")

    result = run_check(repository, base)

    assert result.returncode == 1


def test_the_forbidden_prefix_list_keeps_every_prefix_the_rules_name() -> None:
    # The cases below are built from the list, so shrinking the list would also shrink them.
    # Eight prefixes: five that name no assistant (checked here) and three that do.
    assert len(PREFIXES) == 8
    assert {"ai/", "bot/", "cursor/", "devin/", "dependabot/"} <= set(PREFIXES)


@pytest.mark.parametrize("prefix", PREFIXES)
def test_each_forbidden_branch_prefix_fails(repository: Path, prefix: str) -> None:
    base = git(repository, "rev-parse", "HEAD")

    result = run_check(repository, base, "--branch", f"{prefix}fix-something")

    assert result.returncode == 1
    assert "branch" in result.stderr


def test_branch_prefix_match_ignores_case(repository: Path) -> None:
    base = git(repository, "rev-parse", "HEAD")

    result = run_check(repository, base, "--branch", f"{PREFIXES[0].upper()}topic")

    assert result.returncode == 1


def test_session_id_in_a_branch_name_fails(repository: Path) -> None:
    base = git(repository, "rev-parse", "HEAD")

    result = run_check(repository, base, "--branch", "topic-session_01AbCdEfGhIj")

    assert result.returncode == 1


@pytest.mark.parametrize("branch", ["ci-coverage", "ingest-locator", "ai-metrics", "bots"])
def test_neutral_branch_passes(repository: Path, branch: str) -> None:
    base = git(repository, "rev-parse", "HEAD")

    result = run_check(repository, base, "--branch", branch)

    assert result.returncode == 0, result.stderr


def test_added_line_naming_an_assistant_fails_and_names_the_file(
    repository: Path, commit: Commit
) -> None:
    base = git(repository, "rev-parse", "HEAD")
    commit("Add notes", {"docs/notes.md": f"line one\nwritten with {NAME} help\n"})

    result = run_check(repository, base)

    assert result.returncode == 1
    assert "docs/notes.md" in result.stderr


@pytest.mark.parametrize(
    "text", [f"{GENERATED} {FOOTER_LINK}", f"\U0001f916 {GENERATED} {FOOTER_LINK}", SESSION_LINK]
)
def test_added_generated_line_or_session_link_fails(
    repository: Path, commit: Commit, text: str
) -> None:
    base = git(repository, "rev-parse", "HEAD")
    commit("Add notes", {"docs/notes.md": text + "\n"})

    result = run_check(repository, base)

    assert result.returncode == 1


@pytest.mark.parametrize("path", EXCLUDED_PATHS)
def test_added_line_in_an_excluded_file_is_ignored(
    repository: Path, commit: Commit, path: str
) -> None:
    base = git(repository, "rev-parse", "HEAD")
    commit("Edit the rules", {path: f"{NAME} {GENERATED} {SESSION_LINK}\n"})

    result = run_check(repository, base)

    assert result.returncode == 0, result.stderr


def test_a_file_with_the_same_name_in_a_subdirectory_is_not_excluded(
    repository: Path, commit: Commit
) -> None:
    base = git(repository, "rev-parse", "HEAD")
    commit("Add notes", {f"sub/{EXCLUDED_PATHS[0]}": f"{NAME}\n"})

    result = run_check(repository, base)

    assert result.returncode == 1


def test_only_added_lines_are_scanned(repository: Path, commit: Commit) -> None:
    commit("Add notes", {"docs/notes.md": f"{NAME} was here\nkeep\n"})
    base = git(repository, "rev-parse", "HEAD")
    commit("Drop the line", {"docs/notes.md": "keep\nadded plain line\n"})

    result = run_check(repository, base)

    assert result.returncode == 0, result.stderr


def test_the_generic_word_ai_is_not_flagged(repository: Path, commit: Commit) -> None:
    base = git(repository, "rev-parse", "HEAD")
    commit("Add AI notes", {"docs/notes.md": "an AI model reads the text\n"})

    result = run_check(repository, base)

    assert result.returncode == 0, result.stderr


def test_generated_with_inside_a_sentence_is_not_a_footer(repository: Path, commit: Commit) -> None:
    base = git(repository, "rev-parse", "HEAD")
    text = f"the fixture was {checker.GENERATED_MARKER} pdflatex, see https://example.org"
    commit(text, {"docs/notes.md": text + "\n"})

    result = run_check(repository, base)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "text",
    [
        f"[{checker.GENERATED_MARKER} a tool](https://example.org)",
        f"**{GENERATED}** {FOOTER_LINK}",
    ],
)
def test_footer_forms_with_a_bracket_or_markdown_prefix_fail(
    repository: Path, commit: Commit, text: str
) -> None:
    base = git(repository, "rev-parse", "HEAD")
    commit(f"Add a thing\n\n{text}")

    result = run_check(repository, base)

    assert result.returncode == 1


@pytest.mark.parametrize("joined", ["{upper}_API_KEY", "{lower}_code", "{title}Code", "x-{lower}"])
def test_names_joined_by_underscore_or_camel_case_fail(
    repository: Path, commit: Commit, joined: str
) -> None:
    base = git(repository, "rev-parse", "HEAD")
    token = joined.format(upper=NAME.upper(), lower=NAME, title=NAME.capitalize())
    commit("Add a thing", {"docs/notes.md": f"setting = {token}\n"})

    result = run_check(repository, base)

    assert result.returncode == 1, token


def mixed_case_spellings(name: str) -> list[str]:
    humped = name[:4].capitalize() + name[4:].upper()
    return [name.capitalize(), humped, f"use{humped}Client", f"use{name.capitalize()}Client"]


@pytest.mark.parametrize(
    "token",
    [spelling for name in checker.ASSISTANT_NAMES for spelling in mixed_case_spellings(name)],
)
def test_mixed_case_spellings_of_a_name_fail(repository: Path, commit: Commit, token: str) -> None:
    base = git(repository, "rev-parse", "HEAD")
    commit("Add a thing", {"docs/notes.md": f"client = {token}()\n"})

    result = run_check(repository, base)

    assert result.returncode == 1, token


def test_a_back_dated_orphan_root_in_the_range_does_not_become_the_owner(
    repository: Path, commit: Commit
) -> None:
    base = git(repository, "rev-parse", "HEAD")
    git(repository, "checkout", "-q", "-b", "feature")
    commit("Feature work")
    git(repository, "checkout", "-q", "--orphan", "planted")
    git(repository, "rm", "-rqf", ".")
    long_ago = {
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00",
    }
    for key, value in long_ago.items():
        os.environ[key] = value
    try:
        commit_in(repository, STRANGER, STRANGER, "Planted root", {"planted.txt": "x\n"})
    finally:
        for key in long_ago:
            del os.environ[key]
    git(repository, "checkout", "-q", "feature")
    git(
        repository,
        "merge",
        "-q",
        "--allow-unrelated-histories",
        "--no-ff",
        "-m",
        "Merge planted",
        "planted",
        env={
            "GIT_AUTHOR_NAME": STRANGER[0],
            "GIT_AUTHOR_EMAIL": STRANGER[1],
            "GIT_COMMITTER_NAME": STRANGER[0],
            "GIT_COMMITTER_EMAIL": STRANGER[1],
        },
    )

    result = run_check(repository, base)

    assert result.returncode == 1, result.stdout
    assert STRANGER[1] in result.stderr


def test_all_findings_are_reported_together(repository: Path, commit: Commit) -> None:
    base = git(repository, "rev-parse", "HEAD")
    commit(f"Ask {NAME}", {"docs/notes.md": f"{NAME}\n"}, author=STRANGER)

    result = run_check(repository, base, "--branch", f"{PREFIXES[0]}topic")

    assert result.returncode == 1
    for word in ("author", "message", "docs/notes.md", "branch"):
        assert word in result.stderr


def test_shallow_history_is_an_error_not_a_pass(
    repository: Path, commit: Commit, tmp_path: Path
) -> None:
    commit("Second commit")
    commit("Third commit")
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{repository}", str(shallow)], check=True
    )

    result = run_check(shallow, "HEAD~0", head="HEAD")

    assert result.returncode == 2
    assert "shallow" in result.stderr


def test_all_zero_base_is_an_error(repository: Path, commit: Commit) -> None:
    commit("Second commit")

    result = run_check(repository, ZEROS)

    assert result.returncode == 2
    assert ZEROS in result.stderr


def test_base_that_is_not_an_ancestor_of_head_is_an_error(repository: Path, commit: Commit) -> None:
    git(repository, "checkout", "-q", "-b", "other")
    elsewhere = commit("On another branch")
    git(repository, "checkout", "-q", "main")
    commit("On main")

    result = run_check(repository, elsewhere)

    assert result.returncode == 2
    assert "ancestor" in result.stderr


def test_unknown_base_is_an_error(repository: Path) -> None:
    result = run_check(repository, "f" * 40)

    assert result.returncode == 2


def test_the_owner_login_is_required(repository: Path) -> None:
    base = git(repository, "rev-parse", "HEAD")

    result = run_check(repository, base, login=None)

    assert result.returncode == 2
    assert "--owner-login" in result.stderr


def test_an_empty_branch_name_is_an_error(repository: Path) -> None:
    base = git(repository, "rev-parse", "HEAD")

    result = run_check(repository, base, "--branch", "")

    assert result.returncode == 2
    assert "--branch" in result.stderr


def test_an_empty_range_passes(repository: Path) -> None:
    head = git(repository, "rev-parse", "HEAD")

    result = run_check(repository, head)

    assert result.returncode == 0, result.stderr
