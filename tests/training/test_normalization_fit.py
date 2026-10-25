"""Bounded fit registration and failure evidence, without a training backend."""

import copy
import importlib.util
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import normalization_fit as fit
import normalization_stability as stability
import normalization_train as smoke
import pytest
from dataset_contracts import DatasetError
from pydantic import ValidationError
from test_normalization_stability import encode, rows, trainer_stubs  # noqa: F401
from test_normalization_train import config, local_model, resources  # noqa: F401


def configuration(base, **updates):
    return fit.FitConfig.model_validate(
        {
            **base.model_dump(exclude={"iters", "batch_size", "timeout_seconds"}),
            "iters": 2,
            "exclusions_sha256": smoke.file_sha256(base.exclusions),
            **updates,
        }
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"iters": 0},
        {"iters": 3001},
        {"iters": 1000, "timeout_seconds": 24999},
        {"timeout_seconds": 75001},
        {"batch_size": 1},
        {"learning_rate": 0.1},
        {"adamw_bias_correction": False},
        {"grad_checkpoint": False},
        {"seed": 18},
        {"num_layers": 15},
        {"max_seq_length": 1024},
    ],
)
def test_fixed_bounds(config, updates):  # noqa: F811
    with pytest.raises(ValidationError):
        configuration(config, **updates)


def test_config_defaults_and_explicit_long_timeout(config):  # noqa: F811
    conf = configuration(config, iters=1000, timeout_seconds=25000)
    options = smoke.training_options(conf)
    assert options["learning_rate"] == 1e-5
    assert options["optimizer_config"]["adamw"]["bias_correction"] is True
    assert options["save_every"] == 1000 and options["steps_per_eval"] == 50
    assert options["lr_schedule"] is None and options["val_batches"] == 64
    assert fit.FitConfig.model_fields["iters"].default == 1000
    assert fit.FitConfig.model_fields["timeout_seconds"].default == 25000
    with pytest.raises(ValidationError, match="25"):
        configuration(config, iters=1000, timeout_seconds=24999)
    configuration(config, iters=3000, timeout_seconds=75000)
    with pytest.raises(ValidationError):
        configuration(config, iters=3000, timeout_seconds=74999)
    with pytest.raises(ValidationError):
        configuration(config, iters=3001, timeout_seconds=75000)
    with pytest.raises(ValidationError):
        configuration(config, iters=1000, timeout_seconds=75001)


@pytest.fixture
def registered(config, monkeypatch, tmp_path):  # noqa: F811
    conf = configuration(config)
    monkeypatch.setattr(stability, "DATASET_PIN", conf.dataset_manifest_sha256)
    monkeypatch.setattr(stability, "EXCLUSIONS_PIN", conf.exclusions_sha256)
    monkeypatch.setattr(stability, "read_rows", lambda _: rows(100))
    monkeypatch.setattr(stability, "encoder", lambda *_: encode)
    monkeypatch.setattr(fit, "open_chat_tokenizer", smoke.open_chat_tokenizer)
    plan = tmp_path / "plan.json"
    plan.write_text(conf.model_dump_json())
    out = tmp_path / "registration.json"
    fit.preregister(plan, out)
    return fit.FitConfig.model_validate_json((tmp_path / "fit.json").read_bytes())


def test_registration_binds_selection_budget_and_read_only_files(registered):
    record, selected = fit.verify_preregistration(registered)
    assert len(selected[0]) == 8 and len(selected[1]) == 64
    assert record["budget"] == {"updates": 2, "examples": 8, "supervised_tokens": 40}
    assert record["selection"]["fit"]["permuted"] == record["selection"]["fit"]["selected"]
    assert fit.verify_preregistration(registered)[0] == record
    assert record["design"]["candidate"] == "final update-2 adapter; no best-checkpoint selection"
    assert record["design"]["validation_steps"] == [0, 2]
    for name in ("registration.json", "fit.json", "fit-dry.json"):
        assert registered.preregistration.parent.joinpath(name).stat().st_mode & 0o222 == 0
    expected = {
        "normalization_fit.py",
        "normalization_stability.py",
        "normalization_train.py",
        "dataset_builder.py",
        "dataset_exclusions.py",
        "exclusive_write.py",
        "chat_tokenizer.py",
    }
    assert expected <= {Path(name).name for name in record["implementation"]}


