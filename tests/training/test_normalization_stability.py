"""Synthetic evidence for fixed, preregistered optimization diagnostics; no MLX required."""

import copy
import json
import math
from pathlib import Path

import normalization_stability as stability
import normalization_train as smoke
import pytest
from dataset_contracts import DatasetError
from pydantic import ValidationError
from test_normalization_train import config, cpu_mlx, local_model  # noqa: F401


def rows(n):
    return [
        {
            "messages": [
                {"role": "user", "content": str(i)},
                {
                    "role": "assistant",
                    "content": json.dumps({"canonical_id": None if i % 2 else "revenue"}),
                },
            ]
        }
        for i in range(n)
    ]


def encode(row):
    return list(range(10)), 5


def test_even_stride_covers_entire_file_and_permutation():
    sample = stability.select_rows(rows(1000), 240, encode, 2048)
    assert sample["candidates"] == [k * 999 // 239 for k in range(240)]
    assert sample["selected"] == sample["candidates"]
    assert sample["selected"][0] == 0 and sample["selected"][-1] == 999
    assert len(set(sample["permuted"])) == 240
    assert sample == stability.select_rows(rows(1000), 240, encode, 2048)
    assert sample["stats"]["null_share"] == sample["stats"]["class_counts"]["null"] / 240


def test_skip_over_cap_and_collision_without_duplicates():
    def tokens(row):
        index = int(row["messages"][0]["content"])
        return list(range(2050 if index in (0, 1, 4) else 10)), 5

    sample = stability.select_rows(rows(10), 4, tokens, 2048)
    assert sample["candidates"] == [0, 3, 6, 9]
    assert sample["selected"] == [2, 3, 6, 9]
    assert sample["skipped_over_cap"] == [0, 1]
    with pytest.raises(DatasetError, match="exhausted"):
        stability.select_rows(rows(10), 9, tokens, 2048)
    with pytest.raises(DatasetError, match="exhausted"):
        stability.select_rows(rows(10), 4, lambda r: (list(range(2050)), 5), 2048)
    with pytest.raises(DatasetError, match="at least"):
        stability.select_rows(rows(3), 4, encode, 2048)


def test_encoder_binds_context_to_requested_model_directory(tmp_path):
    requested = tmp_path / "requested"
    other = tmp_path / "other"

    class Context:
        def assert_current(self, directory):
            if directory != requested:
                raise DatasetError("tokenizer context directory differs")

        def encode(self, messages):
            return [1, 2], 1

    with pytest.raises(DatasetError, match="differs"):
        stability.encoder(other, Context())


def test_supervision_refused_instead_of_skipped():
    with pytest.raises(DatasetError, match="masks all targets"):
        stability.select_rows(rows(10), 4, lambda r: ([1, 2], 2), 2048)


def diagnostic(base_config, arm="A", **updates):
    return stability.DiagnosticConfig.model_validate(
        {
            **base_config.model_dump(exclude={"iters", "batch_size", "timeout_seconds"}),
            "exclusions_sha256": smoke.file_sha256(base_config.exclusions),
            "arm": arm,
            "preregistration": base_config.run_dir.parent / "preregistration.json",
            "preregistration_sha256": "a" * 64,
            **updates,
        }
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("iters", 59),
        ("iters", 61),
        ("seed", 18),
        ("arm", "E"),
        ("learning_rate", 0.1),
        ("batch_size", 2),
        ("timeout_seconds", 1501),
        ("max_seq_length", 1024),
        ("num_layers", 8),
        ("grad_checkpoint", False),
    ],
)
def test_fixed_configs_refuse_overrides(config, field, value):  # noqa: F811
    with pytest.raises(ValidationError):
        diagnostic(config, **{field: value})


def test_fixed_config_options_and_smoke_bounds(config):  # noqa: F811
    for arm, (rate, batch) in stability.ARMS.items():
        options = smoke.training_options(diagnostic(config, arm))
        assert (options["learning_rate"], options["batch_size"], options["iters"]) == (
            rate,
            batch,
            60,
        )
        assert options["resume_adapter_file"] is None and options["lr_schedule"] is None
    for field, value in [("learning_rate", 2e-5), ("batch_size", 2), ("seed", 18)]:
        with pytest.raises(ValidationError):
            smoke.SmokeConfig.model_validate({**config.model_dump(), field: value})


def evidence():
    identity = {"pins": "same", "samples": "same"}
    return [
        {
            "status": "diagnostic_completed",
            "arm": arm,
            "preregistration_sha256": "a" * 64,
            "identity": identity,
            "selection": {"fit": "same", "valid": "same"},
            "metrics": {
                "train": [
                    {"iteration": i, "train_loss": 10.0 if arm == "A" else 4.0}
                    for i in range(1, 61)
                ],
                "validation": [
                    {
                        "iteration": i,
                        "val_loss": (1.4 if arm == "A" else 1.05) if i else 1.0,
                        "rows": 64,
                    }
                    for i in (0, 15, 30, 45, 60)
                ],
            },
            "resources": {"peak_mlx_bytes": 100, "peak_rss_bytes": 1000},
            "elapsed_seconds": 1.0,
        }
        for arm in stability.ARMS
    ]


def test_evaluator_literal_baseline_rule_and_ratios():
    result = stability.evaluate_reports(evidence())
    assert result["validity"] == "conclusive on this sample"
    assert result["arms"]["A"]["outcome"] == "not stable on this sample"
    assert result["arms"]["B"]["outcome"] == "stable on this sample"
    assert result["arms"]["B"]["train_max_ratio_to_A"] == 0.4
    assert result["arms"]["B"]["validation_final_ratio"] == 1.05


def test_baseline_without_validation_instability_is_inconclusive():
    reports = evidence()
    for entry in reports[0]["metrics"]["validation"]:
        entry["val_loss"] = 1.0
    result = stability.evaluate_reports(reports)
    assert result["validity"] == "INCONCLUSIVE about LR/batch"
    assert result["winners"] == []
    assert result["arms"]["A"]["outcome"] == "not stable on this sample"


