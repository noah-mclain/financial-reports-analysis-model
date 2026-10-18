"""Roster reading and routing of SEC filings, on synthetic archives only."""

import zipfile
from collections import Counter
from pathlib import Path

import pytest
from dataset_contracts import DatasetError
from sec_source_preparation import (
    ANNUAL_FORMS,
    INTERIM_FORMS,
    SUB_REQUIRED,
    FilingRow,
    issuer_identity,
    read_archive_member,
    read_roster,
    registry_counts,
    route_filings,
)

from fra_core.pools import Identity, PoolRegistry
from fra_core.split import Look, Move, Part, SplitError, cik_key, hashed_part, issuer_key

HEADER = "\t".join(
    ["adsh", "cik", "name", "sic", "countryba", "form", "period", "fy", "fp", "nciks", "aciks"]
)


def cik_in(part: Part, skip: int = 0) -> int:
    found = [c for c in range(1, 2000) if hashed_part(cik_key(c)) == part]
    return found[skip]


def sub_line(
    adsh: str,
    cik: int,
    name: str = "Synthetic Widgets",
    *,
    form: str = "10-K",
    period: str = "20241231",
    aciks: str = "",
) -> str:
    nciks = "1" if not aciks else str(1 + len(aciks.split()))
    return "\t".join([adsh, str(cik), name, "3714", "US", form, period, "2024", "FY", nciks, aciks])


def write_archive(path: Path, lines: list[str], header: str = HEADER) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("sub.txt", "\n".join([header, *lines]) + "\n")
        archive.writestr("pre.txt", "MUST NEVER BE READ\n")
        archive.writestr("num.txt", "MUST NEVER BE READ\n")
    return path


def adsh(n: int) -> str:
    return f"{n:010d}-24-{n:06d}"


def registry(**pools: list[int]) -> PoolRegistry:
    reg = PoolRegistry()
    for pool, ciks in pools.items():
        for cik in ciks:
            reg.register(Identity(cik=cik), pool)
    return reg


def filing(n: int, cik: int, name: str = "Synthetic Widgets", **changes: object) -> FilingRow:
    base: dict[str, object] = {
        "adsh": adsh(n),
        "cik": cik,
        "name": name,
        "sic": "3714",
        "form": "10-K",
        "period": "20241231",
        "fy": "2024",
        "fp": "FY",
        "aciks": (),
    }
    return FilingRow.model_validate({**base, **changes})


def test_reads_the_documented_submission_columns(tmp_path: Path) -> None:
    archive = write_archive(
        tmp_path / "a.zip",
        [
            sub_line(adsh(1), 11, "Alpha Co", form="10-Q", period="20240630"),
            sub_line(adsh(2), 12, "Beta Ltd", aciks="13 14"),
        ],
    )
    rows = read_roster(archive)
    assert [(r.adsh, r.cik, r.name, r.form, r.period, r.aciks) for r in rows] == [
        (adsh(1), 11, "Alpha Co", "10-Q", "20240630", ()),
        (adsh(2), 12, "Beta Ltd", "10-K", "20241231", (13, 14)),
    ]
    assert {"adsh", "cik", "name", "sic", "form", "period", "fy", "fp", "aciks"} <= set(
        SUB_REQUIRED
    )


