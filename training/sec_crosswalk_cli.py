"""Commands for the SEC crosswalk package: draft it, sample it for the owner, record approvals.

    make sec-crosswalk-draft ARCHIVES="a.zip b.zip" OUT_DIR=...
    make sec-crosswalk-approve CROSSWALK=... POLICIES=... VERDICTS=... REVIEWER_ID=... \
        AUTHORITY_ID=... EVIDENCE_REFERENCE=... OUT_DIR=...

Each command writes new files only and refuses to overwrite. The approval records are for the
owner's local trust store (``var/approvals/``, untracked); nothing here writes into the repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from dataset_contracts import DatasetError, text_lines
from exclusive_write import write_new
from pydantic import BaseModel, TypeAdapter, ValidationError
from sec_crosswalk import AbstentionPolicy, Crosswalk, CrosswalkError
from sec_crosswalk_draft import (
    MAPPING_PATH,
    build_draft,
    build_policies,
    load_mapping,
    merge_vocabularies,
    read_tag_vocabulary,
    unmatched_rows,
)
from sec_crosswalk_review import approval_records, owner_sample, review_packet

from fra_core.taxonomy import load_taxonomy

_POLICIES = TypeAdapter(tuple[AbstentionPolicy, ...])


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise DatasetError(f"{path}: cannot read: {exc.strerror}") from exc


def _json[T: BaseModel](path: Path, model: type[T]) -> T:
    try:
        return model.model_validate_json(_read(path))
    except ValidationError as exc:
        raise DatasetError(f"{path}: {exc}") from exc


def _dump(value: object) -> str:
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_REQUIRED = {
    "subjects": (),
    "entry": ("key", "canonical_id"),
    "policy": ("key",),
    "excluded": ("key",),
}


def _read_packet(path: Path) -> list[dict[str, Any]]:
    try:
        text = _read(path).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DatasetError(f"{path}: not UTF-8: {exc.reason}") from exc
    rows: list[dict[str, Any]] = []
    for number, line in text_lines(text):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetError(f"{path}:{number}: not JSON: {exc.msg}") from exc
        if not isinstance(row, dict) or row.get("kind") not in _REQUIRED:
            raise DatasetError(f"{path}:{number}: not a review packet row")
        for name in _REQUIRED[row["kind"]]:
            if name not in row:
                raise DatasetError(f"{path}:{number}: a {row['kind']} row has no {name!r}")
        rows.append(row)
    return rows


def draft(args: argparse.Namespace) -> None:
    taxonomy = load_taxonomy()
    mapping = load_mapping(args.mapping, taxonomy)
    vocabulary = merge_vocabularies(
        d for archive in args.archives for d in read_tag_vocabulary(archive)
    )
    unmatched = unmatched_rows(vocabulary, mapping)
    if unmatched:
        raise DatasetError(
            f"{len(unmatched)} mapping rows match no tag in the supplied archives: "
            + ", ".join(unmatched)
        )
    crosswalk = build_draft(vocabulary, mapping, taxonomy)
    policies = build_policies(vocabulary, mapping, taxonomy)
    packet = review_packet(crosswalk, policies, vocabulary, mapping, taxonomy)
    files = {
        args.out_dir / "crosswalk.json": _dump(crosswalk.model_dump(mode="json")),
        args.out_dir / "policies.json": _dump([p.model_dump(mode="json") for p in policies]),
        args.out_dir / "review_packet.jsonl": "".join(
            json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n" for row in packet
        ),
    }
    write_new(files)
    print(f"mapping sha256: {mapping.sha256}")
    print(f"crosswalk sha256: {crosswalk.content_sha256()} ({len(crosswalk.entries)} entries)")
    for policy in policies:
        print(f"policy {policy.policy_id} sha256: {policy.content_sha256()}")
    for path, text in files.items():
        print(f"{path.name} file sha256: {_sha(text)}")


def sample(args: argparse.Namespace) -> None:
    packet = _read_packet(args.packet)
    chosen = owner_sample(packet, args.n, args.seed)
    write_new(
        {
            args.out: "".join(
                json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in chosen
            )
        }
    )
    print(f"sampled {len(chosen)} of {len(packet)} rows into {args.out}")


def approve(args: argparse.Namespace) -> None:
    crosswalk = _json(args.crosswalk, Crosswalk)
    try:
        policies = _POLICIES.validate_json(_read(args.policies))
    except ValidationError as exc:
        raise DatasetError(f"{args.policies}: {exc}") from exc
    mapping = load_mapping(args.mapping, load_taxonomy())
    package = approval_records(
        crosswalk,
        policies,
        mapping,
        _read(args.verdicts),
        reviewer_id=args.reviewer_id,
        authority_id=args.authority_id,
        evidence_reference=args.evidence_reference,
    )
    write_new(
        {
            args.out_dir / "reviewed_crosswalk.json": _dump(
                package.crosswalk.model_dump(mode="json")
            ),
            args.out_dir / "reviewed_policies.json": _dump(
                [p.model_dump(mode="json") for p in package.policies]
            ),
            args.out_dir / "approvals.json": _dump(
                [a.model_dump(mode="json") for a in package.approvals]
            ),
        }
    )
    print(f"{len(package.approvals)} approval records written to {args.out_dir}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)
    d = commands.add_parser("draft", help="draft crosswalk, policies and review packet")
    d.add_argument("--archives", type=Path, nargs="+", required=True)
    d.add_argument("--mapping", type=Path, default=MAPPING_PATH)
    d.add_argument("--out-dir", type=Path, required=True)
    d.set_defaults(run=draft)
    s = commands.add_parser("sample", help="deterministic owner sample of a review packet")
    s.add_argument("--packet", type=Path, required=True)
    s.add_argument("--n", type=int, required=True)
    s.add_argument("--seed", type=int, required=True)
    s.add_argument("--out", type=Path, required=True)
    s.set_defaults(run=sample)
    a = commands.add_parser("approve", help="approval records from a complete verdict file")
    a.add_argument("--crosswalk", type=Path, required=True)
    a.add_argument("--policies", type=Path, required=True)
    a.add_argument("--mapping", type=Path, default=MAPPING_PATH)
    a.add_argument("--verdicts", type=Path, required=True)
    a.add_argument("--reviewer-id", required=True)
    a.add_argument("--authority-id", required=True)
    a.add_argument("--evidence-reference", required=True)
    a.add_argument("--out-dir", type=Path, required=True)
    a.set_defaults(run=approve)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        args.run(args)
    except (DatasetError, CrosswalkError, ValidationError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