@pytest.mark.parametrize("mutation", ["identity", "sample", "prereg", "duplicate"])
def test_incomplete_or_mismatched_evidence_fails_loudly(mutation):
    reports = copy.deepcopy(evidence())
    if mutation == "nonfinite":
        reports[1]["metrics"]["train"][0]["train_loss"] = float("nan")
    elif mutation == "incomplete":
        reports[1]["metrics"]["validation"].pop()
    elif mutation == "identity":
        reports[1]["identity"] = {"pins": "changed"}
    elif mutation == "sample":
        reports[1]["selection"] = {}
    elif mutation == "prereg":
        reports[1]["preregistration_sha256"] = "b" * 64
    elif mutation == "failed":
        reports[1]["status"] = "failed"
    elif mutation == "missing":
        reports.pop()
    elif mutation == "duplicate":
        reports[1]["arm"] = "A"
    elif mutation == "zero":
        reports[1]["metrics"]["validation"][0]["val_loss"] = 0.0
    elif mutation == "rows":
        reports[1]["metrics"]["validation"][0]["rows"] = 1
    else:
        reports[1]["resources"]["peak_mlx_bytes"] = None
    with pytest.raises(DatasetError):
        stability.evaluate_reports(reports)


def test_instability_at_intermediate_step_or_train_max():
    reports = evidence()
    reports[1]["metrics"]["validation"][1]["val_loss"] = 1.26
    reports[2]["metrics"]["train"][59]["train_loss"] = 5.01
    result = stability.evaluate_reports(reports)
    assert result["arms"]["B"]["outcome"] == "not stable on this sample"
    assert result["arms"]["C"]["outcome"] == "not stable on this sample"


def test_failed_dry_attempt_consumes_directory(config, monkeypatch):  # noqa: F811
    conf = diagnostic(config)
    monkeypatch.setattr(
        stability,
        "verify_preregistration",
        lambda c, **kwargs: (_ for _ in ()).throw(DatasetError("changed pin")),
    )
    assert stability.run(conf, dry_run=True) == 1
    assert json.loads((conf.run_dir / "report.json").read_text())["status"] == "failed"
    with pytest.raises(DatasetError, match="already exists"):
        stability.run(conf, dry_run=True)


def test_training_controller_defers_full_verification_to_supervised_worker(
    config,  # noqa: F811
    monkeypatch,
):
    conf = diagnostic(config)
    monkeypatch.setattr(stability, "version", lambda _: "0.31.3")
    monkeypatch.setattr(
        smoke,
        "probe_resources",
        lambda: smoke.Resources(
            free_disk_bytes=20_000_000_000,
            available_memory_bytes=15_000_000_000,
            services=(),
        ),
    )
    monkeypatch.setattr(
        stability,
        "verify_preregistration",
        lambda *args, **kwargs: pytest.fail("controller performed full verification"),
    )
    monkeypatch.setattr(
        smoke,
        "launch_trainer",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("stop before worker")),
    )
    assert stability.run(conf, dry_run=False) == 1
    report = json.loads((conf.run_dir / "report.json").read_text())
    assert report["verification_status"] == "worker_verification_not_completed"
    assert "identity" not in report and "selection" not in report
    assert report["resource_samples"][0]["stage"] == "controller-readiness"


def test_timeout_and_malformed_worker_artifacts_still_finalize_report(config, monkeypatch):  # noqa: F811
    conf = diagnostic(config)
    monkeypatch.setattr(stability, "version", lambda _: "0.31.3")
    monkeypatch.setattr(
        smoke,
        "probe_resources",
        lambda: smoke.Resources(
            free_disk_bytes=20_000_000_000,
            available_memory_bytes=15_000_000_000,
            services=(),
        ),
    )

    def fail_after_partial_artifacts(_config, **_kwargs):
        (conf.run_dir / "worker-verification.json").write_text('{"identity":')
        (conf.run_dir / "resource-samples.jsonl").write_text(
            json.dumps(
                {
                    "stage": "worker-readiness",
                    "pid": 10,
                    "ppid": 1,
                    "sampled_at": "now",
                    "required_memory_bytes": 14,
                }
            )
            + "\n{truncated"
        )
        raise TimeoutError("supervised deadline")

    monkeypatch.setattr(smoke, "launch_trainer", fail_after_partial_artifacts)
    assert stability.run(conf, dry_run=False) == 1
    report = json.loads((conf.run_dir / "report.json").read_text())
    assert report["status"] == "failed"
    assert "supervised deadline" in report["error"]
    assert len(report["resource_samples"]) == 1
    assert any("worker-verification.json" in error for error in report["artifact_errors"])
    assert any("line 2" in error for error in report["artifact_errors"])


def test_immutable_preregistration_hash_checked_before_data(config):  # noqa: F811
    conf = diagnostic(config)
    conf.preregistration.write_text("{}")
    with pytest.raises(DatasetError, match=r"preregistration.*sha256"):
        stability.verify_preregistration(conf)


def test_result_table_has_individual_criteria_and_partial_evidence():
    reports = evidence()
    result = stability.evaluate_reports(reports)
    assert result["arms"]["B"]["criteria"] == {
        "no_nonfinite": True,
        "final_validation": True,
        "every_validation": True,
        "train_max_vs_A": True,
    }
    reports[1]["status"] = "failed"
    reports[1]["metrics"]["train"][10]["train_loss"] = "NaN"
    reports[1]["metrics"]["validation"].pop()
    reports.pop()
    result = stability.evaluate_reports(reports)
    assert result["arms"]["B"]["criteria"]["no_nonfinite"] is False
    assert result["arms"]["B"]["criteria"]["final_validation"] == "NOT MEASURED"
    assert result["arms"]["B"]["outcome"] == "NOT MEASURED"
    assert result["arms"]["D"]["criteria"]["no_nonfinite"] == "NOT MEASURED"
    assert result["arms"]["D"]["outcome"] == "NOT MEASURED"
    json.dumps(result, allow_nan=False)


def test_missing_baseline_cannot_produce_winner():
    result = stability.evaluate_reports(evidence()[1:])
    assert result["validity"] == "NOT MEASURED: baseline validation rules"
    assert result["winners"] == []
    assert result["arms"]["B"]["criteria"]["train_max_vs_A"] == "NOT MEASURED"


@pytest.mark.parametrize("kind", ["failed", "resource", "identity", "metrics"])
@pytest.mark.parametrize("baseline_passes_validation", [False, True])
def test_unusable_baseline_cannot_supply_comparison_or_validity(kind, baseline_passes_validation):
    reports = evidence()
    baseline = reports[0]
    if baseline_passes_validation:
        for entry in baseline["metrics"]["validation"]:
            entry["val_loss"] = 1.0
    if kind == "failed":
        baseline["status"] = "failed"
    elif kind == "resource":
        baseline["resources"]["peak_mlx_bytes"] = None
    elif kind == "identity":
        baseline.pop("identity")
    else:
        baseline["metrics"]["train"].pop()

    result = stability.evaluate_reports(reports)

    assert result["arms"]["A"]["outcome"] == "NOT MEASURED"
    assert result["arms"]["B"]["criteria"]["train_max_vs_A"] == "NOT MEASURED"
    assert result["arms"]["B"]["train_max_ratio_to_A"] == "NOT MEASURED"
    assert result["validity"] == "NOT MEASURED: baseline validation rules"
    assert result["winners"] == []


