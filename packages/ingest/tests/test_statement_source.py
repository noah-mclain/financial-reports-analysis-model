"""Source authorization uses located ranges, never table confidence or numeric checks."""

from dataclasses import replace

import pytest
from support import HEADER, PAGE_1, PAGE_2, inputs, page, span, structured_source

from fra_core.schemas import StatementType as T
from fra_ingest.config import IngestConfig
from fra_ingest.docling_json import DlDocument
from fra_ingest.results import PageScore, StatementRange, StructureResult
from fra_ingest.statement_source import (
    SourcePlan,
    resolve_tied_blocks,
    source_plan,
    start_decision,
    table_source,
)
from fra_ingest.structure import StructureInputs, structure_document


def plan() -> SourcePlan:
    return source_plan(
        [
            span(T.BALANCE, 5, rank=2),
            span(T.INCOME, 6),
            span(T.COMPREHENSIVE_INCOME, 7),
            span(T.BALANCE, 32),
            span(T.BALANCE, 60, rank=3),
        ],
        [
            page(5, T.BALANCE),
            page(6, T.INCOME),
            page(7, T.COMPREHENSIVE_INCOME),
            page(32, T.BALANCE),
            page(60, T.BALANCE),
            page(59),
            page(40),
        ],
        [T.BALANCE, T.INCOME, T.COMPREHENSIVE_INCOME],
        65,
        1,
    )


def test_rank_one_does_not_displace_the_balance_beside_primary_statements() -> None:
    p = plan()
    primary = table_source(p, 5, T.BALANCE, 2)
    assert start_decision(primary, None, None, False).allowed
    remote = table_source(p, 60, T.BALANCE, 26)
    decision = start_decision(remote, None, None, False)
    assert not decision.allowed and decision.reason == "source_secondary_range"
    assert "source_distance:55" in decision.evidence
    assert (
        start_decision(table_source(p, 32, T.BALANCE, 6), None, None, False).reason
        == "source_secondary_range"
    )


@pytest.mark.parametrize(
    "n,reason", [(59, "source_unrelated_padding"), (40, "source_outside_range")]
)
def test_padding_or_outside_cannot_start(n: int, reason: str) -> None:
    assert start_decision(table_source(plan(), n, T.INCOME, 6), None, None, False).reason == reason


def test_tied_support_is_ambiguous() -> None:
    p = source_plan(
        [
            span(T.BALANCE, 5),
            span(T.INCOME, 6, rank=2),
            span(T.BALANCE, 40, rank=2),
            span(T.INCOME, 41),
        ],
        [page(5, T.BALANCE), page(6, T.INCOME), page(40, T.BALANCE), page(41, T.INCOME)],
        [T.BALANCE, T.INCOME],
        45,
        1,
    )
    assert (
        start_decision(table_source(p, 5, T.BALANCE, 6), None, None, True).reason
        == "source_primary_ambiguous"
    )


def test_negatives_and_conflicting_title_veto_primary_starts() -> None:
    for evidence, reason in [
        (page(5, T.BALANCE, negatives=("notes",)), "source_non_statement_page"),
        (page(5, T.INCOME), "source_conflicting_title"),
    ]:
        p = replace(plan(), pages=(evidence, *plan().pages[1:]))
        assert start_decision(table_source(p, 5, T.BALANCE, 6), None, None, False).reason == reason


def test_opposite_explicit_local_scope_has_its_own_group() -> None:
    primary = start_decision(table_source(plan(), 5, T.BALANCE, 4), True, True, True)
    secondary = start_decision(table_source(plan(), 60, T.BALANCE, 4), True, False, True)
    assert primary.allowed and secondary.allowed and primary.group_key != secondary.group_key
    for scope, titled in [(None, True), (True, True), (False, False)]:
        assert not start_decision(
            table_source(plan(), 60, T.BALANCE, 4), True, scope, titled
        ).allowed


