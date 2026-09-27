"""Command line for the ingest stages.

fra-ingest locate <pdf> [--json] [--no-ocr] [--no-cache] [--config PATH] [--artifacts DIR]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from fra_ingest.config import load_config
from fra_ingest.errors import IngestError
from fra_ingest.ocr import default_engine
from fra_ingest.results import LocateResult
from fra_ingest.stage import locate_pdf


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fra-ingest")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("locate", help="find the statement pages of a PDF")
    run.add_argument("pdf", type=Path)
    run.add_argument("--json", action="store_true", help="print the full result as JSON")
    run.add_argument("--no-ocr", action="store_true", help="leave image pages unread")
    run.add_argument("--no-cache", action="store_true", help="ignore the page text cache")
    run.add_argument("--config", type=Path, default=None)
    run.add_argument("--artifacts", type=Path, default=None, help="artifact root override")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if args.artifacts is not None:
        config = config.model_copy(update={"artifact_root": args.artifacts})
    engine = None if args.no_ocr else default_engine()

    try:
        result = locate_pdf(args.pdf, config, engine, use_cache=not args.no_cache)
    except IngestError as exc:
        print(f"{args.pdf}: {exc.reason} {exc.detail}".rstrip(), file=sys.stderr)
        return 2

    print(result.model_dump_json(indent=2) if args.json else _summary(result))
    return 0


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


if __name__ == "__main__":
    raise SystemExit(main())
