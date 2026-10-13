"""Job settings for API tests: the store under a temporary directory, never var/jobs."""

from __future__ import annotations

from pathlib import Path

from fra_core.jobs import JobSettings


def settings_at(tmp_path: Path) -> JobSettings:
    return JobSettings(
        root=tmp_path / "jobs", max_upload_bytes=10_000, max_pages=400, poll_seconds=0.01
    )
