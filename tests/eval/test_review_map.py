"""Provenance review and human approval with synthetic snapshots."""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from harness.independent_gold import FrozenGold, Reading, load_gold
from harness.review_map import approved_gold, cell_map, draft_positions
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
