"""Label comparison that ignores word boundaries.

Some Arabic text layers lose the spaces between words (spec 11, Almarai AR), so labels are
compared with every space removed after ``normalize_label``, on both sides.
"""

from __future__ import annotations

from collections.abc import Iterable

from fra_core.labels import normalize_label
from fra_core.schemas import StatementType
from fra_core.taxonomy.loader import CanonicalItem, Taxonomy

_Spellings = dict[StatementType, dict[str, dict[str, CanonicalItem]]]
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
    """Every alias and every section heading of every taxonomy item, keyed by statement and
    squashed spelling.

    Two items can share a squashed spelling ("net sales" and "netsales"): ``match_all`` returns
    both and ``match`` returns neither, so a collision is never settled by the order of the file.
    Headings are kept apart from aliases: ``match`` never returns an item for a heading.
    """

    def __init__(self, taxonomy: Taxonomy) -> None:
        self._index: _Spellings = {}
        self._headings: _Spellings = {}
        for item in taxonomy.items:
            for language in ("en", "ar"):
                for alias in item.aliases_for(language):
                    by_key = self._index.setdefault(item.statement, {})
                    by_key.setdefault(squash(alias), {})[item.id] = item
                for heading in item.headings_for(language):
                    by_key = self._headings.setdefault(item.statement, {})
                    by_key.setdefault(squash(heading), {})[item.id] = item

    def match_all(self, label: str, statement: StatementType) -> tuple[CanonicalItem, ...]:
        key = squash(label)
        found = self._index.get(statement, {}).get(key, {}) if key else {}
        return tuple(found.values())

    def match(self, label: str, statement: StatementType) -> CanonicalItem | None:
        found = self.match_all(label, statement)
        return found[0] if len(found) == 1 else None

    def headings(self, label: str, statement: StatementType) -> tuple[CanonicalItem, ...]:
        """The totals whose section heading is exactly this label."""
        key = squash(label)
        found = self._headings.get(statement, {}).get(key, {}) if key else {}
        return tuple(found.values())

    def run_in_heading(self, label: str, statement: StatementType) -> tuple[CanonicalItem, ...]:
        """The totals of the longest section heading the label starts with, when the text layer
        ran that heading into the row below it: what follows the heading must itself be an
        alias. A label that merely contains a heading, or continues with anything else, is a
        row of its own."""
        squashed = squash(label)
        starts = [
            key
            for key in self._headings.get(statement, {})
            if squashed.startswith(key) and squashed[len(key) :] in self._index.get(statement, {})
        ]
        if not starts:
            return ()
        return tuple(self._headings[statement][max(starts, key=len)].values())

    def hits(self, labels: Iterable[str], statement: StatementType) -> int:
        return sum(1 for label in labels if self.match(label, statement) is not None)
