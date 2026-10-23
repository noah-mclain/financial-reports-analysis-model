"""Bounded offline normalization QLoRA smoke; no holdout loading or quality gate scoring."""

from __future__ import annotations

import argparse
import hashlib
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
from typing import Any, Literal, cast

from chat_tokenizer import load_chat_counter
from dataset_builder import BUILDER_VERSION, SYSTEM_PROMPT, DatasetBuildManifest, Row
from dataset_contracts import Contract, DatasetError, Sha256, _taxonomy_digest, digest
from dataset_exclusions import load_exclusions, verify_source
from exclusive_write import write_new
from normalization_mlx import LocalModelManifest, model_identity
from normalization_validation import file_sha256
from pydantic import Field

from fra_core.taxonomy import load_taxonomy

ROOT = Path(__file__).resolve().parents[1]
# 01-constraints 1.3: 11.5 GB training allocation plus 2.5 GB headroom.
MIN_MEMORY_BYTES = 14_000_000_000
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
    available_memory_bytes: int = Field(ge=0)
    services: tuple[str, ...]

    def check(self, minimum_memory_bytes: int = MIN_MEMORY_BYTES) -> None:
        if minimum_memory_bytes <= 0:
            raise ValueError("minimum memory budget must be positive")
        if self.free_disk_bytes < MIN_DISK_BYTES:
            raise DatasetError("preflight requires at least 15 GB free disk")
        if self.available_memory_bytes < minimum_memory_bytes:
            if minimum_memory_bytes == MIN_MEMORY_BYTES:
                raise DatasetError(
                    "preflight requires 14 GB available memory (11.5 GB plus 2.5 GB)"
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
    return Resources(
        free_disk_bytes=shutil.disk_usage(ROOT).free,
        available_memory_bytes=memory * int(match[1]) // 100,
        services=services,
    )


def training_options(config: TrainingConfig) -> dict[str, Any]:
    """Explicit bounded LoRA/AdamW options, with no dataset loader or test scoring."""
    return {
        "model": str(config.model_dir.resolve()),
        "train": True,
        "test": False,
        "fine_tune_type": "lora",
        "optimizer": "adamw",
        "optimizer_config": {"adamw": {}},
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


def verify_inputs(config: TrainingConfig, *, selected_rows_only: bool = False) -> dict[str, Any]:
    """Trust pinned builder artifacts, without rebuilding partitions or opening source labels."""
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
        config.model_dir, expected_tokenizer=manifest.tokenizer_id, snapshot=snapshot
    )
    return {
        "dataset_manifest": manifest.model_dump(mode="json"),
        "exclusions_sha256": file_sha256(config.exclusions),
        "exclusion_audit": exclusions.audit().model_dump(mode="json"),
        **identity,
    }


def bounded_rows(config: TrainingConfig) -> tuple[list[Row], list[Row]]:
    """First fit rows and a fixed validation diagnostic; never choose using validation targets."""
    counter, _ = load_chat_counter(config.model_dir)
    rows: list[list[Row]] = []
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
    validation: Callable[[Any, Any], tuple[float, int]]
    batches: Callable[..., Any]
    record: Callable[[str, dict[str, Any]], None]
    validation_steps: tuple[int, ...]


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

        def on_val_loss_report(self, info: dict[str, Any]) -> None:
            if control is not None:
                control.record("validation", dict(info))
            if not math.isfinite(info["val_loss"]):
                raise DatasetError("nonfinite validation loss; refusing checkpoint")
            reports["validation"].append(dict(info))

    callback = FiniteLoss()

    def record_validation(step: int) -> None:
        assert control is not None
        value, rows = control.validation(model, valid_set)
        callback.on_val_loss_report({"iteration": step, "val_loss": value, "rows": rows})

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
) -> dict[str, Any]:
    """Child-only upstream trainer; import Metal after all non-training checks."""
    if config.run_dir.resolve().is_relative_to(ROOT):
        raise DatasetError("worker run directory must be outside the checkout")
    # Exclusive marker also refuses direct worker reuse of an existing adapter directory.
    if (config.run_dir / "adapter").exists():
        raise DatasetError("worker adapter directory already exists")
    write_new({config.run_dir / "worker-started.json": config.model_dump_json() + "\n"})
    probe_resources().check()
    verify_inputs(config, selected_rows_only=control is not None)
    fit_rows, valid = bounded_rows(config) if selection is None else selection
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
    """A child process gives the entire model load/train phase a wall-clock deadline."""
    with (config.run_dir / "trainer.log").open("x") as log:
        result = subprocess.run(
            [
                sys.executable,
                str(script.resolve()),
                "--config",
                str(config.run_dir / "config.json"),
                "--worker",
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=config.timeout_seconds,
            check=False,
            env={**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"},
        )
    if result.returncode:
        raise DatasetError(f"trainer exit {result.returncode}; see trainer.log")
    return cast(dict[str, Any], json.loads((config.run_dir / "resources.json").read_text()))


def run(
    config: SmokeConfig,
    *,
    dry_run: bool,
    probe: Callable[[], Resources] = probe_resources,
    trainer: Callable[[SmokeConfig], object] = launch_trainer,
) -> int:
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
    }
    write_new({run_dir / "config.json": config.model_dump_json(indent=2) + "\n"})
    try:
        report["environment"] = {
            name: version(name) for name in ("mlx", "mlx-lm", "transformers", "tokenizers", "numpy")
        }
        if report["environment"]["mlx-lm"] != "0.31.3":
            raise DatasetError("training API requires inspected mlx-lm 0.31.3")
        state = probe()
        report["preflight"] = state.model_dump(mode="json")
        state.check()
        report["identity"] = verify_inputs(config)
        fit, valid = bounded_rows(config)
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
        counters = run_dir / "resources.json"
        if counters.is_file():
            report["resources"] = json.loads(counters.read_text())
        else:
            report.setdefault("resources", {"peak_mlx_bytes": None, "peak_rss_bytes": None})
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
