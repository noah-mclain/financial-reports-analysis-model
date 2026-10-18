"""Decode prepared SEC payloads and classify each row for the crosswalk.

A payload is hash-checked by ``sec_source_access`` before it gets here. This module checks its
schema, its filing identity and its declared row count, then gives each row a resolution kind
or a counted drop reason. Classification here is by crosswalk lookup alone: it needs no
approval, so it can audit coverage of a draft. Producing training examples is separate and
goes through ``sec_crosswalk.resolve_tag`` with approved records.
"""

from __future__ import annotations

import gzip
import json
import re
import zlib
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from dataset_contracts import DatasetError, text_lines
from pydantic import ValidationError
from sec_crosswalk import Crosswalk, CrosswalkEntry, TagKey
from sec_crosswalk_draft import CUSTOM_NAMESPACE, CUSTOM_VERSION, STATEMENT_CODES, CuratedMapping
from sec_source_access import VerifiedPayload
from sec_source_pass import PayloadRow

DROP_REASONS = (
    "unsupported_statement_code",
    "unsupported_namespace",
    "ambiguous_semantics",
    "parenthetical",
    "empty_label",
)
_VERSION = re.compile(r"([a-z][a-z0-9-]*)/(\d{4})")


@dataclass(frozen=True)
class Dropped:
    reason: str


@dataclass(frozen=True)
class Resolved:
    """A row with a crosswalk key. ``kind`` is mapped, null_entry, outside_crosswalk or
    custom_extension; ``canonical_id`` is set for mapped rows only."""

    kind: str
    key: TagKey
    canonical_id: str | None


def decode_payload(verified: VerifiedPayload) -> list[PayloadRow]:
    """The payload's rows, checked against the filing and artifact it was authorized for."""
    source_id = verified.descriptor.filing.source_record.source_id
    try:
        text = gzip.decompress(verified.payload).decode("utf-8")
    except (OSError, EOFError, zlib.error):
        raise DatasetError(f"{source_id}: payload is not gzip") from None
    except UnicodeDecodeError:
        raise DatasetError(f"{source_id}: payload is not UTF-8") from None
    rows: list[PayloadRow] = []
    seen: set[tuple[int, int]] = set()
    for number, line in text_lines(text):
        try:
            row = PayloadRow.model_validate_json(line)
        except ValidationError:
            raise DatasetError(f"{source_id}: payload line {number} is not a valid row") from None
        if row.adsh != source_id:
            raise DatasetError(f"{source_id}: payload line {number} belongs to another filing")
        if (row.report, row.line) in seen:
            raise DatasetError(f"{source_id}: report {row.report} line {row.line} appears twice")
        seen.add((row.report, row.line))
        rows.append(row)
    declared = verified.descriptor.artifact.row_count
    if len(rows) != declared:
        raise DatasetError(f"{source_id}: {len(rows)} rows, {declared} declared")
    return rows


class Classifier:
    """Classify rows against one mapping and crosswalk, indexing them once."""

    def __init__(self, mapping: CuratedMapping, crosswalk: Crosswalk) -> None:
        self._mapping = mapping
        self._entries = _index(crosswalk)
        self._excluded = _excluded(mapping)

    def classify(self, row: PayloadRow) -> Dropped | Resolved:
        return _classify(row, self._mapping, self._entries, self._excluded)

    def has_entry(self, key: TagKey) -> bool:
        return key in self._entries


def classify(row: PayloadRow, mapping: CuratedMapping, crosswalk: Crosswalk) -> Dropped | Resolved:
    return Classifier(mapping, crosswalk).classify(row)


def _index(crosswalk: Crosswalk) -> dict[TagKey, CrosswalkEntry]:
    return {entry.key: entry for entry in crosswalk.entries}


def _excluded(mapping: CuratedMapping) -> frozenset[tuple[str, str]]:
    return frozenset((x.statement, x.tag) for x in mapping.excluded)


def _classify(
    row: PayloadRow,
    mapping: CuratedMapping,
    entries: Mapping[TagKey, CrosswalkEntry],
    excluded: frozenset[tuple[str, str]],
) -> Dropped | Resolved:
    statement = STATEMENT_CODES.get(row.stmt)
    if statement is None:
        return Dropped("unsupported_statement_code")
    if row.inpth:
        return Dropped("parenthetical")
    if not row.plabel.split():
        return Dropped("empty_label")
    if row.version == row.adsh:
        key = TagKey(
            namespace=CUSTOM_NAMESPACE,
            tag_taxonomy_version=CUSTOM_VERSION,
            statement=statement,
            tag=row.tag,
        )
        return Resolved("custom_extension", key, None)
    parsed = _VERSION.fullmatch(row.version)
    if parsed is None or parsed.group(1) != mapping.namespace:
        return Dropped("unsupported_namespace")
    if (row.stmt, row.tag) in excluded:
        return Dropped("ambiguous_semantics")
    key = TagKey(
        namespace=parsed.group(1),
        tag_taxonomy_version=parsed.group(2),
        statement=statement,
        tag=row.tag,
    )
    entry = entries.get(key)
    if entry is None:
        return Resolved("outside_crosswalk", key, None)
    if entry.canonical_id is None:
        return Resolved("null_entry", key, None)
    return Resolved("mapped", key, entry.canonical_id)


