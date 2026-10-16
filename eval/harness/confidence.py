"""Which figures would be shown as confident, and which of those are wrong (D20).

Decision D20 (docs/blueprint/06-decisions-and-risks.md) replaces the 98% bar for scanned
Arabic cells with a stricter promise: no wrong figure is shown as confident. Every confirmed
figure of an expected statement gets one outcome:

  right_accepted       right, on a statement that passed review, with no critical cell flag
  right_held           right, but the statement is held for review (the cost of holding)
  wrong_flagged        wrong, and the cell itself carries a critical flag
  wrong_held           wrong, unflagged, on a statement held for review
  wrong_not_extracted  wrong because the statement was not extracted at all
  wrong_confident      wrong, on a statement that passed review, with no critical cell flag

``wrong_confident`` breaks the promise: it is the figure a user would take as checked. A
missing reading on a passed statement counts here too, since the statement vouches for rows it
does not have. Holding everything would also give zero, so ``accepted`` (the share of figures
on passed statements) is reported beside it. Emitted numeric cells outside the confirmed
alignment are reported separately, never added to the accuracy denominator or called wrong
without a reference. Confident unassessed output withholds a D20 pass.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal, get_args

from fra_core.schemas import Statement
from fra_ingest.review import StatementReview, is_critical
from harness.expected import ExpectedStatement
from harness.extraction import CellJudgement, judge_cells

Outcome = Literal[
    "right_accepted",
    "right_held",
    "wrong_flagged",
    "wrong_held",
    "wrong_not_extracted",
    "wrong_confident",
]
OUTCOMES: tuple[Outcome, ...] = get_args(Outcome)


def cell_outcome(
    cell: CellJudgement, statement: Statement | None, review: StatementReview | None
) -> Outcome:
    """The outcome of one judged figure. ``review`` is the review of ``statement``; a statement
    without one is an error, never taken as passed or held."""
    if statement is None:
        return "wrong_not_extracted"
    if review is None or review.statement_id != statement.id:
        msg = f"statement {statement.id!r} has no review"
        raise ValueError(msg)
    read = (
        next((c for c in cell.item.cells if c.period_key == cell.period_key), None)
        if cell.item is not None
        else None
    )
    flagged = read is not None and is_critical(read)
    if review.status == "passed" and not flagged:
        return "right_accepted" if cell.right else "wrong_confident"
    if cell.right:
        return "right_held"
    return "wrong_flagged" if flagged else "wrong_held"


def statement_outcomes(
    expected: ExpectedStatement, statement: Statement | None, review: StatementReview | None
) -> tuple[dict[Outcome, int], list[dict[str, str]]]:
    """Counts per outcome over the statement's confirmed figures (every outcome present, zero
    included), and the confident wrong ones, each with what was expected and what was read."""
    counts = dict.fromkeys(OUTCOMES, 0)
    confident = []
    for cell in judge_cells(expected, statement):
        outcome = cell_outcome(cell, statement, review)
        counts[outcome] += 1
        if outcome == "wrong_confident":
            confident.append(
                {
                    "statement": expected.type.value,
                    "label": cell.row.label,
                    "period": cell.period_key,
                    "expected": "" if cell.expected is None else str(cell.expected),
                    "read": "" if cell.read is None else str(cell.read),
                    "label_read": cell.item.raw_label if cell.item is not None else "",
                }
            )
    return counts, confident


def output_coverage(
    expected: ExpectedStatement | None,
    statement: Statement,
    review: StatementReview | None,
    statement_position: int,
) -> dict[str, Any]:
    """Account for each emitted numeric cell by position, including repeated rows/cells.

    Only the first cell read for a confirmed expected figure is assessed. A duplicate object
    or id at another position is still separate output. A missing reference assesses nothing.
    """
    if review is None or review.statement_id != statement.id:
        raise ValueError(f"statement {statement.id!r} has no review")
    assessed = (
        {
            (c.item_position, c.cell_position)
            for c in judge_cells(expected, statement)
            if c.item_position is not None and c.cell_position is not None
        }
        if expected is not None
        else set()
    )
    emitted = 0
    unassessed = []
    for row_position, item in enumerate(statement.line_items):
        for cell_position, cell in enumerate(item.cells):
            if cell.reported is None:
                continue
            emitted += 1
            if (row_position, cell_position) in assessed:
                continue
            unassessed.append(
                {
                    "statement": statement.type.value,
                    "statement_id": statement.id,
                    "statement_position": statement_position,
                    "row_position": row_position,
                    "cell_position": cell_position,
                    "label": item.raw_label,
                    "period": cell.period_key,
                    "read": str(cell.reported),
                    "confident": review.status == "passed" and not is_critical(cell),
                }
            )
    return {
        "emitted_numeric": emitted,
        "assessed_numeric": emitted - len(unassessed),
        "unassessed_numeric": len(unassessed),
        "unassessed_confident": sum(1 for c in unassessed if c["confident"]),
        "unassessed": unassessed,
    }


def confidence_part(
    documents: Sequence[Mapping[str, Any]], gated: Mapping[tuple[str, str], bool]
) -> dict[str, dict[str, Any]]:
    """Outcomes summed per ``"<status> <page mode> <language>"`` over the documents' figures.
    ``met`` requires no confident wrong or unassessed numeric output on checked gated files.
    Unreferenced gated output can fail, but cannot certify a pass; drafts stay descriptive.
    Accuracy and accepted share retain their confirmed-expected-cell denominator."""
    out: dict[str, dict[str, Any]] = {}
    for d in documents:
        for f in [*d["figures"], *d.get("output_coverage", [])]:
            key = f"{f['status']} {f['page_mode']} {d['language']}"
            block = out.setdefault(
                key,
                {
                    "status": f["status"],
                    "page_mode": f["page_mode"],
                    "language": d["language"],
                    "files": set(),
                    **dict.fromkeys(OUTCOMES, 0),
                    "confident_wrong": [],
                    "emitted_numeric": 0,
                    "assessed_numeric": 0,
                    "unassessed_numeric": 0,
                    "unassessed_confident": 0,
                    "unassessed": [],
                },
            )
            block["files"].add(d["id"])
            for outcome in OUTCOMES:
                block[outcome] += f.get("outcomes", {}).get(outcome, 0)
            block["confident_wrong"] += [{"id": d["id"], **c} for c in f.get("confident_wrong", [])]
            for name in (
                "emitted_numeric",
                "assessed_numeric",
                "unassessed_numeric",
                "unassessed_confident",
            ):
                block[name] += f.get(name, 0)
            block["unassessed"] += [{"id": d["id"], **c} for c in f.get("unassessed", [])]
    for block in out.values():
        cells = sum(block[o] for o in OUTCOMES)
        accepted = block["right_accepted"] + block["wrong_confident"]
        block["files"] = len(block["files"])
        block["cells"] = cells
        block["accepted_share"] = round(accepted / cells, 4) if cells else None
        is_gated = block["status"] in {"checked", "unreferenced"} and gated.get(
            (block["page_mode"], block["language"]), False
        )
        unsafe = block["wrong_confident"] > 0 or block["unassessed_confident"] > 0
        block["met"] = not unsafe if is_gated and (cells or block["unassessed_confident"]) else None
        if block["unassessed_confident"]:
            block["reason"] = "confident unassessed numeric output; safety is not established"
        elif block["wrong_confident"]:
            block["reason"] = "wrong expected figures shown as confident"
        elif not cells:
            block["reason"] = "no confirmed expected figures; safety is not measured"
        else:
            block["reason"] = "no confident wrong or unassessed numeric output"
    return dict(sorted(out.items()))
