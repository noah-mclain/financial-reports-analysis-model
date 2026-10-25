"""Preregistered offline normalization optimization diagnostic, without quality scoring.

Dry runs verify identity and selection in the controller process; training runs verify them in
the heavy worker process.
"""

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
from typing import Any, Literal, Protocol, TypedDict, cast

import normalization_train as smoke
from chat_tokenizer import ChatTokenizerContext, open_chat_tokenizer
from dataset_builder import Row
from dataset_contracts import Contract, DatasetError, NormalizationAnswer, Sha256, digest
from exclusive_write import write_new
from normalization_validation import file_sha256
from pydantic import Field, model_validator


class ValidationProvenance(TypedDict):
    row_id: str
    canonical_id: str | None


ARMS = {"A": (1e-4, 1), "B": (2e-5, 1), "C": (2e-5, 4), "D": (1e-5, 4)}
VALIDATION_STEPS = (0, 15, 30, 45, 60)
FIT_ROWS = 240
VALID_ROWS = 64
PAIR_MAX_TRAIN = 5.142303466796875
PAIR_SUPERVISED_TOKENS = 2161
TREATMENT_MAX_TRAIN = PAIR_MAX_TRAIN
HISTORICAL_ROOT = Path(
    "/Users/nadamohamed/Library/Application Support/financial-reports-analysis-model/"
    "normalization-training/stability-supplement-20261023T111415Z"
)
HISTORICAL = {
    "report": (
        str(HISTORICAL_ROOT / "runs/D/report.json"),
        "53f707221832d50b7109077fc2fc3801a8708319c7332203375793000780e40c",
    ),
    "preregistration": (
        str(HISTORICAL_ROOT / "preregistration.json"),
        "5e5475e201ce2c33b6bdb4321e9b975d0d7f1264c02590fe84d180f8926d114e",
    ),
    "adapter": (
        str(HISTORICAL_ROOT / "runs/D/adapter/adapters.safetensors"),
        "088bca30b60eaf911e0b8c52f8d15e5d5b2db1df2b5f35b68351c1d725a7d772",
    ),
    "checkpoint": (
        str(HISTORICAL_ROOT / "runs/D/adapter/0000060_adapters.safetensors"),
        "088bca30b60eaf911e0b8c52f8d15e5d5b2db1df2b5f35b68351c1d725a7d772",
    ),
    "adapter_config": (
        str(HISTORICAL_ROOT / "runs/D/adapter/adapter_config.json"),
        "a372b5f06c665f957d654a7499b249f135331a9673c310fe39fac14e343d399e",
    ),
}
HISTORICAL_SOURCE_HASHES = {
    "normalization_stability.py": (
        "33753f626dade35c4647c2a9e76ce91b1d251f7b395c512cc3233c86d71b18fb"
    ),
    "normalization_train.py": ("baa71c5d03bab739922f86a50b0b4d5df384f221b822f699b77ee8becaf4f13c"),
}
DATASET_PIN = "9cae5c2febd7e568cb1996150b99bfad7170f5c565dea090010e67b9953843f5"
EXCLUSIONS_PIN = "9d5388129dbdba74dff6bd3fa5724f738b7e0d4f963208cfd4de34941b6c8a7f"
PANEL_RECEIPT_SHA256 = "491f8dcd0e7e2d11513394a9e914e15ecd926a552b3879a34cc5948e8dfb1b43"
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


class BiasCorrectionConfig(DiagnosticConfig):
    """Fresh D replay with one explicitly named optimizer intervention."""

    arm: Literal["D"] = "D"
    batch_size: Literal[4] = 4
    pair_arm: Literal["reference", "treatment"]

    @property
    def adamw_bias_correction(self) -> bool:
        return self.pair_arm == "treatment"


class TreatmentOnlyConfig(BiasCorrectionConfig):
    """One fresh corrected treatment, compared exploratorily with historical D."""

    pair_arm: Literal["treatment"] = "treatment"
    historical_report: Path
    historical_report_sha256: Sha256
    historical_preregistration: Path
    historical_preregistration_sha256: Sha256
    historical_adapter: Path
    historical_adapter_sha256: Sha256
    historical_checkpoint: Path
    historical_checkpoint_sha256: Sha256
    historical_adapter_config: Path
    historical_adapter_config_sha256: Sha256

    @model_validator(mode="after")
    def treatment_is_fixed(self) -> TreatmentOnlyConfig:
        fixed = {
            "iters": 60,
            "batch_size": 4,
            "grad_checkpoint": True,
            "num_layers": 16,
            "max_seq_length": 2048,
            "seed": 17,
            "timeout_seconds": 1500,
        }
        if any(getattr(self, key) != value for key, value in fixed.items()):
            raise ValueError("treatment-only training settings differ from frozen D treatment")
        if self.pair_arm != "treatment":
            raise ValueError("treatment-only config must select treatment")
        for name, (path, sha) in HISTORICAL.items():
            if (
                str(getattr(self, f"historical_{name}")) != path
                or getattr(self, f"historical_{name}_sha256") != sha
            ):
                raise ValueError(f"historical {name} descriptor differs from verified record")
        return self


def private_path(path: Path) -> Path:
    resolved = path.resolve()
    if (
        path.is_symlink()
        or resolved.is_relative_to(smoke.ROOT)
        or any((parent / ".git").exists() for parent in (resolved, *resolved.parents))
    ):
        raise DatasetError("diagnostic artifacts must be private, outside Git and not symlinks")
    return resolved


