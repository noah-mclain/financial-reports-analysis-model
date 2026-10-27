"""Evidence-gated categorical decisions for shadow callers; no production routing.

Check results are trusted deterministic-upstream inputs, never model-supplied proof.
Only scoped subtotal/balance checks reach the existing mapper. No arithmetic is repeated.
"""

from __future__ import annotations

from collections.abc import Sequence

from fra_core.schemas import Cell, CheckResult, Statement
from fra_core.schemas.normalization import (
    NormalizationAudit,
    NormalizationDecision,
    NormalizationObservation,
    NormalizationSourceRow,
    ValueRowReference,
    parse_normalization_answer,
)
from fra_core.schemas.review import is_provenance_flag
from fra_core.taxonomy import Taxonomy
from fra_ingest.label_mapping import MAPPED_TYPES, map_statement
from fra_ingest.label_match import LabelIndex
from fra_ingest.normalization_context import build_normalization_request, validate_row_graph
from fra_ingest.review import is_critical


def _has_source_cell_uncertainty(cell: Cell) -> bool:
    """Whether a cell carries a critical or unrecognized non-provenance flag."""
    return is_critical(cell) or any(
        flag != "per_share" and not is_provenance_flag(flag) for flag in cell.flags
    )


def _source(statement: Statement, target_id: str) -> Statement:
    # Drop all incoming categorical conclusions before validation and recomputation.
    raw = statement.model_dump()
    raw["mapping_findings"] = ()
    raw["line_items"] = [
        {
            **item.model_dump(),
            "canonical_id": None,
            "mapping_source": None,
            "mapping_confidence": None,
            "mapping_flag": None,
            "mapping_evidence": None,
        }
        for item in statement.line_items
    ]
    source = Statement.model_validate(raw, strict=True)
    if not source.id.strip() or any(not r.id.strip() for r in source.line_items):
        raise ValueError("source statement and row IDs must not be blank")
    validate_row_graph(source.line_items, target_id)
    return source


def _anchor_checks(statement: Statement, checks: Sequence[CheckResult]) -> tuple[CheckResult, ...]:
    rows = {r.id: r for r in statement.line_items}
    periods = {p.key for p in statement.periods}
    anchors: list[CheckResult] = []
    for supplied in checks:
        # EPS and cross-statement ties may legitimately reference external note rows.
        # Neither can anchor a categorical mapping, irrespective of its numeric outcome.
        if supplied.kind not in ("subtotal", "balance_identity"):
            continue
        check = CheckResult.model_validate(supplied.model_dump(), strict=True)
        if check.statement_id != statement.id or check.period_key not in periods:
            raise ValueError(f"check {check.id!r}: statement/period scope mismatch")
        if not check.id.strip() or not check.line_item_ids:
            raise ValueError(f"check {check.id!r}: missing anchor identity/rows")
        if len(check.line_item_ids) != len(set(check.line_item_ids)):
            raise ValueError(f"check {check.id!r}: duplicate anchor rows")
        for row_id in check.line_item_ids:
            if row_id not in rows or not rows[row_id].cells:
                raise ValueError(f"check {check.id!r}: nonexistent source value row {row_id!r}")
            if not any(c.period_key == check.period_key for c in rows[row_id].cells):
                raise ValueError(f"check {check.id!r}: source row {row_id!r} lacks its period")
        anchors.append(check)
    if len({c.id for c in anchors}) != len(anchors):
        raise ValueError("duplicate anchor check IDs")
    return tuple(anchors)


