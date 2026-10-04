"""The structure stage: docling tables to statements with provenance and checks (spec 11).

``structure_document`` is pure and does the work; ``structure_pdf`` gathers its inputs from
Parts 1 and 2, caches by settings, and writes ``statements.raw.json`` and
``table_checks.json``.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from fra_core.periods import parse_period
from fra_core.schemas import CheckResult, LineItem, PageMode, Statement, StatementType, TextSource
from fra_core.taxonomy.loader import load_taxonomy
from fra_ingest.caveats import unit_caveats
from fra_ingest.child import convert_in_child
from fra_ingest.classify import Classification, TableContext, classify
from fra_ingest.config import IngestConfig
from fra_ingest.continuation import continues_part, inherit_periods, is_closed, merge_continuations
from fra_ingest.convert import CONVERT_VERSION, settings_hash
from fra_ingest.converter import docling_version
from fra_ingest.docling_json import DlDocument, load_docling_json
from fra_ingest.errors import IngestError
from fra_ingest.figure_checks import check_net_profit_tie
from fra_ingest.header import parse_header
from fra_ingest.hierarchy import RowInput, infer_hierarchy
from fra_ingest.label_mapping import map_statement
from fra_ingest.label_match import LabelIndex
from fra_ingest.metadata import Metadata, detect_metadata
from fra_ingest.ocr import OcrEngine
from fra_ingest.ocr_policy import plan_ranges
from fra_ingest.pages import read_pages
from fra_ingest.parts import PartialStatement, build_part
from fra_ingest.results import ConvertResult, LocateResult, StructureResult, TableDecision
from fra_ingest.review import StatementReview, review_statement
from fra_ingest.row_alignment import realign_rows
from fra_ingest.stage import load_or_locate, page_ocr_languages
from fra_ingest.table_checks import run_checks
from fra_ingest.table_grid import Grid, build_grid
from fra_ingest.text_match import reading_variants
from fra_ingest.visual_order import repair_grid, repair_text

STRUCTURE_VERSION = "12"  # bump whenever structure's output can change, reviews included
NO_CURRENCY = "XXX"  # ISO 4217 code for "no currency"
_FINANCIAL = ("bank", "insurer", "other_financial")


class StructureInputs(BaseModel):
    """Everything structure needs from Parts 1 and 2, gathered so the work itself is pure."""

    model_config = ConfigDict(extra="forbid")

    sha256: str
    language: str
    industry_flags: tuple[str, ...]
    page_modes: dict[int, PageMode]
    visual_pages: set[int]
    page_texts: dict[int, str]
    title_types: dict[int, tuple[StatementType, ...]]
    cue_types: dict[int, tuple[StatementType, ...]]
    documents: list[tuple[str, DlDocument]]
    convert: ConvertResult
    domicile_texts: dict[int, list[str]] = Field(
        default_factory=dict, description="Part 1's page texts and their reading variants, by page"
    )


def _headings(document: DlDocument, grid: Grid, visual: bool) -> list[str]:
    """Texts above the table on its page, top to bottom, the last six, repaired like the table."""
    height = document.page_size(grid.page_no).height
    tops = [c.bbox.top for c in grid.cells if c.bbox is not None]
    table_top = min(tops) if tops else height
    above: list[tuple[float, str]] = []
    for text in document.texts_on(grid.page_no):
        box = text.prov[0].bbox.to_bbox(height)
        if box is not None and box.bottom <= table_top + 1:
            above.append((box.top, repair_text(text.text, visual=visual)))
    return [t for _, t in sorted(above)][-6:]


def _date_hint(headings: Sequence[str]) -> str | None:
    return next((h for h in reversed(headings) if parse_period(h) is not None), None)


def _statement(
    part: PartialStatement, meta: Metadata, inputs: StructureInputs, index: LabelIndex, number: int
) -> Statement:
    rows = []
    for i, item in enumerate(part.line_items):
        matched = index.match(item.raw_label, part.type)
        rows.append(
            RowInput(
                row=i,
                label=item.raw_label,
                indent=part.indents.get(item.id),
                has_values=bool(item.cells),
                subtotal_hint=matched is not None and matched.subtotal,
            )
        )
    items: list[LineItem] = []
    for node, item in zip(infer_hierarchy(rows), part.line_items, strict=True):
        parent = part.line_items[node.parent_row].id if node.parent_row is not None else None
        items.append(
            item.model_copy(
                update={"depth": node.depth, "is_subtotal": node.is_subtotal, "parent_id": parent}
            )
        )
    caveats, caveat_flags = unit_caveats(meta, items)
    return Statement(
        id=f"{inputs.sha256[:12]}-{part.type.value}-{number}",
        document_sha256=inputs.sha256,
        type=part.type,
        entity_name=meta.entity_name,
        consolidated=meta.consolidated,
        currency=meta.currency or NO_CURRENCY,
        scale=meta.scale or 1,
        language=inputs.language,
        periods=part.periods,
        line_items=items,
        source_pages=list(range(part.first_page, part.last_page + 1)),
        flags=[*part.flags, *meta.flags, *caveat_flags],
        caveats=caveats,
    )


@dataclass
class _Candidate:
    grid: Grid
    result: Classification
    headings: list[str]
    hint: str | None
    decision: TableDecision


def _only_below_confidence(result: Classification) -> bool:
    rejections = {"no_period_header", "note_heading"}
    return "below_confidence" in result.evidence and not rejections & set(result.evidence)


def _continued_part(
    grid: Grid,
    hint: str | None,
    parts: Sequence[PartialStatement],
    titles: Sequence[StatementType],
) -> PartialStatement | None:
    """The statement part a low-confidence grid continues: the latest part ending on its page
    or the page before, whose periods and value columns the grid matches, a column its header
    leaves undated taking its period from that part first. A page titled as other statements
    only (``titles``, from locate) continues nothing."""
    previous = next(
        (p for p in reversed(parts) if p.last_page in (grid.page_no, grid.page_no - 1)), None
    )
    if previous is None or (titles and previous.type not in titles):
        return None
    layout = inherit_periods(parse_header(grid, previous.type, hint), grid, previous)
    if not continues_part(layout, grid, previous):
        return None
    return previous


def structure_document(
    inputs: StructureInputs, config: IngestConfig
) -> tuple[StructureResult, list[CheckResult]]:
    index = LabelIndex(load_taxonomy())
    decisions: list[TableDecision] = []
    # Every grid classify accepted, or rejected only for low confidence (a possible tail page).
    candidates: list[_Candidate] = []
    for path, document in inputs.documents:
        for table in document.tables:
            raw = build_grid(table, document, path)
            visual = raw.page_no in inputs.visual_pages
            grid = repair_grid(raw, visual=visual)
            headings = _headings(document, grid, visual)
            hint = _date_hint(headings)
            context = TableContext(
                title_types=inputs.title_types.get(grid.page_no, ()),
                cue_types=inputs.cue_types.get(grid.page_no, ()),
                heading_texts=tuple(headings),
                industry_flags=inputs.industry_flags,
            )
            result = classify(
                grid,
                parse_header(grid, StatementType.BALANCE, hint),
                context,
                index,
                config.min_confidence,
            )
            decision = TableDecision(
                table_ref=grid.table_ref,
                docling_path=path,
                page_no=grid.page_no,
                type=result.type,
                confidence=min(result.confidence, 1.0),
                evidence=result.evidence,
            )
            decisions.append(decision)
            if result.type is not None or _only_below_confidence(result):
                candidates.append(_Candidate(grid, result, headings, hint, decision))

    docling_texts = [
        repair_text(t.text, visual=t.prov[0].page_no in inputs.visual_pages)
        for _, document in inputs.documents
        for t in document.texts
        if t.prov and t.text
    ]
    document_texts = [*docling_texts, *(t for t in inputs.page_texts.values() if t)]
    by_page: dict[int, list[str]] = {n: list(t) for n, t in inputs.domicile_texts.items()}
    for _, document in inputs.documents:
        for t in document.texts:
            if t.prov and t.text:
                by_page.setdefault(t.prov[0].page_no, []).append(t.text)
    domicile_texts = [text for n in sorted(by_page) for text in by_page[n]]

    parts: list[PartialStatement] = []
    metas: dict[str, Metadata] = {}
    for candidate in sorted(candidates, key=lambda c: c.grid.page_no):
        grid, result, hint = candidate.grid, candidate.result, candidate.hint
        if result.type is None:
            titles = inputs.title_types.get(grid.page_no, ())
            continued = _continued_part(grid, hint, parts, titles)
            if continued is None:
                continue
            if is_closed(continued, index):
                candidate.decision.evidence = [
                    *result.evidence,
                    f"after_closed:{continued.type.value}",
                ]
                continue
            evidence = f"continuation_of:{continued.type.value}"
            result = result.model_copy(
                update={"type": continued.type, "evidence": [*result.evidence, evidence]}
            )
            candidate.decision.type = continued.type
            candidate.decision.evidence = result.evidence
        assert result.type is not None
        layout = parse_header(grid, result.type, hint)
        previous = next(
            (
                p
                for p in reversed(parts)
                if p.type is result.type and p.last_page in (grid.page_no, grid.page_no - 1)
            ),
            None,
        )
        if previous is not None:
            layout = inherit_periods(layout, grid, previous)
        if not layout.value_cols:
            continue
        source = (
            TextSource.OCR
            if inputs.page_modes.get(grid.page_no) is PageMode.IMAGE
            else TextSource.TEXT
        )
        realigned = realign_rows(grid, layout)
        part = build_part(
            realigned.grid,
            layout,
            result,
            source=source,
            index=index,
            merged_rows=realigned.merged_labels,
        )
        if realigned.moved:
            part.flags.append("rows_realigned")
        if realigned.unresolved:
            part.flags.append("row_alignment_unresolved")
        parts.append(part)
        # Keyed by the part's first table, which a merged part keeps from its head.
        metas[part.table_refs[0]] = detect_metadata(
            header_text=part.header_text,
            context_texts=[*candidate.headings, inputs.page_texts.get(grid.page_no, "")],
            document_texts=document_texts,
            domicile_texts=domicile_texts,
        )

    merged = sorted(
        merge_continuations(parts, index), key=lambda p: (p.type.value, -p.confidence, p.first_page)
    )
    per_type = Counter(p.type for p in merged)
    decision_by_ref = {f"{d.docling_path}{d.table_ref}": d for d in decisions}
    statements: list[Statement] = []
    checks: list[CheckResult] = []
    for number, part in enumerate(merged, start=1):
        if per_type[part.type] > 1:
            part = part.model_copy(
                update={"flags": [*part.flags, f"ambiguous_statement:{part.type.value}"]}
            )
        statement, results = run_checks(
            _statement(part, metas[part.table_refs[0]], inputs, index, number), index
        )
        statement = map_statement(statement, index, results)
        statements.append(statement)
        checks.extend(results)
        for ref in part.table_refs:
            if ref in decision_by_ref:
                decision_by_ref[ref].statement_id = statement.id

    first: dict[StatementType, int] = {}
    for position, s in enumerate(statements):
        first.setdefault(s.type, position)
    ties: list[CheckResult] = []
    if StatementType.INCOME in first and StatementType.COMPREHENSIVE_INCOME in first:
        tied = (first[StatementType.INCOME], first[StatementType.COMPREHENSIVE_INCOME])
        ties = check_net_profit_tie(statements[tied[0]], statements[tied[1]])
        checks.extend(ties)
        if any(t.status == "fail" for t in ties):
            for position in tied:
                s = statements[position]
                statements[position] = s.model_copy(update={"flags": [*s.flags, "tie_failed"]})

    seen: set[StatementType] = set()
    reviews: list[StatementReview] = []
    for position, s in enumerate(statements):
        own = [c for c in checks if c.statement_id == s.id]
        if s.type is StatementType.INCOME and s.type not in seen:
            own += ties  # recorded on comprehensive income; they vouch for this statement too
        review = review_statement(s, own, primary=s.type not in seen)
        seen.add(s.type)
        reviews.append(review)
        if review.status == "needs_review":
            statements[position] = s.model_copy(update={"flags": [*s.flags, "needs_review"]})

    found = {s.type for s in statements}
    flags = [f"statement_not_extracted:{t.value}" for t in config.enabled_types if t not in found]
    flags += [
        f"range_not_converted:{r.first_page}-{r.last_page}"
        for r in inputs.convert.ranges
        if r.status in ("failed", "skipped")
    ]
    structured = StructureResult(
        version=STRUCTURE_VERSION,
        sha256=inputs.sha256,
        convert_version=inputs.convert.version,
        settings_hash="",
        statements=statements,
        tables=decisions,
        reviews=reviews,
        flags=flags,
    )
    return structured, checks


def _settings_hash(config: IngestConfig, convert: ConvertResult) -> str:
    payload = {
        "structure": STRUCTURE_VERSION,
        "convert": convert.settings_hash,
        "min_confidence": config.min_confidence,
        "enabled_types": sorted(t.value for t in config.enabled_types),
        "taxonomy": load_taxonomy().version,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _write(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _convert(pdf: Path, config: IngestConfig) -> ConvertResult:
    return convert_in_child(pdf, config)


def _missing_docling(out_dir: Path, result: ConvertResult) -> list[str]:
    return [
        r.docling_path
        for r in result.ranges
        if r.docling_path and not (out_dir / r.docling_path).is_file()
    ]


def _stored_convert(out_dir: Path, digest: str) -> ConvertResult | None:
    """``convert.json`` when convert would write the same today and its docling files exist."""
    path = out_dir / "convert.json"
    if not path.is_file():
        return None
    try:
        stored = ConvertResult.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError:
        return None
    if stored.version != CONVERT_VERSION or stored.settings_hash != digest:
        return None
    return None if _missing_docling(out_dir, stored) else stored


def current_convert(
    pdf: Path,
    config: IngestConfig,
    ocr: OcrEngine | None,
    located: LocateResult,
    convert: Callable[[Path, IngestConfig], ConvertResult],
) -> ConvertResult:
    """The stored convert result when it is current and complete, else a fresh conversion."""
    out_dir = config.artifact_root / located.document.sha256
    plans = plan_ranges(located, page_ocr_languages(pdf, config, ocr), config)
    digest = settings_hash(config, plans, docling_version(), located.version)
    stored = _stored_convert(out_dir, digest)
    if stored is not None:
        return stored
    converted = convert(pdf, config)
    missing = _missing_docling(out_dir, converted)
    if missing:
        raise IngestError("convert_failed", f"docling output missing: {', '.join(missing)}")
    return converted


def structure_pdf(
    pdf: Path,
    config: IngestConfig,
    ocr: OcrEngine | None,
    *,
    use_cache: bool = True,
    convert: Callable[[Path, IngestConfig], ConvertResult] = _convert,
) -> StructureResult:
    started = time.perf_counter()
    located = load_or_locate(pdf, config, ocr)
    out_dir = config.artifact_root / located.document.sha256
    converted = current_convert(pdf, config, ocr, located, convert)

    digest = _settings_hash(config, converted)
    target = out_dir / "statements.raw.json"
    if use_cache and target.is_file():
        try:
            cached = StructureResult.model_validate_json(target.read_text(encoding="utf-8"))
        except ValidationError:
            cached = None
        if (
            cached is not None
            and cached.version == STRUCTURE_VERSION
            and cached.settings_hash == digest
            and (out_dir / "table_checks.json").is_file()
        ):
            return cached

    pages = read_pages(pdf, config, ocr, cache_dir=out_dir)
    kind = located.industry.kind
    inputs = StructureInputs(
        sha256=located.document.sha256,
        language=located.document.language,
        industry_flags=(f"likely_{kind}",) if kind in _FINANCIAL else (),
        page_modes={p.page_no: p.mode for p in pages},
        visual_pages={p.page_no for p in pages if p.visual_arabic},
        page_texts={p.page_no: repair_text(p.text, visual=False) for p in pages},
        title_types={p.page_no: tuple(p.title_types) for p in located.pages},
        cue_types={p.page_no: tuple(p.cue_types) for p in located.pages},
        documents=[
            (r.docling_path, load_docling_json(out_dir / r.docling_path))
            for r in converted.ranges
            if r.docling_path
        ],
        convert=converted,
        domicile_texts={p.page_no: reading_variants(p.text, p.visual_arabic) for p in pages},
    )
    result, checks = structure_document(inputs, config)
    result = result.model_copy(
        update={"settings_hash": digest, "timings": {"structure": time.perf_counter() - started}}
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    # Checks first: a present statements.raw.json promises the checks beside it are current.
    _write(
        out_dir / "table_checks.json",
        json.dumps([c.model_dump(mode="json") for c in checks], indent=2, ensure_ascii=False),
    )
    _write(target, result.model_dump_json(indent=2))
    return result
