"""Freeze train-only equity contradictions; apply a hash-bound list without reselection."""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections import Counter, defaultdict
from collections.abc import Iterator, Sequence
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
from exclusive_write import write_new
from pydantic import Field, ValidationError, model_validator

if TYPE_CHECKING:
    from dataset_builder import DatasetBuildManifest, Row

CRITERION = (
    "Exact messages[1].content strings with both equity_attributable_parent and "
    "total_equity targets in train only; remove every matching train row, including null."
)


class ExclusionAudit(Contract):
    keys_sha256: Sha256
    source_train_sha256: Sha256
    rows_before: int = Field(ge=1)
    rows_after: int = Field(ge=1)
    removed_rows: int = Field(ge=1)
    removed_targets: dict[str, int]


class FrozenExclusions(ExclusionAudit):
    version: Literal[1] = 1
    keys: tuple[str, ...] = Field(min_length=1)
    selection_criterion: str = CRITERION
    builder_version: int
    system_prompt_sha256: Sha256
    dataset: DatasetManifest
    source_valid_sha256: Sha256
    tokenizer_id: str
    provenance: Text

    @model_validator(mode="after")
    def _consistent(self) -> FrozenExclusions:
        if self.selection_criterion != CRITERION:
            raise ValueError("unsupported exclusion selection criterion")
        if self.keys != tuple(sorted(set(self.keys))) or digest(self.keys) != self.keys_sha256:
            raise ValueError("exclusion keys must be sorted, unique and match keys_sha256")
        if self.rows_before - self.removed_rows != self.rows_after:
            raise ValueError("exclusion row counts do not reconcile")
        if any(n < 1 for n in self.removed_targets.values()):
            raise ValueError("removed target counts must be positive")
        if sum(self.removed_targets.values()) != self.removed_rows:
            raise ValueError("removed target breakdown does not match removed_rows")
        return self

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
    checked(artifact)
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
    checked(artifact)
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


def freeze_exclusions(dataset_dir: Path, *, provenance: str) -> FrozenExclusions:
    """Select from train targets only. Validation is opened solely as opaque hash bytes."""
    from dataset_builder import BUILDER_VERSION, SYSTEM_PROMPT, DatasetBuildManifest

    try:
        manifest = DatasetBuildManifest.model_validate_json(
            (dataset_dir / "manifest.json").read_bytes()
        )
        if manifest.builder_version != BUILDER_VERSION or manifest.exclusion is not None:
            raise DatasetError("freeze requires the current unexcluded builder version")
        if manifest.system_prompt_sha256 != hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest():
            raise DatasetError("freeze source system prompt sha256 differs")
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--provenance", required=True)
    args = parser.parse_args(argv)
    try:
        if args.out.exists():
            raise DatasetError(f"{args.out}: refusing to overwrite frozen exclusions")
        artifact = freeze_exclusions(args.dataset_dir, provenance=args.provenance)
        write_new({args.out: artifact.model_dump_json(indent=2) + "\n"})
    except (DatasetError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"{len(artifact.keys)} keys, {artifact.removed_rows} removed, "
        f"{artifact.rows_after} retained; keys sha256 {artifact.keys_sha256}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