@pytest.mark.parametrize(
    "mutation", ["registration", "dataset", "exclusion", "model", "source", "config"]
)
def test_drift_refused(registered, monkeypatch, mutation):
    if mutation == "registration":
        registered.preregistration.chmod(0o644)
        registered.preregistration.write_text("{}")
    elif mutation == "source":
        monkeypatch.setattr(fit, "implementations", lambda: {"changed": "f" * 64})
    elif mutation == "config":
        registered = registered.model_copy(update={"iters": 3})
    else:
        path = {
            "dataset": registered.dataset_dir / "train.jsonl",
            "exclusion": registered.exclusions,
            "model": registered.model_manifest,
        }[mutation]
        path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(DatasetError):
        fit.verify_preregistration(registered)


def test_unfiltered_or_replaced_exclusions_refused(config, monkeypatch):  # noqa: F811
    conf = configuration(config)
    monkeypatch.setattr(stability, "DATASET_PIN", conf.dataset_manifest_sha256)
    monkeypatch.setattr(stability, "EXCLUSIONS_PIN", conf.exclusions_sha256)
    fit.identities(conf)
    conf = conf.model_copy(update={"dataset_dir": conf.dataset_dir.parent / "unfiltered"})
    conf = conf.model_copy(
        update={"dataset_manifest_sha256": smoke.file_sha256(conf.dataset_dir / "manifest.json")}
    )
    monkeypatch.setattr(stability, "DATASET_PIN", conf.dataset_manifest_sha256)
    with pytest.raises(DatasetError, match="filtered dataset"):
        fit.identities(conf)


def metrics(conf, budget):
    return {
        "train": [
            {
                "iteration": i,
                "train_loss": 1.0,
                "trained_tokens": (i * budget["supervised_tokens"]) // conf.iters,
            }
            for i in range(1, conf.iters + 1)
        ],
        "validation": [
            {"iteration": i, "val_loss": 1.0, "rows": 64} for i in fit.validation_steps(conf)
        ],
    }


def fake_trainer(record, mutation=None):
    def train(conf, **kwargs):
        evidence = metrics(conf, record["budget"])
        resource = {"peak_mlx_bytes": 100, "peak_rss_bytes": 1000}
        verification = {key: record[key] for key in ("identity", "selection")}
        if mutation == "nonfinite":
            evidence["train"][0]["train_loss"] = "NaN"
        elif mutation == "validation":
            evidence["validation"][1]["val_loss"] = 1.26
        elif mutation == "final":
            evidence["validation"][-1]["val_loss"] = 1.11
        elif mutation == "tokens":
            evidence["train"][-1]["trained_tokens"] += 1
        elif mutation == "memory":
            resource["peak_mlx_bytes"] = smoke.TRAIN_ALLOCATION_BYTES + 1
        elif mutation == "identity":
            verification["identity"] = {}
        (conf.run_dir / "trainer.log").write_text("synthetic trainer log")
        (conf.run_dir / "worker-verification.json").write_text(json.dumps(verification))
        (conf.run_dir / "metrics.json").write_text(json.dumps(evidence))
        (conf.run_dir / "resources.json").write_text(json.dumps(resource))
        (conf.run_dir / "events.jsonl").write_text(
            "".join(
                json.dumps({"kind": kind, **event}) + "\n"
                for kind, events in evidence.items()
                for event in events
            )
        )
        if mutation == "timeout":
            raise TimeoutError("supervised timeout")
        adapter = conf.run_dir / "adapter"
        adapter.mkdir()
        for name in (
            "adapters.safetensors",
            f"{conf.iters:07d}_adapters.safetensors",
            "adapter_config.json",
        ):
            (adapter / name).write_text("synthetic")
        return resource

    return train