def encoder(
    model_dir: Path, tokenizer_context: ChatTokenizerContext | None = None
) -> Callable[[Row], tuple[list[int], int]]:
    context = tokenizer_context or open_chat_tokenizer(model_dir)
    context.assert_current(model_dir)

    def encode(row: Row) -> tuple[list[int], int]:
        messages = row["messages"]
        if not messages or messages[-1]["role"] != "assistant":
            raise DatasetError("selected chat row must end with assistant supervision")
        return context.encode(messages)

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
        exclude={
            "arm",
            "pair_arm",
            "batch_size",
            "run_dir",
            "preregistration",
            "preregistration_sha256",
        },
    )


def identities(
    config: DiagnosticInputs,
    tokenizer_context: ChatTokenizerContext | None = None,
) -> dict[str, Any]:
    if config.dataset_manifest_sha256 != DATASET_PIN or config.exclusions_sha256 != EXCLUSIONS_PIN:
        raise DatasetError("dataset/exclusions pins differ from the authorized diagnostic")
    if file_sha256(config.exclusions) != config.exclusions_sha256:
        raise DatasetError("exclusions sha256 differs from diagnostic pin")
    return smoke.verify_inputs(config, selected_rows_only=True, tokenizer_context=tokenizer_context)


def selections(
    config: DiagnosticInputs,
    tokenizer_context: ChatTokenizerContext | None = None,
) -> tuple[dict[str, Any], tuple[list[Row], list[Row]]]:
    owns_context = tokenizer_context is None
    context = tokenizer_context or open_chat_tokenizer(config.model_dir)
    records: dict[str, Any] = {}
    selected_rows = []
    try:
        encode = encoder(config.model_dir, context)
        for split, filename, count in (
            ("fit", "train.jsonl", FIT_ROWS),
            ("valid", "valid.jsonl", VALID_ROWS),
        ):
            rows = read_rows(config.dataset_dir / filename)
            record = select_rows(rows, count, encode, config.max_seq_length)
            records[split] = record
            selected_rows.append([rows[i] for i in record["permuted"]])
        return records, (selected_rows[0], selected_rows[1])
    finally:
        try:
            context.assert_current(config.model_dir)
        finally:
            if owns_context:
                context.release()


def implementations() -> dict[str, str]:
    return {p.name: file_sha256(p) for p in (Path(__file__), Path(smoke.__file__))}


def treatment_implementations() -> dict[str, str]:
    root = Path(__file__).resolve().parent
    paths = (
        Path(__file__),
        Path(smoke.__file__),
        root / "chat_tokenizer.py",
        root / "normalization_mlx.py",
        root / "normalization_validation.py",
        root / "normalization_eval.py",
        root / "dataset_contracts.py",
        root.parent / "packages/core/src/fra_core/taxonomy/loader.py",
        root.parent / "packages/core/src/fra_core/taxonomy/canonical_items.yaml",
        root.parent / "packages/ingest/src/fra_ingest/label_match.py",
    )
    return {path.name: file_sha256(path) for path in paths}


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
    tokenizer_context: ChatTokenizerContext | None = None,
) -> tuple[dict[str, Any], tuple[list[Row], list[Row]]]:
    if tokenizer_context is None:
        context = open_chat_tokenizer(config.model_dir)
        try:
            return verify_preregistration(config, tokenizer_context=context)
        finally:
            context.release()
    private_path(config.preregistration)
    if file_sha256(config.preregistration) != config.preregistration_sha256:
        raise DatasetError("preregistration sha256 differs; never rewrite or repin")
    record = json.loads(config.preregistration.read_bytes())
    if isinstance(config, TreatmentOnlyConfig):
        return verify_treatment(config, tokenizer_context=tokenizer_context)
    expected_implementation = implementations()
    if (
        record["inputs"] != common_inputs(config)
        or record["implementation"] != expected_implementation
    ):
        raise DatasetError("preregistered inputs/implementation changed")
    if isinstance(config, BiasCorrectionConfig):
        if record.get("schema") != 2 or record.get("pair") != pair_design():
            raise DatasetError("preregistered bias-correction pair changed")
        if record["identity"] != identities(config, tokenizer_context):
            raise DatasetError("preregistered pins/identity changed")
        selected, rows = selections(config, tokenizer_context)
        if (
            selected != record["selection"]
            or supervised_budget(config, rows, tokenizer_context=tokenizer_context)
            != record["budget"]
        ):
            raise DatasetError("preregistered rows/order/token budget changed")
        return record, rows
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
    if record["identity"] != identities(config, tokenizer_context):
        raise DatasetError("preregistered pins/identity changed")
    selected, rows = selections(config, tokenizer_context)
    if selected != record["selection"]:
        raise DatasetError("preregistered selected rows or permutation changed")
    return record, rows


def treatment_protocol_files() -> dict[str, Path]:
    root = Path(__file__).resolve().parents[1]
    return {
        "control_treatment": root / "docs/runbooks/normalization-control-treatment-supplement.md",
        "follow_up": root / "docs/runbooks/normalization-follow-up-protocol.md",
    }


def treatment_evidence_files() -> dict[str, Path]:
    root = Path("/private/tmp/normalization-quality-protocol")
    comparison = Path("/private/tmp/normalization-design-correction-2f7e0pm_")
    return {
        "receipt": root / "protocol-v3-receipt-20261023.json",
        "historical_reference_assessment": comparison / "historical-reference-assessment.json",
        "deterministic_comparison": comparison / "deterministic-comparison.json",
    }


