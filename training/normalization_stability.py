"""Preregistered offline normalization optimization diagnostic, without quality scoring."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import random
import signal
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from importlib.metadata import version
from pathlib import Path
from typing import Any, Literal, Protocol, cast

import normalization_train as smoke
from dataset_builder import Row
from dataset_contracts import DatasetError, NormalizationAnswer, Sha256, digest
from exclusive_write import write_new
from normalization_validation import file_sha256
from pydantic import Field, model_validator

ARMS = {"A": (1e-4, 1), "B": (2e-5, 1), "C": (2e-5, 4), "D": (1e-5, 4)}
VALIDATION_STEPS = (0, 15, 30, 45, 60)
FIT_ROWS = 240
VALID_ROWS = 64
DATASET_PIN = "9cae5c2febd7e568cb1996150b99bfad7170f5c565dea090010e67b9953843f5"
EXCLUSIONS_PIN = "9d5388129dbdba74dff6bd3fa5724f738b7e0d4f963208cfd4de34941b6c8a7f"
SELECTION_POLICY = (
    "zero-based candidates floor(k*(N-1)/(count-1)), k=0..count-1; "
    "scan forward from each candidate, skip already selected and over-cap rows; "
    "no wrap or backward search; exhaustion fails; count unique over-cap indices; "
    "validate supervision; after selection permute with a fresh random.Random(17) per split"
)
HYPOTHESES = (
    "The smoke spike/validation rise may depend on optimization setup. "
    "Smaller learning rates and larger batches may remove instability on this fixed sample."
)
RULE = (
    "Stable iff no nonfinite; final validation <=1.10*initial; every validation "
    "<=1.25*initial; max train <=0.5*max train A in the same experiment. "
    "A is evaluated literally against itself. If A satisfies the two validation rules, "
    "INCONCLUSIVE about LR/batch, with no winner. Missing/incomplete evidence fails."
)
STOPS = (
    "Stop on quota/auth, pin/hash/identity mismatch, preflight refusal, nonfinite loss, "
    "allocation or 1500-second runtime excess, smoke-bounds regression, protected data need "
    "or scope expansion. Preserve failed runs; never rerun, resume or promote."
)


class DiagnosticInputs(smoke.TrainingConfig):
    arm: Literal["A", "B", "C", "D"]
    exclusions_sha256: Sha256
    iters: Literal[60] = 60
    grad_checkpoint: Literal[True] = True
    num_layers: Literal[16] = 16
    max_seq_length: Literal[2048] = 2048
    seed: Literal[17] = 17
    timeout_seconds: int = Field(default=1500, ge=1, le=1500)

    batch_size: Literal[1, 4] = 1

    @model_validator(mode="before")
    @classmethod
    def fixed_batch(cls, value: Any) -> Any:
        if isinstance(value, dict) and value.get("arm") in ARMS:
            value = dict(value)
            batch = ARMS[value["arm"]][1]
            if value.get("batch_size", batch) != batch:
                raise ValueError("batch_size differs from fixed diagnostic arm")
            value["batch_size"] = batch
        return value

    @property
    def learning_rate(self) -> float:
        return ARMS[self.arm][0]

    @property
    def steps_per_eval(self) -> int:
        return 15

    @property
    def val_batches(self) -> int:
        return VALID_ROWS


class DiagnosticConfig(DiagnosticInputs):
    preregistration: Path
    preregistration_sha256: Sha256


def private_path(path: Path) -> Path:
    resolved = path.resolve()
    if (
        path.is_symlink()
        or resolved.is_relative_to(smoke.ROOT)
        or any((parent / ".git").exists() for parent in (resolved, *resolved.parents))
    ):
        raise DatasetError("diagnostic artifacts must be private, outside Git and not symlinks")
    return resolved


def encoder(model_dir: Path) -> Callable[[Row], tuple[list[int], int]]:
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(
        model_dir, local_files_only=True, trust_remote_code=False
    )

    def encode(row: Row) -> tuple[list[int], int]:
        messages = row["messages"]
        if not messages or messages[-1]["role"] != "assistant":
            raise DatasetError("selected chat row must end with assistant supervision")
        tokens = tokenizer.apply_chat_template(messages, return_dict=False)
        offset = len(
            tokenizer.apply_chat_template(
                messages[:-1], add_generation_prompt=True, return_dict=False
            )
        )
        return cast(list[int], tokens), offset

    return encode


class RowSource(Protocol):
    def __len__(self) -> int: ...
    def __getitem__(self, index: int) -> Row: ...


class JsonlRows:
    """Index byte offsets, decoding only candidates rather than the entire fit corpus."""

    def __init__(self, path: Path) -> None:
        if path.name not in ("train.jsonl", "valid.jsonl"):
            raise DatasetError("only fit and validation data contents are permitted")
        self.path = path
        self.offsets: list[int] = []
        with path.open("rb") as handle:
            while True:
                offset = handle.tell()
                if not handle.readline():
                    break
                self.offsets.append(offset)

    def __len__(self) -> int:
        return len(self.offsets)

    def __getitem__(self, index: int) -> Row:
        with self.path.open("rb") as handle:
            handle.seek(self.offsets[index])
            return cast(Row, json.loads(handle.readline()))


def read_rows(path: Path) -> JsonlRows:
    return JsonlRows(path)


def select_rows(
    rows: RowSource,
    count: int,
    encode: Callable[[Row], tuple[list[int], int]],
    cap: int,
) -> dict[str, Any]:
    if cap < 2:
        raise DatasetError("selection cap must allow supervised tokens")
    if count < 2 or len(rows) < count:
        raise DatasetError(f"selection requires at least {max(2, count)} rows")
    candidates = [k * (len(rows) - 1) // (count - 1) for k in range(count)]
    selected: list[int] = []
    used: set[int] = set()
    over_cap: set[int] = set()
    collisions = 0
    for candidate in candidates:
        index = candidate
        while index < len(rows):
            if index in used:
                collisions += 1
            else:
                tokens, offset = encode(rows[index])
                if len(tokens) > cap:
                    over_cap.add(index)
                else:
                    smoke.validate_supervision(tokens, offset, cap)
                    selected.append(index)
                    used.add(index)
                    break
            index += 1
        else:
            raise DatasetError(f"selection exhausted at candidate {candidate}; no wrap permitted")
    permuted = selected.copy()
    random.Random(17).shuffle(permuted)
    # Targets are inspected for description only after indices are fixed.
    classes = Counter(
        NormalizationAnswer.model_validate_json(rows[i]["messages"][-1]["content"]).canonical_id
        for i in selected
    )
    return {
        "source_rows": len(rows),
        "candidates": candidates,
        "selected": selected,
        "permuted": permuted,
        "skipped_over_cap": sorted(over_cap),
        "skipped_over_cap_count": len(over_cap),
        "collisions": collisions,
        "selected_rows_sha256": digest([rows[i] for i in selected]),
        "permuted_rows_sha256": digest([rows[i] for i in permuted]),
        "row_sha256": [digest(rows[i]) for i in selected],
        "stats": {
            "rows": count,
            "null_share": classes[None] / count,
            "class_counts": {
                "null" if key is None else key: value for key, value in classes.items()
            },
        },
    }


def common_inputs(config: DiagnosticInputs) -> dict[str, Any]:
    return config.model_dump(
        mode="json",
        exclude={"arm", "batch_size", "run_dir", "preregistration", "preregistration_sha256"},
    )


def identities(config: DiagnosticInputs) -> dict[str, Any]:
    if config.dataset_manifest_sha256 != DATASET_PIN or config.exclusions_sha256 != EXCLUSIONS_PIN:
        raise DatasetError("dataset/exclusions pins differ from the authorized diagnostic")
    if file_sha256(config.exclusions) != config.exclusions_sha256:
        raise DatasetError("exclusions sha256 differs from diagnostic pin")
    return smoke.verify_inputs(config, selected_rows_only=True)


def selections(config: DiagnosticInputs) -> tuple[dict[str, Any], tuple[list[Row], list[Row]]]:
    encode = encoder(config.model_dir)
    records: dict[str, Any] = {}
    selected_rows = []
    for split, filename, count in (
        ("fit", "train.jsonl", FIT_ROWS),
        ("valid", "valid.jsonl", VALID_ROWS),
    ):
        rows = read_rows(config.dataset_dir / filename)
        record = select_rows(rows, count, encode, config.max_seq_length)
        records[split] = record
        selected_rows.append([rows[i] for i in record["permuted"]])
    return records, (selected_rows[0], selected_rows[1])


def implementations() -> dict[str, str]:
    return {p.name: file_sha256(p) for p in (Path(__file__), Path(smoke.__file__))}


def preregister(plan_path: Path, out: Path) -> None:
    """No resource probe, MLX import, model load or training; consume immutable output once."""
    out = private_path(out)
    template = DiagnosticInputs.model_validate_json(plan_path.read_bytes())
    identity = identities(template)
    selection, _ = selections(template)
    record = {
        "schema": 1,
        "hypotheses": HYPOTHESES,
        "inputs": common_inputs(template),
        "arms": {
            arm: {"learning_rate": rate, "batch_size": batch, "updates": 60}
            for arm, (rate, batch) in ARMS.items()
        },
        "selection_policy": SELECTION_POLICY,
        "selection": selection,
        "identity": identity,
        "seed": 17,
        "implementation": implementations(),
        "validation_steps": list(VALIDATION_STEPS),
        "metrics": (
            "per-step train loss; arithmetic mean row loss over all64 valid; "
            "nonfinite; wall time; peak MLX/RSS"
        ),
        "rule": RULE,
        "stop_conditions": STOPS,
        "limitations": (
            "240 fit/64 valid, one seed, one fresh run per arm; "
            "no accuracy/capacity/Gate C inference"
        ),
    }
    text = json.dumps(record, indent=2, allow_nan=False) + "\n"
    sha = hashlib.sha256(text.encode()).hexdigest()
    files: dict[Path, str] = {out: text}
    for arm in ARMS:
        for mode in ("dry", "runs"):
            config = DiagnosticConfig.model_validate(
                {
                    **template.model_dump(),
                    "arm": arm,
                    "batch_size": ARMS[arm][1],
                    "run_dir": out.parent / mode / arm,
                    "preregistration": out,
                    "preregistration_sha256": sha,
                }
            )
            files[out.parent / f"{arm}{'-dry' if mode == 'dry' else ''}.json"] = (
                config.model_dump_json(indent=2) + "\n"
            )
    write_new(files)
    out.chmod(0o444)
    print(f"preregistration sha256: {sha}")


def verify_preregistration(
    config: DiagnosticConfig,
) -> tuple[dict[str, Any], tuple[list[Row], list[Row]]]:
    private_path(config.preregistration)
    if file_sha256(config.preregistration) != config.preregistration_sha256:
        raise DatasetError("preregistration sha256 differs; never rewrite or repin")
    record = json.loads(config.preregistration.read_bytes())
    if record["inputs"] != common_inputs(config) or record["implementation"] != implementations():
        raise DatasetError("preregistered inputs/implementation changed")
    expected_arms = {
        arm: {"learning_rate": rate, "batch_size": batch, "updates": 60}
        for arm, (rate, batch) in ARMS.items()
    }
    if (
        record["arms"] != expected_arms
        or record["selection_policy"] != SELECTION_POLICY
        or record["rule"] != RULE
        or record["validation_steps"] != list(VALIDATION_STEPS)
    ):
        raise DatasetError("preregistered design changed")
    if record["identity"] != identities(config):
        raise DatasetError("preregistered pins/identity changed")
    selected, rows = selections(config)
    if selected != record["selection"]:
        raise DatasetError("preregistered selected rows or permutation changed")
    return record, rows


def ordered_batches(
    dataset: Any,
    batch_size: int,
    max_seq_length: int,
    loop: bool = False,
    seed: int | None = None,
    comm_group: Any = None,
) -> Iterator[Any]:
    """Contiguous preregistered rows, with the installed trainer's ordinary zero padding.

    mlx-lm 0.31.3 iterate_batches sorts by length and permutes batches, so it cannot
    preserve this experiment's order. Match its tensor/bounds contract directly.
    """
    import mlx.core as mx
    import numpy as np

    if comm_group is not None and comm_group.size() != 1:
        raise DatasetError("diagnostic requires one local device")
    if batch_size < 1 or not len(dataset) or len(dataset) % batch_size:
        raise DatasetError("ordered diagnostic batches must cover every row exactly")
    while True:
        for start in range(0, len(dataset), batch_size):
            pairs = [dataset[i] for i in range(start, start + batch_size)]
            for tokens, offset in pairs:
                smoke.validate_supervision(tokens, offset, max_seq_length)
            lengths = [len(tokens) for tokens, _ in pairs]
            width = min(1 + 32 * ((max(lengths) + 31) // 32), max_seq_length)
            array = np.zeros((batch_size, width), np.int32)
            for i, (tokens, _) in enumerate(pairs):
                array[i, : len(tokens)] = tokens
            yield (
                mx.array(array),
                mx.array([(offset, n) for (_, offset), n in zip(pairs, lengths, strict=True)]),
            )
        if not loop:
            break


def validation_mean(model: Any, dataset: Any) -> tuple[float, int]:
    """Arithmetic mean of 64 singleton row losses, with dropout disabled."""
    if len(dataset) != VALID_ROWS:
        raise DatasetError("validation requires all64 rows")
    model.eval()
    values: list[float] = []
    try:
        for batch in ordered_batches(dataset, 1, 2048):
            value, ntoks = smoke.exclude_padding_loss(model, *batch)
            number = float(value.item())
            if int(ntoks.item()) <= 0:
                raise DatasetError("empty validation supervision")
            if not math.isfinite(number):
                return number, len(
                    values
                ) + 1  # Persist the actual attempted count before refusing.
            values.append(number)
    finally:
        model.train()
    if len(values) != VALID_ROWS:
        raise DatasetError("validation did not evaluate all64 rows")
    return math.fsum(values) / VALID_ROWS, len(values)


def fit_control(config: DiagnosticConfig) -> smoke.FitControl:
    def validate(args: Any) -> None:
        if vars(args) != smoke.training_options(config):
            raise DatasetError("diagnostic fit options differ from fixed config")

    def record(kind: str, info: dict[str, Any]) -> None:
        # Append before finite checks can abort; partial evidence survives a failed child.
        with (config.run_dir / "events.jsonl").open("a") as handle:
            event = {"kind": kind, **info}
            event["nonfinite"] = has_nonfinite(event)
            handle.write(json.dumps(json_safe(event), allow_nan=False) + "\n")

    return smoke.FitControl(validate, validation_mean, ordered_batches, record, VALIDATION_STEPS)


def worker(config: DiagnosticConfig) -> None:
    _, rows = verify_preregistration(config)
    smoke.train_local(config, selection=rows, control=fit_control(config))


def run(config: DiagnosticConfig, *, dry_run: bool) -> int:
    config = DiagnosticConfig.model_validate(config)
    run_dir = private_path(config.run_dir)
    try:
        run_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise DatasetError("run directory already exists; refusing reuse") from exc
    started = time.monotonic()
    report: dict[str, Any] = {
        "status": "failed",
        "arm": config.arm,
        "artifact_errors": [],
        "config": config.model_dump(mode="json"),
        "training_options": smoke.training_options(config),
        "preregistration_sha256": config.preregistration_sha256,
        "command": sys.argv,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "implementation": implementations(),
        "resources": {"peak_mlx_bytes": None, "peak_rss_bytes": None},
    }
    write_new({run_dir / "config.json": config.model_dump_json(indent=2) + "\n"})
    try:
        record, _ = verify_preregistration(config)
        report.update({"identity": record["identity"], "selection": record["selection"]})
        attempt = private_path(
            config.preregistration.parent
            / "attempts"
            / f"{config.arm}-{'dry' if dry_run else 'train'}.json"
        )
        write_new(
            {
                attempt: json.dumps(
                    {
                        "run_dir": str(run_dir),
                        "preregistration_sha256": config.preregistration_sha256,
                    }
                )
            }
        )
        report["environment"] = {
            name: version(name) for name in ("mlx", "mlx-lm", "transformers", "tokenizers", "numpy")
        }
        if report["environment"]["mlx-lm"] != "0.31.3":
            raise DatasetError("diagnostic requires inspected mlx-lm 0.31.3")
        state = smoke.probe_resources()
        report["preflight"] = state.model_dump(mode="json")
        state.check()
        if dry_run:
            report["status"] = "preflight_passed"
        else:
            report["resources"] = smoke.launch_trainer(config, script=Path(__file__))
            report["metrics"] = json.loads((run_dir / "metrics.json").read_bytes())
            validate_metrics(report["metrics"])
            check_resources(report["resources"])
            report["status"] = "diagnostic_completed"
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        print(f"error: {exc}", file=sys.stderr)
    finally:
        counters = run_dir / "resources.json"
        if counters.is_file():
            try:
                resources = json.loads(counters.read_bytes())
                if not isinstance(resources, dict):
                    raise DatasetError("resource artifact must be an object")
                report["resources"] = resources
            except (OSError, ValueError) as exc:
                report["artifact_errors"].append(f"resources.json: {exc}")
        events_path = run_dir / "events.jsonl"
        if events_path.is_file():
            events = []
            try:
                with events_path.open() as handle:
                    for number, line in enumerate(handle, 1):
                        try:
                            event = json.loads(line)
                            if not isinstance(event, dict) or event.get("kind") not in (
                                "train",
                                "validation",
                            ):
                                raise DatasetError("invalid event")
                            events.append(event)
                        except ValueError as exc:
                            report["artifact_errors"].append(f"events.jsonl line {number}: {exc}")
            except OSError as exc:
                report["artifact_errors"].append(f"events.jsonl: {exc}")
            report["events"] = events
            report["metrics"] = {
                kind: [event for event in events if event["kind"] == kind]
                for kind in ("train", "validation")
            }
        if report["artifact_errors"]:
            report["status"] = "failed"
        report["elapsed_seconds"] = time.monotonic() - started
        write_new(
            {
                run_dir / "report.json": json.dumps(json_safe(report), indent=2, allow_nan=False)
                + "\n"
            }
        )
    return int(report["status"] == "failed")


def has_nonfinite(value: Any) -> bool:
    if isinstance(value, dict):
        return value.get("nonfinite") is True or any(has_nonfinite(v) for v in value.values())
    if isinstance(value, list):
        return any(has_nonfinite(v) for v in value)
    return (isinstance(value, float) and not math.isfinite(value)) or (
        isinstance(value, str) and value in ("NaN", "Infinity", "-Infinity")
    )


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
    return value


def finite_number(value: Any, *, positive: bool = False) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        or (positive and value <= 0)
    ):
        raise DatasetError("missing/nonfinite/invalid numerical evidence")
    return float(value)


def validate_metrics(metrics: dict[str, Any]) -> None:
    try:
        for kind, steps, key in (
            ("train", list(range(1, 61)), "train_loss"),
            ("validation", list(VALIDATION_STEPS), "val_loss"),
        ):
            entries = metrics[kind]
            if [entry["iteration"] for entry in entries] != steps:
                raise DatasetError(f"incomplete or mislabeled {kind} evidence")
            for entry in entries:
                finite_number(entry[key])
                if kind == "validation" and entry["rows"] != VALID_ROWS:
                    raise DatasetError("validation evidence must cover all64 rows")
        finite_number(metrics["validation"][0]["val_loss"], positive=True)
    except (KeyError, TypeError) as exc:
        raise DatasetError("missing/malformed metric evidence") from exc


def check_resources(resources: dict[str, Any]) -> None:
    if finite_number(resources["peak_mlx_bytes"], positive=True) > smoke.TRAIN_ALLOCATION_BYTES:
        raise DatasetError("MLX peak exceeds training allocation")
    finite_number(resources["peak_rss_bytes"], positive=True)


NOT_MEASURED = "NOT MEASURED"


def series(metrics: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    entries = metrics.get(kind, [])
    if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
        return []
    return cast(list[dict[str, Any]], entries)


def measured(value: Any, *, positive: bool = False) -> float | None:
    try:
        return finite_number(value, positive=positive)
    except DatasetError:
        return None


def train_max(metrics: dict[str, Any]) -> float | None:
    entries = series(metrics, "train")
    if [entry.get("iteration") for entry in entries] != list(range(1, 61)):
        return None
    values = [measured(entry.get("train_loss")) for entry in entries]
    if any(value is None for value in values):
        return None
    return max(cast(list[float], values))


def summarize_arm(report: dict[str, Any], max_a: float | None) -> dict[str, Any]:
    metrics = report.get("metrics", {})
    if not isinstance(metrics, dict):
        metrics = {}
    errors = []
    try:
        validate_metrics(metrics)
    except DatasetError as exc:
        errors.append(str(exc))
    complete = not errors
    try:
        check_resources(report.get("resources", {}))
        finite_number(report.get("elapsed_seconds"), positive=True)
    except (DatasetError, KeyError, TypeError) as exc:
        errors.append(f"resource/time evidence: {exc}")
    if report.get("status") != "diagnostic_completed":
        errors.append(f"run status: {report.get('status', NOT_MEASURED)}")
    for key in ("identity", "selection", "preregistration_sha256"):
        if not report.get(key):
            errors.append(f"missing {key}")
    if report.get("error"):
        errors.append(str(report["error"]))
    errors.extend(str(error) for error in report.get("artifact_errors", []))
    entries = series(metrics, "validation")

    def validation_at(step: int) -> float | None:
        matches = [entry for entry in entries if entry.get("iteration") == step]
        if len(matches) != 1 or matches[0].get("rows") != VALID_ROWS:
            return None
        return measured(matches[0].get("val_loss"), positive=step == 0)

    initial, final = validation_at(0), validation_at(60)
    validation_complete = [entry.get("iteration") for entry in entries] == list(VALIDATION_STEPS)
    values = [validation_at(step) for step in VALIDATION_STEPS]
    peak_val = (
        max(cast(list[float], values))
        if all(v is not None for v in values) and validation_complete
        else None
    )
    peak_train = train_max(metrics)
    final_ok: bool | str = NOT_MEASURED
    every_ok: bool | str = NOT_MEASURED
    train_ok: bool | str = NOT_MEASURED
    if initial is not None:
        if final is not None:
            final_ok = final <= 1.10 * initial
        if any(v is not None and v > 1.25 * initial for v in values):
            every_ok = False
        elif peak_val is not None:
            every_ok = True
    if peak_train is not None and max_a is not None and max_a > 0:
        train_ok = peak_train <= 0.5 * max_a
    criteria: dict[str, bool | str] = {
        "no_nonfinite": False
        if has_nonfinite([metrics, report.get("events", [])])
        else (True if complete else NOT_MEASURED),
        "final_validation": final_ok,
        "every_validation": every_ok,
        "train_max_vs_A": train_ok,
    }
    stable = not errors and all(value is True for value in criteria.values())
    outcome = (
        "stable on this sample"
        if stable
        else "NOT MEASURED"
        if errors or not all(isinstance(value, bool) for value in criteria.values())
        else "not stable on this sample"
    )
    return {
        "outcome": outcome,
        "criteria": criteria,
        "evidence_errors": errors,
        "validation_rules_pass": final_ok is True and every_ok is True,
        "validation_initial": initial if initial is not None else NOT_MEASURED,
        "validation_final": final if final is not None else NOT_MEASURED,
        "validation_max": peak_val if peak_val is not None else NOT_MEASURED,
        "train_max": peak_train if peak_train is not None else NOT_MEASURED,
        "validation_final_ratio": final / initial
        if final is not None and initial
        else NOT_MEASURED,
        "validation_max_ratio": peak_val / initial
        if peak_val is not None and initial
        else NOT_MEASURED,
        "train_max_ratio_to_A": peak_train / max_a
        if peak_train is not None and max_a
        else NOT_MEASURED,
    }


def evaluate_reports(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply the fixed rules, including a truthful table for missing/failed arms."""
    by_arm: dict[str, dict[str, Any]] = {}
    shared: dict[str, Any] = {}
    for report in reports:
        arm = report.get("arm")
        if arm not in ARMS or arm in by_arm:
            raise DatasetError("require at most one report per A/B/C/D")
        by_arm[arm] = report
        for key in ("identity", "selection", "preregistration_sha256"):
            value = report.get(key)
            if value is not None:
                if not value or (key in shared and shared[key] != value):
                    raise DatasetError(f"cross-config {key} mismatch")
                shared[key] = value
    if not by_arm:
        raise DatasetError("require at least one diagnostic report")
    baseline_report = by_arm.get("A", {})
    baseline_summary = summarize_arm(baseline_report, None)
    baseline_usable = (
        not baseline_summary["evidence_errors"]
        and baseline_summary["train_max"] != NOT_MEASURED
        and isinstance(baseline_summary["criteria"]["final_validation"], bool)
        and isinstance(baseline_summary["criteria"]["every_validation"], bool)
    )
    max_a = (
        train_max(baseline_report.get("metrics", {}))
        if baseline_usable and isinstance(baseline_report.get("metrics"), dict)
        else None
    )
    results = {arm: summarize_arm(by_arm.get(arm, {}), max_a) for arm in ARMS}
    baseline_criteria = results["A"]["criteria"]
    inconclusive = results["A"]["validation_rules_pass"]
    baseline_known = baseline_usable and all(
        isinstance(baseline_criteria[key], bool) for key in ("final_validation", "every_validation")
    )
    validity = (
        "NOT MEASURED: baseline validation rules"
        if not baseline_known
        else "INCONCLUSIVE about LR/batch"
        if inconclusive
        else "conclusive on this sample"
    )
    return {
        "validity": validity,
        "arms": results,
        "winners": [arm for arm in ARMS if results[arm]["outcome"] == "stable on this sample"]
        if baseline_known and not inconclusive
        else [],
        "preregistration_sha256": shared.get("preregistration_sha256", NOT_MEASURED),
        "limitations": (
            "64 validation rows, one seed, one run per arm; missing evidence is NOT MEASURED; "
            "no quality, capacity, accuracy or Gate C claim"
        ),
    }


