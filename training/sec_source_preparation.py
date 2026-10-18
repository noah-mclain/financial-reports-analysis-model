"""Roster reading and routing of SEC filings from Financial Statement Data Sets archives.

Only ``sub.txt`` (submission metadata, no labels) is read here, and no pool is ever allocated:
routing uses recorded assignments and the shared split in ``fra_core.split``.

Form interpretation (the one place): annual filings are 10-K, 10-K/A, 20-F and 40-F; interim
filings are 10-Q and 10-Q/A. Any other form is dropped with a counted reason.
"""

from __future__ import annotations

import re
import zipfile
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from dataset_contracts import Contract, DatasetError
from pydantic import ValidationError

from fra_core.pools import Identity, PoolRegistry
from fra_core.split import (
    Look,
    Move,
    Part,
    check_looks,
    check_moves,
    cik_key,
    development_documents,
    hashed_part,
    issuer_key,
)

SUB_MEMBER = "sub.txt"
# Columns of the submission table the preparation needs, by the names in the SEC readme.
SUB_REQUIRED = ("adsh", "cik", "name", "sic", "form", "period", "fy", "fp", "aciks")
ANNUAL_FORMS = frozenset({"10-K", "10-K/A", "20-F", "40-F"})
INTERIM_FORMS = frozenset({"10-Q", "10-Q/A"})
_ADSH = re.compile(r"\d{10}-\d{2}-\d{6}")
_DATE = re.compile(r"\d{8}")


class FilingRow(Contract):
    """The metadata of one submission; it holds no label."""

    adsh: str
    cik: int
    name: str
    sic: str
    form: str
    period: str | None
    fy: str
    fp: str
    aciks: tuple[int, ...]


def issuer_identity(filing: FilingRow) -> Identity:
    """The issuer as the registry records it: a name that normalizes to nothing is left out."""
    return Identity(filing.name if issuer_key(filing.name) else None, filing.cik)


def read_archive_member(archive: Path, member: str) -> str:
    """The text of the submission member; no other member may be read here."""
    if member != SUB_MEMBER:
        raise DatasetError(f"{archive}: only {SUB_MEMBER} may be read, not {member!r}")
    try:
        with zipfile.ZipFile(archive) as zf:
            try:
                data = zf.read(SUB_MEMBER)
            except KeyError:
                raise DatasetError(f"{archive}: {SUB_MEMBER} is missing") from None
    except (OSError, zipfile.BadZipFile) as exc:
        raise DatasetError(f"{archive}: cannot read archive: {exc}") from exc
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DatasetError(f"{archive}:{SUB_MEMBER}: not UTF-8: {exc.reason}") from exc


def read_roster(archive: Path) -> tuple[FilingRow, ...]:
    """Every submission of one archive, in file order; malformed rows name member and line."""
    where = f"{archive}:{SUB_MEMBER}"
    lines = read_archive_member(archive, SUB_MEMBER).split("\n")
    header = lines[0].rstrip("\r").split("\t")
    missing = [name for name in SUB_REQUIRED if name not in header]
    if missing:
        raise DatasetError(f"{where}:1: header lacks columns {missing}")
    if header[0] != "adsh":
        raise DatasetError(f"{where}:1: adsh must be the first column, not {header[0]!r}")
    index = {name: header.index(name) for name in SUB_REQUIRED}
    rows: list[FilingRow] = []
    seen: set[str] = set()
    for number, line in enumerate(lines[1:], start=2):
        if not line:
            continue
        fields = line.rstrip("\r").split("\t")
        if len(fields) != len(header):
            raise DatasetError(f"{where}:{number}: expected {len(header)} columns")
        values = {name: fields[i] for name, i in index.items()}
        if not _ADSH.fullmatch(values["adsh"]):
            raise DatasetError(f"{where}:{number}: adsh {values['adsh']!r} is not an accession")
        # The readme says period may not be null; real archives hold empty ones, kept as None.
        if values["period"] and not _DATE.fullmatch(values["period"]):
            raise DatasetError(f"{where}:{number}: period {values['period']!r} is not yyyymmdd")
        if values["adsh"] in seen:
            raise DatasetError(f"{where}:{number}: {values['adsh']} appears twice")
        seen.add(values["adsh"])
        extra = values["aciks"].split()
        if not all(re.fullmatch(r"\d{1,10}", c) for c in [values["cik"], *extra]):
            bad = "aciks" if not all(re.fullmatch(r"\d{1,10}", c) for c in extra) else "cik"
            raise DatasetError(f"{where}:{number}: {bad} is not a CIK list")
        try:
            rows.append(
                FilingRow(
                    adsh=values["adsh"],
                    cik=int(values["cik"]),
                    name=values["name"],
                    sic=values["sic"],
                    form=values["form"],
                    period=values["period"] or None,
                    fy=values["fy"],
                    fp=values["fp"],
                    aciks=tuple(int(c) for c in extra),
                )
            )
        except ValidationError as exc:
            raise DatasetError(f"{where}:{number}: {exc}") from exc
        if rows[-1].cik <= 0:
            raise DatasetError(f"{where}:{number}: cik must be positive")
    return tuple(rows)


