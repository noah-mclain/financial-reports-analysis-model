from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "scripts" / "ci" / "tesseract-packages.sh"
DOCKERFILE = Path(__file__).parents[2] / "docker" / "Dockerfile"


def run_script(
    *arguments: Path,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *map(str, arguments)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def write_dockerfile(tmp_path: Path, body: str) -> Path:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(body)
    return dockerfile


def test_lists_the_packages_the_real_dockerfile_installs(tmp_path: Path) -> None:
    # Run from another directory: the default Dockerfile is found from the script's location.
    result = run_script(cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    packages = result.stdout.split()
    assert packages == ["tesseract-ocr", "tesseract-ocr-ara", "tesseract-ocr-eng"]
    for package in packages:
        assert f" {package}" in DOCKERFILE.read_text()


def test_follows_a_changed_package_list(tmp_path: Path) -> None:
    dockerfile = write_dockerfile(
        tmp_path,
        "FROM scratch\n"
        "RUN apt-get update \\\n"
        "    && apt-get install --no-install-recommends -y tesseract-ocr tesseract-ocr-fra \\\n"
        "    && rm -rf /var/lib/apt/lists/*\n",
    )

    result = run_script(dockerfile)

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["tesseract-ocr", "tesseract-ocr-fra"]


def test_preserves_tokens_split_across_a_continuation(tmp_path: Path) -> None:
    dockerfile = write_dockerfile(
        tmp_path,
        "RUN apt-get install -y tesseract-\\\nocr\n",
    )

    result = run_script(dockerfile)

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["tesseract-ocr"]


def test_reads_packages_on_separate_continued_lines(tmp_path: Path) -> None:
    dockerfile = write_dockerfile(
        tmp_path,
        "RUN apt-get install -y \\\n    tesseract-ocr \\\n    tesseract-ocr-ara\n",
    )

    result = run_script(dockerfile)

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["tesseract-ocr", "tesseract-ocr-ara"]


def test_propagates_parser_tool_failure(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    awk = bin_dir / "awk"
    awk.write_text("#!/bin/sh\necho parser-failed >&2\nexit 37\n")
    awk.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    dockerfile = write_dockerfile(tmp_path, "RUN apt-get install -y tesseract-ocr\n")

    result = run_script(dockerfile, env=env)

    assert result.returncode == 37
    assert "parser-failed" in result.stderr
    assert "no 'apt-get install' command" not in result.stderr


def test_stops_at_the_end_of_the_command_not_the_line(tmp_path: Path) -> None:
    dockerfile = write_dockerfile(
        tmp_path,
        "RUN apt-get install -y tesseract-ocr && rm -rf /var/lib/apt/lists/*\n",
    )

    result = run_script(dockerfile)

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["tesseract-ocr"]


def test_stops_at_semicolon_and_pipe_boundaries(tmp_path: Path) -> None:
    for boundary in (";", "|"):
        dockerfile = write_dockerfile(
            tmp_path,
            f"RUN apt-get install -y tesseract-ocr {boundary} echo unrelated\n",
        )

        result = run_script(dockerfile)

        assert result.returncode == 0, result.stderr
        assert result.stdout.split() == ["tesseract-ocr"]


def test_reads_a_single_line_without_a_final_newline(tmp_path: Path) -> None:
    dockerfile = write_dockerfile(tmp_path, "RUN apt-get install -y tesseract-ocr")

    result = run_script(dockerfile)

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["tesseract-ocr"]


def test_fails_loudly_when_the_dockerfile_has_no_apt_line(tmp_path: Path) -> None:
    dockerfile = write_dockerfile(tmp_path, "FROM scratch\nRUN echo hello\n")

    result = run_script(dockerfile)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "apt-get install" in result.stderr


def test_fails_when_the_apt_line_names_no_package(tmp_path: Path) -> None:
    dockerfile = write_dockerfile(tmp_path, "RUN apt-get install -y && true\n")

    result = run_script(dockerfile)

    assert result.returncode != 0
    assert result.stdout == ""


def test_fails_on_a_package_name_that_is_not_literal(tmp_path: Path) -> None:
    dockerfile = write_dockerfile(tmp_path, "RUN apt-get install -y tesseract-ocr $EXTRA\n")

    result = run_script(dockerfile)

    assert result.returncode != 0
    assert "$EXTRA" in result.stderr


def test_fails_when_the_dockerfile_is_missing(tmp_path: Path) -> None:
    result = run_script(tmp_path / "absent")

    assert result.returncode != 0
    assert "absent" in result.stderr
