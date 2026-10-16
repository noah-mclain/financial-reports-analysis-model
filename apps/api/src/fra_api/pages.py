"""The server-rendered pages: upload, a job's live timeline, the results, and the source viewer.

Page images are rendered from the stored upload with pdfium, never from ingest's artifacts, and
cached beside the upload. The source viewer draws a cell's box over its page: boxes are in PDF
points with a top-left origin (``fra_core.schemas.BBox``), placed as shares of the page size.
"""

from __future__ import annotations

import io
import math
import uuid
from contextlib import closing
from pathlib import Path
from threading import RLock
from typing import Annotated

import pypdfium2 as pdfium
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, PackageLoader, select_autoescape

from fra_api.jobs import settings_of, store_of
from fra_api.views import analysis_view
from fra_core.jobs import JobSettings, JobStore
from fra_core.profile import Profile
from fra_core.schemas import JobStage

# Page images at 2x (144 dpi): sharp enough to read a figure, small enough to load at once.
RENDER_SCALE = 2.0
# A page's longest side in pixels at most: a PDF may declare any page size, and the bitmap is
# allocated in full before it is encoded. An A3 page at 2x is 2,384 pixels long.
MAX_RENDER_SIDE = 4000
STAGES = tuple(stage.value for stage in JobStage)
# PDFium is process-global and not thread-safe, even for different documents. Keep native
# construction, access and explicit destruction under the same lock in both sync routes.
_PDFIUM_LOCK = RLock()

_templates = Environment(
    loader=PackageLoader("fra_api", "templates"), autoescape=select_autoescape(["html"])
)


def _page(name: str, **context: object) -> HTMLResponse:
    return HTMLResponse(_templates.get_template(name).render(**context))


def _pdf(store: JobStore, sha256: str) -> Path:
    if store.get_upload(sha256) is None:
        raise HTTPException(status_code=404, detail=f"no document {sha256}")
    return store.upload_path(sha256)


def _within_page_limit(settings: JobSettings, page_no: int) -> None:
    """A page past the upload page limit is never rendered, even of a PDF the worker refused,
    so the limit bounds rendering as it bounds analysis."""
    if page_no > settings.max_pages:
        raise HTTPException(
            status_code=404, detail=f"page {page_no} is past the limit of {settings.max_pages}"
        )


def page_size(pdf: Path, page_no: int) -> tuple[float, float]:
    """Width and height of a page in PDF points; 404 for a page the PDF does not have."""
    with _PDFIUM_LOCK, pdfium.PdfDocument(pdf) as document:
        if not 1 <= page_no <= len(document):
            raise HTTPException(status_code=404, detail=f"no page {page_no}")
        with closing(document[page_no - 1]) as page:
            width, height = page.get_size()
    return float(width), float(height)


def render_scale(width: float, height: float) -> float:
    """``RENDER_SCALE``, lowered for a page so large its longest side would pass
    ``MAX_RENDER_SIDE`` pixels."""
    return min(RENDER_SCALE, MAX_RENDER_SIDE / max(width, height))


def render_page(pdf: Path, page_no: int, target: Path) -> None:
    with _PDFIUM_LOCK:
        # The same 404 for a missing page; the lock is reentrant.
        scale = render_scale(*page_size(pdf, page_no))
        with (
            pdfium.PdfDocument(pdf) as document,
            closing(document[page_no - 1]) as page,
            closing(page.render(scale=scale)) as bitmap,
        ):
            # PIL can share the native bitmap buffer. Detach before closing PDFium objects.
            image = bitmap.to_pil().copy()
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    target.parent.mkdir(parents=True, exist_ok=True)
    # A name of its own, so two requests rendering the same page never share a temporary file.
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_bytes(buffer.getvalue())
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)


def parse_box(text: str) -> tuple[float, float, float, float]:
    try:
        left, top, right, bottom = (float(part) for part in text.split(","))
    except ValueError:
        raise HTTPException(status_code=400, detail="box is left,top,right,bottom") from None
    if not all(math.isfinite(value) for value in (left, top, right, bottom)):
        raise HTTPException(status_code=400, detail="box coordinates must be finite")
    if right <= left or bottom <= top:
        raise HTTPException(status_code=400, detail="box is not ordered")
    return left, top, right, bottom


def place_box(
    box: tuple[float, float, float, float], width: float, height: float
) -> dict[str, float | bool] | None:
    """The box as shares of the page, clipped to it, with whether it was clipped; None when no
    part of it lies on the page. A box past the edge is a fact of the extraction, so the viewer
    shows what lies on the page and says the rest was cut, rather than drawing off the image."""
    left, top, right, bottom = box
    x0, y0 = max(left, 0.0), max(top, 0.0)
    x1, y1 = min(right, width), min(bottom, height)
    if x1 <= x0 or y1 <= y0:
        return None
    return {
        "left": 100 * x0 / width,
        "top": 100 * y0 / height,
        "width": 100 * (x1 - x0) / width,
        "height": 100 * (y1 - y0) / height,
        "clipped": (x0, y0, x1, y1) != (left, top, right, bottom),
    }


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
        _within_page_limit(settings_of(request), page_no)
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
        _within_page_limit(settings_of(request), page_no)
        pdf = _pdf(store, sha256)
        width, height = page_size(pdf, page_no)
        overlay = place_box(parse_box(box), width, height) if box is not None else None
        upload = store.get_upload(sha256)
        return _page(
            "source.html",
            profile=profile.value,
            sha256=sha256,
            filename=upload.filename if upload else sha256,
            page_no=page_no,
            box_given=box is not None,
            overlay=overlay,
        )
