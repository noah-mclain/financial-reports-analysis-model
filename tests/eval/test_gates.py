"""Gate A and Gate B figures: per document from the harness's own functions, then per period kind
on a synthetic report."""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from harness import gates as gates_module
from harness.confidence import OUTCOMES
from harness.expected import ExpectedFile, ExpectedRow, ExpectedStatement
from harness.gates import (
    NOT_MEASURED,
    PAIR_TYPES,
    document_figures,
    gate_report,
    identity_outcome,
    lines,
    pair_records,
)
from harness.locate_evidence import DocumentEvidence, PageFingerprint, incomplete_evidence
from harness.reproducibility import record_hash
from test_mapping_harness import INDEX, TAXONOMY, P, item, statement

from fra_core.schemas import CheckResult, Period, PeriodKind, StatementType
from fra_ingest.config import IngestConfig
from fra_ingest.errors import IngestError
from fra_ingest.results import IndustryDecision, IndustrySignal, StructureResult
from fra_ingest.review import StatementReview

TYPES = (StatementType.BALANCE, StatementType.INCOME)
ENTRY = {"id": "d", "language": "en", "scale": 1, "currency": "SAR", "text_layer": "digital"}


def identity(status: str) -> CheckResult:
    return CheckResult(
        id="c",
        statement_id="s",
        kind="balance_identity",
        period_key=P.key,
        status=status,  # type: ignore[arg-type]
        line_item_ids=["r1"],
    )


def review(status: str = "passed") -> StatementReview:
    return StatementReview(
        statement_id="s",
        status=status,  # type: ignore[arg-type]
        numeric_cells=1,
        checked_cells=1,
        flagged_cells=0,
    )


FY = Period(key="FY", end_date=date(2025, 12, 31), kind=PeriodKind.DURATION, months=12)


def structured(*items: Any, reviews: list[StatementReview] | None = None) -> StructureResult:
    return StructureResult(
        version="v",
        sha256="a" * 64,
        convert_version="c",
        settings_hash="h",
        statements=[statement(list(items)).model_copy(update={"periods": [P, FY]})],
        reviews=reviews or [review()],
    )


def test_identity_outcomes_are_ok_accepted_unexplained_skipped_or_no_balance() -> None:
    st = statement([item(1, "Total assets", "5", canonical="total_assets", evidence="alias")])
    assert identity_outcome(st, [identity("pass")], review()) == "ok"
    assert identity_outcome(st, [identity("skipped")], review()) == "skipped"
    assert identity_outcome(st, [identity("fail")], review("needs_review")) == "failed_unexplained"
    assert identity_outcome(None, [], None) == "no_balance"


def test_a_failed_identity_a_cell_explains_on_a_held_statement_is_accepted() -> None:
    row = item(1, "Total assets", "5", canonical="total_assets", evidence="alias")
    flagged = row.model_copy(
        update={"cells": [row.cells[0].model_copy(update={"flags": ["digit_suspect"]})]}
    )
    st = statement([flagged])
    assert identity_outcome(st, [identity("fail")], review("needs_review")) == "failed_accepted"
    assert identity_outcome(st, [identity("fail")], review("passed")) == "failed_unexplained"


def test_a_document_carries_identity_scale_currency_and_critical_slots() -> None:
    result = structured(item(1, "Total assets", "5", canonical="total_assets", evidence="alias"))
    figures = document_figures(
        ENTRY, result, {"s": [identity("pass")]}, None, INDEX, TAXONOMY, TYPES
    )
    assert figures["period_kind"] == "annual"
    assert figures["identity"] == "ok"
    # two statement types were expected, the income statement was not found: it is counted
    assert figures["scale_currency"] == {"expected": 2, "found": 1, "right": 1}
    assert figures["mapping"]["mapped"] == 1
    assert figures["mapping"]["statement_not_found"] >= 1  # no income statement
    assert figures["figures"] == []


def test_a_wrong_scale_is_counted_and_a_flagged_unsure_scale_is_not() -> None:
    result = structured(item(1, "Total assets", "5", canonical="total_assets", evidence="alias"))
    wrong = document_figures({**ENTRY, "scale": 1000}, result, {}, None, INDEX, TAXONOMY, TYPES)
    assert wrong["scale_currency"] == {"expected": 2, "found": 1, "right": 0}


def test_expected_file_cells_are_scored_per_status_and_page_mode() -> None:
    result = structured(item(1, "Total assets", "5", canonical="total_assets", evidence="alias"))
    expected = ExpectedFile(
        id="d",
        sha256="b" * 64,
        status="checked",
        checked_by=["a", "b"],
        statements=[
            ExpectedStatement(
                type=StatementType.BALANCE,
                pages=[1],
                page_mode="digital",
                scale=1,
                currency="SAR",
                periods=[P],
                rows=[ExpectedRow(label="Total assets", values={P.key: Decimal("5")})],
            )
        ],
    )
    figures = document_figures(ENTRY, result, {}, expected, INDEX, TAXONOMY, TYPES)["figures"]
    assert [(f["status"], f["page_mode"], f["cells"], f["right"]) for f in figures] == [
        ("checked", "digital", 1, 1)
    ]


