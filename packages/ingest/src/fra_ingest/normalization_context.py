"""Conservative PDF Statement adapter for shadow use; no production call sites.

Only existing parent links establish relationships. A preceding heading without a link
does not become a governing heading. No taxonomy, mapping answers or check operands are
read, and no label coordinates are inferred from amount cells.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

from fra_core.schemas import LineItem, Statement
from fra_core.schemas.normalization import (
    MAX_RELATED_LABELS,
    NormalizationAudit,
    NormalizationContext,
    NormalizationPayload,
    NormalizationRequest,
    NormalizationSourceRow,
    ValueRowReference,
)


def _positions(items: Sequence[LineItem], target_id: str) -> dict[str, int]:
    positions: dict[str, int] = {}
    for n, item in enumerate(items):
        if item.id in positions:
            raise ValueError(f"duplicate line item ID {item.id!r}")
        positions[item.id] = n
    if target_id not in positions:
        raise ValueError(f"missing target {target_id!r}")
    for item in items:
        if item.parent_id is not None and item.parent_id not in positions:
            raise ValueError(f"line item {item.id!r} references missing parent {item.parent_id!r}")
    # Each node is visited once; iterative traversal also validates unrelated components.
    settled: set[str] = set()
    for item in items:
        path: set[str] = set()
        current: str | None = item.id
        while current is not None and current not in settled:
            if current in path:
                raise ValueError(f"parent cycle at line item {current!r}")
            path.add(current)
            current = items[positions[current]].parent_id
        settled.update(path)
    return positions


def _is_heading(item: LineItem) -> bool:
    return bool(item.raw_label.strip()) and not item.cells


def _context_rows(
    items: Sequence[LineItem], positions: Mapping[str, int], target: int
) -> tuple[NormalizationContext, int | None, int | None, list[int]]:
    # Headings bound the source run; they do not themselves prove a relationship.
    start = next((n for n in range(target - 1, -1, -1) if _is_heading(items[n])), -1)
    stop = next((n for n in range(target + 1, len(items)) if _is_heading(items[n])), len(items))
    ancestors: list[int] = []
    parent_id = items[target].parent_id
    while parent_id is not None:
        n = positions[parent_id]
        ancestors.append(n)
        parent_id = items[n].parent_id

    heading: int | None = None
    parent: int | None = None
    for n in ancestors:
        item = items[n]
        if _is_heading(item):
            # Older outer headings cannot establish the nearest section. Once an
            # applicable heading is found its outer ancestry supplies no extra context.
            if n != start:
                return NormalizationContext(status="conflicting"), None, None, []
            heading = n
            break
        if not start < n < stop:
            return NormalizationContext(status="conflicting"), None, None, []
        if parent is None and item.is_subtotal and item.raw_label.strip():
            parent = n

    if heading is None and parent is None:
        return NormalizationContext(), None, None, []

    peers = [
        n
        for n in range(start + 1, stop)
        if n != target
        and items[n].parent_id == items[target].parent_id
        and items[n].cells
        and not items[n].is_subtotal
        and items[n].raw_label.strip()
    ]
    # Select the nearest bounded peers, then restore source order. Distance never
    # establishes a parent, heading, section, or categorical meaning.
    related = sorted(sorted(peers, key=lambda n: (abs(n - target), n))[:MAX_RELATED_LABELS])
    context = NormalizationContext(
        status="inferred",
        heading=items[heading].raw_label if heading is not None else None,
        parent=items[parent].raw_label if parent is not None else None,
        related=tuple(items[n].raw_label for n in related),
    )
    return context, heading, parent, related


def build_normalization_source_row(
    item: LineItem,
    source_order: int,
    role: Literal["target", "heading", "parent", "related"],
) -> NormalizationSourceRow:
    """Keep label lineage separate from the value rows, including value-free headings."""
    refs = sorted(
        {(c.provenance.page_no, c.provenance.table_ref, c.provenance.row) for c in item.cells}
    )
    return NormalizationSourceRow(
        role=role,
        line_item_id=item.id,
        source_order=source_order,
        value_rows=tuple(ValueRowReference(page_no=p, table_ref=t, row=r) for p, t, r in refs),
        label_region=item.label_source if item.label_source is not None else "unavailable",
    )


def build_normalization_request(statement: Statement, target_id: str) -> NormalizationRequest:
    """Build an amount-free request without mutating the structured statement.

    All IDs and parent links are validated before selecting labels. Relationships remain
    inferred, including those to closing subtotals. A blank subtotal with no retained
    heading ancestor provides no printed context. Run-in headings are not reconstructed.
    """
    items = statement.line_items
    positions = _positions(items, target_id)
    target = positions[target_id]
    context, heading, parent, related = _context_rows(items, positions, target)

    def source_row(
        n: int, role: Literal["target", "heading", "parent", "related"]
    ) -> NormalizationSourceRow:
        return build_normalization_source_row(items[n], n, role)

    rows = [source_row(target, "target")]
    if heading is not None:
        rows.append(source_row(heading, "heading"))
    if parent is not None:
        rows.append(source_row(parent, "parent"))
    rows.extend(source_row(n, "related") for n in related)
    return NormalizationRequest(
        payload=NormalizationPayload(
            statement=statement.type, label=items[target].raw_label, context=context
        ),
        audit=NormalizationAudit(
            document_sha256=statement.document_sha256, statement_id=statement.id, rows=tuple(rows)
        ),
    )
