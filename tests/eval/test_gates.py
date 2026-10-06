"""Gate A and Gate B figures: per document from the harness's own functions, then per period kind
on a synthetic report."""

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from harness import gates as gates_module
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
from test_mapping_harness import INDEX, TAXONOMY, P, item, statement

from fra_core.schemas import CheckResult, Period, PeriodKind, StatementType
from fra_ingest.config import IngestConfig
from fra_ingest.errors import IngestError
from fra_ingest.results import StructureResult
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
        "figures": figures or [],
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

    assert gates_module.main([]) == 0
    assert factory_configs == [config]
    assert [call[0].name for call in calls] == ["first.pdf", "second.pdf"]
    assert all(call[1] is engine for call in calls)
