"""The first dry run: the whole pipeline of today over the train holdout (week 2, task 2).

    make dry-run        (PYTHONPATH=eval uv run python -m harness.dry_run)

Selecting the holdout is a look, so it goes through `fra_core.split.holdout_for_scoring`, which
has the scoring log row written before any document is returned. There is no pool argument: this
command can only select the holdout. It is spent once per candidate; do not run it to try things.

Per document: locate, convert (a child process), structure and review, timed. Every document
runs under its own fresh artifact root inside this run's output, so no stage reads a result an
earlier run wrote and every time is real work. A document that fails is recorded by failure
class (and, for a crash, the file and line it happened at) and the run goes on. The report,
var/eval/dry_run/<date>-<candidate>.json, holds ids, strata, timings, outcomes, counts and
failure classes: no row label, cell value, page text or error detail, because nobody reads a
holdout document to debug it. A document's artifacts are deleted as soon as its row is
recorded, and no child output reaches the terminal. Every figure is given annual, interim and
total (D18). Headline figures are over corporate documents; negative controls are reported
apart. Times are over the documents that succeeded, compared with the budget of
docs/blueprint/08-revised-plan.md, and projected onto the checkpoint run of model_test and blind.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from fra_core.schemas import CheckResult, Statement, StatementType
from fra_core.split import (
    TRAIN,
    Look,
    Move,
    Part,
    holdout_availability,
    holdout_for_scoring,
)
from fra_ingest.child import convert_in_child
from fra_ingest.config import REPO_ROOT, IngestConfig, load_config
from fra_ingest.errors import IngestError
from fra_ingest.ocr import default_engine
from fra_ingest.results import ConvertResult, LocateResult, StructureResult
from fra_ingest.stage import load_or_locate
from fra_ingest.structure import structure_pdf
from harness.holdout_records import log_look, read_looks, read_moves
from harness.paths import CANDIDATES, CORPUS, FETCHED, HOLDOUT_MOVES, SCORING_LOG
from harness.structure import first_statements, identity_status, load_checks

OUT = REPO_ROOT / "var" / "eval" / "dry_run"
STRATA = ("annual", "interim")
# docs/blueprint/08-revised-plan.md: under 1 minute for a digital report, 2 to 4 minutes for a
# scanned one (a mixed document sits between; it is held to the scanned budget).
DIGITAL_BUDGET_S = 60.0
OTHER_BUDGET_S = 240.0
# The same file, "Run time of the checkpoint": minutes for model_test and blind on one profile,
# at the digital budget and at the scanned budget for mixed documents. An estimate.
PLAN_MINUTES = {"low": 167, "high": 428}
PROJECTED_POOLS = ("model_test", "blind")
DIGITAL = "digital"
UNMEASURED = "unmeasured"  # in a projected pool but not in fetched.yaml with a text layer
NEGATIVE_CONTROL = "negative_control"  # the `role` of a bank or insurer in candidates.yaml
TIMING = {
    "locate": "load_or_locate with the page cache on, as structure_pdf calls it, so the pages "
    "are read once and cached; structure then finds locate.json and the cache",
    "run": "one cold run per document: a fresh artifact root, no cache from an earlier run",
    "convert": "a child process per document, so models are loaded in each child",
}
# Exit codes: 0 done, 1 an uncaught error, 2 argparse (a bad argument), then these.
EXIT_REFUSED = 3  # preflight: no look was taken
EXIT_INCOMPLETE = 4  # interrupted; the look was taken and the partial report written
EXIT_ALL_FAILED = 5  # every document failed

ConvertStage = Callable[[Path, IngestConfig], ConvertResult]


class PreflightFailed(Exception):
    """The run would waste a look, so it was refused before the look was logged."""


@dataclass(frozen=True)
class Stages:
    """The stages of the pipeline as the dry run calls them, so tests can stub them. The
    structure stage takes the convert stage, as `structure_pdf` does, so convert is timed."""

    locate: Callable[[Path, IngestConfig], LocateResult]
    convert: ConvertStage
    structure: Callable[[Path, IngestConfig, ConvertStage], StructureResult]
    checks: Callable[[IngestConfig, str], dict[str, list[CheckResult]]]


def real_stages() -> Stages:
    ocr = default_engine()
    return Stages(
        locate=lambda pdf, config: load_or_locate(pdf, config, ocr),
        convert=convert_in_child,
        structure=lambda pdf, config, convert: structure_pdf(pdf, config, ocr, convert=convert),
        checks=lambda config, sha: load_checks(config.artifact_root / sha / "table_checks.json"),
    )


def budget_s(text_layer: str) -> float:
    return DIGITAL_BUDGET_S if text_layer == "digital" else OTHER_BUDGET_S


def flag_classes(flags: Iterable[str]) -> list[str]:
    """What kind of flag, without its detail: a detail can be a piece of the document."""
    return sorted({flag.split(":")[0] for flag in flags})


def failure_class(exc: Exception) -> str:
    return exc.reason if isinstance(exc, IngestError) else f"crash:{type(exc).__name__}"


def crash_location(exc: Exception) -> str | None:
    """Where a crash happened: the innermost frame's file name and line, never the message."""
    frame = exc.__traceback__
    if frame is None or isinstance(exc, IngestError):
        return None
    while frame.tb_next is not None:
        frame = frame.tb_next
    return f"{Path(frame.tb_frame.f_code.co_filename).name}:{frame.tb_lineno}"


