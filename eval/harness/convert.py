"""Measure the convert stage over the golden set (spec 10, Scoring).

    uv run python eval/harness/convert.py [--only ID ...] [--no-cache]

Each document converts in its own child process. The report goes to stdout and to
var/eval/convert-golden.json. Exit 0 when every range of every document is ok and every
child stayed within the memory budget.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from typing import Any

import yaml

from fra_ingest.child import convert_in_child
from fra_ingest.config import REPO_ROOT, load_config
from fra_ingest.results import ConvertResult
from harness.failures import DOCUMENT_ERRORS, EngineFailure, EngineGuard, aborted, document_failure

MANIFEST = REPO_ROOT / "eval" / "golden" / "manifest.yaml"
OUT = REPO_ROOT / "var" / "eval"
_CHILD_STAGES = ("locate", "models", "convert", "write")


def summarize(doc_id: str, result: ConvertResult) -> dict[str, Any]:
    pages = sum(r.last_page - r.first_page + 1 for r in result.ranges if r.status != "skipped")
    timings = result.timings
    wall = timings.get("child_wall", 0.0)
    return {
        "id": doc_id,
        "pages": pages,
        "tables": sum(r.tables for r in result.ranges),
        "statuses": [r.status for r in result.ranges],
        "ranges": [f"{r.first_page}-{r.last_page}:{r.ocr}:{r.ocr_language}" for r in result.ranges],
        "seconds_per_page": round(timings.get("convert", 0.0) / pages, 3) if pages else None,
        "models_s": round(timings.get("models", 0.0), 2),
        "startup_s": wall - sum(timings.get(k, 0.0) for k in _CHILD_STAGES) if wall else None,
        "wall_s": round(wall, 2),
        "peak_gb": result.peak_footprint_gb,
        "flags": result.flags + [f for r in result.ranges for f in r.flags],
    }


def failure_row(doc_id: str, error: Exception) -> dict[str, Any]:
    failure = document_failure(error)
    reason, detail = failure["error"], failure["detail"]
    return {"id": doc_id, "error": f"{reason} {detail}".strip(), "reason": reason, "detail": detail}


def verdict(rows: Sequence[Mapping[str, Any]], budget_gb: float) -> list[str]:
    reasons = []
    for row in rows:
        if "error" in row:
            reasons.append(f"{row['id']}: {row['error']}")
            continue
        not_ok = [s for s in row["statuses"] if s != "ok"]
        if not_ok or not row["statuses"]:
            reasons.append(f"{row['id']}: ranges {', '.join(not_ok) or 'none'}")
        peak = row["peak_gb"]
        if peak is None or peak > budget_gb:
            shown = "unknown" if peak is None else f"{peak:.2f} GB"
            reasons.append(f"{row['id']}: peak {shown} over {budget_gb:.1f} GB")
    return reasons


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eval/harness/convert.py")
    parser.add_argument("--only", action="append", default=[], help="document id; repeatable")
    parser.add_argument("--no-cache", action="store_true", help="convert again")
    args = parser.parse_args(argv)

    config = load_config()
    documents = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["documents"]
    rows: list[dict[str, Any]] = []
    chosen = [e for e in documents if not args.only or e["id"] in args.only]
    guard = EngineGuard(len(chosen))
    stopped: EngineFailure | None = None
    for entry in chosen:
        pdf = MANIFEST.parent / entry["file"]
        print(f"{entry['id']} ...", file=sys.stderr, flush=True)
        try:
            result = convert_in_child(
                pdf, config, extra_args=["--no-cache"] if args.no_cache else ()
            )
        except DOCUMENT_ERRORS as exc:
            rows.append(failure_row(entry["id"], exc))
            try:
                guard.record(rows[-1]["reason"], rows[-1]["detail"])
            except EngineFailure as exc_stop:
                stopped = exc_stop
                break
            continue
        guard.record(None)
        rows.append(summarize(entry["id"], result))

    for row in rows:
        if "error" in row:
            print(f"{row['id']:34} ERROR {row['error']}")
            continue
        peak = f"{row['peak_gb']:.2f}" if row["peak_gb"] is not None else "-"
        per_page = row["seconds_per_page"] if row["seconds_per_page"] is not None else "-"
        print(
            f"{row['id']:34} pages {row['pages']:3}  tables {row['tables']:3}  "
            f"{'/'.join(row['statuses']):20} s/page {per_page}  models {row['models_s']}s  "
            f"wall {row['wall_s']}s  peak {peak} GB"
        )
    reasons = verdict(rows, config.memory_budget_gb)
    if stopped is not None:
        reasons.append(f"aborted: {stopped}")
    print("PASS" if not reasons else "FAIL\n  " + "\n  ".join(reasons))

    OUT.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "rows": rows,
        "reasons": reasons,
        "budget_gb": config.memory_budget_gb,
    }
    (OUT / "convert-golden.json").write_text(
        json.dumps(
            report if stopped is None else aborted(report, stopped), indent=2, ensure_ascii=False
        ),
        encoding="utf-8",
    )
    if stopped is not None:
        raise stopped
    return 0 if not reasons else 1


if __name__ == "__main__":
    raise SystemExit(main())
