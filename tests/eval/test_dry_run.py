"""The first dry-run harness, on synthetic records and stub stages: no model, no OCR, no PDF
content, and never the real scoring log."""

import json
import re
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from harness.dry_run import (
    DIGITAL_BUDGET_S,
    EXIT_ALL_FAILED,
    EXIT_INCOMPLETE,
    EXIT_REFUSED,
    OTHER_BUDGET_S,
    STRATA,
    UNMEASURED,
    PreflightFailed,
    Stages,
    aggregate,
    budget_s,
    candidate,
    critical_states,
    earlier_figures,
    exit_code,
    main,
    parser,
    pool_layers,
    projection,
    real_stages,
    run,
    run_document,
)
from harness.holdout_records import LOG_HEADER, log_look, read_looks

from fra_analytics.policy import load_policy
from fra_core.schemas import (
    BBox,
    Cell,
    CheckResult,
    Document,
    LineItem,
    MetricInput,
    MetricUnit,
    MetricValue,
    Period,
    PeriodKind,
    Provenance,
    Statement,
    StatementType,
)
from fra_core.split import Look, Part, PoolRefused, hashed_part, issuer_key
from fra_core.taxonomy.loader import PLAN_CRITICAL_IDS, load_taxonomy
from fra_ingest.config import REPO_ROOT, IngestConfig, load_config
from fra_ingest.errors import IngestError
from fra_ingest.label_match import LabelIndex
from fra_ingest.results import (
    ConvertResult,
    IndustryDecision,
    IndustrySignal,
    LocateResult,
    RangeConversion,
    StatementRange,
    StructureResult,
)
from fra_ingest.review import StatementReview

TAXONOMY = load_taxonomy()
INDEX = LabelIndex(TAXONOMY)
GOLDEN = REPO_ROOT / "eval" / "golden" / "documents"
LABEL = "SECRET LABEL"
VALUE = "98765.43"
SHA = "a" * 64
PERIOD = Period(key="2025-12-31", end_date=date(2025, 12, 31), kind=PeriodKind.INSTANT)
HOLDOUT_ISSUERS = [
    name
    for name in (f"Issuer {n}" for n in range(300))
    if hashed_part(issuer_key(name)) is Part.HOLDOUT
]
FIT_ISSUER = next(
    name
    for name in (f"Issuer {n}" for n in range(300))
    if hashed_part(issuer_key(name)) is Part.FIT
)
LOOK = Look("2026-10-04", "dry_run", ("holdout",), ("annual", "interim"), "abc1234")


def candidate_record(
    issuer: str, period: str = "annual", pool: str = "train", n: int = 0
) -> dict[str, Any]:
    return {
        "id": f"{issuer_key(issuer).replace(' ', '-')}-{n}",
        "issuer": issuer,
        "pool": pool,
        "period": period,
        "language": "en",
        "role": "corporate",
    }


def statement(statement_type: StatementType, flags: list[str] | None = None) -> Statement:
    item = LineItem(
        id="r0",
        raw_label=LABEL,
        cells=[
            Cell(
                period_key=PERIOD.key,
                reported=Decimal(VALUE),
                raw_text=VALUE,
                provenance=Provenance(
                    page_no=1,
                    bbox=BBox(left=1, top=1, right=2, bottom=2),
                    table_ref="#/tables/0",
                    row=0,
                    col=1,
                ),
            )
        ],
    )
    return Statement(
        id=statement_type.value,
        document_sha256=SHA,
        type=statement_type,
        entity_name=LABEL,
        currency="SAR",
        scale=1000,
        periods=[PERIOD],
        line_items=[item],
        flags=flags or [],
    )


def located() -> LocateResult:
    return LocateResult(
        version="v",
        document=Document(sha256=SHA, filename=f"{LABEL}.pdf", page_count=10),
        pages=[],
        ranges=[
            StatementRange(type=StatementType.BALANCE, first_page=3, last_page=3, score=8, rank=1),
            StatementRange(type=StatementType.INCOME, first_page=4, last_page=4, score=8, rank=1),
        ],
        convert_ranges=[(2, 5)],
        industry=IndustrySignal(kind="corporate"),
        flags=["likely_corporate"],
    )


def converted() -> ConvertResult:
    return ConvertResult(
        version="v",
        sha256=SHA,
        locate_version="v",
        docling_version="d",
        device="cpu",
        settings_hash="h",
        ranges=[
            RangeConversion(
                first_page=2, last_page=5, ocr="pdf_aware", ocr_language=None, status="ok"
            )
        ],
        peak_footprint_gb=1.5,
        flags=[f"error:{LABEL}"],
    )


def structured() -> StructureResult:
    return StructureResult(
        version="v",
        sha256=SHA,
        convert_version="v",
        settings_hash="h",
        statements=[
            statement(StatementType.BALANCE, flags=["currency_inferred", "scale_missing"]),
            statement(StatementType.INCOME),
        ],
        reviews=[
            StatementReview(
                statement_id="balance",
                status="passed",
                numeric_cells=1,
                checked_cells=1,
                flagged_cells=0,
            ),
            StatementReview(
                statement_id="income",
                status="needs_review",
                reasons=["numbers_missing:3", "identity_not_checked:missing_values"],
                numeric_cells=1,
                checked_cells=0,
                flagged_cells=0,
            ),
        ],
        flags=[f"note:{LABEL}"],
    )


def metric_values() -> list[MetricValue]:
    """One metric computed (its value is a number that must never reach the report) and one null
    with a flag whose detail names the label."""
    return [
        MetricValue(
            metric_id="net_margin",
            period_key=PERIOD.key,
            value=float(VALUE),
            unit=MetricUnit.RATIO,
            formula="net_income / revenue",
            formula_version="1",
            inputs={
                "numerator": [
                    MetricInput(
                        statement_id="income",
                        line_item_id="r0",
                        canonical_id="net_income",
                        period_key=PERIOD.key,
                        reported=Decimal(VALUE),
                        scale=1000,
                        currency="SAR",
                        provenance=Provenance(
                            page_no=1,
                            bbox=BBox(left=1, top=1, right=2, bottom=2),
                            table_ref="#/tables/0",
                            row=0,
                            col=1,
                        ),
                    )
                ]
            },
        ),
        MetricValue(
            metric_id="current_ratio",
            period_key=PERIOD.key,
            value=None,
            unit=MetricUnit.TIMES,
            formula="a / b",
            formula_version="1",
            flags=[f"missing_input:{LABEL}"],
        ),
    ]


def decision(outcome: str, code: str) -> IndustryDecision:
    return IndustryDecision(
        outcome=outcome,  # type: ignore[arg-type]
        code=code,  # type: ignore[arg-type]
        signal=IndustrySignal(kind="bank", score=20.0, distinct_cues=5, evidence=[(3, LABEL)]),
        reason=f"{outcome}:{code}: evidence page 3 '{LABEL}'",
    )


def declined_result(code: str = "bank") -> StructureResult:
    return StructureResult(
        version="v",
        sha256=SHA,
        convert_version="",
        settings_hash="",
        industry=decision("declined", code),
        flags=[f"declined:{code}"],
    )


def held_result(code: str = "weak_verdict") -> StructureResult:
    return structured().model_copy(update={"industry": decision("needs_review", code)})


def identity_checks() -> dict[str, list[CheckResult]]:
    check = CheckResult(
        id="c",
        statement_id="balance",
        kind="balance_identity",
        period_key=PERIOD.key,
        status="pass",
    )
    return {"balance": [check]}


