"""Stress the extraction eval's label rule on the expected files (spec 12, Known limits).

    PYTHONPATH=eval uv run python -m harness.label_stress

Three questions, each over every labelled row of every expected statement. A correct figure
whose own label has one character corrupted: how often is it judged mislabelled? A figure
under its neighbour's label, as printed and with one or two characters corrupted: how often is
it judged right? A cell holding the row's label merged with its neighbour's: how often is the
figure judged wrong?
"""

from __future__ import annotations

import sys

from harness.expected import EXPECTED_DIR, load_expected
from harness.extraction import mislabelled


def corrupt(label: str, position: int) -> str | None:
    """The label with one character replaced by another of its script; None for a space."""
    char = label[position]
    if char.isspace():
        return None
    if "؀" <= char <= "ۿ":
        swap = "ظ" if char != "ظ" else "ض"
    else:
        swap = "x" if char != "x" else "z"
    return label[:position] + swap + label[position + 1 :]


def main() -> int:
    own_wrong = own_total = rows = rows_hit = 0
    shifted = {0: [0, 0], 1: [0, 0], 2: [0, 0]}  # corrupted characters: [judged right, total]
    merged_wrong = merged_total = 0
    for path in sorted(EXPECTED_DIR.glob("*.json")):
        for statement in load_expected(path).statements:
            for i, row in enumerate(statement.rows):
                if not row.label.strip():
                    continue
                rows += 1
                hit = False
                for position in range(len(row.label)):
                    noisy = corrupt(row.label, position)
                    if noisy is None:
                        continue
                    own_total += 1
                    if mislabelled(row, noisy, statement.rows):
                        own_wrong += 1
                        hit = True
                rows_hit += hit
                for j in (i - 1, i + 1):
                    if not 0 <= j < len(statement.rows):
                        continue
                    other = statement.rows[j].label
                    if not other.strip() or other == row.label:
                        continue
                    variants: dict[int, list[str]] = {0: [other], 1: [], 2: []}
                    for position in range(len(other)):
                        once = corrupt(other, position)
                        if once is None:
                            continue
                        variants[1].append(once)
                        second = (position + len(other) // 2) % len(other)
                        twice = corrupt(once, second) if second != position else None
                        if twice is not None:
                            variants[2].append(twice)
                    for level, labels in variants.items():
                        for label in labels:
                            shifted[level][1] += 1
                            shifted[level][0] += not mislabelled(row, label, statement.rows)
                    both = f"{row.label} {other}" if j > i else f"{other} {row.label}"
                    merged_total += 1
                    merged_wrong += mislabelled(row, both, statement.rows)
    print(
        f"own label, one character corrupted, judged wrong: {own_wrong} of {own_total} "
        f"({100 * own_wrong / own_total:.2f}%), on {rows_hit} of {rows} rows"
    )
    for level, (right, total) in shifted.items():
        print(
            f"neighbour's label, {level} characters corrupted, judged right: {right} of {total} "
            f"({100 * right / total:.2f}%)"
        )
    print(f"label merged with its neighbour's, judged wrong: {merged_wrong} of {merged_total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