@pytest.mark.parametrize(
    "mutation",
    [None, "nonfinite", "validation", "final", "tokens", "memory", "timeout", "identity"],
)
def test_controller_failure_rules_and_retained_evidence(registered, monkeypatch, mutation):
    record, _ = fit.verify_preregistration(registered)
    monkeypatch.setattr(smoke, "probe_resources", resources)
    monkeypatch.setattr(smoke, "launch_trainer", fake_trainer(copy.deepcopy(record), mutation))
    monkeypatch.setattr(
        fit, "verify_preregistration", lambda *a, **kw: pytest.fail("controller loaded tokenizer")
    )
    assert fit.run(registered, dry_run=False) == int(mutation is not None)
    report = json.loads((registered.run_dir / "report.json").read_bytes())
    assert report["status"] == ("completed" if mutation is None else "failed")
    assert report["metrics"] and report["elapsed_seconds"] >= 0
    assert report["generated_quality"] == "NOT MEASURED"
    assert report["promotion"] == "not authorized"
    if mutation is None:
        assert all(report["criteria"].values())
        assert report["validation_ratios"] == [
            {"iteration": 0, "ratio": 1.0},
            {"iteration": 2, "ratio": 1.0},
        ]
    with pytest.raises(DatasetError, match="already exists"):
        fit.run(registered, dry_run=False)
    other = registered.model_copy(update={"run_dir": registered.run_dir.parent / "second"})
    assert fit.run(other, dry_run=False) == 1
    assert "already exists" in json.loads((other.run_dir / "report.json").read_bytes())["error"]


def test_memory_floor_refuses_before_trainer(registered, monkeypatch):
    monkeypatch.setattr(
        smoke,
        "probe_resources",
        lambda: resources().model_copy(
            update={"available_memory_bytes": smoke.MIN_MEMORY_BYTES - 1}
        ),
    )
    monkeypatch.setattr(smoke, "launch_trainer", lambda *a, **kw: pytest.fail("training launched"))
    assert fit.run(registered, dry_run=False) == 1
    assert (registered.run_dir / "resource-samples.jsonl").exists()


@pytest.mark.parametrize(
    "mutation", [None, "nonfinite", "validation", "final", "tokens", "memory", "timeout"]
)
def test_fit_callbacks_abort_before_continuing(config, trainer_stubs, monkeypatch, mutation):  # noqa: F811
    conf = configuration(config, iters=101)
    conf.run_dir.mkdir()
    budget = {"updates": 101, "examples": 404, "supervised_tokens": 2020}
    control = fit.fit_control(conf, {"permuted": list(range(64))}, rows(64), budget)
    losses = {"value": 1.0}
    control = replace(control, validation=lambda *a: (losses["value"], 64, []))
    checkpoints = []
    monkeypatch.setattr(smoke, "save_checkpoint", lambda model, path: checkpoints.append(path.name))
    model = SimpleNamespace(layers=[object()] * 16, freeze=lambda: None)
    completed_steps = []

    def train(**kw):
        for step in range(1, 102):
            if mutation == "validation" and step == 50:
                losses["value"] = 1.26
            if mutation == "final" and step == 101:
                losses["value"] = 1.11
            if mutation == "timeout":
                monkeypatch.setattr(fit.time, "monotonic", lambda: 1e12)
            info = {
                "iteration": step,
                "train_loss": float("nan") if mutation == "nonfinite" else 1.0,
                "trained_tokens": step * 20 + int(mutation == "tokens" and step == 101),
                "peak_memory": 11.500000001 if mutation == "memory" else 1.0,
            }
            kw["training_callback"].on_train_loss_report(info)
            completed_steps.append(step)

    trainer_stubs.train = train
    args = SimpleNamespace(**smoke.training_options(conf))
    if mutation:
        with pytest.raises((DatasetError, TimeoutError)):
            smoke.fit(
                args, model, [([1] * 10, 5)] * 404, [([1] * 10, 5)] * 64, loss=None, control=control
            )
        assert len(completed_steps) < 101
    else:
        result = smoke.fit(
            args, model, [([1] * 10, 5)] * 404, [([1] * 10, 5)] * 64, loss=None, control=control
        )
        assert [event["iteration"] for event in result["validation"]] == [0, 50, 100, 101]
        assert checkpoints == ["0000100_adapters.safetensors"]
    assert (conf.run_dir / "events.jsonl").exists()


