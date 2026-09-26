"""Corpus collection: validate the pool split, download candidates, measure them, dedupe.

    uv run python scripts/corpus.py check
    uv run python scripts/corpus.py fetch [--pool train] [--force]

`check` enforces the split rules in eval/corpus/README.md and needs no network.
`fetch` downloads into var/corpus/<pool>/<id>.pdf (gitignored), measures page count and
text layer per page, compares every file against the golden set and against the rest of the
corpus, and records the results in eval/corpus/fetched.yaml.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
CANDIDATES = ROOT / "eval/corpus/candidates.yaml"
FETCHED = ROOT / "eval/corpus/fetched.yaml"
GOLDEN_MANIFEST = ROOT / "eval/golden/manifest.yaml"
GOLDEN_DIR = ROOT / "eval/golden"
STORE = ROOT / "var/corpus"

POOLS = ("dev", "train", "model_test", "blind")
ROLES = ("corporate", "negative_control")
# A page with fewer characters than this has no usable text layer (same rule as the locator).
MIN_TEXT_CHARS = 50
USER_AGENT = "Mozilla/5.0 (fra-corpus; research use of public filings)"

_SUFFIXES = {
    "company",
    "co",
    "the",
    "group",
    "pjsc",
    "sae",
    "plc",
    "inc",
    "corporation",
    "corp",
    "ltd",
    "limited",
    "for",
}


def issuer_key(name: str) -> str:
    """Normalize an issuer name so 'Almarai Company' and 'ALMARAI CO.' compare equal."""
    words = re.findall(r"[a-z0-9]+", name.lower().replace(".", ""))
    return " ".join(w for w in words if w not in _SUFFIXES)


@dataclass
class CheckReport:
    errors: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)  # candidate ids whose issuer is golden


def check(documents: list[dict[str, Any]], golden_issuers: set[str]) -> CheckReport:
    """Apply the split rules. Every error means a leak between pools."""
    report = CheckReport()
    seen_ids: set[str] = set()
    seen_urls: dict[str, str] = {}
    issuer_pools: dict[str, set[str]] = defaultdict(set)

    for doc in documents:
        doc_id, pool, role = doc["id"], doc.get("pool"), doc.get("role")
        if doc_id in seen_ids:
            report.errors.append(f"{doc_id}: duplicate id")
        seen_ids.add(doc_id)
        if pool not in POOLS:
            report.errors.append(f"{doc_id}: unknown pool {pool!r}")
        if role not in ROLES:
            report.errors.append(f"{doc_id}: unknown role {role!r}")
        url = doc.get("url")
        if url in seen_urls:
            report.errors.append(f"{doc_id}: same url as {seen_urls[url]}")
        elif url:
            seen_urls[url] = doc_id

        key = issuer_key(doc["issuer"])
        issuer_pools[key].add(str(pool))
        if key in golden_issuers:
            report.existing.append(doc_id)
            if pool != "dev":
                report.errors.append(
                    f"{doc_id}: issuer {doc['issuer']!r} is in the golden set, so it belongs in dev"
                )

    for key, pools in sorted(issuer_pools.items()):
        if len(pools) > 1:
            report.errors.append(f"issuer {key!r} spans pools {sorted(pools)}")
    return report


def text_layer(chars_per_page: list[int]) -> str:
    """digital, scanned or mixed, decided page by page."""
    without = sum(1 for n in chars_per_page if n < MIN_TEXT_CHARS)
    if without == 0:
        return "digital"
    if without == len(chars_per_page):
        return "scanned"
    return "mixed"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def measure(path: Path) -> dict[str, Any]:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(path)
    try:
        chars = []
        for page in pdf:
            textpage = page.get_textpage()
            chars.append(len(textpage.get_text_range().strip()))
            textpage.close()
            page.close()
    finally:
        pdf.close()
    return {
        "pages": len(chars),
        "pages_without_text": sum(1 for n in chars if n < MIN_TEXT_CHARS),
        "text_layer": text_layer(chars),
    }


def assign_pool(issuer: str) -> str:
    """Default pool for a new issuer: a stable hash of its key, 65% train, 20% model_test,
    15% blind. Golden issuers are always dev. Explicit pools in candidates.yaml win."""
    bucket = int(hashlib.sha256(issuer_key(issuer).encode()).hexdigest(), 16) % 100
    if bucket < 65:
        return "train"
    if bucket < 85:
        return "model_test"
    return "blind"


class Politeness:
    """robots.txt per RFC 9309 and at most one request per second per host."""

    def __init__(self, delay_s: float = 1.0) -> None:
        self.delay_s = delay_s
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._last: dict[str, float] = {}

    def _wait(self, host: str) -> None:
        elapsed = time.monotonic() - self._last.get(host, 0.0)
        if elapsed < self.delay_s:
            time.sleep(self.delay_s - elapsed)
        self._last[host] = time.monotonic()

    def allowed(self, url: str) -> bool:
        parts = urllib.parse.urlsplit(url)
        host = f"{parts.scheme}://{parts.netloc}"
        if host not in self._robots:
            self._wait(parts.netloc)
            parser: urllib.robotparser.RobotFileParser | None = urllib.robotparser.RobotFileParser()
            request = urllib.request.Request(
                f"{host}/robots.txt", headers={"User-Agent": USER_AGENT}
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    lines = response.read().decode("utf-8", errors="replace").splitlines()
                assert parser is not None
                parser.parse(lines)
            except urllib.error.HTTPError as exc:
                # 4xx: no robots.txt, so everything is allowed. 5xx: treat as disallowed.
                parser = None if exc.code < 500 else _DENY_ALL
            except OSError:
                parser = _DENY_ALL
            self._robots[host] = parser
        parser = self._robots[host]
        return parser is None or parser.can_fetch(USER_AGENT, url)

    def get(self, url: str) -> bytes:
        self._wait(urllib.parse.urlsplit(url).netloc)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=120) as response:
            data: bytes = response.read()
        return data


_DENY_ALL = urllib.robotparser.RobotFileParser()
_DENY_ALL.parse(["User-agent: *", "Disallow: /"])


def download(url: str, dest: Path, polite: Politeness) -> None:
    if not polite.allowed(url):
        raise PermissionError("disallowed by robots.txt, or robots.txt unreachable")
    data = polite.get(url)
    if not data.startswith(b"%PDF"):
        raise ValueError(f"not a PDF (starts with {data[:16]!r})")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def golden_index() -> tuple[set[str], dict[str, str]]:
    """Golden issuer keys, and sha256 -> golden document id."""
    manifest = load_yaml(GOLDEN_MANIFEST)
    issuers = {issuer_key(d["issuer"]) for d in manifest["documents"]}
    hashes = {}
    for d in manifest["documents"]:
        path = GOLDEN_DIR / d["file"]
        if path.exists():
            hashes[sha256_file(path)] = d["id"]
    return issuers, hashes


def cmd_check(_: argparse.Namespace) -> int:
    documents = load_yaml(CANDIDATES)["documents"]
    golden_issuers, _hashes = golden_index()
    report = check(documents, golden_issuers)

    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for doc in documents:
        pool = counts[doc["pool"]]
        pool["documents"] += 1
        pool[f"lang:{doc.get('language') or '?'}"] += 1
        if doc.get("role") == "negative_control":
            pool["negative_control"] += 1
    for pool_name in POOLS:
        issuers = {issuer_key(d["issuer"]) for d in documents if d["pool"] == pool_name}
        detail = ", ".join(f"{k}={v}" for k, v in sorted(counts[pool_name].items()))
        print(f"{pool_name:<11} issuers={len(issuers):<3} {detail}")
    if report.existing:
        print(f"\nexisting issuer (golden set), kept in dev: {', '.join(report.existing)}")
    for error in report.errors:
        print(f"ERROR {error}", file=sys.stderr)
    return 1 if report.errors else 0


def cmd_fetch(args: argparse.Namespace) -> int:
    documents = load_yaml(CANDIDATES)["documents"]
    golden_issuers, golden_hashes = golden_index()
    if check(documents, golden_issuers).errors:
        print("split rules fail; run `check` first", file=sys.stderr)
        return 1

    fetched: dict[str, dict[str, Any]] = (
        (load_yaml(FETCHED) or {}).get("documents", {}) if FETCHED.exists() else {}
    )
    today = dt.date.today().isoformat()
    polite = Politeness()
    for doc in documents:
        if args.pool and doc["pool"] != args.pool:
            continue
        doc_id = doc["id"]
        dest = STORE / doc["pool"] / f"{doc_id}.pdf"
        entry: dict[str, Any] = {"pool": doc["pool"]}
        try:
            if args.force or not dest.exists():
                download(doc["url"], dest, polite)
                entry["retrieved"] = today
            else:
                entry["retrieved"] = fetched.get(doc_id, {}).get("retrieved", today)
            entry["sha256"] = sha256_file(dest)
            entry["bytes"] = dest.stat().st_size
            entry.update(measure(dest))
            entry["status"] = "new"
        except Exception as exc:  # recorded per document, the run continues
            entry["status"] = "failed"
            entry["error"] = f"{type(exc).__name__}: {exc}"
        fetched[doc_id] = entry
        print(
            f"{doc_id:<32} {entry['status']:<8} {entry.get('text_layer', entry.get('error', ''))}"
        )

    # Dedupe after the loop so the result does not depend on download order.
    by_hash: dict[str, list[str]] = defaultdict(list)
    for doc_id, entry in sorted(fetched.items()):
        if "sha256" in entry:
            by_hash[entry["sha256"]].append(doc_id)
    for sha, ids in by_hash.items():
        for doc_id in ids:
            entry = fetched[doc_id]
            entry.pop("duplicate_of", None)
            if sha in golden_hashes:
                entry["status"] = "existing_document"
                entry["duplicate_of"] = golden_hashes[sha]
            elif doc_id != ids[0]:
                entry["status"] = "duplicate"
                entry["duplicate_of"] = ids[0]
    for doc in documents:
        found = fetched.get(doc["id"])
        if found and found["status"] == "new" and issuer_key(doc["issuer"]) in golden_issuers:
            found["status"] = "existing_issuer"

    FETCHED.write_text(
        "# Written by scripts/corpus.py fetch. Measured facts; do not edit by hand.\n"
        + yaml.safe_dump({"documents": fetched}, sort_keys=True, allow_unicode=True),
        encoding="utf-8",
    )
    leaks = [
        i
        for i, e in fetched.items()
        if e["pool"] == "blind" and e["status"] in {"existing_document", "existing_issuer"}
    ]
    if leaks:
        print(f"ERROR blind documents already in the golden set: {leaks}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="validate the pool split").set_defaults(func=cmd_check)
    fetch = sub.add_parser("fetch", help="download, measure and dedupe")
    fetch.add_argument("--pool", choices=POOLS)
    fetch.add_argument("--force", action="store_true", help="download again even if present")
    fetch.set_defaults(func=cmd_fetch)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
