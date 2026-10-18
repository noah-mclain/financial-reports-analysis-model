"""Commands for the SEC source preparation: run the routed pass, report crosswalk coverage.

    make sec-prepare ARCHIVES="a.zip b.zip" OUT_DIR=... AUTHORIZATION="..."
    make sec-coverage PREPARED=... CROSSWALK=... ARCHIVES="a.zip" OUT=...

``prepare`` reads each archive's submission and presentation members once, keeps routed
filings only and refuses an existing output directory. ``coverage`` reads only the prepared
payloads (through the access contract) and the tag vocabulary; its report holds counts and
tag definitions, never a label. ``num.txt`` is never opened.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from dataset_contracts import DatasetError
from exclusive_write import write_new
from harness.holdout_records import read_looks, read_moves
from harness.paths import HOLDOUT_MOVES, ROOT, SCORING_LOG
from progress import Progress
from pydantic import TypeAdapter, ValidationError
from sec_crosswalk import Crosswalk, CrosswalkError, RoleAssessment
from sec_crosswalk_draft import MAPPING_PATH, load_mapping, merge_vocabularies, read_tag_vocabulary
from sec_examples import ExampleBuilder, load_trust
from sec_roles import RULES_PATH, approve_roles, draft_assessments, load_rules, roles_header
from sec_source_access import (
    AccessContext,
    SecIndex,
    SourceAccessError,
    SourceManifest,
    VerifiedPayload,
    iter_payloads,
    read_registry,
)
from sec_source_decoder import coverage_report, report_json, top_unlisted, unlisted_jsonl
from sec_source_pass import directory_opener, run_pass
from sec_source_preparation import FilingRow, read_roster

from fra_core.pools import PoolError
from fra_core.split import SplitError
from fra_core.taxonomy import load_taxonomy
from pool_store import REGISTRY_RELATIVE_PATH


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise DatasetError(f"{path}: cannot read: {exc.strerror}") from exc


def prepare(args: argparse.Namespace) -> None:
    log = run_pass(
        archives=args.archives,
        registry_path=args.registry,
        moves_path=args.moves,
        looks_path=args.looks,
        out_dir=args.out_dir,
        authorization=args.authorization,
        on_archive=lambda archive_id: print(f"prepared {archive_id}", flush=True),
    )
    routed = log["routing"]["routed"]
    print(f"routed fit {routed['fit']}, validation {routed['validation']}")
    print(f"dropped {log['routing']['dropped']}")
    print(f"rows kept {log['totals']['kept_rows']}, discarded {log['totals']['discarded_rows']}")
    empty = [a for entry in log["archives"].values() for a in entry["empty_payloads"]]
    print(f"empty payloads: {len(empty)}" + (f" ({', '.join(empty)})" if empty else ""))
    print(f"output bytes {log['totals']['output_bytes']}")
    print(f"pass log {args.out_dir / 'pass-log.json'}")


def _payloads(
    prepared: Path, args: argparse.Namespace, applicable: set[str], progress: Progress | None = None
) -> Iterator[VerifiedPayload]:
    registry = read_registry(args.registry)
    looks = read_looks(args.looks)
    moves = [m for m in read_moves(args.moves, looks) if m.key in applicable]
    for base in sorted(p for p in prepared.iterdir() if p.is_dir()):
        index = SecIndex.model_validate_json(_read(base / "index.json"))
        manifest = SourceManifest.model_validate_json(_read(base / "manifest.json"))
        context = AccessContext.model_validate_json(_read(base / "context.json"))
        payloads = iter_payloads(
            [a.artifact_id for a in manifest.artifacts],
            index=index,
            manifest=manifest,
            registry=registry,
            context=context,
            moves=moves,
            looks=looks,
            opener=directory_opener(prepared),
        )
        for payload in payloads:
            yield payload
            if progress is not None:
                progress.advance(payload.descriptor.artifact.row_count)


def _routed_filings(prepared: Path, archives: Sequence[Path]) -> list[FilingRow]:
    """The prepared filings, with their submission records re-read from the archives."""
    routed: set[str] = set()
    for base in sorted(p for p in prepared.iterdir() if p.is_dir()):
        index = SecIndex.model_validate_json(_read(base / "index.json"))
        routed |= {f.source_record.source_id for f in index.filings}
    filings = [f for archive in archives for f in read_roster(archive) if f.adsh in routed]
    missing = routed - {f.adsh for f in filings}
    if missing:
        raise DatasetError(f"{len(missing)} prepared filings are not in the archives given")
    return filings


def _dump(value: object) -> str:
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def roles_draft(args: argparse.Namespace) -> None:
    rules = load_rules(args.rules)
    assessments = draft_assessments(_routed_filings(args.prepared, args.archives), rules)
    header = roles_header(rules, assessments)
    table = args.rules.read_text(encoding="utf-8")
    packet = json.dumps(header, sort_keys=True) + "\n"
    packet += json.dumps({"kind": "rule_table", "text": table}, sort_keys=True) + "\n"
    write_new(
        {
            args.out_dir / "role_assessments.json": _dump(
                [a.model_dump(mode="json") for a in assessments]
            ),
            args.out_dir / "role_packet.jsonl": packet,
        }
    )
    print(f"rule table sha256: {rules.sha256}")
    print(f"assessments: {header['count']} {header['roles']}")
    print(f"assessments sha256: {header['assessments_sha256']}")


def roles_approve(args: argparse.Namespace) -> None:
    rules = load_rules(args.rules)
    try:
        given = TypeAdapter(tuple[RoleAssessment, ...]).validate_json(_read(args.assessments))
    except ValidationError as exc:
        raise DatasetError(f"{args.assessments}: {exc}") from exc
    package = approve_roles(
        given,
        _routed_filings(args.prepared, args.archives),
        rules,
        _read(args.verdicts),
        reviewer_id=args.reviewer_id,
        authority_id=args.authority_id,
        evidence_reference=args.evidence_reference,
    )
    write_new(
        {
            args.out_dir / "reviewed_role_assessments.json": _dump(
                [a.model_dump(mode="json") for a in package.assessments]
            ),
            args.out_dir / "role_approvals.json": _dump(
                [a.model_dump(mode="json") for a in package.approvals]
            ),
        }
    )
    print(f"{len(package.approvals)} role approval records written to {args.out_dir}")


def examples(args: argparse.Namespace) -> None:
    prepared: Path = args.prepared
    if (prepared / "pass-abort.json").exists() or not (prepared / "pass-log.json").exists():
        raise DatasetError(f"{prepared}: no completed pass log")
    log = json.loads(_read(prepared / "pass-log.json"))
    applicable = set(log["moves"]["applicable"])
    looks = read_looks(args.looks)
    moves = [m for m in read_moves(args.moves, looks) if m.key in applicable]
    builder = ExampleBuilder(
        prepared=prepared,
        bundle=load_trust(args.trust_dir),
        mapping=load_mapping(args.mapping, load_taxonomy()),
        taxonomy=load_taxonomy(),
        registry_path=args.registry,
        moves=moves,
        looks=looks,
        batch_issuers=args.batch_issuers,
        allow_conflicts_up_to=args.allow_conflicts_up_to,
        progress_total=_kept_rows(log),
    )
    counts = builder.run(args.out_dir)
    print(f"conflicting_groups {counts['conflicting_groups']}")
    print(f"conflicting_rows {counts['conflicting_rows']}")
    print(json.dumps(counts, sort_keys=True, indent=2))


def coverage(args: argparse.Namespace) -> None:
    prepared: Path = args.prepared
    if (prepared / "pass-abort.json").exists() or not (prepared / "pass-log.json").exists():
        raise DatasetError(f"{prepared}: no completed pass log")
    log = json.loads(_read(prepared / "pass-log.json"))
    applicable = set(log["moves"]["applicable"])
    mapping = load_mapping(args.mapping, load_taxonomy())
    try:
        crosswalk = Crosswalk.model_validate_json(_read(args.crosswalk))
    except ValidationError as exc:
        raise DatasetError(f"{args.crosswalk}: {exc}") from exc
    if crosswalk.review.sha256 != mapping.sha256:
        raise DatasetError(f"{args.crosswalk}: not drafted from the mapping {args.mapping}")
    vocabulary = merge_vocabularies(d for a in args.archives for d in read_tag_vocabulary(a))
    with Progress("sec-coverage", total=_kept_rows(log), unit="rows") as progress:
        report = coverage_report(
            _payloads(prepared, args, applicable, progress), mapping, crosswalk
        )
    text = report_json(report, top_unlisted(report, args.top, vocabulary))
    files = {args.out: text + "\n"}
    if args.unlisted_out is not None:
        files[args.unlisted_out] = unlisted_jsonl(report, vocabulary)
    write_new(files)
    print(text)


def _kept_rows(log: dict[str, Any]) -> int | None:
    """Return a valid kept-row progress total from a pass log, when present."""
    total = log.get("totals", {}).get("kept_rows")
    return total if isinstance(total, int) else None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)

    def shared(command: argparse.ArgumentParser) -> None:
        command.add_argument("--registry", type=Path, default=ROOT / REGISTRY_RELATIVE_PATH)
        command.add_argument("--moves", type=Path, default=HOLDOUT_MOVES)
        command.add_argument("--looks", type=Path, default=SCORING_LOG)

    p = commands.add_parser("prepare", help="run the routed pass over the archives")
    p.add_argument("--archives", type=Path, nargs="+", required=True)
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--authorization", required=True, help="reference to the owner's authorization")
    shared(p)
    p.set_defaults(run=prepare)
    r = commands.add_parser("roles-draft", help="draft role assessments from the rule table")
    r.add_argument("--prepared", type=Path, required=True)
    r.add_argument("--archives", type=Path, nargs="+", required=True, help="for sub.txt only")
    r.add_argument("--rules", type=Path, default=RULES_PATH)
    r.add_argument("--out-dir", type=Path, required=True)
    r.set_defaults(run=roles_draft)
    ra = commands.add_parser("roles-approve", help="approval records under one rule-table verdict")
    ra.add_argument("--prepared", type=Path, required=True)
    ra.add_argument("--archives", type=Path, nargs="+", required=True, help="for sub.txt only")
    ra.add_argument("--rules", type=Path, default=RULES_PATH)
    ra.add_argument("--assessments", type=Path, required=True)
    ra.add_argument("--verdicts", type=Path, required=True)
    ra.add_argument("--reviewer-id", required=True)
    ra.add_argument("--authority-id", required=True)
    ra.add_argument("--evidence-reference", required=True)
    ra.add_argument("--out-dir", type=Path, required=True)
    ra.set_defaults(run=roles_approve)
    e = commands.add_parser("examples", help="approved training examples from prepared payloads")
    e.add_argument("--prepared", type=Path, required=True)
    e.add_argument("--trust-dir", type=Path, default=ROOT / "var/approvals")
    e.add_argument("--mapping", type=Path, default=MAPPING_PATH)
    e.add_argument("--out-dir", type=Path, required=True)
    e.add_argument("--batch-issuers", type=int, default=100)
    e.add_argument(
        "--allow-conflicts-up-to",
        type=int,
        default=None,
        help="conflicting rows to accept; by default more than 5%% of the candidates stop the run",
    )
    shared(e)
    e.set_defaults(run=examples)
    c = commands.add_parser("coverage", help="crosswalk coverage of the prepared payloads")
    c.add_argument("--prepared", type=Path, required=True)
    c.add_argument("--crosswalk", type=Path, required=True)
    c.add_argument("--mapping", type=Path, default=MAPPING_PATH)
    c.add_argument("--archives", type=Path, nargs="+", required=True, help="for tag.txt only")
    c.add_argument("--top", type=int, default=40)
    c.add_argument("--out", type=Path, required=True)
    c.add_argument("--unlisted-out", type=Path, help="JSONL of every unlisted BS and IS tag")
    shared(c)
    c.set_defaults(run=coverage)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        args.run(args)
    except (
        DatasetError,
        CrosswalkError,
        SourceAccessError,
        SplitError,
        PoolError,
        OSError,
        ValidationError,
        json.JSONDecodeError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
