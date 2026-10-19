"""Frozen equity exclusions bind exact train inputs, never validation targets."""

import json
from pathlib import Path

import pytest
from build_dataset import main as build_main
from dataset_builder import build_dataset, write_dataset
from dataset_contracts import DatasetError, PartitionedExamples
from dataset_exclusions import FrozenExclusions, freeze_exclusions, load_exclusions
from dataset_exclusions import main as freeze_main
from normalization_eval import inference_messages, population_report, score
from normalization_validation import main as validation_main
from test_build_dataset import Files
from test_dataset_builder import fit_source, partitioned, row, valid_source
from test_normalization_eval import prediction, sample
from test_normalization_validation import args as validation_args

from fra_core.schemas import StatementType


def equity_parts() -> PartitionedExamples:
    targets = (
        "equity_attributable_parent",
        "total_equity",
        None,
        "total_liabilities_and_equity",
    )
    fit = tuple(
        row(
            fit_source(i),
            f"equity-{i}",
            label="Equity",
            statement=StatementType.BALANCE,
            canonical_id=target,
            abstention_reason="unmapped" if target is None else None,
        )
        for i, target in enumerate(targets)
    )
    # Scope B and a one-sided equity label must survive.
    others = (
        row(fit_source(4), "other-a", label="Sales"),
        row(fit_source(5), "other-b", label="Sales", canonical_id=None, abstention_reason="none"),
        row(
            fit_source(6),
            "other-c",
            label="Separate equity",
            statement=StatementType.BALANCE,
            canonical_id="total_equity",
        ),
    )
    # Validation supplies the other equity target for this key: it must not select it.
    valid = row(
        valid_source(),
        "valid",
        label="Separate equity",
        statement=StatementType.BALANCE,
        canonical_id="equity_attributable_parent",
    )
    return partitioned((*fit, *others), (valid,))


def freeze(tmp_path: Path) -> tuple[PartitionedExamples, FrozenExclusions]:
    parts = equity_parts()
    built = build_dataset(parts, count_tokens=lambda _: 1, tokenizer_id="fixture")
    write_dataset(built, tmp_path / "source")
    return parts, freeze_exclusions(tmp_path / "source", provenance="fixture, not independent gold")


def test_only_both_target_train_keys_are_selected_and_all_their_rows_removed(
    tmp_path: Path,
) -> None:
    parts, artifact = freeze(tmp_path)
    assert len(artifact.keys) == 1
    assert artifact.removed_targets == {
        "equity_attributable_parent": 1,
        "total_equity": 1,
        "null": 1,
        "total_liabilities_and_equity": 1,
    }
    original = build_dataset(parts, count_tokens=lambda _: 1, tokenizer_id="fixture")
    built = build_dataset(
        parts, count_tokens=lambda _: 1, tokenizer_id="fixture", exclusions=artifact
    )
    assert len(built.fit_rows) == 3
    assert built.validation_rows == original.validation_rows
    assert built.manifest.valid_sha256 == original.manifest.valid_sha256
    assert built.manifest.fit_lengths.count == built.manifest.fit_rows_after == 3
    assert sum(built.manifest.fit_strata.values()) == 3
    assert built.manifest.train_sha256 != original.manifest.train_sha256
    assert built.manifest.exclusion.source_train_sha256 == original.manifest.train_sha256
    write_dataset(built, tmp_path / "filtered")
    assert (tmp_path / "filtered/valid.jsonl").read_bytes() == (
        tmp_path / "source/valid.jsonl"
    ).read_bytes()
    assert "exclusion" not in json.loads((tmp_path / "source/manifest.json").read_text())


@pytest.mark.parametrize(
    "field,value",
    [
        ("keys", ("changed",)),
        ("keys_sha256", "f" * 64),
        ("builder_version", 999),
        ("source_train_sha256", "f" * 64),
        ("source_valid_sha256", "f" * 64),
        ("system_prompt_sha256", "f" * 64),
        ("rows_before", 100),
        ("rows_after", 100),
        ("removed_rows", 100),
        ("removed_targets", {"total_equity": 3}),
    ],
)
def test_tampering_and_source_mismatches_fail_loudly(
    tmp_path: Path, field: str, value: object
) -> None:
    parts, artifact = freeze(tmp_path)
    altered = artifact.model_copy(update={field: value})
    with pytest.raises(DatasetError):
        build_dataset(parts, count_tokens=lambda _: 1, tokenizer_id="fixture", exclusions=altered)


def test_dataset_hash_mismatch_is_refused(tmp_path: Path) -> None:
    parts, artifact = freeze(tmp_path)
    altered = artifact.model_copy(
        update={"dataset": artifact.dataset.model_copy(update={"input_sha256": "f" * 64})}
    )
    with pytest.raises(DatasetError, match="dataset"):
        build_dataset(parts, count_tokens=lambda _: 1, tokenizer_id="fixture", exclusions=altered)