def doc(
    doc_id: str,
    period: str,
    *,
    identity: str = "ok",
    mapping: dict[str, int] | None = None,
    figures: list[dict[str, Any]] | None = None,
    scale: tuple[int, int, int] = (2, 2, 2),
) -> dict[str, Any]:
    return {
        "id": doc_id,
        "language": "en",
        "period_kind": period,
        "identity": identity,
        "scale_currency": {"expected": scale[0], "found": scale[1], "right": scale[2]},
        "mapping": mapping or {"mapped": 6},
        "disagreements": [],
        # A figure given without outcomes has none of interest to the test: all zero.
        "figures": [
            {"outcomes": dict.fromkeys(OUTCOMES, 0), "confident_wrong": [], **f}
            for f in figures or []
        ],
    }


def test_every_figure_is_per_period_kind_beside_the_total_and_marked_not_measured() -> None:
    report = gate_report(
        [doc("a", "annual"), doc("b", "annual", identity="failed_unexplained")], pairs=[]
    )
    assert set(report["strata"]) == {"annual", "interim", "unknown", "total"}
    annual, total = report["strata"]["annual"], report["strata"]["total"]
    assert annual["documents"] == 2 and total["documents"] == 2
    assert annual["gate_a"]["identity"]["counts"] == {"ok": 1, "failed_unexplained": 1}
    assert annual["gate_a"]["identity"]["met"] is False
    for stratum in report["strata"].values():
        assert stratum["measured_on"] == NOT_MEASURED


def test_an_empty_stratum_is_named_not_filled() -> None:
    report = gate_report([doc("a", "annual")], pairs=[])
    assert report["empty_strata"] == ["interim", "unknown"]
    assert report["strata"]["interim"]["documents"] == 0
    assert report["strata"]["interim"]["gate_a"]["identity"]["met"] is None


def test_an_unknown_period_kind_is_its_own_stratum_and_counted_in_the_total() -> None:
    report = gate_report([doc("a", "annual"), doc("u", "unknown")], pairs=[])
    assert report["strata"]["unknown"]["documents"] == 1
    assert report["strata"]["total"]["documents"] == 2


def test_gate_b_needs_every_slot_mapped_or_flagged_and_no_disagreement() -> None:
    good = gate_report(
        [doc("a", "annual", mapping={"mapped": 5, "ambiguous": 1, "verified": 2})], pairs=[]
    )["strata"]["annual"]["gate_b"]
    assert good["met"] is True and good["slots"] == 6
    bad = gate_report(
        [doc("a", "annual", mapping={"mapped": 4, "absent": 1, "statement_not_found": 1})],
        pairs=[],
    )["strata"]["annual"]["gate_b"]
    assert bad["met"] is False and bad["not_accounted"] == 2
    wrong = gate_report([doc("a", "annual", mapping={"mapped": 6, "disagrees": 1})], pairs=[])[
        "strata"
    ]["annual"]["gate_b"]
    assert wrong["met"] is False and wrong["disagrees"] == 1


def test_gate_a_scale_and_currency_must_be_right_on_every_statement() -> None:
    report = gate_report([doc("a", "annual", scale=(3, 3, 2))], pairs=[])
    block = report["strata"]["annual"]["gate_a"]["scale_currency"]
    assert block == {"expected": 3, "found": 3, "not_found": 0, "right": 2, "met": False}


def test_gate_a_figures_are_judged_against_the_g1_threshold_only_when_checked() -> None:
    def fig(status: str, mode: str, cells: int, right: int) -> dict[str, Any]:
        return {"status": status, "page_mode": mode, "cells": cells, "right": right}

    report = gate_report(
        [
            doc("a", "annual", figures=[fig("checked", "digital", 200, 199)]),
            doc("b", "annual", figures=[fig("draft", "digital", 10, 1)]),
        ],
        pairs=[],
    )
    figures = report["strata"]["annual"]["gate_a"]["figures"]
    assert figures["checked"]["digital"] == {
        "files": 1,
        "cells": 200,
        "right": 199,
        "share": 0.995,
        "met": True,
    }
    assert figures["draft"]["digital"]["met"] is None  # provisional, not a gate
    none = gate_report([doc("a", "annual")], pairs=[])["strata"]["annual"]["gate_a"]["figures"]
    assert none["checked"] == {}


def test_pairs_are_gated_per_the_period_kind_of_their_documents() -> None:
    pairs = [
        {
            "pair": "x/y",
            "type": "balance",
            "gated": True,
            "misses": [0, 0],
            "period_kind": "annual",
        },
        {"pair": "x/y", "type": "income", "gated": True, "misses": [1, 0], "period_kind": "annual"},
        {
            "pair": "p/q",
            "type": "balance",
            "gated": False,
            "misses": [4, 4],
            "period_kind": "annual",
        },
    ]
    block = gate_report([doc("a", "annual")], pairs=pairs)["strata"]["annual"]["gate_a"]["pairs"]
    assert block == {"gated": 2, "identical": 1, "ungated": 1, "met": False}


