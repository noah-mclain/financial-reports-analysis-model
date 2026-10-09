"""Caller-supplied period observations must hold ambiguity before values are bound."""

from __future__ import annotations

from decimal import Decimal

import pytest
from test_structure import PAGE_1, _balance_tables, inputs, table

from fra_core.schemas import Statement
from fra_ingest.config import IngestConfig
from fra_ingest.results import StructureResult
from fra_ingest.structure import StructureInputs, structure_document
from fra_ingest.table_grid import Grid

PATH = "synthetic.json"


def evidence(
    col: int = 2,
    *,
    page: int = 1,
    ref: str = "#/tables/0",
    texts: tuple[str, ...] = ("31/12/3025", "31/12/2025"),
) -> dict[str, object]:
    return {
        "observations": [
            {
                "source_id": f"read-{n}",
                "raw_text": text,
                "docling_path": PATH,
                "confidence": 1.0,
                "provenance": {
                    "page_no": page,
                    "table_ref": ref,
                    "row": 0,
                    "col": col,
                    "source": "ocr",
                    "bbox": {
                        "left": 60 + 150 * col,
                        "top": 100,
                        "right": 180 + 150 * col,
                        "bottom": 112,
                    },
                },
            }
            for n, text in enumerate(texts)
        ]
    }


def supplied(
    *observations: dict[str, object], rows: list[list[str]] | None = None
) -> StructureInputs:
    rows = rows or [["", "Notes", "31/12/3025 SAR '000", "31/12/2024 SAR '000"], *PAGE_1[1:]]
    base = inputs([(PATH, _balance_tables(table(0, 1, rows)))])
    return StructureInputs.model_validate({**base.model_dump(), "period_evidence": observations})


def test_conflicting_valid_years_hold_only_the_affected_column() -> None:
    result, _ = structure_document(supplied(evidence()), IngestConfig())
    statement = result.statements[0]
    assert [p.key for p in statement.periods] == ["2024-12-31"]
    assert "period_conflict:2" in statement.flags
    assert result.reviews[0].status == "needs_review"
    assert "period_conflict:2" in result.reviews[0].reasons
    assert statement.line_items[0].cells[0].reported == Decimal("8")
    conflict = statement.period_conflicts[0]
    assert [c.observation.raw_text for c in conflict.candidates] == ["31/12/3025", "31/12/2025"]
    assert [c.period.key for c in conflict.candidates if c.period] == ["3025-12-31", "2025-12-31"]
    assert Statement.model_validate_json(statement.model_dump_json()) == statement
    assert StructureResult.model_validate_json(result.model_dump_json()) == result


def test_all_conflicted_columns_remain_a_reviewable_table() -> None:
    result, _ = structure_document(
        supplied(evidence(), evidence(3, texts=("31/12/2024", "31/12/2023"))), IngestConfig()
    )
    assert result.statements == []
    decision = result.tables[0]
    assert decision.statement_id is None
    assert len(decision.period_conflicts) == 2
    assert "period_conflict:2" in decision.evidence
    assert "period_conflict:3" in decision.evidence
    assert StructureResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    "texts",
    [
        ("31/12/3025", "3025-12-31"),
        ("31/12/3025", " 31/12/3025 ", "31/12/3025"),
    ],
)
def test_agreeing_computed_dates_are_not_conflicts(texts: tuple[str, ...]) -> None:
    result, _ = structure_document(supplied(evidence(texts=texts)), IngestConfig())
    assert result.statements[0].periods[0].key == "3025-12-31"
    assert result.statements[0].period_conflicts == ()


def test_an_unparseable_period_observation_holds_the_column() -> None:
    result, _ = structure_document(
        supplied(evidence(texts=("31/12/3025", "31/13/2025"))), IngestConfig()
    )
    conflict = result.statements[0].period_conflicts[0]
    assert conflict.reason == "unparseable_observation"
    assert conflict.candidates[1].period is None
    assert [p.key for p in result.statements[0].periods] == ["2024-12-31"]


@pytest.mark.parametrize(
    "malformed",
    [
        "31/12-3025",
        "31/12.3025",
        "31-12/3025",
        "31-12.3025",
        "31.12/3025",
        "31.12-3025",
        "٣١/١٢-٣٠٢٥",
    ],
)
def test_mixed_numeric_separators_hold_column_and_preserve_observations(malformed: str) -> None:
    base = supplied(evidence(texts=("31/12/3025", malformed)))
    originals = base.period_evidence[0].observations
    result, _ = structure_document(base, IngestConfig())
    statement = result.statements[0]
    assert [p.key for p in statement.periods] == ["2024-12-31"]
    assert "period_conflict:2" in statement.flags
    assert result.reviews[0].status == "needs_review"
    assert "period_conflict:2" in result.reviews[0].reasons
    conflict = statement.period_conflicts[0]
    assert conflict.reason == "unparseable_observation"
    assert conflict.candidates[0].period is not None
    assert conflict.candidates[1].period is None
    assert tuple(candidate.observation for candidate in conflict.candidates) == originals
    assert base.period_evidence[0].observations == originals
    assert Statement.model_validate_json(statement.model_dump_json()) == statement


@pytest.mark.parametrize(
    "field,value",
    [
        ("docling_path", "wrong.json"),
        ("page_no", 2),
        ("table_ref", "#/tables/9"),
        ("col", 0),
        ("row", 1),
    ],
)
def test_evidence_with_wrong_scope_is_rejected(field: str, value: object) -> None:
    raw = evidence()
    observations = raw["observations"]
    assert isinstance(observations, list)
    for observation in observations:
        target = observation if field == "docling_path" else observation["provenance"]
        target[field] = value
    with pytest.raises(ValueError, match="period evidence"):
        structure_document(supplied(raw), IngestConfig())


