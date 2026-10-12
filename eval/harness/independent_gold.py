"""Human PDF readings with explicit independent/assisted methods, frozen before scoring.

Assisted drafts are extraction-seeded; double-PDF readings are transcribed independently.
Reader identity/source declarations are assertions,
not proof that a reader was independent; the annotation procedure supplies that evidence.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fra_core.taxonomy.loader import load_taxonomy
from fra_ingest.results import StructureResult
from harness.expected import ExpectedRow, ExpectedStatement
from harness.extraction import (
    DIGITAL,
    SCANNED,
    align_rows,
    mislabelled,
    score_statement,
    wilson_interval,
)
from harness.paths import STRATA


class GoldRow(ExpectedRow):
    canonical_id: str | None = Field(description="Independent taxonomy assignment; null if none")


class GoldStatement(ExpectedStatement):
    rows: list[GoldRow]  # type: ignore[assignment]


class Reading(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reader: str = Field(min_length=1)
    source: Literal["pdf_only", "pdf_reviewed"]
    complete: Literal[True]
    statements: list[GoldStatement] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_content(self) -> Reading:
        if not self.reader.strip():
            raise ValueError("reader identity must not be blank")
        if len({s.type for s in self.statements}) != len(self.statements):
            raise ValueError("one independently selected statement per type is required")
        taxonomy = load_taxonomy()
        for statement in self.statements:
            keys = {p.key for p in statement.periods}
            if not keys or len(keys) != len(statement.periods):
                raise ValueError("period keys must be present and unique")
            if not statement.pages or any(p < 1 for p in statement.pages):
                raise ValueError("source pages are one-based")
            if statement.scale <= 0 or not statement.currency.strip() or not statement.rows:
                raise ValueError("scale, currency and all printed rows are required")
            for row in statement.rows:
                if row.unconfirmed or set(row.values) != keys:
                    raise ValueError("each row must confirm every period, including printed blanks")
                if any(v is not None and not v.is_finite() for v in row.values.values()):
                    raise ValueError("figures must be finite decimals")
                if row.canonical_id is not None:
                    item = taxonomy.by_id(row.canonical_id)
                    if item is None or item.statement != statement.type:
                        raise ValueError("canonical id must belong to the statement taxonomy")
        return self


class FrozenGold(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    method: Literal["double_pdf", "assisted_review"] = "double_pdf"
    reference_scope: Literal["full_primary_statements", "reviewed_packet_slots"] = (
        "full_primary_statements"
    )
    readings: tuple[Reading, ...]

    @model_validator(mode="after")
    def independent_agreement(self) -> FrozenGold:
        if self.method == "assisted_review":
            if len(self.readings) != 1 or self.readings[0].source != "pdf_reviewed":
                raise ValueError("assisted gold requires one completed human PDF review")
            return self
        if len(self.readings) != 2 or any(r.source != "pdf_only" for r in self.readings):
            raise ValueError("double PDF gold requires two independent PDF readings")
        first, second = self.readings
        if first.reader.strip().casefold() == second.reader.strip().casefold():
            raise ValueError("two distinct readers are required")
        if first.model_dump(exclude={"reader"}) != second.model_dump(exclude={"reader"}):
            raise ValueError("readings disagree; resolve from the PDF before freezing")
        return self

    @property
    def document(self) -> Reading:
        return self.readings[0]


def load_gold(path: Path) -> FrozenGold:
    return FrozenGold.model_validate_json(path.read_text(encoding="utf-8"))


def freeze(first: Path, second: Path, output: Path) -> None:
    gold = FrozenGold(
        readings=(
            Reading.model_validate_json(first.read_text(encoding="utf-8")),
            Reading.model_validate_json(second.read_text(encoding="utf-8")),
        )
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    # Never silently replace frozen answers after seeing candidate results.
    with output.open("x", encoding="utf-8") as stream:
        stream.write(gold.model_dump_json(indent=2) + "\n")


def counts(gold: FrozenGold, result: StructureResult | None) -> list[dict[str, Any]]:
    """Counts only: no expected labels, values, row indices or disagreement details escape."""
    if result is not None and result.sha256 != gold.document.sha256:
        raise ValueError("candidate document hash differs from gold")
    found = {s.type: s for s in result.statements} if result is not None else {}
    duplicate = {t for t in found if result and sum(s.type == t for s in result.statements) > 1}
    figures: list[dict[str, Any]] = []
    for expected in gold.document.statements:
        # Ambiguous duplicate statements cannot select whichever scores best.
        statement = None if expected.type in duplicate else found.get(expected.type)
        scored = score_statement(expected, statement)
        items = statement.line_items if statement else []
        aligned = align_rows(expected.rows, items)
        # A missing expected blank row is still missing evidence, rather than a correct blank.
        for row, position in zip(expected.rows, aligned, strict=True):
            if statement is not None and position is None:
                scored.right -= sum(value is None for value in row.values.values())
        predictions = sum(item.canonical_id is not None for item in items)
        mapped = right = 0
        for row, position in zip(expected.rows, aligned, strict=True):
            if row.canonical_id is None:
                continue
            mapped += 1
            if position is not None:
                item = items[position]
                right += int(
                    item.canonical_id == row.canonical_id
                    and not mislabelled(row, item.raw_label, expected.rows)
                )
        figures.append(
            {
                "page_mode": expected.page_mode,
                "cells": scored.cells,
                "right": scored.right,
                "periods": scored.periods,
                "periods_right": scored.periods_right,
                "metadata": scored.metadata,
                "metadata_right": scored.metadata_right,
                "statements": 1,
                "statements_found": scored.statements_found,
                "extra_rows": scored.extra_rows,
                "mapping_predictions": predictions,
                "mapping_rows": mapped,
                "mapping_right": right,
            }
        )
    return figures


def aggregate_gold(rows: Sequence[Mapping[str, Any]], *, complete: bool = True) -> dict[str, Any]:
    strata: dict[str, Any] = {}
    for period in (*STRATA, "total"):
        mine = [r for r in rows if period == "total" or r["period"] == period]
        methods = {
            method: sum(
                "gold" in row and row.get("gold_method", "unknown") == method for row in mine
            )
            for method in ("double_pdf", "assisted_review", "unknown")
        }
        independent = methods["double_pdf"] > 0 and not (
            methods["assisted_review"] or methods["unknown"]
        )
        modes: dict[str, Any] = {}
        for mode, threshold in (("digital", DIGITAL), ("scanned", SCANNED)):
            figures = [f for row in mine for f in row.get("gold", []) if f["page_mode"] == mode]
            total = sum(f["cells"] for f in figures)
            right = sum(f["right"] for f in figures)
            modes[mode] = {
                "cells": total,
                "right": right,
                "accuracy": right / total if total and complete else None,
                "observed_accuracy": right / total if total else None,
                "wilson_95": list(wilson_interval(right, total)) if total else None,
                "threshold": threshold,
                "met": right / total >= threshold if total and complete and independent else None,
                "observed_threshold_met": right / total >= threshold if total else None,
            }
        all_figures = [f for row in mine for f in row.get("gold", [])]
        measures = {}
        for denominator, numerator in (
            ("periods", "periods_right"),
            ("metadata", "metadata_right"),
            ("statements", "statements_found"),
            ("mapping_rows", "mapping_right"),
            ("mapping_predictions", "mapping_right"),
        ):
            total = sum(f[denominator] for f in all_figures)
            right = sum(f[numerator] for f in all_figures)
            measures[denominator] = {
                "total": total,
                "right": right,
                "accuracy": right / total if total and complete else None,
                "observed_accuracy": right / total if total else None,
            }
        strata[period] = {
            "documents": len(mine),
            "labeled_documents": sum("gold" in r for r in mine),
            "methods": methods,
            "benchmark_kind": "independent_pdf" if independent else "assisted_or_unverified",
            "extra_rows": sum(f["extra_rows"] for f in all_figures),
            "figures": modes,
            "measures": measures,
        }
    return {
        "note": "Human-approved PDF labels; the gold method is recorded per input file. "
        "These numeric thresholds alone do not establish all of Gate A or Gate B.",
        "complete": complete,
        "measurement": "complete" if complete else "incomplete_not_a_gate_verdict",
        "strata": strata,
        "by_language": {
            language: aggregate_gold(
                [r for r in rows if r.get("language") == language], complete=complete
            )["strata"]
            for language in sorted({str(r["language"]) for r in rows if "language" in r})
        }
        if len({r.get("language") for r in rows}) > 1
        else {},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Freeze two independent PDF transcriptions")
    parser.add_argument("first", type=Path)
    parser.add_argument("second", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    freeze(args.first, args.second, args.output)
    print("Frozen gold written; no extraction or scoring run was made.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