@dataclass
class Coverage:
    """Counts only: rows by resolution kind and drop reason, and unlisted standard tags."""

    rows: int = 0
    by_kind: Counter[str] = field(default_factory=Counter)
    drops: Counter[str] = field(default_factory=Counter)
    unlisted: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    # Outside-crosswalk rows of a listed tag (a version with no entry) and of CI, CF and EQ.
    listed_without_entry: int = 0
    unmodeled: int = 0


def coverage_report(
    payloads: Iterable[VerifiedPayload], mapping: CuratedMapping, crosswalk: Crosswalk
) -> Coverage:
    entries, excluded = _index(crosswalk), _excluded(mapping)
    listed = {(r.statement, r.tag) for r in mapping.rows} | excluded
    report = Coverage()
    for verified in payloads:
        for row in decode_payload(verified):
            report.rows += 1
            result = _classify(row, mapping, entries, excluded)
            if isinstance(result, Dropped):
                report.drops[result.reason] += 1
                continue
            report.by_kind[result.kind] += 1
            if result.kind != "outside_crosswalk":
                continue
            if row.stmt not in ("BS", "IS"):
                report.unmodeled += 1
            elif (row.stmt, row.tag) in listed:
                report.listed_without_entry += 1
            else:
                report.unlisted[row.stmt][row.tag] += 1
    return report


def top_unlisted(
    report: Coverage, n: int, vocabulary: Mapping[str, Mapping[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """The n most frequent unlisted standard tags per statement, with their tag.txt definition
    from the latest vocabulary version that holds the tag."""
    latest = sorted(vocabulary)
    top: dict[str, list[dict[str, Any]]] = {}
    for stmt in ("BS", "IS"):
        ranked = sorted(report.unlisted.get(stmt, Counter()).items(), key=lambda t: (-t[1], t[0]))
        entries = []
        for tag, rows in ranked[:n]:
            found = next(
                (vocabulary[v][tag] for v in reversed(latest) if tag in vocabulary[v]), None
            )
            entries.append(
                {
                    "tag": tag,
                    "rows": rows,
                    "tlabel": found.tlabel if found else None,
                    "doc": found.doc if found else None,
                }
            )
        top[stmt] = entries
    return top


def outside_split(report: Coverage) -> dict[str, int]:
    """Where the outside-crosswalk rows are: unlisted BS and IS tags, listed tags that lack an
    entry for the row's version, and statements the taxonomy has no items for."""
    return {
        "bs_is_unlisted_tag": sum(sum(c.values()) for c in report.unlisted.values()),
        "bs_is_listed_tag_without_entry": report.listed_without_entry,
        "unmodeled_statements": report.unmodeled,
    }


def coverage_curve(report: Coverage, percents: Iterable[int]) -> dict[int, int]:
    """How many unlisted BS and IS tags, most frequent first, cover each share of their rows."""
    counts = sorted((n for c in report.unlisted.values() for n in c.values()), reverse=True)
    total = sum(counts)
    curve: dict[int, int] = {}
    for percent in percents:
        running = 0
        for needed, count in enumerate(counts, start=1):
            running += count
            if running * 100 >= percent * total:
                curve[percent] = needed
                break
        else:
            curve[percent] = 0
    return curve


def unlisted_jsonl(report: Coverage, vocabulary: Mapping[str, Mapping[str, Any]]) -> str:
    """Every unlisted BS and IS tag, most frequent first, with its definition in each tag
    taxonomy version of the vocabulary. Vocabulary text only; no filer label."""
    ranked = sorted(
        (
            (stmt, tag, rows)
            for stmt, counter in report.unlisted.items()
            for tag, rows in counter.items()
        ),
        key=lambda t: (-t[2], t[0], t[1]),
    )
    lines = []
    for stmt, tag, rows in ranked:
        versions = {
            version: {
                "tlabel": vocabulary[version][tag].tlabel,
                "doc": vocabulary[version][tag].doc,
            }
            for version in sorted(vocabulary)
            if tag in vocabulary[version]
        }
        lines.append(
            json.dumps(
                {"statement": stmt, "tag": tag, "rows": rows, "versions": versions},
                sort_keys=True,
                ensure_ascii=False,
            )
        )
    return "".join(line + "\n" for line in lines)


def report_json(report: Coverage, top: Mapping[str, Any]) -> str:
    """The coverage report as JSON text; it holds no label."""
    return json.dumps(
        {
            "rows": report.rows,
            "by_kind": dict(sorted(report.by_kind.items())),
            "drops": dict(sorted(report.drops.items())),
            "outside_split": outside_split(report),
            "distinct_unlisted_tags": {s: len(c) for s, c in sorted(report.unlisted.items())},
            "unlisted_tags_for_percent_of_rows": {
                str(p): n for p, n in coverage_curve(report, (50, 80, 90, 95, 99)).items()
            },
            "top_unlisted": top,
        },
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
    )
