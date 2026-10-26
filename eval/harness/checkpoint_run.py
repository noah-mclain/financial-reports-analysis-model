"""Owner-run English checkpoint pools. No reference-answer accuracy is inferred here.

    PYTHONPATH=eval uv run python -m harness.checkpoint_run model_test --checkpoint
    PYTHONPATH=eval uv run python -m harness.checkpoint_run blind --checkpoint

Only synthetic fixtures exercise this runner during development. A continuation keeps the
original marker and log row and requires the exact commit, document list and configuration.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import multiprocessing
import os
import re
import shutil
import signal
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

from fra_core.schemas import StatementType
from fra_core.split import issuer_key
from fra_ingest.config import REPO_ROOT, IngestConfig, load_config
from fra_ingest.footprint import peak_footprint_reading
from fra_ingest.pages import sha256_file
from fra_ingest.results import StructureResult
from harness import checkpoint_records as records
from harness.dry_run import (
    ConvertStage,
    Stages,
    budget_s,
    failure_class,
    pool_layers,
    projection,
    real_stages,
    run_document,
)
from harness.gates import NO_REFERENCE, identity_outcome
from harness.language_scope import GATED_LANGUAGE, manifest_language
from harness.mapping import SLOT_STATES
from harness.paths import STRATA
from harness.structure import first_statements

RESERVE_SIZE = 8
RESERVE_RULE = (
    'sha256("rehearsal-reserve-v1:" + issuer_key) hex ascending; first 8 English blind issuers'
)
_ID = re.compile(r"[a-z0-9][a-z0-9-]*")
Execute = Callable[[dict[str, Any], str, Path, IngestConfig], dict[str, Any]]


def reserve_issuers(documents: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """D22: choose issuers from manifest language alone, before any result is available."""
    keys = {
        issuer_key(d["issuer"])
        for d in documents
        if d["pool"] == "blind" and manifest_language(d) == GATED_LANGUAGE
    }
    if "" in keys or len(keys) < RESERVE_SIZE:
        raise records.CheckpointError(
            "reservation requires at least 8 distinct English blind issuers"
        )
    return tuple(
        sorted(
            keys,
            key=lambda k: hashlib.sha256(("rehearsal-reserve-v1:" + k).encode("utf-8")).hexdigest(),
        )[:RESERVE_SIZE]
    )


def load_metadata(root: Path) -> tuple[list[dict[str, Any]], dict[str, Any], tuple[str, ...]]:
    folder = root / "eval/corpus"
    documents = yaml.safe_load((folder / "candidates.yaml").read_text())["documents"]
    fetched = yaml.safe_load((folder / "fetched.yaml").read_text())["documents"]
    reserve = yaml.safe_load((folder / "rehearsal_reserve.yaml").read_text())
    if reserve.get("rule") != RESERVE_RULE:
        raise records.CheckpointError("rehearsal reservation rule differs from D22")
    if tuple(reserve["issuers"]) != reserve_issuers(documents):
        raise records.CheckpointError(
            "rehearsal reservation differs from the fixed metadata hash rule"
        )
    return documents, fetched, tuple(reserve["issuers"])


def select_documents(
    pool: str,
    documents: Sequence[dict[str, Any]],
    fetched: Mapping[str, Any],
    reserve: tuple[str, ...],
) -> list[dict[str, Any]]:
    if pool not in ("model_test", "blind"):
        raise records.CheckpointError(f"unknown checkpoint pool {pool!r}")
    if reserve != reserve_issuers(documents):
        raise records.CheckpointError(
            "invalid rehearsal reservation; each issuer needs English documents"
        )
    selected = []
    seen: set[str] = set()
    for entry in documents:
        if entry["pool"] != pool or manifest_language(entry) != GATED_LANGUAGE:
            continue
        if pool == "blind" and issuer_key(entry["issuer"]) in reserve:
            continue
        doc_id = entry["id"]
        if not isinstance(doc_id, str) or not _ID.fullmatch(doc_id) or doc_id in seen:
            raise records.CheckpointError(f"invalid or duplicated document id {doc_id!r}")
        seen.add(doc_id)
        measured = fetched.get(doc_id, {})
        if measured.get("pool") != pool:
            raise records.CheckpointError(f"{doc_id}: missing fetched metadata or pool mismatch")
        if measured.get("text_layer") not in ("digital", "mixed", "scanned"):
            raise records.CheckpointError(f"{doc_id}: missing or invalid measured text layer")
        if not re.fullmatch("[a-f0-9]{64}", str(measured.get("sha256", ""))):
            raise records.CheckpointError(f"{doc_id}: missing or invalid fetched sha256")
        if entry.get("period") not in STRATA:
            raise records.CheckpointError(f"{doc_id}: missing or invalid period")
        selected.append(dict(entry))
    if not selected:
        raise records.CheckpointError(f"{pool}: no English documents selected")
    return sorted(selected, key=lambda d: d["id"])


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


def _write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def pipeline_document(
    entry: dict[str, Any],
    layer: str,
    pdf: Path,
    config: IngestConfig,
    *,
    stages: Stages | None = None,
) -> dict[str, Any]:
    """Reuse the dry-run stages and its count-only row; retain the identity's exact rule."""
    active = stages if stages is not None else real_stages(config)
    results: list[StructureResult] = []

    def structure(path: Path, cfg: IngestConfig, convert: ConvertStage) -> StructureResult:
        result = active.structure(path, cfg, convert)
        results.append(result)
        return result

    row = run_document(
        entry, layer, pdf, config, replace(active, structure=structure), time.perf_counter
    )
    row["identity_outcome"] = "no_balance"
    if row["failure"] is None and results:
        result = results[0]
        balance = first_statements(result, config.enabled_types).get(StatementType.BALANCE)
        review = next((r for r in result.reviews if balance and r.statement_id == balance.id), None)
        checks = active.checks(config, result.sha256)
        row["identity_outcome"] = identity_outcome(
            balance, checks.get(balance.id, []) if balance else [], review
        )
    return row


