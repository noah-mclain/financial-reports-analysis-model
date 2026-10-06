"""Gate A and Gate B on the golden set (`dev`), per period kind beside the total.

    make eval-gates        (PYTHONPATH=eval uv run python -m harness.gates)

Definitions are the build plan's (docs/blueprint/07-build-plan.md, Gates) with the extraction
thresholds of G1 (docs/blueprint/04-execution-phases.md; the values are `harness.extraction`'s
DIGITAL and SCANNED, the one source), applied to what the harness already measures:

Gate A, extraction.
  figures         cells of the expected files read right (`extraction.score_statement`). These are
                  cell shares over n expected files, with a 95% Wilson interval, not a verdict on
                  the gate: G1's thresholds apply to checked files, and a draft file is provisional
                  and judged by nothing. A share over no cells is not measured.
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
from typing import Any

import yaml

from fra_core.schemas import CheckResult, Statement, StatementType
from fra_core.taxonomy.loader import Taxonomy, load_taxonomy
from fra_ingest.config import REPO_ROOT, load_config
from fra_ingest.label_match import LabelIndex
from fra_ingest.ocr import make_engine
from fra_ingest.results import StructureResult
from fra_ingest.review import StatementReview
from fra_ingest.structure import structure_pdf
from harness.expected import EXPECTED_DIR, ExpectedFile, load_expected
from harness.extraction import DIGITAL, SCANNED, score_statement, wilson_interval
from harness.failures import DOCUMENT_ERRORS, EngineFailure, EngineGuard, aborted, document_failure
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
G1 = {"digital": DIGITAL, "scanned": SCANNED}
PAIR_TYPES = (
    StatementType.BALANCE,
    StatementType.INCOME,
)  # "financial position and profit or loss"
IDENTITY_MET = ("ok", "failed_accepted")


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
    reviews = {r.statement_id: r for r in result.reviews}
    balance = found.get(StatementType.BALANCE)
    everything = first_statements(result, StatementType)
    counts, slots, disagreements = critical_slots(everything, expected, index, taxonomy)
    counts["absent"] = sum(1 for slot in slots if slot["state"] == "unmapped")
    figures = []
    for e in expected.statements if expected else []:
        score = score_statement(e, everything.get(e.type))
        figures.append(
            {
                "status": expected.status if expected else None,
                "page_mode": e.page_mode,
                "type": e.type.value,
                "cells": score.cells,
                "right": score.right,
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
            key = (f["status"], f["page_mode"])
            total = cells.setdefault(key, Counter())
            total["cells"] += f["cells"]
            total["right"] += f["right"]
            files.setdefault(key, set()).add(d["id"])
    out: dict[str, dict[str, Any]] = {"checked": {}, "draft": {}}
    for (status, mode), n in sorted(cells.items()):
        share = _share(n["right"], n["cells"])
        met = share >= G1[mode] if status == "checked" and share is not None else None
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
                judged = _met(f["met"]) if status == "checked" else "provisional"
                share = "no cells" if f["share"] is None else f"{f['share']:.2%}"
                out.append(
                    f"  A figures {status} {mode}: {f['right']} of {f['cells']} cells ({share}, "
                    f"{_interval(f['right'], f['cells'])}) {judged} (cell share, n={f['files']} "
                    "files)"
                )
        if not any(a["figures"].values()):
            out.append("  A figures: no expected file")
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
    return out


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(prog="python -m harness.gates", description=__doc__).parse_args(argv)
    config = load_config()
    ocr = make_engine(config)
    taxonomy = load_taxonomy()
    index = LabelIndex(taxonomy)
    entries = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["documents"]
    documents: list[dict[str, Any]] = []
    by_id: dict[str, dict[StatementType, Statement]] = {}
    errors: list[str] = []
    errored: list[dict[str, str]] = []
    guard = EngineGuard(len(entries))
    stopped: EngineFailure | None = None
    for entry in entries:
        print(f"{entry['id']} ...", file=sys.stderr, flush=True)
        try:
            result = structure_pdf(MANIFEST.parent / entry["file"], config, ocr)
        except DOCUMENT_ERRORS as exc:
            failure = document_failure(exc)
            errors.append(f"{entry['id']}: {failure['error']}")
            errored.append(
                {"id": entry["id"], "reason": failure["error"], "detail": failure["detail"]}
            )
            try:
                guard.record(failure["error"], failure["detail"])
            except EngineFailure as exc_stop:
                stopped = exc_stop
                break
            continue
        guard.record(None)
        path = EXPECTED_DIR / f"{entry['id']}.json"
        checks = load_checks(config.artifact_root / result.sha256 / "table_checks.json")
        documents.append(
            document_figures(
                entry,
                result,
                checks,
                load_expected(path) if path.is_file() else None,
                index,
                taxonomy,
                config.enabled_types,
            )
        )
        by_id[entry["id"]] = first_statements(result, PAIR_TYPES)
    kinds = {d["id"]: d["period_kind"] for d in documents}
    report = {
        **gate_report(documents, pair_records(by_id, kinds), errors),
        "errors": errors,
        "errored": errored,
        "documents": documents,
    }
    if stopped is not None:
        report = aborted(report, stopped)
    print("\n".join(lines(report)))
    print("unreadable documents: " + (", ".join(errors) or "none"))
    if stopped is not None:
        print(f"aborted: {stopped}")
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / "gates-dev.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {out.relative_to(REPO_ROOT)}")
    if stopped is not None:
        raise stopped
    return 0


if __name__ == "__main__":
    sys.exit(main())
