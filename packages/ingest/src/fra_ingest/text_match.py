"""Text matching shared by the locator and the industry signal.

Some PDF generators store Arabic in visual order, so the text layer yields every Arabic word
with its letters reversed (Almarai's Arabic annual report: ``ةمئاق`` for ``قائمة``). A word
never starts with ta marbuta or alef maqsura and never ends with the article, so counting
those shapes tells the two orders apart.
"""

from __future__ import annotations

import re

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
