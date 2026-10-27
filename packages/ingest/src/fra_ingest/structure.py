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
from typing import NamedTuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from fra_core.periods import parse_period
from fra_core.schemas import CheckResult, LineItem, PageMode, Statement, StatementType, TextSource
from fra_core.schemas.statement import PeriodEvidence
from fra_core.taxonomy.loader import load_taxonomy
from fra_ingest.caveats import unit_caveats
from fra_ingest.child import convert_in_child
from fra_ingest.classify import Classification, TableContext, classify
from fra_ingest.column_assignment import split_numeric_columns
from fra_ingest.config import IngestConfig
from fra_ingest.continuation import continues_part, inherit_periods, is_closed, merge_continuations
from fra_ingest.convert import cached_convert, invalidate_convert, settings_hash
from fra_ingest.converter import docling_version
from fra_ingest.docling_json import DlDocument, load_docling_json
from fra_ingest.eps_checks import EpsNote, check_eps, structure_eps_note
from fra_ingest.errors import IngestError
from fra_ingest.figure_checks import check_net_profit_tie
from fra_ingest.header import HeaderLayout, parse_header
from fra_ingest.header_recovery import recover_header
from fra_ingest.hierarchy import RowInput, infer_hierarchy
from fra_ingest.industry import industry_decision
from fra_ingest.label_mapping import eps_heading, map_statement
from fra_ingest.label_match import LabelIndex
from fra_ingest.locate import TITLE_LINE_MAX_WORDS, TitleBook, load_title_book
from fra_ingest.metadata import Metadata, detect_metadata, statement_title_scope
from fra_ingest.ocr import OcrEngine
from fra_ingest.ocr_policy import plan_ranges
from fra_ingest.pages import ocr_key, read_pages
from fra_ingest.parts import PartialStatement, build_part
from fra_ingest.period_evidence import merge_evidence, supported_evidence, validate_evidence
from fra_ingest.results import (
    ConvertResult,
    IndustryDecision,
    LocateResult,
    RangeConversion,
    StructureResult,
    TableDecision,
)
from fra_ingest.review import (
    StatementReview,
    confirm_arithmetic,
    hold_for_industry,
    review_statement,
)
from fra_ingest.row_alignment import Realignment, realign_rows
from fra_ingest.stage import load_or_locate, page_ocr_languages
from fra_ingest.statement_source import (
    TableSourceContext,
    resolve_tied_blocks,
    source_plan,
    start_decision,
    table_source,
)
from fra_ingest.table_checks import run_checks
from fra_ingest.table_grid import Grid, build_grid
from fra_ingest.text_match import reading_variants
from fra_ingest.visual_order import (
    page_vocabulary,
    repair_grid,
    repair_text,
    restore_word_order,
)

# Arithmetic confirmation and OCR opening-brace repair change cached results.
STRUCTURE_VERSION = "54"
NO_CURRENCY = "XXX"  # ISO 4217 code for "no currency"
_FINANCIAL = ("bank", "insurer", "other_financial")


def range_conversion_flags(ranges: Sequence[RangeConversion]) -> list[str]:
    """Incomplete ranges, in structure's stable order: unconverted, then partial."""
    return [
        f"range_not_converted:{r.first_page}-{r.last_page}"
        for r in ranges
        if r.status in ("failed", "skipped")
    ] + [
        f"range_partially_converted:{r.first_page}-{r.last_page}"
        for r in ranges
        if r.status == "partial"
    ]


class StructureInputs(BaseModel):
    """Everything structure needs from Parts 1 and 2, gathered so the work itself is pure."""

    model_config = ConfigDict(extra="forbid")

    period_evidence: tuple[PeriodEvidence, ...] = ()
    sha256: str
    language: str
    industry_flags: tuple[str, ...]
    page_modes: dict[int, PageMode]
    visual_pages: set[int]
    page_texts: dict[int, str]
    located: LocateResult
    documents: list[tuple[str, DlDocument]]
    convert: ConvertResult
    industry_hold: str | None = Field(
        default=None, description="Why the industry verdict is uncertain: holds every statement"
    )
    domicile_texts: dict[int, list[str]] = Field(
        default_factory=dict, description="Part 1's page texts and their reading variants, by page"
    )

    @property
    def title_types(self) -> dict[int, tuple[StatementType, ...]]:
        return {p.page_no: tuple(p.title_types) for p in self.located.pages}

    @property
    def cue_types(self) -> dict[int, tuple[StatementType, ...]]:
        return {p.page_no: tuple(p.cue_types) for p in self.located.pages}


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
        period_conflicts=part.period_conflicts,
        line_items=items,
        source_pages=list(range(part.first_page, part.last_page + 1)),
        flags=list(dict.fromkeys([*part.flags, *meta.flags, *caveat_flags])),
        caveats=caveats,
    )


