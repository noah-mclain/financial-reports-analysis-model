"""Gate A and Gate B on the golden set (`dev`), per period kind beside the total.

    make eval-gates        (PYTHONPATH=eval uv run python -m harness.gates)

Definitions are the build plan's (docs/blueprint/07-build-plan.md, Gates) with the extraction
thresholds of G1 (docs/blueprint/04-execution-phases.md; the values are `harness.extraction`'s
DIGITAL and SCANNED, the one source), applied to what the harness already measures:

Gate A, extraction.
  figures         cells of the expected files read right (`extraction.score_statement`). These are
                  cell shares over n expected files, with a 95% Wilson interval, not a verdict on
                  the gate: G1's thresholds apply to checked files, and a draft file is provisional
                  and judged by nothing. A share over no cells is not measured. Scanned cells are
                  split by document language; scanned Arabic has no accuracy bar (D20) and its
                  share is reported only.
  confident       every confirmed figure as accepted, held or confident wrong
                  (`harness.confidence`), per status, page mode and language. For scanned Arabic
                  on checked files this is the part D20 gates: no figure wrong and shown as
                  confident. The share of figures on passed statements is reported beside it.
  pairs           the gated language pair (Almarai) has no value row without a counterpart
                  (`structure.pair_misses`): 100% of matched rows. Over the statement types the
                  gate text names, financial position and profit or loss (`PAIR_TYPES`). A pair
                  belongs to the period kind of its first (English) document, `unknown` when that
                  document is unreadable.
  identity        the balance sheet identity holds within tolerance on every document, or fails
                  on a statement held for review with a cell on the failed rows that explains it
                  (`structure.identity_accepted`). That is explained by rule, not reviewed by a
                  person. A document with no balance sheet, or a skipped check, is not met.
  scale_currency  right, or flagged unsure by its own flag, on every statement expected: one of
                  each enabled type per document. A statement not found is counted and makes the
                  part not met.
Gate B, mapping. The gate text: every critical item is "mapped ... or explicitly flagged as
  unmapped. No silent wrong mapping." Each critical slot of each document is one of: mapped;
  ambiguous (a flagged row names the item); explicitly flagged (a valid named item finding
  covers all observed statement periods); ABSENT (no canonical row or matching flag: silent,
  not met); or statement not found (not met). Findings account for unmapped items, without
  improving mapping accuracy or providing a verdict. Of the mapped slots,
  `mapping.verdict` says which an expected file confirms (verified, consistent with the lexicon,
  or no verdict); "0 disagree" applies only to slots with a verdict.

Any document that could not be read makes every part of every stratum not met, and is listed:
its period kind is unknown, so it cannot be placed in one. Every figure is marked "not yet
measured on model_test": the golden set is in-sample (aliases were taken from it), and model_test
waits for the checkpoint (D13). A stratum with no documents is named, not filled: the golden set
is annual only (D18). Writes var/eval/gates-dev.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from fra_core.schemas import CheckResult, Statement, StatementType, TextSource
from fra_core.taxonomy.loader import Taxonomy, load_taxonomy
from fra_ingest.config import REPO_ROOT, load_config
from fra_ingest.label_match import LabelIndex
from fra_ingest.ocr import make_engine
from fra_ingest.results import StructureResult
from fra_ingest.review import StatementReview
from fra_ingest.structure import structure_pdf
from harness.confidence import OUTCOMES, confidence_part, output_coverage, statement_outcomes
from harness.expected import EXPECTED_DIR, ExpectedFile, load_expected
from harness.extraction import DIGITAL, SCANNED, score_statement, wilson_interval
from harness.failures import DOCUMENT_ERRORS, EngineFailure, EngineGuard, aborted, document_failure
from harness.locate_evidence import (
    compare_evidence,
    document_evidence,
    evidence_from_dict,
    incomplete_evidence,
)
from harness.mapping import SLOT_STATES, critical_slots, document_period_kind
from harness.paths import STRATA
from harness.structure import (
    PAIRS,
    first_statements,
    identity_accepted,
    identity_excuses,
    identity_status,
    load_checks,
    metadata_ok,
    pair_misses,
)

MANIFEST = REPO_ROOT / "eval" / "golden" / "manifest.yaml"
OUT = REPO_ROOT / "var" / "eval"
NOT_MEASURED = "dev only; not yet measured on model_test"
# The cell-accuracy bar of each figures block; None is reported with no bar. Scanned cells are
# split by document language, and scanned Arabic has no accuracy bar (D20).
G1: dict[str, float | None] = {"digital": DIGITAL, "scanned_en": SCANNED, "scanned_ar": None}
# Where D20's "no wrong figure shown as confident" is the verdict: (page mode, language).
CONFIDENT_GATED = {("scanned", "ar"): True}
PAIR_TYPES = (
    StatementType.BALANCE,
    StatementType.INCOME,
)  # "financial position and profit or loss"
IDENTITY_MET = ("ok", "failed_accepted")
EVIDENCE_SCHEMA_VERSION = 2


def identity_outcome(
    statement: Statement | None, checks: list[CheckResult], review: StatementReview | None
) -> str:
    """ok, failed_accepted (failed, explained, held for review), failed_unexplained, skipped, or
    no_balance when there is no balance sheet."""
    if statement is None:
        return "no_balance"
    status, _ = identity_status(checks)
    if status != "failed":
        return status
    excuses = identity_excuses(statement, checks)
    return "failed_accepted" if identity_accepted(excuses, review) else "failed_unexplained"


def document_figures(
    entry: Mapping[str, Any],
    result: StructureResult,
    checks: Mapping[str, list[CheckResult]],
    expected: ExpectedFile | None,
    index: LabelIndex,
    taxonomy: Taxonomy,
    types: Iterable[StatementType],
) -> dict[str, Any]:
    """What one document contributes to the gates. `types` are the statement types whose scale
    and currency are checked."""
    types = tuple(types)
    found = first_statements(result, types)
    reviews: dict[str, StatementReview] = {}
    for review in result.reviews:
        if review.statement_id in reviews and reviews[review.statement_id] != review:
            raise ValueError(f"statement {review.statement_id!r} has ambiguous reviews")
        reviews[review.statement_id] = review
    balance = found.get(StatementType.BALANCE)
    everything = first_statements(result, StatementType)
    counts, slots, disagreements = critical_slots(everything, expected, index, taxonomy)
    counts["absent"] = sum(1 for slot in slots if slot["state"] == "unmapped")
    figures = []
    # The same first-of-type selection as accuracy, tracked by list position rather than id.
    first_positions: dict[StatementType, int] = {}
    for position, statement in enumerate(result.statements):
        first_positions.setdefault(statement.type, position)
    references = {e.type: e for e in expected.statements} if expected else {}
    coverage = []
    for position, statement in enumerate(result.statements):
        reference = references.get(statement.type)
        assessed_reference = reference if first_positions[statement.type] == position else None
        mode = (
            reference.page_mode
            if reference
            else (
                "scanned"
                if entry["text_layer"] != "digital"
                or any(
                    c.provenance.source is TextSource.OCR
                    for item in statement.line_items
                    for c in item.cells
                )
                else "digital"
            )
        )
        coverage.append(
            {
                "status": expected.status if expected else "unreferenced",
                "page_mode": mode,
                "type": statement.type.value,
                **output_coverage(
                    assessed_reference, statement, reviews.get(statement.id), position
                ),
            }
        )
    for e in expected.statements if expected else []:
        extracted = everything.get(e.type)
        score = score_statement(e, extracted)
        outcomes, confident = statement_outcomes(
            e, extracted, reviews.get(extracted.id) if extracted else None
        )
        figures.append(
            {
                "status": expected.status if expected else None,
                "page_mode": e.page_mode,
                "type": e.type.value,
                "cells": score.cells,
                "right": score.right,
                "outcomes": outcomes,
                "confident_wrong": confident,
            }
        )
    return {
        "id": entry["id"],
        "language": entry["language"],
        "period_kind": document_period_kind(list(everything.values())),
        "identity": identity_outcome(
            balance,
            checks.get(balance.id, []) if balance else [],
            reviews.get(balance.id) if balance else None,
        ),
        "scale_currency": {
            "expected": len(types),
            "found": len(found),
            "right": sum(
                metadata_ok(s, entry["scale"], str(entry["currency"])) for s in found.values()
            ),
        },
        "mapping": dict(counts),
        "disagreements": disagreements,
        "figures": figures,
        "output_coverage": coverage,
    }


def _share(right: int, cells: int) -> float | None:
    return round(right / cells, 4) if cells else None


def _unless_unreadable(value: bool | None, unreadable: Sequence[str]) -> bool | None:
    """A part is not met when any document could not be read, whatever the rest says."""
    return False if unreadable else value


def _figures(
    documents: Sequence[Mapping[str, Any]], unreadable: Sequence[str]
) -> dict[str, dict[str, Any]]:
    cells: dict[tuple[str, str], Counter[str]] = {}
    files: dict[tuple[str, str], set[str]] = {}
    for d in documents:
        for f in d["figures"]:
            mode = f["page_mode"] if f["page_mode"] == "digital" else f"scanned_{d['language']}"
            key = (f["status"], mode)
            total = cells.setdefault(key, Counter())
            total["cells"] += f["cells"]
            total["right"] += f["right"]
            files.setdefault(key, set()).add(d["id"])
    out: dict[str, dict[str, Any]] = {"checked": {}, "draft": {}}
    for (status, mode), n in sorted(cells.items()):
        share = _share(n["right"], n["cells"])
        bar = G1[mode]
        met = (
            share >= bar if status == "checked" and share is not None and bar is not None else None
        )
        out[status][mode] = {
            "files": len(files[(status, mode)]),
            "cells": n["cells"],
            "right": n["right"],
            "share": share,
            "met": _unless_unreadable(met, unreadable) if status == "checked" else None,
        }
    return out


def _gate_a(
    documents: Sequence[Mapping[str, Any]],
    pairs: Sequence[Mapping[str, Any]],
    unreadable: Sequence[str],
) -> dict[str, Any]:
    outcomes = Counter(d["identity"] for d in documents)
    expected = sum(d["scale_currency"]["expected"] for d in documents)
    found = sum(d["scale_currency"]["found"] for d in documents)
    right = sum(d["scale_currency"]["right"] for d in documents)
    gated = [p for p in pairs if p["gated"]]
    identical = [p for p in gated if p["misses"] == [0, 0]]
    holds = sum(outcomes[k] for k in IDENTITY_MET)
    return {
        "figures": _figures(documents, unreadable),
        "confident": {
            key: {**block, "met": _unless_unreadable(block["met"], unreadable)}
            if block["met"] is not None
            else block
            for key, block in confidence_part(documents, CONFIDENT_GATED).items()
        },
        "pairs": {
            "gated": len(gated),
            "identical": len(identical),
            "ungated": len(pairs) - len(gated),
            "met": _unless_unreadable(len(identical) == len(gated) if gated else None, unreadable),
        },
        "identity": {
            "counts": dict(outcomes),
            "met": _unless_unreadable(holds == len(documents) if documents else None, unreadable),
        },
        "scale_currency": {
            "expected": expected,
            "found": found,
            "not_found": expected - found,
            "right": right,
            "met": _unless_unreadable(right == expected if expected else None, unreadable),
        },
    }


def _gate_b(documents: Sequence[Mapping[str, Any]], unreadable: Sequence[str]) -> dict[str, Any]:
    total: Counter[str] = Counter()
    for d in documents:
        total.update(d["mapping"])
    slots = sum(total[k] for k in SLOT_STATES)
    # "absent" is the reporting alias of unmapped, not an additional slot state.
    not_accounted = (
        sum(max(d["mapping"].get("absent", 0), d["mapping"].get("unmapped", 0)) for d in documents)
        + total["statement_not_found"]
    )
    return {
        "slots": slots,
        **{
            k: total[k]
            for k in (
                *SLOT_STATES,
                "absent",
                "verified",
                "consistent",
                "no_verdict",
                "disagrees",
            )
        },
        "not_accounted": not_accounted,
        "met": _unless_unreadable(
            (not_accounted == 0 and total["disagrees"] == 0) if documents else None, unreadable
        ),
    }


def gate_report(
    documents: Sequence[Mapping[str, Any]],
    pairs: Sequence[Mapping[str, Any]],
    errors: Sequence[str] = (),
) -> dict[str, Any]:
    """Gate A and B for each period kind and for all documents; `unknown` is always a stratum.
    An unreadable document (`errors`: one line each) has no period kind, so it is listed in every
    stratum and makes every part of it not met."""
    kinds = (*STRATA, "unknown")
    strata: dict[str, Any] = {}
    for name in (*kinds, "total"):
        mine = [d for d in documents if name == "total" or d["period_kind"] == name]
        theirs = [p for p in pairs if name == "total" or p["period_kind"] == name]
        strata[name] = {
            "documents": len(mine),
            "measured_on": NOT_MEASURED,
            "unreadable": list(errors),
            "gate_a": _gate_a(mine, theirs, errors),
            "gate_b": _gate_b(mine, errors),
        }
    return {
        "measured_on": NOT_MEASURED,
        "strata": strata,
        "empty_strata": [k for k in kinds if strata[k]["documents"] == 0],
    }


def pair_records(
    by_id: Mapping[str, Mapping[StatementType, Statement]],
    kinds: Mapping[str, str],
    pairs: Sequence[tuple[str, str, bool]] = PAIRS,
) -> list[dict[str, Any]]:
    """Each language pair over `PAIR_TYPES`: its misses (None when a statement was not found),
    whether it is gated, and the period kind of its first document."""
    records = []
    for left, right, gated in pairs:
        for statement_type in PAIR_TYPES:
            a, b = by_id.get(left, {}).get(statement_type), by_id.get(right, {}).get(statement_type)
            records.append(
                {
                    "pair": f"{left}/{right}",
                    "type": statement_type.value,
                    "gated": gated,
                    "misses": list(pair_misses(a, b)) if a and b else None,
                    "period_kind": kinds.get(left, "unknown"),
                }
            )
    return records


def _met(value: bool | None) -> str:
    return "not measured" if value is None else ("met" if value else "NOT met")


def _interval(right: int, cells: int) -> str:
    low, high = wilson_interval(right, cells)
    return f"95% Wilson {low:.2%} to {high:.2%}"


def _identity_lines(a: Mapping[str, Any], documents: int) -> list[str]:
    counts = Counter(a["identity"]["counts"])
    holds = sum(counts[k] for k in IDENTITY_MET)
    return [
        f"  A identity: {holds} of {documents} documents hold or are explained "
        f"({counts['ok']} ok, {counts['failed_accepted']} failed_accepted: explained by rule, "
        f"not reviewed by a person); {counts['skipped']} skipped, {counts['no_balance']} with "
        f"no balance sheet, {counts['failed_unexplained']} failed unexplained "
        f"{_met(a['identity']['met'])}"
    ]


def _confident_lines(confident: Mapping[str, Mapping[str, Any]]) -> list[str]:
    out = []
    for key, c in confident.items():
        if c["status"] == "draft":
            judged = "provisional"
        elif c["met"] is None:
            judged = "reported"
        else:
            judged = f"{_met(c['met'])} (D20)"
        accepted = "no cells" if c["accepted_share"] is None else f"{c['accepted_share']:.2%}"
        counts = ", ".join(f"{o} {c[o]}" for o in OUTCOMES)
        out.append(
            f"  A confident {key}: {c['wrong_confident']} wrong shown as confident of "
            f"{c['cells']} cells {judged}; on passed statements {accepted} ({counts}; "
            f"n={c['files']} files)"
        )
        out.append(
            f"    output coverage: {c['assessed_numeric']} of {c['emitted_numeric']} emitted "
            f"numeric cells assessed; {c['unassessed_numeric']} unassessed, "
            f"{c['unassessed_confident']} confident unassessed; {c['reason']}"
        )
        out.extend(
            f"    unassessed: {u['id']} {u['statement']}[{u['statement_position']}] "
            f"row {u['row_position']} cell {u['cell_position']} {u['label']!r} "
            f"{u['period']}: read {u['read']}; confident={u['confident']}"
            for u in c["unassessed"]
        )
        out.extend(
            f"    confident wrong: {w['id']} {w['statement']} {w['label']!r} {w['period']}: "
            f"expected {w['expected'] or 'blank'}, read {w['read'] or 'blank'}"
            for w in c["confident_wrong"]
        )
    return out


def lines(report: Mapping[str, Any]) -> list[str]:
    out = [f"{report['measured_on']}; in-sample (aliases were taken from these documents)"]
    for name, s in report["strata"].items():
        a, b = s["gate_a"], s["gate_b"]
        out.append(f"\n[{name}] {s['documents']} documents ({NOT_MEASURED})")
        out.extend(f"  UNREADABLE (every part not met): {e}" for e in s["unreadable"])
        if not s["documents"]:
            out.append("  empty stratum: no document of this period kind")
            continue
        for status, modes in a["figures"].items():
            for mode, f in modes.items():
                if status != "checked":
                    judged = "provisional"
                elif G1[mode] is None:
                    judged = "reported, no accuracy bar (D20)"
                else:
                    judged = _met(f["met"])
                share = "no cells" if f["share"] is None else f"{f['share']:.2%}"
                out.append(
                    f"  A figures {status} {mode}: {f['right']} of {f['cells']} cells ({share}, "
                    f"{_interval(f['right'], f['cells'])}) {judged} (cell share, n={f['files']} "
                    "files)"
                )
        if not any(a["figures"].values()):
            out.append("  A figures: no expected file")
        out.extend(_confident_lines(a["confident"]))
        p = a["pairs"]
        out.append(
            f"  A pairs: {p['identical']} of {p['gated']} gated pairs identical "
            f"({p['ungated']} ungated) {_met(p['met'])}"
        )
        out.extend(_identity_lines(a, s["documents"]))
        sc = a["scale_currency"]
        out.append(
            f"  A scale and currency: {sc['right']} right of {sc['expected']} statements "
            f"expected ({sc['found']} found, {sc['not_found']} not found) {_met(sc['met'])}"
        )
        verdicts = b["verified"] + b["consistent"] + b["disagrees"]
        out.append(
            f"  B critical slots: {b['slots']}: mapped {b['mapped']}, ambiguous (flagged) "
            f"{b['ambiguous']}, explicitly flagged {b['explicitly_flagged']}, "
            f"ABSENT (silent) {b['absent']}, statement not found {b['statement_not_found']} "
            f"{_met(b['met'])}"
        )
        out.append(
            f"  B of the mapped: verified {b['verified']}, consistent with the lexicon "
            f"{b['consistent']}, no verdict {b['no_verdict']}. {verdicts} of the {b['mapped']} "
            f"mapped slots have a verdict from an expected file; {b['disagrees']} of those "
            f"disagree. That says nothing of the {b['no_verdict']} with no verdict"
        )
    out.append("\nempty strata: " + (", ".join(report["empty_strata"]) or "none"))
    for document in report.get("documents", []):
        if "comparison" in document:
            comparison = document["comparison"]
            detail = [f"locate evidence {document['id']}: {comparison['status']}"]
            if comparison.get("changed_pages"):
                detail.append(f"pages {comparison['changed_pages']}")
            if comparison.get("ranges_changed"):
                detail.append(
                    f"lost {comparison['lost_ranges']}; gained {comparison['gained_ranges']}"
                )
            if comparison.get("conversion_spans_changed"):
                detail.append(
                    f"conversion spans changed; lost {comparison['lost_conversion_spans']}; "
                    f"gained {comparison['gained_conversion_spans']}"
                )
            if comparison.get("conversion_settings_changed"):
                detail.append("conversion settings hash changed")
            if comparison.get("locate_version_changed"):
                versions = comparison["locate_version_changed"]
                detail.append(f"locate version {versions['previous']} -> {versions['current']}")
            if comparison.get("ocr_key_changed"):
                detail.append(f"locate OCR key changed: {comparison['ocr_key_changed']}")
            if comparison.get("reason"):
                detail.append(f"reason: {comparison['reason']}")
            out.append("; ".join(detail))
    return out


def earlier_records(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index the documents and errored records of an earlier report by id.

    An errored record without ``locate_evidence`` (written before errored records carried it)
    gets incomplete evidence. An id that appears twice is a malformed report.
    """
    records: dict[str, dict[str, Any]] = {}
    # Reports written before errored records existed have no "errored" key.
    sections = (("documents", report["documents"]), ("errored", report.get("errored", [])))
    for key, items in sections:
        if not isinstance(items, list):
            raise ValueError(f"'{key}' must be a list")
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise ValueError(f"each {key} entry must be an object with a string 'id'")
            if item["id"] in records:
                raise ValueError(f"duplicate document id {item['id']!r}")
            if key == "errored" and item.get("locate_evidence") is None:
                item = {
                    **item,
                    "locate_evidence": incomplete_evidence(
                        "document failed in the earlier report; no locate_evidence recorded", None
                    ).as_dict(),
                }
            records[item["id"]] = item
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m harness.gates", description=__doc__)
    parser.add_argument("--compare-with", type=Path, help="earlier gates-dev.json report")
    parser.add_argument("--output", type=Path, default=OUT / "gates-dev.json")
    args = parser.parse_args(argv)
    previous: dict[str, dict[str, Any]] | None = None
    schema_reason: str | None = None
    if args.compare_with is not None:
        try:
            earlier_report = json.loads(args.compare_with.read_text(encoding="utf-8"))
            previous = earlier_records(earlier_report)
            if "evidence_schema_version" in earlier_report:
                found = earlier_report["evidence_schema_version"]
                if type(found) is not int or found != EVIDENCE_SCHEMA_VERSION:
                    schema_reason = f"earlier report uses evidence schema {found!r}"
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise SystemExit(f"cannot read comparison report {args.compare_with}: {exc}") from exc
    config = load_config()
    ocr = make_engine(config)
    taxonomy = load_taxonomy()
    index = LabelIndex(taxonomy)
    entries = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["documents"]
    documents: list[dict[str, Any]] = []
    by_id: dict[str, dict[StatementType, Statement]] = {}
    errors: list[str] = []
    errored: list[dict[str, Any]] = []
    guard = EngineGuard(len(entries))
    stopped: EngineFailure | None = None
    for entry in entries:
        print(f"{entry['id']} ...", file=sys.stderr, flush=True)
        try:
            result = structure_pdf(MANIFEST.parent / entry["file"], config, ocr)
        except DOCUMENT_ERRORS as exc:
            failure = document_failure(exc)
            errors.append(f"{entry['id']}: {failure['error']}")
            failed: dict[str, Any] = {
                "id": entry["id"],
                "reason": failure["error"],
                "detail": failure["detail"],
                "locate_evidence": incomplete_evidence(
                    f"document failed: {failure['error']}", None
                ).as_dict(),
            }
            if previous is not None:
                failed["comparison"] = {
                    "status": "not comparable",
                    "reason": "document failed in the current run",
                }
            errored.append(failed)
            try:
                guard.record(failure["error"], failure["detail"])
            except EngineFailure as exc_stop:
                stopped = exc_stop
                break
            continue
        guard.record(None)
        # A bank or insurer is declined before convert runs; its convert.json is an old run's.
        converted = result.industry is None or result.industry.outcome != "declined"
        evidence = document_evidence(config.artifact_root / result.sha256, converted=converted)
        path = EXPECTED_DIR / f"{entry['id']}.json"
        checks = load_checks(config.artifact_root / result.sha256 / "table_checks.json")
        record = document_figures(
            entry,
            result,
            checks,
            load_expected(path) if path.is_file() else None,
            index,
            taxonomy,
            config.enabled_types,
        )
        record["locate_evidence"] = evidence.as_dict()
        if previous is not None:
            if schema_reason is not None:
                record["comparison"] = {"status": "not comparable", "reason": schema_reason}
            elif entry["id"] not in previous:
                record["comparison"] = {
                    "status": "not comparable",
                    "reason": "document id absent from earlier report",
                }
            else:
                earlier = evidence_from_dict(previous[entry["id"]].get("locate_evidence"))
                if earlier is None:
                    record["comparison"] = {
                        "status": "not comparable",
                        "reason": "no locate_evidence in the earlier report",
                    }
                else:
                    record["comparison"] = compare_evidence(earlier, evidence)
        documents.append(record)
        by_id[entry["id"]] = first_statements(result, PAIR_TYPES)
    kinds = {d["id"]: d["period_kind"] for d in documents}
    report = {
        **gate_report(documents, pair_records(by_id, kinds), errors),
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "errors": errors,
        "errored": errored,
        "documents": documents,
    }
    if previous is not None:
        current_ids = {entry["id"] for entry in entries}
        report["removed_documents"] = [
            {
                "id": doc_id,
                "comparison": {
                    "status": "not comparable",
                    "reason": "document id absent from the current run",
                },
            }
            for doc_id in sorted(previous.keys() - current_ids)
        ]
    if stopped is not None:
        report = aborted(report, stopped)
    print("\n".join(lines(report)))
    print("unreadable documents: " + (", ".join(errors) or "none"))
    if stopped is not None:
        print(f"aborted: {stopped}")
    out = args.output
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        display_path = out.resolve().relative_to(REPO_ROOT.resolve())
    except ValueError:
        display_path = out
    print(f"wrote {display_path}")
    if stopped is not None:
        raise stopped
    incomplete = any(d["locate_evidence"]["status"] != "complete" for d in documents)
    return 1 if errored or incomplete else 0


if __name__ == "__main__":
    sys.exit(main())
