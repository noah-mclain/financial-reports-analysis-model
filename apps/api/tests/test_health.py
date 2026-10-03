"""The health page and the route table: JSON for machines, HTML for a person, per profile."""

from __future__ import annotations

from importlib.metadata import version

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from fra_api.app import HEALTH_PATH, create_app
from fra_core.profile import Profile


@pytest.mark.parametrize("profile", list(Profile))
def test_health_reports_status_version_and_profile(profile: Profile) -> None:
    response = TestClient(create_app(profile)).get(HEALTH_PATH)

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": version("fra-api"),
        "profile": profile.value,
    }


@pytest.mark.parametrize("profile", list(Profile))
def test_index_is_html_and_names_the_profile(profile: Profile) -> None:
    response = TestClient(create_app(profile)).get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert profile.value in response.text


def test_the_health_path_is_slash_health() -> None:
    assert HEALTH_PATH == "/health"


def test_the_app_serves_the_index_and_health_and_nothing_else() -> None:
    app = create_app(Profile.NATIVE)

    routes = [route for route in app.routes if isinstance(route, APIRoute)]
    assert len(routes) == len(app.routes)
    assert sorted(route.path for route in routes) == ["/", HEALTH_PATH]
