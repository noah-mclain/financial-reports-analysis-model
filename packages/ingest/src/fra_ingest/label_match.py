"""Label comparison that ignores word boundaries.

Some Arabic text layers lose the spaces between words (spec 11, Almarai AR), so labels are
compared with every space removed after ``normalize_label``, on both sides.
"""

from __future__ import annotations

from collections.abc import Iterable

from fra_core.labels import normalize_label
from fra_core.schemas import StatementType
from fra_core.taxonomy.loader import CanonicalItem, Taxonomy

SUBTOTAL_CUES = ("total", "اجمالي", "مجموع")
# The row that closes a balance sheet: total equity and liabilities.
CLOSING_TOTAL_ID = "total_liabilities_and_equity"


def squash(text: str) -> str:
    return normalize_label(text).replace(" ", "")


def has_subtotal_cue(label: str) -> bool:
    tokens = normalize_label(label).split()
    if not tokens:
        return False
    # First token is exactly a cue
    if tokens[0] in SUBTOTAL_CUES:
        return True
    # Single token: check if starts with cue but not a feminine/plural variant
    if len(tokens) == 1:
        token = tokens[0]
        for cue in SUBTOTAL_CUES:
            if token.startswith(cue):
                is_feminine = cue == "مجموع" and len(token) > len(cue) and token[len(cue)] == "ه"
                is_plural = cue == "total" and len(token) > len(cue) and token[len(cue)] == "s"
                return not (is_feminine or is_plural)
    return False


class LabelIndex:
    """Every alias of every taxonomy item, keyed by statement and squashed spelling."""

    def __init__(self, taxonomy: Taxonomy) -> None:
        self._index: dict[StatementType, dict[str, CanonicalItem]] = {}
        for item in taxonomy.items:
            for language in ("en", "ar"):
                for alias in item.aliases_for(language):
                    self._index.setdefault(item.statement, {})[squash(alias)] = item

    def match(self, label: str, statement: StatementType) -> CanonicalItem | None:
        key = squash(label)
        return self._index.get(statement, {}).get(key) if key else None

    def hits(self, labels: Iterable[str], statement: StatementType) -> int:
        return sum(1 for label in labels if self.match(label, statement) is not None)