@dataclass(frozen=True)
class RegistryCounts:
    """Counts of supported-form filers by what the registry records; never the pool of an
    unrecorded CIK."""

    filings: int
    supported_forms: int
    ciks: int
    recorded_by_pool: Counter[str]
    unrecorded: int


def registry_counts(filings: Iterable[FilingRow], registry: PoolRegistry) -> RegistryCounts:
    rows = list(filings)
    supported = [f for f in rows if f.form in ANNUAL_FORMS | INTERIM_FORMS]
    recorded = registry.cik_pools()
    ciks = {f.cik for f in supported}
    return RegistryCounts(
        filings=len(rows),
        supported_forms=len(supported),
        ciks=len(ciks),
        recorded_by_pool=Counter(recorded[c] for c in ciks if c in recorded),
        unrecorded=sum(c not in recorded for c in ciks),
    )


@dataclass
class RoutingResult:
    """Filings that may be read (accession to fit or validation) and counted drop reasons."""

    parts: dict[str, Part] = field(default_factory=dict)
    dropped: Counter[str] = field(default_factory=Counter)
    applicable_moves: tuple[Move, ...] = ()


def route_filings(
    filings: Sequence[FilingRow],
    registry: PoolRegistry,
    moves: Sequence[Move],
    looks: Sequence[Look],
) -> RoutingResult:
    """Fit and validation filings by the recorded pool and the shared split; nothing is allocated.

    A filing is dropped, with a counted reason, for an unsupported form, an unrecorded CIK, a
    pool other than train, an unmoved holdout issuer, a co-registrant that is not recorded train
    in the same part, or a normalized name shared by two CIKs. A move applies only to filings
    that carry the moved issuer's normalized name.
    """
    check_looks(looks)
    check_moves(moves, looks)
    result = RoutingResult()
    pools = registry.cik_pools()
    candidates: list[FilingRow] = []
    for f in filings:
        if f.form not in ANNUAL_FORMS | INTERIM_FORMS:
            result.dropped["unsupported_form"] += 1
        elif f.period is None:
            result.dropped["missing_period"] += 1
        elif f.cik not in pools:
            result.dropped["unrecorded_cik"] += 1
        elif pools[f.cik] != "train":
            result.dropped[f"pool_{pools[f.cik]}"] += 1
        else:
            candidates.append(f)
    # The shared split refuses one normalized name under two CIKs; drop those filings, counted.
    owners: dict[str, set[int]] = {}
    for f in candidates:
        owners.setdefault(issuer_key(f.name), set()).add(f.cik)
    # A name that normalizes to nothing is keyed by its CIK instead (see issuer_identity).
    clashes = {name for name, ciks in owners.items() if name and len(ciks) > 1}
    result.dropped["name_collision"] += sum(issuer_key(f.name) in clashes for f in candidates)
    candidates = [f for f in candidates if issuer_key(f.name) not in clashes]
    if not result.dropped["name_collision"]:
        del result.dropped["name_collision"]
    documents = [
        {
            "id": f.adsh,
            "issuer": f.name if issuer_key(f.name) else cik_key(f.cik),
            "cik": f.cik,
            "pool": "train",
        }
        for f in candidates
    ]
    # A move names an issuer; the split needs the moved issuer among the documents, so a move
    # applies here only when some candidate filing carries that normalized name.
    names = {issuer_key(f.name) for f in candidates} - {""}
    applicable = [m for m in moves if m.key in names]
    result.applicable_moves = tuple(applicable)
    placed = {
        str(d["id"]): part
        for part in (Part.FIT, Part.VALIDATION)
        for d in development_documents(documents, applicable, {part})
    }
    for f in candidates:
        part = placed.get(f.adsh)
        if part is None:
            result.dropped["holdout"] += 1
        elif any(pools.get(c) != "train" or hashed_part(cik_key(c)) != part for c in f.aciks):
            result.dropped["coregistrant_not_routed"] += 1
        else:
            result.parts[f.adsh] = part
    return result
