"""EPS checks use the note's units and attributable profit, never consolidated profit."""

from decimal import Decimal

import pytest
from support import page, span, structured_source, table

from fra_core.schemas import Statement, StatementType, TextSource
from fra_core.taxonomy.loader import load_taxonomy
from fra_ingest.config import IngestConfig
from fra_ingest.docling_json import DlDocument
from fra_ingest.eps_checks import EpsNote, check_eps, structure_eps_note
from fra_ingest.label_mapping import map_statement
from fra_ingest.label_match import LabelIndex
from fra_ingest.structure import structure_document
from fra_ingest.table_grid import build_grid

INDEX = LabelIndex(load_taxonomy())
SCOPE = ("Parent Company Ltd", "Consolidated Financial Statements")


def statement_rows(arabic: bool = False) -> list[list[str]]:
    return [
        ["", "Notes", "2025 SAR '000", "2024 SAR '000"],
        ["Revenue", "", "3000000", "2800000"],
        ["Cost of sales", "", "(543327)", "(486289)"],
        ["Profit for the year", "", "2456673", "2313711"],
        ["Shareholders of the Company", "", "2456093", "2313100"],
        ["Non-Controlling Interests", "", "580", "611"],
        ["ربحية السهم" if arabic else "Earnings per share", "", "", ""],
        ["- الأساسية" if arabic else "- Basic", "32", "2.48", "2.34"],
        ["- المخفضة" if arabic else "- Diluted", "32", "2.46", "2.31"],
    ]


def note_rows(arabic: bool = False) -> list[list[str]]:
    return [
        ["", "", "2025", "2024"],
        [
            "ربح السنة العائد إلى المساهمين في الشركة بآلاف X"
            if arabic
            else "Profit for the year attributable to the shareholders of the Company X '000",
            "",
            "2456093",
            "2313100",
        ],
        ['عدد الأسهم " بالآلاف "' if arabic else "Number of shares '000", "", "", ""],
        [
            "المتوسط المرجح لعدد الأسهم العادية لأغراض احتساب ربحية السهم الأساسية"
            if arabic
            else "Weighted average number of ordinary shares for the purposes of basic earnings",
            "",
            "989306",
            "988191",
        ],
        [
            "المتوسط المرجح لعدد الأسهم العادية لأغراض احتساب ربحية السهم المخفضة"
            if arabic
            else "Weighted average number of ordinary shares for the purposes of diluted earning",
            "",
            "1000000",
            "1000000",
        ],
    ]


def make_note(
    rows: list[list[str]] | None = None,
    arabic: bool = False,
    scope: tuple[str, ...] = SCOPE,
) -> EpsNote:
    doc = DlDocument.model_validate(
        {
            "tables": [table(1, 2, rows or note_rows(arabic))],
            "pages": {"2": {"page_no": 2, "size": {"width": 800, "height": 1000}}},
        }
    )
    note = structure_eps_note(
        build_grid(doc.tables[0], doc, "docling/note.json"),
        [*scope, "٣٢ - ربحية السهم" if arabic else "32. EARNINGS PER SHARE"],
        INDEX,
        source=TextSource.TEXT,
    )
    assert note is not None
    return note


def make_statement(arabic: bool = False) -> Statement:
    inputs = structured_source(
        [(1, statement_rows(arabic))],
        [span(StatementType.INCOME, 1)],
        [page(1, StatementType.INCOME)],
        [(1, 10, SCOPE[0]), (1, 25, SCOPE[1])],
    )
    result, _ = structure_document(inputs, IngestConfig())
    return result.statements[0]


@pytest.mark.parametrize("arabic", [False, True])
def test_context_mapping_and_note_calculation(arabic: bool) -> None:
    statement = make_statement(arabic)
    assert [i.canonical_id for i in statement.line_items[-2:]] == ["eps_basic", "eps_diluted"]
    checks = check_eps(statement, [make_note(arabic=arabic)])
    assert len(checks) == 4
    assert all(c.status == "pass" for c in checks)
    assert all("numerator=" in c.detail and "shares=" in c.detail for c in checks)
    assert all(c.tolerance is not None and c.tolerance > Decimal("0.005") for c in checks)