@pytest.mark.parametrize("kind", ["nonfinite", "incomplete", "failed", "zero", "rows", "resource"])
def test_unusable_evidence_is_reported_without_passing(kind):
    reports = evidence()
    report = reports[1]
    if kind == "nonfinite":
        report["metrics"]["train"][0]["train_loss"] = float("nan")
    elif kind == "incomplete":
        report["metrics"]["validation"].pop()
    elif kind == "failed":
        report["status"] = "failed"
    elif kind == "zero":
        report["metrics"]["validation"][0]["val_loss"] = 0
    elif kind == "rows":
        report["metrics"]["validation"][0]["rows"] = 1
    else:
        report["resources"]["peak_mlx_bytes"] = None
    result = stability.evaluate_reports(reports)
    assert result["arms"]["B"]["outcome"] == "NOT MEASURED"
    assert result["arms"]["B"]["evidence_errors"]


def test_missing_and_preflight_failed_arms_are_not_measured():
    reports = evidence()
    reports.pop()
    reports[1] = {"arm": "B", "status": "failed", "error": "preflight refused"}

    result = stability.evaluate_reports(reports)

    assert result["arms"]["B"]["outcome"] == "NOT MEASURED"
    assert result["arms"]["D"]["outcome"] == "NOT MEASURED"


def test_row_file_only_decodes_selected_candidates(tmp_path, monkeypatch):
    path = tmp_path / "train.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows(1000)))
    decoded = []
    original = stability.json.loads

    def loads(value):
        decoded.append(1)
        return original(value)

    monkeypatch.setattr(stability.json, "loads", loads)
    source = stability.read_rows(path)
    assert not decoded
    sample = stability.select_rows(source, 4, encode, 2048)
    assert sample["selected"] == [0, 333, 666, 999]
    assert len(decoded) < 30


