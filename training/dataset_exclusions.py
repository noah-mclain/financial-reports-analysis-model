"""Freeze train-only exclusions; apply hash-bound keys or input/target pairs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from dataset_contracts import (
    Contract,
    DatasetError,
    DatasetManifest,
    NormalizationAnswer,
    Sha256,
    Text,
    digest,
)
from pydantic import Field, ValidationError, model_validator

from fra_core.schemas import StatementType
from fra_core.taxonomy import load_taxonomy

if TYPE_CHECKING:
    from dataset_builder import DatasetBuildManifest, Row

CRITERION = (
    "Exact messages[1].content strings with both equity_attributable_parent and "
    "total_equity targets in train only; remove every matching train row, including null."
)


CONSISTENCY_CRITERION = "training_input_consistency"
RemovalReason = Literal["equity", "statement", "ambiguous", "outlier"]


class ConsistencyPolicy(Contract):
    statements: tuple[str, ...] = Field(min_length=1)
    dominance: Decimal = Field(gt=0, le=1, allow_inf_nan=False)
    null_dominance: Decimal = Field(gt=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def _statements(self) -> ConsistencyPolicy:
        if self.statements != tuple(sorted(set(self.statements))) or not set(self.statements) <= {
            s.value for s in StatementType
        }:
            raise ValueError("statements must be sorted, unique, known statement types")
        return self


class Removal(Contract):
    input_key: Text
    target: Text | None
    reason: RemovalReason
    rows: int = Field(ge=1)


def _pair_order(pair: tuple[str, str | None]) -> tuple[str, str]:
    return pair[0], target_name(pair[1])


class ExclusionAudit(Contract):
    keys_sha256: Sha256
    source_train_sha256: Sha256
    rows_before: int = Field(ge=1)
    rows_after: int = Field(ge=1)
    removed_rows: int = Field(ge=1)
    removed_targets: dict[str, int]


class FrozenExclusions(ExclusionAudit):
    version: Literal[1, 2] = 1
    keys: tuple[str, ...] = Field(min_length=1)
    selection_criterion: str = CRITERION
    builder_version: int
    system_prompt_sha256: Sha256
    dataset: DatasetManifest
    source_valid_sha256: Sha256
    tokenizer_id: str
    provenance: Text

    policy: ConsistencyPolicy | None = Field(default=None, exclude_if=lambda v: v is None)
    removals: tuple[Removal, ...] | None = Field(default=None, exclude_if=lambda v: v is None)
    equity_exclusions: FrozenExclusions | None = Field(default=None, exclude_if=lambda v: v is None)
    equity_artifact_sha256: Sha256 | None = Field(default=None, exclude_if=lambda v: v is None)
    removed_reasons: dict[RemovalReason, int] | None = Field(
        default=None, exclude_if=lambda v: v is None
    )

    @model_validator(mode="after")
    def _consistent(self) -> FrozenExclusions:
        if self.version == 1:
            if self.selection_criterion != CRITERION:
                raise ValueError("unsupported exclusion selection criterion")
            if self.model_fields_set & {
                "policy",
                "removals",
                "equity_exclusions",
                "equity_artifact_sha256",
                "removed_reasons",
            }:
                raise ValueError("version 1 cannot carry consistency policy fields")
            if digest(self.keys) != self.keys_sha256:
                raise ValueError("exclusion keys must match keys_sha256")
        else:
            self._consistent_v2()
        if self.keys != tuple(sorted(set(self.keys))):
            raise ValueError("exclusion keys must be sorted and unique")
        if self.rows_before - self.removed_rows != self.rows_after:
            raise ValueError("exclusion row counts do not reconcile")
        if any(n < 1 for n in self.removed_targets.values()):
            raise ValueError("removed target counts must be positive")
        if sum(self.removed_targets.values()) != self.removed_rows:
            raise ValueError("removed target breakdown does not match removed_rows")
        return self

    def _consistent_v2(self) -> None:
        if self.selection_criterion != CONSISTENCY_CRITERION:
            raise ValueError("unsupported exclusion selection criterion")
        if (
            self.policy is None
            or not self.removals
            or self.equity_exclusions is None
            or self.equity_artifact_sha256 is None
            or self.removed_reasons is None
        ):
            raise ValueError("version 2 requires policy, removals and equity binding")
        equity = self.equity_exclusions
        if equity.version != 1:
            raise ValueError("carried equity exclusions must be version 1")
        for name in (
            "builder_version",
            "system_prompt_sha256",
            "dataset",
            "source_train_sha256",
            "source_valid_sha256",
            "tokenizer_id",
            "rows_before",
        ):
            if getattr(self, name) != getattr(equity, name):
                raise ValueError(f"equity exclusion source {name} differs")
        pairs = tuple((r.input_key, r.target) for r in self.removals)
        if pairs != tuple(sorted(set(pairs), key=_pair_order)):
            raise ValueError("removal pairs must be sorted and unique")
        if self.keys != tuple(sorted({r.input_key for r in self.removals})):
            raise ValueError("exclusion key coverage differs from removals")
        reasons: Counter[RemovalReason] = Counter()
        targets: Counter[str] = Counter()
        equity_targets: Counter[str] = Counter()
        equity_keys: set[str] = set()
        for removal in self.removals:
            reasons[removal.reason] += removal.rows
            targets[target_name(removal.target)] += removal.rows
            if (removal.input_key in equity.keys) != (removal.reason == "equity"):
                raise ValueError("equity and policy removal reasons overlap or differ")
            if removal.reason == "equity":
                equity_targets[target_name(removal.target)] += removal.rows
                equity_keys.add(removal.input_key)
        if equity_keys != set(equity.keys) or dict(equity_targets) != equity.removed_targets:
            raise ValueError("carried equity key coverage or target counts differ")
        if dict(reasons) != self.removed_reasons or dict(targets) != self.removed_targets:
            raise ValueError("removal reason or target breakdown differs")
        if digest(self.model_dump(mode="json", exclude={"keys_sha256"})) != self.keys_sha256:
            raise ValueError("version 2 policy/removal binding differs from keys_sha256")

    def audit(self) -> ExclusionAudit:
        return ExclusionAudit.model_validate(
            {name: getattr(self, name) for name in ExclusionAudit.model_fields}
        )


def checked(artifact: FrozenExclusions) -> FrozenExclusions:
    try:
        return FrozenExclusions.model_validate(artifact)
    except ValidationError as exc:
        raise DatasetError(f"invalid exclusion artifact: {exc}") from exc


def load_exclusions(path: Path) -> FrozenExclusions:
    try:
        return FrozenExclusions.model_validate_json(path.read_bytes())
    except (OSError, ValidationError) as exc:
        raise DatasetError(f"{path}: invalid exclusion artifact: {exc}") from exc


def verify_source(
    artifact: FrozenExclusions, manifest: DatasetBuildManifest, *, allow_applied: bool = False
) -> None:
    """Bind the frozen list to the pre-exclusion dataset, not just a matching row count."""
    artifact = checked(artifact)
    if manifest.exclusion is not None:
        if not allow_applied or manifest.exclusion != artifact.audit():
            raise DatasetError("exclusions already applied or differ from frozen artifact")
        if manifest.fit_rows_after != artifact.rows_after:
            raise DatasetError("post-exclusion row count differs from frozen artifact")
        manifest = manifest.model_copy(
            update={
                "train_sha256": manifest.exclusion.source_train_sha256,
                "fit_rows_after": manifest.exclusion.rows_before,
                "exclusion": None,
            }
        )
    for name, actual, expected in (
        ("builder_version", manifest.builder_version, artifact.builder_version),
        ("system_prompt_sha256", manifest.system_prompt_sha256, artifact.system_prompt_sha256),
        ("dataset", manifest.dataset, artifact.dataset),
        ("train sha256", manifest.train_sha256, artifact.source_train_sha256),
        ("valid sha256", manifest.valid_sha256, artifact.source_valid_sha256),
        ("tokenizer_id", manifest.tokenizer_id, artifact.tokenizer_id),
        ("rows before", manifest.fit_rows_after, artifact.rows_before),
    ):
        if actual != expected:
            raise DatasetError(f"exclusion source {name} differs from frozen artifact")


def retained_indices(rows: Sequence[Row], artifact: FrozenExclusions) -> tuple[int, ...]:
    """Apply stored keys; assert the removal counts without selecting new keys."""
    artifact = checked(artifact)
    if artifact.version == 2:
        return _retained_pairs(rows, artifact)
    keys = frozenset(artifact.keys)
    removed: Counter[str] = Counter()
    kept: list[int] = []
    seen: set[str] = set()
    for i, row in enumerate(rows):
        key = row["messages"][1]["content"]
        if key in keys:
            seen.add(key)
            removed[
                target_name(
                    NormalizationAnswer.model_validate_json(
                        row["messages"][2]["content"]
                    ).canonical_id
                )
            ] += 1
        else:
            kept.append(i)
    if (
        len(rows) != artifact.rows_before
        or len(kept) != artifact.rows_after
        or sum(removed.values()) != artifact.removed_rows
        or dict(removed) != artifact.removed_targets
        or seen != keys
    ):
        raise DatasetError("exclusion removal count, target breakdown or key coverage differs")
    return tuple(kept)


def _retained_pairs(rows: Sequence[Row], artifact: FrozenExclusions) -> tuple[int, ...]:
    assert artifact.removals is not None
    pairs = {(r.input_key, r.target): r for r in artifact.removals}
    seen: Counter[tuple[str, str | None]] = Counter()
    removed: Counter[str] = Counter()
    kept: list[int] = []
    for i, row in enumerate(rows):
        try:
            key, target = _validated_pair(
                _ChatRow.model_validate({**row, "messages": tuple(row["messages"])}),
                artifact.system_prompt_sha256,
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise DatasetError(f"train row {i + 1}: {exc}") from exc
        pair = key, target
        if pair in pairs:
            seen[pair] += 1
            removed[target_name(target)] += 1
        else:
            kept.append(i)
    if (
        len(rows) != artifact.rows_before
        or len(kept) != artifact.rows_after
        or dict(removed) != artifact.removed_targets
        or dict(seen) != {pair: r.rows for pair, r in pairs.items()}
    ):
        raise DatasetError("exclusion removal count, target breakdown or pair coverage differs")
    return tuple(kept)


def target_name(target: str | None) -> str:
    return "null" if target is None else target


class _Message(Contract):
    role: str
    content: str


class _ChatRow(Contract):
    messages: tuple[_Message, ...]


def _train_targets(path: Path, prompt_sha256: str) -> Iterator[tuple[str, str]]:
    with path.open("rb") as handle:
        for number, line in enumerate(handle, 1):
            try:
                row = _ChatRow.model_validate_json(line)
                if tuple(m.role for m in row.messages) != ("system", "user", "assistant"):
                    raise ValueError("expected system, user, assistant messages")
                if hashlib.sha256(row.messages[0].content.encode()).hexdigest() != prompt_sha256:
                    raise ValueError("row system prompt differs from manifest")
                target = NormalizationAnswer.model_validate_json(row.messages[2].content)
            except ValueError as exc:
                raise DatasetError(f"{path}:{number}: {exc}") from exc
            yield row.messages[1].content, target_name(target.canonical_id)


class _Input(Contract):
    label: Text
    statement: StatementType


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _validated_pair(row: _ChatRow, prompt_sha256: str) -> tuple[str, str | None]:
    if tuple(m.role for m in row.messages) != ("system", "user", "assistant"):
        raise ValueError("expected system, user, assistant messages")
    if hashlib.sha256(row.messages[0].content.encode()).hexdigest() != prompt_sha256:
        raise ValueError("row system prompt differs from manifest")
    key = row.messages[1].content
    # Validate the payload without ever re-rendering the identity string.
    payload = _Input.model_validate_json(key)
    json.loads(key, object_pairs_hook=_unique_object)
    answer = NormalizationAnswer.model_validate_json(row.messages[2].content)
    json.loads(row.messages[2].content, object_pairs_hook=_unique_object)
    if answer.canonical_id is not None:
        item = load_taxonomy().by_id(answer.canonical_id)
        if item is None or item.statement != payload.statement:
            raise ValueError("target is unknown or does not belong to statement")
    return key, answer.canonical_id


def select_consistency_removals(
    targets: Mapping[str, Counter[str | None]],
    policy: ConsistencyPolicy,
) -> tuple[Removal, ...]:
    """Select disjoint pair removals after equity filtering; never relabel a row."""
    policy = ConsistencyPolicy.model_validate(policy)
    removals: list[Removal] = []
    for key in sorted(targets):
        statement = _Input.model_validate_json(key).statement.value
        counts = targets[key]
        reason: RemovalReason
        keep: str | None
        if statement not in policy.statements:
            reason, drop = "statement", set(counts)
        else:
            mapped = {target: n for target, n in counts.items() if target is not None}
            if not mapped:
                continue
            top = max(mapped.values())
            winners = [target for target, n in mapped.items() if n == top]
            if len(winners) != 1 or Fraction(top, sum(mapped.values())) < policy.dominance:
                reason, drop = "ambiguous", set(counts)
            else:
                keep = (
                    None
                    if Fraction(counts[None], sum(counts.values())) >= policy.null_dominance
                    else winners[0]
                )
                reason, drop = "outlier", set(counts) - {keep}
        removals.extend(
            Removal(input_key=key, target=target, reason=reason, rows=counts[target])
            for target in sorted(drop, key=target_name)
        )
    return tuple(removals)


def _freeze_consistency(
    dataset_dir: Path,
    manifest: DatasetBuildManifest,
    policy: ConsistencyPolicy,
    equity_path: Path,
    provenance: str,
) -> FrozenExclusions:
    equity_bytes = equity_path.read_bytes()
    equity = FrozenExclusions.model_validate_json(equity_bytes)
    if equity.version != 1:
        raise DatasetError("carried equity exclusions must be version 1")
    verify_source(equity, manifest)
    targets: dict[str, Counter[str | None]] = defaultdict(Counter)
    source_hash = hashlib.sha256()
    row_error: DatasetError | None = None
    with (dataset_dir / "train.jsonl").open("rb") as handle:
        for number, line in enumerate(handle, 1):
            source_hash.update(line)
            if row_error is not None:
                continue
            try:
                key, target = _validated_pair(
                    _ChatRow.model_validate_json(line), manifest.system_prompt_sha256
                )
            except ValueError as exc:
                row_error = DatasetError(f"{dataset_dir / 'train.jsonl'}:{number}: {exc}")
                continue
            targets[key][target] += 1
    # Verify every byte, including lines after a parse failure, before returning any selection.
    if source_hash.hexdigest() != manifest.train_sha256:
        raise DatasetError("train.jsonl: source sha256 differs from manifest")
    if row_error is not None:
        raise row_error
    before = sum(sum(c.values()) for c in targets.values())
    if before != manifest.fit_rows_after:
        raise DatasetError("source train rows differ from manifest")
    carried = tuple(
        Removal(input_key=key, target=target, reason="equity", rows=n)
        for key in equity.keys
        for target, n in sorted(targets[key].items(), key=lambda p: target_name(p[0]))
    )
    equity_targets: Counter[str] = Counter()
    for removal in carried:
        equity_targets[target_name(removal.target)] += removal.rows
    if (
        {r.input_key for r in carried} != set(equity.keys)
        or dict(equity_targets) != equity.removed_targets
        or sum(equity_targets.values()) != equity.removed_rows
    ):
        raise DatasetError("equity removal count, target breakdown or key coverage differs")
    equity_keys = frozenset(equity.keys)
    remaining = {key: c for key, c in targets.items() if key not in equity_keys}
    removals = tuple(
        sorted(
            (*carried, *select_consistency_removals(remaining, policy)),
            key=lambda r: _pair_order((r.input_key, r.target)),
        )
    )
    removed: Counter[str] = Counter()
    reasons: Counter[RemovalReason] = Counter()
    for removal in removals:
        removed[target_name(removal.target)] += removal.rows
        reasons[removal.reason] += removal.rows
    # Construct only to compute the v2 content digest; checked() validates before returning.
    artifact = FrozenExclusions.model_construct(
        version=2,
        selection_criterion=CONSISTENCY_CRITERION,
        policy=policy,
        removals=removals,
        equity_exclusions=equity,
        equity_artifact_sha256=hashlib.sha256(equity_bytes).hexdigest(),
        removed_reasons=dict(sorted(reasons.items())),
        keys=tuple(sorted({r.input_key for r in removals})),
        builder_version=manifest.builder_version,
        system_prompt_sha256=manifest.system_prompt_sha256,
        dataset=manifest.dataset,
        source_train_sha256=manifest.train_sha256,
        source_valid_sha256=manifest.valid_sha256,
        tokenizer_id=manifest.tokenizer_id,
        rows_before=before,
        rows_after=before - sum(removed.values()),
        removed_rows=sum(removed.values()),
        removed_targets=dict(sorted(removed.items())),
        provenance=provenance,
    )
    return checked(
        artifact.model_copy(
            update={
                "keys_sha256": digest(artifact.model_dump(mode="json", exclude={"keys_sha256"})),
            }
        )
    )


def freeze_exclusions(
    dataset_dir: Path,
    *,
    provenance: str,
    criterion: str = CRITERION,
    statements: tuple[str, ...] | None = None,
    dominance: Decimal | None = None,
    null_dominance: Decimal | None = None,
    equity_exclusions: Path | None = None,
) -> FrozenExclusions:
    """Select train only; v1 also checks validation bytes, v2 uses its manifest hash."""
    from dataset_builder import BUILDER_VERSION, SYSTEM_PROMPT, DatasetBuildManifest

    try:
        policy = None
        if criterion == CONSISTENCY_CRITERION:
            if (
                statements is None
                or dominance is None
                or null_dominance is None
                or equity_exclusions is None
            ):
                raise DatasetError(
                    "consistency requires statements, dominance, null-dominance "
                    "and equity-exclusions"
                )
            policy = ConsistencyPolicy(
                statements=tuple(sorted(statements)),
                dominance=dominance,
                null_dominance=null_dominance,
            )
        elif criterion != CRITERION or any(
            v is not None
            for v in (
                statements,
                dominance,
                null_dominance,
                equity_exclusions,
            )
        ):
            raise DatasetError("unsupported criterion or policy parameters for version 1")
        manifest = DatasetBuildManifest.model_validate_json(
            (dataset_dir / "manifest.json").read_bytes()
        )
        if manifest.builder_version != BUILDER_VERSION or manifest.exclusion is not None:
            raise DatasetError("freeze requires the current unexcluded builder version")
        if manifest.system_prompt_sha256 != hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest():
            raise DatasetError("freeze source system prompt sha256 differs")
        if policy is not None:
            assert equity_exclusions is not None
            return _freeze_consistency(dataset_dir, manifest, policy, equity_exclusions, provenance)
        for name, expected in (
            ("train.jsonl", manifest.train_sha256),
            ("valid.jsonl", manifest.valid_sha256),
        ):
            with (dataset_dir / name).open("rb") as handle:
                if hashlib.file_digest(handle, "sha256").hexdigest() != expected:
                    raise DatasetError(f"{name}: source sha256 differs from manifest")
        targets: dict[str, Counter[str]] = defaultdict(Counter)
        for key, target in _train_targets(
            dataset_dir / "train.jsonl", manifest.system_prompt_sha256
        ):
            targets[key][target] += 1
        before = sum(sum(c.values()) for c in targets.values())
        if before != manifest.fit_rows_after:
            raise DatasetError("source train rows differ from manifest")
        keys = tuple(
            sorted(
                k
                for k, c in targets.items()
                if c["equity_attributable_parent"] and c["total_equity"]
            )
        )
        removed: Counter[str] = Counter()
        for key in keys:
            removed.update(targets[key])
        return FrozenExclusions(
            keys=keys,
            keys_sha256=digest(keys),
            builder_version=manifest.builder_version,
            system_prompt_sha256=manifest.system_prompt_sha256,
            dataset=manifest.dataset,
            source_train_sha256=manifest.train_sha256,
            source_valid_sha256=manifest.valid_sha256,
            tokenizer_id=manifest.tokenizer_id,
            rows_before=before,
            rows_after=before - sum(removed.values()),
            removed_rows=sum(removed.values()),
            removed_targets=dict(sorted(removed.items())),
            provenance=provenance,
        )
    except (OSError, ValidationError) as exc:
        raise DatasetError(f"{dataset_dir}: cannot freeze exclusions: {exc}") from exc


def _threshold(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("threshold must be a decimal number") from exc


def _publish_exclusions(path: Path, data: bytes) -> None:
    """Publish complete bytes with an exclusive hard link in the destination directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise DatasetError(f"{path} already exists; refusing to overwrite") from exc
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--provenance", required=True)
    parser.add_argument("--criterion", choices=("equity", CONSISTENCY_CRITERION), default="equity")
    parser.add_argument(
        "--statements", help="comma-separated statement types; required for consistency"
    )
    parser.add_argument("--dominance", type=_threshold)
    parser.add_argument("--null-dominance", type=_threshold)
    parser.add_argument("--equity-exclusions", type=Path)
    args = parser.parse_args(argv)
    parameters = (args.statements, args.dominance, args.null_dominance, args.equity_exclusions)
    if args.criterion == CONSISTENCY_CRITERION and any(v is None for v in parameters):
        parser.error(
            "consistency requires --statements, --dominance, --null-dominance "
            "and --equity-exclusions"
        )
    if args.criterion == "equity" and any(v is not None for v in parameters):
        parser.error("policy parameters require --criterion training_input_consistency")
    try:
        if args.out.exists():
            raise DatasetError(f"{args.out}: refusing to overwrite frozen exclusions")
        artifact = freeze_exclusions(
            args.dataset_dir,
            provenance=args.provenance,
            criterion=CRITERION if args.criterion == "equity" else args.criterion,
            statements=tuple(args.statements.split(",")) if args.statements is not None else None,
            dominance=args.dominance,
            null_dominance=args.null_dominance,
            equity_exclusions=args.equity_exclusions,
        )
        _publish_exclusions(args.out, (artifact.model_dump_json(indent=2) + "\n").encode("utf-8"))
    except (DatasetError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    hash_label = "keys" if artifact.version == 1 else "artifact content"
    print(
        f"{len(artifact.keys)} keys, {artifact.removed_rows} removed, "
        f"{artifact.rows_after} retained; {hash_label} sha256 {artifact.keys_sha256}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