class Calls:
    """What the stub stages were asked, in order."""

    def __init__(self) -> None:
        self.roots: list[Path] = []
        self.events: list[str] = []


def stub_stages(
    calls: Calls | None = None,
    *,
    locate: Callable[[Path, IngestConfig], LocateResult] | None = None,
    convert: Callable[[Path, IngestConfig], ConvertResult] | None = None,
    result: StructureResult | None = None,
) -> Stages:
    calls = calls or Calls()

    def default_locate(pdf: Path, config: IngestConfig) -> LocateResult:
        calls.roots.append(config.artifact_root)
        calls.events.append(f"locate {pdf.stem}")
        return located()

    def default_convert(pdf: Path, config: IngestConfig) -> ConvertResult:
        calls.events.append(f"convert {pdf.stem}")
        return converted()

    def structure(
        pdf: Path,
        config: IngestConfig,
        convert_stage: Callable[[Path, IngestConfig], ConvertResult],
    ) -> StructureResult:
        convert_stage(pdf, config)
        calls.events.append(f"structure {pdf.stem}")
        return result or structured()

    return Stages(
        locate=locate or default_locate,
        convert=convert or default_convert,
        structure=structure,
        checks=lambda config, sha: identity_checks(),
        critical=lambda result: critical_states(result, TAXONOMY, INDEX),
        metrics=lambda statements: metric_values() if statements else [],
    )


class Setup:
    """A tmp store of PDFs, a tmp scoring log, and the documents of one run."""

    def __init__(self, tmp_path: Path, documents: list[dict[str, Any]]) -> None:
        self.tmp = tmp_path
        self.documents = documents
        self.store = tmp_path / "corpus"
        (self.store / "train").mkdir(parents=True)
        self.log = tmp_path / "scoring_log.tsv"
        self.log.write_text(LOG_HEADER, encoding="utf-8")
        self.report = tmp_path / "out" / "report.json"
        self.artifacts = tmp_path / "out" / "artifacts"
        self.fetched = {d["id"]: {"text_layer": "digital"} for d in documents}
        for d in documents:
            (self.store / "train" / f"{d['id']}.pdf").write_bytes(b"")

    def run(
        self,
        stages: Stages,
        clock: Callable[[], float] | None = None,
        layers_by_pool: dict[str, dict[str, int]] | None = None,
        earlier: Path | None = None,
    ) -> dict[str, Any]:
        return run(
            self.documents,
            self.fetched,
            [],
            read_looks(self.log),
            look=LOOK,
            record=lambda look: log_look(self.log, look),
            store=self.store,
            report_path=self.report,
            artifacts_root=self.artifacts,
            config=IngestConfig(artifact_root=self.tmp / "real-artifacts"),
            stages=stages,
            layers_by_pool=layers_by_pool or {},
            earlier=earlier,
            **({"clock": clock} if clock else {}),
        )


@pytest.fixture
def three(tmp_path: Path) -> Setup:
    a, b, c = HOLDOUT_ISSUERS[:3]
    return Setup(
        tmp_path,
        [
            candidate_record(a, "annual"),
            candidate_record(b, "interim"),
            candidate_record(c, "interim"),
            candidate_record(FIT_ISSUER, "annual"),
        ],
    )


def figures(node: Any, path: str = "") -> list[tuple[str, dict[str, Any]]]:
    """Every stratified figure in the aggregates: a mapping keyed annual, interim and total."""
    if not isinstance(node, dict):
        return []
    if set(node) == {*STRATA, "total"}:
        return [(path, node)]
    return [f for k, v in node.items() for f in figures(v, f"{path}/{k}")]


# ---- the look ---------------------------------------------------------------------------------


@pytest.mark.parametrize("pool", ["model_test", "blind"])
def test_the_run_refuses_model_test_and_blind_before_any_log_row(tmp_path: Path, pool: str) -> None:
    setup = Setup(
        tmp_path, [candidate_record(HOLDOUT_ISSUERS[0]), candidate_record("Other Co", pool=pool)]
    )
    with pytest.raises(PoolRefused, match=pool):
        setup.run(stub_stages())
    assert read_looks(setup.log) == []
    assert not setup.report.exists()


def test_a_run_logs_one_dry_run_row_for_the_holdout_only(three: Setup) -> None:
    calls = Calls()
    report = three.run(stub_stages(calls))
    assert read_looks(three.log) == [LOOK]
    assert report["documents_planned"] == 3  # the fit document is not scored
    assert not any(FIT_ISSUER.lower().replace(" ", "-") in e for e in calls.events)


def test_a_failed_run_still_leaves_its_log_row(three: Setup) -> None:
    def broken(pdf: Path, config: IngestConfig) -> LocateResult:
        raise RuntimeError("stage broke")

    three.run(stub_stages(locate=broken))
    assert read_looks(three.log) == [LOOK]


def test_an_interrupted_run_keeps_its_log_row_and_writes_what_it_has(three: Setup) -> None:
    seen: list[str] = []

    def interrupt_on_second(pdf: Path, config: IngestConfig) -> LocateResult:
        seen.append(pdf.stem)
        if len(seen) == 2:
            raise KeyboardInterrupt
        return located()

    with pytest.raises(KeyboardInterrupt):
        three.run(stub_stages(locate=interrupt_on_second))
    assert read_looks(three.log) == [LOOK]
    written = json.loads(three.report.read_text(encoding="utf-8"))
    assert written["complete"] is False
    assert len(written["rows"]) == 1
    assert figures(written["aggregates"])


def test_each_row_is_on_disk_before_the_next_document_starts(three: Setup) -> None:
    rows_file = three.report.with_name("report.rows.jsonl")
    seen: list[list[str]] = []

    def locate(pdf: Path, config: IngestConfig) -> LocateResult:
        lines = rows_file.read_text(encoding="utf-8").splitlines() if rows_file.exists() else []
        seen.append([json.loads(line)["id"] for line in lines])
        return located()

    report = three.run(stub_stages(locate=locate))
    ids = [r["id"] for r in report["rows"]]
    assert seen == [ids[:0], ids[:1], ids[:2]]
    lines = rows_file.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == report["rows"]
    assert report["rows_file"] == str(rows_file)
    assert LABEL not in rows_file.read_text(encoding="utf-8")


