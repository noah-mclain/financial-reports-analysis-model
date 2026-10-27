import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from test_pipeline import Calls, pipeline_for, queued

from fra_core.schemas import (
    BBox,
    Cell,
    JobStage,
    JobStatus,
    LineItem,
    MappingSource,
    Period,
    PeriodKind,
    Provenance,
    Statement,
    StatementType,
)
from fra_core.taxonomy import load_taxonomy
from fra_ingest.label_mapping import map_statement
from fra_ingest.label_match import LabelIndex
from fra_ingest.results import StructureResult
from fra_worker.model_mapping import apply_normalization
from fra_worker.pipeline import run_job


class Runtime:
    def __init__(self, response: str | list[str]) -> None:
        self.responses = [response] if isinstance(response, str) else response
        self.calls: list[tuple[str, StatementType]] = []

    def validate(self) -> None:
        pass

    def normalize(self, label: str, statement: StatementType) -> str:
        self.calls.append((label, statement))
        return self.responses[len(self.calls) - 1]


def result(label: str) -> StructureResult:
    s = Statement(
        id="s",
        document_sha256="a" * 64,
        type=StatementType.INCOME,
        currency="USD",
        scale=1,
        periods=[
            Period(key="2025", end_date=date(2025, 12, 31), kind=PeriodKind.DURATION, months=12)
        ],
        line_items=[
            LineItem(
                id="r1",
                raw_label=label,
                cells=[
                    Cell(
                        period_key="2025",
                        raw_text="987654321",
                        reported=Decimal("987654321"),
                        provenance=Provenance(
                            page_no=1,
                            table_ref="#/tables/0",
                            row=1,
                            col=1,
                            bbox=BBox(left=1, top=1, right=2, bottom=2),
                        ),
                    )
                ],
            )
        ],
    )
    return StructureResult(
        version="v", sha256="a" * 64, convert_version="v", settings_hash="h", statements=[s]
    )


def test_supported_synthetic_source_accepts_and_audits() -> None:
    source = result("Revenue")  # deliberately absent prior mapping, independently supported caption
    runtime = Runtime('{"canonical_id":"revenue"}')
    mapped, _checks, audit = apply_normalization(source, [], runtime)
    row = mapped.statements[0].line_items[0]
    assert row.canonical_id == "revenue" and row.mapping_source is MappingSource.MODEL
    assert "model_proposed" in (row.mapping_evidence or "")
    decision = audit[0]["decision"]
    assert isinstance(decision, dict) and decision["state"] == "supported"
    assert runtime.calls == [("Revenue", StatementType.INCOME)]
    assert source.statements[0].line_items[0].canonical_id is None
    assert "987654321" not in json.dumps(audit)


@pytest.mark.parametrize(
    "reply,reason",
    [
        ('{"canonical_id":null}', "model_abstained"),
        ('{"canonical_id":"revenue"}', "model_rejected:"),
    ],
)
def test_unsupported_never_fills(reply: str, reason: str) -> None:
    runtime = Runtime(reply)
    mapped, _, _ = apply_normalization(result("Unfamiliar caption"), [], runtime)
    row = mapped.statements[0].line_items[0]
    assert row.canonical_id is None and row.mapping_source is None
    assert (row.mapping_evidence or "").startswith(reason)


def test_alias_never_called_or_marked_model() -> None:
    source = result("Revenue")
    source.statements[0] = map_statement(source.statements[0], LabelIndex(load_taxonomy()), [])
    runtime = Runtime('{"canonical_id":null}')
    mapped, _, audit = apply_normalization(source, [], runtime)
    assert not runtime.calls and not audit
    assert mapped.statements[0].line_items[0].mapping_source is MappingSource.LEXICON


@pytest.mark.parametrize(
    "response",
    [
        '{"canonical_id":"total_equity_and_liabilities"}',
        '{"canonical_id":"revenue"}',
        "null",
    ],
)
def test_invalid_runtime_response_is_audited_as_rejected(response: str) -> None:
    source = result("Unfamiliar caption")
    source.statements[0].type = StatementType.BALANCE
    source.statements[0].line_items[0].mapping_flag = "ambiguous"
    mapped, checks, audit = apply_normalization(source, [], Runtime(response))
    row = mapped.statements[0].line_items[0]
    assert row.canonical_id is None and row.mapping_source is None
    assert row.mapping_flag == "ambiguous"
    assert row.mapping_evidence == "model_rejected:invalid_model_response"
    assert checks == []
    assert len(audit) == 1 and audit[0]["response"] == response
    decision = audit[0]["decision"]
    assert isinstance(decision, dict) and decision["state"] == "invalid"


