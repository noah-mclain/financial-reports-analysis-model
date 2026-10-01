"""The structure eval's arithmetic (spec 11, Scoring)."""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from harness import structure as harness
from harness.structure import (
    identity_accepted,
    identity_excuses,
    identity_status,
    metadata_ok,
    pair_misses,
    value_rows,
)

from fra_core.schemas import (
    BBox,
    Cell,
    CheckResult,
    LineItem,
    Period,
    PeriodKind,
    Provenance,
    Statement,
    StatementType,
)
from fra_ingest.config import IngestConfig
from fra_ingest.results import StructureResult
from fra_ingest.review import StatementReview

P = Period(key="2025-12-31", end_date=date(2025, 12, 31), kind=PeriodKind.INSTANT)
BOX = BBox(left=1, top=1, right=2, bottom=2)


def statement(
    values: list[str | None],
    scale: int = 1000,
    currency: str = "SAR",
    flags: list[str] | None = None,
) -> Statement:
    items = [
        LineItem(
            id=f"r{i}",
            raw_label=f"row {i}",
            cells=[]
            if v is None
            else [
                Cell(
                    period_key=P.key,
                    reported=Decimal(v),
                    raw_text=v,
                    provenance=Provenance(
                        page_no=1, bbox=BOX, table_ref="#/tables/0", row=i, col=1
                    ),
                )
            ],
        )
        for i, v in enumerate(values)
    ]
    return Statement(
        id="s",
        document_sha256="a" * 64,
        type=StatementType.BALANCE,
        currency=currency,
        scale=scale,
        periods=[P],
        line_items=items,
        flags=flags or [],
    )


def test_rows_compare_after_scale_and_ignore_rows_without_values() -> None:
    assert value_rows(statement(["10", None, "5"])) == value_rows(
        statement(["5000", "10000"], scale=1)
    )


def test_pair_misses_count_each_side() -> None:
    assert pair_misses(statement(["10", "5"]), statement(["10", "6"])) == (1, 1)
    assert pair_misses(statement(["10", "5"]), statement(["5", "10"])) == (0, 0)


def test_metadata_is_correct_or_flagged() -> None:
    assert metadata_ok(statement(["1"]), 1000, "SAR")
    assert not metadata_ok(statement(["1"], currency="EGP"), 1000, "SAR")
    assert metadata_ok(statement(["1"], currency="XXX", flags=["currency_missing"]), 1000, "SAR")


def test_unconfirmed_manifest_scale_is_not_scored() -> None:
    assert metadata_ok(statement(["1"], scale=1), "unconfirmed", "SAR")
    assert not metadata_ok(statement(["1"], scale=1, currency="EGP"), "unconfirmed", "SAR")


def test_unconfirmed_is_matched_exactly() -> None:
    assert not metadata_ok(statement(["1"], scale=1), "tbd", "SAR")
    assert not metadata_ok(statement(["1"], scale=1), None, "SAR")


def _identity_check(item_ids: list[str]) -> CheckResult:
    return CheckResult(
        id="c",
        statement_id="s",
        kind="balance_identity",
        period_key=P.key,
        status="fail",
        line_item_ids=item_ids,
    )


def _with_missing_cell(row: int, total: int = 3) -> Statement:
    base = statement([str(i + 1) for i in range(total)])
    for item in base.line_items:
        item.cells[0].flags = ["numbers_missing"] if item.id == f"r{row}" else []
    return base


def test_identity_failure_is_excused_by_a_missing_cell_on_its_own_rows() -> None:
    check = _identity_check(["r0", "r1"])
    assert identity_excuses(_with_missing_cell(1), [check]) == [
        {"item": "r1", "period": P.key, "flag": "numbers_missing"}
    ]


def test_identity_failure_is_excused_by_a_digit_suspect_on_its_own_rows() -> None:
    suspect = statement(["10", "15"])
    cell = suspect.line_items[0].cells[0]
    suspect.line_items[0].cells[0] = cell.model_copy(update={"flags": ["digit_suspect"]})
    check = _identity_check(["r0", "r1"])
    assert identity_excuses(suspect, [check]) == [
        {"item": "r0", "period": P.key, "flag": "digit_suspect"}
    ]


def test_a_failed_identity_is_accepted_only_on_a_held_statement_with_an_excuse() -> None:
    excuse = [{"item": "r0", "period": P.key, "flag": "digit_suspect"}]
    held = StatementReview(
        statement_id="s",
        status="needs_review",
        reasons=["identity_failed"],
        numeric_cells=2,
        checked_cells=0,
        flagged_cells=1,
    )
    passed = held.model_copy(update={"status": "passed", "reasons": []})
    assert identity_accepted(excuse, held)
    assert not identity_accepted(None, held)
    assert not identity_accepted(excuse, passed)
    assert not identity_accepted(excuse, None)