# ---- empty measures, statements not found, unreadable documents --------------------------------


def test_a_statement_not_found_is_counted_and_makes_scale_and_currency_not_met() -> None:
    # 12 documents x 3 types were expected; every statement found is right, 7 were not found
    report = gate_report([doc("a", "annual", scale=(36, 29, 29))], pairs=[])
    block = report["strata"]["annual"]["gate_a"]["scale_currency"]
    assert block["not_found"] == 7 and block["met"] is False


def test_scale_and_currency_with_nothing_expected_is_not_measured() -> None:
    empty = gate_report([doc("a", "annual", scale=(0, 0, 0))], pairs=[])
    assert empty["strata"]["annual"]["gate_a"]["scale_currency"]["met"] is None
    none = gate_report([], pairs=[])
    assert none["strata"]["total"]["gate_a"]["scale_currency"]["met"] is None


def test_figures_with_no_cells_are_not_measured_not_met() -> None:
    fig = {"status": "checked", "page_mode": "digital", "cells": 0, "right": 0}
    report = gate_report([doc("a", "annual", figures=[fig])], pairs=[])
    block = report["strata"]["annual"]["gate_a"]["figures"]["checked"]["digital"]
    assert block["share"] is None and block["met"] is None


def test_an_unreadable_document_makes_every_part_not_met_and_is_listed() -> None:
    fig = {"status": "checked", "page_mode": "digital", "cells": 200, "right": 200}
    pairs = [
        {"pair": "x/y", "type": "balance", "gated": True, "misses": [0, 0], "period_kind": "annual"}
    ]
    report = gate_report(
        [doc("a", "annual", figures=[fig])], pairs=pairs, errors=["gone: unreadable_pdf"]
    )
    for name in ("annual", "total"):
        s = report["strata"][name]
        assert s["unreadable"] == ["gone: unreadable_pdf"]
        a = s["gate_a"]
        assert a["figures"]["checked"]["digital"]["met"] is False
        assert a["pairs"]["met"] is False and a["identity"]["met"] is False
        assert a["scale_currency"]["met"] is False and s["gate_b"]["met"] is False
    printed = "\n".join(lines(report))
    assert "gone: unreadable_pdf" in printed


@pytest.mark.parametrize("unreadable", [False, True])
@pytest.mark.parametrize("cells", [0, 1])
@pytest.mark.parametrize(
    "status,mode,language,eligible",
    [
        ("checked", "scanned", "ar", True),
        ("unreferenced", "scanned", "ar", True),
        ("draft", "scanned", "ar", False),
        ("checked", "digital", "ar", False),
        ("checked", "scanned", "en", False),
    ],
)
def test_unreadable_override_applies_to_eligible_confidence_blocks_even_without_cells(
    unreadable: bool, cells: int, status: str, mode: str, language: str, eligible: bool
) -> None:
    # Unreferenced output has coverage but no expected-cell outcomes.
    figures = (
        [
            {
                "status": status,
                "page_mode": mode,
                "cells": cells,
                "right": cells,
                "outcomes": {"right_accepted": cells},
            }
        ]
        if status != "unreferenced"
        else []
    )
    document = {**doc("a", "annual", figures=figures), "language": language}
    if status == "unreferenced":
        document["output_coverage"] = [
            {"status": status, "page_mode": mode, "unassessed_numeric": 1}
        ]
    errors = ["gone: unreadable_pdf"] if unreadable else []
    report = gate_report([document], pairs=[], errors=errors)
    met = None
    if eligible:
        met = False if unreadable else True if cells and status == "checked" else None
    for stratum in ("annual", "total"):
        block = report["strata"][stratum]["gate_a"]["confident"][f"{status} {mode} {language}"]
        assert block["met"] is met
        assert block["cells"] == (0 if status == "unreferenced" else cells)
        assert block["unassessed_confident"] == 0


# ---- gate B: an item no row carries is absent, whatever else the statement flags --------------


def test_an_item_no_row_carries_is_absent_even_when_other_rows_are_flagged_unmapped() -> None:
    """The mapper flags rows, not missing items, so an unmapped row elsewhere in the statement
    does not make a missing critical item explicitly flagged."""
    carried = item(1, "Total assets", "5", canonical="total_assets", evidence="alias")
    silent = document_figures(
        ENTRY, structured(carried), {}, None, INDEX, TAXONOMY, (StatementType.BALANCE,)
    )["mapping"]
    mystery = item(2, "Mystery", "1", flag="unmapped", evidence="no_alias_no_anchor")
    flagged = document_figures(
        ENTRY, structured(carried, mystery), {}, None, INDEX, TAXONOMY, (StatementType.BALANCE,)
    )["mapping"]
    assert silent["unmapped"] == flagged["unmapped"] > 0
    assert silent["absent"] == silent["unmapped"]
    assert flagged["absent"] == flagged["unmapped"]


