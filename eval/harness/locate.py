"""Score the statement page locator (spec 09, Scoring).

    PYTHONPATH=eval uv run python -m harness.locate golden
    PYTHONPATH=eval uv run python -m harness.locate train
    PYTHONPATH=eval uv run python -m harness.locate model_test --checkpoint

Golden: recall per type against labelled pages, candidate share and time. Corpus pools: whether
each corporate document has a balance and an income range, candidate share, industry verdicts
against the sector labels, and the decline check: the industry decision (declined, held for review
or pass) against each document's sector label, judged right, held or wrong. `train` is the fit and
validation parts after the recorded moves (`fra_core.split.development_documents`), never the
holdout. The blind pool is refused.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from fra_core.schemas import StatementType
from fra_core.split import Part
from fra_ingest.config import REPO_ROOT, load_config
from fra_ingest.industry import industry_decision
from fra_ingest.ocr import make_engine
from fra_ingest.results import IndustryDecision, LocateResult
from fra_ingest.stage import locate_pdf
from harness.development import development_set, positive_int
from harness.failures import DOCUMENT_ERRORS, EngineFailure, EngineGuard, aborted, document_failure
from harness.paths import (
    CANDIDATES,
    CORPUS,
    HOLDOUT_MOVES,
    NEGATIVE_CONTROL,
    SCORING_LOG,
    STRATA,
)

MANIFEST = REPO_ROOT / "eval" / "golden" / "manifest.yaml"
OUT = REPO_ROOT / "var" / "eval"
MANIFEST_KEYS = {
    "financial_position": StatementType.BALANCE,
    "profit_or_loss": StatementType.INCOME,
    "comprehensive_income": StatementType.COMPREHENSIVE_INCOME,
    "changes_in_equity": StatementType.EQUITY,
    "cash_flows": StatementType.CASH_FLOW,
}
POOLS = ("dev", "train", "model_test")


def labelled_ranges(value: Any) -> list[tuple[int, int]]:
    if not value:
        return []
    if isinstance(value[0], list):
        return [(int(a), int(b)) for a, b in value]
    return [(int(value[0]), int(value[1]))]


def found_pages(
    result: LocateResult, statement_type: StatementType, pad: int, max_rank: int | None = None
) -> set[int]:
    count = result.document.page_count
    return {
        page
        for r in result.ranges
        if r.type is statement_type and (max_rank is None or r.rank <= max_rank)
        for page in range(r.padded(pad, count)[0], r.padded(pad, count)[1] + 1)
    }


def top_share(result: LocateResult, enabled: set[StatementType], pad: int) -> float:
    """Pages docling would read if only each enabled type's top-ranked range were converted."""
    pages = set().union(*(found_pages(result, t, pad, max_rank=1) for t in enabled))
    return len(pages) / result.document.page_count


def recall(labelled: set[int], found: set[int]) -> float:
    return len(labelled & found) / len(labelled) if labelled else 1.0


def check_target(target: str, checkpoint: bool) -> None:
    if target == "blind":
        print("the blind pool is never scored during development (R17)", file=sys.stderr)
        raise SystemExit(2)
    if target == "model_test" and not checkpoint:
        print("model_test runs only at checkpoints: pass --checkpoint", file=sys.stderr)
        raise SystemExit(2)
    if target != "golden" and target not in POOLS:
        print(f"unknown target {target!r}", file=sys.stderr)
        raise SystemExit(2)


def truth_label(entry: Mapping[str, Any]) -> str:
    if entry.get("role") != NEGATIVE_CONTROL:
        return "corporate"
    if entry.get("sector") == "other_financial":
        return f"other_financial/{entry.get('subsector', 'other')}"
    return str(entry.get("sector"))


def verdict_label(result: LocateResult) -> str:
    kind = result.industry.kind
    return f"{kind}/{result.industry.subkind or 'other'}" if kind == "other_financial" else kind


def decision_label(decision: IndustryDecision | None) -> str:
    """`pass` when the document goes on, else `declined/<kind>` or `needs_review/<code>`."""
    return "pass" if decision is None else f"{decision.outcome}/{decision.code}"


def judge_decision(truth: str, decision: str) -> str:
    """`right` for a pass on a corporate document or a decline naming the sector's kind, `held`
    for a hold for review (acceptable, 08-revised-plan.md), `wrong` for anything else: a bank,
    insurer or other financial company that passes, a decline naming the other kind, a
    corporate document declined."""
    outcome, _, code = decision.partition("/")
    if outcome == "needs_review":
        return "held"
    if truth == "corporate":
        return "right" if outcome == "pass" else "wrong"
    return "right" if outcome == "declined" and code == truth else "wrong"


