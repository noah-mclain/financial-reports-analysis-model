"""Reconstruct authorized validation rows, freeze a selection, and compare predictions."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from build_dataset import _roster_moves, read_approved, read_jsonl
from dataset_builder import (
    BUILDER_VERSION,
    SYSTEM_PROMPT,
    DatasetBuildManifest,
    dedupe_per_issuer,
    render_row,
)
from dataset_contracts import (
    Contract,
    DatasetError,
    NormalizationExample,
    PartitionConfig,
    Sha256,
    SourceRecord,
    _taxonomy_digest,
    digest,
    partition_examples,
)
from dataset_exclusions import FrozenExclusions, load_exclusions, retained_indices, verify_source
from exclusive_write import write_new
from harness.holdout_records import read_looks, read_moves
from normalization_eval import EvalRow, Prediction, compare, population_report, score, select_rows
from progress import Progress
from pydantic import Field, ValidationError, model_validator
from sec_source_access import read_registry

from fra_core.pools import PoolError
from fra_core.split import SplitError, cik_key
from fra_core.taxonomy import load_taxonomy
from fra_ingest.label_match import LabelIndex


class Selection(Contract):
    part: Literal["validation"] = "validation"
    dataset_manifest: DatasetBuildManifest
    available_rows: int = Field(ge=1)
    seed: int
    rows: tuple[EvalRow, ...]

    @model_validator(mode="after")
    def _valid(self) -> Selection:
        if not self.rows or len(self.rows) > self.available_rows:
            raise ValueError("selection size differs from available validation rows")
        if self.available_rows != self.dataset_manifest.validation_rows_after:
            raise ValueError("available rows differ from dataset manifest")
        if len({r.example_id for r in self.rows}) != len(self.rows):
            raise ValueError("duplicate evaluation example ids")
        return self

    def fingerprint(self) -> str:
        return digest(self.model_dump(mode="json"))


class PredictionRun(Contract):
    selection_sha256: Sha256
    predictor: str
    metadata: dict[str, Any]
    predictions: tuple[Prediction, ...]


def file_sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def execution_metadata() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    paths = (
        "training/normalization_eval.py",
        "training/normalization_validation.py",
        "training/normalization_mlx.py",
        "training/dataset_builder.py",
        "training/dataset_exclusions.py",
        "training/dataset_contracts.py",
        "packages/ingest/src/fra_ingest/label_match.py",
        "pyproject.toml",
        "uv.lock",
    )
    return {
        "checkout": str(root),
        "git_head": result.stdout.strip(),
        "command": sys.argv,
        "python_version": platform.python_version(),
        "implementation_sha256": {p: file_sha256(root / p) for p in paths},
    }


def _verify_rendered(
    path: Path, examples: Sequence[NormalizationExample], expected_sha: str, name: str
) -> None:
    if file_sha256(path) != expected_sha:
        raise DatasetError(f"{name}: dataset bytes differ from manifest sha256")
    with (
        path.open("rb") as handle,
        Progress(f"normalization verify rendered {name}", total=len(examples)) as progress,
    ):
        for example in examples:
            expected = (
                json.dumps(render_row(example), ensure_ascii=False, allow_nan=False) + "\n"
            ).encode("utf-8")
            if handle.readline() != expected:
                raise DatasetError(f"{example.example_id}: rendered {name} differs from dataset")
            progress.advance()
        if handle.read(1):
            raise DatasetError(f"rendered {name}: extra dataset rows")


def prepare_validation(
    *,
    dataset_dir: Path,
    examples_path: Path,
    sources_path: Path,
    approved_path: Path,
    registry_path: Path,
    moves_path: Path,
    looks_path: Path,
    max_per_issuer: int | None = None,
    limit: int | None = None,
    seed: int = 0,
    exclusions: FrozenExclusions | None = None,
) -> Selection:
    """Check metadata before opening labels, then reproduce the original build identities."""
    registry = read_registry(registry_path)
    sources = read_jsonl(sources_path, SourceRecord)
    approved = read_approved(approved_path)
    looks = read_looks(looks_path)
    moves = _roster_moves(read_moves(moves_path, looks), sources)
    taxonomy = load_taxonomy()
    config = PartitionConfig(approved_example_sha256=approved, max_per_issuer=max_per_issuer)
    # Empty supervision validates the authoritative roster and registry before label access.
    partition_examples(
        (),
        sources=sources,
        registry=registry,
        config=config,
        taxonomy=taxonomy,
        moves=moves,
        looks=looks,
    )
    manifest = DatasetBuildManifest.model_validate_json(
        (dataset_dir / "manifest.json").read_bytes()
    )
    if manifest.builder_version != BUILDER_VERSION:
        raise DatasetError("unsupported dataset builder version")
    prompt_sha = hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()
    if manifest.system_prompt_sha256 != prompt_sha:
        raise DatasetError("dataset system prompt sha256 differs")
    examples = read_jsonl(examples_path, NormalizationExample)
    with Progress("normalization reconstruct approved partition", total=3 * len(examples)) as p:
        partitioned = partition_examples(
            examples,
            sources=sources,
            registry=registry,
            config=config,
            taxonomy=taxonomy,
            moves=moves,
            looks=looks,
            on_example=p.advance,
        )
    if partitioned.manifest != manifest.dataset:
        raise DatasetError("reconstructed partition manifest differs from dataset")
    fit, validation = dedupe_per_issuer(partitioned.fit), dedupe_per_issuer(partitioned.validation)
    if exclusions is not None:
        verify_source(exclusions, manifest, allow_applied=True)
    if manifest.exclusion is not None:
        if exclusions is None:
            raise DatasetError("reconstructing excluded train requires --exclusions")
        indices = retained_indices(tuple(render_row(e) for e in fit), exclusions)
        fit = tuple(fit[i] for i in indices)
    if (len(fit), len(validation)) != (manifest.fit_rows_after, manifest.validation_rows_after):
        raise DatasetError("reconstructed row counts differ from dataset manifest")
    _verify_rendered(dataset_dir / "train.jsonl", fit, manifest.train_sha256, "fit")
    _verify_rendered(dataset_dir / "valid.jsonl", validation, manifest.valid_sha256, "validation")
    rows = tuple(
        EvalRow(
            example_id=e.example_id,
            issuer=cik_key(str(e.source_record.issuer.cik)),
            statement=e.statement,
            label=e.label,
            canonical_id=e.canonical_id,
            source=e.source_record.source.value,
            language=e.source_record.language,
            period=e.source_record.period,
        )
        for e in validation
    )
    return Selection(
        dataset_manifest=manifest,
        available_rows=len(rows),
        seed=seed,
        rows=select_rows(rows, limit=limit, seed=seed),
    )


def load_selection(path: Path) -> Selection:
    selection = Selection.model_validate_json(path.read_bytes())
    if selection.dataset_manifest.dataset.taxonomy_sha256 != _taxonomy_digest(load_taxonomy()):
        raise DatasetError("selection taxonomy differs from current taxonomy")
    if (
        selection.dataset_manifest.system_prompt_sha256
        != hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()
    ):
        raise DatasetError("selection prompt differs from current dataset prompt")
    return selection


def lexicon_run(selection: Selection) -> PredictionRun:
    index = LabelIndex(load_taxonomy())
    predictions = []
    with Progress("normalization lexicon", total=len(selection.rows)) as progress:
        for row in selection.rows:
            item = index.match(row.label, row.statement)
            predictions.append(
                Prediction(
                    example_id=row.example_id,
                    response=json.dumps({"canonical_id": item.id if item else None}),
                )
            )
            progress.advance()
    return PredictionRun(
        selection_sha256=selection.fingerprint(),
        predictor="lexicon",
        metadata={"implementation": "LabelIndex.match, unchanged", **execution_metadata()},
        predictions=tuple(predictions),
    )


def score_run(
    selection: Selection, run: PredictionRun, exclusions: FrozenExclusions | None = None
) -> dict[str, Any]:
    if run.selection_sha256 != selection.fingerprint():
        raise DatasetError("predictions belong to a different selection sha256")
    result = {
        "selection_sha256": run.selection_sha256,
        "predictor": run.predictor,
        "metadata": run.metadata,
        "available_rows": selection.available_rows,
        "selection_seed": selection.seed,
        "scorer_execution": execution_metadata(),
        **score(selection.rows, run.predictions),
    }
    if exclusions is not None:
        verify_source(exclusions, selection.dataset_manifest, allow_applied=True)
        if len(selection.rows) != selection.available_rows:
            raise DatasetError("exclusion population reporting requires full validation, no limit")
        result["populations"] = population_report(selection.rows, run.predictions, exclusions)
    return result


def _json(value: Contract | dict[str, Any]) -> str:
    raw = value.model_dump(mode="json") if isinstance(value, Contract) else value
    return json.dumps(raw, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    for name in (
        "dataset-dir",
        "examples",
        "sources",
        "approved",
        "registry",
        "moves",
        "looks",
        "out-dir",
    ):
        prepare.add_argument(f"--{name}", type=Path, required=True)
    prepare.add_argument("--limit", type=int)
    prepare.add_argument("--seed", type=int, default=0)
    prepare.add_argument("--max-per-issuer", type=int)
    prepare.add_argument("--exclusions", type=Path)
    scoring = commands.add_parser("score")
    for name in ("selection", "predictions", "out"):
        scoring.add_argument(f"--{name}", type=Path, required=True)
    scoring.add_argument("--base", type=Path)
    scoring.add_argument("--seed", type=int, default=0)
    scoring.add_argument("--exclusions", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    started = time.monotonic()
    try:
        exclusions = load_exclusions(args.exclusions) if args.exclusions else None
        if args.command == "prepare":
            if args.out_dir.exists():
                raise DatasetError("evaluation output directory already exists")
            selected = prepare_validation(
                dataset_dir=args.dataset_dir,
                examples_path=args.examples,
                sources_path=args.sources,
                approved_path=args.approved,
                registry_path=args.registry,
                moves_path=args.moves,
                looks_path=args.looks,
                max_per_issuer=args.max_per_issuer,
                limit=args.limit,
                seed=args.seed,
                exclusions=exclusions,
            )
            run = lexicon_run(selected)
            report = score_run(selected, run, exclusions)
            report["elapsed_seconds"] = time.monotonic() - started
            write_new(
                {
                    args.out_dir / "selection.json": _json(selected),
                    args.out_dir / "lexicon.json": _json(run),
                    args.out_dir / "report.json": _json(report),
                }
            )
        else:
            selected = load_selection(args.selection)
            run = PredictionRun.model_validate_json(args.predictions.read_bytes())
            report = score_run(selected, run, exclusions)
            if args.base:
                base = PredictionRun.model_validate_json(args.base.read_bytes())
                if base.selection_sha256 != selected.fingerprint():
                    raise DatasetError("base predictions belong to a different selection")
                report["paired_comparison"] = compare(
                    selected.rows, base.predictions, run.predictions, seed=args.seed
                )
            write_new({args.out: _json(report)})
    except (DatasetError, SplitError, PoolError, OSError, ValueError, ValidationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"normalization {args.command} finished in {time.monotonic() - started:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