def test_missing_input_preserves_existing_binding() -> None:
    result, _ = structure_document(supplied(), IngestConfig())
    assert result.statements[0].periods[0].key == "3025-12-31"


@pytest.mark.parametrize(
    "box",
    [
        {"left": 360, "top": 120, "right": 480, "bottom": 132},
        {"left": -1, "top": 100, "right": 480, "bottom": 112},
        {"left": 360, "top": 100, "right": 900, "bottom": 112},
        {"left": 360, "top": 100, "right": 650, "bottom": 112},
        {"left": 360, "top": 100, "right": 480, "bottom": float("nan")},
    ],
)
def test_invalid_or_ambiguous_geometry_is_rejected(box: dict[str, float]) -> None:
    raw = evidence()
    observations = raw["observations"]
    assert isinstance(observations, list)
    observations[1]["provenance"]["bbox"] = box
    with pytest.raises(ValueError, match="period evidence"):
        structure_document(supplied(raw), IngestConfig())


def test_duplicate_source_ids_cannot_pose_as_independent_observations() -> None:
    raw = evidence()
    observations = raw["observations"]
    assert isinstance(observations, list)
    observations[1]["source_id"] = observations[0]["source_id"]
    with pytest.raises(ValueError, match="duplicate source IDs"):
        supplied(raw)


def test_duplicate_column_groups_are_rejected() -> None:
    with pytest.raises(ValueError, match="repeats column"):
        structure_document(supplied(evidence(), evidence()), IngestConfig())


def test_mixed_observation_scopes_are_rejected() -> None:
    raw = evidence()
    observations = raw["observations"]
    assert isinstance(observations, list)
    observations[1]["provenance"]["page_no"] = 2
    with pytest.raises(ValueError, match="one path/page/table/column"):
        supplied(raw)


def test_more_than_two_readings_and_unequal_confidence_still_hold() -> None:
    raw = evidence(texts=("31/12/3025", "31/12/2025", "31/12/2025"))
    observations = raw["observations"]
    assert isinstance(observations, list)
    observations[0]["confidence"] = 0.1
    result, _ = structure_document(supplied(raw), IngestConfig())
    assert len(result.statements[0].period_conflicts[0].candidates) == 3
    assert [p.key for p in result.statements[0].periods] == ["2024-12-31"]


@pytest.mark.parametrize("invalid", ["31 Flober 2025", "31 December", "31/12/202"])
def test_partial_or_unknown_month_readings_do_not_become_year_guesses(invalid: str) -> None:
    result, _ = structure_document(
        supplied(evidence(texts=("31/12/3025", invalid))), IngestConfig()
    )
    assert result.statements[0].period_conflicts[0].candidates[1].period is None


def test_inheritance_does_not_bind_a_held_column_and_part_roundtrips() -> None:
    from fra_core.schemas import StatementType, TextSource
    from fra_ingest.classify import Classification
    from fra_ingest.continuation import inherit_periods
    from fra_ingest.header import HeaderLayout, parse_header
    from fra_ingest.parts import PartialStatement, build_part
    from fra_ingest.table_grid import build_grid

    base = supplied(evidence())
    doc = base.documents[0][1]
    grid = build_grid(doc.tables[0], doc, PATH)
    unheld = parse_header(grid, StatementType.BALANCE, None)
    previous = build_part(
        grid,
        unheld,
        Classification(type=StatementType.BALANCE, confidence=1),
        source=TextSource.TEXT,
    )
    held = parse_header(
        grid, StatementType.BALANCE, "31 December 2025", period_evidence=base.period_evidence
    )
    inherited = inherit_periods(held, grid, previous)
    assert inherited.value_cols.keys() == {3}
    assert inherited.unbound_cols == [2]
    assert inherited.period_conflicts == held.period_conflicts
    assert HeaderLayout.model_validate_json(inherited.model_dump_json()) == inherited
    part = build_part(
        grid,
        inherited,
        Classification(type=StatementType.BALANCE, confidence=1),
        source=TextSource.TEXT,
    )
    assert part.period_conflicts == held.period_conflicts
    assert PartialStatement.model_validate_json(part.model_dump_json()) == part


def test_recovered_header_cannot_replace_conflicting_original_observations() -> None:
    from fra_core.schemas import PageMode
    from fra_ingest.docling_json import DlDocument
    from fra_ingest.table_grid import GridCell

    def recover(grid: Grid, _document: DlDocument) -> Grid:
        original = grid.cell(0, 2)
        assert original is not None
        return grid.model_copy(
            update={
                "recovered_headers": (
                    GridCell(
                        text="31/12/2025",
                        row=-1,
                        col=2,
                        bbox=original.bbox,
                        page_no=1,
                        is_column_header=True,
                    ),
                )
            }
        )

    base = supplied(evidence()).model_copy(update={"page_modes": {1: PageMode.IMAGE}})
    result, _ = structure_document(base, IngestConfig(), header_recover=recover)
    assert [p.key for p in result.statements[0].periods] == ["2024-12-31"]
    assert len(result.tables[0].period_conflicts[0].candidates) == 2