def test_input_order_and_row_count_do_not_change_authorization() -> None:
    p = plan()
    reversed_plan = source_plan(
        list(reversed(p.ranges)),
        list(reversed(p.pages)),
        [T.BALANCE, T.INCOME, T.COMPREHENSIVE_INCOME],
        65,
        1,
    )
    assert (
        start_decision(table_source(p, 5, T.BALANCE, 1), None, None, False).group_key
        == start_decision(
            table_source(reversed_plan, 5, T.BALANCE, 100), None, None, False
        ).group_key
    )


@pytest.mark.parametrize(
    "ranges,pages,count",
    [
        ([span(T.BALANCE, 6)], [page(6, T.BALANCE)], 5),
        ([span(T.BALANCE, 5)], [], 5),
        ([span(T.BALANCE, 5)], [page(5, T.BALANCE), page(5, T.INCOME)], 5),
    ],
)
def test_incomplete_or_invalid_source_inputs_fail(
    ranges: list[StatementRange], pages: list[PageScore], count: int
) -> None:
    with pytest.raises(ValueError):
        source_plan(ranges, pages, [T.BALANCE], count, 1)


def test_failed_primary_is_not_replaced_by_confident_remote_balance() -> None:
    weak = [HEADER, ["Cash", "", "5", "4"], ["Inventories", "", "10", "8"]]
    ranges = [
        span(T.BALANCE, 5),
        span(T.INCOME, 6),
        span(T.COMPREHENSIVE_INCOME, 7),
        span(T.BALANCE, 60, rank=2),
    ]
    scores = [page(5), page(6, T.INCOME), page(7, T.COMPREHENSIVE_INCOME), page(60, T.BALANCE)]
    scores[0].cue_types = [T.BALANCE]
    scores[0].type_scores = {T.BALANCE: 8}
    base = structured_source([(5, weak), (60, [*PAGE_1, *PAGE_2[1:]])], ranges, scores)
    result, _ = structure_document(base, IngestConfig())
    assert not result.statements
    assert "below_confidence" in result.tables[0].evidence
    assert result.tables[1].type is None and result.tables[1].statement_id is None
    assert "source_secondary_range" in result.tables[1].evidence
    assert "source_distance:55" in result.tables[1].evidence
    assert any(e.startswith("balance:hits=") for e in result.tables[1].evidence)
    assert StructureResult.model_validate_json(result.model_dump_json()).tables == result.tables


@pytest.mark.parametrize("second_page", [5, 6, 40])
def test_scopes_do_not_merge_or_lend_periods(second_page: int) -> None:
    first = [*PAGE_1, *PAGE_2[1:]]
    second = first
    if second_page == 5:
        # Place each local title just above its own table.
        doc_titles = [
            (5, 50, "Consolidated statement of financial position"),
            (5, 350, "Standalone statement of financial position"),
        ]
    else:
        doc_titles = [
            (5, 50, "Consolidated statement of financial position"),
            (second_page, 50, "Standalone statement of financial position"),
        ]
    ranges = (
        [span(T.BALANCE, 5), span(T.BALANCE, second_page, rank=2)]
        if second_page != 5
        else [span(T.BALANCE, 5)]
    )
    base = structured_source(
        [(5, first), (second_page, second)],
        ranges,
        [page(n, T.BALANCE) for n in sorted({5, second_page})],
        doc_titles,
    )
    result, _ = structure_document(base, IngestConfig())
    assert len(result.statements) == 2
    assert [s.consolidated for s in result.statements] == [True, False]
    assert [len(s.line_items) for s in result.statements] == [8, 8]
    undated = [["", "Notes", "", ""], *second[1:]]
    base = structured_source(
        [(5, first), (second_page, undated)],
        ranges,
        [page(n, T.BALANCE) for n in sorted({5, second_page})],
        doc_titles,
    )
    result, _ = structure_document(base, IngestConfig())
    assert len(result.statements) == 1


