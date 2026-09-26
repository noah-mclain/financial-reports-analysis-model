"""SEC Financial Statement Data Sets: English line-item labels at scale.

    FRA_SEC_USER_AGENT="Your Name you@example.com" \
        uv run python training/sources/sec_fsds.py 2024q1 2024q2 ...
    uv run python training/sources/sec_fsds.py --last 8

Each quarterly zip holds `sub.txt` (one row per filing) and `pre.txt` (how each line item is
presented: statement, tag, label). Both are read straight from the zip, never unpacked. The
output is one gzipped JSONL file per quarter in training/data/sec_fsds/, one row per distinct
(filer, statement, label, tag), with the filer's pool:

- Split by CIK, the same rule as the PDF corpus: a filer is wholly in train or model_test.
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
import hashlib
import io
import json
import os
import sys
import time
import urllib.request
import zipfile
from collections import Counter
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CANDIDATES = ROOT / "eval/corpus/candidates.yaml"
ZIPS = ROOT / "var/sec_fsds"
OUT = ROOT / "training/data/sec_fsds"
URL = "https://www.sec.gov/files/dera/data/financial-statement-data-sets/{quarter}.zip"

FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "40-F"}
STATEMENTS = {"BS", "IS", "CI", "CF", "EQ"}
FINANCIAL_SIC = range(6000, 6500)
MODEL_TEST_SHARE = 15  # percent of filers held out

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
    """CIK -> pool, for SEC filers that also appear in the PDF corpus."""
    return {int(d["cik"]): d["pool"] for d in documents if d.get("cik")}


def pool_for(cik: int, pinned: dict[int, str]) -> str:
    if cik in pinned:
        return pinned[cik]
    bucket = int(hashlib.sha256(f"cik:{cik}".encode()).hexdigest(), 16) % 100
    return "model_test" if bucket < MODEL_TEST_SHARE else "train"


def role_for(sic: str) -> str:
    return "negative_control" if sic.isdigit() and int(sic) in FINANCIAL_SIC else "corporate"


def _rows(archive: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    with archive.open(name) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
        yield from csv.DictReader(text, delimiter="\t", quoting=csv.QUOTE_NONE)


def extract(zip_path: Path, pinned: dict[int, str]) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Distinct presented labels from one quarter, with pool and role."""
    stats: Counter[str] = Counter()
    with zipfile.ZipFile(zip_path) as archive:
        filings: dict[str, dict[str, Any]] = {}
        for sub in _rows(archive, "sub.txt"):
            stats["filings"] += 1
            if sub["form"] not in FORMS:
                continue
            cik = int(sub["cik"])
            pool = pool_for(cik, pinned)
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

    user_agent = os.environ.get("FRA_SEC_USER_AGENT", "").strip()
    if "@" not in user_agent:
        print(
            'set FRA_SEC_USER_AGENT="Your Name you@example.com" (sec.gov policy)', file=sys.stderr
        )
        return 2
    pinned = pinned_ciks(yaml.safe_load(CANDIDATES.read_text(encoding="utf-8"))["documents"])

    total: Counter[str] = Counter()
    for quarter in quarters:
        try:
            rows, stats = extract(download(quarter, user_agent), pinned)
        except Exception as exc:  # one bad quarter does not stop the rest
            print(f"{quarter}: failed, {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        path = write(rows, quarter)
        total.update(stats)
        print(f"{quarter}: {stats['rows']:>9,} rows  {stats['filers']:>6,} filers  -> {path.name}")
    print(
        f"total: {total['rows']:,} rows, train {total['rows_train']:,}, "
        f"model_test {total['rows_model_test']:,}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
