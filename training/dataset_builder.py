"""Render approved fit/validation supervision as chat rows and audit their token lengths."""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from dataset_contracts import (
    Contract,
    DatasetError,
    DatasetManifest,
    NormalizationExample,
    PartitionedExamples,
    canonical_json,
    example_digest,
)

from fra_core.split import cik_key

BUILDER_VERSION = 1
TOKEN_LIMIT = 2048
SYSTEM_PROMPT = (
    "Map the financial statement line item to a canonical id from the taxonomy, "
    "or null if no canonical id applies."
)
TRAIN_FILE = "train.jsonl"
VALID_FILE = "valid.jsonl"
MANIFEST_FILE = "manifest.json"
# mlx_lm reads a test split by this name; the builder never writes it and refuses to sit beside it.
HOLDOUT_FILE = "test.jsonl"

Messages = list[dict[str, str]]
Row = dict[str, Messages]
TokenCounter = Callable[[Messages], int]


class LengthStats(Contract):
    count: int
    max: int
    mean: str
    p95: int


class DatasetBuildManifest(Contract):
    dataset: DatasetManifest
    builder_version: int
    system_prompt_sha256: str
    tokenizer_id: str
    token_limit: int
    fit_rows_before: int
    fit_rows_after: int
    validation_rows_before: int
    validation_rows_after: int
    fit_lengths: LengthStats
    validation_lengths: LengthStats
    fit_strata: dict[str, int]
    validation_strata: dict[str, int]
    train_sha256: str
    valid_sha256: str


@dataclass(frozen=True)
class BuiltDataset:
    fit_rows: tuple[Row, ...]
    validation_rows: tuple[Row, ...]
    manifest: DatasetBuildManifest


def normalize_label(label: str) -> str:
    """NFKC, casefolded, whitespace collapsed: the label identity used for deduplication."""
    return " ".join(unicodedata.normalize("NFKC", label).casefold().split())


def render_row(example: NormalizationExample) -> Row:
    """One mlx_lm chat row: the label and statement in, the canonical id (or null) out."""
    prompt = canonical_json({"label": example.label, "statement": example.statement.value})
    answer = canonical_json({"canonical_id": example.canonical_id})
    return {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": answer},
        ]
    }


def dedupe_group_key(cik: int | str, statement: str, label: str) -> tuple[str, str, str]:
    """The deduplication group: issuer (the split's CIK key), statement and normalized label."""
    return (cik_key(cik), statement, normalize_label(label))


def dedupe_lowest[T](
    items: Iterable[T],
    *,
    group: Callable[[T], tuple[str, str, str]],
    target: Callable[[T], str | None],
    ident: Callable[[T], str],
) -> tuple[list[T], list[tuple[str, ...]]]:
    """Keep one item per group and target (lowest id), in id order.

    A group whose members have different targets is not resolved: it is left out of the kept
    items and its ids are returned, so the caller decides whether that is an error.
    """
    groups: defaultdict[tuple[str, str, str], list[T]] = defaultdict(list)
    for item in sorted(items, key=ident):
        groups[group(item)].append(item)
    kept: list[T] = []
    conflicts: list[tuple[str, ...]] = []
    for members in groups.values():
        if len({target(m) for m in members}) > 1:
            conflicts.append(tuple(ident(m) for m in members))
            continue
        kept.append(members[0])
    return sorted(kept, key=ident), conflicts


def dedupe_per_issuer(
    examples: Sequence[NormalizationExample],
) -> tuple[NormalizationExample, ...]:
    """Keep one example per issuer, statement, normalized label and target (lowest example_id)."""
    for example in examples:
        # The contracts require a CIK for every authorized row.
        if example.source_record.issuer.cik is None:
            raise DatasetError(
                f"{example.example_id}: issuer has no CIK for per-issuer deduplication"
            )
    kept, conflicts = dedupe_lowest(
        examples,
        group=lambda e: dedupe_group_key(
            str(e.source_record.issuer.cik), e.statement.value, e.label
        ),
        target=lambda e: e.canonical_id,
        ident=lambda e: e.example_id,
    )
    if conflicts:
        raise DatasetError(f"conflicting targets for one issuer label: {', '.join(conflicts[0])}")
    return tuple(kept)


def _serialize(rows: Sequence[Row]) -> bytes:
    return "".join(json.dumps(r, ensure_ascii=False, allow_nan=False) + "\n" for r in rows).encode(
        "utf-8"
    )


