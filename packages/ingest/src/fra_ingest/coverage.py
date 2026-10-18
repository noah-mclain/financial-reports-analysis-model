"""Which figures no passing check vouches for, and how much of a statement they weigh.

``review_statement`` counts the figures a passing check covers; this lists the rest, so a check
can be chosen for the items that are left, from what the documents show.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, TypeAdapter

from fra_core.schemas import CheckResult, Statement, StatementType
from fra_ingest.config import IngestConfig
from fra_ingest.errors import IngestError
from fra_ingest.results import StructureResult
from fra_ingest.review import vouched_cells
from fra_ingest.review_report import resolve_artifacts


class UncheckedCell(BaseModel):
    model_config = ConfigDict(frozen=True)

    statement_id: str
    item_id: str
    label: str
    canonical_id: str | None
    period_key: str
    reported: Decimal
    weight: Decimal  # |reported| over the largest |reported| of the statement in that period


class Tally(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    statements: int
    cells: int
    mean_weight: Decimal


def unchecked_cells(statement: Statement, checks: Sequence[CheckResult]) -> list[UncheckedCell]:
    """The numeric cells of a statement that no passing check covers, in statement order."""
    vouched = vouched_cells(checks)
    largest: dict[str, Decimal] = defaultdict(Decimal)
    for item in statement.line_items:
        for cell in item.cells:
            if cell.reported is not None:
                largest[cell.period_key] = max(largest[cell.period_key], abs(cell.reported))
    return [
        UncheckedCell(
            statement_id=statement.id,
            item_id=item.id,
            label=item.raw_label,
            canonical_id=item.canonical_id,
            period_key=cell.period_key,
            reported=cell.reported,
            weight=abs(cell.reported) / largest[cell.period_key]
            if largest[cell.period_key]
            else Decimal(0),
        )
        for item in statement.line_items
        for cell in item.cells
        if cell.reported is not None and (item.id, cell.period_key) not in vouched
    ]


def tally(
    groups: Iterable[Sequence[UncheckedCell]], key: Callable[[UncheckedCell], str]
) -> list[Tally]:
    """Cells grouped by ``key`` across statements, heaviest mean weight first."""
    cells: dict[str, list[UncheckedCell]] = defaultdict(list)
    seen: dict[str, set[int]] = defaultdict(set)
    for number, group in enumerate(groups):
        for cell in group:
            cells[key(cell)].append(cell)
            seen[key(cell)].add(number)
    rows = [
        Tally(
            key=k,
            statements=len(seen[k]),
            cells=len(found),
            mean_weight=sum((c.weight for c in found), Decimal(0)) / len(found),
        )
        for k, found in cells.items()
    ]
    return sorted(rows, key=lambda r: (-r.mean_weight, r.key))


def document_unchecked(sha256: str, config: IngestConfig) -> dict[str, list[UncheckedCell]]:
    """Per statement id of a structured document, from its artifacts."""
    out = resolve_artifacts(config.artifact_root, sha256)
    for name in ("statements.raw.json", "table_checks.json"):
        if not (out / name).is_file():
            raise IngestError("artifact_missing", name)
    result = StructureResult.model_validate_json((out / "statements.raw.json").read_text("utf-8"))
    checks = TypeAdapter(list[CheckResult]).validate_json(
        (out / "table_checks.json").read_text("utf-8")
    )
    ties = [c for c in checks if c.kind == "net_profit_tie"]
    first_income = next((s.id for s in result.statements if s.type is StatementType.INCOME), None)
    return {
        s.id: unchecked_cells(
            s,
            [c for c in checks if c.statement_id == s.id]
            # Recorded on comprehensive income; they vouch for the first income statement too.
            + (ties if s.id == first_income else []),
        )
        for s in result.statements
    }
