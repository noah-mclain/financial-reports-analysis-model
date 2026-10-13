"""The server-rendered pages: upload, a job's live timeline, the results, and the source viewer.

Page images are rendered from the stored upload with pdfium, never from ingest's artifacts, and
cached beside the upload. The source viewer draws a cell's box over its page: boxes are in PDF
points with a top-left origin (``fra_core.schemas.BBox``), placed as shares of the page size.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Annotated

import pypdfium2 as pdfium
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, PackageLoader, select_autoescape

from fra_api.jobs import settings_of, store_of
from fra_api.views import analysis_view
from fra_core.jobs import JobStore
from fra_core.profile import Profile
from fra_core.schemas import JobStage

# Page images at 2x (144 dpi): sharp enough to read a figure, small enough to load at once.
RENDER_SCALE = 2.0
STAGES = tuple(stage.value for stage in JobStage)

_templates = Environment(
    loader=PackageLoader("fra_api", "templates"), autoescape=select_autoescape(["html"])
)


def _page(name: str, **context: object) -> HTMLResponse:
    return HTMLResponse(_templates.get_template(name).render(**context))


def _pdf(store: JobStore, sha256: str) -> Path:
    if store.get_upload(sha256) is None:
        raise HTTPException(status_code=404, detail=f"no document {sha256}")
    return store.upload_path(sha256)


def page_size(pdf: Path, page_no: int) -> tuple[float, float]:
    """Width and height of a page in PDF points; 404 for a page the PDF does not have."""
    with pdfium.PdfDocument(pdf) as document:
        if not 1 <= page_no <= len(document):
            raise HTTPException(status_code=404, detail=f"no page {page_no}")
        width, height = document[page_no - 1].get_size()
    return float(width), float(height)


def render_page(pdf: Path, page_no: int, target: Path) -> None:
    page_size(pdf, page_no)  # the same 404 for a missing page
    with pdfium.PdfDocument(pdf) as document:
        image = document[page_no - 1].render(scale=RENDER_SCALE).to_pil()
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_bytes(buffer.getvalue())
    tmp.replace(target)


def parse_box(text: str) -> tuple[float, float, float, float]:
    try:
        left, top, right, bottom = (float(part) for part in text.split(","))
    except ValueError:
        raise HTTPException(status_code=400, detail="box is left,top,right,bottom") from None
    if right <= left or bottom <= top:
        raise HTTPException(status_code=400, detail="box is not ordered")
    return left, top, right, bottom


def add_page_routes(app: FastAPI, profile: Profile) -> None:
    @app.get("/", response_class=HTMLResponse)
    def upload_page(request: Request) -> HTMLResponse:
        settings = settings_of(request)
        return _page(
            "upload.html",
            profile=profile.value,
            max_mb=settings.max_upload_bytes // (1024 * 1024),
            max_pages=settings.max_pages,
        )

    @app.get("/jobs/{job_id}/view", response_class=HTMLResponse)
    def job_page(request: Request, job_id: str) -> HTMLResponse:
        store = store_of(request)
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"no job {job_id}")
        upload = store.get_upload(job.upload_sha256)
        return _page(
            "job.html",
            profile=profile.value,
            job=job,
            filename=upload.filename if upload else job.upload_sha256,
            stages=STAGES,
        )

    @app.get("/analyses/{job_id}/view", response_class=HTMLResponse, response_model=None)
    def analysis_page(request: Request, job_id: str) -> HTMLResponse | RedirectResponse:
        store = store_of(request)
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"no job {job_id}")
        analysis = store.load_analysis(job_id)
        if analysis is None:  # still running, or failed: the timeline says which
            return RedirectResponse(f"/jobs/{job_id}/view", status_code=303)
        upload = store.get_upload(job.upload_sha256)
        return _page(
            "analysis.html",
            profile=profile.value,
            job=job,
            filename=upload.filename if upload else job.upload_sha256,
            analysis=analysis,
            view=analysis_view(analysis),
        )

    @app.get("/documents/{sha256}/pages/{page_no}.png")
    def page_image(request: Request, sha256: str, page_no: int) -> Response:
        store = store_of(request)
        pdf = _pdf(store, sha256)
        target = store.root / "pages" / sha256 / f"{page_no}.png"
        if not target.is_file():
            render_page(pdf, page_no, target)
        return FileResponse(target, media_type="image/png")

    @app.get("/documents/{sha256}/pages/{page_no}", response_class=HTMLResponse)
    def source_page(
        request: Request,
        sha256: str,
        page_no: int,
        box: Annotated[str | None, Query()] = None,
    ) -> HTMLResponse:
        store = store_of(request)
        pdf = _pdf(store, sha256)
        width, height = page_size(pdf, page_no)
        overlay = None
        if box is not None:
            left, top, right, bottom = parse_box(box)
            overlay = {
                "left": 100 * left / width,
                "top": 100 * top / height,
                "width": 100 * (right - left) / width,
                "height": 100 * (bottom - top) / height,
            }
        upload = store.get_upload(sha256)
        return _page(
            "source.html",
            profile=profile.value,
            sha256=sha256,
            filename=upload.filename if upload else sha256,
            page_no=page_no,
            overlay=overlay,
        )
