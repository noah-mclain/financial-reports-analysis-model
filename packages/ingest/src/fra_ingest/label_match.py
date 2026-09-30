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


def squash(text: str) -> str:
    return normalize_label(text).replace(" ", "")


def has_subtotal_cue(label: str) -> bool:
    squashed = squash(label)
    return any(squashed.startswith(cue) for cue in SUBTOTAL_CUES)


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
