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
recorded, and no child output reaches the terminal. A document also records the industry
decision (pass, held for review or declined, with its code), the critical items mapped, flagged
or whose statement was not found, and the metrics computed or null by metric id: counts and flag
classes only, never a label, a reason or a value. A declined document is an outcome, not a
failure. Every figure is given annual, interim and total (D18). Headline figures are over
corporate documents; negative controls are reported apart. Times are over the documents that
succeeded, compared with the budget of docs/blueprint/08-revised-plan.md, and projected onto the
checkpoint run of model_test and blind.

The critical-item and metrics steps run after ingest and are timed apart. A failure in either is
recorded in its own fields (`critical_failure`, `metrics_failure`, class and file:line) and the
document stays a success for ingest, identity, time and budget. Per-document seconds include both
steps, which the first run did not have; `seconds_excl_metrics` leaves them out, so ingest time
stays comparable with it. The aggregates add the share of corporate documents with all six items
the plan names mapped (`PLAN_CRITICAL_IDS`), metric availability (attempted, computed and null,
per registry metric), and the share held for review by reason class, industry codes included,
each also per language. The decline judgement against the sector label is given for negative
controls only. `--earlier` names a first run's report, read only: its headline figures are
recomputed under these definitions, or marked not available for run 1.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from fra_analytics.frame import primary_statements
from fra_analytics.metrics.registry import METRIC_IDS, compute
from fra_analytics.policy import load_policy
from fra_core.schemas import CheckResult, MetricValue, Statement, StatementType
from fra_core.split import (
    TRAIN,
    Look,
    Move,
    Part,
    holdout_availability,
    holdout_for_scoring,
)
from fra_core.taxonomy.loader import PLAN_CRITICAL_IDS, Taxonomy, load_taxonomy
from fra_ingest.child import convert_in_child
from fra_ingest.config import REPO_ROOT, IngestConfig, load_config
from fra_ingest.errors import IngestError
from fra_ingest.label_mapping import MAPPED_TYPES
from fra_ingest.label_match import LabelIndex
from fra_ingest.ocr import make_engine
from fra_ingest.pages import sha256_file
from fra_ingest.results import ConvertResult, IndustryDecision, LocateResult, StructureResult
from fra_ingest.stage import load_or_locate
from fra_ingest.structure import structure_pdf
from harness.holdout_records import log_look, read_looks, read_moves
from harness.locate import decision_label, judge_decision, truth_label
from harness.mapping import SLOT_STATES, critical_slots
from harness.paths import (
    ANALYTICS_POLICY,
    CANDIDATES,
    CORPUS,
    FETCHED,
    HOLDOUT_MOVES,
    NEGATIVE_CONTROL,
    SCORING_LOG,
    STRATA,
)
from harness.reproducibility import record_hash, report_evidence
from harness.structure import first_statements, identity_status, load_checks

OUT = REPO_ROOT / "var" / "eval" / "dry_run"
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
_ID = re.compile(r"[a-z0-9][a-z0-9-]*")  # an id is a file name, never a path
TIMING = {
    "locate": "load_or_locate with the page cache on, as structure_pdf calls it, so the pages "
    "are read once and cached; structure then finds locate.json and the cache",
    "run": "one cold run per document: a fresh artifact root, no cache from an earlier run",
    "convert": "a child process per document, so models are loaded in each child",
    "seconds": "seconds per document include the critical-item and metrics steps, which the first "
    "run did not have; seconds_excl_metrics leaves both out, so ingest time stays comparable "
    "with the first run",
    "preloaded": "the taxonomy and the analytics policy are loaded before the look, so no "
    "document's time holds that cost",
}
NOT_AVAILABLE = "not available for run 1"
# What this candidate changed against the first run: the comparison is between two systems.
DIFFERENCES = (
    "the two candidates differ in structure, locate and mapping versions, in the industry hold "
    "(a document held for industry is kept and counted, not declined unseen) and in the timing "
    "definition (per-document seconds include the critical-item and metrics steps)"
)
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
    critical: Callable[[StructureResult], dict[str, str]]
    metrics: Callable[[Sequence[Statement]], list[MetricValue]]


