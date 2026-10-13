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

Only ``wrong_confident`` breaks the promise: it is the figure a user would take as checked. A
missing reading on a passed statement counts here too, since the statement vouches for rows it
does not have. Holding everything would also give zero, so ``accepted`` (the share of figures
on passed statements) is reported beside it.
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


def confidence_part(
    documents: Sequence[Mapping[str, Any]], gated: Mapping[tuple[str, str], bool]
) -> dict[str, dict[str, Any]]:
    """Outcomes summed per ``"<status> <page mode> <language>"`` over the documents' figures.
    ``met`` is whether no figure is wrong_confident, set only for the (page mode, language)
    pairs in ``gated`` on checked files, None elsewhere and when there are no figures."""
    out: dict[str, dict[str, Any]] = {}
    for d in documents:
        for f in d["figures"]:
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
                },
            )
            block["files"].add(d["id"])
            for outcome in OUTCOMES:
                block[outcome] += f["outcomes"][outcome]
            block["confident_wrong"] += [{"id": d["id"], **c} for c in f["confident_wrong"]]
    for block in out.values():
        cells = sum(block[o] for o in OUTCOMES)
        accepted = block["right_accepted"] + block["wrong_confident"]
        block["files"] = len(block["files"])
        block["cells"] = cells
        block["accepted_share"] = round(accepted / cells, 4) if cells else None
        is_gated = block["status"] == "checked" and gated.get(
            (block["page_mode"], block["language"]), False
        )
        block["met"] = (block["wrong_confident"] == 0) if is_gated and cells else None
    return dict(sorted(out.items()))
