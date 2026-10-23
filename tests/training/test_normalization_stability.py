"""Synthetic evidence for fixed, preregistered optimization diagnostics; no MLX required."""

import copy
import json

import normalization_stability as stability
import normalization_train as smoke
import pytest
from dataset_contracts import DatasetError
from pydantic import ValidationError
from test_normalization_train import config, local_model  # noqa: F401


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
        lambda c: (_ for _ in ()).throw(DatasetError("changed pin")),
    )
    assert stability.run(conf, dry_run=True) == 1
    assert json.loads((conf.run_dir / "report.json").read_text())["status"] == "failed"
    with pytest.raises(DatasetError, match="already exists"):
        stability.run(conf, dry_run=True)


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
            item=lambda: 2
        )

    monkeypatch.setattr(smoke, "exclude_padding_loss", loss)

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
                control=stability.fit_control(conf),
            )
        events = [
            json.loads(line) for line in (conf.run_dir / "events.jsonl").read_text().splitlines()
        ]
        assert events[0]["iteration"] == 0 and events[0]["nonfinite"] is True
        assert events[0]["rows"] == 1
        assert "NaN" in json.dumps(events)
    else:
        result = smoke.fit(
            SimpleNamespace(**smoke.training_options(conf)),
            model,
            pairs * 240,
            pairs * 64,
            loss=loss,
            control=stability.fit_control(conf),
        )
        assert counts == [s for s in (0, 15, 30, 45, 60) for _ in range(64)]
        assert [r["iteration"] for r in result["validation"]] == [0, 15, 30, 45, 60]


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


def test_preregistration_verification_and_changed_pin(config, monkeypatch):  # noqa: F811
    conf = diagnostic(config)
    monkeypatch.setattr(stability, "DATASET_PIN", config.dataset_manifest_sha256)
    monkeypatch.setattr(stability, "EXCLUSIONS_PIN", smoke.file_sha256(config.exclusions))
    monkeypatch.setattr(stability, "read_rows", lambda _: rows(8))
    monkeypatch.setattr(stability, "encoder", lambda _: encode)
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
        lambda _: ({"identity": {"pin": 1}, "selection": {"sample": 1}}, ([], [])),
    )
    monkeypatch.setattr(stability, "version", lambda _: "0.31.3")
    monkeypatch.setattr(
        smoke,
        "probe_resources",
        lambda: smoke.Resources(
            free_disk_bytes=20_000_000_000, available_memory_bytes=14_000_000_000, services=()
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
