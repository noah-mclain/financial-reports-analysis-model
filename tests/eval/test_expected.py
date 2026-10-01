"""Draft expected files (spec 12, Data model)."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from harness.expected import ExpectedFile, draft_expected, load_expected, write_draft
from pydantic import ValidationError

from fra_core.schemas import (
    BBox,
    Cell,
    LineItem,
    Period,
    PeriodKind,
    Provenance,
    Statement,
    StatementType,
    TextSource,
)
from fra_ingest.results import StructureResult

P = Period(key="2025-12-31", end_date=date(2025, 12, 31), kind=PeriodKind.INSTANT)
BOX = BBox(left=1, top=1, right=2, bottom=2)
SHA = "a" * 64


def item(n: int, label: str, value: str | None, source: TextSource = TextSource.TEXT) -> LineItem:
    cells = (
        []
        if value is None
        else [
            Cell(
                period_key=P.key,
                reported=Decimal(value) if value else None,
                raw_text=value,
                provenance=Provenance(
                    page_no=5, bbox=BOX, table_ref="#/tables/0", row=n, col=1, source=source
                ),
                flags=[] if value else ["numbers_missing"],
            )
        ]
    )
    return LineItem(id=f"r{n}", raw_label=label, cells=cells)


def statement(
    number: int, items: list[LineItem], kind: StatementType = StatementType.BALANCE
) -> Statement:
    return Statement(
        id=f"s{number}",
        document_sha256=SHA,
        type=kind,
        currency="EGP",
        scale=1,
        periods=[P],
        line_items=items,
        source_pages=[5],
    )


def result(statements: list[Statement]) -> StructureResult:
    return StructureResult(
        version="4", sha256=SHA, convert_version="1", settings_hash="h", statements=statements
    )


ROWS = [item(1, "Assets", None), item(2, "Cash", "5"), item(3, "Lost", ""), item(4, "Total", "5")]


def test_a_draft_takes_the_first_statement_of_each_type_and_its_valued_rows() -> None:
    draft = draft_expected(
        "doc",
        result(
            [
                statement(1, ROWS),
                statement(2, [item(1, "Other", "9")]),
                statement(3, [item(1, "Revenue", "7", TextSource.OCR)], StatementType.INCOME),
            ]
        ),
    )
    assert draft.status == "draft" and draft.checked_by == []
    assert [s.type for s in draft.statements] == [StatementType.BALANCE, StatementType.INCOME]
    balance = draft.statements[0]
    assert [r.label for r in balance.rows] == ["Cash", "Lost", "Total"]
    assert balance.rows[1].values == {P.key: None}
    assert (balance.page_mode, draft.statements[1].page_mode) == ("digital", "scanned")
    assert (balance.scale, balance.currency, balance.pages) == (1, "EGP", [5])


def test_every_figure_of_a_draft_starts_unconfirmed() -> None:
    draft = draft_expected("doc", result([statement(1, ROWS)]))
    assert all(r.unconfirmed == [P.key] for r in draft.statements[0].rows)
    assert draft.confirmed_cells == 0


def test_a_draft_is_written_and_an_untouched_draft_is_replaced(tmp_path: Path) -> None:
    path = tmp_path / "doc.json"
    draft = draft_expected("doc", result([statement(1, ROWS)]))
    assert write_draft(path, draft)
    assert load_expected(path) == draft
    assert write_draft(path, draft)


def test_a_checked_file_or_a_draft_with_a_confirmed_figure_is_never_replaced(
    tmp_path: Path,
) -> None:
    path = tmp_path / "doc.json"
    draft = draft_expected("doc", result([statement(1, ROWS)]))
    read = draft.model_copy(deep=True)
    read.statements[0].rows[0].unconfirmed = []
    assert write_draft(path, read)
    assert not write_draft(path, draft)
    assert load_expected(path) == read
    assert write_draft(path, draft, force=True)
    checked = read.model_copy(deep=True)
    for row in checked.statements[0].rows:
        row.unconfirmed = []
    checked = checked.model_copy(update={"status": "checked", "checked_by": ["n", "m"]})
    path.write_text(checked.model_dump_json(), encoding="utf-8")
    assert not write_draft(path, draft)


def test_a_checked_file_needs_two_readers_and_no_unconfirmed_figure() -> None:
    draft = draft_expected("doc", result([statement(1, ROWS)]))
    with pytest.raises(ValidationError, match="two readers"):
        ExpectedFile.model_validate({**draft.model_dump(mode="json"), "status": "checked"})
    one = {**draft.model_dump(mode="json"), "status": "checked", "checked_by": ["n", "n"]}
    with pytest.raises(ValidationError, match="two readers"):
        ExpectedFile.model_validate(one)
    two = {**draft.model_dump(mode="json"), "status": "checked", "checked_by": ["n", "m"]}
    with pytest.raises(ValidationError, match="unconfirmed"):
        ExpectedFile.model_validate(two)