@pytest.mark.parametrize(
    "update",
    [
        {"removed_rows": 3, "rows_after": 4, "removed_targets": {"total_equity": 3}},
        {
            "removed_targets": {
                "equity_attributable_parent": 1,
                "total_equity": 2,
                "null": 1,
            }
        },
    ],
)
def test_consistent_but_incorrect_removal_audit_is_refused(
    tmp_path: Path, update: dict[str, object]
) -> None:
    parts, artifact = freeze(tmp_path)
    altered = FrozenExclusions.model_validate(artifact.model_copy(update=update))
    with pytest.raises(DatasetError, match="removal count, target breakdown"):
        build_dataset(parts, count_tokens=lambda _: 1, tokenizer_id="fixture", exclusions=altered)


def test_load_tampered_list_raises_dataset_error(tmp_path: Path) -> None:
    _, artifact = freeze(tmp_path)
    path = tmp_path / "exclusions.json"
    raw = artifact.model_dump(mode="json")
    raw["keys"].append("tampered")
    path.write_text(json.dumps(raw))
    with pytest.raises(DatasetError):
        load_exclusions(path)


def test_freeze_verifies_source_files(tmp_path: Path) -> None:
    freeze(tmp_path)
    with (tmp_path / "source/train.jsonl").open("a") as handle:
        handle.write("\n")
    with pytest.raises(DatasetError, match="sha256"):
        freeze_exclusions(tmp_path / "source", provenance="fixture")


def test_population_membership_uses_exact_shared_rendering(tmp_path: Path) -> None:
    _, artifact = freeze(tmp_path)
    rows = tuple(
        sample(str(i), target).model_copy(
            update={"statement": StatementType.BALANCE, "label": label}
        )
        for i, (label, target) in enumerate(
            (
                ("Equity", "total_equity"),
                ("Equity", None),
                ("equity", "equity_attributable_parent"),
                ("Separate equity", "total_equity"),
            )
        )
    )
    predictions = tuple(prediction(r.example_id, None) for r in rows)
    assert inference_messages(rows[0])[1]["content"] == artifact.keys[0]
    report = population_report(rows, predictions, artifact)
    assert report["full_validation"]["score"] == score(rows, predictions)
    assert report["validation_excluding_ambiguity"]["score"]["rows"] == 2
    diagnostic = report["ambiguity_only"]
    assert diagnostic["score"]["rows"] == 2
    assert diagnostic["distinct_inputs"] == 1
    assert diagnostic["inputs"][0]["target_breakdown"] == {"total_equity": 1, "null": 1}
    assert [p["example_id"] for p in diagnostic["inputs"][0]["predictions"]] == ["0", "1"]
    assert "not comparable" in report["validation_excluding_ambiguity"]["label"]


def test_empty_diagnostic_is_explicit_not_a_perfect_score(tmp_path: Path) -> None:
    _, artifact = freeze(tmp_path)
    rows = (sample("a", None),)
    report = population_report(rows, (prediction("a", None),), artifact)
    assert report["ambiguity_only"]["score"] is None
    assert report["ambiguity_only"]["rows"] == 0


def test_cli_freeze_rebuild_and_report(tmp_path: Path) -> None:
    files = Files(tmp_path)
    parts = equity_parts()
    # Include an ambiguous validation key alongside the validation-only disagreement key.
    ambiguous = row(
        valid_source(1),
        "ambiguous",
        label="Equity",
        statement=StatementType.BALANCE,
        canonical_id="total_equity",
    )
    files.write((*parts.fit, *parts.validation, ambiguous))
    assert build_main(files.args()) == 0
    frozen = tmp_path / "exclusions.json"
    command = ["--dataset-dir", str(files.out), "--out", str(frozen), "--provenance", "fixture"]
    assert freeze_main(command) == 0
    assert freeze_main(command) == 1
    original_out = files.out
    files.out = tmp_path / "rebuilt"
    assert build_main([*files.args(), "--exclusions", str(frozen)]) == 0
    assert (original_out / "valid.jsonl").read_bytes() == (files.out / "valid.jsonl").read_bytes()
    assert validation_main([*validation_args(files), "--exclusions", str(frozen)]) == 0
    report = json.loads((tmp_path / "evaluation/report.json").read_text())
    assert report["rows"] == report["populations"]["full_validation"]["score"]["rows"] == 2
    assert report["populations"]["ambiguity_only"]["rows"] == 1


def test_unexcluded_manifest_serialization_preserves_selection_identity(tmp_path: Path) -> None:
    parts, _ = freeze(tmp_path)
    manifest = build_dataset(parts, count_tokens=lambda _: 1, tokenizer_id="fixture").manifest
    assert "exclusion" not in manifest.model_dump(mode="json")
