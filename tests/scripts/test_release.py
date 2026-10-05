from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "ci" / "release.py"


@pytest.fixture
def release_repository(tmp_path: Path) -> tuple[Path, str]:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True
    )
    (tmp_path / "source.txt").write_text("reviewed source\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "source.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "source"], check=True)
    commit = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(tmp_path), "tag", "v1.2.3", commit], check=True)
    return tmp_path, commit


def check_release(
    repository: Path, tag: str, ref: str, sha: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--tag",
            tag,
            "--ref",
            ref,
            "--sha",
            sha,
        ],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )


def test_release_accepts_canonical_tag_at_reviewed_main_sha(
    release_repository: tuple[Path, str],
) -> None:
    repository, commit = release_repository

    result = check_release(repository, "v1.2.3", "refs/heads/main", commit)

    assert result.returncode == 0
    assert result.stdout.strip() == commit


@pytest.mark.parametrize("tag", ["1.2.3", "v01.2.3", "v1.2", "v1.2.3-rc.1", "v1.2.3;id"])
def test_release_rejects_noncanonical_or_injected_tag(
    release_repository: tuple[Path, str], tag: str
) -> None:
    repository, commit = release_repository

    result = check_release(repository, tag, "refs/heads/main", commit)

    assert result.returncode != 0


def test_release_rejects_non_main_dispatch(release_repository: tuple[Path, str]) -> None:
    repository, commit = release_repository

    result = check_release(repository, "v1.2.3", "refs/heads/release", commit)

    assert result.returncode != 0


def test_release_rejects_tag_on_different_commit(release_repository: tuple[Path, str]) -> None:
    repository, commit = release_repository
    (repository / "source.txt").write_text("different source\n")
    subprocess.run(["git", "-C", str(repository), "commit", "-qam", "different"], check=True)
    other_commit = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    result = check_release(repository, "v1.2.3", "refs/heads/main", other_commit)

    assert result.returncode != 0
    assert commit != other_commit


def test_release_rejects_sha_that_is_not_current_checkout(
    release_repository: tuple[Path, str],
) -> None:
    repository, commit = release_repository

    result = check_release(repository, "v1.2.3", "refs/heads/main", "0" * 40)

    assert result.returncode != 0
    assert commit != "0" * 40


def test_release_rejects_missing_tag(release_repository: tuple[Path, str]) -> None:
    repository, commit = release_repository
    result = check_release(repository, "v9.9.9", "refs/heads/main", commit)
    assert result.returncode == 1
    assert "does not resolve to a commit" in result.stderr


@pytest.mark.parametrize("sha", ["", "abc123", "g" * 40, "A" * 40, "0" * 41, "--help"])
def test_release_rejects_malformed_sha(release_repository: tuple[Path, str], sha: str) -> None:
    repository, _ = release_repository
    result = check_release(repository, "v1.2.3", "refs/heads/main", sha)
    assert result.returncode != 0


def test_release_accepts_annotated_tag(release_repository: tuple[Path, str]) -> None:
    repository, commit = release_repository
    subprocess.run(
        ["git", "-C", str(repository), "tag", "-a", "v2.0.0", "-m", "release", commit],
        check=True,
    )
    result = check_release(repository, "v2.0.0", "refs/heads/main", commit)
    assert result.returncode == 0
    assert result.stdout.strip() == commit