def test_continuation_trial_cannot_inherit_a_conflicted_column() -> None:
    from test_structure import INCOME_2, _income_inputs, _income_pages

    tail = [["", "Notes", "2025 SAR '000", "2024 SAR '000"], *INCOME_2[1:]]
    doc = _income_pages(tail)
    base = _income_inputs(doc, {2: ()})
    raw = evidence(3, page=2, ref="#/tables/1", texts=("2024", "2023"))
    raw_observations = raw["observations"]
    assert isinstance(raw_observations, list)
    for observation in raw_observations:
        observation["docling_path"] = "docling/p1-2.json"
    base = StructureInputs.model_validate({**base.model_dump(), "period_evidence": [raw]})
    result, _ = structure_document(base, IngestConfig())
    assert result.statements[0].source_pages == [1]
    decision = next(t for t in result.tables if t.table_ref == "#/tables/1")
    assert decision.statement_id is None
    assert decision.period_conflicts
    assert "continuation_of:income" not in decision.evidence


def test_recorded_conflicting_readings_keep_exact_source_ids_and_boxes() -> None:
    from fra_core.schemas import BBox, Provenance, StatementType, TextSource
    from fra_core.schemas.statement import PeriodEvidence, PeriodObservation
    from fra_ingest.header import parse_header
    from fra_ingest.table_grid import Grid, GridCell

    boxes = (
        BBox(left=387.23, top=146.61, right=422.80, bottom=156.90),
        BBox(left=389.01, top=148.38, right=421.74, bottom=155.56),
    )
    grid = Grid(
        table_ref="#/tables/0",
        docling_path=PATH,
        page_no=5,
        page_width=600,
        num_rows=2,
        num_cols=2,
        cells=(
            GridCell(
                text="31/12/3025", row=0, col=1, bbox=boxes[0], page_no=5, is_column_header=True
            ),
            GridCell(
                text="Total assets",
                row=1,
                col=0,
                bbox=BBox(left=30, top=170, right=200, bottom=180),
                page_no=5,
            ),
            GridCell(
                text="100",
                row=1,
                col=1,
                bbox=BBox(left=387, top=170, right=423, bottom=180),
                page_no=5,
            ),
        ),
    )
    group = PeriodEvidence(
        observations=tuple(
            PeriodObservation(
                source_id=source_id,
                raw_text=text,
                docling_path=PATH,
                confidence=1,
                provenance=Provenance(
                    page_no=5,
                    bbox=box,
                    table_ref=grid.table_ref,
                    row=0,
                    col=1,
                    source=TextSource.OCR,
                ),
            )
            for source_id, text, box in zip(
                ("full_page-5-27", "table-5-1"), ("31/12/3025", "31/12/2025"), boxes, strict=True
            )
        )
    )
    layout = parse_header(grid, StatementType.BALANCE, None, period_evidence=(group,))
    assert layout.value_cols == {}
    assert layout.unbound_cols == [1]
    assert tuple(c.observation for c in layout.period_conflicts[0].candidates) == group.observations
    assert [c.period.key for c in layout.period_conflicts[0].candidates if c.period] == [
        "3025-12-31",
        "2025-12-31",
    ]


def test_merged_parts_preserve_conflicts_from_both_pages() -> None:
    from fra_core.schemas import StatementType, TextSource
    from fra_core.taxonomy.loader import load_taxonomy
    from fra_ingest.classify import Classification
    from fra_ingest.continuation import merge_continuations
    from fra_ingest.header import parse_header
    from fra_ingest.label_match import LabelIndex
    from fra_ingest.parts import build_part
    from fra_ingest.table_grid import build_grid

    base = supplied(evidence())
    doc = base.documents[0][1]
    grid = build_grid(doc.tables[0], doc, PATH)
    layout = parse_header(grid, StatementType.BALANCE, None, period_evidence=base.period_evidence)
    first = build_part(
        grid,
        layout,
        Classification(type=StatementType.BALANCE, confidence=1),
        source=TextSource.TEXT,
    )
    raw = evidence(page=2, ref="#/tables/1")
    second_doc = _balance_tables(
        table(1, 2, [["", "Notes", "31/12/3025", "31/12/2024"], *PAGE_1[1:]])
    )
    second_grid = build_grid(second_doc.tables[0], second_doc, PATH)
    from fra_core.schemas.statement import PeriodEvidence

    second_layout = parse_header(
        second_grid,
        StatementType.BALANCE,
        None,
        period_evidence=(PeriodEvidence.model_validate(raw),),
    )
    second = build_part(
        second_grid,
        second_layout,
        Classification(type=StatementType.BALANCE, confidence=1),
        source=TextSource.TEXT,
    )
    merged = merge_continuations([first, second], LabelIndex(load_taxonomy()))
    assert len(merged) == 1
    assert [c.candidates[0].observation.provenance.page_no for c in merged[0].period_conflicts] == [
        1,
        2,
    ]


def test_agreeing_evidence_cannot_hide_another_bound_header_date() -> None:
    with pytest.raises(ValueError, match="omits the bound header interpretation"):
        structure_document(supplied(evidence(texts=("31/12/2025", "2025-12-31"))), IngestConfig())


def test_same_interim_period_uses_the_column_context() -> None:
    from fra_core.schemas import StatementType
    from fra_ingest.header import parse_header
    from fra_ingest.table_grid import build_grid

    rows = [["", "Notes", "2025 SAR '000", "2024 SAR '000"], *PAGE_1[1:]]
    base = supplied(evidence(texts=("2025", "2025")), rows=rows)
    doc = base.documents[0][1]
    grid = build_grid(doc.tables[0], doc, PATH)
    layout = parse_header(
        grid,
        StatementType.INCOME,
        "For the three months ended 31 March 2025",
        period_evidence=base.period_evidence,
    )
    assert layout.period_conflicts == ()
    assert layout.value_cols[2].months == 3
    assert layout.value_cols[2].end_date.isoformat() == "2025-03-31"