@dataclass
class _Candidate:
    grid: Grid
    result: Classification
    headings: list[str]
    hint: str | None
    decision: TableDecision
    period_evidence: tuple[PeriodEvidence, ...]

    @property
    def proposed_type(self) -> StatementType:
        kind = self.result.proposed_type
        if kind is None:
            raise ValueError(f"source candidate has no proposed type: {self.grid.table_ref}")
        return kind


def _local_statement_titles(
    headings: Sequence[str], kind: StatementType, book: TitleBook
) -> tuple[str, ...]:
    """The nearest recognized title, excluding running notes headings."""
    recognized = []
    for line in (line for h in headings for line in h.splitlines()):
        hits = book.titles.find([line])
        if len(line.split()) <= TITLE_LINE_MAX_WORDS and not any(c.isdigit() for c in line):
            hits.update(book.title_stems.find([line]))
        if (
            any(kind.value in groups for groups in hits.values())
            and not book.negatives.find([line])
            and not book.note_headers.find([line])
        ):
            recognized.append(line)
    return tuple(recognized[-1:])


def _statement_title_follows_eps_heading(headings: Sequence[str], book: TitleBook) -> bool:
    """Whether a statement title sits below the last EPS heading above the table."""
    eps = [i for i, h in enumerate(headings) if eps_heading(h)]
    return bool(eps) and any(
        _local_statement_titles(headings[eps[-1] + 1 :], kind, book) for kind in StatementType
    )


def _candidate_order(candidate: _Candidate) -> tuple[int, str, int]:
    tail = candidate.grid.table_ref.rsplit("/", 1)[-1]
    return candidate.grid.page_no, candidate.grid.docling_path, int(tail) if tail.isdigit() else -1


def _only_below_confidence(result: Classification) -> bool:
    rejections = {"no_period_header", "note_heading"}
    return "below_confidence" in result.evidence and not rejections & set(result.evidence)


def _aligned(grid: Grid, layout: HeaderLayout, *, ocr: bool) -> Realignment:
    """The grid with its rows realigned and, on an OCR page, its merged amounts split."""
    realigned = realign_rows(grid, layout)
    if not ocr:
        return realigned
    return realigned.model_copy(update={"grid": split_numeric_columns(realigned.grid, layout)})


class _Probe(NamedTuple):
    layout: HeaderLayout
    aligned: Realignment | None
    fits: bool


def _probe(layout: HeaderLayout, grid: Grid, part: PartialStatement, *, ocr: bool) -> _Probe:
    """``layout`` with the periods ``part`` lends it, the OCR-realigned grid (``None`` off OCR)
    and whether the columns line up with ``part``. On an OCR page the columns are compared after
    merged amounts are split, and cells still holding merged amounts are left out of the centres
    used to inherit periods and to compare, so a wide merged rectangle cannot pull a column
    centre past the tolerance."""
    inherited = inherit_periods(layout, grid, part, ocr=ocr)
    aligned = _aligned(grid, inherited, ocr=True) if ocr else None
    return _Probe(
        inherited,
        aligned,
        continues_part(inherited, grid if aligned is None else aligned.grid, part, ocr=ocr),
    )