def test_identity_failure_is_not_excused_by_an_unrelated_row() -> None:
    check = _identity_check(["r0", "r1"])
    assert identity_excuses(_with_missing_cell(2), [check]) is None
    assert identity_excuses(statement(["1"]), []) == []


def test_each_metadata_field_is_excused_only_by_its_own_flag() -> None:
    usd = statement(["1"], scale=1, currency="USD", flags=["scale_missing"])
    assert not metadata_ok(usd, "unconfirmed", "EGP")
    assert not metadata_ok(usd, 1, "EGP")
    wrong_scale = statement(["1"], scale=1, currency="EGP", flags=["currency_missing"])
    assert not metadata_ok(wrong_scale, 1000, "EGP")
    assert metadata_ok(statement(["1"], scale=1, flags=["scale_conflict"]), 1000, "SAR")
    assert metadata_ok(statement(["1"], currency="XXX", flags=["currency_conflict"]), 1000, "SAR")


def _check(status: str, detail: str = "", kind: str = "balance_identity") -> CheckResult:
    return CheckResult(
        id=f"c-{status}-{kind}",
        statement_id="s",
        kind=kind,
        period_key=P.key,
        status=status,
        detail=detail,
    )


def test_identity_status_comes_from_the_identity_checks() -> None:
    assert identity_status([_check("pass"), _check("skipped", "missing_values")]) == ("ok", "")
    assert identity_status([_check("pass"), _check("fail")]) == ("failed", "")
    assert identity_status([_check("skipped", "missing_values")]) == ("skipped", "missing_values")
    assert identity_status([_check("pass", kind="subtotal")]) == ("skipped", "not_checked")
    assert identity_status([]) == ("skipped", "not_checked")


def _run_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    found: dict[str, tuple[list[Statement], list[CheckResult]]],
) -> tuple[int, dict[str, object]]:
    manifest = tmp_path / "golden" / "manifest.yaml"
    manifest.parent.mkdir()
    manifest.write_text(
        json.dumps(
            {
                "documents": [
                    {"id": key, "file": f"{key}.pdf", "scale": 1000, "currency": "SAR"}
                    for key in found
                ]
            }
        ),
        encoding="utf-8",
    )
    config = IngestConfig(artifact_root=tmp_path / "artifacts", enabled_types=(P_TYPE,))

    def fake_structure(pdf: Path, *_args: object, **_kw: object) -> StructureResult:
        statements, checks = found[pdf.stem]
        sha = format(abs(hash(pdf.stem)), "x").rjust(64, "0")[:64]
        out = config.artifact_root / sha
        out.mkdir(parents=True, exist_ok=True)
        (out / "table_checks.json").write_text(
            json.dumps([c.model_dump(mode="json") for c in checks]), encoding="utf-8"
        )
        return StructureResult(
            version="2", sha256=sha, convert_version="1", settings_hash="", statements=statements
        )

    monkeypatch.setattr(harness, "MANIFEST", manifest)
    monkeypatch.setattr(harness, "OUT", tmp_path / "eval")
    monkeypatch.setattr(harness, "load_config", lambda: config)
    monkeypatch.setattr(harness, "structure_pdf", fake_structure)
    monkeypatch.setattr(harness, "PAIRS", (("en", "ar", True),))
    code = harness.main([])
    written = json.loads((tmp_path / "eval" / "structure-golden.json").read_text("utf-8"))
    return code, written


P_TYPE = StatementType.BALANCE


def test_an_identity_failure_nothing_explains_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = statement(["10", "10"])
    failed = statement(["10", "9"], flags=["identity_failed"])
    check = _identity_check(["r0", "r1"])
    code, written = _run_main(
        tmp_path, monkeypatch, {"en": ([good], []), "ar": ([failed], [check])}
    )
    assert code == 1
    assert "ar: identity failed and nothing on its rows explains it" in written["reasons"]
    documents = {d["id"]: d for d in written["documents"]}
    assert documents["ar"]["statements"]["balance"]["identity"] == "failed"
    assert documents["en"]["statements"]["balance"]["identity"] == "skipped"


def test_a_gated_pair_missing_a_statement_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, written = _run_main(
        tmp_path, monkeypatch, {"en": ([statement(["10"])], []), "ar": ([], [])}
    )
    assert code == 1
    assert "en/ar balance: missing statement" in written["reasons"]


def test_a_clean_run_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    passed = _check("pass")
    code, written = _run_main(
        tmp_path,
        monkeypatch,
        {"en": ([statement(["10"])], [passed]), "ar": ([statement(["10"])], [passed])},
    )
    assert code == 0 and written["reasons"] == []