@pytest.fixture
def trainer_stubs(monkeypatch):
    import sys
    from types import ModuleType, SimpleNamespace

    import numpy as np

    modules = {
        name: ModuleType(name)
        for name in (
            "mlx",
            "mlx.core",
            "mlx.optimizers",
            "mlx_lm.tuner.datasets",
            "mlx_lm.tuner.trainer",
            "mlx_lm.tuner.callbacks",
            "mlx_lm.tuner.utils",
            "mlx_lm.utils",
        )
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    mx = modules["mlx.core"]
    mx.random = SimpleNamespace(seed=lambda _: None)
    mx.array = np.array
    modules["mlx"].core = mx
    modules["mlx"].optimizers = modules["mlx.optimizers"]
    modules["mlx.optimizers"].AdamW = lambda **kw: object()

    class Cache:
        def __init__(self, data):
            self.data = data

        def __len__(self):
            return len(self.data)

        def __getitem__(self, i):
            return self.data[i]

    modules["mlx_lm.tuner.datasets"].CacheDataset = Cache
    modules["mlx_lm.tuner.callbacks"].TrainingCallback = object
    modules["mlx_lm.tuner.utils"].linear_to_lora_layers = lambda *a, **k: None
    modules["mlx_lm.utils"].save_config = lambda *a: None
    trainer = modules["mlx_lm.tuner.trainer"]
    trainer.TrainingArgs = SimpleNamespace
    trainer.train = lambda **kw: pytest.fail("unexpected trainer call")
    return trainer


def test_ordered_batches_never_sort_or_repermute(trainer_stubs):
    pairs = [([i] * (10 - i % 6), 2) for i in range(240)]
    for batch_size in (1, 4):
        batches = list(stability.ordered_batches(pairs, batch_size, 2048))
        assert len(batches) == 240 // batch_size
        assert [int(row[0]) for batch, bounds in batches for row in batch] == list(range(240))
        assert sum(len(bounds) for batch, bounds in batches) == 240


@pytest.mark.parametrize("nonfinite", [False, True])
def test_true_initial_and_after_update_validation_all64(
    config,  # noqa: F811
    trainer_stubs,
    monkeypatch,
    nonfinite,
):
    from types import SimpleNamespace

    conf = diagnostic(config)
    conf.run_dir.mkdir()
    model = SimpleNamespace(
        updates=0,
        layers=[object()] * 16,
        freeze=lambda: None,
        eval=lambda: None,
        train=lambda: None,
    )
    counts = []

    def loss(model, batch, bounds):
        counts.append(model.updates)
        return SimpleNamespace(item=lambda: float("nan") if nonfinite else 1.0), SimpleNamespace(
            item=lambda: 5
        )

    monkeypatch.setattr(smoke, "exclude_padding_loss", loss)

    def batches(dataset, _batch_size, _max_seq_length, loop=False, seed=None, comm_group=None):
        yield from ((None, None) for _ in range(len(dataset)))

    monkeypatch.setattr(stability, "ordered_batches", batches)

    def train(**kw):
        assert kw["val_dataset"] is None
        assert kw["iterate_batches"] is stability.ordered_batches
        for step in range(1, 61):
            model.updates = step
            kw["training_callback"].on_train_loss_report({"iteration": step, "train_loss": 2.0})

    trainer_stubs.train = train
    pairs = [([1] * 10, 5)]
    if nonfinite:
        with pytest.raises(DatasetError, match="nonfinite"):
            smoke.fit(
                SimpleNamespace(**smoke.training_options(conf)),
                model,
                pairs * 240,
                pairs * 64,
                loss=loss,
                control=stability.fit_control(conf, {"permuted": list(range(64))}, rows(64)),
            )
        events = [
            json.loads(line) for line in (conf.run_dir / "events.jsonl").read_text().splitlines()
        ]
        assert events[0]["iteration"] == 0 and events[0]["nonfinite"] is True
        assert events[0]["rows"] == 1
        assert len(events[0]["row_diagnostics"]) == 1
        assert "NaN" in json.dumps(events)
    else:
        result = smoke.fit(
            SimpleNamespace(**smoke.training_options(conf)),
            model,
            pairs * 240,
            pairs * 64,
            loss=loss,
            control=stability.fit_control(conf, {"permuted": list(range(64))}, rows(64)),
        )
        assert counts == [s for s in (0, 15, 30, 45, 60) for _ in range(64)]
        assert [r["iteration"] for r in result["validation"]] == [0, 15, 30, 45, 60]
        assert all(
            row["iteration"] == step
            for step, event in zip((0, 15, 30, 45, 60), result["validation"], strict=True)
            for row in event["row_diagnostics"]
        )


def test_validation_row_diagnostics_preserve_unweighted_mean_and_provenance(monkeypatch):
    from types import SimpleNamespace

    raw = rows(stability.VALID_ROWS)
    provenance = [
        {
            "row_id": f"{i}:{stability.digest(row)}",
            "canonical_id": json.loads(row["messages"][-1]["content"])["canonical_id"],
        }
        for i, row in enumerate(raw)
    ]
    calls = []
    model = SimpleNamespace(eval=lambda: calls.append("eval"), train=lambda: calls.append("train"))

    def loss(_model, batch, bounds):
        index = len(loss.values)
        loss.values.append(index)
        return SimpleNamespace(item=lambda: float(index + 1)), SimpleNamespace(
            item=lambda: 2 if index == 0 else 5
        )

    loss.values = []
    monkeypatch.setattr(smoke, "exclude_padding_loss", loss)

    def batches(dataset, _batch_size, _max_seq_length, loop=False, seed=None, comm_group=None):
        yield from ((None, None) for _ in range(len(dataset)))

    monkeypatch.setattr(stability, "ordered_batches", batches)
    dataset = [([1] * (7 if i == 0 else 10), 5) for i in range(64)]
    baseline_mean, baseline_rows = stability.validation_mean(model, dataset)
    baseline_modes = list(calls)
    calls.clear()
    loss.values.clear()
    mean, count, row_diagnostics = stability.validation_report(model, dataset, provenance)
    assert mean == pytest.approx(sum(range(1, 65)) / 64)
    assert mean == baseline_mean and count == baseline_rows
    assert calls == baseline_modes == ["eval", "train"]
    assert count == 64
    assert row_diagnostics[0] == {
        "row_id": provenance[0]["row_id"],
        "loss": 1.0,
        "supervised_tokens": 2,
        "canonical_id": "revenue",
    }
    assert row_diagnostics[-1]["row_id"] == provenance[-1]["row_id"]
    assert row_diagnostics[-1]["canonical_id"] is None
    assert mean != pytest.approx((1 * 2 + sum(range(2, 65)) * 5) / (2 + 63 * 5))


def test_validation_diagnostics_reject_duplicate_or_incomplete_metadata(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        stability,
        "ordered_batches",
        lambda dataset, *_args, **_kwargs: iter([(None, None)] * len(dataset)),
    )
    monkeypatch.setattr(
        smoke,
        "exclude_padding_loss",
        lambda *_args: (
            SimpleNamespace(item=lambda: 1.0),
            SimpleNamespace(item=lambda: 5),
        ),
    )
    dataset = [([1] * 10, 5)] * 64
    provenance = [{"row_id": str(i), "canonical_id": None} for i in range(64)]
    provenance[-1]["row_id"] = provenance[0]["row_id"]
    model = SimpleNamespace(eval=lambda: None, train=lambda: None)
    with pytest.raises(DatasetError, match="unique"):
        stability.validation_report(model, dataset, provenance)
    provenance[-1]["row_id"] = "last"
    del provenance[-1]["canonical_id"]
    with pytest.raises(DatasetError, match="canonical_id"):
        stability.validation_report(model, dataset, provenance)


@pytest.mark.parametrize("offset", [0, 1, 6])
def test_validation_shifted_mask_counts_and_single_evaluation_path(monkeypatch, offset):
    from types import SimpleNamespace

    dataset = [([1] * (offset + 2 + i % 3), offset) for i in range(64)]
    calls = []
    model = SimpleNamespace(
        mode="train",
        eval=lambda: (calls.append(("eval", model.mode)), setattr(model, "mode", "eval")),
        train=lambda: (calls.append(("train", model.mode)), setattr(model, "mode", "train")),
    )

    def batches(values, _batch_size, _cap):
        # The integer batch keeps row order explicit while avoiding any MLX dependency.
        yield from ((i, None) for i in range(len(values)))

    def loss(received_model, row_index, _bounds):
        assert received_model.mode == "eval"
        calls.append(("forward", row_index, received_model.mode))
        return SimpleNamespace(item=lambda: row_index / 7), SimpleNamespace(
            item=lambda: len(dataset[row_index][0]) - max(1, dataset[row_index][1])
        )

    monkeypatch.setattr(stability, "ordered_batches", batches)
    monkeypatch.setattr(smoke, "exclude_padding_loss", loss)
    provenance = [{"row_id": f"row-{i}", "canonical_id": None} for i in range(64)]

    scalar = stability.validation_mean(model, dataset)
    scalar_calls = len([call for call in calls if call[0] == "forward"])
    calls.clear()
    mean, count, diagnostics = stability.validation_report(model, dataset, provenance)

    expected_losses = [i / 7 for i in range(64)]
    expected_counts = [len(tokens) - max(1, row_offset) for tokens, row_offset in dataset]
    assert scalar == (math.fsum(expected_losses) / 64, 64)
    assert mean == scalar[0] == math.fsum(record["loss"] for record in diagnostics) / 64
    assert count == scalar[1] == 64
    assert [row["supervised_tokens"] for row in diagnostics] == expected_counts
    assert scalar_calls == 64
    assert len([call for call in calls if call[0] == "forward"]) == 64
    assert all(call[2] == "eval" for call in calls if call[0] == "forward")
    assert model.mode == "train"
    assert calls[0] == ("eval", "train") and calls[-1] == ("train", "eval")


def test_validation_reports_attempted_nonfinite_row_and_restores_mode(monkeypatch):
    from types import SimpleNamespace

    dataset = [([1] * 6, 1) for _ in range(64)]
    provenance = [{"row_id": f"row-{i}", "canonical_id": "revenue"} for i in range(64)]
    calls = []
    model = SimpleNamespace(eval=lambda: calls.append("eval"), train=lambda: calls.append("train"))
    monkeypatch.setattr(
        stability,
        "ordered_batches",
        lambda values, *_args: iter((i, None) for i in range(64)),
    )

    def loss(_model, row_index, _bounds):
        return SimpleNamespace(
            item=lambda: float("nan") if row_index == 2 else 1.0
        ), SimpleNamespace(item=lambda: 5)

    monkeypatch.setattr(smoke, "exclude_padding_loss", loss)
    mean, count, records = stability.validation_report(model, dataset, provenance)
    assert math.isnan(mean) and count == 3 and len(records) == 3
    assert records[-1]["row_id"] == "row-2" and math.isnan(records[-1]["loss"])
    assert calls == ["eval", "train"]


def test_validation_restores_mode_when_forward_raises(monkeypatch):
    dataset = [([1] * 6, 1) for _ in range(64)]
    calls = []
    model = type(
        "Model",
        (),
        {"eval": lambda self: calls.append("eval"), "train": lambda self: calls.append("train")},
    )()
    monkeypatch.setattr(
        stability,
        "ordered_batches",
        lambda values, *_args: iter([(None, None)] * len(values)),
    )
    monkeypatch.setattr(
        smoke,
        "exclude_padding_loss",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("forward failed")),
    )
    with pytest.raises(RuntimeError, match="forward failed"):
        stability.validation_mean(model, dataset)
    assert calls == ["eval", "train"]


