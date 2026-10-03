"""The API application, built for a given run profile."""

from __future__ import annotations

from importlib.metadata import version

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from fra_core.profile import Profile

HEALTH_PATH = "/health"


def create_app(profile: Profile) -> FastAPI:
    """The API for one run profile. Job queue, upload and results arrive in week 4."""
    package_version = version("fra-api")
    # The generated docs and schema are off until the real API arrives in week 4.
    app = FastAPI(
        title="Financial statement analysis",
        version=package_version,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get(HEALTH_PATH)
    def health() -> dict[str, str]:
        return {"status": "ok", "version": package_version, "profile": profile.value}

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (
            "<!doctype html><html lang=en><meta charset=utf-8>"
            "<title>Financial statement analysis</title>"
            f"<h1>Financial statement analysis</h1><p>Running under the {profile.value} profile. "
            f'Health: <a href="{HEALTH_PATH}">{HEALTH_PATH}</a></p></html>'
        )

    return app