def test_an_existing_rows_file_is_refused_before_any_log_row(three: Setup) -> None:
    three.report.parent.mkdir(parents=True)
    three.report.with_name("report.rows.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(PreflightFailed, match="exists"):
        three.run(stub_stages())
    assert read_looks(three.log) == []


# ---- preflight --------------------------------------------------------------------------------


def test_missing_pdfs_are_refused_before_any_log_row_and_only_counted(three: Setup) -> None:
    holdout_ids = [d["id"] for d in three.documents[:3]]
    (three.store / "train" / f"{holdout_ids[1]}.pdf").unlink()
    with pytest.raises(PreflightFailed, match=r"1 of 3") as caught:
        three.run(stub_stages())
    assert not any(i in str(caught.value) for i in holdout_ids)
    assert read_looks(three.log) == []
    assert not three.report.exists()


def test_a_holdout_document_with_no_measured_text_layer_is_refused_before_the_look(
    three: Setup,
) -> None:
    del three.fetched[three.documents[0]["id"]]
    with pytest.raises(PreflightFailed, match=r"1 of 3"):
        three.run(stub_stages())
    assert read_looks(three.log) == []


@pytest.mark.parametrize("bad", ["../outside", "a/b", "..", ""])
def test_an_id_that_is_not_a_plain_name_is_refused_before_the_look(three: Setup, bad: str) -> None:
    """An id becomes a file name and a directory that is deleted, so it may not hold a path."""
    three.documents[0]["id"] = bad
    with pytest.raises(PreflightFailed, match=r"1 train document ids"):
        three.run(stub_stages())
    assert read_looks(three.log) == []
    assert not three.report.exists()


@pytest.mark.parametrize("which", ["report", "artifacts"])
def test_earlier_output_is_refused_so_nothing_can_come_from_a_cache(
    three: Setup, which: str
) -> None:
    target = three.report if which == "report" else three.artifacts
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir() if which == "artifacts" else target.write_text("{}", encoding="utf-8")
    with pytest.raises(PreflightFailed, match="exists"):
        three.run(stub_stages())
    assert read_looks(three.log) == []


# ---- the documents ----------------------------------------------------------------------------


def test_a_failing_document_is_recorded_by_class_and_the_run_goes_on(three: Setup) -> None:
    def locate(pdf: Path, config: IngestConfig) -> LocateResult:
        if pdf.stem == three.documents[0]["id"]:
            raise IngestError("convert_timeout", f"{LABEL} detail")
        if pdf.stem == three.documents[1]["id"]:
            raise ValueError(f"{LABEL} page text")
        return located()

    report = three.run(stub_stages(locate=locate))
    failures = {r["id"]: r["failure"] for r in report["rows"]}
    assert failures == {
        three.documents[0]["id"]: "convert_timeout",
        three.documents[1]["id"]: "crash:ValueError",
        three.documents[2]["id"]: None,
    }
    by_class = report["aggregates"]["failures"]["by_class"]
    assert by_class["convert_timeout"] == {"annual": 1, "interim": 0, "total": 1}
    assert by_class["crash:ValueError"] == {"annual": 0, "interim": 1, "total": 1}
    assert LABEL not in three.report.read_text(encoding="utf-8")


def test_a_row_carries_stage_outcomes_and_the_candidate_facts(three: Setup) -> None:
    three.fetched[three.documents[0]["id"]] = {"text_layer": "mixed"}
    row = three.run(stub_stages())["rows"][0]
    assert (row["period"], row["language"], row["role"], row["text_layer"]) == (
        "annual",
        "en",
        "corporate",
        "mixed",
    )
    assert "sector" not in row
    assert row["failure"] is None
    assert row["stages"]["locate"]["types_found"] == ["balance", "income"]
    assert row["stages"]["convert"]["statuses"] == ["ok"]
    assert sorted(row["statements"]) == ["balance", "income"]
    balance, income = row["statements"]["balance"], row["statements"]["income"]
    assert balance["review"] == "passed" and balance["identity"] == "ok"
    assert income["review"] == "needs_review"
    assert income["reason_classes"] == ["identity_not_checked", "numbers_missing"]
    assert (balance["scale"], balance["currency"]) == ("flagged", "flagged")
    assert (income["scale"], income["currency"]) == ("unflagged", "unflagged")
    assert row["identity"] == "ok"


def test_the_report_holds_no_labels_values_or_page_text(three: Setup) -> None:
    three.run(stub_stages())
    text = three.report.read_text(encoding="utf-8")
    assert LABEL not in text and VALUE not in text and "98765" not in text
    assert "likely_corporate" in text  # flag classes are kept, flag details are not


def test_stage_seconds_come_from_the_stages_that_ran(three: Setup) -> None:
    now = [0.0]

    def tick() -> float:
        return now[0]

    def locate(pdf: Path, config: IngestConfig) -> LocateResult:
        now[0] += 5
        return located()

    def convert(pdf: Path, config: IngestConfig) -> ConvertResult:
        now[0] += 20
        return converted()

    base = stub_stages(locate=locate, convert=convert)

    def structure(
        pdf: Path, config: IngestConfig, convert_stage: Callable[..., ConvertResult]
    ) -> StructureResult:
        now[0] += 1
        result = base.structure(pdf, config, convert_stage)
        now[0] += 2
        return result

    stages = replace(base, structure=structure)
    row = three.run(stages, clock=tick)["rows"][0]
    assert row["stage_seconds"] == {
        "locate": 5.0,
        "convert": 20.0,
        "structure": 3.0,
        "critical": 0.0,
        "metrics": 0.0,
    }
    assert row["seconds"] == 28.0 and row["seconds_excl_metrics"] == 28.0


def test_every_document_gets_a_fresh_artifact_root_so_no_cache_can_hit(three: Setup) -> None:
    calls = Calls()
    three.run(stub_stages(calls))
    assert len(set(calls.roots)) == len(calls.roots) == 3
    assert all(root.parent == three.artifacts for root in calls.roots)
    assert not (three.tmp / "real-artifacts").exists()


# ---- the aggregates ---------------------------------------------------------------------------


def test_every_figure_is_given_annual_interim_and_total(three: Setup) -> None:
    report = three.run(stub_stages())
    paths = {p for p, _ in figures(report["aggregates"])}
    assert {
        "/documents_run",
        "/corporate_documents_run",
        "/balance_and_income_found",
        "/identity/ok",
        "/identity/failed",
        "/identity/skipped",
        "/identity/no_balance",
        "/statements_found/balance",
        "/statements_held/income",
        "/statements_passed_review/balance",
        "/failures/documents_failed",
        "/negative_controls/documents_run",
        "/time/succeeded/documents",
        "/time/succeeded/total_s",
        "/time/succeeded/median_s",
        "/time/succeeded/p90_s",
        "/time/succeeded/max_s",
        "/time/failed/total_s",
        "/budget/digital/documents",
        "/budget/digital/succeeded",
        "/budget/digital/over_budget",
        "/budget/digital/median_s",
        "/budget/digital/p90_s",
        "/budget/digital/total_s",
    } <= paths
    for _, node in figures(report["aggregates"]):
        assert set(node) == {"annual", "interim", "total"}
    a = report["aggregates"]
    assert a["documents_run"] == {"annual": 1, "interim": 2, "total": 3}
    assert a["statements_passed_review"]["balance"] == {"annual": 1, "interim": 2, "total": 3}
    assert a["statements_held"]["income"] == {"annual": 1, "interim": 2, "total": 3}
    assert a["statements_found"]["income"] == a["documents_run"]


def row(
    period: str,
    seconds: float,
    layer: str = "digital",
    failure: str | None = None,
    identity: str | None = "ok",
    found: tuple[str, ...] = ("balance", "income"),
    role: str = "corporate",
    verdict: str = "corporate",
    industry: str = "pass",
    judgement: str = "right",
    critical: dict[str, int] | None = None,
    metrics: dict[str, dict[str, Any]] | None = None,
    language: str = "en",
    plan: dict[str, str] | None = None,
    reasons: list[str] | None = None,
    metrics_failure: str | None = None,
    metrics_seconds: float = 0.0,
) -> dict[str, Any]:
    statements: dict[str, dict[str, Any]] = {
        t: {"review": "passed", "reason_classes": []} for t in found
    }
    if reasons:  # the first statement is held for these reason classes
        first = next(iter(statements))
        statements[first] = {"review": "needs_review", "reason_classes": reasons}
    return {
        "period": period,
        "language": language,
        "seconds": seconds,
        "seconds_excl_metrics": seconds - metrics_seconds,
        "text_layer": layer,
        "failure": failure,
        "metrics_failure": metrics_failure,
        "critical_failure": None,
        "identity": identity,
        "role": role,
        "stages": {"locate": {"industry": verdict}},
        "statements": statements,
        "industry": None
        if failure
        else {
            "label": industry,
            "outcome": industry.partition("/")[0],
            "code": industry.partition("/")[2] or None,
            "judgement": judgement,
        },
        "critical": critical,
        "plan_critical": plan,
        "metrics": metrics or {},
    }


MIXED_ROWS = [
    row("annual", 30),
    row("annual", 90, failure="convert_timeout", identity=None, found=()),
    row("interim", 100, layer="scanned", identity="failed"),
    row("interim", 300, layer="scanned", identity="skipped", found=("balance",)),
    row("interim", 50, layer="mixed"),
]


def test_aggregate_figures_per_stratum() -> None:
    a = aggregate(MIXED_ROWS)
    assert a["documents_run"] == {"annual": 2, "interim": 3, "total": 5}
    assert a["balance_and_income_found"] == {"annual": 1, "interim": 2, "total": 3}
    assert a["identity"]["ok"] == {"annual": 1, "interim": 1, "total": 2}
    assert a["identity"]["failed"] == {"annual": 0, "interim": 1, "total": 1}
    assert a["identity"]["skipped"] == {"annual": 0, "interim": 1, "total": 1}
    assert a["statements_found"]["balance"] == {"annual": 1, "interim": 3, "total": 4}
    assert a["statements_found"]["income"] == {"annual": 1, "interim": 2, "total": 3}
    assert a["statements_passed_review"]["income"] == {"annual": 1, "interim": 2, "total": 3}
    assert a["failures"]["documents_failed"] == {"annual": 1, "interim": 0, "total": 1}
    assert a["budget"]["digital"]["over_budget"] == {"annual": 0, "interim": 0, "total": 0}
    assert a["budget"]["digital"]["documents"] == {"annual": 2, "interim": 0, "total": 2}
    assert a["budget"]["digital"]["succeeded"] == {"annual": 1, "interim": 0, "total": 1}
    assert a["budget"]["scanned"]["over_budget"] == {"annual": 0, "interim": 1, "total": 1}
    assert a["budget"]["scanned"]["median_s"] == {"annual": None, "interim": 200, "total": 200}
    assert a["budget"]["scanned"]["p90_s"] == {"annual": None, "interim": 300, "total": 300}
    assert a["budget"]["scanned"]["total_s"] == {"annual": 0, "interim": 400, "total": 400}
    assert a["budget"]["mixed"]["over_budget"] == {"annual": 0, "interim": 0, "total": 0}


def test_the_identity_buckets_add_up_to_the_documents_run() -> None:
    a = aggregate([*MIXED_ROWS, row("interim", 20, identity=None, found=("income",))])
    assert a["identity"]["failed_document"] == {"annual": 1, "interim": 0, "total": 1}
    assert a["identity"]["no_balance"] == {"annual": 0, "interim": 1, "total": 1}
    for stratum in ("annual", "interim", "total"):
        buckets = ("ok", "failed", "skipped", "no_balance", "failed_document")
        assert (
            sum(a["identity"][b][stratum] for b in buckets) == a["corporate_documents_run"][stratum]
        )


def test_the_budget_is_60_seconds_for_digital_and_240_for_every_other_layer() -> None:
    assert (DIGITAL_BUDGET_S, OTHER_BUDGET_S) == (60.0, 240.0)
    assert budget_s("digital") == 60.0
    assert budget_s("mixed") == budget_s("scanned") == 240.0
    a = aggregate([row("annual", 61), row("annual", 59), row("annual", 241, layer="mixed")])
    assert a["budget"]["digital"]["over_budget"]["total"] == 1
    assert a["budget"]["mixed"]["over_budget"]["total"] == 1


def test_failed_documents_seconds_are_out_of_the_time_figures() -> None:
    a = aggregate(MIXED_ROWS)
    done = a["time"]["succeeded"]
    assert done["documents"] == {"annual": 1, "interim": 3, "total": 4}
    assert done["total_s"] == {"annual": 30, "interim": 450, "total": 480}
    assert done["median_s"] == {"annual": 30, "interim": 100, "total": 75}
    assert done["p90_s"] == {"annual": 30, "interim": 300, "total": 300}
    assert done["max_s"] == {"annual": 30, "interim": 300, "total": 300}
    failed = a["time"]["failed"]
    assert failed["documents"] == {"annual": 1, "interim": 0, "total": 1}
    assert failed["total_s"] == {"annual": 90, "interim": 0, "total": 90}


def test_a_negative_control_stays_out_of_the_headline_but_in_the_time() -> None:
    bank = row("annual", 20, role="negative_control", verdict="bank", found=("balance",))
    failed_bank = row(
        "interim",
        10,
        role="negative_control",
        failure="convert_timeout",
        identity=None,
        found=(),
        verdict="insurer",
    )
    a = aggregate([row("annual", 30), bank, failed_bank])
    assert a["documents_run"] == {"annual": 2, "interim": 1, "total": 3}
    assert a["corporate_documents_run"] == {"annual": 1, "interim": 0, "total": 1}
    assert a["balance_and_income_found"]["total"] == 1
    assert a["identity"]["ok"]["total"] == 1
    assert a["statements_found"]["balance"]["total"] == 1
    assert a["failures"]["documents_failed"]["total"] == 0 and a["failures"]["by_class"] == {}
    assert a["time"]["succeeded"]["total_s"]["total"] == 50
    controls = a["negative_controls"]
    assert controls["documents_run"]["total"] == 2
    assert controls["documents_failed"]["total"] == 1
    assert controls["by_industry_verdict"]["bank"]["total"] == 1
    assert controls["by_industry_verdict"]["insurer"]["total"] == 1
    assert controls["by_failure_class"]["convert_timeout"]["total"] == 1
    assert controls["statements_found"]["balance"]["total"] == 1


def test_an_empty_stratum_has_zero_counts_and_no_time() -> None:
    a = aggregate([row("annual", 10)])
    assert a["documents_run"] == {"annual": 1, "interim": 0, "total": 1}
    assert a["time"]["succeeded"]["median_s"] == {"annual": 10, "interim": None, "total": 10}


# ---- the checkpoint projection ----------------------------------------------------------------


def test_pool_layers_count_the_records_of_the_projected_pools_only() -> None:
    candidates = [
        {"id": "a", "pool": "model_test"},
        {"id": "b", "pool": "model_test"},
        {"id": "c", "pool": "blind"},
        {"id": "d", "pool": "blind"},
        {"id": "e", "pool": "train"},
    ]
    fetched = {
        k: {"text_layer": v} for k, v in {"a": "digital", "b": "mixed", "c": "mixed"}.items()
    }
    assert pool_layers(candidates, fetched) == {
        "model_test": {"digital": 1, "mixed": 1},
        "blind": {"mixed": 1, UNMEASURED: 1},
    }


def test_the_projection_multiplies_counts_by_the_layers_median_and_p90() -> None:
    rows = [
        row("annual", 30),
        row("annual", 50),
        row("annual", 600, failure="convert_timeout"),  # a failure is no sample
        row("interim", 120, layer="mixed"),
        row("interim", 240, layer="mixed"),
        row("interim", 360, layer="scanned"),
    ]
    layers = {"model_test": {"digital": 6, "mixed": 3}, "blind": {"scanned": 1, UNMEASURED: 2}}
    p = projection(rows, layers)
    assert p["pools"]["model_test"]["layers"]["digital"] == {
        "documents": 6,
        "basis": "own",
        "median_s": 40,
        "p90_s": 50,
    }
    assert p["pools"]["model_test"]["minutes"] == {
        "median": 6 * 40 / 60 + 3 * 180 / 60,
        "p90": 6 * 50 / 60 + 3 * 240 / 60,
    }
    # not sampled as such: the non-digital figures (120, 240, 360), flagged
    unmeasured = p["pools"]["blind"]["layers"][UNMEASURED]
    assert unmeasured["basis"] == "non_digital" and unmeasured["median_s"] == 240
    assert p["pools"]["blind"]["layers"]["scanned"]["basis"] == "own"
    assert p["minutes"]["median"] == round(
        p["pools"]["model_test"]["minutes"]["median"] + p["pools"]["blind"]["minutes"]["median"], 1
    )
    assert p["plan_minutes"] == {
        "low": 167,
        "high": 428,
        "source": "docs/blueprint/08-revised-plan.md",
    }
    assert p["documents_not_projected"] == 0


def test_a_digital_layer_with_no_sample_is_counted_and_not_projected() -> None:
    p = projection([row("interim", 100, layer="scanned")], {"blind": {"digital": 4}})
    assert p["pools"]["blind"]["layers"]["digital"] == {"documents": 4, "basis": None}
    assert p["documents_not_projected"] == 4
    assert p["minutes"] == {"median": 0, "p90": 0}


def test_the_report_carries_the_projection_and_the_timing_path(three: Setup) -> None:
    report = three.run(stub_stages(), layers_by_pool={"blind": {"digital": 2}})
    assert report["projection"]["pools"]["blind"]["documents"] == 2
    assert "cold run" in " ".join(report["timing"].values())
    assert "child process" in report["timing"]["convert"]


# ---- failures, output and exit codes ------------------------------------------------------------


def test_a_crash_records_where_it_happened_but_never_its_message(three: Setup) -> None:
    def locate(pdf: Path, config: IngestConfig) -> LocateResult:
        raise ValueError(f"{LABEL} page text")

    report = three.run(stub_stages(locate=locate))
    row = report["rows"][0]
    assert row["failure"] == "crash:ValueError"
    assert re.fullmatch(r"test_dry_run\.py:\d+", row["failure_at"])
    assert LABEL not in three.report.read_text(encoding="utf-8")


def test_a_failure_from_the_pipeline_has_no_location(three: Setup) -> None:
    def locate(pdf: Path, config: IngestConfig) -> LocateResult:
        raise IngestError("convert_timeout", LABEL)

    assert three.run(stub_stages(locate=locate))["rows"][0]["failure_at"] is None


def test_nothing_of_a_failure_reaches_the_terminal_but_ids(
    three: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    def locate(pdf: Path, config: IngestConfig) -> LocateResult:
        raise IngestError("convert_crashed", f"{LABEL} | {VALUE}")

    three.run(stub_stages(locate=locate))
    seen = capsys.readouterr()
    assert LABEL not in seen.out + seen.err and VALUE not in seen.out + seen.err


def test_convert_seconds_are_summed_when_convert_is_called_twice(three: Setup) -> None:
    now = [0.0]

    def convert(pdf: Path, config: IngestConfig) -> ConvertResult:
        now[0] += 7
        return converted()

    base = stub_stages(convert=convert)

    def structure(
        pdf: Path, config: IngestConfig, convert_stage: Callable[..., ConvertResult]
    ) -> StructureResult:
        convert_stage(pdf, config)
        convert_stage(pdf, config)
        return structured()

    stages = replace(base, structure=structure)
    row = three.run(stages, clock=lambda: now[0])["rows"][0]
    assert row["stage_seconds"]["convert"] == 14.0
    assert row["stage_seconds"]["structure"] == 0.0


def test_the_artifact_root_is_gone_after_each_document_and_when_one_fails(three: Setup) -> None:
    seen: list[tuple[Path, list[bool]]] = []

    def locate(pdf: Path, config: IngestConfig) -> LocateResult:
        seen.append((config.artifact_root, [r.exists() for r, _ in seen]))
        config.artifact_root.mkdir(parents=True)
        (config.artifact_root / "pages.json").write_text("page text", encoding="utf-8")
        if len(seen) == 2:
            raise ValueError("broke after writing")
        return located()

    three.run(stub_stages(locate=locate))
    assert len(seen) == 3
    assert [exists for _, exists in seen][-1] == [False, False]
    assert not three.artifacts.exists()


def test_the_artifact_root_is_gone_when_the_run_is_interrupted(three: Setup) -> None:
    def locate(pdf: Path, config: IngestConfig) -> LocateResult:
        config.artifact_root.mkdir(parents=True)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        three.run(stub_stages(locate=locate))
    assert not three.artifacts.exists()


def test_exit_codes_are_distinct() -> None:
    ok = {"complete": True, "rows": [{"failure": None}, {"failure": "convert_timeout"}]}
    assert exit_code(ok) == 0
    assert exit_code({**ok, "complete": False}) == EXIT_INCOMPLETE
    assert exit_code({"complete": True, "rows": [{"failure": "x"}]}) == EXIT_ALL_FAILED
    assert exit_code({"complete": True, "rows": []}) == EXIT_ALL_FAILED
    assert len({0, 1, 2, EXIT_REFUSED, EXIT_INCOMPLETE, EXIT_ALL_FAILED}) == 6


@pytest.mark.slow
@pytest.mark.golden
def test_the_real_stages_run_one_golden_document(tmp_path: Path) -> None:
    """The real stages, no stub, on the smallest golden document with a text layer (the set
    holds no wholly digital one): a cold run in a fresh artifact root."""
    pdf = GOLDEN / "juhayna-2025-en-standalone.pdf"
    if not pdf.is_file():
        pytest.skip(f"golden document {pdf.name} is not present")
    entry = {"id": "juhayna-2025-en-standalone", "period": "annual", "language": "en"}
    config = load_config().model_copy(update={"artifact_root": tmp_path / "artifacts"})
    stages = real_stages(config)
    structured_here: list[StructureResult] = []

    def structure(
        path: Path, cfg: IngestConfig, convert_stage: Callable[..., ConvertResult]
    ) -> StructureResult:
        structured_here.append(stages.structure(path, cfg, convert_stage))
        return structured_here[-1]

    row = run_document(
        entry, "mixed", pdf, config, replace(stages, structure=structure), time.perf_counter
    )
    assert row["failure"] is None, row["failure"]
    assert {"locate", "convert", "structure"} <= set(row["stage_seconds"])
    assert all(row["stage_seconds"][k] > 0 for k in ("locate", "convert", "structure"))
    assert row["statements"]
    labels = [i.raw_label for s in structured_here[0].statements for i in s.line_items]
    assert labels
    text = json.dumps(row, ensure_ascii=False)
    assert not any(label in text for label in labels if len(label) > 3)


# ---- the candidate and the command ------------------------------------------------------------


def test_candidate_is_the_short_commit_and_marks_a_dirty_tree() -> None:
    def git(args: list[str]) -> str:
        return "abc1234\n" if args[0] == "rev-parse" else ""

    assert candidate(git) == "abc1234"

    def dirty(args: list[str]) -> str:
        return "abc1234\n" if args[0] == "rev-parse" else " M file.py\n"

    assert candidate(dirty) == "abc1234-dirty"

    asked: list[list[str]] = []

    def record(args: list[str]) -> str:
        asked.append(args)
        return "abc1234\n"

    candidate(record)
    assert ["status", "--porcelain"] in asked  # an untracked source file marks the tree dirty


def test_the_command_takes_no_pool_argument() -> None:
    with pytest.raises(SystemExit) as caught:
        main(["--pool", "dev"])
    assert caught.value.code == 2


# ---- industry decision, critical items and metrics --------------------------------------------


def test_a_row_carries_the_industry_decision_critical_item_counts_and_metric_counts(
    three: Setup,
) -> None:
    row = three.run(stub_stages())["rows"][0]
    assert row["industry"] == {
        "outcome": "pass",
        "code": None,
        "label": "pass",
        "judgement": "right",
    }
    taxonomy = load_taxonomy()
    slots = sum(
        len(taxonomy.critical_ids(t)) for t in (StatementType.BALANCE, StatementType.INCOME)
    )
    # the one stub row carries no canonical item and is not flagged: every slot is unmapped
    assert row["critical"] == {
        "mapped": 0,
        "ambiguous": 0,
        "unmapped": slots,
        "statement_not_found": 0,
    }
    assert row["metrics"] == {
        "net_margin": {"attempted": 1, "computed": 1, "null": 0, "flag_classes": []},
        "current_ratio": {
            "attempted": 1,
            "computed": 0,
            "null": 1,
            "flag_classes": ["missing_input"],
        },
    }
    assert row["metrics_failure"] is None and row["metrics_failure_at"] is None
    assert row["critical_failure"] is None and row["critical_failure_at"] is None
    assert list(row["plan_critical"]) == list(PLAN_CRITICAL_IDS)
    assert set(row["plan_critical"].values()) == {"unmapped"}


def test_a_declined_document_is_an_outcome_not_a_failure(three: Setup) -> None:
    report = three.run(stub_stages(result=declined_result()))
    first = report["rows"][0]
    assert first["failure"] is None
    assert first["industry"]["label"] == "declined/bank"
    assert first["critical"] is None and first["metrics"] == {}
    assert first["statements"] == {}
    a = report["aggregates"]
    assert a["failures"]["documents_failed"]["total"] == 0
    assert a["industry"]["declined"]["total"] == 3
    assert a["industry"]["by_code"]["declined/bank"]["total"] == 3
    assert "judgement" not in a["industry"]  # judged against the sector label for controls only
    assert a["critical_items"]["documents"]["total"] == 0
    assert exit_code(report) == 0


def test_a_document_held_for_industry_keeps_its_statements_and_is_counted_held(
    three: Setup,
) -> None:
    report = three.run(stub_stages(result=held_result()))
    first = report["rows"][0]
    assert first["industry"]["label"] == "needs_review/weak_verdict"
    assert first["industry"]["judgement"] == "held"
    assert sorted(first["statements"]) == ["balance", "income"]
    assert first["critical"] is not None
    a = report["aggregates"]
    assert a["industry"]["needs_review"]["total"] == 3
    assert a["industry"]["by_code"]["needs_review/weak_verdict"]["total"] == 3


def test_negative_controls_are_judged_apart_from_the_corporate_headline() -> None:
    right = row("annual", 20, role="negative_control", industry="declined/bank", found=())
    held = row(
        "interim",
        20,
        role="negative_control",
        industry="needs_review/weak_verdict",
        judgement="held",
        found=("balance",),
        critical=CRIT,
    )
    wrong = row("interim", 20, role="negative_control", industry="pass", judgement="wrong")
    a = aggregate([row("annual", 30), right, held, wrong])
    assert a["industry"]["pass"]["total"] == 1  # the corporate document only
    controls = a["negative_controls"]
    assert controls["industry_outcome"]["declined"] == {"annual": 1, "interim": 0, "total": 1}
    assert controls["industry_outcome"]["needs_review"]["total"] == 1
    assert controls["industry_outcome"]["pass"]["total"] == 1
    assert controls["judgement_against_label"] == {
        "right": {"annual": 1, "interim": 0, "total": 1},
        "held": {"annual": 0, "interim": 1, "total": 1},
        "wrong": {"annual": 0, "interim": 1, "total": 1},
    }


def test_critical_items_are_summed_per_stratum_over_documents_that_reached_statements() -> None:
    full = {"mapped": 8, "ambiguous": 1, "unmapped": 2, "statement_not_found": 0}
    none = {"mapped": 0, "ambiguous": 0, "unmapped": 0, "statement_not_found": 11}
    declined = row("interim", 5, industry="declined/bank", judgement="wrong", found=())
    a = aggregate(
        [
            row("annual", 30, critical=full),
            row("interim", 30, critical=none),
            row("interim", 30, critical=full),
            declined,
        ]
    )["critical_items"]
    assert a["documents"] == {"annual": 1, "interim": 2, "total": 3}
    assert a["mapped"] == {"annual": 8, "interim": 8, "total": 16}
    assert a["statement_not_found"] == {"annual": 0, "interim": 11, "total": 11}


def test_metrics_computed_and_null_are_counted_by_id_and_stratum_with_flag_classes() -> None:
    m1 = {"net_margin": {"attempted": 1, "computed": 1, "null": 0, "flag_classes": []}}
    m2 = {
        "net_margin": {"attempted": 2, "computed": 0, "null": 2, "flag_classes": ["missing_input"]}
    }
    a = aggregate([row("annual", 1, metrics=m1), row("interim", 1, metrics=m2)])["metrics"]
    assert a["net_margin"]["computed"] == {"annual": 1, "interim": 0, "total": 1}
    assert a["net_margin"]["null"] == {"annual": 0, "interim": 2, "total": 2}
    assert a["net_margin"]["flag_classes"]["missing_input"] == {
        "annual": 0,
        "interim": 1,
        "total": 1,
    }


def test_the_new_figures_are_given_annual_interim_and_total(three: Setup) -> None:
    report = three.run(stub_stages())
    paths = {p for p, _ in figures(report["aggregates"])}
    assert {
        "/industry/pass",
        "/industry/declined",
        "/industry/needs_review",
        "/industry/by_code/pass",
        "/critical_items/documents",
        "/critical_items/mapped",
        "/critical_items/ambiguous",
        "/critical_items/unmapped",
        "/critical_items/statement_not_found",
        "/metrics/net_margin/computed",
        "/metrics/net_margin/null",
        "/metrics/current_ratio/flag_classes/missing_input",
        "/negative_controls/industry_outcome/pass",
        "/negative_controls/judgement_against_label/wrong",
    } <= paths


@pytest.mark.parametrize("result", [None, declined_result(), held_result()])
def test_the_report_holds_no_decision_reason_evidence_or_metric_value(
    three: Setup, result: StructureResult | None
) -> None:
    three.run(stub_stages(result=result))
    text = three.report.read_text(encoding="utf-8") + "".join(
        three.report.with_name("report.rows.jsonl").read_text(encoding="utf-8")
    )
    assert LABEL not in text and VALUE not in text and "98765" not in text
    assert "page 3" not in text  # the decision's reason and evidence stay out


def test_a_bank_control_that_passes_as_corporate_is_judged_wrong_and_a_declined_one_right(
    tmp_path: Path,
) -> None:
    a, b = HOLDOUT_ISSUERS[:2]
    controls = [
        {**candidate_record(a), "role": "negative_control", "sector": "bank"},
        {**candidate_record(b), "role": "negative_control", "sector": "bank"},
    ]
    setup = Setup(tmp_path, controls)
    declined = setup.run(stub_stages(result=declined_result("bank")))
    assert {r["industry"]["judgement"] for r in declined["rows"]} == {"right"}
    other = Setup(tmp_path / "again", controls)
    passed = other.run(stub_stages())
    assert {r["industry"]["judgement"] for r in passed["rows"]} == {"wrong"}


# ---- a failing metrics or critical-item step is not a failed document -------------------------


def raising(_: Any) -> Any:
    raise ValueError(f"{LABEL} {VALUE} detail")


def test_a_metrics_failure_is_its_own_field_and_the_document_stays_a_success(three: Setup) -> None:
    report = three.run(stub_stages_with(metrics=raising))
    first = report["rows"][0]
    assert first["failure"] is None and first["failure_at"] is None
    assert first["identity"] == "ok"  # set before the metrics stage and kept
    assert first["metrics_failure"] == "crash:ValueError"
    assert re.fullmatch(r"test_dry_run\.py:\d+", first["metrics_failure_at"])
    assert first["metrics"] == {} and first["critical"] is not None
    a = report["aggregates"]
    assert a["time"]["succeeded"]["documents"]["total"] == 3
    assert a["failures"]["documents_failed"]["total"] == 0
    assert a["failures"]["metrics_failed"]["total"] == 3
    assert a["metrics_stage"]["failed"]["total"] == 3 and a["metrics_stage"]["ran"]["total"] == 0
    buckets = ("ok", "failed", "skipped", "no_balance", "failed_document")
    assert sum(a["identity"][b]["total"] for b in buckets) == a["corporate_documents_run"]["total"]
    assert a["identity"]["ok"]["total"] == 3
    assert exit_code(report) == 0


def test_a_critical_item_failure_is_its_own_field_and_metrics_still_run(three: Setup) -> None:
    report = three.run(stub_stages_with(critical=raising))
    first = report["rows"][0]
    assert first["failure"] is None
    assert first["critical_failure"] == "crash:ValueError" and first["critical"] is None
    assert re.fullmatch(r"test_dry_run\.py:\d+", first["critical_failure_at"])
    assert first["plan_critical"] is None
    assert first["metrics"]["net_margin"]["computed"] == 1
    assert report["aggregates"]["failures"]["critical_failed"]["total"] == 3


def test_metrics_and_critical_seconds_are_timed_and_left_out_of_the_ingest_seconds(
    three: Setup,
) -> None:
    now = [0.0]

    def critical(result: StructureResult) -> dict[str, str]:
        now[0] += 3
        return critical_states(result, TAXONOMY, INDEX)

    def metrics(statements: Any) -> list[MetricValue]:
        now[0] += 5
        return metric_values()

    report = three.run(stub_stages_with(critical=critical, metrics=metrics), clock=lambda: now[0])
    first = report["rows"][0]
    assert first["stage_seconds"]["critical"] == 3 and first["stage_seconds"]["metrics"] == 5
    assert first["seconds"] == 8 and first["seconds_excl_metrics"] == 0
    done = report["aggregates"]["time"]
    assert done["succeeded"]["total_s"]["total"] == 24
    assert done["succeeded_excl_metrics"]["total_s"]["total"] == 0
    assert "include the critical-item and metrics steps" in report["timing"]["seconds"]


def test_the_real_stages_load_the_taxonomy_and_policy_before_any_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import harness.dry_run as module

    loaded: list[str] = []
    monkeypatch.setattr(module, "make_engine", lambda config: None)
    monkeypatch.setattr(
        module, "load_taxonomy", lambda: loaded.append("taxonomy") or load_taxonomy()
    )
    monkeypatch.setattr(
        module, "load_policy", lambda path: loaded.append("policy") or load_policy(path)
    )
    stages = real_stages(IngestConfig())
    assert sorted(loaded) == ["policy", "taxonomy"]
    stages.critical(structured())
    assert sorted(loaded) == ["policy", "taxonomy"]  # no load inside the first document


def stub_stages_with(**changes: Any) -> Stages:
    return replace(stub_stages(), **changes)


# ---- metrics: attempted, computed and null ----------------------------------------------------


def test_a_metric_never_attempted_is_told_from_one_computed_zero_times() -> None:
    from fra_analytics.metrics.registry import REGISTRY

    computed_never = {"net_margin": {"attempted": 2, "computed": 0, "null": 2, "flag_classes": []}}
    a = aggregate([row("annual", 1, critical=CRIT, metrics=computed_never)])
    figures = a["metrics"]
    assert set(figures) >= {spec.id for spec in REGISTRY}  # every registry metric is listed
    assert figures["net_margin"]["attempted"]["total"] == 2
    assert figures["net_margin"]["computed"]["total"] == 0
    assert figures["net_margin"]["documents_attempted"]["total"] == 1
    assert figures["net_margin"]["documents_computed"]["total"] == 0
    other = next(spec.id for spec in REGISTRY if spec.id != "net_margin")
    assert figures[other]["attempted"] == {"annual": 0, "interim": 0, "total": 0}
    assert a["metrics_stage"]["ran"]["total"] == 1


def test_metric_availability_is_documents_with_a_value_over_documents_where_metrics_ran() -> None:
    value = {"net_margin": {"attempted": 1, "computed": 1, "null": 0, "flag_classes": []}}
    null = {"net_margin": {"attempted": 1, "computed": 0, "null": 1, "flag_classes": []}}
    rows = [
        row("annual", 1, critical=CRIT, metrics=value),
        row("annual", 1, critical=CRIT, metrics=null),
        row("interim", 1, critical=CRIT, metrics=value, language="ar"),
        row("interim", 1, industry="declined/bank", found=()),  # metrics never ran
    ]
    a = aggregate(rows)
    assert a["metrics"]["net_margin"]["availability"] == {
        "annual": 0.5,
        "interim": 1.0,
        "total": 2 / 3,
    }
    assert a["metrics_stage"]["not_run"]["total"] == 1
    arabic = a["by_language"]["ar"]["metric_availability"]["net_margin"]
    assert arabic == {"annual": None, "interim": 1.0, "total": 1.0}


# ---- the six plan items, holds, languages, controls -------------------------------------------

CRIT = {"mapped": 12, "ambiguous": 0, "unmapped": 0, "statement_not_found": 0}
SIX = dict.fromkeys(PLAN_CRITICAL_IDS, "mapped")
FIVE = {**SIX, "revenue": "ambiguous"}


def test_the_share_of_documents_with_all_six_plan_items_mapped_per_stratum_and_language() -> None:
    rows = [
        row("annual", 1, critical=CRIT, plan=SIX),
        row("annual", 1, critical=CRIT, plan=FIVE, language="ar"),
        row("interim", 1, critical=CRIT, plan=SIX, language="ar"),
        row("interim", 1, industry="declined/bank", found=()),  # declined: not in the share
    ]
    plan = aggregate(rows)["plan_critical_items"]
    assert plan["items"] == list(PLAN_CRITICAL_IDS)
    assert plan["documents"] == {"annual": 2, "interim": 1, "total": 3}
    assert plan["all_mapped"] == {"annual": 1, "interim": 1, "total": 2}
    assert plan["share_all_mapped"] == {"annual": 0.5, "interim": 1.0, "total": 2 / 3}
    assert plan["by_item"]["revenue"]["total"] == 2
    arabic = aggregate(rows)["by_language"]["ar"]["plan_critical_items"]
    assert arabic["all_mapped"] == {"annual": 0, "interim": 1, "total": 1}
    assert arabic["share_all_mapped"]["annual"] == 0.0


def test_critical_items_say_the_declined_documents_are_not_in_them() -> None:
    declined = row("annual", 1, industry="declined/bank", found=())
    a = aggregate([row("annual", 1, critical=CRIT, plan=SIX), declined])
    block = a["critical_items"]
    assert block["documents_declined"] == {"annual": 1, "interim": 0, "total": 1}
    assert block["documents"]["total"] == 1
    assert "not declined" in block["scope"]


def test_held_for_review_is_a_share_by_reason_class_with_the_industry_codes() -> None:
    rows = [
        row("annual", 1, critical=CRIT),
        row("annual", 1, critical=CRIT, reasons=["numbers_missing", "identity_failed"]),
        row(
            "interim",
            1,
            critical=CRIT,
            industry="needs_review/weak_verdict",
            judgement="held",
            language="ar",
        ),
        row("interim", 1, failure="convert_timeout", identity=None, found=()),
    ]
    held = aggregate(rows)["held_for_review"]
    assert held["documents"] == {"annual": 2, "interim": 1, "total": 3}  # the failed one is out
    assert held["held"] == {"annual": 1, "interim": 1, "total": 2}
    assert held["share"] == {"annual": 0.5, "interim": 1.0, "total": 2 / 3}
    assert held["by_reason"]["numbers_missing"]["total"] == 1
    assert held["by_reason"]["industry:weak_verdict"] == {"annual": 0, "interim": 1, "total": 1}
    assert aggregate(rows)["by_language"]["ar"]["held_for_review"]["held"]["total"] == 1


def test_by_language_counts_documents_and_failures_apart_for_each_language() -> None:
    rows = [
        row("annual", 1, language="en"),
        row("annual", 1, language="ar", failure="convert_timeout", identity=None, found=()),
        row("interim", 1, language="ar"),
    ]
    arabic = aggregate(rows)["by_language"]["ar"]
    assert arabic["corporate_documents_run"] == {"annual": 1, "interim": 1, "total": 2}
    assert arabic["documents_failed"] == {"annual": 1, "interim": 0, "total": 1}


def test_the_decline_judgement_is_for_controls_only_and_named_as_against_the_label() -> None:
    a = aggregate(
        [
            row("annual", 1, industry="declined/bank", found=(), judgement="wrong"),
            row("annual", 1, role="negative_control", industry="declined/bank", found=()),
        ]
    )
    assert "judgement" not in a["industry"]
    controls = a["negative_controls"]
    assert controls["judgement_against_label"]["right"]["total"] == 1
    assert "not an accuracy" in controls["judgement_note"]


# ---- the comparison with the first run --------------------------------------------------------


def earlier_row(period: str, language: str, failure: str | None = None) -> dict[str, Any]:
    """A row as the first run wrote it: no industry, critical items or metrics."""
    return {
        "id": "x",
        "period": period,
        "language": language,
        "text_layer": "digital",
        "failure": failure,
        "failure_at": None,
        "seconds": 100.0 if not failure else 5.0,
        "stage_seconds": {"locate": 1.0, "convert": 50.0, "structure": 49.0},
        "stages": {"locate": {"industry": "corporate"}},
        "statements": {} if failure else {"balance": {"review": "passed", "identity": "ok"}},
        "identity": None if failure else "ok",
        "label": LABEL,  # a field this run never copies
    }


def write_earlier(tmp_path: Path) -> Path:
    path = tmp_path / "earlier.json"
    report = {
        "look": {"candidate": "a1b2c3d"},
        "rows": [
            earlier_row("annual", "en"),
            earlier_row("annual", "ar", failure="convert_timeout"),
            earlier_row("interim", "ar"),
        ],
    }
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def test_the_comparison_recomputes_the_first_run_under_the_current_definitions(
    three: Setup, tmp_path: Path
) -> None:
    before = write_earlier(tmp_path)
    text = before.read_text(encoding="utf-8")
    report = three.run(stub_stages(), earlier=before)
    assert before.read_text(encoding="utf-8") == text  # read-only
    comparison = report["comparison"]
    assert comparison["earlier_candidate"] == "a1b2c3d"
    assert comparison["this_candidate"] == LOOK.candidate
    figures = comparison["figures"]
    assert figures["corporate_documents_run"] == {"annual": 2, "interim": 1, "total": 3}
    assert figures["documents_failed"] == {"annual": 1, "interim": 0, "total": 1}
    assert figures["identity"]["failed_document"]["total"] == 1
    assert figures["identity"]["ok"] == {"annual": 1, "interim": 1, "total": 2}
    assert figures["time_succeeded"]["median_s"]["total"] == 100.0
    assert figures["by_language"]["ar"]["documents_failed"]["total"] == 1
    for name in ("industry", "critical_items", "plan_critical_items", "metrics", "held_for_review"):
        assert figures[name] == "not available for run 1"
    said = comparison["differences"]
    for word in ("structure", "locate", "mapping", "industry hold", "timing"):
        assert word in said
    assert LABEL not in three.report.read_text(encoding="utf-8")


def test_a_comparison_is_computed_only_from_fields_the_earlier_rows_have() -> None:
    rows = [{k: v for k, v in earlier_row("annual", "en").items() if k != "statements"}]
    figures = earlier_figures(rows)
    assert figures["balance_and_income_found"] == "not available for run 1"
    assert figures["corporate_documents_run"]["total"] == 1


def test_an_earlier_report_that_is_missing_or_unreadable_is_refused_before_the_look(
    three: Setup, tmp_path: Path
) -> None:
    with pytest.raises(PreflightFailed, match=r"gone\.json"):
        three.run(stub_stages(), earlier=tmp_path / "gone.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    with pytest.raises(PreflightFailed, match="rows"):
        three.run(stub_stages(), earlier=bad)
    assert read_looks(three.log) == []


def test_an_earlier_report_that_cannot_be_compared_is_refused_before_the_look(
    three: Setup, tmp_path: Path
) -> None:
    """The comparison is computed before the look, so a report with the right shape and wrong
    values cannot raise after every document has run."""
    earlier = tmp_path / "earlier.json"
    row = {"id": "x", "period": "annual", "text_layer": "digital", "failure": None}
    earlier.write_text(
        json.dumps({"look": {"candidate": "abc1234"}, "rows": [{**row, "seconds": None}]}),
        encoding="utf-8",
    )
    with pytest.raises(PreflightFailed, match="cannot be compared"):
        three.run(stub_stages(), earlier=earlier)
    assert read_looks(three.log) == []
    assert not three.report.exists()


def test_the_earlier_report_is_an_argument_and_optional() -> None:
    assert parser().parse_args([]).earlier is None
    assert parser().parse_args(["--earlier", "some/report.json"]).earlier == Path(
        "some/report.json"
    )


@pytest.mark.parametrize("failing", ["metrics", "critical"])
def test_nothing_of_a_failing_step_or_the_earlier_report_reaches_the_report(
    three: Setup, tmp_path: Path, failing: str
) -> None:
    three.run(stub_stages_with(**{failing: raising}), earlier=write_earlier(tmp_path))
    text = three.report.read_text(encoding="utf-8") + three.report.with_name(
        "report.rows.jsonl"
    ).read_text(encoding="utf-8")
    assert LABEL not in text and VALUE not in text and "98765" not in text