def test_explicit_different_duration_lengths_are_conflicting() -> None:
    from fra_core.schemas import StatementType
    from fra_ingest.header import parse_header
    from fra_ingest.table_grid import build_grid

    base = supplied(
        evidence(
            texts=(
                "For the three months ended 31 December 2025",
                "For the six months ended 31 December 2025",
            )
        )
    )
    doc = base.documents[0][1]
    grid = build_grid(doc.tables[0], doc, PATH)
    layout = parse_header(grid, StatementType.INCOME, None, period_evidence=base.period_evidence)
    assert 2 not in layout.value_cols
    assert [c.period.months for c in layout.period_conflicts[0].candidates if c.period] == [3, 6]


def test_income_classification_probe_does_not_create_a_false_period_kind_conflict() -> None:
    from test_structure import INCOME_1

    from fra_core.schemas import StatementType

    base = supplied(
        evidence(texts=("2025", "For the year ended 31 December 2025")),
        evidence(3, texts=("2024", "For the year ended 31 December 2024")),
        rows=INCOME_1,
    ).model_copy(update={"title_types": {1: (StatementType.INCOME,)}})
    result, _ = structure_document(base, IngestConfig())
    assert len(result.statements) == 1
    assert [p.key for p in result.statements[0].periods] == ["FY2025", "FY2024"]
    assert result.tables[0].period_conflicts == ()
    assert not any(f.startswith("period_conflict") for f in result.tables[0].evidence)


@pytest.mark.parametrize("year", [2025, 3025])
def test_unknown_month_with_any_calendar_year_is_held_end_to_end(year: int) -> None:
    rows = [["", "Notes", f"31/12/{year}", "31/12/2024"], *PAGE_1[1:]]
    texts = (f"31/12/{year}", f"31 Flober {year}")
    result, _ = structure_document(supplied(evidence(texts=texts), rows=rows), IngestConfig())
    statement = result.statements[0]
    assert [p.key for p in statement.periods] == ["2024-12-31"]
    conflict = statement.period_conflicts[0]
    assert conflict.reason == "unparseable_observation"
    assert conflict.candidates[1].period is None
    assert [c.observation.raw_text for c in conflict.candidates] == list(texts)


@pytest.mark.parametrize("year", [2025, 3025])
def test_valid_bare_year_evidence_remains_supported(year: int) -> None:
    rows = [["", "Notes", str(year), "2024"], *PAGE_1[1:]]
    result, _ = structure_document(
        supplied(evidence(texts=(str(year), f"31/12/{year}")), rows=rows), IngestConfig()
    )
    assert result.statements[0].periods[0].key == f"{year}-12-31"
    assert result.tables[0].period_conflicts == ()


def _recovered_period_grid(grid: Grid, caption: str) -> Grid:
    from fra_ingest.table_grid import GridCell

    anchor = grid.cell(0, 2)
    assert anchor is not None
    return grid.model_copy(
        update={
            "recovered_headers": (
                anchor.model_copy(update={"text": "31 December 2025", "row": -1}),
            ),
            "recovered_context": (
                GridCell(text=caption, row=-1, col=-1, bbox=anchor.bbox, page_no=grid.page_no),
            )
            if caption
            else (),
        }
    )


@pytest.mark.parametrize(
    "caption", ["For the year ended 31 December 2025", "For the three months ended", ""]
)
def test_recovered_caption_preserves_conflicting_explicit_durations(caption: str) -> None:
    from test_structure import INCOME_1

    from fra_core.schemas import PageMode, StatementType

    texts = (
        "For the three months ended 31 December 2025",
        "For the six months ended 31 December 2025",
    )
    base = supplied(evidence(texts=texts), rows=INCOME_1).model_copy(
        update={"page_modes": {1: PageMode.IMAGE}, "title_types": {1: (StatementType.INCOME,)}}
    )
    result, _ = structure_document(
        base, IngestConfig(), header_recover=lambda g, _d: _recovered_period_grid(g, caption)
    )
    statement = result.statements[0]
    assert [p.key for p in statement.periods] == ["FY2024"]
    conflict = statement.period_conflicts[0]
    assert [c.period.months for c in conflict.candidates if c.period] == [3, 6]
    assert [c.observation.raw_text for c in conflict.candidates] == list(texts)
    assert "period_conflict:2" in result.reviews[0].reasons
    assert StructureResult.model_validate_json(result.model_dump_json()) == result


def test_recovered_annual_caption_cannot_replace_explicit_duration_or_instant() -> None:
    from fra_core.schemas import PageMode, PeriodKind

    base = supplied(
        evidence(texts=("For the three months ended 31 December 2025", "As at 31 December 2025"))
    )
    base = base.model_copy(update={"page_modes": {1: PageMode.IMAGE}})
    result, _ = structure_document(
        base,
        IngestConfig(),
        header_recover=lambda g, _d: _recovered_period_grid(g, "For the year ended"),
    )
    candidates = result.statements[0].period_conflicts[0].candidates
    assert [(c.period.kind, c.period.months) for c in candidates if c.period] == [
        (PeriodKind.DURATION, 3),
        (PeriodKind.INSTANT, None),
    ]


