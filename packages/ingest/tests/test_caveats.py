"""Unit caveats in structure: assumed scale, fallback currency (spec 13, Structure)."""

from datetime import date
from decimal import Decimal

from fra_core.schemas import BBox, Cell, LineItem, Period, PeriodKind, Provenance
from fra_ingest.caveats import median_figure, unit_caveats
from fra_ingest.metadata import Metadata

P = Period(key="2025-12-31", end_date=date(2025, 12, 31), kind=PeriodKind.INSTANT)
BOX = BBox(left=1, top=1, right=2, bottom=2)


def item(n: int, value: str | None, *flags: str) -> LineItem:
    cell = Cell(
        period_key=P.key,
        reported=Decimal(value) if value is not None else None,
        raw_text=value or "",
        provenance=Provenance(page_no=1, bbox=BOX, table_ref="#/tables/0", row=n, col=1),
        flags=list(flags),
    )
    return LineItem(id=f"r{n}", raw_label=f"row {n}", cells=[cell])


BIG = [
    item(n, v)
    for n, v in enumerate(["5473026194", "469758484", "457686035", "90941886", "6491412599"])
]
SMALL = [item(n, v) for n, v in enumerate(["12", "7", "3", "45", "67"])]
UNSTATED = Metadata(currency="EGP", signals=["currency:header"], flags=["scale_missing"])


def test_the_median_leaves_out_per_share_percent_merged_and_empty_cells() -> None:
    items = [
        item(1, "100"),
        item(2, "300"),
        item(3, "2.18", "per_share"),
        item(4, "0.25", "percent"),
        item(5, "108179492720929919", "implausible_magnitude"),
        item(6, None, "numbers_missing"),
        item(7, "0"),
    ]
    assert median_figure(items) == (Decimal("200"), 2)
    assert median_figure([item(1, None, "numbers_missing")]) == (None, 0)


def test_no_printed_multiplier_gives_the_scale_caveat_with_its_evidence() -> None:
    caveats, flags = unit_caveats(UNSTATED, BIG)
    assert [c.id for c in caveats] == ["scale_assumed_units"]
    assert caveats[0].evidence == {"currency_source": "header", "median_figure": "469758484"}
    assert flags == []


def test_a_stated_multiplier_gives_no_scale_caveat() -> None:
    stated = Metadata(scale=1000, currency="SAR", signals=["scale:header", "currency:header"])
    assert unit_caveats(stated, BIG) == ([], [])


def test_a_fallback_currency_gives_its_own_caveat() -> None:
    domicile = Metadata(
        scale=1000,
        currency="SAR",
        signals=["scale:header", "currency:domicile"],
        flags=["currency_from_domicile"],
    )
    caveats, _ = unit_caveats(domicile, BIG)
    assert [(c.id, c.evidence) for c in caveats] == [
        ("currency_from_domicile", {"currency": "SAR"})
    ]
    inferred = domicile.model_copy(update={"flags": ["currency_inferred"]})
    assert [c.id for c in unit_caveats(inferred, BIG)[0]] == ["currency_inferred"]


def test_small_figures_under_an_assumed_scale_are_implausible() -> None:
    caveats, flags = unit_caveats(UNSTATED, SMALL)
    assert [c.id for c in caveats] == ["scale_assumed_units"] and flags == ["scale_implausible"]


def test_too_few_figures_say_nothing_about_plausibility() -> None:
    assert unit_caveats(UNSTATED, SMALL[:4])[1] == []


def test_small_figures_under_a_stated_scale_are_fine() -> None:
    stated = Metadata(scale=1_000_000, currency="SAR", signals=["scale:header"])
    assert unit_caveats(stated, SMALL) == ([], [])
