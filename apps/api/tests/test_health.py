"""The health page and the route table: JSON for machines, HTML for a person, per profile."""

from __future__ import annotations

from importlib.metadata import version
from pathlib import Path

import pytest
from api_support import settings_at
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from fra_api.app import HEALTH_PATH, create_app
from fra_core.profile import Profile


@pytest.mark.parametrize("profile", list(Profile))
def test_health_reports_status_version_and_profile(profile: Profile, tmp_path: Path) -> None:
    job_settings = settings_at(tmp_path)
    response = TestClient(create_app(profile, job_settings)).get(HEALTH_PATH)

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": version("fra-api"),
        "profile": profile.value,
    }


@pytest.mark.parametrize("profile", list(Profile))
def test_index_is_html_and_names_the_profile(profile: Profile, tmp_path: Path) -> None:
    job_settings = settings_at(tmp_path)
    response = TestClient(create_app(profile, job_settings)).get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert profile.value in response.text


def test_the_health_path_is_slash_health() -> None:
    assert HEALTH_PATH == "/health"


def test_the_app_serves_the_pages_health_and_the_job_routes_and_nothing_else(
    tmp_path: Path,
) -> None:
    job_settings = settings_at(tmp_path)
    app = create_app(Profile.NATIVE, job_settings)

    routes = [route for route in app.routes if isinstance(route, APIRoute)]
    assert len(routes) == len(app.routes)
    assert sorted((route.path, *sorted(route.methods or ())) for route in routes) == [
        ("/", "GET"),
        ("/analyses/{job_id}", "GET"),
        ("/analyses/{job_id}/view", "GET"),
        ("/documents", "POST"),
        ("/documents/{sha256}", "GET"),
        ("/documents/{sha256}/analyses", "POST"),
        ("/documents/{sha256}/pages/{page_no}", "GET"),
        ("/documents/{sha256}/pages/{page_no}.png", "GET"),
        (HEALTH_PATH, "GET"),
        ("/jobs/{job_id}", "GET"),
        ("/jobs/{job_id}/events", "GET"),
        ("/jobs/{job_id}/view", "GET"),
    ]
