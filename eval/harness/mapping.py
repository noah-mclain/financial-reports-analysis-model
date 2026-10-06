"""Report the label mapping over the golden set (plan week 2, Task 4).

    make eval-mapping                          the golden set
    make eval-mapping TARGET=fit [LIMIT=n]     the fit documents of the train pool

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

Fit mode (``fit``): the fit documents after the recorded moves, in id order (the first ``LIMIT``),
read from `fra_core.split.development_documents`, which cannot return the holdout. Every row the
mapping left unmapped or ambiguous is listed by failure class (``harness.unmapped``), with counts
per language and per period kind (D18) and the most frequent labels per class, in
var/eval/mapping-fit.json. Labels are printed: fit documents are development material. A document
with no PDF in the corpus store is counted and listed, not fetched; negative controls are not
mapped.

The rates are in-sample: aliases were taken from these documents. Every golden document is
annual, so interim statements are not measured (D18).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from fra_core.schemas import MappingSource, Period, Statement, StatementType
from fra_core.split import Part
from fra_core.taxonomy.loader import Taxonomy, load_taxonomy
from fra_ingest.config import REPO_ROOT, load_config
from fra_ingest.errors import IngestError
from fra_ingest.label_mapping import MAPPED_TYPES
from fra_ingest.label_match import LabelIndex
from fra_ingest.ocr import make_engine
from fra_ingest.results import StructureResult
from fra_ingest.structure import structure_pdf
from harness.development import development_set, positive_int
from harness.expected import EXPECTED_DIR, ExpectedFile, ExpectedRow, load_expected
from harness.paths import (
    CANDIDATES,
    CORPUS,
    HOLDOUT_MOVES,
    NEGATIVE_CONTROL,
    SCORING_LOG,
    STRATA,
)
from harness.structure import first_statements
from harness.unmapped import flagged_rows, summarize

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


def critical_slots(
    first: Mapping[StatementType, Statement],
    expected: ExpectedFile | None,
    index: LabelIndex,
    taxonomy: Taxonomy,
) -> tuple[Counter[str], list[dict[str, str]], list[str]]:
    """The state of every critical slot of a document (``slot_state``), what the expected file
    says about each mapped one (``verdict``), and a line for each disagreement."""
    counts: Counter[str] = Counter()
    slots: list[dict[str, str]] = []
    disagreements: list[str] = []
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
                    disagreements.append(f"{kind.value} {item_id}: {found.reason}")
                slot["verdict"] = found.kind
                slot["reason"] = found.reason
            slots.append(slot)
    return counts, slots, disagreements


def document_period_kind(statements: list[Statement]) -> str:
    """annual when a statement has a 12 month period, interim for a shorter one."""
    periods: list[Period] = [p for s in statements for p in s.periods]
    if any(p.is_annual for p in periods):
        return "annual"
    if any(p.months is not None and p.months < 12 for p in periods):
        return "interim"
    return "unknown"


SLOT_STATES = ("mapped", "unmapped", "ambiguous", "statement_not_found")


def fit_report(
    entries: Sequence[Mapping[str, Any]],
    pdf_of: Callable[[str], Path],
    structure: Callable[[Path], StructureResult],
    index: LabelIndex,
    taxonomy: Taxonomy,
    limit: int | None = None,
) -> dict[str, Any]:
    """The unmapped and ambiguous rows of these documents by failure class. The first `limit`
    documents in id order are taken; one with no PDF is counted and listed, never fetched."""
    chosen = sorted(entries, key=lambda e: str(e["id"]))
    controls = [e for e in chosen if e.get("role") == NEGATIVE_CONTROL]
    chosen = [e for e in chosen if e.get("role") != NEGATIVE_CONTROL][:limit]
    missing: list[str] = []
    errored: list[dict[str, str]] = []
    documents: list[dict[str, Any]] = []
    slots: dict[str, Counter[str]] = {k: Counter() for k in (*STRATA, "total")}
    for e in chosen:
        pdf = pdf_of(str(e["id"]))
        if not pdf.is_file():
            missing.append(str(e["id"]))
            continue
        try:
            result = structure(pdf)
        except IngestError as exc:
            errored.append({"id": str(e["id"]), "reason": exc.reason})
            continue
        found = first_statements(result, MAPPED_TYPES)
        documents.append(
            {
                "id": e["id"],
                "language": e["language"],
                "period": e["period"],
                "flagged": [row for s in found.values() for row in flagged_rows(s, index)],
            }
        )
        states = Counter(
            {
                k: v
                for k, v in critical_slots(found, None, index, taxonomy)[0].items()
                if k in SLOT_STATES
            }
        )
        slots[str(e["period"])].update(states)
        slots["total"].update(states)
        print(f"{e['id']}: {len(documents[-1]['flagged'])} flagged rows")
    return {
        "target": "fit",
        "documents_selected": len(chosen),
        "documents_run": len(documents),
        "missing": missing,
        "errored": errored,
        "negative_controls_skipped": len(controls),
        "critical_slots": {k: dict(v) for k, v in slots.items()},
        "failure_classes": summarize(documents),
        "documents": documents,
    }


def run_fit(
    limit: int | None,
    candidates: Path = CANDIDATES,
    moves: Path = HOLDOUT_MOVES,
    log: Path = SCORING_LOG,
    store: Path = CORPUS,
) -> int:
    config = load_config()
    ocr = make_engine(config)
    taxonomy = load_taxonomy()
    report = fit_report(
        development_set({Part.FIT}, candidates, moves, log),
        lambda doc_id: store / "train" / f"{doc_id}.pdf",
        lambda pdf: structure_pdf(pdf, config, ocr),
        LabelIndex(taxonomy),
        taxonomy,
        limit,
    )
    print(
        f"\n{report['documents_run']} fit documents run, {len(report['missing'])} with no PDF, "
        f"{len(report['errored'])} unreadable, "
        f"{report['negative_controls_skipped']} negative controls not mapped"
    )
    for name, entry in report["failure_classes"]["classes"].items():
        print(f"  {name}: {entry['count']}  {entry['by_language']}  {entry['by_period_kind']}")
        for label, n in entry["top_labels"]:
            print(f"      {n:4} {label}")
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / "mapping-fit.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {out.relative_to(REPO_ROOT)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m harness.mapping")
    parser.add_argument("target", nargs="?", choices=("golden", "fit"), default="golden")
    parser.add_argument(
        "--limit", type=positive_int, help="fit only: the first n documents in id order"
    )
    args = parser.parse_args(argv)
    if args.target == "fit":
        return run_fit(args.limit)
    if args.limit is not None:
        parser.error("--limit applies to fit only")
    return run_golden()


def run_golden() -> int:
    config = load_config()
    ocr = make_engine(config)
    taxonomy = load_taxonomy()
    documents = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["documents"]
    index = LabelIndex(taxonomy)
    failures: list[str] = []
    total: Counter[str] = Counter()
    reports: list[dict[str, object]] = []
    for entry in documents:
        doc_id = entry["id"]
        try:
            result = structure_pdf(MANIFEST.parent / entry["file"], config, ocr, use_cache=False)
        except IngestError as exc:
            print(f"{doc_id}: error {exc.reason}")
            failures.append(f"{doc_id}: {exc.reason}")
            continue
        first: dict[StatementType, Statement] = {}
        for s in result.statements:
            first.setdefault(s.type, s)
        path = EXPECTED_DIR / f"{doc_id}.json"
        expected = load_expected(path) if path.is_file() else None
        counts, slots, disagreements = critical_slots(first, expected, index, taxonomy)
        failures.extend(f"{doc_id} {d}" for d in disagreements)
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