def _flagged(statement: Statement, prefix: str) -> str:
    flagged = any(c.startswith(prefix) for c in flag_classes(statement.flags))
    return "flagged" if flagged else "unflagged"


def _statements(
    result: StructureResult, config: IngestConfig, checks: Mapping[str, list[CheckResult]]
) -> dict[str, dict[str, Any]]:
    found = first_statements(result, config.enabled_types)
    reviews = {r.statement_id: r for r in result.reviews}
    rows: dict[str, dict[str, Any]] = {}
    for statement_type in sorted(found):
        s = found[statement_type]
        review = reviews.get(s.id)
        rows[statement_type.value] = {
            "review": review.status if review else None,
            "reason_classes": flag_classes(review.reasons) if review else [],
            "identity": (
                identity_status(checks.get(s.id, []))[0]
                if s.type is StatementType.BALANCE
                else None
            ),
            "scale": _flagged(s, "scale_"),
            "currency": _flagged(s, "currency_"),
        }
    return rows


def run_document(
    entry: Mapping[str, Any],
    text_layer: str,
    pdf: Path,
    config: IngestConfig,
    stages: Stages,
    clock: Callable[[], float],
) -> dict[str, Any]:
    """One document through the pipeline. A failure is recorded by class and returned as a row;
    only a stop of the whole run (an interrupt) escapes."""
    row: dict[str, Any] = {
        "id": entry["id"],
        "period": entry["period"],
        "language": entry["language"],
        **{k: entry[k] for k in ("role", "sector") if k in entry},
        "text_layer": text_layer,
        "failure": None,
        "failure_at": None,
        "stage_seconds": {},
        "stages": {},
        "statements": {},
        "identity": None,
    }
    seconds: dict[str, float] = row["stage_seconds"]
    convert_seconds = 0.0
    converted: list[ConvertResult] = []

    def timed_convert(path: Path, cfg: IngestConfig) -> ConvertResult:
        nonlocal convert_seconds
        started = clock()
        try:
            converted.append(stages.convert(path, cfg))
            return converted[-1]
        finally:
            convert_seconds += clock() - started
            seconds["convert"] = convert_seconds

    started = clock()
    try:
        began = clock()
        located = stages.locate(pdf, config)
        seconds["locate"] = clock() - began
        row["stages"]["locate"] = {
            "pages": located.document.page_count,
            "types_found": sorted(
                {r.type.value for r in located.ranges if r.type in config.enabled_types}
            ),
            "candidate_share": round(located.candidate_share, 3),
            "industry": located.industry.kind,
            "flags": flag_classes(located.flags),
        }
        began = clock()
        try:
            result = stages.structure(pdf, config, timed_convert)
        finally:
            if converted:
                convert = converted[0]
                row["stages"]["convert"] = {
                    "statuses": [r.status for r in convert.ranges],
                    "peak_gb": convert.peak_footprint_gb,
                    "flags": flag_classes(
                        [*convert.flags, *(f for r in convert.ranges for f in r.flags)]
                    ),
                }
            seconds["structure"] = clock() - began - convert_seconds
        row["stages"]["structure"] = {"flags": flag_classes(result.flags)}
        row["statements"] = _statements(result, config, stages.checks(config, result.sha256))
        row["identity"] = (row["statements"].get(StatementType.BALANCE.value) or {}).get("identity")
    except Exception as exc:  # recorded per document, the run continues
        row["failure"] = failure_class(exc)
        row["failure_at"] = crash_location(exc)
    row["seconds"] = round(clock() - started, 3)
    row["stage_seconds"] = {k: round(v, 3) for k, v in seconds.items()}
    return row