def prepare_configs(smoke_path: Path, out: Path) -> None:
    """Prepare a plan from the existing model pins, without reading data or running preflight."""
    out = private_path(out)
    source = smoke.SmokeConfig.model_validate_json(smoke_path.read_bytes())
    root = smoke_path.resolve().parent
    plan = DiagnosticInputs(
        dataset_dir=root / "datasets/dataset_sec_5",
        dataset_manifest_sha256=DATASET_PIN,
        exclusions=root / "datasets/consistency_exclusions/exclusions.json",
        exclusions_sha256=EXCLUSIONS_PIN,
        model_dir=source.model_dir.resolve(),
        model_manifest=source.model_manifest.resolve(),
        model_manifest_sha256=source.model_manifest_sha256,
        run_dir=out / "runs/A",
        arm="A",
    )
    write_new({out / "plan.json": plan.model_dump_json(indent=2) + "\n"})


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--prepare-configs", type=Path, metavar="SMOKE_CONFIG")
    action.add_argument("--preregister", type=Path, metavar="PLAN")
    action.add_argument("--evaluate", type=Path, nargs="+", metavar="REPORT")
    action.add_argument("--config", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if (args.dry_run or args.worker) and not args.config:
        parser.error("--dry-run/--worker require --config")
    if not args.config and args.out is None:
        parser.error("--out is required for preparation/preregistration/evaluation")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    try:
        if args.prepare_configs:
            prepare_configs(args.prepare_configs, args.out)
        elif args.preregister:
            preregister(args.preregister, args.out)
        elif args.evaluate:
            reports = [json.loads(path.read_bytes()) for path in args.evaluate]
            for report in reports:
                config = DiagnosticConfig.model_validate_json(json.dumps(report["config"]))
                record, _ = verify_preregistration(config)
                if (
                    report["training_options"] != smoke.training_options(config)
                    or report["arm"] != config.arm
                    or report["preregistration_sha256"] != config.preregistration_sha256
                    or ("selection" in report and report["selection"] != record["selection"])
                    or ("identity" in report and report["identity"] != record["identity"])
                ):
                    raise DatasetError("report differs from preregistered configuration")
            result = evaluate_reports(reports)
            write_new(
                {private_path(args.out): json.dumps(result, indent=2, allow_nan=False) + "\n"}
            )
        else:
            config = DiagnosticConfig.model_validate_json(args.config.read_bytes())
            if args.worker:
                if args.config.resolve() != config.run_dir.resolve() / "config.json":
                    raise DatasetError("worker requires saved run config")

                def deadline(signum: int, frame: object) -> None:
                    raise TimeoutError("diagnostic runtime bound exceeded")

                signal.signal(signal.SIGALRM, deadline)
                signal.alarm(config.timeout_seconds)
                try:
                    worker(config)
                finally:
                    signal.alarm(0)
            else:
                return run(config, dry_run=args.dry_run)
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
