"""Bounded offline normalization QLoRA smoke; no holdout loading or quality gate scoring.

Dry runs verify inputs in the controller process; training runs delegate verification to the
heavy worker process.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import os
import platform
import re
import resource
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, NotRequired, TypedDict, cast

from chat_tokenizer import ChatTokenizerContext, open_chat_tokenizer
from dataset_builder import BUILDER_VERSION, SYSTEM_PROMPT, DatasetBuildManifest, Row
from dataset_contracts import Contract, DatasetError, Sha256, _taxonomy_digest, digest
from dataset_exclusions import ExclusionAudit, load_exclusions, verify_source
from exclusive_write import write_new
from normalization_mlx import LocalModelManifest, model_identity
from normalization_validation import file_sha256
from pydantic import Field

from fra_core.taxonomy import load_taxonomy

ROOT = Path(__file__).resolve().parents[1]
# Completed runs peaked at 8.15 GB MLX and 2.4 to 3.2 GB process RSS (4.0 GB in the
# historical arm D). The counters overlap in unified memory, so their sum is a ceiling:
# 11.35 GB for the pair runs. 12 GB covers that, with 0.65 GB spare. This is not the
# 11.5 GB allocator cap.
MIN_MEMORY_BYTES = 12_000_000_000
PROBE_MIN_MEMORY_BYTES = 8_500_000_000
TRAIN_ALLOCATION_BYTES = 11_500_000_000
DIAGNOSTIC_ALLOCATION_BYTES = 6_000_000_000
CACHE_LIMIT_BYTES = 1_000_000_000
MIN_DISK_BYTES = 15_000_000_000


class TrainingConfig(Contract):
    dataset_dir: Path
    dataset_manifest_sha256: Sha256
    exclusions: Path
    model_dir: Path
    model_manifest: Path
    model_manifest_sha256: Sha256
    run_dir: Path
    iters: int
    batch_size: int
    grad_checkpoint: bool
    num_layers: int
    max_seq_length: int
    seed: int
    timeout_seconds: int

    @property
    def learning_rate(self) -> float:
        return 1e-4

    @property
    def steps_per_eval(self) -> int:
        return self.iters

    @property
    def val_batches(self) -> int:
        return 1

    @property
    def adamw_bias_correction(self) -> bool:
        return False


class ValidationRowDiagnostic(TypedDict):
    row_id: str
    loss: float
    supervised_tokens: int
    canonical_id: str | None
    iteration: NotRequired[int]


class SmokeConfig(TrainingConfig):
    iters: int = Field(default=20, ge=1, le=20)
    batch_size: Literal[1] = 1
    grad_checkpoint: Literal[True] = True
    num_layers: int = Field(default=16, ge=1, le=16)
    max_seq_length: int = Field(default=2048, ge=1, le=2048)
    seed: Literal[17] = 17
    timeout_seconds: int = Field(default=900, ge=1, le=1800)


class Resources(Contract):
    free_disk_bytes: int = Field(ge=0)
    # Compatibility field: this is a percentage-derived estimate, not a byte counter.
    available_memory_bytes: int = Field(
        ge=0, description="Estimate from integer memory_pressure free percentage × physical memory"
    )
    services: tuple[str, ...]
    available_memory_basis: Literal["estimate_from_whole_percent"] = "estimate_from_whole_percent"
    physical_memory_bytes: int | None = None
    memory_pressure_free_percent: int | None = None
    memory_estimate_bytes: int | None = None
    memory_method: str | None = None
    memory_granularity_bytes: int | None = None
    memory_granularity_percentage_points: int | None = None
    rss_method: str | None = None
    current_rss_bytes: int | None = None
    peak_rss_bytes: int | None = None

    def check(self, minimum_memory_bytes: int = MIN_MEMORY_BYTES) -> None:
        if minimum_memory_bytes <= 0:
            raise ValueError("minimum memory budget must be positive")
        if self.free_disk_bytes < MIN_DISK_BYTES:
            raise DatasetError("preflight requires at least 15 GB free disk")
        if self.available_memory_bytes < minimum_memory_bytes:
            if minimum_memory_bytes == MIN_MEMORY_BYTES:
                raise DatasetError(
                    f"preflight requires {MIN_MEMORY_BYTES / 1e9:g} GB available memory"
                )
            raise DatasetError(
                f"preflight requires at least {minimum_memory_bytes} bytes available memory"
            )
        if self.services:
            raise DatasetError(f"stop repository services before training: {self.services}")


def probe_resources() -> Resources:
    """Read system counters only; failures refuse the run, never substitute estimates."""
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise DatasetError("training smoke requires Apple Silicon macOS")

    def output(command: list[str]) -> str:
        try:
            return subprocess.run(
                command, check=True, capture_output=True, text=True, timeout=10
            ).stdout
        except subprocess.CalledProcessError as exc:
            raise DatasetError(
                f"preflight probe {command!r} failed ({exc.returncode}): {exc.stderr.strip()}"
            ) from exc

    memory = int(output(["/usr/sbin/sysctl", "-n", "hw.memsize"]).strip())
    pressure = output(["/usr/bin/memory_pressure", "-Q"])
    match = re.search(r"System-wide memory free percentage:\s*(\d+)%", pressure)
    if match is None:
        raise DatasetError("cannot read memory_pressure free percentage")
    processes = output(["/bin/ps", "-axo", "command="])
    services = tuple(
        line.strip()
        for line in processes.splitlines()
        if re.search(r"\b(fra-api|fra-worker|fra_api|fra_worker)\b", line)
        or ("vite" in line and "financial-reports-analysis-model" in line)
    )
    estimate = memory * int(match[1]) // 100
    rss = current_rss_bytes()
    return Resources(
        free_disk_bytes=shutil.disk_usage(ROOT).free,
        available_memory_bytes=estimate,
        services=services,
        physical_memory_bytes=memory,
        memory_pressure_free_percent=int(match[1]),
        memory_estimate_bytes=estimate,
        memory_method="memory_pressure -Q integer free percentage × hw.memsize",
        memory_granularity_bytes=(memory + 99) // 100,
        memory_granularity_percentage_points=1,
        rss_method="psutil current RSS; resource.getrusage peak RSS",
        current_rss_bytes=rss,
        peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    )


def current_rss_bytes() -> int | None:
    """Read current RSS when the existing runtime provides psutil; never block preflight."""
    try:
        psutil = importlib.import_module("psutil")
    except ImportError:
        return None
    return int(psutil.Process().memory_info().rss)


def check_and_record_resources(
    run_dir: Path,
    *,
    stage: str,
    probe: Callable[[], Resources] = probe_resources,
    minimum_memory_bytes: int = MIN_MEMORY_BYTES,
) -> Resources:
    """Persist the raw, coarse memory sample before any readiness refusal."""
    state = probe()
    sample = {
        **state.model_dump(mode="json"),
        "stage": stage,
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "sampled_at": datetime.now(UTC).isoformat(),
        "required_memory_bytes": minimum_memory_bytes,
    }
    path = run_dir / "resource-samples.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(sample, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    state.check(minimum_memory_bytes)
    return state


def training_options(config: TrainingConfig) -> dict[str, Any]:
    """Explicit bounded LoRA/AdamW options, with no dataset loader or test scoring."""
    return {
        "model": str(config.model_dir.resolve()),
        "train": True,
        "test": False,
        "fine_tune_type": "lora",
        "optimizer": "adamw",
        "optimizer_config": {
            "adamw": {
                "bias_correction": config.adamw_bias_correction,
                "betas": [0.9, 0.999],
                "eps": 1e-8,
                "weight_decay": 0.01,
            }
        },
        "batch_size": config.batch_size,
        "iters": config.iters,
        "num_layers": config.num_layers,
        "grad_checkpoint": config.grad_checkpoint,
        "max_seq_length": config.max_seq_length,
        "seed": config.seed,
        "mask_prompt": True,
        "grad_accumulation_steps": 1,
        "learning_rate": config.learning_rate,
        "lora_parameters": {"rank": 16, "scale": 20.0, "dropout": 0.05},
        "steps_per_report": 1,
        "steps_per_eval": config.steps_per_eval,
        "val_batches": config.val_batches,
        "save_every": config.iters,
        "adapter_path": str(config.run_dir / "adapter"),
        "resume_adapter_file": None,
        "lr_schedule": None,
    }


def verify_inputs(
    config: TrainingConfig,
    *,
    selected_rows_only: bool = False,
    tokenizer_context: ChatTokenizerContext | None = None,
) -> dict[str, Any]:
    """Trust pinned builder artifacts, without rebuilding partitions or opening source labels."""
    if tokenizer_context is not None:
        tokenizer_context.assert_current(config.model_dir)
    if (config.dataset_dir / "test.jsonl").exists() or (
        config.dataset_dir / "test.jsonl"
    ).is_symlink():
        raise DatasetError("test.jsonl must not sit beside fit/validation data")
    path = config.dataset_dir / "manifest.json"
    if file_sha256(path) != config.dataset_manifest_sha256:
        raise DatasetError("dataset manifest sha256 differs from explicit pin")
    manifest = DatasetBuildManifest.model_validate_json(path.read_bytes())
    if manifest.builder_version != BUILDER_VERSION:
        raise DatasetError("unsupported dataset builder version")
    if manifest.system_prompt_sha256 != hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest():
        raise DatasetError("dataset system prompt differs")
    if manifest.dataset.taxonomy_sha256 != _taxonomy_digest(load_taxonomy()):
        raise DatasetError("dataset taxonomy differs")
    if manifest.exclusion is None:
        raise DatasetError("training requires the filtered dataset and exclusion audit")
    exclusions = load_exclusions(config.exclusions)
    verify_source(exclusions, manifest, allow_applied=True)
    for name, expected in (
        ("train.jsonl", manifest.train_sha256),
        ("valid.jsonl", manifest.valid_sha256),
    ):
        if file_sha256(config.dataset_dir / name) != expected:
            raise DatasetError(f"{name}: sha256 differs from dataset manifest")
    if not selected_rows_only and (
        max(manifest.fit_lengths.max, manifest.validation_lengths.max) > config.max_seq_length
    ):
        raise DatasetError("dataset exceeds configured sequence cap; truncation is forbidden")
    if min(manifest.fit_rows_after, manifest.validation_rows_after) < 1:
        raise DatasetError("fit and validation must both contain rows")
    if file_sha256(config.model_manifest) != config.model_manifest_sha256:
        raise DatasetError("model manifest sha256 differs from explicit pin")
    snapshot = LocalModelManifest.model_validate_json(config.model_manifest.read_bytes())
    if Path(snapshot.local_directory).resolve() != config.model_dir.resolve():
        raise DatasetError("model directory differs from pinned local model manifest")
    identity = model_identity(
        config.model_dir,
        expected_tokenizer=manifest.tokenizer_id,
        snapshot=snapshot,
        tokenizer_context=tokenizer_context,
    )
    if tokenizer_context is not None:
        tokenizer_context.assert_current(config.model_dir)
    return {
        "dataset_manifest": manifest.model_dump(mode="json"),
        "exclusions_sha256": file_sha256(config.exclusions),
        "exclusion_audit": exclusions.audit().model_dump(mode="json"),
        **identity,
    }


def bounded_rows(
    config: TrainingConfig,
    tokenizer_context: ChatTokenizerContext | None = None,
) -> tuple[list[Row], list[Row]]:
    """First fit rows and a fixed validation diagnostic; never choose using validation targets."""
    owns_context = tokenizer_context is None
    context = tokenizer_context or open_chat_tokenizer(config.model_dir)
    rows: list[list[Row]] = []
    try:
        context.assert_current(config.model_dir)
        counter = context.count
        for name, limit in (("train.jsonl", 50), ("valid.jsonl", 1)):
            selected = []
            with (config.dataset_dir / name).open() as handle:
                for _ in range(limit):
                    line = handle.readline()
                    if not line:
                        break
                    row = cast(Row, json.loads(line))
                    if not 1 < counter(row["messages"]) <= config.max_seq_length:
                        raise DatasetError(
                            f"{name}: row exceeds sequence cap or has no training tokens"
                        )
                    selected.append(row)
            if not selected:
                raise DatasetError(f"{name}: empty training input")
            rows.append(selected)
    finally:
        try:
            context.assert_current(config.model_dir)
        finally:
            if owns_context:
                context.release()
    return rows[0], rows[1]


def exclude_padding_loss(model: Any, batch: Any, lengths: Any) -> tuple[Any, Any]:
    """Upstream default_loss with real target positions strictly below row length."""
    import mlx.core as mx
    import mlx.nn as nn

    inputs = batch[:, :-1]
    targets = batch[:, 1:]
    logits = model(inputs)
    steps = mx.arange(1, targets.shape[1] + 1)
    mask = mx.logical_and(steps >= lengths[:, 0:1], steps < lengths[:, 1:])
    ce = nn.losses.cross_entropy(logits, targets) * mask
    ntoks = mask.sum()
    return ce.astype(mx.float32).sum() / ntoks, ntoks


def validate_supervision(tokens: Sequence[int], offset: int, max_seq_length: int) -> None:
    if len(tokens) > max_seq_length or not 0 <= offset < len(tokens) - 1:
        raise DatasetError("upstream tokenization exceeds sequence cap or masks all targets")


@dataclass(frozen=True)
class FitControl:
    """Explicit diagnostic hooks; the default fit boundary retains smoke bounds."""

    validate_options: Callable[[Any], None]
    validation: Callable[[Any, Any], tuple[float, int, list[ValidationRowDiagnostic]]]
    batches: Callable[..., Any]
    record: Callable[[str, dict[str, Any]], None]
    validation_steps: tuple[int, ...]
    checkpoint_steps: tuple[int, ...] = ()


def save_checkpoint(model: Any, path: Path) -> None:
    """Retain an explicit diagnostic checkpoint without changing final-save semantics."""
    import mlx.core as mx
    from mlx.utils import tree_flatten

    if path.exists() or path.is_symlink():
        raise DatasetError(f"{path}: checkpoint already exists")
    mx.save_safetensors(str(path), dict(tree_flatten(model.trainable_parameters())))


def fit(
    args: Any,
    model: Any,
    train_set: Any,
    valid_set: Any,
    *,
    loss: Any,
    control: FitControl | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """The bounded LoRA/AdamW setup, using the public trainer's loss hook."""
    import mlx.core as mx
    import mlx.optimizers as optim
    from mlx_lm.tuner.callbacks import TrainingCallback
    from mlx_lm.tuner.datasets import CacheDataset
    from mlx_lm.tuner.trainer import TrainingArgs, train
    from mlx_lm.tuner.utils import linear_to_lora_layers
    from mlx_lm.utils import save_config

    train_set = train_set if isinstance(train_set, CacheDataset) else CacheDataset(train_set)
    valid_set = valid_set if isinstance(valid_set, CacheDataset) else CacheDataset(valid_set)
    # Validate before optimizer construction, including callers bypassing train_local.
    for dataset in (train_set, valid_set):
        if not len(dataset):
            raise DatasetError("empty training or validation dataset")
        for i in range(len(dataset)):
            tokens, offset = dataset[i]
            validate_supervision(tokens, offset, args.max_seq_length)
    if (
        args.fine_tune_type != "lora"
        or args.optimizer != "adamw"
        or args.resume_adapter_file is not None
        or args.lr_schedule is not None
        or args.steps_per_report != 1
    ):
        raise DatasetError("fit requires bounded LoRA/AdamW, no resume/schedule, per-step reports")
    if control is None:
        if (
            args.batch_size != 1
            or not 1 <= args.iters <= 20
            or args.learning_rate != 1e-4
            or args.seed != 17
            or args.grad_accumulation_steps != 1
        ):
            raise DatasetError("direct fit requires smoke batch1, <=20 updates, LR1e-4, seed17")
    else:
        control.validate_options(args)
    reports: dict[str, list[dict[str, Any]]] = {"train": [], "validation": []}

    class FiniteLoss(TrainingCallback):
        # Upstream calls training reports AFTER the optimizer update and BEFORE saving.
        def on_train_loss_report(self, info: dict[str, Any]) -> None:
            if control is not None:
                control.record("train", dict(info))
            if not math.isfinite(info["train_loss"]):
                raise DatasetError("nonfinite train loss; refusing checkpoint")
            reports["train"].append(dict(info))
            if control is not None:
                step = info["iteration"]
                if step in control.validation_steps:
                    record_validation(step)
                if step in control.checkpoint_steps:
                    save_checkpoint(
                        model, Path(args.adapter_path) / f"{step:07d}_adapters.safetensors"
                    )

        def on_val_loss_report(self, info: dict[str, Any]) -> None:
            if control is not None:
                control.record("validation", dict(info))
            if not math.isfinite(info["val_loss"]):
                raise DatasetError("nonfinite validation loss; refusing checkpoint")
            reports["validation"].append(dict(info))

    callback = FiniteLoss()

    def record_validation(step: int) -> None:
        assert control is not None
        value, rows, row_diagnostics = control.validation(model, valid_set)
        for record in row_diagnostics:
            record["iteration"] = step
        callback.on_val_loss_report(
            {
                "iteration": step,
                "val_loss": value,
                "rows": rows,
                "row_diagnostics": row_diagnostics,
            }
        )

    mx.random.seed(args.seed)
    model.freeze()
    if not 1 <= args.num_layers <= len(model.layers):
        raise DatasetError("requested LoRA layer count exceeds model layers")
    linear_to_lora_layers(model, args.num_layers, args.lora_parameters, use_dora=False)
    adapter_path = Path(args.adapter_path)
    adapter_path.mkdir(parents=True, exist_ok=True)
    adapter_file = adapter_path / "adapters.safetensors"
    save_config(vars(args).copy(), adapter_path / "adapter_config.json")
    training_args = TrainingArgs(
        batch_size=args.batch_size,
        iters=args.iters,
        val_batches=args.val_batches,
        steps_per_report=args.steps_per_report,
        steps_per_eval=args.steps_per_eval,
        steps_per_save=args.save_every,
        adapter_file=str(adapter_file),
        max_seq_length=args.max_seq_length,
        grad_checkpoint=args.grad_checkpoint,
        grad_accumulation_steps=args.grad_accumulation_steps,
    )
    optimizer = optim.AdamW(learning_rate=args.learning_rate, **args.optimizer_config["adamw"])
    if control is not None:
        record_validation(0)
    train(
        model=model,
        args=training_args,
        optimizer=optimizer,
        train_dataset=train_set,
        val_dataset=valid_set if control is None else None,
        loss=loss,
        training_callback=callback,
        **({"iterate_batches": control.batches} if control is not None else {}),
    )
    return reports


