"""Provenance review and human approval with synthetic snapshots."""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from harness.independent_gold import FrozenGold, Reading, load_gold
from harness.review_map import approved_gold, cell_map
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
    assert mapped[0]["occurrence"] == 0
    assert mapped[1]["occurrence"] == 1
    assert [r["runs"][0]["value"] for r in mapped] == ["5", "2"]


def test_heading_without_cells_does_not_consume_a_label_occurrence() -> None:
    heading = item(0, "Cash", None)
    actual = result([statement(1, [heading, item(1, "Cash", "5")])])
    mapped = cell_map([actual, actual])
    assert len(mapped) == 1
    assert mapped[0]["occurrence"] == 0
