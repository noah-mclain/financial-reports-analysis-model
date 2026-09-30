"""Repair of Arabic text that docling returns in visual order (spec 11, Data flow step 3).

Measured on Almarai AR: each run of Arabic digits comes out reversed in place (mirrored
brackets swapped back), and letters come out in reading order but separated by single spaces.
Digit runs are reversed back in place only on pages Part 1 marked ``visual_arabic``; spaced
letters are joined wherever they occur.
"""

from __future__ import annotations

import re

from fra_ingest.table_grid import Grid

_DIGIT = "٠-٩۰-۹"
_RUN = re.compile(rf"[{_DIGIT}](?:[{_DIGIT},.٫٬]*[{_DIGIT}])?")
_DIACRITICS = frozenset("ًٌٍَُِّْٰ")
_ARABIC_LETTER = re.compile(r"^[ء-يٱ-ۓ]$")
_LAM_ALEF = frozenset({"لا", "لأ", "لإ", "لآ"})


def restore_digits(text: str) -> str:
    restored = _RUN.sub(lambda m: m.group()[::-1], text)
    stripped = restored.strip()
    if stripped.startswith(")") and stripped.endswith("(") and len(stripped) > 1:
        start = restored.index(")")
        end = restored.rindex("(")
        restored = restored[:start] + "(" + restored[start + 1 : end] + ")" + restored[end + 1 :]
    return restored


def _is_letter(token: str) -> bool:
    core = "".join(c for c in token if c not in _DIACRITICS)
    return bool(_ARABIC_LETTER.match(core)) or core in _LAM_ALEF


def is_letter_spaced(text: str) -> bool:
    tokens = text.split()
    letters = sum(1 for t in tokens if _is_letter(t))
    return letters >= 3 and letters >= 0.6 * len(tokens)


def join_spaced_letters(text: str) -> str:
    if not is_letter_spaced(text):
        return text
    out: list[str] = []
    run: list[str] = []
    for token in text.split():
        if _is_letter(token):
            run.append(token)
            continue
        if run:
            out.append("".join(run))
            run = []
        out.append(token)
    if run:
        out.append("".join(run))
    return " ".join(out)


def repair_text(text: str, *, visual: bool) -> str:
    return join_spaced_letters(restore_digits(text) if visual else text)


def repair_grid(grid: Grid, *, visual: bool) -> Grid:
    cells = []
    for cell in grid.cells:
        flags = list(cell.flags)
        text = restore_digits(cell.text) if visual else cell.text
        if text != cell.text:
            flags.append("digits_reversed")
        joined = join_spaced_letters(text)
        if joined != text:
            flags.append("letters_spaced")
        cells.append(cell.model_copy(update={"text": joined, "flags": tuple(flags)}))
    return grid.model_copy(update={"cells": tuple(cells)})
