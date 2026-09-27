"""Score the statement page locator (spec 09, Scoring).

    uv run python eval/harness/locate.py golden
    uv run python eval/harness/locate.py train
    uv run python eval/harness/locate.py model_test --checkpoint

Golden: recall per type against labelled pages, candidate share and time. Corpus pools: whether
each corporate document has a balance and an income range, candidate share, and industry
verdicts against the sector labels. The blind pool is refused.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from typing import Any

import yaml

from fra_core.schemas import StatementType
from fra_ingest.config import REPO_ROOT, load_config
from fra_ingest.errors import IngestError
from fra_ingest.ocr import default_engine
from fra_ingest.results import LocateResult
from fra_ingest.stage import locate_pdf

MANIFEST = REPO_ROOT / "eval" / "golden" / "manifest.yaml"
CANDIDATES = REPO_ROOT / "eval" / "corpus" / "candidates.yaml"
CORPUS = REPO_ROOT / "var" / "corpus"
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


def found_pages(result: LocateResult, statement_type: StatementType, pad: int) -> set[int]:
    count = result.document.page_count
    return {
        page
        for r in result.ranges
        if r.type is statement_type
        for page in range(r.padded(pad, count)[0], r.padded(pad, count)[1] + 1)
    }


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


def truth_label(entry: dict[str, Any]) -> str:
    if entry.get("role") != "negative_control":
        return "corporate"
    if entry.get("sector") == "other_financial":
        return f"other_financial/{entry.get('subsector', 'other')}"
    return str(entry.get("sector"))


def verdict_label(result: LocateResult) -> str:
    kind = result.industry.kind
    return f"{kind}/{result.industry.subkind or 'other'}" if kind == "other_financial" else kind


def run_golden(no_ocr: bool) -> dict[str, Any]:
    config = load_config()
    engine = None if no_ocr else default_engine()
    enabled = set(config.enabled_types)
    rows, shares, misses = [], [], []
    for doc in yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["documents"]:
        pages = doc.get("statement_pages")
        if not pages:
            print(f"skip {doc['id']}: not labelled")
            continue
        result = locate_pdf(MANIFEST.parent / doc["file"], config, engine)
        row: dict[str, Any] = {
            "id": doc["id"],
            "share": result.candidate_share,
            "seconds": sum(result.timings.values()),
            "recall": {},
        }
        for key, statement_type in MANIFEST_KEYS.items():
            labelled = {p for a, b in labelled_ranges(pages.get(key)) for p in range(a, b + 1)}
            if not labelled:
                continue
            found = found_pages(result, statement_type, config.pad_pages)
            row["recall"][statement_type.value] = recall(labelled, found)
            if statement_type in enabled and labelled - found:
                misses.append(
                    f"{doc['id']} {statement_type.value} pages {sorted(labelled - found)}"
                )
        rows.append(row)
        shares.append(result.candidate_share)
        print(
            f"{doc['id']:34} share {result.candidate_share:5.1%}  {row['seconds']:6.1f}s  "
            + "  ".join(f"{k} {v:.0%}" for k, v in row["recall"].items())
        )

    median_share = statistics.median(shares) if shares else 0.0
    print(f"\nmedian candidate share {median_share:.1%} (target 15% or less)")
    print("enabled-type misses: " + ("none" if not misses else "\n  " + "\n  ".join(misses)))
    return {"documents": rows, "median_share": median_share, "misses": misses}


def run_pool(pool: str, no_ocr: bool) -> dict[str, Any]:
    config = load_config()
    engine = None if no_ocr else default_engine()
    entries = yaml.safe_load(CANDIDATES.read_text(encoding="utf-8"))["documents"]
    rows, confusion, uncovered = [], Counter[tuple[str, str]](), []
    for entry in (e for e in entries if e.get("pool") == pool):
        path = CORPUS / pool / f"{entry['id']}.pdf"
        if not path.exists():
            continue
        try:
            result = locate_pdf(path, config, engine)
        except IngestError as exc:
            rows.append({"id": entry["id"], "error": exc.reason})
            continue
        types = {r.type for r in result.ranges}
        covered = {StatementType.BALANCE, StatementType.INCOME} <= types
        truth, verdict = truth_label(entry), verdict_label(result)
        confusion[(truth, verdict)] += 1
        if truth == "corporate" and not covered:
            uncovered.append(entry["id"])
        rows.append(
            {
                "id": entry["id"],
                "covered": covered,
                "share": result.candidate_share,
                "truth": truth,
                "verdict": verdict,
                "flags": result.flags,
            }
        )

    corporates = [r for r in rows if r.get("truth") == "corporate"]
    coverage = sum(r["covered"] for r in corporates) / len(corporates) if corporates else 0.0
    print(f"{pool}: {len(rows)} documents, corporate coverage {coverage:.1%} (target 95%)")
    print("not covered: " + (", ".join(uncovered) or "none"))
    print("industry (truth -> verdict):")
    for (truth, verdict), n in sorted(confusion.items()):
        print(f"  {truth:34} -> {verdict:34} {n}")
    return {
        "documents": rows,
        "coverage": coverage,
        "uncovered": uncovered,
        "confusion": [[t, v, n] for (t, v), n in sorted(confusion.items())],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("target", help="golden, dev, train or model_test")
    parser.add_argument("--checkpoint", action="store_true")
    parser.add_argument("--no-ocr", action="store_true")
    args = parser.parse_args(argv)
    check_target(args.target, args.checkpoint)

    report = (
        run_golden(args.no_ocr) if args.target == "golden" else run_pool(args.target, args.no_ocr)
    )
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"locate-{args.target}.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {out.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