def test_invalid_proposal_does_not_block_supported_row_in_same_document() -> None:
    source = result("Revenue")
    source.statements[0].line_items.append(
        source.statements[0]
        .line_items[0]
        .model_copy(update={"id": "r2", "raw_label": "Unfamiliar caption", "canonical_id": None})
    )
    mapped, _checks, audit = apply_normalization(
        source, [], Runtime(['{"canonical_id":"revenue"}', "null"])
    )
    rows = mapped.statements[0].line_items
    assert rows[0].canonical_id == "revenue" and rows[0].mapping_source is MappingSource.MODEL
    assert rows[1].canonical_id is None and rows[1].mapping_source is None
    assert rows[1].mapping_evidence == "model_rejected:invalid_model_response"
    states = []
    for row in audit:
        decision = row["decision"]
        assert isinstance(decision, dict)
        states.append(decision["state"])
    assert states == ["supported", "invalid"]


@pytest.mark.parametrize(
    "response",
    [
        '{"canonical_id":"total_equity_and_liabilities"}',
        '{"canonical_id":"revenue"}',
        "null",
    ],
)
def test_invalid_proposal_does_not_fail_document_job(tmp_path: Path, response: str) -> None:
    store, sha = queued(tmp_path)
    source = result("Unfamiliar caption")
    source.statements[0].type = StatementType.BALANCE
    pipeline = pipeline_for(sha, Calls(), result=source)
    pipeline = replace(
        pipeline,
        mapping=lambda structure: apply_normalization(structure, [], Runtime(response))[0],
    )

    job = store.claim()
    assert job is not None
    finished = run_job(store, job, pipeline, 50)
    assert finished.status is JobStatus.NEEDS_REVIEW
    analysis = store.load_analysis(job.id)
    assert analysis is not None
    row = analysis.statements[0].line_items[0]
    assert row.canonical_id is None and row.mapping_flag == "unmapped"
    assert row.mapping_evidence == "model_rejected:invalid_model_response"


def test_mapping_failure_recorded_on_job(tmp_path: Path) -> None:
    store, sha = queued(tmp_path)
    pipeline = pipeline_for(sha, Calls())

    def fail(source: StructureResult) -> StructureResult:
        raise RuntimeError("normalization server unreachable")

    job = store.claim()
    assert job is not None
    finished = run_job(store, job, replace(pipeline, mapping=fail), 50)
    assert finished.status is JobStatus.FAILED and finished.stage is JobStage.MAPPING
    assert "normalization server unreachable" in (finished.error or "")


def test_existing_failure_and_critical_hold_survive() -> None:
    from fra_core.schemas import CheckResult, StatementReview

    source = result("Unfamiliar caption")
    source.reviews = [
        StatementReview(
            statement_id="s",
            status="needs_review",
            reasons=["critical_item_unmapped:revenue"],
            numeric_cells=1,
            checked_cells=0,
            flagged_cells=0,
        )
    ]
    failed = CheckResult(
        id="tie",
        statement_id="s",
        kind="net_profit_tie",
        period_key="2025",
        status="fail",
        line_item_ids=["r1"],
        detail="failed tie",
    )
    mapped, checked, _ = apply_normalization(source, [failed], Runtime('{"canonical_id":null}'))
    assert failed in checked
    assert "tie_failed" in mapped.reviews[0].reasons
    assert "critical_item_unmapped:revenue" in mapped.reviews[0].reasons
    assert mapped.reviews[0].status == "needs_review"


def test_model_origin_is_visible() -> None:
    from fra_api.views import statement_view

    mapped, _, _ = apply_normalization(result("Revenue"), [], Runtime('{"canonical_id":"revenue"}'))
    view = statement_view(mapped.statements[0], mapped.reviews[0], "a" * 32)
    assert view.rows[0].mapping_origin == "Model proposed; evidence checked"


def test_later_failed_identity_holds_after_acceptance(monkeypatch: pytest.MonkeyPatch) -> None:
    from fra_core.schemas import CheckResult

    source = result("Revenue")
    failed = CheckResult(
        id="late",
        statement_id="s",
        kind="balance_identity",
        period_key="2025",
        status="fail",
        line_item_ids=["r1"],
        detail="failure after acceptance",
    )
    monkeypatch.setattr(
        "fra_worker.model_mapping.run_checks", lambda statement, index: (statement, [failed])
    )
    mapped, checked, _ = apply_normalization(source, [], Runtime('{"canonical_id":"revenue"}'))
    assert mapped.statements[0].line_items[0].mapping_source is MappingSource.MODEL
    assert failed in checked
    assert mapped.reviews[0].status == "needs_review"
    assert "identity_failed" in mapped.reviews[0].reasons


