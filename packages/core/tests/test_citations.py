"""Shared span placement keeps occurrence and numeric-boundary behavior."""

import pytest

from fra_core.citations import place_claims
from fra_core.schemas import Claim, Sentence


def test_repeated_values_take_distinct_occurrences() -> None:
    claim = Claim(metric_id="m", period_key="p", text_span="1.5")
    placed = place_claims(Sentence(text="1.50 then 1.5 and 1.5", claims=[claim, claim]))
    assert [(p.start, p.end) for p in placed] == [(10, 13), (18, 21)]
    with pytest.raises(ValueError):
        place_claims(Sentence(text="1,250", claims=[claim.model_copy(update={"text_span": "50"})]))


def test_a_single_claim_rejects_a_span_appearing_more_than_once() -> None:
    claim = Claim(metric_id="m", period_key="p", text_span="1.5")
    with pytest.raises(ValueError, match="ambiguous"):
        place_claims(Sentence(text="1.5 and 1.5 again", claims=[claim]))
