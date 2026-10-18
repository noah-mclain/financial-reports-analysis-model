"""Offline MLX predictions on a frozen validation selection; no evaluation targets in prompts."""

from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import time
from collections.abc import Callable, Sequence
from importlib.metadata import version
from pathlib import Path
from typing import Any, Literal

from chat_tokenizer import load_chat_counter
from dataset_contracts import Contract, DatasetError, Sha256, canonical_json, digest
from exclusive_write import write_new
from normalization_eval import Prediction, inference_messages
from normalization_validation import (
    PredictionRun,
    Selection,
    execution_metadata,
    file_sha256,
    load_selection,
)
from progress import Progress
from pydantic import Field


class LocalModelManifest(Contract):
    repository: Literal["mlx-community/Qwen2.5-7B-Instruct-4bit"]
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    local_directory: str
    tokenizer: str
    artifact_sha256: dict[str, Sha256]


def model_identity(
    model_dir: Path, *, expected_tokenizer: str, snapshot: LocalModelManifest
) -> dict[str, Any]:
    """Verify the caller's pinned Qwen 7B snapshot before loading any MLX weights."""
    if not model_dir.is_dir() or not (model_dir / "config.json").is_file():
        raise DatasetError(f"local model snapshot is missing: {model_dir}")
    config = json.loads((model_dir / "config.json").read_bytes())
    expected_shape = {
        "model_type": "qwen2",
        "hidden_size": 3584,
        "num_hidden_layers": 28,
        "vocab_size": 152064,
    }
    if not isinstance(config, dict) or any(config.get(k) != v for k, v in expected_shape.items()):
        raise DatasetError("local model is not the expected Qwen2.5-7B architecture")
    quantization = config.get("quantization", config.get("quantization_config", {}))
    if (
        not isinstance(quantization, dict)
        or quantization.get("bits") != 4
        or config.get("auto_map")
    ):
        raise DatasetError("local model must be 4-bit and use a built-in model implementation")
    weights = sorted(model_dir.glob("*.safetensors"))
    if not weights:
        raise DatasetError("local model has no safetensors weights")
    _, tokenizer_id = load_chat_counter(model_dir)
    if tokenizer_id != expected_tokenizer:
        raise DatasetError("local model tokenizer differs from dataset tokenizer")
    artifacts = sorted(
        set(weights)
        | set(model_dir.glob("*.json"))
        | set(model_dir.glob("*.jinja"))
        | set(model_dir.glob("*.txt"))
    )
    hashes = {p.name: file_sha256(p) for p in artifacts}
    if hashes != snapshot.artifact_sha256:
        raise DatasetError("local model artifact hashes differ from pinned model manifest")
    return {
        "model_sha256": digest(hashes),
        "artifact_sha256": hashes,
        "tokenizer_id": tokenizer_id,
        "model_repository": snapshot.repository,
        "model_revision": snapshot.revision,
        "model_manifest_sha256": digest(snapshot.model_dump(mode="json")),
    }


def predict_rows(
    selection: Selection,
    generate: Callable[[list[dict[str, str]]], str],
    *,
    predictor: str,
    metadata: dict[str, Any],
) -> PredictionRun:
    started = time.monotonic()
    cache: dict[str, str] = {}
    predictions = []
    with Progress("normalization local inference", total=len(selection.rows)) as progress:
        for row in selection.rows:
            messages = inference_messages(row)
            key = canonical_json(messages)
            if key not in cache:
                cache[key] = generate(messages)
            predictions.append(Prediction(example_id=row.example_id, response=cache[key]))
            progress.advance(check_clock=True)
    elapsed = time.monotonic() - started
    return PredictionRun(
        selection_sha256=selection.fingerprint(),
        predictor=predictor,
        metadata={
            **metadata,
            **execution_metadata(),
            "unique_prompts": len(cache),
            "rows": len(predictions),
            "elapsed_seconds": elapsed,
            "rows_per_second": len(predictions) / elapsed,
        },
        predictions=tuple(predictions),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("selection", "model-dir", "model-manifest", "out"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--adapter-dir", type=Path)
    parser.add_argument("--max-tokens", type=int, default=128)
    args = parser.parse_args(argv)
    try:
        if sys.platform != "darwin":
            raise DatasetError("MLX runner requires macOS")
        if args.out.exists() or args.out.is_symlink():
            raise DatasetError("prediction output already exists")
        if args.max_tokens < 1:
            raise DatasetError("max-tokens must be positive")
        selection = load_selection(args.selection)
        model_dir = args.model_dir.resolve()
        metadata = model_identity(
            model_dir,
            expected_tokenizer=selection.dataset_manifest.tokenizer_id,
            snapshot=LocalModelManifest.model_validate_json(args.model_manifest.read_bytes()),
        )
        metadata.update(
            {
                "mlx_version": version("mlx"),
                "mlx_lm_version": version("mlx-lm"),
                "max_tokens": args.max_tokens,
                "temperature": 0,
            }
        )
        adapter = None
        if args.adapter_dir:
            adapter = args.adapter_dir.resolve()
            if not (adapter / "adapter_config.json").is_file():
                raise DatasetError("adapter_config.json is missing")
            weights = sorted(adapter.glob("*.safetensors"))
            if not weights:
                raise DatasetError("adapter weights are missing")
            metadata["adapter_sha256"] = digest(
                {p.name: file_sha256(p) for p in [adapter / "adapter_config.json", *weights]}
            )
        # A local path is passed to load, and offline mode also prevents implicit HF access.
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        import mlx.core as mx
        from mlx_lm import generate, load
        from mlx_lm.sample_utils import make_sampler

        mx.set_cache_limit(1_000_000_000)
        mx.reset_peak_memory()
        loaded = load(
            str(model_dir),
            adapter_path=str(adapter) if adapter else None,
            tokenizer_config={"trust_remote_code": False, "local_files_only": True},
        )
        model: Any = loaded[0]
        tokenizer: Any = loaded[1]
        del loaded
        sampler = make_sampler(temp=0)

        def answer(messages: list[dict[str, str]]) -> str:
            prompt = tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            )
            response: str = generate(
                model,
                tokenizer,
                prompt=prompt,
                sampler=sampler,
                max_tokens=args.max_tokens,
                verbose=False,
            )
            return response

        run = predict_rows(
            selection, answer, predictor="mlx_adapter" if adapter else "mlx_base", metadata=metadata
        )
        metadata = {
            **run.metadata,
            "peak_mlx_bytes": mx.get_peak_memory(),
            "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        }
        run = run.model_copy(update={"metadata": metadata})
        write_new({args.out: run.model_dump_json(indent=2) + "\n"})
        print(f"predictions: {len(run.predictions)}, unique prompts: {metadata['unique_prompts']}")
        model = None
        tokenizer = None
        mx.clear_cache()
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