def test_eps_without_header_stays_unmapped() -> None:
    statement = make_statement()
    statement = statement.model_copy(
        update={
            "line_items": [i for i in statement.line_items if i.raw_label != "Earnings per share"]
        }
    )
    mapped = map_statement(statement, INDEX, [])
    assert mapped.line_items[-1].canonical_id is None


def test_perturbed_eps_fails_and_review_holds() -> None:
    from fra_ingest.review import review_statement, vouched_cells

    statement = make_statement()
    eps = statement.line_items[-2]
    bad = eps.model_copy(
        update={"cells": [c.model_copy(update={"reported": Decimal("2.58")}) for c in eps.cells]}
    )
    statement = statement.model_copy(
        update={"line_items": [*statement.line_items[:-2], bad, statement.line_items[-1]]}
    )
    checks = check_eps(statement, [make_note()])
    assert [c.status for c in checks[:2]] == ["fail", "fail"]
    assert not any(i == eps.id for i, _ in vouched_cells(checks))
    assert "eps_failed" in review_statement(statement, checks, primary=True).reasons


def test_absent_note_creates_no_result_and_stays_unchecked() -> None:
    statement = make_statement()
    assert check_eps(statement, []) == []


@pytest.mark.parametrize(
    "change,reason",
    [
        ("unit", "share_scale_unsettled"),
        ("numerator", "numerator_ambiguous"),
        ("reference", "note_reference_unsettled"),
        ("duplicate", "note_ambiguous"),
        ("denominator", "shares_not_positive"),
    ],
)
def test_uncertain_note_skips(change: str, reason: str) -> None:
    rows = note_rows()
    if change == "unit":
        rows[2][0] = "Number of shares"
    if change == "numerator":
        rows.insert(2, rows[1].copy())
    if change == "denominator":
        rows[3][2] = "0"
        rows[3][3] = "0"
    note = make_note(rows)
    if change == "reference":
        note = make_note()
        note.references = frozenset({"99"})
    checks = check_eps(make_statement(), [note, note] if change == "duplicate" else [note])
    assert checks and all(c.status == "skipped" for c in checks[:2])
    assert all(reason in c.detail for c in checks[:2])


def test_structure_integration_counts_eps_cells() -> None:
    from fra_ingest.review import vouched_cells

    source = structured_source(
        [(1, statement_rows()), (2, note_rows())],
        [span(StatementType.INCOME, 1)],
        [page(1, StatementType.INCOME)],
        [(n, y, text) for n in (1, 2) for y, text in ((10, SCOPE[0]), (25, SCOPE[1]))]
        + [(2, 40, "32. EARNINGS PER SHARE")],
    )
    result, checks = structure_document(source, IngestConfig())
    assert len(result.statements) == 1  # a note never becomes another statement
    eps = [c for c in checks if c.kind == "eps"]
    assert len(eps) == 4 and all(c.status == "pass" for c in eps)
    ids = {i.id for i in result.statements[0].line_items[-2:]}
    assert len([(i, p) for i, p in vouched_cells(eps) if i in ids]) == 4


def test_only_the_attributable_numerator_can_settle_eps() -> None:
    rows = note_rows()
    rows[1][2:] = ["2456673", "2313711"]  # consolidated profit would still round to the same EPS
    checks = check_eps(make_statement(), [make_note(rows)])
    assert all(c.status == "fail" and "numerator_tie_failed" in c.detail for c in checks)


def test_consolidated_profit_is_never_a_fallback_for_missing_attributable_profit() -> None:
    statement = make_statement()
    statement = statement.model_copy(
        update={
            "line_items": [
                i
                for i in statement.line_items
                if i.canonical_id != "net_income_attributable_parent"
            ]
        }
    )
    checks = check_eps(statement, [make_note()])
    assert all(c.status == "skipped" and "numerator_ambiguous" in c.detail for c in checks)


def test_share_units_are_independent_of_monetary_scale() -> None:
    rows = note_rows()
    rows[2][0] = "Number of shares (units)"
    rows[3][2:] = ["989306000", "988191000"]
    rows[4][2:] = ["1000000000", "1000000000"]
    checks = check_eps(make_statement(), [make_note(rows)])
    assert all(c.status == "pass" for c in checks)
    assert all("scale=1]" in c.detail for c in checks)


