from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "scripts" / "ci" / "tesseract-packages.sh"
DOCKERFILE = Path(__file__).parents[2] / "docker" / "Dockerfile"


def run_script(*arguments: Path, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *map(str, arguments)],
        cwd=cwd,
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


def test_stops_at_the_end_of_the_command_not_the_line(tmp_path: Path) -> None:
    dockerfile = write_dockerfile(
        tmp_path,
        "RUN apt-get install -y tesseract-ocr && rm -rf /var/lib/apt/lists/*\n",
    )

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
