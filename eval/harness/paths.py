"""Where the corpus records live, in one place for the harness (the corpus store, the candidate
and fetched records, the holdout moves and the scoring log). scripts/corpus.py keeps its own
constants for the files it already had: it must import without the harness on the path."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CANDIDATES = ROOT / "eval/corpus/candidates.yaml"
FETCHED = ROOT / "eval/corpus/fetched.yaml"
HOLDOUT_MOVES = ROOT / "eval/corpus/holdout_moves.yaml"
SCORING_LOG = ROOT / "eval/corpus/scoring_log.tsv"
CORPUS = ROOT / "var/corpus"