def test_diluted_denominator_is_never_borrowed_from_basic() -> None:
    note = make_note(note_rows()[:-1])
    checks = check_eps(make_statement(), [note])
    assert [c.status for c in checks] == ["pass", "pass", "skipped", "skipped"]


def test_period_identity_not_just_the_key_must_tie() -> None:
    note = make_note()
    note.part.periods = [p.model_copy(update={"restated": True}) for p in note.part.periods]
    checks = check_eps(make_statement(), [note])
    assert all(c.status == "skipped" and "period_unsettled" in c.detail for c in checks)


def test_arabic_digit_normalization_does_not_make_an_operand_uncertain() -> None:
    note = make_note(arabic=True)
    note.part.line_items = [
        i.model_copy(
            update={
                "cells": [
                    c.model_copy(
                        update={"flags": [*c.flags, "arabic_indic_digits", "digits_reversed"]}
                    )
                    for c in i.cells
                ]
            }
        )
        for i in note.part.line_items
    ]
    assert all(c.status == "pass" for c in check_eps(make_statement(True), [note]))


def test_uncertain_note_operand_is_not_vouched_for() -> None:
    note = make_note()
    note.part.line_items = [
        i.model_copy(
            update={
                "cells": [
                    c.model_copy(update={"flags": [*c.flags, "ambiguous_separator"]})
                    for c in i.cells
                ]
            }
        )
        for i in note.part.line_items
    ]
    assert all(c.status == "skipped" for c in check_eps(make_statement(), [note]))


def test_negative_profit_uses_the_signed_rounding_interval() -> None:
    statement = make_statement()
    note = make_note()
    statement = statement.model_copy(
        update={
            "line_items": [
                i.model_copy(
                    update={
                        "cells": [
                            c.model_copy(
                                update={"reported": -c.reported if c.reported is not None else None}
                            )
                            for c in i.cells
                        ]
                    }
                )
                if i.canonical_id in ("eps_basic", "eps_diluted", "net_income_attributable_parent")
                else i
                for i in statement.line_items
            ]
        }
    )
    note.part.line_items = [
        i.model_copy(
            update={
                "cells": [
                    c.model_copy(
                        update={"reported": -c.reported if c.reported is not None else None}
                    )
                    for c in i.cells
                ]
            }
        )
        if i.id in note.numerator_ids
        else i
        for i in note.part.line_items
    ]
    assert all(c.status == "pass" for c in check_eps(statement, [note]))


def test_note_currency_conflict_cannot_disappear_as_unknown_currency() -> None:
    rows = note_rows()
    rows[0][2:] = ["2025 SAR '000", "2024 EGP '000"]
    checks = check_eps(make_statement(), [make_note(rows)])
    assert all(c.status == "skipped" and "currency_conflict" in c.detail for c in checks)


def test_conflicting_share_scale_is_unsettled() -> None:
    rows = note_rows()
    rows[2][0] = "Number of shares in thousands / millions"
    checks = check_eps(make_statement(), [make_note(rows)])
    assert all(c.status == "skipped" and "share_scale_unsettled" in c.detail for c in checks)


def test_conflicting_row_scale_cannot_fall_back_to_a_section_scale() -> None:
    rows = note_rows()
    rows[3][0] += " (thousands / millions)"
    checks = check_eps(make_statement(), [make_note(rows)])
    assert all(c.status == "skipped" and "share_scale_unsettled" in c.detail for c in checks[:2])


def test_competing_note_headings_do_not_authorize_one_table() -> None:
    note = make_note()
    note.references = frozenset({"32", "99"})
    checks = check_eps(make_statement(), [note])
    assert all(c.status == "skipped" and "note_ambiguous" in c.detail for c in checks)


