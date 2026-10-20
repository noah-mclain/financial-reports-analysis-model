"""The training boundary refuses unsafe inputs before any MLX import or trainer call."""

import json
from pathlib import Path

import normalization_train as smoke
import pytest
from chat_tokenizer import load_chat_counter
from dataset_builder import build_dataset, write_dataset
from dataset_contracts import DatasetError
from normalization_validation import file_sha256
from pydantic import ValidationError
from test_dataset_exclusions import freeze
from test_normalization_mlx import local_model, snapshot  # noqa: F401


@pytest.fixture
def config(tmp_path: Path, local_model: Path, monkeypatch: pytest.MonkeyPatch) -> smoke.SmokeConfig:  # noqa: F811
    parts, exclusions = freeze(tmp_path)
    _, tokenizer_id = load_chat_counter(local_model)
    monkeypatch.setattr(smoke, "load_chat_counter", lambda _: (lambda messages: 32, tokenizer_id))
    # A fresh freeze is required because the tokenizer is part of exclusion identity.
    from dataset_exclusions import freeze_exclusions

    write_dataset(
        build_dataset(parts, count_tokens=lambda _: 1, tokenizer_id=tokenizer_id),
        tmp_path / "unfiltered",
    )
    exclusions = freeze_exclusions(tmp_path / "unfiltered", provenance="synthetic test")
    dataset = tmp_path / "filtered"
    write_dataset(
        build_dataset(
            parts, count_tokens=lambda _: 1, tokenizer_id=tokenizer_id, exclusions=exclusions
        ),
        dataset,
    )
    exclusion_path = tmp_path / "exclusions.json"
    exclusion_path.write_text(exclusions.model_dump_json())
    model_manifest = tmp_path / "model.json"
    model_manifest.write_text(snapshot(local_model).model_dump_json())
    return smoke.SmokeConfig(
        dataset_dir=dataset,
        dataset_manifest_sha256=file_sha256(dataset / "manifest.json"),
        exclusions=exclusion_path,
        model_dir=local_model,
        model_manifest=model_manifest,
        model_manifest_sha256=file_sha256(model_manifest),
        run_dir=tmp_path / "run",
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("iters", 0),
        ("iters", 21),
        ("batch_size", 2),
        ("num_layers", 17),
        ("max_seq_length", 2049),
        ("max_seq_length", 0),
        ("timeout_seconds", 0),
        ("timeout_seconds", 1801),
        ("grad_checkpoint", False),
        ("seed", 1),
    ],
)
def test_invalid_budgets(config: smoke.SmokeConfig, field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        smoke.SmokeConfig.model_validate({**config.model_dump(), field: value})


def resources() -> smoke.Resources:
    return smoke.Resources(
        free_disk_bytes=30_000_000_000, available_memory_bytes=15_000_000_000, services=()
    )


def test_fit_only_boundary_and_dry_run(config: smoke.SmokeConfig) -> None:
    (config.dataset_dir / "test.jsonl").write_text("must never be read")
    with pytest.raises(DatasetError, match=r"test\.jsonl"):
        smoke.verify_inputs(config)
    (config.dataset_dir / "test.jsonl").unlink()
    calls = []
    assert smoke.run(config, dry_run=True, probe=resources, trainer=lambda c: calls.append(c)) == 0
    report = json.loads((config.run_dir / "report.json").read_text())
    assert report["status"] == "preflight_passed"
    assert not calls
    options = smoke.training_options(config)
    assert options["batch_size"] == 1 and options["grad_checkpoint"] is True
    assert options["test"] is False and options["mask_prompt"] is True
    assert not (config.run_dir / "test.jsonl").exists()


@pytest.mark.parametrize("filename", ["train.jsonl", "valid.jsonl", "manifest.json"])
def test_mutated_dataset(config: smoke.SmokeConfig, filename: str) -> None:
    with (config.dataset_dir / filename).open("a") as handle:
        handle.write(" ")
    with pytest.raises(DatasetError, match="sha256"):
        smoke.verify_inputs(config)


@pytest.mark.parametrize("filename", ["model.safetensors", "tokenizer.json"])
def test_mutated_model(config: smoke.SmokeConfig, filename: str) -> None:
    with (config.model_dir / filename).open("ab") as handle:
        handle.write(b" ")
    with pytest.raises((DatasetError, ValueError)):
        smoke.verify_inputs(config)


def test_missing_and_mismatched_exclusions(config: smoke.SmokeConfig) -> None:
    config.exclusions.unlink()
    with pytest.raises(DatasetError, match="exclusion"):
        smoke.verify_inputs(config)


def test_mismatched_exclusion_evidence(config: smoke.SmokeConfig) -> None:
    data = json.loads(config.exclusions.read_text())
    data["source_valid_sha256"] = "f" * 64
    config.exclusions.write_text(json.dumps(data))
    with pytest.raises(DatasetError, match="valid sha256"):
        smoke.verify_inputs(config)


def test_no_silent_truncation(config: smoke.SmokeConfig) -> None:
    config = config.model_copy(update={"max_seq_length": 1})
    with pytest.raises(DatasetError, match="sequence cap"):
        smoke.bounded_rows(config)


@pytest.mark.parametrize("state", ["directory", "symlink"])
def test_output_reuse(config: smoke.SmokeConfig, state: str) -> None:
    if state == "directory":
        config.run_dir.mkdir()
    else:
        config.run_dir.symlink_to(config.run_dir.parent / "missing")
    with pytest.raises(DatasetError, match="run directory"):
        smoke.run(config, dry_run=True, probe=resources)


@pytest.mark.parametrize(
    "bad",
    [
        {"free_disk_bytes": 14_999_999_999},
        {"available_memory_bytes": 13_999_999_999},
        {"services": ("fra-worker",)},
    ],
)
def test_preflight_refuses_before_trainer(config: smoke.SmokeConfig, bad: dict) -> None:
    state = resources().model_copy(update=bad)
    called = []
    assert (
        smoke.run(config, dry_run=False, probe=lambda: state, trainer=lambda c: called.append(c))
        == 1
    )
    assert not called
    assert json.loads((config.run_dir / "report.json").read_text())["status"] == "failed"


def test_stage_specific_resource_thresholds_preserve_training_default() -> None:
    at_training_minimum = smoke.Resources(
        free_disk_bytes=20_000_000_000,
        available_memory_bytes=smoke.MIN_MEMORY_BYTES,
        services=(),
    )
    at_training_minimum.check()
    with pytest.raises(DatasetError, match="14 GB available memory"):
        at_training_minimum.model_copy(
            update={"available_memory_bytes": smoke.MIN_MEMORY_BYTES - 1}
        ).check()
    at_13gb = smoke.Resources(
        free_disk_bytes=20_000_000_000,
        available_memory_bytes=13_000_000_000,
        services=(),
    )
    at_13gb.check(smoke.PROBE_MIN_MEMORY_BYTES)
    with pytest.raises(DatasetError, match="14 GB available memory"):
        at_13gb.check()
    with pytest.raises(DatasetError, match="available memory"):
        smoke.Resources(
            free_disk_bytes=20_000_000_000,
            available_memory_bytes=smoke.PROBE_MIN_MEMORY_BYTES - 1,
            services=(),
        ).check(smoke.PROBE_MIN_MEMORY_BYTES)
    at_probe_minimum = smoke.Resources(
        free_disk_bytes=20_000_000_000,
        available_memory_bytes=smoke.PROBE_MIN_MEMORY_BYTES,
        services=(),
    )
    at_probe_minimum.check(smoke.PROBE_MIN_MEMORY_BYTES)
    with pytest.raises(DatasetError, match="available memory"):
        at_probe_minimum.model_copy(
            update={"available_memory_bytes": smoke.PROBE_MIN_MEMORY_BYTES - 1}
        ).check(smoke.PROBE_MIN_MEMORY_BYTES)


@pytest.mark.parametrize("minimum", [0, -1])
def test_resource_threshold_must_be_positive(minimum: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        resources().check(minimum)


def test_disk_and_services_refuse_for_every_memory_budget() -> None:
    for minimum in (smoke.MIN_MEMORY_BYTES, smoke.PROBE_MIN_MEMORY_BYTES):
        for bad in (
            {"free_disk_bytes": smoke.MIN_DISK_BYTES - 1},
            {"services": ("fra-worker",)},
        ):
            with pytest.raises(DatasetError):
                resources().model_copy(update=bad).check(minimum)


def test_failure_and_timeout_are_recorded(config: smoke.SmokeConfig) -> None:
    def fail(c: smoke.SmokeConfig) -> dict:
        raise TimeoutError("runtime bound exceeded")

    assert smoke.run(config, dry_run=False, probe=resources, trainer=fail) == 1
    report = json.loads((config.run_dir / "report.json").read_text())
    assert report["status"] == "failed" and "runtime bound" in report["error"]


@pytest.mark.parametrize("field", ["dataset_manifest_sha256", "model_manifest_sha256"])
def test_wrong_manifest_pin(config: smoke.SmokeConfig, field: str) -> None:
    with pytest.raises(DatasetError, match="sha256"):
        smoke.verify_inputs(config.model_copy(update={field: "f" * 64}))


def test_unfiltered_dataset_refused(config: smoke.SmokeConfig) -> None:
    dataset = config.dataset_dir.parent / "unfiltered"
    config = config.model_copy(
        update={
            "dataset_dir": dataset,
            "dataset_manifest_sha256": file_sha256(dataset / "manifest.json"),
        }
    )
    with pytest.raises(DatasetError, match="filtered dataset"):
        smoke.verify_inputs(config)


def test_child_deadline_and_offline_environment(
    config: smoke.SmokeConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    def timeout(command, **kwargs):
        assert command[-1] == "--worker"
        assert kwargs["timeout"] == config.timeout_seconds
        assert kwargs["env"]["HF_HUB_OFFLINE"] == "1"
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(smoke.subprocess, "run", timeout)
    assert smoke.run(config, dry_run=False, probe=resources) == 1
    report = json.loads((config.run_dir / "report.json").read_text())
    assert "TimeoutExpired" in report["error"]
    assert report["resources"]["peak_mlx_bytes"] is None


@pytest.mark.parametrize("overflow", [False, True, "empty"])
def test_upstream_boundary_without_metal(
    config: smoke.SmokeConfig, monkeypatch: pytest.MonkeyPatch, overflow: bool | str
) -> None:
    import sys
    from types import ModuleType

    calls = []
    mx = ModuleType("mlx.core")
    for name in ("set_cache_limit", "set_memory_limit", "reset_peak_memory"):
        monkeypatch.setattr(mx, name, lambda *args: None, raising=False)
    monkeypatch.setattr(mx, "get_peak_memory", lambda: 1, raising=False)
    mlx = ModuleType("mlx")
    mlx.core = mx
    lm = ModuleType("mlx_lm")
    lm.load = lambda *args, **kwargs: (object(), object())
    lora = ModuleType("mlx_lm.lora")

    def fitted(options, model, fit, valid, *, loss):
        assert loss is smoke.exclude_padding_loss
        calls.append((options, fit, valid))

    monkeypatch.setattr(smoke, "fit", fitted)
    datasets = ModuleType("mlx_lm.tuner.datasets")

    class Chat:
        def __init__(self, rows, tokenizer, mask_prompt):
            assert mask_prompt is True
            self.rows = rows

        def __len__(self):
            return len(self.rows)

        def __getitem__(self, i):
            return self.rows[i]

        def process(self, row):
            return ([1] * (2049 if overflow is True else 32), 31 if overflow == "empty" else 16)

    datasets.ChatDataset = Chat
    for name, module in {
        "mlx": mlx,
        "mlx.core": mx,
        "mlx_lm": lm,
        "mlx_lm.lora": lora,
        "mlx_lm.tuner.datasets": datasets,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(smoke, "probe_resources", resources)
    config.run_dir.mkdir()
    if overflow:
        with pytest.raises(DatasetError, match="upstream tokenization"):
            smoke.train_local(config)
        assert not calls
        assert not (config.run_dir / "adapter").exists()
    else:
        smoke.train_local(config)
        options, fit, valid = calls[0]
        assert len(fit) == 3 and len(valid) == 1
        assert options.test is False and options.batch_size == 1
        assert options.iters <= 20 and options.num_layers <= 16
        with pytest.raises(DatasetError, match="already exists"):
            smoke.train_local(config)


def test_probe_failure_is_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    monkeypatch.setattr(smoke.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(smoke.platform, "machine", lambda: "arm64")

    def denied(command, **kwargs):
        raise subprocess.CalledProcessError(1, command, stderr="Operation not permitted")

    monkeypatch.setattr(smoke.subprocess, "run", denied)
    with pytest.raises(DatasetError, match="Operation not permitted"):
        smoke.probe_resources()


def test_dry_run_cannot_enter_worker(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as error:
        smoke.main(["--config", str(tmp_path / "missing.json"), "--dry-run", "--worker"])
    assert error.value.code == 2


@pytest.fixture(scope="module")
def cpu_mlx():
    import platform

    if platform.system() == "Darwin":
        # A missing runtime on the owner machine is a failure, never a passing skip.
        import mlx.core as mx
    else:
        mx = pytest.importorskip("mlx.core")
    previous = mx.default_device()
    mx.set_default_device(mx.cpu)
    yield mx
    mx.set_default_device(previous)


class TokenRows:
    def __init__(self, pairs):
        self.pairs = pairs

    def __len__(self):
        return len(self.pairs)

    def __getitem__(self, index):
        return self.pairs[index]

    def process(self, row):
        return row


def bigram_batch(mx, pairs, cap=64):
    import numpy as np
    from mlx_lm.tuner.datasets import CacheDataset
    from mlx_lm.tuner.trainer import iterate_batches

    np.random.seed(17)
    return next(iterate_batches(CacheDataset(TokenRows(pairs)), len(pairs), cap))


def expected_ce(table, batch, lengths, inclusive=False):
    import numpy as np

    values = []
    for ids, (offset, length) in zip(np.asarray(batch), np.asarray(lengths), strict=True):
        for pos in range(max(1, offset), min(length + int(inclusive), len(ids))):
            scores = table[ids[pos - 1]].astype(np.float64)
            maximum = scores.max()
            values.append(np.log(np.exp(scores - maximum).sum()) + maximum - scores[ids[pos]])
    return np.mean(values), len(values)


@pytest.mark.parametrize("lengths,cap,expected", [([10], 64, 4), ([10], 10, 4), ([10, 14], 64, 9)])
def test_padding_loss_numerical(cpu_mlx, lengths, cap, expected):
    import numpy as np
    from mlx_lm.tuner.trainer import default_loss

    mx = cpu_mlx
    pairs = [(list(range(1, length + 1)), 6 if length == 10 else 9) for length in lengths]
    batch, bounds = bigram_batch(mx, pairs, cap)
    table = np.random.default_rng(21).normal(size=(32, 32)).astype(np.float32)
    target, count = expected_ce(table, batch, bounds)
    value, ntoks = smoke.exclude_padding_loss(lambda inputs: mx.array(table)[inputs], batch, bounds)
    assert ntoks.item() == count == expected
    assert value.item() == pytest.approx(target, abs=2e-6)
    if cap == 10:
        old, old_count = default_loss(lambda inputs: mx.array(table)[inputs], batch, bounds)
        assert old_count.item() == count
        assert old.item() == value.item()
    else:
        assert batch.shape[1] == 33


def test_content_end_newline_retained_and_padding_invariant(cpu_mlx):
    import numpy as np
    from mlx_lm.tuner.trainer import default_loss

    mx = cpu_mlx
    batch, bounds = bigram_batch(mx, [(list(range(1, 11)), 6)])
    table = np.zeros((32, 32), np.float32)
    # Four distinct true targets (first content, content, endmarker, newline), then pad CE ~20.
    for pos, score in zip(range(6, 11), [3, 2, 1, 0, -17], strict=True):
        table[pos, 0 if pos == 10 else pos + 1] = score
    target, count = expected_ce(table, batch, bounds)
    loss, ntoks = smoke.exclude_padding_loss(lambda x: mx.array(table)[x], batch, bounds)
    old, _ = default_loss(lambda x: mx.array(table)[x], batch, bounds)
    changed = table.copy()
    changed[10] = np.arange(32, dtype=np.float32)
    changed[0] = -np.arange(32, dtype=np.float32)
    changed_batch = np.asarray(batch).copy()
    changed_batch[:, 10:] = 31
    after, _ = smoke.exclude_padding_loss(
        lambda x: mx.array(changed)[x], mx.array(changed_batch), bounds
    )
    after_old, _ = default_loss(lambda x: mx.array(changed)[x], mx.array(changed_batch), bounds)
    assert count == ntoks.item() == 4
    assert loss.item() == pytest.approx(target, abs=2e-6)
    assert loss.item() == after.item()
    assert old.item() != after_old.item()
    assert old.item() * 5 - loss.item() * 4 == pytest.approx(20.43, abs=0.1)


@pytest.mark.parametrize("arm", ["default_loss", "exclude_padding_loss"])
def test_real_cpu_fit_loss_forwarding(cpu_mlx, tmp_path, monkeypatch, arm):
    from types import SimpleNamespace

    import mlx.nn as nn
    import mlx_lm.tuner.trainer as trainer
    import numpy as np

    mx = cpu_mlx
    # Hardware may expose Metal while this integration test deliberately runs on the CPU.
    # Keep upstream trainer setup on its CPU path, which does not require wired-memory limits.
    monkeypatch.setattr(mx.metal, "is_available", lambda: False)

    class Block(nn.Module):
        def __init__(self):
            super().__init__()
            self.table = nn.Embedding(32, 32)

    class Bigram(nn.Module):
        def __init__(self):
            super().__init__()
            self.layers = [Block()]

        def __call__(self, inputs):
            return self.layers[0].table(inputs)

    model = Bigram()
    pairs = [(list(range(1, 11)), 6)]
    batch, bounds = bigram_batch(mx, pairs)
    initial = np.asarray(model.layers[0].table.weight).copy()
    expected, count = expected_ce(initial, batch, bounds, inclusive=arm == "default_loss")
    selected = trainer.default_loss if arm == "default_loss" else smoke.exclude_padding_loss
    actual_train = trainer.train
    calls = []

    def observed(**kwargs):
        assert kwargs["loss"] is selected
        calls.append(kwargs["loss"])
        return actual_train(**kwargs)

    monkeypatch.setattr(trainer, "train", observed)
    options = SimpleNamespace(
        fine_tune_type="lora",
        optimizer="adamw",
        optimizer_config={"adamw": {}},
        resume_adapter_file=None,
        lr_schedule=None,
        seed=17,
        num_layers=1,
        lora_parameters={"rank": 2, "scale": 2.0, "dropout": 0.0},
        adapter_path=str(tmp_path / "adapter"),
        batch_size=1,
        iters=2,
        val_batches=1,
        steps_per_report=1,
        steps_per_eval=2,
        save_every=1,
        max_seq_length=64,
        grad_checkpoint=False,
        grad_accumulation_steps=1,
        learning_rate=1e-4,
    )
    reports = smoke.fit(options, model, TokenRows(pairs), TokenRows(pairs), loss=selected)
    assert calls == [selected]
    assert reports["train"][0]["iteration"] == 1
    assert reports["train"][0]["train_loss"] == pytest.approx(expected, abs=2e-6)
    assert reports["train"][0]["trained_tokens"] == count == (5 if arm == "default_loss" else 4)
    assert reports["validation"][0]["val_loss"] == pytest.approx(expected, abs=2e-6)
    assert (tmp_path / "adapter" / "0000002_adapters.safetensors").is_file()


def test_fit_empty_supervision_before_optimizer(cpu_mlx, tmp_path, monkeypatch):
    from types import SimpleNamespace

    import mlx.optimizers as optim

    def forbidden(*args, **kwargs):
        pytest.fail("optimizer must not be constructed for empty supervision")

    monkeypatch.setattr(optim, "AdamW", forbidden)
    with pytest.raises(DatasetError, match="masks all targets"):
        smoke.fit(
            SimpleNamespace(max_seq_length=64),
            object(),
            TokenRows([([1] * 10, 10)]),
            TokenRows([([1] * 10, 6)]),
            loss=smoke.exclude_padding_loss,
        )
    assert not (tmp_path / "adapter").exists()


@pytest.mark.parametrize("failure", [None, "train", "validation", "empty"])
def test_fit_public_boundary_without_metal(tmp_path, monkeypatch, failure):
    import sys
    from types import ModuleType, SimpleNamespace

    calls = []
    mx = ModuleType("mlx.core")
    mx.random = SimpleNamespace(seed=lambda value: calls.append(("seed", value)))
    optim = ModuleType("mlx.optimizers")
    optim.AdamW = lambda **kwargs: calls.append(("optimizer", kwargs))
    datasets = ModuleType("mlx_lm.tuner.datasets")

    class Cache:
        def __init__(self, data):
            self.data = data

        def __len__(self):
            return len(self.data)

        def __getitem__(self, index):
            return self.data.process(self.data[index])

    datasets.CacheDataset = Cache
    trainer = ModuleType("mlx_lm.tuner.trainer")
    trainer.TrainingArgs = SimpleNamespace
    callbacks = ModuleType("mlx_lm.tuner.callbacks")
    callbacks.TrainingCallback = object

    def train(**kwargs):
        calls.append(("train", kwargs))
        assert kwargs["loss"] is smoke.exclude_padding_loss
        assert kwargs["args"].steps_per_save == 5
        callback = kwargs["training_callback"]
        callback.on_val_loss_report({"val_loss": float("nan") if failure == "validation" else 1.0})
        callback.on_train_loss_report({"train_loss": float("inf") if failure == "train" else 2.0})
        Path(kwargs["args"].adapter_file).write_text("checkpoint")

    trainer.train = train
    tuner_utils = ModuleType("mlx_lm.tuner.utils")
    tuner_utils.linear_to_lora_layers = lambda *args, **kwargs: calls.append(("lora", kwargs))
    utils = ModuleType("mlx_lm.utils")
    utils.save_config = lambda config, path: Path(path).write_text(json.dumps(config))
    mlx = ModuleType("mlx")
    mlx.core, mlx.optimizers = mx, optim
    for name, module in {
        "mlx": mlx,
        "mlx.core": mx,
        "mlx.optimizers": optim,
        "mlx_lm.tuner.datasets": datasets,
        "mlx_lm.tuner.trainer": trainer,
        "mlx_lm.tuner.callbacks": callbacks,
        "mlx_lm.tuner.utils": tuner_utils,
        "mlx_lm.utils": utils,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    options = SimpleNamespace(
        max_seq_length=64,
        fine_tune_type="lora",
        optimizer="adamw",
        resume_adapter_file=None,
        lr_schedule=None,
        steps_per_report=1,
        seed=17,
        num_layers=1,
        lora_parameters={"rank": 2},
        adapter_path=str(tmp_path / "adapter"),
        batch_size=1,
        iters=1,
        val_batches=1,
        steps_per_eval=1,
        save_every=5,
        grad_checkpoint=False,
        grad_accumulation_steps=1,
        learning_rate=1e-4,
        optimizer_config={"adamw": {}},
    )
    model = SimpleNamespace(freeze=lambda: calls.append(("freeze",)), layers=[object()])
    rows = TokenRows([([1] * 10, 10 if failure == "empty" else 6)])
    if failure:
        with pytest.raises(
            DatasetError, match="masks all targets" if failure == "empty" else "nonfinite"
        ):
            smoke.fit(options, model, rows, rows, loss=smoke.exclude_padding_loss)
        assert not (tmp_path / "adapter/adapters.safetensors").exists()
        if failure == "empty":
            assert not calls
            assert not (tmp_path / "adapter").exists()
    else:
        reports = smoke.fit(options, model, rows, rows, loss=smoke.exclude_padding_loss)
        assert reports == {"train": [{"train_loss": 2.0}], "validation": [{"val_loss": 1.0}]}
        saved = json.loads((tmp_path / "adapter/adapter_config.json").read_text())
        assert saved["save_every"] == 5 and "steps_per_save" not in saved
