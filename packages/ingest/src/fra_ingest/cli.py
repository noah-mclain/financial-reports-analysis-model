"""Command line for the ingest stages.

fra-ingest locate <pdf> [--json] [--no-ocr] [--no-cache] [--config PATH] [--artifacts DIR]
fra-ingest convert <pdf> [--json] [--no-ocr] [--no-cache] [--config PATH] [--artifacts DIR]
fra-ingest structure <pdf> [--json] [--no-ocr] [--no-cache] [--config PATH] [--artifacts DIR]
fra-ingest review-report <sha256> [--config PATH] [--artifacts DIR]

convert exits 0 when it wrote a result, 2 on an ingest error and 3 when every range it
attempted failed. It is the child process of convert_in_child (spec 10). structure exits 0
when it wrote a result and 2 on an ingest error. review-report takes a document's sha256 under
the artifact root, or a unique prefix of it, writes review.html beside the stored results and
prints its path; it runs no stage and exits 2 when the document or an artifact is missing.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from fra_ingest.config import IngestConfig, load_config
from fra_ingest.convert import convert_pdf
from fra_ingest.errors import IngestError
from fra_ingest.ocr import OcrEngine, OcrUnavailableError, make_engine
from fra_ingest.results import ConvertResult, LocateResult, StructureResult
from fra_ingest.review_report import write_review_report
from fra_ingest.stage import load_or_locate, locate_pdf, page_ocr_languages
from fra_ingest.structure import structure_pdf

EXIT_ERROR = 2
EXIT_ALL_FAILED = 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fra-ingest")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (
        ("locate", "find the statement pages of a PDF"),
        ("convert", "convert the located statement pages with docling"),
        ("structure", "structure the converted statements"),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument("pdf", type=Path)
        command.add_argument("--json", action="store_true", help="print the full result as JSON")
        command.add_argument("--no-ocr", action="store_true", help="leave image pages unread")
        command.add_argument(
            "--no-cache",
            action="store_true",
            help="locate: ignore the page text cache; convert: convert again; "
            "structure: structure again",
        )
        command.add_argument("--config", type=Path, default=None)
        command.add_argument("--artifacts", type=Path, default=None, help="artifact root override")
    report = commands.add_parser(
        "review-report", help="draw the extracted cells on their page images"
    )
    report.add_argument("sha256", help="a document's sha256, or a unique prefix of it")
    report.add_argument("--config", type=Path, default=None)
    report.add_argument("--artifacts", type=Path, default=None, help="artifact root override")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if args.artifacts is not None:
        config = config.model_copy(update={"artifact_root": args.artifacts})
    if args.command == "review-report":
        try:
            print(write_review_report(args.sha256, config))
        except IngestError as exc:
            print(f"{args.sha256}: {exc.reason} {exc.detail}".rstrip(), file=sys.stderr)
            return EXIT_ERROR
        return 0

    try:
        engine = None if args.no_ocr else make_engine(config)
        if args.command == "convert":
            return _convert(args, config, engine)
        if args.command == "structure":
            result_s = structure_pdf(args.pdf, config, engine, use_cache=not args.no_cache)
            print(result_s.model_dump_json(indent=2) if args.json else _structure_summary(result_s))
            return 0
        result = locate_pdf(args.pdf, config, engine, use_cache=not args.no_cache)
    except IngestError as exc:
        print(f"{args.pdf}: {exc.reason} {exc.detail}".rstrip(), file=sys.stderr)
        return EXIT_ERROR
    except OcrUnavailableError as exc:
        print(
            f"{exc}; choose another engine with convert.ocr_engine in the settings or "
            "FRA_OCR_ENGINE, or pass --no-ocr",
            file=sys.stderr,
        )
        return EXIT_ERROR

    print(result.model_dump_json(indent=2) if args.json else _summary(result))
    return 0


def _convert(args: argparse.Namespace, config: IngestConfig, engine: OcrEngine | None) -> int:
    started = time.perf_counter()
    located = load_or_locate(args.pdf, config, engine)
    languages = page_ocr_languages(args.pdf, config, engine)
    result = convert_pdf(
        args.pdf,
        located,
        languages,
        config,
        use_cache=not args.no_cache,
        timings={"locate": time.perf_counter() - started},
    )
    print(result.model_dump_json(indent=2) if args.json else _convert_summary(result))
    return EXIT_ALL_FAILED if result.all_failed else 0


def _summary(result: LocateResult) -> str:
    document = result.document
    scanned = len(document.scanned_pages)
    lines = [
        f"{document.filename}  {document.page_count} pages ({scanned} image)  "
        f"language {document.language}",
        f"industry  {result.industry.kind}"
        + (f"/{result.industry.subkind}" if result.industry.subkind else "")
        + f"  score {result.industry.score:.1f}",
    ]
    for r in result.ranges:
        lines.append(
            f"  {r.type.value:22} pp. {r.first_page}-{r.last_page}  "
            f"score {r.score:.1f}  rank {r.rank}"
        )
    spans = ", ".join(f"{first}-{last}" for first, last in result.convert_ranges) or "none"
    lines.append(f"convert  {spans}  ({result.candidate_share:.0%} of pages)")
    lines.append(f"flags  {', '.join(result.flags) or 'none'}")
    lines.append("time  " + "  ".join(f"{k} {v:.1f}s" for k, v in result.timings.items()))
    return "\n".join(lines)


def _convert_summary(result: ConvertResult) -> str:
    peak = f"{result.peak_footprint_gb:.2f} GB" if result.peak_footprint_gb is not None else "n/a"
    lines = [f"{result.sha256[:12]}  docling {result.docling_version}  device {result.device}"]
    for r in result.ranges:
        line = (
            f"  pp. {r.first_page}-{r.last_page}  {r.ocr:9} {r.ocr_language or '-':6} "
            f"{r.status:8} tables {r.tables}  {r.seconds:.1f}s"
        )
        lines.append(line + (f"  {', '.join(r.flags)}" if r.flags else ""))
    lines.append(f"pages  {len(result.page_images)} images  peak {peak}")
    lines.append(f"flags  {', '.join(result.flags) or 'none'}")
    lines.append("time  " + "  ".join(f"{k} {v:.1f}s" for k, v in result.timings.items()))
    return "\n".join(lines)


def _structure_summary(result: StructureResult) -> str:
    lines = [f"{result.sha256[:12]}  structure {result.version}"]
    reviews = {r.statement_id: r for r in result.reviews}
    for s in result.statements:
        periods = ", ".join(p.key for p in s.periods)
        lines.append(
            f"  {s.type.value:22} pp. {s.source_pages[0]}-{s.source_pages[-1]}  "
            f"{len(s.line_items)} lines  "
            f"{s.currency} x{s.scale}  [{periods}]  {', '.join(s.flags) or 'ok'}"
        )
        review = reviews.get(s.id)
        if review is not None:
            lines.append(
                f"    {review.status}  {review.checked_cells} of {review.numeric_cells} cells "
                f"checked  {', '.join(review.reasons)}".rstrip()
            )
    rejected = sum(1 for t in result.tables if t.type is None)
    lines.append(f"tables  {len(result.tables)} ({rejected} not statements)")
    lines.append(f"flags  {', '.join(result.flags) or 'none'}")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