def test_note_period_evidence_is_not_ignored_by_the_check() -> None:
    from fra_core.schemas.statement import PeriodEvidence

    source = structured_source(
        [(1, statement_rows()), (2, note_rows())],
        [span(StatementType.INCOME, 1)],
        [page(1, StatementType.INCOME)],
        [(n, y, text) for n in (1, 2) for y, text in ((10, SCOPE[0]), (25, SCOPE[1]))]
        + [(2, 40, "32. EARNINGS PER SHARE")],
    )
    evidence = PeriodEvidence.model_validate(
        {
            "observations": [
                {
                    "source_id": f"read-{n}",
                    "raw_text": text,
                    "docling_path": "docling/source.json",
                    "confidence": 1.0,
                    "provenance": {
                        "page_no": 2,
                        "table_ref": "#/tables/1",
                        "row": 0,
                        "col": 2,
                        "source": "ocr",
                        "bbox": {"left": 360, "top": 100, "right": 480, "bottom": 112},
                    },
                }
                for n, text in enumerate(("2025", "2026"))
            ]
        }
    )
    source = source.model_copy(update={"period_evidence": (evidence,)})
    _, checks = structure_document(source, IngestConfig())
    eps = [c for c in checks if c.kind == "eps"]
    assert eps and all(c.status == "skipped" and "period_unsettled" in c.detail for c in eps)


def test_eps_uses_joint_profit_interval() -> None:
    statement = make_statement()
    statement = statement.model_copy(
        update={
            "line_items": [
                i.model_copy(
                    update={
                        "cells": [
                            c.model_copy(
                                update={
                                    "reported": Decimal("1500")
                                    if i.canonical_id == "net_income_attributable_parent"
                                    else Decimal("2.40")
                                }
                            )
                            for c in i.cells
                        ]
                    }
                )
                if i.canonical_id in ("net_income_attributable_parent", "eps_basic", "eps_diluted")
                else i
                for i in statement.line_items
            ]
        }
    )
    rows = note_rows()
    rows[1][0] = "Profit for the year attributable to the shareholders of the Company (millions)"
    rows[1][2:] = ["2", "2"]
    rows[2][0] = "Number of shares (units)"
    rows[3][2:] = rows[4][2:] = ["1000000", "1000000"]
    checks = check_eps(statement, [make_note(rows)])
    assert len(checks) == 4
    assert all(c.status == "fail" for c in checks)


def test_other_entity_and_separate_note_cannot_authorize_eps() -> None:
    source = structured_source(
        [(1, statement_rows()), (2, note_rows())],
        [span(StatementType.INCOME, 1)],
        [page(1, StatementType.INCOME)],
        [
            (1, 10, SCOPE[0]),
            (1, 25, "Consolidated Income Statement"),
            (2, 10, "Other Company Ltd"),
            (2, 25, "Separate Financial Statements"),
            (2, 40, "32. EARNINGS PER SHARE"),
        ],
    )
    _, checks = structure_document(source, IngestConfig())
    eps = [c for c in checks if c.kind == "eps"]
    assert len(eps) == 4
    assert all(c.status == "skipped" and "scope_conflict" in c.detail for c in eps)


@pytest.mark.parametrize("arabic", [False, True])
@pytest.mark.parametrize(
    "negation", ["undiluted", "non-diluted", "un-diluted", "excl. diluted", "not fully diluted"]
)
def test_negated_diluted_shares_never_authorize_diluted_eps(arabic: bool, negation: str) -> None:
    statement = make_statement(arabic)
    basic, diluted = statement.line_items[-2:]
    diluted = diluted.model_copy(
        update={
            "cells": [
                c.model_copy(update={"reported": b.reported})
                for c, b in zip(diluted.cells, basic.cells, strict=True)
            ]
        }
    )
    statement = statement.model_copy(update={"line_items": [*statement.line_items[:-1], diluted]})
    rows = note_rows(arabic)[:-1]
    rows[3][0] = (
        "المتوسط المرجح لعدد الأسهم العادية (غير المخفضة)"
        if arabic
        else f"Weighted average number of ordinary shares ({negation})"
    )
    doc = DlDocument.model_validate(
        {
            "tables": [table(1, 2, rows)],
            "pages": {"2": {"page_no": 2, "size": {"width": 800, "height": 1000}}},
        }
    )
    note = structure_eps_note(
        build_grid(doc.tables[0], doc, "docling/note.json"),
        ["٣٢ - ربحية السهم" if arabic else "32. EARNINGS PER SHARE"],
        INDEX,
        source=TextSource.TEXT,
    )
    # The negated label is no share count at all: either no note, or no diluted denominator.
    checks = check_eps(statement, [] if note is None else [note])
    assert not any(c.status == "pass" for c in checks)
    assert all(c.status == "skipped" for c in checks[2:])


