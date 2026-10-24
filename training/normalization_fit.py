"""One preregistered bounded normalization fit; a candidate, never a promotion."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import platform
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path
from typing import Any, Literal

import normalization_stability as stability
import normalization_train as smoke
from chat_tokenizer import ChatTokenizerContext, open_chat_tokenizer
from dataset_builder import Row
from dataset_contracts import DatasetError, Sha256
from exclusive_write import write_new
from normalization_validation import file_sha256
from pydantic import Field, model_validator


class FitConfig(smoke.TrainingConfig):
    exclusions_sha256: Sha256
    iters: int = Field(default=1000, ge=1, le=1500, strict=True)
    batch_size: Literal[4] = 4
    grad_checkpoint: Literal[True] = True
    num_layers: Literal[16] = 16
    max_seq_length: Literal[2048] = 2048
    seed: Literal[17] = 17
    timeout_seconds: int = Field(default=25000, ge=1, le=37500, strict=True)
    preregistration: Path | None = None
    preregistration_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def bounded_timeout(self) -> FitConfig:
        if self.timeout_seconds < self.iters * 25:
            raise ValueError("timeout_seconds must allow at least iters * 25 seconds")
        if (self.preregistration is None) != (self.preregistration_sha256 is None):
            raise ValueError("preregistration path and sha256 must be supplied together")
        return self

    @property
    def learning_rate(self) -> float:
        return 1e-5

    @property
    def adamw_bias_correction(self) -> bool:
        return True

    @property
    def steps_per_eval(self) -> int:
        return 50

    @property
    def val_batches(self) -> int:
        return stability.VALID_ROWS


def validation_steps(config: FitConfig) -> tuple[int, ...]:
    return (*range(0, config.iters, config.steps_per_eval), config.iters)


def inputs(config: FitConfig) -> dict[str, Any]:
    return config.model_dump(
        mode="json", exclude={"run_dir", "preregistration", "preregistration_sha256"}
    )


def design(config: FitConfig) -> dict[str, Any]:
    return {
        "learning_rate": config.learning_rate,
        "lr_schedule": None,
        "bias_correction": config.adamw_bias_correction,
        "batch_size": config.batch_size,
        "updates": config.iters,
        "validation_steps": list(validation_steps(config)),
        "validation_rows": config.val_batches,
        "validation_aggregation": "arithmetic mean of 64 singleton row losses with provenance",
        "fit_selection": "diagnostic stride and cap, selected index order; no shuffle or repeat",
        "validation_selection": stability.SELECTION_POLICY,
        "checkpoint_steps": list(range(100, config.iters, 100)),
        "save_every": config.iters,
        "candidate": f"final update-{config.iters} adapter; no best-checkpoint selection",
        "abort": {
            "nonfinite": "any loss at any step",
            "every_validation_ratio": 1.25,
            "final_validation_ratio": 1.10,
            "timeout_seconds": config.timeout_seconds,
            "peak_mlx_bytes": smoke.TRAIN_ALLOCATION_BYTES,
            "minimum_memory_bytes": smoke.MIN_MEMORY_BYTES,
            "cache_limit_bytes": smoke.CACHE_LIMIT_BYTES,
            "drift": "source, identity, dataset, exclusions, model or supervised-token budget",
            "attempts": "one; no retry or resume; preserve failed artifacts",
        },
    }


def implementations() -> dict[str, str]:
    """Hash repository import dependencies, without importing training backends."""
    root = smoke.ROOT
    search = [root / name for name in ("training", "scripts", "eval")]
    search.extend(sorted(root.glob("packages/*/src")))
    pending = [Path(__file__).resolve()]
    seen: set[Path] = set()
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        for node in ast.walk(ast.parse(path.read_bytes())):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    # Relative imports resolve against the importing package directory.
                    base = path.parent
                    for _ in range(node.level - 1):
                        base = base.parent
                    suffix = (node.module or "").replace(".", "/")
                    candidates = [base / suffix]
                    candidates.extend(base / suffix / alias.name for alias in node.names)
                    for candidate in candidates:
                        pending.extend(
                            p
                            for p in (candidate.with_suffix(".py"), candidate / "__init__.py")
                            if p.is_file()
                        )
                else:
                    names = [node.module] if node.module else []
                    names.extend(
                        f"{node.module}.{alias.name}" for alias in node.names if node.module
                    )
            for name in names:
                parts = name.split(".")
                for directory in search:
                    for length in range(1, len(parts) + 1):
                        candidate = directory.joinpath(*parts[:length])
                        pending.extend(
                            p
                            for p in (candidate.with_suffix(".py"), candidate / "__init__.py")
                            if p.is_file()
                        )
    seen.add(root / "packages/core/src/fra_core/taxonomy/canonical_items.yaml")
    return {path.relative_to(root).as_posix(): file_sha256(path) for path in sorted(seen)}


def identities(
    config: FitConfig, tokenizer_context: ChatTokenizerContext | None = None
) -> dict[str, Any]:
    if (
        config.dataset_manifest_sha256 != stability.DATASET_PIN
        or config.exclusions_sha256 != stability.EXCLUSIONS_PIN
        or file_sha256(config.exclusions) != config.exclusions_sha256
    ):
        raise DatasetError("dataset/exclusions hashes differ from frozen diagnostic inputs")
    return smoke.verify_inputs(config, selected_rows_only=True, tokenizer_context=tokenizer_context)


def record_for(
    config: FitConfig, context: ChatTokenizerContext
) -> tuple[dict[str, Any], tuple[list[Row], list[Row]]]:
    identity = identities(config, context)
    selection, rows = stability.selections(
        config, context, fit_rows=config.iters * config.batch_size, permute_fit=False
    )
    budget = stability.supervised_budget(config, rows, tokenizer_context=context, frozen_pair=False)
    return {
        "schema": 1,
        "inputs": inputs(config),
        "design": design(config),
        "identity": identity,
        "selection": selection,
        "budget": budget,
        "implementation": implementations(),
        "environment": {
            name: version(name) for name in ("mlx", "mlx-lm", "transformers", "tokenizers", "numpy")
        },
    }, rows


def preregister(plan_path: Path, out: Path) -> None:
    out = stability.private_path(out)
    if out.name in {"fit.json", "fit-dry.json"}:
        raise DatasetError("preregistration path conflicts with a generated config")
    template = FitConfig.model_validate_json(plan_path.read_bytes())
    if template.preregistration is not None:
        raise DatasetError("plan must be unregistered; never repin an existing attempt")
    context = open_chat_tokenizer(template.model_dir)
    try:
        record, _ = record_for(template, context)
    finally:
        context.release()
    text = json.dumps(record, indent=2, allow_nan=False) + "\n"
    sha = hashlib.sha256(text.encode()).hexdigest()
    files = {out: text}
    for dry in (False, True):
        config = FitConfig.model_validate(
            {
                **template.model_dump(),
                "run_dir": out.parent / ("dry" if dry else "runs") / "fit",
                "preregistration": out,
                "preregistration_sha256": sha,
            }
        )
        files[out.parent / ("fit-dry.json" if dry else "fit.json")] = (
            config.model_dump_json(indent=2) + "\n"
        )
    write_new(files)
    for path in files:
        path.chmod(0o444)


def registration(config: FitConfig) -> dict[str, Any]:
    if config.preregistration is None or config.preregistration_sha256 is None:
        raise DatasetError("run requires a digest-bound preregistration")
    path = stability.private_path(config.preregistration)
    if file_sha256(path) != config.preregistration_sha256:
        raise DatasetError("preregistration sha256 changed; never rewrite or repin")
    record: dict[str, Any] = json.loads(path.read_bytes())
    if (
        record.get("schema") != 1
        or record.get("inputs") != inputs(config)
        or record.get("design") != design(config)
        or record.get("implementation") != implementations()
    ):
        raise DatasetError("preregistered inputs/design/implementation changed")
    return record


def verify_preregistration(
    config: FitConfig, *, tokenizer_context: ChatTokenizerContext | None = None
) -> tuple[dict[str, Any], tuple[list[Row], list[Row]]]:
    registered = registration(config)
    owns_context = tokenizer_context is None
    context = tokenizer_context or open_chat_tokenizer(config.model_dir)
    try:
        current, rows = record_for(config, context)
        if current != registered:
            raise DatasetError("preregistered identity/selection/budget/environment changed")
        context.assert_current(config.model_dir)
        return registered, rows
    finally:
        if owns_context:
            context.release()


def fit_control(
    config: FitConfig, selection: dict[str, Any], validation_rows: list[Row], budget: dict[str, int]
) -> smoke.FitControl:
    started = time.monotonic()
    initial: float | None = None

    def check(kind: str, info: dict[str, Any]) -> None:
        nonlocal initial
        if time.monotonic() - started > config.timeout_seconds:
            raise TimeoutError("fit timeout exceeded")
        if stability.has_nonfinite(info):
            raise DatasetError("nonfinite loss; refusing further updates")
        if kind == "validation":
            value = stability.finite_number(info["val_loss"], positive=info["iteration"] == 0)
            if info["iteration"] == 0:
                initial = value
            if initial is None:
                raise DatasetError("missing step-0 validation")
            if value > 1.25 * initial:
                raise DatasetError("validation loss exceeds 1.25 x step-0")
            if info["iteration"] == config.iters and value > 1.10 * initial:
                raise DatasetError("final validation loss exceeds 1.10 x step-0")
        else:
            if (
                stability.finite_number(info.get("peak_memory"), positive=True) * 1e9
                > smoke.TRAIN_ALLOCATION_BYTES
            ):
                raise DatasetError("MLX peak exceeds training allocation")
            if (
                info["iteration"] == config.iters
                and info.get("trained_tokens") != budget["supervised_tokens"]
            ):
                raise DatasetError("final supervised-token budget mismatch")

    control = stability.fit_control(
        config,
        selection,
        validation_rows,
        validation_steps=validation_steps(config),
        on_record=check,
    )
    return replace(control, checkpoint_steps=tuple(range(100, config.iters, 100)))


def attempt_path(config: FitConfig, *, dry_run: bool) -> Path:
    if config.preregistration is None:
        raise DatasetError("controller attempt requires a preregistration")
    return stability.private_path(
        config.preregistration.parent / "attempts" / f"fit-{'dry' if dry_run else 'train'}.json"
    )


def worker(config: FitConfig) -> None:
    config = FitConfig.model_validate(config)
    run_dir = stability.private_path(config.run_dir)
    try:
        attempt = json.loads(attempt_path(config, dry_run=False).read_bytes())
        saved = FitConfig.model_validate_json((run_dir / "config.json").read_bytes())
        if (
            attempt
            != {"run_dir": str(run_dir), "preregistration_sha256": config.preregistration_sha256}
            or saved != config
            or not (run_dir / "supervisor-started.json").is_file()
        ):
            raise DatasetError("worker differs from its controller attempt")
    except (OSError, ValueError) as exc:
        raise DatasetError(f"worker requires its exclusive controller attempt: {exc}") from exc
    context = open_chat_tokenizer(config.model_dir)
    try:
        record, rows = verify_preregistration(config, tokenizer_context=context)
        write_new(
            {
                config.run_dir / "worker-verification.json": json.dumps(
                    {key: record[key] for key in ("identity", "selection")}
                )
                + "\n"
            }
        )
        control = fit_control(config, record["selection"]["valid"], rows[1], record["budget"])
        smoke.train_local(config, selection=rows, control=control, tokenizer_context=context)
        verify_preregistration(config, tokenizer_context=context)
    finally:
        context.release()


def read_worker_verification(path: Path, config: FitConfig) -> dict[str, Any]:
    verification = stability._read_worker_verification(
        path,
        config,
        fit_rows=config.iters * config.batch_size,
        preregistration=config.preregistration,
        preregistration_sha256=config.preregistration_sha256,
    )
    record = registration(config)
    if record["selection"]["fit"]["permuted"] != record["selection"]["fit"]["selected"]:
        raise DatasetError("fit row order differs from selected indices")
    return verification


def criteria(
    report: dict[str, Any], config: FitConfig
) -> tuple[dict[str, bool], list[dict[str, Any]]]:
    raw_metrics = report.get("metrics", {})
    metrics = raw_metrics if isinstance(raw_metrics, dict) else {}
    train = stability.series(metrics, "train")
    valid = stability.series(metrics, "validation")
    ratios: list[dict[str, Any]] = []
    initial = stability.measured(valid[0].get("val_loss"), positive=True) if valid else None
    if initial is not None:
        for event in valid:
            value = stability.measured(event.get("val_loss"))
            if value is not None:
                ratios.append({"iteration": event.get("iteration"), "ratio": value / initial})
    complete = (
        [event.get("iteration") for event in train] == list(range(1, config.iters + 1))
        and [event.get("iteration") for event in valid] == list(validation_steps(config))
        and all(event.get("rows") == stability.VALID_ROWS for event in valid)
        and len(ratios) == len(valid)
    )
    no_nonfinite = bool(train and valid) and not stability.has_nonfinite(metrics)
    no_nonfinite = no_nonfinite and all(
        stability.measured(event.get("train_loss")) is not None for event in train
    )
    try:
        stability.check_resources(report.get("resources", {}))
        resources_ok = True
    except (ValueError, KeyError, TypeError):
        resources_ok = False
    elapsed = stability.measured(report.get("elapsed_seconds"))
    raw_budget = report.get("budget", {})
    budget = raw_budget if isinstance(raw_budget, dict) else {}
    result = {
        "complete_metrics": complete,
        "no_nonfinite": no_nonfinite,
        "every_validation": complete
        and bool(ratios)
        and all(event["ratio"] <= 1.25 for event in ratios),
        "final_validation": complete and bool(ratios) and ratios[-1]["ratio"] <= 1.10,
        "token_budget": bool(train)
        and train[-1].get("trained_tokens") == budget.get("supervised_tokens")
        and budget.get("examples") == config.iters * config.batch_size
        and budget.get("updates") == config.iters,
        "resources": resources_ok,
        "timeout": elapsed is not None and elapsed <= config.timeout_seconds,
        "identity": report.get("verification_status") == "verified_in_worker",
        "candidate": bool(report.get("candidate")),
        "artifacts": not report.get("artifact_errors", []),
    }
    return result, ratios


def evaluate_report(report: dict[str, Any]) -> dict[str, Any]:
    config = FitConfig.model_validate_json(json.dumps(report["config"]))
    checks, ratios = criteria(report, config)
    return {
        "criteria_pass": report.get("status") == "completed" and all(checks.values()),
        "criteria": checks,
        "validation_ratios": ratios,
        "selection_rule": design(config)["candidate"],
        "generated_quality": stability.NOT_MEASURED,
        "promotion": "not authorized",
    }


def candidate_artifacts(config: FitConfig) -> dict[str, Any]:
    paths = [
        config.run_dir / "adapter" / name
        for name in (
            "adapters.safetensors",
            "adapter_config.json",
            f"{config.iters:07d}_adapters.safetensors",
            *(f"{step:07d}_adapters.safetensors" for step in range(100, config.iters, 100)),
        )
    ]
    hashes = {path.name: file_sha256(path) for path in paths}
    if hashes["adapters.safetensors"] != hashes[f"{config.iters:07d}_adapters.safetensors"]:
        raise DatasetError("final adapter and update-N checkpoint differ")
    return {"path": str(paths[0]), "sha256": hashes["adapters.safetensors"], "artifacts": hashes}


def run(config: FitConfig, *, dry_run: bool) -> int:
    config = FitConfig.model_validate(config)
    run_dir = stability.private_path(config.run_dir)
    try:
        run_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise DatasetError("run directory already exists; refusing reuse") from exc
    started = time.monotonic()
    report: dict[str, Any] = {
        "status": "failed",
        "artifact_errors": [],
        "config": config.model_dump(mode="json"),
        "training_options": smoke.training_options(config),
        "preregistration_sha256": config.preregistration_sha256,
        "command": sys.argv,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "resources": {"peak_mlx_bytes": None, "peak_rss_bytes": None},
    }
    write_new({run_dir / "config.json": config.model_dump_json(indent=2) + "\n"})
    try:
        record = registration(config)
        report["budget"] = record["budget"]
        attempt = attempt_path(config, dry_run=dry_run)
        write_new(
            {
                attempt: json.dumps(
                    {
                        "run_dir": str(run_dir),
                        "preregistration_sha256": config.preregistration_sha256,
                    }
                )
                + "\n"
            }
        )
        report["environment"] = {name: version(name) for name in record["environment"]}
        if (
            report["environment"] != record["environment"]
            or report["environment"]["mlx-lm"] != "0.31.3"
        ):
            raise DatasetError("environment changed or mlx-lm is not inspected 0.31.3")
        if dry_run:
            verified, _ = verify_preregistration(config)
            report.update({key: verified[key] for key in ("identity", "selection")})
        state = smoke.check_and_record_resources(
            run_dir, stage="controller-readiness", probe=smoke.probe_resources
        )
        report["preflight"] = state.model_dump(mode="json")
        if dry_run:
            report["status"] = "preflight_passed"
        else:
            report["resources"] = smoke.launch_trainer(config, script=Path(__file__))
            report["metrics"] = json.loads((run_dir / "metrics.json").read_bytes())
            report["status"] = "completed"
    except (
        OSError,
        ValueError,
        RuntimeError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ) as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        print(f"error: {exc}", file=sys.stderr)
    finally:

        def finish(result: dict[str, Any]) -> None:
            if not dry_run and result["status"] == "completed":
                try:
                    for name in (
                        "events.jsonl",
                        "resources.json",
                        "trainer.log",
                        "resource-samples.jsonl",
                    ):
                        if not (run_dir / name).is_file():
                            raise DatasetError(f"{name}: missing worker artifact")
                    saved = json.loads((run_dir / "metrics.json").read_bytes())
                    if not isinstance(saved, dict):
                        raise DatasetError("metrics.json: invalid metric object")
                    events = result.get("metrics", {})
                    for kind in ("train", "validation"):
                        normalized = [
                            {
                                key: value
                                for key, value in event.items()
                                if key not in ("kind", "nonfinite")
                            }
                            for event in stability.series(events, kind)
                        ]
                        if normalized != saved.get(kind):
                            raise DatasetError(f"{kind}: events and metrics differ")
                    result["candidate"] = candidate_artifacts(config)
                except (OSError, ValueError) as exc:
                    result["artifact_errors"].append(f"adapter: {exc}")
            result.update(evaluate_report(result))
            if not dry_run and not result["criteria_pass"]:
                result["status"] = "failed"
                if "error" not in result:
                    failed = [key for key, value in result["criteria"].items() if not value]
                    result["error"] = f"fit criteria failed: {', '.join(failed)}"
                    print(result["error"], file=sys.stderr)

        stability.finalize_report(
            config,
            report,
            started,
            dry_run=dry_run,
            verification_reader=lambda path, _: read_worker_verification(path, config),
            finish=finish,
        )
    return int(report["status"] == "failed")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--preregister", type=Path, metavar="PLAN")
    action.add_argument("--config", type=Path)
    action.add_argument("--evaluate", type=Path, metavar="REPORT")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if (args.preregister or args.evaluate) and (args.out is None or args.worker or args.dry_run):
        parser.error("preregister/evaluate require --out and cannot train or dry-run")
    if args.config and (args.out or (args.worker and args.dry_run)):
        parser.error("config cannot use --out or combine worker and dry-run")
    try:
        if args.preregister:
            preregister(args.preregister, args.out)
        elif args.evaluate:
            result = evaluate_report(json.loads(args.evaluate.read_bytes()))
            write_new(
                {
                    stability.private_path(args.out): json.dumps(result, indent=2, allow_nan=False)
                    + "\n"
                }
            )
        else:
            config = FitConfig.model_validate_json(args.config.read_bytes())
            if args.worker:
                worker(config)
            else:
                return run(config, dry_run=args.dry_run)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