def test_validation_diagnostic_count_mismatch_fails_loudly(monkeypatch):
    from types import SimpleNamespace

    dataset = [([1] * 6, 0) for _ in range(64)]
    provenance = [{"row_id": f"row-{i}", "canonical_id": None} for i in range(64)]
    monkeypatch.setattr(
        stability, "ordered_batches", lambda values, *_args: iter([(None, None)] * len(values))
    )
    monkeypatch.setattr(
        smoke,
        "exclude_padding_loss",
        lambda *_args: (SimpleNamespace(item=lambda: 1.0), SimpleNamespace(item=lambda: 6)),
    )
    model = type("Model", (), {"eval": lambda self: None, "train": lambda self: None})()
    with pytest.raises(DatasetError, match="supervision count differs"):
        stability.validation_report(model, dataset, provenance)


@pytest.mark.parametrize("initial_mode", ["train", "eval"])
@pytest.mark.usefixtures("cpu_mlx")
def test_real_cpu_validation_uses_shifted_mask_and_one_padded_forward_per_row(
    initial_mode,
):
    import mlx.core as mx
    import numpy as np

    examples = [
        ([1, 2], 0),  # minimum accepted length; offset zero still predicts token 2
        ([3, 4, 5], 1),  # offset one
        ([5, 6, 7, 8, 9], 3),
        ([2, 4, 6, 8, 10, 12, 14], 5),  # late offset and longest padded row
    ]
    dataset = [examples[index % len(examples)] for index in range(stability.VALID_ROWS)]
    provenance = [
        {"row_id": f"synthetic-{index}", "canonical_id": None}
        for index in range(stability.VALID_ROWS)
    ]
    table = np.arange(16 * 16, dtype=np.float32).reshape(16, 16) / 37

    class TinyModel:
        def __init__(self):
            self.mode = initial_mode
            self.forward_modes = []
            self.input_shapes = []

        def eval(self):
            self.mode = "eval"

        def train(self):
            self.mode = "train"

        def __call__(self, inputs):
            self.forward_modes.append(self.mode)
            self.input_shapes.append(tuple(inputs.shape))
            return mx.array(table)[inputs]

    model = TinyModel()
    batches = list(stability.ordered_batches(dataset, 1, 2048))
    assert len(batches) == stability.VALID_ROWS
    assert max(batch.shape[1] for batch, _ in batches) > max(len(tokens) for tokens, _ in dataset)

    # Compute expected CE from each original target position, independently of the
    # production loss and its returned token count.
    expected_losses = []
    expected_counts = []
    for tokens, offset in dataset:
        row_losses = []
        for target_position in range(1, len(tokens)):
            if target_position < offset:
                continue
            scores = table[tokens[target_position - 1]].astype(np.float64)
            maximum = float(scores.max())
            log_normalizer = maximum + math.log(float(np.exp(scores - maximum).sum()))
            row_losses.append(log_normalizer - float(scores[tokens[target_position]]))
        expected_losses.append(math.fsum(row_losses) / len(row_losses))
        expected_counts.append(len(row_losses))

    scalar = stability.validation_mean(model, dataset)
    assert scalar[0] == pytest.approx(math.fsum(expected_losses) / stability.VALID_ROWS, abs=2e-6)
    assert scalar[1] == stability.VALID_ROWS
    assert model.mode == "train"
    assert model.forward_modes == ["eval"] * stability.VALID_ROWS

    model.forward_modes.clear()
    mean, count, records = stability.validation_report(model, dataset, provenance)
    assert mean == scalar[0]
    assert count == scalar[1] == stability.VALID_ROWS
    assert mean == math.fsum(record["loss"] for record in records) / stability.VALID_ROWS
    assert [record["supervised_tokens"] for record in records] == expected_counts
    assert [record["row_id"] for record in records] == [item["row_id"] for item in provenance]
    assert mean == pytest.approx(math.fsum(expected_losses) / stability.VALID_ROWS, abs=2e-6)
    assert model.forward_modes == ["eval"] * stability.VALID_ROWS
    assert len(model.input_shapes) == 2 * stability.VALID_ROWS
    assert model.mode == "train"


@pytest.mark.parametrize(
    "field,value", [("batch_size", 2), ("seed", 18), ("learning_rate", 2e-5), ("iters", 21)]
)
def test_direct_fit_keeps_smoke_bounds(config, trainer_stubs, field, value):  # noqa: F811
    from types import SimpleNamespace

    options = smoke.training_options(config)
    options[field] = value
    with pytest.raises(DatasetError, match="direct fit requires smoke"):
        smoke.fit(
            SimpleNamespace(**options),
            object(),
            [([1] * 10, 5)],
            [([1] * 10, 5)],
            loss=smoke.exclude_padding_loss,
        )


def test_selections_rechecks_tokenizer_snapshot_after_encoding(config, monkeypatch):  # noqa: F811
    class Context:
        checks = 0

        def assert_current(self, _directory):
            self.checks += 1
            if self.checks == 2:
                raise DatasetError("tokenizer snapshot changed during verification")

        def encode(self, _messages):
            return [1, 2, 3], 1

    class Rows:
        def __getitem__(self, index):
            return {"messages": [{"role": "assistant", "content": '{"canonical_id":null}'}]}

    monkeypatch.setattr(stability, "read_rows", lambda _path: Rows())
    monkeypatch.setattr(
        stability,
        "select_rows",
        lambda rows, count, encode, cap: {"permuted": [0]},
    )
    context = Context()
    with pytest.raises(DatasetError, match="snapshot changed"):
        stability.selections(config, tokenizer_context=context)
    assert context.checks == 2


