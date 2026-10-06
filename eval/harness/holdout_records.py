"""The committed records of the train holdout and of the validation part: how they are written.

`eval/corpus/holdout_moves.yaml` holds the issuers moved out of the holdout into fit,
`eval/corpus/scoring_log.tsv` one row per look at the holdout, and
`eval/corpus/validation_uses.yaml` the validation documents that were used for ingest
development (docs/blueprint/04-execution-phases.md 2.4). This module is the one place that knows
their file format. The rules over the parsed values (an override only before the first look, a
log in date order, and so on) are `fra_core.split`'s; a rule broken in a file is reported with
the file's path.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from fra_core.split import Look, Move, SplitError, check_looks, check_moves, issuer_key

LOG_COLUMNS = ("date", "kind", "parts", "strata", "candidate")
LOG_HEADER = (
    "# Scoring log of the train holdout (docs/blueprint/04-execution-phases.md 2.4).\n"
    "# Append-only: one row per look at the holdout, written by eval/harness/holdout_records.py\n"
    "# before the scoring runs. A dry run is a look, like a model scoring. Never edit or delete\n"
    "# a row.\n"
    "# kind: dry_run | model_scoring. parts: the split parts looked at, comma-separated, always\n"
    "# with holdout. strata: what was reported, comma-separated (for example annual,interim).\n"
    "# candidate: the commit or model candidate that was scored.\n" + "\t".join(LOG_COLUMNS) + "\n"
)
MOVE_FIELDS = ("issuer", "kind", "from", "to", "date", "looks_before", "reason")
USE_FIELDS = ("date", "purpose", "documents")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class ValidationUse:
    """Validation documents used while developing ingest, which 2.4 reserves for model choices."""

    date: str
    purpose: str
    documents: tuple[str, ...]


def _in_file(path: Path, check: Callable[[], None]) -> None:
    """Run a rule from `fra_core.split` and name the file it was broken in."""
    try:
        check()
    except SplitError as exc:
        raise SplitError(f"{path}: {exc}") from exc


def read_looks(path: Path) -> list[Look]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith(LOG_HEADER):
        raise SplitError(f"{path}: the header is not the scoring log header; do not edit it")
    looks: list[Look] = []
    rows = text[len(LOG_HEADER) :]
    if rows and not rows.endswith("\n"):
        raise SplitError(f"{path}: the last row does not end in a newline; the log is damaged")
    for n, line in enumerate(rows.split("\n")[:-1], start=1):
        fields = line.split("\t")
        if len(fields) != len(LOG_COLUMNS):
            raise SplitError(f"{path}: row {n} has {len(fields)} fields, not {len(LOG_COLUMNS)}")
        date, kind, parts, strata, candidate = fields
        looks.append(Look(date, kind, tuple(parts.split(",")), tuple(strata.split(",")), candidate))
    _in_file(path, lambda: check_looks(looks))
    return looks


def log_look(path: Path, look: Look) -> None:
    """Append one look to the scoring log. Rows are only ever appended, in date order."""
    looks = read_looks(path)
    _in_file(path, lambda: check_looks([*looks, look]))
    row = "\t".join((look.date, look.kind, ",".join(look.parts), ",".join(look.strata)))
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{row}\t{look.candidate}\n")


def read_moves(path: Path, looks: list[Look]) -> list[Move]:
    """The moves record, parsed and checked against the scoring log."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("moves"), list):
        raise SplitError(f"{path}: expected a mapping with a 'moves' list")
    moves = [_move(path, n, entry) for n, entry in enumerate(data["moves"], start=1)]
    _in_file(path, lambda: check_moves(moves, looks))
    return moves


def _move(path: Path, n: int, entry: Any) -> Move:
    if not isinstance(entry, dict):
        raise SplitError(f"{path}: move {n} is not a mapping")
    who = entry.get("issuer", f"move {n}")
    missing = [k for k in MOVE_FIELDS if k not in entry]
    if missing:
        raise SplitError(f"{path}: {who} lacks {', '.join(map(repr, missing))}")
    looks_before = entry["looks_before"]
    if not isinstance(looks_before, int) or isinstance(looks_before, bool):
        raise SplitError(f"{path}: {who}: looks_before {looks_before!r} is not an integer")
    if not isinstance(entry["kind"], str):
        raise SplitError(f"{path}: {who}: kind {entry['kind']!r} is not text")
    issuer = str(entry["issuer"])
    return Move(
        issuer=issuer,
        key=issuer_key(issuer),
        kind=entry["kind"],
        source=str(entry["from"]),
        target=str(entry["to"]),
        date=str(entry["date"]),
        looks_before=looks_before,
        reason=str(entry["reason"] or ""),
    )


def read_validation_uses(path: Path) -> list[ValidationUse]:
    """The validation documents used for ingest development, in the order recorded."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("uses"), list):
        raise SplitError(f"{path}: expected a mapping with a 'uses' list")
    return [_use(path, n, entry) for n, entry in enumerate(data["uses"], start=1)]


def _use(path: Path, n: int, entry: Any) -> ValidationUse:
    if not isinstance(entry, dict):
        raise SplitError(f"{path}: use {n} is not a mapping")
    missing = [k for k in USE_FIELDS if k not in entry]
    if missing:
        raise SplitError(f"{path}: use {n} lacks {', '.join(map(repr, missing))}")
    date, documents = entry["date"], entry["documents"]
    if not isinstance(date, str) or not _DATE.fullmatch(date):
        raise SplitError(f"{path}: use {n}: date {date!r} is not written YYYY-MM-DD")
    if not isinstance(documents, list) or not documents:
        raise SplitError(f"{path}: use {n}: documents {documents!r} is not a list of ids")
    if not all(isinstance(d, str) for d in documents):
        raise SplitError(f"{path}: use {n}: documents {documents!r} holds a non-text id")
    repeated = sorted({d for d in documents if documents.count(d) > 1})
    if repeated:
        raise SplitError(f"{path}: use {n}: {repeated} listed twice")
    return ValidationUse(date, str(entry["purpose"]), tuple(documents))
