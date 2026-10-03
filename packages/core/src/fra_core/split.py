"""The issuer split of the `train` pool into fit, validation and holdout parts.

Design: docs/blueprint/04-execution-phases.md 2.4, "The split" (decision D16). An issuer is
placed by SHA-256 of `holdout:` plus its key, modulo 100: buckets 0 to 14 are the holdout, 15 to
29 validation, the rest fit. The key is `cik:<number>` for an issuer with a CIK, so a corpus
issuer whose documents carry `cik` lands where its SEC rows land, and `issuer_key` of its name
otherwise. Only `train` issuers are placed; `model_test` and `blind` are refused.

Two committed records sit beside the corpus files: the moves out of the holdout (overrides made
before the first look, spent looks after it) and the scoring log, one row per look at the
holdout. Paths are relative to the repository root; callers join them to their root.

This module lives in fra-core because the dry-run harness (eval/), the dataset builder
(training/) and the corpus tools (scripts/) all need the same rule, and fra-core is the one
workspace package each of them imports without depending on another.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

MOVES_FILE = Path("eval/corpus/holdout_moves.yaml")
SCORING_LOG = Path("eval/corpus/scoring_log.tsv")

SALT = "holdout:"
HOLDOUT_END = 15  # buckets 0 to 14
VALIDATION_END = 30  # buckets 15 to 29

TRAIN = "train"
REFUSED_POOLS = {
    "model_test": "model_test is scored only at the checkpoint (D13)",
    "blind": "blind is never scored during development (R17)",
}

LOOK_KINDS = ("dry_run", "model_scoring")
MOVE_KINDS = ("override", "spent_look")
LOG_COLUMNS = ("date", "kind", "parts", "strata", "candidate")
LOG_HEADER = (
    "# Scoring log of the train holdout (docs/blueprint/04-execution-phases.md 2.4).\n"
    "# Append-only: one row per look at the holdout, written by fra_core.split before the\n"
    "# scoring runs. A dry run is a look, like a model scoring. Never edit or delete a row.\n"
    "# kind: dry_run | model_scoring. parts: the split parts looked at, comma-separated, always\n"
    "# with holdout. strata: what was reported, comma-separated (for example annual,interim).\n"
    "# candidate: the commit or model candidate that was scored.\n" + "\t".join(LOG_COLUMNS) + "\n"
)

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


def require_train(pool: str, what: str) -> None:
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
            if not value or re.search(r"[\t\n\r,]", value):
                raise SplitError(f"look field {value!r} is empty or holds a tab, newline or comma")


@dataclass(frozen=True)
class Move:
    """An issuer moved out of the holdout into fit: an override or a spent look."""

    issuer: str
    key: str
    kind: str
    date: str
    looks_before: int
    reason: str


def read_looks(path: Path) -> list[Look]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith(LOG_HEADER):
        raise SplitError(f"{path}: the header is not the scoring log header; do not edit it")
    looks: list[Look] = []
    for n, line in enumerate(text[len(LOG_HEADER) :].splitlines(), start=1):
        fields = line.split("\t")
        if len(fields) != len(LOG_COLUMNS):
            raise SplitError(f"{path}: row {n} has {len(fields)} fields, not {len(LOG_COLUMNS)}")
        date, kind, parts, strata, candidate = fields
        look = Look(date, kind, tuple(parts.split(",")), tuple(strata.split(",")), candidate)
        look.validate()
        if looks and look.date < looks[-1].date:
            raise SplitError(f"{path}: row {n} is dated earlier than the row before it")
        looks.append(look)
    return looks


def log_look(path: Path, look: Look) -> None:
    """Append one look to the scoring log. Rows are only ever appended, in date order."""
    look.validate()
    looks = read_looks(path)
    if looks and look.date < looks[-1].date:
        raise SplitError(f"look dated {look.date} is earlier than the last row ({looks[-1].date})")
    row = "\t".join((look.date, look.kind, ",".join(look.parts), ",".join(look.strata)))
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{row}\t{look.candidate}\n")


def read_moves(path: Path, looks: Sequence[Look]) -> list[Move]:
    """The moves record, checked against the scoring log: an override is dated before the first
    look and made with no look in the log; a spent look comes after a logged look."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("moves"), list):
        raise SplitError(f"{path}: expected a mapping with a 'moves' list")
    moves: list[Move] = []
    for entry in data["moves"]:
        issuer = str(entry["issuer"])
        if (entry.get("from"), entry.get("to")) != (Part.HOLDOUT, Part.FIT):
            raise SplitError(f"{issuer}: a move only goes from holdout to fit")
        date = _iso_date(str(entry["date"]), issuer)
        looks_before = int(entry["looks_before"])
        if looks_before > len(looks):
            raise SplitError(
                f"{issuer}: recorded after {looks_before} looks, the log holds {len(looks)}"
            )
        kind = entry["kind"]
        if kind == "override":
            if looks_before != 0 or (looks and date > looks[0].date):
                raise SplitError(f"{issuer}: an override is allowed only before the first look")
        elif kind == "spent_look":
            if looks_before < 1:
                raise SplitError(f"{issuer}: a spent look is recorded after a look")
            if date < looks[looks_before - 1].date:
                raise SplitError(f"{issuer}: dated before the look it follows")
        else:
            raise SplitError(f"{issuer}: move kind {kind!r} is not one of {MOVE_KINDS}")
        reason = str(entry.get("reason") or "").strip()
        if not reason:
            raise SplitError(f"{issuer}: a move needs its reason")
        key = issuer_key(issuer)
        if any(m.key == key for m in moves):
            raise SplitError(f"{issuer}: moved more than once")
        moves.append(Move(issuer, key, kind, date, looks_before, reason))
    return moves


def place(
    documents: Iterable[Mapping[str, Any]], moves: Sequence[Move]
) -> dict[Part, list[Mapping[str, Any]]]:
    """Documents by part. Every document must be a `train` one; pass `moves=[]` for the split
    as hashed."""
    documents = list(documents)
    for d in documents:
        require_train(str(d["pool"]), str(d.get("id", d["issuer"])))
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


def holdout_for_scoring(
    documents: Iterable[Mapping[str, Any]], moves_path: Path, log_path: Path, look: Look
) -> list[Mapping[str, Any]]:
    """The holdout documents for a dry run or a model scoring. The look is appended to the
    scoring log before the documents are returned, so no holdout scoring goes unlogged."""
    look.validate()
    moves = read_moves(moves_path, read_looks(log_path))
    holdout = place(documents, moves)[Part.HOLDOUT]
    log_look(log_path, look)
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
