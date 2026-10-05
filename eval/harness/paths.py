"""Where the corpus records live, in one place for the harness (the corpus store, the candidate
and fetched records, the holdout moves, the validation uses, the scoring log and the analytics
policy), and the two vocabularies of those records that the harness reports by: the period kinds
and the negative-control role. scripts/corpus.py keeps its own constants for the files it already
had: it must import without the harness on the path."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CANDIDATES = ROOT / "eval/corpus/candidates.yaml"
FETCHED = ROOT / "eval/corpus/fetched.yaml"
HOLDOUT_MOVES = ROOT / "eval/corpus/holdout_moves.yaml"
VALIDATION_USES = ROOT / "eval/corpus/validation_uses.yaml"
SCORING_LOG = ROOT / "eval/corpus/scoring_log.tsv"
CORPUS = ROOT / "var/corpus"
ANALYTICS_POLICY = ROOT / "configs/analytics.toml"

# Reported by in every harness report (D18): the golden set and the train pool hold these two.
STRATA = ("annual", "interim")
NEGATIVE_CONTROL = "negative_control"  # the `role` of a bank or insurer in candidates.yaml