def baseline_decision(
    statement: Statement, target_id: str, index: LabelIndex, checks: Sequence[CheckResult]
) -> NormalizationDecision:
    """Recompute a categorical baseline; unknown/uncertain source facts fail closed.

    All statement flags and unknown cell flags conservatively hold this shadow gate.
    The exact per_share cell annotation is unit metadata, not numerical uncertainty;
    benign provenance notes likewise do not establish numerical truth.
    """
    source = _source(statement, target_id)
    request = build_normalization_request(source, target_id)
    anchors = _anchor_checks(source, checks)
    target = next(r for r in source.line_items if r.id == target_id)
    observations: list[NormalizationObservation] = []
    for n, item in enumerate(source.line_items):
        refs = sorted(
            {(c.provenance.page_no, c.provenance.table_ref, c.provenance.row) for c in item.cells}
        )
        observations.append(
            NormalizationObservation(
                source_row=NormalizationSourceRow(
                    role="target" if item.id == target_id else "observed",
                    line_item_id=item.id,
                    source_order=n,
                    value_rows=tuple(
                        ValueRowReference(page_no=p, table_ref=t, row=r) for p, t, r in refs
                    ),
                ),
                printed_label=item.raw_label,
                inferred_parent_id=item.parent_id,
            )
        )
    audit = NormalizationAudit(
        document_sha256=source.document_sha256,
        statement_id=source.id,
        rows=tuple(o.source_row for o in observations),
    )
    reason: str | None = None
    if source.type not in MAPPED_TYPES:
        reason = "statement_type_unsupported"
    elif not target.cells or not any(c.reported is not None for c in target.cells):
        reason = "target_value_missing"
    elif source.flags or source.period_conflicts:
        reason = "source_uncertainty"
    elif any(_has_source_cell_uncertainty(c) for r in source.line_items for c in r.cells):
        reason = "source_cell_uncertainty"
    elif request.payload.context.status == "conflicting":
        reason = "source_relationship_conflict"
    elif any(c.status == "skipped" for c in anchors):
        reason = "anchor_scope_unsettled"
    mapped = None
    if reason is None:
        mapped = next(
            r for r in map_statement(source, index, anchors).line_items if r.id == target_id
        )
        if mapped.mapping_flag is not None or mapped.canonical_id is None:
            reason = mapped.mapping_evidence or "no_supported_mapping"
    if reason is None:
        needed: tuple[str, ...] = ()
    elif reason == "target_value_missing":
        needed = ("Retain a reported value for the target source row.",)
    elif reason == "statement_type_unsupported":
        needed = ("A deterministic mapping rule for this statement type is not available.",)
    elif reason == "source_uncertainty":
        needed = tuple(f"Resolve statement flag: {f}" for f in source.flags)
        if source.period_conflicts:
            needed += ("Resolve conflicting printed period headers.",)
    elif reason == "source_cell_uncertainty":
        needed = tuple(
            f"Resolve source cell {r.id}:{c.period_key} flags: {','.join(c.flags)}"
            for r in source.line_items
            for c in r.cells
            if _has_source_cell_uncertainty(c)
        )
    elif reason == "source_relationship_conflict":
        needed = ("Retain a governing printed section and reconcile inferred parent links.",)
    elif reason == "anchor_scope_unsettled":
        needed = tuple(
            f"Resolve anchor scope for {c.id}: {c.detail or 'skipped check'}"
            for c in anchors
            if c.status == "skipped"
        )
    else:
        needed = (
            "Retain an unambiguous printed caption or governing section with scoped, "
            f"unfailed deterministic anchors; unresolved rule: {reason}",
        )
    return NormalizationDecision(
        state="supported" if reason is None else "unresolved",
        canonical_id=mapped.canonical_id if reason is None and mapped is not None else None,
        reason="unique_rule_mapping" if reason is None else reason,
        needed_evidence=needed,
        mapper_evidence=mapped.mapping_evidence if mapped is not None else None,
        audit=audit,
        observations=tuple(observations),
        supplied_anchor_check_ids=tuple(c.id for c in anchors),
    )


def assess_proposal(
    statement: Statement,
    target_id: str,
    taxonomy: Taxonomy,
    checks: Sequence[CheckResult],
    response: str,
) -> NormalizationDecision:
    """Accept a proposal only when it agrees with independently supported rule evidence.

    A semantic null is a model abstention, not evidence of taxonomy absence. Invalid JSON
    or catalog output never becomes semantic null. This function makes no model calls.
    """
    baseline = baseline_decision(statement, target_id, LabelIndex(taxonomy), checks)
    status, proposal = parse_normalization_answer(response, statement.type, taxonomy)
    if status == "invalid":
        state, reason = "invalid", "invalid_model_response"
    elif proposal is None:
        state, reason = "model_abstention", "explicit_model_abstention"
    elif baseline.state != "supported" or proposal == baseline.canonical_id:
        return baseline
    else:
        state, reason = "rejected", "proposal_disagrees"
    # Revalidate instead of model_copy so decision invariants remain enforced.
    return NormalizationDecision.model_validate(
        {**baseline.model_dump(), "state": state, "reason": reason, "canonical_id": None}
    )
