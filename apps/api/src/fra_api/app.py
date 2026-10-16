"""The API application, built for a given run profile."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import version
from threading import BoundedSemaphore

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from fra_api.jobs import add_job_routes
from fra_core.jobs import JobSettings, JobStore
from fra_core.profile import Profile

HEALTH_PATH = "/health"


def create_app(profile: Profile, settings: JobSettings) -> FastAPI:
    """The API for one run profile. The job store opens when the server starts, so a bad
    storage setting stops the start, and building the app alone writes nothing."""
    package_version = version("fra-api")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.store = JobStore(settings.root)
        yield

    # The generated docs and schema stay off until the results page settles the API.
    app = FastAPI(
        title="Financial statement analysis",
        version=package_version,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.upload_slots = BoundedSemaphore(settings.max_concurrent_uploads)
    add_job_routes(app)

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