def test_an_empty_period_is_kept_as_none(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a.zip", [sub_line(adsh(1), 11, period="")])
    assert read_roster(archive)[0].period is None


def test_only_the_submission_member_can_be_read(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a.zip", [sub_line(adsh(1), 11)])
    for member in ("pre.txt", "num.txt", "tag.txt", "readme.htm"):
        with pytest.raises(DatasetError, match=r"sub\.txt"):
            read_archive_member(archive, member)


@pytest.mark.parametrize(
    ("lines", "header", "match"),
    [
        ([sub_line(adsh(1), 11) + "\textra"], HEADER, r"sub\.txt:2"),
        ([sub_line("bad", 11)], HEADER, r"sub\.txt:2.*adsh"),
        ([sub_line(adsh(1), 0)], HEADER, r"sub\.txt:2.*cik"),
        ([sub_line(adsh(1), 11, period="2024")], HEADER, r"sub\.txt:2.*period"),
        ([sub_line(adsh(1), 11), sub_line(adsh(1), 12)], HEADER, r"sub\.txt:3.*twice"),
        ([sub_line(adsh(1), 11, aciks="x")], HEADER, r"sub\.txt:2.*aciks"),
        ([], HEADER.replace("\tform", ""), r"sub\.txt:1.*form"),
        ([], HEADER.replace("adsh\tcik", "cik\tadsh", 1), r"sub\.txt:1.*adsh.*first"),
    ],
)
def test_malformed_submission_rows_are_named_by_member_and_line(
    tmp_path: Path, lines: list[str], header: str, match: str
) -> None:
    with pytest.raises(DatasetError, match=match):
        read_roster(write_archive(tmp_path / "a.zip", lines, header))


def test_forms_are_split_into_annual_and_interim() -> None:
    assert {"10-K", "10-K/A", "20-F", "40-F"} == ANNUAL_FORMS
    assert {"10-Q", "10-Q/A"} == INTERIM_FORMS


def test_routing_keeps_only_train_fit_and_validation_and_never_allocates() -> None:
    fit, valid, holdout = cik_in(Part.FIT), cik_in(Part.VALIDATION), cik_in(Part.HOLDOUT)
    other = cik_in(Part.FIT, 1)
    reg = registry(train=[fit, valid, holdout], model_test=[other + 1], blind=[other + 2])
    filings = [
        filing(1, fit, "Fit Co"),
        filing(2, valid, "Valid Co"),
        filing(3, holdout, "Holdout Co"),
        filing(4, other + 1, "Test Co"),
        filing(5, other + 2, "Blind Co"),
        filing(6, other + 3, "Unrecorded Co"),
        filing(7, fit, "Fit Co", form="6-K"),
    ]
    before = reg.metadata()
    result = route_filings(filings, reg, moves=[], looks=[])
    assert result.parts == {adsh(1): Part.FIT, adsh(2): Part.VALIDATION}
    assert result.dropped == Counter(
        {
            "holdout": 1,
            "pool_model_test": 1,
            "pool_blind": 1,
            "unrecorded_cik": 1,
            "unsupported_form": 1,
        }
    )
    assert reg.metadata() == before


def test_a_recorded_move_releases_a_holdout_issuer_to_fit() -> None:
    holdout = cik_in(Part.HOLDOUT)
    reg = registry(train=[holdout])
    moves = [
        Move(
            "Held Co",
            issuer_key("Held Co"),
            "override",
            "holdout",
            "fit",
            "2026-10-01",
            0,
            "synthetic review",
        )
    ]
    result = route_filings([filing(1, holdout, "Held Co")], reg, moves=moves, looks=[])
    assert result.parts == {adsh(1): Part.FIT}
    assert not result.dropped


def test_moves_and_looks_are_checked_before_routing() -> None:
    holdout = cik_in(Part.HOLDOUT)
    bad = Move("Held Co", "wrong", "override", "holdout", "fit", "2026-10-01", 0, "r")
    with pytest.raises(SplitError):
        route_filings([filing(1, holdout, "Held Co")], registry(train=[holdout]), [bad], [])
    look = Look("2026-10-01", "dry_run", ("holdout",), ("annual",), "c")
    assert route_filings([], registry(), [], [look]).parts == {}


def test_a_co_registrant_must_route_to_the_same_part() -> None:
    fit, fit2 = cik_in(Part.FIT), cik_in(Part.FIT, 1)
    valid, holdout = cik_in(Part.VALIDATION), cik_in(Part.HOLDOUT)
    reg = registry(train=[fit, fit2, valid, holdout])
    result = route_filings(
        [
            filing(1, fit, "Same Co", aciks=(fit2,)),
            filing(2, fit, "Mixed Co", aciks=(valid,)),
            filing(3, fit, "Held Co", aciks=(holdout,)),
            filing(4, fit, "Unknown Co", aciks=(holdout + 1000,)),
        ],
        reg,
        [],
        [],
    )
    assert result.parts == {adsh(1): Part.FIT}
    assert result.dropped == Counter({"coregistrant_not_routed": 3})


def test_filings_of_one_name_under_two_ciks_are_dropped_and_counted() -> None:
    a, b, c = cik_in(Part.FIT), cik_in(Part.FIT, 1), cik_in(Part.FIT, 2)
    reg = registry(train=[a, b, c])
    result = route_filings(
        [
            filing(1, a, "Same Name Inc"),
            filing(2, b, "SAME NAME, INC."),
            filing(3, a, "Same Name Inc", form="10-Q"),
            filing(4, c, "Other Name"),
        ],
        reg,
        [],
        [],
    )
    assert result.parts == {adsh(4): Part.FIT}
    assert result.dropped == Counter({"name_collision": 3})


def test_a_move_for_an_issuer_without_filings_here_does_not_apply() -> None:
    fit = cik_in(Part.FIT)
    move = Move(
        "Elsewhere Co",
        issuer_key("Elsewhere Co"),
        "override",
        "holdout",
        "fit",
        "2026-10-01",
        0,
        "review",
    )
    result = route_filings([filing(1, fit, "Fit Co")], registry(train=[fit]), [move], [])
    assert result.parts == {adsh(1): Part.FIT}


def test_a_moved_name_shared_by_two_ciks_is_dropped_not_released() -> None:
    a, b = cik_in(Part.HOLDOUT), cik_in(Part.HOLDOUT, 1)
    move = Move(
        "Held Co", issuer_key("Held Co"), "override", "holdout", "fit", "2026-10-01", 0, "review"
    )
    result = route_filings(
        [filing(1, a, "Held Co"), filing(2, b, "Held Co")], registry(train=[a, b]), [move], []
    )
    assert result.parts == {}
    assert result.dropped == Counter({"name_collision": 2})


def test_registry_counts_report_recorded_and_unrecorded_ciks_only_as_counts() -> None:
    fit, valid = cik_in(Part.FIT), cik_in(Part.VALIDATION)
    reg = registry(train=[fit, valid], blind=[fit + 5000])
    counts = registry_counts(
        [
            filing(1, fit),
            filing(2, fit),
            filing(3, valid),
            filing(4, fit + 5000),
            filing(5, fit + 6000),
            filing(6, fit + 6001, form="6-K"),
        ],
        reg,
    )
    assert counts.filings == 6
    assert counts.supported_forms == 5
    assert counts.ciks == 4  # supported-form filers only
    assert counts.recorded_by_pool == Counter({"train": 2, "blind": 1})
    assert counts.unrecorded == 1


def test_a_filing_without_a_period_is_dropped_and_counted() -> None:
    fit = cik_in(Part.FIT)
    result = route_filings(
        [filing(1, fit, "Fit Co", period=None), filing(2, fit, "Fit Co")],
        registry(train=[fit]),
        [],
        [],
    )
    assert result.parts == {adsh(2): Part.FIT}
    assert result.dropped == Counter({"missing_period": 1})


def test_names_without_a_normalized_form_do_not_collide() -> None:
    a, b = cik_in(Part.FIT), cik_in(Part.FIT, 1)
    result = route_filings(
        [filing(1, a, "Co Inc"), filing(2, b, "The Corp")], registry(train=[a, b]), [], []
    )
    assert result.parts == {adsh(1): Part.FIT, adsh(2): Part.FIT}
    assert issuer_identity(filing(1, a, "Co Inc")) == Identity(cik=a)
    assert issuer_identity(filing(1, a, "Fit Co")) == Identity("Fit Co", a)
