"""The training boundary refuses unsafe inputs before any MLX import or trainer call."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import normalization_mlx
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

    class TokenizerContext:
        identity = tokenizer_id

        def assert_current(self, _directory):
            return None

        def count(self, _messages):
            return 32

        def encode(self, _messages):
            return list(range(32)), 16

        def release(self):
            return None

    monkeypatch.setattr(smoke, "open_chat_tokenizer", lambda _directory: TokenizerContext())
    monkeypatch.setattr(
        normalization_mlx, "open_chat_tokenizer", lambda _directory: TokenizerContext()
    )
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


def test_bounded_rows_binds_context_to_requested_model_directory(
    config: smoke.SmokeConfig,
) -> None:
    class Context:
        def assert_current(self, directory):
            if directory != config.model_dir:
                raise DatasetError("tokenizer context directory differs")

        def count(self, _messages):
            return 32

    rows, valid = smoke.bounded_rows(config, Context())
    assert rows and valid

    other = config.model_copy(update={"model_dir": config.model_dir.parent})
    with pytest.raises(DatasetError, match="differs"):
        smoke.bounded_rows(other, Context())


def test_bounded_rows_rechecks_tokenizer_snapshot_after_processing(
    config: smoke.SmokeConfig,
) -> None:
    class Context:
        checks = 0

        def assert_current(self, _directory):
            self.checks += 1
            if self.checks == 2:
                raise DatasetError("tokenizer snapshot changed during verification")

        def count(self, _messages):
            return 32

    context = Context()
    with pytest.raises(DatasetError, match="snapshot changed"):
        smoke.bounded_rows(config, context)
    assert context.checks == 2


def test_train_local_releases_only_contexts_it_owns(
    config: smoke.SmokeConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    contexts = []

    class Context:
        released = False

        def release(self):
            self.released = True

    def open_context(_directory):
        context = Context()
        contexts.append(context)
        return context

    def fail_verification(*_args, **_kwargs):
        raise DatasetError("verification failed")

    monkeypatch.setattr(smoke, "open_chat_tokenizer", open_context)
    monkeypatch.setattr(smoke, "verify_inputs", fail_verification)
    monkeypatch.setattr(smoke, "write_new", lambda _files: None)
    with pytest.raises(DatasetError, match="verification failed"):
        smoke.train_local(config)
    assert contexts[0].released

    caller_context = Context()
    with pytest.raises(DatasetError, match="verification failed"):
        smoke.train_local(config, tokenizer_context=caller_context)
    assert not caller_context.released


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


def test_coarse_pressure_estimate_is_sampled_before_72_percent_refusal(tmp_path: Path) -> None:
    physical = 18 * 1024**3
    estimate = physical * 72 // 100
    state = smoke.Resources(
        free_disk_bytes=20_000_000_000,
        available_memory_bytes=estimate,
        services=(),
        physical_memory_bytes=physical,
        memory_pressure_free_percent=72,
        memory_estimate_bytes=estimate,
        memory_method="memory_pressure -Q integer free percentage × hw.memsize",
        memory_granularity_bytes=(physical + 99) // 100,
        memory_granularity_percentage_points=1,
        current_rss_bytes=1000,
        peak_rss_bytes=2000,
    )
    with pytest.raises(DatasetError, match="14 GB available memory"):
        smoke.check_and_record_resources(tmp_path, stage="worker-readiness", probe=lambda: state)
    sample = json.loads((tmp_path / "resource-samples.jsonl").read_text())
    assert sample["memory_pressure_free_percent"] == 72
    assert sample["physical_memory_bytes"] == physical
    assert sample["memory_estimate_bytes"] == estimate
    assert sample["required_memory_bytes"] == smoke.MIN_MEMORY_BYTES
    assert sample["memory_granularity_bytes"] == (physical + 99) // 100
    assert sample["memory_granularity_percentage_points"] == 1
    assert sample["available_memory_basis"] == "estimate_from_whole_percent"
    assert sample["stage"] == "worker-readiness"
    assert sample["pid"] > 0 and sample["ppid"] > 0 and sample["sampled_at"]
    assert sample["current_rss_bytes"] == 1000 and sample["peak_rss_bytes"] == 2000


def test_coarse_pressure_estimate_at_73_percent_passes(tmp_path: Path) -> None:
    physical = 18 * 1024**3
    estimate = physical * 73 // 100
    state = smoke.Resources(
        free_disk_bytes=20_000_000_000,
        available_memory_bytes=estimate,
        services=(),
        physical_memory_bytes=physical,
        memory_pressure_free_percent=73,
        memory_estimate_bytes=estimate,
        memory_method="memory_pressure -Q integer free percentage × hw.memsize",
        memory_granularity_bytes=(physical + 99) // 100,
        current_rss_bytes=1000,
        peak_rss_bytes=2000,
    )
    assert (
        smoke.check_and_record_resources(
            tmp_path, stage="controller-readiness", probe=lambda: state
        )
        == state
    )
    sample = json.loads((tmp_path / "resource-samples.jsonl").read_text())
    assert sample["memory_pressure_free_percent"] == 73


def test_supervised_controller_imports_without_heavy_backends(tmp_path: Path) -> None:
    import os
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[2]
    helper = tmp_path / "child.py"
    helper.write_text(
        "import json, pathlib, sys; "
        "pathlib.Path(sys.argv[sys.argv.index('--config') + 1]).parent.joinpath('resources.json').write_text('{\\\"peak_mlx_bytes\\\":null,\\\"peak_rss_bytes\\\":1}'); "
        "assert not any(name in sys.modules for name in ('torch','transformers','mlx','mlx.core'))"
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    source = (
        "import os,sys; sys.path[:0]=sys.argv[1].split(os.pathsep); "
        "import normalization_stability, normalization_train as smoke; "
        "assert not any(name in sys.modules for name in ('torch','transformers','mlx','mlx.core')); "
        "from pathlib import Path; from types import SimpleNamespace; "
        "result=smoke.launch_trainer(SimpleNamespace(run_dir=Path(sys.argv[2]), timeout_seconds=5), "
        "script=Path(sys.argv[3])); assert result == {'peak_mlx_bytes':None,'peak_rss_bytes':1}"
    )
    paths = os.pathsep.join(
        str(root / suffix)
        for suffix in (
            "training",
            "eval",
            "scripts",
            "packages/core/src",
            "packages/ingest/src",
        )
    )
    subprocess.run(
        [sys.executable, "-c", source, paths, str(run_dir), str(helper)],
        check=True,
        env={**os.environ, "UV_NO_SYNC": "1", "UV_OFFLINE": "1"},
        capture_output=True,
        text=True,
    )


def test_supervised_worker_refusal_sample_survives_to_parent(
    config: smoke.SmokeConfig, tmp_path: Path
) -> None:
    run_dir = config.run_dir
    run_dir.mkdir()
    (run_dir / "config.json").write_text(config.model_dump_json())
    training = str(Path(__file__).resolve().parents[2] / "training")
    helper = tmp_path / "refusing_worker.py"
    helper.write_text(
        "import json, sys\n"
        f"sys.path.insert(0, {training!r})\n"
        "import normalization_train as smoke\n"
        "saved_config = smoke.SmokeConfig.model_validate_json("
        "open(sys.argv[sys.argv.index('--config') + 1], 'rb').read())\n"
        "class Context:\n"
        "    identity = json.loads((saved_config.dataset_dir / 'manifest.json').read_bytes())['tokenizer_id']\n"
        "    def assert_current(self, _directory): pass\n"
        "    def count(self, _messages): return 32\n"
        "    def release(self): pass\n"
        "smoke.open_chat_tokenizer = lambda _directory: Context()\n"
        "smoke.probe_resources = lambda: smoke.Resources(free_disk_bytes=20000000000, "
        "available_memory_bytes=18*1024**3*72//100, services=(), "
        "physical_memory_bytes=18*1024**3, memory_pressure_free_percent=72, "
        "memory_estimate_bytes=18*1024**3*72//100, memory_method='mocked memory_pressure -Q', "
        "memory_granularity_bytes=(18*1024**3+99)//100, "
        "memory_granularity_percentage_points=1, current_rss_bytes=1000, peak_rss_bytes=2000)\n"
        "raise SystemExit(smoke.main(['--config', sys.argv[sys.argv.index('--config')+1], '--worker']))\n"
    )
    with pytest.raises(DatasetError, match="trainer exit 1"):
        smoke.launch_trainer(config, script=helper)
    samples = [
        json.loads(line) for line in (run_dir / "resource-samples.jsonl").read_text().splitlines()
    ]
    assert len(samples) == 1
    assert samples[0]["stage"] == "worker-readiness"
    assert samples[0]["memory_pressure_free_percent"] == 72
    assert samples[0]["required_memory_bytes"] == smoke.MIN_MEMORY_BYTES
    assert not (run_dir / "resources.json").exists()
    assert not (run_dir / "metrics.json").exists()
    # The real child wrote evidence before its readiness refusal, without MLX.
    verification = smoke._read_worker_verification(run_dir / "worker-verification.json", config)
    assert verification == worker_metadata(config)
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


def test_non_dry_controller_does_not_construct_tokenizer_for_injected_trainer(
    config: smoke.SmokeConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        smoke,
        "open_chat_tokenizer",
        lambda _directory: pytest.fail("controller constructed tokenizer outside dry-run"),
    )

    def fail(_config: smoke.SmokeConfig) -> dict:
        raise TimeoutError("stop after controller handoff")

    assert smoke.run(config, dry_run=False, probe=resources, trainer=fail) == 1


@pytest.mark.skipif(
    importlib.util.find_spec("mlx") is None,
    reason="the child process records mlx package versions; mlx is Apple-only",
)
def test_non_dry_controller_imports_no_heavy_training_modules(
    config: smoke.SmokeConfig, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(config.model_dump_json())
    training = str(Path(__file__).resolve().parents[2] / "training")
    root = Path(__file__).resolve().parents[2]
    evaluation = str(root / "eval")
    scripts = str(root / "scripts")
    script = (
        "import sys; sys.path[:0] = sys.argv[1:4]; "
        "import normalization_train as smoke; "
        "config = smoke.SmokeConfig.model_validate_json(open(sys.argv[4], 'rb').read()); "
        "resources = smoke.Resources(free_disk_bytes=30000000000, "
        "available_memory_bytes=15000000000, services=()); "
        "smoke._read_worker_verification = lambda path, config: "
        "{'identity': {}, 'selection': {}}; "
        "status = smoke.run(config, dry_run=False, probe=lambda: resources, "
        "trainer=lambda config: {}, verify_in_controller=False); "
        "assert status == 0; "
        "assert not {'torch', 'transformers', 'mlx', 'mlx.core'} & sys.modules.keys()"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, training, evaluation, scripts, str(config_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


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

    class Child:
        waits = 0

        def wait(self, timeout):
            self.waits += 1
            if self.waits == 1:
                assert timeout == config.timeout_seconds
                raise subprocess.TimeoutExpired("worker", timeout)
            assert timeout == 5
            return -15

        def terminate(self):
            return None

        def kill(self):
            return None

    def popen(command, **kwargs):
        assert command[-1] == "--worker"
        assert kwargs["env"]["HF_HUB_OFFLINE"] == "1"
        return Child()

    monkeypatch.setattr(smoke.subprocess, "Popen", popen)
    assert smoke.run(config, dry_run=False, probe=resources) == 1
    report = json.loads((config.run_dir / "report.json").read_text())
    assert "TimeoutExpired" in report["error"]
    assert report["resources"]["peak_mlx_bytes"] is None


def test_supervised_child_cancel_is_forwarded_and_reaped(tmp_path, monkeypatch):
    import signal
    from types import SimpleNamespace

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    events = []

    class Child:
        def wait(self, timeout=None):
            if timeout != 5:
                handler = signal.getsignal(signal.SIGTERM)
                handler(signal.SIGTERM, None)
            events.append("reaped")
            return -signal.SIGTERM

        def send_signal(self, signum):
            events.append(("forwarded", signum))

        def terminate(self):
            events.append("terminated")

        def kill(self):
            events.append("killed")

    monkeypatch.setattr(smoke.subprocess, "Popen", lambda *_args, **_kwargs: Child())
    with pytest.raises(InterruptedError, match="signal"):
        smoke.launch_trainer(
            SimpleNamespace(run_dir=run_dir, timeout_seconds=30), script=tmp_path / "worker.py"
        )
    assert events == [("forwarded", signal.SIGTERM), "terminated", "reaped"]


def test_supervised_child_repeat_cancel_during_cleanup_is_reaped(tmp_path, monkeypatch):
    import signal
    from types import SimpleNamespace

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    events = []

    class Child:
        waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            elif timeout == 5:
                assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN
            events.append("reaped")
            return -signal.SIGTERM

        def send_signal(self, signum):
            events.append(("forwarded", signum))

        def terminate(self):
            events.append("terminated")

        def kill(self):
            events.append("killed")

    monkeypatch.setattr(smoke.subprocess, "Popen", lambda *_args, **_kwargs: Child())
    with pytest.raises(InterruptedError, match="signal"):
        smoke.launch_trainer(
            SimpleNamespace(run_dir=run_dir, timeout_seconds=30), script=tmp_path / "worker.py"
        )
    assert events[-1] == "reaped"


def test_supervised_child_error_and_missing_artifacts_fail_loudly(tmp_path, monkeypatch):
    from functools import partial
    from types import SimpleNamespace

    def return_child(child_obj, *_args, **_kwargs):
        return child_obj

    for return_code, missing, expected in (
        (9, False, "trainer exit 9"),
        (0, True, "resources.json"),
    ):
        run_dir = tmp_path / f"run-{return_code}-{missing}"
        run_dir.mkdir()

        class Child:
            def __init__(self, child_return_code):
                self.return_code = child_return_code

            def wait(self, timeout):
                return self.return_code

            def terminate(self):
                pytest.fail("an exited child must not be terminated")

            def kill(self):
                pytest.fail("an exited child must not be killed")

        child = Child(return_code)
        monkeypatch.setattr(smoke.subprocess, "Popen", partial(return_child, child))
        if not missing:
            (run_dir / "resources.json").write_text(
                json.dumps({"peak_mlx_bytes": None, "peak_rss_bytes": 1})
            )
        with pytest.raises((DatasetError, FileNotFoundError), match=expected):
            smoke.launch_trainer(
                SimpleNamespace(run_dir=run_dir, timeout_seconds=30),
                script=tmp_path / "worker.py",
            )


@pytest.mark.parametrize("write_verification", [True, False])
def test_supervised_smoke_collects_worker_identity_and_selection(
    config: smoke.SmokeConfig, monkeypatch: pytest.MonkeyPatch, write_verification: bool
) -> None:
    command: list[str] = []
    metadata = worker_metadata(config)
    monkeypatch.setattr(smoke.platform, "platform", lambda: "synthetic")

    class Child:
        def wait(self, timeout):
            run_dir = Path(command[command.index("--config") + 1]).parent
            if write_verification:
                (run_dir / "worker-verification.json").write_text(json.dumps(metadata))
            (run_dir / "resources.json").write_text(
                json.dumps({"peak_mlx_bytes": 123, "peak_rss_bytes": 456})
            )
            return 0

    def popen(args, **_kwargs):
        command.extend(args)
        return Child()

    monkeypatch.setattr(smoke.subprocess, "Popen", popen)
    monkeypatch.setattr(smoke, "probe_resources", resources)
    assert smoke.run(config, dry_run=False, probe=resources) == int(not write_verification)
    report = json.loads((config.run_dir / "report.json").read_text())
    if write_verification:
        assert report["status"] == "smoke_completed"
        assert report["identity"] == metadata["identity"]
        assert report["selection"] == metadata["selection"]
    else:
        assert report["status"] == "failed"
        assert "worker verification" in report["error"]


def test_timeout_with_malformed_resource_artifact_still_writes_report(
    config: smoke.SmokeConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_with_partial_artifacts(_config):
        (config.run_dir / "resources.json").write_text("{")
        (config.run_dir / "resource-samples.jsonl").write_text(
            json.dumps(
                {
                    "stage": "worker-readiness",
                    "pid": 10,
                    "ppid": 1,
                    "sampled_at": "now",
                    "required_memory_bytes": 14,
                }
            )
            + "\n[]"
        )
        raise TimeoutError("worker deadline")

    monkeypatch.setattr(smoke, "probe_resources", resources)
    assert (
        smoke.run(config, dry_run=False, probe=resources, trainer=fail_with_partial_artifacts) == 1
    )
    report = json.loads((config.run_dir / "report.json").read_text())
    assert report["status"] == "failed"
    assert "worker deadline" in report["error"]
    assert len(report["resource_samples"]) == 1
    assert any("resources.json" in error for error in report["artifact_errors"])
    assert any("line 2" in error for error in report["artifact_errors"])


def test_supervised_start_marker_is_exclusive(tmp_path, monkeypatch):
    from types import SimpleNamespace

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "resources.json").write_text(
        json.dumps({"peak_mlx_bytes": None, "peak_rss_bytes": 1})
    )
    launches = []

    class Child:
        def wait(self, timeout):
            return 0

    def popen(*_args, **_kwargs):
        launches.append(1)
        return Child()

    monkeypatch.setattr(smoke.subprocess, "Popen", popen)
    config = SimpleNamespace(run_dir=run_dir, timeout_seconds=30)
    assert smoke.launch_trainer(config, script=tmp_path / "worker.py")["peak_rss_bytes"] == 1
    with pytest.raises(DatasetError, match="already exists"):
        smoke.launch_trainer(config, script=tmp_path / "worker.py")
    assert launches == [1]


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
def test_real_cpu_fit_loss_forwarding(cpu_mlx, tmp_path, monkeypatch, arm, config):
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
    import mlx.optimizers as optim

    actual_optimizer = optim.AdamW
    optimizer_calls = []

    def constructed(**kwargs):
        optimizer_calls.append(kwargs)
        return actual_optimizer(**kwargs)

    monkeypatch.setattr(optim, "AdamW", constructed)
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
        optimizer_config=smoke.training_options(config)["optimizer_config"],
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
    assert optimizer_calls == [
        {"learning_rate": 1e-4, **smoke.training_options(config)["optimizer_config"]["adamw"]}
    ]
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


def test_explicit_adamw_contract(config):
    assert smoke.training_options(config)["optimizer_config"]["adamw"] == {
        "bias_correction": False,
        "betas": [0.9, 0.999],
        "eps": 1e-8,
        "weight_decay": 0.01,
    }


def test_real_adamw_default_explicit_cpu_equivalence(cpu_mlx, config):
    import mlx.optimizers as optim
    import numpy as np

    mx = cpu_mlx
    implicit = optim.AdamW(learning_rate=1e-4)
    explicit = optim.AdamW(
        learning_rate=1e-4, **smoke.training_options(config)["optimizer_config"]["adamw"]
    )
    a = {"weight": mx.array([0.5, -1.25, 0.0], dtype=mx.float32)}
    b = {"weight": mx.array([0.5, -1.25, 0.0], dtype=mx.float32)}
    for gradient in ([0.25, -0.75, 0.0], [-0.1, 0.2, 0.01], [0.0, 0.0, -0.2]):
        grads = {"weight": mx.array(gradient, dtype=mx.float32)}
        a = implicit.apply_gradients(grads, a)
        b = explicit.apply_gradients(grads, b)
        mx.eval(a, b, implicit.state, explicit.state)
        np.testing.assert_allclose(
            np.asarray(a["weight"]), np.asarray(b["weight"]), rtol=0, atol=1e-7
        )
    assert implicit.step.item() == explicit.step.item() == 3


def worker_metadata(config):
    fit, valid = smoke.bounded_rows(config)
    return {
        "identity": smoke.verify_inputs(config),
        "selection": {
            "policy": "first 50 fit rows; first validation row",
            "fit_rows": len(fit),
            "validation_rows": len(valid),
            "fit_sha256": smoke.digest(fit),
            "validation_sha256": smoke.digest(valid),
        },
    }


@pytest.mark.parametrize("bad_line", [b"\xff", b"{truncated", b"[]"])
@pytest.mark.parametrize("bad_first", [True, False])
def test_timeout_preserves_binary_telemetry_and_primary_error(
    config, monkeypatch, bad_line, bad_first
):
    good = json.dumps(
        {
            "stage": "worker-readiness",
            "pid": 10,
            "ppid": 1,
            "sampled_at": "now",
            "required_memory_bytes": 14,
        }
    ).encode()

    def fail(_config, **kwargs):
        parts = [bad_line, good] if bad_first else [good, bad_line]
        (config.run_dir / "resource-samples.jsonl").write_bytes(b"\n".join(parts))
        (config.run_dir / "resources.json").write_bytes(b"\xff")
        (config.run_dir / "worker-verification.json").write_bytes(b"\xff")
        raise TimeoutError("primary worker timeout")

    monkeypatch.setattr(smoke, "launch_trainer", fail)
    assert smoke.run(config, dry_run=False, probe=resources, trainer=fail) == 1
    report = json.loads((config.run_dir / "report.json").read_bytes())
    assert report["status"] == "failed"
    assert "primary worker timeout" in report["error"]
    assert report["resource_samples"] == [json.loads(good)]
    assert any(f"line {1 if bad_first else 2}" in e for e in report["artifact_errors"])
    assert any("resources.json" in e for e in report["artifact_errors"])


@pytest.mark.parametrize(
    "mutation", ["empty", "garbage", "missing", "nested", "bool_count", "bad_hash"]
)
def test_supervised_smoke_rejects_partial_nested_verification(config, monkeypatch, mutation):
    metadata = worker_metadata(config)
    if mutation == "empty":
        metadata["identity"] = {}
    elif mutation == "garbage":
        metadata["identity"] = {"garbage": True}
    elif mutation == "missing":
        metadata["identity"].pop("model_revision")
    elif mutation == "nested":
        metadata["identity"]["dataset_manifest"]["dataset"] = []
    elif mutation == "bool_count":
        metadata["selection"]["fit_rows"] = True
    else:
        metadata["selection"]["fit_sha256"] = "z" * 64

    def launch(_config, **kwargs):
        (config.run_dir / "worker-verification.json").write_text(json.dumps(metadata))
        return {"peak_mlx_bytes": 1, "peak_rss_bytes": 2}

    monkeypatch.setattr(smoke, "launch_trainer", launch)
    assert smoke.run(config, dry_run=False, probe=resources, trainer=launch) == 1
    report = json.loads((config.run_dir / "report.json").read_bytes())
    assert report["status"] == "failed"
    assert report.get("verification_status") != "verified_in_worker"
    assert any("worker-verification.json" in e for e in report["artifact_errors"])
