"""Validation reconstruction cannot drift from the approved rendered dataset."""

import json
from pathlib import Path

import pytest
from build_dataset import main as build
from dataset_contracts import DatasetError
from normalization_validation import load_selection, main, prepare_validation
from test_build_dataset import Files, pair
from test_dataset_builder import row, valid_source
from test_dataset_contracts import cik_in, source

from fra_core.pools import Pool
from fra_core.split import Part


@pytest.fixture
def files(tmp_path: Path) -> Files:
    files = Files(tmp_path)
    # A later duplicate must not shift the identity/metadata aligned to the one rendered row.
    files.write((*pair(), row(valid_source(), "z-duplicate")))
    assert build(files.args()) == 0
    return files


@pytest.fixture
def holdout_roster(tmp_path: Path) -> Files:
    files = Files(tmp_path)
    examples = pair()
    held = source(cik_in(Part.HOLDOUT))
    files.write(examples, records=(*(e.source_record for e in examples), held))
    assert build(files.args()) == 0
    return files


def prepare(files: Files):  # type: ignore[no-untyped-def]
    return prepare_validation(
        dataset_dir=files.out,
        examples_path=files.examples,
        sources_path=files.sources,
        approved_path=files.approved,
        registry_path=files.registry,
        moves_path=files.moves,
        looks_path=files.looks,
    )


def args(files: Files) -> list[str]:
    return [
        "prepare",
        "--dataset-dir",
        str(files.out),
        "--examples",
        str(files.examples),
        "--sources",
        str(files.sources),
        "--approved",
        str(files.approved),
        "--registry",
        str(files.registry),
        "--moves",
        str(files.moves),
        "--looks",
        str(files.looks),
        "--out-dir",
        str(files.root / "evaluation"),
    ]


def test_reconstruction_keeps_deduped_example_identity(files: Files) -> None:
    selected = prepare(files)
    assert len(selected.rows) == selected.available_rows == 1
    assert selected.rows[0].example_id == "b-1"
    assert selected.rows[0].issuer.startswith("cik:")
    assert selected.rows[0].period == "annual"


def test_holdout_metadata_preserves_builder_partition(holdout_roster: Files) -> None:
    selected = prepare(holdout_roster)
    manifest = json.loads((holdout_roster.out / "manifest.json").read_text())
    assert selected.dataset_manifest.model_dump(mode="json") == manifest
    assert selected.dataset_manifest.fit_rows_after == manifest["fit_rows_after"] == 1
    assert selected.available_rows == manifest["validation_rows_after"] == 1
    assert [r.example_id for r in selected.rows] == ["b-1"]


def test_holdout_example_is_refused_without_outputs(
    holdout_roster: Files, capsys: pytest.CaptureFixture[str]
) -> None:
    files = holdout_roster
    original_dataset = {p.name: p.read_bytes() for p in files.out.iterdir()}
    held = source(cik_in(Part.HOLDOUT))
    files.write((*pair(), row(held, "held-1")))
    message = "holdout source is excluded from supervision"
    with pytest.raises(DatasetError, match=message):
        prepare(files)
    assert main(args(files)) == 1
    assert message in capsys.readouterr().err
    assert not (files.root / "evaluation").exists()
    assert {p.name: p.read_bytes() for p in files.out.iterdir()} == original_dataset

    rejected_dataset = files.root / "rejected-dataset"
    build_args = files.args()
    build_args[build_args.index("--out-dir") + 1] = str(rejected_dataset)
    assert build(build_args) == 1
    assert message in capsys.readouterr().err
    assert not rejected_dataset.exists()


def test_cli_writes_a_bound_selection_and_predictions_and_refuses_overwrite(files: Files) -> None:
    assert main(args(files)) == 0
    directory = files.root / "evaluation"
    selected = load_selection(directory / "selection.json")
    predictions = json.loads((directory / "lexicon.json").read_text())
    assert predictions["selection_sha256"] == selected.fingerprint()
    assert [r["example_id"] for r in predictions["predictions"]] == ["b-1"]
    original = (directory / "report.json").read_bytes()
    assert main(args(files)) == 1
    assert (directory / "report.json").read_bytes() == original


def test_missing_registry_is_refused_before_examples_are_read(files: Files) -> None:
    files.registry.unlink()
    files.examples.write_text("not json")
    with pytest.raises(DatasetError, match="pool registry not found"):
        prepare(files)


def test_nontrain_roster_is_refused_before_examples_are_read(files: Files) -> None:
    records = [json.loads(line) for line in files.sources.read_text().splitlines()]
    records[0]["pool"] = Pool.MODEL_TEST.value
    files.sources.write_text("\n".join(json.dumps(r) for r in records))
    files.examples.write_text("not json")
    with pytest.raises(DatasetError, match="only train"):
        prepare(files)


@pytest.mark.parametrize("name", ["train.jsonl", "valid.jsonl"])
def test_changed_dataset_bytes_are_refused(files: Files, name: str) -> None:
    with (files.out / name).open("a") as handle:
        handle.write("\n")
    with pytest.raises(DatasetError, match="sha256"):
        prepare(files)


def test_changed_approvals_are_refused(files: Files) -> None:
    files.approved.write_text("")
    with pytest.raises(DatasetError, match="approved"):
        prepare(files)


def test_a_self_consistent_changed_validation_file_still_fails_alignment(files: Files) -> None:
    import hashlib

    path = files.out / "valid.jsonl"
    data = json.loads(path.read_text())
    data["messages"][1]["content"] = '{"label":"Invented label","statement":"income"}'
    path.write_text(json.dumps(data) + "\n")
    manifest_path = files.out / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["valid_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(DatasetError, match="rendered validation"):
        prepare(files)


def test_partition_fingerprint_drift_is_refused(files: Files) -> None:
    path = files.out / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["dataset"]["registry_sha256"] = "f" * 64
    path.write_text(json.dumps(manifest))
    with pytest.raises(DatasetError, match="partition manifest"):
        prepare(files)
