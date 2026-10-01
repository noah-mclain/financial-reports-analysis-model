"""Almarai EN and AR through structure (spec 11, Done when)."""

from __future__ import annotations

import json
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


def statements(golden: Callable[[str], Path], name: str) -> dict[StatementType, Statement]:
    pdf = golden(name)
    if not (artifact_dir(pdf) / "convert.json").exists():
        pytest.skip("run make eval-convert first")
    config = load_config()
    result = structure_pdf(pdf, config, None, use_cache=False)
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


def test_almarai_en_balance_sheet(golden: Callable[[str], Path]) -> None:
    found = statements(golden, "almarai-2025-en-annualreport.pdf")
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
    golden: Callable[[str], Path], statement_type: StatementType
) -> None:
    english = statements(golden, "almarai-2025-en-annualreport.pdf")[statement_type]
    arabic = statements(golden, "almarai-2025-ar-annualreport.pdf")[statement_type]
    en_rows, ar_rows = value_rows(english), value_rows(arabic)
    assert en_rows - ar_rows == Counter(), (
        f"English rows without an Arabic counterpart: {en_rows - ar_rows}"
    )
    assert ar_rows - en_rows == Counter(), (
        f"Arabic rows without an English counterpart: {ar_rows - en_rows}"
    )


def test_almarai_balance_sheet_totals_are_all_checked(golden: Callable[[str], Path]) -> None:
    for name in ("almarai-2025-en-annualreport.pdf", "almarai-2025-ar-annualreport.pdf"):
        pdf = golden(name)
        statements(golden, name)
        checks = json.loads((artifact_dir(pdf) / "table_checks.json").read_text(encoding="utf-8"))
        balance = [c for c in checks if "-balance-1:" in c["id"] and c["kind"] == "subtotal"]
        assert balance and {c["status"] for c in balance} == {"pass"}


def test_edita_ifrs_total_equity_passes(golden: Callable[[str], Path]) -> None:
    pdf = golden("edita-2025-en-consolidated-ifrs.pdf")
    balance = statements(golden, "edita-2025-en-consolidated-ifrs.pdf")[StatementType.BALANCE]
    assert "subtotal_failed" not in balance.flags
    checks = json.loads((artifact_dir(pdf) / "table_checks.json").read_text(encoding="utf-8"))
    equity = [c for c in checks if c["id"].startswith(f"{balance.id}:subtotal:p8-t0-r26:")]
    assert [c["status"] for c in equity] == ["pass", "pass"]


def test_edita_2024_ar_equity_and_borrowings_sit_on_their_labels(
    golden: Callable[[str], Path],
) -> None:
    balance = statements(golden, "edita-2024-ar-consolidated-eas.pdf")[StatementType.BALANCE]
    rows = {i.id: i for i in balance.line_items}
    assert "rows_realigned" in balance.flags
    assert rows["p5-t0-r22"].value_for("2024-12-31") == Decimal("4157569146")
    assert rows["p5-t0-r21"].value_for("2024-12-31") == Decimal("102084427")
    assert not rows["p5-t0-r23"].cells
    assert rows["p5-t0-r24"].value_for("2024-12-31") == Decimal("2282057066")
    assert rows["p5-t0-r25"].value_for("2024-12-31") == Decimal("19343101")
    moved = next(c for c in rows["p5-t0-r22"].cells if c.period_key == "2024-12-31")
    assert moved.provenance.row == 21 and "row_realigned" in moved.flags


def test_edita_2024_ar_total_assets_is_the_named_suspect(golden: Callable[[str], Path]) -> None:
    pdf = golden("edita-2024-ar-consolidated-eas.pdf")
    balance = statements(golden, "edita-2024-ar-consolidated-eas.pdf")[StatementType.BALANCE]
    total = next(i for i in balance.line_items if i.id == "p5-t0-r14")
    suspect = next(c for c in total.cells if c.period_key == "2023-12-31")
    assert suspect.reported == Decimal("7743342656") and "digit_suspect" in suspect.flags
    checks = json.loads((artifact_dir(pdf) / "table_checks.json").read_text(encoding="utf-8"))
    identity = next(
        c for c in checks if c["kind"] == "balance_identity" and c["period_key"] == "2023-12-31"
    )
    assert identity["status"] == "fail"
    assert identity["detail"].endswith("single_digit:10^0; suspect:p5-t0-r14=7743342651")


def test_almarai_net_profit_ties(golden: Callable[[str], Path]) -> None:
    pdf = golden("almarai-2025-en-annualreport.pdf")
    statements(golden, "almarai-2025-en-annualreport.pdf")
    checks = json.loads((artifact_dir(pdf) / "table_checks.json").read_text(encoding="utf-8"))
    ties = [c for c in checks if c["kind"] == "net_profit_tie"]
    assert [c["status"] for c in ties] == ["pass", "pass"]
