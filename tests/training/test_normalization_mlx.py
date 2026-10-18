"""The local inference boundary sees no gold and reuses identical prompts only."""

import json
from pathlib import Path

import pytest
from chat_tokenizer import load_chat_counter
from dataset_contracts import DatasetError
from normalization_mlx import LocalModelManifest, model_identity, predict_rows
from normalization_validation import PredictionRun, Selection, file_sha256, score_run
from test_chat_tokenizer import make_tokenizer
from test_normalization_validation import files, prepare  # noqa: F401


def test_generation_gets_only_the_prompt_and_reuses_identical_inputs(files) -> None:  # type: ignore[no-untyped-def] # noqa: F811
    selection = prepare(files)
    first = selection.rows[0]
    second = first.model_copy(update={"example_id": "another-id", "issuer": "cik:999"})
    manifest = selection.dataset_manifest.model_copy(update={"validation_rows_after": 2})
    selection = Selection(dataset_manifest=manifest, available_rows=2, seed=0, rows=(first, second))
    seen = []

    def generate(messages: list[dict[str, str]]) -> str:
        seen.append(messages)
        assert [m["role"] for m in messages] == ["system", "user"]
        assert "canonical_id" not in messages[1]["content"]
        assert "cik:" not in str(messages)
        return '{"canonical_id": "revenue"}'

    run = predict_rows(selection, generate, predictor="fake-local", metadata={})
    assert len(seen) == 1
    assert [p.example_id for p in run.predictions] == ["b-1", "another-id"]
    assert run.metadata["unique_prompts"] == 1
    assert run.metadata["rows"] == 2
    assert score_run(selection, run)["accuracy"] == 1


def test_predictor_failure_is_loud(files) -> None:  # type: ignore[no-untyped-def] # noqa: F811
    def fail(messages: list[dict[str, str]]) -> str:
        raise RuntimeError("local inference failed")

    with pytest.raises(RuntimeError, match="local inference failed"):
        predict_rows(prepare(files), fail, predictor="fake-local", metadata={})


def test_scores_refuse_predictions_for_different_selections(files) -> None:  # type: ignore[no-untyped-def] # noqa: F811
    selection = prepare(files)
    run = PredictionRun(selection_sha256="f" * 64, predictor="fake", metadata={}, predictions=())
    with pytest.raises(DatasetError, match="different selection"):
        score_run(selection, run)


def test_local_model_path_must_exist_without_network_fallback(tmp_path: Path) -> None:
    with pytest.raises(DatasetError, match="local model"):
        model_identity(
            tmp_path / "missing", expected_tokenizer="a" * 64, snapshot=snapshot(tmp_path)
        )


def snapshot(path: Path) -> LocalModelManifest:
    return LocalModelManifest(
        repository="mlx-community/Qwen2.5-7B-Instruct-4bit",
        revision="a" * 40,
        local_directory=str(path),
        tokenizer="synthetic identity fixture",
        artifact_sha256={p.name: file_sha256(p) for p in path.iterdir() if p.is_file()},
    )


@pytest.fixture
def local_model(tmp_path: Path) -> Path:
    model = make_tokenizer(tmp_path / "local-model")
    config = {
        "model_type": "qwen2",
        "hidden_size": 3584,
        "num_hidden_layers": 28,
        "vocab_size": 152064,
        "quantization": {"bits": 4},
    }
    (model / "config.json").write_text(json.dumps(config))
    # Identity checks hash but do not load these bytes; MLX itself is not invoked here.
    (model / "model.safetensors").write_bytes(b"synthetic identity fixture")
    return model


@pytest.mark.parametrize("change", [{"model_type": "llama"}, {"hidden_size": 896}])
def test_wrong_model_family_or_size_is_refused(local_model: Path, change: dict) -> None:
    path = local_model / "config.json"
    config = json.loads(path.read_text())
    config.update(change)
    path.write_text(json.dumps(config))
    _, token_id = load_chat_counter(local_model)
    with pytest.raises(DatasetError, match="7B"):
        model_identity(local_model, expected_tokenizer=token_id, snapshot=snapshot(local_model))


def test_changed_weights_are_refused_against_the_pinned_manifest(local_model: Path) -> None:
    expected = snapshot(local_model)
    _, token_id = load_chat_counter(local_model)
    (local_model / "model.safetensors").write_bytes(b"different weights")
    with pytest.raises(DatasetError, match="pinned model manifest"):
        model_identity(local_model, expected_tokenizer=token_id, snapshot=expected)


def test_matching_pinned_model_records_repository_and_revision(local_model: Path) -> None:
    expected = snapshot(local_model)
    _, token_id = load_chat_counter(local_model)
    result = model_identity(local_model, expected_tokenizer=token_id, snapshot=expected)
    assert result["model_revision"] == "a" * 40
    assert result["model_repository"] == "mlx-community/Qwen2.5-7B-Instruct-4bit"
