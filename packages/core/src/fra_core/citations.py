"""Pure placement of exact claim spans, without value authorization."""

import re
from dataclasses import dataclass

from fra_core.schemas.narrative import Claim, Sentence


@dataclass(frozen=True)
class PlacedClaim:
    claim: Claim
    start: int
    end: int


def _span_pattern(span: str) -> re.Pattern[str]:
    """The span as printed, not as part of a longer figure: ``1.5`` is not found inside
    ``1.50``, nor ``50`` inside ``1,250``."""
    before = r"(?<!\w)(?<!\d[.,])" if span[0].isalnum() else ""
    after = r"(?!\w)(?![.,]\d)" if span[-1].isalnum() else ""
    return re.compile(before + re.escape(span) + after)


def place_claims(sentence: Sentence) -> tuple[PlacedClaim, ...]:
    spans = [claim.text_span for claim in sentence.claims]
    for span in set(spans):
        occurrences = sum(1 for _ in _span_pattern(span).finditer(sentence.text))
        if occurrences > spans.count(span):
            raise ValueError(f"ambiguous occurrence of {span!r} in {sentence.text!r}")
    placed: list[PlacedClaim] = []
    for claim in sentence.claims:
        found = next(
            (
                m
                for m in _span_pattern(claim.text_span).finditer(sentence.text)
                if all(m.end() <= p.start or m.start() >= p.end for p in placed)
            ),
            None,
        )
        if found is None:
            raise ValueError(f"claim {claim.text_span!r} is not in {sentence.text!r}")
        placed.append(PlacedClaim(claim, found.start(), found.end()))
    return tuple(sorted(placed, key=lambda p: p.start))