@pytest.mark.parametrize("side", ["statement", "note"])
def test_unknown_consolidation_scope_skips(side: str) -> None:
    statement = make_statement()
    scope: tuple[str, ...] = SCOPE
    if side == "statement":
        statement = statement.model_copy(update={"consolidated": None})
    else:
        scope = (SCOPE[0],)
    checks = check_eps(statement, [make_note(scope=scope)])
    assert len(checks) == 4
    assert all(c.status == "skipped" and "scope_unsettled" in c.detail for c in checks)


@pytest.mark.parametrize("consolidated", [False, True])
@pytest.mark.parametrize("unknown", ["statement", "note", "both"])
def test_known_consolidation_allows_unknown_entity_names(consolidated: bool, unknown: str) -> None:
    statement = make_statement().model_copy(update={"consolidated": consolidated})
    note = make_note()
    note.metadata.consolidated = consolidated
    if unknown in ("statement", "both"):
        statement = statement.model_copy(update={"entity_name": None})
    if unknown in ("note", "both"):
        note.metadata.entity_name = None
    checks = check_eps(statement, [note])
    assert len(checks) == 4 and all(c.status == "pass" for c in checks)


def test_known_entity_names_match_case_insensitively() -> None:
    note = make_note()
    assert note.metadata.entity_name is not None
    note.metadata.entity_name = note.metadata.entity_name.swapcase()
    checks = check_eps(make_statement(), [note])
    assert len(checks) == 4 and all(c.status == "pass" for c in checks)


@pytest.mark.parametrize("unknown", ["statement", "note", "both"])
def test_unknown_names_do_not_hide_consolidation_conflict(unknown: str) -> None:
    statement = make_statement()
    note = make_note(scope=(SCOPE[0], "Separate Financial Statements"))
    if unknown in ("statement", "both"):
        statement = statement.model_copy(update={"entity_name": None})
    if unknown in ("note", "both"):
        note.metadata.entity_name = None
    checks = check_eps(statement, [note])
    assert len(checks) == 4
    assert all(c.status == "skipped" and "scope_conflict" in c.detail for c in checks)


def test_same_scope_notes_with_unknown_names_remain_ambiguous() -> None:
    statement = make_statement().model_copy(update={"entity_name": None})
    notes = [make_note(scope=(SCOPE[1],)), make_note(scope=(SCOPE[1],))]
    checks = check_eps(statement, notes)
    assert len(checks) == 4
    assert all(c.status == "skipped" and "note_ambiguous" in c.detail for c in checks)


@pytest.mark.parametrize(
    "scope",
    [
        ("Other Company Ltd", SCOPE[1]),
        (SCOPE[0], "Separate Financial Statements"),
    ],
)
def test_each_scope_dimension_must_match(scope: tuple[str, ...]) -> None:
    checks = check_eps(make_statement(), [make_note(scope=scope)])
    assert all(c.status == "skipped" and "scope_conflict" in c.detail for c in checks)


def test_competing_reference_selects_only_matching_scope() -> None:
    notes = [make_note(scope=("Other Company Ltd", "Separate Financial Statements")), make_note()]
    for ordered in (notes, list(reversed(notes))):
        checks = check_eps(make_statement(), ordered)
        assert len(checks) == 4 and all(c.status == "pass" for c in checks)
    checks = check_eps(make_statement(), [*notes, make_note()])
    assert all(c.status == "skipped" and "note_ambiguous" in c.detail for c in checks)


@pytest.mark.parametrize(
    "cue,expected",
    [
        ("basic", "basic"),
        ("diluted", "diluted"),
        ("undiluted", None),
        ("non-diluted", None),
        ("un-diluted", None),
        ("not diluted", None),
        ("excl. diluted", None),
        ("not fully diluted", None),
        ("fully diluted", None),
        ("diluted (restated)", None),
        ("diluted (thousands)", "diluted"),
        ("غير المخفضة", None),
        ("غيرالمخفضة", None),
        ("وغير المخفضة", None),
        ("وغيرالمخفضة", None),
        ("والمخفضة", "diluted"),
        ("الأساسية والمخفضة", None),
        ("غير المخفضة والمخفضة", None),
        ("basic and diluted", None),
        ("الأساسية و المخفضة", None),
        ("non-diluted and diluted", None),
        ("غير المخفضة و المخفضة", None),
    ],
)
def test_share_kind_requires_unambiguous_positive_cue(cue: str, expected: str | None) -> None:
    from fra_ingest.eps_checks import _share_kind

    label = (
        "المتوسط المرجح لعدد الأسهم العادية"
        if any("\u0600" <= c <= "\u06ff" for c in cue)
        else "Weighted average number of ordinary shares"
    )
    assert _share_kind(f"{label} ({cue})") == expected


