"""A light hierarchy: depth from indentation, sections, subtotal cues and parents (spec 11).

Parents from sums, which settle the subtotals the cues miss, are set by
``table_checks.run_checks`` (spec 12).
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from fra_ingest.label_match import has_subtotal_cue

_INDENT_TOLERANCE = 4.0


class RowInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    row: int
    label: str
    indent: float | None
    has_values: bool
    subtotal_hint: bool = False


class RowNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    row: int
    depth: int
    parent_row: int | None
    is_subtotal: bool
    is_section: bool


def _levels(indents: Sequence[float]) -> list[float]:
    levels: list[float] = []
    for value in sorted(indents):
        if not levels or value - levels[-1] > _INDENT_TOLERANCE:
            levels.append(value)
    return levels


def _depth(indent: float | None, levels: Sequence[float]) -> int:
    if indent is None or not levels:
        return 0
    return min(range(len(levels)), key=lambda i: abs(indent - levels[i]))


def infer_hierarchy(rows: Sequence[RowInput]) -> list[RowNode]:
    levels = _levels([r.indent for r in rows if r.indent is not None])
    nodes: list[RowNode] = []
    sections: list[RowNode] = []
    for r in rows:
        depth = _depth(r.indent, levels)
        is_section = bool(r.label) and not r.has_values
        # A printed total without a label (a sum under a rule) is a subtotal too.
        is_subtotal = r.has_values and (
            r.subtotal_hint or not r.label.strip() or has_subtotal_cue(r.label)
        )
        if is_section:
            parent = next((s.row for s in reversed(sections) if s.depth < depth), None)
        else:
            parent = next((s.row for s in reversed(sections) if s.depth <= depth), None)
        node = RowNode(
            row=r.row,
            depth=depth,
            parent_row=parent,
            is_subtotal=is_subtotal,
            is_section=is_section,
        )
        nodes.append(node)
        if is_section:
            sections.append(node)
    return nodes