def test_evaluate_and_make_target(registered, tmp_path):
    record, _ = fit.verify_preregistration(registered)
    report = {
        "config": registered.model_dump(mode="json"),
        "budget": record["budget"],
        "metrics": metrics(registered, record["budget"]),
        "resources": {"peak_mlx_bytes": 1, "peak_rss_bytes": 2},
        "elapsed_seconds": 1,
        "status": "completed",
        "verification_status": "verified_in_worker",
        "candidate": {"path": "adapter/adapters.safetensors", "sha256": "a" * 64},
        "artifact_errors": [],
    }
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report))
    out = tmp_path / "evaluation.json"
    assert fit.main(["--evaluate", str(report_path), "--out", str(out)]) == 0
    evaluated = json.loads(out.read_bytes())
    assert evaluated["criteria_pass"] is True
    assert (
        evaluated["promotion"] == "not authorized"
        and evaluated["generated_quality"] == "NOT MEASURED"
    )
    report["status"] = "failed"
    assert fit.evaluate_report(report)["criteria_pass"] is False
    makefile = Path("Makefile").read_text()
    assert "train-fit: ##" in makefile
    assert "normalization_fit.py" in makefile


@pytest.mark.skipif(
    importlib.util.find_spec("mlx") is None,
    reason="the child process records mlx package versions; mlx is Apple-only",
)
def test_controller_import_guard(registered, tmp_path):
    config_path = tmp_path / "controller.json"
    config_path.write_text(registered.model_dump_json())
    script = """
import importlib.abc, sys
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        assert fullname.split('.')[0] not in {'torch', 'transformers', 'mlx'}, fullname
sys.meta_path.insert(0, Guard())
import normalization_fit as fit
import normalization_train as smoke
smoke.probe_resources = lambda: smoke.Resources(free_disk_bytes=30000000000, available_memory_bytes=15000000000, services=())
smoke.launch_trainer = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('injected stop'))
conf = fit.FitConfig.model_validate_json(open(sys.argv[1], 'rb').read())
assert fit.run(conf, dry_run=False) == 1
assert not {'torch', 'transformers', 'mlx'} & sys.modules.keys()
"""
    import os

    result = subprocess.run(
        [sys.executable, "-c", script, str(config_path)],
        env={
            **os.environ,
            "PYTHONPATH": "training:eval:scripts:packages/core/src:packages/ingest/src",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "artifact",
    [
        "events.jsonl",
        "metrics.json",
        "resources.json",
        "trainer.log",
        "adapter/0000002_adapters.safetensors",
    ],
)
def test_missing_worker_evidence_cannot_pass(registered, monkeypatch, artifact):
    record, _ = fit.verify_preregistration(registered)
    trainer = fake_trainer(record)

    def incomplete(conf, **kwargs):
        counters = trainer(conf, **kwargs)
        (conf.run_dir / artifact).unlink()
        return counters

    monkeypatch.setattr(smoke, "probe_resources", resources)
    monkeypatch.setattr(smoke, "launch_trainer", incomplete)
    assert fit.run(registered, dry_run=False) == 1
    report = json.loads((registered.run_dir / "report.json").read_bytes())
    assert report["status"] == "failed" and report["criteria_pass"] is False


def test_partial_corrupt_events_preserve_primary_timeout(registered, monkeypatch):
    record, _ = fit.verify_preregistration(registered)
    trainer = fake_trainer(record)

    def interrupted(conf, **kwargs):
        trainer(conf, **kwargs)
        with (conf.run_dir / "events.jsonl").open("ab") as handle:
            handle.write(b"\xff\n")
        raise TimeoutError("primary deadline")

    monkeypatch.setattr(smoke, "probe_resources", resources)
    monkeypatch.setattr(smoke, "launch_trainer", interrupted)
    assert fit.run(registered, dry_run=False) == 1
    report = json.loads((registered.run_dir / "report.json").read_bytes())
    assert "primary deadline" in report["error"]
    assert report["metrics"]["train"]
    assert any("events.jsonl line" in error for error in report["artifact_errors"])


def test_dry_run_is_separate_exclusive_attempt(registered, monkeypatch):
    dry = fit.FitConfig.model_validate_json(
        (registered.preregistration.parent / "fit-dry.json").read_bytes()
    )
    monkeypatch.setattr(smoke, "probe_resources", resources)
    monkeypatch.setattr(
        smoke, "launch_trainer", lambda *a, **kw: pytest.fail("dry run launched trainer")
    )
    assert fit.run(dry, dry_run=True) == 0
    assert not (dry.preregistration.parent / "attempts/fit-train.json").exists()
    other = dry.model_copy(update={"run_dir": dry.run_dir.parent / "another"})
    assert fit.run(other, dry_run=True) == 1
    assert (dry.preregistration.parent / "attempts/fit-dry.json").is_file()


def test_registration_and_configs_cannot_be_overwritten(registered, tmp_path):
    plan = tmp_path / "second-plan.json"
    template = registered.model_copy(
        update={"preregistration": None, "preregistration_sha256": None}
    )
    plan.write_text(template.model_dump_json())
    with pytest.raises(DatasetError, match="already exists"):
        fit.preregister(plan, registered.preregistration)
    with pytest.raises(DatasetError, match="conflicts"):
        fit.preregister(plan, tmp_path / "fit.json")


def test_event_metrics_disagreement_fails(registered, monkeypatch):
    record, _ = fit.verify_preregistration(registered)
    trainer = fake_trainer(record)

    def inconsistent(conf, **kwargs):
        counters = trainer(conf, **kwargs)
        path = conf.run_dir / "metrics.json"
        saved = json.loads(path.read_bytes())
        saved["train"][0]["train_loss"] = 0.5
        path.write_text(json.dumps(saved))
        return counters

    monkeypatch.setattr(smoke, "probe_resources", resources)
    monkeypatch.setattr(smoke, "launch_trainer", inconsistent)
    assert fit.run(registered, dry_run=False) == 1
    report = json.loads((registered.run_dir / "report.json").read_bytes())
    assert any("events and metrics differ" in error for error in report["artifact_errors"])


def test_internal_worker_requires_controller_attempt(registered, monkeypatch):
    monkeypatch.setattr(
        fit, "open_chat_tokenizer", lambda _: pytest.fail("unowned worker loaded tokenizer")
    )
    with pytest.raises(DatasetError, match="controller"):
        fit.worker(registered)


@pytest.mark.parametrize("drift", [False, True])
def test_worker_uses_frozen_rows_and_rechecks_after_fit(registered, monkeypatch, drift):
    registered.run_dir.mkdir(parents=True)
    (registered.run_dir / "config.json").write_text(registered.model_dump_json())
    (registered.run_dir / "supervisor-started.json").write_text("{}")
    attempts = registered.preregistration.parent / "attempts"
    attempts.mkdir()
    (attempts / "fit-train.json").write_text(
        json.dumps(
            {
                "run_dir": str(registered.run_dir),
                "preregistration_sha256": registered.preregistration_sha256,
            }
        )
    )
    calls = []

    def train(conf, **kwargs):
        calls.append(kwargs)
        assert len(kwargs["selection"][0]) == 8
        assert kwargs["control"].batches is stability.ordered_batches
        if drift:
            monkeypatch.setattr(fit, "implementations", lambda: {"drift": "b" * 64})

    monkeypatch.setattr(smoke, "train_local", train)
    if drift:
        with pytest.raises(DatasetError, match="implementation"):
            fit.worker(registered)
    else:
        fit.worker(registered)
    assert len(calls) == 1