def _child(
    entry: dict[str, Any],
    layer: str,
    pdf: Path,
    config: IngestConfig,
    output: Path,
    worker: Execute,
) -> None:
    # Keep conversion subprocesses in this group so a timeout stops the whole document.
    os.setsid()
    # Stage diagnostics can contain document text. Only count-only JSON crosses this boundary.
    with open(os.devnull, "w") as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
        try:
            row = worker(entry, layer, pdf, config)
        except Exception as exc:
            row = {"id": entry["id"], "failure": failure_class(exc)}
        peak, fallback = peak_footprint_reading()
        convert_peak = row.get("stages", {}).get("convert", {}).get("peak_gb")
        row["peak_gb"] = max(peak, convert_peak or 0)
        row["memory"] = {
            "pipeline_peak_gb": peak,
            "pipeline_rss_fallback": fallback,
            "convert_peak_gb": convert_peak,
            "basis": "maximum of separate pipeline and conversion process peaks",
        }
        _write(output, row)


def document_in_child(
    entry: dict[str, Any],
    layer: str,
    pdf: Path,
    config: IngestConfig,
    *,
    worker: Execute = pipeline_document,
) -> dict[str, Any]:
    """A fresh process for every complete pipeline, plus the existing conversion child."""
    config.artifact_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=config.artifact_root) as folder:
        output = Path(folder) / "row.json"
        process = multiprocessing.get_context("spawn").Process(
            target=_child, args=(entry, layer, pdf, config, output, worker)
        )
        started = time.perf_counter()
        process.start()
        try:
            process.join(config.child_timeout_s)
            if process.is_alive():
                return {
                    "id": entry["id"],
                    "failure": "pipeline_timeout",
                    "wall_s": round(time.perf_counter() - started, 3),
                    "peak_gb": None,
                }
            if process.exitcode != 0 or not output.is_file():
                return {
                    "id": entry["id"],
                    "failure": "pipeline_child_crashed",
                    "child_exitcode": process.exitcode,
                    "wall_s": round(time.perf_counter() - started, 3),
                    "peak_gb": None,
                }
            row: dict[str, Any] = json.loads(output.read_text())
            row["wall_s"] = round(time.perf_counter() - started, 3)
            return row
        finally:
            # Also kill surviving conversion descendants after a wrapper crash. Before setsid
            # runs, the child PID is not a group id: fall back to killing that child alone.
            if process.pid is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    if process.is_alive():
                        process.kill()
            process.join()
            process.close()