@pytest.mark.parametrize("tail_page", [6, 7])
def test_padding_only_continues_an_authorized_part(tail_page: int) -> None:
    scores = [page(5, T.BALANCE), page(tail_page)]
    base = structured_source([(5, PAGE_1), (tail_page, PAGE_2)], [span(T.BALANCE, 5)], scores)
    result, _ = structure_document(base, IngestConfig())
    if tail_page == 6:
        assert [s.source_pages for s in result.statements] == [[5, 6]]
        assert "continuation_of:balance" in result.tables[1].evidence
    else:
        assert [s.source_pages for s in result.statements] == [[5]]
        assert result.tables[1].type is None
        assert "source_outside_range" in result.tables[1].evidence
    no_head = structured_source([(tail_page, PAGE_2)], [span(T.BALANCE, 5)], scores)
    result, _ = structure_document(no_head, IngestConfig())
    assert not result.statements


@pytest.mark.parametrize("on_primary_range", [False, True])
@pytest.mark.parametrize("confident", [False, True])
def test_unmarked_local_title_keeps_continuation(on_primary_range: bool, confident: bool) -> None:
    tail = PAGE_2 if confident else [HEADER, ["Unrecognized qzx", "", "9", "7"]]
    base = structured_source(
        [(5, PAGE_1), (6, tail)],
        [span(T.BALANCE, 5, 6 if on_primary_range else 5)],
        [page(5, T.BALANCE), page(6, T.BALANCE)],
        [
            (5, 50, "Consolidated statement of financial position"),
            (6, 50, "Statement of financial position (continued)"),
        ],
    )
    result, _ = structure_document(base, IngestConfig())
    assert [s.source_pages for s in result.statements] == [[5, 6]]
    assert "continuation_of:balance" in result.tables[1].evidence
    assert "source_scope_unproven" not in result.tables[1].evidence


def test_unmarked_local_title_may_start_on_primary_range() -> None:
    full = [*PAGE_1, *PAGE_2[1:]]
    base = structured_source(
        [(5, full), (7, full)],
        [span(T.BALANCE, 5, 7)],
        [page(5, T.BALANCE), page(6), page(7, T.BALANCE)],
        [
            (5, 50, "Consolidated statement of financial position"),
            (7, 50, "Statement of financial position"),
        ],
    )
    result, _ = structure_document(base, IngestConfig())
    assert [s.source_pages for s in result.statements] == [[5], [7]]
    assert "source_scope_unproven" not in result.tables[1].evidence


def test_two_type_rank_trap_keeps_primary_and_rejects_remote_range() -> None:
    from support import INCOME_1

    full = [*PAGE_1, *PAGE_2[1:]]
    remote = page(32)
    remote.cue_types = [T.BALANCE]
    remote.type_scores = {T.BALANCE: 8}
    base = structured_source(
        [(5, full), (6, INCOME_1), (32, full)],
        [span(T.BALANCE, 5, rank=2), span(T.INCOME, 6), span(T.BALANCE, 32)],
        [page(5, T.BALANCE), page(6, T.INCOME), remote],
    )
    result, _ = structure_document(base, IngestConfig())
    assert [(s.type, s.source_pages) for s in result.statements] == [
        (T.BALANCE, [5]),
        (T.INCOME, [6]),
    ]
    assert "source_secondary_range" in result.tables[2].evidence


@pytest.mark.parametrize("income_page", [9, 40])
def test_disjoint_tied_spans_are_complementary(income_page: int) -> None:
    from support import INCOME_1

    base = structured_source(
        [(5, [*PAGE_1, *PAGE_2[1:]]), (income_page, INCOME_1)],
        [span(T.BALANCE, 5), span(T.INCOME, income_page)],
        [page(5, T.BALANCE), page(income_page, T.INCOME)],
    )
    result, _ = structure_document(base, IngestConfig())
    assert [(s.type, s.source_pages) for s in result.statements] == [
        (T.BALANCE, [5]),
        (T.INCOME, [income_page]),
    ]
    assert all("source_primary_ambiguous" not in t.evidence for t in result.tables)