def structure_document(
    inputs: StructureInputs,
    config: IngestConfig,
    *,
    header_recover: Callable[[Grid, DlDocument], Grid] | None = None,
) -> tuple[StructureResult, list[CheckResult]]:
    plan = source_plan(
        inputs.located.ranges,
        inputs.located.pages,
        config.enabled_types,
        inputs.located.document.page_count,
        config.pad_pages,
    )
    index = LabelIndex(load_taxonomy())
    book = load_title_book()
    decisions: list[TableDecision] = []
    eps_notes: list[EpsNote] = []
    # Every grid classify accepted, or rejected only for low confidence (a possible tail page).
    candidates: list[_Candidate] = []
    remaining = set(range(len(inputs.period_evidence)))
    for path, document in inputs.documents:
        for table in document.tables:
            raw = build_grid(table, document, path)
            scoped = tuple(
                group
                for group in inputs.period_evidence
                if (
                    group.observations[0].docling_path,
                    group.observations[0].provenance.page_no,
                    group.observations[0].provenance.table_ref,
                )
                == (path, raw.page_no, raw.table_ref)
            )
            for n, group in enumerate(inputs.period_evidence):
                if group in scoped:
                    remaining.discard(n)
            raw_rows = parse_header(raw, StatementType.BALANCE, None).header_rows
            height = document.page_size(raw.page_no).height
            # A caller's group is checked against the original grid before any crop is read.
            scoped = merge_evidence(scoped)
            validate_evidence(raw, scoped, raw_rows, source_table=table, page_height=height)
            visual = raw.page_no in inputs.visual_pages
            vocabulary = page_vocabulary(inputs.page_texts.get(raw.page_no, ""), visual=visual)
            grid = restore_word_order(repair_grid(raw, visual=visual, vocabulary=vocabulary), index)
            if header_recover is not None and inputs.page_modes.get(grid.page_no) is PageMode.IMAGE:
                grid = header_recover(grid, document)
            collected, unsupported = supported_evidence(
                raw, grid.period_evidence, raw_rows, source_table=table, page_height=height
            )
            grid = grid.model_copy(
                update={"period_evidence": collected, "flags": (*grid.flags, *unsupported)}
            )
            # Merge by actual column, without overwriting readings from either producer.
            scoped = merge_evidence(collected, scoped)
            headings = _headings(document, grid, visual)
            note = structure_eps_note(
                grid.model_copy(update={"period_evidence": scoped}),
                headings,
                index,
                source=TextSource.OCR
                if inputs.page_modes.get(grid.page_no) is PageMode.IMAGE
                else TextSource.TEXT,
                visual=visual,
                context_texts=reading_variants(inputs.page_texts.get(grid.page_no, ""), visual),
            )
            if note is not None and _statement_title_follows_eps_heading(headings, book):
                note = None  # a later statement title governs this table
            if note is not None:
                eps_notes.append(note)
            hint = _date_hint(headings)
            context = TableContext(
                title_types=inputs.title_types.get(grid.page_no, ()),
                cue_types=inputs.cue_types.get(grid.page_no, ()),
                heading_texts=tuple(headings),
                industry_flags=inputs.industry_flags,
            )
            initial_layout = parse_header(grid, StatementType.BALANCE, hint)
            result = classify(
                grid,
                initial_layout,
                context,
                index,
                config.min_confidence,
            )
            if scoped:
                # Classification probes date presence; binding uses the classified kind.
                initial_layout = parse_header(
                    grid,
                    result.type or StatementType.BALANCE,
                    hint,
                    period_evidence=scoped,
                    visual=visual,
                )
            decision = TableDecision(
                table_ref=grid.table_ref,
                docling_path=path,
                page_no=grid.page_no,
                type=result.type,
                confidence=min(result.confidence, 1.0),
                period_conflicts=initial_layout.period_conflicts,
                evidence=[
                    *result.evidence,
                    *(["source_outside_range"] if not plan.ranges else []),
                    *(f for f in initial_layout.evidence if scoped and f.startswith("period_")),
                    *(
                        f
                        for f in grid.flags
                        if f.startswith(
                            ("header_recovery_", "header_recovered", "period_evidence_")
                        )
                    ),
                ],
                recovered_headers=grid.recovered_headers,
                recovery_context=grid.recovered_context,
                period_evidence=scoped,
            )
            decisions.append(decision)
            if note is not None:
                decision.type = None
                decision.evidence.append("eps_note")
            if result.proposed_type is not None and note is None:
                candidates.append(_Candidate(grid, result, headings, hint, decision, scoped))

    if remaining:
        raise ValueError(
            f"period evidence does not match an input path/page/table: {sorted(remaining)}"
        )

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
    # Scope evidence is reserved from located primary titles, even if extraction fails.
    local_titles: dict[int, tuple[str, ...]] = {}
    scopes: dict[int, bool | None] = {}
    data_rows: dict[int, int] = {}
    for n, candidate in enumerate(candidates):
        kind = candidate.proposed_type
        local_titles[n] = _local_statement_titles(candidate.headings, kind, book)
        scopes[n] = statement_title_scope(local_titles[n])
        data_rows[n] = len(
            parse_header(candidate.grid, kind, candidate.hint).data_rows(candidate.grid)
        )

    def source_contexts() -> dict[int, TableSourceContext]:
        return {
            n: table_source(plan, c.grid.page_no, c.proposed_type, data_rows[n])
            for n, c in enumerate(candidates)
        }

    contexts = source_contexts()
    # Competing blocks may be resolved only as a pair with fully explicit, opposite local scopes.
    # Each competing group is judged alone: blocks elsewhere in the tie do not matter.

    def block_scope(span: tuple[int, int]) -> bool | None:
        values = {
            scopes[n]
            for n, c in contexts.items()
            if span[0] <= c.page_no <= span[1] and c.matching and local_titles[n]
        }
        return next(iter(values)) if len(values) == 1 else None

    resolving = [
        blocks
        for blocks in plan.competing
        if len(blocks.spans) == 2 and {block_scope(s) for s in blocks.spans} == {True, False}
    ]
    for blocks in resolving:
        plan = resolve_tied_blocks(plan, blocks)
    if resolving:
        contexts = source_contexts()
    primary_scopes: dict[StatementType, bool | None] = {}
    for kind in config.enabled_types:
        heads = sorted(
            (
                n
                for n, c in contexts.items()
                if c.proposed_type is kind and c.role == "primary" and local_titles[n]
            ),
            key=lambda n: _candidate_order(candidates[n]),
        )
        # The first explicit scope on the range, not the first title: a generic title carries none.
        primary_scopes[kind] = next((scopes[n] for n in heads if scopes[n] is not None), None)

    part_scopes: dict[str, bool | None] = {}
    part_order: dict[str, tuple[int, int, int]] = {}
    for n in sorted(
        range(len(candidates)),
        key=lambda n: _candidate_order(candidates[n]),
    ):
        candidate = candidates[n]
        grid, result, hint = candidate.grid, candidate.result, candidate.hint
        kind = candidate.proposed_type
        nearby = [
            p
            for p in reversed(parts)
            if (p.type is kind or _only_below_confidence(result))
            and p.last_page in (grid.page_no, grid.page_no - 1)
        ]
        previous = nearby[0] if nearby else None
        if previous is not None and _only_below_confidence(result) and previous.type is not kind:
            # Low-confidence tails have no reliable winning kind. Preserve the existing
            # continuation rule by probing the latest authorized part's kind instead.
            kind = previous.type
            source_context = table_source(
                plan, grid.page_no, kind, len(parse_header(grid, kind, hint).data_rows(grid))
            )
            local_titles[n] = _local_statement_titles(candidate.headings, kind, book)
            scopes[n] = statement_title_scope(local_titles[n])
        else:
            source_context = contexts[n]
        eligibility = start_decision(
            source_context, primary_scopes.get(kind), scopes[n], bool(local_titles[n])
        )
        candidate.decision.type = None
        candidate.decision.evidence = [*candidate.decision.evidence, *eligibility.evidence]
        alternatives: list[PartialStatement] = []
        if source_context.negatives or (
            source_context.titles and kind not in source_context.titles
        ):
            previous = None
        else:
            # The latest nearby part of the candidate's own kind, scope and source group.
            fitting = [
                p
                for p in nearby
                if p.type is kind
                and not (
                    scopes[n] is not None
                    and part_scopes[p.table_refs[0]] is not None
                    and scopes[n] is not part_scopes[p.table_refs[0]]
                )
                and not (eligibility.allowed and eligibility.group_key != p.source_group)
            ]
            previous = fitting[0] if fitting else None
            # An unmarked table in padding names neither scope nor group, so every nearby part
            # of its kind is a candidate, latest first.
            alternatives = fitting[1:] if scopes[n] is None and not eligibility.allowed else []
        layout = parse_header(
            grid,
            kind,
            hint,
            period_evidence=candidate.period_evidence,
            visual=grid.page_no in inputs.visual_pages,
        )
        candidate.decision.period_conflicts = layout.period_conflicts
        candidate.decision.evidence = list(
            dict.fromkeys(
                [
                    *candidate.decision.evidence,
                    *(
                        f
                        for f in layout.evidence
                        if candidate.period_evidence and f.startswith("period_")
                    ),
                ]
            )
        )
        if layout.period_conflicts and not layout.value_cols:
            continue
        continued = None
        ocr = inputs.page_modes.get(grid.page_no) is PageMode.IMAGE
        aligned: Realignment | None = None
        if previous is not None and (result.type is not None or _only_below_confidence(result)):
            # The latest candidate part that is open and lines up with the table keeps it; when
            # none does, the latest is probed alone, as it is when there is one candidate.
            if alternatives:
                previous = next(
                    (
                        p
                        for p in (previous, *alternatives)
                        if _probe(layout, grid, p, ocr=ocr).fits and not is_closed(p, index)
                    ),
                    previous,
                )
            inherited, aligned, fits = _probe(layout, grid, previous, ocr=ocr)
            if eligibility.allowed and eligibility.group_key == previous.source_group:
                layout = inherited
            if fits:
                if is_closed(previous, index):
                    candidate.decision.evidence.append(f"after_closed:{kind.value}")
                else:
                    continued = previous
                    layout = inherited
        if continued is not None:
            group_key = continued.source_group
            evidence = f"continuation_of:{kind.value}"
            candidate.decision.evidence = [
                e
                for e in candidate.decision.evidence
                if e != eligibility.reason or eligibility.allowed
            ]
            candidate.decision.evidence.append(evidence)
            result = result.model_copy(
                update={"type": kind, "evidence": [*result.evidence, evidence]}
            )
        else:
            if not eligibility.allowed or result.type is None:
                continue
            group_key = eligibility.group_key
        if not layout.value_cols:
            continue
        candidate.decision.type = kind
        candidate.decision.evidence.append(f"source_group:{group_key}")
        source = TextSource.OCR if ocr else TextSource.TEXT
        realigned = (
            aligned
            if continued is not None and aligned is not None
            else (_aligned(grid, layout, ocr=ocr))
        )
        part = build_part(
            realigned.grid,
            layout,
            result,
            source=source,
            index=index,
            merged_rows=realigned.merged_labels,
        )
        part.source_group = group_key
        part_scopes[part.table_refs[0]] = (
            scopes[n] if continued is None else part_scopes[continued.table_refs[0]]
        )
        part_order[part.table_refs[0]] = (
            0 if group_key and group_key.startswith("primary:") else 1,
            grid.page_no,
            _candidate_order(candidate)[2],
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

        scope = part_scopes[part.table_refs[0]]
        if scope is not None and group_key and group_key.startswith("scope:"):
            metas[part.table_refs[0]].consolidated = scope
    merged = sorted(
        merge_continuations(parts, index),
        key=lambda p: (p.type.value, *part_order[p.table_refs[0]]),
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
        results += check_eps(statement, eps_notes, part.header_text)
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
        s = confirm_arithmetic(s, own)
        statements[position] = s
        review = review_statement(s, own, primary=s.type not in seen)
        seen.add(s.type)
        if inputs.industry_hold is not None:
            s, review = hold_for_industry(s, review, inputs.industry_hold)
            statements[position] = s
        reviews.append(review)
        if review.status == "needs_review" and "needs_review" not in s.flags:
            statements[position] = s.model_copy(update={"flags": [*s.flags, "needs_review"]})

    found = {s.type for s in statements}
    flags = [f"statement_not_extracted:{t.value}" for t in config.enabled_types if t not in found]
    if inputs.industry_hold is not None:
        flags.append(f"industry_uncertain:{inputs.industry_hold}")
    flags += range_conversion_flags(inputs.convert.ranges)
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


def _settings_hash(
    config: IngestConfig,
    convert: ConvertResult,
    located: LocateResult,
    ocr: OcrEngine | None = None,
) -> str:
    payload = {
        "source": {
            "sha256": located.document.sha256,
            "page_count": located.document.page_count,
            "ranges": sorted(
                (r.type.value, r.first_page, r.last_page, r.rank) for r in located.ranges
            ),
            "pages": sorted(
                (
                    p.page_no,
                    tuple(t.value for t in p.title_types),
                    tuple(t.value for t in p.cue_types),
                    sorted(p.negatives),
                    p.continuation,
                    sorted(p.title_hits),
                    sorted((t.value, p.is_candidate(t)) for t in config.enabled_types),
                )
                for p in located.pages
            ),
            "pad_pages": config.pad_pages,
        },
        "structure": STRUCTURE_VERSION,
        "convert": convert.settings_hash,
        "min_confidence": config.min_confidence,
        "enabled_types": sorted(t.value for t in config.enabled_types),
        # The content, not the version: an alias edit changes the mapping without a new version.
        "taxonomy": load_taxonomy().digest,
        "header_ocr_scale": config.header_ocr_scale,
        "ocr": ocr_key(config, ocr),
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
    return cached_convert(out_dir, digest)


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
    invalidate_convert(out_dir)
    converted = convert(pdf, config)
    missing = _missing_docling(out_dir, converted)
    if missing:
        raise IngestError("convert_failed", f"docling output missing: {', '.join(missing)}")
    return converted


def _declined(located: LocateResult, decision: IndustryDecision) -> StructureResult:
    """The end state of a bank or an insurer: the reason, and no statements."""
    return StructureResult(
        version=STRUCTURE_VERSION,
        sha256=located.document.sha256,
        convert_version="",  # convert never ran
        settings_hash="",
        industry=decision,
        flags=[f"declined:{decision.code}"],
    )


def structure_pdf(
    pdf: Path,
    config: IngestConfig,
    ocr: OcrEngine | None,
    *,
    use_cache: bool = True,
    convert: Callable[[Path, IngestConfig], ConvertResult] = _convert,
    conversion: ConvertResult | None = None,
) -> StructureResult:
    """``conversion`` is one the caller just made for this document: used as it is, so a
    partial or failed range is read and flagged rather than converted again (a worker job
    times convert as its own stage). Without it, the stored conversion is used when current
    and complete, else ``convert`` runs."""
    started = time.perf_counter()
    located = load_or_locate(pdf, config, ocr)
    out_dir = config.artifact_root / located.document.sha256
    decision = industry_decision(located.industry)
    if decision is not None and decision.outcome == "declined":
        # Decided here, before convert, the expensive stage.
        declined = _declined(located, decision).model_copy(
            update={"timings": {"structure": time.perf_counter() - started}}
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        _write(out_dir / "table_checks.json", "[]")  # statements.raw.json promises these
        _write(out_dir / "statements.raw.json", declined.model_dump_json(indent=2))
        return declined
    if conversion is None:
        converted = current_convert(pdf, config, ocr, located, convert)
    elif conversion.sha256 != located.document.sha256:
        msg = f"conversion of {conversion.sha256} given for {located.document.sha256}"
        raise ValueError(msg)
    else:
        converted = conversion

    digest = _settings_hash(config, converted, located, ocr)
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
            and not any(
                # A missing engine or a failed read can succeed next time. Every other
                # unavailable comparison is a fact of the page and is reused as cached.
                e.startswith(("header_recovery_failed:", "header_recovery_unavailable"))
                for table in cached.tables
                for e in table.evidence
            )
        ):
            return cached

    pages = read_pages(pdf, config, ocr, cache_dir=out_dir)
    kind = located.industry.kind
    inputs = StructureInputs(
        sha256=located.document.sha256,
        language=located.document.language,
        industry_flags=(f"likely_{kind}",) if kind in _FINANCIAL else (),
        industry_hold=decision.code if decision is not None else None,
        page_modes={p.page_no: p.mode for p in pages},
        visual_pages={p.page_no for p in pages if p.visual_arabic},
        page_texts={p.page_no: repair_text(p.text, visual=False) for p in pages},
        located=located,
        documents=[
            (r.docling_path, load_docling_json(out_dir / r.docling_path))
            for r in converted.ranges
            if r.docling_path
        ],
        convert=converted,
        domicile_texts={p.page_no: reading_variants(p.text, p.visual_arabic) for p in pages},
    )
    result, checks = structure_document(
        inputs,
        config,
        header_recover=lambda grid, document: recover_header(grid, document, pdf, config, ocr),
    )
    result = result.model_copy(
        update={
            "settings_hash": digest,
            "industry": decision,
            "timings": {"structure": time.perf_counter() - started},
        }
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    # Checks first: a present statements.raw.json promises the checks beside it are current.
    _write(
        out_dir / "table_checks.json",
        json.dumps([c.model_dump(mode="json") for c in checks], indent=2, ensure_ascii=False),
    )
    _write(target, result.model_dump_json(indent=2))
    return result
