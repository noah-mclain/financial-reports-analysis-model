"""The issuer split of the `train` pool into fit, validation and holdout parts.

Design: docs/blueprint/04-execution-phases.md 2.4, "The split" (decision D16). An issuer is
placed by SHA-256 of `holdout:` plus its key, modulo 100: buckets 0 to 14 are the holdout, 15 to
29 validation, the rest fit. The key is `cik:<number>` for an issuer with a CIK, so a corpus
issuer whose documents carry `cik` lands where its SEC rows land, and `issuer_key` of its name
otherwise. Only `train` issuers are placed; `model_test` and `blind` are refused.

Two committed records sit beside the corpus files: the moves out of the holdout (overrides made
before the first look, spent looks after it) and the scoring log, one row per look at the
holdout. This module knows neither their paths nor their file format (eval/harness/
holdout_records.py reads and writes both); it holds the rules over their parsed values.

Holdout documents leave this module only through `holdout_for_scoring`, which has the look
recorded first. Everything else public returns keys, parts or counts, or, for
`development_documents`, the fit and validation documents, never the holdout's.

This module lives in fra-core because the dry-run harness (eval/), the dataset builder
(training/) and the corpus tools (scripts/) all need the same rule, and fra-core is the one
workspace package each of them imports without depending on another.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = [
    "TRAIN",
    "HoldoutAvailability",
    "Look",
    "Move",
    "Part",
    "PartCounts",
    "PoolRefused",
    "SplitError",
    "bucket",
    "check_looks",
    "check_moves",
    "cik_key",
    "development_documents",
    "document_key",
    "hashed_part",
    "holdout_availability",
    "holdout_for_scoring",
    "issuer_key",
    "part_counts",
]

SALT = "holdout:"
HOLDOUT_END = 15  # buckets 0 to 14
VALIDATION_END = 30  # buckets 15 to 29

TRAIN = "train"
REFUSED_POOLS = {
    "model_test": "model_test is scored only at the checkpoint (D13)",
    "blind": "blind is never scored during development (R17)",
}

# What would break a log row on reading it back: the comma that joins a field's values, every
# control character (tab, newline and the vertical tab, form feed and so on) and the Unicode
# line and paragraph separators.
_UNWRITABLE = re.compile(r"[,\x00-\x1f\x7f-\x9f\u2028\u2029]")

LOOK_KINDS = ("dry_run", "model_scoring")
MOVE_KINDS = ("override", "spent_look")

_SUFFIXES = {
    "company",
    "co",
    "the",
    "group",
    "pjsc",
    "sae",
    "plc",
    "inc",
    "corporation",
    "corp",
    "ltd",
    "limited",
    "for",
}


class SplitError(ValueError):
    """A split, move or log rule was broken."""


class PoolRefused(SplitError):
    """A `model_test` or `blind` document reached the split."""


class Part(StrEnum):
    FIT = "fit"
    VALIDATION = "validation"
    HOLDOUT = "holdout"


def issuer_key(name: str) -> str:
    """Normalize an issuer name so 'Almarai Company' and 'ALMARAI CO.' compare equal."""
    words = re.findall(r"[a-z0-9]+", name.lower().replace(".", ""))
    return " ".join(w for w in words if w not in _SUFFIXES)


def cik_key(cik: int | str) -> str:
    """The key of an SEC filer, and of a corpus issuer whose documents carry its CIK."""
    return f"cik:{int(cik)}"


def document_key(document: Mapping[str, Any]) -> str:
    cik = document.get("cik")
    return cik_key(cik) if cik else issuer_key(str(document["issuer"]))


def bucket(key: str) -> int:
    return int(hashlib.sha256(f"{SALT}{key}".encode()).hexdigest(), 16) % 100


def hashed_part(key: str) -> Part:
    b = bucket(key)
    if b < HOLDOUT_END:
        return Part.HOLDOUT
    if b < VALIDATION_END:
        return Part.VALIDATION
    return Part.FIT


def _require_train(pool: str, what: str) -> None:
    if pool in REFUSED_POOLS:
        raise PoolRefused(f"{what}: {REFUSED_POOLS[pool]}; only train issuers are split")
    if pool != TRAIN:
        raise SplitError(f"{what}: pool {pool!r} is not split; only train issuers are")


@dataclass(frozen=True)
class Look:
    """One row of the scoring log."""

    date: str
    kind: str
    parts: tuple[str, ...]
    strata: tuple[str, ...]
    candidate: str

    def validate(self) -> None:
        _iso_date(self.date, "look")
        if self.kind not in LOOK_KINDS:
            raise SplitError(f"look kind {self.kind!r} is not one of {', '.join(LOOK_KINDS)}")
        if Part.HOLDOUT not in self.parts:
            raise SplitError("a look at the holdout must list holdout among its parts")
        unknown = set(self.parts) - set(Part)
        if unknown:
            raise SplitError(f"unknown parts in look: {', '.join(sorted(unknown))}")
        if not self.strata or not self.candidate:
            raise SplitError("a look needs its strata and its candidate")
        for value in (*self.parts, *self.strata, self.candidate):
            if not value or _UNWRITABLE.search(value):
                raise SplitError(
                    f"look field {value!r} is empty or holds a comma or a control or "
                    "line-separator character"
                )


@dataclass(frozen=True)
class Move:
    """An issuer moved out of the holdout into fit: an override or a spent look."""

    issuer: str
    key: str
    kind: str
    source: str
    target: str
    date: str
    looks_before: int
    reason: str


def check_looks(looks: Sequence[Look]) -> None:
    """Every look is valid and no look is dated before the one ahead of it."""
    for n, look in enumerate(looks, start=1):
        look.validate()
        if n > 1 and look.date < looks[n - 2].date:
            raise SplitError(f"look {n} ({look.date}) is dated earlier than the look before it")


def check_moves(moves: Sequence[Move], looks: Sequence[Look]) -> None:
    """The moves record against the scoring log: an override is dated before the first look and
    made with no look in the log; a spent look comes after a logged look."""
    for m in moves:
        _iso_date(m.date, m.issuer)
        if (m.source, m.target) != (Part.HOLDOUT, Part.FIT):
            raise SplitError(f"{m.issuer}: a move only goes from holdout to fit")
        if m.looks_before > len(looks):
            raise SplitError(
                f"{m.issuer}: recorded after {m.looks_before} looks, the log holds {len(looks)}"
            )
        if m.kind == "override":
            if m.looks_before != 0 or (looks and m.date >= looks[0].date):
                raise SplitError(f"{m.issuer}: an override is allowed only before the first look")
        elif m.kind == "spent_look":
            if m.looks_before < 1:
                raise SplitError(f"{m.issuer}: a spent look is recorded after a look")
            if m.date < looks[m.looks_before - 1].date:
                raise SplitError(f"{m.issuer}: dated before the look it follows")
        else:
            raise SplitError(f"{m.issuer}: move kind {m.kind!r} is not one of {MOVE_KINDS}")
        if not m.reason.strip():
            raise SplitError(f"{m.issuer}: a move needs its reason")
    keys = [m.key for m in moves]
    for m in moves:
        if keys.count(m.key) > 1:
            raise SplitError(f"{m.issuer}: moved more than once")


@dataclass(frozen=True)
class PartCounts:
    """What one part holds, counted. Issuers are counted by key; a language counts the issuers
    with at least one document in it, and `both` those with Arabic and English."""

    issuers: int
    documents: int
    arabic: int
    english: int
    both: int
    annual: int
    interim: int


def part_counts(
    documents: Iterable[Mapping[str, Any]], moves: Sequence[Move]
) -> dict[Part, PartCounts]:
    """Counts per part, never the documents themselves. Pass `moves=[]` for the split as
    hashed. Counting reads only the records, so it is not a look and is not logged."""
    counts = {}
    for part, docs in _place(documents, moves).items():
        languages: dict[str, set[str]] = {}
        for d in docs:
            languages.setdefault(document_key(d), set()).add(str(d["language"]))
        periods = [d["period"] for d in docs]
        counts[part] = PartCounts(
            issuers=len(languages),
            documents=len(docs),
            arabic=sum("ar" in v for v in languages.values()),
            english=sum("en" in v for v in languages.values()),
            both=sum({"ar", "en"} <= v for v in languages.values()),
            annual=periods.count("annual"),
            interim=periods.count("interim"),
        )
    return counts


def _place(
    documents: Iterable[Mapping[str, Any]], moves: Sequence[Move]
) -> dict[Part, list[Mapping[str, Any]]]:
    """Documents by part. Every document must be a `train` one. Private: holdout documents
    are handed out only by `holdout_for_scoring`."""
    documents = list(documents)
    for d in documents:
        _require_train(str(d["pool"]), str(d.get("id", d["issuer"])))
    names: dict[str, str] = {}
    for d in documents:
        name, key = issuer_key(str(d["issuer"])), document_key(d)
        if names.setdefault(name, key) != key:
            raise SplitError(
                f"{d['issuer']}: keyed both {names[name]} and {key}; give every document "
                "of an issuer the same cik, or none"
            )
    moved = set()
    for m in moves:
        found = names.get(m.key)
        if found is None:
            raise SplitError(f"{m.issuer}: moved, but it has no train document")
        if hashed_part(found) is not Part.HOLDOUT:
            raise SplitError(
                f"{m.issuer}: not in the holdout by the hash ({hashed_part(found)}); "
                "a move only takes an issuer out of the holdout"
            )
        moved.add(found)
    parts: dict[Part, list[Mapping[str, Any]]] = {p: [] for p in Part}
    for d in documents:
        key = document_key(d)
        parts[Part.FIT if key in moved else hashed_part(key)].append(d)
    return parts


def development_documents(
    documents: Iterable[Mapping[str, Any]],
    moves: Sequence[Move],
    parts: Collection[Part],
) -> list[Mapping[str, Any]]:
    """The documents of the fit and/or validation part after the recorded moves, in the order
    given. Asking for the holdout raises: it leaves this module only through
    `holdout_for_scoring`. Reads only the records, so it is not a look and is not logged."""
    if not parts:
        raise SplitError("development_documents: no part was asked for")
    if Part.HOLDOUT in parts:
        raise SplitError(
            "development_documents never returns the holdout; its documents leave "
            "fra_core.split only through holdout_for_scoring, which logs the look"
        )
    ordered = list(documents)
    placed = _place(ordered, moves)
    wanted = {id(d) for part in parts for d in placed[Part(part)]}
    return [d for d in ordered if id(d) in wanted]


@dataclass(frozen=True)
class HoldoutAvailability:
    """How many holdout documents there are and how many of them are not ready."""

    documents: int
    missing: int


def holdout_availability(
    documents: Iterable[Mapping[str, Any]],
    moves: Sequence[Move],
    ready_ids: Collection[str],
) -> HoldoutAvailability:
    """Counts only, never the documents: how many holdout documents are not among `ready_ids`
    before a look is spent on them. The caller computes readiness over every train document,
    so no holdout document leaves this module. Like `part_counts` it is not a look and is not
    logged."""
    holdout = _place(documents, moves)[Part.HOLDOUT]
    return HoldoutAvailability(len(holdout), sum(str(d["id"]) not in ready_ids for d in holdout))


def holdout_for_scoring(
    documents: Iterable[Mapping[str, Any]],
    moves: Sequence[Move],
    looks: Sequence[Look],
    look: Look,
    record: Callable[[Look], None],
) -> list[Mapping[str, Any]]:
    """The holdout documents for a dry run or a model scoring. `record` writes the look to the
    scoring log and is called before the documents are returned, so no holdout scoring goes
    unlogged; if it raises, no document leaves. `looks` are the rows already in the log."""
    check_looks([*looks, look])
    if look.parts != (Part.HOLDOUT,):
        raise SplitError(
            f"a holdout scoring returns the holdout only; the look lists {','.join(look.parts)}"
        )
    check_moves(moves, looks)
    holdout = _place(documents, moves)[Part.HOLDOUT]
    record(look)
    return holdout


def _iso_date(value: str, what: str) -> str:
    """A YYYY-MM-DD date, kept as text: rows are ordered by comparing it."""
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError(value)
        dt.date.fromisoformat(value)
    except ValueError as exc:
        raise SplitError(f"{what}: date {value!r} is not YYYY-MM-DD") from exc
    return value
