"""Text matching shared by the locator and the industry signal.

Some PDF generators store Arabic in visual order, so the text layer yields every Arabic word
with its letters reversed (Almarai's Arabic annual report: ``ةمئاق`` for ``قائمة``). A word
never starts with ta marbuta or alef maqsura and never ends with the article, so counting
those shapes tells the two orders apart.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from fra_core.labels import normalize_label

_ARABIC_WORD = re.compile(r"[ء-يٱ-ۓ]+")
_MIN_EVIDENCE = 3


def is_visual_arabic(text: str) -> bool:
    """Whether the Arabic words in ``text`` read backwards."""
    backwards = forwards = 0
    for word in _ARABIC_WORD.findall(text):
        if len(word) <= 2:
            continue
        if word.startswith(("ة", "ى")) or word.endswith("لا"):
            backwards += 1
        if word.endswith(("ة", "ى")) or word.startswith("ال"):
            forwards += 1
    return backwards >= _MIN_EVIDENCE and backwards > 2 * forwards


def reading_variants(text: str, visual: bool) -> list[str]:
    """Texts to search for phrases.

    pypdfium2 returns most Arabic lines with their words in reverse order (77 of 82 Arabic
    corpus PDFs), and a line can mix that with left-to-right runs such as dates. So any text
    holding Arabic is searched as extracted and with each line's words reversed. Visual-order
    text (Almarai AR) first has the letters of each Arabic word restored.
    """
    if not _ARABIC_WORD.search(text):
        return [text]
    lines = [line.split() for line in text.splitlines()]
    if visual:
        lines = [
            [_ARABIC_WORD.sub(lambda match: match.group(0)[::-1], word) for word in words]
            for words in lines
        ]
    same_order = "\n".join(" ".join(words) for words in lines)
    reversed_order = "\n".join(" ".join(reversed(words)) for words in lines)
    return [same_order, reversed_order]


def canonical(text: str) -> str:
    """``normalize_label``, then lam-alef folded to alef-lam. Text layers often split the
    lam-alef ligature in the wrong order (``اآلخر`` for ``الآخر``, ``المعامالت`` for
    ``المعاملات``); folding both sides the same way makes the two spellings meet."""
    return normalize_label(text).replace("لا", "ال")


@dataclass(frozen=True)
class PhraseIndex:
    """Phrases in canonical form, longest first, each with the groups it belongs to."""

    entries: tuple[tuple[str, frozenset[str]], ...]

    @classmethod
    def build(cls, groups: Mapping[str, Iterable[str]]) -> PhraseIndex:
        owners: dict[str, set[str]] = {}
        for group, phrases in groups.items():
            for phrase in phrases:
                normalized = canonical(phrase)
                if normalized:
                    owners.setdefault(normalized, set()).add(group)
        ordered = sorted(owners.items(), key=lambda item: len(item[0]), reverse=True)
        return cls(tuple((phrase, frozenset(names)) for phrase, names in ordered))

    def find(self, texts: Sequence[str]) -> dict[str, frozenset[str]]:
        """Phrases present in any of ``texts`` as whole words. A match consumes its words, so
        a shorter phrase inside a longer one that matched is not reported again."""
        found: dict[str, frozenset[str]] = {}
        for text in texts:
            haystack = f" {canonical(text)} "
            for phrase, groups in self.entries:
                needle = f" {phrase} "
                if needle in haystack:
                    found[phrase] = groups
                    haystack = haystack.replace(needle, " | ")
        return found