def _stats(counts: Sequence[int]) -> LengthStats:
    ordered = sorted(counts)
    mean = (Decimal(sum(ordered)) / Decimal(len(ordered))).quantize(Decimal("0.001"))
    return LengthStats(
        count=len(ordered),
        max=ordered[-1],
        mean=str(mean),
        p95=ordered[math.ceil(0.95 * len(ordered)) - 1],
    )


def _strata(examples: Sequence[NormalizationExample]) -> dict[str, int]:
    counts = Counter(
        f"{e.source_record.source.value}|{e.source_record.language}|{e.source_record.period}"
        for e in examples
    )
    return dict(sorted(counts.items()))


def _count(
    examples: Sequence[NormalizationExample],
    rows: Sequence[Row],
    count_tokens: TokenCounter,
    token_limit: int,
    over: list[str],
) -> list[int]:
    counts = [count_tokens(r["messages"]) for r in rows]
    for example, n in zip(examples, counts, strict=True):
        if type(n) is not int or n < 0:
            raise DatasetError(
                f"{example.example_id}: count_tokens returned {n!r}, not a non-negative int"
            )
    over += [
        f"{e.example_id} ({n})" for e, n in zip(examples, counts, strict=True) if n > token_limit
    ]
    return counts


def build_dataset(
    partitioned: PartitionedExamples,
    *,
    count_tokens: TokenCounter,
    token_limit: int = TOKEN_LIMIT,
    tokenizer_id: str,
) -> BuiltDataset:
    """Deduplicate, render and length-audit both parts; an over-length row is an error.

    ``count_tokens`` returns the token count of the chat-templated sequence for a message
    list. Rows are never truncated or dropped to fit, because mlx_lm truncates silently.
    """
    if type(token_limit) is not int or token_limit < 1:
        raise DatasetError(f"token_limit must be a positive int, got {token_limit!r}")
    for name, original, expected in (
        ("fit", partitioned.fit, partitioned.manifest.fit_sha256),
        ("validation", partitioned.validation, partitioned.manifest.validation_sha256),
    ):
        if example_digest(original) != expected:
            raise DatasetError(f"{name} examples do not match the upstream manifest digest")
    parts: dict[str, tuple[tuple[NormalizationExample, ...], list[int], tuple[Row, ...]]] = {}
    over: list[str] = []
    for name, original in (("fit", partitioned.fit), ("validation", partitioned.validation)):
        kept = dedupe_per_issuer(original)
        if not kept:
            raise DatasetError(f"{name} part is empty after deduplication")
        rows = tuple(render_row(e) for e in kept)
        parts[name] = (kept, _count(kept, rows, count_tokens, token_limit, over), rows)
    if over:
        raise DatasetError(f"rows exceed {token_limit} tokens: {', '.join(over)}")
    (fit, fit_counts, fit_rows), (valid, valid_counts, valid_rows) = (
        parts["fit"],
        parts["validation"],
    )
    manifest = DatasetBuildManifest(
        dataset=partitioned.manifest,
        builder_version=BUILDER_VERSION,
        system_prompt_sha256=hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
        tokenizer_id=tokenizer_id,
        token_limit=token_limit,
        fit_rows_before=len(partitioned.fit),
        fit_rows_after=len(fit),
        validation_rows_before=len(partitioned.validation),
        validation_rows_after=len(valid),
        fit_lengths=_stats(fit_counts),
        validation_lengths=_stats(valid_counts),
        fit_strata=_strata(fit),
        validation_strata=_strata(valid),
        train_sha256=hashlib.sha256(_serialize(fit_rows)).hexdigest(),
        valid_sha256=hashlib.sha256(_serialize(valid_rows)).hexdigest(),
    )
    return BuiltDataset(fit_rows=fit_rows, validation_rows=valid_rows, manifest=manifest)


def write_dataset(built: BuiltDataset, out_dir: Path) -> None:
    """Write train.jsonl, valid.jsonl and manifest.json; never overwrite, never write test.jsonl."""
    for name in (TRAIN_FILE, VALID_FILE, MANIFEST_FILE, HOLDOUT_FILE):
        if (out_dir / name).exists():
            raise DatasetError(f"{out_dir / name} already exists; refusing to write beside it")
    train, valid = _serialize(built.fit_rows), _serialize(built.validation_rows)
    for name, data, expected in (
        ("train", train, built.manifest.train_sha256),
        ("valid", valid, built.manifest.valid_sha256),
    ):
        if hashlib.sha256(data).hexdigest() != expected:
            raise DatasetError(f"{name} rows do not match the manifest sha256")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / TRAIN_FILE).write_bytes(train)
    (out_dir / VALID_FILE).write_bytes(valid)
    manifest = json.loads(built.manifest.model_dump_json())
    (out_dir / MANIFEST_FILE).write_text(
        json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