def test_conflicting_local_consolidation_scope_is_unsettled() -> None:
    note = make_note(scope=(*SCOPE, "Separate Financial Statements"))
    checks = check_eps(make_statement(), [note])
    assert all(c.status == "skipped" and "scope_unsettled" in c.detail for c in checks)


@pytest.mark.parametrize("visual", [True, False])
def test_note_scope_in_reversed_arabic_page_text_is_read(visual: bool) -> None:
    """A page footer extracted letter-reversed (ةدحوملا for الموحدة) still states the scope."""
    source = structured_source(
        [(1, statement_rows()), (2, note_rows())],
        [span(StatementType.INCOME, 1)],
        [page(1, StatementType.INCOME)],
        [(1, 25, "Consolidated Income Statement"), (2, 40, "32. EARNINGS PER SHARE")],
    )
    footer = (
        "ةدحوملا ةيلاملا مئاوقلا نم ءزج ةقفرملا تاحاضيلاا" if visual else "القوائم المالية الموحدة"
    )
    source = source.model_copy(
        update={
            "page_texts": {**source.page_texts, 2: footer},
            "visual_pages": {2} if visual else set(),
        }
    )
    _, checks = structure_document(source, IngestConfig())
    eps = [c for c in checks if c.kind == "eps"]
    assert len(eps) == 4
    assert all(c.status == "pass" for c in eps), [c.detail for c in eps]


@pytest.mark.parametrize(
    "heading",
    [
        "Earnings per share (Saudi halalas)",
        "Earnings per share (fils)",
        "Earnings per share (cents)",
        "ربحية السهم (هللة)",
        "ربحية السهم (قرش)",
    ],
)
def test_eps_printed_in_a_currency_subunit_is_never_vouched(heading: str) -> None:
    rows = statement_rows(arabic=heading.startswith("ربحية"))
    rows[6][0] = heading
    inputs = structured_source(
        [(1, rows)], [span(StatementType.INCOME, 1)], [page(1, StatementType.INCOME)]
    )
    result, _ = structure_document(inputs, IngestConfig())
    statement = result.statements[0]
    checks = check_eps(statement, [make_note(arabic=heading.startswith("ربحية"))])
    assert len(checks) == 4
    assert all(c.status == "skipped" and "eps_unit_unsupported" in c.detail for c in checks)


def test_a_share_row_without_an_eps_note_heading_does_not_remove_the_statement() -> None:
    rows = statement_rows()
    rows.append(
        [
            "Weighted average number of ordinary shares for the purposes of basic earnings",
            "",
            "989306",
            "988191",
        ]
    )
    inputs = structured_source(
        [(1, rows)], [span(StatementType.INCOME, 1)], [page(1, StatementType.INCOME)]
    )
    result, _ = structure_document(inputs, IngestConfig())
    assert [s.type for s in result.statements] == [StatementType.INCOME]


def _structured_eps(rows: list[list[str]]) -> Statement:
    inputs = structured_source(
        [(1, rows)],
        [span(StatementType.INCOME, 1)],
        [page(1, StatementType.INCOME)],
        [(1, 10, SCOPE[0]), (1, 25, SCOPE[1])],
    )
    result, _ = structure_document(inputs, IngestConfig())
    return result.statements[0]


def test_subunit_named_in_the_statement_header_is_never_vouched() -> None:
    statement = _structured_eps(statement_rows())
    checks = check_eps(statement, [make_note()], header_text="Notes 2025 Saudi halalas 2024")
    assert len(checks) == 4
    assert all(c.status == "skipped" and "eps_unit_unsupported" in c.detail for c in checks)


def test_subunit_named_in_a_unit_row_above_the_eps_rows_is_never_vouched() -> None:
    rows = statement_rows()
    rows.insert(6, ["(in halalas)", "", "", ""])
    statement = _structured_eps(rows)
    checks = check_eps(statement, [make_note()])
    assert len(checks) == 4
    assert all(c.status == "skipped" and "eps_unit_unsupported" in c.detail for c in checks)