def test_gate_b_is_not_met_with_an_absent_slot() -> None:
    whole = gate_report([doc("a", "annual", mapping={"mapped": 6})], [])
    assert whole["strata"]["annual"]["gate_b"]["met"] is True
    absent = gate_report([doc("a", "annual", mapping={"mapped": 5, "absent": 1})], [])
    assert absent["strata"]["annual"]["gate_b"]["met"] is False


def test_the_printout_says_zero_disagree_applies_only_to_slots_with_a_verdict() -> None:
    mapping = {"mapped": 6, "verified": 1, "consistent": 2, "no_verdict": 3}
    printed = "\n".join(lines(gate_report([doc("a", "annual", mapping=mapping)], [])))
    assert "verified 1, consistent with the lexicon 2, no verdict 3" in printed
    assert "disagree" in printed and "3 of the 6 mapped slots have a verdict" in printed
    assert "ABSENT" in printed


# ---- figures are cell shares over files, identity is explained by rule ------------------------


def test_every_figure_line_gives_the_file_count_an_interval_and_calls_itself_a_cell_share() -> None:
    def fig(cells: int, right: int) -> dict[str, Any]:
        return {"status": "checked", "page_mode": "digital", "cells": cells, "right": right}

    report = gate_report(
        [doc("a", "annual", figures=[fig(100, 100)]), doc("b", "annual", figures=[fig(100, 99)])],
        [],
    )
    printed = "\n".join(lines(report))
    assert "cell share, n=2 files" in printed
    assert "95% Wilson" in printed
    assert "99.5" not in printed.split("[annual]")[0]  # no threshold copied into the prose


def test_identity_is_printed_as_explained_by_rule_not_reviewed_and_counts_are_plain() -> None:
    docs = [
        doc("a", "annual"),
        doc("b", "annual", identity="failed_accepted"),
        doc("c", "annual", identity="skipped"),
        doc("d", "annual", identity="no_balance"),
    ]
    printed = "\n".join(lines(gate_report(docs, [])))
    assert "explained by rule, not reviewed by a person" in printed
    assert "2 of 4 documents hold or are explained" in printed
    assert "1 skipped" in printed and "1 with no balance sheet" in printed


# ---- pairs -------------------------------------------------------------------------------------


def test_pairs_are_gated_over_the_two_statement_types_the_gate_names() -> None:
    assert PAIR_TYPES == (StatementType.BALANCE, StatementType.INCOME)
    a = {StatementType.BALANCE: statement([item(1, "Total assets", "5")])}
    records = pair_records({"x": a, "y": a}, {"x": "annual"}, pairs=(("x", "y", True),))
    assert [r["type"] for r in records] == ["balance", "income"]
    assert records[0]["misses"] == [0, 0] and records[1]["misses"] is None  # income not found
    assert {r["period_kind"] for r in records} == {"annual"}
    assert pair_records({}, {}, pairs=(("x", "y", True),))[0]["period_kind"] == "unknown"


def test_the_unknown_stratum_line_is_always_printed() -> None:
    printed = "\n".join(lines(gate_report([doc("a", "annual")], [])))
    assert "[unknown] 0 documents" in printed


def test_comparison_summary_prints_changed_pages_and_ranges() -> None:
    report = gate_report([doc("a", "annual")], [])
    report["documents"] = [
        {
            "id": "a",
            "comparison": {
                "status": "inputs changed",
                "changed_pages": [60],
                "ranges_changed": True,
                "lost_ranges": [{"type": "balance", "first_page": 60, "last_page": 60}],
                "gained_ranges": [],
                "conversion_plan_changed": False,
            },
        }
    ]
    printed = "\n".join(lines(report))
    assert "locate evidence a: inputs changed; pages [60]" in printed
    assert "lost [{'type': 'balance', 'first_page': 60, 'last_page': 60}]; gained []" in printed