def test_preregistration_verification_and_changed_pin(config, monkeypatch):  # noqa: F811
    conf = diagnostic(config)
    monkeypatch.setattr(stability, "DATASET_PIN", config.dataset_manifest_sha256)
    monkeypatch.setattr(stability, "EXCLUSIONS_PIN", smoke.file_sha256(config.exclusions))
    monkeypatch.setattr(stability, "read_rows", lambda _: rows(8))
    monkeypatch.setattr(stability, "encoder", lambda _, *_args: encode)
    monkeypatch.setattr(stability, "FIT_ROWS", 2)
    monkeypatch.setattr(stability, "VALID_ROWS", 2)
    plan = conf.run_dir.parent / "plan.json"
    plan.write_text(
        stability.DiagnosticInputs.model_validate(
            conf.model_dump(exclude={"preregistration", "preregistration_sha256"})
        ).model_dump_json()
    )
    stability.preregister(plan, conf.preregistration)
    generated = stability.DiagnosticConfig.model_validate_json(
        (plan.parent / "A.json").read_bytes()
    )
    record, selected = stability.verify_preregistration(generated)
    assert len(selected[0]) == len(selected[1]) == 2
    for arm in stability.ARMS:
        arm_config = stability.DiagnosticConfig.model_validate_json(
            (plan.parent / f"{arm}.json").read_bytes()
        )
        assert stability.verify_preregistration(arm_config)[0]["selection"] == record["selection"]
    config.model_manifest.write_text("changed")
    with pytest.raises(DatasetError, match="sha256"):
        stability.verify_preregistration(generated)


def test_collision_count_and_unique_overcap_count():
    def tokens(row):
        index = int(row["messages"][0]["content"])
        return list(range(2050 if index < 4 else 10)), 5

    sample = stability.select_rows(rows(10), 4, tokens, 2048)
    assert sample["selected"] == [4, 5, 6, 9]
    assert sample["collisions"] == 1
    assert sample["skipped_over_cap_count"] == 4


def test_failed_child_preserves_partial_events_and_malformed_tail(
    config,  # noqa: F811
    monkeypatch,
):
    conf = diagnostic(config)
    monkeypatch.setattr(
        stability,
        "verify_preregistration",
        lambda _, **kwargs: ({"identity": {"pin": 1}, "selection": {"sample": 1}}, ([], [])),
    )
    monkeypatch.setattr(stability, "version", lambda _: "0.31.3")
    monkeypatch.setattr(
        smoke,
        "probe_resources",
        lambda: smoke.Resources(
            free_disk_bytes=20_000_000_000,
            available_memory_bytes=smoke.MIN_MEMORY_BYTES,
            services=(),
        ),
    )

    def fail(c, **kwargs):
        (c.run_dir / "events.jsonl").write_text(
            json.dumps(
                {"kind": "train", "iteration": 1, "train_loss": "Infinity", "nonfinite": True}
            )
            + '\n{"kind":'
        )
        (c.run_dir / "resources.json").write_text('{"peak_mlx_bytes":')
        raise RuntimeError("child failed")

    monkeypatch.setattr(smoke, "launch_trainer", fail)
    assert stability.run(conf, dry_run=False) == 1
    report = json.loads((conf.run_dir / "report.json").read_text())
    assert report["status"] == "failed"
    assert report["events"][0]["iteration"] == 1
    assert report["events"][0]["nonfinite"] is True
    assert report["artifact_errors"]
    result = stability.evaluate_reports([report])
    assert result["arms"]["A"]["criteria"]["no_nonfinite"] is False
    assert result["winners"] == []
    # The experiment attempt marker refuses a changed directory too.
    another = conf.model_copy(update={"run_dir": conf.run_dir.parent / "another"})
    assert stability.run(another, dry_run=False) == 1
    assert "already exists" in json.loads((another.run_dir / "report.json").read_text())["error"]


def test_authorized_dataset_pin_cannot_be_replaced(config):  # noqa: F811
    with pytest.raises(DatasetError, match="authorized diagnostic"):
        stability.identities(diagnostic(config))


def paired(base, pair_arm):
    return stability.BiasCorrectionConfig.model_validate(
        {
            **diagnostic(base, "D").model_dump(),
            "pair_arm": pair_arm,
        }
    )


def test_paired_bias_correction_is_the_only_optimizer_difference(config):  # noqa: F811
    reference, treatment = paired(config, "reference"), paired(config, "treatment")
    a, b = smoke.training_options(reference), smoke.training_options(treatment)
    assert a["optimizer_config"]["adamw"].pop("bias_correction") is False
    assert b["optimizer_config"]["adamw"].pop("bias_correction") is True
    assert a == b
    assert stability.common_inputs(reference) == stability.common_inputs(treatment)
    assert reference.batch_size == treatment.batch_size == 4
    assert reference.learning_rate == treatment.learning_rate == 1e-5
    with pytest.raises(ValidationError):
        stability.BiasCorrectionConfig.model_validate({**reference.model_dump(), "arm": "A"})


def test_paired_preregistration_reuses_selection_and_refuses_drift(config, monkeypatch, tmp_path):  # noqa: F811
    reference = paired(config, "reference")
    plan = tmp_path / "pair-plan.json"
    plan.write_text(reference.model_dump_json())
    monkeypatch.setattr(stability, "identities", lambda c, *_args: {"base": "pinned fresh"})
    monkeypatch.setattr(
        stability,
        "supervised_budget",
        lambda c, r, **kwargs: {"updates": 60, "examples": 240, "supervised_tokens": 2161},
    )
    selection = {"fit": {"permuted": [1, 0]}, "valid": {"permuted": [0, 1]}}
    monkeypatch.setattr(stability, "selections", lambda c, *_args: (selection, ([], [])))
    out = tmp_path / "paired-preregistration.json"
    stability.preregister_bias_correction(plan, out)
    a = stability.BiasCorrectionConfig.model_validate_json(
        (tmp_path / "reference.json").read_bytes()
    )
    b = stability.BiasCorrectionConfig.model_validate_json(
        (tmp_path / "treatment.json").read_bytes()
    )
    assert a.run_dir != b.run_dir and a.preregistration_sha256 == b.preregistration_sha256
    record, _ = stability.verify_preregistration(a)
    assert record["schema"] == 2 and record["selection"] == selection
    stability.verify_preregistration(b)
    monkeypatch.setattr(stability, "implementations", lambda: {"changed": "bytes"})
    with pytest.raises(DatasetError, match="implementation"):
        stability.verify_preregistration(a)


