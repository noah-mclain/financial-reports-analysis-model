"""Score the extraction against the expected files (spec 12, Scoring; 04, Gate G1).

    PYTHONPATH=eval uv run python -m harness.extraction

For each expected statement, its rows are aligned in order with the extracted rows, and every
confirmed expected figure is scored: right when the aligned row holds the same value in that
period and does not sit under another row's label. Where nothing is printed, nothing must be
read. Only files with status ``checked`` count towards G1. A draft starts as a copy of the
extraction, so only the figures confirmed against the page are scored, in a block headed
provisional. Writes var/eval/extraction-golden.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher

import yaml

from fra_core.schemas import LineItem, Period, Statement, StatementType
from fra_ingest.config import REPO_ROOT, load_config
from fra_ingest.label_match import squash
from fra_ingest.structure import structure_pdf
from harness.expected import (
    EXPECTED_DIR,
    MANIFEST,
    ExpectedFile,
    ExpectedRow,
    ExpectedStatement,
    load_expected,
)

OUT = REPO_ROOT / "var" / "eval"
# Gate G1 (04-execution-phases.md).
DIGITAL = 0.995
SCANNED = 0.98
PERIODS = 1.0
SIGN = 0.999
METADATA = 1.0
# How alike an expected label and a stretch of the label read must be to count as found.
LABEL_MATCH = 0.85
# Below this, a label read is not this row's label even allowing for OCR noise.
LABEL_READABLE = 0.6
# Characters by which two stretches of a label may differ or overlap and still count as apart.
_SLOP = 2


@dataclass
class Score:
    cells: int = 0
    right: int = 0
    sign_cells: int = 0
    sign_right: int = 0
    periods: int = 0
    periods_right: int = 0
    metadata: int = 0
    metadata_right: int = 0
    unconfirmed: int = 0
    extra_rows: int = 0
    mislabelled: int = 0
    wrong: list[dict[str, str]] = field(default_factory=list)

    def __add__(self, other: Score) -> Score:
        return Score(
            **{
                name: getattr(self, name) + getattr(other, name)
                for name in self.__dataclass_fields__
            }
        )


def _place(label: str, read: str) -> tuple[float, int, int] | None:
    """Where an expected label sits in the label read, both without spaces: how alike the label
    and that stretch are, and the stretch's start and end. None when they share no two
    characters in a row."""
    blocks = [
        b
        for b in SequenceMatcher(None, label, read, autojunk=False).get_matching_blocks()
        if b.size >= 2
    ]
    if not blocks:
        return None
    first, last = blocks[0], blocks[-1]
    start = max(0, first.b - first.a)
    end = min(len(read), last.b + last.size + len(label) - (last.a + last.size))
    alike = SequenceMatcher(None, label, read[start:end], autojunk=False).ratio()
    return alike, start, end


def mislabelled(row: ExpectedRow, read: str, expected: Sequence[ExpectedRow]) -> bool:
    """Whether a figure sits under another row's label.

    Every expected label is looked for inside the label read. Those found (``LABEL_MATCH``
    alike or more) compete, and the one that accounts for the longest stretch of the label read
    is the label it is: "Cash and cash equivalents" read with a typo is that label, not "Cash".
    The figure is mislabelled when the row read has no label, when the label read is another
    row's and the row's own label is not in it, or when its own label is only found inside the
    stretch the other accounts for. A cell holding two rows' labels side by side serves both,
    and so does one holding a heading before the row's label. A label garbled but still
    readable as the row's own (``LABEL_READABLE``) does not make its figure wrong; one that
    reads as nothing of the row's, such as a section heading alone, does.
    """
    own, got = squash(row.label), squash(read)
    if not own:
        return False
    if not got:
        return True
    places = {}
    for label in {squash(r.label) for r in expected} | {own}:
        place = _place(label, got) if label else None
        if place is not None and place[0] >= LABEL_MATCH:
            places[label] = place
    mine = places.get(own) or _place(own, got)
    if mine is None or mine[0] < LABEL_READABLE:
        # Not this row's label by any reading: another row's, or a heading, or nothing.
        return True

    def overlaps(other: tuple[float, int, int]) -> bool:
        return min(mine[2], other[2]) - max(mine[1], other[1]) > _SLOP

    if own not in places:
        # Readable as this row's through noise, unless that stretch is really another's.
        return any(overlaps(place) for place in places.values())
    # The longest stretch wins; between stretches of about one length, the closer match.
    longest = max(end - start for _, start, end in places.values())
    winner = max(
        (label for label, (_, start, end) in places.items() if longest - (end - start) <= _SLOP),
        key=lambda label: (places[label][0], label == own),
    )
    return winner != own and overlaps(places[winner])


def _matches(row: ExpectedRow, item: LineItem) -> bool:
    label = squash(row.label)
    if label and label == squash(item.raw_label):
        return True
    return any(value and item.value_for(key) == value for key, value in row.values.items())


def align_rows(expected: Sequence[ExpectedRow], items: Sequence[LineItem]) -> list[int | None]:
    """For each expected row, the index of its extracted row or None: the longest alignment
    that keeps both in order, two rows matching by label or by a shared non-zero value."""
    rows, cols = len(expected), len(items)
    best = [[0] * (cols + 1) for _ in range(rows + 1)]
    for i in range(rows - 1, -1, -1):
        for j in range(cols - 1, -1, -1):
            if _matches(expected[i], items[j]):
                best[i][j] = best[i + 1][j + 1] + 1
            else:
                best[i][j] = max(best[i + 1][j], best[i][j + 1])
    aligned: list[int | None] = [None] * rows
    i = j = 0
    while i < rows and j < cols:
        if _matches(expected[i], items[j]) and best[i][j] == best[i + 1][j + 1] + 1:
            aligned[i] = j
            i, j = i + 1, j + 1
        elif best[i + 1][j] >= best[i][j + 1]:
            i += 1
        else:
            j += 1
    return aligned


def _period(period: Period) -> tuple[object, ...]:
    return (period.key, period.end_date, period.kind, period.months)


def score_statement(expected: ExpectedStatement, statement: Statement | None) -> Score:
    score = Score(periods=len(expected.periods), metadata=2)
    items = statement.line_items if statement is not None else []
    if statement is not None:
        found = {_period(p) for p in statement.periods}
        score.periods_right = sum(1 for p in expected.periods if _period(p) in found)
        score.metadata_right = (statement.scale == expected.scale) + (
            statement.currency == expected.currency
        )
    aligned = align_rows(expected.rows, items)
    for row, position in zip(expected.rows, aligned, strict=True):
        item = items[position] if position is not None else None
        labelled = item is not None and not mislabelled(row, item.raw_label, expected.rows)
        for key, value in row.values.items():
            if key in row.unconfirmed:
                score.unconfirmed += 1
                continue
            read = item.value_for(key) if item is not None else None
            score.cells += 1
            # Nothing printed must read as nothing; a figure must be read, on its own label.
            same = statement is not None and read == value
            if same and (value is None or labelled):
                score.right += 1
            else:
                score.mislabelled += same
                score.wrong.append(
                    {
                        "statement": expected.type.value,
                        "label": row.label,
                        "period": key,
                        "expected": "" if value is None else str(value),
                        "read": "" if read is None else str(read),
                        "label_read": item.raw_label if item is not None else "",
                    }
                )
            if value is not None and read is not None and abs(read) == abs(value):
                score.sign_cells += 1
                score.sign_right += read == value
    used = {p for p in aligned if p is not None}
    score.extra_rows = sum(
        1
        for position, item in enumerate(items)
        if position not in used and any(c.reported is not None for c in item.cells)
    )
    return score


def _share(right: int, total: int) -> float:
    return right / total if total else 1.0


def _mode_line(mode: str, score: Score) -> str:
    return (
        f"  {mode:<7}  cells {score.cells}  right {score.right}  "
        f"{100 * _share(score.right, score.cells):6.2f}%"
    )


def _gate(name: str, right: int, total: int, threshold: float) -> tuple[str, bool]:
    met = _share(right, total) >= threshold
    return (
        f"  {name:<7}  {right} of {total}  {'meets' if met else 'below'} {100 * threshold:.2f}%",
        met,
    )


def report(
    files: Sequence[tuple[ExpectedFile, Mapping[StatementType, Statement]]],
) -> tuple[list[str], int]:
    """The lines to print and the exit code: 1 only when a checked file misses a G1 threshold."""
    by_status: dict[str, dict[str, Score]] = {"checked": {}, "draft": {}}
    for expected, statements in files:
        for s in expected.statements:
            modes = by_status[expected.status]
            modes[s.page_mode] = modes.get(s.page_mode, Score()) + score_statement(
                s, statements.get(s.type)
            )
    checked = sum(1 for f, _ in files if f.status == "checked")
    lines = [f"checked files: {checked}"]
    failed = False
    if not checked:
        lines.append("G1 not measured: no expected file is checked yet")
    else:
        total = Score()
        for mode, threshold in (("digital", DIGITAL), ("scanned", SCANNED)):
            score = by_status["checked"].get(mode)
            if score is None or not score.cells:
                lines.append(f"  {mode:<7}  no cells")
                continue
            total = total + score
            met = _share(score.right, score.cells) >= threshold
            failed = failed or not met
            lines.append(
                _mode_line(mode, score) + f"  {'meets' if met else 'below'} {100 * threshold:.2f}%"
            )
        for name, right, count, threshold in (
            ("periods", total.periods_right, total.periods, PERIODS),
            ("sign", total.sign_right, total.sign_cells, SIGN),
            ("units", total.metadata_right, total.metadata, METADATA),
        ):
            line, met = _gate(name, right, count, threshold)
            failed = failed or not met
            lines.append(line)
        lines.append(f"  unconfirmed {total.unconfirmed}  extra rows {total.extra_rows}")
    if by_status["draft"]:
        lines.append("provisional (drafts, confirmed cells only; not a gate)")
        for mode in ("digital", "scanned"):
            score = by_status["draft"].get(mode)
            if score is not None:
                lines.append(
                    _mode_line(mode, score)
                    + f"  unconfirmed {score.unconfirmed}  extra rows {score.extra_rows}"
                )
    return lines, 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(prog="eval/harness/extraction.py").parse_args(argv)
    config = load_config()
    entries = {
        d["id"]: d for d in yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["documents"]
    }
    files: list[tuple[ExpectedFile, dict[StatementType, Statement]]] = []
    rows = []
    for path in sorted(EXPECTED_DIR.glob("*.json")):
        expected = load_expected(path)
        result = structure_pdf(MANIFEST.parent / entries[expected.id]["file"], config, None)
        found: dict[StatementType, Statement] = {}
        for extracted in result.statements:
            found.setdefault(extracted.type, extracted)
        files.append((expected, found))
        for s in expected.statements:
            score = score_statement(s, found.get(s.type))
            rows.append(
                {
                    "id": expected.id,
                    "status": expected.status,
                    "type": s.type.value,
                    "page_mode": s.page_mode,
                    **asdict(score),
                }
            )
            print(
                f"{expected.id:34} {expected.status:8} {s.type.value:22} {s.page_mode:8} "
                f"cells {score.cells:3}  right {score.right:3}  unconfirmed {score.unconfirmed:3}"
            )
    lines, code = report(files)
    print("\n".join(lines))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "extraction-golden.json").write_text(
        json.dumps({"statements": rows, "report": lines}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return code


if __name__ == "__main__":
    sys.exit(main())