@pytest.mark.parametrize(
    "caption,held", [("For the three months ended", False), ("For the year ended", True)]
)
def test_agreeing_explicit_duration_checks_recovered_context(caption: str, held: bool) -> None:
    from test_structure import INCOME_1

    from fra_core.schemas import PageMode, StatementType

    text = "For the three months ended 31 December 2025"
    base = supplied(evidence(texts=(text, text)), rows=INCOME_1).model_copy(
        update={"page_modes": {1: PageMode.IMAGE}, "title_types": {1: (StatementType.INCOME,)}}
    )
    result, _ = structure_document(
        base, IngestConfig(), header_recover=lambda g, _d: _recovered_period_grid(g, caption)
    )
    statement = result.statements[0]
    assert bool(statement.period_conflicts) is held
    if held:
        conflict = statement.period_conflicts[0]
        assert conflict.reason == "conflicting_context"
        assert [c.period.months for c in conflict.candidates if c.period] == [3, 3]
        assert (
            tuple(c.observation for c in conflict.candidates)
            == base.period_evidence[0].observations
        )
    assert [p.key for p in statement.periods] == (
        ["FY2024"] if held else ["3M-2025-12-31", "FY2024"]
    )
    assert StructureResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize("visual", [False, True])
@pytest.mark.parametrize(
    "texts,sources,nonvisual_years,visual_years",
    [
        (("٥٢٠٢", "٥٢٠٢"), ("text", "text"), (5202, 5202), (2025, 2025)),
        (("٥٢٠٢", "٢٠٢٥"), ("text", "ocr"), (5202, 2025), (2025, 2025)),
        (("٥٢٠٢", "٢٠٢٤"), ("text", "ocr"), (5202, 2024), (2025, 2024)),
        (("٥٢٠٢", "٥٢٠٢"), ("text", "ocr"), (5202, 5202), (2025, 5202)),
    ],
)
def test_arabic_evidence_normalization_uses_source_and_visual_page(
    visual: bool,
    texts: tuple[str, str],
    sources: tuple[str, str],
    nonvisual_years: tuple[int, int],
    visual_years: tuple[int, int],
) -> None:
    raw = evidence(texts=texts)
    observations = raw["observations"]
    assert isinstance(observations, list)
    for observation, source in zip(observations, sources, strict=True):
        observation["provenance"]["source"] = source
    rows = [["", "Notes", "٥٢٠٢", "2024"], *PAGE_1[1:]]
    base = supplied(raw, rows=rows).model_copy(update={"visual_pages": {1} if visual else set()})
    result, _ = structure_document(base, IngestConfig())
    statement = result.statements[0]
    years = visual_years if visual else nonvisual_years
    held = years[0] != years[1]
    assert bool(statement.period_conflicts) is held
    if held:
        candidates = statement.period_conflicts[0].candidates
        assert tuple(c.observation for c in candidates) == base.period_evidence[0].observations
        assert [c.period.end_date.year for c in candidates if c.period] == list(years)
        assert [p.key for p in statement.periods] == ["2024-12-31"]
    else:
        assert statement.periods[0].key == ("2025-12-31" if visual else "5202-12-31")
    assert StructureResult.model_validate_json(result.model_dump_json()) == result


def test_logical_ocr_observations_are_not_reversed_on_a_visual_text_page() -> None:
    rows = [["", "Notes", "٥٢٠٢", "2024"], *PAGE_1[1:]]
    base = supplied(evidence(texts=("٢٠٢٥", "٢٠٢٥")), rows=rows).model_copy(
        update={"visual_pages": {1}}
    )
    before = base.model_dump_json()
    result, _ = structure_document(base, IngestConfig())
    assert result.statements[0].periods[0].key == "2025-12-31"
    assert result.statements[0].period_conflicts == ()
    assert base.model_dump_json() == before


def test_visual_source_context_is_passed_through_continuation_trials() -> None:
    from test_structure import INCOME_2, _income_inputs, _income_pages

    tail = [["", "Notes", "٥٢٠٢ SAR '000", "٤٢٠٢ SAR '000"], *INCOME_2[1:]]
    base = _income_inputs(_income_pages(tail), {2: ()})
    raw = evidence(2, page=2, ref="#/tables/1", texts=("٥٢٠٢", "٢٠٢٥"))
    observations = raw["observations"]
    assert isinstance(observations, list)
    for n, observation in enumerate(observations):
        observation["docling_path"] = "docling/p1-2.json"
        observation["provenance"]["source"] = "text" if n == 0 else "ocr"
    base = StructureInputs.model_validate(
        {**base.model_dump(), "period_evidence": [raw], "visual_pages": {2}}
    )
    result, _ = structure_document(base, IngestConfig())
    assert result.statements[0].source_pages == [1, 2]
    assert [p.key for p in result.statements[0].periods] == ["FY2025", "FY2024"]
    assert result.tables[1].period_conflicts == ()
    assert "continuation_of:income" in result.tables[1].evidence


def test_malformed_numeric_date_cannot_use_bare_year_fallback() -> None:
    texts = ("31/12/3025", "30/06 3025")
    result, _ = structure_document(supplied(evidence(texts=texts)), IngestConfig())
    conflict = result.statements[0].period_conflicts[0]
    assert conflict.reason == "unparseable_observation"
    assert conflict.candidates[1].period is None
    assert (
        tuple(c.observation for c in conflict.candidates)
        == supplied(evidence(texts=texts)).period_evidence[0].observations
    )
    assert StructureResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize("recovered", [False, True])