@pytest.mark.parametrize(
    "heading",
    [
        "Earnings per share (recent years)",
        "Earnings per share - Centre",
        "Earnings per share - Vincent",
        "Earnings per share (percentage of capital)",
        "ربحية السهم للسنتين",
    ],
)
def test_words_that_merely_contain_a_subunit_cue_do_not_skip(heading: str) -> None:
    rows = statement_rows(arabic=heading.startswith("ربحية"))
    rows[6][0] = heading
    checks = check_eps(_structured_eps(rows), [make_note(arabic=heading.startswith("ربحية"))])
    assert len(checks) == 4
    assert all(c.status == "pass" for c in checks), [c.detail for c in checks]


@pytest.mark.parametrize("unit", ["(p)", "(c/share)", "(øre)", "(sen)", "(halalas)"])
def test_eps_printed_in_a_subunit_the_guard_does_not_know_fails_instead_of_passing(
    unit: str,
) -> None:
    """Profit / shares is in the statement currency, so a subunit figure is 100x off: it fails."""
    rows = statement_rows()
    rows[6][0] = f"Earnings per share {unit}"
    rows[7][2:] = ["248", "234"]
    rows[8][2:] = ["246", "231"]
    checks = check_eps(_structured_eps(rows), [make_note()])
    assert len(checks) == 4
    assert all(c.status in ("fail", "skipped") for c in checks)
    assert not any(c.status == "pass" for c in checks)


def test_a_later_statement_title_keeps_its_table_out_of_an_earlier_eps_note() -> None:
    rows = statement_rows()
    rows.append(
        [
            "Weighted average number of ordinary shares for the purposes of basic earnings",
            "",
            "989306",
            "988191",
        ]
    )
    inputs = structured_source(
        [(1, rows)],
        [span(StatementType.INCOME, 1)],
        [page(1, StatementType.INCOME)],
        [(1, 10, "32. EARNINGS PER SHARE"), (1, 25, "Consolidated Income Statement")],
    )
    result, _ = structure_document(inputs, IngestConfig())
    assert [s.type for s in result.statements] == [StatementType.INCOME]


@pytest.mark.parametrize("unit", ["(USD per share)", "(US dollars)", "(EUR)", "(GBP)"])
def test_an_eps_label_naming_another_currency_is_never_vouched(unit: str) -> None:
    rows = statement_rows()
    rows[6][0] = f"Earnings per share {unit}"
    checks = check_eps(_structured_eps(rows), [make_note()])
    assert len(checks) == 4
    assert all(c.status == "skipped" and "currency_conflict" in c.detail for c in checks)


def test_the_statements_own_currency_label_on_eps_is_accepted() -> None:
    rows = statement_rows()
    rows[6][0] = "Earnings per share (SAR per share)"
    checks = check_eps(_structured_eps(rows), [make_note()])
    assert all(c.status == "pass" for c in checks), [c.detail for c in checks]


@pytest.mark.parametrize(
    "lower,upper,expected",
    [
        ("2.475", "2.485", True),
        ("-2.485", "-2.475", True),
        ("0.0", "5.5", False),
        ("-0.00003", "0.0099", False),
        ("-1", "1", False),
        ("0.01", "1.00", False),
        ("0.01", "0.99", True),
    ],
)
def test_an_interval_that_cannot_tell_a_hundredfold_slip_does_not_vouch(
    lower: str, upper: str, expected: bool
) -> None:
    from fra_ingest.eps_checks import _discriminates

    assert _discriminates(Decimal(lower), Decimal(upper)) is expected


def test_imprecise_operands_skip_instead_of_passing() -> None:
    rows = statement_rows()
    rows[4][2:] = ["1", "1"]  # attributable profit: 1 thousand
    rows[7][2:] = ["0.01", "0.01"]
    rows[8][2:] = ["0.01", "0.01"]
    note = note_rows()
    note[1][2:] = ["1", "1"]
    note[3][2:] = note[4][2:] = ["100", "100"]
    checks = check_eps(_structured_eps(rows), [make_note(note)])
    assert len(checks) == 4
    assert not any(c.status == "pass" for c in checks), [c.detail for c in checks]