def _stratified(
    rows: Sequence[dict[str, Any]], figure: Callable[[list[dict[str, Any]]], Any]
) -> dict[str, Any]:
    """A figure for each period kind and for all documents."""
    return {
        **{s: figure([r for r in rows if r["period"] == s]) for s in STRATA},
        "total": figure(list(rows)),
    }


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 3) if values else None


def _p90(values: list[float]) -> float | None:
    """The nearest-rank 90th percentile."""
    ranked = sorted(values)
    return round(ranked[math.ceil(0.9 * len(ranked)) - 1], 3) if ranked else None


def _count(
    rows: Sequence[dict[str, Any]], test: Callable[[dict[str, Any]], bool]
) -> dict[str, Any]:
    return _stratified(rows, lambda rs: sum(1 for r in rs if test(r)))


def _seconds(rows: list[dict[str, Any]]) -> list[float]:
    return [r["seconds"] for r in rows]


def _times(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Time figures over these rows, with their document count."""
    return {
        "documents": _count(rows, lambda r: True),
        "total_s": _stratified(rows, lambda rs: round(sum(_seconds(rs)), 3)),
        "median_s": _stratified(rows, lambda rs: _median(_seconds(rs))),
        "p90_s": _stratified(rows, lambda rs: _p90(_seconds(rs))),
        "max_s": _stratified(rows, lambda rs: max(_seconds(rs), default=None)),
    }


def _budget(rows: Sequence[dict[str, Any]], layer: str) -> dict[str, Any]:
    """The succeeded documents of one text layer against their budget."""
    mine = [r for r in rows if r["text_layer"] == layer]
    done = [r for r in mine if r["failure"] is None]
    times = _times(done)
    return {
        "budget_s": budget_s(layer),
        "documents": _count(mine, lambda r: True),
        "succeeded": times["documents"],
        "over_budget": _count(done, lambda r: r["seconds"] > budget_s(layer)),
        "median_s": times["median_s"],
        "p90_s": times["p90_s"],
        "total_s": times["total_s"],
    }


def _is_control(row: Mapping[str, Any]) -> bool:
    return bool(row.get("role") == NEGATIVE_CONTROL)


def _equals(key: str, value: Any) -> Callable[[dict[str, Any]], bool]:
    return lambda r: bool(r[key] == value)


def _statement_test(
    statement_type: str, test: Callable[[dict[str, Any]], bool]
) -> Callable[[dict[str, Any]], bool]:
    return lambda r: statement_type in r["statements"] and test(r["statements"][statement_type])


def _per_type(
    rows: Sequence[dict[str, Any]], test: Callable[[dict[str, Any]], bool]
) -> dict[str, Any]:
    """For each statement type that any row found, the rows whose statement of that type
    passes `test`."""
    types = sorted({t for r in rows for t in r["statements"]})
    return {t: _count(rows, _statement_test(t, test)) for t in types}


def _is_verdict(verdict: str) -> Callable[[dict[str, Any]], bool]:
    return lambda r: _verdict(r) == verdict


def _verdict(row: Mapping[str, Any]) -> str:
    return str(row["stages"].get("locate", {}).get("industry", "none"))


def _failures(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return {
        c: _count(rows, _equals("failure", c))
        for c in sorted({r["failure"] for r in rows if r["failure"]})
    }


def _controls(controls: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The negative controls (banks and insurers, which v1 declines): the locator's industry
    verdict, whether they failed, and which statements were found."""
    return {
        "documents_run": _count(controls, lambda r: True),
        "documents_failed": _count(controls, lambda r: r["failure"] is not None),
        "by_industry_verdict": {
            v: _count(controls, _is_verdict(v)) for v in sorted({_verdict(r) for r in controls})
        },
        "by_failure_class": _failures(controls),
        "statements_found": _per_type(controls, lambda s: True),
    }


def aggregate(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Every figure of the run, each given as annual, interim and total. Headline figures are
    over corporate documents; negative controls are reported apart; times are over the
    documents that succeeded, with the seconds of the failed ones apart."""
    corporate = [r for r in rows if not _is_control(r)]
    done = [r for r in rows if r["failure"] is None]
    failed = [r for r in rows if r["failure"] is not None]
    identity = {s: _count(corporate, _equals("identity", s)) for s in ("ok", "failed", "skipped")}
    completed = [r for r in corporate if r["failure"] is None]
    return {
        "documents_run": _count(rows, lambda r: True),
        "corporate_documents_run": _count(corporate, lambda r: True),
        "balance_and_income_found": _count(
            corporate, lambda r: {"balance", "income"} <= set(r["statements"])
        ),
        # The five buckets add up to corporate_documents_run. A failed document has no identity
        # and is its own bucket, apart from a document with no balance sheet found.
        "identity": {
            **identity,
            "no_balance": _count(completed, _equals("identity", None)),
            "failed_document": _count(corporate, lambda r: r["failure"] is not None),
        },
        "statements_found": _per_type(corporate, lambda s: True),
        "statements_held": _per_type(corporate, lambda s: s["review"] == "needs_review"),
        "statements_passed_review": _per_type(corporate, lambda s: s["review"] == "passed"),
        "failures": {
            "documents_failed": _count(corporate, lambda r: r["failure"] is not None),
            "by_class": _failures(corporate),
        },
        "negative_controls": _controls([r for r in rows if _is_control(r)]),
        "time": {
            "succeeded": _times(done),
            "failed": {
                "documents": _count(failed, lambda r: True),
                "total_s": _stratified(failed, lambda rs: round(sum(_seconds(rs)), 3)),
            },
        },
        "budget": {
            layer: _budget(rows, layer) for layer in sorted({r["text_layer"] for r in rows})
        },
    }


def pool_layers(
    candidates: Iterable[Mapping[str, Any]], fetched: Mapping[str, Mapping[str, Any]]
) -> dict[str, dict[str, int]]:
    """Documents of each projected pool by measured text layer, from the records only."""
    counts: dict[str, dict[str, int]] = {pool: {} for pool in PROJECTED_POOLS}
    for d in candidates:
        if d["pool"] in counts:
            layer = fetched.get(d["id"], {}).get("text_layer", UNMEASURED)
            counts[d["pool"]][layer] = counts[d["pool"]].get(layer, 0) + 1
    return counts


def _layer_stats(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    values = _seconds(list(rows))
    return {
        "documents": len(values),
        "median_s": _median(values),
        "p90_s": _p90(values),
        "total_s": round(sum(values), 3),
    }


def projection(
    rows: Sequence[dict[str, Any]], layers_by_pool: Mapping[str, Mapping[str, int]]
) -> dict[str, Any]:
    """The checkpoint run (model_test and blind) projected from the succeeded holdout
    documents: per text layer, the count of documents times the median and the 90th percentile
    of that layer here. A layer with no sample here, and not digital, uses the non-digital
    figures and says so; a digital layer with no sample is not projected, and counted."""
    done = [r for r in rows if r["failure"] is None]
    own = {
        layer: _layer_stats([r for r in done if r["text_layer"] == layer])
        for layer in sorted({r["text_layer"] for r in done})
    }
    non_digital = _layer_stats([r for r in done if r["text_layer"] != DIGITAL])
    pools: dict[str, Any] = {}
    for pool in PROJECTED_POOLS:
        minutes = {"median": 0.0, "p90": 0.0}
        not_projected = 0
        layers: dict[str, Any] = {}
        for layer, count in sorted(layers_by_pool.get(pool, {}).items()):
            if layer in own:
                basis, used = "own", own[layer]
            elif layer != DIGITAL and non_digital["documents"]:
                basis, used = "non_digital", non_digital
            else:
                layers[layer] = {"documents": count, "basis": None}
                not_projected += count
                continue
            layers[layer] = {
                "documents": count,
                "basis": basis,
                "median_s": used["median_s"],
                "p90_s": used["p90_s"],
            }
            minutes["median"] += count * used["median_s"] / 60
            minutes["p90"] += count * used["p90_s"] / 60
        pools[pool] = {
            "documents": sum(layers_by_pool.get(pool, {}).values()),
            "documents_not_projected": not_projected,
            "layers": layers,
            "minutes": {k: round(v, 1) for k, v in minutes.items()},
        }
    return {
        "sample_by_layer": own,
        "sample_non_digital": non_digital,
        "pools": pools,
        "minutes": {
            k: round(sum(p["minutes"][k] for p in pools.values()), 1) for k in ("median", "p90")
        },
        "documents_not_projected": sum(p["documents_not_projected"] for p in pools.values()),
        "plan_minutes": {**PLAN_MINUTES, "source": "docs/blueprint/08-revised-plan.md"},
    }


def rows_path(report_path: Path) -> Path:
    """The rows file beside a report: report.json has report.rows.jsonl."""
    return report_path.with_name(f"{report_path.stem}.rows.jsonl")


def run(
    documents: Sequence[Mapping[str, Any]],
    fetched: Mapping[str, Mapping[str, Any]],
    moves: Sequence[Move],
    looks: Sequence[Look],
    *,
    look: Look,
    record: Callable[[Look], None],
    store: Path,
    report_path: Path,
    artifacts_root: Path,
    config: IngestConfig,
    stages: Stages,
    layers_by_pool: Mapping[str, Mapping[str, int]],
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    """Refuse a run that would waste its look, take the look, run every holdout document and
    write the report, also when the run stops half way: the look has happened by then. A
    document's artifacts are deleted as soon as its row is recorded."""

    def pdf_of(document: Mapping[str, Any]) -> Path:
        return store / TRAIN / f"{document['id']}.pdf"

    ready = {
        str(d["id"])
        for d in documents
        if pdf_of(d).is_file() and "text_layer" in fetched.get(d["id"], {})
    }
    availability = holdout_availability(documents, moves, ready)
    if availability.missing:
        raise PreflightFailed(
            f"{availability.missing} of {availability.documents} holdout documents have no PDF in "
            f"{store / TRAIN} or no measured text layer in {FETCHED.name}; no look was taken"
        )
    rows_file = rows_path(report_path)
    for output in (report_path, rows_file, artifacts_root):
        if output.exists():
            raise PreflightFailed(f"{output} exists; a run never reuses an earlier one's output")
    holdout = sorted(
        holdout_for_scoring(documents, moves, looks, look, record), key=lambda d: d["id"]
    )

    rows: list[dict[str, Any]] = []
    rows_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        for entry in holdout:
            print(f"{entry['id']} ...", file=sys.stderr, flush=True)
            root = artifacts_root / entry["id"]
            try:
                rows.append(
                    run_document(
                        entry,
                        fetched[entry["id"]]["text_layer"],
                        pdf_of(entry),
                        config.model_copy(update={"artifact_root": root}),
                        stages,
                        clock,
                    )
                )
                # On disk at once, so a crash of this process loses no finished document.
                with rows_file.open("a", encoding="utf-8") as out:
                    out.write(json.dumps(rows[-1], ensure_ascii=False) + "\n")
                    out.flush()
            finally:
                if root.exists():
                    shutil.rmtree(root)
    finally:
        report = {
            "look": {
                "date": look.date,
                "kind": look.kind,
                "candidate": look.candidate,
                "strata": list(look.strata),
            },
            "complete": len(rows) == len(holdout),
            "documents_planned": len(holdout),
            "budget_s": {"digital": DIGITAL_BUDGET_S, "other": OTHER_BUDGET_S},
            "timing": TIMING,
            "rows_file": str(rows_file),
            "rows": rows,
            "aggregates": aggregate(rows),
            "projection": projection(rows, layers_by_pool),
        }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        if artifacts_root.exists():
            artifacts_root.rmdir()
    return report


def summary(report: Mapping[str, Any]) -> str:
    lines = []
    for r in report["rows"]:
        outcome = r["failure"] or " ".join(f"{t}:{s['review']}" for t, s in r["statements"].items())
        lines.append(
            f"{r['id']:44} {r['period']:8} {r['text_layer']:8} {r['seconds']:8.1f}s  "
            f"identity {r['identity'] or '-':8} {outcome}"
        )
    lines.append(f"complete: {report['complete']}  (budget_s {report['budget_s']})")
    lines.append(
        json.dumps({k: report[k] for k in ("timing", "aggregates", "projection")}, indent=2)
    )
    return "\n".join(lines)


def exit_code(report: Mapping[str, Any]) -> int:
    if not report["complete"]:
        return EXIT_INCOMPLETE
    if not any(r["failure"] is None for r in report["rows"]):
        return EXIT_ALL_FAILED
    return 0


def _git(args: list[str]) -> str:
    done = subprocess.run(["git", *args], cwd=REPO_ROOT, check=True, capture_output=True, text=True)
    return done.stdout


def candidate(git: Callable[[list[str]], str] = _git) -> str:
    """The short commit that was run, marked when the tree has uncommitted changes, an
    untracked source file included (`var/` is ignored)."""
    sha = git(["rev-parse", "--short", "HEAD"]).strip()
    dirty = git(["status", "--porcelain"]).strip()
    return f"{sha}-dirty" if dirty else sha


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(
        prog="python -m harness.dry_run", description="Dry run on the train holdout (one look)"
    ).parse_args(argv)
    candidates = yaml.safe_load(CANDIDATES.read_text(encoding="utf-8"))["documents"]
    documents = [d for d in candidates if d["pool"] == TRAIN]
    fetched = yaml.safe_load(FETCHED.read_text(encoding="utf-8"))["documents"]
    looks = read_looks(SCORING_LOG)
    moves = read_moves(HOLDOUT_MOVES, looks)
    look = Look(date.today().isoformat(), "dry_run", (Part.HOLDOUT.value,), STRATA, candidate())
    name = f"{look.date}-{look.candidate}"
    report_path = OUT / f"{name}.json"
    try:
        report = run(
            documents,
            fetched,
            moves,
            looks,
            look=look,
            record=lambda each: log_look(SCORING_LOG, each),
            store=CORPUS,
            report_path=report_path,
            artifacts_root=OUT / f"{name}-artifacts",
            config=load_config(),
            stages=real_stages(),
            layers_by_pool=pool_layers(candidates, fetched),
        )
    except PreflightFailed as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except KeyboardInterrupt:
        if report_path.is_file():  # the look was taken: show what was measured
            print(summary(json.loads(report_path.read_text(encoding="utf-8"))))
        return EXIT_INCOMPLETE
    print(summary(report))
    return exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