def verify_historical(
    config: TreatmentOnlyConfig, identity: dict[str, Any], selection: dict[str, Any]
) -> dict[str, Any]:
    """Recheck historical D artifacts by their frozen hashes; stability is exploratory only."""
    for name in HISTORICAL:
        path = private_path(getattr(config, f"historical_{name}"))
        expected = getattr(config, f"historical_{name}_sha256")
        if file_sha256(path) != expected:
            raise DatasetError(f"historical {name} sha256 differs from verified record")
    report = json.loads(config.historical_report.read_bytes())
    preregistration = json.loads(config.historical_preregistration.read_bytes())
    if report.get("status") != "diagnostic_completed" or report.get("arm") != "D":
        raise DatasetError("historical comparator must be completed D report")
    if report.get("identity") != identity or report.get("selection") != selection:
        raise DatasetError("historical comparator identity/selection differs from treatment")
    expected_environment = {
        name: version(name) for name in ("mlx", "mlx-lm", "transformers", "tokenizers", "numpy")
    }
    if report.get("environment") != expected_environment:
        raise DatasetError("historical comparator environment differs from treatment")
    metrics = report.get("metrics", {})
    train, validation = series(metrics, "train"), series(metrics, "validation")
    if (
        [entry.get("iteration") for entry in train] != list(range(1, 61))
        or train[-1].get("trained_tokens") != PAIR_SUPERVISED_TOKENS
        or [entry.get("iteration") for entry in validation] != list(VALIDATION_STEPS)
        or any(entry.get("rows") != VALID_ROWS for entry in validation)
    ):
        raise DatasetError("historical comparator lacks complete D exposure/validation")
    if preregistration.get("identity") != identity or preregistration.get("selection") != selection:
        raise DatasetError("historical preregistration identity/selection differs")
    if preregistration.get("implementation") != HISTORICAL_SOURCE_HASHES:
        raise DatasetError("historical preregistration source hashes differ from verified record")
    initial = finite_number(validation[0].get("val_loss"), positive=True)
    max_validation = max(finite_number(entry.get("val_loss")) for entry in validation)
    historical_max_ratio = max_validation / initial
    return {
        "status": "historical exploratory comparator; stability not required",
        "historical_stability": (
            "unstable" if historical_max_ratio > 1.25 else "validation stability ceilings met"
        ),
        "historical_max_validation_ratio": historical_max_ratio,
        "promotable": False,
        "report_sha256": config.historical_report_sha256,
        "preregistration_sha256": config.historical_preregistration_sha256,
        "adapter_sha256": config.historical_adapter_sha256,
        "checkpoint_sha256": config.historical_checkpoint_sha256,
        "adapter_config_sha256": config.historical_adapter_config_sha256,
        "source_hashes": HISTORICAL_SOURCE_HASHES,
        "limitations": [
            "historical source is not byte-identical to the current treatment source",
            "only a 35-update scientific prefix was compared; this is not a full replay",
            "historical stability failure is descriptive and does not determine "
            "treatment acceptance",
        ],
    }