@pytest.mark.parametrize(
    "residue",
    ["SAR30/06", "2024SAR", "SR30/06", "million30/06", "(000)7", "restated30/06"],
)
def test_marker_residue_holds_the_column_end_to_end(residue: str, recovered: bool) -> None:
    from fra_core.schemas import PageMode

    texts = ("31/12/3025", f"31 December3025 {residue}")
    base = supplied(evidence(texts=texts)).model_copy(update={"page_modes": {1: PageMode.IMAGE}})

    def recover(grid: Grid, _doc: object) -> Grid:
        recovered_grid = _recovered_period_grid(grid, "")
        return recovered_grid.model_copy(
            update={
                "recovered_headers": tuple(
                    c.model_copy(update={"text": texts[0]})
                    for c in recovered_grid.recovered_headers
                )
            }
        )

    result, _ = structure_document(
        base, IngestConfig(), header_recover=recover if recovered else None
    )
    statement = result.statements[0]
    assert [p.key for p in statement.periods] == ["2024-12-31"]
    conflict = statement.period_conflicts[0]
    assert conflict.reason == "unparseable_observation"
    assert conflict.candidates[1].period is None
    assert tuple(c.observation for c in conflict.candidates) == base.period_evidence[0].observations
    assert "period_conflict:2" in result.tables[0].evidence
    assert "period_conflict:2" in result.reviews[0].reasons
    assert StructureResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize("year", [2025, 3025])
@pytest.mark.parametrize("marker", ["SR", "LE", "KD", "SAR", "EGP", "KWD", "SR '000"])
@pytest.mark.parametrize("with_evidence", [False, True])
def test_valid_marker_recovery_keeps_binding_with_optional_evidence(
    year: int, marker: str, with_evidence: bool
) -> None:
    from fra_core.schemas import PageMode

    text = f"{year} {marker}"
    rows = [["", "Notes", text, "2024"], *PAGE_1[1:]]
    base = supplied(*([evidence(texts=(text, text))] if with_evidence else []), rows=rows)
    base = base.model_copy(update={"page_modes": {1: PageMode.IMAGE}})

    def recover(grid: Grid, _doc: object) -> Grid:
        recovered_grid = _recovered_period_grid(grid, "")
        return recovered_grid.model_copy(
            update={
                "recovered_headers": tuple(
                    c.model_copy(update={"text": text}) for c in recovered_grid.recovered_headers
                )
            }
        )

    result, _ = structure_document(base, IngestConfig(), header_recover=recover)
    assert result.statements[0].periods[0].key == f"{year}-12-31"
    assert result.tables[0].period_conflicts == ()
    assert result.statements[0].period_conflicts == ()


@pytest.mark.parametrize("marker", ["SR", "LE", "KD"])
def test_valid_capital_markers_retain_both_conflicting_candidates(marker: str) -> None:
    texts = (f"3025 {marker}", f"2025 {marker}")
    rows = [["", "Notes", texts[0], "2024"], *PAGE_1[1:]]
    base = supplied(evidence(texts=texts), rows=rows)
    result, _ = structure_document(base, IngestConfig())
    conflict = result.statements[0].period_conflicts[0]
    assert conflict.reason == "differing_periods"
    assert [c.period.key for c in conflict.candidates if c.period] == ["3025-12-31", "2025-12-31"]
    assert tuple(c.observation for c in conflict.candidates) == base.period_evidence[0].observations


@pytest.mark.parametrize("residue", ["SAR30/06", "2024SAR", "million30/06"])
def test_recovered_marker_residue_without_evidence_remains_unbound(residue: str) -> None:
    from fra_core.schemas import StatementType
    from fra_ingest.header import parse_header
    from fra_ingest.table_grid import build_grid

    base = supplied()
    doc = base.documents[0][1]
    grid = _recovered_period_grid(build_grid(doc.tables[0], doc, PATH), "")
    grid = grid.model_copy(
        update={
            "recovered_headers": tuple(
                c.model_copy(update={"text": f"31 December3025 {residue}"})
                for c in grid.recovered_headers
            )
        }
    )
    layout = parse_header(grid, StatementType.BALANCE, None)
    assert 2 not in layout.value_cols
    assert "period_unbound:2" in layout.evidence
    assert layout.value_cols[3].key == "2024-12-31"


@pytest.mark.parametrize("recovered", [False, True])
def test_unsupported_stated_length_is_held_in_every_binding_path(recovered: bool) -> None:
    from test_structure import INCOME_1

    from fra_core.schemas import PageMode, StatementType

    texts = ("For the year ended 31 December 2025", "For the 10 months ended 31 December 2025")
    base = supplied(evidence(texts=texts), rows=INCOME_1).model_copy(
        update={"page_modes": {1: PageMode.IMAGE}, "title_types": {1: (StatementType.INCOME,)}}
    )
    result, _ = structure_document(
        base,
        IngestConfig(),
        header_recover=(lambda g, _d: _recovered_period_grid(g, "For the year ended"))
        if recovered
        else None,
    )
    conflict = result.statements[0].period_conflicts[0]
    assert conflict.reason == "unparseable_observation"
    assert conflict.candidates[1].period is None
    assert [p.key for p in result.statements[0].periods] == ["FY2024"]


def test_generic_period_wording_takes_missing_length_from_recovery() -> None:
    from test_structure import INCOME_1

    from fra_core.schemas import PageMode, StatementType

    text = "For the period ended 31 December 2025"
    base = supplied(evidence(texts=(text, text)), rows=INCOME_1).model_copy(
        update={"page_modes": {1: PageMode.IMAGE}, "title_types": {1: (StatementType.INCOME,)}}
    )
    result, _ = structure_document(
        base,
        IngestConfig(),
        header_recover=lambda g, _d: _recovered_period_grid(g, "For the three months ended"),
    )
    assert result.statements[0].period_conflicts == ()
    assert [p.key for p in result.statements[0].periods] == ["3M-2025-12-31", "FY2024"]