def _outcome(row: Mapping[str, Any]) -> str:
    if row.get("failure") or row.get("critical_failure") or row.get("metrics_failure"):
        return "failed"
    industry = row.get("industry") or {}
    if industry.get("outcome") == "declined":
        return "declined"
    statements = row.get("statements", {})
    if (
        industry.get("outcome") == "needs_review"
        or not statements
        or any(s.get("review") != "passed" for s in statements.values())
    ):
        return "held_for_review"
    return "completed"


def summarize(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Observed outcomes and checks only: no truth judgement without reference answers."""
    outcomes = Counter(_outcome(r) for r in rows)
    identity = Counter(
        "passed"
        if r.get("identity_outcome") == "ok" and not r.get("failure")
        else "held_with_explanation"
        if r.get("identity_outcome") == "failed_accepted" and not r.get("failure")
        else "not_met"
        for r in rows
    )
    timing = {}
    for layer in sorted({r["text_layer"] for r in rows}):
        mine = [r for r in rows if r["text_layer"] == layer]
        values = [r["wall_s"] for r in mine if r.get("wall_s") is not None]
        timing[layer] = {
            "documents": len(mine),
            "budget_s": budget_s(layer),
            "wall_s": values,
            "within_budget": sum(v <= budget_s(layer) for v in values),
            "not_measured": len(mine) - len(values),
        }
    peaks = [r["peak_gb"] for r in rows if r.get("peak_gb") is not None]
    return {
        "documents": len(rows),
        "outcomes": {
            key: outcomes[key] for key in ("completed", "held_for_review", "declined", "failed")
        },
        "identity": {key: identity[key] for key in ("passed", "held_with_explanation", "not_met")},
        "critical_items": {
            state: sum((r.get("critical") or {}).get(state, 0) for r in rows)
            for state in SLOT_STATES
        },
        "critical_not_assessed_documents": sum(r.get("critical") is None for r in rows),
        "failure_classes": dict(
            Counter(
                r[key]
                for r in rows
                for key in ("failure", "critical_failure", "metrics_failure")
                if r.get(key)
            )
        ),
        "time_by_text_layer": timing,
        "peak_gb": max(peaks) if peaks else None,
        "memory_not_measured_documents": len(rows) - len(peaks),
        "accuracy": NO_REFERENCE,
    }


def _projection(
    root: Path, pool: str, selected: list[dict[str, Any]], fetched: dict[str, Any]
) -> dict[str, Any]:
    reports = sorted((root / "var/eval/dry_run").glob("*.json"))
    sample = json.loads(reports[-1].read_text())["rows"] if reports else []
    report = projection(sample, pool_layers(selected, fetched))
    report["source"] = str(reports[-1]) if reports else None
    projected = report["pools"][pool]
    print(
        f"{pool}: {len(selected)} documents; projected runtime (holdout median/p90): "
        f"{projected['minutes']}; documents without projection: "
        f"{projected['documents_not_projected']} (no estimate for those documents)",
        flush=True,
    )
    return report


def run_pool(
    pool: str,
    *,
    root: Path = REPO_ROOT,
    config: IngestConfig,
    continuing: bool = False,
    execute: Execute = document_in_child,
) -> dict[str, Any]:
    documents, fetched, reserve = load_metadata(root)
    selected = select_documents(pool, documents, fetched, reserve)
    revision = records.commit(root)
    document_hash = _digest([{**d, "measured": fetched[d["id"]]} for d in selected])
    config_hash = _digest(config.model_dump(mode="json"))
    candidate = f"{revision}:documents={document_hash}:config={config_hash}"
    step = f"{pool}_pipeline"
    out = root / "var/checkpoint" / pool
    out.mkdir(parents=True, exist_ok=True)
    # An exclusive marker prevents new looks. This nonblocking runtime lock also excludes
    # concurrent continuations of the same spent look, and releases when a process dies.
    with (out / "run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise records.CheckpointError(f"{pool}: checkpoint runner is already active") from exc
        if continuing:
            records.resume(step, candidate=candidate, root=root)
        else:
            records.begin(step, candidate=candidate, root=root)
        projected = _projection(root, pool, selected, fetched)
        # Hashing is the first access to PDF bytes, after the checkpoint has been logged.
        for entry in selected:
            pdf = root / "var/corpus" / pool / f"{entry['id']}.pdf"
            if not pdf.is_file():
                raise records.CheckpointError(f"{entry['id']}: missing PDF {pdf}")
            if sha256_file(pdf) != fetched[entry["id"]]["sha256"]:
                raise records.CheckpointError(f"{entry['id']}: PDF sha256 mismatch")
        rows: list[dict[str, Any]] = []
        for entry in selected:
            doc_id = entry["id"]
            stored = out / "documents" / f"{doc_id}.json"
            if stored.exists():
                saved = json.loads(stored.read_text())
                if (
                    not continuing
                    or saved.get("candidate") != candidate
                    or saved.get("commit") != revision
                ):
                    raise records.CheckpointError(f"{doc_id}: result candidate or commit mismatch")
                rows.append(saved["row"])
                continue
            layer = fetched[doc_id]["text_layer"]
            artifacts = out / "artifacts" / doc_id
            if artifacts.exists():
                shutil.rmtree(artifacts)
            cfg = config.model_copy(update={"artifact_root": artifacts})
            started = time.perf_counter()
            try:
                row = execute(entry, layer, root / "var/corpus" / pool / f"{doc_id}.pdf", cfg)
            except Exception as exc:
                row = {"failure": failure_class(exc)}
            finally:
                if artifacts.exists():
                    shutil.rmtree(artifacts)
            row.update(
                {
                    "id": doc_id,
                    "period": entry["period"],
                    "language": entry["language"],
                    "text_layer": layer,
                }
            )
            row.setdefault("wall_s", round(time.perf_counter() - started, 3))
            row.setdefault("peak_gb", None)
            _write(stored, {"candidate": candidate, "commit": revision, "row": row})
            rows.append(row)
        report = {
            "pool": pool,
            "candidate": candidate,
            "commit": revision,
            "document_list_sha256": document_hash,
            "configuration_sha256": config_hash,
            "projection": projected,
            **summarize(rows),
            "by_period": {
                period: summarize([r for r in rows if r["period"] == period]) for period in STRATA
            },
            "rows": rows,
        }
        summary = out / "summary.json"
        _write(summary, report)
        records.finish(step, result=summary, root=root)
        return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m harness.checkpoint_run")
    parser.add_argument("pool", choices=("model_test", "blind"))
    parser.add_argument("--checkpoint", action="store_true")
    parser.add_argument("--continue", action="store_true", dest="continuing")
    args = parser.parse_args(argv)
    if not args.checkpoint:
        print("refused: --checkpoint is required; no protected inputs read", file=sys.stderr)
        return 3
    try:
        run_pool(args.pool, config=load_config(), continuing=args.continuing)
    except (records.CheckpointError, OSError, ValueError, KeyError) as exc:
        print(f"checkpoint refused: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
