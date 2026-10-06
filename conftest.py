"""Repository-wide test guards and the golden structure fixture.

Tests never write to the real ``var/artifacts``. A session guard fails the run when anything
under it changed, and ``golden_structure`` runs the structure stage over a copy of one
document's cache, so a stale cache is a named skip rather than a deletion or a conversion.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from cache_isolation import (
    REAL_ARTIFACTS,
    GoldenStructure,
    changed_paths,
    fingerprint,
    structure_isolated,
)

from fra_ingest.config import load_config

GOLDEN_DIR = Path(__file__).resolve().parent / "eval" / "golden" / "documents"

_BEFORE: dict[str, tuple[int, int, str]] = {}


def pytest_sessionstart(session: pytest.Session) -> None:
    _BEFORE.update(fingerprint(REAL_ARTIFACTS))


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    changes = changed_paths(_BEFORE, fingerprint(REAL_ARTIFACTS))
    if not changes:
        return
    writer = session.config.get_terminal_writer()
    writer.line(
        f"\nFAILED: the test run changed the real cache {REAL_ARTIFACTS} "
        "(exit status set to 1, whatever the summary above says):",
        red=True,
    )
    for line in changes:
        writer.line(f"  {line}", red=True)
    # An interrupted run or one that collected no tests keeps its own status.
    if exitstatus in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture(scope="session")
def golden_structure(
    tmp_path_factory: pytest.TempPathFactory,
) -> Callable[[str], GoldenStructure]:
    """``golden_structure(name)``: the structure stage over a golden document's cache, run in a
    temporary copy. Computed once per document and session, a skip repeated."""
    done: dict[str, GoldenStructure | str] = {}

    def run(name: str) -> GoldenStructure:
        pdf = GOLDEN_DIR / name
        if not pdf.is_file():
            pytest.skip(f"golden document {name} is not present")
        if name not in done:
            try:
                done[name] = structure_isolated(
                    pdf, load_config(), tmp_path_factory.mktemp(Path(name).stem)
                )
            except pytest.skip.Exception as skipped:
                done[name] = str(skipped)
        outcome = done[name]
        if isinstance(outcome, str):
            pytest.skip(outcome)
        return outcome

    return run
