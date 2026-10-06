"""Almarai EN and AR through label mapping (week 2, Task 4)."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

import pytest
from cache_isolation import GoldenStructure

from fra_core.schemas import Statement, StatementType
from fra_core.taxonomy.loader import load_taxonomy
from fra_ingest.label_mapping import MAPPED_TYPES

pytestmark = pytest.mark.golden

ENGLISH = "almarai-2025-en-annualreport.pdf"
ARABIC = "almarai-2025-ar-annualreport.pdf"
CRITICAL = {kind: load_taxonomy().critical_ids(kind) for kind in MAPPED_TYPES}
# Critical items both Almarai editions map, a floor and not the whole list: the others are
# flagged where no check confirms them, which `make eval-mapping` counts.
MAPPED_IN_BOTH = {
    StatementType.INCOME: ("revenue", "net_income"),
    StatementType.BALANCE: ("total_assets", "total_equity"),
}


def mapped_statements(
    golden_structure: Callable[[str], GoldenStructure], name: str
) -> dict[StatementType, Statement]:
    found: dict[StatementType, Statement] = {}
    for s in golden_structure(name).result.statements:
        found.setdefault(s.type, s)
    return found


def by_figures(statement: Statement) -> dict[tuple[tuple[str, Decimal], ...], set[str]]:
    """The canonical ids of the mapped rows, keyed by the figures they print."""
    rows: dict[tuple[tuple[str, Decimal], ...], set[str]] = {}
    for item in statement.line_items:
        if item.canonical_id is None:
            continue
        key = tuple(
            sorted((c.period_key, c.reported) for c in item.cells if c.reported is not None)
        )
        rows.setdefault(key, set()).add(item.canonical_id)
    return rows


@pytest.mark.parametrize("statement_type", MAPPED_TYPES)
def test_almarai_critical_items_map_once_or_not_at_all(
    golden_structure: Callable[[str], GoldenStructure], statement_type: StatementType
) -> None:
    for name in (ENGLISH, ARABIC):
        statement = mapped_statements(golden_structure, name)[statement_type]
        for item_id in CRITICAL[statement_type]:
            rows = [i for i in statement.line_items if i.canonical_id == item_id]
            assert len(rows) <= 1, f"{name} {item_id}: {len(rows)} rows mapped"
            if item_id in MAPPED_IN_BOTH[statement_type]:
                assert len(rows) == 1, f"{name} {item_id} is not mapped"


@pytest.mark.parametrize("statement_type", MAPPED_TYPES)
def test_almarai_english_and_arabic_rows_map_to_the_same_items(
    golden_structure: Callable[[str], GoldenStructure], statement_type: StatementType
) -> None:
    english = by_figures(mapped_statements(golden_structure, ENGLISH)[statement_type])
    arabic = by_figures(mapped_statements(golden_structure, ARABIC)[statement_type])
    shared = english.keys() & arabic.keys()
    assert shared, "no row mapped in both languages"
    for key in shared:
        assert english[key] == arabic[key], f"figures {key}: {english[key]} against {arabic[key]}"