def critical_states(
    result: StructureResult, taxonomy: Taxonomy, index: LabelIndex
) -> dict[str, str]:
    """The state of each critical item: mapped, ambiguous (a flagged row names the item),
    explicitly_flagged (a valid finding names it), unmapped (no row carries it) or
    statement_not_found. Item ids are unique in the taxonomy."""
    slots = critical_slots(first_statements(result, MAPPED_TYPES), None, index, taxonomy)[1]
    return {slot["item"]: slot["state"] for slot in slots}


def real_stages(config: IngestConfig) -> Stages:
    """The stages of today. The engine, the taxonomy and the policy are all loaded here, before
    the look, so no first document pays for them."""
    ocr = make_engine(config)
    policy = load_policy(ANALYTICS_POLICY)
    taxonomy = load_taxonomy()
    index = LabelIndex(taxonomy)
    return Stages(
        locate=lambda pdf, config: load_or_locate(pdf, config, ocr),
        convert=convert_in_child,
        structure=lambda pdf, config, convert: structure_pdf(pdf, config, ocr, convert=convert),
        checks=lambda config, sha: load_checks(config.artifact_root / sha / "table_checks.json"),
        critical=lambda result: critical_states(result, taxonomy, index),
        metrics=lambda statements: compute(primary_statements(statements), policy),
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


def _industry(entry: Mapping[str, Any], decision: IndustryDecision | None) -> dict[str, Any]:
    """The decision as a record: its outcome and code, never its reason or evidence, which quote
    the document. `judgement` is `judge_decision` against the document's sector label."""
    label = decision_label(decision)
    return {
        "outcome": decision.outcome if decision else "pass",
        "code": decision.code if decision else None,
        "label": label,
        "judgement": judge_decision(truth_label(entry), label),
    }


def _metric_counts(values: Iterable[MetricValue]) -> dict[str, dict[str, Any]]:
    """Per metric id: periods attempted (one value record each), computed and null, and the
    classes of its flags. No value. A metric the stage returned no record for is absent, so it
    was never attempted; one attempted and computed nowhere has attempted above zero."""
    counts: dict[str, dict[str, Any]] = {}
    for m in values:
        entry = counts.setdefault(
            m.metric_id, {"attempted": 0, "computed": 0, "null": 0, "flag_classes": set()}
        )
        entry["attempted"] += 1
        entry["null" if m.value is None else "computed"] += 1
        entry["flag_classes"].update(flag_classes(m.flags))
    return {k: {**v, "flag_classes": sorted(v["flag_classes"])} for k, v in counts.items()}


def _critical_counts(
    stages: Stages, result: StructureResult
) -> tuple[dict[str, int], dict[str, str]]:
    """Critical items by state, and the state of each of the plan's six. Counts only."""
    states = stages.critical(result)
    counts = {state: sum(1 for v in states.values() if v == state) for state in SLOT_STATES}
    return counts, {item: states[item] for item in PLAN_CRITICAL_IDS}


def _step(
    row: dict[str, Any],
    name: str,
    step: Callable[[], Any],
    seconds: dict[str, float],
    clock: Callable[[], float],
) -> Any:
    """A step after ingest, timed. A failure is recorded as `<name>_failure` (class) and
    `<name>_failure_at` (file and line) and the step's result is None; `failure` is untouched."""
    began = clock()
    try:
        return step()
    except Exception as exc:  # recorded per step, the document goes on
        row[f"{name}_failure"] = failure_class(exc)
        row[f"{name}_failure_at"] = crash_location(exc)
        return None
    finally:
        seconds[name] = clock() - began


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
        "ingest_config": config.model_dump(mode="json"),
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
        "industry": None,
        "critical": None,
        "plan_critical": None,
        "critical_failure": None,
        "critical_failure_at": None,
        "metrics": {},
        "metrics_failure": None,
        "metrics_failure_at": None,
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
        row["industry"] = _industry(entry, result.industry)
        if row["industry"]["outcome"] != "declined":  # a declined document has no statements
            # After the ingest figures are set: a failure here is the step's own, and the document
            # stays a success for identity, time and budget.
            counted = _step(
                row, "critical", lambda: _critical_counts(stages, result), seconds, clock
            )
            if counted is not None:
                row["critical"], row["plan_critical"] = counted
            values = _step(
                row, "metrics", lambda: stages.metrics(result.statements), seconds, clock
            )
            if values is not None:
                row["metrics"] = _metric_counts(values)
    except Exception as exc:  # recorded per document, the run continues
        row["failure"] = failure_class(exc)
        row["failure_at"] = crash_location(exc)
    total = clock() - started
    row["seconds"] = round(total, 3)
    row["seconds_excl_metrics"] = round(
        total - seconds.get("critical", 0.0) - seconds.get("metrics", 0.0), 3
    )
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


def _seconds(rows: list[dict[str, Any]], key: str = "seconds") -> list[float]:
    return [r[key] for r in rows]


def _times(rows: Sequence[dict[str, Any]], key: str = "seconds") -> dict[str, Any]:
    """Time figures over these rows (`key`: the whole document, or without the metrics and
    critical-item steps), with their document count."""
    return {
        "documents": _count(rows, lambda r: True),
        "total_s": _stratified(rows, lambda rs: round(sum(_seconds(rs, key)), 3)),
        "median_s": _stratified(rows, lambda rs: _median(_seconds(rs, key))),
        "p90_s": _stratified(rows, lambda rs: _p90(_seconds(rs, key))),
        "max_s": _stratified(rows, lambda rs: max(_seconds(rs, key), default=None)),
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


JUDGEMENTS = ("right", "held", "wrong")
OUTCOMES = ("pass", "declined", "needs_review")
Test = Callable[[dict[str, Any]], bool]


def _decided(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rows that went through the whole pipeline: a failed document has no decision."""
    return [r for r in rows if r["industry"] is not None]


def _industry_is(key: str, value: str) -> Test:
    return lambda r: bool(r["industry"][key] == value)


def _industry_figures(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The industry outcome and code of the documents."""
    decided = _decided(rows)
    return {
        **{o: _count(decided, _industry_is("outcome", o)) for o in OUTCOMES},
        "by_code": {
            c: _count(decided, _industry_is("label", c))
            for c in sorted({r["industry"]["label"] for r in decided})
        },
    }


def _judgement_figures(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Whether each decision is right for the document's sector label (`judge_decision`): a
    judgement against the label in candidates.yaml, the only truth a bank or insurer has here.
    It is not an accuracy, and it is given for negative controls only."""
    decided = _decided(rows)
    return {j: _count(decided, _industry_is("judgement", j)) for j in JUDGEMENTS}


def _total_of(state: str) -> Callable[[list[dict[str, Any]]], int]:
    # reports written before explicit findings existed carry no explicitly_flagged count
    if state == "explicitly_flagged":
        return lambda rs: sum(r["critical"].get(state, 0) for r in rs)
    return lambda rs: sum(r["critical"][state] for r in rs)


def _critical_figures(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Critical item states summed over the documents that reached statements. A declined
    document has none, so it is counted beside, not inside."""
    reached = [r for r in rows if r["critical"] is not None]
    return {
        "scope": "corporate documents that were not declined and whose critical-item step ran; "
        "the declined documents are counted in documents_declined",
        "documents": _count(reached, lambda r: True),
        "documents_declined": _count(_decided(rows), _industry_is("outcome", "declined")),
        **{state: _stratified(reached, _total_of(state)) for state in SLOT_STATES},
    }


def _share_of(numerator: Test, denominator: Test) -> Callable[[list[dict[str, Any]]], float | None]:
    """The share of the rows passing `denominator` that also pass `numerator`; None when none
    pass it."""

    def figure(rows: list[dict[str, Any]]) -> float | None:
        wanted = sum(1 for r in rows if denominator(r))
        return sum(1 for r in rows if numerator(r)) / wanted if wanted else None

    return figure


def _all_rows(row: dict[str, Any]) -> bool:
    return True


def _ran(row: Mapping[str, Any]) -> bool:
    """The metrics stage ran to its end: the document was ingested, not declined, no failure."""
    industry = row["industry"]
    return bool(
        row["failure"] is None
        and industry is not None
        and industry["outcome"] != "declined"
        and row["metrics_failure"] is None
    )


def _metrics_failed(row: Mapping[str, Any]) -> bool:
    return row["metrics_failure"] is not None


def _metrics_stage(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Documents whose metrics stage ran, failed, or never ran (declined, or failed in ingest)."""
    return {
        "ran": _count(rows, _ran),
        "failed": _count(rows, _metrics_failed),
        "not_run": _count(rows, lambda r: not _ran(r) and not _metrics_failed(r)),
    }


def _item_mapped(item: str) -> Test:
    return lambda r: bool(r["plan_critical"][item] == "mapped")


def _all_six_mapped(row: Mapping[str, Any]) -> bool:
    return all(v == "mapped" for v in row["plan_critical"].values())


def _plan_figures(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The plan's six critical items (`PLAN_CRITICAL_IDS`): documents with all six mapped, and
    each item's mapped count, over the documents whose critical-item step ran."""
    reached = [r for r in rows if r["plan_critical"] is not None]
    return {
        "items": list(PLAN_CRITICAL_IDS),
        "documents": _count(reached, _all_rows),
        "all_mapped": _count(reached, _all_six_mapped),
        "share_all_mapped": _stratified(reached, _share_of(_all_six_mapped, _all_rows)),
        "by_item": {i: _count(reached, _item_mapped(i)) for i in PLAN_CRITICAL_IDS},
    }


def _reasons(row: Mapping[str, Any]) -> list[str]:
    """Why a document is held: the reason classes of its statements and, for a hold on the
    industry, `industry:<code>`."""
    classes = {c for s in row["statements"].values() for c in s.get("reason_classes", [])}
    industry = row["industry"]
    if industry is not None and industry["outcome"] == "needs_review":
        classes.add(f"industry:{industry['code']}")
    return sorted(classes)


def _is_held(row: Mapping[str, Any]) -> bool:
    return bool(_reasons(row)) or any(
        s.get("review") == "needs_review" for s in row["statements"].values()
    )


def _has_reason(reason: str) -> Test:
    return lambda r: reason in _reasons(r)


def _held_figures(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The share of ingested documents held for review, and by reason class (the industry codes
    among them). A document may have several reasons, so the classes do not add up."""
    done = [r for r in rows if r["failure"] is None]
    return {
        "documents": _count(done, _all_rows),
        "held": _count(done, _is_held),
        "share": _stratified(done, _share_of(_is_held, _all_rows)),
        "by_reason": {
            c: _count(done, _has_reason(c)) for c in sorted({c for r in done for c in _reasons(r)})
        },
    }


def _metric_total(metric: str, kind: str) -> Callable[[list[dict[str, Any]]], int]:
    return lambda rs: sum(r["metrics"].get(metric, {}).get(kind, 0) for r in rs)


def _metric_in_document(metric: str, kind: str) -> Test:
    return lambda r: bool(r["metrics"].get(metric, {}).get(kind, 0) > 0)


def _has_flag_class(metric: str, flag_class: str) -> Test:
    return lambda r: flag_class in r["metrics"].get(metric, {}).get("flag_classes", [])


def _metric_ids(rows: Sequence[dict[str, Any]]) -> list[str]:
    """Every metric `compute` produces, so one never attempted is listed at zero, not left out."""
    return sorted({*METRIC_IDS, *(m for r in rows for m in r["metrics"])})


def _metric_availability(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Per metric: the share of documents whose metrics ran with at least one period computed."""
    return {
        m: _stratified(list(rows), _share_of(_metric_in_document(m, "computed"), _ran))
        for m in _metric_ids(rows)
    }


def _metric_figures(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Per metric id: periods attempted, computed and null (attempted 0 means never attempted),
    the documents for each, the availability, and documents carrying each flag class."""
    figures: dict[str, Any] = {}
    available = _metric_availability(rows)
    for metric in _metric_ids(rows):
        classes = {c for r in rows for c in r["metrics"].get(metric, {}).get("flag_classes", [])}
        figures[metric] = {
            "attempted": _stratified(list(rows), _metric_total(metric, "attempted")),
            "computed": _stratified(list(rows), _metric_total(metric, "computed")),
            "null": _stratified(list(rows), _metric_total(metric, "null")),
            "documents_attempted": _count(rows, _metric_in_document(metric, "attempted")),
            "documents_computed": _count(rows, _metric_in_document(metric, "computed")),
            "availability": available[metric],
            "flag_classes": {c: _count(rows, _has_flag_class(metric, c)) for c in sorted(classes)},
        }
    return figures


def _failures(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return {
        c: _count(rows, _equals("failure", c))
        for c in sorted({r["failure"] for r in rows if r["failure"]})
    }


def _controls(controls: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The negative controls (banks and insurers, which v1 declines): the locator's industry
    verdict, whether they failed, and which statements were found."""
    figures = _industry_figures(controls)
    return {
        "documents_run": _count(controls, lambda r: True),
        "documents_failed": _count(controls, lambda r: r["failure"] is not None),
        "by_industry_verdict": {
            v: _count(controls, _is_verdict(v)) for v in sorted({_verdict(r) for r in controls})
        },
        "by_failure_class": _failures(controls),
        "industry_outcome": {k: v for k, v in figures.items() if k in OUTCOMES},
        "judgement_against_label": _judgement_figures(controls),
        "judgement_note": "each decision judged against the sector label in candidates.yaml, "
        "which is the only truth a control has here; it is not an accuracy",
        "statements_found": _per_type(controls, lambda s: True),
    }


def _identity_figures(corporate: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The identity buckets add up to the corporate documents run. A failed document has no
    identity and is its own bucket, apart from a document with no balance sheet found. A document
    whose metrics or critical-item step failed was ingested: it has its identity, and is no
    failure here."""
    completed = [r for r in corporate if r["failure"] is None]
    return {
        **{s: _count(corporate, _equals("identity", s)) for s in ("ok", "failed", "skipped")},
        "no_balance": _count(completed, _equals("identity", None)),
        "failed_document": _count(corporate, lambda r: r["failure"] is not None),
    }


def _by_language(corporate: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The figures that matter per language (Arabic is where the first run failed)."""
    return {
        language: {
            "corporate_documents_run": _count(mine, _all_rows),
            "documents_failed": _count(mine, lambda r: r["failure"] is not None),
            "plan_critical_items": _plan_figures(mine),
            "held_for_review": _held_figures(mine),
            "metric_availability": _metric_availability(mine),
        }
        for language in sorted({r["language"] for r in corporate})
        for mine in [[r for r in corporate if r["language"] == language]]
    }


def aggregate(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Every figure of the run, each given as annual, interim and total. Headline figures are
    over corporate documents; negative controls are reported apart; times are over the
    documents that succeeded, with the seconds of the failed ones apart."""
    corporate = [r for r in rows if not _is_control(r)]
    done = [r for r in rows if r["failure"] is None]
    failed = [r for r in rows if r["failure"] is not None]
    return {
        "documents_run": _count(rows, _all_rows),
        "corporate_documents_run": _count(corporate, _all_rows),
        "balance_and_income_found": _count(
            corporate, lambda r: {"balance", "income"} <= set(r["statements"])
        ),
        "identity": _identity_figures(corporate),
        "industry": _industry_figures(corporate),
        "critical_items": _critical_figures(corporate),
        "plan_critical_items": _plan_figures(corporate),
        "held_for_review": _held_figures(corporate),
        "metrics_stage": _metrics_stage(corporate),
        "metrics": _metric_figures(corporate),
        "by_language": _by_language(corporate),
        "statements_found": _per_type(corporate, lambda s: True),
        "statements_held": _per_type(corporate, lambda s: s["review"] == "needs_review"),
        "statements_passed_review": _per_type(corporate, lambda s: s["review"] == "passed"),
        "failures": {
            "documents_failed": _count(corporate, lambda r: r["failure"] is not None),
            "by_class": _failures(corporate),
            "metrics_failed": _count(corporate, _metrics_failed),
            "critical_failed": _count(corporate, lambda r: r["critical_failure"] is not None),
        },
        "negative_controls": _controls([r for r in rows if _is_control(r)]),
        "time": {
            "succeeded": _times(done),
            "succeeded_excl_metrics": _times(done, "seconds_excl_metrics"),
            "failed": {
                "documents": _count(failed, _all_rows),
                "total_s": _stratified(failed, lambda rs: round(sum(_seconds(rs)), 3)),
            },
        },
        "budget": {
            layer: _budget(rows, layer) for layer in sorted({r["text_layer"] for r in rows})
        },
    }


def _when_fields(
    rows: Sequence[Mapping[str, Any]], fields: tuple[str, ...], figure: Callable[[], Any]
) -> Any:
    """The figure when every row has every field it reads, else "not available for run 1"."""
    return figure() if all(f in r for r in rows for f in fields) else NOT_AVAILABLE


def earlier_figures(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The headline figures of the first run, recomputed from its rows with this run's
    definitions. A figure that reads a field its rows lack (industry decision, critical items,
    metrics) is not available for run 1, never filled in."""
    corporate = [r for r in rows if not _is_control(r)]
    done = [r for r in rows if r["failure"] is None]

    def failed(row: dict[str, Any]) -> bool:
        return row["failure"] is not None

    def have(*fields: str) -> Callable[[Callable[[], Any]], Any]:
        return lambda figure: _when_fields(rows, fields, figure)

    return {
        "documents_run": _count(rows, _all_rows),
        "corporate_documents_run": _count(corporate, _all_rows),
        "documents_failed": have("failure")(lambda: _count(corporate, failed)),
        "failures_by_class": have("failure")(lambda: _failures(corporate)),
        "balance_and_income_found": have("statements")(
            lambda: _count(corporate, lambda r: {"balance", "income"} <= set(r["statements"]))
        ),
        "identity": have("failure", "identity")(lambda: _identity_figures(corporate)),
        "statements_held": have("statements")(
            lambda: _per_type(corporate, lambda s: s["review"] == "needs_review")
        ),
        "time_succeeded": have("failure", "seconds")(lambda: _times(done)),
        "budget": have("failure", "seconds", "text_layer")(
            lambda: {
                layer: _budget(rows, layer) for layer in sorted({r["text_layer"] for r in rows})
            }
        ),
        "by_language": have("failure", "language")(
            lambda: {
                lang: {
                    "corporate_documents_run": _count(mine, _all_rows),
                    "documents_failed": _count(mine, failed),
                }
                for lang in sorted({r["language"] for r in corporate})
                for mine in [[r for r in corporate if r["language"] == lang]]
            }
        ),
        "industry": have("industry")(lambda: _industry_figures(corporate)),
        "critical_items": have("critical")(lambda: _critical_figures(corporate)),
        "plan_critical_items": have("plan_critical")(lambda: _plan_figures(corporate)),
        "held_for_review": have("industry")(lambda: _held_figures(corporate)),
        "metrics": have("metrics")(lambda: _metric_figures(corporate)),
    }


def load_earlier(path: Path) -> dict[str, Any]:
    """The first run's report, read and checked before the look: it is read, never written."""
    if not path.is_file():
        raise PreflightFailed(f"the earlier report {path} is not a file; no look was taken")
    report = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(report, dict) or not isinstance(report.get("rows"), list):
        raise PreflightFailed(f"the earlier report {path} has no rows; no look was taken")
    named = report.get("look", {}).get("candidate")
    if not isinstance(named, str) or not re.fullmatch(r"[0-9a-f]+(-dirty)?", named):
        raise PreflightFailed(f"the earlier report {path} names no candidate; no look was taken")
    return report


def comparison(earlier: Mapping[str, Any], this_candidate: str) -> dict[str, Any]:
    return {
        "earlier_candidate": earlier["look"]["candidate"],
        "this_candidate": this_candidate,
        "differences": DIFFERENCES,
        "figures": earlier_figures(earlier["rows"]),
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
    earlier: Path | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    """Refuse a run that would waste its look, take the look, run every holdout document and
    write the report, also when the run stops half way: the look has happened by then. A
    document's artifacts are deleted as soon as its row is recorded."""

    def pdf_of(document: Mapping[str, Any]) -> Path:
        return store / TRAIN / f"{document['id']}.pdf"

    unsafe = sum(1 for d in documents if not _ID.fullmatch(str(d["id"])))
    if unsafe:
        raise PreflightFailed(
            f"{unsafe} train document ids are not plain names ({_ID.pattern}); an id names a "
            "file and a directory this run deletes; no look was taken"
        )
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
    # Compared before the look: an earlier report that cannot be read back must refuse the run,
    # not raise once every document has run.
    compared: dict[str, Any] | None = None
    if earlier is not None:
        try:
            compared = comparison(load_earlier(earlier), look.candidate)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise PreflightFailed(
                f"the earlier report {earlier} cannot be compared "
                f"({type(exc).__name__}); no look was taken"
            ) from exc
    rows_file = rows_path(report_path)
    for output in (report_path, rows_file, artifacts_root):
        if output.exists():
            raise PreflightFailed(f"{output} exists; a run never reuses an earlier one's output")
    # This uses only the inputs already admitted by the scoring guard. It neither selects
    # another holdout nor reads labels/values. Hash once before each document's existing run.
    evidence = report_evidence(
        config,
        documents={},
        expected={},
        inputs={
            "candidates": record_hash(documents),
            "fetched": record_hash(fetched),
            "moves": record_hash([asdict(m) for m in moves]),
        },
    )
    holdout = sorted(
        holdout_for_scoring(documents, moves, looks, look, record), key=lambda d: d["id"]
    )

    rows: list[dict[str, Any]] = []
    rows_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        for entry in holdout:
            print(f"{entry['id']} ...", file=sys.stderr, flush=True)
            evidence["documents"][entry["id"]] = sha256_file(pdf_of(entry))
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
            "reproducibility": evidence,
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
            "comparison": compared,
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
        json.dumps(
            {k: report[k] for k in ("timing", "aggregates", "projection", "comparison")}, indent=2
        )
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


def parser() -> argparse.ArgumentParser:
    parse = argparse.ArgumentParser(
        prog="python -m harness.dry_run", description="Dry run on the train holdout (one look)"
    )
    parse.add_argument(
        "--earlier",
        type=Path,
        help="an earlier run's report.json, read only: its headline figures are recomputed "
        "under today's definitions beside this run's",
    )
    return parse


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    candidates = yaml.safe_load(CANDIDATES.read_text(encoding="utf-8"))["documents"]
    documents = [d for d in candidates if d["pool"] == TRAIN]
    fetched = yaml.safe_load(FETCHED.read_text(encoding="utf-8"))["documents"]
    looks = read_looks(SCORING_LOG)
    moves = read_moves(HOLDOUT_MOVES, looks)
    look = Look(date.today().isoformat(), "dry_run", (Part.HOLDOUT.value,), STRATA, candidate())
    name = f"{look.date}-{look.candidate}"
    report_path = OUT / f"{name}.json"
    config = load_config()
    # Built before the look is taken, so a missing engine refuses the run and wastes no look.
    stages = real_stages(config)
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
            config=config,
            stages=stages,
            layers_by_pool=pool_layers(candidates, fetched),
            earlier=args.earlier,
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
