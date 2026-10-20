"""Read-only attribution and projection report for fit train-input conflicts."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import yaml
from build_dataset import _moves, _roster_moves, read_jsonl
from dataset_builder import BUILDER_VERSION, SYSTEM_PROMPT, render_input
from dataset_contracts import Contract, DatasetError, NormalizationExample, SourceRecord
from exclusive_write import write_new
from harness.holdout_records import read_looks
from harness.paths import HOLDOUT_MOVES, ROOT, SCORING_LOG
from progress import Progress
from pydantic import ValidationError

from fra_core.pools import Pool
from fra_core.split import Part, development_documents

STANDARD_NAMESPACES = ("us-gaap:", "ifrs-full:", "dei:", "srt:")
UNSUPPORTED_STATEMENTS = frozenset({"cash_flow", "equity", "comprehensive_income"})
PINNED_DATASET_EXPECTED_COUNTS = {
    "row_count": 674_930,
    "conflicting_keys": 2_709,
    "conflicting_rows": 119_599,
    "mixed_groups": 2_663,
    "null_rows_in_mixed_groups": 32_680,
    "fit_source_count": 28_160,
    "multi_non_null_conflict_keys": 214,
    "nonnull_only_multi_target_conflict_keys": 46,
}
PINNED_TRAIN_SHA256 = "635e11ef04c3af63c40f9c28702e6722c9870cd765c107eb0c26cf63ade43346"


class AuditExample(Contract):
    source_id: str
    pool: str
    statement: str
    label: str
    canonical_id: str | None
    abstention_reason: str | None
    role: str | None
    tag: str | None


def _answer(row: dict[str, Any]) -> tuple[str, str, str]:
    try:
        messages = row["messages"]
        if (
            len(messages) != 3
            or messages[0]["role"] != "system"
            or messages[1]["role"] != "user"
            or messages[2]["role"] != "assistant"
            or messages[0]["content"] != SYSTEM_PROMPT
        ):
            raise ValueError("unexpected chat row shape or system prompt")
        key, answer = messages[1]["content"], messages[2]["content"]
        decoded = json.loads(answer)
        if (
            not isinstance(key, str)
            or not isinstance(decoded, dict)
            or set(decoded) != {"canonical_id"}
        ):
            raise ValueError("invalid model input or target")
        canonical = decoded["canonical_id"]
        if canonical is not None and not isinstance(canonical, str):
            raise ValueError("canonical_id must be string or null")
        prompt = json.loads(key)
        if not isinstance(prompt, dict) or set(prompt) != {"label", "statement"}:
            raise ValueError("model input must contain only label and statement")
        return key, answer, str(prompt["statement"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DatasetError(f"invalid train row: {exc}") from exc


def _bucket(example: dict[str, Any] | None, canonical: str | None, statement: str) -> str:
    if canonical is not None:
        return "mapped"
    if statement in UNSUPPORTED_STATEMENTS:
        return "unsupported_statement"
    if example is None:
        return "unresolved_provenance"
    role, tag, reason = example.get("role"), example.get("tag"), example.get("abstention_reason")
    if role is None or tag is None or reason is None:
        return "unresolved_provenance"
    if role == "negative_control":
        return "negative_control_role"
    if not tag.startswith(STANDARD_NAMESPACES):
        return "custom_tag"
    return "standard_tag_omission"


def _summary(
    rows: list[dict[str, Any]], annotations: list[str], *, excluded: set[int] | None = None
) -> dict[str, Any]:
    excluded = excluded or set()
    keep = [i for i in range(len(rows)) if i not in excluded]
    keys: defaultdict[str, set[str]] = defaultdict(set)
    key_counts: Counter[str] = Counter()
    for i in keep:
        keys[rows[i]["key"]].add(rows[i]["answer"])
        key_counts[rows[i]["key"]] += 1
    conflicts = {key for key, targets in keys.items() if len(targets) > 1}
    statement: defaultdict[str, Counter[str]] = defaultdict(Counter)
    removed_by_statement = Counter(rows[i]["statement"] for i in excluded)
    classes: defaultdict[str, Counter[str]] = defaultdict(Counter)
    null_n = nonnull_n = 0
    unsupported = 0
    for i in keep:
        row = rows[i]
        is_null = json.loads(row["answer"])["canonical_id"] is None
        null_n += is_null
        nonnull_n += not is_null
        statement[row["statement"]]["rows"] += 1
        statement[row["statement"]]["null" if is_null else "non_null"] += 1
        if annotations[i] == "unsupported_statement":
            unsupported += 1
        if not is_null:
            canonical = json.loads(row["answer"])["canonical_id"]
            classes[canonical]["rows"] += 1
            classes[canonical]["after"] += 1
    before_classes = Counter(
        json.loads(rows[i]["answer"])["canonical_id"]
        for i in range(len(rows))
        if json.loads(rows[i]["answer"])["canonical_id"] is not None
    )
    class_retention = {}
    for name, before in sorted(before_classes.items()):
        after = classes[name]["after"]
        class_retention[name] = {
            "before": before,
            "after": after,
            "removed_percent": round((before - after) * 100 / before, 6),
        }
    return {
        "retained_rows": len(keep),
        "removed_rows": len(excluded),
        "retained_null_rows": null_n,
        "retained_non_null_rows": nonnull_n,
        "removed_null_rows": sum(
            json.loads(rows[i]["answer"])["canonical_id"] is None for i in excluded
        ),
        "removed_non_null_rows": sum(
            json.loads(rows[i]["answer"])["canonical_id"] is not None for i in excluded
        ),
        "remaining_conflicting_keys": len(conflicts),
        "remaining_conflicting_rows": sum(key_counts[k] for k in conflicts),
        "per_statement": {
            k: {
                "rows": statement[k]["rows"],
                "null": statement[k]["null"],
                "non_null": statement[k]["non_null"],
                "removed": removed_by_statement[k],
            }
            for k in sorted(set(statement) | set(removed_by_statement))
        },
        "per_canonical_class": class_retention,
        "classes_losing_all_supervision": sorted(
            k for k, v in class_retention.items() if v["after"] == 0
        ),
        "classes_losing_more_than_50_percent": sorted(
            k for k, v in class_retention.items() if v["removed_percent"] > 50
        ),
        "unsupported_statement_negatives": {
            "retained_rows": unsupported,
            "removed_rows": sum(annotations[i] == "unsupported_statement" for i in excluded),
        },
        "unattributed_rows": sum(a == "unresolved_provenance" for a in annotations),
    }


def audit_rows(
    train_rows: list[dict[str, Any]], examples: list[dict[str, Any]], *, fit_source_ids: set[str]
) -> dict[str, Any]:
    """Audit synthetic or caller-provided fit rows; never changes input rows."""
    parsed: list[dict[str, str]] = []
    example_by_pair: defaultdict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for raw_item in examples:
        try:
            item = AuditExample.model_validate(raw_item).model_dump()
        except ValidationError as exc:
            raise DatasetError(
                f"upstream fit example is missing or has invalid audit fields: {exc}"
            ) from exc
        if item.get("pool") != Pool.TRAIN.value:
            raise DatasetError(f"non-train source in upstream examples: {item.get('source_id')}")
        if item.get("source_id") not in fit_source_ids:
            raise DatasetError(f"non-fit source in upstream examples: {item.get('source_id')}")
        key = render_input(str(item["label"]), str(item["statement"]))
        answer = json.dumps(
            {"canonical_id": item["canonical_id"]}, ensure_ascii=False, separators=(",", ":")
        )
        example_by_pair[(key, answer)].append(item)
    unmatched: Counter[tuple[str, str]] = Counter()
    ambiguous_train_rows = 0
    train_groups: defaultdict[tuple[str, str], list[tuple[int, str, str, str]]] = defaultdict(list)
    for index, row in enumerate(train_rows):
        key, answer, statement = _answer(row)
        parsed.append({"key": key, "answer": answer, "statement": statement})
        train_groups[(key, answer)].append((index, key, answer, statement))
    row_examples: list[dict[str, Any] | None] = [None] * len(train_rows)
    annotations = [""] * len(train_rows)
    for pair, group in train_groups.items():
        candidates = sorted(
            example_by_pair[pair],
            key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
        )
        target = json.loads(pair[1])["canonical_id"]
        candidate_buckets = {_bucket(item, target, group[0][3]) for item in candidates}
        if not candidates:
            unmatched[pair] += len(group)
            for index, _, _, statement in group:
                annotations[index] = _bucket(None, target, statement)
        elif len(group) != len(candidates) and len(candidate_buckets) > 1:
            ambiguous_train_rows += len(group)
            for index, _, _, _ in group:
                annotations[index] = "unresolved_provenance"
        else:
            matched = candidates[: len(group)]
            for item, (index, _, _, statement) in zip(matched, group, strict=True):
                row_examples[index] = item
                annotations[index] = _bucket(item, target, statement)
            for index, _, _, statement in group[len(matched) :]:
                unmatched[pair] += 1
                annotations[index] = _bucket(None, target, statement)
        example_by_pair[pair] = candidates[len(group) :]
    unmatched_upstream = [
        {
            "key_sha256": hashlib.sha256(key.encode()).hexdigest(),
            "answer": answer,
            "upstream_rows_without_train_match": len(candidates),
        }
        for (key, answer), candidates in example_by_pair.items()
        if candidates
    ]
    key_targets: defaultdict[str, set[str]] = defaultdict(set)
    for row in parsed:
        key_targets[row["key"]].add(row["answer"])
    conflict_keys = {key for key, values in key_targets.items() if len(values) > 1}
    by_key: defaultdict[str, list[int]] = defaultdict(list)
    for i, row in enumerate(parsed):
        by_key[row["key"]].append(i)
    conflicts = []
    r1_removed: set[int] = set()
    for key in conflict_keys:
        indices = by_key[key]
        buckets = Counter(annotations[i] for i in indices)
        targets = Counter(parsed[i]["answer"] for i in indices)
        mapped_by_class = Counter(
            json.loads(parsed[i]["answer"])["canonical_id"]
            for i in indices
            if annotations[i] == "mapped"
        )
        for i in indices:
            if (
                json.loads(parsed[i]["answer"])["canonical_id"] is None
                and annotations[i] in {"negative_control_role", "custom_tag"}
                and annotations[i] != "unsupported_statement"
            ):
                r1_removed.add(i)
        conflicts.append(
            {
                "key_sha256": hashlib.sha256(key.encode()).hexdigest(),
                "target_counts": dict(sorted(targets.items())),
                "buckets": dict(sorted(buckets.items())),
                "mapped_by_canonical_id": dict(sorted(mapped_by_class.items())),
                "multi_non_null_status": "suspected_supervision_error_pending_source_review"
                if sum(1 for target in targets if json.loads(target)["canonical_id"] is not None)
                > 1
                else None,
            }
        )
    multi_non_null_conflict_keys = sum(
        len(
            {
                json.loads(parsed[i]["answer"])["canonical_id"]
                for i in by_key[key]
                if json.loads(parsed[i]["answer"])["canonical_id"] is not None
            }
        )
        > 1
        for key in conflict_keys
    )
    nonnull_only_multi_target_conflict_keys = sum(
        len(
            {
                json.loads(parsed[i]["answer"])["canonical_id"]
                for i in by_key[key]
                if json.loads(parsed[i]["answer"])["canonical_id"] is not None
            }
        )
        > 1
        and all(json.loads(parsed[i]["answer"])["canonical_id"] is not None for i in by_key[key])
        for key in conflict_keys
    )
    r1_conflicts = {
        key
        for key in conflict_keys
        if len({parsed[i]["answer"] for i in by_key[key] if i not in r1_removed}) > 1
    }
    r2_removed = r1_removed | {i for key in r1_conflicts for i in by_key[key]}
    conflict_row_indices = {i for key in conflict_keys for i in by_key[key]}
    return {
        "row_count": len(parsed),
        "input_key_definition": "train.jsonl messages[1].content exact string",
        "conflicting_keys": len(conflict_keys),
        "conflicting_rows": sum(len(by_key[k]) for k in conflict_keys),
        "mixed_groups": sum(
            any(json.loads(parsed[i]["answer"])["canonical_id"] is None for i in by_key[k])
            and any(json.loads(parsed[i]["answer"])["canonical_id"] is not None for i in by_key[k])
            for k in conflict_keys
        ),
        "null_rows_in_mixed_groups": sum(
            json.loads(parsed[i]["answer"])["canonical_id"] is None
            for k in conflict_keys
            for i in by_key[k]
            if any(json.loads(parsed[j]["answer"])["canonical_id"] is not None for j in by_key[k])
        ),
        "multi_non_null_conflict_keys": multi_non_null_conflict_keys,
        "nonnull_only_multi_target_conflict_keys": nonnull_only_multi_target_conflict_keys,
        "conflicts": sorted(conflicts, key=lambda x: x["key_sha256"]),
        "unattributed_train_rows": sum(unmatched.values()),
        "unmatched_train_rows_count": sum(unmatched.values()),
        "ambiguous_provenance_train_rows": ambiguous_train_rows,
        "unresolved_provenance_train_rows": sum(unmatched.values()) + ambiguous_train_rows,
        "provenance_accounting": (
            "unmatched rows have no upstream key/answer match; ambiguous rows have partial "
            "multiplicity matches across different attribution buckets and remain unresolved"
        ),
        "unmatched_train_rows": [
            {
                "key_sha256": hashlib.sha256(key.encode()).hexdigest(),
                "answer": answer,
                "train_rows_without_upstream_fit_match": count,
            }
            for (key, answer), count in sorted(
                unmatched.items(),
                key=lambda item: (hashlib.sha256(item[0][0].encode()).hexdigest(), item[0][1]),
            )
        ],
        "upstream_row_counts_reconcile": (
            not unmatched and not unmatched_upstream and ambiguous_train_rows == 0
        ),
        "unmatched_upstream_examples": sorted(
            unmatched_upstream, key=lambda x: (x["key_sha256"], x["answer"])
        ),
        "top_standard_omission_reasons": dict(
            sorted(
                Counter(
                    str(e.get("abstention_reason"))
                    for i, e in enumerate(row_examples)
                    if i in conflict_row_indices
                    and e is not None
                    and e.get("abstention_reason")
                    and _bucket(e, None, str(e.get("statement"))) == "standard_tag_omission"
                ).items()
            )
        ),
        "projections": {
            "baseline": _summary(parsed, annotations),
            "R1": _summary(parsed, annotations, excluded=r1_removed),
            "R2": _summary(parsed, annotations, excluded=r2_removed),
        },
    }


def write_report(path: Path, report: dict[str, Any]) -> None:
    write_new(
        {path: json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"}
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train", type=Path, required=True)
    parser.add_argument("--examples", type=Path, required=True)
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--moves", type=Path, default=HOLDOUT_MOVES)
    parser.add_argument("--looks", type=Path, default=SCORING_LOG)
    parser.add_argument("--out", type=Path, default=ROOT / "var/dataset-conflict-audit.json")
    return parser


def _read_fit_examples(path: Path, fit_source_ids: set[str]) -> list[NormalizationExample]:
    """Read only authorized fit examples; validate labels after source authorization."""
    selected: list[NormalizationExample] = []
    try:
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    raise DatasetError(
                        f"invalid upstream JSON at {path}: line {line_number}"
                    ) from None
                if not isinstance(item, dict):
                    raise DatasetError(f"invalid upstream record at {path}: line {line_number}")
                source_record = item.get("source_record")
                if not isinstance(source_record, dict):
                    raise DatasetError(
                        f"cannot authorize upstream source at {path}: line {line_number}"
                    )
                source_id = source_record.get("source_id")
                if not isinstance(source_id, str):
                    raise DatasetError(
                        f"cannot authorize upstream source at {path}: line {line_number}"
                    )
                if source_id not in fit_source_ids:
                    continue
                embedded_pool = source_record.get("pool")
                if embedded_pool not in (None, Pool.TRAIN.value):
                    raise DatasetError(f"non-train upstream source at {path}: line {line_number}")
                try:
                    selected.append(NormalizationExample.model_validate_json(line))
                except ValidationError:
                    raise DatasetError(
                        f"invalid authorized fit example at {path}: line {line_number}"
                    ) from None
    except UnicodeError:
        raise DatasetError(f"invalid upstream text at {path}") from None
    return selected


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        sources = read_jsonl(args.sources, SourceRecord)
        looks = read_looks(args.looks)
        moves = _roster_moves(_moves(args.moves, looks), sources)
        roster = [
            {
                "id": s.source_id,
                "issuer": s.issuer.name or "",
                "cik": s.issuer.cik,
                "pool": s.pool.value,
            }
            for s in sources
        ]
        fit = development_documents(roster, moves, (Part.FIT,))
        fit_ids = {str(source["id"]) for source in fit}
        if any(s.pool != Pool.TRAIN for s in sources if s.source_id in fit_ids):
            raise DatasetError("fit roster contains a non-train-pool source")
        fit_examples = _read_fit_examples(args.examples, fit_ids)
        with Progress("conflict audit read fit train rows", unit="rows"):
            rows = [
                json.loads(line)
                for line in args.train.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        report = audit_rows(
            rows,
            [
                {
                    "source_id": e.source_record.source_id,
                    "pool": e.source_record.pool.value,
                    "statement": e.statement.value,
                    "label": e.label,
                    "canonical_id": e.canonical_id,
                    "abstention_reason": e.abstention_reason,
                    "role": e.role,
                    "tag": e.tag,
                }
                for e in fit_examples
            ],
            fit_source_ids=fit_ids,
        )
        report["fit_source_count"] = len(fit_ids)
        report["input_hashes"] = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (args.train, args.examples, args.sources, args.moves, args.looks)
        }
        train_hash = report["input_hashes"][args.train.name]
        report["train_sha256_matches_expected"] = train_hash == PINNED_TRAIN_SHA256
        mismatches = {
            key: {"expected": value, "actual": report.get(key)}
            for key, value in PINNED_DATASET_EXPECTED_COUNTS.items()
            if report.get(key) != value
        }
        report.update(
            {
                "builder_version": BUILDER_VERSION,
                "system_prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
                "expected_count_checks": {
                    "expected": PINNED_DATASET_EXPECTED_COUNTS,
                    "mismatches": mismatches,
                },
            }
        )
        if not report["train_sha256_matches_expected"]:
            mismatches["train_sha256"] = {
                "expected": PINNED_TRAIN_SHA256,
                "actual": train_hash,
            }
            report["expected_count_checks"]["mismatches"] = mismatches
        write_report(args.out, report)
        if mismatches:
            raise DatasetError(f"reported counts differ from expected definitions: {mismatches}")
    except (DatasetError, OSError, ValueError, ValidationError, yaml.YAMLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