@pytest.mark.parametrize("engine", [object(), None])
def test_golden_gate_run_builds_one_configured_engine_and_reuses_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: object | None
) -> None:
    config = IngestConfig(artifact_root=tmp_path / "artifacts")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        "documents:\n  - {id: first, file: first.pdf}\n  - {id: second, file: second.pdf}\n",
        encoding="utf-8",
    )
    factory_configs: list[IngestConfig] = []
    calls: list[tuple[Path, object | None]] = []

    def make_engine(actual_config: IngestConfig) -> object | None:
        factory_configs.append(actual_config)
        return engine

    def structure_pdf(
        pdf: Path, _actual_config: IngestConfig, actual_engine: object | None
    ) -> StructureResult:
        calls.append((pdf, actual_engine))
        raise IngestError("unreadable_pdf", "test")

    monkeypatch.setattr(gates_module, "load_config", lambda: config)
    monkeypatch.setattr(gates_module, "make_engine", make_engine, raising=False)
    monkeypatch.setattr(gates_module, "MANIFEST", manifest)
    monkeypatch.setattr(gates_module, "OUT", tmp_path / "out")
    monkeypatch.setattr(gates_module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(gates_module, "structure_pdf", structure_pdf)

    assert gates_module.main([]) == 1
    assert factory_configs == [config]
    assert [call[0].name for call in calls] == ["first.pdf", "second.pdf"]
    assert all(call[1] is engine for call in calls)


def test_gate_writes_requested_output_and_fails_on_incomplete_locate_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = IngestConfig(artifact_root=tmp_path / "artifacts")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        "documents:\n  - {id: first, file: first.pdf, language: en, scale: 1, currency: SAR, text_layer: digital}\n",
        encoding="utf-8",
    )
    output = tmp_path / "custom" / "report.json"
    monkeypatch.setattr(gates_module, "load_config", lambda: config)
    monkeypatch.setattr(gates_module, "make_engine", lambda _config: None, raising=False)
    monkeypatch.setattr(gates_module, "MANIFEST", manifest)
    monkeypatch.setattr(gates_module, "EXPECTED_DIR", tmp_path / "expected")
    monkeypatch.setattr(gates_module, "structure_pdf", lambda *_args: structured())
    monkeypatch.setattr(gates_module, "load_checks", lambda _path: {})
    monkeypatch.setattr(
        gates_module,
        "document_evidence",
        lambda _path, *, converted: DocumentEvidence(
            status="incomplete",
            conversion="read" if converted else "not_run",
            reason="missing cache",
            page_count=None,
            pages_sha256=None,
            pages=(),
            locate_version=None,
            ranges=(),
            conversion_spans=(),
            conversion_settings_hash=None,
            ocr_key=None,
        ),
    )

    assert gates_module.main(["--output", str(output)]) == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["evidence_schema_version"] == 2
    assert report["documents"][0]["locate_evidence"]["status"] == "incomplete"


def test_gate_figures_ignore_locate_comparison_fields() -> None:
    original = doc("a", "annual", scale=(2, 2, 1))
    extended = {
        **original,
        "locate_evidence": {"status": "complete", "ocr_key": "pages-key"},
        "comparison": {"status": "conversion settings hash changed"},
    }
    assert gate_report([extended], []) == gate_report([original], [])


