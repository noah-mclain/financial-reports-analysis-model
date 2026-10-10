"""Real partial-header crops supply period observations before recovery can fail."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from test_header_recovery import (
    Observation,
    RecordedEngine,
    inject_render,
    partial_observation,
    safety_lines,
)
from test_structure import inputs

from fra_core.schemas import PageMode, StatementType
from fra_ingest import header_recovery, structure
from fra_ingest.config import IngestConfig
from fra_ingest.docling_json import DlDocument
from fra_ingest.results import StructureResult
from fra_ingest.table_grid import Grid, build_grid


def run_crop(
    monkeypatch: pytest.MonkeyPatch,
    alternate: str,
    *,
    missing: str = "31 December 2024",
    header: str = "31 December 2023",
    confidence: float = 0.1,
) -> tuple[Observation, Grid, StructureResult, RecordedEngine]:
    observation = partial_observation(header=header)
    inject_render(monkeypatch, observation)
    engine = RecordedEngine(observation)
    engine.lines = [
        replace(line, confidence=confidence) if line.text == alternate else line
        for line in safety_lines("For the year ended 31 December", (alternate, missing))
    ]
    original = inputs([(observation.docling_path, observation.document())]).model_copy(
        update={
            "page_modes": {observation.grid.page_no: PageMode.IMAGE},
            "title_types": {observation.grid.page_no: (StatementType.INCOME,)},
        }
    )
    captured: list[Grid] = []

    def recovery(grid: Grid, document: DlDocument) -> Grid:
        recovered = header_recovery.recover_header(
            grid, document, Path("unused.pdf"), IngestConfig(), engine
        )
        captured.append(recovered)
        return recovered

    result, _ = structure.structure_document(original, IngestConfig(), header_recover=recovery)
    return observation, captured[0], result, engine


@pytest.mark.parametrize("alternate", ["31 December 3025", "31 April 2023", "unreadable"])
def test_crop_automatically_holds_bound_column_even_at_low_confidence(
    monkeypatch: pytest.MonkeyPatch, alternate: str
) -> None:
    observation, recovered, result, engine = run_crop(monkeypatch, alternate)
    assert len(engine.calls) == 1
    assert recovered.cells == observation.grid.cells
    assert [c.col for c in recovered.recovered_headers] == [1]
    statement = result.statements[0]
    assert [p.key for p in statement.periods] == ["FY2024"]
    conflict = statement.period_conflicts[0]
    assert [c.observation.raw_text for c in conflict.candidates] == ["31 December 2023", alternate]
    assert conflict.candidates[1].observation.confidence == 0.1
    assert "period_conflict:0" in result.reviews[0].reasons
    assert result.tables[0].period_evidence == recovered.period_evidence
    assert StructureResult.model_validate_json(result.model_dump_json()) == result


def test_crop_retains_exact_docling_literal_and_equivalent_status_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = " 31  December\n2023 (Restated) (Unaudited) "
    observation, recovered, result, engine = run_crop(
        monkeypatch, "2023-12-31 (Restated) (Unaudited)", header=raw
    )
    assert len(engine.calls) == 1
    group = recovered.period_evidence[0]
    primary = group.observations[0]
    assert primary.raw_text == raw
    assert primary.confidence is None
    assert primary.source_id.startswith("docling-cell:")
    cell = observation.table.data.table_cells[0]
    assert cell.bbox is not None
    assert primary.provenance.bbox == cell.bbox.to_bbox(observation.page.size.height)
    period = result.statements[0].periods[0]
    assert period.key == "FY2023" and period.restated and period.audited is False
    assert not result.statements[0].period_conflicts
    assert result.tables[0].period_evidence == (group,)


def test_collected_conflict_survives_missing_slot_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation, recovered, result, engine = run_crop(
        monkeypatch, "31 December 3025", missing="31 April 2024"
    )
    assert len(engine.calls) == 1
    assert not recovered.recovered_headers
    assert recovered.cells == observation.grid.cells
    assert result.statements == []
    assert result.tables[0].statement_id is None
    assert result.tables[0].period_conflicts[0].col == 0
    assert len(result.tables[0].period_evidence[0].observations) == 2


def test_missing_alternate_is_visible_and_never_fabricated(monkeypatch: pytest.MonkeyPatch) -> None:
    observation = partial_observation(header="31 December 2023")
    inject_render(monkeypatch, observation)
    engine = RecordedEngine(observation)
    engine.lines = [
        line
        for line in safety_lines("For the year ended 31 December", ("31 December 2023", "2024"))
        if line.text != "31 December 2023"
    ]
    recovered = header_recovery.recover_header(
        observation.grid, observation.document(), Path("unused.pdf"), IngestConfig(), engine
    )
    assert not recovered.period_evidence
    assert "period_evidence_unavailable:0:no_attributable_reading" in recovered.flags
    assert len(engine.calls) == 1


@pytest.mark.parametrize(
    "alternate,missing", [("3025", "2024"), ("3025", "31 April 2024"), ("missing", "2024")]
)
def test_structure_pdf_serializes_real_crop_observations_and_holds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, alternate: str, missing: str
) -> None:
    from support import located, text_page

    from fra_ingest.results import PageScore, RangeConversion

    observation = partial_observation(header="31 December 2023")
    config = IngestConfig(artifact_root=tmp_path)
    base = inputs([(observation.docling_path, observation.document())])
    page = observation.grid.page_no
    where = located([PageMode.IMAGE] * page, [(page, page)], sha256=base.sha256)
    where.pages = [PageScore(page_no=page, title_types=[StatementType.INCOME], type_scores={})]
    converted = base.convert.model_copy(
        update={
            "ranges": [
                RangeConversion(
                    first_page=page,
                    last_page=page,
                    ocr="full_page",
                    ocr_language=None,
                    docling_path=observation.docling_path,
                    status="ok",
                )
            ]
        }
    )
    out = tmp_path / base.sha256
    source = out / observation.docling_path
    source.parent.mkdir(parents=True)
    source.write_text(observation.document().model_dump_json())
    monkeypatch.setattr(structure, "load_or_locate", lambda *_: where)
    monkeypatch.setattr(structure, "current_convert", lambda *_: converted)
    page_text = text_page("Statement of income", page_no=page).model_copy(
        update={"mode": PageMode.IMAGE}
    )
    reads: list[int] = []

    def pages(*_args: object, **_kwargs: object) -> list[object]:
        reads.append(1)
        return [page_text]

    monkeypatch.setattr(structure, "read_pages", pages)
    inject_render(monkeypatch, observation)
    engine = RecordedEngine(observation)
    engine.lines = safety_lines("For the year ended 31 December", (alternate, missing))
    if alternate == "missing":
        engine.lines = [line for line in engine.lines if line.text != "missing"]
    result = structure.structure_pdf(Path("unused.pdf"), config, engine)
    stored = StructureResult.model_validate_json((out / "statements.raw.json").read_text())
    assert stored == result
    assert len(engine.calls) == 1
    if alternate == "missing":
        assert not stored.tables[0].period_evidence
        assert "period_evidence_unavailable:0:no_attributable_reading" in stored.tables[0].evidence
        # No reading over the bound header is a fact of this page, not a failed read:
        # the result is reused without another crop or page read.
        assert structure.structure_pdf(Path("unused.pdf"), config, engine) == result
        assert len(engine.calls) == 1 and len(reads) == 1
        return
    assert len(stored.tables[0].period_conflicts) == 1
    assert [o.raw_text for o in stored.tables[0].period_evidence[0].observations] == [
        "31 December 2023",
        alternate,
    ]
    if missing == "2024":
        assert [p.key for p in stored.statements[0].periods] == ["FY2024"]
        assert "period_conflict:0" in stored.reviews[0].reasons
        # A completed comparison is cached, with no extra crops or upstream page reads.
        assert structure.structure_pdf(Path("unused.pdf"), config, engine) == result
        assert len(engine.calls) == 1 and len(reads) == 1
    else:
        assert stored.statements == [] and stored.reviews == []
        assert stored.tables[0].statement_id is None


def test_multiple_crop_lines_and_caller_groups_are_all_retained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fra_core.schemas.statement import PeriodEvidence

    observation, captured, _, engine = run_crop(monkeypatch, "31 December 3025")
    primary, alternate = captured.period_evidence[0].observations
    caller1 = alternate.model_copy(update={"source_id": "caller-a", "raw_text": "2022"})
    caller2 = alternate.model_copy(update={"source_id": "caller-b", "raw_text": "2021"})
    base = inputs([(observation.docling_path, observation.document())]).model_copy(
        update={
            "page_modes": {observation.grid.page_no: PageMode.IMAGE},
            "title_types": {observation.grid.page_no: (StatementType.INCOME,)},
            "period_evidence": (
                PeriodEvidence(observations=(primary, caller1)),
                PeriodEvidence(observations=(primary, caller2)),
            ),
        }
    )
    # Two different recognition entries retain their real line-index identities, even
    # when literals agree. Neither is a fabricated full-page OCR token identity.
    engine.lines.append(next(line for line in engine.lines if line.text == "31 December 3025"))

    def recovery(grid: Grid, document: DlDocument) -> Grid:
        return header_recovery.recover_header(
            grid, document, Path("unused.pdf"), IngestConfig(), engine
        )

    result, _ = structure.structure_document(base, IngestConfig(), header_recover=recovery)
    observations = result.tables[0].period_evidence[0].observations
    assert [o.raw_text for o in observations] == [
        "31 December 2023",
        "31 December 3025",
        "31 December 3025",
        "2022",
        "2021",
    ]
    assert len({o.source_id for o in observations}) == 5
    assert (
        tuple(c.observation for c in result.statements[0].period_conflicts[0].candidates)
        == observations
    )
    clash = PeriodEvidence(
        observations=(primary, alternate.model_copy(update={"raw_text": "2020"}))
    )
    base = base.model_copy(update={"period_evidence": (clash,)})
    with pytest.raises(ValueError, match=r"source ID.*different facts"):
        structure.structure_document(base, IngestConfig(), header_recover=recovery)


@pytest.mark.parametrize(
    "mutation", ["bbox", "source_span", "source_row", "duplicate_source", "multirow_literal"]
)
def test_original_source_contract_is_required_not_a_supplied_unit_grid_cell(
    monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    observation = partial_observation(header="31 December 2023")
    grid = observation.grid
    document = observation.document()
    table = document.tables[0]
    source = table.data.table_cells[0]
    assert source.bbox is not None
    if mutation == "bbox":
        table.data.table_cells[0] = source.model_copy(
            update={"bbox": source.bbox.model_copy(update={"left": source.bbox.left - 1})}
        )
    elif mutation == "source_span":
        table.data.table_cells[0] = source.model_copy(update={"row_span": 0})
    elif mutation == "source_row":
        table.data.table_cells[0] = source.model_copy(update={"start_row_offset_idx": -1})
    elif mutation == "duplicate_source":
        table.data.table_cells.append(source)
    else:
        # A second source header contributes status; a single date box cannot stand
        # for that composite multirow literal.
        other = source.model_copy(
            update={
                "text": "(Restated)",
                "start_row_offset_idx": 1,
                "bbox": source.bbox.model_copy(
                    update={"top": source.bbox.bottom + 1, "bottom": source.bbox.bottom + 4}
                ),
            }
        )
        table.data.table_cells.append(other)
        grid = build_grid(table, document, observation.docling_path)
    requests = inject_render(monkeypatch, observation)
    engine = RecordedEngine(observation)
    result = header_recovery.recover_header(
        grid, document, Path("unused.pdf"), IngestConfig(), engine
    )
    assert not result.period_evidence
    assert "period_evidence_unavailable:0:source_anchor_invalid" in result.flags
    assert "header_recovery_source_anchor_invalid" not in result.flags
    assert result.cells == grid.cells
    # The bound column has no comparison; the missing slot is still read, once.
    assert len(requests) == 1 and len(engine.calls) == 1


@pytest.mark.parametrize(
    "mutation", ["outside_anchor", "cross_column", "cross_row", "nan", "unit_overflow"]
)
def test_unsupported_alternate_geometry_is_unavailable_without_a_comparison(
    monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    observation = partial_observation(header="31 December 2023")
    inject_render(monkeypatch, observation)
    engine = RecordedEngine(observation)
    lines = safety_lines("For the year ended 31 December", ("31 December 3025", "2024"))
    line = next(line for line in lines if line.text == "31 December 3025")
    if mutation == "outside_anchor":
        changed = replace(line, left=line.left - 0.001)
    elif mutation == "cross_column":
        changed = replace(line, width=0.4)
    elif mutation == "cross_row":
        changed = replace(line, top=line.top - 0.003, height=line.height + 0.003)
    elif mutation == "nan":
        changed = replace(line, left=float("nan"))
    else:
        changed = replace(line, left=1.1)
    engine.lines = [changed if item == line else item for item in lines]
    # This engine deliberately returns malformed normalized geometry too; it does
    # not filter or clamp unsupported output as the recorded engine normally does.
    from PIL import Image

    from fra_ingest.ocr import OcrLine

    def recognize(image: Image.Image, languages: Sequence[str]) -> list[OcrLine]:
        normalized = RecordedEngine.recognize(engine, image, languages)
        if mutation in ("nan", "unit_overflow"):
            normalized.append(changed)
        return normalized

    monkeypatch.setattr(engine, "recognize", recognize)
    result = header_recovery.recover_header(
        observation.grid, observation.document(), Path("unused.pdf"), IngestConfig(), engine
    )
    assert not result.period_evidence
    assert any(f.startswith("period_evidence_unavailable:0:") for f in result.flags)
    assert result.cells == observation.grid.cells
    assert len(engine.calls) == 1


def test_actual_rounded_crop_extent_defines_alternate_box(monkeypatch: pytest.MonkeyPatch) -> None:
    from PIL import Image

    from fra_core.schemas import BBox
    from fra_ingest.ocr import OcrLine

    observation = partial_observation(header="31 December 2023")
    crops: list[header_recovery.RasterCrop] = []
    returned: list[OcrLine] = []

    def render(_pdf: Path, page_no: int, bbox: BBox, scale: float) -> header_recovery.RasterCrop:
        actual = bbox.model_copy(update={"left": bbox.left + 0.125, "top": bbox.top + 0.125})
        image = Image.new("RGB", (round((actual.right - actual.left) * scale), 97))
        image.info["crop"] = actual.model_dump()
        crop = header_recovery.RasterCrop(image, actual, *observation.ocr.page_points)
        crops.append(crop)
        return crop

    monkeypatch.setattr(header_recovery, "render_crop", render)
    engine = RecordedEngine(observation)
    engine.lines = safety_lines("For the year ended 31 December", ("3025", "2024"))

    def recognize(image: Image.Image, languages: list[str]) -> list[OcrLine]:
        returned.extend(RecordedEngine.recognize(engine, image, languages))
        return returned

    monkeypatch.setattr(engine, "recognize", recognize)
    result = header_recovery.recover_header(
        observation.grid, observation.document(), Path("unused.pdf"), IngestConfig(), engine
    )
    alternate = result.period_evidence[0].observations[1]
    index = next(i for i, line in enumerate(returned) if line.text == "3025")
    assert alternate.source_id.endswith(f":{index}")
    assert alternate.provenance.bbox == crops[0].line_box(returned[index])
    assert alternate.raw_text == returned[index].text
    assert alternate.confidence == returned[index].confidence
    assert len(engine.calls) == 1


def test_fully_bound_table_never_renders_or_recognizes(monkeypatch: pytest.MonkeyPatch) -> None:
    observation = partial_observation(header="31 December 2023", target_header="31 December 2024")
    requests = inject_render(monkeypatch, observation)
    engine = RecordedEngine(observation)
    grid = observation.grid
    result = header_recovery.recover_header(
        grid, observation.document(), Path("unused.pdf"), IngestConfig(), engine
    )
    assert result == grid
    assert not requests and not engine.calls and not result.period_evidence


@pytest.mark.parametrize("geometry", ["out_of_page", "nonfinite", "sibling_overlap"])
def test_actual_crop_with_unsupported_extent_never_supplies_evidence(
    monkeypatch: pytest.MonkeyPatch, geometry: str
) -> None:
    from PIL import Image

    from fra_core.schemas import BBox

    observation = partial_observation(header="31 December 2023")
    document = observation.document()
    if geometry == "sibling_overlap":
        original = document.tables[0]
        prov = original.prov[0]
        sibling = original.model_copy(
            update={
                "self_ref": "#/tables/other",
                "prov": [
                    prov.model_copy(
                        update={
                            "bbox": prov.bbox.model_copy(
                                update={"coord_origin": "TOPLEFT", "top": 0.0, "bottom": 5.0}
                            )
                        }
                    )
                ],
            }
        )
        document.tables.append(sibling)

    def render(_pdf: Path, page_no: int, bbox: BBox, scale: float) -> header_recovery.RasterCrop:
        top = -1 if geometry == "out_of_page" else float("nan") if geometry == "nonfinite" else 0
        actual = bbox.model_copy(update={"top": top})
        return header_recovery.RasterCrop(
            Image.new("RGB", (20, 20)), actual, *observation.ocr.page_points
        )

    monkeypatch.setattr(header_recovery, "render_crop", render)
    from support import FakeOcr

    engine = FakeOcr()
    result = header_recovery.recover_header(
        observation.grid, document, Path("unused.pdf"), IngestConfig(), engine
    )
    assert engine.calls == 1
    assert not result.period_evidence and not result.recovered_headers
    assert "period_evidence_unavailable:0:geometry_invalid" in result.flags


def test_partial_crop_hold_cannot_rebind_from_previous_part(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fra_ingest.docling_json import DlDocument

    previous = partial_observation(header="31 December 2023", target_header="31 December 2024")
    current = partial_observation(header="31 December 2023")
    document = current.document()
    page = current.grid.page_no
    prior_table = previous.table.model_copy(
        update={
            "self_ref": "#/tables/prior",
            "prov": [previous.table.prov[0].model_copy(update={"page_no": page - 1})],
        }
    )
    document = DlDocument(
        tables=[prior_table, current.table],
        texts=document.texts,
        pages={
            **document.pages,
            str(page - 1): current.page.model_copy(update={"page_no": page - 1}),
        },
    )
    base = inputs([(current.docling_path, document)]).model_copy(
        update={
            "page_modes": {page - 1: PageMode.IMAGE, page: PageMode.IMAGE},
            "title_types": {page - 1: (StatementType.INCOME,), page: (StatementType.INCOME,)},
        }
    )
    inject_render(monkeypatch, current)
    engine = RecordedEngine(current)
    engine.lines = safety_lines("For the year ended 31 December", ("3025", "31 April 2024"))

    def recover(grid: Grid, doc: DlDocument) -> Grid:
        return header_recovery.recover_header(grid, doc, Path("unused.pdf"), IngestConfig(), engine)

    result, _ = structure.structure_document(base, IngestConfig(), header_recover=recover)
    assert len(engine.calls) == 1
    decision = result.tables[1]
    assert decision.statement_id is None
    assert decision.period_conflicts[0].col == 0
    assert all(page not in statement.source_pages for statement in result.statements)


def test_unusable_source_anchor_still_recovers_the_missing_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation = partial_observation(header="31 December 2023")
    grid = observation.grid  # built before the source cell changes under it
    document = observation.document()
    source = document.tables[0].data.table_cells[0]
    assert source.bbox is not None
    # The original rectangle differs from the grid cell's: no primary observation.
    document.tables[0].data.table_cells[0] = source.model_copy(
        update={"bbox": source.bbox.model_copy(update={"left": source.bbox.left - 1})}
    )
    inject_render(monkeypatch, observation)
    engine = RecordedEngine(observation)
    engine.lines = safety_lines(
        "For the year ended 31 December", ("31 December 2023", "31 December 2024")
    )
    result = header_recovery.recover_header(
        grid, document, Path("unused.pdf"), IngestConfig(), engine
    )
    assert [c.col for c in result.recovered_headers] == [1]
    assert not result.period_evidence
    assert result.flags == (
        "period_evidence_unavailable:0:source_anchor_invalid",
        "header_recovered",
    )
    assert len(engine.calls) == 1


@pytest.mark.parametrize("inset", [0.0, 0.002])
def test_alternate_inside_the_source_rectangle_is_collected(
    monkeypatch: pytest.MonkeyPatch, inset: float
) -> None:
    observation = partial_observation(header="31 December 2023")
    inject_render(monkeypatch, observation)
    engine = RecordedEngine(observation)
    lines = safety_lines("For the year ended 31 December", ("31 December 2023", "2024"))
    engine.lines = [
        replace(
            line,
            left=line.left + inset,
            top=line.top + inset / 4,
            width=line.width - 2 * inset,
            height=line.height - inset / 2,
        )
        if line.text == "31 December 2023"
        else line
        for line in lines
    ]
    result = header_recovery.recover_header(
        observation.grid, observation.document(), Path("unused.pdf"), IngestConfig(), engine
    )
    primary, alternate = result.period_evidence[0].observations
    box, source = alternate.provenance.bbox, primary.provenance.bbox
    assert source.left <= box.left < box.right <= source.right
    assert source.top <= box.top < box.bottom <= source.bottom
    assert (box != source) is bool(inset)
    assert not any(f.startswith("period_evidence_unavailable:") for f in result.flags)


def test_failed_read_is_named_for_each_bound_column(monkeypatch: pytest.MonkeyPatch) -> None:
    observation = partial_observation(header="31 December 2023")
    inject_render(monkeypatch, observation)
    from PIL import Image

    from fra_ingest.ocr import OcrLine

    calls: list[int] = []
    engine = RecordedEngine(observation)

    def failed(_image: Image.Image, _languages: Sequence[str]) -> list[OcrLine]:
        calls.append(1)
        raise RuntimeError("bridge down")

    monkeypatch.setattr(engine, "recognize", failed)
    result = header_recovery.recover_header(
        observation.grid, observation.document(), Path("unused.pdf"), IngestConfig(), engine
    )
    assert result.flags == (
        "header_recovery_failed:RuntimeError:bridge down",
        "period_evidence_unavailable:0:failed:RuntimeError:bridge down",
    )
    assert not result.period_evidence and calls == [1]


def test_collected_group_the_original_grid_cannot_carry_is_dropped_not_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation, recovered, _, _ = run_crop(monkeypatch, "31 December 3025")
    group = recovered.period_evidence[0]
    # A collector working on the repaired grid names a row the original grid does not
    # treat as a header: the comparison is unavailable, and the document still structures.
    moved = group.model_copy(
        update={
            "observations": tuple(
                o.model_copy(update={"provenance": o.provenance.model_copy(update={"row": 3})})
                for o in group.observations
            )
        }
    )
    base = inputs([(observation.docling_path, observation.document())]).model_copy(
        update={
            "page_modes": {observation.grid.page_no: PageMode.IMAGE},
            "title_types": {observation.grid.page_no: (StatementType.INCOME,)},
        }
    )

    def recovery(grid: Grid, document: DlDocument) -> Grid:
        return recovered.model_copy(update={"period_evidence": (moved,)})

    result, _ = structure.structure_document(base, IngestConfig(), header_recover=recovery)
    assert not result.tables[0].period_evidence and not result.tables[0].period_conflicts
    assert "period_evidence_unavailable:0:raw_source_mismatch" in result.tables[0].evidence
    assert [p.key for p in result.statements[0].periods] == ["FY2023", "FY2024"]


def test_invalid_caller_group_fails_before_any_crop_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fra_core.schemas.statement import PeriodEvidence

    observation, recovered, _, engine = run_crop(monkeypatch, "31 December 3025")
    primary = recovered.period_evidence[0].observations[0]
    far = primary.provenance.bbox.model_copy(update={"left": 0.0, "right": 1.0})
    invalid = PeriodEvidence(
        observations=(
            primary.model_copy(
                update={
                    "source_id": "caller",
                    "provenance": primary.provenance.model_copy(update={"bbox": far}),
                }
            ),
            primary.model_copy(update={"source_id": "caller-2"}),
        )
    )
    base = inputs([(observation.docling_path, observation.document())]).model_copy(
        update={
            "page_modes": {observation.grid.page_no: PageMode.IMAGE},
            "title_types": {observation.grid.page_no: (StatementType.INCOME,)},
            "period_evidence": (invalid,),
        }
    )
    before = len(engine.calls)
    recoveries: list[int] = []

    def recovery(grid: Grid, document: DlDocument) -> Grid:
        recoveries.append(1)
        return grid

    with pytest.raises(ValueError, match="invalid header geometry"):
        structure.structure_document(base, IngestConfig(), header_recover=recovery)
    assert not recoveries and len(engine.calls) == before


def test_rounded_crop_touching_a_sibling_still_recovers_without_a_comparison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from PIL import Image

    from fra_core.schemas import BBox

    observation = partial_observation(header="31 December 2023")
    requested = inject_render(monkeypatch, observation)
    probe = RecordedEngine(observation)
    lines = safety_lines("For the year ended 31 December", ("31 December 2023", "31 December 2024"))
    probe.lines = lines
    header_recovery.recover_header(
        observation.grid, observation.document(), Path("unused.pdf"), IngestConfig(), probe
    )
    top = requested[0].top
    # A sibling table ends exactly where the requested crop starts: no overlap, as before.
    document = observation.document()
    prov = document.tables[0].prov[0]
    document.tables.append(
        document.tables[0].model_copy(
            update={
                "self_ref": "#/tables/above",
                "prov": [
                    prov.model_copy(
                        update={
                            "bbox": prov.bbox.model_copy(
                                update={"coord_origin": "TOPLEFT", "top": top - 5, "bottom": top}
                            )
                        }
                    )
                ],
            }
        )
    )

    def render(_pdf: Path, page_no: int, bbox: BBox, scale: float) -> header_recovery.RasterCrop:
        # Pixel rounding starts the rendered extent a fraction of a point early.
        actual = bbox.model_copy(update={"top": bbox.top - 0.01})
        image = Image.new("RGB", (round((bbox.right - bbox.left) * scale), 97))
        image.info["crop"] = bbox.model_dump()
        return header_recovery.RasterCrop(image, actual, *observation.ocr.page_points)

    monkeypatch.setattr(header_recovery, "render_crop", render)
    engine = RecordedEngine(observation)
    engine.lines = lines
    result = header_recovery.recover_header(
        observation.grid, document, Path("unused.pdf"), IngestConfig(), engine
    )
    assert [c.col for c in result.recovered_headers] == [1]
    assert not result.period_evidence
    assert result.flags == ("period_evidence_unavailable:0:geometry_invalid", "header_recovered")
