"""Report the label mapping over the golden set (plan week 2, Task 4).

    uv run python eval/harness/mapping.py

For every critical item of the taxonomy (``critical: true``, the one definition the golden test
shares) in each document's balance sheet and income statement: mapped, flagged unmapped, flagged
ambiguous, or statement not found. Of the mapped rows, what an expected file says about each:

  verified      the expected row's label names the item and the row was mapped by an anchor, so
                the two readings do not share the lexicon
  consistent    the expected label names the item through the same lexicon that mapped the row.
                That is a circular check, so it is not called verified
  no verdict    no expected file, a blank expected label, a label the lexicon does not read, or
                figures that no expected row (or a different number of rows) prints
  disagrees     the expected row of the figures is another item. This fails the run

The expected row of a mapped row is the one that prints its figures, paired by position among
the rows that share them: total assets and total liabilities and equity print one figure, and
position tells which is which. A figure on no expected row is a misread figure, which the
extraction eval scores, and is counted apart from the mapping. Writes var/eval/mapping-golden.json.

The rates are in-sample: aliases were taken from these documents. Every golden document is
annual, so interim statements are not measured (D18).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from typing import Literal

import yaml

from fra_core.schemas import MappingSource, Period, Statement, StatementType
from fra_core.taxonomy.loader import load_taxonomy
from fra_ingest.config import REPO_ROOT, load_config
from fra_ingest.errors import IngestError
from fra_ingest.label_mapping import MAPPED_TYPES
from fra_ingest.label_match import LabelIndex
from fra_ingest.structure import structure_pdf
from harness.expected import EXPECTED_DIR, ExpectedFile, ExpectedRow, load_expected

MANIFEST = REPO_ROOT / "eval" / "golden" / "manifest.yaml"
OUT = REPO_ROOT / "var" / "eval"
IN_SAMPLE_NOTE = (
    "These rates are in-sample: the aliases were taken from these documents. All golden "
    "documents are annual, so interim statements are not measured (D18)."
)
SHARED = "figures shared by a different number of rows than the expected file prints"
_ID = re.compile(r"[a-z][a-z_]*")
VerdictKind = Literal["verified", "consistent", "no_verdict", "disagrees"]


@dataclass(frozen=True)
class Verdict:
    kind: VerdictKind
    reason: str = ""


def slot_state(statement: Statement | None, item_id: str) -> str:
    """Mapped when a row carries the item. Otherwise ambiguous when a flagged row names the item
    among its candidates (by exact id), else unmapped."""
    if statement is None:
        return "statement_not_found"
    if any(i.canonical_id == item_id for i in statement.line_items):
        return "mapped"
    named = (
        i.mapping_flag == "ambiguous" and item_id in _ID.findall(i.mapping_evidence or "")
        for i in statement.line_items
    )
    return "ambiguous" if any(named) else "unmapped"


def verdict(
    statement: Statement, item_id: str, expected: ExpectedFile | None, index: LabelIndex
) -> Verdict:
    """What the expected file says about the one row mapped to this item."""
    if expected is None:
        return Verdict("no_verdict", "no expected file")
    found = [e for e in expected.statements if e.type is statement.type]
    row = statement.find(item_id)
    if not found or row is None:
        return Verdict("no_verdict", "no expected statement")
    figures = {c.period_key: c.reported for c in row.cells if c.reported is not None}
    sharing = [
        i
        for i in statement.line_items
        if {c.period_key: c.reported for c in i.cells if c.reported is not None} == figures
    ]
    same: list[ExpectedRow] = [
        r for r in found[0].rows if all(r.values.get(k) == v for k, v in figures.items())
    ]
    if not same:
        return Verdict("no_verdict", "figures on no expected row")
    if len(same) != len(sharing):
        return Verdict("no_verdict", SHARED)
    label = same[sharing.index(row)].label
    if not label.strip():
        return Verdict("no_verdict", "blank expected label")
    named = {i.id for i in index.match_all(label, statement.type)}
    if not named:
        return Verdict("no_verdict", "expected label not in the lexicon")
    if item_id not in named:
        return Verdict("disagrees", f"expected row {label!r} is {sorted(named)}")
    if row.mapping_source is MappingSource.ANCHOR:
        return Verdict("verified")
    return Verdict("consistent")


def document_period_kind(statements: list[Statement]) -> str:
    """annual when a statement has a 12 month period, interim for a shorter one."""
    periods: list[Period] = [p for s in statements for p in s.periods]
    if any(p.is_annual for p in periods):
        return "annual"
    if any(p.months is not None and p.months < 12 for p in periods):
        return "interim"
    return "unknown"


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(prog="eval/harness/mapping.py").parse_args(argv)
    config = load_config()
    taxonomy = load_taxonomy()
    documents = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["documents"]
    index = LabelIndex(taxonomy)
    failures: list[str] = []
    total: Counter[str] = Counter()
    reports: list[dict[str, object]] = []
    for entry in documents:
        doc_id = entry["id"]
        try:
            result = structure_pdf(MANIFEST.parent / entry["file"], config, None, use_cache=False)
        except IngestError as exc:
            print(f"{doc_id}: error {exc.reason}")
            failures.append(f"{doc_id}: {exc.reason}")
            continue
        first: dict[StatementType, Statement] = {}
        for s in result.statements:
            first.setdefault(s.type, s)
        path = EXPECTED_DIR / f"{doc_id}.json"
        expected = load_expected(path) if path.is_file() else None
        counts: Counter[str] = Counter()
        slots: list[dict[str, str]] = []
        for kind in MAPPED_TYPES:
            statement = first.get(kind)
            for item_id in taxonomy.critical_ids(kind):
                state = slot_state(statement, item_id)
                counts[state] += 1
                slot = {"statement": kind.value, "item": item_id, "state": state}
                if state == "mapped" and statement is not None:
                    found = verdict(statement, item_id, expected, index)
                    counts[found.kind] += 1
                    if found.kind == "no_verdict":
                        counts[f"no_verdict:{found.reason}"] += 1
                    if found.reason == "figures on no expected row":
                        counts["misread_figures"] += 1
                    if found.kind == "disagrees":
                        failures.append(f"{doc_id} {kind.value} {item_id}: {found.reason}")
                    slot["verdict"] = found.kind
                    slot["reason"] = found.reason
                slots.append(slot)
        language = entry["language"]
        period_kind = document_period_kind(list(first.values()))
        print(f"{doc_id} ({language}, {period_kind})")
        print("  " + " ".join(f"{k} {v}" for k, v in sorted(counts.items())))
        total.update(counts)
        reports.append(
            {
                "id": doc_id,
                "language": language,
                "period_kind": period_kind,
                "counts": dict(counts),
                "slots": slots,
            }
        )
    slots_total = sum(total[k] for k in ("mapped", "unmapped", "ambiguous", "statement_not_found"))
    print(f"\n{slots_total} critical slots over {len(reports)} documents")
    for key in ("mapped", "unmapped", "ambiguous", "statement_not_found"):
        print(f"  {key.replace('_', ' ')}: {total[key]}")
    print("of the mapped:")
    print(f"  verified (expected label read apart from the lexicon): {total['verified']}")
    print(f"  consistent with the lexicon (circular, not verified): {total['consistent']}")
    print(f"  no verdict: {total['no_verdict']}")
    for key in sorted(k for k in total if k.startswith("no_verdict:")):
        print(f"    {key.removeprefix('no_verdict:')}: {total[key]}")
    print(f"  disagrees: {total['disagrees']}")
    print(
        f"{total['misread_figures']} mapped rows print figures that are on no expected row "
        "(a misread figure for the extraction eval, not a mapping error)"
    )
    print(IN_SAMPLE_NOTE)
    print("PASS" if not failures else "FAIL\n  " + "\n  ".join(failures))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "mapping-golden.json").write_text(
        json.dumps(
            {
                "documents": reports,
                "totals": dict(total),
                "note": IN_SAMPLE_NOTE,
                "failures": failures,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
