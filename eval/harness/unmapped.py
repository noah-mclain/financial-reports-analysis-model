"""Failure classes of the rows the label mapping left unmapped or ambiguous.

One place defines the classes. A class is decided mechanically: from the evidence code the
mapper wrote on the row (``fra_ingest.label_mapping._resolve`` and ``_settle_duplicates``), and,
for a row with no alias and no anchor, from the label itself (blank, no letters, an exact prefix
of an alias). Nothing is classified by hand. A row flagged with a code this module does not know
is an error that names it, so a new mapper outcome cannot be filed under another class.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from fra_core.labels import normalize_label
from fra_core.schemas import LineItem, Statement, StatementType
from fra_ingest.label_match import LabelIndex
from harness.paths import STRATA

NO_EVIDENCE = "no_alias_no_anchor"
# Evidence codes of the mapper, each a class of its own.
EVIDENCE_CLASSES = (
    "alias_multiple",
    "anchor_conflict",
    "alias_total_unconfirmed",
    "repeat_of",
    "duplicate",
)
# What a row with no alias and no anchor is, read from its label, most specific first.
LABEL_CLASSES = ("blank_label", "no_letters", "near_alias_prefix")
FAILURE_CLASSES = (*LABEL_CLASSES, NO_EVIDENCE, *EVIDENCE_CLASSES)
TOP_LABELS = 10


def failure_class(item: LineItem, statement_type: StatementType, index: LabelIndex) -> str:
    if item.mapping_flag is None:
        raise ValueError(f"{item.id}: not flagged, so it has no failure class")
    if not item.mapping_evidence:
        raise ValueError(f"{item.id}: flagged {item.mapping_flag} with no evidence code")
    code = item.mapping_evidence.split(":")[0]
    if code in EVIDENCE_CLASSES:
        return code
    if code != NO_EVIDENCE:
        raise ValueError(f"{item.id}: unknown mapping evidence code {code!r}")
    if not item.raw_label.strip():
        return "blank_label"
    if not any(c.isalpha() for c in item.raw_label):
        return "no_letters"
    if index.prefix_matches(item.raw_label, statement_type):
        return "near_alias_prefix"
    return NO_EVIDENCE


def flagged_rows(statement: Statement, index: LabelIndex) -> list[dict[str, str]]:
    """The valued rows of a statement the mapping flagged, each with its class."""
    return [
        {
            "statement": statement.type.value,
            "label": i.raw_label,
            "class": failure_class(i, statement.type, index),
        }
        for i in statement.line_items
        if i.cells and i.mapping_flag is not None
    ]


def summarize(documents: Sequence[Mapping[str, Any]], top: int = TOP_LABELS) -> dict[str, Any]:
    """Counts per class, per language and per period kind (D18), and the most frequent labels of
    each class. A document is {id, language, period, flagged: [{label, class, ...}]}."""
    classes: dict[str, dict[str, Any]] = {
        c: {
            "count": 0,
            "by_language": Counter[str](),
            "by_period_kind": Counter[str](dict.fromkeys(STRATA, 0)),
            "labels": Counter[str](),
        }
        for c in FAILURE_CLASSES
    }
    for d in documents:
        for row in d["flagged"]:
            entry = classes[row["class"]]
            entry["count"] += 1
            entry["by_language"][d["language"]] += 1
            entry["by_period_kind"][d["period"]] += 1
            entry["labels"][normalize_label(row["label"])] += 1
    return {
        "documents": {
            "total": len(documents),
            "by_language": dict(Counter(d["language"] for d in documents)),
            "by_period_kind": {s: sum(1 for d in documents if d["period"] == s) for s in STRATA},
            "with_flagged_rows": sum(1 for d in documents if d["flagged"]),
        },
        "classes": {
            c: {
                "count": e["count"],
                "by_language": dict(e["by_language"]),
                "by_period_kind": dict(e["by_period_kind"]),
                "top_labels": [[label, n] for label, n in e["labels"].most_common(top)],
            }
            for c, e in classes.items()
        },
    }