def treatment_record(
    config: TreatmentOnlyConfig,
    tokenizer_context: ChatTokenizerContext | None = None,
) -> tuple[dict[str, Any], tuple[list[Row], list[Row]]]:
    owns_context = tokenizer_context is None
    context = tokenizer_context or open_chat_tokenizer(config.model_dir)
    try:
        identity = identities(config, context)
        selected, rows = selections(config, context)
        budget = supervised_budget(config, rows, tokenizer_context=context)
    finally:
        if owns_context:
            context.release()
    if budget != {"updates": 60, "examples": FIT_ROWS, "supervised_tokens": PAIR_SUPERVISED_TOKENS}:
        raise DatasetError("treatment exposure differs from frozen historical D")
    protocol = treatment_protocol_files()
    generated = config.run_dir.parent / "generated-v1"
    lexicon = config.run_dir.parent / "lexicon-v1"
    predictions = {
        f"{predictor}-{panel}": str(generated / f"{predictor}-{panel}.json")
        for predictor in ("base", "historical-D", "treatment")
        for panel in ("representative", "challenge")
    }
    scores = {
        f"{predictor}-{panel}": str(generated / f"{predictor}-{panel}-score.json")
        for predictor in ("historical-D", "treatment")
        for panel in ("representative", "challenge")
    }
    lexicon_predictions = {
        panel: str(lexicon / f"{panel}-predictions.json")
        for panel in ("representative", "challenge")
    }
    lexicon_scores = {
        panel: str(lexicon / f"{panel}-score.json") for panel in ("representative", "challenge")
    }
    panel_root = Path("/private/tmp/normalization-quality-protocol/panels-v1")
    panel_files = {
        f"{panel}-{kind}": panel_root / f"{panel}-{kind}.json"
        for panel in ("representative", "challenge")
        for kind in ("selection", "manifest")
    }
    panel_files["population-and-reconciliation"] = panel_root / "population-and-reconciliation.json"
    panel_hashes = {key: file_sha256(path) for key, path in panel_files.items()}
    evidence_files = treatment_evidence_files()
    if file_sha256(evidence_files["receipt"]) != PANEL_RECEIPT_SHA256:
        raise DatasetError("approved panel receipt sha256 differs")
    receipt = json.loads(evidence_files["receipt"].read_bytes())
    receipt_files = receipt.get("files_sha256", {})
    for key, path in panel_files.items():
        receipt_key = str(path)
        if receipt_files.get(receipt_key) != panel_hashes[key]:
            raise DatasetError(f"frozen panel {key} differs from the approved receipt")
    treatment_config_path = config.preregistration.parent / "treatment.json"
    generation_commands = {}
    scoring_commands = {}
    generation_child = (
        "import mlx.core as mx,runpy; mx.set_cache_limit(1_000_000_000); "
        "mx.set_memory_limit(6_000_000_000); "
        "runpy.run_path('training/normalization_mlx.py',run_name='__main__')"
    )
    for key, output in predictions.items():
        panel = key.rsplit("-", 1)[1]
        predictor = key[: -(len(panel) + 1)]
        generation_commands[key] = {
            "cwd": str(smoke.ROOT),
            "timeout_seconds": 1500 if panel == "representative" else 600,
            "environment": {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"},
            "argv": [
                sys.executable,
                "-c",
                generation_child,
                "--selection",
                str(panel_files[f"{panel}-selection"]),
                "--model-dir",
                str(config.model_dir),
                "--model-manifest",
                str(config.model_manifest),
                "--out",
                output,
                "--max-tokens",
                "64",
                *(
                    [
                        "--adapter-dir",
                        str(config.historical_adapter.parent)
                        if predictor == "historical-D"
                        else str(config.run_dir / "adapter"),
                    ]
                    if predictor != "base"
                    else []
                ),
            ],
        }
    for key, output in scores.items():
        panel = key.rsplit("-", 1)[1]
        predictor = key[: -(len(panel) + 1)]
        scoring_commands[key] = {
            "cwd": str(smoke.ROOT),
            "environment": {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"},
            "argv": [
                sys.executable,
                "training/normalization_validation.py",
                "score",
                "--selection",
                str(panel_files[f"{panel}-selection"]),
                "--predictions",
                predictions[key],
                "--base",
                predictions[f"base-{panel}"],
                "--seed",
                "20261023",
                "--out",
                output,
            ],
        }
    return {
        "schema": 3,
        "mode": "treatment_only_historical_exploratory_comparator",
        "inputs": common_inputs(config),
        "treatment_config": config.model_dump(mode="json", exclude={"preregistration_sha256"}),
        "identity": identity,
        "selection": selected,
        "panel_artifacts": {
            "paths": {key: str(path) for key, path in panel_files.items()},
            "sha256": panel_hashes,
        },
        "comparison_evidence": {
            key: {"path": str(path), "sha256": file_sha256(path)}
            for key, path in evidence_files.items()
        },
        "budget": budget,
        "training_options": smoke.training_options(config),
        "treatment_settings": {
            "only_intervention": "AdamW bias_correction=true",
            "thresholds": {
                "final_validation_at_most_initial_ratio": 1.10,
                "each_validation_at_most_initial_ratio": 1.25,
                "max_train_loss": TREATMENT_MAX_TRAIN,
            },
            "stop_conditions": STOPS,
            "validation_steps": list(VALIDATION_STEPS),
            "checkpoint": "final update 60 only",
            "resources": {
                "child_timeout_seconds": 1500,
                "mlx_allocation_bytes": smoke.TRAIN_ALLOCATION_BYTES,
                "cache_bytes": smoke.CACHE_LIMIT_BYTES,
            },
        },
        "historical_comparator": verify_historical(config, identity, selected),
        "implementation": treatment_implementations(),
        "protocol_sha256": {key: file_sha256(path) for key, path in protocol.items()},
        "environment": {
            name: version(name) for name in ("mlx", "mlx-lm", "transformers", "tokenizers", "numpy")
        },
        "commands": {
            "treatment": {
                "cwd": str(smoke.ROOT),
                "argv": [
                    sys.executable,
                    "training/normalization_stability.py",
                    "--config",
                    str(treatment_config_path),
                ],
            },
            "generation": {
                "gate": (
                    "conditional on accepted treatment and a fresh resource check; "
                    "sequential base, historical-D, treatment"
                ),
                "selection_paths": {
                    panel: (
                        "/private/tmp/normalization-quality-protocol/panels-v1/"
                        f"{panel}-selection.json"
                    )
                    for panel in ("representative", "challenge")
                },
                "prediction_outputs": predictions,
                "score_outputs": scores,
                "generation_commands": generation_commands,
                "generation_allocation_wrapper": generation_child,
                "historical_adapter": str(config.historical_adapter),
                "treatment_adapter": str(config.run_dir / "adapter"),
                "scoring_commands": scoring_commands,
                "lexicon": {
                    "implementation": "normalization_validation.lexicon_run / LabelIndex.match",
                    "prediction_outputs": lexicon_predictions,
                    "score_outputs": lexicon_scores,
                    "score_commands": {
                        panel: {
                            "cwd": str(smoke.ROOT),
                            "environment": {
                                "HF_HUB_OFFLINE": "1",
                                "TRANSFORMERS_OFFLINE": "1",
                            },
                            "argv": [
                                sys.executable,
                                "training/normalization_validation.py",
                                "score",
                                "--selection",
                                str(panel_files[f"{panel}-selection"]),
                                "--predictions",
                                lexicon_predictions[panel],
                                "--base",
                                predictions[f"base-{panel}"],
                                "--seed",
                                "20261023",
                                "--out",
                                lexicon_scores[panel],
                            ],
                        }
                        for panel in ("representative", "challenge")
                    },
                },
                "limits": {
                    "per_representative_process_seconds": 1500,
                    "per_challenge_process_seconds": 600,
                    "total_seconds": 6300,
                    "total_heavy_seconds": 7800,
                    "allocation_bytes": smoke.DIAGNOSTIC_ALLOCATION_BYTES,
                    "cache_bytes": smoke.CACHE_LIMIT_BYTES,
                },
            },
        },
        "outputs": {"run_dir": str(config.run_dir), "preregistration": str(config.preregistration)},
        "comparator_acceptance": (
            "historical completeness only; no both_stable or historical stability gate"
        ),
    }, rows


def preregister_treatment(plan_path: Path, out: Path) -> None:
    out = private_path(out)
    template = TreatmentOnlyConfig.model_validate_json(plan_path.read_bytes())
    template = TreatmentOnlyConfig.model_validate({**template.model_dump(), "preregistration": out})
    record, _ = treatment_record(template)
    text = json.dumps(record, indent=2, allow_nan=False) + "\n"
    sha = hashlib.sha256(text.encode()).hexdigest()
    config = TreatmentOnlyConfig.model_validate(
        {**template.model_dump(), "preregistration": out, "preregistration_sha256": sha}
    )
    write_new({out: text, out.parent / "treatment.json": config.model_dump_json(indent=2) + "\n"})
    out.chmod(0o444)
    (out.parent / "treatment.json").chmod(0o444)


def verify_treatment(
    config: TreatmentOnlyConfig,
    *,
    tokenizer_context: ChatTokenizerContext | None = None,
) -> tuple[dict[str, Any], tuple[list[Row], list[Row]]]:
    private_path(config.preregistration)
    if file_sha256(config.preregistration) != config.preregistration_sha256:
        raise DatasetError("treatment preregistration sha256 differs; never rewrite or repin")
    record, rows = treatment_record(config, tokenizer_context)
    saved = json.loads(config.preregistration.read_bytes())
    if saved != record:
        raise DatasetError(
            "treatment preregistration, source, protocol, historical artifact or output "
            "binding changed"
        )
    return saved, rows


def treatment_artifacts(config: TreatmentOnlyConfig) -> dict[str, dict[str, str]]:
    paths = {
        "adapter": config.run_dir / "adapter/adapters.safetensors",
        "checkpoint": config.run_dir / "adapter/0000060_adapters.safetensors",
        "adapter_config": config.run_dir / "adapter/adapter_config.json",
    }
    artifacts = {
        key: {"path": str(path), "sha256": file_sha256(path)} for key, path in paths.items()
    }
    if artifacts["adapter"]["sha256"] != artifacts["checkpoint"]["sha256"]:
        raise DatasetError("final treatment adapter differs from update-60 checkpoint")
    return artifacts


def pair_design() -> dict[str, Any]:
    return {
        "reference": {"arm": "D", "bias_correction": False},
        "treatment": {"arm": "D", "bias_correction": True},
        "validation_steps": list(VALIDATION_STEPS),
        "validation_aggregation": "unchanged arithmetic mean of64 singleton row losses",
        "checkpoint": "final update60 only; no best-checkpoint selection or promotion",
        "environment": {
            name: version(name) for name in ("mlx", "mlx-lm", "transformers", "tokenizers", "numpy")
        },
        "hypothesis": "Corrected moments may reduce early update magnitude; quality may worsen.",
        "stability": "Both arms: finite; final<=1.10*initial; every<=1.25*initial; "
        f"max train<={PAIR_MAX_TRAIN} (frozen historical A comparison ceiling).",
        "stop_conditions": STOPS,
    }


def supervised_budget(
    config: BiasCorrectionConfig,
    rows: tuple[list[Row], list[Row]],
    *,
    tokenizer_context: ChatTokenizerContext | None = None,
) -> dict[str, int]:
    owns_context = tokenizer_context is None
    context = tokenizer_context or open_chat_tokenizer(config.model_dir)
    try:
        encode = encoder(config.model_dir, context)
        budget = {
            "updates": config.iters,
            "examples": len(rows[0]),
            "supervised_tokens": sum(
                len(tokens) - max(1, offset) for tokens, offset in map(encode, rows[0])
            ),
        }
    finally:
        try:
            context.assert_current(config.model_dir)
        finally:
            if owns_context:
                context.release()
    if budget != {"updates": 60, "examples": FIT_ROWS, "supervised_tokens": PAIR_SUPERVISED_TOKENS}:
        raise DatasetError("matched pair token budget differs from frozen D exposure")
    return budget


def preregister_bias_correction(plan_path: Path, out: Path) -> None:
    """Freeze one matched pair without loading weights or running resource preflight."""
    out = private_path(out)
    template = BiasCorrectionConfig.model_validate_json(plan_path.read_bytes())
    identity = identities(template)
    selected, rows = selections(template)
    record = {
        "schema": 2,
        "inputs": common_inputs(template),
        "pair": pair_design(),
        "identity": identity,
        "selection": selected,
        "budget": supervised_budget(template, rows),
        "implementation": implementations(),
    }
    text = json.dumps(record, indent=2, allow_nan=False) + "\n"
    sha = hashlib.sha256(text.encode()).hexdigest()
    files = {out: text}
    for arm in ("reference", "treatment"):
        config = BiasCorrectionConfig.model_validate(
            {
                **template.model_dump(),
                "pair_arm": arm,
                "run_dir": out.parent / "runs" / arm,
                "preregistration": out,
                "preregistration_sha256": sha,
            }
        )
        files[out.parent / f"{arm}.json"] = config.model_dump_json(indent=2) + "\n"
    write_new(files)
    for path in files:
        path.chmod(0o444)


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


def _evaluate_validation(
    model: Any,
    dataset: Any,
    provenance: list[ValidationProvenance] | None = None,
) -> tuple[float, int, list[smoke.ValidationRowDiagnostic]]:
    """Run the single ordered singleton evaluation path, optionally recording row metadata."""
    if len(dataset) != VALID_ROWS:
        raise DatasetError("validation requires all64 rows")
    identifiers: list[str] | None = None
    if provenance is not None:
        if len(provenance) != VALID_ROWS:
            raise DatasetError("validation diagnostics require all64 rows and provenance records")
        raw_identifiers = [item.get("row_id") for item in provenance]
        if any(not isinstance(value, str) or not value for value in raw_identifiers):
            raise DatasetError("validation provenance requires nonempty row identifiers")
        identifiers = raw_identifiers
        if len(set(identifiers)) != VALID_ROWS:
            raise DatasetError("validation provenance row identifiers must be unique")
    values: list[float] = []
    records: list[smoke.ValidationRowDiagnostic] = []
    model.eval()
    try:
        for index, batch in enumerate(ordered_batches(dataset, 1, 2048)):
            value, ntoks = smoke.exclude_padding_loss(model, *batch)
            number = float(value.item())
            token_count = int(ntoks.item())
            if token_count <= 0:
                raise DatasetError("empty validation supervision")
            if provenance is not None:
                tokens, offset = dataset[index]
                supervised = len(tokens) - max(1, offset)
                if token_count != supervised:
                    raise DatasetError(
                        f"validation row {index} supervision count differs from tokens"
                    )
                canonical_id = provenance[index].get("canonical_id")
                if "canonical_id" not in provenance[index] or (
                    canonical_id is not None and not isinstance(canonical_id, str)
                ):
                    raise DatasetError(f"validation row {index} canonical_id is malformed")
            else:
                canonical_id = None
            values.append(number)
            if provenance is not None and identifiers is not None:
                records.append(
                    {
                        "row_id": identifiers[index],
                        "loss": number,
                        "supervised_tokens": token_count,
                        "canonical_id": canonical_id,
                    }
                )
            if not math.isfinite(number):
                return number, len(values), records
    finally:
        model.train()
    if len(values) != VALID_ROWS:
        raise DatasetError("validation did not evaluate all64 rows")
    return math.fsum(values) / VALID_ROWS, len(values), records


def validation_mean(model: Any, dataset: Any) -> tuple[float, int]:
    """Arithmetic mean of 64 singleton row losses, with dropout disabled."""
    mean, count, _ = _evaluate_validation(model, dataset)
    return mean, count


def validation_report(
    model: Any, dataset: Any, provenance: list[ValidationProvenance]
) -> tuple[float, int, list[smoke.ValidationRowDiagnostic]]:
    """Evaluate rows once and attach diagnostics to each already-computed loss."""
    return _evaluate_validation(model, dataset, provenance)


def fit_control(
    config: DiagnosticConfig,
    selection: dict[str, Any],
    validation_rows: list[Row],
) -> smoke.FitControl:
    def validate(args: Any) -> None:
        if vars(args) != smoke.training_options(config):
            raise DatasetError("diagnostic fit options differ from fixed config")

    def record(kind: str, info: dict[str, Any]) -> None:
        # Append before finite checks can abort; partial evidence survives a failed child.
        with (config.run_dir / "events.jsonl").open("a") as handle:
            event = {"kind": kind, **info}
            event["nonfinite"] = has_nonfinite(event)
            handle.write(json.dumps(json_safe(event), allow_nan=False) + "\n")
        if (
            isinstance(config, BiasCorrectionConfig)
            and kind == "train"
            and info["iteration"] == 60
            and info.get("trained_tokens") != PAIR_SUPERVISED_TOKENS
        ):
            raise DatasetError("matched pair final token budget differs")

    indices = selection.get("permuted")
    if (
        not isinstance(indices, list)
        or len(indices) != VALID_ROWS
        or len(validation_rows) != VALID_ROWS
    ):
        raise DatasetError("validation provenance must match the preregistered64-row selection")
    provenance: list[ValidationProvenance] = []
    for index, row in zip(indices, validation_rows, strict=True):
        if not isinstance(index, int) or index < 0:
            raise DatasetError("validation selection index is malformed")
        answer = NormalizationAnswer.model_validate_json(row["messages"][-1]["content"])
        provenance.append({"row_id": f"{index}:{digest(row)}", "canonical_id": answer.canonical_id})

    def validation(
        model: Any, dataset: Any
    ) -> tuple[float, int, list[smoke.ValidationRowDiagnostic]]:
        return validation_report(model, dataset, provenance)

    return smoke.FitControl(validate, validation, ordered_batches, record, VALIDATION_STEPS)


def worker(config: DiagnosticConfig) -> None:
    context = open_chat_tokenizer(config.model_dir)
    try:
        record, rows = verify_preregistration(config, tokenizer_context=context)
        write_new(
            {
                config.run_dir / "worker-verification.json": json.dumps(
                    {"identity": record["identity"], "selection": record["selection"]},
                    separators=(",", ":"),
                )
                + "\n"
            }
        )
        control = fit_control(config, record["selection"]["valid"], rows[1])
        smoke.train_local(
            config,
            selection=rows,
            control=control,
            tokenizer_context=context,
        )
    finally:
        context.release()


class SelectionStats(Contract):
    rows: int = Field(ge=1)
    null_share: float = Field(ge=0, le=1, allow_inf_nan=False)
    class_counts: dict[str, int] = Field(min_length=1)


class DiagnosticSelection(Contract):
    source_rows: int = Field(ge=1)
    candidates: list[int]
    selected: list[int]
    permuted: list[int]
    skipped_over_cap: list[int]
    skipped_over_cap_count: int = Field(ge=0)
    collisions: int = Field(ge=0)
    selected_rows_sha256: Sha256
    permuted_rows_sha256: Sha256
    row_sha256: list[Sha256]
    stats: SelectionStats

    def check_count(self, count: int) -> None:
        if (
            self.source_rows < count
            or self.candidates != [k * (self.source_rows - 1) // (count - 1) for k in range(count)]
            or len(self.selected) != count
            or self.selected != sorted(set(self.selected))
            or len(self.permuted) != count
            or set(self.permuted) != set(self.selected)
            or len(self.row_sha256) != count
            or self.skipped_over_cap != sorted(set(self.skipped_over_cap))
            or self.skipped_over_cap_count != len(self.skipped_over_cap)
            or any(i < 0 or i >= self.source_rows for i in self.selected + self.skipped_over_cap)
            or self.stats.rows != count
            or any(value <= 0 for value in self.stats.class_counts.values())
            or sum(self.stats.class_counts.values()) != count
            or self.stats.null_share != self.stats.class_counts.get("null", 0) / count
        ):
            raise DatasetError("worker selection has invalid indices, counts or statistics")


def _read_worker_verification(path: Path, config: DiagnosticConfig) -> dict[str, Any]:
    verification = json.loads(path.read_bytes())
    if not isinstance(verification, dict) or set(verification) != {"identity", "selection"}:
        raise DatasetError("invalid worker verification object")
    smoke.validate_worker_identity(verification["identity"], config)
    selection = verification["selection"]
    if not isinstance(selection, dict) or set(selection) != {"fit", "valid"}:
        raise DatasetError("invalid worker selection object")
    for split, count in (("fit", FIT_ROWS), ("valid", VALID_ROWS)):
        DiagnosticSelection.model_validate(selection[split]).check_count(count)
    # This is a small saved JSON record, not input/tokenizer/model verification.
    registration_bytes = config.preregistration.read_bytes()
    if hashlib.sha256(registration_bytes).hexdigest() != config.preregistration_sha256:
        raise DatasetError("worker verification preregistration sha256 differs")
    registration = json.loads(registration_bytes)
    if not isinstance(registration, dict) or any(
        verification[name] != registration.get(name) for name in ("identity", "selection")
    ):
        raise DatasetError("worker verification differs from saved preregistration metadata")
    return verification


def run(config: DiagnosticConfig, *, dry_run: bool) -> int:
    config = type(config).model_validate(config)
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
        if dry_run:
            context = open_chat_tokenizer(config.model_dir)
            try:
                record, _ = verify_preregistration(config, tokenizer_context=context)
            finally:
                context.release()
            report.update({"identity": record["identity"], "selection": record["selection"]})
        attempt_arm = config.pair_arm if isinstance(config, BiasCorrectionConfig) else config.arm
        attempt = private_path(
            config.preregistration.parent
            / "attempts"
            / f"{attempt_arm}-{'dry' if dry_run else 'train'}.json"
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
        state = smoke.check_and_record_resources(
            run_dir, stage="controller-readiness", probe=smoke.probe_resources
        )
        report["preflight"] = state.model_dump(mode="json")
        if dry_run:
            report["status"] = "preflight_passed"
        else:
            launcher_started = time.monotonic()
            try:
                report["resources"] = smoke.launch_trainer(config, script=Path(__file__))
            finally:
                report["trainer_launcher_elapsed_seconds"] = time.monotonic() - launcher_started
            report["metrics"] = json.loads((run_dir / "metrics.json").read_bytes())
            validate_metrics(report["metrics"])
            if isinstance(config, TreatmentOnlyConfig):
                validate_treatment_exposure(report["metrics"])
            check_resources(report["resources"])
            if isinstance(config, TreatmentOnlyConfig):
                report["treatment_artifacts"] = treatment_artifacts(config)
            report["status"] = "diagnostic_completed"
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        print(f"error: {exc}", file=sys.stderr)
    finally:
        counters = run_dir / "resources.json"
        if counters.is_file():
            try:
                resources = json.loads(counters.read_bytes())
                if not isinstance(resources, dict) or not {
                    "peak_mlx_bytes",
                    "peak_rss_bytes",
                }.issubset(resources):
                    raise ValueError("resource artifact has an invalid object shape")
                report["resources"] = resources
            except (OSError, ValueError) as exc:
                report["artifact_errors"].append(f"resources.json: {exc}")
        events_path = run_dir / "events.jsonl"
        verification_path = run_dir / "worker-verification.json"
        if verification_path.is_file():
            try:
                verification = _read_worker_verification(verification_path, config)
                report["identity"] = verification["identity"]
                report["selection"] = verification["selection"]
                report["verification_status"] = "verified_in_worker"
            except (OSError, ValueError) as exc:
                report["artifact_errors"].append(f"worker-verification.json: {exc}")
                report["verification_status"] = "worker_verification_invalid"
        elif not dry_run:
            report["verification_status"] = "worker_verification_not_completed"
            report["artifact_errors"].append("worker-verification.json: missing")
        samples = run_dir / "resource-samples.jsonl"
        if samples.is_file():
            report["resource_samples"] = _read_resource_samples(samples, report["artifact_errors"])
            controller_samples = [
                sample
                for sample in report["resource_samples"]
                if sample.get("stage") == "controller-readiness"
            ]
            if controller_samples and "preflight" not in report:
                report["preflight"] = controller_samples[-1]
        if events_path.is_file():
            events = []
            try:
                with events_path.open("rb") as handle:
                    for number, line in enumerate(handle, 1):
                        try:
                            event = json.loads(line.decode("utf-8"))
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


def validate_treatment_exposure(metrics: dict[str, Any]) -> None:
    validate_metrics(metrics)
    train, validation = series(metrics, "train"), series(metrics, "validation")
    if (
        len(train) != 60
        or train[-1].get("trained_tokens") != PAIR_SUPERVISED_TOKENS
        or len(validation) != len(VALIDATION_STEPS)
        or any(entry.get("rows") != VALID_ROWS for entry in validation)
    ):
        raise DatasetError("treatment exposure differs from frozen 60-update/2161-token budget")


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


def evaluate_bias_correction(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Reuse the diagnostic criteria; generated quality remains separately unmeasured."""
    by_arm: dict[str, dict[str, Any]] = {}
    shared: dict[str, Any] = {}
    for report in reports:
        config = BiasCorrectionConfig.model_validate_json(json.dumps(report["config"]))
        if config.pair_arm in by_arm:
            raise DatasetError("duplicate bias-correction pair arm")
        by_arm[config.pair_arm] = report
        for key, value in {
            "inputs": common_inputs(config),
            **{k: report.get(k) for k in ("identity", "selection", "preregistration_sha256")},
        }.items():
            if not value or (key in shared and shared[key] != value):
                raise DatasetError(f"bias-correction pair {key} mismatch")
            shared[key] = value
        entries = series(report.get("metrics", {}), "train")
        if not entries or entries[-1].get("trained_tokens") != PAIR_SUPERVISED_TOKENS:
            raise DatasetError("bias-correction pair token budget differs")
    if set(by_arm) != {"reference", "treatment"}:
        raise DatasetError("require exactly reference and treatment")
    results = {arm: summarize_arm(report, 2 * PAIR_MAX_TRAIN) for arm, report in by_arm.items()}
    return {
        "arms": results,
        "both_stable": all(r["outcome"] == "stable on this sample" for r in results.values()),
        "generated_quality": "NOT MEASURED",
        "promotion": "not authorized by teacher-forced stability",
    }


def evaluate_treatment(report: dict[str, Any]) -> dict[str, Any]:
    config = TreatmentOnlyConfig.model_validate_json(json.dumps(report["config"]))
    record, _ = verify_treatment(config)
    if (
        report.get("training_options") != smoke.training_options(config)
        or report.get("arm") != "D"
        or report.get("preregistration_sha256") != config.preregistration_sha256
        or report.get("identity") != record["identity"]
        or report.get("selection") != record["selection"]
        or report.get("implementation") != implementations()
        or report.get("environment") != record["environment"]
        or report.get("training_options") != record["training_options"]
    ):
        raise DatasetError("treatment report differs from frozen treatment registration")
    validate_treatment_exposure(report.get("metrics", {}))
    expected_artifacts = treatment_artifacts(config)
    if report.get("treatment_artifacts") != expected_artifacts:
        raise DatasetError(
            "treatment adapter/config/checkpoint artifacts differ from report hashes"
        )
    launcher_seconds = finite_number(report.get("trainer_launcher_elapsed_seconds"), positive=True)
    if launcher_seconds > config.timeout_seconds:
        raise DatasetError("trainer launcher elapsed time exceeds frozen 1500-second bound")
    summary = summarize_arm(report, 2 * TREATMENT_MAX_TRAIN)
    # summarize_arm's shared helper uses the historical A ratio; replace only its
    # named criterion with the treatment's direct, preregistered absolute ceiling.
    train_max_value = train_max(report.get("metrics", {}))
    summary["criteria"]["train_max_vs_A"] = (
        train_max_value <= TREATMENT_MAX_TRAIN if train_max_value is not None else NOT_MEASURED
    )
    summary["criteria"]["trainer_launcher_upper_bound"] = launcher_seconds <= config.timeout_seconds
    accepted = summary["outcome"] == "stable on this sample" and all(
        summary["criteria"][key] is True
        for key in (
            "no_nonfinite",
            "final_validation",
            "every_validation",
            "train_max_vs_A",
            "trainer_launcher_upper_bound",
        )
    )
    return {
        "schema": 1,
        "mode": "treatment_only_historical_exploratory_comparator",
        "treatment": summary,
        "treatment_accepted_for_conditional_generation": accepted,
        "historical_comparator": record["historical_comparator"],
        "historical_comparator_complete": True,
        "both_stable": "NOT APPLICABLE: no contemporaneous reference arm",
        "generation": (
            "NOT AUTHORIZED unless treatment alone passes integrity, exposure, stability, "
            "and resource gates"
        ),
        "promotion": "not authorized by teacher-forced treatment stability",
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
    action.add_argument("--preregister-bias-correction", type=Path, metavar="PLAN")
    action.add_argument("--preregister-treatment", type=Path, metavar="PLAN")
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
        elif args.preregister_bias_correction:
            preregister_bias_correction(args.preregister_bias_correction, args.out)
        elif args.preregister_treatment:
            preregister_treatment(args.preregister_treatment, args.out)
        elif args.evaluate:
            reports = [json.loads(path.read_bytes()) for path in args.evaluate]
            if len(reports) == 1 and "historical_report" in reports[0].get("config", {}):
                result = evaluate_treatment(reports[0])
                write_new({args.out: json.dumps(result, indent=2, allow_nan=False) + "\n"})
                return 0
            for report in reports:
                config = (
                    TreatmentOnlyConfig
                    if "historical_report" in report["config"]
                    else BiasCorrectionConfig
                    if "pair_arm" in report["config"]
                    else DiagnosticConfig
                ).model_validate_json(json.dumps(report["config"]))
                record, _ = verify_preregistration(config)
                if (
                    report["training_options"] != smoke.training_options(config)
                    or report["arm"] != config.arm
                    or report["preregistration_sha256"] != config.preregistration_sha256
                    or ("selection" in report and report["selection"] != record["selection"])
                    or ("identity" in report and report["identity"] != record["identity"])
                ):
                    raise DatasetError("report differs from preregistered configuration")
            result = (
                evaluate_bias_correction(reports)
                if any("pair_arm" in r["config"] for r in reports)
                else evaluate_reports(reports)
            )
            write_new(
                {private_path(args.out): json.dumps(result, indent=2, allow_nan=False) + "\n"}
            )
        else:
            raw = args.config.read_bytes()
            config = (
                TreatmentOnlyConfig
                if "historical_report" in json.loads(raw)
                else BiasCorrectionConfig
                if "pair_arm" in json.loads(raw)
                else DiagnosticConfig
            ).model_validate_json(raw)
            if args.worker:
                if args.config.resolve() != config.run_dir.resolve() / "config.json":
                    raise DatasetError("worker requires saved run config")
                marker = json.loads((config.run_dir / "supervisor-started.json").read_bytes())
                if marker.get("pid") != os.getppid():
                    raise DatasetError("worker was not started by its registered supervisor")

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
