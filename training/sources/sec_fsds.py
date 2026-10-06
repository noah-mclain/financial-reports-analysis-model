"""SEC Financial Statement Data Sets: English line-item labels at scale.

    FRA_SEC_USER_AGENT="Your Name you@example.com" \
        uv run python training/sources/sec_fsds.py 2024q1 2024q2 ...
    uv run python training/sources/sec_fsds.py --last 8

Each quarterly zip holds `sub.txt` (one row per filing) and `pre.txt` (how each line item is
presented: statement, tag, label). Both are read straight from the zip, never unpacked. The
output is one gzipped JSONL file per quarter in training/data/sec_fsds/, one row per distinct
(filer, statement, label, tag), with the filer's pool:

- Recorded issuer pools are authoritative across SEC and PDF sources. New SEC filers keep
  the legacy CIK default (85% train, 15% model_test), distinct from the PDF default.
- A CIK listed in eval/corpus/candidates.yaml follows that document's pool; a blind CIK is
  dropped from the output entirely.
- Filers with financial-sector SIC codes (6000 to 6499) are kept with role negative_control.

sec.gov asks automated clients to identify themselves, hence FRA_SEC_USER_AGENT.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import io
import json
import os
import re
import sys
import time
import urllib.request
import zipfile
from collections import Counter
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from fra_core.pools import (
    REGISTRY_RELATIVE_PATH,
    SEC_OUTPUT_RELATIVE_PATH,
    Identity,
    PoolError,
    PoolRegistry,
    Source,
    locked_registry,
    record_pdf_metadata,
    save_registry,
)
from fra_core.split import issuer_key

ROOT = Path(__file__).resolve().parents[2]
CANDIDATES = ROOT / "eval/corpus/candidates.yaml"
ZIPS = ROOT / "var/sec_fsds"
OUT = ROOT / SEC_OUTPUT_RELATIVE_PATH
URL = "https://www.sec.gov/files/dera/data/financial-statement-data-sets/{quarter}.zip"

FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "40-F"}
STATEMENTS = {"BS", "IS", "CI", "CF", "EQ"}
FINANCIAL_SIC = range(6000, 6500)
POOL_METADATA = ROOT / REGISTRY_RELATIVE_PATH
GOLDEN_MANIFEST = ROOT / "eval/golden/manifest.yaml"

csv.field_size_limit(sys.maxsize)


def quarters_back(n: int, today: dt.date | None = None) -> list[str]:
    """The n most recent quarters that SEC has published (each appears after its quarter ends)."""
    today = today or dt.date.today()
    year, q = today.year, (today.month - 1) // 3  # the current quarter is not out yet
    out = []
    for _ in range(n):
        if q == 0:
            year, q = year - 1, 4
        out.append(f"{year}q{q}")
        q -= 1
    return list(reversed(out))


def pinned_ciks(documents: Iterable[dict[str, Any]]) -> dict[int, str]:
    """Validated CIK pins, never last-write-wins."""
    registry = PoolRegistry()
    registry.record_documents(documents)
    return registry.cik_pools()


def _registry(pinned: dict[int, str] | PoolRegistry) -> PoolRegistry:
    if isinstance(pinned, PoolRegistry):
        return pinned
    registry = PoolRegistry()
    for cik, pool in pinned.items():
        registry.register(Identity(cik=cik), pool)
    return registry


def pool_for(cik: int, pinned: dict[int, str] | PoolRegistry, name: str | None = None) -> str:
    if name is not None and not issuer_key(name):
        name = None
    return str(_registry(pinned).assign(Identity(name, cik), Source.SEC))


def role_for(sic: str) -> str:
    return "negative_control" if sic.isdigit() and int(sic) in FINANCIAL_SIC else "corporate"


def _rows(archive: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    with archive.open(name) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
        yield from csv.DictReader(text, delimiter="\t", quoting=csv.QUOTE_NONE)


def extract(
    zip_path: Path, pinned: dict[int, str] | PoolRegistry
) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Distinct presented labels from one quarter, with pool and role."""
    registry = _registry(pinned)
    stats: Counter[str] = Counter()
    with zipfile.ZipFile(zip_path) as archive:
        filings: dict[str, dict[str, Any]] = {}
        for sub in _rows(archive, "sub.txt"):
            stats["filings"] += 1
            if sub["form"] not in FORMS:
                continue
            cik = int(sub["cik"])
            pool = pool_for(cik, registry, sub["name"])
            if pool == "blind":
                stats["filings_blind_dropped"] += 1
                continue
            filings[sub["adsh"]] = {
                "cik": cik,
                "name": sub["name"],
                "sic": sub["sic"],
                "form": sub["form"],
                "fy": sub["fy"],
                "fp": sub["fp"],
                "period": sub["period"],
                "pool": pool,
                "role": role_for(sub["sic"]),
            }
        seen: set[tuple[int, str, str, str]] = set()
        rows = []
        for pre in _rows(archive, "pre.txt"):
            filing = filings.get(pre["adsh"])
            if filing is None or pre["stmt"] not in STATEMENTS:
                continue
            label = " ".join(pre["plabel"].split())
            key = (filing["cik"], pre["stmt"], label.lower(), pre["tag"])
            if not label or key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    **filing,
                    "stmt": pre["stmt"],
                    "tag": pre["tag"],
                    "version": pre["version"],
                    "plabel": label,
                    "negating": pre["negating"] == "1",
                    "line": int(pre["line"]),
                }
            )
    stats["filers"] = len({r["cik"] for r in rows})
    stats["rows"] = len(rows)
    for r in rows:
        stats[f"rows_{r['pool']}"] += 1
    return rows, stats


