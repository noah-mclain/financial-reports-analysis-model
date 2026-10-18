"""Build the normalization train/valid files from approved, caller-supplied records.

    make dataset-build EXAMPLES=... SOURCES=... APPROVED=... TOKENIZER_DIR=... OUT_DIR=...

Every input is an explicit path. The approvals file is the caller's trust boundary: this
command checks each example against it but cannot tell whether the hashes were reviewed.
It composes ``partition_examples``, ``build_dataset`` and ``write_dataset``; the rules live there.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path

import yaml
from chat_tokenizer import load_chat_counter
from dataset_builder import TOKEN_LIMIT, build_dataset, write_dataset
from dataset_contracts import (
    Contract,
    DatasetError,
    NormalizationExample,
    PartitionConfig,
    SourceRecord,
    partition_examples,
    text_lines,
)
from harness.holdout_records import read_looks, read_moves
from harness.paths import HOLDOUT_MOVES, ROOT, SCORING_LOG
from pydantic import ValidationError
from sec_source_access import read_registry

from fra_core.pools import PoolError
from fra_core.split import Look, Move, SplitError, cik_key, issuer_key
from fra_core.taxonomy import load_taxonomy
from pool_store import REGISTRY_RELATIVE_PATH

SHA256 = re.compile(r"[0-9a-f]{64}")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DatasetError(f"{path}: cannot read: {exc.strerror}") from exc


def read_jsonl[T: Contract](path: Path, model: type[T]) -> list[T]:
    """One model per non-blank line; a bad line is reported with its file and number."""
    records: list[T] = []
    for number, line in text_lines(_read_text(path)):
        try:
            records.append(model.model_validate_json(line))
        except ValidationError as exc:
            raise DatasetError(f"{path}:{number}: {exc}") from exc
    return records


def read_approved(path: Path) -> frozenset[str]:
    """Example content hashes, one per line; blank lines and ``#`` comments are ignored."""
    hashes: set[str] = set()
    for number, line in text_lines(_read_text(path)):
        text = line.strip()
        if text.startswith("#"):
            continue
        if not SHA256.fullmatch(text):
            raise DatasetError(f"{path}:{number}: not a lowercase sha256 hex digest")
        hashes.add(text)
    return frozenset(hashes)


def _positive(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not an integer") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"{value} is not at least 1")
    return value


def _moves(path: Path, looks: list[Look]) -> list[Move]:
    try:
        return read_moves(path, looks)
    except yaml.YAMLError as exc:
        raise DatasetError(f"{path}: {exc}") from exc


def _roster_moves(moves: list[Move], sources: list[SourceRecord]) -> list[Move]:
    """The recorded moves of issuers in this roster; the record also covers other corpora."""
    keys = {issuer_key(s.issuer.name or cik_key(str(s.issuer.cik))) for s in sources}
    kept = [m for m in moves if m.key in keys]
    print(f"moves applied: {len(kept)} of {len(moves)} recorded")
    return kept


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--examples", type=Path, required=True, help="JSONL of examples")
    parser.add_argument("--sources", type=Path, required=True, help="JSONL of source records")
    parser.add_argument("--approved", type=Path, required=True, help="approved example hashes")
    parser.add_argument("--tokenizer-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--registry", type=Path, default=ROOT / REGISTRY_RELATIVE_PATH)
    parser.add_argument("--moves", type=Path, default=HOLDOUT_MOVES)
    parser.add_argument("--looks", type=Path, default=SCORING_LOG)
    parser.add_argument("--max-per-issuer", type=_positive, default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        sources = read_jsonl(args.sources, SourceRecord)
        approved = read_approved(args.approved)
        looks = read_looks(args.looks)
        moves = _roster_moves(_moves(args.moves, looks), sources)
        examples = read_jsonl(args.examples, NormalizationExample)
        count_tokens, tokenizer_id = load_chat_counter(args.tokenizer_dir)
        partitioned = partition_examples(
            examples,
            sources=sources,
            registry=read_registry(args.registry),
            config=PartitionConfig(
                approved_example_sha256=approved, max_per_issuer=args.max_per_issuer
            ),
            taxonomy=load_taxonomy(),
            moves=moves,
            looks=looks,
        )
        built = build_dataset(
            partitioned,
            count_tokens=count_tokens,
            token_limit=TOKEN_LIMIT,
            tokenizer_id=tokenizer_id,
        )
        write_dataset(built, args.out_dir)
    except (DatasetError, SplitError, PoolError, OSError, ValidationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"fit rows: {len(built.fit_rows)}, validation rows: {len(built.validation_rows)}")
    print(f"manifest: {args.out_dir / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
