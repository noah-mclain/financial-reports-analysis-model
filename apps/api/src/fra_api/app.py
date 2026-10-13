"""The API application, built for a given run profile."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import version

from fastapi import FastAPI

from fra_api.jobs import add_job_routes
from fra_api.pages import add_page_routes
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
    add_job_routes(app)
    add_page_routes(app, profile)

    @app.get(HEALTH_PATH)
    def health() -> dict[str, str]:
        return {"status": "ok", "version": package_version, "profile": profile.value}

    return app