def download(quarter: str, user_agent: str) -> Path:
    dest = ZIPS / f"{quarter}.zip"
    if dest.exists():
        return dest
    request = urllib.request.Request(
        URL.format(quarter=quarter), headers={"User-Agent": user_agent}
    )
    ZIPS.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    with urllib.request.urlopen(request, timeout=600) as response, tmp.open("wb") as fh:
        while chunk := response.read(1 << 20):
            fh.write(chunk)
    tmp.rename(dest)
    time.sleep(1)  # well under sec.gov's 10 requests per second
    return dest


def write(rows: list[dict[str, Any]], quarter: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"labels-{quarter}.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("quarters", nargs="*", help="e.g. 2025q1")
    parser.add_argument("--last", type=int, help="the N most recent published quarters")
    args = parser.parse_args(argv)
    quarters = args.quarters or quarters_back(args.last or 8)
    if any(not re.fullmatch(r"[0-9]{4}q[1-4]", quarter) for quarter in quarters):
        print("quarters must use YYYYq1 through YYYYq4", file=sys.stderr)
        return 2

    user_agent = os.environ.get("FRA_SEC_USER_AGENT", "").strip()
    if "@" not in user_agent:
        print(
            'set FRA_SEC_USER_AGENT="Your Name you@example.com" (sec.gov policy)', file=sys.stderr
        )
        return 2
    import truststore  # system trust store; see scripts/corpus.py use_system_trust

    truststore.inject_into_ssl()
    total: Counter[str] = Counter()
    failed = False
    try:
        with locked_registry(POOL_METADATA, OUT.glob("labels-*.jsonl.gz")) as registry:
            record_pdf_metadata(registry, CANDIDATES.parent, GOLDEN_MANIFEST)
            for quarter in quarters:
                try:
                    rows, stats = extract(download(quarter, user_agent), registry)
                    # Persist identities (including excluded blind filers) before output.
                    registry.sec_outputs.add(f"labels-{quarter}.jsonl.gz")
                    save_registry(registry, POOL_METADATA)
                    path = write(rows, quarter)
                except PoolError:
                    raise  # an identity conflict stops the whole run, not just a quarter
                except Exception as exc:
                    print(f"{quarter}: failed, {type(exc).__name__}: {exc}", file=sys.stderr)
                    failed = True
                    continue
                total.update(stats)
                print(
                    f"{quarter}: {stats['rows']:>9,} rows  "
                    f"{stats['filers']:>6,} filers  -> {path.name}"
                )
    except (PoolError, OSError, ValueError) as exc:
        print(f"pool assignments fail: {exc}", file=sys.stderr)
        return 1
    print(
        f"total: {total['rows']:,} rows, train {total['rows_train']:,}, "
        f"model_test {total['rows_model_test']:,}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