def test_primary_metadata_keeps_detect_metadata_precedence() -> None:
    base = structured_source(
        [(5, [*PAGE_1, *PAGE_2[1:]])],
        [span(T.BALANCE, 5)],
        [page(5, T.BALANCE)],
        [(5, 50, "Standalone statement of financial position")],
    )
    base.page_texts = {5: "Consolidated annual financial statements"}
    result, _ = structure_document(base, IngestConfig())
    assert len(result.statements) == 1
    assert result.statements[0].consolidated is True


@pytest.mark.parametrize(
    "secondary_title",
    [
        "Statement of financial position",
        "Consolidated statement of financial position",
        "Consolidated standalone statement of financial position",
        "Notes to standalone statement of financial position",
    ],
)
def test_secondary_scope_must_be_explicit_and_opposite(secondary_title: str) -> None:
    full = [*PAGE_1, *PAGE_2[1:]]
    base = structured_source(
        [(5, full), (40, full)],
        [span(T.BALANCE, 5), span(T.BALANCE, 40, rank=2)],
        [page(5, T.BALANCE), page(40, T.BALANCE)],
        [(5, 50, "Consolidated statement of financial position"), (40, 50, secondary_title)],
    )
    result, _ = structure_document(base, IngestConfig())
    assert len(result.statements) == 1
    assert result.tables[1].type is None
    assert any(
        e in result.tables[1].evidence for e in ("source_scope_unproven", "source_secondary_range")
    )


def test_missing_locate_is_an_error() -> None:
    from pydantic import ValidationError

    base = inputs([]).model_dump()
    del base["located"]
    with pytest.raises(ValidationError, match="located"):
        StructureInputs.model_validate(base)


@pytest.mark.parametrize("negative", [True, False])
def test_secondary_padding_summary_cannot_replace_primary(negative: bool) -> None:
    full = [*PAGE_1, *PAGE_2[1:]]
    ranges = [
        span(T.BALANCE, 5),
        span(T.INCOME, 6),
        span(T.INCOME, 29, rank=2),
        span(T.BALANCE, 60, rank=2),
    ]
    scores = [
        page(5, T.BALANCE),
        page(6, T.INCOME),
        page(29, T.INCOME),
        page(60, T.BALANCE),
        page(28, negatives=("notes",) if negative else ()),
    ]
    base = structured_source([(5, full), (28, full), (60, full)], ranges, scores)
    result, _ = structure_document(base, IngestConfig())
    assert [s.source_pages for s in result.statements] == [[5]]
    assert result.tables[1].type is None and result.tables[2].type is None
    assert (
        "source_non_statement_page" if negative else "source_unrelated_padding"
    ) in result.tables[1].evidence
    assert "source_secondary_range" in result.tables[2].evidence


