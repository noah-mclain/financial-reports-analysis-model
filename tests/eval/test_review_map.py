"""Provenance review and human approval with synthetic snapshots."""

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from harness.independent_gold import FrozenGold, Reading, load_gold
from harness.review_map import (
    approved_gold,
    build_review,
    cell_map,
    draft_positions,
    render_row_identity,
    row_reference,
)
from pydantic import ValidationError
from tests.eval.test_expected import item, result, statement
from tests.eval.test_independent_gold import reading


def test_repeat_agreement_keeps_source_map_and_missing_observations() -> None:
    first = result([statement(1, [item(1, "Assets", "5"), item(2, "Cash", "2")])])
    second = result([statement(1, [item(1, "Assets", "5")])])
    mapped = cell_map([first, second])
    assert mapped[0]["consistent"] is True
    assert mapped[0]["runs"][0]["source"]["page_no"] == 5
    assert mapped[0]["runs"][0]["source"]["row"] == 1
    assert mapped[0]["runs"][0]["source"]["bbox"]["left"] == 1
    assert mapped[1]["consistent"] is False
    assert mapped[1]["runs"][1] is None
    assert cell_map([first])[0]["consistent"] is False
    second.statements[0].line_items[0].cells[0].reported = Decimal("6")
    assert cell_map([first, second])[0]["consistent"] is False


def test_assisted_review_requires_explicit_method_and_completed_pdf_reading(tmp_path: Path) -> None:
    data = reading("Owner")
    data["source"] = "pdf_reviewed"
    human = Reading.model_validate(data)
    with pytest.raises(ValidationError):
        FrozenGold(readings=(human,))
    source = tmp_path / "reading.json"
    target = tmp_path / "frozen.json"
    source.write_text(json.dumps(data))
    approved_gold(source, target)
    assert load_gold(target).method == "assisted_review"
    with pytest.raises(FileExistsError):
        approved_gold(source, target)
    data["statements"][0]["rows"][0]["unconfirmed"] = ["2025-12-31"]
    source.write_text(json.dumps(data))
    with pytest.raises(ValidationError):
        approved_gold(source, tmp_path / "not-approved.json")


def test_duplicate_labels_are_not_collapsed() -> None:
    actual = result([statement(1, [item(1, "Cash", "5"), item(2, "Cash", "2")])])
    mapped = cell_map([actual, actual])
    assert len(mapped) == 2
    assert [r["position"]["row"] for r in mapped] == [1, 2]
    assert [r["runs"][0]["value"] for r in mapped] == ["5", "2"]


def test_display_row_references_are_statement_namespaced_and_reject_unknown_types() -> None:
    from fra_core.schemas import StatementType

    assert row_reference(StatementType.BALANCE, "line-4") == "bc:line-4"
    assert row_reference(StatementType.INCOME, "line-4") == "ic:line-4"
    assert row_reference(StatementType.COMPREHENSIVE_INCOME, "line-4") == "oci:line-4"
    assert row_reference(StatementType.CASH_FLOW, "line-4") == "cf:line-4"
    assert row_reference(StatementType.EQUITY, "line-4") == "eq:line-4"


def test_shared_id_badges_are_per_statement_snapshot_and_ignore_null_ids() -> None:
    from fra_core.schemas import StatementType

    first = result(
        [
            statement(
                1,
                [item(1, "Cash", "5"), item(2, "Cash", "2"), item(3, "Other", "1")],
            )
        ]
    )
    a, b, other = first.statements[0].line_items
    a.canonical_id = b.canonical_id = "cash_and_equivalents"
    other.canonical_id = None
    other_statement = statement(2, [item(1, "Cash", "5")], StatementType.INCOME)
    other_statement.line_items[0].canonical_id = "cash_and_equivalents"
    first.statements.append(other_statement)
    second = result([statement(1, [item(1, "Cash", "5"), item(2, "Cash", "2")])])
    second.statements[0].line_items[0].canonical_id = "cash_and_equivalents"
    second.statements[0].line_items[1].canonical_id = "trade_receivables"
    mapped = cell_map([first, second])
    balance = [row for row in mapped if row["statement"] == "balance"]
    assert [
        (
            row["runs"][0]["shared_count"],
            None if row["runs"][1] is None else row["runs"][1]["shared_count"],
        )
        for row in balance
    ] == [(2, None), (2, None), (None, None)]
    assert balance[0]["runs"][0]["peer_row_refs"] == ["bc:r2"]
    assert (
        next(row for row in balance if row["label"] == "Other")["runs"][0]["shared_count"] is None
    )
    assert all(
        row["runs"][0]["shared_count"] is None for row in mapped if row["statement"] == "income"
    )


def test_repeated_period_cells_do_not_inflate_shared_row_count() -> None:
    from datetime import date

    from fra_core.schemas import Period, PeriodKind

    first = result([statement(1, [item(1, "Cash", "5"), item(2, "Cash", "2")])])
    first.statements[0].line_items[0].canonical_id = "cash_and_equivalents"
    first.statements[0].line_items[1].canonical_id = "cash_and_equivalents"
    later = Period(key="2024-12-31", end_date=date(2024, 12, 31), kind=PeriodKind.INSTANT)
    for row in first.statements[0].line_items:
        cell = row.cells[0].model_copy(deep=True, update={"period_key": later.key})
        row.cells.append(cell)
    mapped = cell_map([first, first])
    assert len(mapped) == 4
    assert all(row["runs"][0]["shared_count"] == 2 for row in mapped)


