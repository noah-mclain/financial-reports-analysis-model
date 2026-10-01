"""What a figure rests on when the page does not say (spec 13)."""

from datetime import date

import pytest
from pydantic import ValidationError

from fra_core.schemas import Caveat, Period, PeriodKind, Statement, StatementType

P = Period(key="2025-12-31", end_date=date(2025, 12, 31), kind=PeriodKind.INSTANT)


def statement(caveats: list[Caveat] | None = None) -> Statement:
    base = Statement(
        id="s",
        document_sha256="a" * 64,
        type=StatementType.BALANCE,
        currency="EGP",
        scale=1,
        periods=[P],
    )
    return base if caveats is None else base.model_copy(update={"caveats": caveats})


def test_a_statement_has_no_caveats_unless_given() -> None:
    assert statement().caveats == []


def test_a_caveat_round_trips_on_its_statement() -> None:
    caveat = Caveat(
        id="scale_assumed_units",
        evidence={"currency_source": "header", "median_figure": "789000000"},
    )
    stored = statement([caveat]).model_dump_json()
    loaded = Statement.model_validate_json(stored)
    assert loaded.caveats == [caveat]
    assert loaded.caveats[0].scope == "currency_amounts"


def test_only_known_caveats_exist() -> None:
    with pytest.raises(ValidationError):
        Caveat.model_validate({"id": "anything_else"})