@pytest.mark.parametrize("recovered", [False, True])
@pytest.mark.parametrize(
    "invalid",
    [
        "30/06 2025",
        "30 06/2025",
        "2025-06 30",
        "31/12/202",
        "31 February 2025",
        "29 February 2025",
        "31 December",
        "31/13/2025",
        "31 Flober 2025",
        "31 Flober 3025",
        "31/12/2025 7",
        "31 ديسمبر 1446هـ",
        "For the 10 months ended 31 December 2025",
        "For the 13 months ended 31 December 2025",
        "For the two months ended 31 December 2025",
        "For the unknown months ended 31 December 2025",
        "not three months ended 31 December 2025",
        "three months and six months ended 31 December 2025",
    ],
)
def test_invalid_facts_cannot_be_healed_by_date_or_duration_context(
    invalid: str, recovered: bool
) -> None:
    from test_structure import INCOME_1

    from fra_core.schemas import PageMode, StatementType

    text = "For the year ended 31 December 2025"
    base = supplied(evidence(texts=(text, invalid)), rows=INCOME_1).model_copy(
        update={"page_modes": {1: PageMode.IMAGE}, "title_types": {1: (StatementType.INCOME,)}}
    )
    before = base.model_dump_json()
    result, _ = structure_document(
        base,
        IngestConfig(),
        header_recover=(
            lambda g, _d: _recovered_period_grid(g, "For the year ended 31 December 2025")
        )
        if recovered
        else None,
    )
    conflict = result.statements[0].period_conflicts[0]
    assert conflict.reason == "unparseable_observation"
    assert conflict.candidates[1].period is None
    assert tuple(c.observation for c in conflict.candidates) == base.period_evidence[0].observations
    assert [p.key for p in result.statements[0].periods] == ["FY2024"]
    assert "period_conflict:2" in result.tables[0].evidence
    assert StructureResult.model_validate_json(result.model_dump_json()) == result
    assert base.model_dump_json() == before


@pytest.mark.parametrize("caption", ["For the three months ended", "عن ثلاثة أشهر المنتهية"])
@pytest.mark.parametrize("generic", ["For the period ended", "عن الفترة المنتهية في", ""])
def test_generic_and_absent_lengths_take_only_the_missing_caption_length(
    caption: str, generic: str
) -> None:
    from test_structure import INCOME_1

    from fra_core.schemas import PageMode, StatementType

    text = f"{generic} 31 December 2025"
    base = supplied(evidence(texts=(text, text)), rows=INCOME_1).model_copy(
        update={"page_modes": {1: PageMode.IMAGE}, "title_types": {1: (StatementType.INCOME,)}}
    )
    result, _ = structure_document(
        base, IngestConfig(), header_recover=lambda g, _d: _recovered_period_grid(g, caption)
    )
    assert result.statements[0].period_conflicts == ()
    assert [p.key for p in result.statements[0].periods] == ["3M-2025-12-31", "FY2024"]


@pytest.mark.parametrize("length", [3, 6, 9, 12])
def test_supported_explicit_lengths_agree_in_recovery(length: int) -> None:
    from test_structure import INCOME_1

    from fra_core.schemas import PageMode, StatementType

    caption = f"For the {length} months ended"
    text = f"{caption} 31 December 2025"
    base = supplied(evidence(texts=(text, text)), rows=INCOME_1).model_copy(
        update={"page_modes": {1: PageMode.IMAGE}, "title_types": {1: (StatementType.INCOME,)}}
    )
    result, _ = structure_document(
        base, IngestConfig(), header_recover=lambda g, _d: _recovered_period_grid(g, caption)
    )
    assert result.statements[0].period_conflicts == ()
    assert result.statements[0].periods[0].months == length


def test_direct_date_hint_cannot_overwrite_explicit_observation_length() -> None:
    from fra_core.schemas import StatementType
    from fra_ingest.header import parse_header
    from fra_ingest.table_grid import build_grid

    text = "For the six months ended 31 December 2025"
    rows = [["", "Notes", text, "2024"], *PAGE_1[1:]]
    base = supplied(evidence(texts=(text, text)), rows=rows)
    doc = base.documents[0][1]
    layout = parse_header(
        build_grid(doc.tables[0], doc, PATH),
        StatementType.INCOME,
        "For the three months ended 31 December 2025",
        period_evidence=base.period_evidence,
    )
    assert 2 not in layout.value_cols
    assert layout.period_conflicts[0].reason == "conflicting_context"
    assert [c.period.months for c in layout.period_conflicts[0].candidates if c.period] == [6, 6]


def test_partial_caption_date_conflict_keeps_explicit_candidate_dates() -> None:
    from fra_core.schemas import StatementType
    from fra_ingest.header import parse_header
    from fra_ingest.table_grid import build_grid

    text = "For the three months ended 31 December 2025"
    rows = [["", "Notes", text, "2024"], *PAGE_1[1:]]
    base = supplied(evidence(texts=(text, text)), rows=rows)
    doc = base.documents[0][1]
    grid = _recovered_period_grid(
        build_grid(doc.tables[0], doc, PATH), "For the three months ended 30 June"
    )
    # The recovered label itself also carries a complete date: context may not rewrite it.
    grid = grid.model_copy(
        update={
            "recovered_headers": tuple(
                c.model_copy(update={"text": text}) for c in grid.recovered_headers
            )
        }
    )
    layout = parse_header(grid, StatementType.INCOME, None, period_evidence=base.period_evidence)
    assert 2 not in layout.value_cols
    assert layout.period_conflicts[0].reason == "conflicting_context"
    assert [
        c.period.end_date.isoformat() for c in layout.period_conflicts[0].candidates if c.period
    ] == ["2025-12-31", "2025-12-31"]