def train_local(
    config: TrainingConfig,
    *,
    selection: tuple[list[Row], list[Row]] | None = None,
    control: FitControl | None = None,
    tokenizer_context: ChatTokenizerContext | None = None,
) -> dict[str, Any]:
    """Child-only upstream trainer; import Metal after all non-training checks."""
    if config.run_dir.resolve().is_relative_to(ROOT):
        raise DatasetError("worker run directory must be outside the checkout")
    # Exclusive marker also refuses direct worker reuse of an existing adapter directory.
    if (config.run_dir / "adapter").exists():
        raise DatasetError("worker adapter directory already exists")
    write_new({config.run_dir / "worker-started.json": config.model_dump_json() + "\n"})
    owns_context = tokenizer_context is None
    context = tokenizer_context or open_chat_tokenizer(config.model_dir)
    try:
        identity = verify_inputs(
            config,
            selected_rows_only=control is not None,
            tokenizer_context=context,
        )
        fit_rows, valid = bounded_rows(config, context) if selection is None else selection
        if control is None:
            write_new(
                {
                    config.run_dir / "worker-verification.json": json.dumps(
                        {
                            "identity": identity,
                            "selection": {
                                "policy": "first 50 fit rows; first validation row",
                                "fit_rows": len(fit_rows),
                                "validation_rows": len(valid),
                                "fit_sha256": digest(fit_rows),
                                "validation_sha256": digest(valid),
                            },
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                }
            )
    finally:
        if owns_context:
            context.release()
    del context
    check_and_record_resources(config.run_dir, stage="worker-readiness", probe=probe_resources)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    import mlx.core as mx
    import numpy as np
    from mlx_lm import load
    from mlx_lm.tuner.datasets import ChatDataset

    mx.set_cache_limit(CACHE_LIMIT_BYTES)
    mx.set_memory_limit(TRAIN_ALLOCATION_BYTES)
    mx.reset_peak_memory()
    np.random.seed(config.seed)
    try:
        loaded = load(
            str(config.model_dir.resolve()),
            tokenizer_config={"trust_remote_code": False, "local_files_only": True},
        )
        model: Any = loaded[0]
        tokenizer: Any = loaded[1]
        del loaded
        # Upstream ChatDataset annotates chat rows as str values; its runtime expects messages.
        train_set: Any = ChatDataset(cast(Any, fit_rows), tokenizer, mask_prompt=True)
        valid_set: Any = ChatDataset(cast(Any, valid), tokenizer, mask_prompt=True)
        # Check the exact upstream tokenization before its batcher can truncate anything.
        for dataset in (train_set, valid_set):
            for i in range(len(dataset)):
                tokens, offset = dataset.process(dataset[i])
                validate_supervision(tokens, offset, config.max_seq_length)
        reports = fit(
            SimpleNamespace(**training_options(config)),
            model,
            train_set,
            valid_set,
            loss=exclude_padding_loss,
            **({"control": control} if control is not None else {}),
        )
        if control is not None:
            write_new({config.run_dir / "metrics.json": json.dumps(reports, indent=2) + "\n"})
            if mx.get_peak_memory() > TRAIN_ALLOCATION_BYTES:
                raise DatasetError("MLX peak exceeds training allocation")
        return {
            "peak_mlx_bytes": mx.get_peak_memory(),
            "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        }
    finally:
        write_new(
            {
                config.run_dir / "resources.json": json.dumps(
                    {
                        "peak_mlx_bytes": mx.get_peak_memory(),
                        "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                    }
                )
                + "\n"
            }
        )


def launch_trainer(config: TrainingConfig, *, script: Path = Path(__file__)) -> dict[str, Any]:
    """A supervised child bounds verification, model load and training together."""
    write_new(
        {
            config.run_dir / "supervisor-started.json": json.dumps(
                {"pid": os.getpid(), "started_at": datetime.now(UTC).isoformat()}
            )
            + "\n"
        }
    )
    with (config.run_dir / "trainer.log").open("x") as log:
        child = subprocess.Popen(
            [
                sys.executable,
                str(script.resolve()),
                "--config",
                str(config.run_dir / "config.json"),
                "--worker",
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            env={
                **os.environ,
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "PYTHONPATH": os.pathsep.join(
                    [
                        str(ROOT / "training"),
                        str(ROOT / "eval"),
                        str(ROOT / "scripts"),
                        str(ROOT / "packages/core/src"),
                        str(ROOT / "packages/ingest/src"),
                        os.environ.get("PYTHONPATH", ""),
                    ]
                ).rstrip(os.pathsep),
            },
        )
        previous_handlers: dict[int, Any] = {}

        def forward_signal(signum: int, frame: object) -> None:
            child.send_signal(signum)
            raise InterruptedError(f"supervised worker interrupted by signal {signum}")

        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[signum] = signal.signal(signum, forward_signal)
            return_code = child.wait(timeout=config.timeout_seconds)
        except BaseException:
            # Repeated Ctrl-C/termination must not interrupt the owned child's reap.
            for signum in (signal.SIGINT, signal.SIGTERM):
                signal.signal(signum, signal.SIG_IGN)
            try:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            finally:
                for signal_number, handler in previous_handlers.items():
                    signal.signal(signal_number, handler)
            raise
        finally:
            for signal_number, handler in previous_handlers.items():
                signal.signal(signal_number, handler)
        if return_code:
            raise DatasetError(f"trainer exit {return_code}; see trainer.log")
    return cast(dict[str, Any], json.loads((config.run_dir / "resources.json").read_text()))


class WorkerIdentity(Contract):
    """Cheap structural contract for the evidence returned by verify_inputs."""

    dataset_manifest: DatasetBuildManifest
    exclusions_sha256: Sha256
    exclusion_audit: ExclusionAudit
    model_sha256: Sha256
    artifact_sha256: dict[str, Sha256] = Field(min_length=1)
    tokenizer_id: Sha256
    model_repository: Literal["mlx-community/Qwen2.5-7B-Instruct-4bit"]
    model_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    model_manifest_sha256: Sha256


def validate_worker_identity(value: object, config: TrainingConfig) -> WorkerIdentity:
    # JSON-mode preserves the existing nested contracts' date/tuple wire format.
    identity = WorkerIdentity.model_validate_json(json.dumps(value))
    manifest_bytes = (config.dataset_dir / "manifest.json").read_bytes()
    model_bytes = config.model_manifest.read_bytes()
    manifest = DatasetBuildManifest.model_validate_json(manifest_bytes)
    model = LocalModelManifest.model_validate_json(model_bytes)
    if (
        hashlib.sha256(manifest_bytes).hexdigest() != config.dataset_manifest_sha256
        or hashlib.sha256(model_bytes).hexdigest() != config.model_manifest_sha256
        or identity.dataset_manifest != manifest
        or manifest.exclusion is None
        or identity.exclusion_audit != manifest.exclusion
        or identity.exclusions_sha256 != file_sha256(config.exclusions)
        or identity.tokenizer_id != manifest.tokenizer_id
        or identity.artifact_sha256 != model.artifact_sha256
        or identity.model_sha256 != digest(model.artifact_sha256)
        or identity.model_repository != model.repository
        or identity.model_revision != model.revision
        or identity.model_manifest_sha256 != digest(model.model_dump(mode="json"))
    ):
        raise DatasetError("worker verification identity differs from pinned metadata")
    return identity


class SmokeSelection(Contract):
    policy: Literal["first 50 fit rows; first validation row"]
    fit_rows: int = Field(ge=1, le=50)
    validation_rows: int = Field(ge=1, le=1)
    fit_sha256: Sha256
    validation_sha256: Sha256


def _read_worker_verification(path: Path, config: TrainingConfig) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
        if not isinstance(value, dict) or set(value) != {"identity", "selection"}:
            raise DatasetError("invalid object shape")
        identity = validate_worker_identity(value["identity"], config)
        selection = SmokeSelection.model_validate(value["selection"])
        if selection.fit_rows != min(50, identity.dataset_manifest.fit_rows_after):
            raise DatasetError("smoke selection count differs from the pinned manifest")
    except (OSError, ValueError) as exc:
        raise DatasetError(f"worker verification is missing or invalid: {exc}") from exc
    return value


def _read_resource_samples(path: Path, errors: list[str]) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    required = {"stage", "pid", "ppid", "sampled_at", "required_memory_bytes"}
    try:
        with path.open("rb") as handle:
            for number, line in enumerate(handle, 1):
                try:
                    value = json.loads(line.decode("utf-8"))
                    if (
                        not isinstance(value, dict)
                        or not required.issubset(value)
                        or not isinstance(value["stage"], str)
                        or not isinstance(value["pid"], int)
                        or not isinstance(value["ppid"], int)
                    ):
                        raise ValueError("invalid resource sample object")
                    samples.append(value)
                except ValueError as exc:
                    errors.append(f"resource-samples.jsonl line {number}: {exc}")
    except OSError as exc:
        errors.append(f"resource-samples.jsonl: {exc}")
    return samples


def run(
    config: SmokeConfig,
    *,
    dry_run: bool,
    probe: Callable[[], Resources] = probe_resources,
    trainer: Callable[[SmokeConfig], object] = launch_trainer,
    verify_in_controller: bool = False,
) -> int:
    """Run preflight; verify in-process only for dry runs or explicit test requests."""
    config = SmokeConfig.model_validate(config)
    run_dir = config.run_dir.resolve()
    if (
        run_dir.is_relative_to(ROOT)
        or config.run_dir.is_symlink()
        or any((parent / ".git").exists() for parent in run_dir.parents)
    ):
        raise DatasetError("run directory must be private, outside the Git checkout")
    # Normalize paths before saving the config for the child.
    config = config.model_copy(
        update={
            name: getattr(config, name).resolve()
            for name in ("dataset_dir", "exclusions", "model_dir", "model_manifest", "run_dir")
        }
    )
    try:
        run_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise DatasetError("run directory already exists; refusing reuse") from exc
    started = time.monotonic()
    report: dict[str, Any] = {
        "status": "failed",
        "started_at": datetime.now(UTC).isoformat(),
        "config": config.model_dump(mode="json"),
        "training_options": training_options(config),
        "command": sys.argv,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "implementation_sha256": file_sha256(Path(__file__)),
        "trained": False,
        "artifact_errors": [],
    }
    write_new({run_dir / "config.json": config.model_dump_json(indent=2) + "\n"})
    try:
        report["environment"] = {
            name: version(name) for name in ("mlx", "mlx-lm", "transformers", "tokenizers", "numpy")
        }
        if report["environment"]["mlx-lm"] != "0.31.3":
            raise DatasetError("training API requires inspected mlx-lm 0.31.3")
        state = check_and_record_resources(run_dir, stage="controller-readiness", probe=probe)
        report["preflight"] = state.model_dump(mode="json")
        if dry_run or verify_in_controller:
            context = open_chat_tokenizer(config.model_dir)
            try:
                report["identity"] = verify_inputs(config, tokenizer_context=context)
                fit, valid = bounded_rows(config, context)
            finally:
                context.release()
            report["selection"] = {
                "policy": "first 50 fit rows; first validation row",
                "fit_rows": len(fit),
                "validation_rows": len(valid),
                "fit_sha256": digest(fit),
                "validation_sha256": digest(valid),
            }
        if dry_run:
            report["status"] = "preflight_passed"
        else:
            report["resources"] = trainer(config)
            report["trained"] = True
            report["status"] = "smoke_completed"
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        print(f"error: {exc}", file=sys.stderr)
    finally:
        if not dry_run and not verify_in_controller:
            try:
                verification = _read_worker_verification(
                    run_dir / "worker-verification.json", config
                )
                report.update(verification)
                report["verification_status"] = "verified_in_worker"
            except (OSError, ValueError) as exc:
                report["artifact_errors"].append(f"worker-verification.json: {exc}")
                report["verification_status"] = "worker_verification_invalid"
                report.setdefault("error", f"{type(exc).__name__}: {exc}")
                report["trained"] = False
        counters = run_dir / "resources.json"
        if counters.is_file():
            try:
                resources = json.loads(counters.read_text())
                if not isinstance(resources, dict) or not {
                    "peak_mlx_bytes",
                    "peak_rss_bytes",
                }.issubset(resources):
                    raise ValueError("resources.json has an invalid object shape")
                report["resources"] = resources
            except (OSError, ValueError) as exc:
                report["artifact_errors"].append(f"resources.json: {exc}")
        else:
            report.setdefault("resources", {"peak_mlx_bytes": None, "peak_rss_bytes": None})
        samples = run_dir / "resource-samples.jsonl"
        if samples.is_file():
            parsed = _read_resource_samples(samples, report["artifact_errors"])
            report["resource_samples"] = parsed
            if "preflight" not in report:
                controller_samples = [
                    sample for sample in parsed if sample.get("stage") == "controller-readiness"
                ]
                if controller_samples:
                    report["preflight"] = controller_samples[0]
        if report["artifact_errors"]:
            report["status"] = "failed"
        report["elapsed_seconds"] = time.monotonic() - started
        report["finished_at"] = datetime.now(UTC).isoformat()
        report["peak_parent_rss_bytes"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        write_new({run_dir / "report.json": json.dumps(report, indent=2) + "\n"})
    return int(report["status"] == "failed")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        config = SmokeConfig.model_validate_json(args.config.read_bytes())
        if args.worker:
            if args.config.resolve() != config.run_dir.resolve() / "config.json":
                raise DatasetError("worker requires the saved run config")
            marker = json.loads((config.run_dir / "supervisor-started.json").read_bytes())
            if marker.get("pid") != os.getppid():
                raise DatasetError("worker was not started by its registered supervisor")

            def deadline(signum: int, frame: object) -> None:
                raise TimeoutError("training runtime bound exceeded")

            signal.signal(signal.SIGALRM, deadline)
            signal.alarm(config.timeout_seconds)
            try:
                train_local(config)
            finally:
                signal.alarm(0)
            return 0
        return run(config, dry_run=args.dry_run)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