@pytest.mark.parametrize("status", ["pass", "fail", "skipped"])
def test_accepted_mapping_rebuilds_arithmetic_confirmation(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    from fra_core.schemas import CheckResult

    source = result("Revenue")
    before = CheckResult(
        id="identity",
        statement_id="s",
        kind="balance_identity",
        period_key="2025",
        status="pass",
        line_item_ids=["r1"],
    )
    source.statements[0].line_items[0].cells[0].arithmetic_checks = [before.id, "obsolete"]
    after = CheckResult.model_validate({**before.model_dump(), "status": status})
    monkeypatch.setattr(
        "fra_worker.model_mapping.run_checks", lambda statement, index: (statement, [after])
    )
    mapped, checked, _ = apply_normalization(
        source, [before], Runtime('{"canonical_id":"revenue"}')
    )
    assert mapped.statements[0].line_items[0].cells[0].arithmetic_checks == (
        [before.id] if status == "pass" else []
    )
    assert checked[0].status == status
    assert source.statements[0].line_items[0].cells[0].arithmetic_checks == [before.id, "obsolete"]


def test_accepted_mapping_confirms_real_subtotal_and_both_sides_of_tie() -> None:
    source = result("Revenue")
    income = source.statements[0]
    revenue = income.line_items[0]
    revenue.cells[0].reported = Decimal("100")
    revenue.cells[0].raw_text = "100"
    income.line_items += [
        revenue.model_copy(
            update={
                "id": row_id,
                "raw_label": label,
                "canonical_id": canonical,
                "is_subtotal": subtotal,
                "cells": [revenue.cells[0].model_copy(update={"reported": Decimal(value)})],
            }
        )
        for row_id, label, canonical, subtotal, value in (
            ("r2", "Cost of sales", "cost_of_revenue", False, "-40"),
            ("r3", "Gross profit", "gross_profit", True, "60"),
        )
    ]
    comprehensive = income.model_copy(
        update={
            "id": "comprehensive",
            "type": StatementType.COMPREHENSIVE_INCOME,
            "line_items": [income.line_items[-1].model_copy(update={"id": "r1"})],
        }
    )
    source.statements.append(comprehensive)
    mapped, checked, _ = apply_normalization(source, [], Runtime('{"canonical_id":"revenue"}'))
    subtotal = next(c for c in checked if c.kind == "subtotal" and c.status == "pass")
    tie = next(c for c in checked if c.kind == "net_profit_tie" and c.status == "pass")
    rows = mapped.statements[0].line_items
    assert rows[0].cells[0].arithmetic_checks == [subtotal.id]
    assert rows[1].cells[0].arithmetic_checks == [subtotal.id]
    assert rows[2].cells[0].arithmetic_checks == sorted([subtotal.id, tie.id])
    assert mapped.statements[1].line_items[0].cells[0].arithmetic_checks == [tie.id]
    assert all(not row.cells[0].arithmetic_checks for row in income.line_items)


def test_unaccepted_mapping_preserves_arithmetic_confirmation() -> None:
    source = result("Unfamiliar caption")
    source.statements[0].line_items[0].cells[0].arithmetic_checks = ["existing"]
    mapped, _, _ = apply_normalization(source, [], Runtime('{"canonical_id":null}'))
    assert mapped.statements[0].line_items[0].cells[0].arithmetic_checks == ["existing"]


def test_off_records_skipped_mapping_stage(tmp_path: Path) -> None:
    store, sha = queued(tmp_path)
    job = store.claim()
    assert job is not None
    run_job(store, job, pipeline_for(sha, Calls()), 50)
    assert any(
        e.stage is JobStage.MAPPING and e.kind == "finished" and e.detail == "skipped: disabled"
        for e in store.events(job.id)
    )


def test_rechecks_do_not_duplicate_check_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    from fra_core.schemas import CheckResult

    source = result("Revenue")
    before = CheckResult(
        id="eps",
        statement_id="s",
        kind="eps",
        period_key="2025",
        status="fail",
        line_item_ids=["r1"],
        detail="existing failure",
    )
    after = before.model_copy(update={"status": "pass"})
    monkeypatch.setattr(
        "fra_worker.model_mapping.run_checks", lambda statement, index: (statement, [after])
    )
    mapped, checks, _ = apply_normalization(source, [before], Runtime('{"canonical_id":"revenue"}'))
    assert len([c for c in checks if c.id == "eps"]) == 1
    assert checks[0].status == "fail"
    assert "eps_failed" in mapped.reviews[0].reasons


def test_eps_checker_runs_again_after_acceptance(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import Mock

    checker = Mock(return_value=[])
    monkeypatch.setattr("fra_worker.model_mapping.check_eps", checker, raising=False)
    source = result("Revenue")
    apply_normalization(source, [], Runtime('{"canonical_id":"revenue"}'))
    assert checker.call_count == 1


def test_declined_result_does_not_contact_model(tmp_path: Path) -> None:
    from fra_core.normalization import NormalizationSettings
    from fra_ingest.results import IndustryDecision, IndustrySignal
    from fra_worker.model_mapping import map_structure

    source = result("Unfamiliar caption")
    source.industry = IndustryDecision(
        outcome="declined", code="bank", reason="bank", signal=IndustrySignal(kind="bank")
    )
    config = NormalizationSettings(
        enabled=True,
        url="http://127.0.0.1:1",
        model_path=tmp_path / "absent.gguf",
        sha256="0" * 64,
        size_bytes=1,
        timeout_seconds=1,
    )
    assert map_structure(source, tmp_path, config) is source
