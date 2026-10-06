"""The development documents of the train pool: fit and validation, read from the records.

The one place the harness turns `candidates.yaml` and the holdout records into the documents a
development command may run on. The split rule is `fra_core.split.development_documents`, which
cannot return the holdout; this only reads the files and keeps the `train` pool, so `dev`,
`model_test` and `blind` records never reach the split.
"""

from __future__ import annotations

import argparse
from collections.abc import Collection
from pathlib import Path
from typing import Any

import yaml

from fra_core.split import TRAIN, Part, development_documents
from harness.holdout_records import read_looks, read_moves
from harness.paths import CANDIDATES, HOLDOUT_MOVES, SCORING_LOG


def development_set(
    parts: Collection[Part],
    candidates: Path = CANDIDATES,
    moves: Path = HOLDOUT_MOVES,
    log: Path = SCORING_LOG,
) -> list[dict[str, Any]]:
    records = yaml.safe_load(candidates.read_text(encoding="utf-8"))["documents"]
    train = [d for d in records if d["pool"] == TRAIN]
    recorded = read_moves(moves, read_looks(log))
    return [dict(d) for d in development_documents(train, recorded, parts)]


def positive_int(text: str) -> int:
    """An argparse type for `--limit`: a slice of zero or less would run nothing or drop the last
    documents without saying so."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"{text!r} is not a positive number of documents")
    return value
