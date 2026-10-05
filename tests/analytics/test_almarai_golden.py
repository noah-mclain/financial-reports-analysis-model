"""Analytics on the golden Almarai statements (week 2, Task 5).

This is the one place the analytics package meets ingest: the package itself depends on
``fra_core`` only, so the test that needs extracted statements sits with the other cross-package
tests. Both editions print in thousands of SAR; the inputs are what label mapping resolved, so a
null here names an item mapping did not place, not a calculation error.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path

import pytest

from fra_analytics.frame import primary_statements, to_frame
from fra_analytics.identities import check_identities
from fra_analytics.metrics.registry import compute
from fra_analytics.policy import load_policy
from fra_core.schemas import CheckResult, MetricValue, Statement
from fra_ingest.config import load_config
from fra_ingest.structure import structure_pdf

pytestmark = pytest.mark.golden

REPO_ROOT = Path(__file__).resolve().parents[2]
ENGLISH = "almarai-2025-en-annualreport.pdf"
ARABIC = "almarai-2025-ar-annualreport.pdf"
POLICY = load_policy(REPO_ROOT / "configs" / "analytics.toml")
# Metrics both editions' mapped inputs support, a floor and not the whole list: the others are
# null where mapping flags the row, and say which.
COMPUTED_IN_BOTH = [
    ("gross_margin", "FY2025"),
    ("operating_margin", "FY2025"),
    ("net_margin", "FY2025"),
    ("roa", "FY2025"),
    ("asset_turnover", "FY2025"),
    ("revenue_growth", "FY2025"),
    ("net_income_growth", "FY2025"),
]

Metrics = Mapping[tuple[str, str], MetricValue]


def load_edition(name: str) -> tuple[list[Statement], list[CheckResult]]:
    """The primary statement of each type (the package's rule) and the checks the structure
    stage wrote for the document."""
    pdf = REPO_ROOT / "eval" / "golden" / "documents" / name
    if not pdf.exists():
        pytest.skip(f"golden document {name} is not present")
    config = load_config()
    out_dir = config.artifact_root / hashlib.sha256(pdf.read_bytes()).hexdigest()
    if not (out_dir / "convert.json").exists():
        pytest.skip("run make eval-convert first")
    result = structure_pdf(pdf, config, None, use_cache=False)
    checks = [
        CheckResult.model_validate(c)
        for c in json.loads((out_dir / "table_checks.json").read_text("utf-8"))
    ]
    return primary_statements(result.statements), checks


@pytest.fixture(scope="module")
def english() -> tuple[list[Statement], list[CheckResult]]:
    return load_edition(ENGLISH)


@pytest.fixture(scope="module")
def arabic() -> tuple[list[Statement], list[CheckResult]]:
    return load_edition(ARABIC)


def metrics_of(statements: list[Statement]) -> Metrics:
    return {(m.metric_id, m.period_key): m for m in compute(to_frame(statements), POLICY)}


@pytest.mark.parametrize("edition", ["english", "arabic"])
def test_the_core_metrics_compute_on_each_edition(
    edition: str, request: pytest.FixtureRequest
) -> None:
    statements, _ = request.getfixturevalue(edition)
    found = metrics_of(statements)
    for key in COMPUTED_IN_BOTH:
        assert found[key].value is not None, f"{edition} {key}: {found[key].flags}"
    margin = found[("net_margin", "FY2025")].value
    assert margin is not None
    assert 0.0 < margin < 1.0  # a profitable issuer


@pytest.mark.parametrize("edition", ["english", "arabic"])
def test_every_value_traces_to_printed_cells_and_recomputes_from_them(
    edition: str, request: pytest.FixtureRequest
) -> None:
    statements, _ = request.getfixturevalue(edition)
    cells = {
        (s.id, i.id, c.period_key): c for s in statements for i in s.line_items for c in i.cells
    }
    found = metrics_of(statements)
    for key, metric in found.items():
        if metric.value is None:
            assert metric.flags, f"{edition} {key} is null without a flag"
            continue
        assert metric.inputs, f"{edition} {key} has a value and no inputs"
        for used in (c for role in metric.inputs.values() for c in role):
            cell = cells[(used.statement_id, used.line_item_id, used.period_key)]
            assert used.provenance == cell.provenance
            assert used.reported == cell.reported
            assert used.scale == 1000
    # An independent recomputation of three margins from the cells they cite.
    for name, top in [
        ("gross_margin", "gross_profit"),
        ("operating_margin", "operating_income"),
        ("net_margin", "net_income"),
    ]:
        metric = found[(name, "FY2025")]
        numerator = metric.inputs[top][0].reported
        denominator = metric.inputs["revenue"][0].reported
        assert metric.value == pytest.approx(float(numerator / denominator), rel=1e-12)
        assert denominator > Decimal(0)


def test_english_and_arabic_agree_wherever_both_compute(
    english: tuple[list[Statement], list[CheckResult]],
    arabic: tuple[list[Statement], list[CheckResult]],
) -> None:
    en, ar = metrics_of(english[0]), metrics_of(arabic[0])
    assert en.keys() == ar.keys()
    both = [k for k in en if en[k].value is not None and ar[k].value is not None]
    assert set(COMPUTED_IN_BOTH) <= set(both)
    for key in both:
        assert en[key].value == pytest.approx(ar[key].value, rel=1e-12), key
    # A value one edition lacks is null because an input is missing there, never for another
    # reason.
    for key in set(en) - set(both):
        for side in (en[key], ar[key]):
            if side.value is None:
                assert side.flags[0].startswith("missing_input:"), (key, side.flags)


@pytest.mark.parametrize("edition", ["english", "arabic"])
def test_the_identity_results_agree_with_the_structure_stage(
    edition: str, request: pytest.FixtureRequest
) -> None:
    statements, checks = request.getfixturevalue(edition)
    ours = {c.id: c for c in check_identities(to_frame(statements)) if c.kind == "balance_identity"}
    theirs = {c.id: c for c in checks if c.kind == "balance_identity"}
    assert ours, "the balance sheet was not checked"
    for check_id, ours_one in ours.items():
        assert theirs[check_id].status == ours_one.status, check_id
        assert theirs[check_id].difference == ours_one.difference, check_id
        assert theirs[check_id].tolerance == ours_one.tolerance, check_id
        assert ours_one.status == "pass"
    # No subtotal tie fails on statements the review stage passed.
    assert not [c for c in check_identities(to_frame(statements)) if c.status == "fail"]