@pytest.mark.parametrize("prefix", ["", "For the period ended", "For the six months ended"])
@pytest.mark.parametrize("caption", ["For the six months ended 2025", "عن ستة أشهر المنتهية 2025"])
@pytest.mark.parametrize("recovered", [False, True])
def test_year_only_context_keeps_the_complete_header_and_observation_date(
    prefix: str, caption: str, recovered: bool
) -> None:
    from fra_core.schemas import StatementType
    from fra_ingest.header import parse_header
    from fra_ingest.table_grid import build_grid

    text = f"{prefix} 30 June 2024 (Unaudited)"
    rows = [["", "Notes", text, "2023"], *PAGE_1[1:]]
    base = supplied(evidence(texts=(text, text)), rows=rows)
    doc = base.documents[0][1]
    grid = build_grid(doc.tables[0], doc, PATH)
    if recovered:
        grid = _recovered_period_grid(grid, caption)
        grid = grid.model_copy(
            update={
                "recovered_headers": tuple(
                    c.model_copy(update={"text": text}) for c in grid.recovered_headers
                )
            }
        )
    layout = parse_header(
        grid,
        StatementType.INCOME,
        caption,
        period_evidence=base.period_evidence,
    )
    assert layout.period_conflicts == ()
    assert layout.value_cols[2].key == "6M-2024-06-30"
    assert layout.value_cols[2].audited is False


@pytest.mark.parametrize(
    "caption,expected",
    [
        ("", None),
        ("2025", None),
        ("For the period ended 2025", None),
        ("For the period ended 30 June 2025", None),
        ("30 June 2025", None),
        ("For the three months ended", 3),
        ("For the six months ended 2025", 6),
        ("For the nine months ended 30 June 2025", 9),
        ("For the year ended 2025", 12),
        ("For the 10 months ended 2025", None),
        ("For the six months ended 31 February 2025", None),
    ],
)
def test_generic_observations_require_and_can_take_an_explicit_context_length(
    caption: str, expected: int | None
) -> None:
    from fra_core.schemas import PeriodKind
    from fra_ingest.header import period_with_context

    text = "For the period ended 30 June 2024 (Restated)"
    period = period_with_context(text, caption, PeriodKind.DURATION)
    if expected is None:
        assert period is None
    else:
        assert period is not None
        assert period.months == expected
        assert period.end_date.isoformat() == "2024-06-30"
        assert period.restated is True


@pytest.mark.parametrize("caption", ["", "2025", "For the period ended 2025"])
def test_generic_observations_with_no_context_length_hold_the_column(caption: str) -> None:
    from fra_core.schemas import StatementType
    from fra_ingest.header import parse_header
    from fra_ingest.table_grid import build_grid

    text = "For the period ended 30 June 2025"
    rows = [["", "Notes", text, "2024"], *PAGE_1[1:]]
    base = supplied(evidence(texts=(text, text)), rows=rows)
    doc = base.documents[0][1]
    layout = parse_header(
        build_grid(doc.tables[0], doc, PATH),
        StatementType.INCOME,
        caption,
        period_evidence=base.period_evidence,
    )
    assert 2 not in layout.value_cols
    assert 2 in layout.unbound_cols
    assert layout.period_conflicts[0].reason == "unparseable_observation"
    assert all(c.period is None for c in layout.period_conflicts[0].candidates)


@pytest.mark.parametrize("caption", ["2025", "For the period ended 2025"])
def test_complete_explicit_dates_do_not_need_a_full_caption_date(caption: str) -> None:
    from fra_core.schemas import PeriodKind
    from fra_ingest.header import period_with_context

    text = "For the six months ended 30 June 2024"
    period = period_with_context(text, caption, PeriodKind.DURATION, match_caption_date=True)
    assert period is not None and period.key == "6M-2024-06-30"


def test_contradictory_year_only_duration_context_holds_without_overwriting_candidates() -> None:
    from fra_core.schemas import StatementType
    from fra_ingest.header import parse_header
    from fra_ingest.table_grid import build_grid

    text = "For the three months ended 30 June 2025"
    rows = [["", "Notes", text, "2024"], *PAGE_1[1:]]
    base = supplied(evidence(texts=(text, text)), rows=rows)
    doc = base.documents[0][1]
    layout = parse_header(
        build_grid(doc.tables[0], doc, PATH),
        StatementType.INCOME,
        "For the six months ended 2025",
        period_evidence=base.period_evidence,
    )
    assert 2 not in layout.value_cols
    conflict = layout.period_conflicts[0]
    assert conflict.reason == "conflicting_context"
    assert [c.period.key for c in conflict.candidates if c.period] == [
        "3M-2025-06-30",
        "3M-2025-06-30",
    ]


def test_year_only_context_cannot_complete_a_year_only_observation() -> None:
    from fra_core.schemas import PeriodKind
    from fra_ingest.header import period_with_context

    assert period_with_context("2024", "six months ended 2025", PeriodKind.DURATION) is None
