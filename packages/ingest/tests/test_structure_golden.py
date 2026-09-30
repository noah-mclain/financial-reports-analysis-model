"""Almarai EN and AR through structure (spec 11, Done when)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest
from support import artifact_dir

from fra_core.schemas import Statement, StatementType
from fra_ingest.config import load_config
from fra_ingest.structure import structure_pdf

pytestmark = pytest.mark.golden


def statements(
    golden: Callable[[str], Path], name: str, tmp_path: Path
) -> dict[StatementType, Statement]:
    pdf = golden(name)
    if not (artifact_dir(pdf) / "convert.json").exists():
        pytest.skip("run make eval-convert first")
    config = load_config()
    result = structure_pdf(pdf, config, None)
    found: dict[StatementType, Statement] = {}
    for s in result.statements:
        found.setdefault(s.type, s)
    return found


def value_rows(statement: Statement) -> Counter[tuple[tuple[str, Decimal], ...]]:
    rows: Counter[tuple[tuple[str, Decimal], ...]] = Counter()
    for item in statement.line_items:
        values = tuple(
            sorted(
                (c.period_key, c.reported * statement.scale)
                for c in item.cells
                if c.reported is not None
            )
        )
        if values:
            rows[values] += 1
    return rows


def test_almarai_en_balance_sheet(golden: Callable[[str], Path], tmp_path: Path) -> None:
    found = statements(golden, "almarai-2025-en-annualreport.pdf", tmp_path)
    balance = found[StatementType.BALANCE]
    assert balance.source_pages == [156, 157, 158]
    assert (balance.currency, balance.scale) == ("SAR", 1000)
    assert [p.key for p in balance.periods] == ["2025-12-31", "2024-12-31"]
    ppe = next(i for i in balance.line_items if i.raw_label.startswith("Property, Plant"))
    assert ppe.value_for("2025-12-31") == Decimal("26058632")
    assert "identity_failed" not in balance.flags


@pytest.mark.parametrize(
    "statement_type",
    [StatementType.BALANCE, StatementType.INCOME, StatementType.COMPREHENSIVE_INCOME],
)
def test_almarai_english_and_arabic_figures_match(
    golden: Callable[[str], Path], tmp_path: Path, statement_type: StatementType
) -> None:
    english = statements(golden, "almarai-2025-en-annualreport.pdf", tmp_path)[statement_type]
    arabic = statements(golden, "almarai-2025-ar-annualreport.pdf", tmp_path)[statement_type]
    en_rows, ar_rows = value_rows(english), value_rows(arabic)
    assert en_rows - ar_rows == Counter(), (
        f"English rows without an Arabic counterpart: {en_rows - ar_rows}"
    )
    assert ar_rows - en_rows == Counter(), (
        f"Arabic rows without an English counterpart: {ar_rows - en_rows}"
    )