@pytest.mark.parametrize("primary_ok", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_rank_trap_keeps_primary_extraction_failure_visible(
    primary_ok: bool, reverse: bool
) -> None:
    full = [*PAGE_1, *PAGE_2[1:]]
    weak = [HEADER, ["Cash", "", "5", "4"], ["Inventories", "", "10", "8"]]
    rows = [(5, full if primary_ok else weak), (32, full)]
    if reverse:
        rows.reverse()
    scores = [page(5), page(6, T.INCOME), page(7, T.COMPREHENSIVE_INCOME), page(32, T.BALANCE)]
    scores[0].cue_types = [T.BALANCE]
    scores[0].type_scores = {T.BALANCE: 8}
    base = structured_source(
        rows,
        [
            span(T.BALANCE, 5, rank=2),
            span(T.INCOME, 6),
            span(T.COMPREHENSIVE_INCOME, 7),
            span(T.BALANCE, 32),
        ],
        scores,
    )
    result, _ = structure_document(base, IngestConfig())
    assert [s.source_pages for s in result.statements] == ([[5]] if primary_ok else [])
    assert all(t.type is None for t in result.tables if t.page_no == 32)


def test_primary_without_header_does_not_authorize_a_remote_substitute() -> None:
    full = [*PAGE_1, *PAGE_2[1:]]
    no_header = [["", "Notes", "", ""], *full[1:]]
    base = structured_source(
        [(5, no_header), (60, full)],
        [span(T.BALANCE, 5), span(T.INCOME, 6), span(T.BALANCE, 60, rank=2)],
        [page(5, T.BALANCE), page(6, T.INCOME), page(60, T.BALANCE)],
    )
    result, _ = structure_document(base, IngestConfig())
    assert not result.statements
    assert "no_period_header" in result.tables[0].evidence
    assert "source_secondary_range" in result.tables[1].evidence


def test_an_opposite_scope_with_one_date_keeps_only_its_own_date() -> None:
    full = [*PAGE_1, *PAGE_2[1:]]
    partial = [["", "Notes", "31 December 2025 SAR thousands", ""], *full[1:]]
    base = structured_source(
        [(5, full), (6, partial)],
        [span(T.BALANCE, 5), span(T.BALANCE, 6, rank=2)],
        [page(5, T.BALANCE), page(6, T.BALANCE)],
        [
            (5, 50, "Consolidated statement of financial position"),
            (6, 50, "Standalone statement of financial position"),
        ],
    )
    result, _ = structure_document(base, IngestConfig())
    assert len(result.statements) == 2
    assert [p.key for p in result.statements[1].periods] == ["2025-12-31"]
    assert not any(f.startswith("inherited:") for f in result.statements[1].flags)


def test_unproven_tied_blocks_emit_no_statements() -> None:
    full = [*PAGE_1, *PAGE_2[1:]]
    base = structured_source(
        [(5, full), (40, full)],
        [
            span(T.BALANCE, 5),
            span(T.INCOME, 6, rank=2),
            span(T.BALANCE, 40, rank=2),
            span(T.INCOME, 41),
        ],
        [page(5, T.BALANCE), page(6, T.INCOME), page(40, T.BALANCE), page(41, T.INCOME)],
    )
    result, _ = structure_document(base, IngestConfig())
    assert not result.statements
    assert "source_primary_ambiguous" in result.tables[0].evidence


def test_a_four_row_comprehensive_statement_needs_no_row_floor() -> None:
    rows = [
        [
            "",
            "Notes",
            "For the year ended 31 December 2025 SAR thousands",
            "For the year ended 31 December 2024 SAR thousands",
        ],
        ["Other comprehensive income", "", "1", "2"],
        ["Will not be reclassified", "", "1", "2"],
        ["May be reclassified", "", "0", "0"],
        ["Total comprehensive income", "", "1", "2"],
    ]
    base = structured_source(
        [(5, rows)], [span(T.COMPREHENSIVE_INCOME, 5)], [page(5, T.COMPREHENSIVE_INCOME)]
    )
    result, _ = structure_document(base, IngestConfig())
    assert len(result.statements) == 1 and result.statements[0].type is T.COMPREHENSIVE_INCOME


def test_unrecognized_low_confidence_tail_still_continues_authorized_balance() -> None:
    tail = [HEADER, ["Unrecognized qzx", "", "3", "2"]]
    base = structured_source(
        [(5, PAGE_1), (6, tail)], [span(T.BALANCE, 5)], [page(5, T.BALANCE), page(6)]
    )
    result, _ = structure_document(base, IngestConfig())
    assert [s.source_pages for s in result.statements] == [[5, 6]]
    assert "continuation_of:balance" in result.tables[1].evidence


@pytest.mark.parametrize("explicit", [True, False])
def test_two_tied_blocks_require_explicit_opposite_scope_titles(explicit: bool) -> None:
    from support import INCOME_1

    full = [*PAGE_1, *PAGE_2[1:]]
    ranges = [
        span(T.BALANCE, 5),
        span(T.INCOME, 6, rank=2),
        span(T.BALANCE, 40, rank=2),
        span(T.INCOME, 41),
    ]
    scores = [page(5, T.BALANCE), page(6, T.INCOME), page(40, T.BALANCE), page(41, T.INCOME)]
    titles = [
        (5, 50, "Consolidated statement of financial position"),
        (6, 50, "Consolidated statement of income"),
        (40, 50, ("Standalone " if explicit else "") + "statement of financial position"),
        (41, 50, ("Standalone " if explicit else "") + "statement of income"),
    ]
    base = structured_source(
        [(5, full), (6, INCOME_1), (40, full), (41, INCOME_1)], ranges, scores, titles
    )
    result, _ = structure_document(base, IngestConfig())
    assert len(result.statements) == (4 if explicit else 0)
    if explicit:
        assert [s.consolidated for s in result.statements] == [True, False, True, False]
    else:
        assert all("source_primary_ambiguous" in t.evidence for t in result.tables)


def test_income_scopes_keep_separate_metadata_and_cannot_inherit() -> None:
    from support import INCOME_1

    rows = [(5, INCOME_1), (6, INCOME_1)]
    titles = [
        (5, 50, "Consolidated statement of income"),
        (6, 50, "Standalone statement of income"),
    ]
    base = structured_source(
        rows,
        [span(T.INCOME, 5), span(T.INCOME, 6, rank=2)],
        [page(5, T.INCOME), page(6, T.INCOME)],
        titles,
    )
    result, _ = structure_document(base, IngestConfig())
    assert [s.consolidated for s in result.statements] == [True, False]
    assert [len(s.line_items) for s in result.statements] == [3, 3]
    weak = [INCOME_1[0], ["Unrecognized qzx", "", "100", "90"]]
    base = structured_source(
        [(5, INCOME_1), (6, weak)],
        [span(T.INCOME, 5), span(T.INCOME, 6, rank=2)],
        [page(5, T.INCOME), page(6, T.INCOME)],
        titles,
    )
    result, _ = structure_document(base, IngestConfig())
    assert [s.source_pages for s in result.statements] == [[5]]
    assert "below_confidence" in result.tables[1].evidence and result.tables[1].type is None


CONSOLIDATED_BALANCE = "Consolidated statement of financial position"
STANDALONE_BALANCE = "Standalone statement of financial position"


def _placed_source(
    tables: list[tuple[int, list[list[str]], float]],
    ranges: list[StatementRange],
    scores: list[PageScore],
    titles: list[tuple[int, int, str]],
) -> StructureInputs:
    """Tables with an explicit vertical shift, so titles sit above their own table."""
    from support import table

    placed = []
    for i, (n, rows, shift) in enumerate(tables):
        t = table(i, n, rows)
        for cell in t["data"]["table_cells"]:  # type: ignore[index]
            cell["bbox"]["t"] += shift
            cell["bbox"]["b"] += shift
        placed.append(t)
    doc = DlDocument.model_validate(
        {
            "tables": placed,
            "texts": [
                {
                    "self_ref": f"#/texts/{i}",
                    "text": text,
                    "label": "section_header",
                    "prov": [
                        {
                            "page_no": n,
                            "bbox": {
                                "l": 50,
                                "t": y,
                                "r": 700,
                                "b": y + 10,
                                "coord_origin": "TOPLEFT",
                            },
                        }
                    ],
                }
                for i, (n, y, text) in enumerate(titles)
            ],
            "pages": {
                str(n): {"page_no": n, "size": {"width": 800, "height": 1000}}
                for n in {n for n, _, _ in tables}
            },
        }
    )
    base = inputs([("docling/source.json", doc)])
    where = base.located.model_copy(
        update={
            "document": base.located.document.model_copy(update={"page_count": 65}),
            "ranges": ranges,
            "pages": scores,
        }
    )
    return base.model_copy(update={"located": where})


def test_a_resolved_second_block_is_primary_for_its_unique_types() -> None:
    from support import INCOME_1

    comprehensive = [
        [
            "",
            "Notes",
            "For the year ended 31 December 2025 SAR thousands",
            "For the year ended 31 December 2024 SAR thousands",
        ],
        ["Other comprehensive income", "", "1", "2"],
        ["Will not be reclassified", "", "1", "2"],
        ["May be reclassified", "", "0", "0"],
        ["Total comprehensive income", "", "1", "2"],
    ]
    full = [*PAGE_1, *PAGE_2[1:]]
    ranges = [
        span(T.BALANCE, 5),
        span(T.INCOME, 6, rank=2),
        span(T.BALANCE, 40, rank=2),
        span(T.COMPREHENSIVE_INCOME, 41),
    ]
    scores = [
        page(5, T.BALANCE),
        page(6, T.INCOME),
        page(40, T.BALANCE),
        page(41, T.COMPREHENSIVE_INCOME),
    ]
    titles = [
        (5, 50, CONSOLIDATED_BALANCE),
        (6, 50, "Consolidated statement of income"),
        (40, 50, STANDALONE_BALANCE),
        (41, 50, "Standalone statement of comprehensive income"),
    ]
    base = structured_source(
        [(5, full), (6, INCOME_1), (40, full), (41, comprehensive)], ranges, scores, titles
    )
    result, _ = structure_document(base, IngestConfig())
    assert [(s.type, s.consolidated) for s in result.statements] == [
        (T.BALANCE, True),
        (T.BALANCE, False),
        (T.COMPREHENSIVE_INCOME, False),
        (T.INCOME, True),
    ]
    # The same blocks without explicit titles stay ambiguous.
    unmarked = [
        (n, y, t.replace("Consolidated ", "").replace("Standalone ", "")) for n, y, t in titles
    ]
    base = structured_source(
        [(5, full), (6, INCOME_1), (40, full), (41, comprehensive)], ranges, scores, unmarked
    )
    result, _ = structure_document(base, IngestConfig())
    assert not result.statements
    assert all("source_primary_ambiguous" in t.evidence for t in result.tables)


@pytest.mark.parametrize("weak", [True, False])
def test_interleaved_scopes_continue_within_their_own_group(weak: bool) -> None:
    tail_a = [HEADER, ["Unrecognized qzx", "", "3", "2"]] if weak else PAGE_2
    tail_b = [HEADER, ["Unrecognized qzy", "", "4", "1"]] if weak else PAGE_2
    base = _placed_source(
        [(5, PAGE_1, 0), (5, PAGE_1, 350), (6, tail_a, 0), (6, tail_b, 350)],
        [span(T.BALANCE, 5, 6)],
        [page(5, T.BALANCE), page(6, T.BALANCE)],
        [
            (5, 50, CONSOLIDATED_BALANCE),
            (5, 350, STANDALONE_BALANCE),
            (6, 50, CONSOLIDATED_BALANCE),
            (6, 350, STANDALONE_BALANCE),
        ],
    )
    result, _ = structure_document(base, IngestConfig())
    assert [(s.consolidated, s.source_pages) for s in result.statements] == [
        (True, [5, 6]),
        (False, [5, 6]),
    ]
    expected = len(PAGE_1) - 1 + len(tail_a) - 1
    assert [len(s.line_items) for s in result.statements] == [expected, expected]


def test_a_tie_without_any_candidate_block_is_not_resolved() -> None:
    p = source_plan(
        [span(T.BALANCE, 5), span(T.BALANCE, 40, rank=2)],
        [page(5), page(40)],
        [T.BALANCE],
        45,
        1,
    )
    assert p.ambiguous and p.primary_span is None and len(p.tied_spans) == 2
    assert resolve_tied_blocks(p) == p
