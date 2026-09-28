"""Command line for the ingest stages.

fra-ingest locate <pdf> [--json] [--no-ocr] [--no-cache] [--config PATH] [--artifacts DIR]
fra-ingest convert <pdf> [--json] [--no-ocr] [--no-cache] [--config PATH] [--artifacts DIR]

convert exits 0 when it wrote a result, 2 on an ingest error and 3 when every range it
attempted failed. It is the child process of convert_in_child (spec 10).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from fra_ingest.config import IngestConfig, load_config
from fra_ingest.convert import convert_pdf
from fra_ingest.errors import IngestError
from fra_ingest.ocr import OcrEngine, default_engine
from fra_ingest.results import ConvertResult, LocateResult
from fra_ingest.stage import load_or_locate, locate_pdf, page_ocr_languages

EXIT_ERROR = 2
EXIT_ALL_FAILED = 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fra-ingest")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (
        ("locate", "find the statement pages of a PDF"),
        ("convert", "convert the located statement pages with docling"),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument("pdf", type=Path)
        command.add_argument("--json", action="store_true", help="print the full result as JSON")
        command.add_argument("--no-ocr", action="store_true", help="leave image pages unread")
        command.add_argument(
            "--no-cache",
            action="store_true",
            help="locate: ignore the page text cache; convert: convert again",
        )
        command.add_argument("--config", type=Path, default=None)
        command.add_argument("--artifacts", type=Path, default=None, help="artifact root override")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if args.artifacts is not None:
        config = config.model_copy(update={"artifact_root": args.artifacts})
    engine = None if args.no_ocr else default_engine()

    try:
        if args.command == "convert":
            return _convert(args, config, engine)
        result = locate_pdf(args.pdf, config, engine, use_cache=not args.no_cache)
    except IngestError as exc:
        print(f"{args.pdf}: {exc.reason} {exc.detail}".rstrip(), file=sys.stderr)
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


if __name__ == "__main__":
    raise SystemExit(main())