def decline_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Judgements for the negative controls and for the corporate documents, and the wrong ones,
    for all documents and for each period kind (D18). A document that could not be read is
    counted as errored, not as right."""

    def count(group: list[dict[str, Any]]) -> dict[str, int]:
        judged = Counter(r.get("judgement", "errored") for r in group)
        return {k: judged[k] for k in ("right", "held", "wrong", "errored")}

    def split(group: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
        return {
            "negative_controls": count([r for r in group if r["truth"] != "corporate"]),
            "corporate": count([r for r in group if r["truth"] == "corporate"]),
        }

    judged = [r for r in rows if "truth" in r]
    return {
        **split(judged),
        "by_period_kind": {s: split([r for r in judged if r["period"] == s]) for s in STRATA},
        "wrong": [
            {"id": r["id"], "truth": r["truth"], "decision": r["decision"]}
            for r in judged
            if r.get("judgement") == "wrong"
        ],
        "errored": [r["id"] for r in judged if "error" in r],
    }


def pool_entries(
    pool: str,
    candidates: Path = CANDIDATES,
    moves: Path = HOLDOUT_MOVES,
    log: Path = SCORING_LOG,
) -> list[dict[str, Any]]:
    """The documents of a pool to run. `train` is the fit and validation parts, so the holdout
    is never among them."""
    if pool == "train":
        return development_set({Part.FIT, Part.VALIDATION}, candidates, moves, log)
    records = yaml.safe_load(candidates.read_text(encoding="utf-8"))["documents"]
    return [e for e in records if e.get("pool") == pool]


def run_golden(no_ocr: bool, fresh: bool = False) -> dict[str, Any]:
    config = load_config()
    engine = None if no_ocr else make_engine(config)
    enabled = set(config.enabled_types)
    rows, shares, top_shares, misses, top_misses = [], [], [], [], []
    documents = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["documents"]
    guard = EngineGuard(sum(1 for d in documents if d.get("statement_pages")))
    stopped: EngineFailure | None = None
    for doc in documents:
        pages = doc.get("statement_pages")
        if not pages:
            print(f"skip {doc['id']}: not labelled")
            continue
        started = time.perf_counter()
        try:
            result = locate_pdf(MANIFEST.parent / doc["file"], config, engine, use_cache=not fresh)
        except DOCUMENT_ERRORS as exc:
            failure = document_failure(exc)
            rows.append({"id": doc["id"], **failure})
            print(f"{doc['id']:34} unreadable: {failure['error']} ({failure['detail']})")
            try:
                guard.record(failure["error"], failure["detail"])
            except EngineFailure as exc_stop:
                stopped = exc_stop
                break
            continue
        guard.record(None)
        row: dict[str, Any] = {
            "id": doc["id"],
            "share": result.candidate_share,
            "seconds": time.perf_counter() - started,
            "recall": {},
            "top_recall": {},
            "top_share": top_share(result, enabled, config.pad_pages),
        }
        for key, statement_type in MANIFEST_KEYS.items():
            labelled = {p for a, b in labelled_ranges(pages.get(key)) for p in range(a, b + 1)}
            if not labelled:
                continue
            found = found_pages(result, statement_type, config.pad_pages)
            top = found_pages(result, statement_type, config.pad_pages, max_rank=1)
            row["recall"][statement_type.value] = recall(labelled, found)
            row["top_recall"][statement_type.value] = recall(labelled, top)
            if statement_type in enabled and labelled - found:
                misses.append(
                    f"{doc['id']} {statement_type.value} pages {sorted(labelled - found)}"
                )
            if statement_type in enabled and labelled - top:
                top_misses.append(
                    f"{doc['id']} {statement_type.value} pages {sorted(labelled - top)}"
                )
        rows.append(row)
        shares.append(result.candidate_share)
        top_shares.append(row["top_share"])
        print(
            f"{doc['id']:34} share {result.candidate_share:5.1%} (top only "
            f"{row['top_share']:5.1%})  {row['seconds']:6.1f}s  "
            + "  ".join(f"{k} {v:.0%}" for k, v in row["recall"].items())
        )

    median_share = statistics.median(shares) if shares else 0.0
    median_top = statistics.median(top_shares) if top_shares else 0.0
    print(f"\nmedian candidate share {median_share:.1%} (target 15% or less)")
    print(f"median share, top-ranked range per type only {median_top:.1%}")
    print("enabled-type misses: " + ("none" if not misses else "\n  " + "\n  ".join(misses)))
    print(
        "enabled-type misses, top-ranked range only: "
        + ("none" if not top_misses else "\n  " + "\n  ".join(top_misses))
    )
    report = {
        "documents": rows,
        "median_share": median_share,
        "median_top_share": median_top,
        "misses": misses,
        "top_misses": top_misses,
    }
    if stopped is not None:
        stopped.partial = aborted(report, stopped)
        raise stopped
    return report


def pool_summary(rows: list[dict[str, Any]], missing: list[str]) -> dict[str, Any]:
    """Coverage and industry table. A document that could not be read stays in the count as
    not covered, and missing files are listed, so neither silently improves the numbers."""
    corporates = [r for r in rows if r.get("truth") == "corporate"]
    uncovered = [r["id"] for r in corporates if not r.get("covered")]
    confusion = Counter[tuple[str, str]](
        (r["truth"], r.get("verdict", "error")) for r in rows if "truth" in r
    )
    return {
        "documents": rows,
        "decline": decline_summary(rows),
        "coverage": (len(corporates) - len(uncovered)) / len(corporates) if corporates else 0.0,
        "uncovered": uncovered,
        "errored": [r["id"] for r in rows if "error" in r],
        "missing": missing,
        "confusion": [[t, v, n] for (t, v), n in sorted(confusion.items())],
    }


def run_pool(
    pool: str,
    no_ocr: bool,
    limit: int | None = None,
    candidates: Path = CANDIDATES,
    moves: Path = HOLDOUT_MOVES,
    log: Path = SCORING_LOG,
    store: Path = CORPUS,
) -> dict[str, Any]:
    config = load_config()
    engine = None if no_ocr else make_engine(config)
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    entries = sorted(pool_entries(pool, candidates, moves, log), key=lambda e: str(e["id"]))[:limit]
    guard = EngineGuard(len(entries))
    stopped: EngineFailure | None = None
    for number, entry in enumerate(entries, start=1):
        path = store / pool / f"{entry['id']}.pdf"
        # The pool report prints only at the end; this says the run is moving.
        if not path.exists():
            missing.append(entry["id"])
            print(f"{number}/{len(entries)} {entry['id']} missing", file=sys.stderr, flush=True)
            continue
        print(f"{number}/{len(entries)} {entry['id']} ...", file=sys.stderr, flush=True)
        truth = truth_label(entry)
        try:
            result = locate_pdf(path, config, engine)
        except DOCUMENT_ERRORS as exc:
            # One page the engine could not read fails its document, not the whole run.
            failure = document_failure(exc)
            rows.append({"id": entry["id"], "period": entry["period"], "truth": truth, **failure})
            try:
                guard.record(failure["error"], failure["detail"])
            except EngineFailure as exc_stop:
                stopped = exc_stop
                break
            continue
        guard.record(None)
        types = {r.type for r in result.ranges}
        decision = decision_label(industry_decision(result.industry))
        rows.append(
            {
                "id": entry["id"],
                "period": entry["period"],
                "covered": {StatementType.BALANCE, StatementType.INCOME} <= types,
                "share": result.candidate_share,
                "truth": truth,
                "verdict": verdict_label(result),
                "decision": decision,
                "judgement": judge_decision(truth, decision),
                "flags": result.flags,
            }
        )

    if stopped is None:
        try:
            guard.finish()  # a pool with files missing may attempt fewer documents than planned
        except EngineFailure as exc_stop:
            stopped = exc_stop
    summary = pool_summary(rows, missing)
    if stopped is not None:
        stopped.partial = aborted(summary, stopped)
        raise stopped
    corporates = sum(1 for r in rows if r.get("truth") == "corporate")
    print(
        f"{pool}: {len(rows)} documents, corporate coverage {summary['coverage']:.1%} of "
        f"{corporates} (target 95%)"
    )
    print("not covered: " + (", ".join(summary["uncovered"]) or "none"))
    unreadable = [
        f"{r['id']}: {r['error']}" + (f" ({r['detail']})" if r.get("detail") else "")
        for r in rows
        if "error" in r
    ]
    print("unreadable: " + ("; ".join(unreadable) or "none"))
    print(f"missing files: {len(missing)}" + (f" ({', '.join(missing)})" if missing else ""))
    print("industry (truth -> verdict):")
    for truth, verdict, n in summary["confusion"]:
        print(f"  {truth:34} -> {verdict:34} {n}")
    decline = summary["decline"]
    print("decline check (decision against the sector label):")
    print(f"  negative controls {decline['negative_controls']}")
    print(f"  corporate documents {decline['corporate']}")
    for kind, parts in decline["by_period_kind"].items():
        print(f"  {kind}: negative controls {parts['negative_controls']}")
        print(f"  {kind}: corporate documents {parts['corporate']}")
    print("  wrong: " + ("none" if not decline["wrong"] else ""))
    for w in decline["wrong"]:
        print(f"    {w['id']}: {w['truth']} -> {w['decision']}")
    return summary


def write_report(target: str, report: Mapping[str, Any]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"locate-{target}.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {out.relative_to(REPO_ROOT)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("target", help="golden, dev, train or model_test")
    parser.add_argument("--checkpoint", action="store_true")
    parser.add_argument("--no-ocr", action="store_true")
    parser.add_argument(
        "--limit", type=positive_int, help="corpus pools: the first n documents in id order"
    )
    parser.add_argument(
        "--fresh", action="store_true", help="ignore the page cache, so times include OCR"
    )
    args = parser.parse_args(argv)
    check_target(args.target, args.checkpoint)

    try:
        report = (
            run_golden(args.no_ocr, args.fresh)
            if args.target == "golden"
            else run_pool(args.target, args.no_ocr, args.limit)
        )
    except EngineFailure as exc:
        if exc.partial is not None:
            write_report(args.target, exc.partial)
        raise
    write_report(args.target, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
