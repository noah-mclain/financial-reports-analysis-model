"""Corpus collection: validate the pool split, download candidates, measure them, dedupe.

    uv run python scripts/corpus.py check
    uv run python scripts/corpus.py fetch [--pool train] [--force] [--new] [--id ID ...]

`check` enforces the split rules in eval/corpus/README.md and needs no network.
`fetch` downloads into var/corpus/<pool>/<id>.pdf (gitignored), measures page count and
text layer per page, compares every file against the golden set and against the rest of the
corpus, and records the results in eval/corpus/fetched.yaml. `--new` leaves out every document
already measured there, so a few added documents need no re-download of the rest.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import http.client
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from collections import Counter, defaultdict
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
SECTORS = ("bank", "insurer", "other_financial")
SUBSECTORS = (
    "investment_holding",
    "brokerage",
    "exchange_operator",
    "consumer_finance",
    "asset_manager",
    "other",
)
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
        report.errors.extend(_sector_errors(doc_id, role, doc.get("sector"), doc.get("subsector")))
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


def _sector_errors(doc_id: str, role: Any, sector: Any, subsector: Any) -> list[str]:
    """Negative controls say what kind of financial company they are, for the industry eval."""
    if role != "negative_control":
        return [f"{doc_id}: sector is only for negative_control"] if sector else []
    if sector not in SECTORS:
        return [f"{doc_id}: negative_control needs a sector ({', '.join(SECTORS)})"]
    if sector == "other_financial" and subsector not in SUBSECTORS:
        return [f"{doc_id}: other_financial needs a subsector ({', '.join(SUBSECTORS)})"]
    if sector != "other_financial" and subsector:
        return [f"{doc_id}: subsector is only for other_financial"]
    return []


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

    def __init__(self, delay_s: float = 1.0, retries: int = 3, backoff_s: float = 2.0) -> None:
        self.delay_s = delay_s
        self.retries = retries
        self.backoff_s = backoff_s
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._why: dict[str, str] = {}
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
                lines = (
                    self._read(request, timeout=30).decode("utf-8", errors="replace").splitlines()
                )
                assert parser is not None
                parser.parse(lines)
            except urllib.error.HTTPError as exc:
                # 4xx: no robots.txt, so everything is allowed. 5xx: treat as disallowed.
                parser = None if exc.code < 500 else _DENY_ALL
                if parser is _DENY_ALL:
                    self._why[host] = f"robots.txt returned HTTP {exc.code}"
            except (OSError, http.client.HTTPException) as exc:
                # Unreachable robots.txt means no permission (RFC 9309). Keep the cause: a TLS
                # or proxy failure here is a local problem, not the site saying no.
                parser = _DENY_ALL
                reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
                self._why[host] = f"robots.txt unreachable: {reason}"
            self._robots[host] = parser
        parser = self._robots[host]
        return parser is None or parser.can_fetch(USER_AGENT, url)

    def refusal(self, url: str) -> str:
        """Why `allowed` said no for this URL."""
        parts = urllib.parse.urlsplit(url)
        return self._why.get(f"{parts.scheme}://{parts.netloc}", "disallowed by robots.txt")

    def get(self, url: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        return self._read(request, timeout=120)

    def _read(self, request: urllib.request.Request, timeout: float) -> bytes:
        """One polite request, retried with backoff when the connection drops or times out.
        An HTTP status is an answer, not a dropped connection, so it is never retried."""
        host = urllib.parse.urlsplit(request.full_url).netloc
        for attempt in range(self.retries + 1):
            self._wait(host)
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    data: bytes = response.read()
                return data
            except urllib.error.HTTPError:
                raise
            except (OSError, http.client.HTTPException):
                if attempt == self.retries:
                    raise
                time.sleep(self.backoff_s * 2**attempt)
        raise AssertionError("unreachable")


_DENY_ALL = urllib.robotparser.RobotFileParser()
_DENY_ALL.parse(["User-agent: *", "Disallow: /"])


def download(url: str, dest: Path, polite: Politeness) -> None:
    if not polite.allowed(url):
        raise PermissionError(polite.refusal(url))
    data = polite.get(url)
    if not data.startswith(b"%PDF"):
        raise ValueError(f"not a PDF (starts with {data[:16]!r})")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def after_failure(
    prior: dict[str, Any] | None, entry: dict[str, Any], error: str
) -> dict[str, Any]:
    """A failed download never erases facts measured by an earlier run (possibly on another
    machine): the bytes behind the URL were already hashed and measured."""
    if prior and "sha256" in prior:
        return {**prior, "last_error": error}
    return {**entry, "status": "failed", "error": error}


def refused(error: str) -> bool:
    """The site said no to automated clients, as opposed to a dropped connection."""
    return error.startswith("PermissionError") or any(
        f"HTTP Error {code}" in error for code in (401, 403)
    )


def use_system_trust() -> None:
    """Verify TLS against the operating system's trust store. uv's standalone Python does not
    read the macOS keychain, so plain urllib fails every HTTPS request there."""
    import truststore

    truststore.inject_into_ssl()


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


def write_fetched(fetched: dict[str, dict[str, Any]]) -> None:
    FETCHED.write_text(
        "# Written by scripts/corpus.py fetch. Measured facts; do not edit by hand.\n"
        + yaml.safe_dump({"documents": fetched}, sort_keys=True, allow_unicode=True),
        encoding="utf-8",
    )


def to_fetch(
    documents: list[dict[str, Any]],
    fetched: dict[str, dict[str, Any]],
    *,
    pool: str | None = None,
    new: bool = False,
    ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """The candidates one `fetch` run covers: one pool or all, only the documents named in
    `ids`, and with `new` only those not measured yet. A refused download is recorded as failed
    with no measurement, so `new` tries it again, which is also how a file saved from a browser
    gets measured."""
    unknown = sorted(set(ids or []) - {doc["id"] for doc in documents})
    if unknown:
        raise ValueError(f"not in candidates.yaml: {', '.join(unknown)}")
    return [
        doc
        for doc in documents
        if (not pool or doc["pool"] == pool)
        and (not ids or doc["id"] in ids)
        and not (new and "sha256" in fetched.get(doc["id"], {}))
    ]


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
    failures: Counter[str] = Counter()
    by_hand: list[tuple[Path, str]] = []
    for doc in to_fetch(documents, fetched, pool=args.pool, new=args.new, ids=args.id):
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
            prior = fetched.get(doc_id, {})
            if prior.get("sha256") == entry["sha256"] and "retrieved" in prior:
                entry["retrieved"] = prior["retrieved"]  # the same bytes, first retrieved then
            entry["bytes"] = dest.stat().st_size
            entry.update(measure(dest))
            entry["status"] = "new"
        except Exception as exc:  # recorded per document, the run continues
            entry = after_failure(fetched.get(doc_id), entry, f"{type(exc).__name__}: {exc}")
        fetched[doc_id] = entry
        problem = entry.get("error") or entry.get("last_error")
        note = f"kept earlier measurement; {problem}" if entry.get("last_error") else problem
        print(f"{doc_id:<32} {entry['status']:<8} {note or entry.get('text_layer', '')}")
        if problem:
            failures[problem] += 1
            if entry["status"] == "failed" and refused(problem):
                by_hand.append((dest, doc["url"]))

    if failures:
        print(f"\n{sum(failures.values())} downloads failed. Most common causes:", file=sys.stderr)
        for cause, n in failures.most_common(3):
            print(f"  {n:>4}  {cause}", file=sys.stderr)
        if any(not refused(cause) for cause in failures):
            print("Run again to retry dropped connections.", file=sys.stderr)
    if by_hand:
        print(
            "\nThese sites refuse automated downloads. Save each one from a browser to the path"
            " shown, then run again to measure it:",
            file=sys.stderr,
        )
        for path, url in by_hand:
            print(f"  {path.relative_to(ROOT)}\n      {url}", file=sys.stderr)

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

    write_fetched(fetched)
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
    fetch.add_argument("--new", action="store_true", help="only documents not measured yet")
    fetch.add_argument("--id", nargs="+", help="only these documents (ids in candidates.yaml)")
    fetch.set_defaults(func=cmd_fetch)
    args = parser.parse_args(argv)
    use_system_trust()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
