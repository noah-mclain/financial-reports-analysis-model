"""The convert stage inside the child: plan, run docling per range, write the artifacts
(spec 10, Data flow and Failure handling)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from pydantic import ValidationError

from fra_ingest.config import IngestConfig
from fra_ingest.converter import DoclingRunner, RangeRunner, docling_version
from fra_ingest.footprint import peak_footprint_gb
from fra_ingest.ocr_policy import plan_ranges
from fra_ingest.results import ConvertResult, LocateResult, RangeConversion, RangePlan

CONVERT_VERSION = "1"

# Errors that mean our code is wrong, not that docling could not read the pages. They are
# raised, as in the pages stage, so a broken adapter cannot pass as unreadable ranges.
_PROGRAMMING_ERRORS = (TypeError, AttributeError, NameError)
_ERROR_CHARS = 200


def settings_hash(
    config: IngestConfig, plans: Sequence[RangePlan], docling: str, locate_version: str
) -> str:
    """Everything that changes what convert writes. The timeouts on the child and the memory
    budget change how a run is judged, not its output, so they are left out."""
    payload = {
        "device": config.device,
        "ocr_engine": config.convert_ocr,
        "images_scale": config.images_scale,
        "batch_size": config.batch_size,
        "do_cell_matching": config.do_cell_matching,
        "document_timeout_s": config.document_timeout_s,
        "docling": docling,
        "locate": locate_version,
        "plans": [plan.model_dump(mode="json") for plan in plans],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def convert_pdf(
    pdf: Path,
    located: LocateResult,
    ocr_languages: Mapping[int, str | None],
    config: IngestConfig,
    *,
    runner_factory: Callable[[IngestConfig], RangeRunner] = DoclingRunner,
    use_cache: bool = True,
    docling: str | None = None,
    timings: Mapping[str, float] | None = None,
) -> ConvertResult:
    out_dir = config.artifact_root / located.document.sha256
    plans = plan_ranges(located, ocr_languages, config)
    release = docling or docling_version()
    digest = settings_hash(config, plans, release, located.version)
    if use_cache and (cached := _cached(out_dir, digest)) is not None:
        return cached

    for name in ("docling", "pages"):
        shutil.rmtree(out_dir / name, ignore_errors=True)
        (out_dir / name).mkdir(parents=True)

    runner: RangeRunner | None = None
    ranges: list[RangeConversion] = []
    page_images: dict[int, str] = {}
    run_seconds = 0.0
    for plan in plans:
        if plan.ocr == "skipped":
            ranges.append(_conversion(plan, "skipped", flags=[f"ocr_unavailable:{plan.label}"]))
            continue
        if runner is None:
            runner = runner_factory(config)
        started = time.perf_counter()
        ranges.append(_convert_range(runner, pdf, plan, out_dir, page_images))
        run_seconds += time.perf_counter() - started

    models = runner.models_seconds if runner is not None else 0.0
    flags = [] if plans else ["no_statements_found"]
    result = ConvertResult(
        version=CONVERT_VERSION,
        sha256=located.document.sha256,
        locate_version=located.version,
        docling_version=release,
        device=config.device,
        settings_hash=digest,
        ranges=ranges,
        page_images=page_images,
        flags=flags,
        timings={**(timings or {}), "models": models, "convert": max(run_seconds - models, 0.0)},
    )
    if result.all_failed:
        result.flags.append("all_ranges_failed")
    return _write(out_dir, result, config)


def _convert_range(
    runner: RangeRunner,
    pdf: Path,
    plan: RangePlan,
    out_dir: Path,
    page_images: dict[int, str],
) -> RangeConversion:
    started = time.perf_counter()
    try:
        output = runner(pdf, plan)
    except _PROGRAMMING_ERRORS:
        raise
    except Exception as exc:
        return _conversion(
            plan,
            "failed",
            seconds=time.perf_counter() - started,
            flags=[f"convert_failed:{plan.label}", _error(f"{type(exc).__name__}: {exc}")],
        )

    seconds = time.perf_counter() - started
    errors = [_error(text) for text in output.errors]
    if any(not plan.first_page <= n <= plan.last_page for n in output.page_numbers):
        return _conversion(
            plan, "failed", seconds=seconds, flags=[f"page_outside_range:{plan.label}"]
        )
    if output.status == "failed":
        return _conversion(
            plan, "failed", seconds=seconds, flags=[f"convert_failed:{plan.label}", *errors]
        )

    flags = [f"convert_partial:{plan.label}", *errors] if output.status == "partial" else []
    docling_path = f"docling/p{plan.first_page}-{plan.last_page}.json"
    output.write_json(out_dir / docling_path)
    for page_no in range(plan.first_page, plan.last_page + 1):
        if page_no not in output.page_images:
            flags.append(f"page_not_converted:{page_no}")
            continue
        image = output.page_images[page_no]
        if image is None:
            flags.append(f"page_image_missing:{page_no}")
            continue
        relative = f"pages/{page_no}.png"
        image.save(out_dir / relative)
        page_images[page_no] = relative
    return _conversion(
        plan,
        output.status,
        seconds=time.perf_counter() - started,
        docling_path=docling_path,
        tables=output.tables,
        flags=flags,
    )


def _conversion(
    plan: RangePlan,
    status: str,
    *,
    seconds: float = 0.0,
    docling_path: str | None = None,
    tables: int = 0,
    flags: list[str] | None = None,
) -> RangeConversion:
    return RangeConversion.model_validate(
        {
            "first_page": plan.first_page,
            "last_page": plan.last_page,
            "ocr": plan.ocr,
            "ocr_language": plan.ocr_language,
            "docling_path": docling_path,
            "tables": tables,
            "seconds": seconds,
            "status": status,
            "flags": flags or [],
        }
    )


def _error(text: str) -> str:
    return f"error:{' '.join(text.split())[:_ERROR_CHARS]}"


def _cached(out_dir: Path, digest: str) -> ConvertResult | None:
    """A previous result, when it was made with the same settings, converted every range
    without failure, and every file it names is still there. A failed or partial range is
    retried, as a failed OCR read is in the pages stage."""
    path = out_dir / "convert.json"
    if not path.is_file():
        return None
    try:
        result = ConvertResult.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError):
        return None
    if result.version != CONVERT_VERSION or result.settings_hash != digest:
        return None
    if any(r.status in ("failed", "partial") for r in result.ranges):
        return None
    files = [r.docling_path for r in result.ranges if r.docling_path is not None]
    files += list(result.page_images.values())
    if not all((out_dir / name).is_file() for name in files):
        return None
    return result


def _write(out_dir: Path, result: ConvertResult, config: IngestConfig) -> ConvertResult:
    started = time.perf_counter()
    peak = peak_footprint_gb()
    flags = list(result.flags)
    if peak > config.memory_budget_gb:
        flags.append("memory_over_budget")
    out_dir.mkdir(parents=True, exist_ok=True)
    final = result.model_copy(update={"peak_footprint_gb": peak, "flags": flags})
    final.timings["write"] = time.perf_counter() - started
    temporary = out_dir / "convert.json.tmp"
    temporary.write_text(final.model_dump_json(indent=2), encoding="utf-8")
    os.replace(temporary, out_dir / "convert.json")
    return final