def test_html_row_identity_escapes_producer_and_peer_ids() -> None:
    rendered = render_row_identity(
        {
            "row_ref": 'bc:<img src=x onerror="bad">',
            "canonical_id": "cash",
            "shared_count": 2,
            "peer_row_refs": ['bc:<script>alert("x")</script>'],
        }
    )
    assert "&lt;img" in rendered and "&lt;script&gt;" in rendered
    assert "<img" not in rendered and "<script>" not in rendered


def test_build_review_renders_display_identity_without_exporting_it(
    tmp_path: Path, monkeypatch
) -> None:
    import harness.review_map as review_map

    pdf_path = tmp_path / "synthetic.pdf"
    pdf = __import__("pypdfium2").PdfDocument.new()
    for _ in range(5):
        pdf.new_page(612, 792)
    pdf.save(pdf_path)
    pdf.close()
    digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        yaml.safe_dump({"documents": [{"id": "synthetic", "file": "synthetic.pdf"}]})
    )
    monkeypatch.setattr(review_map, "MANIFEST", manifest)

    extracted = result([statement(1, [item(1, '<Cash "A">', "5"), item(2, "Cash", "2")])])
    extracted.sha256 = digest
    extracted.statements[0].document_sha256 = digest
    extracted.statements[0].line_items[0].id = '<row "one">'
    extracted.statements[0].line_items[0].canonical_id = "cash_and_equivalents"
    extracted.statements[0].line_items[1].canonical_id = "cash_and_equivalents"
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(extracted.model_dump_json())

    html_path = build_review("synthetic", [snapshot], tmp_path / "review")
    rendered = html_path.read_text()
    assert "bc:&lt;row &quot;one&quot;&gt;" in rendered and "bc:r2" in rendered
    assert "shared canonical ID (2 rows): bc:r2" in rendered
    assert "&lt;Cash &quot;A&quot;&gt;" in rendered
    assert "JSON.stringify(initial, null, 2)" in rendered
    assert "row_ref" not in rendered.split('<script id="draft"', 1)[1].split("</script>", 1)[0]
    draft = json.loads((html_path.parent / "reading-draft.json").read_text())
    assert draft["statements"][0]["rows"][0]["canonical_id"] == "cash_and_equivalents"
    draft_json = json.dumps(draft)
    assert all(key not in draft_json for key in ('"row_ref"', '"shared_count"', '"peer_row_refs"'))
    reviewed = reading("Owner")
    reviewed["source"] = "pdf_reviewed"
    reading_file = tmp_path / "reviewed.json"
    reading_file.write_text(json.dumps(reviewed))
    frozen_file = tmp_path / "frozen.json"
    approved_gold(reading_file, frozen_file)
    frozen = json.loads(frozen_file.read_text())
    assert frozen["readings"][0]["statements"][0]["rows"][0]["canonical_id"] == "total_assets"
    frozen_json = json.dumps(frozen)
    assert all(key not in frozen_json for key in ('"row_ref"', '"shared_count"', '"peer_row_refs"'))
    invalid = reading("Owner")
    invalid["statements"][0]["rows"][0]["canonical_id"] = "bc:total_assets"
    with pytest.raises(ValidationError, match="canonical id"):
        Reading.model_validate(invalid)


def test_heading_without_cells_does_not_consume_a_label_occurrence() -> None:
    heading = item(0, "Cash", None)
    actual = result([statement(1, [heading, item(1, "Cash", "5")])])
    mapped = cell_map([actual, actual])
    assert len(mapped) == 1
    assert mapped[0]["position"]["row"] == 1


def test_an_omitted_earlier_repeat_does_not_shift_the_later_ones() -> None:
    first = result([statement(1, [item(1, "Cash", "5"), item(2, "Cash", "2")])])
    second = result([statement(1, [item(2, "Cash", "2")])])
    mapped = cell_map([first, second])
    assert [r["position"]["row"] for r in mapped] == [1, 2]
    assert mapped[0]["runs"][1] is None
    assert [r["value"] for r in mapped[1]["runs"]] == ["2", "2"]
    assert mapped[1]["consistent"] is True


def test_the_same_row_of_another_table_is_another_position() -> None:
    other = item(1, "Cash", "7")
    for cell in other.cells:
        cell.provenance = cell.provenance.model_copy(update={"table_ref": "#/tables/1"})
    mapped = cell_map([result([statement(1, [item(1, "Cash", "5"), other])])])
    assert [r["position"]["table_ref"] for r in mapped] == ["#/tables/0", "#/tables/1"]


def test_draft_rows_are_found_by_the_position_of_their_source_row() -> None:
    from harness.expected import draft_expected

    extracted = result(
        [
            statement(
                1,
                [item(1, "Cash", "5"), item(2, "Heading", None), item(3, "Cash", "2")],
            )
        ]
    )
    draft = draft_expected("synthetic", extracted).model_dump(mode="json")
    positions = draft_positions(draft, extracted)
    keys = {(label, where[2]): row for (_, label, where, _), row in positions.items()}
    assert keys == {("Cash", 1): (0, 0), ("Cash", 3): (0, 1)}