def test_pair_evaluation_requires_matched_token_exposure(config):  # noqa: F811
    reports = []
    for arm in ("reference", "treatment"):
        conf = paired(config, arm)
        report = copy.deepcopy(evidence()[3])
        report["config"] = conf.model_dump(mode="json")
        report["metrics"]["train"][-1]["trained_tokens"] = 2161
        reports.append(report)
    result = stability.evaluate_bias_correction(reports)
    assert result["both_stable"] is True
    assert result["generated_quality"] == "NOT MEASURED"
    reports[1]["metrics"]["train"][-1]["trained_tokens"] = 2160
    with pytest.raises(DatasetError, match="token budget"):
        stability.evaluate_bias_correction(reports)


def test_pair_callback_refuses_wrong_final_token_count(config):  # noqa: F811
    conf = paired(config, "treatment")
    conf.run_dir.mkdir()
    conf.preregistration.write_text(json.dumps({"budget": {"supervised_tokens": 2161}}))
    control = stability.fit_control(conf, {"permuted": list(range(64))}, rows(64))
    with pytest.raises(DatasetError, match="token budget"):
        control.record("train", {"iteration": 60, "trained_tokens": 2160})
    assert (conf.run_dir / "events.jsonl").is_file()


def treatment_only(base):
    fields = {
        f"historical_{name}": stability.Path(path)
        for name, (path, _sha) in stability.HISTORICAL.items()
    }
    fields.update(
        {f"historical_{name}_sha256": sha for name, (_path, sha) in stability.HISTORICAL.items()}
    )
    return stability.TreatmentOnlyConfig.model_validate(
        {
            **diagnostic(base, "D").model_dump(),
            "pair_arm": "treatment",
            "run_dir": base.run_dir.parent / "treatment-only",
            "preregistration": base.run_dir.parent / "treatment-preregistration.json",
            **fields,
        }
    )


def test_treatment_only_freezes_settings_and_historical_identity_descriptor(config):  # noqa: F811
    conf = treatment_only(config)
    assert smoke.training_options(conf)["optimizer_config"]["adamw"]["bias_correction"] is True
    with pytest.raises(ValidationError):
        stability.TreatmentOnlyConfig.model_validate({**conf.model_dump(), "seed": 18})
    with pytest.raises(ValidationError, match="descriptor"):
        stability.TreatmentOnlyConfig.model_validate(
            {**conf.model_dump(), "historical_report_sha256": "b" * 64}
        )


def test_treatment_verification_rechecks_historical_file_hashes(config, monkeypatch):  # noqa: F811
    conf = treatment_only(config)
    monkeypatch.setattr(stability, "file_sha256", lambda _path: "b" * 64)
    with pytest.raises(DatasetError, match="historical report sha256"):
        stability.verify_historical(conf, {}, {})


@pytest.mark.skipif(
    not Path("/private/tmp/normalization-quality-protocol/panels-v1").is_dir(),
    reason="reads the frozen validation panels, which exist only on the evaluation host",
)
def test_treatment_registration_is_exclusive_and_source_bound(config, monkeypatch, tmp_path):  # noqa: F811
    conf = treatment_only(config)
    plan = tmp_path / "treatment-plan.json"
    plan.write_text(conf.model_dump_json())
    monkeypatch.setattr(stability, "identities", lambda _c, *_args: {"model": "pinned"})
    monkeypatch.setattr(
        stability,
        "selections",
        lambda _c, *_args: ({"fit": {"permuted": [1]}, "valid": {"permuted": [2]}}, ([], [])),
    )
    monkeypatch.setattr(
        stability,
        "supervised_budget",
        lambda _c, _rows, **kwargs: {"updates": 60, "examples": 240, "supervised_tokens": 2161},
    )
    monkeypatch.setattr(stability, "verify_historical", lambda *_args: {"status": "exploratory"})
    monkeypatch.setattr(stability, "treatment_protocol_files", lambda: {})
    out = tmp_path / "treatment" / "preregistration.json"
    out.parent.mkdir()
    stability.preregister_treatment(plan, out)
    saved = json.loads(out.read_bytes())
    run_config = stability.TreatmentOnlyConfig.model_validate_json(
        (out.parent / "treatment.json").read_bytes()
    )
    assert saved["schema"] == 3
    assert saved["mode"] == "treatment_only_historical_exploratory_comparator"
    assert saved["selection"]["fit"]["permuted"] == [1]
    treatment_command = saved["commands"]["treatment"]
    assert treatment_command["argv"][0] == stability.sys.executable
    assert treatment_command["argv"][-1] == str(out.parent / "treatment.json")
    assert treatment_command["cwd"] == str(smoke.ROOT)
    assert saved["training_options"] == smoke.training_options(run_config)
    assert set(saved["panel_artifacts"]["sha256"]) == {
        "representative-selection",
        "representative-manifest",
        "challenge-selection",
        "challenge-manifest",
        "population-and-reconciliation",
    }
    historical_argv = saved["commands"]["generation"]["generation_commands"][
        "historical-D-representative"
    ]["argv"]
    assert historical_argv[historical_argv.index("--adapter-dir") + 1] == str(
        run_config.historical_adapter.parent
    )
    assert saved["commands"]["generation"]["lexicon"]["implementation"].endswith("LabelIndex.match")
    assert run_config.pair_arm == "treatment"
    stability.verify_treatment(run_config)
    with pytest.raises(DatasetError, match="already exists"):
        stability.preregister_treatment(plan, out)
    monkeypatch.setattr(stability, "treatment_implementations", lambda: {"changed": "source"})
    with pytest.raises(DatasetError, match="binding changed"):
        stability.verify_treatment(run_config)


def test_historical_comparator_is_descriptive_and_schema2_pair_stays_both_stable(config):  # noqa: F811
    reports = []
    for arm in ("reference", "treatment"):
        conf = paired(config, arm)
        report = copy.deepcopy(evidence()[3])
        report["config"] = conf.model_dump(mode="json")
        report["metrics"]["train"][-1]["trained_tokens"] = 2161
        reports.append(report)
    assert stability.evaluate_bias_correction(reports)["both_stable"] is True


def treatment_report_fixture(config, monkeypatch):  # noqa: F811
    conf = treatment_only(config)
    record = {
        "identity": {"model": "pinned"},
        "selection": {"fit": "fixed", "valid": "fixed"},
        "environment": {
            name: "0.31.3" for name in ("mlx", "mlx-lm", "transformers", "tokenizers", "numpy")
        },
        "training_options": smoke.training_options(conf),
        "historical_comparator": {"status": "historical exploratory comparator"},
    }
    monkeypatch.setattr(stability, "verify_treatment", lambda _config: (record, ([], [])))
    monkeypatch.setattr(stability, "implementations", lambda: {"source": "frozen"})
    artifacts = {
        key: {"path": key, "sha256": key} for key in ("adapter", "checkpoint", "adapter_config")
    }
    monkeypatch.setattr(stability, "treatment_artifacts", lambda _config: copy.deepcopy(artifacts))
    report = copy.deepcopy(evidence()[3])
    report.update(
        {
            "config": conf.model_dump(mode="json"),
            "training_options": record["training_options"],
            "implementation": {"source": "frozen"},
            "environment": record["environment"],
            "identity": record["identity"],
            "selection": record["selection"],
            "treatment_artifacts": copy.deepcopy(artifacts),
            "trainer_launcher_elapsed_seconds": 10.0,
        }
    )
    report["metrics"]["train"][-1]["trained_tokens"] = 2161
    return report