def test_compare_with_reports_legacy_missing_and_absent_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = IngestConfig(artifact_root=tmp_path / "artifacts")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        "documents:\n"
        "  - {id: legacy, file: a.pdf, language: en, scale: 1, currency: SAR, text_layer: digital}\n"
        "  - {id: comparable, file: b.pdf, language: en, scale: 1, currency: SAR, text_layer: digital}\n"
        "  - {id: absent, file: c.pdf, language: en, scale: 1, currency: SAR, text_layer: digital}\n",
        encoding="utf-8",
    )
    page_hash = "a" * 64
    evidence = DocumentEvidence(
        status="complete",
        reason=None,
        page_count=1,
        pages_sha256=record_hash([page_hash]),
        pages=(PageFingerprint(page_no=1, sha256=page_hash),),
        locate_version="7",
        ranges=(),
        conversion="read",
        conversion_spans=(),
        conversion_settings_hash="b" * 64,
        ocr_key="c" * 64,
    )
    earlier = tmp_path / "earlier.json"
    earlier.write_text(
        json.dumps(
            {
                "documents": [
                    {"id": "legacy"},
                    {"id": "comparable", "locate_evidence": evidence.as_dict()},
                ]
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "report.json"
    monkeypatch.setattr(gates_module, "load_config", lambda: config)
    monkeypatch.setattr(gates_module, "make_engine", lambda _config: None, raising=False)
    monkeypatch.setattr(gates_module, "MANIFEST", manifest)
    monkeypatch.setattr(gates_module, "EXPECTED_DIR", tmp_path / "expected")
    monkeypatch.setattr(gates_module, "OUT", tmp_path)
    monkeypatch.setattr(gates_module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(gates_module, "structure_pdf", lambda *_args: structured())
    monkeypatch.setattr(gates_module, "load_checks", lambda _path: {})
    monkeypatch.setattr(gates_module, "document_evidence", lambda _path, *, converted: evidence)

    assert gates_module.main(["--compare-with", str(earlier), "--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    comparisons = {item["id"]: item["comparison"] for item in report["documents"]}
    assert comparisons["legacy"] == {
        "status": "not comparable",
        "reason": "no locate_evidence in the earlier report",
    }
    assert comparisons["comparable"]["status"] == "unchanged"
    assert comparisons["absent"]["reason"] == "document id absent from earlier report"
    assert "wrote report.json" in capsys.readouterr().out


def test_malformed_compare_with_fails_before_loading_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    malformed = tmp_path / "bad-report.json"
    malformed.write_text("{", encoding="utf-8")
    monkeypatch.setattr(gates_module, "load_config", lambda: pytest.fail("work started"))
    with pytest.raises(SystemExit, match=f"cannot read comparison report {malformed}"):
        gates_module.main(["--compare-with", str(malformed)])


def test_explicit_findings_keep_slot_denominator_and_missing_statements_blocking() -> None:
    mapping = {
        "mapped": 56,
        "ambiguous": 9,
        "explicitly_flagged": 55,
        "statement_not_found": 24,
        "absent": 0,
        "no_verdict": 36,
    }
    report = gate_report([doc("synthetic", "annual", mapping=mapping)], pairs=[])
    gate = report["strata"]["total"]["gate_b"]
    assert gate["slots"] == 144
    assert gate["mapped"] == 56 and gate["ambiguous"] == 9 and gate["no_verdict"] == 36
    assert gate["explicitly_flagged"] == 55 and gate["not_accounted"] == 24
    assert gate["met"] is False
    assert "explicitly flagged 55" in "\n".join(lines(report))
    complete = {"explicitly_flagged": 6}
    assert (
        gate_report([doc("synthetic", "annual", mapping=complete)], pairs=[])["strata"]["total"][
            "gate_b"
        ]["met"]
        is True
    )
    for blockers, errors in [({"disagrees": 1}, []), ({"unmapped": 1}, []), ({}, ["unreadable"])]:
        blocked = gate_report(
            [doc("synthetic", "annual", mapping={**complete, **blockers})], pairs=[], errors=errors
        )
        assert blocked["strata"]["total"]["gate_b"]["met"] is False


def test_rejected_remote_balance_cannot_receive_statement_or_gate_credit() -> None:
    from support import HEADER, INCOME_1, PAGE_1, PAGE_2, page, span, structured_source

    from fra_ingest.structure import structure_document

    weak = [HEADER, ["Cash", "", "5", "4"], ["Inventories", "", "10", "8"]]
    scores = [
        page(5),
        page(6, StatementType.INCOME),
        page(7, StatementType.COMPREHENSIVE_INCOME),
        page(60, StatementType.BALANCE),
    ]
    scores[0].cue_types = [StatementType.BALANCE]
    scores[0].type_scores = {StatementType.BALANCE: 8}
    base = structured_source(
        [(5, weak), (6, INCOME_1), (60, [*PAGE_1, *PAGE_2[1:]])],
        [
            span(StatementType.BALANCE, 5),
            span(StatementType.INCOME, 6),
            span(StatementType.COMPREHENSIVE_INCOME, 7),
            span(StatementType.BALANCE, 60, rank=2),
        ],
        scores,
    )
    result, checks = structure_document(base, IngestConfig())
    by_statement = {s.id: [c for c in checks if c.statement_id == s.id] for s in result.statements}
    figures = document_figures(
        {**ENTRY, "scale": 1000},
        result,
        by_statement,
        None,
        INDEX,
        TAXONOMY,
        IngestConfig().enabled_types,
    )
    assert figures["identity"] == "no_balance"
    assert figures["scale_currency"] == {"expected": 3, "found": 1, "right": 1}
    assert figures["mapping"]["statement_not_found"] == 8
    from harness.mapping import SLOT_STATES

    assert sum(figures["mapping"].get(state, 0) for state in SLOT_STATES) == 12


def test_a_declined_document_is_recorded_without_conversion_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = IngestConfig(artifact_root=tmp_path / "artifacts")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        "documents:\n"
        "  - {id: bank, file: a.pdf, language: en, scale: 1, currency: SAR, text_layer: digital}\n"
        "  - {id: corporate, file: b.pdf, language: en, scale: 1, currency: SAR, text_layer: digital}\n",
        encoding="utf-8",
    )
    declined = StructureResult(
        version="v",
        sha256="b" * 64,
        convert_version="",
        settings_hash="",
        industry=IndustryDecision(
            outcome="declined", code="bank", signal=IndustrySignal(kind="bank"), reason="a bank"
        ),
    )
    held = structured().model_copy(
        update={
            "industry": declined.industry.model_copy(  # type: ignore[union-attr]
                update={"outcome": "needs_review", "code": "weak_verdict"}
            )
        }
    )
    results = iter([declined, held])
    flags: list[bool] = []

    def evidence_for(_path: Path, *, converted: bool) -> DocumentEvidence:
        flags.append(converted)
        page_hash = "a" * 64
        return DocumentEvidence(
            status="complete",
            reason=None,
            page_count=1,
            pages_sha256=record_hash([page_hash]),
            pages=(PageFingerprint(page_no=1, sha256=page_hash),),
            locate_version="7",
            ranges=(),
            conversion="read" if converted else "not_run",
            conversion_spans=(),
            conversion_settings_hash="b" * 64 if converted else None,
            ocr_key="c" * 64,
        )

    output = tmp_path / "report.json"
    monkeypatch.setattr(gates_module, "load_config", lambda: config)
    monkeypatch.setattr(gates_module, "make_engine", lambda _config: None, raising=False)
    monkeypatch.setattr(gates_module, "MANIFEST", manifest)
    monkeypatch.setattr(gates_module, "EXPECTED_DIR", tmp_path / "expected")
    monkeypatch.setattr(gates_module, "OUT", tmp_path)
    monkeypatch.setattr(gates_module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(gates_module, "structure_pdf", lambda *_args: next(results))
    monkeypatch.setattr(gates_module, "load_checks", lambda _path: {})
    monkeypatch.setattr(gates_module, "document_evidence", evidence_for)

    assert gates_module.main(["--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    recorded = {item["id"]: item["locate_evidence"]["conversion"] for item in report["documents"]}
    assert recorded == {"bank": "not_run", "corporate": "read"}
    assert flags == [False, True]


def _complete_evidence() -> DocumentEvidence:
    page_hash = "a" * 64
    return DocumentEvidence(
        status="complete",
        reason=None,
        page_count=1,
        pages_sha256=record_hash([page_hash]),
        pages=(PageFingerprint(page_no=1, sha256=page_hash),),
        locate_version="7",
        ranges=(),
        conversion="read",
        conversion_spans=(),
        conversion_settings_hash="b" * 64,
        ocr_key="c" * 64,
    )


def _stub_gate_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ids: tuple[str, ...],
    failing: tuple[str, ...] = (),
) -> Path:
    """Run main over `ids` with the engine, the reads and the evidence stubbed. A document in
    `failing` raises an ingest error instead of being read. Returns the report path."""
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        "documents:\n"
        + "".join(
            f"  - {{id: {doc_id}, file: {doc_id}.pdf, language: en, scale: 1, currency: SAR, "
            "text_layer: digital}\n"
            for doc_id in ids
        ),
        encoding="utf-8",
    )

    def structure_pdf(pdf: Path, *_args: object) -> StructureResult:
        if pdf.stem in failing:
            raise IngestError("unreadable_pdf", "test")
        return structured()

    monkeypatch.setattr(
        gates_module, "load_config", lambda: IngestConfig(artifact_root=tmp_path / "artifacts")
    )
    monkeypatch.setattr(gates_module, "make_engine", lambda _config: None, raising=False)
    monkeypatch.setattr(gates_module, "MANIFEST", manifest)
    monkeypatch.setattr(gates_module, "EXPECTED_DIR", tmp_path / "expected")
    monkeypatch.setattr(gates_module, "OUT", tmp_path)
    monkeypatch.setattr(gates_module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(gates_module, "structure_pdf", structure_pdf)
    monkeypatch.setattr(gates_module, "load_checks", lambda _path: {})
    monkeypatch.setattr(
        gates_module, "document_evidence", lambda _path, *, converted: _complete_evidence()
    )
    return tmp_path / "report.json"


def test_a_document_that_fails_to_read_fails_the_run_and_is_recorded_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _stub_gate_run(tmp_path, monkeypatch, ("bad", "good"), failing=("bad",))

    assert gates_module.main(["--output", str(output)]) == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    [failed] = report["errored"]
    assert failed["id"] == "bad"
    assert failed["locate_evidence"]["status"] == "incomplete"
    assert failed["locate_evidence"]["reason"] == "document failed: unreadable_pdf"
    assert [item["id"] for item in report["documents"]] == ["good"]


def test_a_failed_document_is_not_comparable_when_compared_with_an_earlier_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    earlier = tmp_path / "earlier.json"
    earlier.write_text(json.dumps({"documents": [{"id": "bad"}, {"id": "good"}]}), encoding="utf-8")
    output = _stub_gate_run(tmp_path, monkeypatch, ("bad", "good"), failing=("bad",))

    assert gates_module.main(["--compare-with", str(earlier), "--output", str(output)]) == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["errored"][0]["comparison"] == {
        "status": "not comparable",
        "reason": "document failed in the current run",
    }


def test_a_document_missing_from_the_current_run_is_listed_as_removed_and_does_not_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    complete = _complete_evidence().as_dict()
    earlier = tmp_path / "earlier.json"
    earlier.write_text(
        json.dumps(
            {
                "documents": [
                    {"id": "a", "locate_evidence": complete},
                    {"id": "b", "locate_evidence": complete},
                    {"id": "gone", "locate_evidence": complete},
                ]
            }
        ),
        encoding="utf-8",
    )
    output = _stub_gate_run(tmp_path, monkeypatch, ("a", "b"))

    assert gates_module.main(["--compare-with", str(earlier), "--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["removed_documents"] == [
        {
            "id": "gone",
            "comparison": {
                "status": "not comparable",
                "reason": "document id absent from the current run",
            },
        }
    ]


def test_removed_documents_are_listed_in_sorted_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    earlier = tmp_path / "earlier.json"
    earlier.write_text(
        json.dumps({"documents": [{"id": "zeta"}, {"id": "b"}, {"id": "alpha"}]}), encoding="utf-8"
    )
    output = _stub_gate_run(tmp_path, monkeypatch, ("b",))

    assert gates_module.main(["--compare-with", str(earlier), "--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert [item["id"] for item in report["removed_documents"]] == ["alpha", "zeta"]


def test_a_run_with_every_document_read_exits_zero_and_has_no_removed_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _stub_gate_run(tmp_path, monkeypatch, ("a", "b"))

    assert gates_module.main(["--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["errored"] == []
    assert "removed_documents" not in report


def test_an_earlier_report_on_another_evidence_schema_is_not_compared_field_by_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    complete = _complete_evidence().as_dict()
    earlier = tmp_path / "earlier.json"
    earlier.write_text(
        json.dumps(
            {
                "evidence_schema_version": 1,
                "documents": [
                    {"id": "a", "locate_evidence": complete},
                    {"id": "b", "locate_evidence": complete},
                ],
            }
        ),
        encoding="utf-8",
    )
    output = _stub_gate_run(tmp_path, monkeypatch, ("a", "b", "c"))
    monkeypatch.setattr(gates_module, "compare_evidence", lambda *_args: pytest.fail("compared"))

    assert gates_module.main(["--compare-with", str(earlier), "--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    not_comparable = {
        "status": "not comparable",
        "reason": "earlier report uses evidence schema 1",
    }
    assert [item["comparison"] for item in report["documents"]] == [not_comparable] * 3


def test_an_earlier_report_on_evidence_schema_2_is_compared_field_by_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    complete = _complete_evidence().as_dict()
    earlier = tmp_path / "earlier.json"
    earlier.write_text(
        json.dumps(
            {
                "evidence_schema_version": 2,
                "documents": [{"id": "a", "locate_evidence": complete}],
            }
        ),
        encoding="utf-8",
    )
    output = _stub_gate_run(tmp_path, monkeypatch, ("a",))

    assert gates_module.main(["--compare-with", str(earlier), "--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["documents"][0]["comparison"]["status"] == "unchanged"


def test_an_earlier_report_with_a_duplicate_document_id_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    complete = _complete_evidence().as_dict()
    earlier = tmp_path / "earlier.json"
    earlier.write_text(
        json.dumps(
            {
                "documents": [
                    {"id": "a", "locate_evidence": complete},
                    {"id": "a", "locate_evidence": complete},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gates_module, "load_config", lambda: pytest.fail("work started"))
    with pytest.raises(SystemExit, match=f"cannot read comparison report {earlier}.*duplicate"):
        gates_module.main(["--compare-with", str(earlier)])


def test_an_id_in_both_documents_and_errored_of_the_earlier_report_is_a_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    complete = _complete_evidence().as_dict()
    earlier = tmp_path / "earlier.json"
    earlier.write_text(
        json.dumps(
            {
                "documents": [{"id": "a", "locate_evidence": complete}],
                "errored": [{"id": "a", "locate_evidence": complete}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gates_module, "load_config", lambda: pytest.fail("work started"))
    with pytest.raises(SystemExit, match=f"cannot read comparison report {earlier}.*duplicate"):
        gates_module.main(["--compare-with", str(earlier)])


def test_a_malformed_errored_list_in_the_earlier_report_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    earlier = tmp_path / "earlier.json"
    earlier.write_text(json.dumps({"documents": [], "errored": {"id": "a"}}), encoding="utf-8")
    monkeypatch.setattr(gates_module, "load_config", lambda: pytest.fail("work started"))
    with pytest.raises(SystemExit, match=f"cannot read comparison report {earlier}"):
        gates_module.main(["--compare-with", str(earlier)])


def test_a_document_that_errored_earlier_is_not_comparable_because_its_evidence_is_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    earlier_evidence = incomplete_evidence("document failed: unreadable_pdf", None).as_dict()
    earlier = tmp_path / "earlier.json"
    earlier.write_text(
        json.dumps(
            {
                "documents": [],
                "errored": [
                    {"id": "recorded", "locate_evidence": earlier_evidence},
                    {"id": "legacy"},
                ],
            }
        ),
        encoding="utf-8",
    )
    output = _stub_gate_run(tmp_path, monkeypatch, ("recorded", "legacy"))

    assert gates_module.main(["--compare-with", str(earlier), "--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    comparisons = {item["id"]: item["comparison"] for item in report["documents"]}
    assert comparisons["recorded"] == {
        "status": "not comparable",
        "reason": "earlier evidence incomplete: document failed: unreadable_pdf",
    }
    assert comparisons["legacy"]["status"] == "not comparable"
    assert comparisons["legacy"]["reason"] == (
        "earlier evidence incomplete: document failed in the earlier report; "
        "no locate_evidence recorded"
    )


def test_a_document_that_errored_earlier_and_is_gone_now_is_listed_as_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    earlier = tmp_path / "earlier.json"
    earlier.write_text(
        json.dumps({"documents": [{"id": "a"}], "errored": [{"id": "broken"}]}), encoding="utf-8"
    )
    output = _stub_gate_run(tmp_path, monkeypatch, ("a",))

    assert gates_module.main(["--compare-with", str(earlier), "--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert [item["id"] for item in report["removed_documents"]] == ["broken"]
