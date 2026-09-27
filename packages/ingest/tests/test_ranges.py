"""Grouping candidate pages into statement ranges, and choosing what docling converts."""

from collections.abc import Iterable

from fra_core.schemas import StatementType
from fra_ingest.locate import find_ranges, plan_conversion
from fra_ingest.results import PageScore, StatementRange

B, INC, CF, EQ = (
    StatementType.BALANCE,
    StatementType.INCOME,
    StatementType.CASH_FLOW,
    StatementType.EQUITY,
)


def score(
    page_no: int,
    titled: Iterable[StatementType] = (),
    *,
    value: float = 8.0,
    numbers: int = 40,
    continuation: bool = False,
    negatives: tuple[str, ...] = (),
) -> PageScore:
    names = list(titled)
    return PageScore(
        page_no=page_no,
        type_scores={t: (value if t in names else 0.0) for t in StatementType},
        title_types=names,
        numeric_tokens=numbers,
        continuation=continuation,
        negatives=list(negatives),
    )


def spans(ranges: list[StatementRange], statement_type: StatementType) -> list[tuple[int, int]]:
    return [(r.first_page, r.last_page) for r in ranges if r.type is statement_type]


def test_consecutive_pages_of_one_type_form_one_range() -> None:
    ranges = find_ranges([score(1), score(2, [B]), score(3, [B]), score(4)])
    assert spans(ranges, B) == [(2, 4)]  # page 4: numeric, untitled, continues the range


def test_an_untitled_page_with_few_numbers_does_not_continue() -> None:
    ranges = find_ranges([score(2, [B]), score(3, numbers=3)])
    assert spans(ranges, B) == [(2, 2)]


def test_a_page_titled_as_another_type_ends_the_range() -> None:
    ranges = find_ranges([score(5, [B]), score(6, [INC]), score(7)])
    assert spans(ranges, B) == [(5, 5)]
    assert spans(ranges, INC) == [(6, 7)]


def test_separate_candidates_are_all_kept_and_ranked_by_score() -> None:
    ranges = find_ranges([score(3, [B], value=6.0), score(4, numbers=0), score(9, [B], value=9.0)])
    balance = [r for r in ranges if r.type is B]
    assert [(r.first_page, r.rank) for r in balance] == [(9, 1), (3, 2)]


def test_padding_is_clamped_to_the_document() -> None:
    first = StatementRange(type=B, first_page=1, last_page=2, score=8.0, rank=1)
    last = StatementRange(type=B, first_page=9, last_page=10, score=8.0, rank=1)
    assert first.padded(1, 10) == (1, 3)
    assert last.padded(1, 10) == (8, 10)


def test_conversion_merges_enabled_ranges_and_skips_optional_ones() -> None:
    ranges = [
        StatementRange(type=B, first_page=5, last_page=5, score=8.0, rank=1),
        StatementRange(type=INC, first_page=7, last_page=7, score=8.0, rank=1),
        StatementRange(type=CF, first_page=12, last_page=13, score=8.0, rank=1),
        StatementRange(type=INC, first_page=30, last_page=30, score=5.0, rank=2),
    ]
    assert plan_conversion(ranges, [B, INC], page_count=40, pad=1) == [(4, 8), (29, 31)]


def test_a_notes_page_never_continues_a_range() -> None:
    # Juhayna AR consolidated: one unmarked notes page started a range that then ran through
    # 24 numeric notes pages, each headed "تابع الإيضاحات المتممة".
    ranges = find_ranges([score(32, [B]), score(33, negatives=("notes",)), score(34)])
    assert spans(ranges, B) == [(32, 32)]
