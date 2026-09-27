"""SEC label extraction: filer-level split, blind exclusion, statement and form filters."""

import datetime as dt
import zipfile
from pathlib import Path

import pytest

from sec_fsds import extract, pool_for, quarters_back, role_for

SUB = ["adsh", "cik", "name", "sic", "form", "period", "fy", "fp"]
PRE = ["adsh", "report", "line", "stmt", "inpth", "rfile", "tag", "version", "plabel", "negating"]


def make_zip(path: Path, subs: list[list[str]], pres: list[list[str]]) -> Path:
    def tsv(header: list[str], rows: list[list[str]]) -> str:
        return "\n".join("\t".join(r) for r in [header, *rows]) + "\n"

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("sub.txt", tsv(SUB, subs))
        archive.writestr("pre.txt", tsv(PRE, pres))
    return path


def pre(adsh: str, stmt: str, tag: str, label: str, line: str = "1") -> list[str]:
    return [adsh, "2", line, stmt, "0", "H", tag, "us-gaap/2024", label, "0"]


@pytest.fixture
def quarter(tmp_path: Path) -> Path:
    return make_zip(
        tmp_path / "2025q1.zip",
        subs=[
            ["a1", "40704", "GENERAL MILLS INC", "2040", "10-K", "20250531", "2025", "FY"],
            ["a2", "1111", "ACME WIDGETS", "3560", "10-K", "20241231", "2024", "FY"],
            ["a3", "2222", "FIRST BANK", "6022", "10-Q", "20250331", "2025", "Q1"],
            ["a4", "3333", "SOME FUND", "", "N-CSR", "20250331", "2025", "Q1"],
        ],
        pres=[
            pre("a1", "BS", "Assets", "Total assets"),
            pre("a2", "BS", "Assets", "Total assets"),
            pre("a2", "BS", "Assets", "Total  assets", line="9"),  # same label after spacing
            pre("a2", "IS", "Revenues", "Net sales"),
            pre("a2", "IS", "Revenues", ""),  # empty label
            pre("a2", "DEI", "EntityRegistrantName", "Entity name"),  # not a statement
            pre("a3", "BS", "Deposits", "Deposits"),
            pre("a4", "BS", "Assets", "Total assets"),  # form not kept
        ],
    )


def test_blind_filer_is_dropped_and_others_kept(quarter: Path) -> None:
    rows, stats = extract(quarter, pinned={40704: "blind"})
    assert {r["cik"] for r in rows} == {1111, 2222}
    assert stats["filings_blind_dropped"] == 1


def test_labels_are_distinct_per_filer_statement_and_tag(quarter: Path) -> None:
    rows, _ = extract(quarter, pinned={})
    acme = sorted((r["stmt"], r["plabel"]) for r in rows if r["cik"] == 1111)
    assert acme == [("BS", "Total assets"), ("IS", "Net sales")]


def test_bank_is_kept_as_negative_control(quarter: Path) -> None:
    rows, _ = extract(quarter, pinned={})
    assert {r["role"] for r in rows if r["cik"] == 2222} == {"negative_control"}
    assert {r["role"] for r in rows if r["cik"] == 1111} == {"corporate"}


def test_pinned_pool_wins_and_hash_is_stable() -> None:
    assert pool_for(40704, {40704: "model_test"}) == "model_test"
    assert pool_for(1111, {}) == pool_for(1111, {})
    assert {pool_for(c, {}) for c in range(1, 400)} == {"train", "model_test"}


@pytest.mark.parametrize(
    ("sic", "role"),
    [
        ("6022", "negative_control"),
        ("6311", "negative_control"),
        ("2040", "corporate"),
        ("", "corporate"),
    ],
)
def test_role_by_sic(sic: str, role: str) -> None:
    assert role_for(sic) == role


def test_quarters_back_skips_the_current_quarter() -> None:
    assert quarters_back(3, dt.date(2026, 9, 26)) == ["2025q4", "2026q1", "2026q2"]
    assert quarters_back(1, dt.date(2026, 1, 5)) == ["2025q4"]