def test_treatment_evaluation_rejects_incomplete_exposure(config, monkeypatch):  # noqa: F811
    report = treatment_report_fixture(config, monkeypatch)
    assert stability.evaluate_treatment(report)["treatment_accepted_for_conditional_generation"]
    report["metrics"]["train"][-1]["trained_tokens"] = 2160
    with pytest.raises(DatasetError, match="exposure"):
        stability.evaluate_treatment(report)


@pytest.mark.parametrize("field", ["environment", "implementation"])
def test_treatment_evaluation_rejects_wrong_runtime_identity(config, monkeypatch, field):  # noqa: F811
    report = treatment_report_fixture(config, monkeypatch)
    report[field] = {"wrong": "runtime"}
    with pytest.raises(DatasetError, match="frozen treatment registration"):
        stability.evaluate_treatment(report)


def test_treatment_evaluation_requires_hashed_final_artifacts(config, monkeypatch):  # noqa: F811
    report = treatment_report_fixture(config, monkeypatch)
    report["treatment_artifacts"]["checkpoint"]["sha256"] = "altered"
    with pytest.raises(DatasetError, match="artifacts"):
        stability.evaluate_treatment(report)


@pytest.mark.parametrize("bad_line", [b"\xff", b"{truncated", b"[]"])
@pytest.mark.parametrize("bad_first", [True, False])
def test_timeout_preserves_binary_artifacts_and_primary_error(
    config,  # noqa: F811
    monkeypatch,
    bad_line,
    bad_first,
):
    conf = diagnostic(config)
    monkeypatch.setattr(stability, "version", lambda _: "0.31.3")
    monkeypatch.setattr(
        smoke,
        "probe_resources",
        lambda: smoke.Resources(
            free_disk_bytes=20_000_000_000, available_memory_bytes=15_000_000_000, services=()
        ),
    )
    good = {
        "stage": "worker-readiness",
        "pid": 10,
        "ppid": 1,
        "sampled_at": "now",
        "required_memory_bytes": 14,
    }
    event = {"kind": "train", "iteration": 1, "train_loss": 1.0}

    def fail(_config, **kwargs):
        for filename, sample in (("resource-samples.jsonl", good), ("events.jsonl", event)):
            encoded = json.dumps(sample).encode()
            parts = [bad_line, encoded] if bad_first else [encoded, bad_line]
            (conf.run_dir / filename).write_bytes(b"\n".join(parts))
        (conf.run_dir / "resources.json").write_bytes(b"\xff")
        (conf.run_dir / "worker-verification.json").write_bytes(b"\xff")
        raise TimeoutError("primary supervised timeout")

    monkeypatch.setattr(smoke, "launch_trainer", fail)
    assert stability.run(conf, dry_run=False) == 1
    report = json.loads((conf.run_dir / "report.json").read_bytes())
    assert report["status"] == "failed"
    assert "primary supervised timeout" in report["error"]
    assert report["resource_samples"] == [good]
    assert report["events"] == [event]
    for name in ("resource-samples.jsonl", "events.jsonl"):
        assert any(f"{name} line {1 if bad_first else 2}" in e for e in report["artifact_errors"])
    assert any("worker-verification.json" in e for e in report["artifact_errors"])
    assert any("resources.json" in e for e in report["artifact_errors"])


@pytest.mark.parametrize(
    "mutation",
    [
        "valid",
        "empty_identity",
        "garbage_identity",
        "missing_identity",
        "nested_identity",
        "empty_selection",
        "garbage_selection",
        "missing_selection",
        "nested_selection",
        "missing_evidence",
    ],
)
def test_diagnostic_validates_nested_worker_metadata(config, monkeypatch, mutation):  # noqa: F811
    from test_normalization_train import worker_metadata

    metadata = worker_metadata(config)
    metadata["selection"] = {
        split: stability.select_rows(rows(count), count, encode, 2048)
        for split, count in (("fit", 240), ("valid", 64))
    }
    conf = diagnostic(config)
    if mutation.startswith("empty_"):
        metadata[mutation.removeprefix("empty_")] = {}
    elif mutation.startswith("garbage_"):
        metadata[mutation.removeprefix("garbage_")] = {"garbage": 1}
    elif mutation == "missing_identity":
        metadata["identity"].pop("model_repository")
    elif mutation == "nested_identity":
        metadata["identity"]["artifact_sha256"] = []
    elif mutation == "missing_selection":
        metadata["selection"]["fit"].pop("row_sha256")
    elif mutation == "nested_selection":
        metadata["selection"]["valid"]["stats"] = []
    conf.preregistration.write_text(json.dumps(metadata))
    conf = conf.model_copy(
        update={"preregistration_sha256": smoke.file_sha256(conf.preregistration)}
    )
    monkeypatch.setattr(stability, "version", lambda _: "0.31.3")
    monkeypatch.setattr(
        smoke,
        "probe_resources",
        lambda: smoke.Resources(
            free_disk_bytes=20_000_000_000, available_memory_bytes=15_000_000_000, services=()
        ),
    )

    def launch(_config, **kwargs):
        if mutation != "missing_evidence":
            (conf.run_dir / "worker-verification.json").write_text(json.dumps(metadata))
        (conf.run_dir / "metrics.json").write_text(json.dumps(evidence()[0]["metrics"]))
        return {"peak_mlx_bytes": 1, "peak_rss_bytes": 2}

    monkeypatch.setattr(smoke, "launch_trainer", launch)
    assert stability.run(conf, dry_run=False) == int(mutation != "valid")
    report = json.loads((conf.run_dir / "report.json").read_bytes())
    if mutation == "valid":
        assert report["status"] == "diagnostic_completed"
        assert report["verification_status"] == "verified_in_worker"
        assert report["identity"] == metadata["identity"]
        assert report["selection"] == metadata["selection"]
    else:
        assert report["status"] == "failed"
        assert report["verification_status"] != "verified_in_worker"
        assert any("worker-verification.json" in e for e in report["artifact_errors"])
